// Fenêtre principale : dossier, scan, onglets, menus (port de ui/main_window.py).
import "./style.css";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open, save } from "@tauri-apps/plugin-dialog";
import { call, run, type ActionRecord, type Diff, type Drive, type Init, type Scan, type Settings, type Stats, type Summary, type Task, type TreeNode } from "./lib/api";
import { ask, h, info, mount, progressBar, showMenu, type MenuItem } from "./lib/dom";
import { basename, humanCount, humanDate, humanDuration, humanSize } from "./lib/format";
import { Actions, actionEntries, type Kind } from "./views/actions";
import { Charts } from "./views/charts";
import { CleanupView, autoSelection } from "./views/cleanup";
import { htmlReport, toCsv } from "./views/export";
import { duplicatesView, sameNameView, similarView } from "./views/finder";
import { HistoryView } from "./views/history";
import { Home } from "./views/home";
import { SearchView } from "./views/search";
import { THEMES, applyTheme, editSettings } from "./views/settings";
import { StatsView } from "./views/stats";
import { TreeView } from "./views/tree";
import { Updates } from "./views/update";

const boot = await call<Init>("init");
let settings: Settings = boot.settings;
applyTheme(settings.theme);

const actions = new Actions();
let current: Scan | null = null;
let stats: Stats | null = null;
let scanner: Task<{ root: string; cancelled: boolean; duration: number; file_count: number }> | null = null;
let safetyTask: Task<Summary | null> | null = null;
let autoSel: [string[], Record<string, number>] = [[], {}];
let drives: Drive[] = [];

/** Action demandée depuis une vue : tailles lues dans le scan affiché. */
async function request(kind: Kind, paths: string[]) {
  const sizes = await call<Record<string, number>>("sizes_of", { scanId: current?.id ?? 0, paths });
  await actions.request(kind, paths, sizes);
}

// --- vues --------------------------------------------------------------------------------

const home = new Home({
  openDrive: (root) => void openDrive(root),
  scanDrive: (root) => { setRoot(root); void startScan(); },
  openCleanup: () => select("cleanup"),
  openHistory: () => select("history"),
  autoClean: () => { if (autoSel[0].length) void actions.request("trash", ...autoSel); },
  relaunchAdmin: () => void relaunchAdmin(),
  trash: (path) => void request("trash", [path]),
});
home.treemap.onContext = (e: TreeNode) => [
  ...(e.d ? [{ label: "Zoomer ici", action: () => void home.treemap.navigate(e.p) }] : []),
  { label: "Afficher dans l'Explorateur", action: () => void call("reveal", { path: e.p }) },
  { label: "Copier le chemin", action: () => void navigator.clipboard.writeText(e.p) },
  ...actionEntries([e.p], (k, p) => void request(k, p), { level: e.l, reason: e.r }),
];
const showInTreemap = (path: string, dir: boolean) => { void home.treemap.showPath(path, dir); select("home"); };
const req = (k: Kind, p: string[]) => void request(k, p);
const tree = new TreeView(showInTreemap, req);
const statsView = new StatsView(req);
statsView.onOldDays = () => void computeStats();
const charts = new Charts();
const dups = duplicatesView(req);
const similar = similarView(req);
const samename = sameNameView(req);
const cleanup = new CleanupView();
cleanup.onTargets = (t) => { autoSel = autoSelection(t); home.setAutoClean(Object.values(autoSel[1]).reduce((a, b) => a + b, 0)); };
cleanup.onClean = (paths, sizes) => void actions.request("trash", paths, sizes);
const history = new HistoryView();
history.onDiff = (d: Diff) => { if (current && d.new.id === current.id) home.setGrowth(d); };
const search = new SearchView(showInTreemap, req);

const TABS = [
  ["home", "Accueil", home.el], ["tree", "Arborescence", tree.el], ["stats", "Statistiques", statsView.el],
  ["charts", "Graphiques", charts.el], ["dups", "Doublons", dups.el], ["similar", "Images similaires", similar.el],
  ["samename", "Même nom", samename.el], ["cleanup", "Nettoyage", cleanup.el], ["history", "Historique", history.el],
  ["search", "Recherche", search.el],
] as const;
type TabKey = (typeof TABS)[number][0];
const SIMPLE: TabKey[] = ["home", "cleanup"];
let tab: TabKey = "home";

// --- coquille ------------------------------------------------------------------------------

const pathInput = h("input", { type: "text", list: "roots", placeholder: "Glissez un dossier ici ou choisissez-en un…", class: "grow" });
const rootsList = h("datalist", { id: "roots" });
const scanBtn = h("button", { class: "primary", onclick: () => void startScan() }, "Scanner");
const cancelBtn = h("button", { disabled: true, onclick: () => cancelScan() }, "Annuler");
const simLabel = h("span", { class: "sim", hidden: true, title: "Mode simulation : les actions sont journalisées mais rien n'est modifié." }, "SIMULATION");
const pathBar = h("div", { class: "row grow" }, h("label", {}, "Dossier :"), pathInput, rootsList,
  h("button", { onclick: () => void browse() }, "Parcourir…"), scanBtn);
const bar = progressBar();
const progressLabel = h("div", { class: "progress-label sel-text" });
const tabBar = h("nav", { class: "tabs" });
const panels = h("main", { class: "panels" });
const status = h("span", { class: "grow ellipsis" });
const updates = new Updates(() => settings, boot.version, boot.self_update, () => actions.busy);

function menuButton(label: string, items: () => MenuItem[]) {
  const b = h("button", { class: "menubtn" }, label);
  b.onclick = () => { const r = b.getBoundingClientRect(); showMenu(r.left, r.bottom + 2, items()); };
  return b;
}
const check = (on: boolean, label: string) => `${on ? "✓ " : " "}${label}`;
const menus = h("div", { class: "menus" },
  menuButton("Fichier", () => [
    { label: "Exporter l'onglet en CSV…  Ctrl+E", action: () => void exportCsv() },
    { label: "Exporter le rapport HTML…  Ctrl+Maj+E", action: () => void exportHtml() },
    { label: "", separator: true },
    { label: "Paramètres…  Ctrl+,", action: () => void openSettings() },
    { label: "", separator: true },
    { label: "Quitter  Ctrl+Q", action: () => void getCurrentWindow().close() },
  ]),
  menuButton("Affichage", () => [
    { label: check(settings.advanced, "Mode avancé (tous les onglets, n'importe quel dossier)"), action: () => void setAdvanced(!settings.advanced) },
    { label: "", separator: true },
    { label: "Rechercher…  Ctrl+F", action: () => { select("search"); search.focus(); } },
    { label: "", separator: true },
    ...Object.entries(THEMES).map(([k, v]) => ({ label: check(settings.theme === k, `Thème : ${v}`), action: () => void setTheme(k) })),
  ]),
  menuButton("Actions", () => [
    { label: check(actions.simulated, "Mode simulation"), action: () => { actions.simulated = !actions.simulated; simLabel.hidden = !actions.simulated; } },
    { label: "", separator: true },
    { label: "Annuler la dernière action  Ctrl+Z", action: () => void actions.undoLast() },
    { label: "Journal des actions…", action: () => void actions.showLog(boot.data_dir) },
  ]),
  menuButton("Aide", () => [
    { label: "Rechercher des mises à jour…", action: () => void updates.check(true) },
    { label: "À propos de Débarras", action: () => void about() },
  ]));

document.body.append(h("div", { class: "app" },
  h("header", {}, menus, pathBar, cancelBtn, simLabel), bar.el, progressLabel, tabBar, panels,
  h("footer", { class: "status" }, status, updates.readyBtn)));

function renderTabs() {
  const visible = TABS.filter(([k]) => settings.advanced || SIMPLE.includes(k));
  if (!visible.some(([k]) => k === tab)) tab = "home";
  mount(tabBar, visible.map(([k, label]) => h("button", { class: k === tab ? "on" : "", onclick: () => select(k) }, label)));
  mount(panels, TABS.find(([k]) => k === tab)![2]);
}

function select(k: TabKey) {
  tab = k;
  renderTabs();
  if (k === "cleanup" && !cleanup.analyzed) void cleanup.analyze();
}

function applyMode() {
  pathBar.hidden = !settings.advanced;
  cancelBtn.hidden = !settings.advanced && !scanner;
  renderTabs();
}

let statusTimer = 0;
function showStatus(text: string, ms = 0) {
  status.textContent = text;
  clearTimeout(statusTimer);
  if (ms) statusTimer = setTimeout(() => (status.textContent = ""), ms);
}

async function saveSettings() {
  try { await call("save_settings", { settings }); } catch (e) { await info("Paramètres", `Enregistrement impossible : ${e}`, true); }
}

async function setAdvanced(on: boolean) {
  settings.advanced = on;
  await saveSettings();
  applyMode();
}

async function setTheme(theme: string) {
  settings.theme = theme;
  await saveSettings();
  applyTheme(theme);
  home.refreshTheme();
  charts.refreshTheme();
  history.refreshTheme();
}

async function openSettings() {
  const s = await editSettings(settings);
  if (!s) return;
  const theme = settings.theme;
  settings = s;
  await saveSettings();
  if (s.theme !== theme) await setTheme(s.theme);
}

async function about() {
  await info("À propos de Débarras", `**Débarras ${boot.version}**\n\nVoir ce qui encombre le disque, et s'en débarrasser sans risque.\n\nArborescence et treemap, doublons, fichiers anciens, caches et installeurs oubliés, historique des scans.\nJamais de suppression définitive : tout passe par la corbeille et peut être annulé.\n\nDonnées : ${boot.data_dir}`);
}

// --- disques et dossier ------------------------------------------------------------------

async function refreshDrives() {
  drives = await call<Drive[]>("list_drives");
  home.setDrives(drives, !!scanner);
}

const root = () => pathInput.value.trim().replace(/^"|"$/g, "");
function setRoot(path: string) { pathInput.value = path; }

async function openDrive(r: string) {
  setRoot(r);
  if (!(await loadLatest(r))) await startScan();
}

async function browse() {
  const p = await open({ directory: true, title: "Choisir un dossier", defaultPath: root() || undefined });
  if (typeof p !== "string") return;
  setRoot(p);
  if (!(await loadLatest(p))) await startScan();
}

async function relaunchAdmin() {
  const r = current?.root ?? root();
  if (!(await ask("Analyser en administrateur", "Débarras va se relancer avec les droits administrateur (Windows demandera confirmation) pour analyser aussi les dossiers protégés.\n\nLes dossiers système restent protégés : rien n'y sera supprimé."))) return;
  const ok = await call<boolean>("relaunch_admin", { root: r }).catch(() => false);
  if (!ok) await info("Analyser en administrateur", "Relance annulée.");
}

// --- scan --------------------------------------------------------------------------------

function setScanning(on: boolean) {
  scanBtn.disabled = on;
  cancelBtn.disabled = !on;
  cancelBtn.hidden = !on && !settings.advanced;
  pathInput.disabled = on;
  bar.el.hidden = !on;
  bar.set(null);
  home.setDrives(drives, on);
}

async function startScan() {
  if (scanner) return;
  const r = root();
  if (!r) return info("Dossier invalide", "Choisissez un dossier.", true);
  setScanning(true);
  progressLabel.textContent = `Scan de ${r}…`;
  const t = run<{ root: string; cancelled: boolean; duration: number; file_count: number }>("scan", { root: r }, (d) => {
    if (d.current) progressLabel.textContent = `${humanCount(d.files)} fichiers — ${humanSize(d.size)} — ${d.current}`;
  });
  scanner = t;
  try {
    const res = await t.promise;
    if (res.cancelled) {
      progressLabel.textContent = `Scan annulé après ${humanDuration(res.duration)} (${humanCount(res.file_count)} fichiers parcourus).`;
    } else {
      if (![...rootsList.options].some((o) => o.value === res.root)) rootsList.prepend(h("option", { value: res.root }));
      setRoot(res.root);
      scanner = null;
      await loadLatest(res.root);
    }
  } catch (e) {
    progressLabel.textContent = String(e);
    await info("Échec du scan", String(e), true);
  } finally {
    scanner = null;
    setScanning(false);
  }
}

function cancelScan() {
  if (!scanner) return;
  cancelBtn.disabled = true;
  progressLabel.textContent = "Annulation…";
  scanner.cancel();
}

// --- affichage d'un scan -----------------------------------------------------------------

/** Affiche le dernier scan terminé de `r`, s'il existe. */
async function loadLatest(r: string): Promise<boolean> {
  const { scan, drive } = r ? await call<{ scan: Scan | null; drive: Drive | null }>("latest_scan", { root: r }) : { scan: null, drive: null };
  current = scan;
  stats = null;
  home.root = scan?.root ?? "";
  void refreshDrives(); // carte du disque affiché mise en évidence
  if (!scan) {
    tree.clear(); home.clear(); statsView.clear(); charts.clear(); history.clear();
    for (const v of [dups, similar, samename]) v.setScan("");
    search.setScan("");
    progressLabel.textContent = "Aucun scan pour ce dossier.";
    return false;
  }
  home.setLoading(scan.id, scan.root, scan.total_size);
  home.setGrowth(null);
  home.treemap.load(scan.id, scan.root);
  if (drive) home.treemap.setExtras(drive.free, Math.max(drive.used - scan.total_size, 0));
  home.setContext(drive, scan.errors, boot.admin);
  void tree.loadScan(scan.id, scan.root, scan.total_size, scan.file_count);
  void classify(scan);
  void computeStats();
  for (const v of [dups, similar, samename]) v.setScan(scan.root);
  void history.setRoot(scan.root);
  search.setScan(scan.root);
  const duration = (scan.finished || scan.started) - scan.started;
  progressLabel.textContent = `Scan du ${humanDate(scan.finished)} — ${humanCount(scan.file_count)} fichiers — ${humanSize(scan.total_size)} — ${humanDuration(duration)}`
    + (scan.errors ? ` — ${scan.errors} élément(s) inaccessible(s)` : "");
  return true;
}

/** Repère les logiciels du scan et répartit l'espace par niveau de risque. */
async function classify(scan: Scan) {
  safetyTask?.cancel();
  const t = run<Summary | null>("safety_summary", { scanId: scan.id, root: scan.root });
  safetyTask = t;
  try {
    const s = await t.promise;
    if (!s || current?.id !== scan.id) return; // annulé ou périmé
    home.treemap.refresh();
    home.setSummary(scan.total_size, s);
  } catch (e) {
    showStatus(String(e), 8000);
  } finally {
    if (safetyTask === t) safetyTask = null;
  }
}

async function computeStats() {
  const scan = current;
  if (!scan) return;
  statsView.setLoading();
  const days = statsView.oldDaysValue;
  try {
    const r = await run<Stats>("compute_stats", { scanId: scan.id, root: scan.root, oldDays: days }).promise;
    if (current?.id !== r.scan_id || r.old_days !== statsView.oldDaysValue) return; // périmé
    stats = r;
    statsView.setResult(r);
    charts.setData(scan.id, r);
  } catch (e) {
    statsView.clear(String(e));
  }
}

actions.onFinished = (rec: ActionRecord) => {
  if (rec.simulated) return;
  void cleanup.analyze(); // tailles à jour (onglet et bouton automatique)
  for (const v of [dups, similar, samename]) void v.pruneMissing();
  // Les fichiers ont bougé : rescan incrémental (empreintes conservées) pour rafraîchir les vues.
  if (current && !scanner) { setRoot(current.root); void startScan(); }
};

// --- exports -----------------------------------------------------------------------------

function defaultName(label: string, ext: string) {
  const base = current ? basename(current.root).replace(/:/g, "") : "";
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${label}_${base || "analyse"}_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}.${ext}`;
}

async function exportCsv() {
  const label = TABS.find(([k]) => k === tab)![1];
  let table: [string[], (string | number)[][]] | null = null;
  if ((tab === "tree" || tab === "home") && current) table = [["Dossier", "Octets", "Fichiers"], await call("dir_table", { scanId: current.id })];
  else if (tab === "charts" && stats) table = [["Catégorie", "Octets", "Fichiers"], stats.by_category.map(([c, s, n]) => [c, s, n])];
  else table = ({ stats: statsView, dups, similar, samename, history, search } as Record<string, { exportTable(): typeof table } | undefined>)[tab]?.exportTable() ?? null;
  if (!table || !table[1].length) return info("Export CSV", "Rien à exporter dans cet onglet.");
  const path = await save({ title: "Exporter en CSV", defaultPath: defaultName(label, "csv"), filters: [{ name: "CSV", extensions: ["csv"] }] });
  if (!path) return;
  try {
    await call("write_text", { path, content: toCsv(...table), bom: true });
    showStatus(`${humanCount(table[1].length)} lignes exportées vers ${path}`, 8000);
  } catch (e) {
    await info("Export CSV", `Écriture impossible : ${e}`, true);
  }
}

async function exportHtml() {
  const scan = current;
  if (!scan || !stats || stats.scan_id !== scan.id) return info("Rapport HTML", "Chargez un scan et attendez la fin du calcul des statistiques.");
  const path = await save({ title: "Rapport HTML", defaultPath: defaultName("rapport", "html"), filters: [{ name: "Page HTML", extensions: ["html"] }] });
  if (!path) return;
  const l = await call<{ dirs: [string, number, number][] }>("list_dir", { scanId: scan.id, path: scan.root });
  const dr = dups.result;
  const page = htmlReport({
    scan, stats,
    topDirs: [...l.dirs].sort((a, b) => b[1] - a[1]).slice(0, 25),
    duplicates: dr ? { wasted: dups.wasted ?? 0, groups: dr.groups.map((g) => ({ files: g.files, size: g.size ?? 0, wasted: (g.size ?? 0) * (g.files.length - 1) })) } : null,
    history: history.diff && history.diff.new.id === scan.id ? history.diff : null,
  });
  try {
    await call("write_text", { path, content: page, bom: false });
  } catch (e) {
    return info("Rapport HTML", `Écriture impossible : ${e}`, true);
  }
  if (await ask("Rapport HTML", `Rapport enregistré :\n${path}`, "Ouvrir", "Fermer")) await call("open_path", { target: path });
}

// --- raccourcis, glisser-déposer, démarrage ------------------------------------------------

addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement;
  if (e.ctrlKey && k === "e") { e.preventDefault(); void (e.shiftKey ? exportHtml() : exportCsv()); }
  else if (e.ctrlKey && k === ",") { e.preventDefault(); void openSettings(); }
  else if (e.ctrlKey && k === "f") { e.preventDefault(); if (settings.advanced) { select("search"); search.focus(); } }
  else if (e.ctrlKey && k === "q") { e.preventDefault(); void getCurrentWindow().close(); }
  else if (e.ctrlKey && k === "z" && !typing) { e.preventDefault(); void actions.undoLast(); }
});
pathInput.addEventListener("keydown", (e) => { if (e.key === "Enter") void startScan(); });
pathInput.addEventListener("change", () => { if (boot.roots.includes(root())) void loadLatest(root()); });
addEventListener("contextmenu", (e) => { if (!(e.target as HTMLElement).closest("input, textarea")) e.preventDefault(); });

void getCurrentWebview().onDragDropEvent((e) => {
  if (e.payload.type !== "drop" || !e.payload.paths.length || scanner) return;
  setRoot(e.payload.paths[0]);
  void startScan();
});
void listen<string>("open-folder", (e) => { setRoot(e.payload); void startScan(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (settings.theme === "system") void setTheme("system"); });

mount(rootsList, boot.roots.map((r) => h("option", { value: r })));
applyMode();
home.setAutoClean(null);
void cleanup.analyze(); // taille du nettoyage automatique, en arrière-plan
updates.schedule(boot.no_update_check);
await refreshDrives();
if (boot.folder) { setRoot(boot.folder); void startScan(); }
else if (boot.roots.length) { setRoot(boot.roots[0]); void loadLatest(boot.roots[0]); }
