//! Statistiques tirées du cache : types, gros fichiers, fichiers anciens, dossiers vides,
//! temporaires et caches, installeurs oubliés. Port de `core/stats.py`.

use rusqlite::{Connection, OptionalExtension, Result, params, params_from_iter, types::Value};

use crate::cache::{now, subtree};
use crate::filetypes::{CATEGORIES, category};
use crate::paths::{dirname, split};
use crate::scanner::extension;

const TEMP_DIR_NAMES: &[&str] = &[
    "temp",
    "tmp",
    ".tmp",
    "cache",
    "caches",
    ".cache",
    "__pycache__",
    "gpucache",
    "code cache",
    "shadercache",
    "dxcache",
    "glcache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".sass-cache",
    ".parcel-cache",
    ".next",
    ".nuxt",
    ".gradle",
];
const TEMP_EXTS: [&str; 3] = [".tmp", ".temp", ".dmp"];
const INSTALLER_EXTS: [&str; 5] = [".msi", ".msix", ".msixbundle", ".appx", ".appxbundle"];
const DOWNLOAD_DIRS: [&str; 2] = ["downloads", "téléchargements"];
const INSTALLER_MIN_AGE_DAYS: f64 = 30.0;
pub const DEFAULT_OLD_DAYS: i64 = 365;
const LIMIT: usize = 500;

#[derive(Debug, Clone, PartialEq)]
pub struct FileStat {
    pub path: String,
    pub size: i64,
    pub mtime: f64,
    pub atime: f64,
}

impl FileStat {
    /// Accès souvent non mis à jour sous Windows : le plus récent des deux.
    pub fn last_used(&self) -> f64 {
        self.atime.max(self.mtime)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DirStat {
    pub path: String,
    pub size: i64,
    pub file_count: i64,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct StatsResult {
    pub scan_id: i64,
    pub root: String,
    pub total_size: i64,
    pub file_count: i64,
    /// Catégorie → (taille, nombre), dans l'ordre de `CATEGORIES`.
    pub by_category: Vec<(String, i64, i64)>,
    /// (extension, taille, nombre), les plus lourdes d'abord.
    pub by_ext: Vec<(String, i64, i64)>,
    pub largest: Vec<FileStat>,
    pub old_days: i64,
    pub old: Vec<FileStat>,
    /// (taille, nombre).
    pub old_total: (i64, i64),
    pub empty_dirs: Vec<String>,
    pub temp_dirs: Vec<DirStat>,
    pub temp_files: Vec<FileStat>,
    pub installers: Vec<FileStat>,
}

/// Fichiers sous `root` vérifiant `cond`, les plus gros d'abord (à taille égale, par chemin).
fn files(
    conn: &Connection,
    root: &str,
    cond: &str,
    args: Vec<Value>,
    limit: usize,
) -> Result<Vec<FileStat>> {
    let (scope, sargs) = subtree("parent", root);
    let sql = format!(
        "SELECT path, size, mtime, atime FROM file_paths WHERE {scope} AND {cond} ORDER BY size DESC, path LIMIT {limit}"
    );
    let all: Vec<Value> = sargs.into_iter().map(Value::from).chain(args).collect();
    conn.prepare(&sql)?
        .query_map(params_from_iter(all), |r| {
            Ok(FileStat {
                path: r.get(0)?,
                size: r.get(1)?,
                mtime: r.get(2)?,
                atime: r.get(3)?,
            })
        })?
        .collect()
}

/// `setup|install|x64|x86|win64|win32|amd64|[-_]win\b`, sans casse, n'importe où dans le nom.
fn looks_like_installer(name: &str) -> bool {
    let n = name.to_lowercase();
    if ["setup", "install", "x64", "x86", "win64", "win32", "amd64"]
        .iter()
        .any(|t| n.contains(t))
    {
        return true;
    }
    n.match_indices("win").any(|(i, _)| {
        let before = n[..i].chars().next_back();
        let after = n[i + 3..].chars().next();
        matches!(before, Some('-' | '_')) && !after.is_some_and(|c| c.is_alphanumeric() || c == '_')
    })
}

pub fn compute_stats(
    conn: &Connection,
    scan_id: i64,
    root: &str,
    old_days: i64,
) -> Result<StatsResult> {
    let mut res = StatsResult {
        scan_id,
        root: root.to_string(),
        old_days,
        ..Default::default()
    };
    let (scope, sargs) = subtree("path", root);
    let in_root = format!("dir_id IN (SELECT id FROM dirpaths WHERE {scope})"); // fichiers sous `root`
    let now = now();

    if let Some((size, count)) = conn
        .query_row(
            "SELECT size, file_count FROM dir_sizes WHERE scan_id=? AND path=?",
            params![scan_id, root],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?
    {
        (res.total_size, res.file_count) = (size, count);
    }

    // Répartition par extension puis par catégorie.
    let mut cats: Vec<(String, i64, i64)> =
        CATEGORIES.iter().map(|c| (c.to_string(), 0, 0)).collect();
    let mut st = conn.prepare(&format!(
        "SELECT ext, SUM(size), COUNT(*) FROM files WHERE {in_root} GROUP BY ext"
    ))?;
    for row in st.query_map(params_from_iter(&sargs), |r| {
        Ok((r.get::<_, String>(0)?, r.get(1)?, r.get(2)?))
    })? {
        let (ext, size, count): (String, i64, i64) = row?;
        let c = cats
            .iter_mut()
            .find(|c| c.0 == category(&ext))
            .expect("catégorie connue");
        c.1 += size;
        c.2 += count;
        res.by_ext.push((ext, size, count));
    }
    res.by_ext.sort_by_key(|a| std::cmp::Reverse(a.1));
    res.by_category = cats;
    res.largest = files(conn, root, "1", vec![], LIMIT)?;

    // Fichiers anciens : ni accédés ni modifiés depuis `old_days` jours.
    let cutoff = now - old_days as f64 * 86400.0;
    res.old = files(
        conn,
        root,
        "MAX(atime, mtime) < ?",
        vec![cutoff.into()],
        LIMIT,
    )?;
    let args: Vec<Value> = sargs
        .iter()
        .cloned()
        .map(Value::from)
        .chain([cutoff.into()])
        .collect();
    res.old_total = conn.query_row(
        &format!("SELECT COALESCE(SUM(size), 0), COUNT(*) FROM files WHERE {in_root} AND MAX(atime, mtime) < ?"),
        params_from_iter(args),
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;

    // Dossiers vides : seulement le plus haut d'une branche vide, sans rien d'ignoré dedans.
    res.empty_dirs = conn
        .prepare(&format!(
            "SELECT dp.path FROM dirs d JOIN dirs p ON p.scan_id = d.scan_id AND p.dir_id = d.parent_id \
             JOIN dirpaths dp ON dp.id = d.dir_id \
             WHERE d.scan_id = ? AND d.file_count = 0 AND d.skipped = 0 \
             AND (p.file_count > 0 OR p.skipped > 0 OR p.parent_id IS NULL) ORDER BY dp.path LIMIT {LIMIT}"
        ))?
        .query_map([scan_id], |r| r.get(0))?
        .collect::<Result<_>>()?;

    // Temp/caches : dossiers au nom connu (le plus haut seulement) + fichiers temporaires isolés.
    let mut cands: Vec<DirStat> = conn
        .prepare(
            "SELECT d.path, s.size, s.file_count FROM dirs s JOIN dirpaths d ON d.id = s.dir_id \
             WHERE s.scan_id = ? AND s.parent_id IS NOT NULL AND s.size > 0",
        )?
        .query_map([scan_id], |r| {
            Ok(DirStat {
                path: r.get(0)?,
                size: r.get(1)?,
                file_count: r.get(2)?,
            })
        })?
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .filter(|d| TEMP_DIR_NAMES.contains(&split(&d.path).1.to_lowercase().as_str()))
        .collect();
    cands.sort_by(|a, b| a.path.cmp(&b.path));
    let mut prefixes: Vec<String> = Vec::new();
    let mut kept = Vec::new();
    for d in cands {
        if !prefixes.iter().any(|p| d.path.starts_with(p)) {
            prefixes.push(format!("{}\\", d.path));
            kept.push(d);
        }
    }
    kept.sort_by_key(|a| std::cmp::Reverse(a.size));
    kept.truncate(LIMIT);
    res.temp_dirs = kept;
    let marks = vec!["?"; TEMP_EXTS.len()].join(",");
    let temp_args = TEMP_EXTS
        .iter()
        .map(|e| Value::from(e.to_string()))
        .collect();
    res.temp_files = files(
        conn,
        root,
        &format!("(ext IN ({marks}) OR name LIKE '~$%')"),
        temp_args,
        LIMIT * 4,
    )?
    .into_iter()
    .filter(|f| !prefixes.iter().any(|p| f.path.starts_with(p)))
    .take(LIMIT)
    .collect();

    // Installeurs oubliés : paquets d'installation, ou .exe/.iso ressemblant à un setup.
    let age_cut = now - INSTALLER_MIN_AGE_DAYS * 86400.0;
    let exts: Vec<&str> = INSTALLER_EXTS
        .iter()
        .copied()
        .chain([".exe", ".iso"])
        .collect();
    let marks = vec!["?"; exts.len()].join(",");
    let args = exts
        .iter()
        .map(|e| Value::from(e.to_string()))
        .chain([age_cut.into()])
        .collect();
    for f in files(
        conn,
        root,
        &format!("ext IN ({marks}) AND mtime < ?"),
        args,
        LIMIT * 20,
    )? {
        let name = split(&f.path).1;
        let ext = extension(name);
        let in_downloads =
            DOWNLOAD_DIRS.contains(&split(dirname(&f.path)).1.to_lowercase().as_str());
        if INSTALLER_EXTS.contains(&ext.as_str())
            || in_downloads
            || (ext == ".exe" && looks_like_installer(name))
        {
            res.installers.push(f);
            if res.installers.len() >= LIMIT {
                break;
            }
        }
    }
    Ok(res)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::Cache;
    use crate::testutil::{age, scan_dir, write};

    #[test]
    fn computes_stats() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        let w = |rel: &str, n: usize, days: u64| {
            write(&root.join(rel), n);
            if days > 0 {
                age(&root.join(rel), days);
            }
        };
        w(r"docs\rapport.pdf", 3000, 0);
        w(r"docs\old.docx", 2000, 800);
        w(r"media\film.mkv", 50_000, 0);
        w(r"proj\__pycache__\m.pyc", 400, 0);
        w(r"proj\__pycache__\cache\inner.pyc", 100, 0); // caché dans un cache : pas listé à part
        w(r"proj\build.tmp", 10, 0);
        w(r"Downloads\tool.exe", 700, 90);
        w(r"apps\Setup_x64.exe", 800, 90);
        w(r"apps\portable.exe", 900, 90); // ni installeur ni dans Téléchargements
        w(r"apps\new_setup.exe", 800, 0); // trop récent
        w(r"pkg\app.msi", 600, 90);
        std::fs::create_dir_all(root.join(r"vide\sous-vide")).unwrap();
        let db = scan_dir(&root, &tmp.path().join("c.db"));
        let c = Cache::open(&db).unwrap();
        let r = root.to_string_lossy().into_owned();
        let scan = c.latest_scan(&r).unwrap().unwrap();
        let st = compute_stats(&c.conn, scan.id, &r, 365).unwrap();
        let rel = |paths: Vec<&String>| {
            let mut v: Vec<String> = paths.iter().map(|p| p[r.len() + 1..].to_string()).collect();
            v.sort();
            v
        };
        assert_eq!(
            (st.total_size, st.file_count),
            (scan.total_size, scan.file_count)
        );
        let cat = |n: &str| {
            st.by_category
                .iter()
                .find(|c| c.0 == n)
                .map(|c| (c.1, c.2))
                .unwrap()
        };
        assert_eq!(cat("Vidéos"), (50_000, 1));
        assert_eq!(cat("Documents"), (5000, 2));
        assert!(st.largest[0].path.ends_with("film.mkv"));
        assert_eq!(
            rel(st.old.iter().map(|f| &f.path).collect()),
            [r"docs\old.docx"]
        );
        assert_eq!(st.old_total, (2000, 1));
        assert_eq!(rel(st.empty_dirs.iter().collect()), ["vide"]); // seulement le plus haut
        assert_eq!(
            rel(st.temp_dirs.iter().map(|d| &d.path).collect()),
            [r"proj\__pycache__"]
        );
        assert_eq!(
            rel(st.temp_files.iter().map(|f| &f.path).collect()),
            [r"proj\build.tmp"]
        );
        assert_eq!(
            rel(st.installers.iter().map(|f| &f.path).collect()),
            [r"Downloads\tool.exe", r"apps\Setup_x64.exe", r"pkg\app.msi"]
        );
    }

    #[test]
    fn installer_names() {
        for (name, yes) in [
            ("Setup_x64.exe", true),
            ("tool-win.exe", true),
            ("tool_win.zip", true),
            ("winrar.exe", false),
            ("tool-windows.exe", false),
            ("portable.exe", false),
            ("AMD64-driver.exe", true),
        ] {
            assert_eq!(looks_like_installer(name), yes, "{name}");
        }
    }
}
