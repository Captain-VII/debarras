"""Onglet Images similaires : mêmes groupes à cocher que les doublons, avec vignettes,
résolution, degré de ressemblance et aperçu en grand de l'image sélectionnée."""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QSplitter, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.similar import DEFAULT_THRESHOLD, THRESHOLDS, SimilarFinder
from ui.dup_view import FILE_ROLE, DupView
from utils.export import iso
from utils.format import human_count, human_date, human_duration, human_size

PREVIEW = 420


def resemblance(bits: int) -> str:
    """Écart de Hamming (sur 64 bits) exprimé en ressemblance."""
    return "identique" if bits == 0 else f"{round(100 - bits * 100 / 64)} %"


class SimilarView(DupView):
    RULES = [("Garder la meilleure résolution", "resolution"), *DupView.RULES]
    SEARCH_LABEL = "Rechercher les images similaires"
    MIN_SIZE_KO = 20
    HEADERS = ["Image", "Résolution", "Taille", "Modifié", "Ressemblance"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tree.setIconSize(QSize(72, 72))
        self.tree.setUniformRowHeights(False)
        self.tree.currentItemChanged.connect(self._show_preview)

    # --- points d'extension de DupView -----------------------------------------------------

    def _extra_search_options(self, layout: QHBoxLayout) -> None:
        self.level = QComboBox()
        for label, bits in THRESHOLDS.items():
            self.level.addItem(label, bits)
        self.level.setCurrentIndex(self.level.findData(DEFAULT_THRESHOLD))
        self.level.setToolTip("Identiques à l'œil : même image recompressée ou redimensionnée.\n"
                              "Très proches : + petites retouches.\n"
                              "Proches : + variantes d'une même prise (rafales) — à vérifier.")
        layout.addWidget(QLabel("Ressemblance :"))
        layout.addWidget(self.level)

    def _central_widget(self) -> QWidget:
        self.preview = QLabel("Sélectionnez une image pour l'afficher ici.")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumWidth(260)
        self.preview.setWordWrap(True)
        self.preview_info = QLabel()
        self.preview_info.setWordWrap(True)
        self.preview_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 0, 0, 0)
        sl.addWidget(self.preview, 1)
        sl.addWidget(self.preview_info)
        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(side)
        split.setSizes([700, PREVIEW])
        return split

    def _make_finder(self):
        return SimilarFinder(self._db_path, self._root, self.min_size.value() * 1024,
                             self.level.currentData())

    def _group_label(self, g) -> list[str]:
        return [f"{len(g.files)} images — {human_size(g.wasted)} récupérables",
                "", human_size(sum(f.size for f in g.files)), "", ""]

    def _row(self, f) -> list[str]:
        res = f"{f.width} × {f.height}" if f.width else "?"
        return [f.path, res, human_size(f.size), human_date(f.mtime), resemblance(f.distance)]

    def _decorate(self, child: QTreeWidgetItem, f) -> None:
        if f.thumb:
            child.setIcon(0, QIcon(QPixmap.fromImage(QImage.fromData(f.thumb))))

    def _ordered(self, g) -> list:
        return list(g.files)  # déjà triées : meilleure résolution d'abord

    def _result_text(self, r) -> str:
        return (f"{human_count(len(r.groups))} groupes, "
                f"{human_count(sum(len(g.files) for g in r.groups))} images — "
                f"{human_size(r.wasted)} récupérables en ne gardant que la meilleure version. "
                f"{human_count(r.candidates)} images examinées, {human_count(r.hashed)} empreintes "
                f"calculées en {human_duration(r.duration)}.")

    # --- aperçu -------------------------------------------------------------------------

    def _show_preview(self, item: QTreeWidgetItem | None, _prev=None) -> None:
        f = item.data(0, FILE_ROLE) if item else None
        if f is None:
            return
        reader = QImageReader(f.path)
        reader.setAutoTransform(True)  # orientation EXIF
        size = reader.size()
        if size.isValid():
            size.scale(PREVIEW, PREVIEW, Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(size)  # décodage directement à la taille d'affichage
        img = reader.read()
        if img.isNull():
            self.preview.setText("Aperçu indisponible.")
        else:
            self.preview.setPixmap(QPixmap.fromImage(img))
        self.preview_info.setText(
            f"<b>{os.path.basename(f.path)}</b><br>{f.width} × {f.height} — {human_size(f.size)}"
            f" — {human_date(f.mtime)}<br>Ressemblance avec la meilleure version : {resemblance(f.distance)}")

    def export_table(self) -> tuple[list[str], list[list]] | None:
        if self._result is None:
            return None
        checked = {f.path for f in self.checked_files()}
        rows = [[n, f.path, f.width, f.height, f.size, iso(f.mtime), f.distance,
                 "oui" if f.path in checked else ""]
                for n, g in enumerate(self._result.groups, 1) for f in g.files]
        return ["Groupe", "Chemin", "Largeur", "Hauteur", "Octets", "Modifié", "Écart (bits/64)", "Coché"], rows
