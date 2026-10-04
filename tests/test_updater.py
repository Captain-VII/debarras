"""Mise à jour : faux GitHub local (aucun accès réseau), vérifications et remplacement réel."""
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.updater import (
    Release, UpdateError, download, fetch_latest, is_newer, parse_version, stage, write_swap_script,
)


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def github():
    """Serveur HTTP local imitant l'API GitHub et le téléchargement des pièces jointes."""
    routes: dict[str, tuple[int, bytes]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, body = routes.get(self.path, (404, b"{}"))
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def publish(version: str, payload: bytes, digest: str | None = "auto", sidecar: bool = False,
                asset_name: str | None = None):
        name = asset_name or f"Debarras-{version}-win64.zip"
        sha = hashlib.sha256(payload).hexdigest()
        asset = {"name": name, "size": len(payload), "browser_download_url": f"{base}/dl/{name}"}
        if digest == "auto":
            asset["digest"] = f"sha256:{sha}"
        elif digest:
            asset["digest"] = digest
        assets = [asset]
        routes[f"/dl/{name}"] = (200, payload)
        if sidecar:
            assets.append({"name": name + ".sha256", "size": 64,
                           "browser_download_url": f"{base}/dl/{name}.sha256"})
            routes[f"/dl/{name}.sha256"] = (200, f"{sha}  {name}\n".encode())
        release = {"tag_name": f"v{version}", "body": "## Nouveautés\n- tout", "html_url": f"{base}/page",
                   "assets": assets}
        routes["/repos/me/app/releases/latest"] = (200, json.dumps(release).encode())

    yield base, publish
    server.shutdown()


def test_versions():
    assert parse_version("v1.10.2") == (1, 10, 2)
    assert parse_version("1.3") == parse_version("1.3.0")
    assert parse_version("v2.0.0-beta") == (2,)
    assert is_newer("1.10.0", "1.9.9") and is_newer("v1.3.1", "1.3.0")
    assert not is_newer("1.3.0", "1.3.0") and not is_newer("1.2.9", "1.3.0")


def test_fetch_latest(github):
    base, publish = github
    assert fetch_latest("me/app", base) is None               # aucune release
    publish("9.1.0", b"zip", asset_name="Debarras-9.1.0-source.tar.gz")
    assert fetch_latest("me/app", base) is None               # pas d'archive Windows
    publish("9.1.0", b"zip")
    rel = fetch_latest("me/app", base)
    assert (rel.version, rel.tag, rel.asset_size) == ("9.1.0", "v9.1.0", 3)
    assert rel.sha256 == hashlib.sha256(b"zip").hexdigest() and "Nouveautés" in rel.notes


def test_download_verifies_integrity(github, tmp_path):
    base, publish = github
    payload = make_zip({"Debarras/Debarras.exe": b"new"})
    publish("9.2.0", payload, digest=None, sidecar=True)      # empreinte via fichier .sha256
    rel = fetch_latest("me/app", base)
    assert rel.sha256 is None and rel.sha256_url
    path = download(rel, tmp_path / "dl")
    assert path.read_bytes() == payload

    publish("9.2.1", payload, digest="sha256:" + "0" * 64)   # empreinte fausse
    with pytest.raises(UpdateError, match="SHA-256 incorrecte"):
        download(fetch_latest("me/app", base), tmp_path / "dl2")

    publish("9.2.2", payload, digest=None)                    # aucune empreinte : refusé
    with pytest.raises(UpdateError, match="absente"):
        download(fetch_latest("me/app", base), tmp_path / "dl3")

    bad = Release("9.2.3", "v9.2.3", "", "", "x.zip", f"{base}/dl/Debarras-9.2.0-win64.zip",
                  len(payload) + 5, hashlib.sha256(payload).hexdigest())
    with pytest.raises(UpdateError, match="taille inattendue"):
        download(bad, tmp_path / "dl4")


def test_stage_accepts_expected_layouts_and_rejects_zip_slip(tmp_path):
    for i, files in enumerate(({"Debarras/Debarras.exe": b"x", "Debarras/_internal/a.dll": b"y"},
                               {"Debarras.exe": b"x", "_internal/a.dll": b"y"})):
        z = tmp_path / f"ok{i}.zip"
        z.write_bytes(make_zip(files))
        staged = stage(z, tmp_path / f"st{i}")
        assert (staged / "Debarras.exe").read_bytes() == b"x" and (staged / "_internal" / "a.dll").exists()
    evil = tmp_path / "evil.zip"
    evil.write_bytes(make_zip({"../../pwned.txt": b"!", "Debarras.exe": b"x"}))
    with pytest.raises(UpdateError, match="suspect"):
        stage(evil, tmp_path / "st_evil")
    assert not (tmp_path.parent / "pwned.txt").exists()
    empty = tmp_path / "empty.zip"
    empty.write_bytes(make_zip({"readme.txt": b"?"}))
    with pytest.raises(UpdateError, match="introuvable"):
        stage(empty, tmp_path / "st_empty")


@pytest.mark.skipif(sys.platform != "win32", reason="script PowerShell")
def test_swap_script_replaces_folder_and_relaunches(tmp_path):
    install = tmp_path / "Mon appli (test)"   # espaces et parenthèses dans le chemin
    install.mkdir()
    (install / "version.txt").write_text("old")
    (install / "unins000.exe").write_text("uninstaller")   # posé par l'installateur
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "version.txt").write_text("new")
    marker = tmp_path / "relaunched.txt"
    # L'« exe » relancé est ici un .cmd qui laisse une trace.
    (staged / "run.cmd").write_text(f'@echo relance> "{marker}"\n')
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()  # processus déjà terminé : le script n'attend pas
    started = tmp_path / "started.flag"
    script = write_swap_script(dead.pid, install, staged, "run.cmd", folder=tmp_path, started=started)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                   check=True, timeout=60)
    for _ in range(50):  # Start-Process est asynchrone
        if marker.exists():
            break

        time.sleep(0.1)
    assert (install / "version.txt").read_text() == "new"
    assert (install / "unins000.exe").read_text() == "uninstaller"   # désinstalleur conservé
    assert (tmp_path / "Mon appli (test).old" / "version.txt").read_text() == "old"
    assert not staged.exists() and marker.exists() and started.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="lancement Windows")
def test_launch_swap_from_windowless_parent_that_exits(tmp_path):
    """Régression : depuis l'exe sans console, DETACHED_PROCESS empêchait PowerShell de
    s'exécuter. On reproduit la situation avec pythonw (sans console) qui quitte aussitôt."""
    from core.updater import wait_started
    marker = tmp_path / "ran.flag"
    script = tmp_path / "probe.ps1"
    script.write_text(f"Start-Sleep -Milliseconds 800; New-Item -ItemType File -Path '{marker}' | Out-Null\n",
                      encoding="utf-8-sig")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from core.updater import launch_swap; launch_swap(Path(sys.argv[2]))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([pythonw, "-c", code, root, str(script)], check=True, timeout=30)  # parent terminé
    assert wait_started(marker, timeout=20), "le script lancé n'a pas tourné après la fin du parent"


def test_dialog_and_controller_without_network(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QWidget

    import ui.update_ui as uu

    rel = Release("9.9.9", "v9.9.9", "## Notes", "https://example.invalid", "Debarras-9.9.9-win64.zip",
                  "https://example.invalid/x.zip", 1234, "a" * 64)
    dlg = uu.UpdateDialog(rel, self_update=False)
    labels = [b.text() for b in dlg.findChildren(uu.QDialogButtonBox)[0].buttons()]
    assert "Ouvrir la page de téléchargement" in labels and "Installer et redémarrer" not in labels

    class FakeSettings:
        check_updates, last_update_check, skipped_version = True, 0.0, ""
        def save(self):
            pass

    win = QWidget()
    win.settings = FakeSettings()
    ctl = uu.UpdateController(win)
    monkeypatch.setattr(uu.UpdateDialog, "exec", lambda self: uu.SKIP)
    ctl._on_checked(rel, manual=False)
    assert win.settings.skipped_version == "9.9.9" and win.settings.last_update_check > 0
    shown = []
    monkeypatch.setattr(uu.UpdateDialog, "exec", lambda self: shown.append(1) or 0)
    ctl._on_checked(rel, manual=False)           # version ignorée : rien n'est proposé
    assert shown == []
    ctl._on_checked(rel, manual=True)            # vérification manuelle : proposée quand même
    assert shown == [1]
    infos = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a: infos.append(a[2])))
    ctl._on_checked(None, manual=True)
    assert "dernière version" in infos[0]
    monkeypatch.delenv("DEBARRAS_NO_UPDATE_CHECK", raising=False)
    called = []
    monkeypatch.setattr(ctl, "check", lambda manual=True: called.append(manual))
    ctl._auto_check()                            # vérifié il y a moins de 24 h : rien
    assert called == []
    win.settings.last_update_check = 0
    ctl._auto_check()
    assert called == [False]
    os.environ["DEBARRAS_NO_UPDATE_CHECK"] = "1"


@pytest.mark.skipif(sys.platform != "win32", reason="registre Windows")
def test_sync_uninstall_entry(tmp_path):
    import winreg
    from core.updater import sync_uninstall_entry
    key = r"Software\DebarrasTest\Uninstall_is1"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(tmp_path))
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, "1.0.0")
    try:
        assert not sync_uninstall_entry(tmp_path / "ailleurs", "2.0.0", key)   # autre installation
        assert sync_uninstall_entry(tmp_path, "2.0.0", key)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            assert winreg.QueryValueEx(k, "DisplayVersion")[0] == "2.0.0"
        assert not sync_uninstall_entry(tmp_path, "2.0.0", key)                # déjà à jour
        assert not sync_uninstall_entry(tmp_path, "2.0.0", key + "_absent")    # installé via ZIP
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\DebarrasTest")
