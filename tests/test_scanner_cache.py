import os
import sqlite3

from core.cache import Cache
from core.scanner import ScanOptions, Scanner


def dirs(cache: Cache, scan_id: int) -> dict[str, tuple[int, int, int]]:
    return {p: (s, c, k) for p, s, c, k in cache.conn.execute(
        "SELECT path, size, file_count, skipped FROM dirs WHERE scan_id=?", (scan_id,))}


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
            cache.conn.execute("SELECT path, partial_hash, full_hash FROM files")}
    assert rows["same.bin"] == ("p", "f")
    assert rows["changed.bin"] == (None, None)
    assert "gone.bin" not in rows  # purgé à la fin du scan complet
    assert r.file_count == 2
    assert str(same) in {p for (p,) in cache.conn.execute("SELECT path FROM files")}


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


def test_migration_adds_skipped_column(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE dirs (scan_id INTEGER, path TEXT, parent TEXT, size INTEGER, "
              "file_count INTEGER, PRIMARY KEY (scan_id, path))")
    c.commit()
    c.close()
    Cache(path).close()
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(dirs)")]
    assert "skipped" in cols


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
