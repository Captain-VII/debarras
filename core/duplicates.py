"""Recherche de doublons : même taille → hash partiel (4 Ko) → hash complet (xxHash).

Les hash sont relus/écrits dans le cache : un second passage sur des fichiers inchangés
ne relit presque rien sur le disque.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import xxhash

from core.cache import Cache, subtree
from core.finder import CLOUD, Finder, fresh_stat

PARTIAL_SIZE = 4096
CHUNK = 1 << 20


def xxh_file(path: str, limit: int | None = None, stop=lambda: False,
             on_read=None) -> tuple[str | None, int]:
    """xxh3-128 des `limit` premiers octets (ou du fichier entier) -> (empreinte, octets lus).

    Empreinte None si `stop()` devient vrai en cours de lecture. Lève OSError si illisible.
    Format partagé par les doublons et « même nom » (colonnes partial_hash / full_hash du cache).
    """
    h = xxhash.xxh3_128()
    read = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            want = CHUNK if limit is None else min(CHUNK, limit - read)
            if want <= 0:
                break
            block = f.read(want)
            if not block:
                break
            h.update(block)
            read += len(block)
            if limit is None:
                if stop():
                    return None, read
                if on_read:
                    on_read(read)
    return h.hexdigest(), read


class HashFinder(Finder):
    """Recherche qui compare des contenus par empreinte xxHash, mémorisée dans le cache."""

    HASH_PHASE = "Hash complet"

    def _hash(self, c, limit: int | None, res, done: int = 0, total: int = 0) -> str | None:
        """xxh3-128 des `limit` premiers octets (ou du fichier entier) ; None si illisible ou annulé."""
        try:
            digest, read = xxh_file(c.path, limit, self.isInterruptionRequested,
                                    lambda n: self._emit(self.HASH_PHASE, done + n, total))
        except OSError:
            res.skipped_error += 1
            return None
        if digest:
            res.hashed_bytes += read
            c.dirty = True
        return digest

    @staticmethod
    def _save(cache: Cache, cands: list) -> None:
        """Mémorise les hash (seulement si le fichier n'a pas changé entre-temps)."""
        if not cands:
            return
        with cache.conn:
            cache.conn.executemany(
                "UPDATE files SET partial_hash = ?, full_hash = ? "
                "WHERE dir_id = ? AND name = ? AND size = ? AND mtime = ?",
                [(c.partial, c.full, c.dir_id, c.name, c.size, c.mtime) for c in cands],
            )


@dataclass(frozen=True, slots=True)
class DupFile:
    path: str
    size: int
    mtime: float


@dataclass
class DupGroup:
    size: int
    digest: str
    files: list[DupFile]

    @property
    def wasted(self) -> int:
        """Espace récupérable si on ne garde qu'un exemplaire."""
        return self.size * (len(self.files) - 1)


@dataclass
class DupResult:
    root: str
    min_size: int
    groups: list[DupGroup] = field(default_factory=list)
    candidates: int = 0        # fichiers de même taille examinés
    hashed_bytes: int = 0      # octets réellement lus
    skipped_cloud: int = 0
    skipped_changed: int = 0   # modifiés/supprimés depuis le scan
    skipped_error: int = 0
    cancelled: bool = False
    duration: float = 0.0

    @property
    def wasted(self) -> int:
        return sum(g.wasted for g in self.groups)


class _Cand:
    __slots__ = ("path", "dir_id", "name", "size", "mtime", "partial", "full", "dirty")

    def __init__(self, path: str, dir_id: int, name: str, size: int, mtime: float,
                 partial: str | None, full: str | None):
        self.path, self.dir_id, self.name, self.size, self.mtime = path, dir_id, name, size, mtime
        self.partial, self.full = partial, full
        self.dirty = False  # hash à écrire dans le cache


class DuplicateFinder(HashFinder):
    """`result_ready` émet un DupResult."""

    def __init__(self, db_path: str | Path, root: str, min_size: int = 1, parent=None) -> None:
        super().__init__(db_path, root, parent)
        self.min_size = max(1, min_size)

    def _find(self, cache: Cache) -> DupResult:
        t0 = time.monotonic()
        res = DupResult(self.root, self.min_size)
        scope, sargs = subtree("parent", self.root)
        dscope, _ = subtree("path", self.root)

        # 1. Candidats : tailles partagées par au moins deux fichiers.
        rows = cache.conn.execute(
            "SELECT path, dir_id, name, size, mtime, partial_hash, full_hash FROM file_paths "
            f"WHERE {scope} AND size >= ? AND size IN ("
            "  SELECT size FROM files "
            f"  WHERE dir_id IN (SELECT id FROM dirpaths WHERE {dscope}) AND size >= ? "
            "  GROUP BY size HAVING COUNT(*) > 1)",
            (*sargs, self.min_size, *sargs, self.min_size),
        ).fetchall()
        res.candidates = len(rows)

        # 2. Vérification (existe, inchangé, pas en ligne) + fusion des liens physiques.
        by_size: dict[int, list[_Cand]] = {}
        seen_ids: dict[int, set[tuple[int, int]]] = {}
        for i, (path, dir_id, name, size, mtime, partial, full) in enumerate(rows):
            if self.isInterruptionRequested():
                return self._done(res, t0, cancelled=True)
            self._emit("Vérification des fichiers", i, len(rows))
            st, why = fresh_stat(path, size, mtime)
            if st is None:
                if why == CLOUD:
                    res.skipped_cloud += 1
                else:
                    res.skipped_changed += 1
                continue
            ident = (st.st_dev, st.st_ino)
            ids = seen_ids.setdefault(size, set())
            if st.st_ino and ident in ids:
                continue  # lien physique vers un fichier déjà compté
            ids.add(ident)
            by_size.setdefault(size, []).append(_Cand(path, dir_id, name, size, mtime, partial, full))
        groups = [g for g in by_size.values() if len(g) > 1]

        # 3. Hash partiel (4 premiers Ko).
        todo = [c for g in groups for c in g if c.partial is None]
        for i, c in enumerate(todo):
            if self.isInterruptionRequested():
                break
            self._emit("Hash partiel (4 Ko)", i, len(todo))
            c.partial = self._hash(c, PARTIAL_SIZE, res)
            if c.partial and c.size <= PARTIAL_SIZE:
                c.full = c.partial  # le début est le fichier entier
        self._save(cache, [c for c in todo if c.dirty])
        if self.isInterruptionRequested():
            return self._done(res, t0, cancelled=True)
        groups = self._regroup(groups, lambda c: c.partial)

        # 4. Hash complet des fichiers encore en concurrence.
        todo = [c for g in groups for c in g if c.full is None]
        total = sum(c.size for c in todo)
        done = 0
        for c in todo:
            if self.isInterruptionRequested():
                break
            c.full = self._hash(c, None, res, done, total)
            done += c.size
        self._emit("Hash complet", done, total, force=True)
        self._save(cache, [c for c in todo if c.dirty])
        if self.isInterruptionRequested():
            return self._done(res, t0, cancelled=True)

        for g in self._regroup(groups, lambda c: c.full):
            res.groups.append(DupGroup(g[0].size, g[0].full or "",
                                       [DupFile(c.path, c.size, c.mtime) for c in g]))
        res.groups.sort(key=lambda g: g.wasted, reverse=True)
        return self._done(res, t0)

    @staticmethod
    def _regroup(groups: list[list[_Cand]], key) -> list[list[_Cand]]:
        out: list[list[_Cand]] = []
        for g in groups:
            sub: dict[str, list[_Cand]] = {}
            for c in g:
                k = key(c)
                if k:  # None = illisible
                    sub.setdefault(k, []).append(c)
            out.extend(s for s in sub.values() if len(s) > 1)
        return out
