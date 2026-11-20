// Arborescence triée par taille, chargée à la demande depuis le cache.
import { call, copyPath, reveal } from "../lib/api";
import { Table, h, showMenu } from "../lib/dom";
import { basename, humanCount, humanDate, humanSize } from "../lib/format";
import { actionEntries, type Kind } from "./actions";

interface Node { path: string; name: string; dir: boolean; size: number; count: number; mtime: number; parent: Node | null }
interface Listing { info: [number, number] | null; dirs: [string, number, number][]; files: [string, string, number, number][] }

const pct = (n: Node) => (n.parent && n.parent.size > 0 ? (100 * n.size) / n.parent.size : 100);

export class TreeView {
  readonly el: HTMLElement;
  private table: Table<Node>;
  private scanId = 0;

  constructor(showInTreemap: (path: string, dir: boolean) => void, request: (k: Kind, paths: string[]) => void) {
    this.table = new Table<Node>([
      { title: "Nom", text: (n) => h("span", {}, h("span", { class: "ico" }, n.dir ? "📁" : "📄"), n.name), key: (n) => `${n.dir ? 0 : 1}${n.name.toLowerCase()}`, title_: (n) => n.path },
      { title: "Taille", num: true, text: (n) => humanSize(n.size), key: (n) => n.size, width: "90px" },
      { title: "% parent", text: (n) => h("div", { class: "pct" }, h("i", { style: { width: `${Math.min(pct(n), 100)}%` } }), h("span", {}, `${pct(n).toFixed(1).replace(".", ",")} %`)), key: (n) => n.size, width: "120px" },
      { title: "Fichiers", num: true, text: (n) => (n.dir ? humanCount(n.count) : ""), key: (n) => n.count, width: "90px" },
      { title: "Modifié", num: true, text: (n) => (n.mtime ? humanDate(n.mtime) : ""), key: (n) => n.mtime, width: "130px" },
    ], {
      multi: true,
      hasChildren: (n) => n.dir,
      load: (n) => this.load(n),
      onContext: (n, sel, e) => {
        const paths = sel.length && sel.includes(n) ? sel.map((s) => s.path) : [n.path];
        showMenu(e.clientX, e.clientY, [
          { label: "Afficher dans l'Explorateur", action: () => reveal(n.path) },
          { label: "Copier le chemin", action: () => copyPath(n.path) },
          { label: "Afficher dans le treemap", action: () => showInTreemap(n.path, n.dir) },
          ...actionEntries(paths, request),
        ]);
      },
    });
    this.el = h("div", { class: "fill" }, this.table.el);
  }

  private async load(n: Node): Promise<Node[]> {
    const l = await call<Listing>("list_dir", { scanId: this.scanId, path: n.path });
    return [
      ...l.dirs.map(([path, size, count]) => ({ path, name: basename(path), dir: true, size, count, mtime: 0, parent: n })),
      ...l.files.map(([path, name, size, mtime]) => ({ path, name, dir: false, size, count: 1, mtime, parent: n })),
    ];
  }

  async loadScan(scanId: number, root: string, size: number, count: number) {
    this.scanId = scanId;
    const top: Node = { path: root, name: root, dir: true, size, count, mtime: 0, parent: null };
    this.table.setRows([top], { col: 1, desc: true });
    await this.table.toggle(top, true);
  }

  clear() {
    this.scanId = 0;
    this.table.setRows([]);
  }
}
