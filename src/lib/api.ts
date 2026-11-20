// Appels au moteur Rust (commandes Tauri) et types des réponses.
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { Level } from "./meta";

export interface Scan {
  id: number; root: string; started: number; finished: number | null;
  total_size: number; file_count: number; dir_count: number; errors: number;
}
export interface Drive { root: string; title: string; total: number; free: number; used: number; scan?: Scan | null }
export interface Settings {
  excluded_paths: string[]; excluded_names: string[]; blacklist: string[]; whitelist: string[];
  theme: string; check_updates: boolean; auto_install: boolean; last_update_check: number;
  skipped_version: string; advanced: boolean; version: number;
}
export interface Init {
  version: string; data_dir: string; admin: boolean; roots: string[]; settings: Settings;
  folder: string | null; self_update: boolean; no_update_check: boolean;
}
/** Nœud de la carte : nom, chemin, taille, dossier, fichiers, date, niveau, raison,
 * enfants (dossier détaillé), reste regroupé [nombre, taille]. */
export interface TreeNode {
  n: string; p: string; s: number; d: boolean; c: number; m: number; l: Level; r: string;
  k?: TreeNode[]; x?: [number, number];
}
export interface Summary { scan_id: number; totals: Partial<Record<Level, number>>; top: Partial<Record<Level, [string, number, string][]>> }
export type FileRow = [string, number, number, number]; // chemin, taille, modifié, dernière utilisation
export interface Stats {
  scan_id: number; root: string; total_size: number; file_count: number;
  by_category: [string, number, number][]; by_ext: [string, number, number][];
  largest: FileRow[]; old_days: number; old: FileRow[]; old_total: [number, number];
  empty_dirs: string[]; temp_dirs: [string, number, number][]; temp_files: FileRow[]; installers: FileRow[];
}
export type Delta = [number, number, number, number, string]; // avant, après, fichiers avant/après, état
export interface Diff { old: Scan; new: Scan; deltas: Record<string, Delta>; children: Record<string, string[]>; focus: string[] }
export interface ActionItem { src: string; size: number; dst: string | null; status: string; error: string }
export interface ActionRecord {
  id: string; kind: "trash" | "move" | "archive"; created: number; simulated: boolean; items: ActionItem[];
  target: string; trash_originals: boolean; undone: boolean; note: string; label: string; undoable: boolean;
}
export interface Target {
  key: string; title: string; description: string; kind: string; paths: string[]; sizes: [string, number][];
  size: number; count: number; note: string; recommended: boolean; size_known: boolean;
}
export interface Release { version: string; notes: string; page_url: string; asset_size: number }

type Args = Record<string, unknown>;

export const call = <T>(cmd: string, args: Args = {}) => invoke<T>(cmd, args);

const handlers = new Map<string, (data: any) => void>();
listen<{ task: string; data: unknown }>("progress", (e) => handlers.get(e.payload.task)?.(e.payload.data));
let seq = 0;

export interface Task<T> { id: string; promise: Promise<T>; cancel: () => void }

/** Tâche longue côté Rust : progression par événements, annulable. */
export function run<T>(cmd: string, args: Args = {}, progress?: (data: any) => void): Task<T> {
  const id = `${cmd}-${++seq}`;
  if (progress) handlers.set(id, progress);
  const promise = invoke<T>(cmd, { ...args, task: id }).finally(() => handlers.delete(id));
  return { id, promise, cancel: () => void invoke("cancel", { task: id }) };
}

export const reveal = (path: string) => call("reveal", { path }).catch(() => {});
export const copyPath = (path: string) => navigator.clipboard.writeText(path);
