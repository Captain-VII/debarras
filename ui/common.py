"""Éléments partagés par les vues : rôles de données, tri numérique, thème."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem, QWidget

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2


def is_dark(w: QWidget) -> bool:
    return w.palette().window().color().lightness() < 128


class SortItem(QTreeWidgetItem):
    """Tri numérique sur SORT_ROLE quand il est défini, sinon par texte."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(col, SORT_ROLE), other.data(col, SORT_ROLE)
        if a is not None and b is not None:
            return a < b
        # Pas de super().__lt__ : PySide rappellerait cette méthode en boucle.
        return self.text(col).lower() < other.text(col).lower()
