// Accueil : disques, synthèse « que puis-je supprimer sans risque ? », carte et détail.
import { call, reveal, type Diff, type Drive, type Summary, type TreeNode } from "../lib/api";
import { h, mount } from "../lib/dom";
import { basename, dirname, displayName, humanDate, humanSize, inside, normcase, signedSize } from "../lib/format";
import { HINTS, LABELS, LEVELS, isDark, levelColors, type Level } from "../lib/meta";
import { SPECIAL_TEXT, Treemap } from "./treemap";

const LIBRARY_DIRS = new Set(["common", "epic games", "gog games", "xboxgames", "games", "jeux"]);

/** Index du premier dossier après une bibliothèque de jeux (common, Epic Games…), sinon 0. */
function anchor(parts: string[]): number {
  let best = 0;
  parts.slice(0, -1).forEach((p, i) => { if (LIBRARY_DIRS.has(p.toLowerCase())) best = i + 1; });
  return best;
}

const parts = (path: string, root: string) => displayName(path, root).split("\\").filter(Boolean);

/** Libellé court : « Path of Exile 2 › Content » plutôt que le chemin complet. */
export function shortName(path: string, root: string): string {
  const p = parts(path, root);
  if (p.length <= 1) return p[0] ?? path;
  const a = anchor(p);
  if (!a) return `${p[p.length - 2]} › ${p[p.length - 1]}`;
  return a === p.length - 1 ? p[a] : `${p[a]} › ${p[p.length - 1]}`;
}

export interface Growth { path: string; delta: number }

/** Les `n` plus fortes hausses (`grown`) ou baisses de la vue à plat. */
export function topChanges(diff: Diff, grown: boolean, n: number): Growth[] {
  const out: Growth[] = [];
  for (const path of diff.focus) {
    const d = diff.deltas[path];
    const delta = d[1] - d[0];
    if ((delta > 0) === grown) out.push({ path, delta });
    if (out.length >= n) break;
  }
  return out;
}

/** Les `n` plus fortes hausses, regroupées par jeu d'une bibliothèque, sans doublon. */
export function headlineGrowth(diff: Diff, n = 3): Growth[] {
  const root = diff.new.root;
  const picks = new Map<string, Growth>();
  for (const d of topChanges(diff, true, 15)) {
    const p = parts(d.path, root);
    const a = anchor(p);
    let key = d.path;
    if (a && p.length > a + 1) key = [root.replace(/\\+$/, ""), ...p.slice(0, a + 1)].join("\\");
    if (!picks.has(key.toLowerCase())) {
      const g = diff.deltas[key];
      picks.set(key.toLowerCase(), g ? { path: key, delta: g[1] - g[0] } : d);
    }
  }
  const cands = [...picks.values()];
  const keep = cands.filter((d) => !cands.some((o) => o !== d && o.path.toLowerCase().startsWith(d.path.toLowerCase().replace(/\\+$/, "") + "\\")));
  return keep.sort((a, b) => b.delta - a.delta).slice(0, n);
}

export interface HomeEvents {
  openDrive(root: string): void;
  scanDrive(root: string): void;
  openCleanup(): void;
  openHistory(): void;
  autoClean(): void;
  relaunchAdmin(): void;
  trash(path: string): void;
}

export class Home {
  readonly el: HTMLElement;
  readonly treemap = new Treemap();
  private drives = h("div", { class: "drives" });
  private headline = h("div", { class: "headline" }, "Choisissez un disque à analyser ci-dessus.");
  private chips = new Map<Level, HTMLButtonElement>();
  private autoBtn = h("button", { class: "primary big", disabled: true }, "🧹 Nettoyer automatiquement");
  private details = h("aside", { class: "details" });
  private summary: Summary | null = null;
  private base = "";
  private extra = "";
  private growthText: (Node | string)[] = [];
  private growth = new Map<string, number>();
  private since = 0;
  private detailPath = "";
  root = "";
  private scanId = 0;

  constructor(private ev: HomeEvents) {
    for (const lv of ["cleanable", "personal", "software", "system"] as Level[]) {
      const b = h("button", { class: "chip", title: `${HINTS[lv]}\n\nCliquez pour voir les plus gros éléments.`, onclick: () => this.chip(lv) });
      this.chips.set(lv, b);
    }
    this.autoBtn.onclick = () => ev.autoClean();
    this.autoBtn.title = "Met à la corbeille, en un clic, ce qui est recréé automatiquement : fichiers temporaires, caches des navigateurs fermés, rapports d'erreurs.\nUn récapitulatif s'affiche avant ; tout reste récupérable dans la corbeille.";
    this.treemap.onSelect = (e) => this.selected(e);
    const banner = h("section", { class: "banner" }, this.headline,
      h("div", { class: "chips" }, [...this.chips.values()], h("span", { class: "grow" }),
        h("div", { class: "side" }, this.autoBtn,
          h("button", { title: "Caches des navigateurs, fichiers temporaires, cache graphique, Windows Update…", onclick: () => ev.openCleanup() }, "Choisir quoi nettoyer…"))));
    this.el = h("div", { class: "home" }, this.drives, banner, h("div", { class: "split" }, this.treemap.el, this.details));
    this.setChips();
    this.welcome();
  }

  setDrives(drives: Drive[], busy: boolean) {
    const cur = normcase(this.root);
    mount(this.drives, drives.length ? drives.map((d) => h("div", { class: `drive${normcase(d.root) === cur ? " current" : ""}` },
      h("b", {}, d.title),
      h("div", { class: "meter" }, h("i", { style: { width: `${d.total ? (100 * d.used) / d.total : 0}%` } })),
      h("div", {}, `${humanSize(d.free)} libres sur ${humanSize(d.total)}`),
      h("div", { class: "muted" }, d.scan ? `Analysé le ${humanDate(d.scan.finished)}` : "Jamais analysé"),
      h("div", { class: "row" }, d.scan
        ? [h("button", { disabled: busy, onclick: () => this.ev.openDrive(d.root) }, "Afficher"), h("button", { disabled: busy, onclick: () => this.ev.scanDrive(d.root) }, "Réanalyser")]
        : h("button", { disabled: busy, onclick: () => this.ev.scanDrive(d.root) }, "Analyser"))))
      : h("p", {}, "Aucun disque détecté."));
  }

  // --- données ----------------------------------------------------------------------

  clear() {
    this.summary = null;
    this.root = "";
    this.scanId = 0;
    this.treemap.clear();
    this.setChips();
    this.base = this.extra = "";
    this.growthText = [];
    this.growth.clear();
    mount(this.headline, "Choisissez un disque à analyser ci-dessus.");
    this.welcome();
  }

  setLoading(scanId: number, root: string, total: number) {
    this.scanId = scanId;
    this.root = root;
    this.summary = null;
    this.setChips();
    this.welcome();
    this.base = `**${humanSize(total)}** analysés dans ${root} — classement en cours…`;
    this.growthText = [];
    this.update();
  }

  setSummary(total: number, s: Summary) {
    this.summary = s;
    this.setChips();
    const clean = s.totals.cleanable ?? 0;
    this.base = clean
      ? `**${humanSize(total)}** analysés dans ${this.root}. **${humanSize(clean)}** de caches et fichiers temporaires, recréés automatiquement`
      : `**${humanSize(total)}** analysés dans ${this.root}.`;
    this.update();
  }

  setGrowth(diff: Diff | null) {
    if (!diff) {
      this.growthText = [];
      this.growth.clear();
      this.treemap.setGrowth({}, 0);
    } else {
      const deltas = Object.fromEntries(Object.entries(diff.deltas).map(([p, d]) => [p, d[1] - d[0]]));
      this.since = diff.old.finished || diff.old.started;
      this.growth = new Map(Object.entries(deltas).map(([k, v]) => [normcase(k), v]));
      this.treemap.setGrowth(deltas, this.since);
      const total = diff.new.total_size - diff.old.total_size;
      const detail = headlineGrowth(diff).map((d) => `${shortName(d.path, diff.new.root)} ${signedSize(d.delta)}`).join(", ");
      this.growthText = [h("br"), `Depuis le scan du ${humanDate(this.since)} : `, h("b", {}, signedSize(total)),
        detail && total > 0 ? ` — surtout ${detail}` : "", " (", this.link("voir sur la carte", () => this.treemap.setMode("growth")),
        " · ", this.link("détail", () => this.ev.openHistory()), ")"];
    }
    this.update();
  }

  setContext(drive: Drive | null, errors: number, admin: boolean) {
    const p: string[] = [];
    if (drive) p.push(`Disque de ${humanSize(drive.total)} : ${humanSize(drive.used)} utilisés, ${humanSize(drive.free)} libres.`);
    this.extra = p.join(" ");
    this.adminLink = errors > 0 && !admin ? errors : 0;
    this.update();
  }
  private adminLink = 0;

  setAutoClean(size: number | null) {
    if (size === null) {
      this.autoBtn.textContent = "🧹 Nettoyer automatiquement (calcul…)";
      this.autoBtn.disabled = true;
    } else {
      this.autoBtn.textContent = size ? `🧹 Nettoyer automatiquement (${humanSize(size)})` : "🧹 Rien à nettoyer automatiquement";
      this.autoBtn.disabled = size <= 0;
    }
  }

  private link(text: string, f: () => void) {
    return h("a", { href: "#", onclick: (e: Event) => { e.preventDefault(); f(); } }, text);
  }

  private update() {
    if (!this.base) return;
    const bold = (s: string) => s.split(/\*\*(.+?)\*\*/).map((x, i) => (i % 2 ? h("b", {}, x) : x));
    const clean = this.summary?.totals.cleanable;
    mount(this.headline, bold(this.base),
      clean ? [" (", this.link("voir", () => this.chip("cleanable", true)), ")."] : null,
      this.extra ? ` ${this.extra}` : "",
      this.adminLink ? [` ⚠ ${this.adminLink} dossier(s) inaccessible(s) sans droits administrateur — `,
        this.link("analyser en administrateur", () => this.ev.relaunchAdmin()), "."] : null,
      this.growthText);
  }

  private setChips() {
    const colors = levelColors(isDark());
    for (const [lv, b] of this.chips) {
      const size = this.summary ? this.summary.totals[lv] ?? 0 : null;
      b.style.borderLeftColor = colors[lv];
      mount(b, h("span", {}, LABELS[lv]), h("b", {}, size === null ? "…" : humanSize(size)));
      b.classList.remove("on");
    }
  }

  refreshTheme() {
    this.treemap.refreshTheme();
    this.setChips();
    if (!this.detailPath) this.welcome();
  }

  // --- panneau de détail ------------------------------------------------------------

  private badge(lv: Level) {
    return h("span", { class: "badge", style: { background: levelColors(isDark())[lv] } }, `● ${LABELS[lv]}`);
  }

  private welcome() {
    this.detailPath = "";
    const c = levelColors(isDark());
    mount(this.details, h("h3", {}, "Cliquez sur un rectangle"),
      h("p", {}, "Chaque rectangle est un dossier ou un fichier, proportionnel à sa taille. Sa couleur indique si vous pouvez le supprimer :"),
      LEVELS.map((lv) => h("p", {}, h("span", { style: { color: c[lv] } }, "■ "), h("b", {}, LABELS[lv]), ` — ${HINTS[lv]}`)),
      h("p", {}, "Les éléments système ne peuvent pas être supprimés depuis Débarras, et tout ce que vous supprimez passe par la corbeille."));
  }

  private selected(e: TreeNode) {
    for (const b of this.chips.values()) b.classList.remove("on");
    if (e.p.startsWith("::")) {
      this.detailPath = "";
      mount(this.details, h("h3", {}, e.p.endsWith("free") ? "Espace libre" : "Non analysé"),
        h("p", {}, h("b", {}, humanSize(e.s))), h("p", {}, SPECIAL_TEXT[e.p]));
    } else void this.showPath(e.p, e.s);
  }

  async showPath(path: string, size: number) {
    this.detailPath = path;
    const v = await call<{ level: Level; reason: string; is_dir: boolean; inside: string[] }>("inspect", { path });
    if (this.detailPath !== path) return;
    const colors = levelColors(isDark());
    const d = this.growth.get(normcase(path));
    const blocked = v.level === "system";
    mount(this.details,
      h("h3", { class: "sel-text" }, basename(path)), h("div", { class: "muted small sel-text" }, path),
      this.badge(v.level),
      h("p", {}, h("b", {}, humanSize(size)), v.is_dir ? " — dossier" : ""),
      h("p", {}, h("b", {}, "Pourquoi : "), `${v.reason}.`),
      h("p", {}, HINTS[v.level]),
      v.inside.length ? h("p", {}, h("span", { style: { color: colors.software } }, "⚠ ", h("b", {}, "Contient des logiciels :")),
        v.inside.map((x) => [h("br"), `• ${displayName(x, path)}`])) : null,
      d ? h("p", {}, h("b", {}, `Depuis le scan du ${humanDate(this.since)} : `), signedSize(d)) : null,
      h("div", { class: "stack" },
        v.is_dir ? h("button", { onclick: () => this.treemap.navigate(path) }, "Zoomer sur ce dossier") : null,
        h("button", { onclick: () => reveal(path) }, "Afficher dans l'Explorateur"),
        h("button", { disabled: blocked, onclick: () => this.ev.trash(path) },
          blocked ? "🔒 Protégé par Débarras" : v.level === "software" ? "Mettre à la corbeille… ⚠" : "Mettre à la corbeille…")));
  }

  private chip(lv: Level, force = false) {
    const b = this.chips.get(lv)!;
    const on = force || !b.classList.contains("on");
    for (const c of this.chips.values()) c.classList.toggle("on", c === b && on);
    if (!on) return this.welcome();
    this.detailPath = "";
    const rows = this.summary?.top[lv] ?? [];
    mount(this.details, h("h3", {}, `Les plus gros éléments — ${LABELS[lv]}`), this.badge(lv),
      h("p", {}, HINTS[lv]), h("p", {}, "Cliquez sur un élément pour le situer."),
      h("ul", { class: "items" }, rows.length ? rows.map(([path, size, reason]) =>
        h("li", { title: `${path}\n${reason}`, onclick: () => this.reveal(path, size) }, h("span", { class: "n" }, humanSize(size)), h("span", { class: "ellipsis rtl" }, path)))
        : h("li", {}, "Aucun élément de ce type dans ce scan.")));
  }

  /** Situe un élément dans la carte (sélectionné dans son dossier parent) et le détaille. */
  private async reveal(path: string, size: number) {
    if (!this.treemap.loaded) return;
    const parent = dirname(path);
    if (parent !== path && inside(parent, this.root) && normcase(path) !== normcase(this.root)) await this.treemap.navigate(parent, path);
    else await this.treemap.navigate(path);
    void this.showPath(path, size);
  }

  get scan() { return this.scanId; }
}
