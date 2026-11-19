//! Recherche de doublons : même taille → empreinte partielle (4 Ko) → empreinte complète
//! (xxh3-128). Port de `core/duplicates.py`. Les empreintes sont relues/écrites dans le
//! cache : un second passage sur des fichiers inchangés ne relit presque rien.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use rusqlite::{Result, params_from_iter};

use crate::cache::{Cache, subtree};
use crate::finder::{
    Cand, HashStats, PARTIAL_SIZE, Progress, Stale, fresh_stat, group_by, hash_cand, load_cands,
    save_hashes,
};

#[derive(Debug, Clone, PartialEq)]
pub struct DupFile {
    pub path: String,
    pub size: i64,
    pub mtime: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DupGroup {
    pub size: i64,
    pub digest: String,
    pub files: Vec<DupFile>,
}

impl DupGroup {
    /// Espace récupérable si on ne garde qu'un exemplaire.
    pub fn wasted(&self) -> i64 {
        self.size * (self.files.len() as i64 - 1)
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct DupResult {
    pub root: String,
    pub min_size: i64,
    pub groups: Vec<DupGroup>,
    /// Fichiers de même taille examinés.
    pub candidates: usize,
    /// Octets réellement lus.
    pub hashed_bytes: u64,
    pub skipped_cloud: u64,
    /// Modifiés ou supprimés depuis le scan.
    pub skipped_changed: u64,
    pub skipped_error: u64,
    pub cancelled: bool,
    pub duration: f64,
}

impl DupResult {
    pub fn wasted(&self) -> i64 {
        self.groups.iter().map(DupGroup::wasted).sum()
    }
}

/// Doublons sous `root` parmi les fichiers d'au moins `min_size` octets.
/// `progress(étape, fait, total)`.
pub fn find_duplicates(
    cache: &Cache,
    root: &str,
    min_size: i64,
    cancel: &AtomicBool,
    progress: &mut dyn FnMut(&str, u64, u64),
) -> Result<DupResult> {
    let t0 = Instant::now();
    let min_size = min_size.max(1);
    let mut res = DupResult {
        root: root.to_string(),
        min_size,
        ..Default::default()
    };
    let mut prog = Progress::new(progress);
    let done = |mut res: DupResult, cancelled: bool| {
        res.cancelled = cancelled;
        res.duration = t0.elapsed().as_secs_f64();
        Ok(res)
    };
    let stop = || cancel.load(Ordering::Relaxed);

    // 1. Candidats : tailles partagées par au moins deux fichiers.
    let (scope, sargs) = subtree("parent", root);
    let (dscope, _) = subtree("path", root);
    let sql = format!(
        "SELECT path, dir_id, name, size, mtime, partial_hash, full_hash FROM file_paths \
         WHERE {scope} AND size >= ? AND size IN (\
           SELECT size FROM files WHERE dir_id IN (SELECT id FROM dirpaths WHERE {dscope}) AND size >= ? \
           GROUP BY size HAVING COUNT(*) > 1)"
    );
    let args: Vec<rusqlite::types::Value> = sargs
        .iter()
        .cloned()
        .map(Into::into)
        .chain([min_size.into()])
        .chain(sargs.iter().cloned().map(Into::into))
        .chain([min_size.into()])
        .collect();
    let rows = load_cands(cache, &sql, params_from_iter(args))?;
    res.candidates = rows.len();

    // 2. Vérification (existe, inchangé, pas en ligne) + fusion des liens physiques.
    let total = rows.len() as u64;
    let mut seen: HashMap<i64, HashSet<(u32, u64)>> = HashMap::new();
    let mut kept = Vec::with_capacity(rows.len());
    for (i, c) in rows.into_iter().enumerate() {
        if stop() {
            return done(res, true);
        }
        prog.emit("Vérification des fichiers", i as u64, total, false);
        let st = match fresh_stat(&c.path, c.size, c.mtime) {
            Ok(st) => st,
            Err(Stale::Cloud) => {
                res.skipped_cloud += 1;
                continue;
            }
            Err(Stale::Changed) => {
                res.skipped_changed += 1;
                continue;
            }
        };
        // Lien physique vers un fichier déjà compté : même identité.
        if !seen.entry(c.size).or_default().insert(st.id) && st.id.1 != 0 {
            continue;
        }
        kept.push(c);
    }
    let mut groups: Vec<Vec<Cand>> = group_by(kept, |c| c.size)
        .into_iter()
        .map(|(_, g)| g)
        .filter(|g| g.len() > 1)
        .collect();

    // 3. Empreinte partielle (4 premiers Ko).
    let mut stats = HashStats::default();
    let todo_total = groups
        .iter()
        .flatten()
        .filter(|c| c.partial.is_none())
        .count() as u64;
    let mut i = 0;
    'partial: for g in &mut groups {
        for c in g.iter_mut().filter(|c| c.partial.is_none()) {
            if stop() {
                break 'partial;
            }
            prog.emit("Hash partiel (4 Ko)", i, todo_total, false);
            i += 1;
            c.partial = hash_cand(c, Some(PARTIAL_SIZE), &mut stats, cancel, &mut |_| {});
            if c.partial.is_some() && c.size as u64 <= PARTIAL_SIZE {
                c.full = c.partial.clone(); // le début est le fichier entier
            }
        }
    }
    save_hashes(cache, groups.iter().flatten())?;
    if stop() {
        absorb(&mut res, &stats);
        return done(res, true);
    }
    for g in &mut groups {
        g.iter_mut().for_each(|c| c.dirty = false);
    }
    let mut groups = regroup(groups, |c| c.partial.clone());

    // 4. Empreinte complète des fichiers encore en concurrence.
    let total: u64 = groups
        .iter()
        .flatten()
        .filter(|c| c.full.is_none())
        .map(|c| c.size as u64)
        .sum();
    let mut hashed = 0u64;
    'full: for g in &mut groups {
        for c in g.iter_mut().filter(|c| c.full.is_none()) {
            if stop() {
                break 'full;
            }
            let size = c.size as u64;
            c.full = hash_cand(c, None, &mut stats, cancel, &mut |n| {
                prog.emit("Hash complet", hashed + n, total, false)
            });
            hashed += size;
        }
    }
    prog.emit("Hash complet", hashed, total, true);
    save_hashes(cache, groups.iter().flatten())?;
    absorb(&mut res, &stats);
    if stop() {
        return done(res, true);
    }

    for g in regroup(groups, |c| c.full.clone()) {
        res.groups.push(DupGroup {
            size: g[0].size,
            digest: g[0].full.clone().unwrap_or_default(),
            files: g
                .into_iter()
                .map(|c| DupFile {
                    path: c.path,
                    size: c.size,
                    mtime: c.mtime,
                })
                .collect(),
        });
    }
    // Le plus de place à gagner d'abord ; à égalité, par chemin (les fichiers sont déjà triés).
    res.groups.sort_by(|a, b| {
        b.wasted()
            .cmp(&a.wasted())
            .then_with(|| a.files[0].path.cmp(&b.files[0].path))
    });
    done(res, false)
}

fn absorb(res: &mut DupResult, stats: &HashStats) {
    res.hashed_bytes = stats.hashed_bytes;
    res.skipped_error = stats.skipped_error;
}

/// Sous-groupes de même clé (au moins deux) ; clé `None` = illisible, écarté.
fn regroup(groups: Vec<Vec<Cand>>, key: impl Fn(&Cand) -> Option<String>) -> Vec<Vec<Cand>> {
    groups
        .into_iter()
        .flat_map(|g| {
            group_by(g.into_iter().filter(|c| key(c).is_some()).collect(), |c| {
                key(c)
            })
        })
        .map(|(_, g)| g)
        .filter(|g| g.len() > 1)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scanner::{ScanOptions, scan};
    use std::path::Path;

    fn write(root: &Path, rel: &str, content: &[u8]) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, content).unwrap();
    }

    fn setup(files: &[(&str, &[u8])]) -> (tempfile::TempDir, String, Cache) {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        for (rel, content) in files {
            write(&root, rel, content);
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
        let cache = Cache::open(&db).unwrap();
        (tmp, root.to_string_lossy().into_owned(), cache)
    }

    fn find(cache: &Cache, root: &str) -> DupResult {
        find_duplicates(cache, root, 1, &AtomicBool::new(false), &mut |_, _, _| {}).unwrap()
    }

    #[test]
    fn finds_groups_and_caches_hashes() {
        let big = vec![7u8; 10_000];
        let mut big2 = big.clone();
        *big2.last_mut().unwrap() = 8; // même début, fin différente
        let (_t, root, cache) = setup(&[
            ("a/x.bin", &big),
            ("b/x copie.bin", &big),
            ("c/autre.bin", &big2),
            ("p1.txt", b"petit"),
            ("p2.txt", b"petit"),
            ("seul.txt", b"unique"),
        ]);
        let r = find(&cache, &root);
        assert_eq!(r.groups.len(), 2);
        assert_eq!(r.groups[0].size, 10_000); // le plus de place à gagner d'abord
        let names: Vec<&str> = r.groups[0]
            .files
            .iter()
            .map(|f| f.path.rsplit('\\').next().unwrap())
            .collect();
        assert_eq!(names, ["x.bin", "x copie.bin"]);
        assert_eq!(r.wasted(), 10_000 + 5);
        assert!(r.hashed_bytes > 0 && !r.cancelled);
        // 2e passage : tout vient du cache.
        let again = find(&cache, &root);
        assert_eq!(again.hashed_bytes, 0);
        assert_eq!(again.groups, r.groups);
    }

    #[test]
    fn hard_links_count_once_and_changed_files_are_skipped() {
        let (t, root, cache) = setup(&[
            ("a.bin", b"contenu"),
            ("b.bin", b"contenu"),
            ("c.bin", b"autre!!"),
        ]);
        std::fs::hard_link(
            Path::new(&root).join("a.bin"),
            Path::new(&root).join("lien.bin"),
        )
        .unwrap();
        let db = t.path().join("cache.db");
        let o = ScanOptions {
            excluded_paths: vec![],
            excluded_names: vec![],
            ignore_patterns: vec![],
        };
        scan(&root, &db, &o, &AtomicBool::new(false), |_, _, _| {}).unwrap();
        std::fs::write(Path::new(&root).join("c.bin"), b"change").unwrap(); // modifié après le scan
        let r = find(&cache, &root);
        assert_eq!(r.skipped_changed, 1);
        assert_eq!(r.groups.len(), 1);
        assert_eq!(
            r.groups[0].files.len(),
            2,
            "le lien physique ne compte pas deux fois"
        );
    }

    #[test]
    fn cancelled_search() {
        let (_t, root, cache) = setup(&[("a.bin", b"x"), ("b.bin", b"x")]);
        let r =
            find_duplicates(&cache, &root, 1, &AtomicBool::new(true), &mut |_, _, _| {}).unwrap();
        assert!(r.cancelled && r.groups.is_empty());
    }
}
