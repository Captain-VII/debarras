"""Treemap squarified dessinée au QPainter, cliquable et navigable.

Trois coloriages : sécurité (peut-on supprimer ?), type de fichier, évolution depuis le scan précédent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFontMetrics, QGuiApplication, QKeyEvent, QLinearGradient, QMouseEvent,
    QPainter, QPaintEvent, QPen, QPixmap, QResizeEvent,
)
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QToolTip, QVBoxLayout, QWidget,
)

from core import safety
from core.cache import Cache
from ui.actions_ui import add_action_entries
from ui.tree_view import show_in_explorer
from utils.filetypes import CATEGORIES, OTHER, category, category_colors
from utils.format import human_count, human_size

MAX_DEPTH = 8          # niveaux de dossiers imbriqués
MIN_AREA = 12.0        # px² : en dessous, regroupé dans "autres éléments"
MIN_SIDE = 6.0         # px : taille mini d'un dossier pour détailler son contenu
MAX_EXPANSIONS = 2000  # dossiers détaillés au maximum (borne les requêtes SQLite)
HEADER = 15.0          # hauteur du bandeau de nom d'un dossier
PAD = 2.0

SAFETY, TYPES, GROWTH = "safety", "types", "growth"
MODES = {SAFETY: "Sécurité (peut-on supprimer ?)", TYPES: "Type de fichier", GROWTH: "Évolution depuis le scan précédent"}
_GROWTH_COLORS = {  # clair / sombre : a grossi, a diminué, nouveau ou modifié, inchangé
    False: {"up": "#d03b3b", "down": "#2e9d5b", "new": "#e08a00", "same": "#b9b8b3"},
    True: {"up": "#e5534b", "down": "#2fa866", "new": "#d98a1c", "same": "#4a4946"},
}


# --- algorithme --------------------------------------------------------------------

def _worst(total: float, largest: float, smallest: float, side: float) -> float:
    """Pire rapport d'aspect d'une rangée (Bruls, Huizing, van Wijk)."""
    s2, w2 = total * total, side * side
    return max(w2 * largest / s2, s2 / (w2 * smallest))


def squarify(areas: list[float], rect: QRectF) -> list[QRectF]:
    """Découpe `rect` en rectangles d'aires `areas` (triées décroissantes, somme = aire)."""
    out: list[QRectF] = []
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    i, n = 0, len(areas)
    while i < n:
        side = min(w, h)
        if side <= 0:
            out.extend(QRectF(x, y, 0, 0) for _ in range(n - i))
            break
        row_total, largest = areas[i], areas[i]
        j = i + 1
        best = _worst(row_total, largest, areas[i], side)
        while j < n:
            cand = _worst(row_total + areas[j], largest, areas[j], side)
            if cand > best:
                break
            row_total += areas[j]
            best = cand
            j += 1
        thick = row_total / side
        pos = y if w >= h else x
        for a in areas[i:j]:
            length = a / thick
            if w >= h:  # colonne à gauche
                out.append(QRectF(x, pos, thick, length))
            else:       # rangée en haut
                out.append(QRectF(pos, y, length, thick))
            pos += length
        if w >= h:
            x, w = x + thick, w - thick
        else:
            y, h = y + thick, h - thick
        i = j
    return out


# --- données -----------------------------------------------------------------------

@dataclass(slots=True)
class Entry:
    path: str
    name: str
    size: int
    is_dir: bool
    count: int = 0
    mtime: float = 0.0
    special: str = ""     # FREE / UNSCANNED : blocs ajoutés pour un disque entier


FREE, UNSCANNED = "::free", "::unscanned"
SPECIAL_TEXT = {
    FREE: "Espace libre sur le disque.",
    UNSCANNED: "Non analysé : dossier Windows, fichiers système (fichier d'échange, points de "
               "restauration, index du disque) et dossiers inaccessibles sans droits administrateur.",
}


@dataclass(slots=True)
class Item:
    rect: QRectF
    entry: Entry | None   # None = regroupement de petits éléments
    label: str
    depth: int
    detailed: bool = False  # dossier dont le contenu est dessiné


class TreemapCanvas(QWidget):
    item_selected = Signal(str, "qlonglong")  # chemin ("" si regroupement), taille
    directory_changed = Signal(str)
    action_requested = Signal(str, list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 150)
        self._cache: Cache | None = None
        self._scan_id = 0
        self._root = ""
        self.current = ""
        self._children: dict[str, list[Entry]] = {}
        self._items: list[Item] = []
        self._pixmap: QPixmap | None = None
        self._hover: Item | None = None
        self._selected: str | None = None
        self._expansions = 0
        self.mode = SAFETY
        self.growth: dict[str, int] = {}     # chemin normalisé -> variation (octets) depuis le scan précédent
        self.since = 0.0                     # date du scan précédent (fichiers plus récents = nouveaux)
        self.extras: list[Entry] = []        # blocs « espace libre / non analysé » à la racine

    # --- API ------------------------------------------------------------------------

    def load(self, cache: Cache, scan_id: int, root: str) -> None:
        self._cache, self._scan_id, self._root = cache, scan_id, root
        self._children.clear()
        self.extras = []
        self._selected = None
        self.navigate(root)

    def clear(self) -> None:
        self._cache = None
        self._items, self._pixmap, self._hover = [], None, None
        self.current = ""
        self.growth, self.since = {}, 0.0
        self.extras = []
        self.update()

    def set_extras(self, free: int, unscanned: int) -> None:
        """Disque entier : la carte montre aussi l'espace libre et ce qui n'a pas été analysé,
        pour que le total corresponde à la taille du disque."""
        self.extras = [Entry(path, label, size, False, special=path) for path, label, size in (
            (UNSCANNED, "Non analysé (Windows, système)", unscanned), (FREE, "Espace libre", free)) if size > 0]
        self._children.pop(self._root, None)
        self._invalidate()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._invalidate()

    def set_growth(self, deltas: dict[str, int], since: float) -> None:
        self.growth = {os.path.normcase(k): v for k, v in deltas.items()}
        self.since = since
        if self.mode == GROWTH:
            self._invalidate()

    def verdict(self, e: Entry) -> safety.Verdict:
        return safety.current().classify(e.path, e.is_dir)

    def refresh(self) -> None:
        """Redessine (après une mise à jour du classement de sécurité)."""
        self._invalidate()

    def navigate(self, path: str, select: str | None = None) -> None:
        """Zoome sur le dossier `path` (borné à la racine du scan)."""
        if not self._cache or not self._inside_root(path):
            return
        self.current = path
        self._selected = select
        self._invalidate()
        self.directory_changed.emit(path)

    def go_up(self) -> None:
        if self.current and os.path.normcase(self.current) != os.path.normcase(self._root):
            self.navigate(os.path.dirname(self.current), select=self.current)

    def can_go_up(self) -> bool:
        return bool(self.current) and os.path.normcase(self.current) != os.path.normcase(self._root)

    def _inside_root(self, path: str) -> bool:
        p, r = os.path.normcase(path), os.path.normcase(self._root)
        return p == r or p.startswith(r.rstrip(os.sep) + os.sep)

    def _entries(self, path: str) -> list[Entry]:
        """Enfants d'un dossier (taille > 0, décroissants), mis en cache mémoire."""
        if path not in self._children and self._cache:
            kids = [Entry(p, os.path.basename(p), s, True, c)
                    for p, s, c in self._cache.child_dirs(self._scan_id, path) if s > 0]
            kids += [Entry(p, name, s, False, 0, m)
                     for p, name, s, m in self._cache.child_files(path) if s > 0]
            if os.path.normcase(path) == os.path.normcase(self._root):
                kids += self.extras
            kids.sort(key=lambda e: e.size, reverse=True)
            self._children[path] = kids
        return self._children.get(path, [])

    # --- mise en page ---------------------------------------------------------------

    def _invalidate(self) -> None:
        self._pixmap = None
        self._hover = None
        self.update()

    def _layout(self) -> None:
        self._items = []
        self._expansions = 0
        if self.current:
            self._layout_dir(self.current, QRectF(self.rect()).adjusted(1, 1, -1, -1), 0)

    def _layout_dir(self, path: str, rect: QRectF, depth: int) -> None:
        self._expansions += 1
        kids = self._entries(path)
        total = sum(e.size for e in kids)
        if total <= 0 or rect.width() < 1 or rect.height() < 1:
            return
        scale = rect.width() * rect.height() / total
        keep = [e for e in kids if e.size * scale >= MIN_AREA]
        rest = kids[len(keep):]
        areas = [e.size * scale for e in keep]
        if rest:
            areas.append(sum(e.size for e in rest) * scale)

        for idx, r in enumerate(squarify(areas, rect)):
            if idx >= len(keep):
                n = len(rest)
                label = f"{human_count(n)} autre{'s' if n > 1 else ''} élément{'s' if n > 1 else ''}"
                self._items.append(Item(r, None, label, depth))
                continue
            e = keep[idx]
            item = Item(r, e, e.name, depth)
            self._items.append(item)
            if not e.is_dir:
                continue
            head = HEADER if r.height() >= HEADER * 2.5 and r.width() >= 40 else 0.0
            inner = r.adjusted(PAD, PAD + head, -PAD, -PAD)
            if (depth < MAX_DEPTH and self._expansions < MAX_EXPANSIONS
                    and inner.width() >= MIN_SIDE and inner.height() >= MIN_SIDE):
                item.detailed = True
                self._layout_dir(e.path, inner, depth + 1)

    # --- rendu ----------------------------------------------------------------------

    def _is_dark(self) -> bool:
        return self.palette().window().color().lightness() < 128

    def _fill_color(self, e: Entry, dark: bool, colors: dict[str, QColor],
                    levels: dict[str, QColor], growth: dict[str, QColor]) -> QColor:
        """Couleur d'un fichier ou d'un dossier non détaillé, selon le mode."""
        if e.special:
            return QColor("#2b2b29" if dark else "#f1f0ec") if e.special == FREE else QColor("#5c5b57")
        if self.mode == SAFETY:
            return levels[self.verdict(e).level]
        if self.mode == GROWTH:
            if e.is_dir:
                d = self.growth.get(os.path.normcase(e.path), 0)
                if d == 0:
                    return growth["same"]
                base = growth["up" if d > 0 else "down"]
                # Intensité selon la part de variation (atténué pour les petites variations).
                ratio = min(abs(d) / max(e.size, 1), 1.0)
                mix = 0.35 + 0.65 * ratio
                same = growth["same"]
                return QColor(*(int(same.getRgb()[i] * (1 - mix) + base.getRgb()[i] * mix) for i in range(3)))
            return growth["new"] if self.since and e.mtime > self.since else growth["same"]
        if e.is_dir:
            return colors[OTHER].darker(130)
        return colors[category(os.path.splitext(e.name)[1])]

    @staticmethod
    def _tint(base: QColor, color: QColor, amount: float) -> QColor:
        return QColor(*(int(base.getRgb()[i] * (1 - amount) + color.getRgb()[i] * amount) for i in range(3)))

    def _render(self) -> QPixmap:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(self.size() * dpr)
        pm.setDevicePixelRatio(dpr)
        dark = self._is_dark()
        surface = QColor("#1a1a19" if dark else "#fcfcfb")
        pm.fill(surface)
        colors = {k: QColor(v) for k, v in category_colors(dark).items()}
        levels = {k: QColor(v) for k, v in safety.level_colors(dark).items()}
        growth = {k: QColor(v) for k, v in _GROWTH_COLORS[dark].items()}
        dir_fill = [QColor("#2a2a28"), QColor("#343432")] if dark else [QColor("#ecebe7"), QColor("#e2e1dc")]
        text = QColor("#ffffff" if dark else "#0b0b0b")
        muted = QColor("#c3c2b7" if dark else "#52514e")

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        font = p.font()
        font.setPointSizeF(8.5)
        p.setFont(font)
        fm = QFontMetrics(font)
        gap = QPen(surface, 1)

        for it in self._items:
            r = it.rect
            if r.width() < 1 or r.height() < 1:
                continue
            e = it.entry
            if e is None:  # regroupement : gris hachuré
                p.fillRect(r, dir_fill[1])
                p.fillRect(r, QColor(muted.red(), muted.green(), muted.blue(), 60))
                p.setBrush(Qt.BrushStyle.NoBrush)
            elif e.is_dir and it.detailed:
                bg = dir_fill[it.depth % 2]
                if self.mode == SAFETY:  # fond du dossier teinté par son niveau
                    bg = self._tint(bg, levels[self.verdict(e).level], 0.22)
                elif self.mode == GROWTH and self.growth.get(os.path.normcase(e.path), 0):
                    bg = self._tint(bg, growth["up" if self.growth[os.path.normcase(e.path)] > 0 else "down"], 0.15)
                p.fillRect(r, bg)
            elif e.special:  # espace libre : uni ; non analysé : hachuré
                p.fillRect(r, self._fill_color(e, dark, colors, levels, growth))
                if e.special == UNSCANNED:
                    p.fillRect(r, QBrush(QColor(255, 255, 255, 40), Qt.BrushStyle.BDiagPattern))
            else:
                base = self._fill_color(e, dark, colors, levels, growth)
                g = QLinearGradient(r.topLeft(), r.bottomRight())
                g.setColorAt(0, base.lighter(118))
                g.setColorAt(1, base.darker(118))
                p.fillRect(r, g)
            p.setPen(gap)
            p.drawRect(r.adjusted(0, 0, -1, -1))

            # Libellés : bandeau de dossier, ou nom au centre si ça rentre.
            if e is not None and e.is_dir and it.detailed and r.height() >= HEADER * 2.5 and r.width() >= 40:
                p.setPen(muted)
                band = QRectF(r.x() + 4, r.y() + 1, r.width() - 8, HEADER)
                label = f"{e.name}  {human_size(e.size)}"
                p.drawText(band, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           fm.elidedText(label, Qt.TextElideMode.ElideMiddle, int(band.width())))
            elif r.width() >= 50 and r.height() >= 18:
                p.setPen(text)
                inner = r.adjusted(3, 2, -3, -2)
                lines = [it.label]
                if e is not None and r.height() >= 34:
                    lines.append(human_size(e.size))
                txt = "\n".join(fm.elidedText(s, Qt.TextElideMode.ElideMiddle, int(inner.width()))
                                for s in lines)
                p.drawText(inner, Qt.AlignmentFlag.AlignCenter, txt)
        p.end()
        return pm

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        if not self.current:
            p.setPen(self.palette().placeholderText().color())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Aucun scan chargé.")
            return
        if self._pixmap is None:
            self._layout()
            self._pixmap = self._render()
        p.drawPixmap(0, 0, self._pixmap)

        sel = next((i for i in self._items if i.entry and i.entry.path == self._selected), None)
        if sel:
            p.setPen(QPen(self.palette().highlight().color(), 3))
            p.drawRect(sel.rect.adjusted(1, 1, -2, -2))
        if self._hover and self._hover is not sel:
            p.setPen(QPen(QColor(255, 255, 255, 200) if self._is_dark() else QColor(0, 0, 0, 180), 2))
            p.drawRect(self._hover.rect.adjusted(1, 1, -2, -2))

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._invalidate()
        super().resizeEvent(event)

    # --- interaction ----------------------------------------------------------------

    def item_at(self, pos: QPointF) -> Item | None:
        """Élément le plus profond sous le curseur."""
        for it in reversed(self._items):
            if it.rect.contains(pos):
                return it
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        it = self.item_at(event.position())
        if it is not self._hover:
            self._hover = it
            self.update()
            if it is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(event.globalPosition().toPoint(), self._tooltip(it), self)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()

    def _tooltip(self, it: Item) -> str:
        e = it.entry
        if e is None:
            return it.label
        if e.special:
            return f"{e.name} — {human_size(e.size)}\n{SPECIAL_TEXT[e.special]}"
        if e.is_dir:
            text = f"{e.path}\n{human_size(e.size)} — {human_count(e.count)} fichiers"
        else:
            text = f"{e.path}\n{human_size(e.size)} — {category(os.path.splitext(e.name)[1])}"
        v = self.verdict(e)
        text += f"\n\n{v.label} : {v.reason}"
        if self.mode == GROWTH and e.is_dir and (d := self.growth.get(os.path.normcase(e.path))):
            text += f"\nDepuis le scan précédent : {'+' if d > 0 else '−'}{human_size(abs(d))}"
        elif self.mode == GROWTH and not e.is_dir and self.since and e.mtime > self.since:
            text += "\nNouveau ou modifié depuis le scan précédent"
        return text

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.BackButton:
            self.go_up()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        it = self.item_at(event.position())
        if it and it.entry:
            self._selected = it.entry.path
            self.item_selected.emit(it.entry.path, it.entry.size)
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        it = self.item_at(event.position())
        if not it or not it.entry:
            return
        target = it.entry.path if it.entry.is_dir else os.path.dirname(it.entry.path)
        # Zoom d'un seul niveau sous le dossier courant.
        while os.path.normcase(os.path.dirname(target)) != os.path.normcase(self.current) \
                and self._inside_root(os.path.dirname(target)) and target != os.path.dirname(target):
            target = os.path.dirname(target)
        if os.path.normcase(target) != os.path.normcase(self.current):
            self.navigate(target, select=it.entry.path)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Escape):
            self.go_up()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        it = self.item_at(QPointF(event.pos()))
        menu = QMenu(self)
        if it and it.entry and not it.entry.special:
            e = it.entry
            if e.is_dir:
                menu.addAction("Zoomer ici", lambda: self.navigate(e.path))
            menu.addAction("Afficher dans l'Explorateur", lambda: show_in_explorer(e.path))
            menu.addAction("Copier le chemin", lambda: QGuiApplication.clipboard().setText(e.path))
            add_action_entries(menu, [e.path], self.action_requested.emit, self.verdict(e))
            menu.addSeparator()
        up = menu.addAction("Remonter", self.go_up)
        up.setEnabled(self.can_go_up())
        menu.exec(event.globalPos())


class TreemapView(QWidget):
    """Canvas + barre de navigation + légende."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.canvas = TreemapCanvas()
        self.up_btn = QPushButton("⬆ Remonter")
        self.up_btn.clicked.connect(self.canvas.go_up)
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.canvas.directory_changed.connect(self._on_dir)
        self.action_requested = self.canvas.action_requested

        self.mode_box = QComboBox()
        for key, label in MODES.items():
            self.mode_box.addItem(label, key)
        self.mode_box.currentIndexChanged.connect(self._on_mode)
        hint = QLabel("Double-clic : zoomer — Retour arrière : remonter")
        hint.setStyleSheet("color: palette(placeholder-text);")

        nav = QHBoxLayout()
        nav.addWidget(self.up_btn)
        nav.addWidget(self.path_label, 1)
        nav.addWidget(hint)
        nav.addWidget(QLabel("Couleurs :"))
        nav.addWidget(self.mode_box)

        self.legend = QHBoxLayout()
        self._build_legend()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(nav)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(self.legend)
        self._on_dir("")

    def refresh_theme(self) -> None:
        """Couleurs recalculées après un changement de thème."""
        self._build_legend()
        self.canvas._invalidate()

    def _build_legend(self) -> None:
        while self.legend.count():
            w = self.legend.takeAt(0).widget()
            if w:
                w.hide()  # sinon visible jusqu'à sa destruction différée
                w.deleteLater()
        dark = self.canvas._is_dark()
        mode = self.canvas.mode
        if mode == SAFETY:
            colors = safety.level_colors(dark)
            entries = [(colors[lv], safety.LABELS[lv], safety.HINTS[lv]) for lv in safety.LEVELS]
        elif mode == GROWTH:
            g = _GROWTH_COLORS[dark]
            entries = [(g["up"], "A grossi", ""), (g["down"], "A diminué", ""),
                       (g["new"], "Fichier nouveau ou modifié", ""), (g["same"], "Inchangé", "")]
        else:
            colors = category_colors(dark)
            entries = [(colors[cat], cat, "") for cat in CATEGORIES]
        for color, text, tip in entries:
            swatch = QLabel()
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background:{color}; border-radius:2px;")
            label = QLabel(text)
            if tip:
                label.setToolTip(tip)
                swatch.setToolTip(tip)
            self.legend.addWidget(swatch)
            self.legend.addWidget(label)
            self.legend.addSpacing(10)
        self.legend.addStretch(1)

    def _on_mode(self) -> None:
        self.canvas.set_mode(self.mode_box.currentData())
        self._build_legend()

    def set_mode(self, mode: str) -> None:
        self.mode_box.setCurrentIndex(max(0, self.mode_box.findData(mode)))

    def load(self, cache: Cache, scan_id: int, root: str) -> None:
        self.canvas.load(cache, scan_id, root)

    def clear(self) -> None:
        self.canvas.clear()
        self._on_dir("")

    def show_path(self, path: str) -> None:
        """Zoome sur un dossier, ou sur le parent d'un fichier en le sélectionnant."""
        if os.path.isdir(path):
            self.canvas.navigate(path)
        else:
            self.canvas.navigate(os.path.dirname(path), select=path)

    def _on_dir(self, path: str) -> None:
        self.path_label.setText(path)
        self.up_btn.setEnabled(self.canvas.can_go_up())
