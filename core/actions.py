"""Actions sur les fichiers : corbeille, déplacement, archivage ZIP.

- Jamais de suppression définitive : tout ce qui disparaît passe par la corbeille.
- Chaque action est tracée dans un journal JSON (une ligne par action).
- Mode simulation : l'action est planifiée et journalisée, rien n'est touché.
- La dernière action réelle peut être annulée (restauration depuis la corbeille incluse).
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import struct
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal
from send2trash import send2trash

from core.cache import default_db_path

TRASH, MOVE, ARCHIVE = "trash", "move", "archive"
KIND_LABELS = {TRASH: "Mise à la corbeille", MOVE: "Déplacement", ARCHIVE: "Archivage ZIP"}

# Statuts d'un élément
PENDING, DONE, SIMULATED, SKIPPED, ERROR, RESTORED = (
    "en attente", "fait", "simulé", "ignoré", "erreur", "restauré")

Progress = Callable[[int, int, str], None]
Cancelled = Callable[[], bool]


def _protected_roots() -> list[str]:
    env = os.environ
    roots = [env.get("SystemRoot", r"C:\Windows"), env.get("ProgramFiles", r"C:\Program Files"),
             env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             env.get("ProgramData", r"C:\ProgramData")]
    return [os.path.normcase(os.path.normpath(r)) for r in roots]


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _is_under(path: str, parent: str) -> bool:
    p, q = os.path.normcase(path), os.path.normcase(parent).rstrip(os.sep)
    return p == q or p.startswith(q + os.sep)


def check_path(path: str) -> str | None:
    """Raison de refuser `path`, ou None s'il peut être traité."""
    if not os.path.lexists(path):
        return "introuvable"
    drive, rest = os.path.splitdrive(path)
    if rest.strip("\\/") == "":
        return "racine de lecteur"
    if os.path.normcase(path) == os.path.normcase(str(Path.home())):
        return "dossier utilisateur"
    for root in _protected_roots():
        if _is_under(path, root):
            return "dossier système protégé"
    if "$recycle.bin" in os.path.normcase(path):
        return "corbeille"
    return None


def protection(path: str, whitelist: list[str]) -> str | None:
    """Liste blanche : un chemin (protège tout ce qu'il contient) ou un motif sur le nom."""
    name = os.path.basename(path)
    for entry in whitelist:
        entry = entry.strip()
        if not entry:
            continue
        if os.sep in entry or "/" in entry or ":" in entry:
            protected = _norm(entry)
            if _is_under(path, protected):
                return "protégé (liste blanche)"
            if _is_under(protected, path):
                return "contient un élément protégé (liste blanche)"
        elif fnmatch.fnmatch(name.lower(), entry.lower()):
            return f"protégé (liste blanche : {entry})"
    return None


# --- journal ---------------------------------------------------------------------------

@dataclass
class ActionItem:
    src: str
    size: int = 0
    dst: str | None = None
    status: str = PENDING
    error: str = ""


@dataclass
class ActionRecord:
    id: str
    kind: str
    created: float
    simulated: bool
    items: list[ActionItem]
    target: str = ""               # dossier de destination ou fichier ZIP
    trash_originals: bool = True   # archivage : originaux à la corbeille après vérification
    undone: bool = False
    undone_at: float | None = None
    note: str = ""                 # message global (archive invalide, annulation…)

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def count(self, *statuses: str) -> int:
        return sum(1 for i in self.items if i.status in statuses)

    def size(self, *statuses: str) -> int:
        return sum(i.size for i in self.items if i.status in statuses)

    @property
    def undoable(self) -> bool:
        return not self.simulated and not self.undone and self.count(DONE) > 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> ActionRecord:
        d = dict(d)
        d["items"] = [ActionItem(**i) for i in d.get("items", [])]
        return cls(**d)


class ActionLog:
    """Journal JSON Lines : une action par ligne, réécrit atomiquement à chaque mise à jour."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path().parent / "actions.jsonl"

    def load(self) -> list[ActionRecord]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(ActionRecord.from_dict(json.loads(line)))
            except (ValueError, TypeError):
                continue  # ligne abîmée : ignorée plutôt que de tout perdre
        return out

    def save(self, rec: ActionRecord) -> None:
        records = [r for r in self.load() if r.id != rec.id] + [rec]
        records.sort(key=lambda r: r.created)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(r.to_json() + "\n" for r in records), encoding="utf-8")
        os.replace(tmp, self.path)

    def last_undoable(self) -> ActionRecord | None:
        real = [r for r in self.load() if not r.simulated]
        return real[-1] if real and real[-1].undoable else None


# --- planification ---------------------------------------------------------------------

def _unique(path: str, reserved: set[str]) -> str:
    """'x.txt' -> 'x (2).txt' tant que le nom est pris (disque ou déjà réservé)."""
    base, ext = os.path.splitext(path)
    candidate, n = path, 2
    while os.path.lexists(candidate) or os.path.normcase(candidate) in reserved:
        candidate = f"{base} ({n}){ext}"
        n += 1
    reserved.add(os.path.normcase(candidate))
    return candidate


def plan(kind: str, paths: list[str], sizes: dict[str, int] | None = None, target: str = "",
         trash_originals: bool = True, simulated: bool = False,
         whitelist: list[str] | None = None) -> ActionRecord:
    """Prépare une action : normalise, dédoublonne, retire les éléments inclus dans un autre
    élément sélectionné, vérifie chaque chemin et calcule les destinations."""
    sizes = {os.path.normcase(_norm(k)): v for k, v in (sizes or {}).items()}
    norm = sorted({_norm(p) for p in paths}, key=lambda p: (len(p), p))
    refused = {p: r for p in norm if (r := check_path(p) or protection(p, whitelist or []))}
    kept: list[str] = []
    for p in norm:
        # Un chemin refusé n'absorbe pas ses sous-éléments : ils sont évalués pour eux-mêmes.
        if p in refused or not any(_is_under(p, k) for k in kept if k not in refused):
            kept.append(p)
    target = _norm(target) if target else ""
    rec = ActionRecord(uuid.uuid4().hex, kind, time.time(), simulated, [], target, trash_originals)
    reserved: set[str] = set()
    for p in kept:
        item = ActionItem(p, sizes.get(os.path.normcase(p), 0))
        reason = refused.get(p)
        if kind == MOVE and not reason:
            if _is_under(target, p):
                reason = "destination incluse dans l'élément"
            elif os.path.normcase(os.path.dirname(p)) == os.path.normcase(target):
                reason = "déjà dans la destination"
            else:
                item.dst = _unique(os.path.join(target, os.path.basename(p)), reserved)
        if kind == ARCHIVE and not reason and _is_under(target, p):
            reason = "l'archive serait créée dans l'élément"
        if reason:
            item.status, item.error = SKIPPED, reason
        rec.items.append(item)
    if kind == ARCHIVE and target:
        rec.target = _unique(target, set()) if os.path.lexists(target) else target
    return rec


# --- exécution -------------------------------------------------------------------------

def execute(rec: ActionRecord, progress: Progress, cancelled: Cancelled) -> None:
    todo = [i for i in rec.items if i.status == PENDING]
    if rec.simulated:
        for i in todo:
            i.status = SIMULATED
        return
    if rec.kind == ARCHIVE:
        _archive(rec, todo, progress, cancelled)
        return
    if rec.kind == MOVE:
        os.makedirs(rec.target, exist_ok=True)
    for n, item in enumerate(todo):
        if cancelled():
            rec.note = "Interrompue par l'utilisateur."
            break
        progress(n, len(todo), item.src)
        try:
            if rec.kind == TRASH:
                send2trash(item.src)
            elif rec.kind == MOVE:
                if os.path.lexists(item.dst):  # apparu entre la planification et l'exécution
                    item.dst = _unique(item.dst, set())
                shutil.move(item.src, item.dst)
            item.status = DONE
        except Exception as exc:  # noqa: BLE001 - on continue avec les suivants
            item.status, item.error = ERROR, str(exc)
    progress(len(todo), len(todo), "")


def _archive(rec: ActionRecord, todo: list[ActionItem], progress: Progress, cancelled: Cancelled) -> None:
    """Crée le ZIP dans un fichier .part, le vérifie, puis (option) met les originaux à la corbeille."""
    base = os.path.commonpath([os.path.dirname(i.src) for i in todo]) if todo else ""
    entries: list[tuple[str, str]] = []  # (chemin disque, nom dans l'archive)
    for item in todo:
        if os.path.isdir(item.src):
            for dirpath, _, files in os.walk(item.src):
                for f in files:
                    full = os.path.join(dirpath, f)
                    entries.append((full, os.path.relpath(full, base)))
        else:
            entries.append((item.src, os.path.relpath(item.src, base)))

    part = rec.target + ".part"
    os.makedirs(os.path.dirname(rec.target), exist_ok=True)
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as z:
            for n, (full, arc) in enumerate(entries):
                if cancelled():
                    raise InterruptedError
                progress(n, len(entries), full)
                z.write(full, arc)
        progress(len(entries), len(entries), "Vérification de l'archive…")
        with zipfile.ZipFile(part) as z:
            if z.testzip() is not None or len(z.infolist()) != len(entries):
                raise zipfile.BadZipFile("archive incohérente")
        os.replace(part, rec.target)
    except InterruptedError:
        _drop_partial(part)
        rec.note = "Interrompue : aucune archive créée, originaux intacts."
        return
    except Exception as exc:  # noqa: BLE001
        _drop_partial(part)
        rec.note = f"Archive non créée, originaux intacts : {exc}"
        for i in todo:
            i.status, i.error = ERROR, "archivage échoué"
        return

    for item in todo:
        item.dst = rec.target
        if not rec.trash_originals:
            item.status = DONE
            continue
        try:
            send2trash(item.src)
            item.status = DONE
        except Exception as exc:  # noqa: BLE001
            item.status, item.error = ERROR, f"archivé mais pas mis à la corbeille : {exc}"


def _drop_partial(part: str) -> None:
    """Retire l'archive incomplète que nous venons de créer (jamais un fichier utilisateur)."""
    try:
        os.remove(part)
    except OSError:
        pass


# --- annulation ------------------------------------------------------------------------

def _recycle_entries(drive: str):
    """(horodatage, chemin d'origine, fichier $I, fichier $R) de la corbeille de ce lecteur."""
    bin_dir = drive + os.sep + "$Recycle.Bin"
    try:
        sids = [e.path for e in os.scandir(bin_dir) if e.is_dir()]
    except OSError:
        return
    for sid in sids:
        try:
            infos = [e for e in os.scandir(sid) if e.name.upper().startswith("$I")]
        except OSError:
            continue  # corbeille d'un autre utilisateur
        for info in infos:
            try:
                data = Path(info.path).read_bytes()
                version, _, filetime = struct.unpack_from("<qqq", data)
                if version == 2:
                    n = struct.unpack_from("<i", data, 24)[0]
                    original = data[28:28 + 2 * n].decode("utf-16-le")
                else:
                    original = data[24:24 + 520].decode("utf-16-le")
                original = original.split("\0")[0]
            except (OSError, struct.error, UnicodeDecodeError):
                continue
            ts = filetime / 1e7 - 11644473600
            yield ts, original, info.path, os.path.join(sid, "$R" + info.name[2:])


def restore_from_trash(original: str, since: float) -> None:
    """Remet `original` à sa place depuis la corbeille (élément supprimé après `since`)."""
    drive = os.path.splitdrive(original)[0]
    matches = [e for e in _recycle_entries(drive)
               if os.path.normcase(e[1]) == os.path.normcase(original) and e[0] >= since]
    if not matches:
        raise FileNotFoundError("introuvable dans la corbeille (vidée ?)")
    _, _, info, data = max(matches)
    if os.path.lexists(original):
        raise FileExistsError("un élément du même nom existe déjà à l'emplacement d'origine")
    os.makedirs(os.path.dirname(original), exist_ok=True)
    os.replace(data, original)
    os.remove(info)  # métadonnées de corbeille de l'élément restauré


def undo(rec: ActionRecord, progress: Progress) -> None:
    since = rec.created - 5
    done = [i for i in rec.items if i.status == DONE]
    for n, item in enumerate(done):
        progress(n, len(done), item.src)
        try:
            if rec.kind == TRASH or (rec.kind == ARCHIVE and rec.trash_originals):
                restore_from_trash(item.src, since)
            elif rec.kind == MOVE:
                if os.path.lexists(item.src):
                    raise FileExistsError("un élément du même nom existe déjà à l'emplacement d'origine")
                os.makedirs(os.path.dirname(item.src), exist_ok=True)
                shutil.move(item.dst, item.src)
            item.status = RESTORED
        except Exception as exc:  # noqa: BLE001
            item.error = f"annulation impossible : {exc}"
    if rec.kind == ARCHIVE and os.path.exists(rec.target):
        try:
            send2trash(rec.target)  # l'archive part à la corbeille, pas supprimée
            rec.note = "Archive envoyée à la corbeille."
        except Exception as exc:  # noqa: BLE001
            rec.note = f"Archive conservée : {exc}"
    rec.undone, rec.undone_at = True, time.time()
    progress(len(done), len(done), "")


class ActionWorker(QThread):
    """Exécute (ou annule) une action hors du thread UI et met le journal à jour."""

    progress = Signal(int, int, str)
    completed = Signal(object)  # ActionRecord

    def __init__(self, rec: ActionRecord, log: ActionLog, undo: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.rec, self.log, self.undo = rec, log, undo

    def run(self) -> None:
        try:
            if self.undo:
                undo(self.rec, self.progress.emit)
            else:
                self.log.save(self.rec)  # trace même en cas de plantage
                execute(self.rec, self.progress.emit, self.isInterruptionRequested)
        except Exception as exc:  # noqa: BLE001
            self.rec.note = f"Erreur : {exc!r}"
        finally:
            try:
                self.log.save(self.rec)
            except OSError as exc:
                self.rec.note += f" (journal non écrit : {exc})"
            self.completed.emit(self.rec)
