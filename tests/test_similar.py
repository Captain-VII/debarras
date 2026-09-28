import os
import random
import sqlite3

import pytest
from PIL import Image, ImageDraw

from core.cache import Cache
from core.similar import (
    BKTree, SimilarFinder, SimilarResult, dhash, distance, from_signed, group_hashes, to_signed,
)


def picture(seed: int, size=(640, 480)) -> Image.Image:
    """Image « photo » déterministe : dégradé + formes aléatoires."""
    rnd = random.Random(seed)
    img = Image.linear_gradient("L").resize(size).convert("RGB")
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        r = rnd.randrange(20, 160)
        d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rnd.randrange(256) for _ in range(3)))
    return img


def save(img: Image.Image, path, **kw):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, **kw)
    return path


# --- empreinte -------------------------------------------------------------------------------

def test_dhash_survives_resize_recompression_and_exif_rotation(tmp_path):
    a = picture(1)
    ref = dhash(str(save(a, tmp_path / "ref.png")))
    small = dhash(str(save(a.resize((200, 150)), tmp_path / "small.png")))
    jpeg = dhash(str(save(a, tmp_path / "low.jpg", quality=35)))
    # Pixels tournés + balise EXIF « à redresser » (comme un téléphone) : même image affichée.
    exif = Image.Exif()
    exif[0x0112] = 6
    rotated = dhash(str(save(a.rotate(90, expand=True), tmp_path / "rot.jpg", quality=92, exif=exif)))
    other = dhash(str(save(picture(2), tmp_path / "other.png")))
    assert distance(ref, small) <= 3
    assert distance(ref, jpeg) <= 5
    assert distance(ref, rotated) <= 5
    assert distance(ref, other) > 15


def test_signed_roundtrip():
    for h in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
        s = to_signed(h)
        assert -(1 << 63) <= s < (1 << 63)
        assert from_signed(s) == h


def test_bktree_matches_brute_force():
    rnd = random.Random(7)
    values = [rnd.getrandbits(64) for _ in range(400)]
    values += [v ^ (1 << rnd.randrange(64)) for v in values[:50]]  # voisins à 1 bit
    tree = BKTree()
    for v in values:
        tree.add(v)
    for q in values[:40]:
        for radius in (0, 3, 10):
            expected = {v for v in values if distance(q, v) <= radius}
            assert set(tree.search(q, radius)) == expected


def test_group_hashes_connected_components():
    a, b = 0, 0b111                      # distance 3
    c = 0b111 | (0b1111 << 20)           # distance 4 de b, 7 de a
    far = (1 << 64) - 1
    groups = group_hashes([a, b, c, far, a], radius=4)
    assert sorted(sorted(g) for g in groups) == [[0, 1, 2, 4]]
    assert sorted(sorted(g) for g in group_hashes([a, b, c, far, a], radius=0)) == [[0, 4]]


# --- recherche complète ---------------------------------------------------------------------

@pytest.fixture
def find_similar(db, qapp):
    def _find(root, threshold=5, min_size=1) -> SimilarResult:
        f = SimilarFinder(db, str(root), min_size, threshold)
        out, failed = [], []
        f.result_ready.connect(out.append)
        f.failed.connect(failed.append)
        f.run()
        assert not failed, failed
        return out[0]
    return _find


def names(res: SimilarResult, root) -> list[list[str]]:
    return sorted(sorted(os.path.relpath(f.path, root) for f in g.files) for g in res.groups)


def test_finder_groups_variants_and_uses_cache(root, scan, find_similar, cache):
    a, b = picture(10), picture(20)
    save(a, root / "orig" / "a.png")
    save(a.resize((320, 240)), root / "web" / "a_small.jpg", quality=80)
    save(a, root / "copie" / "a.jpg", quality=50)
    save(b, root / "b.png")
    save(b.resize((400, 300)), root / "b_mini.webp")
    save(picture(30), root / "seule.png")
    (root / "casse.jpg").write_bytes(b"pas une image")
    (root / "notes.txt").write_text("ignoré")
    scan(root)

    res = find_similar(root)
    assert names(res, root) == [["b.png", "b_mini.webp"],
                                ["copie\\a.jpg", "orig\\a.png", "web\\a_small.jpg"]]
    assert res.skipped_error == 1 and res.candidates == 7 and res.hashed == 6
    big = next(g for g in res.groups if len(g.files) == 3)
    assert (big.files[0].width, big.files[0].height) == (640, 480)  # meilleure version en tête
    assert big.files[0].distance == 0 and big.files[0].thumb.startswith(b"\xff\xd8")
    assert big.wasted == sum(f.size for f in big.files[1:])

    again = find_similar(root)
    assert again.hashed == 0 and names(again, root) == names(res, root)  # empreintes en cache

    # Fichier modifié -> empreinte invalidée par le rescan, puis recalculée.
    save(picture(99), root / "b_mini.webp")
    scan(root)
    assert cache.conn.execute("SELECT image_hash FROM files WHERE name='b_mini.webp'").fetchone() == (None,)
    third = find_similar(root)
    assert third.hashed == 1
    assert names(third, root) == [["copie\\a.jpg", "orig\\a.png", "web\\a_small.jpg"]]


def test_view_keeps_best_resolution_and_respects_whitelist(root, scan, db, qapp):
    from ui.similar_view import SimilarView

    a = picture(5)
    save(a, root / "big.png")
    save(a.resize((320, 240)), root / "mid.png")
    save(a.resize((160, 120)), root / "keep" / "tiny.png")
    scan(root)
    finder = SimilarFinder(db, str(root), 1, 5)
    out = []
    finder.result_ready.connect(out.append)
    finder.run()
    v = SimilarView()
    v.set_scan(db, str(root))
    v._on_result(out[0])
    assert v.rule.currentData() == "resolution"
    v.auto_select()
    assert sorted(os.path.basename(f.path) for f in v.checked_files()) == ["mid.png", "tiny.png"]
    v.whitelist = [str(root / "keep")]
    v.auto_select()
    assert [os.path.basename(f.path) for f in v.checked_files()] == ["mid.png"]
    headers, rows = v.export_table()
    assert headers[2:4] == ["Largeur", "Hauteur"] and len(rows) == 3


# --- cache ------------------------------------------------------------------------------------

def test_migration_v2_to_v3_adds_image_hash(tmp_path):
    path = tmp_path / "v2.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL, started REAL NOT NULL,
            finished REAL, status TEXT NOT NULL DEFAULT 'running', total_size INTEGER DEFAULT 0,
            file_count INTEGER DEFAULT 0, dir_count INTEGER DEFAULT 0, errors INTEGER DEFAULT 0);
        CREATE TABLE dirpaths (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
        CREATE TABLE files (dir_id INTEGER NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL,
            size INTEGER NOT NULL, mtime REAL NOT NULL, atime REAL NOT NULL, ctime REAL NOT NULL,
            partial_hash TEXT, full_hash TEXT, last_scan INTEGER NOT NULL,
            PRIMARY KEY (dir_id, name)) WITHOUT ROWID;
        CREATE TABLE dirs (scan_id INTEGER NOT NULL, dir_id INTEGER NOT NULL, parent_id INTEGER,
            size INTEGER NOT NULL, file_count INTEGER NOT NULL, skipped INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (scan_id, dir_id)) WITHOUT ROWID;
        CREATE VIEW file_paths AS SELECT d.path || '\\' || f.name AS path, d.path AS parent, f.name,
            f.size, f.dir_id FROM files f JOIN dirpaths d ON d.id = f.dir_id;
        INSERT INTO dirpaths VALUES (1, 'C:\\r');
        INSERT INTO files VALUES (1, 'a.jpg', '.jpg', 10, 1, 1, 1, 'p', 'f', 1);
        PRAGMA user_version = 2;
    """)
    c.close()
    cache = Cache(path)
    try:
        assert cache.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        row = cache.conn.execute("SELECT path, full_hash, image_hash FROM file_paths").fetchone()
        assert row == ("C:\\r\\a.jpg", "f", None)
    finally:
        cache.close()
