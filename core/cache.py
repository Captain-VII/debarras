"""Cache SQLite : état courant des fichiers + agrégats de dossiers par scan.

Scan incrémental : un fichier dont (taille, mtime) n'a pas changé conserve
ses hash déjà calculés ; sinon ils sont invalidés (NULL).
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    root        TEXT NOT NULL,
    started     REAL NOT NULL,
    finished    REAL,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | cancelled
    total_size  INTEGER DEFAULT 0,
    file_count  INTEGER DEFAULT 0,
    dir_count   INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0
);

-- État le plus récent de chaque fichier connu.
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    parent        TEXT NOT NULL,
    name          TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    atime         REAL NOT NULL,
    ctime         REAL NOT NULL,
    partial_hash  TEXT,
    full_hash     TEXT,
    last_scan     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent);
CREATE INDEX IF NOT EXISTS idx_files_size   ON files(size);

-- Tailles cumulées des dossiers, conservées pour chaque scan (historique).
CREATE TABLE IF NOT EXISTS dirs (
    scan_id     INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    parent      TEXT,
    size        INTEGER NOT NULL,
    file_count  INTEGER NOT NULL,
    skipped     INTEGER NOT NULL DEFAULT 0,  -- sous-éléments exclus/inaccessibles (cumulé)
    PRIMARY KEY (scan_id, path)
);
CREATE INDEX IF NOT EXISTS idx_dirs_parent ON dirs(scan_id, parent);
"""

# Réutilise les hash si taille et mtime sont identiques.
_UPSERT_FILE = """
INSERT INTO files (path, parent, name, ext, size, mtime, atime, ctime, last_scan)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    parent = excluded.parent,
    name = excluded.name,
    ext = excluded.ext,
    atime = excluded.atime,
    ctime = excluded.ctime,
    last_scan = excluded.last_scan,
    partial_hash = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.partial_hash END,
    full_hash    = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.full_hash END,
    size = excluded.size,
    mtime = excluded.mtime
"""

FileRow = tuple[str, str, str, str, int, float, float, float]  # path, parent, name, ext, size, m/a/ctime
DirRow = tuple[str, str | None, int, int, int]                # path, parent, size, file_count, skipped


@dataclass(frozen=True)
class ScanInfo:
    id: int
    root: str
    started: float
    finished: float | None
    status: str
    total_size: int
    file_count: int
    dir_count: int
    errors: int


def default_db_path() -> Path:
    """%LOCALAPPDATA%\\FileAnalyzer\\cache.db"""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FileAnalyzer"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cache.db"


class Cache:
    """Accès SQLite. Une instance par thread (sqlite3 n'est pas partageable entre threads)."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(dirs)")}
        if "skipped" not in cols:
            self.conn.execute("ALTER TABLE dirs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    # --- cycle de vie d'un scan -------------------------------------------------

    def start_scan(self, root: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO scans (root, started) VALUES (?, ?)", (root, time.time())
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def upsert_files(self, scan_id: int, rows: Sequence[FileRow]) -> None:
        with self.conn:
            self.conn.executemany(_UPSERT_FILE, [(*r, scan_id) for r in rows])

    def insert_dirs(self, scan_id: int, rows: Iterable[DirRow]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO dirs (scan_id, path, parent, size, file_count, skipped) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((scan_id, *r) for r in rows),
            )

    def finish_scan(self, scan_id: int, root: str, total_size: int,
                    file_count: int, dir_count: int, errors: int) -> None:
        """Clôt le scan et purge les fichiers disparus sous `root`."""
        prefix = root if root.endswith(os.sep) else root + os.sep
        with self.conn:
            self.conn.execute(
                "DELETE FROM files WHERE last_scan <> ? AND substr(path, 1, ?) = ?",
                (scan_id, len(prefix), prefix),
            )
            self.conn.execute(
                "UPDATE scans SET finished=?, status='done', total_size=?, file_count=?, "
                "dir_count=?, errors=? WHERE id=?",
                (time.time(), total_size, file_count, dir_count, errors, scan_id),
            )

    def cancel_scan(self, scan_id: int) -> None:
        """Scan annulé : on garde les fichiers vus mais on jette les agrégats partiels."""
        with self.conn:
            self.conn.execute("DELETE FROM dirs WHERE scan_id=?", (scan_id,))
            self.conn.execute(
                "UPDATE scans SET finished=?, status='cancelled' WHERE id=?",
                (time.time(), scan_id),
            )

    # --- lecture ------------------------------------------------------------------

    def list_scans(self, root: str | None = None) -> list[ScanInfo]:
        sql = "SELECT * FROM scans WHERE status='done'"
        args: tuple = ()
        if root:
            sql += " AND root=?"
            args = (root,)
        return [ScanInfo(*r) for r in self.conn.execute(sql + " ORDER BY id DESC", args)]

    def latest_scan(self, root: str) -> ScanInfo | None:
        scans = self.list_scans(root)
        return scans[0] if scans else None

    def delete_scan(self, scan_id: int) -> None:
        """Retire un scan de l'historique (données du cache uniquement, aucun fichier touché)."""
        with self.conn:
            self.conn.execute("DELETE FROM dirs WHERE scan_id=?", (scan_id,))
            self.conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))

    def roots(self) -> list[str]:
        """Racines déjà scannées, la plus récente en premier."""
        rows = self.conn.execute(
            "SELECT root FROM scans WHERE status='done' GROUP BY root ORDER BY MAX(id) DESC"
        )
        return [r[0] for r in rows]

    def dir_info(self, scan_id: int, path: str) -> tuple[int, int] | None:
        """(taille, nb fichiers) cumulés d'un dossier."""
        row = self.conn.execute(
            "SELECT size, file_count FROM dirs WHERE scan_id=? AND path=?", (scan_id, path)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def size_of(self, scan_id: int, path: str) -> int:
        """Taille connue d'un fichier ou d'un dossier (0 si inconnu)."""
        row = self.conn.execute("SELECT size FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            row = self.conn.execute("SELECT size FROM dirs WHERE scan_id=? AND path=?",
                                    (scan_id, path)).fetchone()
        return row[0] if row else 0

    def child_dirs(self, scan_id: int, parent: str) -> list[tuple[str, int, int]]:
        """Sous-dossiers directs : (chemin, taille, nb fichiers)."""
        return self.conn.execute(
            "SELECT path, size, file_count FROM dirs WHERE scan_id=? AND parent=?",
            (scan_id, parent),
        ).fetchall()

    def child_files(self, parent: str) -> list[tuple[str, str, int, float]]:
        """Fichiers directs : (chemin, nom, taille, mtime)."""
        return self.conn.execute(
            "SELECT path, name, size, mtime FROM files WHERE parent=?", (parent,)
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
