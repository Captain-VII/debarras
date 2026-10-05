"""1.6 : disques, mode simple, nettoyage automatique, blocs « libre / non analysé », migration."""
import json
import os
import sys

import pytest

from tests.test_ui import wait_until


@pytest.mark.skipif(sys.platform != "win32", reason="disques Windows")
def test_list_drives_and_drive_of():
    from core.drives import drive_of, list_drives
    drives = list_drives()
    assert drives and all(d.total > 0 and 0 <= d.free <= d.total for d in drives)
    system = os.environ.get("SystemDrive", "C:") + "\\"
    assert drive_of(system) is not None
    assert drive_of(os.path.join(system, "Windows")) is None   # un dossier n'est pas un disque


def test_settings_migration_from_v1(isolated_appdata):
    from core.scanner import default_excluded_paths, old_default_excluded_paths
    from ui.settings import Settings
    path = Settings.path()
    path.write_text(json.dumps({"excluded_paths": old_default_excluded_paths(), "excluded_names": ["AppData"],
                                "theme": "dark"}), encoding="utf-8")
    s = Settings.load()
    assert s.excluded_paths == default_excluded_paths() and s.excluded_names == [] and s.theme == "dark"
    assert s.version == 2 and not s.advanced
    path.write_text(json.dumps({"excluded_paths": [r"D:\Perso"], "excluded_names": ["AppData"]}), encoding="utf-8")
    custom = Settings.load()                                    # réglages personnalisés : conservés
    assert custom.excluded_paths == [r"D:\Perso"] and custom.excluded_names == []


def test_auto_selection_skips_costly_targets():
    from core.cleanup import RECYCLE_KIND, TRASH_KIND, Target, auto_selection
    targets = [Target("temp", "T", "", paths=["a"], sizes={"a": 5}),
               Target("browser:X", "X", "", paths=["b"], sizes={"b": 7}, recommended=False),  # ouvert
               Target("shaders", "S", "", paths=["c"], sizes={"c": 9}, recommended=False),
               Target("recycle", "R", "", kind=RECYCLE_KIND, sizes={"recycle": 3})]
    assert auto_selection(targets) == (["a"], {"a": 5})
    assert targets[0].kind == TRASH_KIND


def test_treemap_free_and_unscanned_blocks(qapp, root, write, db, scan):
    from core.cache import Cache
    from ui.treemap import FREE, UNSCANNED, TreemapView
    write("a/x.bin", 1000)
    r = scan(root)
    cache = Cache(db)
    try:
        view = TreemapView()
        view.resize(600, 400)
        view.load(cache, r.scan_id, str(root))
        view.canvas.set_extras(free=3000, unscanned=500)
        kids = view.canvas._entries(str(root))
        assert [e.special for e in kids] == [FREE, "", UNSCANNED]   # triés par taille
        assert "Non analysé" in view.canvas._tooltip(type("I", (), {"entry": kids[2], "label": ""})())
        view.grab()                                                 # rendu sans erreur
        view.load(cache, r.scan_id, str(root))                      # nouveau chargement : blocs retirés
        assert all(not e.special for e in view.canvas._entries(str(root)))
    finally:
        cache.close()


def test_simple_mode_and_auto_clean(qapp, root, write, monkeypatch):
    from core.actions import TRASH
    from ui.main_window import MainWindow
    write("a/x.bin", 100)
    w = MainWindow()
    try:
        visible = [w.tabs.tabText(i) for i in range(w.tabs.count()) if w.tabs.isTabVisible(i)]
        assert visible == ["Accueil", "Nettoyage"] and not w.path_bar.isVisibleTo(w)
        w.advanced_action.setChecked(True)
        assert all(w.tabs.isTabVisible(i) for i in range(w.tabs.count())) and w.path_bar.isVisibleTo(w)
        w.advanced_action.setChecked(False)

        wait_until(lambda: w.cleanup_view._worker is None)
        calls = []
        monkeypatch.setattr(w.actions, "request", lambda kind, p, s: calls.append((kind, p, s)))
        w._on_cleanup_targets([])
        assert not w.home.auto_btn.isEnabled() and "Rien" in w.home.auto_btn.text()
        from core.cleanup import Target
        w._on_cleanup_targets([Target("temp", "T", "", paths=["z"], sizes={"z": 2048})])
        assert w.home.auto_btn.isEnabled() and "2,0 Ko" in w.home.auto_btn.text()
        w.home.auto_btn.click()
        assert calls == [(TRASH, ["z"], {"z": 2048})]

        w.open_folder(str(root))
        wait_until(lambda: w.scanner is None and w.current_scan is not None)
        assert w.home.drives._row.count() >= 1
    finally:
        w.close()
