"""Même nom, contenu différent : versions divergentes d'un même fichier dans plusieurs dossiers.

Un groupe = fichiers de même nom (casse ignorée) sous la racine, retenu seulement s'il
contient au moins deux contenus distincts. Tailles différentes = contenus différents sans
rien lire ; à taille égale, empreintes 4 Ko puis complètes (partagées avec les doublons).
"""
from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.cache import Cache, subtree
from core.duplicates import PARTIAL_SIZE, is_cloud_only, xxh_file
from utils.filetypes import extensions

PERSONAL_CATEGORIES = ("Documents", "Images", "Vidéos", "Audio", "Archives")
# Dossiers de code, dépendances et caches : des milliers de fichiers homonymes sans intérêt.
DEV_DIRS = frozenset({
    "node_modules", ".venv", "venv", "env", "site-packages", "dist-packages", ".git", ".svn",
    "__pycache__", ".cargo", ".rustup", ".gradle", ".m2", ".nuget", "packages", "vendor",
    "bower_components", ".tox", ".mypy_cache", ".pytest_cache", "target", "build", "dist",
    "appdata", "$recycle.bin",
})
GENERIC_NAMES = ("desktop.ini", "thumbs.db", ".ds_store", "readme*", "license*", "licence*",
                 "changelog*", "copying*", "index.*", "__init__.py", "folder.jpg", "cover.jpg",
                 "albumart*.jpg", "icon.*", "favicon.*", "logo.*", "*.lnk", "*.url")
MAX_GROUP = 50           # au-delà : nom générique (image1.png…), écarté
_PROGRESS_INTERVAL = 0.1


@dataclass
class SameNameFile:
    path: str
    size: int
    mtime: float
    version: str = "?"     # A = contenu le plus récent ; même lettre = copies identiques

    pixels = 0             # compatibilité avec les règles de sélection de DupView


@dataclass
class SameNameGroup:
    name: str
    files: list[SameNameFile]

    @property
    def versions(self) -> int:
        return len({f.version for f in self.files if f.version != "?"})

    @property
    def wasted(self) -> int:
        """Place libérée en ne gardant que le fichier le plus récent."""
        newest = max(self.files, key=lambda f: f.mtime)
        return sum(f.size for f in self.files) - newest.size


@dataclass
class SameNameResult:
    root: str
    groups: list[SameNameGroup] = field(default_factory=list)
    candidates: int = 0
    hashed_bytes: int = 0
    skipped_generic: int = 0     # groupes écartés (noms génériques ou trop fréquents)
    skipped_cloud: int = 0
    skipped_changed: int = 0
    skipped_error: int = 0
    cancelled: bool = False
    duration: float = 0.0

    @property
    def wasted(self) -> int:
        return sum(g.wasted for g in self.groups)


class _Cand:
    __slots__ = ("path", "dir_id", "name", "size", "mtime", "partial", "full", "key", "dirty", "usable")

    def __init__(self, path, dir_id, name, size, mtime, partial, full):
        self.path, self.dir_id, self.name, self.size, self.mtime = path, dir_id, name, size, mtime
        self.partial, self.full = partial, full
        self.key: tuple | None = None   # identité du contenu ; None = inconnue
        self.dirty = False
        self.usable = False             # lisible sans téléchargement et inchangé depuis le scan


def is_generic(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, pat) for pat in GENERIC_NAMES)


def in_dev_dir(path: str, root: str) -> bool:
    """Sous un dossier de code/dépendances, ou de configuration d'application (.claude, .vscode…) :
    les fichiers homonymes y sont des conventions (SKILL.md, config.json…), pas des versions."""
    rel = os.path.relpath(os.path.dirname(path), root)
    return any(part.lower() in DEV_DIRS or (part.startswith(".") and part not in (".", ".."))
               for part in rel.split(os.sep))


class SameNameFinder(QThread):
    progress = Signal(str, "qlonglong", "qlonglong")
    result_ready = Signal(object)   # SameNameResult
    failed = Signal(str)

    def __init__(self, db_path: str | Path, root: str, min_size: int = 1024,
                 personal_only: bool = True, skip_dev_dirs: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.db_path, self.root, self.min_size = db_path, root, max(0, min_size)
        self.personal_only, self.skip_dev_dirs = personal_only, skip_dev_dirs
        self._last_emit = 0.0

    def cancel(self) -> None:
        self.requestInterruption()

    def _emit(self, phase: str, done: int, total: int, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit >= _PROGRESS_INTERVAL:
            self._last_emit = now
            self.progress.emit(phase, done, total)

    def run(self) -> None:
        try:
            cache = Cache(self.db_path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Cache inaccessible : {exc!r}")
            return
        try:
            self.result_ready.emit(self._find(cache))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Erreur pendant la recherche : {exc!r}")
        finally:
            cache.close()

    # --- algorithme -----------------------------------------------------------------

    def _find(self, cache: Cache) -> SameNameResult:
        t0 = time.monotonic()
        res = SameNameResult(self.root)
        scope, sargs = subtree("parent", self.root)
        dscope, _ = subtree("path", self.root)
        ext_cond, ext_args = "1", ()
        if self.personal_only:
            exts = tuple(e for c in PERSONAL_CATEGORIES for e in extensions(c))
            ext_cond, ext_args = f"ext IN ({','.join('?' * len(exts))})", exts
        # Noms présents au moins deux fois (lower() SQLite : casse ASCII ; affiné en Python).
        rows = cache.conn.execute(
            "SELECT path, dir_id, name, size, mtime, partial_hash, full_hash FROM file_paths "
            f"WHERE {scope} AND size >= ? AND {ext_cond} AND lower(name) IN ("
            "  SELECT lower(name) FROM files "
            f"  WHERE dir_id IN (SELECT id FROM dirpaths WHERE {dscope}) AND size >= ? AND {ext_cond} "
            "  GROUP BY lower(name) HAVING COUNT(*) > 1)",
            (*sargs, self.min_size, *ext_args, *sargs, self.min_size, *ext_args)).fetchall()

        by_name: dict[str, list[_Cand]] = {}
        for r in rows:
            c = _Cand(*r)
            if self.skip_dev_dirs and in_dev_dir(c.path, self.root):
                continue
            by_name.setdefault(c.name.casefold(), []).append(c)
        groups = []
        for key, cands in by_name.items():
            if len(cands) < 2:
                continue
            if len(cands) > MAX_GROUP or is_generic(key):
                res.skipped_generic += 1
                continue
            groups.append(cands)
        res.candidates = sum(len(g) for g in groups)

        # Identité du contenu : la taille suffit si elle est unique dans le groupe.
        to_check: list[list[_Cand]] = []
        for g in groups:
            by_size: dict[int, list[_Cand]] = {}
            for c in g:
                by_size.setdefault(c.size, []).append(c)
            for size, same in by_size.items():
                if len(same) == 1:
                    same[0].key = ("size", size)
                else:
                    to_check.append(same)

        # À taille égale : 4 premiers Ko, puis fichier entier si nécessaire.
        total = sum(c.size for same in to_check for c in same)
        done = 0
        for same in to_check:
            for c in same:
                if self.isInterruptionRequested():
                    return self._finish(cache, res, groups, t0, cancelled=True)
                self._emit("Comparaison du contenu", done, total)
                c.usable = self._usable(c, res)
                if c.usable and c.partial is None:
                    c.partial = self._hash(c, PARTIAL_SIZE, res)
                    if c.partial and c.size <= PARTIAL_SIZE:
                        c.full = c.partial
            by_partial: dict[str, list[_Cand]] = {}
            for c in same:
                if c.partial:
                    by_partial.setdefault(c.partial, []).append(c)
            for partial, sub in by_partial.items():
                if len(sub) == 1:
                    sub[0].key = ("partial", sub[0].size, partial)
                    continue
                for c in sub:
                    if c.full is None and c.usable:  # jamais de lecture d'un fichier en ligne
                        c.full = self._hash(c, None, res, done, total)
                    if c.full:
                        c.key = ("full", c.size, c.full)
            done += sum(c.size for c in same)
        self._emit("Comparaison du contenu", total, total, force=True)
        return self._finish(cache, res, groups, t0)

    def _usable(self, c: _Cand, res: SameNameResult) -> bool:
        """Fichier lisible sans téléchargement et inchangé depuis le scan."""
        try:
            st = os.stat(c.path)
        except OSError:
            res.skipped_changed += 1
            return False
        if st.st_size != c.size or abs(st.st_mtime - c.mtime) > 1e-3:
            res.skipped_changed += 1
            return False
        if is_cloud_only(st):
            res.skipped_cloud += 1
            return False
        return True

    def _hash(self, c: _Cand, limit: int | None, res: SameNameResult,
              done: int = 0, total: int = 0) -> str | None:
        try:
            digest, read = xxh_file(c.path, limit, self.isInterruptionRequested,
                                    lambda n: self._emit("Comparaison du contenu", done + n, total))
        except OSError:
            res.skipped_error += 1
            return None
        if digest:
            res.hashed_bytes += read
            c.dirty = True
        return digest

    def _finish(self, cache: Cache, res: SameNameResult, groups: list[list[_Cand]], t0: float,
                cancelled: bool = False) -> SameNameResult:
        dirty = [c for g in groups for c in g if c.dirty]
        if dirty:  # empreintes réutilisables par la recherche de doublons
            with cache.conn:
                cache.conn.executemany(
                    "UPDATE files SET partial_hash = ?, full_hash = ? "
                    "WHERE dir_id = ? AND name = ? AND size = ? AND mtime = ?",
                    [(c.partial, c.full, c.dir_id, c.name, c.size, c.mtime) for c in dirty])
        res.cancelled = cancelled
        if not cancelled:
            for g in groups:
                grp = self._labelled(g)
                if grp.versions >= 2:
                    res.groups.append(grp)
            res.groups.sort(key=lambda g: sum(f.size for f in g.files), reverse=True)
        res.duration = time.monotonic() - t0
        return res

    @staticmethod
    def _labelled(cands: list[_Cand]) -> SameNameGroup:
        """Lettres de version : A = contenu le plus récemment modifié."""
        newest: dict[tuple, float] = {}
        for c in cands:
            if c.key is not None:
                newest[c.key] = max(newest.get(c.key, 0.0), c.mtime)
        letters = {k: chr(ord("A") + i) if i < 26 else str(i + 1)
                   for i, k in enumerate(sorted(newest, key=lambda k: -newest[k]))}
        files = [SameNameFile(c.path, c.size, c.mtime, letters.get(c.key, "?") if c.key else "?")
                 for c in sorted(cands, key=lambda c: -c.mtime)]
        return SameNameGroup(cands[0].name, files)
