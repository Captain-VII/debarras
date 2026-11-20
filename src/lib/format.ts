// Formatage lisible des tailles, dates et durées (comme utils/format.py de la 1.6.x).

const UNITS = ["o", "Ko", "Mo", "Go", "To", "Po"];
const THIN = "\u202f"; // espace insécable fine

/** 1536 -> « 1,5 Ko ». */
export function humanSize(size: number): string {
  for (const unit of UNITS) {
    if (Math.abs(size) < 1024 || unit === UNITS[UNITS.length - 1]) {
      if (unit === "o") return `${Math.trunc(size)} o`;
      return `${size.toFixed(1).replace(".", ",")} ${unit}`;
    }
    size /= 1024;
  }
  return `${size} o`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** Horodatage (secondes) -> « JJ/MM/AAAA HH:MM ». */
export function humanDate(ts: number | null | undefined, seconds = false): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return "—";
  const s = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return seconds ? `${s}:${pad(d.getSeconds())}` : s;
}

/** Date ISO locale pour les exports (« 2026-11-20 14:03:00 »). */
export function iso(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** 75.3 -> « 1 min 15 s ». */
export function humanDuration(seconds: number): string {
  seconds = Math.trunc(seconds);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.trunc(seconds / 60);
  if (minutes < 60) return `${minutes} min ${pad(seconds % 60)} s`;
  return `${Math.trunc(minutes / 60)} h ${pad(minutes % 60)} min`;
}

/** 124190 -> « 124 190 ». */
export function humanCount(n: number): string {
  return String(Math.trunc(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, THIN).replace(/^/, n < 0 ? "-" : "");
}

const sign = (n: number) => (n > 0 ? "+" : n < 0 ? "−" : "");

/** Variation de taille : « +1,5 Ko », « −3 o ». */
export const signedSize = (n: number) => sign(n) + humanSize(Math.abs(n));
export const signedCount = (n: number) => sign(n) + humanCount(Math.abs(n));

/** « 12,3 % ». */
export const percent = (part: number, total: number) =>
  `${((100 * part) / (total || 1)).toFixed(1).replace(".", ",")} %`;

/** Dernier élément d'un chemin Windows. */
export function basename(path: string): string {
  const p = path.replace(/\\+$/, "");
  return p.slice(p.lastIndexOf("\\") + 1) || path;
}

/** Dossier parent (`C:\a\b` -> `C:\a`, `C:\a` -> `C:\`). */
export function dirname(path: string): string {
  const p = path.replace(/\\+$/, "");
  const i = p.lastIndexOf("\\");
  if (i < 0) return path;
  return i <= 2 && p[1] === ":" ? p.slice(0, 3) : p.slice(0, i);
}

export const normcase = (p: string) => p.toLowerCase().replace(/\//g, "\\");

/** `path` est `root` ou se trouve dessous. */
export function inside(path: string, root: string): boolean {
  const p = normcase(path), r = normcase(root).replace(/\\+$/, "");
  return p === r || p.startsWith(r + "\\");
}

/** Chemin relatif au dossier analysé (sinon le chemin complet). */
export function displayName(path: string, root: string): string {
  if (normcase(path) === normcase(root)) return path;
  const r = root.replace(/\\+$/, "");
  return normcase(path).startsWith(normcase(r) + "\\") ? path.slice(r.length + 1) : path;
}
