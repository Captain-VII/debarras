// Doublons, images similaires, même nom : groupes de fichiers « en trop » à cocher.
import { open } from "@tauri-apps/plugin-dialog";
import { call, copyPath, reveal, run, type Task } from "../lib/api";
import { Table, h, mount, progressBar, showMenu, type Col } from "../lib/dom";
import { basename, humanCount, humanDate, humanDuration, humanSize, iso, normcase } from "../lib/format";
import { LABELS, isDark, levelColors, type Level } from "../lib/meta";
import type { Kind } from "./actions";
import type { Table2 } from "./stats";

export interface FFile {
  path: string; size: number; mtime: number;
  width?: number; height?: number; distance?: number; thumb?: string | null; version?: string;
  checked?: boolean; risk?: { level: Level; reason: string }; guarded?: boolean;
}
export interface FGroup { files: FFile[]; size?: number; digest?: string; name?: string; versions?: number }
interface Result { root: string; groups: FGroup[]; cancelled: boolean; duration: number; candidates: number; [k: string]: any }
type Row = { g: FGroup; f?: FFile };

/** Place libérée en ne gardant que `keep(g)`. */
const freed = (keep: (g: FGroup) => FFile) => (g: FGroup) => g.files.reduce((s, f) => s + f.size, 0) - (g.files.length ? keep(g).size : 0);
const maxBy = (files: FFile[], key: (f: FFile) => number[]) => files.reduce((a, b) => {
  const x = key(a), y = key(b);
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return y[i] > x[i] ? b : a;
  return b; // à égalité, le dernier (comme max_by de Rust)
});
const dupWasted = freed((g) => g.files[0]);
const imgWasted = freed((g) => maxBy(g.files, (f) => [(f.width ?? 0) * (f.height ?? 0), f.size]));
const nameWasted = freed((g) => maxBy(g.files, (f) => [f.mtime]));

/** Écart de Hamming (sur 64 bits) exprimé en ressemblance. */
export const resemblance = (bits: number) => (bits === 0 ? "identique" : `${Math.round(100 - (bits * 100) / 64)} %`);

interface Config {
  rules: [string, string][];
  search: string;
  minKo: number;
  cols: Col<Row>[];
  options?: () => HTMLElement[];
  start: (root: string, minSize: number, progress: (d: any) => void) => Task<Result>;
  resultText: (r: Result) => string;
  ordered?: (g: FGroup) => FFile[];
  exportTable: (r: Result, checked: Set<string>) => Table2;
  wasted: (g: FGroup) => number;
  preview?: boolean;
}

export class FinderView {
  readonly el: HTMLElement;
  private table: Table<Row>;
  private root = "";
  private task: Task<Result> | null = null;
  result: Result | null = null;
  private minSize: HTMLInputElement;
  private searchBtn: HTMLButtonElement;
  private cancelBtn = h("button", { disabled: true }, "Annuler");
  private bar = progressBar();
  private info = h("p", { class: "summary" }, "Chargez un scan puis lancez la recherche.");
  private selLabel = h("span", { class: "grow" });
  private rule: HTMLSelectElement;
  private priority = h("input", { type: "text", placeholder: "Dossier(s) à conserver en priorité, séparés par « ; »" });
  private priorityBox: HTMLElement;
  private actionBtns: HTMLButtonElement[];
  private previewImg = h("div", { class: "preview" }, "Sélectionnez une image pour l'afficher ici.");
  private previewInfo = h("p", { class: "small" });

  constructor(private cfg: Config, private request: (k: Kind, paths: string[]) => void) {
    this.minSize = h("input", { type: "number", min: 1, max: 10_000_000, value: String(cfg.minKo) });
    this.searchBtn = h("button", { class: "primary", onclick: () => this.start() }, cfg.search);
    this.cancelBtn.onclick = () => { this.cancelBtn.disabled = true; this.task?.cancel(); };
    this.rule = h("select", {}, cfg.rules.map(([label, key]) => h("option", { value: key }, label)));
    this.priorityBox = h("span", { class: "row grow", hidden: true }, this.priority, h("button", { onclick: () => this.pickPriority() }, "…"));
    this.rule.onchange = () => (this.priorityBox.hidden = this.rule.value !== "priority");
    this.table = new Table<Row>(cfg.cols, {
      multi: true,
      children: (r) => (r.f ? undefined : (cfg.ordered ?? ((g) => [...g.files].sort((a, b) => a.path.toLowerCase().localeCompare(b.path.toLowerCase()))))(r.g).map((f) => ({ g: r.g, f }))),
      expanded: () => true,
      check: { get: (r) => (!r.f || r.f.risk?.level === "system" ? null : !!r.f.checked), set: (r, on) => this.setChecked(r, on) },
      rowClass: (r) => (r.f ? "" : "group"),
      onContext: (r, _s, e) => r.f && showMenu(e.clientX, e.clientY, [
        { label: "Garder uniquement celui-ci", action: () => this.keepOnly(r) },
        { label: "", separator: true },
        { label: "Afficher dans l'Explorateur", action: () => reveal(r.f!.path) },
        { label: "Copier le chemin", action: () => copyPath(r.f!.path) },
      ]),
      onSelect: (r) => cfg.preview && r?.f && void this.showPreview(r.f),
      limit: 30000,
    });
    this.actionBtns = ([["Corbeille…", "trash"], ["Déplacer…", "move"], ["Archiver…", "archive"]] as const).map(([l, k]) =>
      h("button", { disabled: true, onclick: () => this.act(k) }, l));
    const center = cfg.preview
      ? h("div", { class: "split" }, this.table.el, h("aside", { class: "details" }, this.previewImg, this.previewInfo))
      : this.table.el;
    this.el = h("div", { class: "finder" },
      h("div", { class: "toolbar" }, h("label", {}, "Taille minimale : ", this.minSize, " Ko"), cfg.options?.() ?? null,
        this.searchBtn, this.cancelBtn, this.bar.el),
      this.info,
      h("div", { class: "toolbar" }, this.rule, this.priorityBox,
        h("button", { onclick: () => this.autoSelect() }, "Appliquer la sélection auto"),
        h("button", { onclick: () => this.setChecks(() => []) }, "Tout décocher")),
      center,
      h("div", { class: "toolbar" }, this.selLabel, this.actionBtns));
    this.setScan("");
  }

  /** Nouveau scan chargé : résultats conservés (épurés) si c'est le même dossier. */
  setScan(root: string) {
    if (root && root === this.root && this.result) return void this.pruneMissing();
    this.task?.cancel();
    this.root = root;
    this.result = null;
    this.table.setRows([]);
    this.searchBtn.disabled = !root || !!this.task;
    this.info.textContent = root ? `Dossier : ${root}` : "Chargez un scan puis lancez la recherche.";
    this.updateSelection();
  }

  private async start() {
    if (!this.root || this.task) return;
    this.table.setRows([]);
    this.result = null;
    this.searchBtn.disabled = true;
    this.cancelBtn.disabled = false;
    this.bar.el.hidden = false;
    this.bar.set(null);
    this.info.textContent = "Recherche en cours…";
    const root = this.root;
    const t = this.cfg.start(root, Math.max(1, Number(this.minSize.value) || 1) * 1024, (d) => {
      this.bar.set(d.total ? d.done / d.total : 0);
      const bytes = d.phase === "Hash complet" || d.phase === "Comparaison du contenu";
      this.info.textContent = `${d.phase} : ${bytes ? humanSize(d.done) : humanCount(d.done)} / ${bytes ? humanSize(d.total) : humanCount(d.total)}`;
    });
    this.task = t;
    try {
      const r = await t.promise;
      if (r.root !== this.root) return; // un autre scan a été chargé entre-temps
      if (r.cancelled) { this.info.textContent = "Recherche annulée (les empreintes déjà calculées sont conservées)."; return; }
      await this.mark(r.groups);
      this.result = r;
      this.fill();
      const notes = [];
      if (r.skipped_cloud) notes.push(`${humanCount(r.skipped_cloud)} fichiers en ligne ignorés (non téléchargés)`);
      if (r.skipped_changed) notes.push(`${humanCount(r.skipped_changed)} modifiés/supprimés depuis le scan`);
      if (r.skipped_error) notes.push(`${humanCount(r.skipped_error)} illisibles`);
      this.info.textContent = this.cfg.resultText(r) + (notes.length ? ` ${notes.join(" ; ")}.` : "");
    } catch (e) {
      this.info.textContent = String(e);
    } finally {
      this.task = null;
      this.bar.el.hidden = true;
      this.cancelBtn.disabled = true;
      this.searchBtn.disabled = !this.root;
    }
  }

  /** Fichiers système ou de logiciel signalés (jamais cochés d'office) ; liste blanche. */
  private async mark(groups: FGroup[]) {
    const files = groups.flatMap((g) => g.files);
    const [verdicts, guarded] = await Promise.all([
      call<{ level: Level; reason: string }[]>("classify", { items: files.map((f) => [f.path, false]) }),
      call<boolean[]>("protected", { paths: files.map((f) => f.path) }),
    ]);
    files.forEach((f, i) => {
      if (verdicts[i].level === "system" || verdicts[i].level === "software") f.risk = verdicts[i];
      f.guarded = guarded[i];
    });
  }

  private fill() {
    this.table.setRows((this.result?.groups ?? []).map((g) => ({ g })));
    this.updateSelection();
  }

  private checked(): FFile[] {
    return (this.result?.groups ?? []).flatMap((g) => g.files.filter((f) => f.checked));
  }

  private updateSelection(message?: string) {
    const files = this.checked();
    const size = files.reduce((s, f) => s + f.size, 0);
    this.selLabel.textContent = message ?? (files.length ? `${humanCount(files.length)} fichier(s) coché(s) — ${humanSize(size)} récupérables` : "Aucun fichier coché.");
    this.actionBtns.forEach((b) => (b.disabled = !files.length));
  }

  private setChecked(r: Row, on: boolean) {
    if (!r.f) return;
    r.f.checked = on;
    if (on && r.g.files.every((f) => f.checked)) {
      // Garde-fou : au moins un exemplaire de chaque groupe est conservé.
      r.f.checked = false;
      this.table.render();
      return this.updateSelection("⚠ Impossible de cocher tous les fichiers d'un groupe : au moins un exemplaire est toujours conservé.");
    }
    this.updateSelection();
  }

  private setChecks(choose: (g: FGroup) => FFile[]) {
    for (const g of this.result?.groups ?? []) {
      const pick = new Set(choose(g));
      for (const f of g.files) if (f.risk?.level !== "system") f.checked = pick.has(f);
    }
    this.table.render();
    this.updateSelection();
  }

  private keepOnly(r: Row) {
    for (const f of r.g.files) if (f.risk?.level !== "system") f.checked = f !== r.f;
    this.table.render();
    this.updateSelection();
  }

  private autoSelect() {
    const rule = this.rule.value;
    const prefixes = this.priority.value.split(";").map((p) => p.trim().replace(/^"|"$/g, "")).filter(Boolean)
      .map((p) => normcase(p).replace(/\\+$/, "") + "\\");
    if (rule === "priority" && !prefixes.length) return this.updateSelection("⚠ Indiquez au moins un dossier prioritaire.");
    let untouched = 0;
    const best = (files: FFile[], key: (f: FFile) => number[]) => files.reduce((a, b) => {
      const x = key(a), y = key(b);
      for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return y[i] > x[i] ? b : a;
      return a;
    });
    const keeper = (files: FFile[]): FFile | null => {
      if (rule === "resolution") return best(files, (f) => [(f.width ?? 0) * (f.height ?? 0), f.size, f.mtime]);
      if (rule === "largest") return best(files, (f) => [f.size, f.mtime]);
      if (rule === "newest") return best(files, (f) => [f.mtime, -f.path.length]);
      if (rule === "oldest") return best(files, (f) => [-f.mtime, -f.path.length]);
      for (const pre of prefixes) {
        const hits = files.filter((f) => normcase(f.path).startsWith(pre));
        if (hits.length) return best(hits, (f) => [f.mtime]);
      }
      untouched++;
      return null;
    };
    this.setChecks((g) => {
      const keep = keeper(g.files);
      return keep ? g.files.filter((f) => f !== keep && !f.guarded && !f.risk) : [];
    });
    if (untouched) this.updateSelection(`${this.selLabel.textContent} — ${humanCount(untouched)} groupe(s) sans fichier dans le chemin prioritaire, laissés décochés.`);
  }

  private async pickPriority() {
    const p = await open({ directory: true, title: "Dossier prioritaire", defaultPath: this.root || undefined });
    if (typeof p === "string") this.priority.value = this.priority.value.trim() ? `${this.priority.value.trim()};${p}` : p;
  }

  /** Vérifie qu'un exemplaire de chaque groupe reste sur le disque avant d'agir. */
  private async act(kind: Kind) {
    const groups = (this.result?.groups ?? []).filter((g) => g.files.some((f) => f.checked));
    const kept = groups.map((g) => g.files.filter((f) => !f.checked));
    const exist = await call<boolean[]>("exists", { paths: kept.flat().map((f) => f.path) });
    let i = 0;
    for (const k of kept) {
      const any = k.map(() => exist[i++]).some(Boolean);
      if (!any) return this.updateSelection("⚠ Un groupe n'a plus aucun exemplaire conservé sur le disque : action refusée. Relancez la recherche.");
    }
    const paths = groups.flatMap((g) => g.files.filter((f) => f.checked).map((f) => f.path));
    if (paths.length) this.request(kind, paths);
  }

  /** Retire les fichiers qui n'existent plus et les groupes devenus uniques. */
  async pruneMissing() {
    const r = this.result;
    if (!r) return;
    const all = r.groups.flatMap((g) => g.files);
    const exist = await call<boolean[]>("exists", { paths: all.map((f) => f.path) });
    const alive = new Set(all.filter((_, i) => exist[i]));
    for (const g of r.groups) g.files = g.files.filter((f) => alive.has(f));
    r.groups = r.groups.filter((g) => g.files.length > 1);
    this.fill();
    this.info.textContent = `${humanCount(r.groups.length)} groupes restants — ${humanSize(r.groups.reduce((s, g) => s + this.cfg.wasted(g), 0))} récupérables.`;
  }

  private async showPreview(f: FFile) {
    mount(this.previewImg, "…");
    try {
      const b64 = await call<string>("preview", { path: f.path });
      mount(this.previewImg, h("img", { src: `data:image/jpeg;base64,${b64}`, alt: "" }));
    } catch {
      mount(this.previewImg, "Aperçu indisponible.");
    }
    mount(this.previewInfo, h("b", {}, basename(f.path)), h("br"), `${f.width} × ${f.height} — ${humanSize(f.size)} — ${humanDate(f.mtime)}`,
      h("br"), `Ressemblance avec la meilleure version : ${resemblance(f.distance ?? 0)}`);
  }

  exportTable(): Table2 | null {
    if (!this.result) return null;
    return this.cfg.exportTable(this.result, new Set(this.checked().map((f) => f.path)));
  }

  get wasted(): number | null {
    return this.result ? this.result.groups.reduce((s, g) => s + this.cfg.wasted(g), 0) : null;
  }
}

// --- les trois vues ------------------------------------------------------------------------

const riskCls = (r: Row) => (r.f?.risk ? `risk-${r.f.risk.level}` : "");
const riskTip = (r: Row) => r.f ? (r.f.risk ? `${r.f.path}\n\n⚠ ${LABELS[r.f.risk.level]} : ${r.f.risk.reason}\nNe le supprimez que si vous savez ce que vous faites.` : r.f.path) : "";
const pathCol = (title: string, groupLabel: (g: FGroup) => string, thumb = false): Col<Row> => ({
  title, cls: riskCls, title_: riskTip,
  text: (r) => r.f
    ? h("span", { style: r.f.risk ? { color: levelColors(isDark())[r.f.risk.level] } : undefined },
      thumb && r.f.thumb ? h("img", { class: "thumb", src: `data:image/jpeg;base64,${r.f.thumb}`, alt: "" }) : null, r.f.path)
    : h("b", {}, groupLabel(r.g)),
});

/** Nombre de versions distinctes (lettres) d'un groupe « même nom ». */
const versions = (g: FGroup) => new Set(g.files.map((f) => f.version).filter((v) => v !== "?")).size;

const RULES: [string, string][] = [["Garder le plus récent", "newest"], ["Garder le plus ancien", "oldest"], ["Garder le chemin prioritaire", "priority"]];

export function duplicatesView(request: (k: Kind, paths: string[]) => void) {
  return new FinderView({
    rules: RULES, search: "Rechercher les doublons", minKo: 1024, wasted: dupWasted,
    cols: [pathCol("Fichier", (g) => `${g.files.length} × ${humanSize(g.size!)} — ${humanSize(dupWasted(g))} récupérables`),
      { title: "Taille", num: true, text: (r) => humanSize(r.f ? r.f.size : (r.g.size ?? 0) * r.g.files.length) },
      { title: "Modifié", num: true, text: (r) => (r.f ? humanDate(r.f.mtime) : "") }],
    start: (root, minSize, p) => run<Result>("find_duplicates", { root, minSize }, p),
    resultText: (r) => `${humanCount(r.groups.length)} groupes, ${humanCount(r.groups.reduce((s, g) => s + g.files.length, 0))} fichiers — ${humanSize(r.groups.reduce((s, g) => s + dupWasted(g), 0))} récupérables. ${humanCount(r.candidates)} candidats, ${humanSize(r.hashed_bytes)} lus en ${humanDuration(r.duration)}.`,
    exportTable: (r, checked) => [["Groupe", "Empreinte xxh3-128", "Chemin", "Octets", "Modifié", "Coché"],
      r.groups.flatMap((g, n) => g.files.map((f) => [n + 1, g.digest ?? "", f.path, f.size, iso(f.mtime), checked.has(f.path) ? "oui" : ""]))],
  }, request);
}

export function similarView(request: (k: Kind, paths: string[]) => void) {
  const level = h("select", { title: "Identiques à l'œil : même image recompressée ou redimensionnée.\nTrès proches : + petites retouches.\nProches : + variantes d'une même prise (rafales) — à vérifier." },
    ([["Identiques à l'œil", 0], ["Très proches", 5], ["Proches", 10]] as const).map(([l, b]) => h("option", { value: b }, l)));
  level.value = "5";
  return new FinderView({
    rules: [["Garder la meilleure résolution", "resolution"], ...RULES], search: "Rechercher les images similaires", minKo: 20, preview: true, wasted: imgWasted,
    options: () => [h("label", {}, "Ressemblance : ", level)],
    cols: [pathCol("Image", (g) => `${g.files.length} images — ${humanSize(imgWasted(g))} récupérables`, true),
      { title: "Résolution", num: true, text: (r) => (r.f ? (r.f.width ? `${r.f.width} × ${r.f.height}` : "?") : "") },
      { title: "Taille", num: true, text: (r) => humanSize(r.f ? r.f.size : r.g.files.reduce((s, f) => s + f.size, 0)) },
      { title: "Modifié", num: true, text: (r) => (r.f ? humanDate(r.f.mtime) : "") },
      { title: "Ressemblance", num: true, text: (r) => (r.f ? resemblance(r.f.distance ?? 0) : "") }],
    ordered: (g) => g.files,
    start: (root, minSize, p) => run<Result>("find_similar", { root, minSize, threshold: Number(level.value) }, p),
    resultText: (r) => `${humanCount(r.groups.length)} groupes, ${humanCount(r.groups.reduce((s, g) => s + g.files.length, 0))} images — ${humanSize(r.groups.reduce((s, g) => s + imgWasted(g), 0))} récupérables en ne gardant que la meilleure version. ${humanCount(r.candidates)} images examinées, ${humanCount(r.hashed)} empreintes calculées en ${humanDuration(r.duration)}.`,
    exportTable: (r, checked) => [["Groupe", "Chemin", "Largeur", "Hauteur", "Octets", "Modifié", "Écart (bits/64)", "Coché"],
      r.groups.flatMap((g, n) => g.files.map((f) => [n + 1, f.path, f.width ?? 0, f.height ?? 0, f.size, iso(f.mtime), f.distance ?? 0, checked.has(f.path) ? "oui" : ""]))],
  }, request);
}

export function sameNameView(request: (k: Kind, paths: string[]) => void) {
  const types = h("select", {}, h("option", { value: "1" }, "Documents, images, audio, vidéo, archives"), h("option", { value: "0" }, "Tous les types de fichiers"));
  const skipDev = h("input", { type: "checkbox", checked: true });
  return new FinderView({
    rules: [["Garder la version la plus récente", "newest"], ["Garder la plus ancienne", "oldest"], ["Garder la plus volumineuse", "largest"], ["Garder le chemin prioritaire", "priority"]],
    search: "Rechercher les homonymes", minKo: 1, wasted: nameWasted,
    options: () => [h("label", {}, "Types : ", types),
      h("label", { class: "check", title: "node_modules, .venv, .git, __pycache__, build, AppData, et tout dossier dont le nom commence par un point (.claude, .vscode…).\nLes noms génériques (readme, index.*, desktop.ini…) et ceux présents plus de 50 fois sont toujours ignorés." },
        skipDev, " Ignorer code, dépendances et dossiers d'applications")],
    cols: [pathCol("Fichier", (g) => `« ${g.name} » — ${g.files.length} fichiers, ${versions(g)} versions`),
      { title: "Version", text: (r) => (r.f ? h("span", { class: r.f.version === "A" ? "ok" : "", title: r.f.version === "?" ? "Contenu non vérifié (fichier en ligne, illisible ou modifié depuis le scan) : même taille qu'une autre version." : undefined }, r.f.version + (r.f.version === "A" ? " (récente)" : "")) : "") },
      { title: "Taille", num: true, text: (r) => humanSize(r.f ? r.f.size : r.g.files.reduce((s, f) => s + f.size, 0)) },
      { title: "Modifié", num: true, text: (r) => humanDate(r.f ? r.f.mtime : Math.max(...r.g.files.map((f) => f.mtime))) }],
    ordered: (g) => g.files,
    start: (root, minSize, p) => run<Result>("find_same_names", { root, minSize, personalOnly: types.value === "1", skipDev: skipDev.checked }, p),
    resultText: (r) => `${humanCount(r.groups.length)} noms avec des versions différentes, ${humanCount(r.groups.reduce((s, g) => s + g.files.length, 0))} fichiers. ${humanCount(r.candidates)} fichiers homonymes examinés, ${humanSize(r.hashed_bytes)} lus en ${humanDuration(r.duration)}.${r.skipped_generic ? ` ${humanCount(r.skipped_generic)} noms génériques ignorés.` : ""} Même lettre = copies identiques ; vérifiez avant de supprimer une version.`,
    exportTable: (r, checked) => [["Nom", "Version", "Chemin", "Octets", "Modifié", "Coché"],
      r.groups.flatMap((g) => g.files.map((f) => [g.name ?? "", f.version ?? "", f.path, f.size, iso(f.mtime), checked.has(f.path) ? "oui" : ""]))],
  }, request);
}
