import os

import pytest

from core.duplicates import PARTIAL_SIZE, DuplicateFinder, DupResult


@pytest.fixture
def find(db, qapp):
    def _find(root, min_size: int = 1) -> DupResult:
        f = DuplicateFinder(db, str(root), min_size)
        out, failed = [], []
        f.result_ready.connect(out.append)
        f.failed.connect(failed.append)
        f.run()
        assert not failed, failed
        return out[0]
    return _find


def group_names(res: DupResult, root) -> list[list[str]]:
    return sorted(sorted(os.path.relpath(f.path, root) for f in g.files) for g in res.groups)


def test_detects_true_duplicates_only(root, write, scan, find):
    big = os.urandom(3 * PARTIAL_SIZE)
    write("a/film.bin", big)
    write("b/film copie.bin", big)
    write("a/fin_diff.bin", big[:-1] + bytes([big[-1] ^ 1]))   # même début, fin différente
    write("a/debut_diff.bin", bytes([big[0] ^ 1]) + big[1:])   # début différent
    small = os.urandom(100)
    write("a/petit.txt", small)
    write("b/petit.txt", small)
    write("z/unique.bin", 500)
    scan(root)
    res = find(root)
    assert group_names(res, root) == [["a\\film.bin", "b\\film copie.bin"],
                                      ["a\\petit.txt", "b\\petit.txt"]]
    assert res.wasted == len(big) + len(small)
    assert res.groups[0].size == len(big)  # trié par espace récupérable


def test_second_run_reads_nothing_thanks_to_cache(root, write, scan, find):
    data = os.urandom(50_000)
    write("a.bin", data)
    write("b.bin", data)
    scan(root)
    first = find(root)
    assert first.hashed_bytes > 0
    second = find(root)
    assert second.hashed_bytes == 0
    assert group_names(second, root) == group_names(first, root)


def test_hard_links_are_counted_once(root, write, scan, find):
    a = write("a.bin", 5000)
    os.link(a, root / "link.bin")
    scan(root)
    assert find(root).groups == []


def test_min_size_and_changed_files(root, write, scan, find):
    write("s1.txt", b"same")
    write("s2.txt", b"same")
    big = os.urandom(20_000)
    write("b1.bin", big)
    b2 = write("b2.bin", big)
    scan(root)
    assert group_names(find(root, min_size=1000), root) == [["b1.bin", "b2.bin"]]
    # Modifié depuis le scan : ignoré (et signalé) plutôt que comparé sur des données périmées.
    b2.write_bytes(os.urandom(20_000))
    res = find(root, min_size=1000)
    assert res.groups == [] and res.skipped_changed == 1


def test_cancel_returns_cancelled_result(root, write, scan, db, qapp):
    data = os.urandom(10_000)
    write("a.bin", data)
    write("b.bin", data)
    scan(root)
    f = DuplicateFinder(db, str(root), 1)
    out = []
    f.result_ready.connect(out.append)
    # requestInterruption() est ignoré hors d'un thread démarré : on simule la demande.
    f.isInterruptionRequested = lambda: True
    f.run()
    assert out[0].cancelled and out[0].groups == []
