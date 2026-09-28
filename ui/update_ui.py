"""Interface de mise à jour : vérification (auto/manuelle), fenêtre « nouvelle version »,
téléchargement avec progression, installation et redémarrage."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressDialog, QTextBrowser,
    QVBoxLayout,
)

from core.updater import (
    Release, UpdateChecker, UpdateDownloader, can_self_update, install_dir, launch_swap, work_dir,
    write_swap_script,
)
from utils.format import human_size
from version import __version__

AUTO_CHECK_DELAY_MS = 4000
AUTO_CHECK_INTERVAL = 24 * 3600

INSTALL, SKIP, PAGE = 10, 11, 12  # codes de retour de UpdateDialog


class UpdateDialog(QDialog):
    def __init__(self, rel: Release, self_update: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mise à jour disponible")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<h3>Débarras {rel.version} est disponible</h3>"
                                f"Version installée : {__version__} — téléchargement : {human_size(rel.asset_size)}"))
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(rel.notes or "_Pas de notes de version._")
        layout.addWidget(notes, 1)
        if not self_update:
            hint = QLabel("L'installation automatique n'est possible que depuis Debarras.exe placé dans "
                          "un dossier modifiable. Téléchargez la nouvelle version depuis sa page.")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        buttons = QDialogButtonBox()
        if self_update:
            buttons.addButton("Installer et redémarrer", QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(
                lambda: self.done(INSTALL))
        else:
            buttons.addButton("Ouvrir la page de téléchargement", QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(
                lambda: self.done(PAGE))
        buttons.addButton("Ignorer cette version", QDialogButtonBox.ButtonRole.DestructiveRole).clicked.connect(
            lambda: self.done(SKIP))
        buttons.addButton("Plus tard", QDialogButtonBox.ButtonRole.RejectRole).clicked.connect(self.reject)
        layout.addWidget(buttons)


class UpdateController(QObject):
    """Branché sur la fenêtre principale (paramètres dans window.settings)."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self._checker: UpdateChecker | None = None
        self._downloader: UpdateDownloader | None = None
        self._progress: QProgressDialog | None = None

    # --- vérification ------------------------------------------------------------------

    def schedule_auto_check(self) -> None:
        QTimer.singleShot(AUTO_CHECK_DELAY_MS, self._auto_check)

    def _auto_check(self) -> None:
        s = self.window.settings
        if os.environ.get("DEBARRAS_NO_UPDATE_CHECK"):  # tests, environnements hors ligne
            return
        if s.check_updates and time.time() - s.last_update_check > AUTO_CHECK_INTERVAL:
            self.check(manual=False)

    def check(self, manual: bool = True) -> None:
        if self._checker or self._downloader:
            return
        self._checker = UpdateChecker()
        self._checker.done.connect(lambda rel: self._on_checked(rel, manual))
        self._checker.failed.connect(lambda msg: self._on_failed(msg, manual))
        self._checker.finished.connect(self._checker_done)
        self._checker.start()

    def _checker_done(self) -> None:
        if self._checker:
            self._checker.deleteLater()
        self._checker = None

    def _remember_check(self) -> None:
        s = self.window.settings
        s.last_update_check = time.time()
        try:
            s.save()
        except OSError:
            pass

    def _on_failed(self, msg: str, manual: bool) -> None:
        if manual:  # en automatique, on reste silencieux (hors ligne, etc.)
            QMessageBox.warning(self.window, "Mise à jour", f"Vérification impossible : {msg}")

    def _on_checked(self, rel: Release | None, manual: bool) -> None:
        self._remember_check()
        if rel is None:
            if manual:
                QMessageBox.information(self.window, "Mise à jour",
                                        f"Vous utilisez la dernière version de Débarras ({__version__}).")
            return
        s = self.window.settings
        if not manual and rel.version == s.skipped_version:
            return
        choice = UpdateDialog(rel, can_self_update(), self.window).exec()
        if choice == INSTALL:
            self._download(rel)
        elif choice == PAGE:
            QDesktopServices.openUrl(QUrl(rel.page_url))
        elif choice == SKIP:
            s.skipped_version = rel.version
            self._remember_check()

    # --- téléchargement et installation ---------------------------------------------------

    def _download(self, rel: Release) -> None:
        self._progress = QProgressDialog(f"Téléchargement de Débarras {rel.version}…", "Annuler", 0, 1000,
                                         self.window)
        self._progress.setWindowTitle("Mise à jour")
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._downloader = UpdateDownloader(rel, work_dir())
        self._progress.canceled.connect(self._downloader.requestInterruption)
        self._downloader.progress.connect(
            lambda d, t: self._progress and self._progress.setValue(int(1000 * d / t) if t else 0))
        self._downloader.ready.connect(lambda staged: self._install(rel, staged))
        self._downloader.failed.connect(lambda msg: QMessageBox.warning(
            self.window, "Mise à jour", f"La mise à jour n'a pas été installée : {msg}"))
        self._downloader.finished.connect(self._download_done)
        self._downloader.start()

    def _download_done(self) -> None:
        if self._progress:
            self._progress.reset()
            self._progress.deleteLater()
            self._progress = None
        if self._downloader:
            self._downloader.deleteLater()
        self._downloader = None

    def _install(self, rel: Release, staged) -> None:
        if getattr(self.window, "actions", None) and self.window.actions.busy:
            QMessageBox.information(self.window, "Mise à jour", "Une action est en cours : relancez la "
                                    "mise à jour quand elle sera terminée.")
            return
        answer = QMessageBox.question(
            self.window, "Installer la mise à jour",
            f"Débarras {rel.version} est prêt (empreinte SHA-256 vérifiée).\n\n"
            "Débarras va se fermer, remplacer ses fichiers puis redémarrer. "
            "L'ancienne version est gardée à côté (dossier « .old »).")
        if answer != QMessageBox.StandardButton.Yes:
            return
        script = write_swap_script(os.getpid(), install_dir(), staged)
        launch_swap(script)
        self.window.close()
        QApplication.quit()

    def shutdown(self) -> None:
        for th in (self._checker, self._downloader):
            if th:
                th.requestInterruption()
                th.wait(3000)
