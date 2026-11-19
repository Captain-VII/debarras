//! Comparaison de deux scans d'un même dossier : ce qui a grossi, diminué, apparu, disparu.
//! Se base sur les tailles de dossiers conservées pour chaque scan (table `dirs`).
//! Port de `core/history.py`.

use std::collections::{BTreeSet, HashMap};

use rusqlite::{Connection, Result};

use crate::cache::ScanInfo;

/// Un dossier est « précis » si aucun enfant ne porte au moins 80 % de sa variation.
const FOCUS_RATIO: f64 = 0.8;
const FOCUS_MIN_BYTES: i64 = 1 << 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Change {
    New,
    Removed,
    Changed,
}

impl Change {
    pub fn as_str(self) -> &'static str {
        match self {
            Change::New => "nouveau",
            Change::Removed => "supprimé",
            Change::Changed => "modifié",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DirDelta {
    pub path: String,
    pub parent: Option<String>,
    pub old_size: i64,
    pub new_size: i64,
    pub old_count: i64,
    pub new_count: i64,
    pub status: Change,
}

impl DirDelta {
    pub fn delta(&self) -> i64 {
        self.new_size - self.old_size
    }

    pub fn count_delta(&self) -> i64 {
        self.new_count - self.old_count
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct HistoryDiff {
    pub old: ScanInfo,
    pub new: ScanInfo,
    /// Dossiers qui ont changé.
    pub deltas: HashMap<String, DirDelta>,
    /// Parent → enfants changés, la plus grande variation d'abord.
    pub children: HashMap<String, Vec<String>>,
    /// Vue à plat : dossiers où la variation se concentre, la plus grande d'abord.
    pub focus: Vec<String>,
}

impl HistoryDiff {
    pub fn total_delta(&self) -> i64 {
        self.new.total_size - self.old.total_size
    }

    /// Les `n` plus fortes hausses (`grown`) ou baisses de la vue à plat.
    pub fn top(&self, grown: bool, n: usize) -> Vec<&DirDelta> {
        self.focus
            .iter()
            .map(|p| &self.deltas[p])
            .filter(|d| (d.delta() > 0) == grown)
            .take(n)
            .collect()
    }
}

/// Dossier → (parent, taille, nombre de fichiers) pour un scan.
type DirSizes = HashMap<String, (Option<String>, i64, i64)>;

fn load(conn: &Connection, scan_id: i64) -> Result<DirSizes> {
    conn.prepare("SELECT path, parent, size, file_count FROM dir_sizes WHERE scan_id=?")?
        .query_map([scan_id], |r| {
            Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?)))
        })?
        .collect()
}

pub fn compare(conn: &Connection, old: &ScanInfo, new: &ScanInfo) -> Result<HistoryDiff> {
    let (a, b) = (load(conn, old.id)?, load(conn, new.id)?);
    let mut deltas: HashMap<String, DirDelta> = HashMap::new();
    let mut children: HashMap<String, Vec<String>> = HashMap::new();
    let paths: BTreeSet<&String> = a.keys().chain(b.keys()).collect();
    for path in paths {
        let d = match (a.get(path), b.get(path)) {
            (Some(pa), Some(pb)) if pa.1 == pb.1 && pa.2 == pb.2 => continue,
            (Some(pa), Some(pb)) => (pb.0.clone(), pa.1, pb.1, pa.2, pb.2, Change::Changed),
            (None, Some(pb)) => (pb.0.clone(), 0, pb.1, 0, pb.2, Change::New),
            (Some(pa), None) => (pa.0.clone(), pa.1, 0, pa.2, 0, Change::Removed),
            (None, None) => unreachable!(),
        };
        let d = DirDelta {
            path: path.clone(),
            parent: d.0,
            old_size: d.1,
            new_size: d.2,
            old_count: d.3,
            new_count: d.4,
            status: d.5,
        };
        if let Some(p) = &d.parent {
            children.entry(p.clone()).or_default().push(path.clone());
        }
        deltas.insert(path.clone(), d);
    }
    // À variation égale, par chemin (Python : ordre arbitraire).
    let by_size = |x: &String, y: &String| {
        deltas[y]
            .delta()
            .abs()
            .cmp(&deltas[x].delta().abs())
            .then_with(|| x.cmp(y))
    };
    for kids in children.values_mut() {
        kids.sort_by(by_size);
    }
    // Vue à plat : dossiers où la variation se concentre (pas expliquée par un seul enfant).
    let mut focus: Vec<String> = deltas
        .values()
        .filter(|d| d.delta().abs() >= FOCUS_MIN_BYTES)
        .filter(|d| {
            let biggest = children
                .get(&d.path)
                .into_iter()
                .flatten()
                .map(|k| deltas[k].delta())
                .filter(|k| (*k > 0) == (d.delta() > 0))
                .max_by_key(|k| k.abs())
                .unwrap_or(0);
            (biggest.abs() as f64) < FOCUS_RATIO * d.delta().abs() as f64
        })
        .map(|d| d.path.clone())
        .collect();
    focus.sort_by(by_size);
    Ok(HistoryDiff {
        old: old.clone(),
        new: new.clone(),
        deltas,
        children,
        focus,
    })
}

/// Chemin relatif à la racine du scan, pour des libellés courts.
pub fn display_name<'a>(path: &'a str, root: &str) -> &'a str {
    if path.eq_ignore_ascii_case(root) {
        return path;
    }
    let root = root.trim_end_matches('\\');
    match path.get(..root.len()) {
        Some(head) if head.eq_ignore_ascii_case(root) && path[root.len()..].starts_with('\\') => {
            &path[root.len() + 1..]
        }
        _ => path,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::Cache;
    use crate::testutil::{scan_dir, write};

    #[test]
    fn compares_scans() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        const MB: usize = 1024 * 1024;
        write(&root.join(r"a\deep\x.bin"), 1000);
        write(&root.join(r"a\other.bin"), 500);
        write(&root.join(r"b\y.bin"), 3000);
        write(&root.join(r"b\z.bin"), 2 * MB);
        write(&root.join(r"d\gone.bin"), 1500);
        let db = scan_dir(&root, &tmp.path().join("c.db"));
        write(&root.join(r"a\deep\big.bin"), 3 * MB);
        std::fs::remove_file(root.join(r"b\z.bin")).unwrap();
        write(&root.join(r"c\new.bin"), 800);
        std::fs::remove_dir_all(root.join("d")).unwrap();
        scan_dir(&root, &db);

        let c = Cache::open(&db).unwrap();
        let r = root.to_string_lossy().into_owned();
        let scans = c.list_scans(Some(&r)).unwrap(); // le plus récent d'abord
        let diff = compare(&c.conn, &scans[1], &scans[0]).unwrap();
        let mut status: Vec<(String, &str)> = diff
            .deltas
            .values()
            .map(|d| (display_name(&d.path, &r).to_string(), d.status.as_str()))
            .collect();
        status.sort();
        let want = [
            ("a", "modifié"),
            (r"a\deep", "modifié"),
            ("b", "modifié"),
            ("c", "nouveau"),
            ("d", "supprimé"),
        ];
        let mut want: Vec<(String, &str)> = want.iter().map(|(p, s)| (p.to_string(), *s)).collect();
        want.insert(0, (r.clone(), "modifié"));
        want.sort();
        assert_eq!(status, want);
        assert_eq!(diff.deltas[&format!(r"{r}\a\deep")].delta(), 3 * MB as i64);
        assert_eq!(diff.deltas[&format!(r"{r}\d")].delta(), -1500);
        assert_eq!(
            diff.total_delta(),
            3 * MB as i64 - 2 * MB as i64 + 800 - 1500
        );
        // Vue « où se concentre la variation » : a\deep et b, pas leurs parents.
        let mut focus: Vec<&str> = diff.focus.iter().map(|p| display_name(p, &r)).collect();
        focus.sort();
        assert_eq!(focus, [r"a\deep", "b"]);
        let top: Vec<&str> = diff
            .top(true, 10)
            .iter()
            .map(|d| display_name(&d.path, &r))
            .collect();
        assert_eq!(top, [r"a\deep"]);
    }
}
