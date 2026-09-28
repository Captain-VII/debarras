"""Tests d'interface hors écran : algorithme du treemap, garde-fous des doublons, fenêtre."""
import os
import time

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import QApplication

from ui.treemap import squarify


def wait_until(cond, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise TimeoutError
        QApplication.processEvents()
        time.sleep(0.02)


@pytest.mark.parametrize("n", [1, 2, 7, 300])
def test_squarify_conserves_areas_and_bounds(n):
    rect = QRectF(10, 20, 800, 500)
    vals = sorted(((i % 13 + 1) ** 2 for i in range(n)), reverse=True)
    total = sum(vals)
    areas = [v / total * rect.width() * rect.height() for v in vals]
    out = squarify(areas, rect)
    assert len(out) == n
    bounds = rect.adjusted(-1e-6, -1e-6, 1e-6, 1e-6)
    for r, a in zip(out, areas):
        assert abs(r.width() * r.height() - a) < 1e-6 * rect.width() * rect.height()
        assert bounds.contains(r)


def test_dup_view_guard_and_auto_select(qapp, root, write, db, scan):
    from core.duplicates import DuplicateFinder
    from ui.dup_view import KEEP_OLDEST, KEEP_PRIORITY, DupView

    data = os.urandom(5000)
    write("old/f.bin", data, age_days=10)
    write("keep/f.bin", data, age_days=5)
    write("new/f.bin", data)
    scan(root)
    res = []
    finder = DuplicateFinder(db, str(root), 1)
    finder.result_ready.connect(res.append)
    finder.run()

    v = DupView()
    v.set_scan(db, str(root))
    v._on_result(res[0])

    def checked():
        return sorted(os.path.relpath(f.path, root) for f in v.checked_files())

    v.auto_select()  # garder le plus récent
    assert checked() == ["keep\\f.bin", "old\\f.bin"]
    v.rule.setCurrentIndex(KEEP_OLDEST)
    v.auto_select()
    assert checked() == ["keep\\f.bin", "new\\f.bin"]
    v.rule.setCurrentIndex(KEEP_PRIORITY)
    v.priority.setText(str(root / "keep"))
    v.auto_select()
    assert checked() == ["new\\f.bin", "old\\f.bin"]
    v.whitelist = [str(root / "new")]  # un fichier protégé n'est jamais coché
    v.auto_select()
    assert checked() == ["old\\f.bin"]

    # Garde-fou : impossible de cocher tout un groupe.
    head = v.tree.topLevelItem(0)
    for j in range(head.childCount()):
        head.child(j).setCheckState(0, Qt.CheckState.Checked)
    states = [head.child(j).checkState(0) for j in range(head.childCount())]
    assert states.count(Qt.CheckState.Unchecked) == 1

    # Action refusée si le seul exemplaire conservé a disparu du disque.
    requested = []
    v.action_requested.connect(lambda kind, paths: requested.append(paths))
    v.rule.setCurrentIndex(KEEP_OLDEST)
    v.whitelist = []
    v.auto_select()
    (root / "old" / "f.bin").unlink()
    v._request("trash")
    assert requested == []


def test_main_window_scan_populates_views(qapp, root, write):
    from ui.main_window import MainWindow

    write("a/film.mkv", 20_000)
    write("b/doc.pdf", 3_000)
    w = MainWindow()
    try:
        w.open_folder(str(root))
        wait_until(lambda: w.scanner is None and w._stats is not None)
        assert w.current_scan and w.current_scan.file_count == 2
        model = w.tree.tree_model
        top = model.index(0, 0)
        assert model.canFetchMore(top)  # chargement à la demande
        model.fetchMore(top)
        assert model.rowCount(top) == 2
        assert model.index(0, 0, top).data() == "a"  # trié par taille décroissante
        assert w._stats.by_category["Vidéos"] == (20_000, 1)
        w.search_view.text.setText("doc")
        w.search_view.run()
        assert [h.name for h in w.search_view.hits] == ["doc.pdf"]
        assert w.treemap.canvas.current == str(root)
    finally:
        w.close()
