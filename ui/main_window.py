"""Fenêtre principale : choix/glisser-déposer du dossier, scan, onglets, menus."""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QAction, QActionGroup, QCloseEvent, QDesktopServices, QDragEnterEvent, QDropEvent, QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from core import safety
from core.actions import TRASH, ActionRecord
from core.cache import Cache, ScanInfo
from core.safety import SafetyWorker
from core.scanner import Scanner, ScanResult
from core.stats import StatsResult, StatsWorker
from core.updater import install_dir
from ui.actions_ui import SIM_STYLE, ActionController
from ui.charts import ChartsView
from ui.cleanup_view import CleanupView
from ui.dup_view import DupView
from ui.history_view import HistoryView
from ui.home_view import HomeView
from ui.search_view import SearchView
from ui.samename_view import SameNameView
from ui.similar_view import SimilarView
from ui.settings import THEMES, Settings, SettingsDialog, apply_theme
from ui.stats_view import StatsView
from ui.tree_view import TreeView
from ui.update_ui import UpdateController
from utils.export import ReportData, write_csv, write_html_report
from utils.format import human_count, human_date, human_duration, human_size


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Débarras")
        self.resize(1100, 720)
        self.setAcceptDrops(True)

        self.settings = Settings.load()
        self.cache = Cache()
        self.scanner: Scanner | None = None
        self.current_scan: ScanInfo | None = None
        self._stats: StatsResult | None = None

        # Barre du haut : dossier + boutons
        self.path_box = QComboBox(editable=True)
        self.path_box.setMinimumWidth(400)
        self.path_box.lineEdit().setPlaceholderText("Glissez un dossier ici ou choisissez-en un…")
        self.path_box.addItems(self.cache.roots())
        self.path_box.setCurrentIndex(-1)
        self.path_box.activated.connect(lambda _: self._load_latest(self._root()))
        self.path_box.lineEdit().returnPressed.connect(self.start_scan)

        browse_btn = QPushButton("Parcourir…")
        browse_btn.clicked.connect(self._browse)
        self.scan_btn = QPushButton("Scanner")
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_scan)

        top = QHBoxLayout()
        top.addWidget(QLabel("Dossier :"))
        top.addWidget(self.path_box, 1)
        top.addWidget(browse_btn)
        top.addWidget(self.scan_btn)
        top.addWidget(self.cancel_btn)
        self.sim_label = QLabel("SIMULATION")
        self.sim_label.setStyleSheet(SIM_STYLE)
        self.sim_label.setToolTip("Mode simulation : les actions sont journalisées mais rien n'est modifié.")
        self.sim_label.hide()
        top.addWidget(self.sim_label)

        # Progression
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indéterminé
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        self.progress_label = QLabel()
        self.progress_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # Onglets
        self.tree = TreeView()
        self.home = HomeView()
        self.treemap = self.home.treemap
        self.cleanup_view = CleanupView()
        self.stats_view = StatsView()
        self.charts = ChartsView()
        self.dup_view = DupView()
        self.similar_view = SimilarView()
        self.samename_view = SameNameView()
        self.history_view = HistoryView()
        self.search_view = SearchView()
        self.tabs = QTabWidget()
        for widget, title in ((self.home, "Accueil"), (self.tree, "Arborescence"),
                              (self.stats_view, "Statistiques"), (self.charts, "Graphiques"),
                              (self.dup_view, "Doublons"), (self.similar_view, "Images similaires"),
                              (self.samename_view, "Même nom"), (self.cleanup_view, "Nettoyage"),
                              (self.history_view, "Historique"),
                              (self.search_view, "Recherche")):
            self.tabs.addTab(widget, title)
        self.tabs.currentChanged.connect(self._on_tab)
        self.home.open_cleanup.connect(lambda: self.tabs.setCurrentWidget(self.cleanup_view))
        self.home.open_history.connect(lambda: self.tabs.setCurrentWidget(self.history_view))
        self.history_view.diff_ready.connect(self._on_diff)
        self._safety_worker: SafetyWorker | None = None
        self.stats_view.old_days_changed.connect(lambda _: self._compute_stats())
        self._stats_workers: set[StatsWorker] = set()
        self.tree.show_in_treemap.connect(self._show_in_treemap)
        self.search_view.show_in_treemap.connect(self._show_in_treemap)
        self.treemap.canvas.item_selected.connect(
            lambda path, size: self.statusBar().showMessage(f"{path} — {human_size(size)}")
        )

        # Actions (corbeille, déplacement, archivage)
        self.actions = ActionController(self)
        for view in (self.tree, self.treemap, self.stats_view, self.dup_view, self.similar_view,
                     self.samename_view, self.search_view):
            view.action_requested.connect(self._request_action)
        self.actions.finished.connect(self._on_action_done)
        self.cleanup_view.cleanup_requested.connect(lambda paths, sizes: self.actions.request(TRASH, paths, sizes))
        self.actions.simulation_changed.connect(self.sim_label.setVisible)
        self.updates = UpdateController(self)
        self._apply_settings()
        self._build_menu()
        self.updates.schedule_auto_check()

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.tabs, 1)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        if self.path_box.count():
            self.path_box.setCurrentIndex(0)
            self._load_latest(self._root())

    # --- menus ----------------------------------------------------------------------

    def _action(self, menu, text: str, slot, shortcut=None) -> QAction:
        act = QAction(text, self)
        if shortcut is not None:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        menu.addAction(act)
        return act

    def _build_menu(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&Fichier")
        self._action(file_menu, "Exporter l'onglet en CSV…", self.export_csv, "Ctrl+E")
        self._action(file_menu, "Exporter le rapport HTML…", self.export_html, "Ctrl+Shift+E")
        file_menu.addSeparator()
        self._action(file_menu, "Paramètres…", self.open_settings, "Ctrl+,")
        file_menu.addSeparator()
        self._action(file_menu, "Quitter", self.close, QKeySequence.StandardKey.Quit)

        view_menu = bar.addMenu("A&ffichage")
        self._action(view_menu, "Rechercher…", self._focus_search, QKeySequence.StandardKey.Find)
        theme_menu = view_menu.addMenu("Thème")
        self._theme_group = QActionGroup(self)
        for key, label in THEMES.items():
            act = QAction(label, self, checkable=True)
            act.setData(key)
            act.setChecked(key == self.settings.theme)
            act.triggered.connect(lambda _=False, k=key: self._set_theme(k))
            self._theme_group.addAction(act)
            theme_menu.addAction(act)

        menu = bar.addMenu("&Actions")
        self.sim_action = QAction("Mode simulation", self, checkable=True)
        self.sim_action.toggled.connect(self.actions.set_simulation)
        menu.addAction(self.sim_action)
        menu.addSeparator()
        self._action(menu, "Annuler la dernière action", self.actions.undo_last, QKeySequence.StandardKey.Undo)
        self._action(menu, "Journal des actions…", self.actions.show_log)

        help_menu = bar.addMenu("&Aide")
        self._action(help_menu, "Rechercher des mises à jour…", lambda: self.updates.check(manual=True))
        self._action(help_menu, "À propos de Débarras", self._about)

    # --- paramètres et thème ------------------------------------------------------------

    def _apply_settings(self) -> None:
        self.actions.whitelist = list(self.settings.whitelist)
        self.dup_view.whitelist = list(self.settings.whitelist)
        self.similar_view.whitelist = list(self.settings.whitelist)
        self.samename_view.whitelist = list(self.settings.whitelist)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() != SettingsDialog.DialogCode.Accepted:
            return
        old_theme = self.settings.theme
        self.settings = dlg.result_settings()
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "Paramètres", f"Enregistrement impossible : {exc}")
        self._apply_settings()
        if self.settings.theme != old_theme:
            self._set_theme(self.settings.theme)

    def _set_theme(self, theme: str) -> None:
        self.settings.theme = theme
        try:
            self.settings.save()
        except OSError:
            pass
        for act in self._theme_group.actions():
            act.setChecked(act.data() == theme)
        apply_theme(theme)
        # Treemap et graphiques calculent leurs couleurs selon la palette : on redessine.
        self.home.refresh_theme()
        if self.current_scan:
            self._load_latest(self.current_scan.root)
        else:
            self.charts.clear()
            self.history_view.clear()

    def _about(self) -> None:
        version = QApplication.applicationVersion()
        box = QMessageBox(self)
        box.setWindowTitle("À propos de Débarras")
        box.setIconPixmap(QApplication.windowIcon().pixmap(96, 96))
        box.setText(
            f"<h3>Débarras {version}</h3>"
            "<p>Voir ce qui encombre le disque, et s'en débarrasser sans risque.</p>"
            "<p>Arborescence et treemap, doublons, fichiers anciens, caches et installeurs "
            "oubliés, historique des scans.<br>Jamais de suppression définitive : tout passe "
            "par la corbeille et peut être annulé.</p>"
            f"<p><small>Données : {os.path.dirname(self.cache.db_path)}</small></p>")
        box.exec()

    def _focus_search(self) -> None:
        self.tabs.setCurrentWidget(self.search_view)
        self.search_view.focus_search()

    # --- exports --------------------------------------------------------------------

    def _default_name(self, label: str, ext: str) -> str:
        base = os.path.basename(self.current_scan.root.rstrip(os.sep)) if self.current_scan else ""
        name = f"{label}_{base or 'analyse'}_{datetime.now():%Y%m%d_%H%M}.{ext}"
        return os.path.join(os.path.expanduser("~"), "Documents", name.replace(":", ""))

    def export_csv(self) -> None:
        widget = self.tabs.currentWidget()
        label = self.tabs.tabText(self.tabs.currentIndex())
        table = None
        if widget in (self.tree, self.home) and self.current_scan:
            rows = self.cache.conn.execute(
                "SELECT path, size, file_count FROM dir_sizes WHERE scan_id=? ORDER BY size DESC",
                (self.current_scan.id,)).fetchall()
            table = (["Dossier", "Octets", "Fichiers"], rows)
        elif widget is self.charts and self._stats:
            table = (["Catégorie", "Octets", "Fichiers"],
                     [[c, s, n] for c, (s, n) in self._stats.by_category.items()])
        elif hasattr(widget, "export_table"):
            table = widget.export_table()
        if not table or not table[1]:
            QMessageBox.information(self, "Export CSV", "Rien à exporter dans cet onglet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exporter en CSV", self._default_name(label, "csv"),
                                              "CSV (*.csv)")
        if not path:
            return
        try:
            n = write_csv(path, *table)
        except OSError as exc:
            QMessageBox.critical(self, "Export CSV", f"Écriture impossible : {exc}")
            return
        self.statusBar().showMessage(f"{human_count(n)} lignes exportées vers {path}", 8000)

    def export_html(self) -> None:
        scan = self.current_scan
        if scan is None or self._stats is None or self._stats.scan_id != scan.id:
            QMessageBox.information(self, "Rapport HTML", "Chargez un scan et attendez la fin du "
                                    "calcul des statistiques.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Rapport HTML", self._default_name("rapport", "html"),
                                              "Page HTML (*.html)")
        if not path:
            return
        top_dirs = sorted(self.cache.child_dirs(scan.id, scan.root), key=lambda d: d[1], reverse=True)[:25]
        history = self.history_view._diff
        if history and history.new.id != scan.id:
            history = None
        data = ReportData(scan, self._stats, top_dirs, self.dup_view._result, history)
        try:
            write_html_report(path, data)
        except OSError as exc:
            QMessageBox.critical(self, "Rapport HTML", f"Écriture impossible : {exc}")
            return
        box = QMessageBox(QMessageBox.Icon.Information, "Rapport HTML", f"Rapport enregistré :\n{path}",
                          QMessageBox.StandardButton.Close, self)
        open_btn = box.addButton("Ouvrir", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # --- actions --------------------------------------------------------------------

    def _request_action(self, kind: str, paths: list[str]) -> None:
        scan_id = self.current_scan.id if self.current_scan else 0
        sizes = {p: self.cache.size_of(scan_id, p) for p in paths}
        self.actions.request(kind, paths, sizes)

    def _on_action_done(self, rec: ActionRecord) -> None:
        if rec.simulated:
            return
        if self.cleanup_view.analyzed:
            self.cleanup_view.analyze()
        # Les fichiers ont bougé : rescan incrémental (hash conservés) pour rafraîchir les vues.
        self.dup_view.prune_missing()
        self.similar_view.prune_missing()
        self.samename_view.prune_missing()
        if self.current_scan and self.scanner is None:
            self._set_root(self.current_scan.root)
            self.start_scan()

    # --- dossier cible --------------------------------------------------------------

    def _root(self) -> str:
        text = self.path_box.currentText().strip().strip('"')
        return os.path.abspath(text) if text else ""

    def _set_root(self, path: str) -> None:
        self.path_box.setCurrentText(os.path.abspath(path))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choisir un dossier", self._root())
        if path:
            self._set_root(path)
            if not self._load_latest(self._root()):
                self.start_scan()

    def open_folder(self, path: str) -> None:
        self._set_root(path)
        self.start_scan()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and os.path.isdir(urls[0].toLocalFile()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        event.acceptProposedAction()
        self.open_folder(event.mimeData().urls()[0].toLocalFile())

    # --- scan -----------------------------------------------------------------------

    def start_scan(self) -> None:
        if self.scanner is not None:
            return
        root = self._root()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Dossier invalide", f"Dossier introuvable :\n{root}")
            return
        self.scanner = Scanner(root, self.cache.db_path, self.settings.scan_options())
        self.scanner.progress.connect(self._on_progress)
        self.scanner.scan_finished.connect(self._on_finished)
        self.scanner.scan_failed.connect(self._on_failed)
        self.scanner.finished.connect(self._on_thread_done)
        self._set_scanning(True)
        self.progress_label.setText(f"Scan de {root}…")
        self.scanner.start()

    def _cancel_scan(self) -> None:
        if self.scanner:
            self.cancel_btn.setEnabled(False)
            self.progress_label.setText("Annulation…")
            self.scanner.cancel()

    def _set_scanning(self, on: bool) -> None:
        self.scan_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        self.path_box.setEnabled(not on)
        self.progress_bar.setVisible(on)

    def _on_progress(self, files: int, size: int, current: str) -> None:
        if current:
            self.progress_label.setText(
                f"{human_count(files)} fichiers — {human_size(size)} — {current}"
            )

    def _on_finished(self, r: ScanResult) -> None:
        if r.cancelled:
            self.progress_label.setText(
                f"Scan annulé après {human_duration(r.duration)} "
                f"({human_count(r.file_count)} fichiers parcourus)."
            )
            return
        if self.path_box.findText(r.root) < 0:
            self.path_box.insertItem(0, r.root)
        self._load_latest(r.root)

    def _on_failed(self, message: str) -> None:
        self.progress_label.setText(message)
        QMessageBox.critical(self, "Échec du scan", message)

    def _on_thread_done(self) -> None:
        if self.scanner:
            self.scanner.deleteLater()
        self.scanner = None
        self._set_scanning(False)

    # --- affichage ------------------------------------------------------------------

    def _load_latest(self, root: str) -> bool:
        """Affiche le dernier scan terminé de `root`, s'il existe."""
        scan = self.cache.latest_scan(root) if root else None
        self.current_scan = scan
        self._stats = None
        if scan is None:
            self.tree.clear()
            self.home.clear()
            self.stats_view.clear()
            self.charts.clear()
            self.dup_view.set_scan(None, "")
            self.similar_view.set_scan(None, "")
            self.samename_view.set_scan(None, "")
            self.history_view.clear()
            self.search_view.set_scan(None, "")
            self.progress_label.setText("Aucun scan pour ce dossier.")
            return False
        self.tree.load(self.cache, scan.id, scan.root)
        self.home.set_growth(None)
        self.treemap.load(self.cache, scan.id, scan.root)
        self.home.set_loading(scan.root, scan.total_size)
        self._classify(scan)
        self._compute_stats()
        self.dup_view.set_scan(self.cache.db_path, scan.root)
        self.similar_view.set_scan(self.cache.db_path, scan.root)
        self.samename_view.set_scan(self.cache.db_path, scan.root)
        self.history_view.set_root(self.cache, scan.root)
        self.search_view.set_scan(self.cache, scan.root)
        duration = (scan.finished or scan.started) - scan.started
        errors = f" — {scan.errors} élément(s) inaccessible(s)" if scan.errors else ""
        self.progress_label.setText(
            f"Scan du {human_date(scan.finished)} — {human_count(scan.file_count)} fichiers — "
            f"{human_size(scan.total_size)} — {human_duration(duration)}{errors}"
        )
        return True

    def _compute_stats(self) -> None:
        """Lance le calcul des stats du scan courant en arrière-plan."""
        scan = self.current_scan
        if scan is None:
            return
        self.stats_view.set_loading()
        worker = StatsWorker(self.cache.db_path, scan.id, scan.root, self.stats_view.current_old_days())
        worker.done.connect(self._on_stats)
        worker.failed.connect(lambda msg: self.stats_view.clear(msg))
        worker.finished.connect(lambda w=worker: self._stats_workers.discard(w) or w.deleteLater())
        self._stats_workers.add(worker)
        worker.start()

    def _on_stats(self, r: StatsResult) -> None:
        scan = self.current_scan
        if scan is None or scan.id != r.scan_id or r.old_days != self.stats_view.current_old_days():
            return  # résultat périmé
        self._stats = r
        self.stats_view.set_result(r)
        self.charts.set_data(self.cache, r)

    def _classify(self, scan: ScanInfo) -> None:
        """Repère les logiciels du scan et répartit l'espace par niveau de risque (arrière-plan)."""
        if self._safety_worker:
            self._safety_worker.requestInterruption()
        app = install_dir()
        worker = SafetyWorker(self.cache.db_path, scan.id, scan.root, str(app) if app else None)
        worker.done.connect(lambda res, w=worker: self._safety_worker is w and self._on_safety(w.scan_id, *res))
        worker.failed.connect(lambda msg: self.statusBar().showMessage(msg, 8000))
        worker.finished.connect(lambda w=worker: self._safety_done(w))
        self._safety_worker = worker
        worker.start()

    def _safety_done(self, worker: SafetyWorker) -> None:
        if self._safety_worker is worker:
            self._safety_worker = None
        worker.deleteLater()

    def _on_safety(self, scan_id: int, clf: safety.Classifier, summary: safety.SafetySummary) -> None:
        scan = self.current_scan
        if scan is None or scan.id != scan_id:
            return  # résultat périmé
        safety.set_current(clf)
        self.treemap.canvas.refresh()
        self.home.set_summary(scan.root, scan.total_size, summary)

    def _on_diff(self, diff) -> None:
        if self.current_scan and diff.new.id == self.current_scan.id:
            self.home.set_growth(diff)

    def _on_tab(self, index: int) -> None:
        if self.tabs.widget(index) is self.cleanup_view and not self.cleanup_view.analyzed:
            self.cleanup_view.analyze()

    def _show_in_treemap(self, path: str) -> None:
        self.treemap.show_path(path)
        self.tabs.setCurrentWidget(self.home)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.scanner:
            self.scanner.cancel()
            self.scanner.wait()
        self.dup_view.shutdown()
        self.similar_view.shutdown()
        self.samename_view.shutdown()
        self.updates.shutdown()
        self.history_view.shutdown()
        self.search_view.shutdown()
        self.cleanup_view.shutdown()
        if self._safety_worker:
            self._safety_worker.requestInterruption()
            self._safety_worker.wait()
        for w in list(self._stats_workers):
            w.wait()
        self.cache.close()
        super().closeEvent(event)
