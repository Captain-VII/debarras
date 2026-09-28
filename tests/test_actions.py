"""Actions sur fichiers. Les tests « corbeille » utilisent la vraie corbeille Windows
et restaurent tout ce qu'ils y envoient."""
import os
import zipfile

import pytest

from core.actions import (
    ARCHIVE, DONE, ERROR, MOVE, PENDING, RESTORED, SIMULATED, SKIPPED, TRASH, ActionLog,
    check_path, execute, plan, protection, undo,
)

nop = lambda *a: None  # noqa: E731
never = lambda: False  # noqa: E731


def files(root) -> list[str]:
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


def run(rec):
    execute(rec, nop, never)
    return rec


# --- garde-fous ----------------------------------------------------------------------

def test_check_path_refusals(root, write):
    f = write("ok.txt")
    assert check_path(str(f)) is None
    assert check_path(str(root / "absent")) == "introuvable"
    assert check_path("C:\\") == "racine de lecteur"
    assert check_path(os.path.expanduser("~")) == "dossier utilisateur"
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    assert check_path(os.path.join(windir, "notepad.exe")) == "dossier système protégé"


def test_whitelist_protection(root):
    wl = [str(root / "keep"), "*.KDBX"]
    assert protection(str(root / "keep" / "a.txt"), wl)
    assert protection(str(root / "keep"), wl)
    assert "contient" in protection(str(root), wl)
    assert protection(str(root / "x" / "vault.kdbx"), wl)
    assert protection(str(root / "x" / "a.txt"), wl) is None


def test_plan_dedupes_nested_and_refused_paths_do_not_absorb(root, write):
    a = write("src/a.txt")
    write("src/sub/b.txt")
    rec = plan(TRASH, ["C:\\", str(a), str(root / "src"), str(root / "src" / "sub"), str(root / "nope")])
    got = {os.path.basename(i.src) or i.src: (i.status, i.error) for i in rec.items}
    assert got == {"C:\\": (SKIPPED, "racine de lecteur"), "src": (PENDING, ""),
                   "nope": (SKIPPED, "introuvable")}


def test_plan_applies_whitelist(root, write):
    a = write("keep/a.txt")
    b = write("b.txt")
    rec = plan(TRASH, [str(a), str(b)], whitelist=[str(root / "keep")])
    assert [(os.path.basename(i.src), i.status) for i in rec.items] == [("b.txt", PENDING), ("a.txt", SKIPPED)]


# --- simulation, déplacement, journal -----------------------------------------------------

def test_simulation_touches_nothing(root, write):
    a = write("a.txt")
    rec = run(plan(TRASH, [str(a)], simulated=True))
    assert rec.items[0].status == SIMULATED and a.exists()
    assert not rec.undoable


def test_move_renames_on_conflict_and_undo_restores(root, write):
    a = write("src/a.txt", b"mine")
    write("dest/a.txt", b"other")
    b = write("src/b.txt")
    rec = run(plan(MOVE, [str(a), str(b)], target=str(root / "dest")))
    assert [i.status for i in rec.items] == [DONE, DONE]
    assert files(root) == ["dest\\a (2).txt", "dest\\a.txt", "dest\\b.txt"]
    undo(rec, nop)
    assert rec.undone and [i.status for i in rec.items] == [RESTORED, RESTORED]
    assert files(root) == ["dest\\a.txt", "src\\a.txt", "src\\b.txt"]
    assert (root / "src" / "a.txt").read_bytes() == b"mine"


def test_move_refuses_into_itself(root, write):
    write("src/a.txt")
    rec = plan(MOVE, [str(root / "src")], target=str(root / "src" / "inner"))
    assert rec.items[0].status == SKIPPED


def test_undo_never_overwrites(root, write):
    a = write("src/a.txt", b"v1")
    rec = run(plan(MOVE, [str(a)], target=str(root / "dest")))
    write("src/a.txt", b"nouveau")  # un fichier a repris la place
    undo(rec, nop)
    assert (root / "src" / "a.txt").read_bytes() == b"nouveau"
    assert (root / "dest" / "a.txt").exists()
    assert "annulation impossible" in rec.items[0].error


def test_log_roundtrip_and_last_undoable(tmp_path, root, write):
    log = ActionLog(tmp_path / "log.jsonl")
    sim = plan(TRASH, [str(write("a.txt"))], simulated=True)
    log.save(run(sim))
    assert log.last_undoable() is None  # une simulation ne s'annule pas
    real = run(plan(MOVE, [str(write("b.txt"))], target=str(root / "dest")))
    log.save(real)
    (tmp_path / "log.jsonl").open("a", encoding="utf-8").write("ligne abîmée\n")
    loaded = log.load()
    assert [r.id for r in loaded] == [sim.id, real.id]
    assert log.last_undoable().id == real.id
    undo(real, nop)
    log.save(real)
    assert log.last_undoable() is None


def test_archive_without_trash_keeps_originals(root, write):
    a = write("a.txt", b"hello")
    write("d/b.txt", b"world")
    z = root / "out" / "arch.zip"
    rec = run(plan(ARCHIVE, [str(a), str(root / "d")], target=str(z), trash_originals=False))
    assert [i.status for i in rec.items] == [DONE, DONE]
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "d/b.txt"]
        assert zf.read("d/b.txt") == b"world"
    assert a.exists() and not (root / "out" / "arch.zip.part").exists()


def test_archive_failure_leaves_originals(root, write, monkeypatch):
    a = write("a.txt")
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "a.txt")  # archive « corrompue »
    rec = run(plan(ARCHIVE, [str(a)], target=str(root / "x.zip")))
    assert rec.items[0].status == ERROR and a.exists()
    assert not (root / "x.zip").exists() and not (root / "x.zip.part").exists()


# --- corbeille réelle ----------------------------------------------------------------------

@pytest.mark.recycle_bin
def test_trash_and_undo_via_recycle_bin(root, write):
    a = write("a é.txt", b"contenu")
    write("d/b.txt", b"b")
    rec = run(plan(TRASH, [str(a), str(root / "d")]))
    assert [i.status for i in rec.items] == [DONE, DONE]
    assert files(root) == []
    undo(rec, nop)
    assert [i.status for i in rec.items] == [RESTORED, RESTORED]
    assert files(root) == ["a é.txt", "d\\b.txt"]
    assert a.read_bytes() == b"contenu"


@pytest.mark.recycle_bin
def test_archive_trash_originals_and_undo(root, write):
    a = write("a.txt", b"hello")
    z = root / "arch.zip"
    rec = run(plan(ARCHIVE, [str(a)], target=str(z)))
    assert rec.items[0].status == DONE and not a.exists() and z.exists()
    undo(rec, nop)
    assert a.read_bytes() == b"hello"
    assert not z.exists()  # l'archive part à la corbeille
    # Nettoyage : l'archive envoyée à la corbeille est restaurée puis retirée du dossier de test.
    from core.actions import restore_from_trash
    restore_from_trash(str(z), rec.created - 5)
    assert z.exists()
