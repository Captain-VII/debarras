// Historique : liste des scans, évolution de la taille, comparaison entre deux scans.
import { call, copyPath, reveal, run, type Diff, type Scan } from "../lib/api";
import { Table, ask, h, info, mount, showMenu } from "../lib/dom";
import { basename, displayName, humanCount, humanDate, humanSize, signedCount, signedSize } from "../lib/format";
import { isDark } from "../lib/meta";
import { niceTicks, unit } from "./charts";
import { topChanges } from "./home";
import type { Table2 } from "./stats";

const MAX_CHILDREN = 500;
const NS = "http://www.w3.org/2000/svg";
export const scanLabel = (s: Scan) => humanDate(s.finished || s.started, true);

interface Row { path: string; full: boolean; lazy: boolean }

export class HistoryView {
  readonly el: HTMLElement;
  private scans: Scan[] = [];
  private root = "";
  diff: Diff | null = null;
  private list: Table<Scan>;
  private chart = h("div", { class: "chart" });
  private oldBox = h("select");
  private newBox = h("select");
  private summary = h("p", { class: "summary" });
  private varTree: Table<Row>;
  private focusTree: Table<Row>;
  private deleteBtn = h("button", { onclick: () => this.deleteScan() }, "Retirer de l'historique…");
  private request = 0;
  onDiff: (d: Diff) => void = () => {};

  constructor() {
    this.list = new Table<Scan>([
      { title: "Scan", text: (s) => scanLabel(s) },
      { title: "Taille", num: true, text: (s) => humanSize(s.total_size) },
      { title: "Fichiers", num: true, text: (s) => humanCount(s.file_count) },
      { title: "Variation", num: true, text: (s) => { const p = this.scans[this.scans.indexOf(s) + 1]; return p ? signedSize(s.total_size - p.total_size) : "—"; } },
    ]);
    const d = (r: Row) => this.diff!.deltas[r.path];
    const cols = [
      { title: "Dossier", text: (r: Row) => (r.full ? r.path : basename(r.path)), title_: (r: Row) => r.path, key: (r: Row) => r.path },
      { title: "Avant", num: true, text: (r: Row) => humanSize(d(r)[0]), key: (r: Row) => d(r)[0] },
      { title: "Après", num: true, text: (r: Row) => humanSize(d(r)[1]), key: (r: Row) => d(r)[1] },
      { title: "Variation", num: true, text: (r: Row) => signedSize(d(r)[1] - d(r)[0]), key: (r: Row) => Math.abs(d(r)[1] - d(r)[0]), cls: (r: Row) => (d(r)[1] > d(r)[0] ? "up" : d(r)[1] < d(r)[0] ? "down" : "") },
      { title: "Fichiers", num: true, text: (r: Row) => signedCount(d(r)[3] - d(r)[2]), key: (r: Row) => d(r)[3] - d(r)[2] },
      { title: "État", text: (r: Row) => d(r)[4] },
    ];
    const kids = new WeakMap<Row, Row[]>();
    const ctx = (r: Row, _s: Row[], e: MouseEvent) => void call<boolean[]>("exists", { paths: [r.path] }).then(([ok]) => showMenu(e.clientX, e.clientY, [
      { label: "Afficher dans l'Explorateur", action: ok ? () => reveal(r.path) : undefined },
      { label: "Copier le chemin", action: () => copyPath(r.path) },
    ]));
    this.varTree = new Table<Row>(cols.map((c) => ({ ...c, key: undefined })), {
      children: (r) => {
        if (!r.lazy || !this.diff) return undefined;
        if (!kids.has(r)) kids.set(r, (this.diff.children[r.path] ?? []).slice(0, MAX_CHILDREN).map((path) => ({ path, full: false, lazy: true })));
        return kids.get(r);
      },
      expanded: (r) => r.full,
      onContext: ctx,
    });
    this.focusTree = new Table<Row>(cols, { onContext: ctx });
    const tabs = h("div", { class: "subtabs" });
    const panes = [this.varTree.el, this.focusTree.el];
    const titles = ["Arbre des variations", "Où se concentre la variation"];
    const host = h("div", { class: "fill" });
    const pick = (i: number) => { mount(host, panes[i]); [...tabs.children].forEach((b, j) => b.classList.toggle("on", i === j)); };
    mount(tabs, titles.map((t, i) => h("button", { onclick: () => pick(i) }, t)));
    pick(0);
    this.el = h("div", { class: "history" },
      h("div", { class: "split top" }, h("div", { class: "col" }, this.list.el, h("div", {}, this.deleteBtn)), this.chart),
      h("div", { class: "toolbar" }, "Comparer le scan du ", this.oldBox, " avec celui du ", this.newBox, h("button", { onclick: () => this.compare() }, "Comparer")),
      this.summary, tabs, host);
    this.clear();
  }

  clear() {
    this.scans = [];
    this.root = "";
    this.diff = null;
    this.list.setRows([]);
    mount(this.oldBox);
    mount(this.newBox);
    this.varTree.setRows([]);
    this.focusTree.setRows([]);
    this.summary.textContent = "Aucun scan chargé.";
    this.deleteBtn.disabled = true;
    mount(this.chart, h("h3", {}, "Évolution de la taille"));
  }

  /** Recharge l'historique de `root` et compare automatiquement les deux derniers scans. */
  async setRoot(root: string) {
    this.clear();
    this.root = root;
    this.scans = await call<Scan[]>("list_scans", { root });
    this.list.setRows(this.scans);
    for (const box of [this.oldBox, this.newBox]) mount(box, this.scans.map((s) => h("option", { value: s.id }, scanLabel(s))));
    this.deleteBtn.disabled = this.scans.length < 2;
    this.buildChart();
    if (this.scans.length < 2) {
      this.summary.textContent = "Un seul scan pour ce dossier : rescannez plus tard pour voir ce qui a changé.";
      return;
    }
    this.newBox.selectedIndex = 0;
    this.oldBox.selectedIndex = 1;
    await this.compare();
  }

  refreshTheme() { this.buildChart(); }

  private buildChart() {
    const scans = [...this.scans].reverse();
    if (!scans.length) return mount(this.chart, h("h3", {}, "Évolution de la taille"));
    const W = 560, H = 240, L = 50, B = 30, T = 10;
    const max = Math.max(...scans.map((s) => s.total_size));
    const [u, f] = unit(max);
    const ticks = niceTicks(max / f);
    const top = ticks[ticks.length - 1] || 1;
    const t0 = scans[0].finished || 0, t1 = scans[scans.length - 1].finished || 0;
    const pad = Math.max((t1 - t0) * 0.05, 3600);
    const x = (t: number) => L + ((W - L - 10) * (t - t0 + pad)) / (t1 - t0 + 2 * pad);
    const y = (v: number) => T + (H - T - B) * (1 - v / top);
    const el = (tag: string, a: Record<string, string | number>, text?: string) => {
      const e = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(a)) e.setAttribute(k, String(v));
      if (text) e.textContent = text;
      return e;
    };
    const svgEl = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "linechart" });
    for (const t of ticks) svgEl.append(el("line", { x1: L, x2: W - 10, y1: y(t), y2: y(t), class: "grid" }), el("text", { x: L - 6, y: y(t) + 4, "text-anchor": "end", class: "axis" }, String(t)));
    svgEl.append(el("text", { x: 4, y: 12, class: "axis" }, u));
    const pts = scans.map((s) => [x(s.finished || s.started), y(s.total_size / f)]);
    svgEl.append(el("polyline", { points: pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: isDark() ? "#3987e5" : "#2a78d6", "stroke-width": 2 }));
    const n = Math.min(Math.max(scans.length, 2), 6);
    for (let i = 0; i < n; i++) {
      const t = t0 - pad + ((t1 - t0 + 2 * pad) * i) / (n - 1);
      const dt = new Date(t * 1000);
      svgEl.append(el("text", { x: x(t), y: H - 10, "text-anchor": "middle", class: "axis" },
        `${String(dt.getDate()).padStart(2, "0")}/${String(dt.getMonth() + 1).padStart(2, "0")}/${String(dt.getFullYear()).slice(2)}`));
    }
    scans.forEach((s, i) => {
      const c = el("circle", { cx: pts[i][0], cy: pts[i][1], r: 4, fill: isDark() ? "#3987e5" : "#2a78d6" });
      c.append(el("title", {}, `${humanDate(s.finished)}\n${humanSize(s.total_size)} — ${humanCount(s.file_count)} fichiers`));
      svgEl.append(c);
    });
    mount(this.chart, h("h3", {}, "Évolution de la taille"), svgEl);
  }

  async compare() {
    if (this.scans.length < 2) return;
    let a = Number(this.oldBox.value), b = Number(this.newBox.value);
    if (!a || !b || a === b) return void (this.summary.textContent = "Choisissez deux scans différents.");
    if (a > b) [a, b] = [b, a];
    this.summary.textContent = "Comparaison en cours…";
    const req = ++this.request;
    try {
      const d = await run<Diff>("compare", { oldId: a, newId: b }).promise;
      if (req !== this.request || d.new.root !== this.root) return;
      this.show(d);
    } catch (e) {
      this.summary.textContent = String(e);
    }
  }

  private show(d: Diff) {
    this.diff = d;
    this.onDiff(d);
    const vals = Object.values(d.deltas);
    const fmt = (xs: { path: string; delta: number }[]) => xs.map((x) => `${displayName(x.path, this.root)} (${signedSize(x.delta)})`).join(", ");
    const grown = fmt(topChanges(d, true, 3)), shrunk = fmt(topChanges(d, false, 3));
    mount(this.summary, `Du ${scanLabel(d.old)} au ${scanLabel(d.new)} : `, h("b", {}, signedSize(d.new.total_size - d.old.total_size)),
      ` (${signedCount(d.new.file_count - d.old.file_count)} fichiers) — ${humanCount(vals.length)} dossiers modifiés dont ${humanCount(vals.filter((v) => v[4] === "nouveau").length)} nouveaux et ${humanCount(vals.filter((v) => v[4] === "supprimé").length)} disparus.`,
      grown ? [h("br"), `Ont le plus grossi : ${grown}`] : null, shrunk ? [h("br"), `Ont le plus diminué : ${shrunk}`] : null);
    this.varTree.setRows(d.deltas[this.root] ? [{ path: this.root, full: true, lazy: true }] : []);
    this.focusTree.setRows(d.focus.slice(0, 1000).map((path) => ({ path, full: true, lazy: false })), { col: 3, desc: true });
  }

  private async deleteScan() {
    const s = this.list.selected()[0];
    if (!s) return info("Historique", "Sélectionnez un scan dans la liste.");
    if (s.id === this.scans[0].id) return info("Historique", "Le scan le plus récent sert à l'affichage courant et ne peut pas être retiré.");
    if (!(await ask("Retirer de l'historique", `Retirer le scan du ${scanLabel(s)} de l'historique ?\nSeules les données du cache sont effacées, aucun fichier n'est touché.`))) return;
    await call("delete_scan", { id: s.id });
    await this.setRoot(this.root);
  }

  exportTable(): Table2 | null {
    const d = this.diff;
    if (!d) return null;
    const rows = Object.entries(d.deltas).sort((a, b) => Math.abs(b[1][1] - b[1][0]) - Math.abs(a[1][1] - a[1][0]));
    return [["Dossier", "Octets avant", "Octets après", "Variation (octets)", "Fichiers avant", "Fichiers après", "État"],
      rows.map(([p, x]) => [p, x[0], x[1], x[1] - x[0], x[2], x[3], x[4]])];
  }
}
