//! Couche Tauri : une commande par action de l'interface. Les tâches longues tournent hors du
//! fil principal, envoient des événements `progress` ({task, data}) et s'arrêtent par `cancel`.

mod settings;

use std::collections::{BinaryHeap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use base64::Engine;
use debarras_core::actions::{self, ActionLog, ActionRecord, Kind, Status};
use debarras_core::cache::{Cache, ScanInfo, default_db_path};
use debarras_core::safety::{self, Classifier, Env, Level};
use debarras_core::{
    cleanup, drives, duplicates, history, paths, recycle, samename, scanner, search, similar,
    stats, updater,
};
use serde_json::{Value, json};
use settings::Settings;
use tauri::{AppHandle, Emitter, Manager, State};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
/// Dossiers détaillés au plus par la carte (borne les requêtes SQLite).
const TREE_BUDGET: usize = 40_000;
const TREE_MAX_DEPTH: usize = 9;
/// Enfants gardés par dossier dans la carte ; le reste est regroupé.
const TREE_MAX_CHILDREN: usize = 400;

type Res = Result<Value, String>;

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

pub struct App {
    db: PathBuf,
    env: Env,
    app_dir: Option<String>,
    /// Lectures rapides (carte, arborescence) ; les tâches longues ouvrent leur connexion.
    cache: Mutex<Cache>,
    settings: Mutex<Settings>,
    /// Classement du scan affiché (un classement sans logiciels repérés avant l'analyse).
    clf: RwLock<Arc<Classifier>>,
    tasks: Mutex<HashMap<String, Arc<AtomicBool>>>,
    action_busy: AtomicBool,
    release: Mutex<Option<updater::Release>>,
    /// Mise à jour téléchargée et vérifiée, à installer : (version, dossier préparé).
    ready: Mutex<Option<(String, PathBuf)>>,
}

impl App {
    fn new() -> Result<App, String> {
        let db = default_db_path().map_err(err)?;
        let env = Env::current();
        let app_dir = updater::install_dir().map(|d| d.to_string_lossy().into_owned());
        let clf = Classifier::new(&env, Vec::<String>::new(), app_dir.as_deref(), Vec::new());
        Ok(App {
            cache: Mutex::new(Cache::open(&db).map_err(err)?),
            settings: Mutex::new(Settings::load(&Settings::path(&db))),
            db,
            env,
            app_dir,
            clf: RwLock::new(Arc::new(clf)),
            tasks: Mutex::default(),
            action_busy: AtomicBool::new(false),
            release: Mutex::default(),
            ready: Mutex::default(),
        })
    }

    fn classifier(&self) -> Arc<Classifier> {
        self.clf.read().unwrap().clone()
    }

    fn log(&self) -> ActionLog {
        ActionLog {
            path: self.db.with_file_name("actions.jsonl"),
        }
    }
}

// --- tâches longues --------------------------------------------------------------------

/// Lance `f` hors du fil principal ; `emit` envoie un événement `progress` pour `task`.
async fn job<F>(app: AppHandle, task: String, f: F) -> Res
where
    F: FnOnce(&App, &AtomicBool, &dyn Fn(Value)) -> Res + Send + 'static,
{
    let flag = Arc::new(AtomicBool::new(false));
    app.state::<App>()
        .tasks
        .lock()
        .unwrap()
        .insert(task.clone(), flag.clone());
    let a = app.clone();
    let t = task.clone();
    let out = tauri::async_runtime::spawn_blocking(move || {
        let emit = |data: Value| {
            let _ = a.emit("progress", json!({"task": t, "data": data}));
        };
        f(&a.state::<App>(), &flag, &emit)
    })
    .await
    .map_err(err)?;
    app.state::<App>().tasks.lock().unwrap().remove(&task);
    out
}

#[tauri::command]
fn cancel(st: State<App>, task: String) {
    if let Some(f) = st.tasks.lock().unwrap().get(&task) {
        f.store(true, Ordering::Relaxed);
    }
}

/// Limite un flux de progression à un événement toutes les 100 ms.
struct Throttle(Option<Instant>);

impl Throttle {
    fn ready(&mut self) -> bool {
        let go = self
            .0
            .is_none_or(|t| t.elapsed() >= Duration::from_millis(100));
        if go {
            self.0 = Some(Instant::now());
        }
        go
    }
}

// --- conversions -----------------------------------------------------------------------

fn scan_json(s: &ScanInfo) -> Value {
    json!({
        "id": s.id, "root": s.root, "started": s.started, "finished": s.finished,
        "total_size": s.total_size, "file_count": s.file_count, "dir_count": s.dir_count,
        "errors": s.errors,
    })
}

fn drive_json(d: &drives::Drive) -> Value {
    json!({"root": d.root, "title": d.title(), "total": d.total, "free": d.free, "used": d.used()})
}

fn level(l: Level) -> &'static str {
    l.as_str()
}

fn verdict_json(v: &safety::Verdict) -> Value {
    json!({"level": level(v.level), "reason": v.reason})
}

fn record_json(rec: &ActionRecord) -> Value {
    let mut v = serde_json::to_value(rec).unwrap_or_default();
    v["label"] = json!(rec.kind.label());
    v["undoable"] = json!(rec.undoable());
    v
}

// --- démarrage, disques, scans ---------------------------------------------------------

#[tauri::command]
fn init(st: State<App>) -> Res {
    let roots = st.cache.lock().unwrap().roots().map_err(err)?;
    let folder = std::env::args()
        .skip(1)
        .find(|a| Path::new(a).is_dir())
        .map(|a| recycle::absolute(&a));
    Ok(json!({
        "version": VERSION,
        "data_dir": st.db.parent(),
        "admin": drives::is_admin(),
        "roots": roots,
        "settings": *st.settings.lock().unwrap(),
        "folder": folder,
        "self_update": updater::can_self_update(),
        "no_update_check": std::env::var_os("DEBARRAS_NO_UPDATE_CHECK").is_some(),
    }))
}

#[tauri::command]
fn list_drives(st: State<App>) -> Res {
    let c = st.cache.lock().unwrap();
    Ok(drives::list_drives()
        .iter()
        .map(|d| {
            let scan = c.latest_scan(&d.root).ok().flatten();
            let mut v = drive_json(d);
            v["scan"] = scan.as_ref().map(scan_json).into();
            v
        })
        .collect())
}

/// Dernier scan terminé de `root` et le disque s'il s'agit d'un disque entier.
#[tauri::command]
fn latest_scan(st: State<App>, root: String) -> Res {
    let scan = st.cache.lock().unwrap().latest_scan(&root).map_err(err)?;
    let drive = drives::drive_of(&root);
    Ok(json!({
        "scan": scan.as_ref().map(scan_json),
        "drive": drive.as_ref().map(drive_json),
    }))
}

#[tauri::command]
async fn scan(app: AppHandle, task: String, root: String) -> Res {
    job(app, task, move |st, cancel, emit| {
        let opts = st.settings.lock().unwrap().scan_options();
        let r = scanner::scan(&root, &st.db, &opts, cancel, |files, size, current| {
            emit(json!({"files": files, "size": size, "current": current}))
        })?;
        Ok(json!({
            "root": r.root, "cancelled": r.cancelled, "duration": r.duration,
            "file_count": r.file_count,
        }))
    })
    .await
}

#[tauri::command]
fn list_scans(st: State<App>, root: String) -> Res {
    let scans = st
        .cache
        .lock()
        .unwrap()
        .list_scans(Some(&root))
        .map_err(err)?;
    Ok(scans.iter().map(scan_json).collect())
}

#[tauri::command]
fn delete_scan(st: State<App>, id: i64) -> Res {
    st.cache.lock().unwrap().delete_scan(id).map_err(err)?;
    Ok(Value::Null)
}

// --- classement de sécurité ------------------------------------------------------------

/// Repère les logiciels du scan et répartit l'espace par niveau ; le classement devient
/// celui de toutes les vues.
#[tauri::command]
async fn safety_summary(app: AppHandle, task: String, scan_id: i64, root: String) -> Res {
    job(app, task, move |st, cancel, _| {
        let Some((clf, s)) = safety::analyze(
            &st.db,
            scan_id,
            &root,
            st.app_dir.as_deref(),
            &st.env,
            cancel,
        )
        .map_err(|e| format!("Analyse de sécurité impossible : {e}"))?
        else {
            return Ok(Value::Null);
        };
        *st.clf.write().unwrap() = Arc::new(clf);
        let totals: HashMap<&str, i64> = s.totals.iter().map(|(l, v)| (level(*l), *v)).collect();
        let top: HashMap<&str, Vec<Value>> = s
            .top
            .iter()
            .map(|(l, v)| {
                let rows = v
                    .iter()
                    .map(|(p, size, reason)| json!([p, size, reason]))
                    .collect();
                (level(*l), rows)
            })
            .collect();
        Ok(json!({"scan_id": scan_id, "totals": totals, "top": top}))
    })
    .await
}

/// Verdict de chaque chemin (`is_dir` : `null` pour le demander au disque).
#[tauri::command]
fn classify(st: State<App>, items: Vec<(String, Option<bool>)>) -> Res {
    let clf = st.classifier();
    Ok(items
        .iter()
        .map(|(p, d)| verdict_json(&clf.classify(p, *d)))
        .collect())
}

/// Détail d'un élément : verdict, logiciels contenus, dossier ou non.
#[tauri::command]
fn inspect(st: State<App>, path: String) -> Res {
    let clf = st.classifier();
    let is_dir = Path::new(&path).is_dir();
    let v = clf.classify(&path, Some(is_dir));
    let inside = if is_dir && matches!(v.level, Level::Personal | Level::Cleanable) {
        clf.risky_inside(&path, 5)
    } else {
        vec![]
    };
    Ok(json!({"level": level(v.level), "reason": v.reason, "is_dir": is_dir, "inside": inside}))
}

// --- carte et arborescence -------------------------------------------------------------

struct Flat {
    path: String,
    name: String,
    size: i64,
    is_dir: bool,
    count: i64,
    mtime: f64,
    kids: Option<Vec<usize>>,
    rest: (i64, i64),
}

fn children(c: &Cache, scan_id: i64, path: &str) -> rusqlite::Result<Vec<Flat>> {
    let mut kids: Vec<Flat> = c
        .child_dirs(scan_id, path)?
        .into_iter()
        .filter(|d| d.1 > 0)
        .map(|(p, size, count)| Flat {
            name: paths::split(&p).1.to_string(),
            path: p,
            size,
            is_dir: true,
            count,
            mtime: 0.0,
            kids: None,
            rest: (0, 0),
        })
        .collect();
    kids.extend(c.child_files(path)?.into_iter().filter(|f| f.2 > 0).map(
        |(path, name, size, mtime)| Flat {
            path,
            name,
            size,
            is_dir: false,
            count: 0,
            mtime,
            kids: None,
            rest: (0, 0),
        },
    ));
    kids.sort_by(|a, b| b.size.cmp(&a.size).then_with(|| a.path.cmp(&b.path)));
    Ok(kids)
}

/// Sous-arbre de `path` pour la carte.
#[tauri::command]
fn tree(st: State<App>, scan_id: i64, path: String) -> Res {
    let c = st.cache.lock().unwrap();
    build_tree(
        &c,
        &st.classifier(),
        scan_id,
        &path,
        TREE_BUDGET,
        TREE_MAX_CHILDREN,
    )
    .map_err(err)
}

/// Les plus gros dossiers sont détaillés d'abord, dans la limite de `budget` éléments ; au-delà
/// de `max_children` enfants, le reste d'un dossier est regroupé (`x` : [nombre, taille]).
/// Chaque élément porte son niveau de sécurité.
fn build_tree(
    c: &Cache,
    clf: &Classifier,
    scan_id: i64,
    path: &str,
    budget: usize,
    max_children: usize,
) -> rusqlite::Result<Value> {
    let path = path.to_string();
    let (size, count) = c.dir_info(scan_id, &path)?.unwrap_or((0, 0));
    let mut arena = vec![Flat {
        name: paths::split(&path).1.to_string(),
        path: path.clone(),
        size,
        is_dir: true,
        count,
        mtime: 0.0,
        kids: None,
        rest: (0, 0),
    }];
    let mut heap = BinaryHeap::from([(size, 0usize, 0usize)]);
    while let Some((_, i, depth)) = heap.pop() {
        if arena.len() >= budget || depth >= TREE_MAX_DEPTH {
            continue;
        }
        let mut kids = children(c, scan_id, &arena[i].path)?;
        if kids.len() > max_children {
            let rest = kids.split_off(max_children);
            arena[i].rest = (rest.len() as i64, rest.iter().map(|k| k.size).sum());
        }
        let first = arena.len();
        for k in kids {
            if k.is_dir {
                heap.push((k.size, arena.len(), depth + 1));
            }
            arena.push(k);
        }
        arena[i].kids = Some((first..arena.len()).collect());
    }
    fn node(arena: &[Flat], i: usize, clf: &Classifier) -> Value {
        let f = &arena[i];
        let v = clf.classify(&f.path, Some(f.is_dir));
        let mut out = json!({
            "n": f.name, "p": f.path, "s": f.size, "d": f.is_dir, "c": f.count, "m": f.mtime,
            "l": level(v.level), "r": v.reason,
        });
        if let Some(k) = &f.kids {
            out["k"] = k.iter().map(|&j| node(arena, j, clf)).collect();
        }
        if f.rest.0 > 0 {
            out["x"] = json!([f.rest.0, f.rest.1]);
        }
        out
    }
    Ok(node(&arena, 0, clf))
}

/// Contenu direct d'un dossier : sous-dossiers [chemin, taille, fichiers] et fichiers
/// [chemin, nom, taille, date].
#[tauri::command]
fn list_dir(st: State<App>, scan_id: i64, path: String) -> Res {
    let c = st.cache.lock().unwrap();
    Ok(json!({
        "info": c.dir_info(scan_id, &path).map_err(err)?,
        "dirs": c.child_dirs(scan_id, &path).map_err(err)?,
        "files": c.child_files(&path).map_err(err)?,
    }))
}

/// Taille connue de chaque chemin dans le scan (actions lancées depuis les vues).
#[tauri::command]
fn sizes_of(st: State<App>, scan_id: i64, paths: Vec<String>) -> Res {
    let c = st.cache.lock().unwrap();
    let mut out = HashMap::new();
    for p in paths {
        let s = c.size_of(scan_id, &p).map_err(err)?;
        out.insert(p, s);
    }
    Ok(json!(out))
}

/// Tous les dossiers du scan (export CSV de l'arborescence).
#[tauri::command]
fn dir_table(st: State<App>, scan_id: i64) -> Res {
    let c = st.cache.lock().unwrap();
    let rows: Vec<(String, i64, i64)> = c
        .conn
        .prepare("SELECT path, size, file_count FROM dir_sizes WHERE scan_id=? ORDER BY size DESC")
        .map_err(err)?
        .query_map([scan_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .map_err(err)?
        .collect::<rusqlite::Result<_>>()
        .map_err(err)?;
    Ok(json!(rows))
}

#[tauri::command]
fn exists(paths: Vec<String>) -> Vec<bool> {
    paths.iter().map(|p| Path::new(p).exists()).collect()
}

// --- statistiques, historique, recherche -----------------------------------------------

fn files_json(files: &[stats::FileStat]) -> Value {
    files
        .iter()
        .map(|f| json!([f.path, f.size, f.mtime, f.last_used()]))
        .collect()
}

#[tauri::command]
async fn compute_stats(
    app: AppHandle,
    task: String,
    scan_id: i64,
    root: String,
    old_days: i64,
) -> Res {
    job(app, task, move |st, _, _| {
        let c = Cache::open(&st.db).map_err(err)?;
        let r = stats::compute_stats(&c.conn, scan_id, &root, old_days)
            .map_err(|e| format!("Calcul des statistiques impossible : {e}"))?;
        Ok(json!({
            "scan_id": r.scan_id, "root": r.root, "total_size": r.total_size,
            "file_count": r.file_count, "by_category": r.by_category, "by_ext": r.by_ext,
            "largest": files_json(&r.largest), "old_days": r.old_days, "old": files_json(&r.old),
            "old_total": r.old_total, "empty_dirs": r.empty_dirs,
            "temp_dirs": r.temp_dirs.iter().map(|d| json!([d.path, d.size, d.file_count])).collect::<Vec<_>>(),
            "temp_files": files_json(&r.temp_files), "installers": files_json(&r.installers),
        }))
    })
    .await
}

#[tauri::command]
async fn compare(app: AppHandle, task: String, old_id: i64, new_id: i64) -> Res {
    job(app, task, move |st, _, _| {
        let c = Cache::open(&st.db).map_err(err)?;
        let scans = c.list_scans(None).map_err(err)?;
        let find = |id| scans.iter().find(|s| s.id == id).ok_or("scan introuvable");
        let d = history::compare(&c.conn, find(old_id)?, find(new_id)?).map_err(err)?;
        let deltas: HashMap<&String, Value> = d
            .deltas
            .iter()
            .map(|(p, x)| {
                (
                    p,
                    json!([
                        x.old_size,
                        x.new_size,
                        x.old_count,
                        x.new_count,
                        x.status.as_str()
                    ]),
                )
            })
            .collect();
        Ok(json!({
            "old": scan_json(&d.old), "new": scan_json(&d.new), "deltas": deltas,
            "children": d.children, "focus": d.focus,
        }))
    })
    .await
}

#[derive(serde::Deserialize)]
struct Query {
    text: String,
    category: Option<String>,
    exts: Vec<String>,
    min_size: i64,
    max_size: i64,
    newer_than_days: i64,
    older_than_days: i64,
}

#[tauri::command]
async fn search_files(app: AppHandle, task: String, root: String, q: Query) -> Res {
    job(app, task, move |st, _, _| {
        let c = Cache::open(&st.db).map_err(err)?;
        let q = search::SearchQuery {
            text: q.text,
            category: q.category,
            exts: q.exts,
            min_size: q.min_size,
            max_size: q.max_size,
            newer_than_days: q.newer_than_days,
            older_than_days: q.older_than_days,
        };
        let r = search::search_files(&c.conn, &root, &q, search::MAX_RESULTS).map_err(err)?;
        let hits: Vec<Value> = r
            .hits
            .iter()
            .map(|h| json!([h.path, h.name, h.size, h.mtime]))
            .collect();
        Ok(json!({"hits": hits, "count": r.count, "total_size": r.total_size, "max": search::MAX_RESULTS}))
    })
    .await
}

// --- doublons, images similaires, même nom ---------------------------------------------

fn finder_progress(emit: &dyn Fn(Value)) -> impl FnMut(&str, u64, u64) + '_ {
    move |phase, done, total| emit(json!({"phase": phase, "done": done, "total": total}))
}

#[tauri::command]
async fn find_duplicates(app: AppHandle, task: String, root: String, min_size: i64) -> Res {
    job(app, task, move |st, cancel, emit| {
        let c = Cache::open(&st.db).map_err(err)?;
        let mut p = finder_progress(emit);
        let r = duplicates::find_duplicates(&c, &root, min_size, cancel, &mut p).map_err(err)?;
        let groups: Vec<Value> = r
            .groups
            .iter()
            .map(|g| {
                let files: Vec<Value> = g
                    .files
                    .iter()
                    .map(|f| json!({"path": f.path, "size": f.size, "mtime": f.mtime}))
                    .collect();
                json!({"size": g.size, "digest": g.digest, "files": files})
            })
            .collect();
        Ok(json!({
            "root": r.root, "groups": groups, "candidates": r.candidates,
            "hashed_bytes": r.hashed_bytes, "skipped_cloud": r.skipped_cloud,
            "skipped_changed": r.skipped_changed, "skipped_error": r.skipped_error,
            "cancelled": r.cancelled, "duration": r.duration,
        }))
    })
    .await
}

#[tauri::command]
async fn find_similar(
    app: AppHandle,
    task: String,
    root: String,
    min_size: i64,
    threshold: u32,
) -> Res {
    job(app, task, move |st, cancel, emit| {
        let c = Cache::open(&st.db).map_err(err)?;
        let mut p = finder_progress(emit);
        let r =
            similar::find_similar(&c, &root, min_size, threshold, cancel, &mut p).map_err(err)?;
        let b64 = base64::engine::general_purpose::STANDARD;
        let groups: Vec<Value> = r
            .groups
            .iter()
            .map(|g| {
                let files: Vec<Value> = g
                    .files
                    .iter()
                    .map(|f| {
                        json!({
                            "path": f.path, "size": f.size, "mtime": f.mtime, "width": f.width,
                            "height": f.height, "distance": f.distance,
                            "thumb": (!f.thumb.is_empty()).then(|| b64.encode(&f.thumb)),
                        })
                    })
                    .collect();
                json!({"files": files})
            })
            .collect();
        Ok(json!({
            "root": r.root, "groups": groups, "candidates": r.candidates, "hashed": r.hashed,
            "skipped_cloud": r.skipped_cloud, "skipped_changed": r.skipped_changed,
            "skipped_error": r.skipped_error, "cancelled": r.cancelled, "duration": r.duration,
        }))
    })
    .await
}

#[tauri::command]
async fn find_same_names(
    app: AppHandle,
    task: String,
    root: String,
    min_size: i64,
    personal_only: bool,
    skip_dev: bool,
) -> Res {
    job(app, task, move |st, cancel, emit| {
        let c = Cache::open(&st.db).map_err(err)?;
        let mut p = finder_progress(emit);
        let r = samename::find_same_names(&c, &root, min_size, personal_only, skip_dev, cancel, &mut p)
            .map_err(err)?;
        let groups: Vec<Value> = r
            .groups
            .iter()
            .map(|g| {
                let files: Vec<Value> = g
                    .files
                    .iter()
                    .map(|f| json!({"path": f.path, "size": f.size, "mtime": f.mtime, "version": f.version}))
                    .collect();
                json!({"name": g.name, "versions": g.versions(), "files": files})
            })
            .collect();
        Ok(json!({
            "root": r.root, "groups": groups, "candidates": r.candidates,
            "hashed_bytes": r.hashed_bytes, "skipped_generic": r.skipped_generic,
            "skipped_cloud": r.skipped_cloud, "skipped_changed": r.skipped_changed,
            "skipped_error": r.skipped_error, "cancelled": r.cancelled, "duration": r.duration,
        }))
    })
    .await
}

/// Aperçu d'une image (420 px au plus, orientation EXIF appliquée), en JPEG base64.
#[tauri::command]
async fn preview(path: String) -> Res {
    tauri::async_runtime::spawn_blocking(move || {
        use image::ImageDecoder;
        let mut dec = image::ImageReader::open(&path)
            .map_err(err)?
            .with_guessed_format()
            .map_err(err)?
            .into_decoder()
            .map_err(err)?;
        let orientation = dec.orientation().map_err(err)?;
        let mut img = image::DynamicImage::from_decoder(dec).map_err(err)?;
        img.apply_orientation(orientation);
        let mut buf = Vec::new();
        let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 85);
        img.thumbnail(420, 420)
            .to_rgb8()
            .write_with_encoder(enc)
            .map_err(err)?;
        Ok(json!(base64::engine::general_purpose::STANDARD.encode(buf)))
    })
    .await
    .map_err(err)?
}

/// Chemins protégés par la liste blanche (jamais cochés d'office).
#[tauri::command]
fn protected(st: State<App>, paths: Vec<String>) -> Vec<bool> {
    let wl = st.settings.lock().unwrap().whitelist.clone();
    paths
        .iter()
        .map(|p| actions::protection(p, &wl).is_some())
        .collect()
}

// --- nettoyage guidé -------------------------------------------------------------------

#[tauri::command]
async fn cleanup_analyze(app: AppHandle, task: String) -> Res {
    job(app, task, move |st, cancel, _| {
        Ok(cleanup::analyze(&st.env, cancel)
            .iter()
            .map(|t| {
                json!({
                    "key": t.key, "title": t.title, "description": t.description, "kind": t.kind,
                    "paths": t.paths, "sizes": t.sizes, "size": t.size(), "count": t.count,
                    "note": t.note, "recommended": t.recommended, "size_known": t.size_known,
                })
            })
            .collect())
    })
    .await
}

#[tauri::command]
fn open_windows_cleanup(st: State<App>) -> Result<(), String> {
    cleanup::open_windows_cleanup(&st.env).map_err(err)
}

#[tauri::command]
fn open_recycle_bin() -> Result<(), String> {
    cleanup::open_recycle_bin().map_err(err)
}

// --- actions ---------------------------------------------------------------------------

fn kind_of(kind: &str) -> Result<Kind, String> {
    serde_json::from_value(json!(kind)).map_err(err)
}

/// Avant confirmation : éléments système (refusés), logiciels (avertissement) et capacité de
/// la corbeille (Windows effacerait définitivement ce qui ne tient pas).
#[tauri::command]
fn prepare_action(
    st: State<App>,
    kind: String,
    paths: Vec<String>,
    sizes: HashMap<String, i64>,
) -> Res {
    let clf = st.classifier();
    let (mut blocked, mut warnings) = (vec![], vec![]);
    for p in &paths {
        let v = clf.classify(p, None);
        if v.blocked() {
            blocked.push(format!("{p} — {}", v.reason));
        } else if v.level == Level::Software {
            warnings.push(format!("{p} — {}", v.reason));
        } else if Path::new(p).is_dir() {
            let inside = clf.risky_inside(p, 5);
            if !inside.is_empty() {
                warnings.push(format!(
                    "{p} — contient des logiciels : {}",
                    inside.join(", ")
                ));
            }
        }
    }
    let bin = if kind_of(&kind)? == Kind::Move {
        vec![]
    } else {
        recycle::capacity_problems(&paths, &sizes, &recycle::bin_info)
    };
    Ok(json!({"blocked": blocked, "warnings": warnings, "bin": bin}))
}

fn action_progress(emit: &dyn Fn(Value)) -> impl FnMut(usize, usize, &str) + '_ {
    let mut t = Throttle(None);
    move |done, total, path| {
        if t.ready() || done == total {
            emit(json!({"done": done, "total": total, "path": path}))
        }
    }
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn run_action(
    app: AppHandle,
    task: String,
    kind: String,
    paths: Vec<String>,
    sizes: HashMap<String, i64>,
    target: String,
    trash_originals: bool,
    simulated: bool,
) -> Res {
    job(app, task, move |st, cancel, emit| {
        let whitelist = st.settings.lock().unwrap().whitelist.clone();
        let clf = st.classifier();
        let ctx = actions::Context {
            env: &st.env,
            classifier: &clf,
            whitelist: &whitelist,
        };
        let mut rec = actions::plan(
            kind_of(&kind)?,
            &paths,
            &sizes,
            &target,
            trash_originals,
            simulated,
            &ctx,
        );
        if rec.items.iter().all(|i| i.status == Status::Skipped) {
            return Ok(json!({"nothing": true, "record": record_json(&rec)}));
        }
        if st.action_busy.swap(true, Ordering::SeqCst) {
            return Err("Une action est déjà en cours.".into());
        }
        let log = st.log();
        let _ = log.save(&rec); // trace même en cas de plantage
        actions::execute(
            &mut rec,
            &recycle::bin_info,
            &mut action_progress(emit),
            &|| cancel.load(Ordering::Relaxed),
        );
        if let Err(e) = log.save(&rec) {
            rec.note += &format!(" (journal non écrit : {e})");
        }
        st.action_busy.store(false, Ordering::SeqCst);
        Ok(json!({"nothing": false, "record": record_json(&rec)}))
    })
    .await
}

#[tauri::command]
fn last_undoable(st: State<App>) -> Value {
    st.log().last_undoable().as_ref().map(record_json).into()
}

#[tauri::command]
fn action_log(st: State<App>) -> Value {
    st.log().load().iter().rev().map(record_json).collect()
}

#[tauri::command]
async fn undo_last(app: AppHandle, task: String) -> Res {
    job(app, task, move |st, _, emit| {
        let log = st.log();
        let Some(mut rec) = log.last_undoable() else {
            return Ok(Value::Null);
        };
        if st.action_busy.swap(true, Ordering::SeqCst) {
            return Err("Une action est déjà en cours.".into());
        }
        actions::undo(&mut rec, &mut action_progress(emit));
        if let Err(e) = log.save(&rec) {
            rec.note += &format!(" (journal non écrit : {e})");
        }
        st.action_busy.store(false, Ordering::SeqCst);
        Ok(record_json(&rec))
    })
    .await
}

// --- divers : paramètres, fichiers, Explorateur ----------------------------------------

#[tauri::command]
fn save_settings(st: State<App>, mut settings: Settings) -> Result<(), String> {
    let mut s = st.settings.lock().unwrap();
    // Tenus à jour par le moteur (vérification des mises à jour) : jamais reculés par l'interface.
    settings.last_update_check = settings.last_update_check.max(s.last_update_check);
    *s = settings;
    s.save(&Settings::path(&st.db)).map_err(err)
}

#[tauri::command]
fn default_settings() -> Settings {
    Settings::default()
}

/// Écrit un export (CSV avec BOM pour Excel, ou page HTML).
#[tauri::command]
fn write_text(path: String, content: String, bom: bool) -> Result<(), String> {
    let mut data = if bom {
        "\u{feff}".to_string()
    } else {
        String::new()
    };
    data += &content;
    std::fs::write(path, data).map_err(err)
}

/// Ouvre l'Explorateur sur l'élément sélectionné (n'exécute jamais le fichier).
#[tauri::command]
fn reveal(path: String) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let p = recycle::absolute(&path);
        std::process::Command::new("explorer")
            .raw_arg(format!("/select,\"{p}\""))
            .spawn()
            .map(drop)
            .map_err(err)
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err("Explorateur indisponible".into())
    }
}

/// Ouvre un dossier, une page HTML exportée ou un lien https par le shell.
#[tauri::command]
fn open_path(target: String) -> Result<(), String> {
    let web = target.starts_with("https://");
    let local = Path::new(&target);
    if !web && !(local.is_dir() || target.to_lowercase().ends_with(".html")) {
        return Err("ouverture refusée".into()); // jamais un exécutable
    }
    std::process::Command::new("explorer")
        .arg(&target)
        .spawn()
        .map(drop)
        .map_err(err)
}

#[tauri::command]
fn relaunch_admin(app: AppHandle, root: String) -> Result<bool, String> {
    let args: Vec<String> = if root.is_empty() { vec![] } else { vec![root] };
    let ok = drives::relaunch_as_admin(&args).map_err(err)?;
    if ok {
        app.exit(0);
    }
    Ok(ok)
}

// --- mises à jour ----------------------------------------------------------------------

fn remember_check(st: &App) {
    let mut s = st.settings.lock().unwrap();
    s.last_update_check = debarras_core::cache::now();
    let _ = s.save(&Settings::path(&st.db));
}

/// Dernière version publiée si elle est plus récente (`null` sinon).
#[tauri::command]
async fn update_check(app: AppHandle, task: String) -> Res {
    job(app, task, move |st, _, _| {
        let rel =
            updater::fetch_latest(updater::GITHUB_REPO, &updater::api(), VERSION).map_err(err)?;
        remember_check(st);
        let out = rel.as_ref().map(|r| {
            json!({
                "version": r.version, "notes": r.notes, "page_url": r.page_url,
                "asset_size": r.asset_size,
            })
        });
        *st.release.lock().unwrap() = rel;
        Ok(out.into())
    })
    .await
}

#[tauri::command]
fn skip_version(st: State<App>, version: String) {
    st.settings.lock().unwrap().skipped_version = version;
    remember_check(&st);
}

/// Télécharge et vérifie la version trouvée par `update_check`, prête à installer.
#[tauri::command]
async fn update_download(app: AppHandle, task: String) -> Res {
    job(app, task, move |st, cancel, emit| {
        let rel = st
            .release
            .lock()
            .unwrap()
            .clone()
            .ok_or("aucune version à installer")?;
        let work = updater::work_dir().map_err(err)?;
        let mut t = Throttle(None);
        let zip = updater::download(
            &rel,
            &work,
            VERSION,
            &mut |done, total| {
                if t.ready() {
                    emit(json!({"done": done, "total": total}))
                }
            },
            &|| cancel.load(Ordering::Relaxed),
        )
        .map_err(err)?;
        let staged =
            updater::stage(&zip, &work.join(format!("Debarras-{}", rel.version))).map_err(err)?;
        let _ = std::fs::remove_file(zip);
        *st.ready.lock().unwrap() = Some((rel.version.clone(), staged));
        Ok(json!(rel.version))
    })
    .await
}

/// Lance le script de remplacement ; `true` s'il a démarré (Débarras doit alors se fermer).
fn start_swap(st: &App, relaunch: bool, timeout: Duration) -> bool {
    let Some((_, staged)) = st.ready.lock().unwrap().take() else {
        return false;
    };
    let Some(install) = updater::install_dir() else {
        return false;
    };
    let Ok(work) = updater::work_dir() else {
        return false;
    };
    let marker = work.join("swap_started");
    let _ = std::fs::remove_file(&marker);
    let Ok(script) = updater::write_swap_script(
        std::process::id(),
        &install,
        &staged,
        updater::EXE_NAME,
        &std::env::temp_dir(),
        Some(&marker),
        relaunch,
    ) else {
        return false;
    };
    updater::launch_swap(&script).is_ok() && updater::wait_started(&marker, timeout, &mut || {})
}

/// Installe maintenant et redémarre. `false` : rien n'a démarré, Débarras reste ouvert.
#[tauri::command]
async fn update_install(app: AppHandle) -> Result<bool, String> {
    if app.state::<App>().action_busy.load(Ordering::SeqCst) {
        return Err(
            "Une action est en cours : relancez la mise à jour quand elle sera terminée.".into(),
        );
    }
    let a = app.clone();
    let ok = tauri::async_runtime::spawn_blocking(move || {
        start_swap(&a.state::<App>(), true, Duration::from_secs(30))
    })
    .await
    .map_err(err)?;
    if ok {
        app.exit(0);
    }
    Ok(ok)
}

// --- démarrage -------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    updater::cleanup_after_update(VERSION); // restes d'une mise à jour déjà installée
    let state = match App::new() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Débarras : démarrage impossible : {e}");
            std::process::exit(1);
        }
    };
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
            if let Some(folder) = args.iter().skip(1).find(|a| Path::new(a).is_dir()) {
                let _ = app.emit("open-folder", recycle::absolute(folder));
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            cancel,
            init,
            list_drives,
            latest_scan,
            scan,
            list_scans,
            delete_scan,
            safety_summary,
            classify,
            inspect,
            tree,
            list_dir,
            sizes_of,
            dir_table,
            exists,
            compute_stats,
            compare,
            search_files,
            find_duplicates,
            find_similar,
            find_same_names,
            preview,
            protected,
            cleanup_analyze,
            open_windows_cleanup,
            open_recycle_bin,
            prepare_action,
            run_action,
            last_undoable,
            action_log,
            undo_last,
            save_settings,
            default_settings,
            write_text,
            reveal,
            open_path,
            relaunch_admin,
            update_check,
            skip_version,
            update_download,
            update_install,
        ])
        .build(tauri::generate_context!())
        .expect("Débarras : démarrage de Tauri impossible")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                let st = app.state::<App>();
                // Mise à jour téléchargée : installée à la fermeture, sans relance.
                if !st.action_busy.load(Ordering::SeqCst) {
                    start_swap(&st, false, Duration::from_secs(10));
                }
                for f in st.tasks.lock().unwrap().values() {
                    f.store(true, Ordering::Relaxed);
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    #[test]
    fn tree_details_biggest_dirs_and_groups_the_rest() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        let put = |rel: &str, n: usize| {
            let p = root.join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, vec![b'x'; n]).unwrap();
        };
        put("gros/a/f.bin", 5000);
        put("gros/b.bin", 3000);
        put("petit/c.bin", 10);
        for i in 0..5 {
            put(&format!("vrac/{i}.txt"), 100 + i);
        }
        put("vide.txt", 0); // taille nulle : jamais dessinée
        let db = tmp.path().join("c.db");
        let r = root.to_string_lossy().into_owned();
        let res = scanner::scan(
            &r,
            &db,
            &scanner::ScanOptions::default(),
            &AtomicBool::new(false),
            |_, _, _| {},
        )
        .unwrap();
        let c = Cache::open(&db).unwrap();
        let clf = Classifier::new(&Env::current(), Vec::<String>::new(), None, Vec::new());
        let t = build_tree(&c, &clf, res.scan_id, &r, 100, 3).unwrap();
        assert_eq!(t["s"], 8520);
        let names: Vec<&str> = t["k"]
            .as_array()
            .unwrap()
            .iter()
            .map(|k| k["n"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["gros", "vrac", "petit"]); // tailles décroissantes, sans « vide.txt »
        let vrac = &t["k"][1];
        assert_eq!(vrac["k"].as_array().unwrap().len(), 3);
        assert_eq!(vrac["x"], json!([2, 201])); // 0.txt (100) et 1.txt (101) regroupés
        assert_eq!(t["k"][0]["k"][0]["k"][0]["n"], "f.bin"); // détaillé en profondeur
        // Niveau du classement courant (le dossier temporaire peut être en nom court 8.3).
        let gros = t["k"][0]["p"].as_str().unwrap();
        assert_eq!(t["k"][0]["l"], level(clf.classify(gros, Some(true)).level));
        // Budget épuisé : les dossiers restants ne sont pas détaillés.
        let small = build_tree(&c, &clf, res.scan_id, &r, 2, 3).unwrap();
        assert!(small["k"][0].get("k").is_none());
    }
}
