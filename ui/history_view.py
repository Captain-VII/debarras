"""Onglet Historique : liste des scans, évolution de la taille, comparaison entre deux scans."""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QPushButton, QSplitter, QTabWidget, QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from core.cache import Cache, ScanInfo
from core.history import NEW, REMOVED, DirDelta, HistoryDiff, HistoryWorker, display_name
from ui.charts import _is_dark, _set_chart, _style_chart, _unit
from ui.tree_view import show_in_explorer
from utils.format import human_count, human_date, human_size

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
MAX_CHILDREN = 500
_PLACEHOLDER = "…"


def scan_label(s: ScanInfo) -> str:
    """Date du scan à la seconde (deux scans rapprochés restent distincts)."""
    return datetime.fromtimestamp(s.finished or s.started).strftime("%d/%m/%Y %H:%M:%S")


def signed_size(n: int) -> str:
    return ("+" if n > 0 else "−" if n < 0 else "") + human_size(abs(n))


def signed_count(n: int) -> str:
    return ("+" if n > 0 else "−" if n < 0 else "") + human_count(abs(n))


class HistoryView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cache: Cache | None = None
        self._root = ""
        self._scans: list[ScanInfo] = []       # du plus récent au plus ancien
        self._diff: HistoryDiff | None = None
        self._workers: set[HistoryWorker] = set()

        # --- liste des scans + évolution
        self.scan_list = QTreeWidget()
        self.scan_list.setHeaderLabels(["Scan", "Taille", "Fichiers", "Variation"])
        self.scan_list.setRootIsDecorated(False)
        self.scan_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        h = self.scan_list.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2, 3):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.delete_btn = QPushButton("Retirer de l'historique…")
        self.delete_btn.clicked.connect(self._delete_scan)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.scan_list, 1)
        ll.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self.chart_view = QChartView()
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setMinimumHeight(200)
        top = QSplitter()
        top.addWidget(left)
        top.addWidget(self.chart_view)
        top.setSizes([520, 580])

        # --- comparaison
        self.old_box = QComboBox()
        self.new_box = QComboBox()
        cmp_btn = QPushButton("Comparer")
        cmp_btn.clicked.connect(self.compare)
        row = QHBoxLayout()
        row.addWidget(QLabel("Comparer le scan du"))
        row.addWidget(self.old_box)
        row.addWidget(QLabel("avec celui du"))
        row.addWidget(self.new_box)
        row.addWidget(cmp_btn)
        row.addStretch(1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)

        self.var_tree = self._make_tree(["Dossier", "Avant", "Après", "Variation", "Fichiers", "État"])
        self.var_tree.itemExpanded.connect(self._on_expand)
        self.focus_tree = self._make_tree(["Dossier", "Avant", "Après", "Variation", "Fichiers", "État"])
        self.focus_tree.setRootIsDecorated(False)
        self.focus_tree.setSortingEnabled(True)
        inner = QTabWidget()
        inner.addTab(self.var_tree, "Arbre des variations")
        inner.addTab(self.focus_tree, "Où se concentre la variation")

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addLayout(row)
        bl.addWidget(self.summary)
        bl.addWidget(inner, 1)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([260, 440])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(split)
        self.clear()

    def _make_tree(self, headers: list[str]) -> QTreeWidget:
        t = QTreeWidget()
        t.setHeaderLabels(headers)
        t.setUniformRowHeights(True)
        t.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        h = t.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(headers)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos, t=t: self._context_menu(t, pos))
        return t

    # --- chargement -----------------------------------------------------------------

    def clear(self) -> None:
        self._cache, self._root, self._scans, self._diff = None, "", [], None
        self.scan_list.clear()
        self.old_box.clear()
        self.new_box.clear()
        self.var_tree.clear()
        self.focus_tree.clear()
        self.summary.setText("Aucun scan chargé.")
        self.delete_btn.setEnabled(False)
        chart = QChart()
        _style_chart(chart, _is_dark(self))
        chart.setTitle("Évolution de la taille")
        _set_chart(self.chart_view, chart)

    def set_root(self, cache: Cache, root: str) -> None:
        """Recharge l'historique de `root` et compare automatiquement les deux derniers scans."""
        self.clear()
        self._cache, self._root = cache, root
        self._scans = cache.list_scans(root)
        for i, s in enumerate(self._scans):
            prev = self._scans[i + 1] if i + 1 < len(self._scans) else None
            delta = signed_size(s.total_size - prev.total_size) if prev else "—"
            it = QTreeWidgetItem([scan_label(s), human_size(s.total_size),
                                  human_count(s.file_count), delta])
            for col in (1, 2, 3):
                it.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it.setData(0, PATH_ROLE, s.id)
            self.scan_list.addTopLevelItem(it)
            label = scan_label(s)
            self.old_box.addItem(label, s.id)
            self.new_box.addItem(label, s.id)
        self.delete_btn.setEnabled(len(self._scans) > 1)
        self._build_chart()
        if len(self._scans) < 2:
            self.summary.setText("Un seul scan pour ce dossier : rescannez plus tard pour voir ce qui a changé.")
            return
        self.new_box.setCurrentIndex(0)
        self.old_box.setCurrentIndex(1)
        self.compare()

    def _build_chart(self) -> None:
        dark = _is_dark(self)
        chart = QChart()
        _style_chart(chart, dark)
        chart.setTitle("Évolution de la taille")
        chart.legend().hide()
        scans = list(reversed(self._scans))
        if scans:
            unit, factor = _unit(max(s.total_size for s in scans))
            series = QLineSeries()
            series.setPen(QPen(QColor("#3987e5" if dark else "#2a78d6"), 2))
            series.setPointsVisible(True)
            series.setMarkerSize(8)
            for s in scans:
                series.append(QPointF(float((s.finished or s.started) * 1000), s.total_size / factor))
            series.hovered.connect(self._on_point_hover)
            chart.addSeries(series)
            ax = QDateTimeAxis()
            ax.setFormat("dd/MM/yy HH:mm")
            ax.setTickCount(min(max(len(scans), 2), 6))
            first, last = scans[0].finished or 0, scans[-1].finished or 0
            pad = max((last - first) * 0.05, 3600)
            ax.setRange(QDateTime.fromSecsSinceEpoch(int(first - pad)),
                        QDateTime.fromSecsSinceEpoch(int(last + pad)))
            ay = QValueAxis()
            ay.setTitleText(unit)
            ay.setMin(0)
            ay.setMax(max(s.total_size for s in scans) / factor)
            ay.applyNiceNumbers()
            ay.setLabelFormat("%.0f")
            grid = QColor("#383835" if dark else "#e8e7e3")
            for axis in (ax, ay):
                axis.setGridLinePen(QPen(grid, 1))
            chart.addAxis(ax, Qt.AlignmentFlag.AlignBottom)
            chart.addAxis(ay, Qt.AlignmentFlag.AlignLeft)
            series.attachAxis(ax)
            series.attachAxis(ay)
        _set_chart(self.chart_view, chart)

    def _on_point_hover(self, point: QPointF, on: bool) -> None:
        if not on:
            QToolTip.hideText()
            return
        s = min(self._scans, key=lambda s: abs((s.finished or 0) * 1000 - point.x()))
        QToolTip.showText(QCursor.pos(), f"{human_date(s.finished)}\n{human_size(s.total_size)} — "
                                         f"{human_count(s.file_count)} fichiers", self.chart_view)

    # --- comparaison ----------------------------------------------------------------

    def compare(self) -> None:
        if not self._cache or len(self._scans) < 2:
            return
        by_id = {s.id: s for s in self._scans}
        old, new = by_id.get(self.old_box.currentData()), by_id.get(self.new_box.currentData())
        if old is None or new is None or old.id == new.id:
            self.summary.setText("Choisissez deux scans différents.")
            return
        if old.id > new.id:
            old, new = new, old
        self.summary.setText("Comparaison en cours…")
        worker = HistoryWorker(self._cache.db_path, old, new)
        worker.done.connect(self._on_diff)
        worker.failed.connect(self.summary.setText)
        worker.finished.connect(lambda w=worker: self._workers.discard(w) or w.deleteLater())
        self._workers.add(worker)
        worker.start()

    def _on_diff(self, diff: HistoryDiff) -> None:
        if not self._scans or diff.new.root != self._root:
            return
        self._diff = diff
        changed = len(diff.deltas)
        new = sum(1 for d in diff.deltas.values() if d.status == NEW)
        removed = sum(1 for d in diff.deltas.values() if d.status == REMOVED)
        grown = ", ".join(f"{display_name(d.path, self._root)} ({signed_size(d.delta)})"
                          for d in diff.top(True, 3))
        shrunk = ", ".join(f"{display_name(d.path, self._root)} ({signed_size(d.delta)})"
                           for d in diff.top(False, 3))
        text = (f"Du {scan_label(diff.old)} au {scan_label(diff.new)} : "
                f"<b>{signed_size(diff.total_delta)}</b> "
                f"({signed_count(diff.new.file_count - diff.old.file_count)} fichiers) — "
                f"{human_count(changed)} dossiers modifiés dont {human_count(new)} nouveaux "
                f"et {human_count(removed)} disparus.")
        if grown:
            text += f"<br>Ont le plus grossi : {grown}"
        if shrunk:
            text += f"<br>Ont le plus diminué : {shrunk}"
        self.summary.setText(text)

        self.var_tree.clear()
        root_delta = diff.deltas.get(self._root)
        if root_delta:
            top = self._make_item(root_delta, full=True)
            self.var_tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.focus_tree.setSortingEnabled(False)
        self.focus_tree.clear()
        self.focus_tree.addTopLevelItems([self._make_item(d, full=True, lazy=False) for d in diff.focus[:1000]])
        self.focus_tree.setSortingEnabled(True)
        self.focus_tree.sortByColumn(3, Qt.SortOrder.DescendingOrder)

    def _make_item(self, d: DirDelta, full: bool = False, lazy: bool = True) -> QTreeWidgetItem:
        name = d.path if full else os.path.basename(d.path)
        it = _DeltaItem([name, human_size(d.old_size), human_size(d.new_size), signed_size(d.delta),
                         signed_count(d.count_delta), d.status])
        it.setData(0, PATH_ROLE, d.path)
        for col, key in ((1, d.old_size), (2, d.new_size), (3, abs(d.delta)), (4, d.count_delta)):
            it.setData(col, Qt.ItemDataRole.UserRole, key)
        it.setToolTip(0, d.path)
        for col in (1, 2, 3, 4):
            it.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Couleur doublée du signe (+/−) : l'information ne repose pas sur la couleur seule.
        color = QColor("#e66767") if d.delta > 0 else QColor("#0ca30c") if d.delta < 0 else None
        if color is not None:
            it.setForeground(3, QBrush(color))
        if lazy and self._diff and self._diff.children.get(d.path):
            it.addChild(QTreeWidgetItem([_PLACEHOLDER]))
        return it

    def _on_expand(self, item: QTreeWidgetItem) -> None:
        if item.childCount() != 1 or item.child(0).text(0) != _PLACEHOLDER or not self._diff:
            return
        item.takeChild(0)
        kids = self._diff.children.get(item.data(0, PATH_ROLE), [])
        item.addChildren([self._make_item(self._diff.deltas[p]) for p in kids[:MAX_CHILDREN]])
        if len(kids) > MAX_CHILDREN:
            item.addChild(QTreeWidgetItem([f"… {human_count(len(kids) - MAX_CHILDREN)} autres dossiers"]))

    # --- divers ---------------------------------------------------------------------

    def _context_menu(self, tree: QTreeWidget, pos) -> None:
        it = tree.itemAt(pos)
        path = it.data(0, PATH_ROLE) if it else None
        if not path:
            return
        menu = QMenu(self)
        explorer = menu.addAction("Afficher dans l'Explorateur", lambda: show_in_explorer(path))
        explorer.setEnabled(os.path.exists(path))
        menu.addAction("Copier le chemin", lambda: QGuiApplication.clipboard().setText(path))
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _delete_scan(self) -> None:
        it = self.scan_list.currentItem()
        if not it or not self._cache:
            QMessageBox.information(self, "Historique", "Sélectionnez un scan dans la liste.")
            return
        scan_id = it.data(0, PATH_ROLE)
        if scan_id == self._scans[0].id:
            QMessageBox.information(self, "Historique", "Le scan le plus récent sert à l'affichage "
                                    "courant et ne peut pas être retiré.")
            return
        if QMessageBox.question(self, "Retirer de l'historique",
                                f"Retirer le scan du {it.text(0)} de l'historique ?\n"
                                "Seules les données du cache sont effacées, aucun fichier n'est touché."
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._cache.delete_scan(scan_id)
        self.set_root(self._cache, self._root)

    def export_table(self) -> tuple[list[str], list[list]] | None:
        if self._diff is None:
            return None
        rows = sorted(self._diff.deltas.values(), key=lambda d: abs(d.delta), reverse=True)
        return (["Dossier", "Octets avant", "Octets après", "Variation (octets)",
                 "Fichiers avant", "Fichiers après", "État"],
                [[d.path, d.old_size, d.new_size, d.delta, d.old_count, d.new_count, d.status] for d in rows])

    def shutdown(self) -> None:
        for w in list(self._workers):
            w.wait()


class _DeltaItem(QTreeWidgetItem):
    """Tri numérique des colonnes chiffrées (Variation : valeur absolue)."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(col, Qt.ItemDataRole.UserRole), other.data(col, Qt.ItemDataRole.UserRole)
        if a is not None and b is not None:
            return a < b
        # Pas de super().__lt__ : PySide rappellerait cette méthode en boucle.
        return self.text(col).lower() < other.text(col).lower()
