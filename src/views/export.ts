// Exports : CSV (compatible Excel en français) et rapport HTML autonome.
import type { Diff, Scan, Stats } from "../lib/api";
import { basename, humanCount, humanDate, humanSize, percent, signedSize } from "../lib/format";
import { CATEGORIES, categoryColors } from "../lib/meta";

/** CSV « ; » comme le module csv de Python (guillemets seulement si nécessaire). */
export function toCsv(headers: string[], rows: (string | number)[][]): string {
  const cell = (v: string | number) => {
    const s = String(v);
    return /[;"\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [headers, ...rows].map((r) => r.map(cell).join(";") + "\r\n").join("");
}

const CSS = `
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
`;

const e = (s: unknown) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");

function table(headers: [string, boolean][], rows: string[][]): string {
  const head = headers.map(([t, num]) => `<th class="${t === "Date" ? "n d" : num ? "n" : ""}">${e(t)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + r.map((c, i) => `<td class="${headers[i][1] ? "n" : "p"}">${c}</td>`).join("") + "</tr>").join("");
  return `<div class="scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

const bar = (v: number, top: number) => `<div class="bar" style="width:${(top ? (100 * v) / top : 0).toFixed(1)}%"></div>`;
const signed = (n: number) => `<span class="${n > 0 ? "up" : n < 0 ? "down" : ""}">${e(signedSize(n))}</span>`;

export interface ReportData {
  scan: Scan; stats: Stats; topDirs: [string, number, number][];
  duplicates: { wasted: number; groups: { files: { path: string }[]; size: number; wasted: number }[] } | null;
  history: Diff | null;
}

export function htmlReport(d: ReportData): string {
  const s = d.scan, st = d.stats;
  const root = s.root.replace(/\\+$/, "");
  const rel = (p: string) => e(p.toLowerCase().startsWith(root.toLowerCase() + "\\") ? p.slice(root.length + 1) : p);
  const parts: string[] = [];
  const add = (x: string) => parts.push(x);
  add(`<h1>Débarras — ${e(s.root)}</h1>`);
  add(`<p class="muted">Scan du ${e(humanDate(s.finished))} — rapport généré le ${e(humanDate(Date.now() / 1000))}</p>`);
  const temp = st.temp_dirs.reduce((a, x) => a + x[1], 0) + st.temp_files.reduce((a, f) => a + f[1], 0);
  const tiles: [string, string][] = [[humanSize(s.total_size), "taille totale"], [humanCount(s.file_count), "fichiers"],
    [humanCount(s.dir_count), "dossiers"], [humanSize(temp), "temp & caches"], [humanSize(st.old_total[0]), `inutilisés depuis ${st.old_days} j`]];
  if (d.duplicates) tiles.push([humanSize(d.duplicates.wasted), "doublons récupérables"]);
  add('<div class="tiles">' + tiles.map(([v, l]) => `<div class=tile><b>${e(v)}</b><span>${e(l)}</span></div>`).join("") + "</div>");

  const colors = categoryColors(false);
  const total = st.total_size || 1;
  const rows = CATEGORIES.map((c) => st.by_category.find((x) => x[0] === c)).filter((x): x is [string, number, number] => !!x && x[2] > 0)
    .map(([cat, size, count]) => [`<span class="sw" style="background:${colors[cat]}"></span>${e(cat)}`, e(humanSize(size)), percent(size, total), e(humanCount(count)), bar(size, total)]);
  add("<h2>Répartition par type</h2>");
  add(table([["Type", false], ["Taille", true], ["Part", true], ["Fichiers", true], ["", false]], rows));

  if (d.topDirs.length) {
    const top = d.topDirs[0][1];
    add("<h2>Plus gros dossiers</h2>");
    add(table([["Dossier", false], ["Taille", true], ["Fichiers", true], ["", false]],
      d.topDirs.map(([p, sz, n]) => [rel(p), e(humanSize(sz)), e(humanCount(n)), bar(sz, top)])));
  }
  const files = (title: string, list: Stats["largest"], used = false, n = 50, note = "") => {
    if (!list.length) return;
    add(`<h2>${e(title)}</h2>`);
    if (note) add(`<p class="muted">${e(note)}</p>`);
    add(table([["Fichier", false], ["Taille", true], ["Date", true]], list.slice(0, n).map((f) => [rel(f[0]), e(humanSize(f[1])), e(humanDate(used ? f[3] : f[2]))])));
  };
  files("Plus gros fichiers", st.largest);
  files(`Fichiers inutilisés depuis plus de ${st.old_days} jours`, st.old, true, 30, `${humanCount(st.old_total[1])} fichiers, ${humanSize(st.old_total[0])} au total.`);
  if (st.temp_dirs.length || st.temp_files.length) {
    add("<h2>Temp &amp; caches</h2>");
    add(table([["Élément", false], ["Taille", true], ["Fichiers", true]], [
      ...st.temp_dirs.slice(0, 30).map(([p, sz, n]) => [rel(p) + "\\", e(humanSize(sz)), e(humanCount(n))]),
      ...st.temp_files.slice(0, 20).map((f) => [rel(f[0]), e(humanSize(f[1])), "1"])]));
  }
  files("Installeurs oubliés", st.installers, false, 30);
  if (st.empty_dirs.length) {
    add(`<h2>Dossiers vides (${humanCount(st.empty_dirs.length)})</h2>`);
    add(table([["Dossier", false]], st.empty_dirs.slice(0, 100).map((p) => [rel(p)])));
  }
  const dup = d.duplicates;
  if (dup?.groups.length) {
    add(`<h2>Doublons — ${humanCount(dup.groups.length)} groupes, ${e(humanSize(dup.wasted))} récupérables</h2>`);
    add(table([["Fichiers identiques", false], ["Copies", true], ["Récupérable", true]],
      dup.groups.slice(0, 50).map((g) => [g.files.map((f) => rel(f.path)).join("<br>"), e(`${g.files.length} × ${humanSize(g.size)}`), e(humanSize(g.wasted))])));
  }
  const hist = d.history;
  if (hist) {
    add(`<h2>Évolution depuis le scan du ${e(humanDate(hist.old.finished))}</h2>`);
    add(`<p>Variation totale : ${signed(hist.new.total_size - hist.old.total_size)}</p>`);
    add(table([["Dossier", false], ["Avant", true], ["Après", true], ["Variation", true]],
      hist.focus.slice(0, 30).map((p) => { const x = hist.deltas[p]; return [rel(p), e(humanSize(x[0])), e(humanSize(x[1])), signed(x[1] - x[0])]; })));
  }
  return `<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">`
    + `<title>Débarras — ${e(basename(s.root) || s.root)}</title><style>${CSS}</style></head><body><main>${parts.join("")}</main></body></html>`;
}
