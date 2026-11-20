// Pilote Débarras par le port de débogage de WebView2 (appelé par make_screenshots.ps1).
//   node screenshots.mjs scan <X:>            premier scan du disque de démo
//   node screenshots.mjs shots <X:> <dossier>  second scan puis captures
// Les disques affichés sont fictifs : la réponse des commandes de disques est remplacée dans la page.
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const [mode, letter, out] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let page;
for (let i = 0; i < 60 && !page; i++) {
  try { page = (await (await fetch("http://127.0.0.1:9334/json")).json()).find((p) => p.type === "page"); } catch { await sleep(500); }
}
if (!page) throw new Error("WebView2 injoignable");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
ws.onclose = (e) => { if (!done) { console.error(`connexion à WebView2 perdue (${e.code} ${e.reason})`); process.exit(1); } };
let done = false;
let id = 0;
const send = (method, params = {}) => new Promise((res, rej) => {
  const my = ++id;
  ws.addEventListener("message", function f(e) {
    const m = JSON.parse(e.data);
    if (m.id !== my) return;
    ws.removeEventListener("message", f);
    m.error ? rej(new Error(m.error.message)) : res(m.result);
  });
  ws.send(JSON.stringify({ id: my, method, params }));
});
const js = async (expr) => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result.value;
const until = async (expr, what, sec = 300) => {
  console.error(`attente : ${what}`);
  for (let i = 0; i < sec * 4; i++) { if (await js(expr)) return; await sleep(250); }
  throw new Error(`délai dépassé : ${what}`);
};
const click = (sel, text) => js(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(sel)})].find((e) => e.textContent.includes(${JSON.stringify(text)})); b?.click(); return !!b; })()`);
const shot = async (name, clipSel) => {
  console.error(`capture : ${name}`);
  await sleep(700);
  const clip = clipSel ? await js(`(() => { const r = document.querySelector(${JSON.stringify(clipSel)}).getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, scale: 1 }; })()`) : undefined;
  const { data } = await send("Page.captureScreenshot", { format: "png", clip });
  writeFileSync(join(out, name), Buffer.from(data, "base64"));
};

const GB = 1024 ** 3;
// Réponses IPC de Tauri (fetch vers ipc.localhost) retouchées avant d'arriver à l'interface.
const patch = `(() => {
  const real = window.fetch;
  const demo = (scan) => { const free = 512 * ${GB} - (scan ? scan.total_size : 0) - 41 * ${GB};
    return { root: "${letter}\\\\", title: "Données (${letter})", total: 512 * ${GB}, free, used: 512 * ${GB} - free }; };
  window.fetch = async (input, init) => {
    const res = await real(input, init);
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("http://ipc.localhost/") || !res.ok) return res;
    const cmd = decodeURIComponent(url.slice("http://ipc.localhost/".length));
    if (!["list_drives", "latest_scan", "init"].includes(cmd)) return res;
    let r = await res.clone().json();
    if (cmd === "list_drives") {
      const d = r.find((x) => x.root.startsWith("${letter}"));
      r = [{ root: "C:\\\\", title: "Windows (C:)", total: 256 * ${GB}, free: 88 * ${GB}, used: 168 * ${GB}, scan: null }, { ...demo(d && d.scan), scan: d ? d.scan : null }];
    } else if (cmd === "latest_scan") {
      const root = JSON.parse(init.body).root;
      r = { ...r, drive: root.startsWith("${letter}") ? demo(r.scan) : null };
    } else r = { ...r, admin: true };
    return new Response(JSON.stringify(r), { status: res.status, headers: res.headers });
  };
})();`;

await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 880, deviceScaleFactor: 1, mobile: false });
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
const scanned = `document.querySelector(".progress-label").textContent.startsWith("Scan du") && ![...document.querySelectorAll(".chip b")].some((b) => b.textContent === "…")`;

if (mode === "scan") {
  await send("Page.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", { source: patch });
  await send("Page.reload");
  await sleep(1500);
  await until(`document.querySelectorAll(".drive").length === 2`, "disques");
  await click(".drive:last-child button", "Analyser");
  await until(scanned, "premier scan");
} else {
  await click(".drive:last-child button", "Réanalyser");
  await until(`document.querySelector(".headline").textContent.includes("Depuis le scan") && ${scanned}`, "second scan");
  // Accueil : le jeu le plus lourd sélectionné sur la carte du disque entier (survol de la carte
  // jusqu'à trouver son rectangle, puis clic).
  const found = await js(`(() => {
    const w = document.querySelector(".canvas-wrap"), r = w.getBoundingClientRect(), tip = w.querySelector(".tooltip");
    for (let y = r.top + 2; y < r.bottom; y += 4) for (let x = r.left + 2; x < r.right; x += 6) {
      w.dispatchEvent(new MouseEvent("mousemove", { clientX: x, clientY: y, bubbles: true }));
      if (tip.textContent.split("\\n")[0].endsWith("Aventure Céleste")) {
        w.dispatchEvent(new MouseEvent("mousedown", { clientX: x, clientY: y, button: 0, bubbles: true }));
        w.dispatchEvent(new MouseEvent("mouseleave"));
        return true;
      }
    }
    return false;
  })()`);
  if (!found) throw new Error("jeu introuvable sur la carte");
  await until(`document.querySelector(".details h3")?.textContent === "Aventure Céleste"`, "détail du jeu");
  await shot("accueil.png");
  await click(".details button", "Mettre à la corbeille");
  await until(`!!document.querySelector("dialog[open]")`, "confirmation");
  await shot("confirmation.png", "dialog[open]");
  await click("dialog[open] footer button", "Annuler");
  // Évolution depuis le scan précédent.
  await js(`(() => { const s = document.querySelector(".treemap select"); s.value = "growth"; s.dispatchEvent(new Event("change")); })()`);
  await click(".chip", "Vos fichiers");
  await click(".chip", "Vos fichiers");
  await shot("evolution.png");
  // Nettoyage guidé.
  await click(".tabs button", "Nettoyage");
  await until(`document.querySelector(".cleanup .toolbar span").textContent.startsWith("Sélection")`, "nettoyage");
  await shot("nettoyage.png");
}
done = true;
ws.close();
