"""Socle des recherches (doublons, même nom, images similaires) : thread, progression, annulation."""
from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.cache import Cache

_PROGRESS_INTERVAL = 0.1  # secondes
# Fichiers « en ligne uniquement » (OneDrive, Proton Drive…) : les lire les téléchargerait.
_CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
CHANGED, CLOUD = "changed", "cloud"


def is_cloud_only(st: os.stat_result) -> bool:
    """Fichier « en ligne uniquement » (OneDrive, Proton Drive…) : le lire le téléchargerait."""
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_ATTRS)


def fresh_stat(path: str, size: int, mtime: float) -> tuple[os.stat_result | None, str]:
    """(stat, "") si le fichier est lisible et tel qu'au scan ; sinon (None, CHANGED ou CLOUD)."""
    try:
        st = os.stat(path)
    except OSError:
        return None, CHANGED
    if st.st_size != size or abs(st.st_mtime - mtime) > 1e-3:
        return None, CHANGED
    if is_cloud_only(st):
        return None, CLOUD
    return st, ""


class Finder(QThread):
    """Recherche dans le cache hors du thread UI. Les sous-classes écrivent `_find`."""

    progress = Signal(str, "qlonglong", "qlonglong")  # étape, fait, total
    result_ready = Signal(object)                     # résultat de `_find`
    failed = Signal(str)

    def __init__(self, db_path: str | Path, root: str, parent=None) -> None:
        super().__init__(parent)
        self.db_path, self.root = db_path, root
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

    def _find(self, cache: Cache):
        raise NotImplementedError

    @staticmethod
    def _done(res, t0: float, cancelled: bool = False):
        res.cancelled = cancelled
        res.duration = time.monotonic() - t0
        return res
