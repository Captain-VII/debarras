import os

import pytest

from core.samename import SameNameFinder, SameNameResult, in_dev_dir, is_generic


@pytest.fixture
def find_same(db, qapp):
    def _find(root, min_size=1, personal_only=True, skip_dev=True) -> SameNameResult:
        f = SameNameFinder(db, str(root), min_size, personal_only, skip_dev)
        out, failed = [], []
        f.result_ready.connect(out.append)
        f.failed.connect(failed.append)
        f.run()
        assert not failed, failed
        return out[0]
    return _find


def table(res: SameNameResult, root) -> dict[str, list[tuple[str, str]]]:
    """nom -> [(chemin relatif, version)] dans l'ordre affiché."""
    return {g.name.lower(): [(os.path.relpath(f.path, root), f.version) for f in g.files]
            for g in res.groups}


def test_versions_are_detected_and_labelled(root, write, scan, find_same):
    same = os.urandom(8000)
    write("2023/Rapport.docx", 5000, age_days=400)              # ancienne version (taille différente)
    write("2024/rapport.DOCX", same, age_days=30)                # casse différente : même nom
    write("backup/rapport.docx", same, age_days=20)              # copie identique de 2024
    head = os.urandom(4096)  # PARTIAL_SIZE
    write("a/budget.xlsx", head + b"version 1" * 100, age_days=10)   # même taille, même début,
    write("b/budget.xlsx", head + b"version 2" * 100, age_days=5)    # fin différente
    write("x/photo.jpg", b"meme contenu" * 100)                  # homonymes identiques :
    write("y/photo.jpg", b"meme contenu" * 100)                  # pas un groupe
    write("seul/unique.pdf", 3000)
    scan(root)
    res = find_same(root)
    t = table(res, root)
    assert set(t) == {"rapport.docx", "budget.xlsx"}
    # A = contenu le plus récent ; copies identiques = même lettre.
    assert t["rapport.docx"] == [("backup\\rapport.docx", "A"), ("2024\\rapport.DOCX", "A"),
                                 ("2023\\Rapport.docx", "B")]
    assert t["budget.xlsx"] == [("b\\budget.xlsx", "A"), ("a\\budget.xlsx", "B")]
    g = next(g for g in res.groups if g.name.lower() == "rapport.docx")
    assert g.versions == 2
    assert g.wasted == 8000 + 5000  # on ne garderait que le plus récent
    assert res.hashed_bytes > 0

    again = find_same(root)
    assert again.hashed_bytes == 0 and table(again, root) == t  # empreintes en cache


def test_noise_filters(root, write, scan, find_same):
    write("p1/node_modules/lib/notes.pdf", 10)
    write("p2/node_modules/lib/notes.pdf", 20)
    write("m1/README.md", 10)
    write("m2/README.md", 20)
    write("c1/main.py", 10)
    write("c2/main.py", 20)
    for i in range(55):                                   # nom trop fréquent
        write(f"cam{i}/IMG_0001.jpg", 10 + i)
    scan(root)
    res = find_same(root)
    assert res.groups == [] and res.skipped_generic >= 1
    everything = find_same(root, personal_only=False, skip_dev=False)
    names = {g.name.lower() for g in everything.groups}
    assert {"notes.pdf", "main.py"} <= names
    assert "readme.md" not in names   # générique : toujours ignoré


def test_helpers():
    assert is_generic("Desktop.ini") and is_generic("readme.txt") and is_generic("index.html")
    assert not is_generic("rapport.docx")
    assert in_dev_dir(r"C:\r\proj\.venv\x.pdf", r"C:\r")
    assert in_dev_dir(r"C:\r\.claude\plugins\SKILL.md", r"C:\r")
    assert not in_dev_dir(r"C:\r\docs\x.pdf", r"C:\r")
    assert not in_dev_dir(r"C:\r\x.pdf", r"C:\r")


def test_view_keeps_newest_by_default(root, write, scan, db, qapp):
    from ui.samename_view import SameNameView

    write("old/plan.pdf", 1000, age_days=100)
    write("mid/plan.pdf", 2000, age_days=50)
    write("new/plan.pdf", 1500, age_days=1)
    scan(root)
    f = SameNameFinder(db, str(root), 1)
    out = []
    f.result_ready.connect(out.append)
    f.run()
    v = SameNameView()
    v.set_scan(db, str(root))
    v._on_result(out[0])
    v.auto_select()
    assert sorted(os.path.relpath(x.path, root) for x in v.checked_files()) == ["mid\\plan.pdf", "old\\plan.pdf"]
    v.rule.setCurrentIndex(v.rule.findData("largest"))
    v.auto_select()
    assert sorted(os.path.relpath(x.path, root) for x in v.checked_files()) == ["new\\plan.pdf", "old\\plan.pdf"]
    headers, rows = v.export_table()
    assert headers[:2] == ["Nom", "Version"] and [r[1] for r in rows] == ["A", "B", "C"]
