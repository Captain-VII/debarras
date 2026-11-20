// Mises à jour : vérification (auto/manuelle), « nouvelle version », téléchargement, installation.
import { call, run, type Release, type Settings } from "../lib/api";
import { h, info, modal, mount, progressBar, rich } from "../lib/dom";
import { humanSize } from "../lib/format";

const AUTO_CHECK_DELAY = 4000;
const RECHECK_INTERVAL = 6 * 3600 * 1000; // tant que Débarras reste ouvert

/** Notes de version en Markdown réduit : titres, listes, gras. */
function notes(md: string): HTMLElement {
  const box = h("div", { class: "notes" });
  if (!md.trim()) return mount(box, h("i", {}, "Pas de notes de version.")), box;
  for (const line of md.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    const m = t.match(/^(#+)\s+(.*)$/);
    if (m) box.append(h("h4", {}, m[2]));
    else if (/^[-*]\s+/.test(t)) box.append(h("li", {}, rich(t.replace(/^[-*]\s+/, ""))));
    else box.append(h("p", {}, rich(t)));
  }
  return box;
}

export class Updates {
  readonly readyBtn = h("button", { class: "link strong", hidden: true });
  private busy = false;
  private ready: string | null = null;

  constructor(private settings: () => Settings, private version: string, private selfUpdate: boolean, private actionsBusy: () => boolean) {
    this.readyBtn.onclick = () => void this.install(false);
  }

  schedule(disabled: boolean) {
    if (disabled) return; // tests, environnements hors ligne
    const auto = () => { if (this.settings().check_updates && !this.ready) void this.check(false); };
    setTimeout(auto, AUTO_CHECK_DELAY);
    setInterval(auto, RECHECK_INTERVAL);
  }

  async check(manual = true) {
    if (this.busy) return;
    this.busy = true;
    let rel: Release | null;
    try {
      rel = await run<Release | null>("update_check").promise;
    } catch (e) {
      this.busy = false;
      if (manual) await info("Mise à jour", `Vérification impossible : ${e}`, true);
      return;
    }
    this.busy = false;
    if (!rel) {
      if (manual) await info("Mise à jour", `Vous utilisez la dernière version de Débarras (${this.version}).`);
      return;
    }
    const s = this.settings();
    if (!manual && rel.version === s.skipped_version) return;
    if (this.ready === rel.version) { if (manual) await this.install(false); return; }
    if (!manual && s.auto_install && this.selfUpdate) return void this.download(rel, true);
    const choice = await modal<string>("Mise à jour disponible", (close) => [
      h("h3", {}, `Débarras ${rel.version} est disponible`),
      h("p", {}, `Version installée : ${this.version} — téléchargement : ${humanSize(rel.asset_size)}`),
      notes(rel.notes),
      this.selfUpdate ? null : h("p", { class: "muted" }, "L'installation automatique n'est possible que depuis Debarras.exe placé dans un dossier modifiable. Téléchargez la nouvelle version depuis sa page."),
      h("footer", {},
        h("button", { onclick: () => close("skip") }, "Ignorer cette version"), h("span", { class: "grow" }),
        h("button", { onclick: () => close() }, "Plus tard"),
        this.selfUpdate ? h("button", { class: "primary", onclick: () => close("install") }, "Installer et redémarrer")
          : h("button", { class: "primary", onclick: () => close("page") }, "Ouvrir la page de téléchargement")),
    ], "wide");
    if (choice === "install") await this.download(rel, false);
    else if (choice === "page") await call("open_path", { target: rel.page_url });
    else if (choice === "skip") { s.skipped_version = rel.version; await call("skip_version", { version: rel.version }); }
  }

  private async download(rel: Release, silent: boolean) {
    this.busy = true;
    let dlg: HTMLDialogElement | null = null;
    const bar = progressBar();
    const t = run<string>("update_download", {}, (d) => bar.set(d.total ? d.done / d.total : null));
    if (!silent) {
      bar.el.hidden = false;
      bar.set(0);
      dlg = h("dialog", { class: "modal" }, h("header", {}, h("h2", {}, "Mise à jour")),
        h("div", { class: "body" }, h("p", {}, `Téléchargement de Débarras ${rel.version}…`), bar.el,
          h("footer", {}, h("button", { onclick: () => t.cancel() }, "Annuler"))));
      dlg.addEventListener("cancel", (e) => e.preventDefault());
      document.body.append(dlg);
      dlg.showModal();
    }
    try {
      await t.promise;
      dlg?.remove();
      this.ready = rel.version;
      if (!silent) return void (await this.install(true));
      this.readyBtn.textContent = `⬆ Débarras ${rel.version} sera installé à la fermeture — redémarrer maintenant`;
      this.readyBtn.title = `Archive téléchargée et vérifiée (empreinte SHA-256).\n\n${rel.notes}`;
      this.readyBtn.hidden = false;
    } catch (e) {
      dlg?.remove();
      if (!silent) await info("Mise à jour", `La mise à jour n'a pas été installée : ${e}`, true);
    } finally {
      this.busy = false;
    }
  }

  private async install(ask: boolean) {
    if (!this.ready) return;
    if (this.actionsBusy()) return info("Mise à jour", "Une action est en cours : relancez la mise à jour quand elle sera terminée.");
    if (ask) {
      const yes = await modal<boolean>("Installer la mise à jour", (close) => [
        h("p", { class: "msg" }, `Débarras ${this.ready} est prêt (empreinte SHA-256 vérifiée).\n\nDébarras va se fermer, remplacer ses fichiers puis redémarrer. L'ancienne version est gardée à côté (dossier « .old ») jusqu'au prochain démarrage.`),
        h("footer", {}, h("button", { onclick: () => close(false) }, "Non"), h("button", { class: "primary", onclick: () => close(true) }, "Oui")),
      ]);
      if (!yes) return;
    }
    const ok = await call<boolean>("update_install").catch(() => false);
    if (!ok) {
      this.ready = null;
      this.readyBtn.hidden = true;
      await info("Mise à jour", "Le programme d'installation n'a pas pu démarrer : Débarras reste ouvert, sans changement.\nVous pouvez télécharger la nouvelle version manuellement.", true);
    }
  }
}
