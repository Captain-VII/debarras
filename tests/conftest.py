"""Fixtures communes : Qt hors écran, dossier de données isolé, arborescences de test."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DEBARRAS_NO_UPDATE_CHECK"] = "1"  # jamais d'appel réseau pendant les tests

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.cache import Cache  # noqa: E402
from core.scanner import ScanOptions, Scanner, ScanResult  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Cache, journal et paramètres dans un dossier temporaire : jamais ceux de l'utilisateur."""
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    return appdata


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "data"
    r.mkdir()
    return r


WriteFn = Callable[..., Path]


@pytest.fixture
def write(root: Path) -> WriteFn:
    """write('a/b.txt', b'contenu' | taille, age_days=0) -> chemin créé sous `root`."""
    def _write(rel: str, content: bytes | int = b"x", age_days: float = 0) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(content) if isinstance(content, int) else content)
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(p, (t, t))
        return p
    return _write


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


@pytest.fixture
def scan(db: Path) -> Callable[..., ScanResult]:
    """Scan synchrone (run() dans le thread courant : les signaux sont directs)."""
    def _scan(path: Path, options: ScanOptions | None = None) -> ScanResult:
        s = Scanner(str(path), db, options or ScanOptions(excluded_paths=[], excluded_names=[]))
        out: list[ScanResult] = []
        failed: list[str] = []
        s.scan_finished.connect(out.append)
        s.scan_failed.connect(failed.append)
        s.run()
        assert not failed, failed
        return out[0]
    return _scan


@pytest.fixture
def cache(db: Path):
    c = Cache(db)
    yield c
    c.close()
