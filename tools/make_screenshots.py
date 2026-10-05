"""Captures d'écran du README sur un disque de démonstration fictif (aucune donnée réelle).

Fichiers « creux » (sparse) : tailles réalistes, quasi aucune place réelle sur le disque.
Le dossier de démo est monté sur une lettre de lecteur (subst) pour des chemins courts ;
cache, paramètres, Temp et caches « navigateurs » sont factices eux aussi.

    .venv\\Scripts\\python tools\\make_screenshots.py      -> docs\\screenshots\\*.png
"""
import ctypes
import msvcrt
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshots"
SCRATCH = Path(tempfile.mkdtemp(prefix="debarras-demo-"))
DEMO, APPDATA, TEMP = SCRATCH / "disque", SCRATCH / "appdata", SCRATCH / "temp"
os.environ.update(LOCALAPPDATA=str(APPDATA), TEMP=str(TEMP), TMP=str(TEMP), DEBARRAS_NO_UPDATE_CHECK="1")
sys.path.insert(0, str(REPO))

GB, MB = 1024 ** 3, 1024 ** 2
OLD = time.time() - 40 * 86400
random.seed(7)
_k32 = ctypes.windll.kernel32


def sparse(path: Path, size: int, mtime: float = OLD) -> None:
    """Fichier creux de `size` octets. Pas de f.truncate() : sous Windows il écrit des zéros."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        h = wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno()))
        ret = wintypes.DWORD()
        ok = (_k32.DeviceIoControl(h, 0x900C4, None, 0, None, 0, ctypes.byref(ret), None)  # FSCTL_SET_SPARSE
              and _k32.SetFilePointerEx(h, ctypes.c_longlong(size), None, 0) and _k32.SetEndOfFile(h))
        if not ok:
            raise OSError(f"fichier creux impossible : {path}")
    os.utime(path, (mtime, mtime))


def many(folder: Path, pattern: str, n: int, lo: int, hi: int, mtime: float = OLD) -> None:
    for i in range(n):
        sparse(folder / pattern.format(i=i + 1), random.randint(lo, hi), mtime)


def build() -> None:
    d = DEMO
    sparse(d / "pagefile.sys", 16 * GB)
    game = d / "Jeux" / "SteamLibrary" / "steamapps" / "common"
    for name, paks in (("Aventure Céleste", (38, 22, 9)), ("Rallye Extrême", (14, 6)), ("Cité Pixel", (4, 2))):
        g = game / name
        sparse(g / f"{name.split()[0]}.exe", 90 * MB)
        sparse(g / "bin" / "engine.dll", 40 * MB)
        sparse(g / "bin" / "physics.dll", 12 * MB)
        for i, s in enumerate(paks):
            sparse(g / "data" / f"pak{i:02d}.pak", s * GB)
        many(g / "shaders", "shader_{i:03d}.bin", 40, 1 * MB, 20 * MB)
    ed = d / "Programmes" / "Studio Photo"
    sparse(ed / "studio.exe", 120 * MB)
    many(ed, "module{i}.dll", 12, 5 * MB, 60 * MB)
    many(ed / "ressources", "pack{i}.res", 8, 100 * MB, 400 * MB)

    u = d / "Utilisateur"
    many(u / "Documents" / "Factures", "facture-2025-{i:02d}.pdf", 24, 200_000, 2 * MB)
    many(u / "Documents" / "Projets", "rapport-{i}.docx", 15, 1 * MB, 30 * MB)
    many(u / "Documents", "présentation-{i}.pptx", 6, 20 * MB, 180 * MB)
    for year, n in (("2023", 400), ("2024", 650), ("2025", 500)):
        many(u / "Photos" / year, "IMG_{i:04d}.jpg", n, 3 * MB, 9 * MB)
    many(u / "Photos" / "2024" / "Mariage RAW", "DSC_{i:04d}.nef", 300, 25 * MB, 30 * MB)
    many(u / "Vidéos" / "Vacances", "clip-{i:02d}.mp4", 30, 300 * MB, 1500 * MB)
    many(u / "Vidéos" / "Films", "film-{i:02d}.mkv", 8, 3 * GB, 7 * GB)
    many(u / "Musique", "piste-{i:03d}.flac", 300, 20 * MB, 45 * MB)
    many(u / "Téléchargements", "installeur-{i}.exe", 6, 50 * MB, 900 * MB)
    many(u / "Téléchargements", "archive-{i}.zip", 5, 100 * MB, 2 * GB)

    p = d / "Projets"
    many(p / "site-web" / "node_modules" / "paquets", "lib{i:03d}.js", 300, 1 * MB, 8 * MB)
    many(p / "site-web" / ".git" / "objects", "pack-{i}.pack", 5, 50 * MB, 120 * MB)
    many(p / "site-web" / "src", "page{i}.tsx", 40, 5_000, 80_000)
    many(p / "outil-python" / ".venv" / "Lib", "dep{i}.pyd", 30, 2 * MB, 30 * MB)
    many(p / "outil-python" / "outil" / "__pycache__", "mod{i}.pyc", 30, 20_000, 400_000)
    many(p / "outil-python" / ".pytest_cache", "v{i}", 5, 1_000, 50_000)

    # Caches et temporaires (onglet Nettoyage)
    many(TEMP / "installation-1a2b", "fichier{i}.tmp", 20, 10 * MB, 60 * MB)
    many(TEMP / "maj-pilote", "paquet{i}.cab", 6, 80 * MB, 200 * MB)
    many(TEMP, "log{i}.tmp", 10, 100_000, 3 * MB)
    for prof in ("Default", "Profile 1"):
        chrome = APPDATA / "Google" / "Chrome" / "User Data" / prof
        many(chrome / "Cache" / "Cache_Data", "f_{i:06x}", 120, 1 * MB, 6 * MB)
        many(chrome / "Code Cache" / "js", "{i:016x}_0", 60, 200_000, 2 * MB)
    many(APPDATA / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache" / "Cache_Data", "f_{i:06x}", 80,
         1 * MB, 4 * MB)
    many(APPDATA / "CrashDumps", "Aventure.exe.{i}.dmp", 4, 150 * MB, 400 * MB)
    many(APPDATA / "NVIDIA" / "DXCache", "{i:016x}.nvph", 60, 20 * MB, 120 * MB)


def grow() -> None:
    """Ce qui change entre deux scans (coloriage Évolution)."""
    now = time.time()
    g = DEMO / "Jeux" / "SteamLibrary" / "steamapps" / "common" / "Aventure Céleste" / "data"
    sparse(g / "pak03_extension.pak", 24 * GB, now)
    many(DEMO / "Utilisateur" / "Vidéos" / "Vacances 2025", "clip-{i:02d}.mp4", 25, 400 * MB, 1800 * MB, now)
    many(DEMO / "Utilisateur" / "Téléchargements", "nouveau-{i}.zip", 3, 1 * GB, 3 * GB, now)


def main(drive: str) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from core.actions import TRASH
    from ui.actions_ui import ConfirmDialog
    from ui.main_window import MainWindow
    from ui.settings import apply_theme
    from ui.treemap import GROWTH, SAFETY
    from version import __version__

    app = QApplication([])
    app.setApplicationVersion(__version__)

    # Disques affichés : fictifs (le disque de démo et un second), jamais ceux du PC.
    import ui.main_window as mw
    from core.drives import Drive
    def demo_drive() -> Drive:  # recalculé à chaque appel : le disque de démo grossit entre deux scans
        used = sum(f.stat().st_size for f in DEMO.rglob("*") if f.is_file())
        return Drive(drive + "\\", "Données", "Disque local", 512 * GB, 512 * GB - used - 41 * GB)

    system = Drive("C:\\", "Windows", "Disque local", 256 * GB, 88 * GB)
    mw.list_drives = lambda: [system, demo_drive()]
    mw.drive_of = lambda path: (demo_drive() if os.path.normcase(path.rstrip("\\")) == os.path.normcase(drive)
                                else None)
    mw.is_admin = lambda: True

    def pump(sec: float) -> None:
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    def scan(w: MainWindow) -> None:
        w.open_folder(drive + "\\")
        while w.scanner is not None:
            pump(0.1)
        for _ in range(300):
            if w._safety_worker is None:
                break
            pump(0.1)
        pump(1.5)

    apply_theme("dark")
    w = MainWindow()
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    w.resize(1440, 880)
    w.show()
    scan(w)
    grow()
    scan(w)
    OUT.mkdir(parents=True, exist_ok=True)

    game = drive + r"\Jeux\SteamLibrary\steamapps\common\Aventure Céleste"
    w.home._reveal(game)
    w.treemap.canvas.navigate(drive + "\\", select=game)
    pump(0.8)
    w.grab().save(str(OUT / "accueil.png"))

    w.treemap.set_mode(GROWTH)
    w.home.details.show_welcome()
    pump(0.8)
    w.grab().save(str(OUT / "evolution.png"))
    w.treemap.set_mode(SAFETY)

    w.tabs.setCurrentWidget(w.cleanup_view)
    for _ in range(300):
        pump(0.1)
        if w.cleanup_view._worker is None:
            break
    pump(0.8)
    w.grab().save(str(OUT / "nettoyage.png"))

    dlg = ConfirmDialog(TRASH, [game], {game: w.cache.size_of(w.current_scan.id, game)}, False, w)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    dlg.resize(720, 430)
    dlg.show()
    pump(0.5)
    dlg.grab().save(str(OUT / "confirmation.png"))
    w.close()


if __name__ == "__main__":
    letter = next(f"{c}:" for c in "XYWVUT" if not os.path.exists(f"{c}:\\"))
    DEMO.mkdir()
    subprocess.run(["subst", letter, str(DEMO)], check=True)
    try:
        build()
        main(letter)
    finally:
        subprocess.run(["subst", letter, "/D"])
        shutil.rmtree(SCRATCH, ignore_errors=True)  # disque de démo : uniquement des fichiers générés ici
    print("captures :", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))
