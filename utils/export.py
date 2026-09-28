"""Exports : CSV (compatible Excel FR) et rapport HTML autonome."""
from __future__ import annotations

import csv
import html
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from utils.filetypes import CATEGORIES, category_colors
from utils.format import human_count, human_date, human_size

if TYPE_CHECKING:
    from core.cache import ScanInfo
    from core.duplicates import DupResult
    from core.history import HistoryDiff
    from core.stats import StatsResult


def iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def write_csv(path: str, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """UTF-8 avec BOM et « ; » : s'ouvre directement dans Excel en français."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        for row in rows:
            w.writerow(row)
            n += 1
    return n


# --- rapport HTML ----------------------------------------------------------------------

@dataclass
class ReportData:
    scan: ScanInfo
    stats: StatsResult
    top_dirs: list[tuple[str, int, int]] = field(default_factory=list)  # (chemin, taille, nb)
    duplicates: DupResult | None = None
    history: HistoryDiff | None = None


_CSS = """
:root { --bg:#fcfcfb; --card:#ffffff; --text:#0b0b0b; --muted:#52514e; --line:#e8e7e3;
        --bar:#2a78d6; --up:#c23434; --down:#0a7d0a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1a1a19; --card:#232322; --text:#ffffff; --muted:#c3c2b7; --line:#383835;
          --bar:#3987e5; --up:#e66767; --down:#3fbf3f; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:14px/1.45 "Segoe UI", system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:32px 0 10px; }
.muted { color:var(--muted); }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:18px 0; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
.tile b { display:block; font-size:20px; } .tile span { color:var(--muted); font-size:12px; }
table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
        border-radius:8px; overflow:hidden; table-layout:fixed; }
th, td { padding:6px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:12px; }
td.n, th.n { text-align:right; width:110px; white-space:nowrap; }
th.d { width:150px; }
td.p { word-break:break-all; }
.bar { height:8px; border-radius:4px; background:var(--bar); min-width:2px; }
.sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
.up { color:var(--up); } .down { color:var(--down); }
.scroll { overflow-x:auto; }
"""


def _e(s: Any) -> str:
    return html.escape(str(s))


def _table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    """headers = [(titre, numérique)] ; les cellules sont déjà échappées."""
    head = "".join(f'<th class="{"n d" if h == "Date" else "n" if num else ""}">{_e(h)}</th>'
                   for h, num in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="{"n" if headers[i][1] else "p"}">{c}</td>' for i, c in enumerate(r))
        + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _bar(value: int, top: int) -> str:
    pct = 100 * value / top if top else 0
    return f'<div class="bar" style="width:{pct:.1f}%"></div>'


def _signed(n: int) -> str:
    cls = "up" if n > 0 else "down" if n < 0 else ""
    sign = "+" if n > 0 else "−" if n < 0 else ""
    return f'<span class="{cls}">{sign}{_e(human_size(abs(n)))}</span>'


def write_html_report(path: str, d: ReportData) -> None:
    s, st = d.scan, d.stats
    root = s.root.rstrip(os.sep)

    def rel(p: str) -> str:
        """Chemin relatif au dossier analysé (rappelé en titre), échappé."""
        if os.path.normcase(p).startswith(os.path.normcase(root) + os.sep):
            return _e(p[len(root) + 1:])
        return _e(p)

    parts: list[str] = []
    add = parts.append
    add(f"<h1>Débarras — {_e(s.root)}</h1>")
    add(f'<p class="muted">Scan du {_e(human_date(s.finished))} — rapport généré le '
        f'{_e(human_date(datetime.now().timestamp()))}</p>')

    wasted = d.duplicates.wasted if d.duplicates else None
    temp = sum(x.size for x in st.temp_dirs) + sum(f.size for f in st.temp_files)
    tiles = [(human_size(s.total_size), "taille totale"), (human_count(s.file_count), "fichiers"),
             (human_count(s.dir_count), "dossiers"), (human_size(temp), "temp & caches"),
             (human_size(st.old_total[0]), f"inutilisés depuis {st.old_days} j")]
    if wasted is not None:
        tiles.append((human_size(wasted), "doublons récupérables"))
    add('<div class="tiles">' + "".join(f"<div class=tile><b>{_e(v)}</b><span>{_e(l)}</span></div>"
                                        for v, l in tiles) + "</div>")

    # Types
    colors = category_colors(False)
    total = st.total_size or 1
    rows = []
    for cat in CATEGORIES:
        size, count = st.by_category.get(cat, (0, 0))
        if count:
            rows.append([f'<span class="sw" style="background:{colors[cat]}"></span>{_e(cat)}',
                         _e(human_size(size)), f"{100 * size / total:.1f} %".replace(".", ","),
                         _e(human_count(count)), _bar(size, total)])
    add("<h2>Répartition par type</h2>")
    add(_table([("Type", False), ("Taille", True), ("Part", True), ("Fichiers", True), ("", False)], rows))

    if d.top_dirs:
        top = d.top_dirs[0][1] if d.top_dirs else 0
        add("<h2>Plus gros dossiers</h2>")
        add(_table([("Dossier", False), ("Taille", True), ("Fichiers", True), ("", False)],
                   [[rel(p), _e(human_size(sz)), _e(human_count(n)), _bar(sz, top)] for p, sz, n in d.top_dirs]))

    def files_table(title: str, files, date_attr: str = "mtime", n: int = 50, note: str = "") -> None:
        if not files:
            return
        add(f"<h2>{_e(title)}</h2>")
        if note:
            add(f'<p class="muted">{_e(note)}</p>')
        add(_table([("Fichier", False), ("Taille", True), ("Date", True)],
                   [[rel(f.path), _e(human_size(f.size)), _e(human_date(getattr(f, date_attr)))]
                    for f in files[:n]]))

    files_table("Plus gros fichiers", st.largest)
    files_table(f"Fichiers inutilisés depuis plus de {st.old_days} jours", st.old, "last_used", 30,
                f"{human_count(st.old_total[1])} fichiers, {human_size(st.old_total[0])} au total.")
    if st.temp_dirs or st.temp_files:
        add("<h2>Temp & caches</h2>")
        rows = [[rel(x.path) + _e(os.sep), _e(human_size(x.size)), _e(human_count(x.file_count))]
                for x in st.temp_dirs[:30]]
        rows += [[rel(f.path), _e(human_size(f.size)), "1"] for f in st.temp_files[:20]]
        add(_table([("Élément", False), ("Taille", True), ("Fichiers", True)], rows))
    files_table("Installeurs oubliés", st.installers, n=30)
    if st.empty_dirs:
        add(f"<h2>Dossiers vides ({human_count(len(st.empty_dirs))})</h2>")
        add(_table([("Dossier", False)], [[rel(p)] for p in st.empty_dirs[:100]]))

    if d.duplicates and d.duplicates.groups:
        dup = d.duplicates
        add(f"<h2>Doublons — {human_count(len(dup.groups))} groupes, {_e(human_size(dup.wasted))} récupérables</h2>")
        rows = [["<br>".join(rel(f.path) for f in g.files), _e(f"{len(g.files)} × {human_size(g.size)}"),
                 _e(human_size(g.wasted))] for g in dup.groups[:50]]
        add(_table([("Fichiers identiques", False), ("Copies", True), ("Récupérable", True)], rows))

    if d.history:
        h = d.history
        add(f"<h2>Évolution depuis le scan du {_e(human_date(h.old.finished))}</h2>")
        add(f"<p>Variation totale : {_signed(h.total_delta)}</p>")
        add(_table([("Dossier", False), ("Avant", True), ("Après", True), ("Variation", True)],
                   [[rel(x.path), _e(human_size(x.old_size)), _e(human_size(x.new_size)), _signed(x.delta)]
                    for x in h.focus[:30]]))

    doc = (f'<!doctype html><html lang="fr"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">'
           f"<title>Débarras — {_e(os.path.basename(s.root.rstrip(os.sep)) or s.root)}</title>"
           f"<style>{_CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
