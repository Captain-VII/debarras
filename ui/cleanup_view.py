"""Onglet Nettoyage guidé : caches des navigateurs, fichiers temporaires, Windows Update…"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.cleanup import (
    RECYCLE_KIND, TRASH_KIND, WINDOWS_KIND, CleanupWorker, Target, open_recycle_bin, open_windows_cleanup,
)
from ui.tree_view import show_in_explorer
from utils.format import human_count, human_size

TARGET_ROLE = Qt.ItemDataRole.UserRole + 1
PATH_ROLE = Qt.ItemDataRole.UserRole + 2
MAX_CHILDREN = 300


class CleanupView(QWidget):
    cleanup_requested = Signal(list, dict)   # chemins, tailles
    targets_ready = Signal(list)             # résultat de chaque analyse (list[Target])

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: CleanupWorker | None = None
        self._updating = False
        self.analyzed = False

        intro = QLabel(
            "<b>Nettoyage guidé</b> — uniquement des emplacements recréés automatiquement par Windows "
            "et vos logiciels. Les éléments cochés vont à la <b>corbeille</b> (annulable) : "
            "videz-la ensuite depuis Windows pour libérer l'espace.")
        intro.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Élément", "Taille", "Fichiers", ""])
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setWordWrap(True)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._on_changed)
        self.tree.itemDoubleClicked.connect(self._on_double)

        self.analyze_btn = QPushButton("Analyser")
        self.analyze_btn.clicked.connect(self.analyze)
        self.status = QLabel()
        self.clean_btn = QPushButton("Mettre la sélection à la corbeille…")
        self.clean_btn.setEnabled(False)
        self.clean_btn.clicked.connect(self._request)
        row = QHBoxLayout()
        row.addWidget(self.analyze_btn)
        row.addWidget(self.status, 1)
        row.addWidget(self.clean_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.tree, 1)
        layout.addLayout(row)

    # --- analyse -------------------------------------------------------------------------

    def analyze(self) -> None:
        if self._worker:
            return
        self.analyzed = True
        self.analyze_btn.setEnabled(False)
        self.clean_btn.setEnabled(False)
        self.status.setText("Mesure des emplacements…")
        self._worker = CleanupWorker()
        self._worker.done.connect(self._fill)
        self._worker.failed.connect(self.status.setText)
        self._worker.finished.connect(self._worker_done)
        self._worker.start()

    def _worker_done(self) -> None:
        if self._worker:
            self._worker.deleteLater()
        self._worker = None
        self.analyze_btn.setEnabled(True)

    def _fill(self, targets: list[Target]) -> None:
        self._updating = True
        self.tree.clear()
        for t in targets:
            size = human_size(t.size) if t.size_known else "?"
            top = QTreeWidgetItem([t.title, size, human_count(t.count) if t.size_known else "", ""])
            top.setData(0, TARGET_ROLE, t)
            tip = t.description + (f"\n\n⚠ {t.note}" if t.note else "")
            top.setToolTip(0, tip)
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for col in (1, 2):
                top.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            desc = QTreeWidgetItem([t.description + (f"\n⚠ {t.note}" if t.note else "")])
            desc.setFlags(Qt.ItemFlag.ItemIsEnabled)
            desc.setToolTip(0, tip)
            top.addChild(desc)
            if t.kind == TRASH_KIND and t.paths:
                top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                for p in sorted(t.paths, key=lambda x: t.sizes.get(x, 0), reverse=True)[:MAX_CHILDREN]:
                    child = QTreeWidgetItem([p, human_size(t.sizes.get(p, 0)), ""])
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Checked if t.recommended else Qt.CheckState.Unchecked)
                    child.setData(0, PATH_ROLE, p)
                    child.setToolTip(0, p + "\nDouble-clic : afficher dans l'Explorateur")
                    child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    top.addChild(child)
                if len(t.paths) > MAX_CHILDREN:
                    more = QTreeWidgetItem([f"… et {human_count(len(t.paths) - MAX_CHILDREN)} autres "
                                            "(traités avec la catégorie)"])
                    more.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    top.addChild(more)
            elif t.kind == TRASH_KIND:
                top.setText(1, "0 o")
            self.tree.addTopLevelItem(top)
            if t.kind in (WINDOWS_KIND, RECYCLE_KIND):
                btn = QPushButton("Ouvrir le Nettoyage de disque…" if t.kind == WINDOWS_KIND else "Ouvrir la corbeille")
                btn.clicked.connect(open_windows_cleanup if t.kind == WINDOWS_KIND else open_recycle_bin)
                self.tree.setItemWidget(top, 3, btn)
            desc.setFirstColumnSpanned(True)
            top.setExpanded(t.kind != TRASH_KIND or bool(t.note))
        self._updating = False
        self._update_total()
        self.targets_ready.emit(targets)

    # --- sélection -----------------------------------------------------------------------

    def _on_changed(self, item: QTreeWidgetItem, col: int) -> None:
        if not self._updating and col == 0:
            self._update_total()

    def _selection(self) -> tuple[list[str], dict[str, int]]:
        paths: list[str] = []
        sizes: dict[str, int] = {}
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            t: Target = top.data(0, TARGET_ROLE)
            if t.kind != TRASH_KIND or not t.paths:
                continue
            if top.checkState(0) == Qt.CheckState.Checked:   # toute la catégorie, y compris au-delà de la liste
                chosen = t.paths
            else:
                chosen = [top.child(j).data(0, PATH_ROLE) for j in range(top.childCount())
                          if top.child(j).data(0, PATH_ROLE) and top.child(j).checkState(0) == Qt.CheckState.Checked]
            paths += chosen
            sizes.update({p: t.sizes.get(p, 0) for p in chosen})
        return paths, sizes

    def _update_total(self) -> None:
        paths, sizes = self._selection()
        total = sum(sizes.values())
        self.status.setText(f"Sélection : {human_size(total)} ({human_count(len(paths))} éléments)"
                            if paths else "Rien de sélectionné.")
        self.clean_btn.setEnabled(bool(paths))

    def _on_double(self, item: QTreeWidgetItem, col: int) -> None:
        path = item.data(0, PATH_ROLE)
        if path:
            show_in_explorer(path)

    def _request(self) -> None:
        paths, sizes = self._selection()
        if paths:
            self.cleanup_requested.emit(paths, sizes)

    def shutdown(self) -> None:
        if self._worker:
            self._worker.requestInterruption()
            self._worker.wait(5000)
