//! Scan récursif d'un dossier vers le cache, avec progression et annulation.
//! Port de `core/scanner.py` : mêmes lignes en base, mêmes compteurs.
//!
//! Points tenus à l'identique de Python (sinon un rescan perdrait les empreintes en cache,
//! ou les sorties de référence divergeraient) :
//! - dates = `sec + nsec * 1e-9` comme `os.stat` (même valeur flottante au bit près) ;
//! - jonction : comptée « ignorée » ; lien symbolique : ni suivi ni compté ;
//! - extension = `os.path.splitext(name)[1].lower()` ;
//! - motifs de la liste noire = `fnmatch`, sans casse, sur les fichiers et les dossiers.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::cache::{Cache, DirRow, FileRow};
use crate::paths::norm;

const BATCH_SIZE: usize = 5000;
const PROGRESS_INTERVAL: Duration = Duration::from_millis(100);

/// Seul le dossier Windows est exclu : ses fichiers y figurent plusieurs fois (liens durs de
/// WinSxS), le parcourir fausserait les tailles. Le reste est analysé ; la sécurité vient du
/// classement.
pub fn default_excluded_paths() -> Vec<String> {
    vec![std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into())]
}

#[derive(Debug, Clone)]
pub struct ScanOptions {
    pub excluded_paths: Vec<String>,
    /// Noms exclus n'importe où (ex. « node_modules »).
    pub excluded_names: Vec<String>,
    /// Liste noire : motifs sur le nom (`*.log`…).
    pub ignore_patterns: Vec<String>,
}

impl Default for ScanOptions {
    fn default() -> Self {
        ScanOptions {
            excluded_paths: default_excluded_paths(),
            excluded_names: vec![],
            ignore_patterns: vec![],
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ScanResult {
    pub scan_id: i64,
    pub root: String,
    pub total_size: i64,
    pub file_count: i64,
    pub dir_count: i64,
    pub errors: i64,
    pub duration: f64,
    pub cancelled: bool,
}

/// Scanne `root` dans la base `db`. `progress(fichiers, octets, dossier courant)` est appelé
/// au plus toutes les 100 ms, puis une dernière fois avec un dossier vide. `Err` : erreur
/// fatale, message pour l'utilisateur (le scan entamé est alors marqué annulé).
pub fn scan(
    root: &str,
    db: &Path,
    options: &ScanOptions,
    cancel: &AtomicBool,
    mut progress: impl FnMut(i64, i64, &str),
) -> Result<ScanResult, String> {
    let root = std::path::absolute(root)
        .map_or_else(|_| root.to_string(), |p| p.to_string_lossy().into_owned());
    if !Path::new(&root).is_dir() {
        return Err(format!("Dossier introuvable : {root}"));
    }
    let mut cache = Cache::open(db).map_err(|e| format!("Cache inaccessible : {e}"))?;
    let filter = Filter::new(options);
    let scan_id = cache
        .start_scan(&root)
        .map_err(|e| format!("Cache inaccessible : {e}"))?;
    let t0 = Instant::now();
    let result = walk(&mut cache, scan_id, &root, &filter, cancel, &mut progress).map(|mut r| {
        r.duration = t0.elapsed().as_secs_f64();
        r
    });
    result.map_err(|e| {
        let _ = cache.cancel_scan(scan_id);
        format!("Erreur pendant le scan : {e}")
    })
}

fn walk(
    cache: &mut Cache,
    scan_id: i64,
    root: &str,
    filter: &Filter,
    cancel: &AtomicBool,
    progress: &mut impl FnMut(i64, i64, &str),
) -> rusqlite::Result<ScanResult> {
    let mut parents: HashMap<String, Option<String>> = HashMap::from([(root.to_string(), None)]);
    let mut direct: HashMap<String, [i64; 3]> = HashMap::new(); // dossier -> [taille, fichiers, ignorés]
    let mut batch: Vec<FileRow> = Vec::with_capacity(BATCH_SIZE);
    let (mut total_files, mut total_bytes, mut errors) = (0i64, 0i64, 0i64);
    let mut last_emit: Option<Instant> = None;
    let walker = Walker {
        filter,
        cancel,
        failed: AtomicBool::new(false),
        interrupted: AtomicBool::new(false),
    };

    // Les dossiers sont lus en parallèle (rayon) ; ce fil-ci seul écrit en base.
    let (tx, rx) = mpsc::channel::<Listing>();
    std::thread::scope(|s| {
        s.spawn(|| rayon::scope(|rs| walker.visit(rs, root.to_string(), tx)));
        for l in rx {
            total_files += l.count;
            total_bytes += l.size;
            errors += l.errors;
            for sub in l.subdirs {
                parents.insert(sub, Some(l.path.clone()));
            }
            batch.extend(l.files);
            if batch.len() >= BATCH_SIZE {
                if let Err(e) = cache.upsert_files(scan_id, &batch) {
                    walker.failed.store(true, Ordering::Relaxed); // arrête les lectures en cours
                    return Err(e);
                }
                batch.clear();
            }
            if last_emit.is_none_or(|t| t.elapsed() >= PROGRESS_INTERVAL) {
                last_emit = Some(Instant::now());
                progress(total_files, total_bytes, &l.path);
            }
            direct.insert(l.path, [l.size, l.count, l.skipped]);
        }
        Ok(())
    })?;
    let cancelled = walker.interrupted.load(Ordering::Relaxed);

    if !batch.is_empty() {
        cache.upsert_files(scan_id, &batch)?;
    }
    let dir_count = direct.len() as i64;
    if cancelled {
        cache.cancel_scan(scan_id)?;
    } else {
        cache.insert_dirs(scan_id, &aggregate(direct, &parents))?;
        cache.finish_scan(scan_id, root, total_bytes, total_files, dir_count, errors)?;
    }
    progress(total_files, total_bytes, "");
    Ok(ScanResult {
        scan_id,
        root: root.to_string(),
        total_size: total_bytes,
        file_count: total_files,
        dir_count,
        errors,
        duration: 0.0,
        cancelled,
    })
}

/// Contenu direct d'un dossier.
struct Listing {
    path: String,
    files: Vec<FileRow>,
    subdirs: Vec<String>,
    size: i64,
    count: i64,
    skipped: i64,
    errors: i64,
}

struct Walker<'a> {
    filter: &'a Filter,
    cancel: &'a AtomicBool,
    failed: AtomicBool,
    /// Au moins un dossier n'a pas été lu à cause de l'annulation.
    interrupted: AtomicBool,
}

impl<'a> Walker<'a> {
    fn visit<'s>(&'s self, s: &rayon::Scope<'s>, path: String, tx: mpsc::Sender<Listing>) {
        if self.failed.load(Ordering::Relaxed) {
            return;
        }
        if self.cancel.load(Ordering::Relaxed) {
            self.interrupted.store(true, Ordering::Relaxed);
            return;
        }
        let l = list_dir(path, self.filter);
        for sub in &l.subdirs {
            let (sub, tx) = (sub.clone(), tx.clone());
            s.spawn(move |s| self.visit(s, sub, tx));
        }
        let _ = tx.send(l); // récepteur fermé : le scan s'arrête sur une erreur
    }
}

fn list_dir(path: String, filter: &Filter) -> Listing {
    let mut l = Listing {
        path,
        files: vec![],
        subdirs: vec![],
        size: 0,
        count: 0,
        skipped: 0,
        errors: 0,
    };
    let entries = match fs::read_dir(&l.path) {
        Ok(entries) => entries,
        Err(_) => {
            // Accès refusé, dossier disparu, chemin trop long… : contenu inconnu, jamais « vide ».
            l.errors += 1;
            l.skipped += 1;
            return l;
        }
    };
    for entry in entries {
        let Some((entry, ft)) = entry
            .ok()
            .and_then(|e| e.file_type().ok().map(|ft| (e, ft)))
        else {
            l.errors += 1;
            l.skipped += 1;
            continue;
        };
        let name = entry.file_name().to_string_lossy().into_owned();
        if ft.is_dir() {
            let path = entry.path().to_string_lossy().into_owned();
            if filter.skip_dir(&name, &path) {
                l.skipped += 1;
            } else {
                l.subdirs.push(path);
            }
        } else if ft.is_symlink() {
            // Python : une jonction est un dossier à ne pas suivre (compté), un lien
            // symbolique n'est ni dossier ni fichier (ignoré sans être compté).
            if is_dir_junction(&entry) {
                l.skipped += 1;
            }
        } else if ft.is_file() {
            if filter.ignored(&name) {
                l.skipped += 1;
                continue;
            }
            let Ok(md) = entry.metadata() else {
                l.errors += 1;
                l.skipped += 1;
                continue;
            };
            let size = md.len() as i64;
            l.files.push(FileRow {
                parent: l.path.clone(),
                ext: extension(&name),
                name,
                size,
                mtime: py_time(md.modified()),
                atime: py_time(md.accessed()),
                ctime: py_time(md.created()),
            });
            l.size += size;
            l.count += 1;
        }
    }
    l
}

/// Remonte tailles, nombres de fichiers et d'éléments ignorés vers la racine.
fn aggregate(
    mut totals: HashMap<String, [i64; 3]>,
    parents: &HashMap<String, Option<String>>,
) -> Vec<DirRow> {
    let mut order: Vec<String> = totals.keys().cloned().collect();
    order.sort_by_key(|p| std::cmp::Reverse(p.matches('\\').count())); // les plus profonds d'abord
    for path in &order {
        if let Some(Some(parent)) = parents.get(path) {
            let child = totals[path];
            if let Some(t) = totals.get_mut(parent) {
                for (a, b) in t.iter_mut().zip(child) {
                    *a += b;
                }
            }
        }
    }
    totals
        .into_iter()
        .map(|(path, [size, file_count, skipped])| DirRow {
            parent: parents.get(&path).cloned().flatten(),
            path,
            size,
            file_count,
            skipped,
        })
        .collect()
}

struct Filter {
    excluded_paths: Vec<String>,
    excluded_names: Vec<String>,
    patterns: Vec<Vec<char>>,
}

impl Filter {
    fn new(o: &ScanOptions) -> Self {
        Filter {
            excluded_paths: o.excluded_paths.iter().map(|p| norm(p)).collect(),
            excluded_names: o.excluded_names.iter().map(|n| n.to_lowercase()).collect(),
            patterns: o
                .ignore_patterns
                .iter()
                .map(|p| p.trim())
                .filter(|p| !p.is_empty())
                .map(|p| p.to_lowercase().chars().collect())
                .collect(),
        }
    }

    fn ignored(&self, name: &str) -> bool {
        if self.patterns.is_empty() {
            return false;
        }
        let name: Vec<char> = name.to_lowercase().chars().collect();
        self.patterns.iter().any(|p| fnmatch(p, &name))
    }

    fn skip_dir(&self, name: &str, path: &str) -> bool {
        self.excluded_names.contains(&name.to_lowercase())
            || self.excluded_paths.contains(&norm(path))
            || self.ignored(name)
    }
}

/// `fnmatch` sur des textes déjà en minuscules : `*`, `?`, `[abc]`, `[a-z]`, `[!abc]` ;
/// un `[` sans `]` est un caractère ordinaire.
fn fnmatch(pat: &[char], s: &[char]) -> bool {
    let (mut p, mut i) = (0, 0);
    let mut star: Option<(usize, usize)> = None; // (position après `*`, position dans s)
    while i < s.len() {
        let step = match pat.get(p) {
            Some('*') => {
                star = Some((p + 1, i));
                p += 1;
                continue;
            }
            Some('?') => Some(1),
            Some('[') => match class(pat, p, s[i]) {
                Some((true, len)) => Some(len),
                Some((false, _)) => None,
                None => (s[i] == '[').then_some(1), // `[` littéral
            },
            Some(&c) => (c == s[i]).then_some(1),
            None => None,
        };
        match (step, star) {
            (Some(len), _) => {
                p += len;
                i += 1;
            }
            (None, Some((sp, si))) => {
                p = sp;
                i = si + 1;
                star = Some((sp, si + 1));
            }
            (None, None) => return false,
        }
    }
    pat[p..].iter().all(|&c| c == '*')
}

/// Classe `[...]` commençant à `pat[at]` : `Some((correspond, longueur))`, `None` si non fermée.
fn class(pat: &[char], at: usize, c: char) -> Option<(bool, usize)> {
    let mut j = at + 1;
    let negate = pat.get(j) == Some(&'!');
    if negate {
        j += 1;
    }
    let start = j;
    let mut hit = false;
    while j < pat.len() && (pat[j] != ']' || j == start) {
        if pat.get(j + 1) == Some(&'-') && pat.get(j + 2).is_some_and(|&e| e != ']') {
            hit |= pat[j] <= c && c <= pat[j + 2];
            j += 3;
        } else {
            hit |= pat[j] == c;
            j += 1;
        }
    }
    (j < pat.len()).then_some((hit != negate, j + 1 - at))
}

/// `os.path.splitext(name)[1].lower()` : les points de tête ne comptent pas (`.gitignore` → ``).
pub fn extension(name: &str) -> String {
    match name.rfind('.') {
        Some(dot) if name[..dot].chars().any(|c| c != '.') => name[dot..].to_lowercase(),
        _ => String::new(),
    }
}

/// Date au format de `os.stat` : `sec + nsec * 1e-9`, `sec` arrondi vers le bas.
fn py_time(t: io::Result<SystemTime>) -> f64 {
    let Ok(t) = t else { return 0.0 };
    let (sec, nsec) = match t.duration_since(UNIX_EPOCH) {
        Ok(d) => (d.as_secs() as i64, d.subsec_nanos()),
        Err(e) => {
            let d = e.duration();
            match d.subsec_nanos() {
                0 => (-(d.as_secs() as i64), 0),
                n => (-(d.as_secs() as i64) - 1, 1_000_000_000 - n),
            }
        }
    };
    sec as f64 + nsec as f64 * 1e-9
}

/// Le lien est-il une jonction (point de montage) vers un dossier ? Une erreur compte comme
/// une jonction : Python ne suit pas un dossier qu'il ne peut pas examiner.
#[cfg(windows)]
fn is_dir_junction(entry: &fs::DirEntry) -> bool {
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::fs::MetadataExt;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::Storage::FileSystem::{FindClose, FindFirstFileW, WIN32_FIND_DATAW};
    const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
    const IO_REPARSE_TAG_MOUNT_POINT: u32 = 0xA000_0003;

    let Ok(md) = entry.metadata() else {
        return true;
    };
    if md.file_attributes() & FILE_ATTRIBUTE_DIRECTORY == 0 {
        return false; // lien vers un fichier
    }
    let wide: Vec<u16> = entry.path().as_os_str().encode_wide().chain([0]).collect();
    // SAFETY : chemin terminé par un zéro ; `data` est une structure C remplie par l'appel.
    unsafe {
        let mut data: WIN32_FIND_DATAW = std::mem::zeroed();
        let h = FindFirstFileW(wide.as_ptr(), &mut data);
        if h == INVALID_HANDLE_VALUE {
            return true;
        }
        FindClose(h);
        data.dwReserved0 == IO_REPARSE_TAG_MOUNT_POINT
    }
}

#[cfg(not(windows))]
fn is_dir_junction(_entry: &fs::DirEntry) -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn write(root: &Path, rel: &str, size: usize) -> PathBuf {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, vec![b'x'; size]).unwrap();
        p
    }

    fn opts() -> ScanOptions {
        ScanOptions {
            excluded_paths: vec![],
            excluded_names: vec![],
            ignore_patterns: vec![],
        }
    }

    fn run(root: &Path, db: &Path, o: &ScanOptions) -> ScanResult {
        scan(
            root.to_str().unwrap(),
            db,
            o,
            &AtomicBool::new(false),
            |_, _, _| {},
        )
        .unwrap()
    }

    fn dirs(db: &Path, scan_id: i64) -> HashMap<String, (i64, i64, i64)> {
        let c = Cache::open(db).unwrap();
        let mut st = c
            .conn
            .prepare("SELECT path, size, file_count, skipped FROM dir_sizes WHERE scan_id=?")
            .unwrap();
        st.query_map([scan_id], |r| {
            Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?)))
        })
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
    }

    fn s(p: PathBuf) -> String {
        p.to_string_lossy().into_owned()
    }

    #[test]
    fn counts_and_aggregates() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        write(&root, "a/one.bin", 100);
        write(&root, "a/deep/two.bin", 250);
        write(&root, "b/three.txt", 50);
        fs::create_dir(root.join("empty")).unwrap();
        let r = run(&root, &db, &opts());
        assert_eq!(
            (r.file_count, r.total_size, r.dir_count, r.cancelled),
            (3, 400, 5, false)
        );
        let d = dirs(&db, r.scan_id);
        assert_eq!(d[&s(root.clone())], (400, 3, 0));
        assert_eq!(d[&s(root.join("a"))], (350, 2, 0));
        assert_eq!(d[&s(root.join("a").join("deep"))], (250, 1, 0));
        assert_eq!(d[&s(root.join("empty"))], (0, 0, 0));
        let info = Cache::open(&db)
            .unwrap()
            .latest_scan(&s(root))
            .unwrap()
            .unwrap();
        assert_eq!((info.status.as_str(), info.file_count), ("done", 3));
    }

    #[test]
    fn exclusions_blacklist_and_skipped_counter() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        write(&root, "keep/a.txt", 10);
        write(&root, "AppData/secret.bin", 10);
        write(&root, "skipme/b.bin", 10);
        write(&root, "logs/app.log", 10);
        write(&root, "proj/node_modules/x.js", 10);
        let o = ScanOptions {
            excluded_paths: vec![s(root.join("skipme"))],
            excluded_names: vec!["AppData".into()],
            ignore_patterns: vec!["*.LOG".into(), "node_modules".into()],
        };
        let r = run(&root, &db, &o);
        let c = Cache::open(&db).unwrap();
        let names: Vec<String> = c
            .conn
            .prepare("SELECT name FROM files")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(names, ["a.txt"]);
        let d = dirs(&db, r.scan_id);
        assert_eq!(d[&s(root.join("logs"))], (0, 0, 1)); // contenu ignoré : pas « vide »
        assert_eq!(d[&s(root.join("proj"))].2, 1);
        assert!(!d.contains_key(&s(root.join("AppData"))));
        assert_eq!(d[&s(root)].2, 4); // AppData, skipme, app.log, node_modules
    }

    #[test]
    fn incremental_scan_keeps_hashes_of_unchanged_files() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        write(&root, "same.bin", 3);
        let changed = write(&root, "changed.bin", 3);
        run(&root, &db, &opts());
        Cache::open(&db)
            .unwrap()
            .conn
            .execute("UPDATE files SET partial_hash='p', full_hash='f'", [])
            .unwrap();
        fs::write(&changed, b"abcd").unwrap();
        let gone = write(&root, "gone.bin", 1);
        run(&root, &db, &opts());
        fs::remove_file(gone).unwrap();
        let r = run(&root, &db, &opts());
        let c = Cache::open(&db).unwrap();
        let rows: HashMap<String, (Option<String>, Option<String>)> = c
            .conn
            .prepare("SELECT name, partial_hash, full_hash FROM file_paths")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?))))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(rows["same.bin"], (Some("p".into()), Some("f".into())));
        assert_eq!(rows["changed.bin"], (None, None));
        assert!(!rows.contains_key("gone.bin")); // purgé à la fin du scan complet
        assert_eq!(r.file_count, 2);
    }

    #[test]
    fn cancelled_scan_keeps_no_partial_aggregates() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        for i in 0..5 {
            write(&root, &format!("d{i}/f.bin"), 10);
        }
        let r = scan(
            root.to_str().unwrap(),
            &db,
            &opts(),
            &AtomicBool::new(true),
            |_, _, _| {},
        )
        .unwrap();
        assert!(r.cancelled);
        let c = Cache::open(&db).unwrap();
        let status: String = c
            .conn
            .query_row("SELECT status FROM scans", [], |r| r.get(0))
            .unwrap();
        assert_eq!(status, "cancelled");
        assert!(dirs(&db, r.scan_id).is_empty());
        assert!(c.latest_scan(&s(root)).unwrap().is_none());
    }

    #[test]
    fn missing_root_reports_failure() {
        let tmp = tempfile::tempdir().unwrap();
        let e = scan(
            tmp.path().join("nope").to_str().unwrap(),
            &tmp.path().join("c.db"),
            &opts(),
            &AtomicBool::new(false),
            |_, _, _| {},
        )
        .unwrap_err();
        assert!(e.contains("introuvable"), "{e}");
    }

    #[cfg(windows)]
    #[test]
    fn junction_counted_as_skipped_and_not_followed() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        write(&root, "real/a.bin", 10);
        let made = std::process::Command::new("cmd")
            .args(["/c", "mklink", "/J"])
            .arg(root.join("lien"))
            .arg(root.join("real"))
            .output()
            .unwrap();
        assert!(made.status.success(), "{made:?}");
        let r = run(&root, &db, &opts());
        assert_eq!((r.file_count, r.dir_count), (1, 2));
        assert_eq!(dirs(&db, r.scan_id)[&s(root)].2, 1);
    }

    #[test]
    fn mtime_matches_python_float() {
        // os.stat : sec + nsec * 1e-9. 1 700 000 000 s + 123 456 700 ns.
        let t = UNIX_EPOCH + Duration::new(1_700_000_000, 123_456_700);
        assert_eq!(
            py_time(Ok(t)).to_bits(),
            (1_700_000_000f64 + 123_456_700f64 * 1e-9).to_bits()
        );
        let before = UNIX_EPOCH - Duration::new(10, 250_000_000);
        assert_eq!(py_time(Ok(before)), -11.0 + 750_000_000f64 * 1e-9);
    }

    #[test]
    fn extension_like_splitext() {
        for (name, ext) in [
            ("a.TXT", ".txt"),
            ("a.tar.gz", ".gz"),
            (".gitignore", ""),
            ("..a", ""),
            ("a.", "."),
            (".a.b", ".b"),
            ("sans", ""),
        ] {
            assert_eq!(extension(name), ext, "{name}");
        }
    }

    #[test]
    fn fnmatch_like_python() {
        let m = |p: &str, s: &str| {
            fnmatch(
                &p.chars().collect::<Vec<_>>(),
                &s.chars().collect::<Vec<_>>(),
            )
        };
        assert!(m("*.log", "app.log") && !m("*.log", "app.log.1") && m("*", ""));
        assert!(m("a?c", "abc") && !m("a?c", "ac"));
        assert!(m("[ab]*", "b1") && !m("[!ab]*", "b1") && m("[a-c]x", "bx") && !m("[a-c]x", "dx"));
        assert!(m("[]]", "]") && m("[x", "[x") && m("*a*b", "zzazzb") && !m("*a*b", "zzazz"));
    }
}
