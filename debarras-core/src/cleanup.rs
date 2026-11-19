//! Nettoyage guidé : emplacements connus, recréés automatiquement, mesurés sur le disque.
//! Port de `core/cleanup.py`.
//!
//! Débarras n'efface rien lui-même : les éléments choisis passent par la corbeille (actions
//! habituelles, avec confirmation et annulation). Ce qui demande des droits administrateur
//! (Windows Update, fichiers temporaires de Windows) est confié à l'outil de Windows.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use crate::recycle;
use crate::safety::Env;
use crate::scanner::{is_dir_junction, py_time};

pub const TRASH_KIND: &str = "trash";
pub const WINDOWS_KIND: &str = "windows";
pub const RECYCLE_KIND: &str = "recycle";
/// Au-delà, un dossier est proposé élément par élément (voir `add`).
pub const SPLIT_SIZE: i64 = 4 * 1024 * 1024 * 1024;
/// Fichiers temporaires plus récents : peut-être en cours d'utilisation.
pub const TEMP_MIN_AGE: f64 = 24.0 * 3600.0;

/// Navigateurs Chromium : dossier « User Data » relatif à `%LOCALAPPDATA%`, processus.
const CHROMIUM: [(&str, &str, &str); 5] = [
    ("Google Chrome", r"Google\Chrome\User Data", "chrome.exe"),
    ("Microsoft Edge", r"Microsoft\Edge\User Data", "msedge.exe"),
    (
        "Brave",
        r"BraveSoftware\Brave-Browser\User Data",
        "brave.exe",
    ),
    ("Vivaldi", r"Vivaldi\User Data", "vivaldi.exe"),
    ("Chromium", r"Chromium\User Data", "chrome.exe"),
];
const CHROMIUM_PROFILE_CACHES: [&str; 8] = [
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    r"Service Worker\CacheStorage",
    r"Service Worker\ScriptCache",
];
const CHROMIUM_SHARED_CACHES: [&str; 3] = ["ShaderCache", "GrShaderCache", "GraphiteDawnCache"];
const BROWSING_DESC: &str = "Pages et images gardées pour accélérer la navigation. Vos favoris, mots de passe, \
                             historique et sessions ne sont pas touchés.";

/// Une catégorie proposée au nettoyage.
#[derive(Debug, Clone, PartialEq)]
pub struct Target {
    pub key: String,
    pub title: String,
    pub description: String,
    /// `trash` (éléments à la corbeille), `windows` (outil de Windows), `recycle` (corbeille).
    pub kind: &'static str,
    /// Éléments à mettre à la corbeille.
    pub paths: Vec<String>,
    /// Taille par élément, dans l'ordre des `paths` (ou clé `windows` / `recycle`).
    pub sizes: Vec<(String, i64)>,
    /// Nombre de fichiers.
    pub count: i64,
    /// Avertissement (navigateur ouvert…).
    pub note: String,
    /// Coché par défaut.
    pub recommended: bool,
    pub size_known: bool,
}

impl Target {
    fn new(key: &str, title: &str, description: impl Into<String>) -> Self {
        Target {
            key: key.into(),
            title: title.into(),
            description: description.into(),
            kind: TRASH_KIND,
            paths: vec![],
            sizes: vec![],
            count: 0,
            note: String::new(),
            recommended: true,
            size_known: true,
        }
    }

    pub fn size(&self) -> i64 {
        self.sizes.iter().map(|(_, s)| s).sum()
    }
}

fn s(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

/// (octets, fichiers, date de modification la plus récente) d'un fichier ou dossier.
/// Jonctions sautées (déjà comptées ailleurs) ; un lien symbolique compte pour lui-même.
pub fn measure(path: &str, cancel: &AtomicBool) -> (i64, i64, f64) {
    let Ok(md) = fs::symlink_metadata(path) else {
        return (0, 0, 0.0);
    };
    if !md.is_dir() {
        return (md.len() as i64, 1, py_time(md.modified()));
    }
    let (mut total, mut count, mut newest) = (0i64, 0i64, py_time(md.modified()));
    let mut stack = vec![PathBuf::from(path)];
    while let Some(dir) = stack.pop() {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        for e in entries.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                stack.push(e.path());
                continue;
            }
            if ft.is_symlink() && is_dir_junction(&e) {
                continue;
            }
            let Ok(m) = e.metadata() else { continue };
            total += m.len() as i64;
            count += 1;
            newest = newest.max(py_time(m.modified()));
        }
    }
    (total, count, newest)
}

/// Noms des exécutables en cours (minuscules).
#[cfg(windows)]
pub fn running_processes() -> HashSet<String> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
        TH32CS_SNAPPROCESS,
    };
    let mut out = HashSet::new();
    // SAFETY : instantané fermé en fin de lecture ; `entry` est une structure C de la taille annoncée.
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return out;
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snap, &mut entry);
        while ok != 0 {
            let name = &entry.szExeFile;
            let len = name.iter().position(|&c| c == 0).unwrap_or(name.len());
            out.insert(String::from_utf16_lossy(&name[..len]).to_lowercase());
            ok = Process32NextW(snap, &mut entry);
        }
        CloseHandle(snap);
    }
    out
}

/// Ajoute `path` s'il contient quelque chose ; un dossier trop gros pour un seul élément de
/// corbeille (Windows le refuserait, erreur 161) est proposé élément par élément.
fn add(t: &mut Target, path: &Path, cancel: &AtomicBool, split_size: i64) {
    let p = s(path);
    let (size, count, _) = measure(&p, cancel);
    if count == 0 {
        return;
    }
    if size > split_size && path.is_dir() {
        let children: Vec<PathBuf> = fs::read_dir(path)
            .map(|it| it.flatten().map(|e| e.path()).collect())
            .unwrap_or_default();
        for child in children {
            add(t, &child, cancel, split_size);
        }
        return;
    }
    t.paths.push(p.clone());
    t.sizes.push((p, size));
    t.count += count;
}

fn local(env: &Env) -> PathBuf {
    env.local_appdata.clone().map_or_else(
        || Path::new(&env.home).join(r"AppData\Local"),
        PathBuf::from,
    )
}

/// Fichiers temporaires inchangés depuis plus de 24 h (`now` : maintenant, en secondes).
pub fn temp_target(env: &Env, cancel: &AtomicBool, now: f64) -> Target {
    let temp = env
        .temp
        .clone()
        .unwrap_or_else(|| s(&local(env).join("Temp")));
    let mut t = Target::new(
        "temp",
        "Fichiers temporaires",
        format!(
            "Laissés par les installations et les logiciels dans {temp}. Seuls les éléments inchangés \
             depuis plus de 24 h sont proposés (les autres peuvent être en cours d'utilisation)."
        ),
    );
    let limit = now - TEMP_MIN_AGE;
    let Ok(entries) = fs::read_dir(&temp) else {
        return t;
    };
    for e in entries.flatten() {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let p = s(&e.path());
        let (size, count, newest) = measure(&p, cancel);
        if newest != 0.0 && newest < limit && (count > 0 || e.path().is_dir()) {
            t.paths.push(p.clone());
            t.sizes.push((p, size));
            t.count += count.max(1);
        }
    }
    t
}

fn browser_note(t: &mut Target, name: &str, exe: &str, running: &HashSet<String>) {
    if running.contains(exe) {
        t.note = format!(
            "{name} est ouvert : fermez-le pour un nettoyage complet (les fichiers en cours d'utilisation \
             seront ignorés)."
        );
        if exe == "msedge.exe" {
            t.note += " Edge tourne parfois en arrière-plan même fenêtre fermée.";
        }
        t.recommended = false;
    }
}

fn subdirs(p: &Path) -> Vec<PathBuf> {
    fs::read_dir(p)
        .map(|it| it.flatten().map(|e| e.path()).collect())
        .unwrap_or_default()
}

/// Caches des navigateurs installés. `running` : processus ouverts (`running_processes`).
pub fn browser_targets(env: &Env, cancel: &AtomicBool, running: &HashSet<String>) -> Vec<Target> {
    browser_targets_split(env, cancel, running, SPLIT_SIZE)
}

fn browser_targets_split(
    env: &Env,
    cancel: &AtomicBool,
    running: &HashSet<String>,
    split: i64,
) -> Vec<Target> {
    let mut out = Vec::new();
    let local = local(env);
    for (name, rel, exe) in CHROMIUM {
        let root = local.join(rel);
        if !root.is_dir() {
            continue;
        }
        let mut t = Target::new(
            &format!("browser:{name}"),
            &format!("Cache de {name}"),
            BROWSING_DESC,
        );
        let profiles: Vec<PathBuf> = subdirs(&root)
            .into_iter()
            .filter(|p| {
                let n = p
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                p.is_dir()
                    && (n == "Default"
                        || n.starts_with("Profile ")
                        || n == "Guest Profile"
                        || n == "System Profile")
            })
            .collect();
        for prof in &profiles {
            for sub in CHROMIUM_PROFILE_CACHES {
                if prof.join(sub).is_dir() {
                    add(&mut t, &prof.join(sub), cancel, split);
                }
            }
        }
        for sub in CHROMIUM_SHARED_CACHES {
            if root.join(sub).is_dir() {
                add(&mut t, &root.join(sub), cancel, split);
            }
        }
        browser_note(&mut t, name, exe, running);
        out.push(t);
    }
    let ff = local.join(r"Mozilla\Firefox\Profiles");
    if ff.is_dir() {
        let mut t = Target::new("browser:Firefox", "Cache de Firefox", BROWSING_DESC);
        for prof in subdirs(&ff) {
            for sub in ["cache2", "startupCache", "jumpListCache"] {
                if prof.join(sub).is_dir() {
                    add(&mut t, &prof.join(sub), cancel, split);
                }
            }
        }
        browser_note(&mut t, "Firefox", "firefox.exe", running);
        out.push(t);
    }
    let opera = local.join("Opera Software");
    if opera.is_dir() {
        let mut t = Target::new(
            "browser:Opera",
            "Cache d'Opera",
            "Pages et images gardées pour accélérer la navigation.",
        );
        for prof in subdirs(&opera) {
            for sub in ["Cache", "Code Cache", "GPUCache"] {
                if prof.join(sub).is_dir() {
                    add(&mut t, &prof.join(sub), cancel, split);
                }
            }
        }
        browser_note(&mut t, "Opera", "opera.exe", running);
        out.push(t);
    }
    out
}

pub fn reports_target(env: &Env, cancel: &AtomicBool) -> Target {
    let mut t = Target::new(
        "reports",
        "Rapports d'erreurs et vidages mémoire",
        "Copies de la mémoire d'un logiciel au moment d'un plantage, et rapports d'erreurs déjà envoyés \
         à Microsoft. Utiles seulement pour un dépannage en cours.",
    );
    for rel in [
        "CrashDumps",
        r"Microsoft\Windows\WER\ReportArchive",
        r"Microsoft\Windows\WER\ReportQueue",
    ] {
        let p = local(env).join(rel);
        if p.is_dir() {
            add(&mut t, &p, cancel, SPLIT_SIZE);
        }
    }
    t
}

pub fn shaders_target(env: &Env, cancel: &AtomicBool) -> Target {
    let mut t = Target::new(
        "shaders",
        "Cache des shaders (carte graphique)",
        "Effets graphiques précalculés par DirectX et le pilote de la carte graphique. Recréés \
         automatiquement : les jeux seront un peu plus lents (saccades) à leurs premiers lancements.",
    );
    t.recommended = false;
    for rel in [
        "D3DSCache",
        r"NVIDIA\DXCache",
        r"NVIDIA\GLCache",
        r"AMD\DxCache",
        r"AMD\DxcCache",
        r"AMD\GLCache",
        r"Intel\ShaderCache",
    ] {
        let p = local(env).join(rel);
        if p.is_dir() {
            add(&mut t, &p, cancel, SPLIT_SIZE);
        }
    }
    if t.size() > 10 * 1024 * 1024 * 1024 {
        t.note = "Cache inhabituellement gros : sa taille maximale se règle dans le panneau de configuration \
                  du pilote (NVIDIA : « Taille du cache du shader »)."
            .into();
    }
    t
}

pub fn windows_update_target(env: &Env, cancel: &AtomicBool) -> Target {
    let mut t = Target::new(
        "windows",
        "Windows Update et fichiers temporaires de Windows",
        "Mises à jour déjà installées et fichiers temporaires du système. Ils demandent des droits \
         administrateur : Débarras ouvre le « Nettoyage de disque » de Windows, puis cliquez sur \
         « Nettoyer les fichiers système ».",
    );
    t.kind = WINDOWS_KIND;
    t.recommended = false;
    let windir = Path::new(&env.system_root);
    let (size, count, _) = measure(&s(&windir.join(r"SoftwareDistribution\Download")), cancel);
    let (s2, c2, _) = measure(&s(&windir.join("Temp")), cancel);
    t.sizes.push(("windows".into(), size + s2));
    t.count = count + c2;
    t.size_known = count > 0 || c2 > 0;
    t
}

pub fn recycle_target() -> Target {
    let (size, n) = recycle::usage(None);
    let mut t = Target::new(
        "recycle",
        "Corbeille",
        "Tout ce que Débarras supprime y passe d'abord. Vérifiez-la, puis videz-la depuis Windows pour \
         libérer réellement l'espace (Débarras ne la vide jamais).",
    );
    t.kind = RECYCLE_KIND;
    t.recommended = false;
    t.sizes.push(("recycle".into(), size));
    t.count = n;
    t
}

pub fn open_windows_cleanup(env: &Env) -> std::io::Result<()> {
    std::process::Command::new(Path::new(&env.system_root).join(r"System32\cleanmgr.exe"))
        .spawn()
        .map(drop)
}

pub fn open_recycle_bin() -> std::io::Result<()> {
    std::process::Command::new("explorer.exe")
        .arg("shell:RecycleBinFolder")
        .spawn()
        .map(drop)
}

/// Ce que « Nettoyer automatiquement » prend : catégories sans contrepartie, cochées par défaut
/// (temporaires, caches des navigateurs fermés, rapports d'erreurs). Jamais le cache graphique
/// (jeux plus lents ensuite) ni un navigateur ouvert.
pub fn auto_selection(targets: &[Target]) -> (Vec<String>, Vec<(String, i64)>) {
    let mut paths = Vec::new();
    let mut sizes = Vec::new();
    for t in targets
        .iter()
        .filter(|t| t.kind == TRASH_KIND && t.recommended)
    {
        paths.extend(t.paths.iter().cloned());
        sizes.extend(t.sizes.iter().cloned());
    }
    (paths, sizes)
}

/// Toutes les catégories, dans l'ordre de l'écran de nettoyage.
pub fn analyze(env: &Env, cancel: &AtomicBool) -> Vec<Target> {
    let running = running_processes();
    let mut targets = vec![temp_target(env, cancel, crate::cache::now())];
    targets.extend(browser_targets(env, cancel, &running));
    targets.push(reports_target(env, cancel));
    targets.push(shaders_target(env, cancel));
    targets.retain(|t| !t.paths.is_empty() || t.key.starts_with("browser:"));
    targets.push(windows_update_target(env, cancel));
    targets.push(recycle_target());
    targets
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, SystemTime};

    fn env(home: &Path) -> Env {
        Env {
            system_root: s(&home.join("Windows")),
            program_data: s(&home.join("ProgramData")),
            program_files: [
                s(&home.join("PF")),
                s(&home.join("PF86")),
                s(&home.join("PF")),
            ],
            home: s(home),
            local_appdata: Some(s(&home.join("Local"))),
            temp: Some(s(&home.join("Temp"))),
        }
    }

    fn write(p: &Path, n: usize) {
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, vec![b'z'; n]).unwrap();
    }

    fn age(p: &Path, days: u64) {
        let t = SystemTime::now() - Duration::from_secs(days * 86_400);
        fs::File::options()
            .write(true)
            .open(p)
            .unwrap()
            .set_modified(t)
            .unwrap();
    }

    #[test]
    fn temp_and_browsers() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path();
        let e = env(home);
        let no = AtomicBool::new(false);
        let temp = home.join("Temp");
        write(&temp.join(r"vieux\a.tmp"), 100);
        write(&temp.join("recent.tmp"), 50);
        age(&temp.join(r"vieux\a.tmp"), 3);
        {
            // Un dossier s'ouvre avec FILE_FLAG_BACKUP_SEMANTICS ; FILE_WRITE_ATTRIBUTES suffit.
            use std::os::windows::fs::OpenOptionsExt;
            let dir = fs::OpenOptions::new()
                .access_mode(0x100)
                .custom_flags(0x0200_0000)
                .open(temp.join("vieux"))
                .unwrap();
            dir.set_modified(SystemTime::now() - Duration::from_secs(3 * 86_400))
                .unwrap();
        }
        let t = temp_target(&e, &no, crate::cache::now());
        assert_eq!(t.paths, [s(&temp.join("vieux"))]);
        assert_eq!(t.size(), 100);

        let cache = home.join(r"Local\Google\Chrome\User Data\Default\Cache");
        write(&cache.join("data_1"), 300);
        write(
            &home.join(r"Local\Google\Chrome\User Data\Default\Bookmarks"),
            2,
        );
        let running = HashSet::from(["chrome.exe".to_string()]);
        let targets = browser_targets(&e, &no, &running);
        assert_eq!(
            targets.iter().map(|t| t.key.as_str()).collect::<Vec<_>>(),
            ["browser:Google Chrome"]
        );
        let chrome = &targets[0];
        assert_eq!(
            (chrome.paths.clone(), chrome.size()),
            (vec![s(&cache)], 300)
        );
        assert!(chrome.note.contains("ouvert") && !chrome.recommended);
        assert!(browser_targets(&e, &no, &HashSet::new())[0].recommended);

        // Dossier trop gros pour la corbeille d'un seul tenant : proposé fichier par fichier.
        write(&cache.join("data_2"), 100);
        let split = &browser_targets_split(&e, &no, &HashSet::new(), 200)[0];
        let mut paths = split.paths.clone();
        paths.sort();
        assert_eq!(paths, [s(&cache.join("data_1")), s(&cache.join("data_2"))]);
        assert_eq!(split.size(), 400);
    }

    #[test]
    fn auto_selection_skips_costly_targets() {
        let mk = |key: &str, path: &str, size: i64, recommended: bool, kind: &'static str| Target {
            paths: if path.is_empty() {
                vec![]
            } else {
                vec![path.into()]
            },
            sizes: vec![(if path.is_empty() { key } else { path }.into(), size)],
            recommended,
            kind,
            ..Target::new(key, key, "")
        };
        let targets = [
            mk("temp", "a", 5, true, TRASH_KIND),
            mk("browser:X", "b", 7, false, TRASH_KIND), // ouvert
            mk("shaders", "c", 9, false, TRASH_KIND),
            mk("recycle", "", 3, false, RECYCLE_KIND),
        ];
        assert_eq!(
            auto_selection(&targets),
            (vec!["a".to_string()], vec![("a".to_string(), 5)])
        );
    }

    #[test]
    fn measure_and_processes() {
        let tmp = tempfile::tempdir().unwrap();
        write(&tmp.path().join(r"d\a"), 10);
        write(&tmp.path().join(r"d\sub\b"), 20);
        let (size, count, newest) = measure(&s(&tmp.path().join("d")), &AtomicBool::new(false));
        assert_eq!((size, count), (30, 2));
        assert!(newest > 0.0);
        assert_eq!(
            measure(&s(&tmp.path().join("absent")), &AtomicBool::new(false)),
            (0, 0, 0.0)
        );
        let procs = running_processes();
        assert!(procs.iter().any(|p| p.ends_with(".exe")), "{procs:?}");
    }

    #[test]
    fn analyze_real_machine_does_not_fail() {
        let targets = analyze(&Env::current(), &AtomicBool::new(false));
        let keys: Vec<&str> = targets.iter().map(|t| t.key.as_str()).collect();
        assert_eq!(keys[keys.len() - 2..], ["windows", "recycle"]);
    }
}
