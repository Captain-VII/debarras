// Confirmation, exécution, journal et annulation des actions (corbeille, déplacement, archive).
import { open, save } from "@tauri-apps/plugin-dialog";
import { call, run, type ActionRecord, type Task } from "../lib/api";
import { Table, ask, h, info, modal, mount, progressBar, type MenuItem, SEP } from "../lib/dom";
import { basename, dirname, humanCount, humanDate, humanSize } from "../lib/format";
import type { Level } from "../lib/meta";

export type Kind = "trash" | "move" | "archive";
const LABELS: Record<Kind, string> = { trash: "Mise à la corbeille", move: "Déplacement", archive: "Archivage ZIP" };
const VERBS: Record<Kind, string> = { trash: "mettre à la corbeille", move: "déplacer", archive: "archiver" };
const DONE = ["fait", "simulé", "restauré"];

/** Entrées d'action d'un menu contextuel (grisées pour un élément système). */
export function actionEntries(paths: string[], request: (k: Kind, p: string[]) => void, verdict?: { level: Level; reason: string }): MenuItem[] {
  if (!paths.length) return [];
  const suffix = paths.length > 1 ? ` (${paths.length})` : "";
  if (verdict?.level === "system") return [SEP, { label: `🔒 Protégé : ${verdict.reason}`, disabled: true }];
  const warn = verdict?.level === "software" ? " ⚠" : "";
  return [SEP,
    { label: `Mettre à la corbeille…${suffix}${warn}`, action: () => request("trash", paths) },
    { label: `Déplacer vers…${suffix}${warn}`, action: () => request("move", paths) },
    { label: `Archiver en ZIP…${suffix}`, action: () => request("archive", paths) }];
}

interface Risks { blocked: string[]; warnings: string[]; bin: string[] }

/** Récapitulatif obligatoire avant toute action ; `null` si annulé. */
async function confirm(kind: Kind, paths: string[], sizes: Record<string, number>, simulated: boolean) {
  const risks = await call<Risks>("prepare_action", { kind, paths, sizes });
  const total = paths.reduce((s, p) => s + (sizes[p] ?? 0), 0);
  return modal<{ target: string; trashOriginals: boolean }>(LABELS[kind] + (simulated ? " — simulation" : ""), (close) => {
    const first = paths[0] ?? "";
    const target = h("input", {
      type: "text",
      value: kind === "archive" ? `${dirname(first)}\\${paths.length === 1 ? basename(first) : "archive"}.zip`.replace("\\\\", "\\") : "",
    });
    const originals = h("input", { type: "checkbox", checked: true });
    const understood = h("input", { type: "checkbox", checked: !risks.warnings.length });
    const binBox = h("div", { class: "risk red" }, h("b", {}, "🛑 La corbeille ne peut pas tout recevoir :"), risks.bin.map((b) => h("div", {}, b)));
    const ok = h("button", { class: "primary" }, simulated ? "Simuler" : "Confirmer");
    const refresh = () => {
      const binBlocks = risks.bin.length > 0 && (kind !== "archive" || originals.checked);
      binBox.hidden = !binBlocks;
      ok.disabled = !understood.checked || (binBlocks && !simulated);
    };
    originals.onchange = understood.onchange = refresh;
    const browse = async () => {
      const start = dirname(first);
      const p = kind === "move"
        ? await open({ directory: true, title: "Dossier de destination", defaultPath: start })
        : await save({ title: "Archive ZIP", defaultPath: target.value || start, filters: [{ name: "Archive ZIP", extensions: ["zip"] }] });
      if (typeof p === "string") target.value = p;
    };
    ok.onclick = () => {
      let t = target.value.trim();
      if (kind !== "trash" && !t) return void info("Destination manquante", "Choisissez une destination.");
      if (kind === "archive" && !t.toLowerCase().endsWith(".zip")) t += ".zip";
      close({ target: t, trashOriginals: originals.checked });
    };
    const list = paths.slice(0, 5000).map((p) => h("li", { title: p }, p));
    if (paths.length > 5000) list.push(h("li", {}, `… et ${humanCount(paths.length - 5000)} autres`));
    const cut = (xs: string[]) => [...xs.slice(0, 8).map((x) => h("div", {}, x)), xs.length > 8 ? h("div", {}, "…") : null];
    queueMicrotask(refresh);
    return [
      simulated ? h("div", { class: "sim" }, "MODE SIMULATION — aucun fichier ne sera modifié.") : null,
      h("p", {}, `Vous allez ${VERBS[kind]} ${humanCount(paths.length)} élément(s) — ${humanSize(total)} :`),
      h("ul", { class: "paths" }, list),
      kind !== "trash" ? h("div", { class: "row" }, h("label", {}, kind === "move" ? "Destination :" : "Archive :"), target,
        h("button", { onclick: browse }, "Parcourir…")) : null,
      kind === "archive" ? h("label", { class: "check" }, originals, " Mettre les originaux à la corbeille une fois l'archive créée et vérifiée") : null,
      kind === "trash" ? h("p", { class: "muted" }, "Les éléments restent récupérables depuis la corbeille et via Actions › Annuler la dernière action.") : null,
      risks.blocked.length ? h("div", { class: "risk red" }, h("b", {}, "🔒 Ignorés — indispensables à Windows :"), cut(risks.blocked)) : null,
      risks.warnings.length ? [h("div", { class: "risk orange" },
        h("b", {}, "⚠ Attention — ces éléments font partie de logiciels."),
        " Les supprimer peut empêcher un programme ou un jeu de fonctionner. Désinstallez plutôt le logiciel depuis Paramètres › Applications.",
        cut(risks.warnings)),
        h("label", { class: "check" }, understood, " J'ai compris : je veux quand même traiter ces éléments de logiciel")] : null,
      binBox,
      h("footer", {}, h("button", { onclick: () => close() }, "Annuler"), ok),
    ];
  }, "wide");
}

/** Fenêtre de progression d'une tâche d'action, fermée à la fin. */
function progressDialog(title: string): { update: (d: { done: number; total: number; path: string }) => void; done: () => void; onCancel: (f: () => void) => void } {
  const dlg = h("dialog", { class: "modal" });
  const bar = progressBar();
  bar.el.hidden = false;
  bar.set(null);
  const label = h("p", { class: "ellipsis" }, `${title}…`);
  const stop = h("button", {}, "Interrompre");
  mount(dlg, h("header", {}, h("h2", {}, title)), h("div", { class: "body" }, label, bar.el, h("footer", {}, stop)));
  dlg.addEventListener("cancel", (e) => e.preventDefault());
  let shown = false;
  const timer = setTimeout(() => { document.body.append(dlg); dlg.showModal(); shown = true; }, 300);
  return {
    update: (d) => { bar.set(d.total ? d.done / d.total : null); label.textContent = d.path ? d.path.slice(-90) : "Finalisation…"; },
    done: () => { clearTimeout(timer); if (shown) dlg.close(); dlg.remove(); },
    onCancel: (f) => { stop.onclick = () => { stop.disabled = true; f(); }; },
  };
}

/** Point d'entrée unique des actions depuis toutes les vues. */
export class Actions {
  simulated = false;
  private current: Task<unknown> | null = null;
  onFinished: (rec: ActionRecord) => void = () => {};

  get busy() { return this.current !== null; }

  async request(kind: Kind, paths: string[], sizes: Record<string, number>) {
    if (this.busy) return info("Action en cours", "Une action est déjà en cours.");
    if (!paths.length) return;
    const c = await confirm(kind, paths, sizes, this.simulated);
    if (!c) return;
    const dlg = progressDialog(LABELS[kind]);
    const t = run<{ nothing: boolean; record: ActionRecord }>("run_action", {
      kind, paths, sizes, target: c.target, trashOriginals: c.trashOriginals, simulated: this.simulated,
    }, dlg.update);
    dlg.onCancel(t.cancel);
    this.current = t;
    try {
      const r = await t.promise;
      dlg.done();
      if (r.nothing) {
        return info("Rien à faire", "Aucun élément ne peut être traité :\n" + r.record.items.slice(0, 15).map((i) => `${i.src} — ${i.error}`).join("\n"), true);
      }
      await this.report(r.record);
      this.onFinished(r.record);
    } catch (e) {
      dlg.done();
      await info(LABELS[kind], `Erreur : ${e}`, true);
    } finally {
      this.current = null;
    }
  }

  async undoLast() {
    if (this.busy) return;
    const rec = await call<ActionRecord | null>("last_undoable");
    if (!rec) return info("Annuler", "Aucune action à annuler.");
    const n = rec.items.filter((i) => i.status === "fait").length;
    let text = `Annuler « ${rec.label} » du ${humanDate(rec.created)} ?\n\n${humanCount(n)} élément(s) seront remis à leur emplacement d'origine.`;
    if (rec.kind === "archive") text += `\nL'archive ${rec.target} sera envoyée à la corbeille.`;
    if (!(await ask("Annuler la dernière action", text))) return;
    const dlg = progressDialog("Annulation");
    const t = run<ActionRecord | null>("undo_last", {}, dlg.update);
    this.current = t;
    try {
      const done = await t.promise;
      dlg.done();
      if (done) { await this.report(done); this.onFinished(done); }
    } catch (e) {
      dlg.done();
      await info("Annulation", `Erreur : ${e}`, true);
    } finally {
      this.current = null;
    }
  }

  private report(rec: ActionRecord) {
    let msg: string, failed;
    if (rec.undone) {
      failed = rec.items.filter((i) => i.error.startsWith("annulation"));
      msg = `${humanCount(rec.items.filter((i) => i.status === "restauré").length)} élément(s) restauré(s).`;
    } else {
      const ok = rec.items.filter((i) => i.status === "fait" || i.status === "simulé");
      failed = rec.items.filter((i) => i.status === "erreur" || i.status === "ignoré");
      msg = `${humanCount(ok.length)} élément(s) ${rec.simulated ? "seraient traités" : "traités"} — ${humanSize(ok.reduce((s, i) => s + i.size, 0))}.`;
    }
    if (rec.note) msg += `\n${rec.note}`;
    if (failed.length) {
      msg += `\n\n${humanCount(failed.length)} non traité(s) :\n` + failed.slice(0, 12).map((i) => `• ${i.src} — ${i.error}`).join("\n");
      if (failed.length > 12) msg += "\n…";
    }
    return info(rec.label + (rec.simulated ? " (simulation)" : ""), msg, failed.length > 0);
  }

  /** Journal des actions, avec le détail par élément. */
  async showLog(dataDir: string) {
    const records = await call<ActionRecord[]>("action_log");
    type Row = { rec?: ActionRecord; item?: ActionRecord["items"][number] };
    const rows: Row[] = records.map((rec) => ({ rec }));
    const kids = new Map<Row, Row[]>(rows.map((r) => [r, r.rec!.items.map((item) => ({ item }))]));
    const table = new Table<Row>([
      { title: "Date / élément", text: (r) => r.rec ? humanDate(r.rec.created) : r.item!.src, title_: (r) => r.item?.src ?? "" },
      { title: "Action", text: (r) => r.rec ? r.rec.label + (r.rec.target ? ` → ${r.rec.target}` : "") : r.item!.dst ?? "" },
      { title: "Éléments", num: true, text: (r) => r.rec ? `${humanCount(r.rec.items.filter((i) => DONE.includes(i.status)).length)}/${humanCount(r.rec.items.length)}` : "" },
      { title: "Taille", num: true, text: (r) => humanSize(r.rec ? r.rec.items.filter((i) => DONE.includes(i.status)).reduce((s, i) => s + i.size, 0) : r.item!.size) },
      { title: "État", text: (r) => r.rec ? (r.rec.simulated ? "simulée" : r.rec.undone ? "annulée" : "effectuée") + (r.rec.note ? ` — ${r.rec.note}` : "") : r.item!.status + (r.item!.error ? ` : ${r.item!.error}` : "") },
    ], { children: (r) => kids.get(r), empty: "Aucune action." });
    table.setRows(rows);
    const undoable = !!records.find((r) => !r.simulated)?.undoable; // la dernière action réelle
    await modal<void>("Journal des actions", (close) => [
      table.el,
      h("footer", {},
        h("button", { disabled: !undoable, onclick: () => { close(); void this.undoLast(); } }, "Annuler la dernière action"),
        h("button", { onclick: () => call("open_path", { target: dataDir }) }, "Ouvrir le dossier du journal"),
        h("span", { class: "grow" }),
        h("button", { class: "primary", onclick: () => close() }, "Fermer")),
    ], "large");
  }
}
