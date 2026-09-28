"""Mise à jour automatique via les releases GitHub publiques.

1. Vérification : GET /repos/<dépôt>/releases/latest (aucune donnée envoyée hormis le
   User-Agent). Une release est retenue si sa version est supérieure et qu'elle contient
   l'archive « Debarras-<version>-win64.zip ».
2. Téléchargement vérifié : taille et empreinte SHA-256 (champ `digest` de l'API GitHub,
   à défaut fichier « .sha256 » joint à la release).
3. Préparation : extraction dans un dossier temporaire (chemins malveillants refusés).
4. Remplacement : un script PowerShell attend la fermeture de Débarras, renomme l'ancien
   dossier en « <dossier>.old » (secours), met le nouveau à sa place et relance l'exe.
   En cas d'échec, l'ancien dossier est remis en place et relancé.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

from version import GITHUB_REPO, __version__

API = "https://api.github.com"
EXE_NAME = "Debarras.exe"
ASSET_RE = re.compile(r"^Debarras-(\d+(?:\.\d+)*)-win64\.zip$", re.IGNORECASE)
TIMEOUT = 15
CHUNK = 1 << 16


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str | None        # None : à récupérer dans le fichier .sha256 joint
    sha256_url: str | None = None


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.10.2' -> (1, 10, 2). Tout suffixe (-beta…) est ignoré."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        raise ValueError(f"version illisible : {text!r}")
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def _get(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"Debarras/{__version__}"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 - URL HTTPS construite ici


def fetch_latest(repo: str = GITHUB_REPO, api: str = API) -> Release | None:
    """Dernière release publiée (hors brouillons/préversions), ou None si pas d'archive Windows."""
    try:
        with _get(f"{api}/repos/{repo}/releases/latest") as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # dépôt sans release
        raise UpdateError(f"GitHub a répondu {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"GitHub injoignable : {exc}") from exc
    assets = {a["name"]: a for a in data.get("assets", [])}
    for name, a in assets.items():
        if ASSET_RE.match(name):
            digest = a.get("digest") or ""
            sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None
            side = assets.get(name + ".sha256")
            return Release(
                version=ASSET_RE.match(name).group(1), tag=data.get("tag_name", ""),
                notes=data.get("body") or "", page_url=data.get("html_url", ""),
                asset_name=name, asset_url=a["browser_download_url"], asset_size=int(a.get("size", 0)),
                sha256=sha, sha256_url=side["browser_download_url"] if side else None)
    return None


def download(rel: Release, dest: Path, progress: Callable[[int, int], None] = lambda d, t: None,
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    """Télécharge l'archive dans `dest` et vérifie taille + SHA-256. Renvoie le chemin du zip."""
    expected = rel.sha256
    if expected is None and rel.sha256_url:
        with _get(rel.sha256_url, "application/octet-stream") as resp:
            expected = resp.read(200).decode("ascii", "replace").split()[0].lower()
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("empreinte SHA-256 absente de la release : téléchargement refusé")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / rel.asset_name
    h = hashlib.sha256()
    done = 0
    try:
        with _get(rel.asset_url, "application/octet-stream") as resp, open(target, "wb") as out:
            total = rel.asset_size or int(resp.headers.get("Content-Length") or 0)
            while block := resp.read(CHUNK):
                if cancelled():
                    raise UpdateError("téléchargement interrompu")
                out.write(block)
                h.update(block)
                done += len(block)
                progress(done, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"téléchargement impossible : {exc}") from exc
    if rel.asset_size and done != rel.asset_size:
        raise UpdateError(f"taille inattendue ({done} octets au lieu de {rel.asset_size})")
    if h.hexdigest() != expected:
        raise UpdateError("empreinte SHA-256 incorrecte : fichier corrompu ou altéré")
    return target


def stage(zip_path: Path, dest: Path) -> Path:
    """Extrait l'archive dans `dest` et renvoie le dossier qui contient Debarras.exe."""
    if dest.exists():
        shutil.rmtree(dest)  # dossier de préparation à nous
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            target = (dest / member.filename).resolve()
            if not target.is_relative_to(root):
                raise UpdateError(f"archive refusée : chemin suspect {member.filename!r}")
        z.extractall(dest)
    exes = [p for p in dest.rglob(EXE_NAME) if p.is_file()]
    if len(exes) != 1 or len(exes[0].relative_to(dest).parts) > 2:
        raise UpdateError(f"archive inattendue : {EXE_NAME} introuvable à la racine")
    return exes[0].parent


def install_dir() -> Path | None:
    """Dossier de l'application installée (exe PyInstaller), None en développement."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else None


def can_self_update() -> bool:
    folder = install_dir()
    if folder is None:
        return False
    try:  # droits d'écriture sur le dossier parent (renommage du dossier de l'appli)
        with tempfile.TemporaryFile(dir=folder.parent):
            pass
        return True
    except OSError:
        return False


def _ps(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def write_swap_script(pid: int, install: Path, staged: Path, exe_name: str = EXE_NAME,
                      folder: Path | None = None, started: Path | None = None) -> Path:
    """Script PowerShell de remplacement (exécuté après la fermeture de Débarras).

    `started` : fichier témoin créé dès le démarrage du script ; Débarras attend de le voir
    avant de se fermer (sinon l'utilisateur se retrouverait sans application ni mise à jour).
    """
    backup = install.with_name(install.name + ".old")
    signal = f"New-Item -ItemType File -Force -Path {_ps(started)} | Out-Null\n" if started else ""
    script = f"""$ErrorActionPreference = 'Stop'
{signal}$install = {_ps(install)}; $staged = {_ps(staged)}; $backup = {_ps(backup)}; $exe = {_ps(exe_name)}
Set-Location ([IO.Path]::GetTempPath())
try {{ Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue }} catch {{}}
$moved = $false
try {{
    for ($i = 0; $i -lt 40 -and -not $moved; $i++) {{
        try {{
            if (Test-Path -LiteralPath $backup) {{ Remove-Item -LiteralPath $backup -Recurse -Force }}
            Rename-Item -LiteralPath $install -NewName (Split-Path $backup -Leaf)
            $moved = $true
        }} catch {{ Start-Sleep -Milliseconds 500 }}
    }}
    if (-not $moved) {{ throw 'dossier de Débarras verrouillé' }}
    Move-Item -LiteralPath $staged -Destination $install
    Start-Process -FilePath (Join-Path $install $exe)
}} catch {{
    if ($moved -and -not (Test-Path -LiteralPath $install)) {{
        Rename-Item -LiteralPath $backup -NewName (Split-Path $install -Leaf)
    }}
    Start-Process -FilePath (Join-Path $install $exe)
    exit 1
}}
"""
    folder = folder or Path(tempfile.gettempdir())
    path = folder / "debarras_update.ps1"
    path.write_text(script, encoding="utf-8-sig")  # BOM : accents lus correctement par PowerShell 5
    return path


def launch_swap(script: Path) -> None:
    """Lance le script en arrière-plan ; il survit à la fermeture de Débarras.

    Pas de DETACHED_PROCESS : sans console, PowerShell 5 s'arrête avant d'exécuter le script
    (constaté depuis l'exe sans console). CREATE_NO_WINDOW lui donne une console invisible.
    Entrées/sorties sur DEVNULL : un exe fenêtré n'a pas de descripteurs standard valides.
    """
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(script)], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_started(marker: Path, timeout: float = 15.0, tick: Callable[[], None] = lambda: None) -> bool:
    """Attend le fichier témoin du script de remplacement (tick : garder l'interface vivante)."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if marker.exists():
            return True
        tick()
        time.sleep(0.05)
    return marker.exists()


# --- threads ---------------------------------------------------------------------------------

class UpdateChecker(QThread):
    done = Signal(object)    # Release plus récente, ou None
    failed = Signal(str)

    def __init__(self, repo: str = GITHUB_REPO, api: str = API, parent=None) -> None:
        super().__init__(parent)
        self.repo, self.api = repo, api

    def run(self) -> None:
        try:
            rel = fetch_latest(self.repo, self.api)
            self.done.emit(rel if rel and is_newer(rel.version) else None)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UpdateDownloader(QThread):
    progress = Signal("qlonglong", "qlonglong")
    ready = Signal(object)   # dossier préparé (Path)
    failed = Signal(str)

    def __init__(self, rel: Release, work: Path, parent=None) -> None:
        super().__init__(parent)
        self.rel, self.work = rel, work

    def run(self) -> None:
        try:
            zip_path = download(self.rel, self.work, self.progress.emit, self.isInterruptionRequested)
            staged = stage(zip_path, self.work / f"Debarras-{self.rel.version}")
            zip_path.unlink(missing_ok=True)
            self.ready.emit(staged)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def work_dir() -> Path:
    """Dossier de préparation des mises à jour (à côté du cache)."""
    from core.cache import default_db_path
    return default_db_path().parent / "update"


def cleanup_after_update() -> None:
    """Au démarrage : supprime les restes d'une mise à jour terminée (préparation, script)."""
    shutil.rmtree(work_dir(), ignore_errors=True)
    Path(tempfile.gettempdir(), "debarras_update.ps1").unlink(missing_ok=True)
