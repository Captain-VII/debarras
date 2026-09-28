import csv

from utils.export import iso, write_csv
from utils.filetypes import CATEGORIES, OTHER, category, category_colors, extensions
from utils.format import human_count, human_duration, human_size


def test_human_size():
    assert human_size(0) == "0 o"
    assert human_size(1023) == "1023 o"
    assert human_size(1536) == "1,5 Ko"
    assert human_size(176.6 * 1024 ** 3) == "176,6 Go"
    assert human_size(5 * 1024 ** 6) == "5120,0 Po"  # plafonné à la dernière unité


def test_human_count_keeps_decimal_comma_intact():
    assert human_count(124190) == "124\u202f190"
    assert "," not in human_count(1_000_000)


def test_human_duration():
    assert human_duration(5) == "5 s"
    assert human_duration(75) == "1 min 15 s"
    assert human_duration(3725) == "1 h 02 min"


def test_category_is_case_insensitive_and_defaults_to_other():
    assert category(".JPG") == "Images"
    assert category(".mkv") == "Vidéos"
    assert category(".inconnu") == OTHER
    assert category("") == OTHER


def test_extensions_of_other_lists_known_ones():
    assert ".pdf" in extensions("Documents")
    known = extensions(OTHER)
    assert ".pdf" in known and ".mkv" in known


def test_category_colors_cover_every_category_in_both_modes():
    for dark in (False, True):
        colors = category_colors(dark)
        assert list(colors) == list(CATEGORIES)
        assert len(set(colors.values())) == len(CATEGORIES)


def test_write_csv_is_excel_friendly(tmp_path):
    path = tmp_path / "out.csv"
    n = write_csv(str(path), ["Chemin", "Octets"], [["C:\\é;x", 12], ["b", 3]])
    assert n == 2
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM UTF-8
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert rows == [["Chemin", "Octets"], ["C:\\é;x", "12"], ["b", "3"]]


def test_iso():
    assert iso(None) == ""
    assert len(iso(0.0 + 86400 * 365)) == 19
