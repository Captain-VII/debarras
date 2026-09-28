# PyInstaller — build « un dossier » (démarrage rapide, moins de faux positifs antivirus
# qu'un exe unique). Lancer : build.ps1   (ou : pyinstaller --noconfirm file_analyzer.spec)
# -*- mode: python ; coding: utf-8 -*-

import os
import re

APP_NAME = "Debarras"

# Métadonnées Windows de l'exe (Propriétés > Détails), générées depuis version.py.
VERSION = re.search(r'__version__ = "([^"]+)"', open("version.py", encoding="utf-8").read()).group(1)
_v = tuple(int(x) for x in VERSION.split(".")) + (0,) * (4 - len(VERSION.split(".")))
os.makedirs("build", exist_ok=True)
with open("build/version_info.txt", "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_v}, prodvers={_v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040C04B0', [
      StringStruct('FileDescription', 'Débarras — analyse et nettoyage de disque'),
      StringStruct('ProductName', 'Débarras'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('ProductVersion', '{VERSION}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x040C, 1200])]),
  ]
)
""")

# Modules Qt jamais utilisés : on les écarte pour alléger le livrable.
QT_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/icon.ico", "assets"), ("assets/icon.png", "assets"), ("assets/logo.png", "assets")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "test", *QT_EXCLUDES],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon="assets/icon.ico",
    console=False,          # application fenêtrée
    upx=False,              # UPX déclenche souvent les antivirus
    version="build/version_info.txt",
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
