//! Niveau de risque d'un fichier ou dossier : peut-on le supprimer sans casser Windows ou un
//! logiciel ? Port de `core/safety.py`, mêmes règles dans le même ordre et mêmes raisons.
//!
//! Quatre niveaux, du plus au moins risqué :
//! - `System` : indispensable à Windows, toute action est refusée ;
//! - `Software` : fait partie d'un logiciel, d'un jeu ou de ses réglages, déconseillé ;
//! - `Personal` : vos fichiers, sans risque pour l'ordinateur, à vous de juger ;
//! - `Cleanable` : caches et temporaires recréés automatiquement, sans risque.
//!
//! Les règles sont d'abord des chemins connus (Windows, AppData, caches…). S'y ajoutent les
//! « dossiers de logiciel » trouvés dans le scan : un dossier contenant des .dll est un
//! programme installé, tout ce qu'il contient en fait partie.
//! Libellés, conseils et couleurs des niveaux sont à l'interface.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use rusqlite::{Connection, OptionalExtension, Result, params_from_iter};

use crate::cache::subtree;
use crate::paths::{dirname, long_path, norm, split};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Level {
    System,
    Software,
    Personal,
    Cleanable,
}

impl Level {
    pub const ALL: [Level; 4] = [
        Level::System,
        Level::Software,
        Level::Personal,
        Level::Cleanable,
    ];

    /// Identifiant stable (celui de la version Python).
    pub fn as_str(self) -> &'static str {
        match self {
            Level::System => "system",
            Level::Software => "software",
            Level::Personal => "personal",
            Level::Cleanable => "cleanable",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verdict {
    pub level: Level,
    pub reason: String,
}

impl Verdict {
    fn new(level: Level, reason: impl Into<String>) -> Self {
        Verdict {
            level,
            reason: reason.into(),
        }
    }

    /// Toute action est refusée.
    pub fn blocked(&self) -> bool {
        self.level == Level::System
    }
}

// Fichiers et dossiers système présents à la racine d'un lecteur.
const ROOT_SYSTEM_FILES: &[&str] = &[
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "dumpstack.log",
    "dumpstack.log.tmp",
    "bootmgr",
    "bootnxt",
    "bootsect.bak",
];
const ROOT_SYSTEM_DIRS: &[&str] = &[
    "system volume information",
    "recovery",
    "boot",
    "efi",
    "$recycle.bin",
    "$winreagent",
    "$windows.~bt",
    "$windows.~ws",
    "$sysreset",
    "$getcurrent",
    "config.msi",
    "documents and settings",
    "msocache",
    "onedrivetemp",
];
const PROFILE_SYSTEM: &[&str] = &["ntuser.", "usrclass.dat"]; // registre de l'utilisateur
const USERS_SYSTEM: &[&str] = &["default", "default user", "all users"];
// Sous-dossiers de Program Files qui font partie de Windows (bloqués comme le système).
const WINDOWS_PROGRAMS: &[&str] = &[
    "common files",
    "windowsapps",
    "windows defender",
    "windows defender advanced threat protection",
    "windows mail",
    "windows media player",
    "windows nt",
    "windows photo viewer",
    "windows portable devices",
    "windows security",
    "windows sidebar",
    "windowspowershell",
    "internet explorer",
    "microsoft update health tools",
    "modifiablewindowsapps",
    "reference assemblies",
    "msbuild",
    "uninstall information",
    "installshield installation information",
    "dotnet",
];
// Bibliothèques de jeux : à désinstaller depuis le lanceur, pas à la main.
const GAME_DIRS: &[(&str, &str)] = &[
    ("steamapps", "Steam"),
    ("steamlibrary", "Steam"),
    ("epic games", "Epic Games"),
    ("gog games", "GOG"),
    ("gog galaxy", "GOG"),
    ("xboxgames", "Xbox"),
    ("riot games", "Riot"),
    ("ubisoft game launcher", "Ubisoft Connect"),
    ("ea games", "EA"),
    ("battle.net", "Battle.net"),
];
// Dossiers de cache reconnus par leur nom (dans AppData, ou n'importe où pour ceux de développement).
const APP_CACHE_NAMES: &[&str] = &[
    "cache",
    "caches",
    "cache2",
    "code cache",
    "gpucache",
    "grshadercache",
    "shadercache",
    "dawncache",
    "dawngraphitecache",
    "dawnwebgpucache",
    "crashpad",
    "crashdumps",
    "d3dscache",
    "inetcache",
    "webcache",
    "service worker",
];
const DEV_CLEANABLE: &[(&str, &str)] = &[
    ("__pycache__", "Cache Python, recréé automatiquement"),
    (".pytest_cache", "Cache de tests, recréé automatiquement"),
    (".mypy_cache", "Cache d'analyse, recréé automatiquement"),
    (".ruff_cache", "Cache d'analyse, recréé automatiquement"),
    (
        "node_modules",
        "Dépendances JavaScript, re-téléchargées par « npm install »",
    ),
];
// Sous-dossiers où les logiciels rangent leurs .dll ; le logiciel lui-même est au-dessus.
const BIN_NAMES: &[&str] = &[
    "bin", "bin32", "bin64", "binaries", "win64", "win32", "x64", "x86", "amd64", "lib", "libs",
    "dll", "dlls", "plugins", "redist", "runtime", "engine",
];
pub const INSTALLERS: &[&str] = &[
    ".exe",
    ".msi",
    ".msix",
    ".msixbundle",
    ".appx",
    ".appxbundle",
];
const GIT_LIKE: &[&str] = &[".git", ".hg", ".svn"];
const VENVS: &[&str] = &[".venv", "venv", ".tox", ".conda"];

/// Un dossier occupé à 50 % ou plus par un logiciel est un dossier de logiciel.
const CONTAINER_SHARE: f64 = 0.5;

fn lookup<'a>(table: &[(&str, &'a str)], key: &str) -> Option<&'a str> {
    table.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
}

/// `p` est `root` ou se trouve dessous (chemins normalisés).
fn under(p: &str, root: &str) -> bool {
    !root.is_empty()
        && (p == root
            || p.strip_prefix(root)
                .is_some_and(|rest| rest.starts_with('\\')))
}

/// `os.path.splitdrive` d'un chemin normalisé.
fn split_drive(p: &str) -> (&str, &str) {
    if let Some(unc) = p.strip_prefix("\\\\") {
        // `\\serveur\partage` : jusqu'au séparateur qui suit le partage.
        let cut = match unc.find('\\') {
            Some(i) => 3 + i + unc[i + 1..].find('\\').unwrap_or(unc.len() - i - 1),
            None => p.len(),
        };
        return p.split_at(cut);
    }
    if p.len() >= 2 && p.as_bytes()[1] == b':' {
        p.split_at(2)
    } else {
        ("", p)
    }
}

/// `os.path.splitext(name)[1]`.
fn ext(name: &str) -> &str {
    match name.rfind('.') {
        Some(dot) if name[..dot].chars().any(|c| c != '.') => &name[dot..],
        _ => "",
    }
}

/// Dossiers Windows connus, lus dans l'environnement (ou donnés pour les tests).
#[derive(Debug, Clone)]
pub struct Env {
    pub system_root: String,
    pub program_data: String,
    /// `ProgramFiles`, `ProgramFiles(x86)`, `ProgramW6432`.
    pub program_files: [String; 3],
    /// Profil (`USERPROFILE`).
    pub home: String,
    pub local_appdata: Option<String>,
    pub temp: Option<String>,
}

impl Env {
    pub fn current() -> Env {
        let var = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        let or = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.into());
        Env {
            system_root: or("SystemRoot", r"C:\Windows"),
            program_data: or("ProgramData", r"C:\ProgramData"),
            program_files: [
                or("ProgramFiles", r"C:\Program Files"),
                or("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                or("ProgramW6432", r"C:\Program Files"),
            ],
            home: var("USERPROFILE").unwrap_or_else(|| {
                format!(
                    "{}{}",
                    or("HOMEDRIVE", "C:"),
                    or("HOMEPATH", r"\Users\Default")
                )
            }),
            local_appdata: var("LOCALAPPDATA"),
            temp: var("TEMP"),
        }
    }
}

fn path_join(a: &str, b: &str) -> String {
    Path::new(a).join(b).to_string_lossy().into_owned()
}

fn parent_of(p: &str) -> String {
    Path::new(p)
        .parent()
        .map_or_else(|| p.to_string(), |x| x.to_string_lossy().into_owned())
}

/// `_root` de Python : nom long puis forme normalisée.
fn root_of(p: &str) -> String {
    norm(&long_path(p))
}

/// Classe des chemins. `software_dirs` : dossiers de logiciels trouvés dans le scan.
#[derive(Debug, Clone)]
pub struct Classifier {
    system_roots: Vec<(String, String)>, // (normalisé, tel quel)
    program_roots: Vec<(String, String)>,
    home: String,
    users: String,
    appdata: String,
    temp: String,
    pub downloads: String,
    local_programs: String,
    app_dir: String,
    /// normalisé -> tel quel
    pub software_dirs: HashMap<String, String>,
    /// Dossiers surtout occupés par un logiciel (eux seuls), normalisés.
    pub containers: HashSet<String>,
}

impl Classifier {
    pub fn new(
        env: &Env,
        software_dirs: impl IntoIterator<Item = String>,
        app_dir: Option<&str>,
        containers: impl IntoIterator<Item = String>,
    ) -> Self {
        let roots = |xs: &[&String]| {
            let mut out: Vec<(String, String)> = Vec::new();
            for x in xs.iter().filter(|x| !x.is_empty()) {
                if !out.iter().any(|(_, shown)| shown == *x) {
                    out.push((root_of(x), x.to_string()));
                }
            }
            out
        };
        let local = env
            .local_appdata
            .clone()
            .unwrap_or_else(|| path_join(&env.home, r"AppData\Local"));
        let local_name = Path::new(&local)
            .file_name()
            .map(|n| n.to_string_lossy().to_lowercase());
        let appdata = if local_name.as_deref() == Some("local") {
            root_of(&parent_of(&local))
        } else {
            root_of(&path_join(&env.home, "AppData"))
        };
        Classifier {
            system_roots: roots(&[&env.system_root, &env.program_data]),
            program_roots: roots(&env.program_files.iter().collect::<Vec<_>>()),
            home: root_of(&env.home),
            users: root_of(&parent_of(&env.home)),
            appdata,
            temp: root_of(
                &env.temp
                    .clone()
                    .unwrap_or_else(|| path_join(&local, "Temp")),
            ),
            downloads: root_of(&path_join(&env.home, "Downloads")),
            local_programs: root_of(&path_join(&local, "Programs")),
            app_dir: app_dir.map(norm).unwrap_or_default(),
            software_dirs: software_dirs.into_iter().map(|d| (norm(&d), d)).collect(),
            containers: containers.into_iter().map(|d| norm(&d)).collect(),
        }
    }

    /// Niveau de `path`. `is_dir` : `None` pour le demander au disque.
    pub fn classify(&self, path: &str, is_dir: Option<bool>) -> Verdict {
        let is_dir = is_dir.unwrap_or_else(|| Path::new(path).is_dir());
        self.classify_norm(&norm(path), is_dir)
    }

    fn classify_norm(&self, p: &str, is_dir: bool) -> Verdict {
        use Level::*;
        let (_, rest) = split_drive(p);
        let parts: Vec<&str> = rest.split('\\').filter(|x| !x.is_empty()).collect();
        let name = parts.last().copied().unwrap_or("");

        // 1. Système
        let Some(&first) = parts.first() else {
            return Verdict::new(System, "Racine du lecteur");
        };
        for (root, shown) in &self.system_roots {
            if under(p, root) {
                return Verdict::new(System, format!("Dossier de Windows ({shown})"));
            }
        }
        for (root, shown) in &self.program_roots {
            if !under(p, root) {
                continue;
            }
            let inner: Vec<&str> = if p == root {
                vec![]
            } else {
                p[root.len() + 1..].split('\\').collect()
            };
            let Some(&top) = inner.first() else {
                return Verdict::new(
                    System,
                    format!("Dossier des programmes installés ({shown})"),
                );
            };
            if let Some(game) = inner.iter().find_map(|x| lookup(GAME_DIRS, x)) {
                return Verdict::new(
                    Software,
                    format!("Bibliothèque de jeux {game} : désinstallez depuis le lanceur"),
                );
            }
            if WINDOWS_PROGRAMS.contains(&top)
                || top.starts_with("windows ")
                || top.starts_with("microsoft.net")
            {
                return Verdict::new(System, "Composant de Windows");
            }
            return Verdict::new(
                Software,
                "Logiciel installé : désinstallez-le depuis Paramètres › Applications",
            );
        }
        if parts.contains(&"$recycle.bin") {
            return Verdict::new(System, "Corbeille : videz-la depuis Windows");
        }
        // Dossier système de la racine, contenu compris (Recovery\winre.wim, Boot\BCD…).
        if ROOT_SYSTEM_DIRS.contains(&first)
            || (parts.len() == 1 && !is_dir && ROOT_SYSTEM_FILES.contains(&name))
        {
            return Verdict::new(System, "Fichier ou dossier système à la racine du lecteur");
        }
        if p == self.home || p == self.users {
            return Verdict::new(System, "Dossier des comptes utilisateurs");
        }
        if under(p, &self.users) && p.len() > self.users.len() {
            let first = p[self.users.len() + 1..].split('\\').next().unwrap_or("");
            if USERS_SYSTEM.contains(&first) {
                return Verdict::new(System, "Profil modèle de Windows");
            }
        }
        if dirname(p) == self.home && PROFILE_SYSTEM.iter().any(|x| name.starts_with(x)) {
            return Verdict::new(System, "Registre de votre compte Windows");
        }

        // 2. Nettoyable à coup sûr (avant AppData, qui les contient)
        if under(p, &self.temp) {
            return Verdict::new(
                Cleanable,
                "Fichiers temporaires de Windows et des logiciels",
            );
        }
        if under(p, &self.app_dir) {
            return Verdict::new(Software, "Débarras lui-même");
        }
        if under(p, &self.appdata) {
            let inner: Vec<&str> = if p == self.appdata {
                vec![]
            } else {
                p[self.appdata.len() + 1..].split('\\').collect()
            };
            if let Some(cache) = inner.iter().rev().find(|x| APP_CACHE_NAMES.contains(x)) {
                return Verdict::new(
                    Cleanable,
                    format!("Cache d'application ({cache}), recréé automatiquement"),
                );
            }
            if under(p, &self.local_programs) {
                return Verdict::new(Software, "Logiciel installé pour votre compte");
            }
            return Verdict::new(Software, "AppData : réglages et données des logiciels");
        }

        // 3. Logiciels, jeux, outils
        for part in &parts {
            if let Some(game) = lookup(GAME_DIRS, part) {
                return Verdict::new(
                    Software,
                    format!("Bibliothèque de jeux {game} : désinstallez depuis le lanceur"),
                );
            }
            if GIT_LIKE.contains(part) {
                return Verdict::new(
                    Software,
                    "Historique d'un projet (dépôt Git) : le supprimer perd les versions",
                );
            }
            if VENVS.contains(part) {
                return Verdict::new(
                    Software,
                    "Environnement Python d'un projet (re-créable, mais le projet ne fonctionnera plus d'ici là)",
                );
            }
            if let Some(reason) = lookup(DEV_CLEANABLE, part)
                && !self.in_software(p)
            {
                return Verdict::new(Cleanable, reason);
            }
        }
        if self.in_software(p) {
            return Verdict::new(
                Software,
                "Fait partie d'un logiciel installé (bibliothèques .dll)",
            );
        }
        if self.containers.contains(p) {
            return Verdict::new(Software, "Contient surtout un logiciel ou un jeu installé");
        }
        if under(p, &self.home) && p != self.home {
            let first = p[self.home.len() + 1..].split('\\').next().unwrap_or("");
            if first == ".cache" {
                return Verdict::new(Cleanable, "Cache d'outils, recréé automatiquement");
            }
            if first.starts_with('.') {
                return Verdict::new(
                    Software,
                    format!("Réglages ou données d'un outil ({first})"),
                );
            }
        }

        // 4. Fichiers isolés reconnaissables
        if !is_dir {
            if name == "thumbs.db" {
                return Verdict::new(
                    Cleanable,
                    "Miniatures de l'Explorateur, recréées automatiquement",
                );
            }
            if INSTALLERS.contains(&ext(name)) && under(p, &self.downloads) {
                return Verdict::new(
                    Cleanable,
                    "Installeur téléchargé : le logiciel reste installé sans lui",
                );
            }
        }

        // 5. Données personnelles
        Verdict::new(Personal, "Vos fichiers")
    }

    fn in_software(&self, p: &str) -> bool {
        let mut q = p;
        loop {
            if self.software_dirs.contains_key(q) {
                return true;
            }
            let parent = dirname(q);
            if parent == q || parent.chars().count() <= 3 {
                return false;
            }
            q = parent;
        }
    }

    /// Logiciels repérés à l'intérieur de `folder` (pour avertir avant de le supprimer).
    pub fn risky_inside(&self, folder: &str, limit: usize) -> Vec<String> {
        let f = norm(folder);
        let mut found: Vec<&String> = self
            .software_dirs
            .keys()
            .filter(|d| **d != f && under(d, &f))
            .collect();
        found.sort();
        let mut tops: Vec<&String> = Vec::new();
        for d in found {
            // on ne garde que les plus hauts
            if !tops.iter().any(|t| under(d, t)) {
                tops.push(d);
            }
        }
        tops.into_iter()
            .take(limit)
            .map(|d| self.software_dirs[d].clone())
            .collect()
    }
}

/// Dossiers du scan contenant des .dll (programmes installés, portables ou jeux).
pub fn software_dirs(conn: &Connection, root: &str) -> Result<HashSet<String>> {
    let (cond, args) = subtree("d.path", root);
    let dirs_with = |ext: &str| -> Result<HashSet<String>> {
        let sql = format!(
            "SELECT DISTINCT d.path FROM files f JOIN dirpaths d ON d.id = f.dir_id WHERE f.ext = '{ext}' AND {cond}"
        );
        conn.prepare(&sql)?
            .query_map(params_from_iter(&args), |r| r.get(0))?
            .collect()
    };
    let mut out = dirs_with(".dll")?;
    let exe_dirs = dirs_with(".exe")?;
    let root_n = norm(root);
    let short = |p: &str| p.chars().count() <= 3;
    for d in out.clone() {
        // Un logiciel range souvent ses .dll dans un sous-dossier (bin, lib…) : on remonte d'un
        // cran quand le dossier parent contient l'exécutable.
        let parent = dirname(&d);
        if exe_dirs.contains(parent) && norm(parent) != root_n && !short(parent) {
            out.insert(parent.to_string());
        }
        // Dossiers techniques (bin, Binaries\Win64…) : le logiciel est le dossier au-dessus.
        let mut cur = d.as_str();
        while BIN_NAMES.contains(&split(cur).1.to_lowercase().as_str()) {
            let parent = dirname(cur);
            if parent == cur || norm(parent) == root_n || short(parent) {
                break;
            }
            out.insert(parent.to_string());
            cur = parent;
        }
    }
    Ok(out)
}

/// Dossiers surtout occupés par un logiciel (ex. StarCitizen autour de son dossier LIVE) : on
/// remonte depuis chaque logiciel tant qu'il représente l'essentiel du dossier parent. Seul le
/// dossier lui-même est concerné, pas ses autres sous-dossiers. On s'arrête au profil (`home`)
/// et à ses dossiers (Documents…) et à la racine du lecteur.
pub fn containers(
    conn: &Connection,
    scan_id: i64,
    roots: &HashSet<String>,
    scan_root: &str,
    home: &str,
) -> Result<HashSet<String>> {
    let home = norm(home);
    let scan_root = norm(scan_root);
    let mut sizes: HashMap<String, i64> = HashMap::new();
    let mut st = conn.prepare_cached("SELECT size FROM dir_sizes WHERE scan_id=? AND path=?")?;
    let mut size = |path: &str| -> Result<i64> {
        if let Some(&s) = sizes.get(path) {
            return Ok(s);
        }
        let s = st
            .query_row(rusqlite::params![scan_id, path], |r| r.get(0))
            .optional()?
            .unwrap_or(0);
        sizes.insert(path.to_string(), s);
        Ok(s)
    };
    let mut out = HashSet::new();
    for d in roots {
        let (mut cur, own) = (d.as_str(), size(d)?);
        loop {
            let parent = dirname(cur);
            let np = norm(parent);
            if parent == cur
                || np.chars().count() <= 2
                || np == home
                || dirname(&np) == home
                || under(&home, &np)
                || np == scan_root
            {
                break;
            }
            let total = size(parent)?;
            if total == 0 || (own as f64) < CONTAINER_SHARE * total as f64 {
                break;
            }
            out.insert(parent.to_string());
            cur = parent;
        }
    }
    Ok(out)
}

#[derive(Debug, Clone, PartialEq)]
pub struct SafetySummary {
    pub scan_id: i64,
    /// Octets par niveau.
    pub totals: HashMap<Level, i64>,
    /// Niveau -> les plus gros éléments représentatifs (chemin, taille, raison), 8 au plus.
    pub top: HashMap<Level, Vec<(String, i64, String)>>,
    pub software: Vec<String>,
}

/// Répartition de l'espace par niveau. Chaque dossier compte ses fichiers directs ; les plus
/// gros éléments « homogènes » de chaque niveau sont gardés pour la synthèse.
/// `None` si `cancel` a été levé.
pub fn summarize(
    conn: &Connection,
    scan_id: i64,
    root: &str,
    clf: &Classifier,
    home: &str,
    cancel: &AtomicBool,
) -> Result<Option<SafetySummary>> {
    let rows: Vec<(String, Option<String>, i64)> = conn
        .prepare("SELECT path, parent, size FROM dir_sizes WHERE scan_id=?")?
        .query_map([scan_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
        .collect::<Result<_>>()?;
    let mut child_sum: HashMap<&str, i64> = HashMap::new();
    for (_, parent, size) in &rows {
        if let Some(p) = parent {
            *child_sum.entry(p).or_default() += size;
        }
    }
    let mut totals: HashMap<Level, i64> = Level::ALL.iter().map(|&l| (l, 0)).collect();
    let mut top: HashMap<Level, Vec<(String, i64, String)>> =
        Level::ALL.iter().map(|&l| (l, vec![])).collect();
    for (i, (path, parent, size)) in rows.iter().enumerate() {
        if i % 5000 == 0 && cancel.load(Ordering::Relaxed) {
            return Ok(None);
        }
        let v = clf.classify(path, Some(true));
        *totals.get_mut(&v.level).unwrap() +=
            (size - child_sum.get(path.as_str()).copied().unwrap_or(0)).max(0);
        // « Élément représentatif » : premier dossier de la branche à changer de niveau.
        if parent
            .as_ref()
            .is_none_or(|p| clf.classify(p, Some(true)).level != v.level)
        {
            top.get_mut(&v.level)
                .unwrap()
                .push((path.clone(), *size, v.reason));
        }
    }
    // Installeurs de Téléchargements : comptés comme nettoyables.
    let root_n = norm(root);
    if under(&root_n, &clf.downloads) || under(&clf.downloads, &root_n) {
        let (cond, args) = subtree("d.path", &path_join(home, "Downloads"));
        let marks = vec!["?"; INSTALLERS.len()].join(",");
        let sql = format!(
            "SELECT COALESCE(SUM(f.size), 0) FROM files f JOIN dirpaths d ON d.id = f.dir_id \
             WHERE {cond} AND f.ext IN ({marks})"
        );
        let args: Vec<&str> = args
            .iter()
            .map(String::as_str)
            .chain(INSTALLERS.iter().copied())
            .collect();
        let n: i64 = conn.query_row(&sql, params_from_iter(args), |r| r.get(0))?;
        let n = n.min(totals[&Level::Personal]);
        *totals.get_mut(&Level::Personal).unwrap() -= n;
        *totals.get_mut(&Level::Cleanable).unwrap() += n;
    }
    for items in top.values_mut() {
        // Les plus gros d'abord ; à taille égale, ordre des chemins (Python : ordre en base).
        items.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        // Un élément contenu dans un autre de la liste n'apporte rien.
        let mut kept: Vec<(String, i64, String)> = Vec::new();
        for t in items.drain(..) {
            if !kept.iter().any(|k| under(&norm(&t.0), &norm(&k.0))) {
                kept.push(t);
            }
            if kept.len() >= 8 {
                break;
            }
        }
        *items = kept;
    }
    let mut software: Vec<String> = clf.software_dirs.values().cloned().collect();
    software.sort();
    Ok(Some(SafetySummary {
        scan_id,
        totals,
        top,
        software,
    }))
}

/// Ce que fait `SafetyWorker` : repère les logiciels du scan, puis calcule la répartition.
/// `None` si annulé (un résultat partiel n'est jamais publié).
pub fn analyze(
    db: &Path,
    scan_id: i64,
    root: &str,
    app_dir: Option<&str>,
    env: &Env,
    cancel: &AtomicBool,
) -> Result<Option<(Classifier, SafetySummary)>> {
    let conn = Connection::open(db)?;
    conn.busy_timeout(std::time::Duration::from_secs(30))?;
    let roots = software_dirs(&conn, root)?;
    let cont = containers(&conn, scan_id, &roots, root, &env.home)?;
    let clf = Classifier::new(env, roots, app_dir, cont);
    Ok(summarize(&conn, scan_id, root, &clf, &env.home, cancel)?.map(|s| (clf, s)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scanner::{ScanOptions, scan};
    use Level::*;

    fn env(home: &str) -> Env {
        Env {
            system_root: r"C:\Windows".into(),
            program_data: r"C:\ProgramData".into(),
            program_files: [
                r"C:\Program Files".into(),
                r"C:\Program Files (x86)".into(),
                r"C:\Program Files".into(),
            ],
            home: home.into(),
            local_appdata: Some(format!(r"{home}\AppData\Local")),
            temp: Some(format!(r"{home}\AppData\Local\Temp")),
        }
    }

    #[test]
    fn rules() {
        let h = r"C:\Users\Alice";
        let clf = Classifier::new(&env(h), Vec::<String>::new(), None, Vec::<String>::new());
        let cases: &[(&str, bool, Level)] = &[
            (r"C:\Windows\System32", true, System),
            (r"C:\pagefile.sys", false, System),
            (r"D:\System Volume Information", true, System),
            (r"D:\System Volume Information\tracking.log", false, System),
            (r"D:\Recovery\WindowsRE\winre.wim", false, System),
            (r"D:\Boot\BCD", false, System),
            (r"D:\Projets\boot\notes.txt", false, Personal), // « boot » hors de la racine
            ("D:\\", true, System),
            (h, true, System),
            (r"C:\Users\Alice\NTUSER.DAT", false, System),
            (r"C:\Users\Alice\AppData\Roaming\Code", true, Software),
            (
                r"C:\Users\Alice\AppData\Local\Google\Chrome\User Data\Default\Cache",
                true,
                Cleanable,
            ),
            (r"C:\Users\Alice\.vscode", true, Software),
            (r"C:\Users\Alice\.cache", true, Cleanable),
            (r"C:\Users\Alice\Downloads\setup.exe", false, Cleanable),
            (r"C:\Users\Alice\Downloads\photo.jpg", false, Personal),
            (r"D:\SteamLibrary\steamapps\common\Jeu", true, Software),
            (r"D:\projet\.git", true, Software),
            (r"D:\projet\node_modules", true, Cleanable),
            (r"D:\projet\src\__pycache__", true, Cleanable),
            (r"D:\Photos\vacances.jpg", false, Personal),
            (r"C:\Program Files (x86)", true, System),
            (
                r"C:\Program Files (x86)\Steam\steamapps\common\Jeu",
                true,
                Software,
            ),
            (r"C:\Program Files\Common Files\x", true, System),
            (r"C:\Program Files\WindowsApps", true, System),
            (r"C:\Program Files\VideoLAN\VLC", true, Software),
            (r"C:\ProgramData\NVIDIA", true, System),
            (r"D:\$Recycle.Bin", true, System),
            (r"C:\Users\Default\NTUSER.DAT", false, System),
            (r"C:\Users\Alice\AppData\Local\Temp\x.tmp", false, Cleanable),
            (r"D:\a\Thumbs.db", false, Cleanable),
        ];
        for &(path, is_dir, level) in cases {
            let v = clf.classify(path, Some(is_dir));
            assert_eq!(v.level, level, "{path} : {v:?}");
            assert!(!v.reason.is_empty());
        }
        let v = clf.classify(r"D:\$Recycle.Bin", Some(true));
        assert_eq!(v.reason, "Corbeille : videz-la depuis Windows");
        assert!(v.blocked() && !clf.classify(r"D:\Photos", Some(true)).blocked());
    }

    fn write(root: &Path, rel: &str, size: usize) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, vec![b'x'; size]).unwrap();
    }

    fn scanned(root: &Path, db: &Path) -> i64 {
        let o = ScanOptions {
            excluded_paths: vec![],
            excluded_names: vec![],
            ignore_patterns: vec![],
        };
        scan(
            root.to_str().unwrap(),
            db,
            &o,
            &AtomicBool::new(false),
            |_, _, _| {},
        )
        .unwrap()
        .scan_id
    }

    #[test]
    fn software_dirs_containers_and_summary() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        write(&root, "Jeux/Foo/bin/moteur.dll", 10);
        write(&root, "Jeux/Foo/foo.exe", 10);
        write(&root, "Jeux/Foo/data/pack.bin", 5000);
        write(&root, "Jeux/notes.txt", 10);
        write(&root, "Docs/rapport.pdf", 3000);
        let id = scanned(&root, &db);
        let s = |p: &Path| p.to_string_lossy().into_owned();
        let e = env(&s(&tmp.path().join("profil")));
        let (clf, sum) = analyze(&db, id, &s(&root), None, &e, &AtomicBool::new(false))
            .unwrap()
            .unwrap();
        let level = |rel: &str, dir| clf.classify(&s(&root.join(rel)), Some(dir)).level;
        assert_eq!(level(r"Jeux\Foo\data\pack.bin", false), Software);
        assert_eq!(level("Jeux", true), Software); // occupé à > 50 % par Foo
        assert_eq!(level(r"Jeux\notes.txt", false), Personal); // voisin : pas concerné
        assert_eq!(level("Docs", true), Personal);
        assert_eq!(
            clf.risky_inside(&s(&root), 5),
            [s(&root.join("Jeux").join("Foo"))]
        );
        assert!(sum.totals[&Software] >= 5000);
        assert_eq!(sum.totals.values().sum::<i64>(), 8030);
        let tops: Vec<&String> = sum.top[&Software].iter().map(|t| &t.0).collect();
        assert_eq!(tops, [&s(&root.join("Jeux"))]);
    }

    #[test]
    fn game_with_binaries_subfolder() {
        let tmp = tempfile::tempdir().unwrap();
        let (root, db) = (tmp.path().join("data"), tmp.path().join("cache.db"));
        write(
            &root,
            "Jeux/rocketleague/Binaries/Win64/RocketLeague.exe",
            10,
        );
        write(
            &root,
            "Jeux/rocketleague/Binaries/Win64/steam_api64.dll",
            10,
        );
        write(
            &root,
            "Jeux/rocketleague/TAGame/CookedPCConsole/map.upk",
            3000,
        );
        write(&root, "Jeux/Photos/a.jpg", 5000);
        scanned(&root, &db);
        let conn = Connection::open(&db).unwrap();
        let r = root.to_string_lossy();
        let clf = Classifier::new(
            &env(r"C:\Users\X"),
            software_dirs(&conn, &r).unwrap(),
            None,
            Vec::<String>::new(),
        );
        let p = root.join(r"Jeux\rocketleague\TAGame\CookedPCConsole\map.upk");
        assert_eq!(
            clf.classify(&p.to_string_lossy(), Some(false)).level,
            Software
        );
        assert_eq!(
            clf.classify(
                &root.join(r"Jeux\Photos\a.jpg").to_string_lossy(),
                Some(false)
            )
            .level,
            Personal
        );
    }

    #[cfg(windows)]
    #[test]
    fn short_8dot3_env_paths_are_recognized() {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::GetShortPathNameW;
        let tmp = tempfile::tempdir().unwrap();
        let long = tmp.path().join("dossier temporaire au nom long");
        std::fs::create_dir(&long).unwrap();
        let wide: Vec<u16> = long.as_os_str().encode_wide().chain([0]).collect();
        let mut buf = vec![0u16; 32768];
        // SAFETY : chemin terminé par un zéro, tampon de la taille annoncée.
        let n = unsafe { GetShortPathNameW(wide.as_ptr(), buf.as_mut_ptr(), buf.len() as u32) }
            as usize;
        let short = String::from_utf16_lossy(&buf[..n]);
        if n == 0 || short.ends_with("dossier temporaire au nom long") {
            eprintln!("noms courts 8.3 désactivés sur ce volume : test sauté");
            return;
        }
        let mut e = env(r"C:\Users\X");
        e.temp = Some(short);
        let clf = Classifier::new(&e, Vec::<String>::new(), None, Vec::<String>::new());
        assert_eq!(
            clf.classify(&long.join("x.tmp").to_string_lossy(), Some(false))
                .level,
            Cleanable
        );
    }

    #[test]
    fn helpers() {
        assert!(under(r"c:\a\b", r"c:\a") && under(r"c:\a", r"c:\a") && !under(r"c:\ab", r"c:\a"));
        assert!(!under(r"c:\a", ""));
        assert_eq!(split_drive(r"c:\x\y"), ("c:", r"\x\y"));
        assert_eq!(split_drive(r"\\srv\share\x"), (r"\\srv\share", r"\x"));
        assert_eq!(ext("setup.EXE"), ".EXE");
        assert_eq!(ext(".gitignore"), "");
    }
}
