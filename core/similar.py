"""Images similaires : empreinte visuelle (dHash 64 bits) + regroupement par distance de Hamming.

- Robuste au redimensionnement, à la recompression et aux petites retouches ; pas aux
  recadrages importants ni aux rotations (l'orientation EXIF est corrigée).
- Les empreintes sont mémorisées dans le cache (colonne files.image_hash) et invalidées
  quand le fichier change : un second passage ne relit presque rien.
- Fichiers « en ligne uniquement » ignorés (les lire les téléchargerait).
"""
from __future__ import annotations

import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QThread, Signal

from core.cache import Cache, subtree
from core.duplicates import is_cloud_only

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp")
THRESHOLDS = {"Identiques à l'œil": 0, "Très proches": 5, "Proches": 10}  # bits différents sur 64
DEFAULT_THRESHOLD = 5
THUMB = 96
_MASK = (1 << 64) - 1
_PROGRESS_INTERVAL = 0.1

Image.MAX_IMAGE_PIXELS = 200_000_000  # garde-fou contre les images piégées (« bombes »)


# --- empreinte ---------------------------------------------------------------------------

def _open(path: str, target: int) -> Image.Image:
    """Ouvre en décodant le moins possible (JPEG réduit à la source), orientation EXIF appliquée."""
    img = Image.open(path)
    img.draft("L", (target, target))  # sans effet hors JPEG
    if img.mode == "P":
        img = img.convert("RGBA")  # palette + transparence : conversion propre (sans avertissement)
    return ImageOps.exif_transpose(img)


def dhash(path: str) -> int:
    """dHash 64 bits : compare chaque pixel à son voisin de droite sur une vignette 9×8."""
    with _open(path, 64) as img:
        small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (px[base + col] > px[base + col + 1])
    return bits


def to_signed(h: int) -> int:
    """SQLite stocke des entiers signés 64 bits."""
    return h - (1 << 64) if h >= 1 << 63 else h


def from_signed(h: int) -> int:
    return h & _MASK


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def image_info(path: str) -> tuple[int, int, bytes]:
    """(largeur, hauteur, vignette JPEG) — la taille vient de l'en-tête, avant réduction."""
    with Image.open(path) as raw:
        w, h = raw.size
        if raw.getexif().get(0x0112) in (5, 6, 7, 8):  # photo tournée d'un quart de tour
            w, h = h, w
    with _open(path, THUMB * 2) as img:
        img.thumbnail((THUMB, THUMB))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
    return w, h, buf.getvalue()


# --- regroupement ------------------------------------------------------------------------

class BKTree:
    """Arbre BK sur la distance de Hamming : trouve les voisins sans tout comparer."""

    def __init__(self) -> None:
        self.root: list | None = None  # [valeur, {distance: nœud}]

    def add(self, value: int) -> None:
        if self.root is None:
            self.root = [value, {}]
            return
        node = self.root
        while True:
            d = distance(value, node[0])
            if d == 0:
                return
            child = node[1].get(d)
            if child is None:
                node[1][d] = [value, {}]
                return
            node = child

    def search(self, value: int, radius: int) -> list[int]:
        out, stack = [], [self.root] if self.root else []
        while stack:
            val, children = stack.pop()
            d = distance(value, val)
            if d <= radius:
                out.append(val)
            for k, child in children.items():
                if d - radius <= k <= d + radius:
                    stack.append(child)
        return out


def group_hashes(hashes: list[int], radius: int) -> list[list[int]]:
    """Composantes connexes « distance ≤ radius » (union-find) ; renvoie les index par groupe."""
    by_hash: dict[int, list[int]] = {}
    for i, h in enumerate(hashes):
        by_hash.setdefault(h, []).append(i)
    tree = BKTree()
    for h in by_hash:
        tree.add(h)
    parent = {h: h for h in by_hash}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if radius > 0:
        for h in by_hash:
            for other in tree.search(h, radius):
                ra, rb = find(h), find(other)
                if ra != rb:
                    parent[ra] = rb
    groups: dict[int, list[int]] = {}
    for h, idxs in by_hash.items():
        groups.setdefault(find(h), []).extend(idxs)
    return [g for g in groups.values() if len(g) > 1]


# --- résultats ---------------------------------------------------------------------------

@dataclass
class SimilarFile:
    path: str
    size: int
    mtime: float
    hash: int
    width: int = 0
    height: int = 0
    thumb: bytes = b""
    distance: int = 0   # à l'image de référence du groupe (la meilleure résolution)

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class SimilarGroup:
    files: list[SimilarFile]

    @property
    def wasted(self) -> int:
        """Espace libéré si l'on ne garde que la meilleure version (plus grande résolution)."""
        best = max(self.files, key=lambda f: (f.pixels, f.size))
        return sum(f.size for f in self.files) - best.size


@dataclass
class SimilarResult:
    root: str
    threshold: int
    groups: list[SimilarGroup] = field(default_factory=list)
    candidates: int = 0
    hashed: int = 0            # empreintes calculées (les autres venaient du cache)
    skipped_cloud: int = 0
    skipped_changed: int = 0
    skipped_error: int = 0     # formats non lisibles, fichiers corrompus
    cancelled: bool = False
    duration: float = 0.0

    @property
    def wasted(self) -> int:
        return sum(g.wasted for g in self.groups)


class _Cand:
    __slots__ = ("path", "dir_id", "name", "size", "mtime", "hash")

    def __init__(self, path, dir_id, name, size, mtime, h):
        self.path, self.dir_id, self.name, self.size, self.mtime = path, dir_id, name, size, mtime
        self.hash = None if h is None else from_signed(h)


class SimilarFinder(QThread):
    progress = Signal(str, "qlonglong", "qlonglong")  # étape, fait, total
    result_ready = Signal(object)                     # SimilarResult
    failed = Signal(str)

    def __init__(self, db_path: str | Path, root: str, min_size: int = 20 * 1024,
                 threshold: int = DEFAULT_THRESHOLD, parent=None) -> None:
        super().__init__(parent)
        self.db_path, self.root = db_path, root
        self.min_size, self.threshold = max(1, min_size), threshold
        self.workers = min(8, os.cpu_count() or 4)
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

    def _find(self, cache: Cache) -> SimilarResult:
        t0 = time.monotonic()
        res = SimilarResult(self.root, self.threshold)
        scope, sargs = subtree("parent", self.root)
        marks = ",".join("?" * len(IMAGE_EXTS))
        rows = cache.conn.execute(
            "SELECT path, dir_id, name, size, mtime, image_hash FROM file_paths "
            f"WHERE {scope} AND ext IN ({marks}) AND size >= ?",
            (*sargs, *IMAGE_EXTS, self.min_size)).fetchall()
        cands = [_Cand(*r) for r in rows]
        res.candidates = len(cands)

        # 1. Empreintes manquantes, calculées en parallèle (Pillow libère le GIL au décodage).
        todo = [c for c in cands if c.hash is None]
        ready = [c for c in cands if c.hash is not None]
        pending: list[_Cand] = []
        with ThreadPoolExecutor(self.workers) as pool:
            for n, (c, outcome) in enumerate(zip(todo, pool.map(self._hash_one, todo))):
                if self.isInterruptionRequested():
                    pool.shutdown(cancel_futures=True)
                    break
                self._emit("Empreintes visuelles", n, len(todo))
                if isinstance(outcome, int):
                    c.hash = outcome
                    ready.append(c)
                    pending.append(c)
                    res.hashed += 1
                elif outcome == "cloud":
                    res.skipped_cloud += 1
                elif outcome == "changed":
                    res.skipped_changed += 1
                else:
                    res.skipped_error += 1
                if len(pending) >= 500:
                    self._save(cache, pending)
                    pending = []
        self._save(cache, pending)  # même en cas d'annulation : le travail fait est gardé
        if self.isInterruptionRequested():
            return self._done(res, t0, cancelled=True)

        # 2. Regroupement.
        self._emit("Regroupement", 0, 1, force=True)
        groups = [[ready[i] for i in idxs] for idxs in group_hashes([c.hash for c in ready], self.threshold)]

        # 3. Résolution + vignette des images retenues.
        members = [c for g in groups for c in g]
        info: dict[str, tuple[int, int, bytes] | None] = {}
        with ThreadPoolExecutor(self.workers) as pool:
            for n, (c, meta) in enumerate(zip(members, pool.map(self._info_one, members))):
                if self.isInterruptionRequested():
                    pool.shutdown(cancel_futures=True)
                    return self._done(res, t0, cancelled=True)
                self._emit("Vignettes", n, len(members))
                info[c.path] = meta
        for g in groups:
            files = [SimilarFile(c.path, c.size, c.mtime, c.hash, *(info.get(c.path) or (0, 0, b"")))
                     for c in g]
            ref = max(files, key=lambda f: (f.pixels, f.size))
            for f in files:
                f.distance = distance(f.hash, ref.hash)
            files.sort(key=lambda f: (-f.pixels, -f.size, f.path.lower()))
            res.groups.append(SimilarGroup(files))
        res.groups.sort(key=lambda g: g.wasted, reverse=True)
        return self._done(res, t0)

    @staticmethod
    def _hash_one(c: _Cand) -> int | str:
        try:
            st = os.stat(c.path)
        except OSError:
            return "changed"
        if st.st_size != c.size or abs(st.st_mtime - c.mtime) > 1e-3:
            return "changed"
        if is_cloud_only(st):
            return "cloud"
        try:
            return dhash(c.path)
        except Exception:  # noqa: BLE001 - format non géré, fichier corrompu…
            return "error"

    @staticmethod
    def _info_one(c: _Cand) -> tuple[int, int, bytes] | None:
        try:
            return image_info(c.path)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _save(cache: Cache, cands: list[_Cand]) -> None:
        if not cands:
            return
        with cache.conn:
            cache.conn.executemany(
                "UPDATE files SET image_hash = ? WHERE dir_id = ? AND name = ? AND size = ? AND mtime = ?",
                [(to_signed(c.hash), c.dir_id, c.name, c.size, c.mtime) for c in cands])

    @staticmethod
    def _done(res: SimilarResult, t0: float, cancelled: bool = False) -> SimilarResult:
        res.cancelled = cancelled
        res.duration = time.monotonic() - t0
        return res
