import os
import time

from core.history import CHANGED, NEW, REMOVED, compare
from core.search import SearchQuery, search_files
from core.stats import compute_stats


def rel(paths, root):
    return sorted(os.path.relpath(p, root) for p in paths)


# --- statistiques ------------------------------------------------------------------

def test_compute_stats(root, write, scan, cache):
    write("docs/rapport.pdf", 3000)
    write("docs/old.docx", 2000, age_days=800)
    write("media/film.mkv", 50_000)
    write("proj/__pycache__/m.pyc", 400)
    write("proj/__pycache__/cache/inner.pyc", 100)   # caché dans un cache : pas listé à part
    write("proj/build.tmp", 10)
    write("Downloads/tool.exe", 700, age_days=90)
    write("apps/Setup_x64.exe", 800, age_days=90)
    write("apps/portable.exe", 900, age_days=90)      # ni installeur ni dans Téléchargements
    write("apps/new_setup.exe", 800)                   # trop récent
    write("pkg/app.msi", 600, age_days=90)
    (root / "vide" / "sous-vide").mkdir(parents=True)
    r = scan(root)
    st = compute_stats(cache.conn, r.scan_id, str(root), old_days=365)

    assert st.total_size == r.total_size and st.file_count == r.file_count
    assert st.by_category["Vidéos"] == (50_000, 1)
    assert st.by_category["Documents"] == (5000, 2)
    assert st.largest[0].path.endswith("film.mkv")
    assert rel((f.path for f in st.old), root) == ["docs\\old.docx"]
    assert st.old_total == (2000, 1)
    assert rel(st.empty_dirs, root) == ["vide"]  # seulement le plus haut
    assert rel((d.path for d in st.temp_dirs), root) == ["proj\\__pycache__"]
    assert rel((f.path for f in st.temp_files), root) == ["proj\\build.tmp"]
    assert rel((f.path for f in st.installers), root) == [
        "Downloads\\tool.exe", "apps\\Setup_x64.exe", "pkg\\app.msi"]


# --- recherche ---------------------------------------------------------------------

def test_search_filters(root, write, scan, cache):
    write("docs/Rapport été 2024.pdf", 3000, age_days=400)
    write("docs/rapport_01.docx", 500)
    write("docs/notes.txt", 20)
    write("media/vacances.mp4", 90_000, age_days=800)
    write("code/main.py", 30)
    write("misc/data.xyz", 10)
    scan(root)

    def names(**kw):
        return sorted(h.name for h in search_files(cache.conn, str(root), SearchQuery(**kw)).hits)

    assert names(text="ÉTÉ") == ["Rapport été 2024.pdf"]              # casse et accents
    assert names(text="rapport*") == ["Rapport été 2024.pdf", "rapport_01.docx"]
    assert names(text="*.py") == ["main.py"]
    assert names(category="Vidéos") == ["vacances.mp4"]
    assert names(category="Autres") == ["data.xyz"]
    assert names(exts=["TXT", ".py"]) == ["main.py", "notes.txt"]
    assert names(min_size=1000) == ["Rapport été 2024.pdf", "vacances.mp4"]
    assert names(max_size=25) == ["data.xyz", "notes.txt"]
    assert names(older_than_days=365) == ["Rapport été 2024.pdf", "vacances.mp4"]
    assert names(newer_than_days=30, category="Documents") == ["notes.txt", "rapport_01.docx"]
    res = search_files(cache.conn, str(root), SearchQuery(text="o"), limit=2)
    assert len(res.hits) == 2 and res.count == 3  # Rapport…, rapport_01, notes
    assert SearchQuery().is_empty()


def test_search_is_limited_to_root(root, write, scan, cache, tmp_path):
    write("inside.txt", 5)
    other = tmp_path / "data2"  # préfixe commun avec « data »
    other.mkdir()
    (other / "inside2.txt").write_bytes(b"x")
    scan(root)
    scan(other)
    hits = search_files(cache.conn, str(root), SearchQuery(text="inside")).hits
    assert [h.name for h in hits] == ["inside.txt"]


# --- historique --------------------------------------------------------------------

def test_history_compare(root, write, scan, cache):
    write("a/deep/x.bin", 1000)
    write("a/other.bin", 500)
    write("b/y.bin", 3000)
    z = write("b/z.bin", 2 * 1024 * 1024)
    write("d/gone.bin", 1500)
    first = scan(root)
    time.sleep(0.01)
    write("a/deep/big.bin", 3 * 1024 * 1024)
    z.unlink()
    write("c/new.bin", 800)
    import shutil
    shutil.rmtree(root / "d")
    second = scan(root)

    scans = {s.id: s for s in cache.list_scans(str(root))}
    diff = compare(cache, scans[first.scan_id], scans[second.scan_id])
    status = {os.path.relpath(p, root): d.status for p, d in diff.deltas.items()}
    assert status == {".": CHANGED, "a": CHANGED, "a\\deep": CHANGED, "b": CHANGED,
                      "c": NEW, "d": REMOVED}
    assert diff.deltas[str(root / "a" / "deep")].delta == 3 * 1024 * 1024
    assert diff.deltas[str(root / "d")].delta == -1500
    assert diff.total_delta == 3 * 1024 * 1024 - 2 * 1024 * 1024 + 800 - 1500
    # Vue « où se concentre la variation » : a\deep et b, pas leurs parents.
    assert rel((d.path for d in diff.focus), root) == ["a\\deep", "b"]
    assert [os.path.relpath(d.path, root) for d in diff.top(True)] == ["a\\deep"]
