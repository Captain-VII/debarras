// Catégories de fichiers et niveaux de sécurité : libellés et couleurs (palettes de la 1.6.x,
// validées pour le daltonisme courant).

const EXTENSIONS: [string, string[]][] = [
  ["Images", [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".raw", ".cr2", ".nef", ".arw", ".dng", ".svg", ".ico", ".psd", ".xcf"]],
  ["Vidéos", [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp"]],
  ["Audio", [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".aiff"]],
  ["Documents", [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".txt", ".rtf", ".md", ".csv", ".epub", ".mobi"]],
  ["Archives", [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".iso", ".img", ".vhd", ".vhdx", ".cab"]],
  ["Code", [".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".html", ".css", ".json", ".xml", ".yml", ".yaml", ".toml", ".sql", ".sh", ".ps1", ".bat", ".ipynb", ".pyc", ".class", ".o", ".obj"]],
  ["Exécutables", [".exe", ".msi", ".dll", ".sys", ".appx", ".msix", ".jar", ".apk", ".bin", ".so", ".dylib"]],
];
export const OTHER = "Autres";
export const CATEGORIES = [...EXTENSIONS.map(([c]) => c), OTHER];
const BY_EXT = new Map(EXTENSIONS.flatMap(([c, exts]) => exts.map((e) => [e, c] as const)));

const CAT_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#9a9993"];
const CAT_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#6b6a66"];

/** Extension (avec le point, comme le cache) d'un nom de fichier. */
export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i).toLowerCase() : "";
}

export const category = (ext: string) => BY_EXT.get(ext.toLowerCase()) ?? OTHER;

export function categoryColors(dark: boolean): Record<string, string> {
  const pal = dark ? CAT_DARK : CAT_LIGHT;
  return Object.fromEntries(CATEGORIES.map((c, i) => [c, pal[i]]));
}

export type Level = "system" | "software" | "personal" | "cleanable";
export const LEVELS: Level[] = ["system", "software", "personal", "cleanable"];
export const LABELS: Record<Level, string> = {
  system: "Système", software: "Logiciel", personal: "Vos fichiers", cleanable: "Nettoyable",
};
export const HINTS: Record<Level, string> = {
  system: "Indispensable à Windows : Débarras refuse d'y toucher.",
  software: "Fait partie d'un logiciel ou de ses réglages : le supprimer peut le casser. Désinstallez plutôt le logiciel (Paramètres › Applications).",
  personal: "Aucun risque pour Windows ni vos logiciels : vérifiez seulement que vous n'en avez plus besoin.",
  cleanable: "Cache ou fichier temporaire, recréé automatiquement si besoin : supprimable sans risque.",
};
const LV_LIGHT = ["#d03b3b", "#e08a00", "#2e9d5b", "#2a78d6"];
const LV_DARK = ["#e5534b", "#d98a1c", "#2fa866", "#3987e5"];

export function levelColors(dark: boolean): Record<Level, string> {
  const pal = dark ? LV_DARK : LV_LIGHT;
  return Object.fromEntries(LEVELS.map((l, i) => [l, pal[i]])) as Record<Level, string>;
}

/** Thème effectif (réglage ou système). */
export function isDark(): boolean {
  const t = document.documentElement.dataset.theme;
  return t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
}
