"""Onglet Statistiques : listes (types, gros fichiers, anciens, vides, temp, installeurs)."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QListWidget, QMenu, QSpinBox,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.stats import DEFAULT_OLD_DAYS, INSTALLER_MIN_AGE_DAYS, StatsResult
from ui.actions_ui import add_action_entries
from ui.tree_view import show_in_explorer
from utils.filetypes import CATEGORIES, category
from utils.export import iso
from utils.format import human_count, human_date, human_size

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2


class SortItem(QTreeWidgetItem):
    """Tri numérique sur SORT_ROLE quand il est défini."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(col, SORT_ROLE), other.data(col, SORT_ROLE)
        if a is not None and b is not None:
            return a < b
        return self.text(col).lower() < other.text(col).lower()


def _item(cells: list[tuple[str, Any]], path: str | None = None) -> SortItem:
    """cells = [(texte, clé de tri ou None), ...]"""
    it = SortItem([c[0] for c in cells])
    for i, (_, key) in enumerate(cells):
        if key is not None:
            it.setData(i, SORT_ROLE, key)
        if i > 0:
            it.setTextAlignment(i, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if path:
        it.setData(0, PATH_ROLE, path)
        it.setToolTip(0, path)
    return it


class StatsView(QWidget):
    old_days_changed = Signal(int)
    action_requested = Signal(str, list)

    SECTIONS = ("Types de fichiers", "Plus gros fichiers", "Fichiers anciens",
                "Dossiers vides", "Temp & caches", "Installeurs oubliés")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.menu = QListWidget()
        self.menu.setFixedWidth(220)
        self.stack = QStackedWidget()
        self.tables: list[QTreeWidget] = []
        headers = (
            ("Catégorie / extension", "Taille", "Fichiers", "% du total"),
            ("Fichier", "Taille", "Modifié"),
            ("Fichier", "Taille", "Dernière utilisation"),
            ("Dossier",),
            ("Élément", "Taille", "Fichiers"),
            ("Fichier", "Taille", "Modifié"),
        )
        for title, cols in zip(self.SECTIONS, headers):
            self.menu.addItem(title)
            self.stack.addWidget(self._make_table(cols))
        self.menu.currentRowChanged.connect(self._on_section)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.old_days = QSpinBox()
        self.old_days.setRange(30, 3650)
        self.old_days.setSingleStep(30)
        self.old_days.setValue(DEFAULT_OLD_DAYS)
        self.old_days.setSuffix(" jours")
        self.old_days.editingFinished.connect(lambda: self.old_days_changed.emit(self.old_days.value()))
        self.old_box = QWidget()
        ob = QHBoxLayout(self.old_box)
        ob.setContentsMargins(0, 0, 0, 0)
        ob.addWidget(QLabel("Inutilisés depuis plus de"))
        ob.addWidget(self.old_days)
        ob.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(self.summary)
        right.addWidget(self.old_box)
        right.addWidget(self.stack, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.menu)
        layout.addLayout(right, 1)

        self._summaries = [""] * len(self.SECTIONS)
        self.menu.setCurrentRow(0)

    def _make_table(self, cols: tuple[str, ...]) -> QTreeWidget:
        t = QTreeWidget()
        t.setHeaderLabels(list(cols))
        t.setRootIsDecorated(cols[0].startswith("Catégorie"))
        t.setUniformRowHeights(True)
        t.setSortingEnabled(True)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        h = t.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(cols)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos, t=t: self._context_menu(t, pos))
        self.tables.append(t)
        return t

    def _on_section(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        self.summary.setText(self._summaries[row])
        self.old_box.setVisible(row == 2)

    def _context_menu(self, table: QTreeWidget, pos) -> None:
        it = table.itemAt(pos)
        path = it.data(0, PATH_ROLE) if it else None
        if not path:
            return
        menu = QMenu(self)
        menu.addAction("Afficher dans l'Explorateur", lambda: show_in_explorer(path))
        menu.addAction("Copier le chemin", lambda: QGuiApplication.clipboard().setText(path))
        selected = [p for p in (i.data(0, PATH_ROLE) for i in table.selectedItems()) if p]
        add_action_entries(menu, selected if path in selected else [path], self.action_requested.emit)
        menu.exec(table.viewport().mapToGlobal(pos))

    # --- remplissage ----------------------------------------------------------------

    def clear(self, message: str = "") -> None:
        for t in self.tables:
            t.clear()
        self._summaries = [message] * len(self.SECTIONS)
        self._on_section(self.menu.currentRow())

    def set_loading(self) -> None:
        self.clear("Calcul des statistiques…")

    def export_table(self) -> tuple[list[str], list[list]] | None:
        """Section affichée, en valeurs brutes (octets, dates ISO)."""
        r = getattr(self, "_result", None)
        if r is None:
            return None
        sec = self.menu.currentRow()
        if sec == 0:
            return (["Catégorie", "Extension", "Octets", "Fichiers"],
                    [[category(e), e, s, n] for e, s, n in r.by_ext])
        if sec in (1, 2, 5):
            files = {1: r.largest, 2: r.old, 5: r.installers}[sec]
            return (["Chemin", "Octets", "Modifié", "Dernière utilisation"],
                    [[f.path, f.size, iso(f.mtime), iso(f.last_used)] for f in files])
        if sec == 3:
            return ["Dossier"], [[p] for p in r.empty_dirs]
        return (["Chemin", "Type", "Octets", "Fichiers"],
                [[d.path, "dossier", d.size, d.file_count] for d in r.temp_dirs]
                + [[f.path, "fichier", f.size, 1] for f in r.temp_files])

    def set_result(self, r: StatsResult) -> None:
        self.clear()
        self._result = r
        total = r.total_size or 1

        def fill(idx: int, rows: list[QTreeWidgetItem], summary: str,
                 sort_col: int = 1) -> None:
            t = self.tables[idx]
            t.setSortingEnabled(False)
            t.addTopLevelItems(rows)
            t.setSortingEnabled(True)
            t.sortByColumn(sort_col, Qt.SortOrder.DescendingOrder if sort_col else Qt.SortOrder.AscendingOrder)
            self._summaries[idx] = summary
            self.menu.item(idx).setText(f"{self.SECTIONS[idx]} ({human_count(len(rows))})")

        def size_cells(size: int) -> tuple[str, int]:
            return human_size(size), size

        def date_cells(ts: float) -> tuple[str, float]:
            return human_date(ts), ts

        # Types : catégories (ordre fixe) avec extensions en enfants.
        exts_by_cat: dict[str, list[tuple[str, int, int]]] = {c: [] for c in CATEGORIES}
        for ext, size, count in r.by_ext:
            exts_by_cat[category(ext)].append((ext, size, count))
        cat_rows = []
        for cat in CATEGORIES:
            size, count = r.by_category.get(cat, (0, 0))
            if not count:
                continue
            parent = _item([(cat, None), size_cells(size), (human_count(count), count),
                            (f"{100 * size / total:.1f} %".replace(".", ","), size)])
            for ext, s, c in exts_by_cat[cat]:
                parent.addChild(_item([(ext or "(sans extension)", None), size_cells(s),
                                       (human_count(c), c),
                                       (f"{100 * s / total:.1f} %".replace(".", ","), s)]))
            cat_rows.append(parent)
        fill(0, cat_rows, f"{human_count(r.file_count)} fichiers — {human_size(r.total_size)} "
                          f"— {human_count(len(r.by_ext))} extensions")

        fill(1, [_item([(f.path, None), size_cells(f.size), date_cells(f.mtime)], f.path)
                 for f in r.largest],
             f"Les {human_count(len(r.largest))} plus gros fichiers "
             f"({human_size(sum(f.size for f in r.largest))} au total).")

        fill(2, [_item([(f.path, None), size_cells(f.size), date_cells(f.last_used)], f.path)
                 for f in r.old],
             f"{human_count(r.old_total[1])} fichiers ({human_size(r.old_total[0])}) ni ouverts ni "
             f"modifiés depuis plus de {r.old_days} jours — {len(r.old)} plus gros affichés. "
             "Windows ne met pas toujours à jour la date d'accès : la date la plus récente "
             "entre accès et modification est utilisée.")

        fill(3, [_item([(p, None)], p) for p in r.empty_dirs],
             "Dossiers ne contenant aucun fichier (seul le plus haut de chaque branche vide est "
             "listé ; les dossiers dont une partie est exclue ou inaccessible sont ignorés).",
             sort_col=0)

        temp_rows = [_item([(d.path + "\\", None), size_cells(d.size),
                            (human_count(d.file_count), d.file_count)], d.path) for d in r.temp_dirs]
        temp_rows += [_item([(f.path, None), size_cells(f.size), ("1", 1)], f.path)
                      for f in r.temp_files]
        temp_size = sum(d.size for d in r.temp_dirs) + sum(f.size for f in r.temp_files)
        fill(4, temp_rows, f"{human_size(temp_size)} dans des dossiers temporaires/caches et "
                           "fichiers .tmp/.dmp/~$. Ces caches sont en général régénérés "
                           "automatiquement, mais vérifiez avant de les supprimer.")

        fill(5, [_item([(f.path, None), size_cells(f.size), date_cells(f.mtime)], f.path)
                 for f in r.installers],
             f"{human_size(sum(f.size for f in r.installers))} d'installeurs (.msi, .msix, setup*.exe, "
             f"exécutables et ISO dans Téléchargements) datant de plus de {INSTALLER_MIN_AGE_DAYS} jours.")

        self._on_section(self.menu.currentRow())

    def current_old_days(self) -> int:
        return self.old_days.value()
