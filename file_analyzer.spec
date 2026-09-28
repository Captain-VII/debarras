# PyInstaller — build « un dossier » (démarrage rapide, moins de faux positifs antivirus
# qu'un exe unique). Lancer : build.ps1   (ou : pyinstaller --noconfirm file_analyzer.spec)
# -*- mode: python ; coding: utf-8 -*-

APP_NAME = "AnalyseurFichiers"

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
    datas=[("assets/icon.ico", "assets"), ("assets/icon.png", "assets")],
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
    version="version_info.txt",
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
