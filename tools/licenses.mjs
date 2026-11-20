// Licences des composants tiers embarqués dans Debarras.exe : crates Rust liées à l'exécutable
// (dépendances normales, sans celles de test ni de build) et paquets npm de l'interface.
// Usage : node tools/licenses.mjs <dossier de sortie>  -> <dossier>/THIRD-PARTY-NOTICES.txt
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const out = process.argv[2];
if (!out) throw new Error("usage : node tools/licenses.mjs <dossier>");
const root = join(dirname(new URL(import.meta.url).pathname.replace(/^\/(\w:)/, "$1")), "..");

const meta = JSON.parse(execFileSync("cargo", ["metadata", "--format-version", "1", "--locked", "--filter-platform", "x86_64-pc-windows-msvc"],
  { cwd: root, maxBuffer: 256 << 20, encoding: "utf8" }));
const byId = new Map(meta.packages.map((p) => [p.id, p]));
const nodes = new Map(meta.resolve.nodes.map((n) => [n.id, n]));
const members = new Set(meta.workspace_members);
const start = meta.packages.find((p) => p.name === "debarras").id;

// Parcours des dépendances normales depuis l'exécutable.
const seen = new Set([start]);
const todo = [start];
while (todo.length) {
  for (const d of nodes.get(todo.pop()).deps) {
    if (!d.dep_kinds.some((k) => k.kind === null) || seen.has(d.pkg)) continue;
    seen.add(d.pkg);
    todo.push(d.pkg);
  }
}

const LICENSE_FILE = /^(licen[cs]e|copying|notice|unlicense)/i;
const texts = (dir) => existsSync(dir)
  ? readdirSync(dir).filter((f) => LICENSE_FILE.test(f)).sort().map((f) => readFileSync(join(dir, f), "utf8").replace(/\r\n/g, "\n").trim())
  : [];

const comps = [...seen].filter((id) => !members.has(id)).map((id) => byId.get(id))
  .map((p) => ({ name: p.name, version: p.version, license: p.license ?? p.license_file ?? "?", source: p.repository ?? `https://crates.io/crates/${p.name}`, texts: texts(dirname(p.manifest_path)) }));

const lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
for (const [path, p] of Object.entries(lock.packages)) {
  if (!path.startsWith("node_modules/") || p.dev) continue;
  const name = path.slice("node_modules/".length);
  comps.push({ name: `${name} (npm)`, version: p.version, license: p.license ?? "?", source: `https://www.npmjs.com/package/${name}`, texts: texts(join(root, path)) });
}
comps.sort((a, b) => a.name.localeCompare(b.name));

// Textes identiques écrits une seule fois.
const pool = [];
const ref = (t) => { let i = pool.indexOf(t); if (i < 0) { pool.push(t); i = pool.length - 1; } return i + 1; };
const lines = [
  "Débarras — licences des composants tiers",
  "=========================================",
  "",
  "Débarras est distribué sous licence MIT (voir ../LICENSE). Debarras.exe embarque les composants",
  "suivants, chacun sous sa propre licence. Les textes de licence fournis par chaque composant sont",
  "reproduits en fin de fichier (un texte identique n'est écrit qu'une fois).",
  "SQLite, compilé dans l'exécutable par rusqlite, est dans le domaine public : https://www.sqlite.org/copyright.html",
  "WebView2 (Microsoft Edge) n'est pas embarqué : c'est un composant de Windows.",
  "",
];
for (const c of comps) {
  const refs = c.texts.map(ref);
  const where = refs.length ? `texte(s) n° ${refs.join(", ")}` : "texte standard de la licence (le paquet n'en fournit pas)";
  lines.push(`- ${c.name} ${c.version} — ${c.license} — ${c.source} — ${where}`);
}
lines.push("", "Textes de licence", "=================");
pool.forEach((t, i) => lines.push("", `--- n° ${i + 1} ${"-".repeat(60)}`, "", t));
mkdirSync(out, { recursive: true });
writeFileSync(join(out, "THIRD-PARTY-NOTICES.txt"), lines.join("\r\n") + "\r\n");
const missing = comps.filter((c) => !c.texts.length).map((c) => c.name);
console.log(`${comps.length} composants, ${pool.length} textes distincts${missing.length ? ` ; sans fichier de licence : ${missing.join(", ")}` : ""}`);
