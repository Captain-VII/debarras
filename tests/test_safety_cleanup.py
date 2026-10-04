"""Niveaux de sécurité, garde-fous des actions, nettoyage guidé et page d'accueil."""
import os
import sqlite3
import time
from pathlib import Path

import pytest

from core import safety
from core.safety import (
    CLEANABLE, PERSONAL, SOFTWARE, SYSTEM, Classifier, containers, software_dirs, summarize,
)

HOME = Path.home()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Les dossiers de test sont dans %TEMP% (donc AppData) : profil, AppData et Temp factices."""
    home = tmp_path / "profil"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("TEMP", str(home / "AppData" / "Local" / "Temp"))
    monkeypatch.setattr(safety, "_current", Classifier())
    return home


@pytest.mark.parametrize("path, is_dir, level", [
    (os.environ.get("SystemRoot", r"C:\Windows") + r"\System32", True, SYSTEM),
    (r"C:\pagefile.sys", False, SYSTEM),
    (r"D:\System Volume Information", True, SYSTEM),
    ("D:\\", True, SYSTEM),
    (str(HOME), True, SYSTEM),
    (str(HOME / "NTUSER.DAT"), False, SYSTEM),
    (str(HOME / "AppData" / "Roaming" / "Code"), True, SOFTWARE),
    (str(HOME / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Cache"), True, CLEANABLE),
    (str(HOME / ".vscode"), True, SOFTWARE),
    (str(HOME / ".cache"), True, CLEANABLE),
    (str(HOME / "Downloads" / "setup.exe"), False, CLEANABLE),
    (str(HOME / "Downloads" / "photo.jpg"), False, PERSONAL),
    (r"D:\SteamLibrary\steamapps\common\Jeu", True, SOFTWARE),
    (r"D:\projet\.git", True, SOFTWARE),
    (r"D:\projet\node_modules", True, CLEANABLE),
    (r"D:\projet\src\__pycache__", True, CLEANABLE),
    (r"D:\Photos\vacances.jpg", False, PERSONAL),
])
def test_rules(path, is_dir, level, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(HOME / "AppData" / "Local"))
    monkeypatch.setenv("TEMP", str(HOME / "AppData" / "Local" / "Temp"))
    v = Classifier().classify(path, is_dir)
    assert v.level == level, v
    assert v.reason


@pytest.mark.usefixtures("fake_home")
def test_software_dirs_and_containers(root, write, scan):
    write("Jeux/Foo/bin/moteur.dll", 10)
    write("Jeux/Foo/foo.exe", 10)
    write("Jeux/Foo/data/pack.bin", 5000)
    write("Jeux/notes.txt", 10)
    write("Docs/rapport.pdf", 3000)
    r = scan(root)
    conn = sqlite3.connect(Path(root).parent / "cache.db")
    try:
        found = software_dirs(conn, str(root))
        clf = Classifier(found, containers=containers(conn, r.scan_id, found, str(root)))
        assert clf.classify(str(root / "Jeux" / "Foo" / "data" / "pack.bin"), False).level == SOFTWARE
        assert clf.classify(str(root / "Jeux"), True).level == SOFTWARE        # occupé à > 50 % par Foo
        assert clf.classify(str(root / "Jeux" / "notes.txt"), False).level == PERSONAL  # voisin : pas concerné
        assert clf.classify(str(root / "Docs"), True).level == PERSONAL
        assert clf.risky_inside(str(root)) == [str(root / "Jeux" / "Foo")]
        s = summarize(conn, r.scan_id, str(root), clf)
        assert s.totals[SOFTWARE] >= 5000 and sum(s.totals.values()) == r.total_size
        assert [p for p, _, _ in s.top[SOFTWARE]] == [str(root / "Jeux")]
    finally:
        conn.close()


@pytest.mark.usefixtures("fake_home")
def test_actions_refuse_system_items(tmp_path):
    from core.actions import TRASH, SKIPPED, plan
    f = tmp_path / "a.txt"
    f.write_text("x")
    old = safety.current()
    clf = Classifier()
    clf._memo[(os.path.normpath(str(f)), False)] = safety.Verdict(SYSTEM, "test")   # simule un fichier système
    clf._memo[(os.path.normpath(str(f)), True)] = safety.Verdict(SYSTEM, "test")
    safety.set_current(clf)
    try:
        rec = plan(TRASH, [str(f)], simulated=True)
        assert rec.items[0].status == SKIPPED and "protégé" in rec.items[0].error
    finally:
        safety.set_current(old)


@pytest.mark.usefixtures("fake_home")
def test_confirm_dialog_requires_ack_for_software(qapp, tmp_path):
    from core.actions import TRASH
    from ui.actions_ui import ConfirmDialog
    d = tmp_path / "Appli"
    d.mkdir()
    old = safety.current()
    safety.set_current(Classifier({str(d)}))
    try:
        dlg = ConfirmDialog(TRASH, [str(d)], {str(d): 10}, simulated=True)
        assert not dlg.ok.isEnabled()
        dlg.understood.setChecked(True)
        assert dlg.ok.isEnabled()
        plain = tmp_path / "doc.txt"
        plain.write_text("x")
        assert ConfirmDialog(TRASH, [str(plain)], {}, simulated=True).ok.isEnabled()
    finally:
        safety.set_current(old)


def test_cleanup_temp_and_browsers(tmp_path, monkeypatch):
    from core import cleanup
    temp = tmp_path / "Temp"
    (temp / "vieux").mkdir(parents=True)
    (temp / "vieux" / "a.tmp").write_bytes(b"x" * 100)
    (temp / "recent.tmp").write_bytes(b"y" * 50)
    old = time.time() - 3 * 86400
    os.utime(temp / "vieux" / "a.tmp", (old, old))
    os.utime(temp / "vieux", (old, old))
    monkeypatch.setenv("TEMP", str(temp))
    t = cleanup.temp_target()
    assert t.paths == [str(temp / "vieux")] and t.size == 100

    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    cache = local / "Google" / "Chrome" / "User Data" / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "data_1").write_bytes(b"z" * 300)
    (local / "Google" / "Chrome" / "User Data" / "Default" / "Bookmarks").write_text("{}")
    targets = cleanup.browser_targets(running={"chrome.exe"})
    assert [x.key for x in targets] == ["browser:Google Chrome"]
    chrome = targets[0]
    assert chrome.paths == [str(cache)] and chrome.size == 300
    assert "ouvert" in chrome.note and not chrome.recommended
    assert cleanup.browser_targets(running=set())[0].recommended

    # Dossier trop gros pour la corbeille d'un seul tenant : proposé fichier par fichier.
    monkeypatch.setattr(cleanup, "SPLIT_SIZE", 200)
    (cache / "data_2").write_bytes(b"z" * 100)
    split = cleanup.browser_targets(running=set())[0]
    assert sorted(split.paths) == [str(cache / "data_1"), str(cache / "data_2")] and split.size == 400


@pytest.mark.usefixtures("fake_home")
def test_home_and_cleanup_views(qapp, root, write, db, scan, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from core.cache import Cache
    from ui.cleanup_view import CleanupView
    from ui.home_view import HomeView
    from core.cleanup import Target

    write("a/x.bin", 1000)
    write("a/__pycache__/m.pyc", 200)
    r = scan(root)
    cache = Cache(db)
    try:
        home = HomeView()
        home.treemap.load(cache, r.scan_id, str(root))
        clf = Classifier()
        s = summarize(cache.conn, r.scan_id, str(root), clf)
        home.set_summary(str(root), r.total_size, s)
        assert s.totals[CLEANABLE] == 200
        assert "sans aucun risque" in home.headline.text()
        home.chips[CLEANABLE].click()
        assert home.details.items.count() == 1
        home._reveal(str(root / "a" / "__pycache__"))
        assert "Nettoyable" in home.details.badge.text()
        assert home.details.trash_btn.isEnabled()
    finally:
        cache.close()

    view = CleanupView()
    got = []
    view.cleanup_requested.connect(lambda p, s: got.append((p, s)))
    t = Target("temp", "Temp", "desc", paths=["A", "B"], sizes={"A": 5, "B": 7}, count=2)
    t2 = Target("browser:X", "X", "desc", paths=["C"], sizes={"C": 1}, count=1, recommended=False)
    view._fill([t, t2])
    QApplication.processEvents()
    view._request()
    assert got and sorted(got[0][0]) == ["A", "B"] and got[0][1] == {"A": 5, "B": 7}


def test_recycle_capacity_guard(qapp, tmp_path, monkeypatch):
    from core import recycle
    from core.actions import ARCHIVE, TRASH
    from ui import actions_ui

    def fake(drive, capacity=1000, used=0, disabled=False):
        return lambda d: recycle.BinInfo(d, capacity, used, disabled)

    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    sizes = {a: 400, b: 400}
    assert recycle.capacity_problems([a, b], sizes, fake(None)) == []
    assert "place libre" in recycle.capacity_problems([a, b], sizes, fake(None, used=300))[0]
    assert "dépasse la capacité" in recycle.capacity_problems([a], {a: 2000}, fake(None))[0]
    assert "désactivée" in recycle.capacity_problems([a], sizes, fake(None, disabled=True))[0]
    assert recycle.capacity_problems([a], sizes, fake(None, capacity=None)) == []   # inconnue

    monkeypatch.setattr(actions_ui, "capacity_problems", lambda p, s: ["trop gros"])
    (tmp_path / "a").write_text("x")
    dlg = actions_ui.ConfirmDialog(TRASH, [a], sizes, simulated=False)
    assert not dlg.ok.isEnabled()                              # jamais de suppression définitive
    assert actions_ui.ConfirmDialog(TRASH, [a], sizes, simulated=True).ok.isEnabled()
    arch = actions_ui.ConfirmDialog(ARCHIVE, [a], sizes, simulated=False)
    arch.target.setText(str(tmp_path / "x.zip"))
    assert not arch.ok.isEnabled()
    arch.trash_originals.setChecked(False)                     # originaux gardés : plus de risque
    assert arch.ok.isEnabled()


def test_bin_info_reads_real_drive():
    from core.recycle import bin_info
    b = bin_info(os.environ.get("SystemDrive", "C:") + "\\")
    assert b.used >= 0 and (b.capacity is None or b.capacity > 0)
