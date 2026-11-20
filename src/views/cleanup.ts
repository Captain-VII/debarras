// Nettoyage guidé : caches des navigateurs, fichiers temporaires, Windows Update…
import { call, reveal, run, type Target, type Task } from "../lib/api";
import { Table, h } from "../lib/dom";
import { humanCount, humanSize } from "../lib/format";

const MAX_CHILDREN = 300;
type Row = { t: Target; path?: string; desc?: boolean; more?: number };

export class CleanupView {
  readonly el: HTMLElement;
  private table: Table<Row>;
  private analyzeBtn = h("button", { onclick: () => this.analyze() }, "Analyser");
  private status = h("span", { class: "grow" });
  private cleanBtn = h("button", { class: "primary", disabled: true, onclick: () => this.request() }, "Mettre la sélection à la corbeille…");
  private task: Task<Target[]> | null = null;
  private checked = new Set<string>();
  private targets: Target[] = [];
  analyzed = false;
  onTargets: (t: Target[]) => void = () => {};
  onClean: (paths: string[], sizes: Record<string, number>) => void = () => {};

  constructor() {
    const kids = new WeakMap<Row, Row[]>();
    this.table = new Table<Row>([
      {
        title: "Élément",
        text: (r) => r.desc ? h("span", { class: "muted wrap" }, r.t.description, r.t.note ? [h("br"), `⚠ ${r.t.note}`] : null)
          : r.more ? h("span", { class: "muted" }, `… et ${humanCount(r.more)} autres (traités avec la catégorie)`)
          : r.path ? r.path : h("b", {}, r.t.title),
        title_: (r) => (r.path ? `${r.path}\nDouble-clic : afficher dans l'Explorateur` : r.desc || !r.path ? r.t.description + (r.t.note ? `\n\n⚠ ${r.t.note}` : "") : ""),
      },
      { title: "Taille", num: true, text: (r) => this.sizeText(r) },
      { title: "Fichiers", num: true, text: (r) => (!r.path && !r.desc && !r.more && r.t.size_known ? humanCount(r.t.count) : "") },
      {
        title: "", text: (r) => !r.path && !r.desc && !r.more && (r.t.kind === "windows" || r.t.kind === "recycle")
          ? h("button", { onclick: () => call(r.t.kind === "windows" ? "open_windows_cleanup" : "open_recycle_bin").catch(() => {}) },
            r.t.kind === "windows" ? "Ouvrir le Nettoyage de disque…" : "Ouvrir la corbeille") : "",
      },
    ], {
      children: (r) => {
        if (r.path || r.desc || r.more) return undefined;
        if (!kids.has(r)) {
          const sizes = new Map(r.t.sizes);
          const list: Row[] = [{ t: r.t, desc: true }];
          if (r.t.kind === "trash") {
            list.push(...[...r.t.paths].sort((a, b) => (sizes.get(b) ?? 0) - (sizes.get(a) ?? 0)).slice(0, MAX_CHILDREN).map((path) => ({ t: r.t, path })));
            if (r.t.paths.length > MAX_CHILDREN) list.push({ t: r.t, more: r.t.paths.length - MAX_CHILDREN });
          }
          kids.set(r, list);
        }
        return kids.get(r);
      },
      expanded: (r) => r.t.kind !== "trash" || !!r.t.note,
      check: {
        get: (r) => {
          if (r.t.kind !== "trash" || !r.t.paths.length || r.desc || r.more) return null;
          if (r.path) return this.checked.has(r.path);
          return r.t.paths.every((p) => this.checked.has(p));
        },
        set: (r, on) => {
          for (const p of r.path ? [r.path] : r.t.paths) on ? this.checked.add(p) : this.checked.delete(p);
          this.table.render();
          this.updateTotal();
        },
      },
      onActivate: (r) => r.path && reveal(r.path),
    });
    this.el = h("div", { class: "cleanup" },
      h("p", { class: "summary" }, h("b", {}, "Nettoyage guidé"), " — uniquement des emplacements recréés automatiquement par Windows et vos logiciels. Les éléments cochés vont à la ",
        h("b", {}, "corbeille"), " (annulable) : videz-la ensuite depuis Windows pour libérer l'espace."),
      this.table.el, h("div", { class: "toolbar" }, this.analyzeBtn, this.status, this.cleanBtn));
  }

  private sizeText(r: Row): string {
    const sizes = new Map(r.t.sizes);
    if (r.path) return humanSize(sizes.get(r.path) ?? 0);
    if (r.desc || r.more) return "";
    if (r.t.kind === "trash" && !r.t.paths.length) return "0 o";
    return r.t.size_known ? humanSize(r.t.size) : "?";
  }

  async analyze() {
    if (this.task) return;
    this.analyzed = true;
    this.analyzeBtn.disabled = this.cleanBtn.disabled = true;
    this.status.textContent = "Mesure des emplacements…";
    this.task = run<Target[]>("cleanup_analyze");
    try {
      this.targets = await this.task.promise;
      this.checked = new Set(this.targets.filter((t) => t.kind === "trash" && t.recommended).flatMap((t) => t.paths));
      this.table.setRows(this.targets.map((t) => ({ t })));
      this.updateTotal();
      this.onTargets(this.targets);
    } catch (e) {
      this.status.textContent = `Analyse impossible : ${e}`;
    } finally {
      this.task = null;
      this.analyzeBtn.disabled = false;
    }
  }

  private selection(): [string[], Record<string, number>] {
    const paths: string[] = [], sizes: Record<string, number> = {};
    for (const t of this.targets) {
      if (t.kind !== "trash") continue;
      const m = new Map(t.sizes);
      // Catégorie entière cochée : tout, y compris au-delà de la liste ; sinon les lignes cochées.
      const listed = t.paths.every((p) => this.checked.has(p)) ? t.paths
        : [...t.paths].sort((a, b) => (m.get(b) ?? 0) - (m.get(a) ?? 0)).slice(0, MAX_CHILDREN).filter((p) => this.checked.has(p));
      for (const p of listed) { paths.push(p); sizes[p] = m.get(p) ?? 0; }
    }
    return [paths, sizes];
  }

  private updateTotal() {
    const [paths, sizes] = this.selection();
    const total = Object.values(sizes).reduce((a, b) => a + b, 0);
    this.status.textContent = paths.length ? `Sélection : ${humanSize(total)} (${humanCount(paths.length)} éléments)` : "Rien de sélectionné.";
    this.cleanBtn.disabled = !paths.length;
  }

  private request() {
    const [paths, sizes] = this.selection();
    if (paths.length) this.onClean(paths, sizes);
  }

  shutdown() { this.task?.cancel(); }
}

/** Ce que « Nettoyer automatiquement » met à la corbeille : catégories recommandées. */
export function autoSelection(targets: Target[]): [string[], Record<string, number>] {
  const paths: string[] = [], sizes: Record<string, number> = {};
  for (const t of targets.filter((t) => t.kind === "trash" && t.recommended)) {
    paths.push(...t.paths);
    for (const [p, s] of t.sizes) sizes[p] = s;
  }
  return [paths, sizes];
}
