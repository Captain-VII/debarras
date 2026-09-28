"""Scan récursif (os.scandir) dans un QThread, avec progression et annulation."""
from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.cache import Cache, DirRow, FileRow

# Reparse points à ne pas suivre (boucles possibles) : jonctions et liens symboliques.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C

_BATCH_SIZE = 5000
_PROGRESS_INTERVAL = 0.1  # secondes


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def default_excluded_paths() -> list[str]:
    env = os.environ
    return [
        env.get("SystemRoot", r"C:\Windows"),
        env.get("ProgramFiles", r"C:\Program Files"),
        env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]


@dataclass
class ScanOptions:
    excluded_paths: list[str] = field(default_factory=default_excluded_paths)
    excluded_names: list[str] = field(default_factory=lambda: ["AppData"])  # n'importe où
    ignore_patterns: list[str] = field(default_factory=list)  # liste noire : motifs sur le nom (*.log…)


@dataclass(frozen=True)
class ScanResult:
    scan_id: int
    root: str
    total_size: int
    file_count: int
    dir_count: int
    errors: int
    duration: float
    cancelled: bool


class Scanner(QThread):
    """Usage : s = Scanner(root); s.progress.connect(...); s.start(); s.cancel()."""

    progress = Signal("qlonglong", "qlonglong", str)  # nb fichiers, octets, dossier courant
    scan_finished = Signal(object)     # ScanResult
    scan_failed = Signal(str)          # erreur fatale (base inaccessible, racine invalide…)

    def __init__(self, root: str, db_path: str | Path | None = None,
                 options: ScanOptions | None = None, parent=None) -> None:
        super().__init__(parent)
        self.root = os.path.abspath(root)
        self.db_path = db_path
        self.options = options or ScanOptions()
        self._excl_paths = {_norm(p) for p in self.options.excluded_paths}
        self._excl_names = {n.lower() for n in self.options.excluded_names}
        pats = [fnmatch.translate(p.strip()) for p in self.options.ignore_patterns if p.strip()]
        self._ignore = re.compile("|".join(pats), re.IGNORECASE) if pats else None

    def _ignored(self, name: str) -> bool:
        return self._ignore is not None and self._ignore.match(name) is not None

    def cancel(self) -> None:
        self.requestInterruption()

    # --- filtrage -----------------------------------------------------------------

    def _skip_dir(self, entry: os.DirEntry) -> bool:
        if (entry.name.lower() in self._excl_names or _norm(entry.path) in self._excl_paths
                or self._ignored(entry.name)):
            return True
        try:
            tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
        except OSError:
            return True
        return tag in (_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK)

    # --- thread -------------------------------------------------------------------

    def run(self) -> None:
        if not os.path.isdir(self.root):
            self.scan_failed.emit(f"Dossier introuvable : {self.root}")
            return
        try:
            cache = Cache(self.db_path)
        except Exception as exc:  # noqa: BLE001 - remonté à l'UI
            self.scan_failed.emit(f"Cache inaccessible : {exc}")
            return
        self._scan_id: int | None = None
        try:
            self._scan(cache)
        except Exception as exc:  # noqa: BLE001
            if self._scan_id is not None:
                cache.cancel_scan(self._scan_id)
            self.scan_failed.emit(f"Erreur pendant le scan : {exc!r}")
        finally:
            cache.close()

    def _scan(self, cache: Cache) -> None:
        t0 = time.monotonic()
        scan_id = self._scan_id = cache.start_scan(self.root)

        stack: list[str] = [self.root]
        parents: dict[str, str | None] = {self.root: None}
        direct: dict[str, list[int]] = {}  # dossier -> [taille, nb fichiers, nb ignorés] (contenu direct)
        batch: list[FileRow] = []
        total_files = total_bytes = errors = 0
        last_emit = 0.0
        cancelled = False

        while stack:
            if self.isInterruptionRequested():
                cancelled = True
                break
            current = stack.pop()
            d_size = d_count = d_skipped = 0
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if self._skip_dir(entry):
                                    d_skipped += 1
                                else:
                                    stack.append(entry.path)
                                    parents[entry.path] = current
                            elif entry.is_file(follow_symlinks=False):
                                if self._ignored(entry.name):
                                    d_skipped += 1
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                name = entry.name
                                batch.append((
                                    entry.path, current, name, os.path.splitext(name)[1].lower(),
                                    st.st_size, st.st_mtime, st.st_atime, st.st_ctime,
                                ))
                                d_size += st.st_size
                                d_count += 1
                        except OSError:
                            errors += 1
                            d_skipped += 1
            except OSError:  # PermissionError, dossier disparu, chemin trop long…
                errors += 1
                d_skipped += 1  # contenu inconnu : ne jamais le considérer comme vide
            direct[current] = [d_size, d_count, d_skipped]
            total_files += d_count
            total_bytes += d_size

            if len(batch) >= _BATCH_SIZE:
                cache.upsert_files(scan_id, batch)
                batch.clear()
            now = time.monotonic()
            if now - last_emit >= _PROGRESS_INTERVAL:
                last_emit = now
                self.progress.emit(total_files, total_bytes, current)

        if batch:
            cache.upsert_files(scan_id, batch)

        if cancelled:
            cache.cancel_scan(scan_id)
        else:
            cache.insert_dirs(scan_id, self._aggregate(direct, parents))
            cache.finish_scan(scan_id, self.root, total_bytes, total_files, len(direct), errors)

        self.progress.emit(total_files, total_bytes, "")
        self.scan_finished.emit(ScanResult(
            scan_id, self.root, total_bytes, total_files, len(direct), errors,
            time.monotonic() - t0, cancelled,
        ))

    @staticmethod
    def _aggregate(direct: dict[str, list[int]],
                   parents: dict[str, str | None]) -> list[DirRow]:
        """Remonte tailles, nb de fichiers et nb d'éléments ignorés vers la racine."""
        totals = {p: v[:] for p, v in direct.items()}
        for path in sorted(totals, key=lambda p: p.count(os.sep), reverse=True):
            parent = parents.get(path)
            if parent is not None and parent in totals:
                for i in range(3):
                    totals[parent][i] += totals[path][i]
        return [(p, parents.get(p), s, c, k) for p, (s, c, k) in totals.items()]
