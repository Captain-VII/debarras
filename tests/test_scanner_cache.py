import os
import sqlite3

from core.cache import Cache
from core.scanner import ScanOptions, Scanner


def dirs(cache: Cache, scan_id: int) -> dict[str, tuple[int, int, int]]:
    return {p: (s, c, k) for p, s, c, k in cache.conn.execute(
        "SELECT path, size, file_count, skipped FROM dir_sizes WHERE scan_id=?", (scan_id,))}


def test_scan_counts_and_aggregates(root, write, scan, cache):
    write("a/one.bin", 100)
    write("a/deep/two.bin", 250)
    write("b/three.txt", 50)
    (root / "empty").mkdir()
    r = scan(root)
    assert (r.file_count, r.total_size, r.dir_count, r.cancelled) == (3, 400, 5, False)
    d = dirs(cache, r.scan_id)
    assert d[str(root)][:2] == (400, 3)
    assert d[str(root / "a")][:2] == (350, 2)
    assert d[str(root / "a" / "deep")][:2] == (250, 1)
    assert d[str(root / "empty")] == (0, 0, 0)
    info = cache.latest_scan(str(root))
    assert info and info.status == "done" and info.file_count == 3


def test_exclusions_blacklist_and_skipped_counter(root, write, scan, cache):
    write("keep/a.txt", 10)
    write("AppData/secret.bin", 10)
    write("skipme/b.bin", 10)
    write("logs/app.log", 10)
    write("proj/node_modules/x.js", 10)
    opts = ScanOptions(excluded_paths=[str(root / "skipme")], excluded_names=["AppData"],
                       ignore_patterns=["*.LOG", "node_modules"])
    r = scan(root, opts)
    names = {n for (n,) in cache.conn.execute("SELECT name FROM files")}
    assert names == {"a.txt"}
    d = dirs(cache, r.scan_id)
    # Un dossier dont le contenu a été ignoré n'est pas « vide » : skipped > 0.
    assert d[str(root / "logs")] == (0, 0, 1)
    assert d[str(root / "proj")][2] == 1
    assert str(root / "AppData") not in d
    assert d[str(root)][2] == 4  # AppData, skipme, app.log, node_modules


def test_incremental_scan_keeps_hashes_of_unchanged_files(root, write, scan, cache):
    same = write("same.bin", b"abc")
    changed = write("changed.bin", b"abc")
    scan(root)
    cache.conn.execute("UPDATE files SET partial_hash='p', full_hash='f'")
    cache.conn.commit()
    changed.write_bytes(b"abcd")
    gone = root / "gone.bin"
    gone.write_bytes(b"z")
    scan(root)
    gone.unlink()
    r = scan(root)
    rows = {os.path.basename(p): (ph, fh) for p, ph, fh in
            cache.conn.execute("SELECT path, partial_hash, full_hash FROM file_paths")}
    assert rows["same.bin"] == ("p", "f")
    assert rows["changed.bin"] == (None, None)
    assert "gone.bin" not in rows  # purgé à la fin du scan complet
    assert r.file_count == 2
    assert str(same) in {p for (p,) in cache.conn.execute("SELECT path FROM file_paths")}


def test_cancelled_scan_keeps_no_partial_aggregates(root, write, db, cache, qapp):
    for i in range(5):
        write(f"d{i}/f.bin", 10)
    s = Scanner(str(root), db, ScanOptions([], []))
    out = []
    s.scan_finished.connect(out.append)
    # requestInterruption() est ignoré hors d'un thread démarré : on simule la demande.
    s.isInterruptionRequested = lambda: True
    s.run()
    assert out[0].cancelled
    scan = cache.conn.execute("SELECT id, status FROM scans").fetchone()
    assert scan[1] == "cancelled"
    assert cache.conn.execute("SELECT COUNT(*) FROM dirs WHERE scan_id=?", (scan[0],)).fetchone()[0] == 0
    assert cache.latest_scan(str(root)) is None


def test_missing_root_reports_failure(tmp_path, db, qapp):
    s = Scanner(str(tmp_path / "nope"), db)
    failed = []
    s.scan_failed.connect(failed.append)
    s.run()
    assert failed and "introuvable" in failed[0]


V1_SCHEMA = """
CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL, started REAL NOT NULL,
    finished REAL, status TEXT NOT NULL DEFAULT 'running', total_size INTEGER DEFAULT 0,
    file_count INTEGER DEFAULT 0, dir_count INTEGER DEFAULT 0, errors INTEGER DEFAULT 0);
CREATE TABLE files (path TEXT PRIMARY KEY, parent TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL,
    size INTEGER NOT NULL, mtime REAL NOT NULL, atime REAL NOT NULL, ctime REAL NOT NULL,
    partial_hash TEXT, full_hash TEXT, last_scan INTEGER NOT NULL);
CREATE INDEX idx_files_parent ON files(parent);
CREATE INDEX idx_files_size ON files(size);
CREATE TABLE dirs (scan_id INTEGER NOT NULL, path TEXT NOT NULL, parent TEXT, size INTEGER NOT NULL,
    file_count INTEGER NOT NULL, PRIMARY KEY (scan_id, path));
CREATE INDEX idx_dirs_parent ON dirs(scan_id, parent);
INSERT INTO scans VALUES (1, 'C:\\r', 1, 2, 'done', 30, 2, 2, 0), (2, 'C:\\r', 3, 4, 'done', 35, 2, 2, 0);
INSERT INTO files VALUES ('C:\\r\\a.txt', 'C:\\r', 'a.txt', '.txt', 10, 5, 5, 5, 'p1', 'f1', 2),
                         ('C:\\r\\s\\b.bin', 'C:\\r\\s', 'b.bin', '.bin', 25, 6, 6, 6, NULL, NULL, 2);
INSERT INTO dirs VALUES (1, 'C:\\r', NULL, 30, 2), (1, 'C:\\r\\s', 'C:\\r', 20, 1),
                        (2, 'C:\\r', NULL, 35, 2), (2, 'C:\\r\\s', 'C:\\r', 25, 1);
"""


def test_migration_from_v1_keeps_files_hashes_and_history(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(V1_SCHEMA)  # base v1 sans colonne « skipped » (toute première version)
    c.close()
    cache = Cache(path)
    try:
        conn = cache.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "path" not in {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        assert sorted(conn.execute("SELECT path, size, partial_hash, full_hash FROM file_paths")) == [
            ("C:\\r\\a.txt", 10, "p1", "f1"), ("C:\\r\\s\\b.bin", 25, None, None)]
        assert sorted(conn.execute("SELECT scan_id, path, parent, size, skipped FROM dir_sizes")) == [
            (1, "C:\\r", None, 30, 0), (1, "C:\\r\\s", "C:\\r", 20, 0),
            (2, "C:\\r", None, 35, 0), (2, "C:\\r\\s", "C:\\r", 25, 0)]
        assert [s.id for s in cache.list_scans("C:\\r")] == [2, 1]
        assert cache.child_dirs(2, "C:\\r") == [("C:\\r\\s", 25, 1)]
    finally:
        cache.close()
    Cache(path).close()  # réouverture : pas de seconde migration


def test_paths_at_drive_root(cache):
    """Un fichier à la racine d'un lecteur : pas de double séparateur dans le chemin."""
    sid = cache.start_scan("X:\\")
    cache.upsert_files(sid, [("X:\\a.txt", "X:\\", "a.txt", ".txt", 1, 0, 0, 0)])
    cache.insert_dirs(sid, [("X:\\", None, 1, 1, 0), ("X:\\sub", "X:\\", 0, 0, 0)])
    assert [p for (p,) in cache.conn.execute("SELECT path FROM file_paths")] == ["X:\\a.txt"]
    assert cache.child_files("X:\\") == [("X:\\a.txt", "a.txt", 1, 0.0)]
    assert cache.size_of(sid, "X:\\a.txt") == 1
    assert [p for p, *_ in cache.child_dirs(sid, "X:\\")] == ["X:\\sub"]


def test_unused_dir_paths_are_pruned(root, write, scan, cache):
    write("a/x.bin", 10)
    write("b/y.bin", 10)
    first = scan(root)
    import shutil
    shutil.rmtree(root / "b")
    second = scan(root)
    paths = {p for (p,) in cache.conn.execute("SELECT path FROM dirpaths")}
    assert str(root / "b") in paths  # encore référencé par l'historique du 1er scan
    cache.delete_scan(first.scan_id)
    paths = {p for (p,) in cache.conn.execute("SELECT path FROM dirpaths")}
    assert str(root / "b") not in paths and str(root / "a") in paths
    assert cache.latest_scan(str(root)).id == second.scan_id


def test_cache_queries(root, write, scan, cache):
    write("a/x.bin", 30)
    write("y.bin", 12)
    r = scan(root)
    assert cache.roots() == [str(root)]
    assert cache.size_of(r.scan_id, str(root / "a")) == 30
    assert cache.size_of(r.scan_id, str(root / "y.bin")) == 12
    assert cache.size_of(r.scan_id, str(root / "zzz")) == 0
    assert [p for p, *_ in cache.child_dirs(r.scan_id, str(root))] == [str(root / "a")]
    assert [n for _, n, *_ in cache.child_files(str(root))] == ["y.bin"]
    old = r.scan_id
    r2 = scan(root)
    cache.delete_scan(old)
    assert [s.id for s in cache.list_scans(str(root))] == [r2.scan_id]
