// Statistiques : types, gros fichiers, anciens, vides, temp & caches, installeurs oubliés.
import { copyPath, reveal, type FileRow, type Stats } from "../lib/api";
import { Table, h, mount, showMenu, type Col } from "../lib/dom";
import { humanCount, humanDate, humanSize, iso, percent } from "../lib/format";
import { CATEGORIES, category } from "../lib/meta";
import { actionEntries, type Kind } from "./actions";

export const DEFAULT_OLD_DAYS = 365;
const INSTALLER_MIN_AGE_DAYS = 30;
const SECTIONS = ["Types de fichiers", "Plus gros fichiers", "Fichiers anciens", "Dossiers vides", "Temp & caches", "Installeurs oubliés"];

interface Row { cells: string[]; keys: (number | string)[]; path?: string; kids?: Row[] }
export type Table2 = [string[], (string | number)[][]];

export class StatsView {
  readonly el: HTMLElement;
  private menu = h("nav", { class: "sections" });
  private summary = h("p", { class: "summary" });
  private oldDays = h("input", { type: "number", min: 30, max: 3650, step: 30, value: String(DEFAULT_OLD_DAYS) });
  private oldBox = h("label", { class: "row" }, "Inutilisés depuis plus de ", this.oldDays, " jours");
  private host = h("div", { class: "fill" });
  private tables: Table<Row>[] = [];
  private summaries: string[] = SECTIONS.map(() => "");
  private counts: (number | null)[] = SECTIONS.map(() => null);
  private section = 0;
  result: Stats | null = null;
  onOldDays: () => void = () => {};

  constructor(request: (k: Kind, paths: string[]) => void) {
    const heads = [["Catégorie / extension", "Taille", "Fichiers", "% du total"], ["Fichier", "Taille", "Modifié"],
      ["Fichier", "Taille", "Dernière utilisation"], ["Dossier"], ["Élément", "Taille", "Fichiers"], ["Fichier", "Taille", "Modifié"]];
    this.tables = heads.map((cols) => new Table<Row>(cols.map((title, i): Col<Row> => ({
      title, num: i > 0, text: (r) => r.cells[i], key: (r) => r.keys[i], title_: i === 0 ? (r) => r.path ?? "" : undefined,
    })), {
      multi: true,
      children: (r) => r.kids,
      onContext: (r, sel, e) => {
        if (!r.path) return;
        const paths = sel.filter((s) => s.path).map((s) => s.path!);
        showMenu(e.clientX, e.clientY, [
          { label: "Afficher dans l'Explorateur", action: () => reveal(r.path!) },
          { label: "Copier le chemin", action: () => copyPath(r.path!) },
          ...actionEntries(paths.includes(r.path) ? paths : [r.path], request),
        ]);
      },
    }));
    this.oldDays.onchange = () => this.onOldDays();
    this.el = h("div", { class: "stats" }, this.menu, h("div", { class: "col" }, this.summary, this.oldBox, this.host));
    this.renderMenu();
    this.show(0);
  }

  get oldDaysValue() { return Math.min(3650, Math.max(30, Number(this.oldDays.value) || DEFAULT_OLD_DAYS)); }

  private renderMenu() {
    mount(this.menu, SECTIONS.map((s, i) => h("button", { class: i === this.section ? "on" : "", onclick: () => this.show(i) },
      this.counts[i] === null ? s : `${s} (${humanCount(this.counts[i]!)})`)));
  }

  private show(i: number) {
    this.section = i;
    this.renderMenu();
    mount(this.host, this.tables[i].el);
    this.summary.textContent = this.summaries[i];
    this.oldBox.hidden = i !== 2;
  }

  clear(message = "") {
    this.result = null;
    this.tables.forEach((t) => t.setRows([]));
    this.summaries = SECTIONS.map(() => message);
    this.counts = SECTIONS.map(() => null);
    this.show(this.section);
  }

  setLoading() { this.clear("Calcul des statistiques…"); }

  setResult(r: Stats) {
    this.result = r;
    const total = r.total_size || 1;
    const size = (s: number) => [humanSize(s), s] as const;
    const date = (t: number) => [humanDate(t), t] as const;
    const row = (cells: (readonly [string, number | string])[], path?: string, kids?: Row[]): Row =>
      ({ cells: cells.map((c) => c[0]), keys: cells.map((c) => c[1]), path, kids });
    const fill = (i: number, rows: Row[], summary: string, sortCol = 1) => {
      this.tables[i].setRows(rows, { col: sortCol, desc: sortCol > 0 });
      this.summaries[i] = summary;
      this.counts[i] = rows.length;
    };
    const byCat = new Map(CATEGORIES.map((c) => [c, [] as [string, number, number][]]));
    for (const e of r.by_ext) byCat.get(category(e[0]))!.push(e);
    const catRows = r.by_category.filter(([, , n]) => n).map(([cat, s, n]) => row(
      [[cat, cat], size(s), [humanCount(n), n], [percent(s, total), s]], undefined,
      byCat.get(cat)!.map(([ext, es, en]) => row([[ext || "(sans extension)", ext], size(es), [humanCount(en), en], [percent(es, total), es]]))));
    fill(0, catRows, `${humanCount(r.file_count)} fichiers — ${humanSize(r.total_size)} — ${humanCount(r.by_ext.length)} extensions`);
    const files = (list: FileRow[], used = false) => list.map((f) => row([[f[0], f[0]], size(f[1]), date(used ? f[3] : f[2])], f[0]));
    fill(1, files(r.largest), `Les ${humanCount(r.largest.length)} plus gros fichiers (${humanSize(r.largest.reduce((s, f) => s + f[1], 0))} au total).`);
    fill(2, files(r.old, true), `${humanCount(r.old_total[1])} fichiers (${humanSize(r.old_total[0])}) ni ouverts ni modifiés depuis plus de ${r.old_days} jours — ${r.old.length} plus gros affichés. Windows ne met pas toujours à jour la date d'accès : la date la plus récente entre accès et modification est utilisée.`);
    fill(3, r.empty_dirs.map((p) => row([[p, p]], p)), "Dossiers ne contenant aucun fichier (seul le plus haut de chaque branche vide est listé ; les dossiers dont une partie est exclue ou inaccessible sont ignorés).", 0);
    const temp = [...r.temp_dirs.map(([p, s, n]) => row([[p + "\\", p], size(s), [humanCount(n), n]], p)),
      ...r.temp_files.map((f) => row([[f[0], f[0]], size(f[1]), ["1", 1]], f[0]))];
    const tempSize = r.temp_dirs.reduce((s, d) => s + d[1], 0) + r.temp_files.reduce((s, f) => s + f[1], 0);
    fill(4, temp, `${humanSize(tempSize)} dans des dossiers temporaires/caches et fichiers .tmp/.dmp/~$. Ces caches sont en général régénérés automatiquement, mais vérifiez avant de les supprimer.`);
    fill(5, files(r.installers), `${humanSize(r.installers.reduce((s, f) => s + f[1], 0))} d'installeurs (.msi, .msix, setup*.exe, exécutables et ISO dans Téléchargements) datant de plus de ${INSTALLER_MIN_AGE_DAYS} jours.`);
    this.show(this.section);
  }

  /** Section affichée, en valeurs brutes (octets, dates ISO). */
  exportTable(): Table2 | null {
    const r = this.result;
    if (!r) return null;
    const s = this.section;
    if (s === 0) return [["Catégorie", "Extension", "Octets", "Fichiers"], r.by_ext.map(([e, sz, n]) => [category(e), e, sz, n])];
    if (s === 1 || s === 2 || s === 5) {
      const files = s === 1 ? r.largest : s === 2 ? r.old : r.installers;
      return [["Chemin", "Octets", "Modifié", "Dernière utilisation"], files.map((f) => [f[0], f[1], iso(f[2]), iso(f[3])])];
    }
    if (s === 3) return [["Dossier"], r.empty_dirs.map((p) => [p])];
    return [["Chemin", "Type", "Octets", "Fichiers"], [...r.temp_dirs.map(([p, sz, n]) => [p, "dossier", sz, n]), ...r.temp_files.map((f) => [f[0], "fichier", f[1], 1])]];
  }
}
