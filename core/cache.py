"""Cache SQLite : état courant des fichiers + agrégats de dossiers par scan.

Schéma compact (v2) : chaque chemin de dossier est stocké une seule fois (`dirpaths`),
fichiers et agrégats n'y font référence que par identifiant. Les vues `file_paths` et
`dir_sizes` reconstituent les chemins complets pour les lectures.

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

SCHEMA_VERSION = 3

_SCHEMA = r"""
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

-- Chemins de dossiers, une seule fois chacun.
CREATE TABLE IF NOT EXISTS dirpaths (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL UNIQUE
);

-- État le plus récent de chaque fichier connu (clé = dossier + nom, pas de rowid).
CREATE TABLE IF NOT EXISTS files (
    dir_id        INTEGER NOT NULL,
    name          TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    atime         REAL NOT NULL,
    ctime         REAL NOT NULL,
    partial_hash  TEXT,
    full_hash     TEXT,
    last_scan     INTEGER NOT NULL,
    image_hash    INTEGER,          -- empreinte visuelle dHash (images similaires)
    PRIMARY KEY (dir_id, name)
) WITHOUT ROWID;

-- Tailles cumulées des dossiers, conservées pour chaque scan (historique).
CREATE TABLE IF NOT EXISTS dirs (
    scan_id     INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    dir_id      INTEGER NOT NULL,
    parent_id   INTEGER,
    size        INTEGER NOT NULL,
    file_count  INTEGER NOT NULL,
    skipped     INTEGER NOT NULL DEFAULT 0,  -- sous-éléments exclus/inaccessibles (cumulé)
    PRIMARY KEY (scan_id, dir_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_dirs2_parent ON dirs(scan_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_dirs2_dir    ON dirs(dir_id);

-- Vues de lecture avec chemins complets.
CREATE VIEW IF NOT EXISTS file_paths AS
SELECT CASE WHEN substr(d.path, -1) = '\' THEN d.path || f.name
            ELSE d.path || '\' || f.name END AS path,
       d.path AS parent, f.name, f.ext, f.size, f.mtime, f.atime, f.ctime,
       f.partial_hash, f.full_hash, f.last_scan, f.dir_id, f.image_hash
FROM files f JOIN dirpaths d ON d.id = f.dir_id;

CREATE VIEW IF NOT EXISTS dir_sizes AS
SELECT s.scan_id, d.path, p.path AS parent, s.size, s.file_count, s.skipped,
       s.dir_id, s.parent_id
FROM dirs s JOIN dirpaths d ON d.id = s.dir_id
LEFT JOIN dirpaths p ON p.id = s.parent_id;
"""

# Réutilise les hash si taille et mtime sont identiques.
_UPSERT_FILE = """
INSERT INTO files (dir_id, name, ext, size, mtime, atime, ctime, last_scan)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(dir_id, name) DO UPDATE SET
    ext = excluded.ext,
    atime = excluded.atime,
    ctime = excluded.ctime,
    last_scan = excluded.last_scan,
    partial_hash = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.partial_hash END,
    full_hash    = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.full_hash END,
    image_hash   = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.image_hash END,
    size = excluded.size,
    mtime = excluded.mtime
"""

# Migration depuis le schéma v1 (chemins complets répétés dans chaque ligne).
_MIGRATE_V1 = """
BEGIN;
DROP INDEX IF EXISTS idx_files_parent;
DROP INDEX IF EXISTS idx_files_size;
DROP INDEX IF EXISTS idx_dirs_parent;
ALTER TABLE files RENAME TO files_v1;
ALTER TABLE dirs RENAME TO dirs_v1;
{schema}
INSERT OR IGNORE INTO dirpaths (path)
    SELECT parent FROM files_v1
    UNION SELECT path FROM dirs_v1
    UNION SELECT parent FROM dirs_v1 WHERE parent IS NOT NULL;
INSERT OR IGNORE INTO files (dir_id, name, ext, size, mtime, atime, ctime,
                             partial_hash, full_hash, last_scan)
    SELECT d.id, f.name, f.ext, f.size, f.mtime, f.atime, f.ctime,
           f.partial_hash, f.full_hash, f.last_scan
    FROM files_v1 f JOIN dirpaths d ON d.path = f.parent;
INSERT OR IGNORE INTO dirs
    SELECT s.scan_id, d.id, p.id, s.size, s.file_count, s.skipped
    FROM dirs_v1 s JOIN dirpaths d ON d.path = s.path
    LEFT JOIN dirpaths p ON p.path = s.parent;
DROP TABLE files_v1;
DROP TABLE dirs_v1;
PRAGMA user_version = {version};
COMMIT;
"""

_V1_TABLES = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, parent TEXT NOT NULL, name TEXT NOT NULL,
    ext TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, atime REAL NOT NULL,
    ctime REAL NOT NULL, partial_hash TEXT, full_hash TEXT, last_scan INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dirs (scan_id INTEGER NOT NULL, path TEXT NOT NULL, parent TEXT,
    size INTEGER NOT NULL, file_count INTEGER NOT NULL, PRIMARY KEY (scan_id, path));
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


APP_DIR = "Debarras"
_OLD_APP_DIR = "FileAnalyzer"  # nom des premières versions


def default_db_path() -> Path:
    """%LOCALAPPDATA%\\Debarras\\cache.db (cache, journal et paramètres dans ce dossier)."""
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    base, old = local / APP_DIR, local / _OLD_APP_DIR
    if not base.exists() and old.is_dir():
        try:
            old.rename(base)  # reprise des données de l'ancienne version
        except OSError:
            base = old        # dossier verrouillé : on continue avec l'ancien
    base.mkdir(parents=True, exist_ok=True)
    return base / "cache.db"


def subtree(column: str, root: str) -> tuple[str, tuple[str, str, str]]:
    """Condition SQL « `column` est `root` ou un de ses sous-dossiers » (utilise l'index)."""
    lo = root if root.endswith(os.sep) else root + os.sep
    hi = lo[:-1] + chr(ord(lo[-1]) + 1)
    return f"({column} = ? OR ({column} >= ? AND {column} < ?))", (root, lo, hi)


class Cache:
    """Accès SQLite. Une instance par thread (sqlite3 n'est pas partageable entre threads)."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._ids: dict[str, int] = {}
        self._migrate()
        self.conn.executescript(_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Met à niveau un cache existant sans perdre fichiers, hash ni historique."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if version == 2:  # v2 -> v3 : empreinte visuelle
            self.conn.execute("ALTER TABLE files ADD COLUMN image_hash INTEGER")
            self.conn.execute("DROP VIEW IF EXISTS file_paths")  # recréée avec la colonne
            self.conn.commit()
            return
        # v1 -> v3 : reconstruit les tables (chemins complets -> identifiants de dossiers).
        if "path" not in self._columns("files") and "path" not in self._columns("dirs"):
            return  # base neuve
        self.conn.executescript(_V1_TABLES)  # tables v1 éventuellement manquantes
        if "skipped" not in self._columns("dirs"):
            self.conn.execute("ALTER TABLE dirs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        self.conn.executescript(_MIGRATE_V1.format(schema=_SCHEMA, version=SCHEMA_VERSION))
        self.conn.execute("VACUUM")  # rend au disque la place libérée

    # --- identifiants de dossiers -----------------------------------------------------

    def dir_id(self, path: str, create: bool = True) -> int | None:
        """Identifiant du dossier `path` (créé si besoin et si `create`)."""
        i = self._ids.get(path)
        if i is not None:
            return i
        row = self.conn.execute("SELECT id FROM dirpaths WHERE path=?", (path,)).fetchone()
        if row:
            i = row[0]
        elif create:
            i = self.conn.execute("INSERT INTO dirpaths (path) VALUES (?)", (path,)).lastrowid
        else:
            return None
        self._ids[path] = i
        return i

    def _prune_dirpaths(self) -> None:
        """Supprime les chemins que plus rien ne référence."""
        self.conn.execute(
            "DELETE FROM dirpaths WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.dir_id = dirpaths.id) "
            "AND NOT EXISTS (SELECT 1 FROM dirs s WHERE s.dir_id = dirpaths.id)")
        self._ids.clear()

    # --- cycle de vie d'un scan -------------------------------------------------

    def start_scan(self, root: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO scans (root, started) VALUES (?, ?)", (root, time.time())
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def upsert_files(self, scan_id: int, rows: Sequence[FileRow]) -> None:
        with self.conn:
            self.conn.executemany(
                _UPSERT_FILE,
                [(self.dir_id(parent), name, ext, size, m, a, c, scan_id)
                 for _, parent, name, ext, size, m, a, c in rows],
            )

    def insert_dirs(self, scan_id: int, rows: Iterable[DirRow]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO dirs (scan_id, dir_id, parent_id, size, file_count, skipped) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(scan_id, self.dir_id(path), self.dir_id(parent) if parent else None, size, count, skipped)
                 for path, parent, size, count, skipped in rows],
            )

    def finish_scan(self, scan_id: int, root: str, total_size: int,
                    file_count: int, dir_count: int, errors: int) -> None:
        """Clôt le scan et purge les fichiers disparus sous `root`."""
        cond, args = subtree("path", root)
        with self.conn:
            self.conn.execute(
                f"DELETE FROM files WHERE last_scan <> ? AND dir_id IN (SELECT id FROM dirpaths WHERE {cond})",
                (scan_id, *args),
            )
            self._prune_dirpaths()
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
            self._prune_dirpaths()

    def roots(self) -> list[str]:
        """Racines déjà scannées, la plus récente en premier."""
        rows = self.conn.execute(
            "SELECT root FROM scans WHERE status='done' GROUP BY root ORDER BY MAX(id) DESC"
        )
        return [r[0] for r in rows]

    def dir_info(self, scan_id: int, path: str) -> tuple[int, int] | None:
        """(taille, nb fichiers) cumulés d'un dossier."""
        i = self.dir_id(path, create=False)
        row = None if i is None else self.conn.execute(
            "SELECT size, file_count FROM dirs WHERE scan_id=? AND dir_id=?", (scan_id, i)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def size_of(self, scan_id: int, path: str) -> int:
        """Taille connue d'un fichier ou d'un dossier (0 si inconnu)."""
        parent = self.dir_id(os.path.dirname(path), create=False)
        if parent is not None:
            row = self.conn.execute("SELECT size FROM files WHERE dir_id=? AND name=?",
                                    (parent, os.path.basename(path))).fetchone()
            if row:
                return row[0]
        info = self.dir_info(scan_id, path)
        return info[0] if info else 0

    def child_dirs(self, scan_id: int, parent: str) -> list[tuple[str, int, int]]:
        """Sous-dossiers directs : (chemin, taille, nb fichiers)."""
        i = self.dir_id(parent, create=False)
        if i is None:
            return []
        return self.conn.execute(
            # Index imposé : sans statistiques, SQLite parcourt tout le scan (x100 plus lent).
            "SELECT d.path, s.size, s.file_count FROM dirs s INDEXED BY idx_dirs2_parent "
            "JOIN dirpaths d ON d.id = s.dir_id WHERE s.scan_id=? AND s.parent_id=?", (scan_id, i),
        ).fetchall()

    def child_files(self, parent: str) -> list[tuple[str, str, int, float]]:
        """Fichiers directs : (chemin, nom, taille, mtime)."""
        i = self.dir_id(parent, create=False)
        if i is None:
            return []
        return [(os.path.join(parent, name), name, size, mtime) for name, size, mtime in
                self.conn.execute("SELECT name, size, mtime FROM files WHERE dir_id=?", (i,))]

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")  # statistiques du planificateur à jour
        except sqlite3.Error:
            pass
        self.conn.close()
