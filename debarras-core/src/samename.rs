//! Même nom, contenu différent : versions divergentes d'un même fichier dans plusieurs
//! dossiers. Port de `core/samename.py`.
//!
//! Un groupe = fichiers de même nom (casse ignorée) sous la racine, retenu seulement s'il
//! contient au moins deux contenus distincts. Tailles différentes = contenus différents sans
//! rien lire ; à taille égale, empreintes 4 Ko puis complètes (partagées avec les doublons).

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use rusqlite::{Result, params_from_iter, types::Value};

use crate::cache::{Cache, subtree};
use crate::filetypes::extensions;
use crate::finder::{
    Cand, HashStats, PARTIAL_SIZE, Progress, Stale, fresh_stat, group_by, hash_cand, load_cands,
    save_hashes,
};
use crate::paths::{dirname, norm};
use crate::scanner::fnmatch_str;

const PERSONAL_CATEGORIES: [&str; 5] = ["Documents", "Images", "Vidéos", "Audio", "Archives"];
/// Dossiers de code, dépendances et caches : des milliers de fichiers homonymes sans intérêt.
const DEV_DIRS: &[&str] = &[
    "node_modules",
    ".venv",
    "venv",
    "env",
    "site-packages",
    "dist-packages",
    ".git",
    ".svn",
    "__pycache__",
    ".cargo",
    ".rustup",
    ".gradle",
    ".m2",
    ".nuget",
    "packages",
    "vendor",
    "bower_components",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "build",
    "dist",
    "appdata",
    "$recycle.bin",
];
const GENERIC_NAMES: &[&str] = &[
    "desktop.ini",
    "thumbs.db",
    ".ds_store",
    "readme*",
    "license*",
    "licence*",
    "changelog*",
    "copying*",
    "index.*",
    "__init__.py",
    "folder.jpg",
    "cover.jpg",
    "albumart*.jpg",
    "icon.*",
    "favicon.*",
    "logo.*",
    "*.lnk",
    "*.url",
];
/// Au-delà : nom générique (image1.png…), écarté.
const MAX_GROUP: usize = 50;
const HASH_PHASE: &str = "Hash complet";

#[derive(Debug, Clone, PartialEq)]
pub struct SameNameFile {
    pub path: String,
    pub size: i64,
    pub mtime: f64,
    /// A = contenu le plus récent ; même lettre = copies identiques ; `?` = inconnu.
    pub version: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SameNameGroup {
    pub name: String,
    pub files: Vec<SameNameFile>,
}

impl SameNameGroup {
    pub fn versions(&self) -> usize {
        let mut v: Vec<&str> = self
            .files
            .iter()
            .map(|f| f.version.as_str())
            .filter(|v| *v != "?")
            .collect();
        v.sort();
        v.dedup();
        v.len()
    }

    /// Place libérée en ne gardant que le fichier le plus récent.
    pub fn wasted(&self) -> i64 {
        let newest = self
            .files
            .iter()
            .max_by(|a, b| a.mtime.total_cmp(&b.mtime))
            .map_or(0, |f| f.size);
        self.files.iter().map(|f| f.size).sum::<i64>() - newest
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SameNameResult {
    pub root: String,
    pub groups: Vec<SameNameGroup>,
    pub candidates: usize,
    pub hashed_bytes: u64,
    /// Groupes écartés (noms génériques ou trop fréquents).
    pub skipped_generic: u64,
    pub skipped_cloud: u64,
    pub skipped_changed: u64,
    pub skipped_error: u64,
    pub cancelled: bool,
    pub duration: f64,
}

impl SameNameResult {
    pub fn wasted(&self) -> i64 {
        self.groups.iter().map(SameNameGroup::wasted).sum()
    }
}

pub fn is_generic(name: &str) -> bool {
    let low = name.to_lowercase();
    GENERIC_NAMES.iter().any(|p| fnmatch_str(p, &low))
}

/// Sous un dossier de code/dépendances, ou de configuration d'application (.claude, .vscode…) :
/// les fichiers homonymes y sont des conventions (SKILL.md, config.json…), pas des versions.
pub fn in_dev_dir(path: &str, root: &str) -> bool {
    let (dir, root) = (norm(dirname(path)), norm(root));
    let rel = dir.strip_prefix(&root).unwrap_or(&dir);
    rel.split('\\')
        .filter(|p| !p.is_empty())
        .any(|p| DEV_DIRS.contains(&p) || (p.starts_with('.') && p != "." && p != ".."))
}

/// Identité du contenu : la taille si elle est unique, sinon l'empreinte partielle ou complète.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum Key {
    Size(i64),
    Partial(i64, String),
    Full(i64, String),
}

struct Item {
    c: Cand,
    key: Option<Key>,
}

/// Fichiers homonymes sous `root`. `personal_only` : seulement documents, images, vidéos,
/// audio et archives ; `skip_dev_dirs` : hors dossiers de code et de réglages.
pub fn find_same_names(
    cache: &Cache,
    root: &str,
    min_size: i64,
    personal_only: bool,
    skip_dev_dirs: bool,
    cancel: &AtomicBool,
    progress: &mut dyn FnMut(&str, u64, u64),
) -> Result<SameNameResult> {
    let t0 = Instant::now();
    let min_size = min_size.max(0);
    let mut res = SameNameResult {
        root: root.to_string(),
        ..Default::default()
    };
    let mut prog = Progress::new(progress);
    let stop = || cancel.load(Ordering::Relaxed);

    let (scope, sargs) = subtree("parent", root);
    let (dscope, _) = subtree("path", root);
    let exts: Vec<&str> = if personal_only {
        PERSONAL_CATEGORIES
            .iter()
            .flat_map(|c| extensions(c))
            .collect()
    } else {
        vec![]
    };
    let ext_cond = if personal_only {
        format!("ext IN ({})", vec!["?"; exts.len()].join(","))
    } else {
        "1".into()
    };
    // Noms présents au moins deux fois (lower() SQLite : casse ASCII ; affiné ensuite).
    let sql = format!(
        "SELECT path, dir_id, name, size, mtime, partial_hash, full_hash FROM file_paths \
         WHERE {scope} AND size >= ? AND {ext_cond} AND lower(name) IN (\
           SELECT lower(name) FROM files \
           WHERE dir_id IN (SELECT id FROM dirpaths WHERE {dscope}) AND size >= ? AND {ext_cond} \
           GROUP BY lower(name) HAVING COUNT(*) > 1)"
    );
    let ext_vals = || exts.iter().map(|e| Value::from(e.to_string()));
    let sv = || sargs.iter().cloned().map(Value::from);
    let args: Vec<Value> = sv()
        .chain([min_size.into()])
        .chain(ext_vals())
        .chain(sv())
        .chain([min_size.into()])
        .chain(ext_vals())
        .collect();
    let rows = load_cands(cache, &sql, params_from_iter(args))?;

    // shortcut: casse comparée par to_lowercase et non casefold (ß ≠ ss) ; à revoir si besoin.
    let rows: Vec<Cand> = rows
        .into_iter()
        .filter(|c| !(skip_dev_dirs && in_dev_dir(&c.path, root)))
        .collect();
    let mut groups: Vec<Vec<Item>> = Vec::new();
    for (key, cands) in group_by(rows, |c| c.name.to_lowercase()) {
        if cands.len() < 2 {
            continue;
        }
        if cands.len() > MAX_GROUP || is_generic(&key) {
            res.skipped_generic += 1;
            continue;
        }
        groups.push(cands.into_iter().map(|c| Item { c, key: None }).collect());
    }
    res.candidates = groups.iter().map(Vec::len).sum();

    // Identité du contenu : la taille suffit si elle est unique dans le groupe.
    let mut stats = HashStats::default();
    let mut to_check: Vec<(usize, Vec<usize>)> = Vec::new(); // (groupe, indices de même taille)
    for (gi, g) in groups.iter_mut().enumerate() {
        for (size, idx) in group_by((0..g.len()).collect(), |&i| g[i].c.size) {
            if idx.len() == 1 {
                g[idx[0]].key = Some(Key::Size(size));
            } else {
                to_check.push((gi, idx));
            }
        }
    }

    // À taille égale : 4 premiers Ko, puis fichier entier si nécessaire.
    let total: u64 = to_check
        .iter()
        .map(|(gi, idx)| {
            idx.iter()
                .map(|&i| groups[*gi][i].c.size as u64)
                .sum::<u64>()
        })
        .sum();
    let mut done = 0u64;
    let mut cancelled = false;
    'outer: for (gi, idx) in &to_check {
        let g = &mut groups[*gi];
        let mut usable = vec![false; g.len()];
        for &i in idx {
            if stop() {
                cancelled = true;
                break 'outer;
            }
            prog.emit(HASH_PHASE, done, total, false);
            let c = &mut g[i].c;
            usable[i] = match fresh_stat(&c.path, c.size, c.mtime) {
                Ok(_) => true,
                Err(Stale::Cloud) => {
                    res.skipped_cloud += 1;
                    false
                }
                Err(Stale::Changed) => {
                    res.skipped_changed += 1;
                    false
                }
            };
            if usable[i] && c.partial.is_none() {
                c.partial = hash_cand(c, Some(PARTIAL_SIZE), &mut stats, cancel, &mut |_| {});
                if c.partial.is_some() && c.size as u64 <= PARTIAL_SIZE {
                    c.full = c.partial.clone();
                }
            }
        }
        let with_partial: Vec<usize> = idx
            .iter()
            .copied()
            .filter(|&i| g[i].c.partial.is_some())
            .collect();
        for (partial, sub) in group_by(with_partial, |&i| {
            g[i].c.partial.clone().unwrap_or_default()
        }) {
            if sub.len() == 1 {
                g[sub[0]].key = Some(Key::Partial(g[sub[0]].c.size, partial));
                continue;
            }
            for i in sub {
                let it = &mut g[i];
                if it.c.full.is_none() && usable[i] {
                    // jamais de lecture d'un fichier en ligne
                    let base = done;
                    it.c.full = hash_cand(&mut it.c, None, &mut stats, cancel, &mut |n| {
                        prog.emit(HASH_PHASE, base + n, total, false)
                    });
                }
                if let Some(full) = &it.c.full {
                    it.key = Some(Key::Full(it.c.size, full.clone()));
                }
            }
        }
        done += idx.iter().map(|&i| g[i].c.size as u64).sum::<u64>();
    }
    if !cancelled {
        prog.emit(HASH_PHASE, total, total, true);
    }

    // Empreintes réutilisables par la recherche de doublons.
    save_hashes(cache, groups.iter().flatten().map(|it| &it.c))?;
    res.hashed_bytes = stats.hashed_bytes;
    res.skipped_error = stats.skipped_error;
    if !cancelled {
        for g in groups {
            let grp = labelled(g);
            if grp.versions() >= 2 {
                res.groups.push(grp);
            }
        }
        res.groups.sort_by(|a, b| {
            let total = |g: &SameNameGroup| g.files.iter().map(|f| f.size).sum::<i64>();
            total(b).cmp(&total(a))
        });
    }
    res.cancelled = cancelled;
    res.duration = t0.elapsed().as_secs_f64();
    Ok(res)
}

/// Lettres de version : A = contenu le plus récemment modifié.
fn labelled(items: Vec<Item>) -> SameNameGroup {
    let mut newest: Vec<(Key, f64)> = Vec::new();
    for it in &items {
        if let Some(k) = &it.key {
            match newest.iter_mut().find(|(n, _)| n == k) {
                Some((_, t)) => *t = t.max(it.c.mtime),
                None => newest.push((k.clone(), it.c.mtime)),
            }
        }
    }
    newest.sort_by(|a, b| b.1.total_cmp(&a.1)); // stable : à égalité, ordre d'apparition
    let letters: HashMap<Key, String> = newest
        .into_iter()
        .enumerate()
        .map(|(i, (k, _))| {
            (
                k,
                if i < 26 {
                    char::from(b'A' + i as u8).to_string()
                } else {
                    (i + 1).to_string()
                },
            )
        })
        .collect();
    let name = items[0].c.name.clone();
    let mut items = items;
    items.sort_by(|a, b| b.c.mtime.total_cmp(&a.c.mtime));
    let files = items
        .into_iter()
        .map(|it| SameNameFile {
            version: it
                .key
                .and_then(|k| letters.get(&k).cloned())
                .unwrap_or_else(|| "?".into()),
            path: it.c.path,
            size: it.c.size,
            mtime: it.c.mtime,
        })
        .collect();
    SameNameGroup { name, files }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scanner::{ScanOptions, scan};
    use std::path::Path;

    fn setup(files: &[(&str, &[u8], u64)]) -> (tempfile::TempDir, String, Cache) {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        let base = std::time::SystemTime::now() - std::time::Duration::from_secs(86_400);
        for (rel, content, age) in files {
            let p = root.join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(&p, content).unwrap();
            let f = std::fs::File::options().write(true).open(&p).unwrap();
            f.set_modified(base - std::time::Duration::from_secs(*age))
                .unwrap();
        }
        let db = tmp.path().join("cache.db");
        let o = ScanOptions {
            excluded_paths: vec![],
            excluded_names: vec![],
            ignore_patterns: vec![],
        };
        scan(
            root.to_str().unwrap(),
            &db,
            &o,
            &AtomicBool::new(false),
            |_, _, _| {},
        )
        .unwrap();
        (
            tmp,
            root.to_string_lossy().into_owned(),
            Cache::open(&db).unwrap(),
        )
    }

    fn find(cache: &Cache, root: &str) -> SameNameResult {
        find_same_names(
            cache,
            root,
            1,
            true,
            true,
            &AtomicBool::new(false),
            &mut |_, _, _| {},
        )
        .unwrap()
    }

    #[test]
    fn versions_lettered_newest_first() {
        let (_t, root, cache) = setup(&[
            ("a/Rapport.docx", b"version 1", 300),
            ("b/rapport.docx", b"version 2", 100), // la plus récente
            ("c/rapport.docx", b"version 1", 200), // copie de a
            ("d/rapport.docx", b"version 3!", 400), // taille différente : rien à lire
            ("x/pareil.pdf", b"meme", 10),
            ("y/pareil.pdf", b"meme", 20), // copies identiques seulement : pas de groupe
            ("z/README.md", b"un", 1),
            ("w/readme.md", b"deux", 1), // nom générique
            ("proj/node_modules/rapport.docx", b"ignore", 1),
        ]);
        let r = find(&cache, &root);
        assert_eq!(r.skipped_generic, 1);
        assert_eq!(r.groups.len(), 1);
        let g = &r.groups[0];
        let v: Vec<(&str, &str)> = g
            .files
            .iter()
            .map(|f| (&f.path[root.len() + 1..], f.version.as_str()))
            .collect();
        assert_eq!(
            v,
            [
                (r"b\rapport.docx", "A"),
                (r"c\rapport.docx", "B"),
                (r"a\Rapport.docx", "B"),
                (r"d\rapport.docx", "C")
            ]
        );
        assert_eq!(g.versions(), 3);
        assert_eq!(g.wasted(), 9 + 9 + 10);
    }

    #[test]
    fn dev_dirs_and_generic_names() {
        assert!(in_dev_dir(r"C:\r\p\node_modules\x.js", r"C:\r"));
        assert!(in_dev_dir(r"C:\r\.claude\SKILL.md", r"C:\r"));
        assert!(!in_dev_dir(r"C:\r\Docs\x.pdf", r"C:\r"));
        assert!(!in_dev_dir(r"C:\r\x.pdf", r"C:\r"));
        assert!(
            is_generic("README.txt") && is_generic("raccourci.lnk") && !is_generic("rapport.docx")
        );
    }

    #[test]
    fn cancelled_search() {
        let (_t, root, cache) = setup(&[("a/x.pdf", b"1234", 1), ("b/x.pdf", b"5678", 2)]);
        let r = find_same_names(
            &cache,
            &root,
            1,
            true,
            true,
            &AtomicBool::new(true),
            &mut |_, _, _| {},
        )
        .unwrap();
        assert!(r.cancelled && r.groups.is_empty());
        let _ = Path::new(&root);
    }
}
