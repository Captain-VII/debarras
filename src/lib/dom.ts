// Petits outils DOM : création d'éléments, menus contextuels, fenêtres modales, tableaux.

export type Child = Node | string | number | null | undefined | false | Child[];
type Props = Record<string, unknown>;

/** `h("button", { class: "btn", onclick: fn }, "Texte")`. */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K, props: Props = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value as EventListener);
    else if (key === "class") el.className = String(value);
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "value" || key === "checked" || key === "disabled" || key === "indeterminate") (el as any)[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(parent: Node, children: Child[]) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(parent, c);
    else parent.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function mount(el: Element, ...children: Child[]) {
  el.replaceChildren();
  append(el, children);
}

/** Texte riche minimal : **gras**, retours à la ligne. Le reste est échappé. */
export function rich(text: string): DocumentFragment {
  const frag = document.createDocumentFragment();
  text.split("\n").forEach((line, i) => {
    if (i) frag.append(h("br"));
    line.split(/\*\*(.+?)\*\*/).forEach((part, j) => frag.append(j % 2 ? h("b", {}, part) : part));
  });
  return frag;
}

// --- menu contextuel -------------------------------------------------------------------

export interface MenuItem { label: string; action?: () => void; disabled?: boolean; separator?: boolean }
export const SEP: MenuItem = { label: "", separator: true };

let openMenu: HTMLElement | null = null;
export function closeMenu() {
  openMenu?.remove();
  openMenu = null;
}
addEventListener("mousedown", (e) => { if (openMenu && !openMenu.contains(e.target as Node)) closeMenu(); });
addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
addEventListener("blur", closeMenu);

/** Menu flottant à la position (x, y). */
export function showMenu(x: number, y: number, items: MenuItem[]) {
  closeMenu();
  const cleaned = items.filter((it, i, a) => !(it.separator && (i === 0 || a[i - 1].separator || i === a.length - 1)));
  if (!cleaned.length) return;
  const menu = h("div", { class: "menu", role: "menu" },
    cleaned.map((it) => it.separator ? h("hr") : h("button", {
      role: "menuitem", disabled: it.disabled || !it.action,
      onclick: () => { closeMenu(); it.action?.(); },
    }, it.label)));
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - r.width - 4)}px`;
  menu.style.top = `${Math.min(y, innerHeight - r.height - 4)}px`;
  openMenu = menu;
  (menu.querySelector("button:not([disabled])") as HTMLElement | null)?.focus();
}

// --- fenêtres modales ------------------------------------------------------------------

/** Fenêtre modale ; `build(close)` fournit le contenu. Résout avec la valeur de `close`. */
export function modal<T>(title: string, build: (close: (v?: T) => void) => Child, cls = ""): Promise<T | undefined> {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: `modal ${cls}` });
    const close = (v?: T) => { dlg.close(); dlg.remove(); resolve(v); };
    mount(dlg, h("header", {}, h("h2", {}, title), h("button", { class: "icon", title: "Fermer", onclick: () => close() }, "✕")),
      h("div", { class: "body" }, build(close)));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

/** Message simple, bouton OK. */
export function info(title: string, text: string, warn = false): Promise<void> {
  return modal<void>(title, (close) => [
    h("p", { class: warn ? "msg warn" : "msg" }, rich(text)),
    h("footer", {}, h("button", { class: "primary", onclick: () => close() }, "OK")),
  ]).then(() => {});
}

/** Question Oui / Non. */
export function ask(title: string, text: string, yes = "Oui", no = "Non"): Promise<boolean> {
  return modal<boolean>(title, (close) => [
    h("p", { class: "msg" }, rich(text)),
    h("footer", {}, h("button", { onclick: () => close(false) }, no), h("button", { class: "primary", onclick: () => close(true) }, yes)),
  ]).then((v) => v === true);
}

// --- tableau arborescent ---------------------------------------------------------------

export interface Col<T> {
  title: string;
  text: (r: T) => string | Node;
  /** Clé de tri ; colonne non triable sans elle. */
  key?: (r: T) => number | string;
  num?: boolean;
  width?: string;
  title_?: (r: T) => string;
  cls?: (r: T) => string;
}

export interface TableOptions<T> {
  children?: (r: T) => T[] | undefined;
  /** Enfants chargés à la demande (`hasChildren` dit s'il y en a). */
  load?: (r: T) => Promise<T[]>;
  hasChildren?: (r: T) => boolean;
  expanded?: (r: T) => boolean;
  check?: { get: (r: T) => boolean | null; set: (r: T, on: boolean) => void };
  multi?: boolean;
  onContext?: (r: T, selected: T[], e: MouseEvent) => void;
  onActivate?: (r: T) => void;
  onSelect?: (r: T | null) => void;
  rowClass?: (r: T) => string;
  empty?: string;
  /** Lignes affichées au plus (le reste est signalé). */
  limit?: number;
}

/** Tableau triable avec lignes dépliables, cases à cocher et sélection multiple. */
export class Table<T extends object> {
  readonly el: HTMLElement;
  private body: HTMLTableSectionElement;
  private rows: T[] = [];
  private open = new WeakSet<T>();
  private kids = new WeakMap<T, T[]>();
  private sel = new Set<T>();
  private anchor: T | null = null;
  private sort: { col: number; desc: boolean } | null = null;
  private visible: T[] = [];

  constructor(private cols: Col<T>[], private opt: TableOptions<T> = {}) {
    const head = h("tr", {}, opt.check ? h("th", { class: "chk" }) : null, cols.map((c, i) => {
      const th = h("th", { class: c.num ? "n" : "", style: c.width ? { width: c.width } : undefined }, c.title);
      if (c.key) {
        th.classList.add("sortable");
        th.onclick = () => this.sortBy(i, this.sort?.col === i ? !this.sort.desc : !!c.num);
      }
      return th;
    }));
    this.body = h("tbody");
    this.el = h("div", { class: "table" }, h("table", {}, h("thead", {}, head), this.body));
    this.body.addEventListener("contextmenu", (e) => this.context(e));
  }

  setRows(rows: T[], sort?: { col: number; desc: boolean }) {
    this.rows = rows;
    this.sel.clear();
    this.anchor = null;
    if (sort) this.sort = sort;
    for (const r of rows) if (this.opt.expanded?.(r)) this.open.add(r);
    this.render();
  }

  get all(): T[] { return this.rows; }
  selected(): T[] { return this.visible.filter((r) => this.sel.has(r)); }

  sortBy(col: number, desc: boolean) {
    this.sort = { col, desc };
    this.render();
  }

  private sorted(list: T[]): T[] {
    const s = this.sort;
    const key = s && this.cols[s.col]?.key;
    if (!s || !key) return list;
    return [...list].sort((a, b) => {
      const x = key(a), y = key(b);
      const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), "fr", { sensitivity: "base" });
      return s.desc ? -c : c;
    });
  }

  private childrenOf(r: T): T[] | undefined {
    return this.opt.children?.(r) ?? this.kids.get(r);
  }

  private canOpen(r: T): boolean {
    const k = this.childrenOf(r);
    if (k) return k.length > 0;
    return !!this.opt.load && !!this.opt.hasChildren?.(r);
  }

  async toggle(r: T, on = !this.open.has(r)) {
    if (on && this.opt.load && !this.childrenOf(r)) this.kids.set(r, await this.opt.load(r));
    on ? this.open.add(r) : this.open.delete(r);
    this.render();
  }

  render() {
    const frag = document.createDocumentFragment();
    this.visible = [];
    const limit = this.opt.limit ?? 20000;
    const walk = (list: T[], depth: number) => {
      for (const r of this.sorted(list)) {
        if (this.visible.length >= limit) return;
        this.visible.push(r);
        frag.append(this.row(r, depth));
        if (this.open.has(r)) walk(this.childrenOf(r) ?? [], depth + 1);
      }
    };
    walk(this.rows, 0);
    if (!this.rows.length && this.opt.empty) {
      frag.append(h("tr", { class: "empty" }, h("td", { colspan: this.cols.length + (this.opt.check ? 1 : 0) }, this.opt.empty)));
    } else if (this.visible.length >= limit) {
      frag.append(h("tr", { class: "empty" }, h("td", { colspan: this.cols.length + (this.opt.check ? 1 : 0) }, "… affichage limité")));
    }
    this.body.replaceChildren(frag);
  }

  private row(r: T, depth: number): HTMLTableRowElement {
    const tr = h("tr", { class: `${this.opt.rowClass?.(r) ?? ""}${this.sel.has(r) ? " sel" : ""}` });
    const chk = this.opt.check;
    if (chk) {
      const state = chk.get(r);
      tr.append(h("td", { class: "chk" }, state === null ? null : h("input", {
        type: "checkbox", checked: state, onclick: (e: Event) => e.stopPropagation(),
        onchange: (e: Event) => chk.set(r, (e.target as HTMLInputElement).checked),
      })));
    }
    this.cols.forEach((c, i) => {
      const td = h("td", { class: c.num ? "n" : c.cls?.(r) ?? "" }, c.text(r));
      if (c.cls && c.num) td.classList.add(c.cls(r));
      const tip = c.title_?.(r);
      if (tip) td.title = tip;
      if (i === 0) {
        td.style.paddingLeft = `${6 + depth * 18}px`;
        const can = this.canOpen(r);
        td.prepend(h("span", {
          class: `twisty${can ? "" : " none"}`,
          onclick: (e: Event) => { e.stopPropagation(); if (can) void this.toggle(r); },
        }, can ? (this.open.has(r) ? "▾" : "▸") : ""));
      }
      tr.append(td);
    });
    tr.onclick = (e) => this.click(r, e);
    tr.ondblclick = () => (this.canOpen(r) && !this.opt.onActivate ? void this.toggle(r) : this.opt.onActivate?.(r));
    (tr as any)._row = r;
    return tr;
  }

  private click(r: T, e: MouseEvent) {
    if (this.opt.multi && e.shiftKey && this.anchor) {
      const a = this.visible.indexOf(this.anchor), b = this.visible.indexOf(r);
      this.sel = new Set(this.visible.slice(Math.min(a, b), Math.max(a, b) + 1));
    } else if (this.opt.multi && e.ctrlKey) {
      this.sel.has(r) ? this.sel.delete(r) : this.sel.add(r);
      this.anchor = r;
    } else {
      this.sel = new Set([r]);
      this.anchor = r;
    }
    for (const tr of this.body.children) tr.classList.toggle("sel", this.sel.has((tr as any)._row));
    this.opt.onSelect?.(r);
  }

  private context(e: MouseEvent) {
    const tr = (e.target as HTMLElement).closest("tr") as any;
    const r: T | undefined = tr?._row;
    if (!r || !this.opt.onContext) return;
    e.preventDefault();
    if (!this.sel.has(r)) this.click(r, new MouseEvent("click"));
    this.opt.onContext(r, this.selected(), e);
  }
}

/** Barre de progression : `null` = indéterminée. */
export function progressBar(): { el: HTMLElement; set: (frac: number | null) => void } {
  const bar = h("div", { class: "bar" });
  const el = h("div", { class: "progress", hidden: true }, bar);
  return {
    el,
    set(frac) {
      el.classList.toggle("busy", frac === null);
      bar.style.width = frac === null ? "" : `${Math.round(Math.min(Math.max(frac, 0), 1) * 1000) / 10}%`;
    },
  };
}
