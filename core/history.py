"""Comparaison de deux scans d'un même dossier : ce qui a grossi, diminué, apparu, disparu.

Se base sur les tailles de dossiers conservées pour chaque scan (table `dirs`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.cache import Cache, ScanInfo

NEW, REMOVED, CHANGED = "nouveau", "supprimé", "modifié"
FOCUS_RATIO = 0.8        # un dossier est « précis » si aucun enfant ne porte ≥ 80 % de sa variation
FOCUS_MIN_BYTES = 1 << 20


@dataclass(slots=True)
class DirDelta:
    path: str
    parent: str | None
    old_size: int
    new_size: int
    old_count: int
    new_count: int
    status: str

    @property
    def delta(self) -> int:
        return self.new_size - self.old_size

    @property
    def count_delta(self) -> int:
        return self.new_count - self.old_count


@dataclass
class HistoryDiff:
    old: ScanInfo
    new: ScanInfo
    deltas: dict[str, DirDelta] = field(default_factory=dict)       # dossiers qui ont changé
    children: dict[str, list[str]] = field(default_factory=dict)    # parent -> enfants changés
    focus: list[DirDelta] = field(default_factory=list)             # vue à plat

    @property
    def total_delta(self) -> int:
        return self.new.total_size - self.old.total_size

    def top(self, grown: bool, n: int = 10) -> list[DirDelta]:
        items = [d for d in self.focus if (d.delta > 0) == grown]
        return sorted(items, key=lambda d: abs(d.delta), reverse=True)[:n]


def _load(cache: Cache, scan_id: int) -> dict[str, tuple[str | None, int, int]]:
    return {p: (parent, s, c) for p, parent, s, c in cache.conn.execute(
        "SELECT path, parent, size, file_count FROM dirs WHERE scan_id=?", (scan_id,))}


def compare(cache: Cache, old: ScanInfo, new: ScanInfo) -> HistoryDiff:
    diff = HistoryDiff(old, new)
    a, b = _load(cache, old.id), _load(cache, new.id)
    for path in a.keys() | b.keys():
        pa, pb = a.get(path), b.get(path)
        if pa and pb:
            if pa[1] == pb[1] and pa[2] == pb[2]:
                continue
            d = DirDelta(path, pb[0], pa[1], pb[1], pa[2], pb[2], CHANGED)
        elif pb:
            d = DirDelta(path, pb[0], 0, pb[1], 0, pb[2], NEW)
        else:
            d = DirDelta(path, pa[0], pa[1], 0, pa[2], 0, REMOVED)
        diff.deltas[path] = d
        if d.parent is not None:
            diff.children.setdefault(d.parent, []).append(path)
    for kids in diff.children.values():
        kids.sort(key=lambda p: abs(diff.deltas[p].delta), reverse=True)

    # Vue à plat : dossiers où la variation se concentre (pas expliquée par un seul enfant).
    for d in diff.deltas.values():
        if abs(d.delta) < FOCUS_MIN_BYTES:
            continue
        kids = diff.children.get(d.path, [])
        biggest = max((diff.deltas[k].delta for k in kids
                       if (diff.deltas[k].delta > 0) == (d.delta > 0)), key=abs, default=0)
        if abs(biggest) < FOCUS_RATIO * abs(d.delta):
            diff.focus.append(d)
    diff.focus.sort(key=lambda d: abs(d.delta), reverse=True)
    return diff


class HistoryWorker(QThread):
    done = Signal(object)   # HistoryDiff
    failed = Signal(str)

    def __init__(self, db_path: str | Path, old: ScanInfo, new: ScanInfo, parent=None) -> None:
        super().__init__(parent)
        self.db_path, self.old, self.new = db_path, old, new

    def run(self) -> None:
        try:
            cache = Cache(self.db_path)
            try:
                self.done.emit(compare(cache, self.old, self.new))
            finally:
                cache.close()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Comparaison impossible : {exc!r}")


def display_name(path: str, root: str) -> str:
    """Chemin relatif à la racine du scan, pour des libellés courts."""
    if os.path.normcase(path) == os.path.normcase(root):
        return path
    return os.path.relpath(path, root)
