"""Onglet Même nom : versions divergentes d'un fichier (groupes à cocher, comme les doublons)."""
from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QTreeWidgetItem

from core.samename import MAX_GROUP, SameNameFinder
from ui.dup_view import DupView
from utils.export import iso
from utils.format import human_count, human_date, human_duration, human_size


class SameNameView(DupView):
    RULES = [("Garder la version la plus récente", "newest"),
             ("Garder la plus ancienne", "oldest"),
             ("Garder la plus volumineuse", "largest"),
             ("Garder le chemin prioritaire", "priority")]
    SEARCH_LABEL = "Rechercher les homonymes"
    MIN_SIZE_KO = 1
    HEADERS = ["Fichier", "Version", "Taille", "Modifié"]

    # --- points d'extension de DupView -----------------------------------------------------

    def _extra_search_options(self, layout: QHBoxLayout) -> None:
        self.types = QComboBox()
        self.types.addItem("Documents, images, audio, vidéo, archives", True)
        self.types.addItem("Tous les types de fichiers", False)
        self.skip_dev = QCheckBox("Ignorer code, dépendances et dossiers d'applications")
        self.skip_dev.setChecked(True)
        self.skip_dev.setToolTip("node_modules, .venv, .git, __pycache__, build, AppData, et tout "
                                 "dossier dont le nom commence par un point (.claude, .vscode…).\n"
                                 "Les noms génériques (readme, index.*, desktop.ini…) et ceux présents "
                                 f"plus de {MAX_GROUP} fois sont toujours ignorés.")
        layout.addWidget(QLabel("Types :"))
        layout.addWidget(self.types)
        layout.addWidget(self.skip_dev)

    def _make_finder(self):
        return SameNameFinder(self._db_path, self._root, self.min_size.value() * 1024,
                              self.types.currentData(), self.skip_dev.isChecked())

    def _group_label(self, g) -> list[str]:
        newest = max(f.mtime for f in g.files)
        return [f"« {g.name} » — {len(g.files)} fichiers, {g.versions} versions",
                "", human_size(sum(f.size for f in g.files)), human_date(newest)]

    def _row(self, f) -> list[str]:
        label = f.version + (" (récente)" if f.version == "A" else "")
        return [f.path, label, human_size(f.size), human_date(f.mtime)]

    def _decorate(self, child: QTreeWidgetItem, f) -> None:
        if f.version == "A":
            child.setForeground(1, QBrush(QColor("#3fbf3f")))
        elif f.version == "?":
            child.setToolTip(1, "Contenu non vérifié (fichier en ligne, illisible ou modifié "
                                "depuis le scan) : même taille qu'une autre version.")

    def _ordered(self, g) -> list:
        return list(g.files)  # déjà triés du plus récent au plus ancien

    def _result_text(self, r) -> str:
        text = (f"{human_count(len(r.groups))} noms avec des versions différentes, "
                f"{human_count(sum(len(g.files) for g in r.groups))} fichiers. "
                f"{human_count(r.candidates)} fichiers homonymes examinés, "
                f"{human_size(r.hashed_bytes)} lus en {human_duration(r.duration)}.")
        if r.skipped_generic:
            text += f" {human_count(r.skipped_generic)} noms génériques ignorés."
        return text + " Même lettre = copies identiques ; vérifiez avant de supprimer une version."

    def export_table(self) -> tuple[list[str], list[list]] | None:
        if self._result is None:
            return None
        checked = {f.path for f in self.checked_files()}
        rows = [[g.name, f.version, f.path, f.size, iso(f.mtime), "oui" if f.path in checked else ""]
                for g in self._result.groups for f in g.files]
        return ["Nom", "Version", "Chemin", "Octets", "Modifié", "Coché"], rows
