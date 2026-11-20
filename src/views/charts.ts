// Graphiques SVG : anneau par type de fichier, barres par sous-dossier (navigables).
import { call, type Stats } from "../lib/api";
import { h, mount } from "../lib/dom";
import { basename, dirname, humanCount, humanSize, normcase, percent } from "../lib/format";
import { CATEGORIES, categoryColors, isDark } from "../lib/meta";

const TOP_FOLDERS = 15;
const NS = "http://www.w3.org/2000/svg";

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number>, ...kids: (SVGElement | string)[]) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  for (const c of kids) el.append(c);
  return el;
}

/** Unité lisible pour un axe : (nom, facteur). */
export function unit(max: number): [string, number] {
  for (const [n, f] of [["To", 1024 ** 4], ["Go", 1024 ** 3], ["Mo", 1024 ** 2], ["Ko", 1024]] as const) if (max >= f) return [n, f];
  return ["o", 1];
}

/** Graduation « ronde » d'un axe de 0 à `max`. */
export function niceTicks(max: number, count = 5): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = 0; v <= max + step * 0.999; v += step) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}

export class Charts {
  readonly el: HTMLElement;
  private pie = h("div", { class: "chart" });
  private legend = h("table", { class: "legend-table" });
  private bars = h("div", { class: "chart" });
  private upBtn = h("button", { onclick: () => this.up() }, "⬆ Remonter");
  private barLabel = h("span", { class: "path grow ellipsis" });
  private scanId = 0;
  private root = "";
  private dir = "";
  private stats: Stats | null = null;

  constructor() {
    this.el = h("div", { class: "charts" },
      h("section", {}, this.pie, this.legend),
      h("section", {}, h("div", { class: "toolbar" }, this.upBtn, this.barLabel, h("span", { class: "muted" }, "Clic sur une barre : détailler le dossier")), this.bars));
    this.clear();
  }

  clear() {
    this.scanId = 0;
    this.stats = null;
    mount(this.pie, h("h3", {}, "Répartition par type"));
    mount(this.legend);
    mount(this.bars, h("h3", {}, "Plus gros sous-dossiers"));
    this.barLabel.textContent = "";
    this.upBtn.disabled = true;
  }

  setData(scanId: number, s: Stats) {
    this.scanId = scanId;
    this.root = s.root;
    this.stats = s;
    this.buildPie(s);
    void this.showFolder(s.root);
  }

  refreshTheme() {
    if (this.stats) this.setData(this.scanId, this.stats);
  }

  private buildPie(s: Stats) {
    const colors = categoryColors(isDark());
    const cats = CATEGORIES.map((c): [string, number, number] => s.by_category.find((x) => x[0] === c) ?? [c, 0, 0]).filter((c) => c[1] > 0);
    const total = cats.reduce((t, c) => t + c[1], 0) || 1;
    const R = 120, r = 0.45 * R, C = 140;
    const g = svg("g", { transform: `translate(${C},${C})` });
    let a0 = -Math.PI / 2;
    for (const [cat, size, count] of cats) {
      const a1 = a0 + (2 * Math.PI * size) / total;
      const mid = (a0 + a1) / 2;
      const big = a1 - a0 > Math.PI ? 1 : 0;
      const p = (rad: number, a: number) => `${rad * Math.cos(a)},${rad * Math.sin(a)}`;
      const d = cats.length === 1
        ? `M${-R},0A${R},${R} 0 1 1 ${R},0A${R},${R} 0 1 1 ${-R},0ZM${-r},0A${r},${r} 0 1 0 ${r},0A${r},${r} 0 1 0 ${-r},0Z`
        : `M${p(R, a0)}A${R},${R} 0 ${big} 1 ${p(R, a1)}L${p(r, a1)}A${r},${r} 0 ${big} 0 ${p(r, a0)}Z`;
      const path = svg("path", { d, fill: colors[cat], class: "slice", "fill-rule": "evenodd" },
        svg("title", {}, `${cat}\n${humanSize(size)} — ${percent(size, total)}\n${humanCount(count)} fichiers`));
      path.addEventListener("mouseenter", () => path.setAttribute("transform", `translate(${8 * Math.cos(mid)},${8 * Math.sin(mid)})`));
      path.addEventListener("mouseleave", () => path.removeAttribute("transform"));
      g.append(path);
      a0 = a1;
    }
    mount(this.pie, h("h3", {}, `Répartition par type — ${humanSize(s.total_size)}`), svg("svg", { viewBox: `0 0 ${2 * C} ${2 * C}`, class: "donut" }, g));
    mount(this.legend, cats.map(([cat, size, count]) => h("tr", {},
      h("td", {}, h("i", { class: "sw", style: { background: colors[cat] } })), h("td", {}, cat),
      h("td", { class: "n" }, humanSize(size)), h("td", { class: "n" }, percent(size, total)), h("td", { class: "n" }, `${humanCount(count)} fichiers`))));
  }

  async showFolder(path: string) {
    if (!this.scanId) return;
    this.dir = path;
    const l = await call<{ info: [number, number] | null; dirs: [string, number, number][] }>("list_dir", { scanId: this.scanId, path });
    const subdirs = [...l.dirs].sort((a, b) => b[1] - a[1]);
    const own = (l.info?.[0] ?? 0) - subdirs.reduce((s, d) => s + d[1], 0);
    let entries: [string, string | null, number][] = subdirs.filter((d) => d[1] > 0).map(([p, s]) => [basename(p), p, s]);
    if (own > 0) entries.push(["(fichiers directs)", null, own]);
    entries.sort((a, b) => b[2] - a[2]);
    if (entries.length > TOP_FOLDERS) {
      const rest = entries.slice(TOP_FOLDERS - 1);
      entries = [...entries.slice(0, TOP_FOLDERS - 1), [`${rest.length} autres`, null, rest.reduce((s, e) => s + e[2], 0)]];
    }
    const dark = isDark();
    const max = Math.max(0, ...entries.map((e) => e[2]));
    const [u, f] = unit(max);
    const ticks = niceTicks(max / f);
    const top = ticks[ticks.length - 1] || 1;
    const W = 640, L = 250, rowH = 26, H = entries.length * rowH + 40;
    const x = (v: number) => L + ((W - L - 20) * v) / top;
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "barchart" });
    for (const t of ticks) {
      root.append(svg("line", { x1: x(t), x2: x(t), y1: 0, y2: H - 30, class: "grid" }),
        svg("text", { x: x(t), y: H - 14, "text-anchor": "middle", class: "axis" }, String(t)));
    }
    root.append(svg("text", { x: W - 10, y: H - 2, "text-anchor": "end", class: "axis" }, u));
    entries.forEach(([name, p, size], i) => {
      const y = i * rowH + 4;
      const label = `${name.length <= 28 ? name : name.slice(0, 26) + "…"}  ·  ${humanSize(size)}`;
      const bar = svg("rect", { x: L, y, width: Math.max(x(size / f) - L, 1), height: rowH * 0.7, rx: 2, fill: dark ? "#3987e5" : "#2a78d6", class: p ? "bar link" : "bar" },
        svg("title", {}, `${p ?? this.dir}\n${humanSize(size)}`));
      if (p) bar.addEventListener("click", () => void this.showFolder(p));
      root.append(svg("text", { x: L - 8, y: y + rowH * 0.35, "text-anchor": "end", "dominant-baseline": "middle", class: "label" }, label), bar);
    });
    mount(this.bars, h("h3", {}, `Plus gros sous-dossiers — ${basename(path)}`), root);
    this.barLabel.textContent = path;
    this.upBtn.disabled = normcase(path) === normcase(this.root);
  }

  private up() {
    if (normcase(this.dir) !== normcase(this.root)) void this.showFolder(dirname(this.dir));
  }
}
