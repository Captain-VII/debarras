// Recherche : filtres combinables sur le cache, résultats triables, actions.
import { copyPath, reveal, run } from "../lib/api";
import { Table, h, showMenu } from "../lib/dom";
import { dirname, humanCount, humanDate, humanSize, iso } from "../lib/format";
import { CATEGORIES } from "../lib/meta";
import { actionEntries, type Kind } from "./actions";
import type { Table2 } from "./stats";

const MB = 1024 * 1024;
// (libellé, modifiés depuis moins de N jours, modifiés il y a plus de N jours)
const DATE_FILTERS: [string, number, number][] = [["Toutes dates", 0, 0], ["Moins de 7 jours", 7, 0], ["Moins de 30 jours", 30, 0],
  ["Moins d'un an", 365, 0], ["Plus d'un an", 0, 365], ["Plus de 2 ans", 0, 730], ["Plus de 5 ans", 0, 1825]];

type Hit = [string, string, number, number]; // chemin, nom, taille, date

export class SearchView {
  readonly el: HTMLElement;
  readonly text = h("input", { type: "search", placeholder: "Nom contient…  (jokers acceptés : *.iso, rapport_??.pdf)" });
  private category = h("select", {}, h("option", { value: "" }, "Tous types"), CATEGORIES.map((c) => h("option", { value: c }, c)));
  private exts = h("input", { type: "text", placeholder: ".pdf, .docx" });
  private minMb = h("input", { type: "number", min: 0, step: 0.1, placeholder: "—" });
  private maxMb = h("input", { type: "number", min: 0, step: 0.1, placeholder: "—" });
  private date = h("select", {}, DATE_FILTERS.map(([l], i) => h("option", { value: i }, l)));
  private summary = h("p", { class: "summary" });
  private table: Table<Hit>;
  private root = "";
  private hits: Hit[] = [];
  private request = 0;
  private timer = 0;

  constructor(showInTreemap: (path: string, dir: boolean) => void, request: (k: Kind, paths: string[]) => void) {
    this.table = new Table<Hit>([
      { title: "Nom", text: (r) => r[1], key: (r) => r[1].toLowerCase(), title_: (r) => r[0], width: "32%" },
      { title: "Dossier", text: (r) => dirname(r[0]), key: (r) => r[0].toLowerCase() },
      { title: "Taille", num: true, text: (r) => humanSize(r[2]), key: (r) => r[2], width: "100px" },
      { title: "Modifié", num: true, text: (r) => humanDate(r[3]), key: (r) => r[3], width: "130px" },
    ], {
      multi: true,
      onContext: (r, sel, e) => {
        const paths = sel.map((s) => s[0]);
        showMenu(e.clientX, e.clientY, [
          { label: "Afficher dans l'Explorateur", action: () => reveal(r[0]) },
          { label: "Copier le chemin", action: () => copyPath(r[0]) },
          { label: "Afficher dans le treemap", action: () => showInTreemap(r[0], false) },
          ...actionEntries(paths.includes(r[0]) ? paths : [r[0]], request),
        ]);
      },
    });
    const later = () => { clearTimeout(this.timer); this.timer = setTimeout(() => this.run(), 300); };
    for (const w of [this.text, this.exts, this.minMb, this.maxMb]) w.addEventListener("input", later);
    for (const w of [this.category, this.date]) w.addEventListener("change", later);
    this.el = h("div", { class: "search" },
      h("div", { class: "filters" },
        h("label", { class: "wide" }, "Nom : ", this.text), h("label", {}, "Type : ", this.category), h("label", {}, "Extensions : ", this.exts),
        h("label", {}, "Taille min : ", this.minMb, " Mo"), h("label", {}, "max : ", this.maxMb, " Mo"), h("label", {}, "Modifié : ", this.date),
        h("button", { onclick: () => this.reset() }, "Effacer les filtres")),
      this.summary, this.table.el);
    this.setScan("");
  }

  setScan(root: string) {
    this.root = root;
    void this.run();
  }

  focus() {
    this.text.focus();
    this.text.select();
  }

  private reset() {
    this.text.value = this.exts.value = this.minMb.value = this.maxMb.value = "";
    this.category.value = "";
    this.date.value = "0";
    void this.run();
  }

  private async run() {
    const req = ++this.request;
    this.hits = [];
    this.table.setRows([]);
    if (!this.root) return void (this.summary.textContent = "Aucun scan chargé.");
    const [, newer, older] = DATE_FILTERS[Number(this.date.value)];
    const q = {
      text: this.text.value, category: this.category.value || null,
      exts: this.exts.value.replace(/;/g, ",").split(",").map((e) => e.trim()).filter(Boolean),
      min_size: Math.trunc((Number(this.minMb.value) || 0) * MB), max_size: Math.trunc((Number(this.maxMb.value) || 0) * MB),
      newer_than_days: newer, older_than_days: older,
    };
    if (!q.text.trim() && !q.category && !q.exts.length && !q.min_size && !q.max_size && !newer && !older) {
      return void (this.summary.textContent = "Saisissez un nom ou choisissez un filtre.");
    }
    this.summary.textContent = "Recherche…";
    try {
      const r = await run<{ hits: Hit[]; count: number; total_size: number; max: number }>("search_files", { root: this.root, q }).promise;
      if (req !== this.request) return; // dépassée par une frappe plus récente
      this.hits = r.hits;
      this.table.setRows(r.hits, { col: 2, desc: true });
      const more = r.count > r.max ? ` — ${humanCount(r.max)} plus gros affichés` : "";
      this.summary.textContent = `${humanCount(r.count)} fichier(s) — ${humanSize(r.total_size)}${more}`;
    } catch (e) {
      if (req === this.request) this.summary.textContent = String(e);
    }
  }

  exportTable(): Table2 | null {
    return this.hits.length ? [["Chemin", "Nom", "Octets", "Modifié"], this.hits.map((x) => [x[0], x[1], x[2], iso(x[3])])] : null;
  }
}
