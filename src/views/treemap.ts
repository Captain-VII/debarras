// Carte « treemap » squarifiée sur <canvas>, cliquable et navigable (port de ui/treemap.py).
// Trois coloriages : sécurité (peut-on supprimer ?), type de fichier, évolution.
import { call, type TreeNode } from "../lib/api";
import { h, mount, showMenu, type MenuItem } from "../lib/dom";
import { dirname, humanCount, humanSize, inside, normcase } from "../lib/format";
import { CATEGORIES, HINTS, LABELS, LEVELS, OTHER, category, categoryColors, extOf, isDark, levelColors } from "../lib/meta";

const MIN_AREA = 12; // px² : en dessous, regroupé dans « autres éléments »
const MIN_SIDE = 6; // px : taille mini d'un dossier pour détailler son contenu
const HEADER = 15; // bandeau de nom d'un dossier
const PAD = 2;

export type Mode = "safety" | "types" | "growth";
export const MODES: Record<Mode, string> = {
  safety: "Sécurité (peut-on supprimer ?)", types: "Type de fichier", growth: "Évolution depuis le scan précédent",
};
const GROWTH_COLORS = {
  light: { up: "#d03b3b", down: "#2e9d5b", new: "#e08a00", same: "#b9b8b3" },
  dark: { up: "#e5534b", down: "#2fa866", new: "#d98a1c", same: "#4a4946" },
};
export const FREE = "::free", UNSCANNED = "::unscanned";
export const SPECIAL_TEXT: Record<string, string> = {
  [FREE]: "Espace libre sur le disque.",
  [UNSCANNED]: "Non analysé : dossier Windows, fichiers système (fichier d'échange, points de restauration, index du disque) et dossiers inaccessibles sans droits administrateur.",
};

type Rect = [number, number, number, number];
interface Item { r: Rect; e: TreeNode | null; label: string; depth: number; detailed: boolean }

function worst(total: number, largest: number, smallest: number, side: number) {
  const s2 = total * total, w2 = side * side;
  return Math.max((w2 * largest) / s2, s2 / (w2 * smallest));
}

/** Découpe `rect` en rectangles d'aires `areas` (triées décroissantes, somme = aire). */
export function squarify(areas: number[], rect: Rect): Rect[] {
  const out: Rect[] = [];
  let [x, y, w, hh] = rect;
  let i = 0;
  const n = areas.length;
  while (i < n) {
    const side = Math.min(w, hh);
    if (side <= 0) {
      for (; i < n; i++) out.push([x, y, 0, 0]);
      break;
    }
    let rowTotal = areas[i];
    const largest = areas[i];
    let j = i + 1;
    let best = worst(rowTotal, largest, areas[i], side);
    while (j < n) {
      const cand = worst(rowTotal + areas[j], largest, areas[j], side);
      if (cand > best) break;
      rowTotal += areas[j];
      best = cand;
      j++;
    }
    const thick = rowTotal / side;
    let pos = w >= hh ? y : x;
    for (const a of areas.slice(i, j)) {
      const len = a / thick;
      out.push(w >= hh ? [x, pos, thick, len] : [pos, y, len, thick]);
      pos += len;
    }
    if (w >= hh) { x += thick; w -= thick; } else { y += thick; hh -= thick; }
    i = j;
  }
  return out;
}

const hex = (c: string) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
const rgb = (v: number[]) => `rgb(${v.map((x) => Math.round(Math.min(255, Math.max(0, x)))).join(",")})`;
const mix = (a: string, b: string, t: number) => { const x = hex(a), y = hex(b); return rgb(x.map((v, i) => v * (1 - t) + y[i] * t)); };
const scale = (c: string, f: number) => rgb(hex(c).map((v) => v * f));

function elide(ctx: CanvasRenderingContext2D, text: string, width: number): string {
  if (ctx.measureText(text).width <= width) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    const half = mid >> 1;
    const t = text.slice(0, half) + "…" + text.slice(text.length - (mid - half));
    if (ctx.measureText(t).width <= width) lo = mid; else hi = mid - 1;
  }
  if (lo < 2) return "";
  const half = lo >> 1;
  return text.slice(0, half) + "…" + text.slice(text.length - (lo - half));
}

export class Treemap {
  readonly el: HTMLElement;
  private canvas = h("canvas");
  private tip = h("div", { class: "tooltip", hidden: true });
  private upBtn = h("button", { onclick: () => this.goUp() }, "⬆ Remonter");
  private pathLabel = h("span", { class: "path grow ellipsis" });
  private modeBox = h("select", {}, Object.entries(MODES).map(([k, v]) => h("option", { value: k }, v)));
  private legend = h("div", { class: "legend" });
  private scanId = 0;
  private root = "";
  current = "";
  private data: TreeNode | null = null;
  private items: Item[] = [];
  private hover: Item | null = null;
  private selected: string | null = null;
  private dirty = true;
  mode: Mode = "safety";
  private growth = new Map<string, number>();
  private since = 0;
  private extras: TreeNode[] = [];
  private request = 0;
  onSelect: (e: TreeNode) => void = () => {};
  onContext: (e: TreeNode) => MenuItem[] = () => [];

  constructor() {
    this.modeBox.onchange = () => this.setMode(this.modeBox.value as Mode);
    const wrap = h("div", { class: "canvas-wrap", tabindex: 0 }, this.canvas, this.tip);
    this.el = h("div", { class: "treemap" },
      h("div", { class: "toolbar" }, this.upBtn, this.pathLabel,
        h("span", { class: "muted" }, "Double-clic : zoomer — Retour arrière : remonter"),
        h("label", {}, "Couleurs : ", this.modeBox)),
      wrap, this.legend);
    new ResizeObserver(() => this.invalidate()).observe(wrap);
    wrap.addEventListener("mousemove", (e) => this.move(e));
    wrap.addEventListener("mouseleave", () => { this.hover = null; this.tip.hidden = true; this.paint(); });
    wrap.addEventListener("mousedown", (e) => this.press(e));
    wrap.addEventListener("dblclick", (e) => this.zoom(e));
    wrap.addEventListener("contextmenu", (e) => this.context(e));
    wrap.addEventListener("keydown", (e) => { if (e.key === "Backspace" || e.key === "Escape") this.goUp(); });
    this.buildLegend();
    this.updateNav();
  }

  // --- API --------------------------------------------------------------------------

  load(scanId: number, root: string) {
    this.scanId = scanId;
    this.root = root;
    this.extras = [];
    this.selected = null;
    void this.navigate(root);
  }

  clear() {
    this.scanId = 0;
    this.data = null;
    this.current = "";
    this.items = [];
    this.growth.clear();
    this.since = 0;
    this.extras = [];
    this.updateNav();
    this.invalidate();
  }

  get loaded() { return this.scanId > 0; }

  /** Disque entier : la carte montre aussi l'espace libre et le non-analysé. */
  setExtras(free: number, unscanned: number) {
    this.extras = ([[UNSCANNED, "Non analysé (Windows, système)", unscanned], [FREE, "Espace libre", free]] as const)
      .filter(([, , s]) => s > 0)
      .map(([p, n, s]) => ({ n, p, s, d: false, c: 0, m: 0, l: "system", r: "" }));
    this.invalidate();
  }

  setMode(mode: Mode) {
    this.mode = mode;
    this.modeBox.value = mode;
    this.buildLegend();
    this.invalidate();
  }

  setGrowth(deltas: Record<string, number>, since: number) {
    this.growth = new Map(Object.entries(deltas).map(([k, v]) => [normcase(k), v]));
    this.since = since;
    if (this.mode === "growth") this.invalidate();
  }

  /** Recharge le dossier affiché (classement de sécurité arrivé, thème changé). */
  refresh() {
    if (this.current) void this.navigate(this.current, this.selected);
  }

  refreshTheme() {
    this.buildLegend();
    this.invalidate();
  }

  async navigate(path: string, select: string | null = null) {
    if (!this.scanId || !inside(path, this.root)) return;
    const req = ++this.request;
    const data = await call<TreeNode>("tree", { scanId: this.scanId, path });
    if (req !== this.request) return;
    this.data = data;
    this.current = path;
    this.selected = select;
    this.updateNav();
    this.invalidate();
  }

  /** Zoome sur un dossier, ou sur le parent d'un fichier en le sélectionnant. */
  async showPath(path: string, isDir: boolean) {
    if (isDir) await this.navigate(path);
    else await this.navigate(dirname(path), path);
  }

  canGoUp() { return !!this.current && normcase(this.current) !== normcase(this.root); }

  goUp() {
    if (this.canGoUp()) void this.navigate(dirname(this.current), this.current);
  }

  private updateNav() {
    this.pathLabel.textContent = this.current;
    this.upBtn.disabled = !this.canGoUp();
  }

  private buildLegend() {
    const dark = isDark();
    let entries: [string, string, string][];
    if (this.mode === "safety") {
      const c = levelColors(dark);
      entries = LEVELS.map((l) => [c[l], LABELS[l], HINTS[l]]);
    } else if (this.mode === "growth") {
      const g = GROWTH_COLORS[dark ? "dark" : "light"];
      entries = [[g.up, "A grossi", ""], [g.down, "A diminué", ""], [g.new, "Fichier nouveau ou modifié", ""], [g.same, "Inchangé", ""]];
    } else {
      const c = categoryColors(dark);
      entries = CATEGORIES.map((cat) => [c[cat], cat, ""]);
    }
    mount(this.legend, entries.map(([color, text, tip]) =>
      h("span", { title: tip || undefined }, h("i", { style: { background: color } }), text)));
  }

  // --- mise en page -----------------------------------------------------------------

  private invalidate() {
    this.dirty = true;
    this.hover = null;
    requestAnimationFrame(() => this.paint());
  }

  private kids(e: TreeNode): TreeNode[] {
    const k = e.k ?? [];
    return normcase(e.p) === normcase(this.root) && this.extras.length
      ? [...k, ...this.extras].sort((a, b) => b.s - a.s)
      : k;
  }

  private layout(w: number, hh: number) {
    this.items = [];
    if (this.data) this.layoutDir(this.data, [1, 1, w - 2, hh - 2], 0);
  }

  private layoutDir(dir: TreeNode, rect: Rect, depth: number) {
    const kids = this.kids(dir);
    const restSize = dir.x?.[1] ?? 0;
    const total = kids.reduce((s, e) => s + e.s, 0) + restSize;
    if (total <= 0 || rect[2] < 1 || rect[3] < 1) return;
    const sc = (rect[2] * rect[3]) / total;
    const keep = kids.filter((e) => e.s * sc >= MIN_AREA);
    const rest = kids.slice(keep.length);
    const restN = rest.length + (dir.x?.[0] ?? 0);
    const areas = keep.map((e) => e.s * sc);
    if (restN) areas.push((rest.reduce((s, e) => s + e.s, 0) + restSize) * sc);
    squarify(areas, rect).forEach((r, i) => {
      if (i >= keep.length) {
        const s = restN > 1 ? "s" : "";
        this.items.push({ r, e: null, label: `${humanCount(restN)} autre${s} élément${s}`, depth, detailed: false });
        return;
      }
      const e = keep[i];
      const item: Item = { r, e, label: e.n, depth, detailed: false };
      this.items.push(item);
      if (!e.d || !e.k) return;
      const head = r[3] >= HEADER * 2.5 && r[2] >= 40 ? HEADER : 0;
      const inner: Rect = [r[0] + PAD, r[1] + PAD + head, r[2] - 2 * PAD, r[3] - 2 * PAD - head];
      if (inner[2] >= MIN_SIDE && inner[3] >= MIN_SIDE) {
        item.detailed = true;
        this.layoutDir(e, inner, depth + 1);
      }
    });
  }

  // --- rendu ------------------------------------------------------------------------

  private fill(e: TreeNode, dark: boolean): string {
    if (e.p === FREE) return dark ? "#2b2b29" : "#f1f0ec";
    if (e.p === UNSCANNED) return "#5c5b57";
    if (this.mode === "safety") return levelColors(dark)[e.l];
    const g = GROWTH_COLORS[dark ? "dark" : "light"];
    if (this.mode === "growth") {
      if (e.d) {
        const d = this.growth.get(normcase(e.p)) ?? 0;
        if (!d) return g.same;
        const ratio = Math.min(Math.abs(d) / Math.max(e.s, 1), 1);
        return mix(g.same, d > 0 ? g.up : g.down, 0.35 + 0.65 * ratio);
      }
      return this.since && e.m > this.since ? g.new : g.same;
    }
    const c = categoryColors(dark);
    return e.d ? scale(c[OTHER], 1 / 1.3) : c[category(extOf(e.n))];
  }

  private paint() {
    const wrap = this.canvas.parentElement!;
    const dpr = devicePixelRatio || 1;
    const w = wrap.clientWidth, hh = wrap.clientHeight;
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(hh * dpr)) {
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(hh * dpr);
      this.canvas.style.width = `${w}px`;
      this.canvas.style.height = `${hh}px`;
      this.dirty = true;
    }
    const ctx = this.canvas.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const dark = isDark();
    const surface = dark ? "#1a1a19" : "#fcfcfb";
    ctx.fillStyle = surface;
    ctx.fillRect(0, 0, w, hh);
    ctx.font = "11px 'Segoe UI Variable Text', 'Segoe UI', sans-serif";
    if (!this.data) {
      ctx.fillStyle = dark ? "#8f8e88" : "#7a7975";
      ctx.textAlign = "center";
      ctx.fillText("Aucun scan chargé.", w / 2, hh / 2);
      ctx.textAlign = "left";
      return;
    }
    if (this.dirty) { this.layout(w, hh); this.dirty = false; }
    const levels = levelColors(dark);
    const g = GROWTH_COLORS[dark ? "dark" : "light"];
    const dirFill = dark ? ["#2a2a28", "#343432"] : ["#ecebe7", "#e2e1dc"];
    const text = dark ? "#ffffff" : "#0b0b0b";
    const muted = dark ? "#c3c2b7" : "#52514e";
    ctx.textBaseline = "middle";
    for (const it of this.items) {
      const [x, y, rw, rh] = it.r;
      if (rw < 1 || rh < 1) continue;
      const e = it.e;
      if (!e) {
        ctx.fillStyle = dirFill[1];
        ctx.fillRect(x, y, rw, rh);
        ctx.fillStyle = dark ? "rgba(195,194,183,.24)" : "rgba(82,81,78,.24)";
        ctx.fillRect(x, y, rw, rh);
      } else if (e.d && it.detailed) {
        let bg = dirFill[it.depth % 2];
        const d = this.growth.get(normcase(e.p)) ?? 0;
        if (this.mode === "safety") bg = mix(bg, levels[e.l], 0.22);
        else if (this.mode === "growth" && d) bg = mix(bg, d > 0 ? g.up : g.down, 0.15);
        ctx.fillStyle = bg;
        ctx.fillRect(x, y, rw, rh);
      } else if (e.p.startsWith("::")) {
        ctx.fillStyle = this.fill(e, dark);
        ctx.fillRect(x, y, rw, rh);
        if (e.p === UNSCANNED) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(x, y, rw, rh);
          ctx.clip();
          ctx.strokeStyle = "rgba(255,255,255,.16)";
          ctx.beginPath();
          for (let t = -rh; t < rw; t += 8) { ctx.moveTo(x + t, y + rh); ctx.lineTo(x + t + rh, y); }
          ctx.stroke();
          ctx.restore();
        }
      } else {
        const base = this.fill(e, dark);
        const grad = ctx.createLinearGradient(x, y, x + rw, y + rh);
        grad.addColorStop(0, scale(base, 1.18));
        grad.addColorStop(1, scale(base, 1 / 1.18));
        ctx.fillStyle = grad;
        ctx.fillRect(x, y, rw, rh);
      }
      ctx.strokeStyle = surface;
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, rw - 1, rh - 1);
      if (e && e.d && it.detailed && rh >= HEADER * 2.5 && rw >= 40) {
        ctx.fillStyle = muted;
        ctx.fillText(elide(ctx, `${e.n}  ${humanSize(e.s)}`, rw - 8), x + 4, y + 1 + HEADER / 2);
      } else if (rw >= 50 && rh >= 18) {
        ctx.fillStyle = text;
        ctx.textAlign = "center";
        const lines = [it.label];
        if (e && rh >= 34) lines.push(humanSize(e.s));
        const cy = y + rh / 2 - ((lines.length - 1) * 13) / 2;
        lines.forEach((l, i) => ctx.fillText(elide(ctx, l, rw - 6), x + rw / 2, cy + i * 13));
        ctx.textAlign = "left";
      }
    }
    const sel = this.items.find((i) => i.e && i.e.p === this.selected);
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2a78d6";
    if (sel) {
      ctx.strokeStyle = accent;
      ctx.lineWidth = 3;
      ctx.strokeRect(sel.r[0] + 1.5, sel.r[1] + 1.5, sel.r[2] - 3, sel.r[3] - 3);
    }
    if (this.hover && this.hover !== sel) {
      ctx.strokeStyle = dark ? "rgba(255,255,255,.8)" : "rgba(0,0,0,.7)";
      ctx.lineWidth = 2;
      ctx.strokeRect(this.hover.r[0] + 1, this.hover.r[1] + 1, this.hover.r[2] - 2, this.hover.r[3] - 2);
    }
  }

  // --- interaction ------------------------------------------------------------------

  private itemAt(ev: MouseEvent): Item | null {
    const b = this.canvas.getBoundingClientRect();
    const x = ev.clientX - b.left, y = ev.clientY - b.top;
    for (let i = this.items.length - 1; i >= 0; i--) {
      const [rx, ry, rw, rh] = this.items[i].r;
      if (x >= rx && x < rx + rw && y >= ry && y < ry + rh) return this.items[i];
    }
    return null;
  }

  private tooltip(it: Item): string {
    const e = it.e;
    if (!e) return it.label;
    if (e.p.startsWith("::")) return `${e.n} — ${humanSize(e.s)}\n${SPECIAL_TEXT[e.p]}`;
    let t = e.d ? `${e.p}\n${humanSize(e.s)} — ${humanCount(e.c)} fichiers` : `${e.p}\n${humanSize(e.s)} — ${category(extOf(e.n))}`;
    t += `\n\n${LABELS[e.l]} : ${e.r}`;
    const d = this.growth.get(normcase(e.p));
    if (this.mode === "growth" && e.d && d) t += `\nDepuis le scan précédent : ${d > 0 ? "+" : "−"}${humanSize(Math.abs(d))}`;
    else if (this.mode === "growth" && !e.d && this.since && e.m > this.since) t += "\nNouveau ou modifié depuis le scan précédent";
    return t;
  }

  private move(ev: MouseEvent) {
    const it = this.itemAt(ev);
    if (it !== this.hover) {
      this.hover = it;
      this.paint();
      this.tip.hidden = !it;
      if (it) this.tip.textContent = this.tooltip(it);
    }
    if (it) {
      const b = this.canvas.getBoundingClientRect();
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      let x = ev.clientX - b.left + 14, y = ev.clientY - b.top + 16;
      if (x + tw > b.width) x = Math.max(0, ev.clientX - b.left - tw - 8);
      if (y + th > b.height) y = Math.max(0, ev.clientY - b.top - th - 8);
      this.tip.style.left = `${x}px`;
      this.tip.style.top = `${y}px`;
    }
  }

  private press(ev: MouseEvent) {
    if (ev.button === 3) return this.goUp(); // bouton « précédent » de la souris
    if (ev.button !== 0) return;
    const it = this.itemAt(ev);
    if (it?.e) {
      this.selected = it.e.p;
      this.onSelect(it.e);
      this.paint();
    }
  }

  private zoom(ev: MouseEvent) {
    const it = this.itemAt(ev);
    if (!it?.e || it.e.p.startsWith("::")) return;
    let target = it.e.d ? it.e.p : dirname(it.e.p);
    // Zoom d'un seul niveau sous le dossier courant.
    while (normcase(dirname(target)) !== normcase(this.current) && inside(dirname(target), this.root) && target !== dirname(target)) {
      target = dirname(target);
    }
    if (normcase(target) !== normcase(this.current)) void this.navigate(target, it.e.p);
  }

  private context(ev: MouseEvent) {
    ev.preventDefault();
    const it = this.itemAt(ev);
    const items: MenuItem[] = it?.e && !it.e.p.startsWith("::") ? this.onContext(it.e) : [];
    items.push({ label: "", separator: true }, { label: "Remonter", action: this.canGoUp() ? () => this.goUp() : undefined });
    showMenu(ev.clientX, ev.clientY, items);
  }
}
