"""Sorties de référence du cœur Python, pour vérifier le portage en Rust (phase 0 du plan).

Crée un faux PC déterministe (Windows, Program Files, profil, jeux, projets, doublons,
homonymes, images), le monte sur une lettre de lecteur (subst) pour tester les règles de
racine, puis enregistre en JSON ce que le cœur en tire : scan, classification de chaque
élément, synthèse de sécurité, doublons, même nom, images similaires, stats, recherche,
nettoyage. Les chemins sont écrits sans la lettre (« \\Users\\Alice\\… »).

    .venv\\Scripts\\python tools\\make_golden.py          -> tests\\golden\\*.json
    .venv\\Scripts\\python tools\\make_golden.py --check  -> compare sans écrire (code 1 si écart)
    .venv\\Scripts\\python tools\\make_golden.py --fixture  -> régénère aussi le faux disque

Le faux disque est enregistré dans tests\\golden\\fixture.zip (+ fixture.json) : le portage
Rust rejoue exactement les mêmes octets, images Pillow comprises. Tous les fichiers datent
du 2020-01-01 12:00 (heure locale), sauf ceux de « recent » (une heure avant le lancement).
"""
import dataclasses
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "tests" / "golden"
sys.path.insert(0, str(REPO))

OLD = time.mktime((2020, 1, 1, 12, 0, 0, 0, 0, -1))
NOW = time.time() - 3600  # récent, mais pas « en cours d'utilisation »
DROP = {"mtime", "atime", "duration", "thumb"}  # dépendent de l'instant ou de Pillow


def build(d: Path) -> list[str]:
    """Crée le faux disque ; renvoie les fichiers récents (chemins relatifs)."""
    rnd = random.Random(42)
    recent: list[str] = []

    def w(rel: str, content: bytes | int = 1000, mtime: float = OLD) -> bytes:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = rnd.randbytes(content) if isinstance(content, int) else content
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))
        if mtime != OLD:
            recent.append(rel)
        return data

    # Racine du lecteur et Windows
    for rel in ("pagefile.sys", "hiberfil.sys", r"System Volume Information\tracking.log",
                r"$Recycle.Bin\S-1-5-21\$R1.txt", r"Recovery\winre.wim", r"Windows\System32\kernel32.dll",
                r"Windows\notepad.exe", r"ProgramData\Vendor\settings.dat"):
        w(rel, 4000)
    # Logiciels installés
    w(r"Program Files\Studio\studio.exe", 9000)
    w(r"Program Files\Studio\bin\render.dll", 7000)
    w(r"Program Files\Common Files\shared.dll", 3000)
    w(r"Program Files (x86)\Vieux\vieux.exe", 5000)
    w(r"Program Files (x86)\Vieux\vieux.dll", 5000)
    # Jeux et logiciel portable
    w(r"Jeux\SteamLibrary\steamapps\common\Aventure\Aventure.exe", 8000)
    w(r"Jeux\SteamLibrary\steamapps\common\Aventure\Binaries\Win64\engine.dll", 8000)
    w(r"Jeux\SteamLibrary\steamapps\common\Aventure\data\pak00.pak", 20000)
    w(r"Portable\Outil\outil.exe", 6000)
    w(r"Portable\Outil\outil.dll", 6000)
    w(r"Portable\Outil\notes.txt", 500)
    # Profil
    u = r"Users\Alice"
    w(rf"{u}\NTUSER.DAT", 2000)
    w(r"Users\Default\NTUSER.DAT", 2000)
    w(rf"{u}\Documents\facture.pdf", 12000)
    w(rf"{u}\Documents\rapport.docx", 15000, NOW)
    w(rf"{u}\Documents\readme.txt", 3000)
    w(rf"{u}\Downloads\setup-jeu.exe", 30000)
    w(rf"{u}\Downloads\notice.pdf", 8000)
    w(rf"{u}\AppData\Local\Temp\vieux.tmp", 5000)
    w(rf"{u}\AppData\Local\Temp\install-1a2b\paquet.cab", 9000)
    w(rf"{u}\AppData\Local\Temp\recent.tmp", 1000, NOW)
    w(rf"{u}\AppData\Local\Google\Chrome\User Data\Default\Cache\Cache_Data\f_000001", 6000)
    w(rf"{u}\AppData\Local\Google\Chrome\User Data\Default\Code Cache\js\0001_0", 3000)
    w(rf"{u}\AppData\Local\Google\Chrome\User Data\Default\Bookmarks", 800)
    w(rf"{u}\AppData\Local\CrashDumps\appli.exe.1.dmp", 7000)
    w(rf"{u}\AppData\Local\Programs\Debarras\Debarras.exe", 9000)
    w(rf"{u}\AppData\Local\Programs\Editeur\editeur.exe", 6000)
    w(rf"{u}\AppData\Local\Programs\Editeur\lib\core.dll", 6000)
    w(rf"{u}\AppData\Roaming\Appli\config.ini", 400)
    # Projets
    w(r"Projets\site\src\page.tsx", 2000)
    w(r"Projets\site\node_modules\lib\index.js", 4000)
    w(r"Projets\site\.git\objects\pack.pack", 6000)
    w(r"Projets\outil\outil\__pycache__\mod.cpython-312.pyc", 1500)
    w(r"Projets\outil\.venv\Lib\dep.pyd", 5000)
    w(r"Projets\outil\.pytest_cache\v\cache.json", 200)
    w(r"Projets\outil\rapport.docx", 15000)
    # Doublons : 3 copies, une paire, et deux faux candidats (même début, fin différente)
    a = w(rf"{u}\Photos\vacances.mov", 50000)
    w(rf"{u}\Sauvegarde\vacances (copie).mov", a)
    w(r"Archives\2020\vacances.mov", a)
    b = w(rf"{u}\Documents\contrat.pdf", 20000)
    w(rf"{u}\Sauvegarde\contrat.pdf", b)
    head = rnd.randbytes(4096)
    w(rf"{u}\Musique\piste1.flac", head + rnd.randbytes(30000))
    w(rf"{u}\Musique\piste2.flac", head + rnd.randbytes(30000))
    # Même nom, contenu différent
    w(rf"{u}\Sauvegarde\rapport.docx", 14000)
    w(r"Archives\rapport.docx", 13000)
    w(rf"{u}\Sauvegarde\readme.txt", 2500)  # nom générique : écarté
    # Images similaires
    from PIL import Image, ImageDraw

    def picture(seed: int) -> Image.Image:
        r = random.Random(seed)
        img = Image.new("RGB", (320, 240))
        dr = ImageDraw.Draw(img)
        for y in range(240):
            dr.line([(0, y), (319, y)], fill=(y, 120, 255 - y))
        for _ in range(12):
            x, y = r.randrange(300), r.randrange(220)
            dr.ellipse([x, y, x + r.randrange(20, 90), y + r.randrange(20, 90)],
                       fill=(r.randrange(256), r.randrange(256), r.randrange(256)))
        return img

    def save(img: Image.Image, rel: str, fmt: str, **kw) -> None:
        buf = io.BytesIO()
        img.save(buf, fmt, **kw)
        w(rel, buf.getvalue())

    plage, foret = picture(1), picture(2)
    save(plage, rf"{u}\Images\plage.png", "PNG")
    save(plage, rf"{u}\Images\plage.jpg", "JPEG", quality=85)
    save(plage.resize((160, 120)), rf"{u}\Images\plage-petite.png", "PNG")
    save(foret, rf"{u}\Images\foret.png", "PNG")
    save(foret.transpose(Image.Transpose.FLIP_LEFT_RIGHT), rf"{u}\Images\foret-miroir.png", "PNG")
    return recent


def restore(d: Path) -> None:
    """Rejoue le faux disque enregistré, avec ses dates."""
    recent = set(json.loads((OUT / "fixture.json").read_text(encoding="utf-8"))["recent"])
    with zipfile.ZipFile(OUT / "fixture.zip") as z:
        z.extractall(d)  # archive générée par ce script, versionnée dans le repo
        for name in z.namelist():
            rel = name.replace("/", "\\")
            t = NOW if rel in recent else OLD
            os.utime(d / rel, (t, t))


def save_fixture(d: Path, recent: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT / "fixture.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(x for x in d.rglob("*") if x.is_file()):
            z.write(p, p.relative_to(d).as_posix())
    (OUT / "fixture.json").write_text(json.dumps({"old": "2020-01-01T12:00 heure locale", "recent": sorted(recent)},
                                                 ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def clean(obj, drive: str):
    """Dataclasses → dict, chemins sans lettre, champs dépendant de l'instant retirés."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: clean(getattr(obj, f.name), drive) for f in dataclasses.fields(obj) if f.name not in DROP}
    if isinstance(obj, dict):
        return {clean(k, drive): clean(v, drive) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(clean(x, drive) for x in obj)
    if isinstance(obj, (list, tuple)):
        return [clean(x, drive) for x in obj]
    if isinstance(obj, str) and obj[:2].lower() == drive.lower():
        return obj[2:] or "\\"
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


def collect(drive: str, db: Path) -> dict[str, object]:
    import sqlite3

    from core import safety
    from core.cache import Cache
    from core.cleanup import auto_selection, browser_targets, temp_target
    from core.duplicates import DuplicateFinder
    from core.samename import SameNameFinder
    from core.scanner import ScanOptions, Scanner
    from core.search import SearchQuery, search_files
    from core.similar import SimilarFinder
    from core.stats import compute_stats

    root = drive + "\\"
    out: list = []
    s = Scanner(root, db, ScanOptions(excluded_paths=[], excluded_names=[]))
    s.scan_finished.connect(out.append)
    s.scan_failed.connect(lambda e: sys.exit(f"scan impossible : {e}"))
    s.run()
    scan_id = out[0].scan_id
    res: dict[str, object] = {}

    conn = sqlite3.connect(db)
    dirs = conn.execute("SELECT path, size, file_count FROM dir_sizes WHERE scan_id=? ORDER BY path",
                        (scan_id,)).fetchall()
    files = conn.execute("SELECT path, size FROM file_paths ORDER BY path").fetchall()
    res["scan"] = {"result": out[0], "dirs": dirs, "files": files}

    roots = safety.software_dirs(conn, root)
    app_dir = drive + r"\Users\Alice\AppData\Local\Programs\Debarras"
    clf = safety.Classifier(roots, app_dir, safety.containers(conn, scan_id, roots, root))
    verdict = lambda p, is_dir: {"path": p, "dir": is_dir, **dataclasses.asdict(clf.classify(p, is_dir))}  # noqa: E731
    res["safety"] = {
        "software_dirs": roots,
        "containers": clf.containers,
        "items": [verdict(p, True) for p, _, _ in dirs] + [verdict(p, False) for p, _ in files],
        "summary": safety.summarize(conn, scan_id, root, clf),
    }
    res["stats"] = compute_stats(conn, scan_id, root)
    res["search"] = {name: search_files(conn, root, q) for name, q in (
        ("texte_rapport", SearchQuery(text="rapport")),
        ("motif_jpg", SearchQuery(text="*.jpg")),
        ("ext_pdf", SearchQuery(exts=[".pdf"])),
        ("taille_20k_40k", SearchQuery(min_size=20000, max_size=40000)),
        ("ancien_365j", SearchQuery(older_than_days=365)),
    )}
    conn.close()

    cache = Cache(db)
    try:
        res["duplicates"] = DuplicateFinder(db, root)._find(cache)
        res["samename"] = SameNameFinder(db, root)._find(cache)
        res["similar"] = SimilarFinder(db, root, min_size=1)._find(cache)
    finally:
        cache.close()

    targets = [temp_target(now=time.time()), *browser_targets(running=set())]
    res["cleanup"] = {"targets": targets, "auto": auto_selection(targets)}
    return {k: clean(v, drive) for k, v in res.items()}


def main() -> int:
    check = "--check" in sys.argv
    scratch = Path(tempfile.mkdtemp(prefix="debarras-golden-"))
    disk = scratch / "disque"
    disk.mkdir()
    letter = next(f"{c}:" for c in "XYWVUT" if not os.path.exists(f"{c}:\\"))
    subprocess.run(["subst", letter, str(disk)], check=True)
    try:
        u = letter + r"\Users\Alice"
        os.environ.update({
            "SystemRoot": letter + r"\Windows", "ProgramData": letter + r"\ProgramData",
            "ProgramFiles": letter + r"\Program Files", "ProgramW6432": letter + r"\Program Files",
            "ProgramFiles(x86)": letter + r"\Program Files (x86)", "USERPROFILE": u,
            "LOCALAPPDATA": u + r"\AppData\Local", "APPDATA": u + r"\AppData\Roaming",
            "TEMP": u + r"\AppData\Local\Temp", "TMP": u + r"\AppData\Local\Temp",
            "DEBARRAS_NO_UPDATE_CHECK": "1",
        })
        if "--fixture" in sys.argv or not (OUT / "fixture.zip").exists():
            save_fixture(disk, build(disk))
        else:
            restore(disk)
        res = collect(letter, scratch / "cache.db")
    finally:
        subprocess.run(["subst", letter, "/D"])
        shutil.rmtree(scratch, ignore_errors=True)  # uniquement des fichiers générés ici

    diffs = []
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in res.items():
        text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        path = OUT / f"{name}.json"
        if check:
            # Contenu JSON et non texte : un clone en CRLF (core.autocrlf) ne compte pas comme écart.
            if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != json.loads(text):
                diffs.append(name)
        else:
            path.write_text(text, encoding="utf-8")
    if check:
        print("écarts :", ", ".join(diffs) if diffs else "aucun")
        return 1 if diffs else 0
    print("écrit :", ", ".join(sorted(p.name for p in OUT.glob("*.json"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
