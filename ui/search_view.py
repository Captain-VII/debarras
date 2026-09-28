"""Onglet Recherche : filtres combinables sur le cache, résultats triables, actions."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QGridLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.cache import Cache
from core.search import MAX_RESULTS, Hit, SearchQuery, search_files
from ui.actions_ui import add_action_entries
from ui.tree_view import show_in_explorer
from utils.filetypes import CATEGORIES
from utils.export import iso
from utils.format import human_count, human_date, human_size

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
KEY_ROLE = Qt.ItemDataRole.UserRole + 2
MB = 1024 * 1024

# (libellé, modifiés depuis moins de N jours, modifiés il y a plus de N jours)
DATE_FILTERS = [("Toutes dates", 0, 0), ("Moins de 7 jours", 7, 0), ("Moins de 30 jours", 30, 0),
                ("Moins d'un an", 365, 0), ("Plus d'un an", 0, 365), ("Plus de 2 ans", 0, 730),
                ("Plus de 5 ans", 0, 1825)]


class _Item(QTreeWidgetItem):
    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(col, KEY_ROLE), other.data(col, KEY_ROLE)
        if a is not None and b is not None:
            return a < b
        return self.text(col).lower() < other.text(col).lower()


class SearchView(QWidget):
    action_requested = Signal(str, list)
    show_in_treemap = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cache: Cache | None = None
        self._root = ""
        self.hits: list[Hit] = []

        self.text = QLineEdit()
        self.text.setPlaceholderText("Nom contient…  (jokers acceptés : *.iso, rapport_??.pdf)")
        self.text.setClearButtonEnabled(True)
        self.category = QComboBox()
        self.category.addItem("Tous types", None)
        for c in CATEGORIES:
            self.category.addItem(c, c)
        self.exts = QLineEdit()
        self.exts.setPlaceholderText(".pdf, .docx")
        self.min_mb = self._size_box()
        self.max_mb = self._size_box()
        self.date = QComboBox()
        for label, *_ in DATE_FILTERS:
            self.date.addItem(label)
        reset = QPushButton("Effacer les filtres")
        reset.clicked.connect(self.reset)

        grid = QGridLayout()
        grid.addWidget(QLabel("Nom :"), 0, 0)
        grid.addWidget(self.text, 0, 1, 1, 3)
        grid.addWidget(QLabel("Type :"), 0, 4)
        grid.addWidget(self.category, 0, 5)
        grid.addWidget(QLabel("Extensions :"), 0, 6)
        grid.addWidget(self.exts, 0, 7)
        grid.addWidget(QLabel("Taille min :"), 1, 0)
        grid.addWidget(self.min_mb, 1, 1)
        grid.addWidget(QLabel("max :"), 1, 2)
        grid.addWidget(self.max_mb, 1, 3)
        grid.addWidget(QLabel("Modifié :"), 1, 4)
        grid.addWidget(self.date, 1, 5)
        grid.addWidget(reset, 1, 7)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(7, 1)

        self.summary = QLabel()
        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Nom", "Dossier", "Taille", "Modifié"])
        self.results.setRootIsDecorated(False)
        self.results.setUniformRowHeights(True)
        self.results.setSortingEnabled(True)
        self.results.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        h = self.results.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.results.setColumnWidth(0, 320)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(grid)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)

        # Recherche automatique, temporisée pendant la frappe.
        self._timer = QTimer(self, singleShot=True, interval=300)
        self._timer.timeout.connect(self.run)
        for w in (self.text, self.exts):
            w.textChanged.connect(self._timer.start)
        for w in (self.category, self.date):
            w.currentIndexChanged.connect(self._timer.start)
        for w in (self.min_mb, self.max_mb):
            w.valueChanged.connect(self._timer.start)
        self.set_scan(None, "")

    @staticmethod
    def _size_box() -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0, 10_000_000)
        box.setDecimals(1)
        box.setSuffix(" Mo")
        box.setSpecialValueText("—")
        return box

    def set_scan(self, cache: Cache | None, root: str) -> None:
        self._cache, self._root = cache, root
        self.run()

    def focus_search(self) -> None:
        self.text.setFocus()
        self.text.selectAll()

    def reset(self) -> None:
        for w in (self.text, self.exts):
            w.clear()
        self.category.setCurrentIndex(0)
        self.date.setCurrentIndex(0)
        self.min_mb.setValue(0)
        self.max_mb.setValue(0)

    def query(self) -> SearchQuery:
        _, newer, older = DATE_FILTERS[self.date.currentIndex()]
        exts = [e.strip() for e in self.exts.text().replace(";", ",").split(",") if e.strip()]
        return SearchQuery(self.text.text(), self.category.currentData(), exts,
                           int(self.min_mb.value() * MB), int(self.max_mb.value() * MB), newer, older)

    def run(self) -> None:
        self.results.clear()
        self.hits = []
        if not self._cache or not self._root:
            self.summary.setText("Aucun scan chargé.")
            return
        q = self.query()
        if q.is_empty():
            self.summary.setText("Saisissez un nom ou choisissez un filtre.")
            return
        res = search_files(self._cache.conn, self._root, q)
        self.hits = res.hits
        items = []
        for h in res.hits:
            it = _Item([h.name, os.path.dirname(h.path), human_size(h.size), human_date(h.mtime)])
            it.setData(0, PATH_ROLE, h.path)
            it.setData(2, KEY_ROLE, h.size)
            it.setData(3, KEY_ROLE, h.mtime)
            it.setToolTip(0, h.path)
            it.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(it)
        self.results.setSortingEnabled(False)
        self.results.addTopLevelItems(items)
        self.results.setSortingEnabled(True)
        self.results.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        more = f" — {human_count(MAX_RESULTS)} plus gros affichés" if res.count > MAX_RESULTS else ""
        self.summary.setText(f"{human_count(res.count)} fichier(s) — {human_size(res.total_size)}{more}")

    def export_table(self) -> tuple[list[str], list[list]] | None:
        if not self.hits:
            return None
        return ["Chemin", "Nom", "Octets", "Modifié"], [[h.path, h.name, h.size, iso(h.mtime)] for h in self.hits]

    def _context_menu(self, pos) -> None:
        it = self.results.itemAt(pos)
        path = it.data(0, PATH_ROLE) if it else None
        if not path:
            return
        menu = QMenu(self)
        menu.addAction("Afficher dans l'Explorateur", lambda: show_in_explorer(path))
        menu.addAction("Copier le chemin", lambda: QGuiApplication.clipboard().setText(path))
        menu.addAction("Afficher dans le treemap", lambda: self.show_in_treemap.emit(path))
        selected = [i.data(0, PATH_ROLE) for i in self.results.selectedItems()]
        add_action_entries(menu, selected if path in selected else [path], self.action_requested.emit)
        menu.exec(self.results.viewport().mapToGlobal(pos))
