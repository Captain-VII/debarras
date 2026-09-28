"""Graphiques QtCharts : anneau par type de fichier, barres par sous-dossier (navigables)."""
from __future__ import annotations

import os

from PySide6.QtCharts import (
    QBarCategoryAxis, QBarSet, QChart, QChartView, QHorizontalBarSeries, QPieSeries,
    QPieSlice, QValueAxis,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QSplitter, QToolTip, QVBoxLayout, QWidget,
)

from core.cache import Cache
from core.stats import StatsResult
from utils.filetypes import CATEGORIES, category_colors
from utils.format import human_count, human_size

TOP_FOLDERS = 15
_FILES_LABEL = "(fichiers directs)"


def _is_dark(w: QWidget) -> bool:
    return w.palette().window().color().lightness() < 128


def _style_chart(chart: QChart, dark: bool) -> None:
    chart.setTheme(QChart.ChartTheme.ChartThemeDark if dark else QChart.ChartTheme.ChartThemeLight)
    chart.setBackgroundBrush(QColor("#1a1a19" if dark else "#fcfcfb"))
    chart.setTitleBrush(QColor("#ffffff" if dark else "#0b0b0b"))
    chart.legend().setLabelColor(QColor("#c3c2b7" if dark else "#52514e"))
    chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)


def _set_chart(view: QChartView, chart: QChart) -> None:
    """Remplace le graphique d'une vue en libérant l'ancien."""
    old = view.chart()
    view.setChart(chart)
    if old is not None:
        old.deleteLater()


def _unit(max_bytes: int) -> tuple[str, float]:
    for name, factor in (("To", 1024 ** 4), ("Go", 1024 ** 3), ("Mo", 1024 ** 2), ("Ko", 1024)):
        if max_bytes >= factor:
            return name, float(factor)
    return "o", 1.0


class ChartsView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cache: Cache | None = None
        self._scan_id = 0
        self._root = ""
        self._bar_dir = ""
        self._bar_paths: list[str | None] = []
        self._bar_sizes: list[int] = []

        self.pie_view = QChartView()
        self.pie_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.bar_view = QChartView()
        self.bar_view.setRenderHint(QPainter.RenderHint.Antialiasing)

        self.up_btn = QPushButton("⬆ Remonter")
        self.up_btn.clicked.connect(self._go_up)
        self.bar_label = QLabel()
        nav = QHBoxLayout()
        nav.addWidget(self.up_btn)
        nav.addWidget(self.bar_label, 1)
        nav.addWidget(QLabel("Clic sur une barre : détailler le dossier"))
        bars = QWidget()
        bl = QVBoxLayout(bars)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addLayout(nav)
        bl.addWidget(self.bar_view, 1)

        # Légende maison : les libellés de QChart sont tronqués faute de place.
        self.pie_legend = QGridLayout()
        self.pie_legend.setHorizontalSpacing(12)
        pie = QWidget()
        pl = QVBoxLayout(pie)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.pie_view, 1)
        pl.addLayout(self.pie_legend)

        split = QSplitter()
        split.addWidget(pie)
        split.addWidget(bars)
        split.setSizes([450, 650])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(split)
        self.clear()

    # --- API ------------------------------------------------------------------------

    def clear(self) -> None:
        self._cache = None
        for view, title in ((self.pie_view, "Répartition par type"),
                            (self.bar_view, "Plus gros sous-dossiers")):
            chart = QChart()
            _style_chart(chart, _is_dark(self))
            chart.setTitle(title)
            _set_chart(view, chart)
        self.bar_label.clear()
        self.up_btn.setEnabled(False)
        self._clear_legend()

    def _clear_legend(self) -> None:
        while self.pie_legend.count():
            w = self.pie_legend.takeAt(0).widget()
            if w:
                w.deleteLater()

    def set_data(self, cache: Cache, stats: StatsResult) -> None:
        self._cache, self._scan_id, self._root = cache, stats.scan_id, stats.root
        self._build_pie(stats)
        self.show_folder(stats.root)

    # --- anneau par catégorie ---------------------------------------------------------

    def _build_pie(self, stats: StatsResult) -> None:
        dark = _is_dark(self)
        colors = category_colors(dark)
        surface = QColor("#1a1a19" if dark else "#fcfcfb")
        total = sum(s for s, _ in stats.by_category.values()) or 1
        series = QPieSeries()
        series.setHoleSize(0.45)
        for cat in CATEGORIES:  # ordre fixe : la couleur suit la catégorie
            size, count = stats.by_category.get(cat, (0, 0))
            if size <= 0:
                continue
            pct = 100 * size / total
            sl = series.append(cat, size)
            sl.setColor(QColor(colors[cat]))
            sl.setBorderColor(surface)
            sl.setBorderWidth(2)
            sl.setExplodeDistanceFactor(0.06)
            tip = f"{cat}\n{human_size(size)} — {pct:.1f} %\n{human_count(count)} fichiers".replace(".", ",")
            sl.hovered.connect(lambda on, sl=sl, tip=tip: self._pie_hover(sl, on, tip))

        chart = QChart()
        _style_chart(chart, dark)
        chart.setTitle(f"Répartition par type — {human_size(stats.total_size)}")
        chart.addSeries(series)
        chart.legend().hide()
        _set_chart(self.pie_view, chart)

        self._clear_legend()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        row = 0
        for cat in CATEGORIES:
            size, count = stats.by_category.get(cat, (0, 0))
            if size <= 0:
                continue
            swatch = QLabel()
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background:{colors[cat]}; border-radius:2px;")
            pct = QLabel(f"{100 * size / total:.1f} %".replace(".", ","))
            size_lbl = QLabel(human_size(size))
            n_lbl = QLabel(f"{human_count(count)} fichiers")
            for lbl in (size_lbl, pct, n_lbl):
                lbl.setAlignment(right)
            for col, wdg in enumerate((swatch, QLabel(cat), size_lbl, pct, n_lbl)):
                self.pie_legend.addWidget(wdg, row, col)
            row += 1
        self.pie_legend.setColumnStretch(1, 1)

    def _pie_hover(self, sl: QPieSlice, on: bool, tip: str) -> None:
        sl.setExploded(on)
        if on:
            QToolTip.showText(QCursor.pos(), tip, self.pie_view)
        else:
            QToolTip.hideText()

    # --- barres par sous-dossier ------------------------------------------------------

    def show_folder(self, path: str) -> None:
        if not self._cache:
            return
        self._bar_dir = path
        subdirs = sorted(self._cache.child_dirs(self._scan_id, path), key=lambda d: d[1], reverse=True)
        info = self._cache.dir_info(self._scan_id, path)
        own_files = (info[0] if info else 0) - sum(s for _, s, _ in subdirs)

        entries: list[tuple[str, str | None, int]] = [
            (os.path.basename(p), p, s) for p, s, _ in subdirs if s > 0
        ]
        if own_files > 0:
            entries.append((_FILES_LABEL, None, own_files))
        entries.sort(key=lambda e: e[2], reverse=True)
        if len(entries) > TOP_FOLDERS:  # le reste regroupé
            rest = entries[TOP_FOLDERS - 1:]
            entries = entries[:TOP_FOLDERS - 1] + [(f"{len(rest)} autres", None, sum(e[2] for e in rest))]
        entries.reverse()  # la plus grosse barre en haut

        dark = _is_dark(self)
        unit, factor = _unit(max((e[2] for e in entries), default=0))
        bar_set = QBarSet("Taille")
        bar_set.setColor(QColor("#3987e5" if dark else "#2a78d6"))
        bar_set.setBorderColor(QColor(0, 0, 0, 0))
        bar_set.setLabelColor(QColor("#ffffff" if dark else "#0b0b0b"))
        for _, _, size in entries:
            bar_set.append(size / factor)
        self._bar_paths = [e[1] for e in entries]
        self._bar_sizes = [e[2] for e in entries]
        bar_set.hovered.connect(self._bar_hover)
        bar_set.clicked.connect(self._bar_click)

        series = QHorizontalBarSeries()
        series.append(bar_set)
        series.setBarWidth(0.7)

        chart = QChart()
        _style_chart(chart, dark)
        chart.setTitle(f"Plus gros sous-dossiers — {os.path.basename(path) or path}")
        chart.addSeries(series)
        chart.legend().hide()
        # Taille lisible (format français) directement dans le libellé.
        names = [f"{e[0] if len(e[0]) <= 28 else e[0][:26] + '…'}  ·  {human_size(e[2])}" for e in entries]
        axis_y = QBarCategoryAxis()
        axis_y.append(names)
        axis_x = QValueAxis()
        axis_x.setTitleText(unit)
        axis_x.setLabelFormat("%.0f")
        axis_x.setMin(0)
        axis_x.setMax(max((s / factor for s in self._bar_sizes), default=1))
        axis_x.applyNiceNumbers()
        grid = QColor("#383835" if dark else "#e8e7e3")
        axis_x.setGridLinePen(QPen(grid, 1))
        axis_y.setGridLineVisible(False)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_y)
        series.attachAxis(axis_x)
        _set_chart(self.bar_view, chart)

        self.bar_label.setText(path)
        self.up_btn.setEnabled(os.path.normcase(path) != os.path.normcase(self._root))

    def _bar_hover(self, on: bool, index: int) -> None:
        if on and 0 <= index < len(self._bar_sizes):
            target = self._bar_paths[index] or self._bar_dir
            QToolTip.showText(QCursor.pos(), f"{target}\n{human_size(self._bar_sizes[index])}",
                              self.bar_view)
            self.bar_view.setCursor(Qt.CursorShape.PointingHandCursor if self._bar_paths[index]
                                    else Qt.CursorShape.ArrowCursor)
        else:
            QToolTip.hideText()
            self.bar_view.unsetCursor()

    def _bar_click(self, index: int) -> None:
        if 0 <= index < len(self._bar_paths) and self._bar_paths[index]:
            path = self._bar_paths[index]
            QTimer.singleShot(0, lambda: self.show_folder(path))  # hors du signal de la barre

    def _go_up(self) -> None:
        if os.path.normcase(self._bar_dir) != os.path.normcase(self._root):
            self.show_folder(os.path.dirname(self._bar_dir))
