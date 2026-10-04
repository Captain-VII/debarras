"""Nettoyage guidé : emplacements connus, recréés automatiquement, mesurés sur le disque.

Débarras n'efface rien lui-même : les éléments choisis passent par la corbeille (actions
habituelles, avec confirmation et annulation). Ce qui demande des droits administrateur
(Windows Update, fichiers temporaires de Windows) est confié à l'outil de Windows.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

TRASH_KIND, WINDOWS_KIND, RECYCLE_KIND = "trash", "windows", "recycle"
SPLIT_SIZE = 4 * 1024 ** 3  # au-delà, un dossier est proposé élément par élément (voir _add)
TEMP_MIN_AGE = 24 * 3600   # fichiers temporaires plus récents : peut-être en cours d'utilisation

# Navigateurs Chromium : dossier « User Data » relatif à %LOCALAPPDATA%, processus.
_CHROMIUM = {
    "Google Chrome": (r"Google\Chrome\User Data", "chrome.exe"),
    "Microsoft Edge": (r"Microsoft\Edge\User Data", "msedge.exe"),
    "Brave": (r"BraveSoftware\Brave-Browser\User Data", "brave.exe"),
    "Vivaldi": (r"Vivaldi\User Data", "vivaldi.exe"),
    "Chromium": (r"Chromium\User Data", "chrome.exe"),
}
_CHROMIUM_PROFILE_CACHES = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
                            "DawnWebGPUCache", r"Service Worker\CacheStorage", r"Service Worker\ScriptCache")
_CHROMIUM_SHARED_CACHES = ("ShaderCache", "GrShaderCache", "GraphiteDawnCache")


@dataclass
class Target:
    key: str
    title: str
    description: str
    kind: str = TRASH_KIND
    paths: list[str] = field(default_factory=list)   # éléments à mettre à la corbeille
    sizes: dict[str, int] = field(default_factory=dict)
    count: int = 0                                     # nombre de fichiers
    note: str = ""                                     # avertissement (navigateur ouvert…)
    recommended: bool = True                           # coché par défaut
    size_known: bool = True

    @property
    def size(self) -> int:
        return sum(self.sizes.values())


def measure(path: str, cancelled: Callable[[], bool] = lambda: False) -> tuple[int, int, float]:
    """(octets, fichiers, date de modification la plus récente) d'un fichier ou dossier."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0, 0, 0.0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size, 1, st.st_mtime
    total, count, newest = 0, 0, st.st_mtime
    stack = [path]
    while stack:
        if cancelled():
            break
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.is_junction():  # jonctions : déjà comptées ailleurs
                                stack.append(e.path)
                            continue
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += s.st_size
                    count += 1
                    newest = max(newest, s.st_mtime)
        except OSError:
            continue
    return total, count, newest


def running_processes() -> set[str]:
    """Noms des exécutables en cours (minuscules)."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}


def _add(t: Target, path: str, cancelled) -> None:
    size, count, _ = measure(path, cancelled)
    if not count:
        return
    if size > SPLIT_SIZE and os.path.isdir(path):
        # Trop gros pour un seul élément de corbeille : Windows le refuse (erreur 161)
        # plutôt que de le supprimer définitivement. On propose son contenu à la place.
        try:
            children = [e.path for e in os.scandir(path)]
        except OSError:
            children = []
        for child in children:
            _add(t, child, cancelled)
        return
    t.paths.append(path)
    t.sizes[path] = size
    t.count += count


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def temp_target(cancelled=lambda: False, now: float | None = None) -> Target:
    temp = os.environ.get("TEMP") or str(_local() / "Temp")
    t = Target("temp", "Fichiers temporaires",
               f"Laissés par les installations et les logiciels dans {temp}. Seuls les éléments "
               "inchangés depuis plus de 24 h sont proposés (les autres peuvent être en cours d'utilisation).")
    limit = (now or time.time()) - TEMP_MIN_AGE
    try:
        entries = list(os.scandir(temp))
    except OSError:
        return t
    for e in entries:
        if cancelled():
            break
        size, count, newest = measure(e.path, cancelled)
        if newest and newest < limit and (count or e.is_dir()):
            t.paths.append(e.path)
            t.sizes[e.path] = size
            t.count += max(count, 1)
    return t


def browser_targets(cancelled=lambda: False, running: set[str] | None = None) -> list[Target]:
    running = running if running is not None else running_processes()
    out: list[Target] = []
    local = _local()
    for name, (rel, exe) in _CHROMIUM.items():
        root = local / rel
        if not root.is_dir():
            continue
        t = Target(f"browser:{name}", f"Cache de {name}",
                   "Pages et images gardées pour accélérer la navigation. Vos favoris, mots de passe, "
                   "historique et sessions ne sont pas touchés.")
        profiles = [p for p in root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile ")
                                                              or p.name in ("Guest Profile", "System Profile"))]
        for prof in profiles:
            for sub in _CHROMIUM_PROFILE_CACHES:
                if (prof / sub).is_dir():
                    _add(t, str(prof / sub), cancelled)
        for sub in _CHROMIUM_SHARED_CACHES:
            if (root / sub).is_dir():
                _add(t, str(root / sub), cancelled)
        _browser_note(t, name, exe, running)
        out.append(t)
    ff = local / "Mozilla" / "Firefox" / "Profiles"
    if ff.is_dir():
        t = Target("browser:Firefox", "Cache de Firefox",
                   "Pages et images gardées pour accélérer la navigation. Vos favoris, mots de passe, "
                   "historique et sessions ne sont pas touchés.")
        for prof in ff.iterdir():
            for sub in ("cache2", "startupCache", "jumpListCache"):
                if (prof / sub).is_dir():
                    _add(t, str(prof / sub), cancelled)
        _browser_note(t, "Firefox", "firefox.exe", running)
        out.append(t)
    opera = local / "Opera Software"
    if opera.is_dir():
        t = Target("browser:Opera", "Cache d'Opera", "Pages et images gardées pour accélérer la navigation.")
        for prof in opera.iterdir():
            for sub in ("Cache", "Code Cache", "GPUCache"):
                if (prof / sub).is_dir():
                    _add(t, str(prof / sub), cancelled)
        _browser_note(t, "Opera", "opera.exe", running)
        out.append(t)
    return out


def _browser_note(t: Target, name: str, exe: str, running: set[str]) -> None:
    if exe in running:
        t.note = (f"{name} est ouvert : fermez-le pour un nettoyage complet "
                  "(les fichiers en cours d'utilisation seront ignorés).")
        if exe == "msedge.exe":
            t.note += " Edge tourne parfois en arrière-plan même fenêtre fermée."
        t.recommended = False


def reports_target(cancelled=lambda: False) -> Target:
    local = _local()
    t = Target("reports", "Rapports d'erreurs et vidages mémoire",
               "Copies de la mémoire d'un logiciel au moment d'un plantage, et rapports d'erreurs "
               "déjà envoyés à Microsoft. Utiles seulement pour un dépannage en cours.")
    for rel in ("CrashDumps", r"Microsoft\Windows\WER\ReportArchive", r"Microsoft\Windows\WER\ReportQueue"):
        p = local / rel
        if p.is_dir():
            _add(t, str(p), cancelled)
    return t


def shaders_target(cancelled=lambda: False) -> Target:
    local = _local()
    t = Target("shaders", "Cache des shaders (carte graphique)",
               "Effets graphiques précalculés par DirectX et le pilote de la carte graphique. Recréés "
               "automatiquement : les jeux seront un peu plus lents (saccades) à leurs premiers lancements.",
               recommended=False)
    for rel in ("D3DSCache", r"NVIDIA\DXCache", r"NVIDIA\GLCache", r"AMD\DxCache", r"AMD\DxcCache", r"AMD\GLCache",
                r"Intel\ShaderCache"):
        p = local / rel
        if p.is_dir():
            _add(t, str(p), cancelled)
    if t.size > 10 * 1024 ** 3:
        t.note = ("Cache inhabituellement gros : sa taille maximale se règle dans le panneau de configuration "
                  "du pilote (NVIDIA : « Taille du cache du shader »).")
    return t


def windows_update_target(cancelled=lambda: False) -> Target:
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    t = Target("windows", "Windows Update et fichiers temporaires de Windows",
               "Mises à jour déjà installées et fichiers temporaires du système. Ils demandent des droits "
               "administrateur : Débarras ouvre le « Nettoyage de disque » de Windows, puis cliquez sur "
               "« Nettoyer les fichiers système ».", kind=WINDOWS_KIND, recommended=False)
    size, count, _ = measure(os.path.join(windir, "SoftwareDistribution", "Download"), cancelled)
    s2, c2, _ = measure(os.path.join(windir, "Temp"), cancelled)
    t.sizes["windows"] = size + s2
    t.count = count + c2
    t.size_known = bool(count or c2)
    return t


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


def recycle_bin_size() -> tuple[int, int]:
    """(octets, éléments) dans la corbeille, tous lecteurs."""
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return int(info.i64Size), int(info.i64NumItems)
    except (AttributeError, OSError):
        pass
    return 0, 0


def recycle_target() -> Target:
    size, n = recycle_bin_size()
    t = Target("recycle", "Corbeille",
               "Tout ce que Débarras supprime y passe d'abord. Vérifiez-la, puis videz-la depuis Windows "
               "pour libérer réellement l'espace (Débarras ne la vide jamais).", kind=RECYCLE_KIND,
               recommended=False)
    t.sizes["recycle"] = size
    t.count = n
    return t


def open_windows_cleanup() -> None:
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    subprocess.Popen([os.path.join(windir, "System32", "cleanmgr.exe")])


def open_recycle_bin() -> None:
    subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])


def analyze(cancelled: Callable[[], bool] = lambda: False) -> list[Target]:
    targets = [temp_target(cancelled), *browser_targets(cancelled), reports_target(cancelled),
               shaders_target(cancelled)]
    targets = [t for t in targets if t.paths or t.key.startswith("browser:")]
    return targets + [windows_update_target(cancelled), recycle_target()]


class CleanupWorker(QThread):
    done = Signal(object)   # list[Target]
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.done.emit(analyze(self.isInterruptionRequested))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Analyse impossible : {exc!r}")
