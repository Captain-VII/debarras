"""Statistiques tirées du cache : types, gros fichiers, fichiers anciens, dossiers vides,
temp/caches, installeurs oubliés. Calcul hors thread UI via StatsWorker."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.cache import Cache, subtree
from utils.filetypes import CATEGORIES, category

TEMP_DIR_NAMES = frozenset({
    "temp", "tmp", ".tmp", "cache", "caches", ".cache", "__pycache__", "gpucache",
    "code cache", "shadercache", "dxcache", "glcache", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".sass-cache", ".parcel-cache", ".next", ".nuxt", ".gradle",
})
TEMP_EXTS = (".tmp", ".temp", ".dmp")
INSTALLER_EXTS = (".msi", ".msix", ".msixbundle", ".appx", ".appxbundle")
DOWNLOAD_DIRS = frozenset({"downloads", "téléchargements"})
_INSTALLER_NAME = re.compile(r"setup|install|x64|x86|win64|win32|amd64|[-_]win\b", re.IGNORECASE)
INSTALLER_MIN_AGE_DAYS = 30
DEFAULT_OLD_DAYS = 365
LIMIT = 500


@dataclass(frozen=True, slots=True)
class FileStat:
    path: str
    size: int
    mtime: float
    atime: float

    @property
    def last_used(self) -> float:
        """Accès souvent non mis à jour sous Windows : on prend le plus récent des deux."""
        return max(self.atime, self.mtime)


@dataclass(frozen=True, slots=True)
class DirStat:
    path: str
    size: int
    file_count: int


@dataclass
class StatsResult:
    scan_id: int
    root: str
    total_size: int = 0
    file_count: int = 0
    by_category: dict[str, tuple[int, int]] = field(default_factory=dict)  # cat -> (taille, nb)
    by_ext: list[tuple[str, int, int]] = field(default_factory=list)       # (ext, taille, nb)
    largest: list[FileStat] = field(default_factory=list)
    old_days: int = DEFAULT_OLD_DAYS
    old: list[FileStat] = field(default_factory=list)
    old_total: tuple[int, int] = (0, 0)                                      # (taille, nb)
    empty_dirs: list[str] = field(default_factory=list)
    temp_dirs: list[DirStat] = field(default_factory=list)
    temp_files: list[FileStat] = field(default_factory=list)
    installers: list[FileStat] = field(default_factory=list)


def _files(conn: sqlite3.Connection, root: str, where: str, args: tuple = (),
           order: str = "size DESC", limit: int = LIMIT) -> list[FileStat]:
    """Fichiers sous `root` vérifiant `where`."""
    scope, sargs = subtree("parent", root)
    sql = f"SELECT path, size, mtime, atime FROM file_paths WHERE {scope} AND {where}"
    sql += f" ORDER BY {order} LIMIT {int(limit)}"
    return [FileStat(*r) for r in conn.execute(sql, (*sargs, *args))]


def compute_stats(conn: sqlite3.Connection, scan_id: int, root: str,
                  old_days: int = DEFAULT_OLD_DAYS) -> StatsResult:
    res = StatsResult(scan_id, root, old_days=old_days)
    scope, sargs = subtree("path", root)
    in_root = f"dir_id IN (SELECT id FROM dirpaths WHERE {scope})"  # fichiers sous `root`
    now = time.time()

    row = conn.execute("SELECT size, file_count FROM dir_sizes WHERE scan_id=? AND path=?",
                       (scan_id, root)).fetchone()
    if row:
        res.total_size, res.file_count = row

    # Répartition par extension puis par catégorie.
    cats = {c: [0, 0] for c in CATEGORIES}
    for ext, size, count in conn.execute(
        f"SELECT ext, SUM(size), COUNT(*) FROM files WHERE {in_root} GROUP BY ext", sargs,
    ):
        res.by_ext.append((ext, size, count))
        c = cats[category(ext)]
        c[0] += size
        c[1] += count
    res.by_ext.sort(key=lambda e: e[1], reverse=True)
    res.by_category = {c: (s, n) for c, (s, n) in cats.items()}

    res.largest = _files(conn, root, "1")

    # Fichiers anciens : ni accédés ni modifiés depuis `old_days` jours.
    cutoff = now - old_days * 86400
    res.old = _files(conn, root, "MAX(atime, mtime) < ?", (cutoff,))
    res.old_total = conn.execute(
        f"SELECT COALESCE(SUM(size), 0), COUNT(*) FROM files WHERE {in_root} AND MAX(atime, mtime) < ?",
        (*sargs, cutoff),
    ).fetchone()

    # Dossiers vides : seulement le plus haut d'une branche vide, sans rien d'ignoré dedans.
    res.empty_dirs = [r[0] for r in conn.execute(
        "SELECT dp.path FROM dirs d JOIN dirs p ON p.scan_id = d.scan_id AND p.dir_id = d.parent_id "
        "JOIN dirpaths dp ON dp.id = d.dir_id "
        "WHERE d.scan_id = ? AND d.file_count = 0 AND d.skipped = 0 "
        "AND (p.file_count > 0 OR p.skipped > 0 OR p.parent_id IS NULL) "
        f"ORDER BY dp.path LIMIT {LIMIT}", (scan_id,),
    )]

    # Temp/caches : dossiers au nom connu (le plus haut seulement) + fichiers temporaires isolés.
    cands = sorted(
        (DirStat(p, s, c) for p, s, c in conn.execute(
            "SELECT d.path, s.size, s.file_count FROM dirs s JOIN dirpaths d ON d.id = s.dir_id "
            "WHERE s.scan_id = ? AND s.parent_id IS NOT NULL AND s.size > 0", (scan_id,))
         if os.path.basename(p).lower() in TEMP_DIR_NAMES),
        key=lambda d: d.path,
    )
    kept: list[DirStat] = []
    prefixes: tuple[str, ...] = ()
    for d in cands:
        if not d.path.startswith(prefixes):
            kept.append(d)
            prefixes += (d.path + os.sep,)
    res.temp_dirs = sorted(kept, key=lambda d: d.size, reverse=True)[:LIMIT]
    temp_prefixes = prefixes
    placeholders = ",".join("?" * len(TEMP_EXTS))
    res.temp_files = [
        f for f in _files(conn, root, f"(ext IN ({placeholders}) OR name LIKE '~$%')",
                          TEMP_EXTS, limit=LIMIT * 4)
        if not f.path.startswith(temp_prefixes)
    ][:LIMIT]

    # Installeurs oubliés : paquets d'installation, ou .exe/.iso ressemblant à un setup.
    age_cut = now - INSTALLER_MIN_AGE_DAYS * 86400
    exts = (*INSTALLER_EXTS, ".exe", ".iso")
    placeholders = ",".join("?" * len(exts))
    for f in _files(conn, root, f"ext IN ({placeholders}) AND mtime < ?", (*exts, age_cut),
                    limit=LIMIT * 20):
        name = os.path.basename(f.path)
        ext = os.path.splitext(name)[1].lower()
        in_downloads = os.path.basename(os.path.dirname(f.path)).lower() in DOWNLOAD_DIRS
        if ext in INSTALLER_EXTS or in_downloads or (ext == ".exe" and _INSTALLER_NAME.search(name)):
            res.installers.append(f)
            if len(res.installers) >= LIMIT:
                break
    return res


class StatsWorker(QThread):
    """Calcule les stats dans un thread avec sa propre connexion SQLite."""

    done = Signal(object)    # StatsResult
    failed = Signal(str)

    def __init__(self, db_path: str | Path, scan_id: int, root: str,
                 old_days: int = DEFAULT_OLD_DAYS, parent=None) -> None:
        super().__init__(parent)
        self.db_path, self.scan_id, self.root, self.old_days = db_path, scan_id, root, old_days

    def run(self) -> None:
        try:
            cache = Cache(self.db_path)
            try:
                self.done.emit(compute_stats(cache.conn, self.scan_id, self.root, self.old_days))
            finally:
                cache.close()
        except Exception as exc:  # noqa: BLE001 - remonté à l'UI
            self.failed.emit(f"Calcul des statistiques impossible : {exc!r}")
