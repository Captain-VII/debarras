"""Confirmation, exécution en arrière-plan, journal et annulation des actions."""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMenu, QMessageBox, QProgressDialog, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core import safety
from core.actions import (
    ARCHIVE, DONE, ERROR, KIND_LABELS, MOVE, RESTORED, SIMULATED, SKIPPED, TRASH, ActionLog,
    ActionRecord, ActionWorker, plan,
)
from utils.format import human_count, human_date, human_size

SIM_STYLE = "background:#c98500; color:#000; padding:4px 8px; border-radius:3px; font-weight:bold;"
_RISK_STYLE = "border-left: 4px solid {color}; padding: 6px 10px;"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def add_action_entries(menu: QMenu, paths: list[str], emit: Callable[[str, list[str]], None],
                       verdict: safety.Verdict | None = None) -> None:
    """Ajoute les trois actions au menu contextuel d'une vue (grisées pour un élément système)."""
    if not paths:
        return
    if verdict is None and len(paths) == 1:
        verdict = safety.current().classify(paths[0])
    suffix = f" ({len(paths)})" if len(paths) > 1 else ""
    menu.addSeparator()
    if verdict is not None and verdict.blocked:
        info = menu.addAction(f"🔒 Protégé : {verdict.reason}")
        info.setEnabled(False)
        return
    warn = " ⚠" if verdict is not None and verdict.level == safety.SOFTWARE else ""
    menu.addAction(f"Mettre à la corbeille…{suffix}{warn}", lambda: emit(TRASH, paths))
    menu.addAction(f"Déplacer vers…{suffix}{warn}", lambda: emit(MOVE, paths))
    menu.addAction(f"Archiver en ZIP…{suffix}", lambda: emit(ARCHIVE, paths))


def risk_report(paths: list[str]) -> tuple[list[str], list[str]]:
    """(éléments système refusés, avertissements logiciels) pour une liste de chemins."""
    clf = safety.current()
    blocked, warnings = [], []
    for p in paths:
        v = clf.classify(p)
        if v.blocked:
            blocked.append(f"{p} — {v.reason}")
        elif v.level == safety.SOFTWARE:
            warnings.append(f"{p} — {v.reason}")
        elif os.path.isdir(p):
            inside = clf.risky_inside(p)
            if inside:
                warnings.append(f"{p} — contient des logiciels : " + ", ".join(inside))
    return blocked, warnings


class ConfirmDialog(QDialog):
    """Récapitulatif obligatoire avant toute action (réelle ou simulée)."""

    def __init__(self, kind: str, paths: list[str], sizes: dict[str, int],
                 simulated: bool, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setWindowTitle(KIND_LABELS[kind] + (" — simulation" if simulated else ""))
        self.resize(720, 460)
        total = sum(sizes.get(p, 0) for p in paths)

        layout = QVBoxLayout(self)
        if simulated:
            banner = QLabel("MODE SIMULATION — aucun fichier ne sera modifié.")
            banner.setStyleSheet(SIM_STYLE)
            layout.addWidget(banner)
        verb = {TRASH: "mettre à la corbeille", MOVE: "déplacer", ARCHIVE: "archiver"}[kind]
        layout.addWidget(QLabel(f"Vous allez {verb} {human_count(len(paths))} élément(s) — "
                                f"{human_size(total)} :"))
        lst = QListWidget()
        lst.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        lst.addItems(paths[:5000])
        if len(paths) > 5000:
            lst.addItem(f"… et {human_count(len(paths) - 5000)} autres")
        layout.addWidget(lst, 1)

        self.target = QLineEdit()
        self.trash_originals = QCheckBox(
            "Mettre les originaux à la corbeille une fois l'archive créée et vérifiée")
        self.trash_originals.setChecked(True)
        if kind in (MOVE, ARCHIVE):
            row = QHBoxLayout()
            row.addWidget(QLabel("Destination :" if kind == MOVE else "Archive :"))
            row.addWidget(self.target, 1)
            browse = QPushButton("Parcourir…")
            browse.clicked.connect(lambda: self._browse(paths))
            row.addWidget(browse)
            layout.addLayout(row)
            if kind == ARCHIVE:
                first = paths[0] if paths else ""
                name = os.path.basename(first) if len(paths) == 1 else "archive"
                self.target.setText(os.path.join(os.path.dirname(first), f"{name}.zip"))
                layout.addWidget(self.trash_originals)
        if kind == TRASH:
            layout.addWidget(QLabel("Les éléments restent récupérables depuis la corbeille "
                                    "et via Actions › Annuler la dernière action."))

        # Sécurité : éléments système (refusés) et logiciels (confirmation explicite).
        blocked, warnings = risk_report(paths) if kind != ARCHIVE or self.trash_originals.isChecked() else ([], [])
        self.understood = QCheckBox("J'ai compris : je veux quand même traiter ces éléments de logiciel")
        if blocked:
            box = QLabel("🔒 <b>Ignorés — indispensables à Windows :</b><br>" + "<br>".join(
                _esc(b) for b in blocked[:8]) + ("<br>…" if len(blocked) > 8 else ""))
            box.setWordWrap(True)
            box.setStyleSheet(_RISK_STYLE.format(color="#d03b3b"))
            layout.addWidget(box)
        if warnings:
            box = QLabel("⚠ <b>Attention — ces éléments font partie de logiciels.</b> Les supprimer peut "
                         "empêcher un programme ou un jeu de fonctionner. Désinstallez plutôt le logiciel "
                         "depuis Paramètres › Applications.<br><br>" + "<br>".join(
                             _esc(w) for w in warnings[:8]) + ("<br>…" if len(warnings) > 8 else ""))
            box.setWordWrap(True)
            box.setStyleSheet(_RISK_STYLE.format(color="#e08a00"))
            layout.addWidget(box)
            layout.addWidget(self.understood)
        else:
            self.understood.setChecked(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.addButton("Simuler" if simulated else "Confirmer",
                                    QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if warnings:
            self.ok.setEnabled(False)
            self.understood.toggled.connect(self.ok.setEnabled)

    def _browse(self, paths: list[str]) -> None:
        start = os.path.dirname(paths[0]) if paths else ""
        if self.kind == MOVE:
            path = QFileDialog.getExistingDirectory(self, "Dossier de destination", start)
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Archive ZIP", self.target.text() or start,
                                                  "Archive ZIP (*.zip)")
        if path:
            self.target.setText(os.path.normpath(path))

    def _accept(self) -> None:
        if self.kind in (MOVE, ARCHIVE) and not self.target.text().strip():
            QMessageBox.warning(self, "Destination manquante", "Choisissez une destination.")
            return
        if self.kind == ARCHIVE and not self.target.text().lower().endswith(".zip"):
            self.target.setText(self.target.text().strip() + ".zip")
        self.accept()


class ActionLogDialog(QDialog):
    """Historique des actions, avec détail par élément."""

    def __init__(self, log: ActionLog, on_undo: Callable[[], None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Journal des actions")
        self.resize(900, 520)
        self.log = log
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Date / élément", "Action", "Éléments", "Taille", "État"])
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 5):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.undo_btn = QPushButton("Annuler la dernière action")
        self.undo_btn.clicked.connect(lambda: (on_undo(), self.accept()))
        open_btn = QPushButton("Ouvrir le fichier journal")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log.path.parent))))
        close_btn = QPushButton("Fermer")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.undo_btn)
        row.addWidget(open_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tree, 1)
        layout.addLayout(row)
        self._fill()

    def _fill(self) -> None:
        for rec in reversed(self.log.load()):
            state = "simulée" if rec.simulated else ("annulée" if rec.undone else "effectuée")
            statuses = (DONE, SIMULATED, RESTORED)
            done = rec.count(*statuses)
            top = QTreeWidgetItem([
                human_date(rec.created), rec.label + (f" → {rec.target}" if rec.target else ""),
                f"{human_count(done)}/{human_count(len(rec.items))}", human_size(rec.size(*statuses)),
                state + (f" — {rec.note}" if rec.note else ""),
            ])
            for i in rec.items:
                child = QTreeWidgetItem([i.src, i.dst or "", "", human_size(i.size),
                                         i.status + (f" : {i.error}" if i.error else "")])
                child.setToolTip(0, i.src)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
        self.undo_btn.setEnabled(self.log.last_undoable() is not None)


class ActionController(QObject):
    """Point d'entrée unique des actions depuis toutes les vues."""

    finished = Signal(object)            # ActionRecord (exécuté ou annulé)
    simulation_changed = Signal(bool)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.window = parent
        self.log = ActionLog()
        self.simulated = False
        self.whitelist: list[str] = []
        self._worker: ActionWorker | None = None
        self._dialog: QProgressDialog | None = None

    def set_simulation(self, on: bool) -> None:
        self.simulated = on
        self.simulation_changed.emit(on)

    @property
    def busy(self) -> bool:
        return self._worker is not None

    # --- lancement ------------------------------------------------------------------

    def request(self, kind: str, paths: list[str], sizes: dict[str, int]) -> None:
        if self.busy:
            QMessageBox.information(self.window, "Action en cours", "Une action est déjà en cours.")
            return
        if not paths:
            return
        dlg = ConfirmDialog(kind, paths, sizes, self.simulated, self.window)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rec = plan(kind, paths, sizes, dlg.target.text().strip(),
                   dlg.trash_originals.isChecked(), self.simulated, self.whitelist)
        skipped = [i for i in rec.items if i.status == SKIPPED]
        if len(skipped) == len(rec.items):
            QMessageBox.warning(self.window, "Rien à faire", "Aucun élément ne peut être traité :\n"
                                + "\n".join(f"{i.src} — {i.error}" for i in skipped[:15]))
            return
        self._start(ActionWorker(rec, self.log), rec.label)

    def undo_last(self) -> None:
        if self.busy:
            return
        rec = self.log.last_undoable()
        if rec is None:
            QMessageBox.information(self.window, "Annuler", "Aucune action à annuler.")
            return
        n = rec.count(DONE)
        text = (f"Annuler « {rec.label} » du {human_date(rec.created)} ?\n\n"
                f"{human_count(n)} élément(s) seront remis à leur emplacement d'origine.")
        if rec.kind == ARCHIVE:
            text += f"\nL'archive {rec.target} sera envoyée à la corbeille."
        if QMessageBox.question(self.window, "Annuler la dernière action", text) != QMessageBox.StandardButton.Yes:
            return
        self._start(ActionWorker(rec, self.log, undo=True), "Annulation")

    def show_log(self) -> None:
        ActionLogDialog(self.log, self.undo_last, self.window).exec()

    # --- suivi ----------------------------------------------------------------------

    def _start(self, worker: ActionWorker, title: str) -> None:
        self._worker = worker
        self._dialog = QProgressDialog(title + "…", "Interrompre", 0, 0, self.window)
        self._dialog.setWindowTitle(title)
        self._dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._dialog.setMinimumDuration(300)
        self._dialog.setMinimumWidth(520)
        self._dialog.canceled.connect(worker.requestInterruption)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_progress(self, done: int, total: int, path: str) -> None:
        if self._dialog:
            self._dialog.setMaximum(max(total, 1))
            self._dialog.setValue(done)
            self._dialog.setLabelText(path[-90:] if path else "Finalisation…")

    def _on_completed(self, rec: ActionRecord) -> None:
        if self._dialog:
            self._dialog.reset()
            self._dialog.deleteLater()
            self._dialog = None
        self._worker = None
        self._report(rec)
        self.finished.emit(rec)

    def _report(self, rec: ActionRecord) -> None:
        if rec.undone:
            ok = rec.count(RESTORED)
            failed = [i for i in rec.items if i.error.startswith("annulation")]
            msg = f"{human_count(ok)} élément(s) restauré(s)."
        else:
            ok = rec.count(DONE, SIMULATED)
            failed = [i for i in rec.items if i.status in (ERROR, SKIPPED)]
            verb = "seraient traités" if rec.simulated else "traités"
            msg = f"{human_count(ok)} élément(s) {verb} — {human_size(rec.size(DONE, SIMULATED))}."
        if rec.note:
            msg += f"\n{rec.note}"
        if failed:
            msg += f"\n\n{human_count(len(failed))} non traité(s) :\n" + "\n".join(
                f"• {i.src} — {i.error}" for i in failed[:12])
            if len(failed) > 12:
                msg += "\n…"
        title = rec.label + (" (simulation)" if rec.simulated else "")
        (QMessageBox.warning if failed else QMessageBox.information)(self.window, title, msg)
