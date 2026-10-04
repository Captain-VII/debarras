"""Niveau de risque d'un fichier ou dossier : peut-on le supprimer sans casser Windows ou un logiciel ?

Quatre niveaux, du plus au moins risqué :
  SYSTEM    — indispensable à Windows : toute action est refusée ;
  SOFTWARE  — fait partie d'un logiciel, d'un jeu ou de ses réglages : déconseillé ;
  PERSONAL  — vos fichiers : sans risque pour l'ordinateur, à vous de juger ;
  CLEANABLE — caches et fichiers temporaires recréés automatiquement : sans risque.

Les règles sont d'abord des chemins connus (dossiers Windows, AppData, caches…). S'y ajoutent
les « dossiers de logiciel » trouvés dans le scan : un dossier contenant des .dll est un
programme installé, tout ce qu'il contient en fait partie.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

SYSTEM, SOFTWARE, PERSONAL, CLEANABLE = "system", "software", "personal", "cleanable"
LEVELS = (SYSTEM, SOFTWARE, PERSONAL, CLEANABLE)
LABELS = {SYSTEM: "Système", SOFTWARE: "Logiciel", PERSONAL: "Vos fichiers", CLEANABLE: "Nettoyable"}
HINTS = {
    SYSTEM: "Indispensable à Windows : Débarras refuse d'y toucher.",
    SOFTWARE: "Fait partie d'un logiciel ou de ses réglages : le supprimer peut le casser. "
              "Désinstallez plutôt le logiciel (Paramètres › Applications).",
    PERSONAL: "Aucun risque pour Windows ni vos logiciels : vérifiez seulement que vous n'en avez plus besoin.",
    CLEANABLE: "Cache ou fichier temporaire, recréé automatiquement si besoin : supprimable sans risque.",
}
# Clair / sombre, même ordre que LEVELS (rouge, orange, vert, bleu — lisibles en daltonisme courant).
_COLORS_LIGHT = ("#d03b3b", "#e08a00", "#2e9d5b", "#2a78d6")
_COLORS_DARK = ("#e5534b", "#d98a1c", "#2fa866", "#3987e5")


def level_colors(dark: bool) -> dict[str, str]:
    return dict(zip(LEVELS, _COLORS_DARK if dark else _COLORS_LIGHT))


@dataclass(frozen=True, slots=True)
class Verdict:
    level: str
    reason: str

    @property
    def label(self) -> str:
        return LABELS[self.level]

    @property
    def blocked(self) -> bool:
        return self.level == SYSTEM


# Fichiers et dossiers système présents à la racine d'un lecteur.
_ROOT_SYSTEM_FILES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log", "dumpstack.log.tmp",
                      "bootmgr", "bootnxt", "bootsect.bak"}
_ROOT_SYSTEM_DIRS = {"system volume information", "recovery", "boot", "efi", "$recycle.bin", "$winreagent",
                     "$windows.~bt", "$windows.~ws", "$sysreset", "$getcurrent", "config.msi",
                     "documents and settings", "msocache", "onedrivetemp"}
_PROFILE_SYSTEM = ("ntuser.", "usrclass.dat")  # registre de l'utilisateur
_USERS_SYSTEM = {"default", "default user", "all users"}

# Bibliothèques de jeux : à désinstaller depuis le lanceur, pas à la main.
_GAME_DIRS = {"steamapps": "Steam", "steamlibrary": "Steam", "epic games": "Epic Games",
              "gog games": "GOG", "gog galaxy": "GOG", "xboxgames": "Xbox", "riot games": "Riot",
              "ubisoft game launcher": "Ubisoft Connect", "ea games": "EA", "battle.net": "Battle.net"}
# Dossiers de cache reconnus par leur nom (dans AppData, ou n'importe où pour ceux de développement).
_APP_CACHE_NAMES = {"cache", "caches", "cache2", "code cache", "gpucache", "grshadercache", "shadercache",
                    "dawncache", "dawngraphitecache", "dawnwebgpucache", "crashpad", "crashdumps",
                    "d3dscache", "inetcache", "webcache", "service worker"}
_DEV_CLEANABLE = {"__pycache__": "Cache Python, recréé automatiquement",
                  ".pytest_cache": "Cache de tests, recréé automatiquement",
                  ".mypy_cache": "Cache d'analyse, recréé automatiquement",
                  ".ruff_cache": "Cache d'analyse, recréé automatiquement",
                  "node_modules": "Dépendances JavaScript, re-téléchargées par « npm install »"}
_INSTALLERS = {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle"}
_GIT_LIKE = {".git", ".hg", ".svn"}
_VENVS = {".venv", "venv", ".tox", ".conda"}


def _n(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).rstrip("\\")


def _under(p: str, root: str) -> bool:
    return bool(root) and (p == root or p.startswith(root + "\\"))


class Classifier:
    """Classe des chemins. `software_dirs` : dossiers de logiciels trouvés dans le scan."""

    def __init__(self, software_dirs: set[str] | None = None, app_dir: str | None = None,
                 containers: set[str] | None = None) -> None:
        env = os.environ
        home = Path.home()
        self.system_roots = [(_n(x), x) for x in (
            env.get("SystemRoot", r"C:\Windows"), env.get("ProgramFiles", r"C:\Program Files"),
            env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), env.get("ProgramW6432", ""),
            env.get("ProgramData", r"C:\ProgramData")) if x]
        self.home = _n(home)
        self.users = _n(home.parent)
        local = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        self.appdata = _n(Path(local).parent) if Path(local).name.lower() == "local" else _n(home / "AppData")
        self.local = _n(local)
        self.temp = _n(env.get("TEMP") or Path(local) / "Temp")
        self.downloads = _n(home / "Downloads")
        self.local_programs = _n(Path(local) / "Programs")
        self.app_dir = _n(app_dir) if app_dir else ""
        self.software_dirs = {_n(d): d for d in software_dirs or ()}  # normalisé -> tel quel
        self.containers = {_n(d) for d in containers or ()}  # surtout occupés par un logiciel (eux seuls)
        self._memo: dict[tuple[str, bool], Verdict] = {}

    # --- règles ------------------------------------------------------------------------

    def classify(self, path: str, is_dir: bool | None = None) -> Verdict:
        if is_dir is None:
            is_dir = os.path.isdir(path)
        key = (path, is_dir)
        v = self._memo.get(key)
        if v is None:
            v = self._memo[key] = self._classify(_n(path), is_dir)
        return v

    def _classify(self, p: str, is_dir: bool) -> Verdict:
        drive, rest = os.path.splitdrive(p)
        parts = [x for x in rest.split("\\") if x]
        name = parts[-1] if parts else ""

        # 1. Système
        if not parts:
            return Verdict(SYSTEM, "Racine du lecteur")
        for root, shown in self.system_roots:
            if _under(p, root):
                return Verdict(SYSTEM, f"Dossier de Windows ou des programmes installés ({shown})")
        if len(parts) == 1 and (name in _ROOT_SYSTEM_DIRS or (not is_dir and name in _ROOT_SYSTEM_FILES)):
            return Verdict(SYSTEM, "Fichier ou dossier système à la racine du lecteur")
        if "$recycle.bin" in parts:
            return Verdict(SYSTEM, "Corbeille : videz-la depuis Windows")
        if p in (self.home, self.users):
            return Verdict(SYSTEM, "Dossier des comptes utilisateurs")
        if _under(p, self.users) and len(p) > len(self.users):
            first = p[len(self.users) + 1:].split("\\")[0]
            if first in _USERS_SYSTEM:
                return Verdict(SYSTEM, "Profil modèle de Windows")
        if os.path.dirname(p) == self.home and name.startswith(_PROFILE_SYSTEM):
            return Verdict(SYSTEM, "Registre de votre compte Windows")

        # 2. Nettoyable à coup sûr (avant AppData, qui les contient)
        if _under(p, self.temp):
            return Verdict(CLEANABLE, "Fichiers temporaires de Windows et des logiciels")
        if self.app_dir and _under(p, self.app_dir):
            return Verdict(SOFTWARE, "Débarras lui-même")
        if _under(p, self.appdata):
            inner = p[len(self.appdata) + 1:].split("\\") if p != self.appdata else []
            caches = [x for x in inner if x in _APP_CACHE_NAMES]
            if caches:
                return Verdict(CLEANABLE, f"Cache d'application ({caches[-1]}), recréé automatiquement")
            if _under(p, self.local_programs):
                return Verdict(SOFTWARE, "Logiciel installé pour votre compte")
            return Verdict(SOFTWARE, "AppData : réglages et données des logiciels")

        # 3. Logiciels, jeux, outils
        for part in parts:
            if part in _GAME_DIRS:
                return Verdict(SOFTWARE, f"Bibliothèque de jeux {_GAME_DIRS[part]} : désinstallez depuis le lanceur")
            if part in _GIT_LIKE:
                return Verdict(SOFTWARE, "Historique d'un projet (dépôt Git) : le supprimer perd les versions")
            if part in _VENVS:
                return Verdict(SOFTWARE, "Environnement Python d'un projet (re-créable, mais le projet ne "
                                         "fonctionnera plus d'ici là)")
            if part in _DEV_CLEANABLE and not self._in_software(p):
                return Verdict(CLEANABLE, _DEV_CLEANABLE[part])
        if self._in_software(p):
            return Verdict(SOFTWARE, "Fait partie d'un logiciel installé (bibliothèques .dll)")
        if p in self.containers:
            return Verdict(SOFTWARE, "Contient surtout un logiciel ou un jeu installé")
        if _under(p, self.home) and p != self.home:
            first = p[len(self.home) + 1:].split("\\")[0]
            if first == ".cache":
                return Verdict(CLEANABLE, "Cache d'outils, recréé automatiquement")
            if first.startswith("."):
                return Verdict(SOFTWARE, f"Réglages ou données d'un outil ({first})")

        # 4. Fichiers isolés reconnaissables
        if not is_dir:
            ext = os.path.splitext(name)[1]
            if name == "thumbs.db":
                return Verdict(CLEANABLE, "Miniatures de l'Explorateur, recréées automatiquement")
            if ext in _INSTALLERS and _under(p, self.downloads):
                return Verdict(CLEANABLE, "Installeur téléchargé : le logiciel reste installé sans lui")

        # 5. Données personnelles
        return Verdict(PERSONAL, "Vos fichiers")

    def _in_software(self, p: str) -> bool:
        if not self.software_dirs:
            return False
        q = p
        while True:
            if q in self.software_dirs:
                return True
            parent = os.path.dirname(q)
            if parent == q or len(parent) <= 3:
                return q in self.software_dirs
            q = parent

    # --- contenu à risque d'un dossier --------------------------------------------------

    def risky_inside(self, folder: str, limit: int = 5) -> list[str]:
        """Logiciels repérés à l'intérieur de `folder` (pour avertir avant de le supprimer)."""
        f = _n(folder)
        found = sorted(d for d in self.software_dirs if d != f and d.startswith(f + "\\"))
        tops: list[str] = []
        for d in found:  # on ne garde que les plus hauts
            if not any(d.startswith(t + "\\") for t in tops):
                tops.append(d)
        return [self.software_dirs[d] for d in tops[:limit]]


# --- instance partagée --------------------------------------------------------------------

_current = Classifier()


def current() -> Classifier:
    """Classificateur actif (mis à jour quand un scan est chargé)."""
    return _current


def set_current(c: Classifier) -> None:
    global _current
    _current = c


CONTAINER_SHARE = 0.5   # un dossier occupé à 50 % ou plus par un logiciel est un dossier de logiciel


def software_dirs(conn: sqlite3.Connection, root: str) -> set[str]:
    """Dossiers du scan contenant des .dll (programmes installés, portables ou jeux)."""
    from core.cache import subtree
    cond, args = subtree("d.path", root)
    rows = conn.execute(
        f"SELECT DISTINCT d.path FROM files f JOIN dirpaths d ON d.id = f.dir_id "
        f"WHERE f.ext = '.dll' AND {cond}", args)
    out = {r[0] for r in rows}
    # Un logiciel range souvent ses .dll dans un sous-dossier (bin, lib…) : on remonte d'un cran
    # quand le dossier parent contient l'exécutable.
    exe_dirs = {r[0] for r in conn.execute(
        f"SELECT DISTINCT d.path FROM files f JOIN dirpaths d ON d.id = f.dir_id "
        f"WHERE f.ext = '.exe' AND {cond}", args)}
    for d in list(out):
        parent = os.path.dirname(d)
        if parent in exe_dirs and _n(parent) != _n(root) and len(parent) > 3:
            out.add(parent)
    return out


def containers(conn: sqlite3.Connection, scan_id: int, roots: set[str], scan_root: str = "") -> set[str]:
    """Dossiers surtout occupés par un logiciel (ex. StarCitizen autour de son dossier LIVE) :
    on remonte depuis chaque logiciel tant qu'il représente l'essentiel du dossier parent.
    Seul le dossier lui-même est concerné, pas ses autres sous-dossiers.
    On s'arrête au profil utilisateur et à ses dossiers (Documents…) et à la racine du lecteur."""
    home = _n(Path.home())
    sizes: dict[str, int] = {}

    def size(path: str) -> int:
        if path not in sizes:
            row = conn.execute("SELECT size FROM dir_sizes WHERE scan_id=? AND path=?", (scan_id, path)).fetchone()
            sizes[path] = row[0] if row else 0
        return sizes[path]

    out: set[str] = set()
    for d in roots:
        cur, own = d, size(d)
        while True:
            parent = os.path.dirname(cur)
            np = _n(parent)
            if (parent == cur or len(np) <= 2 or np == home or os.path.dirname(np) == home or _under(home, np)
                    or np == _n(scan_root)):
                break
            total = size(parent)
            if not total or own < CONTAINER_SHARE * total:
                break
            out.add(parent)
            cur = parent
    return out


@dataclass
class SafetySummary:
    scan_id: int
    totals: dict[str, int]          # octets par niveau
    top: dict[str, list[tuple[str, int, str]]]  # niveau -> [(chemin, taille, raison)] les plus gros
    software: set[str]


def summarize(conn: sqlite3.Connection, scan_id: int, root: str, clf: Classifier,
              cancelled=lambda: False) -> SafetySummary:
    """Répartition de l'espace par niveau. Chaque dossier compte ses fichiers directs ;
    les plus gros éléments « homogènes » de chaque niveau sont gardés pour la synthèse."""
    rows = conn.execute("SELECT path, parent, size FROM dir_sizes WHERE scan_id=?", (scan_id,)).fetchall()
    child_sum: dict[str, int] = {}
    for path, parent, size in rows:
        if parent is not None:
            child_sum[parent] = child_sum.get(parent, 0) + size
    totals = dict.fromkeys(LEVELS, 0)
    top: dict[str, list[tuple[str, int, str]]] = {lv: [] for lv in LEVELS}
    for i, (path, parent, size) in enumerate(rows):
        if i % 5000 == 0 and cancelled():
            break
        v = clf.classify(path, True)
        totals[v.level] += max(size - child_sum.get(path, 0), 0)
        # « Élément représentatif » : premier dossier de la branche à changer de niveau.
        if parent is None or clf.classify(parent, True).level != v.level:
            top[v.level].append((path, size, v.reason))
    # Installeurs de Téléchargements : comptés comme nettoyables.
    from core.cache import subtree
    if _under(_n(root), clf.downloads) or _under(clf.downloads, _n(root)):
        cond, args = subtree("d.path", str(Path.home() / "Downloads"))
        exts = tuple(_INSTALLERS)
        n = conn.execute(f"SELECT COALESCE(SUM(f.size), 0) FROM files f JOIN dirpaths d ON d.id = f.dir_id "
                         f"WHERE {cond} AND f.ext IN ({','.join('?' * len(exts))})", (*args, *exts)).fetchone()[0]
        n = min(n, totals[PERSONAL])
        totals[PERSONAL] -= n
        totals[CLEANABLE] += n
    for lv in LEVELS:
        top[lv].sort(key=lambda t: t[1], reverse=True)
        # Un élément contenu dans un autre de la liste n'apporte rien.
        kept: list[tuple[str, int, str]] = []
        for t in top[lv]:
            if not any(_under(_n(t[0]), _n(k[0])) for k in kept):
                kept.append(t)
            if len(kept) >= 8:
                break
        top[lv] = kept
    return SafetySummary(scan_id, totals, top, set(clf.software_dirs.values()))


class SafetyWorker(QThread):
    """Repère les logiciels du scan et calcule la répartition par niveau (arrière-plan)."""

    done = Signal(object)    # (Classifier, SafetySummary)
    failed = Signal(str)

    def __init__(self, db_path: str | Path, scan_id: int, root: str, app_dir: str | None, parent=None) -> None:
        super().__init__(parent)
        self.db_path, self.scan_id, self.root, self.app_dir = db_path, scan_id, root, app_dir

    def run(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                roots = software_dirs(conn, self.root)
                clf = Classifier(roots, self.app_dir, containers(conn, self.scan_id, roots, self.root))
                summary = summarize(conn, self.scan_id, self.root, clf, self.isInterruptionRequested)
            finally:
                conn.close()
            self.done.emit((clf, summary))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Analyse de sécurité impossible : {exc!r}")
