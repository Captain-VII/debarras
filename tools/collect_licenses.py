"""Assemble les licences du livrable : dist/Debarras/LICENSE + dist/Debarras/licenses/.

Appelé par build.ps1 après PyInstaller. Versions et textes lus dans les paquets installés
(importlib.metadata) ; LGPL/GPL v3 versionnées dans licenses/ (la roue PySide6 ne les fournit pas).
Usage : python tools/collect_licenses.py dist/Debarras
"""
from __future__ import annotations

import shutil
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (paquet, nom affiché, licence, source, fichier de licence dans la distribution ou None)
COMPONENTS = [
    ("PySide6-Essentials", "Qt for Python (PySide6) et Qt 6", "LGPL-3.0 (bibliothèques dynamiques)",
     "https://code.qt.io/cgit/pyside/pyside-setup.git — https://download.qt.io/official_releases/QtForPython/",
     None),
    ("shiboken6", "Shiboken6", "LGPL-3.0", "https://code.qt.io/cgit/pyside/pyside-setup.git", None),
    ("pillow", "Pillow", "MIT-CMU (HPND)", "https://github.com/python-pillow/Pillow", "LICENSE"),
    ("xxhash", "python-xxhash (et xxHash)", "BSD-2-Clause", "https://github.com/ifduyue/python-xxhash", "LICENSE"),
    ("send2trash", "Send2Trash", "BSD-3-Clause", "https://github.com/arsenetar/send2trash", "LICENSE"),
    ("pyinstaller", "Chargeur PyInstaller (bootloader)",
     "GPL-2.0 avec exception autorisant la distribution de programmes construits",
     "https://github.com/pyinstaller/pyinstaller", "COPYING.txt"),
]

NOTICE_HEAD = """Débarras — licences des composants tiers
==========================================

Débarras est distribué sous licence MIT (voir ../LICENSE). L'exécutable embarque les
composants suivants, chacun sous sa propre licence ; les textes complets sont dans ce dossier.

"""

LGPL_NOTE = """
Qt / PySide6 (LGPL-3.0)
-----------------------
Les bibliothèques Qt et PySide6 sont utilisées sans modification et liées dynamiquement :
ce sont des fichiers séparés (Qt6*.dll, *.pyd) dans les dossiers _internal\\PySide6 et
_internal\\shiboken6 de l'application. Vous pouvez les remplacer par une version modifiée compatible de ces mêmes
bibliothèques. Leur code source est disponible aux adresses indiquées ci-dessus, et le code
source complet de Débarras sur https://github.com/Captain-VII/debarras.
Textes : LGPL-3.0.txt, complété par GPL-3.0.txt auquel la LGPL v3 fait référence.
Qt intègre lui-même des composants tiers : https://doc.qt.io/qt-6/licenses-used-in-qt.html

SQLite (via Python) est dans le domaine public : https://www.sqlite.org/copyright.html
"""


def _license_file(dist: metadata.Distribution, name: str) -> Path | None:
    for f in dist.files or []:
        if f.name == name and "licenses" in f.parts:
            return Path(dist.locate_file(f))
    return None


def collect(app_dir: Path) -> list[str]:
    out = app_dir / "licenses"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", app_dir / "LICENSE")
    for text in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        shutil.copy2(ROOT / "licenses" / text, out / text)
    lines = [NOTICE_HEAD]
    py = Path(sys.base_prefix) / "LICENSE.txt"
    if py.exists():
        shutil.copy2(py, out / "Python-PSF.txt")
    lines.append(f"- Python {sys.version.split()[0]} — PSF License — https://www.python.org — Python-PSF.txt\n")
    written = ["LICENSE", "LGPL-3.0.txt", "GPL-3.0.txt", "Python-PSF.txt"]
    for pkg, label, lic, src, filename in COMPONENTS:
        dist = metadata.distribution(pkg)
        target = ""
        if filename:
            found = _license_file(dist, filename)
            if found is None:
                raise SystemExit(f"licence introuvable pour {pkg} ({filename})")
            target = f"{pkg}-LICENSE.txt"
            shutil.copy2(found, out / target)
            written.append(target)
        else:
            target = "LGPL-3.0.txt"
        lines.append(f"- {label} {dist.version} — {lic} — {src} — {target}\n")
    lines.append(LGPL_NOTE)
    (out / "THIRD_PARTY_NOTICES.txt").write_text("".join(lines), encoding="utf-8-sig")
    return written + ["THIRD_PARTY_NOTICES.txt"]


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "Debarras")
    if not (target / "Debarras.exe").exists():
        raise SystemExit(f"Debarras.exe introuvable dans {target}")
    files = collect(target)
    print(f"licences copiées ({len(files)}) dans {target}")
