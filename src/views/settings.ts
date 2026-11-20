// Paramètres : exclusions, liste noire, liste blanche, thème, mises à jour.
import { open } from "@tauri-apps/plugin-dialog";
import { call, type Settings } from "../lib/api";
import { h, modal, mount } from "../lib/dom";

export const THEMES: Record<string, string> = { system: "Système", light: "Clair", dark: "Sombre" };

const lines = (t: HTMLTextAreaElement) => t.value.split("\n").map((l) => l.trim()).filter(Boolean);

/** Fenêtre des paramètres ; résout avec les nouveaux paramètres (non enregistrés) ou rien. */
export function editSettings(current: Settings): Promise<Settings | undefined> {
  return modal<Settings>("Paramètres", (close) => {
    const paths = h("select", { multiple: true, size: 5, class: "list" });
    const names = h("input", { type: "text", placeholder: "node_modules, .git, …" });
    const black = h("textarea", { rows: 4, placeholder: "Un motif par ligne, ex. :\n*.log\nnode_modules\nThumbs.db" });
    const white = h("textarea", { rows: 4, placeholder: "Un chemin ou un motif par ligne, ex. :\nC:\\Users\\moi\\Documents\\Important\n*.kdbx" });
    const theme = h("select", {}, Object.entries(THEMES).map(([k, v]) => h("option", { value: k }, v)));
    const updates = h("input", { type: "checkbox" });
    const auto = h("input", { type: "checkbox" });
    updates.onchange = () => (auto.disabled = !updates.checked);
    const fill = (s: Settings) => {
      mount(paths, s.excluded_paths.map((p) => h("option", { value: p }, p)));
      names.value = s.excluded_names.join(", ");
      black.value = s.blacklist.join("\n");
      white.value = s.whitelist.join("\n");
      theme.value = s.theme in THEMES ? s.theme : "system";
      updates.checked = s.check_updates;
      auto.checked = s.auto_install;
      auto.disabled = !s.check_updates;
    };
    fill(current);
    const pick = async (title: string) => {
      const p = await open({ directory: true, title });
      return typeof p === "string" ? p : null;
    };
    return [
      h("fieldset", {}, h("legend", {}, "Exclusions du scan"), h("div", {}, "Dossiers exclus :"),
        h("div", { class: "row" }, paths, h("div", { class: "stack" },
          h("button", { onclick: async () => { const p = await pick("Dossier à exclure"); if (p) paths.append(h("option", { value: p }, p)); } }, "Ajouter…"),
          h("button", { onclick: () => [...paths.selectedOptions].forEach((o) => o.remove()) }, "Retirer"))),
        h("label", { class: "row" }, "Noms exclus partout : ", names)),
      h("fieldset", {}, h("legend", {}, "Liste noire — ignorés lors du scan"), h("div", {}, "Fichiers et dossiers dont le nom correspond (jokers * et ?)."), black),
      h("fieldset", {}, h("legend", {}, "Liste blanche — protégés"),
        h("div", {}, "Jamais cochés par la sélection automatique ni traités par une action (corbeille, déplacement, archivage)."), white,
        h("div", {}, h("button", { onclick: async () => { const p = await pick("Dossier à protéger"); if (p) white.value = (white.value.trim() ? white.value.trimEnd() + "\n" : "") + p; } }, "Ajouter un dossier…"))),
      h("fieldset", {}, h("legend", {}, "Apparence et mises à jour"), h("label", { class: "row" }, "Thème : ", theme),
        h("label", { class: "check", title: "Consulte les releases publiques de Débarras sur GitHub. Aucune donnée n'est envoyée." }, updates, " Vérifier les nouvelles versions (au démarrage, puis toutes les 6 heures)"),
        h("label", { class: "check", title: "Sinon, Débarras demande avant chaque mise à jour." }, auto, " Installer automatiquement les mises à jour (à la fermeture de Débarras)")),
      h("p", { class: "muted" }, "Les exclusions et la liste noire s'appliquent au prochain scan."),
      h("footer", {},
        h("button", { onclick: async () => fill(await call<Settings>("default_settings")) }, "Valeurs par défaut"),
        h("span", { class: "grow" }),
        h("button", { onclick: () => close() }, "Annuler"),
        h("button", { class: "primary", onclick: () => close({
          ...current,
          excluded_paths: [...paths.options].map((o) => o.value),
          excluded_names: names.value.split(",").map((n) => n.trim()).filter(Boolean),
          blacklist: lines(black), whitelist: lines(white), theme: theme.value,
          check_updates: updates.checked, auto_install: auto.checked,
        }) }, "OK")),
    ];
  }, "wide");
}

export function applyTheme(theme: string) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
