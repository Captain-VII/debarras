"""Paramètres (JSON) : exclusions, liste noire, liste blanche, thème — et leur fenêtre d'édition."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from core.cache import default_db_path
from core.scanner import ScanOptions, default_excluded_paths, old_default_excluded_paths

THEMES = {"system": "Système", "light": "Clair", "dark": "Sombre"}


@dataclass
class Settings:
    excluded_paths: list[str] = field(default_factory=default_excluded_paths)
    excluded_names: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)   # ignorés au scan (motifs sur le nom)
    whitelist: list[str] = field(default_factory=list)   # protégés : jamais traités par les actions
    theme: str = "system"
    check_updates: bool = True        # vérifier les nouvelles versions au démarrage (1×/jour)
    last_update_check: float = 0.0
    skipped_version: str = ""         # version que l'utilisateur a choisi d'ignorer
    advanced: bool = False            # mode avancé : tous les onglets et le choix libre du dossier
    version: int = 2                  # format des paramètres (2 : exclusions allégées, 1.6)

    @staticmethod
    def path() -> Path:
        return default_db_path().parent / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            if data.get("version", 1) < 2:
                known = cls._migrate_v1(known)
            return cls(**known)
        except (OSError, ValueError, TypeError):
            return cls()

    @staticmethod
    def _migrate_v1(known: dict) -> dict:
        """1.5 -> 1.6 : les anciennes exclusions par défaut (Program Files, AppData) masquaient
        une partie du disque. Retirées si l'utilisateur ne les avait pas modifiées."""
        known = dict(known, version=2)
        norm = lambda paths: sorted(os.path.normcase(p) for p in paths)  # noqa: E731
        if norm(known.get("excluded_paths", [])) == norm(old_default_excluded_paths()):
            known["excluded_paths"] = default_excluded_paths()
        if known.get("excluded_names") == ["AppData"]:
            known["excluded_names"] = []
        return known

    def save(self) -> None:
        tmp = self.path().with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path())

    def scan_options(self) -> ScanOptions:
        return ScanOptions(list(self.excluded_paths), list(self.excluded_names), list(self.blacklist))


def apply_theme(theme: str) -> None:
    """Clair / sombre / système, via le schéma de couleurs de Qt."""
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(theme, Qt.ColorScheme.Unknown)
    QApplication.styleHints().setColorScheme(scheme)


def _lines(edit: QPlainTextEdit) -> list[str]:
    return [l.strip() for l in edit.toPlainText().splitlines() if l.strip()]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paramètres")
        self.resize(640, 640)

        # Exclusions
        self.paths = QListWidget()
        self.paths.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        add = QPushButton("Ajouter…")
        add.clicked.connect(self._add_path)
        remove = QPushButton("Retirer")
        remove.clicked.connect(lambda: [self.paths.takeItem(self.paths.row(i))
                                        for i in self.paths.selectedItems()])
        btns = QVBoxLayout()
        btns.addWidget(add)
        btns.addWidget(remove)
        btns.addStretch(1)
        prow = QHBoxLayout()
        prow.addWidget(self.paths, 1)
        prow.addLayout(btns)
        self.names = QLineEdit()
        self.names.setPlaceholderText("node_modules, .git, …")
        excl = QGroupBox("Exclusions du scan")
        el = QVBoxLayout(excl)
        el.addWidget(QLabel("Dossiers exclus :"))
        el.addLayout(prow)
        form = QFormLayout()
        form.addRow("Noms exclus partout :", self.names)
        el.addLayout(form)

        # Listes noire / blanche
        self.black = QPlainTextEdit()
        self.black.setPlaceholderText("Un motif par ligne, ex. :\n*.log\nnode_modules\nThumbs.db")
        self.black.setMaximumHeight(110)
        black = QGroupBox("Liste noire — ignorés lors du scan")
        bl = QVBoxLayout(black)
        bl.addWidget(QLabel("Fichiers et dossiers dont le nom correspond (jokers * et ?)."))
        bl.addWidget(self.black)

        self.white = QPlainTextEdit()
        self.white.setPlaceholderText("Un chemin ou un motif par ligne, ex. :\nC:\\Users\\moi\\Documents\\Important\n*.kdbx")
        self.white.setMaximumHeight(110)
        add_white = QPushButton("Ajouter un dossier…")
        add_white.clicked.connect(self._add_white)
        white = QGroupBox("Liste blanche — protégés")
        wl = QVBoxLayout(white)
        wl.addWidget(QLabel("Jamais cochés par la sélection automatique ni traités par une action "
                            "(corbeille, déplacement, archivage)."))
        wl.addWidget(self.white)
        wl.addWidget(add_white, 0, Qt.AlignmentFlag.AlignLeft)

        # Apparence
        self.theme = QComboBox()
        for key, label in THEMES.items():
            self.theme.addItem(label, key)
        look = QGroupBox("Apparence et mises à jour")
        ll = QFormLayout(look)
        ll.addRow("Thème :", self.theme)
        self.updates = QCheckBox("Vérifier les nouvelles versions au démarrage (une fois par jour)")
        self.updates.setToolTip("Consulte les releases publiques de Débarras sur GitHub. "
                                "Aucune donnée n'est envoyée.")
        ll.addRow(self.updates)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            lambda: self._fill(Settings()))

        layout = QVBoxLayout(self)
        layout.addWidget(excl)
        layout.addWidget(black)
        layout.addWidget(white)
        layout.addWidget(look)
        note = QLabel("Les exclusions et la liste noire s'appliquent au prochain scan.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self._fill(settings)

    def _fill(self, s: Settings) -> None:
        self._state = (s.last_update_check, s.skipped_version, s.advanced)  # non éditables, conservés
        self.updates.setChecked(s.check_updates)
        self.paths.clear()
        self.paths.addItems(s.excluded_paths)
        self.names.setText(", ".join(s.excluded_names))
        self.black.setPlainText("\n".join(s.blacklist))
        self.white.setPlainText("\n".join(s.whitelist))
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))

    def _add_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Dossier à exclure")
        if path:
            self.paths.addItem(os.path.normpath(path))

    def _add_white(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Dossier à protéger")
        if path:
            self.white.appendPlainText(os.path.normpath(path))

    def result_settings(self) -> Settings:
        return Settings(
            excluded_paths=[self.paths.item(i).text() for i in range(self.paths.count())],
            excluded_names=[n.strip() for n in self.names.text().split(",") if n.strip()],
            blacklist=_lines(self.black),
            whitelist=_lines(self.white),
            theme=self.theme.currentData(),
            check_updates=self.updates.isChecked(),
            last_update_check=self._state[0],
            skipped_version=self._state[1],
            advanced=self._state[2],
        )
