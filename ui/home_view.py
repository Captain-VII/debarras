"""Page d'accueil : synthèse « que puis-je supprimer sans risque ? », treemap et panneau de détail."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

from core import safety
from core.actions import TRASH
from core.history import HistoryDiff, display_name
from core.safety import SafetySummary
from ui.tree_view import show_in_explorer
from ui.treemap import GROWTH, TreemapView
from utils.format import human_date, human_size

PATH_ROLE = Qt.ItemDataRole.UserRole + 1


def _signed(n: int) -> str:
    return ("+" if n > 0 else "−" if n < 0 else "") + human_size(abs(n))


_LIBRARY_DIRS = {"common", "epic games", "gog games", "xboxgames", "games", "jeux"}


def _anchor(parts: list[str]) -> int:
    """Index du premier dossier après une bibliothèque de jeux (common, Epic Games…), sinon 0."""
    lowered = [p.lower() for p in parts]
    return max((i + 1 for i, p in enumerate(lowered[:-1]) if p in _LIBRARY_DIRS), default=0)


def short_name(path: str, root: str) -> str:
    """Libellé court : « Path of Exile 2 › Content » plutôt que le chemin complet."""
    parts = [p for p in display_name(path, root).split(os.sep) if p]
    if len(parts) <= 1:
        return parts[0] if parts else path
    anchor = _anchor(parts)
    if not anchor:  # hors bibliothèque de jeux : dossier parent › dossier
        return f"{parts[-2]} › {parts[-1]}"
    head = parts[anchor]
    return head if anchor == len(parts) - 1 else f"{head} › {parts[-1]}"


def headline_growth(diff: HistoryDiff, n: int = 3) -> list:
    """Plus fortes hausses, regroupées par jeu ou logiciel d'une bibliothèque, sans doublon
    (un dossier qui en contient un autre de la liste est écarté)."""
    root = diff.new.root
    picks: dict[str, object] = {}
    for d in diff.top(True, 15):
        parts = [p for p in display_name(d.path, root).split(os.sep) if p]
        anchor = _anchor(parts)
        key = d.path
        if anchor and len(parts) > anchor + 1:  # sous-dossier d'un jeu : on remonte au jeu
            key = os.path.join(root, *parts[:anchor + 1])
        picks.setdefault(key.lower(), diff.deltas.get(key) or d)
    cands = list(picks.values())
    keep = [d for d in cands if not any(
        o is not d and o.path.lower().startswith(d.path.lower().rstrip(os.sep) + os.sep) for o in cands)]
    return sorted(keep, key=lambda d: d.delta, reverse=True)[:n]


def _dark(w: QWidget) -> bool:
    return w.palette().window().color().lightness() < 128


class LevelChip(QPushButton):
    """Bouton de synthèse d'un niveau : pastille de couleur, libellé, taille."""

    def __init__(self, level: str, parent=None) -> None:
        super().__init__(parent)
        self.level = level
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(safety.HINTS[level] + "\n\nCliquez pour voir les plus gros éléments.")
        self.set_size(None)

    def set_size(self, size: int | None) -> None:
        color = safety.level_colors(_dark(self))[self.level]
        self.setText(f"{safety.LABELS[self.level]}\n{human_size(size) if size is not None else '…'}")
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; padding: 6px 12px; border: 1px solid palette(mid);"
            f" border-left: 6px solid {color}; border-radius: 4px; font-weight: bold; }}"
            f"QPushButton:checked {{ background: palette(midlight); }}")


class DetailsPanel(QWidget):
    """Explique l'élément sélectionné, ou liste les plus gros éléments d'un niveau."""

    action_requested = Signal(str, list)
    reveal = Signal(str)           # afficher dans le treemap
    zoom = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        self._path = ""
        self._growth: dict[str, int] = {}
        self._since = 0.0

        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.badge = QLabel()
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.items = QListWidget()
        self.items.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # chemins élidés
        self.items.itemClicked.connect(lambda it: self.reveal.emit(it.data(PATH_ROLE)))
        self.items.hide()

        self.explorer_btn = QPushButton("Afficher dans l'Explorateur")
        self.explorer_btn.clicked.connect(lambda: show_in_explorer(self._path))
        self.zoom_btn = QPushButton("Zoomer sur ce dossier")
        self.zoom_btn.clicked.connect(lambda: self.zoom.emit(self._path))
        self.trash_btn = QPushButton("Mettre à la corbeille…")
        self.trash_btn.clicked.connect(lambda: self.action_requested.emit(TRASH, [self._path]))
        buttons = QVBoxLayout()
        for b in (self.zoom_btn, self.explorer_btn, self.trash_btn):
            buttons.addWidget(b)
        self.buttons = QWidget()
        self.buttons.setLayout(buttons)
        buttons.setContentsMargins(0, 0, 0, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        layout.addWidget(self.items, 1)
        layout.addWidget(self.buttons)
        self.stretch = QWidget()
        layout.addWidget(self.stretch, 1)
        self.show_welcome()

    def set_growth(self, deltas: dict[str, int], since: float) -> None:
        self._growth = {os.path.normcase(k): v for k, v in deltas.items()}
        self._since = since

    def _set_badge(self, level: str) -> None:
        color = safety.level_colors(_dark(self))[level]
        self.badge.setText(f"● {safety.LABELS[level]}")
        self.badge.setStyleSheet(f"color: white; background: {color}; padding: 3px 10px; "
                                 "border-radius: 9px; font-weight: bold;")
        self.badge.show()

    def show_welcome(self) -> None:
        self._path = ""
        self.title.setText("<b>Cliquez sur un rectangle</b>")
        self.badge.hide()
        self.body.setText(
            "Chaque rectangle est un dossier ou un fichier, proportionnel à sa taille. "
            "Sa couleur indique si vous pouvez le supprimer :<br><br>"
            + "<br><br>".join(
                f"<span style='color:{safety.level_colors(_dark(self))[lv]}'>■</span> "
                f"<b>{safety.LABELS[lv]}</b> — {safety.HINTS[lv]}" for lv in safety.LEVELS)
            + "<br><br>Les éléments système ne peuvent pas être supprimés depuis Débarras, et tout ce "
              "que vous supprimez passe par la corbeille.")
        self.items.hide()
        self.buttons.hide()
        self.stretch.show()

    def show_path(self, path: str, size: int) -> None:
        self._path = path
        is_dir = os.path.isdir(path)
        v = safety.current().classify(path, is_dir)
        name = os.path.basename(path.rstrip("\\")) or path
        self.title.setText(f"<b>{name}</b><br><small>{path}</small>")
        self._set_badge(v.level)
        parts = [f"<b>{human_size(size)}</b>{' — dossier' if is_dir else ''}",
                 f"<b>Pourquoi :</b> {v.reason}.", safety.HINTS[v.level]]
        if is_dir and v.level in (safety.PERSONAL, safety.CLEANABLE):
            inside = safety.current().risky_inside(path)
            if inside:
                parts.append("<span style='color:%s'>⚠ <b>Contient des logiciels :</b></span><br>%s"
                             % (safety.level_colors(_dark(self))[safety.SOFTWARE],
                                "<br>".join(f"• {display_name(d, path)}" for d in inside)))
        d = self._growth.get(os.path.normcase(path))
        if d:
            parts.append(f"<b>Depuis le scan du {human_date(self._since)} :</b> {_signed(d)}")
        self.body.setText("<br><br>".join(parts))
        self.items.hide()
        self.buttons.show()
        self.stretch.show()
        self.zoom_btn.setVisible(is_dir)
        self.trash_btn.setEnabled(not v.blocked)
        self.trash_btn.setText("🔒 Protégé par Débarras" if v.blocked else
                               "Mettre à la corbeille… ⚠" if v.level == safety.SOFTWARE else
                               "Mettre à la corbeille…")

    def show_level(self, level: str, summary: SafetySummary | None) -> None:
        self._path = ""
        self.title.setText(f"<b>Les plus gros éléments — {safety.LABELS[level]}</b>")
        self._set_badge(level)
        self.body.setText(safety.HINTS[level] + "<br><br>Cliquez sur un élément pour le situer.")
        self.items.clear()
        for path, size, reason in (summary.top.get(level, []) if summary else []):
            it = QListWidgetItem(f"{human_size(size):>10}   {path}")
            it.setToolTip(f"{path}\n{reason}")
            it.setData(PATH_ROLE, path)
            self.items.addItem(it)
        if not self.items.count():
            self.items.addItem("Aucun élément de ce type dans ce scan.")
        self.items.show()
        self.buttons.hide()
        self.stretch.hide()


class HomeView(QWidget):
    """Synthèse + treemap + détail. Signaux relayés vers la fenêtre principale."""

    open_cleanup = Signal()
    open_history = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.treemap = TreemapView()
        self.details = DetailsPanel()
        self.summary: SafetySummary | None = None
        self._base_headline = ""
        self._growth_text = ""
        self.action_requested = self.treemap.action_requested
        self.details.action_requested.connect(self.treemap.action_requested.emit)
        self.details.reveal.connect(self._reveal)
        self.details.zoom.connect(self.treemap.canvas.navigate)
        self.treemap.canvas.item_selected.connect(self._on_selected)

        self.chips = {lv: LevelChip(lv) for lv in (safety.CLEANABLE, safety.PERSONAL, safety.SOFTWARE, safety.SYSTEM)}
        for chip in self.chips.values():
            chip.clicked.connect(lambda _=False, c=chip: self._on_chip(c))
        self.headline = QLabel("Choisissez un dossier puis lancez le scan.")
        self.headline.setWordWrap(True)
        self.headline.setTextFormat(Qt.TextFormat.RichText)
        self.headline.linkActivated.connect(self._on_link)
        cleanup_btn = QPushButton("🧹 Nettoyage guidé…")
        cleanup_btn.setToolTip("Caches des navigateurs, fichiers temporaires, Windows Update…")
        cleanup_btn.clicked.connect(self.open_cleanup.emit)

        chips = QHBoxLayout()
        for chip in self.chips.values():
            chips.addWidget(chip)
        chips.addStretch(1)
        chips.addWidget(cleanup_btn)
        banner = QFrame()
        banner.setFrameShape(QFrame.Shape.StyledPanel)
        bl = QVBoxLayout(banner)
        bl.addWidget(self.headline)
        bl.addLayout(chips)

        split = QSplitter()
        split.addWidget(self.treemap)
        split.addWidget(self.details)
        split.setStretchFactor(0, 1)
        split.setSizes([820, 300])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(banner)
        layout.addWidget(split, 1)

    # --- données -----------------------------------------------------------------------

    def clear(self) -> None:
        self.summary = None
        self.treemap.clear()
        for chip in self.chips.values():
            chip.set_size(None)
            chip.setChecked(False)
        self._base_headline = self._growth_text = ""
        self.headline.setText("Aucun scan pour ce dossier : lancez le scan.")
        self.details.set_growth({}, 0)
        self.details.show_welcome()

    def set_loading(self, root: str, total: int) -> None:
        for chip in self.chips.values():
            chip.set_size(None)
            chip.setChecked(False)
        self.details.show_welcome()
        self._base_headline = f"<b>{human_size(total)}</b> analysés dans {root} — classement en cours…"
        self.headline.setText(self._base_headline + self._growth_text)

    def set_summary(self, root: str, total: int, summary: SafetySummary) -> None:
        self.summary = summary
        for lv, chip in self.chips.items():
            chip.set_size(summary.totals.get(lv, 0))
        clean = summary.totals.get(safety.CLEANABLE, 0)
        text = (f"<b>{human_size(total)}</b> analysés dans {root}. "
                f"<b>{human_size(clean)}</b> peuvent être supprimés sans aucun risque"
                f" (<a href='cleanable'>voir</a>)." if clean else
                f"<b>{human_size(total)}</b> analysés dans {root}.")
        self._base_headline = text
        self.headline.setText(text + self._growth_text)

    def set_growth(self, diff: HistoryDiff | None) -> None:
        """Résumé de ce qui a changé depuis le scan précédent (même dossier)."""
        if diff is None:
            self._growth_text = ""
            self.treemap.canvas.set_growth({}, 0)
            self.details.set_growth({}, 0)
        else:
            deltas = {p: d.delta for p, d in diff.deltas.items()}
            since = diff.old.finished or diff.old.started
            self.treemap.canvas.set_growth(deltas, since)
            self.details.set_growth(deltas, since)
            detail = ", ".join(f"{short_name(d.path, diff.new.root)} {_signed(d.delta)}"
                               for d in headline_growth(diff))
            self._growth_text = (
                f"<br>Depuis le scan du {human_date(since)} : <b>{_signed(diff.total_delta)}</b>"
                + (f" — surtout {detail}" if detail and diff.total_delta > 0 else "")
                + " (<a href='growth'>voir sur la carte</a> · <a href='history'>détail</a>)")
        if self._base_headline:
            self.headline.setText(self._base_headline + self._growth_text)

    def refresh_theme(self) -> None:
        self.treemap.refresh_theme()
        for chip in self.chips.values():
            chip.set_size(self.summary.totals.get(chip.level, 0) if self.summary else None)
        if not self.details._path:
            self.details.show_welcome()

    # --- interactions ------------------------------------------------------------------

    def _on_selected(self, path: str, size: int) -> None:
        for chip in self.chips.values():
            chip.setChecked(False)
        if path:
            self.details.show_path(path, size)

    def _on_chip(self, chip: LevelChip) -> None:
        for c in self.chips.values():
            c.setChecked(c is chip and chip.isChecked())
        if chip.isChecked():
            self.details.show_level(chip.level, self.summary)
        else:
            self.details.show_welcome()

    def _on_link(self, link: str) -> None:
        if link == "cleanable":
            self.chips[safety.CLEANABLE].setChecked(True)
            self._on_chip(self.chips[safety.CLEANABLE])
        elif link == "growth":
            self.treemap.set_mode(GROWTH)
        elif link == "history":
            self.open_history.emit()

    def _reveal(self, path: str) -> None:
        """Situe un élément dans le treemap (sélectionné dans son dossier parent) et le détaille."""
        c = self.treemap.canvas
        if c._cache is None:
            return
        parent = os.path.dirname(path)
        if parent != path and c._inside_root(parent) and os.path.normcase(path) != os.path.normcase(c._root):
            c.navigate(parent, select=path)
        else:
            c.navigate(path)
        self.details.show_path(path, c._cache.size_of(c._scan_id, path))
