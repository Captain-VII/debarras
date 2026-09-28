"""Point d'entrée de l'analyseur de fichiers."""
import os
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.settings import Settings, apply_theme


__version__ = "1.1.0"


def resource_path(rel: str) -> str:
    """Chemin d'une ressource, en développement comme dans l'exe PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def main() -> int:
    if sys.platform == "win32":
        # Icône propre dans la barre des tâches (sinon celle de python.exe en développement).
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Debarras.App")
    app = QApplication(sys.argv)
    app.setApplicationName("Débarras")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(resource_path(os.path.join("assets", "icon.ico"))))
    # Boutons et dialogues standard de Qt en français (Oui/Non, Annuler…).
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.Language.French), "qtbase", "_",
                       QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
    apply_theme(Settings.load().theme)
    window = MainWindow()
    window.show()
    # Dossier passé en argument (ou glissé sur l'exe) : scan immédiat.
    folder = next((a for a in sys.argv[1:] if os.path.isdir(a)), None)
    if folder:
        window.open_folder(folder)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
