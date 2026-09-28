"""Arborescence triée par taille, chargée à la demande depuis le cache."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QFileIconProvider, QHeaderView, QMenu, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTreeView,
)

from core.cache import Cache
from ui.actions_ui import add_action_entries
from utils.format import human_count, human_date, human_size

COL_NAME, COL_SIZE, COL_PERCENT, COL_FILES, COL_MTIME = range(5)
HEADERS = ("Nom", "Taille", "% parent", "Fichiers", "Modifié")
SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class Node:
    __slots__ = ("path", "name", "is_dir", "size", "count", "mtime",
                 "parent", "children", "fetched", "row")

    def __init__(self, path: str, name: str, is_dir: bool, size: int, count: int,
                 mtime: float | None, parent: Node | None) -> None:
        self.path, self.name, self.is_dir = path, name, is_dir
        self.size, self.count, self.mtime = size, count, mtime
        self.parent = parent
        self.children: list[Node] = []
        self.fetched = not is_dir
        self.row = 0


class SizeTreeModel(QAbstractItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cache: Cache | None = None
        self._scan_id = 0
        self._root = Node("", "", True, 0, 0, None, None)
        self._root.fetched = True
        self._sort = (COL_SIZE, Qt.SortOrder.DescendingOrder)
        icons = QFileIconProvider()
        self._dir_icon = icons.icon(QFileIconProvider.IconType.Folder)
        self._file_icon = icons.icon(QFileIconProvider.IconType.File)

    def load(self, cache: Cache, scan_id: int, root: str) -> None:
        """Affiche le résultat du scan `scan_id` à partir de `root`."""
        self.beginResetModel()
        self._cache, self._scan_id = cache, scan_id
        self._root.children = []
        size, count = cache.dir_info(scan_id, root) or (0, 0)
        top = Node(root, root, True, size, count, None, self._root)
        self._root.children.append(top)
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._root.children = []
        self.endResetModel()

    def node(self, index: QModelIndex) -> Node | None:
        return index.internalPointer() if index.isValid() else None

    # --- structure ------------------------------------------------------------------

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        p = self.node(parent) or self._root
        if 0 <= row < len(p.children):
            return self.createIndex(row, column, p.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]
        n = self.node(index)
        if n is None or n.parent is None or n.parent is self._root:
            return QModelIndex()
        return self.createIndex(n.parent.row, 0, n.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        return len((self.node(parent) or self._root).children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(HEADERS)

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        n = self.node(parent) or self._root
        return n.is_dir and (not n.fetched or bool(n.children))

    def canFetchMore(self, parent: QModelIndex) -> bool:
        n = self.node(parent)
        return n is not None and not n.fetched

    def fetchMore(self, parent: QModelIndex) -> None:
        n = self.node(parent)
        if n is None or n.fetched or self._cache is None:
            return
        n.fetched = True
        kids = [Node(p, os.path.basename(p), True, s, c, None, n)
                for p, s, c in self._cache.child_dirs(self._scan_id, n.path)]
        kids += [Node(p, name, False, s, 1, m, n)
                 for p, name, s, m in self._cache.child_files(n.path)]
        self._sort_list(kids)
        if kids:
            self.beginInsertRows(parent, 0, len(kids) - 1)
            n.children = kids
            self.endInsertRows()
        else:
            # Plus de flèche d'expansion pour un dossier vide.
            self.dataChanged.emit(parent, parent)

    # --- données --------------------------------------------------------------------

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        n = self.node(index)
        if n is None:
            return None
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_NAME:
                return n.name
            if col == COL_SIZE:
                return human_size(n.size)
            if col == COL_PERCENT:
                return f"{self._percent(n):.1f} %".replace(".", ",")
            if col == COL_FILES:
                return human_count(n.count) if n.is_dir else ""
            if col == COL_MTIME:
                return human_date(n.mtime) if n.mtime else ""
        elif role == SORT_ROLE:
            return self._percent(n) if col == COL_PERCENT else None
        elif role == Qt.ItemDataRole.DecorationRole and col == COL_NAME:
            return self._dir_icon if n.is_dir else self._file_icon
        elif role == Qt.ItemDataRole.TextAlignmentRole and col != COL_NAME:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole:
            return n.path
        return None

    def _percent(self, n: Node) -> float:
        p = n.parent
        if p is None or p is self._root or p.size <= 0:
            return 100.0
        return 100.0 * n.size / p.size

    # --- tri ------------------------------------------------------------------------

    def _key(self, col: int):
        return {
            COL_NAME: lambda n: (not n.is_dir, n.name.lower()),
            COL_FILES: lambda n: n.count,
            COL_MTIME: lambda n: n.mtime or 0.0,
        }.get(col, lambda n: n.size)  # taille et % : même ordre

    def _sort_list(self, nodes: list[Node]) -> None:
        col, order = self._sort
        nodes.sort(key=self._key(col), reverse=order == Qt.SortOrder.DescendingOrder)
        for i, n in enumerate(nodes):
            n.row = i

    def _sort_rec(self, node: Node) -> None:
        self._sort_list(node.children)
        for c in node.children:
            if c.children:
                self._sort_rec(c)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        refs = [(i.internalPointer(), i.column()) for i in old]
        for top in self._root.children:
            self._sort_rec(top)
        new = [self.createIndex(n.row, c, n) for n, c in refs]
        self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()


class PercentDelegate(QStyledItemDelegate):
    """Colonne % dessinée comme une barre proportionnelle."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        pct = index.data(SORT_ROLE) or 0.0
        if not option.state & QStyle.StateFlag.State_Selected:
            r = option.rect.adjusted(2, 3, -2, -3)
            r.setWidth(int(r.width() * min(pct, 100.0) / 100.0))
            color = QColor(option.palette.highlight().color())
            color.setAlpha(90)
            painter.fillRect(r, color)
        super().paint(painter, option, index)


class TreeView(QTreeView):
    show_in_treemap = Signal(str)
    action_requested = Signal(str, list)  # type d'action, chemins

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tree_model = SizeTreeModel(self)
        self.setModel(self.tree_model)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.sortByColumn(COL_SIZE, Qt.SortOrder.DescendingOrder)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setItemDelegateForColumn(COL_PERCENT, PercentDelegate(self))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_SIZE, 90), (COL_PERCENT, 110), (COL_FILES, 90), (COL_MTIME, 130)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(col, width)

    def load(self, cache: Cache, scan_id: int, root: str) -> None:
        self.tree_model.load(cache, scan_id, root)
        self.expand(self.tree_model.index(0, 0))

    def clear(self) -> None:
        self.tree_model.clear()

    def selected_paths(self) -> list[str]:
        nodes = (self.tree_model.node(i) for i in self.selectionModel().selectedRows())
        return [n.path for n in nodes if n]

    def _context_menu(self, pos) -> None:
        n = self.tree_model.node(self.indexAt(pos))
        if n is None:
            return
        menu = QMenu(self)
        menu.addAction("Afficher dans l'Explorateur", lambda: show_in_explorer(n.path))
        menu.addAction("Copier le chemin", lambda: QGuiApplication.clipboard().setText(n.path))
        menu.addAction("Afficher dans le treemap", lambda: self.show_in_treemap.emit(n.path))
        selected = self.selected_paths()
        add_action_entries(menu, selected if n.path in selected else [n.path], self.action_requested.emit)
        menu.exec(self.viewport().mapToGlobal(pos))


def show_in_explorer(path: str) -> None:
    """Ouvre l'Explorateur avec l'élément sélectionné (n'exécute jamais le fichier)."""
    subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
