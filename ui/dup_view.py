"""Onglet Doublons : recherche, groupes à cocher, sélection automatique."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QProgressBar, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from core.duplicates import DupFile, DuplicateFinder
from core.actions import ARCHIVE, MOVE, TRASH, protection
from utils.export import iso
from ui.tree_view import show_in_explorer
from utils.format import human_count, human_date, human_duration, human_size

FILE_ROLE = Qt.ItemDataRole.UserRole + 1

KEEP_NEWEST, KEEP_OLDEST, KEEP_PRIORITY = range(3)  # index des règles de DupView


class DupView(QWidget):
    """Groupes de fichiers « en trop » à cocher. Sous-classable (voir SimilarView) :
    RULES, SEARCH_LABEL, HEADERS, _make_finder, _group_label, _row, _decorate, _result_text."""

    selection_changed = Signal(int, "qlonglong")  # nb cochés, octets récupérables
    action_requested = Signal(str, list)

    RULES = [("Garder le plus récent", "newest"), ("Garder le plus ancien", "oldest"),
             ("Garder le chemin prioritaire", "priority")]
    SEARCH_LABEL = "Rechercher les doublons"
    MIN_SIZE_KO = 1024
    HEADERS = ["Fichier", "Taille", "Modifié"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._db_path: str | Path | None = None
        self._root = ""
        self._finder = None
        self._result = None
        self._updating = False
        self.whitelist: list[str] = []

        # --- recherche
        self.min_size = QSpinBox()
        self.min_size.setRange(1, 10_000_000)
        self.min_size.setValue(self.MIN_SIZE_KO)
        self.min_size.setSuffix(" Ko")
        self.search_btn = QPushButton(self.SEARCH_LABEL)
        self.search_btn.clicked.connect(self.start_search)
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        self.progress = QProgressBar()
        self.progress.setMaximumHeight(14)
        self.progress.hide()
        top = QHBoxLayout()
        top.addWidget(QLabel("Taille minimale :"))
        top.addWidget(self.min_size)
        self._extra_search_options(top)
        top.addWidget(self.search_btn)
        top.addWidget(self.cancel_btn)
        top.addWidget(self.progress, 3)
        top.addStretch(1)

        # --- sélection automatique
        self.rule = QComboBox()
        for label, key in self.RULES:
            self.rule.addItem(label, key)
        self.rule.currentIndexChanged.connect(
            lambda _: self._priority_box.setVisible(self.rule.currentData() == "priority"))
        self.priority = QLineEdit()
        self.priority.setPlaceholderText(r"Dossier(s) à conserver en priorité, séparés par « ; »")
        pick = QPushButton("…")
        pick.setFixedWidth(30)
        pick.clicked.connect(self._pick_priority)
        self._priority_box = QWidget()
        pb = QHBoxLayout(self._priority_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.addWidget(self.priority, 1)
        pb.addWidget(pick)
        self._priority_box.hide()
        apply_btn = QPushButton("Appliquer la sélection auto")
        apply_btn.clicked.connect(self.auto_select)
        clear_btn = QPushButton("Tout décocher")
        clear_btn.clicked.connect(self.uncheck_all)
        auto = QHBoxLayout()
        auto.addWidget(self.rule)
        auto.addWidget(self._priority_box, 1)
        auto.addWidget(apply_btn)
        auto.addWidget(clear_btn)
        auto.addStretch(0)

        self.info = QLabel("Chargez un scan puis lancez la recherche.")
        self.info.setWordWrap(True)
        self.selection_label = QLabel()

        # --- groupes
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.HEADERS)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        h = self.tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(self.HEADERS)):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.info)
        layout.addLayout(auto)
        self.action_btns = []
        bottom = QHBoxLayout()
        bottom.addWidget(self.selection_label, 1)
        for label, kind in (("Corbeille…", TRASH), ("Déplacer…", MOVE), ("Archiver…", ARCHIVE)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=kind: self._request(k))
            bottom.addWidget(b)
            self.action_btns.append(b)
        layout.addWidget(self._central_widget(), 1)
        layout.addLayout(bottom)
        self.set_scan(None, "")

    # --- points d'extension -------------------------------------------------------------

    def _extra_search_options(self, layout: QHBoxLayout) -> None:
        """Options de recherche supplémentaires (sous-classes)."""

    def _central_widget(self) -> QWidget:
        return self.tree

    def _make_finder(self):
        return DuplicateFinder(self._db_path, self._root, self.min_size.value() * 1024)

    def _group_label(self, g) -> list[str]:
        return [f"{len(g.files)} × {human_size(g.size)} — {human_size(g.wasted)} récupérables",
                human_size(g.size * len(g.files)), ""]

    def _row(self, f) -> list[str]:
        return [f.path, human_size(f.size), human_date(f.mtime)]

    def _decorate(self, child: QTreeWidgetItem, f) -> None:
        """Retouche d'une ligne fichier (vignette…)."""

    def _ordered(self, g) -> list:
        return sorted(g.files, key=lambda f: f.path.lower())

    def _result_text(self, r) -> str:
        return (f"{human_count(len(r.groups))} groupes, "
                f"{human_count(sum(len(g.files) for g in r.groups))} fichiers — "
                f"{human_size(r.wasted)} récupérables. {human_count(r.candidates)} candidats, "
                f"{human_size(r.hashed_bytes)} lus en {human_duration(r.duration)}.")

    # --- contexte -------------------------------------------------------------------

    def set_scan(self, db_path: str | Path | None, root: str) -> None:
        """Nouveau scan chargé : résultats conservés (épurés) si c'est le même dossier."""
        if root and root == self._root and self._result is not None:
            self._db_path = db_path
            self.prune_missing()
            return
        if self._finder:
            self._finder.cancel()
        self._db_path, self._root = db_path, root
        self._result = None
        self.tree.clear()
        self.search_btn.setEnabled(bool(root) and self._finder is None)
        self.info.setText(f"Dossier : {root}" if root else "Chargez un scan puis lancez la recherche.")
        self._update_selection()

    # --- recherche ------------------------------------------------------------------

    def start_search(self) -> None:
        if not self._root or self._finder:
            return
        self.tree.clear()
        self._result = None
        self._finder = self._make_finder()
        self._finder.progress.connect(self._on_progress)
        self._finder.result_ready.connect(self._on_result)
        self._finder.failed.connect(self.info.setText)
        self._finder.finished.connect(self._on_finder_done)
        self.search_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.info.setText("Recherche en cours…")
        self._finder.start()

    def _cancel(self) -> None:
        if self._finder:
            self.cancel_btn.setEnabled(False)
            self._finder.cancel()

    def _on_progress(self, phase: str, done: int, total: int) -> None:
        # QProgressBar est en int 32 bits : on travaille en pour mille.
        self.progress.setRange(0, 1000)
        self.progress.setValue(int(1000 * done / total) if total else 0)
        if phase in ("Hash complet", "Comparaison du contenu"):  # progression en octets
            self.info.setText(f"{phase} : {human_size(done)} / {human_size(total)}")
        else:
            self.info.setText(f"{phase} : {human_count(done)} / {human_count(total)}")

    def _on_finder_done(self) -> None:
        if self._finder:
            self._finder.deleteLater()
        self._finder = None
        self.progress.hide()
        self.cancel_btn.setEnabled(False)
        self.search_btn.setEnabled(bool(self._root))

    def _on_result(self, r) -> None:
        if r.root != self._root:
            return  # un autre scan a été chargé entre-temps
        if r.cancelled:
            self.info.setText("Recherche annulée (les hash déjà calculés sont conservés).")
            return
        self._result = r
        self._fill(r.groups)
        notes = []
        if r.skipped_cloud:
            notes.append(f"{human_count(r.skipped_cloud)} fichiers en ligne ignorés (non téléchargés)")
        if r.skipped_changed:
            notes.append(f"{human_count(r.skipped_changed)} modifiés/supprimés depuis le scan")
        if r.skipped_error:
            notes.append(f"{human_count(r.skipped_error)} illisibles")
        self.info.setText(self._result_text(r) + (" " + " ; ".join(notes) + "." if notes else ""))

    def _fill(self, groups: list) -> None:
        self._updating = True
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        items = []
        for g in groups:
            head = QTreeWidgetItem(self._group_label(g))
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for f in self._ordered(g):
                child = QTreeWidgetItem(self._row(f))
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                               | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setData(0, FILE_ROLE, f)
                child.setToolTip(0, f.path)
                for col in range(1, len(self.HEADERS)):
                    child.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._decorate(child, f)
                head.addChild(child)
            items.append(head)
        self.tree.addTopLevelItems(items)
        if len(items) <= 200:
            self.tree.expandAll()
        else:
            for it in items[:200]:
                it.setExpanded(True)
        self.tree.setUpdatesEnabled(True)
        self._updating = False
        self._update_selection()

    # --- cases à cocher ---------------------------------------------------------------

    def _groups(self):
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            yield head, [head.child(j) for j in range(head.childCount())]

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or column != 0 or item.parent() is None:
            return
        head = item.parent()
        kids = [head.child(j) for j in range(head.childCount())]
        if all(k.checkState(0) == Qt.CheckState.Checked for k in kids):
            # Garde-fou : au moins un exemplaire de chaque groupe est conservé.
            self._updating = True
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self._updating = False
            self.selection_label.setText("⚠ Impossible de cocher tous les fichiers d'un groupe : "
                                         "au moins un exemplaire est toujours conservé.")
            return
        self._update_selection()

    def _update_selection(self) -> None:
        files = self.checked_files()
        size = sum(f.size for f in files)
        self.selection_label.setText(
            f"{human_count(len(files))} fichier(s) coché(s) — {human_size(size)} récupérables"
            if files else "Aucun fichier coché."
        )
        self.selection_changed.emit(len(files), size)
        for b in getattr(self, "action_btns", []):
            b.setEnabled(bool(files))

    def checked_files(self) -> list[DupFile]:
        return [k.data(0, FILE_ROLE) for _, kids in self._groups() for k in kids
                if k.checkState(0) == Qt.CheckState.Checked]

    def uncheck_all(self) -> None:
        self._set_checks(lambda head, kids: [])

    def _set_checks(self, choose) -> None:
        """choose(head, kids) -> enfants à cocher."""
        self._updating = True
        for head, kids in self._groups():
            to_check = set(map(id, choose(head, kids)))
            for k in kids:
                k.setCheckState(0, Qt.CheckState.Checked if id(k) in to_check else Qt.CheckState.Unchecked)
        self._updating = False
        self._update_selection()

    # --- sélection automatique --------------------------------------------------------

    def auto_select(self) -> None:
        rule = self.rule.currentData()
        prefixes = [os.path.normcase(os.path.abspath(p.strip().strip('"'))).rstrip(os.sep) + os.sep
                    for p in self.priority.text().split(";") if p.strip()]
        if rule == "priority" and not prefixes:
            self.selection_label.setText("⚠ Indiquez au moins un dossier prioritaire.")
            return
        untouched = 0

        def keeper(kids: list[QTreeWidgetItem]) -> QTreeWidgetItem | None:
            nonlocal untouched
            files = [(k, k.data(0, FILE_ROLE)) for k in kids]
            if rule == "resolution":  # images : meilleure définition, puis plus lourde, puis récente
                return max(files, key=lambda kf: (kf[1].pixels, kf[1].size, kf[1].mtime))[0]
            if rule == "largest":
                return max(files, key=lambda kf: (kf[1].size, kf[1].mtime))[0]
            if rule == "newest":
                return max(files, key=lambda kf: (kf[1].mtime, -len(kf[1].path)))[0]
            if rule == "oldest":
                return min(files, key=lambda kf: (kf[1].mtime, len(kf[1].path)))[0]
            # Chemin prioritaire : le premier dossier de la liste l'emporte, puis le plus récent.
            for pre in prefixes:
                hits = [kf for kf in files if os.path.normcase(kf[1].path).startswith(pre)]
                if hits:
                    return max(hits, key=lambda kf: kf[1].mtime)[0]
            untouched += 1
            return None

        def choose(head, kids):
            keep = keeper(kids)
            if keep is None:
                return []
            # Liste blanche : un fichier protégé n'est jamais coché.
            return [k for k in kids if k is not keep
                    and not protection(k.data(0, FILE_ROLE).path, self.whitelist)]

        self._set_checks(choose)
        if untouched:
            self.selection_label.setText(
                self.selection_label.text()
                + f" — {human_count(untouched)} groupe(s) sans fichier dans le chemin prioritaire, laissés décochés."
            )

    def _pick_priority(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Dossier prioritaire", self._root)
        if path:
            current = self.priority.text().strip()
            self.priority.setText(f"{current};{os.path.normpath(path)}" if current else os.path.normpath(path))

    # --- menu contextuel --------------------------------------------------------------

    def _context_menu(self, pos) -> None:
        it = self.tree.itemAt(pos)
        f: DupFile | None = it.data(0, FILE_ROLE) if it else None
        if f is None:
            return
        menu = QMenu(self)
        menu.addAction("Garder uniquement celui-ci", lambda: self._keep_only(it))
        menu.addSeparator()
        menu.addAction("Afficher dans l'Explorateur", lambda: show_in_explorer(f.path))
        menu.addAction("Copier le chemin", lambda: QGuiApplication.clipboard().setText(f.path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _keep_only(self, item: QTreeWidgetItem) -> None:
        head = item.parent()
        self._updating = True
        for j in range(head.childCount()):
            k = head.child(j)
            k.setCheckState(0, Qt.CheckState.Unchecked if k is item else Qt.CheckState.Checked)
        self._updating = False
        self._update_selection()

    def _request(self, kind: str) -> None:
        """Vérifie qu'un exemplaire de chaque groupe reste sur le disque avant d'agir."""
        paths: list[str] = []
        for head, kids in self._groups():
            checked = [k for k in kids if k.checkState(0) == Qt.CheckState.Checked]
            if not checked:
                continue
            kept = [k for k in kids if k.checkState(0) != Qt.CheckState.Checked
                    and os.path.exists(k.data(0, FILE_ROLE).path)]
            if not kept:
                self.selection_label.setText("⚠ Un groupe n'a plus aucun exemplaire conservé sur le disque : "
                                             "action refusée. Relancez la recherche.")
                self.tree.scrollToItem(head)
                return
            paths += [k.data(0, FILE_ROLE).path for k in checked]
        if paths:
            self.action_requested.emit(kind, paths)

    def prune_missing(self) -> None:
        """Retire les fichiers qui n'existent plus et les groupes devenus uniques."""
        if self._result is None:
            return
        groups = []
        for g in self._result.groups:
            g.files = [f for f in g.files if os.path.exists(f.path)]
            if len(g.files) > 1:
                groups.append(g)
        self._result.groups = groups
        self._fill(groups)
        self.info.setText(f"{human_count(len(groups))} groupes restants — "
                          f"{human_size(self._result.wasted)} récupérables.")

    def export_table(self) -> tuple[list[str], list[list]] | None:
        if self._result is None:
            return None
        checked = {f.path for f in self.checked_files()}
        rows = [[n, g.digest, f.path, f.size, iso(f.mtime), "oui" if f.path in checked else ""]
                for n, g in enumerate(self._result.groups, 1) for f in g.files]
        return ["Groupe", "Empreinte xxh3-128", "Chemin", "Octets", "Modifié", "Coché"], rows

    def shutdown(self) -> None:
        if self._finder:
            self._finder.cancel()
            self._finder.wait()
