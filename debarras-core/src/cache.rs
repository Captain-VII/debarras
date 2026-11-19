//! Cache SQLite : état courant des fichiers + agrégats de dossiers par scan.
//!
//! Même base que la version Python (`%LOCALAPPDATA%\Debarras\cache.db`) : historique des
//! scans et empreintes xxHash sont repris tels quels. Schéma compact : chaque chemin de
//! dossier est stocké une fois (`dirpaths`) ; les vues `file_paths` et `dir_sizes`
//! reconstituent les chemins complets.
//!
//! Scan incrémental : un fichier dont (taille, mtime) n'a pas changé garde ses empreintes ;
//! sinon elles sont remises à NULL.
//!
//! Versions : v1 chemins complets répétés, v2 identifiants de dossiers, v3 `image_hash`,
//! v4 (Rust) `image_hash` remis à NULL : le dHash de Rust diffère un peu de celui de Pillow.
//! La version Python ouvre une base v4 sans souci (elle la remet en v3 : les empreintes
//! d'images seront alors recalculées une fois de plus, rien d'autre).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OptionalExtension, Result, params};

use crate::paths::{join, split};

pub const SCHEMA_VERSION: i64 = 4;

const SCHEMA: &str = r"
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    root        TEXT NOT NULL,
    started     REAL NOT NULL,
    finished    REAL,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | cancelled
    total_size  INTEGER DEFAULT 0,
    file_count  INTEGER DEFAULT 0,
    dir_count   INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0
);

-- Chemins de dossiers, une seule fois chacun.
CREATE TABLE IF NOT EXISTS dirpaths (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL UNIQUE
);

-- État le plus récent de chaque fichier connu (clé = dossier + nom, pas de rowid).
CREATE TABLE IF NOT EXISTS files (
    dir_id        INTEGER NOT NULL,
    name          TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    atime         REAL NOT NULL,
    ctime         REAL NOT NULL,
    partial_hash  TEXT,
    full_hash     TEXT,
    last_scan     INTEGER NOT NULL,
    image_hash    INTEGER,          -- empreinte visuelle dHash (images similaires)
    PRIMARY KEY (dir_id, name)
) WITHOUT ROWID;

-- Tailles cumulées des dossiers, conservées pour chaque scan (historique).
CREATE TABLE IF NOT EXISTS dirs (
    scan_id     INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    dir_id      INTEGER NOT NULL,
    parent_id   INTEGER,
    size        INTEGER NOT NULL,
    file_count  INTEGER NOT NULL,
    skipped     INTEGER NOT NULL DEFAULT 0,  -- sous-éléments exclus/inaccessibles (cumulé)
    PRIMARY KEY (scan_id, dir_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_dirs2_parent ON dirs(scan_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_dirs2_dir    ON dirs(dir_id);

-- Vues de lecture avec chemins complets.
CREATE VIEW IF NOT EXISTS file_paths AS
SELECT CASE WHEN substr(d.path, -1) = '\' THEN d.path || f.name
            ELSE d.path || '\' || f.name END AS path,
       d.path AS parent, f.name, f.ext, f.size, f.mtime, f.atime, f.ctime,
       f.partial_hash, f.full_hash, f.last_scan, f.dir_id, f.image_hash
FROM files f JOIN dirpaths d ON d.id = f.dir_id;

CREATE VIEW IF NOT EXISTS dir_sizes AS
SELECT s.scan_id, d.path, p.path AS parent, s.size, s.file_count, s.skipped,
       s.dir_id, s.parent_id
FROM dirs s JOIN dirpaths d ON d.id = s.dir_id
LEFT JOIN dirpaths p ON p.id = s.parent_id;
";

/// Réutilise les empreintes si taille et mtime sont identiques.
const UPSERT_FILE: &str = "
INSERT INTO files (dir_id, name, ext, size, mtime, atime, ctime, last_scan)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(dir_id, name) DO UPDATE SET
    ext = excluded.ext,
    atime = excluded.atime,
    ctime = excluded.ctime,
    last_scan = excluded.last_scan,
    partial_hash = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.partial_hash END,
    full_hash    = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.full_hash END,
    image_hash   = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                        THEN files.image_hash END,
    size = excluded.size,
    mtime = excluded.mtime
";

/// Tables v1 éventuellement manquantes avant la migration.
const V1_TABLES: &str = "
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, parent TEXT NOT NULL, name TEXT NOT NULL,
    ext TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, atime REAL NOT NULL,
    ctime REAL NOT NULL, partial_hash TEXT, full_hash TEXT, last_scan INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dirs (scan_id INTEGER NOT NULL, path TEXT NOT NULL, parent TEXT,
    size INTEGER NOT NULL, file_count INTEGER NOT NULL, PRIMARY KEY (scan_id, path));
";

/// v1 → actuel : chemins complets → identifiants de dossiers (`{schema}` inséré).
const MIGRATE_V1: &str = "
BEGIN;
DROP INDEX IF EXISTS idx_files_parent;
DROP INDEX IF EXISTS idx_files_size;
DROP INDEX IF EXISTS idx_dirs_parent;
ALTER TABLE files RENAME TO files_v1;
ALTER TABLE dirs RENAME TO dirs_v1;
{schema}
INSERT OR IGNORE INTO dirpaths (path)
    SELECT parent FROM files_v1
    UNION SELECT path FROM dirs_v1
    UNION SELECT parent FROM dirs_v1 WHERE parent IS NOT NULL;
INSERT OR IGNORE INTO files (dir_id, name, ext, size, mtime, atime, ctime,
                             partial_hash, full_hash, last_scan)
    SELECT d.id, f.name, f.ext, f.size, f.mtime, f.atime, f.ctime,
           f.partial_hash, f.full_hash, f.last_scan
    FROM files_v1 f JOIN dirpaths d ON d.path = f.parent;
INSERT OR IGNORE INTO dirs
    SELECT s.scan_id, d.id, p.id, s.size, s.file_count, s.skipped
    FROM dirs_v1 s JOIN dirpaths d ON d.path = s.path
    LEFT JOIN dirpaths p ON p.path = s.parent;
DROP TABLE files_v1;
DROP TABLE dirs_v1;
COMMIT;
";

/// Un fichier vu par le scan.
#[derive(Debug, Clone, PartialEq)]
pub struct FileRow {
    pub parent: String,
    pub name: String,
    pub ext: String,
    pub size: i64,
    pub mtime: f64,
    pub atime: f64,
    pub ctime: f64,
}

/// Agrégat d'un dossier pour un scan.
#[derive(Debug, Clone, PartialEq)]
pub struct DirRow {
    pub path: String,
    pub parent: Option<String>,
    pub size: i64,
    pub file_count: i64,
    pub skipped: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ScanInfo {
    pub id: i64,
    pub root: String,
    pub started: f64,
    pub finished: Option<f64>,
    pub status: String,
    pub total_size: i64,
    pub file_count: i64,
    pub dir_count: i64,
    pub errors: i64,
}

pub const APP_DIR: &str = "Debarras";
const OLD_APP_DIR: &str = "FileAnalyzer"; // nom des premières versions

/// `%LOCALAPPDATA%\Debarras\cache.db` (cache, journal et paramètres dans ce dossier).
/// Reprend le dossier `FileAnalyzer` des premières versions s'il existe seul.
pub fn default_db_path() -> std::io::Result<PathBuf> {
    let local = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
        .unwrap_or_default();
    let (mut base, old) = (local.join(APP_DIR), local.join(OLD_APP_DIR));
    if !base.exists() && old.is_dir() && std::fs::rename(&old, &base).is_err() {
        base = old; // dossier verrouillé : on continue avec l'ancien
    }
    std::fs::create_dir_all(&base)?;
    Ok(base.join("cache.db"))
}

/// Condition SQL « `column` est `root` ou un de ses sous-dossiers » (utilise l'index) :
/// `column = root OR (column >= root\ AND column < root])`.
pub fn subtree(column: &str, root: &str) -> (String, [String; 3]) {
    let lo = if root.ends_with('\\') {
        root.to_string()
    } else {
        format!("{root}\\")
    };
    let mut hi = lo.clone();
    let last = hi.pop().expect("lo finit par \\");
    hi.push(char::from_u32(last as u32 + 1).expect("caractère suivant valide"));
    (
        format!("({column} = ? OR ({column} >= ? AND {column} < ?))"),
        [root.to_string(), lo, hi],
    )
}

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// Accès SQLite. Une instance par thread (`Connection` n'est pas `Sync`).
pub struct Cache {
    pub conn: Connection,
    ids: HashMap<String, i64>,
}

impl Cache {
    pub fn open(db_path: &Path) -> Result<Self> {
        let conn = Connection::open(db_path)?;
        conn.busy_timeout(std::time::Duration::from_secs(30))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        let cache = Cache {
            conn,
            ids: HashMap::new(),
        };
        cache.migrate()?;
        cache.conn.execute_batch(SCHEMA)?;
        cache
            .conn
            .pragma_update(None, "user_version", SCHEMA_VERSION)?;
        Ok(cache)
    }

    fn columns(&self, table: &str) -> Result<Vec<String>> {
        let mut st = self.conn.prepare(&format!("PRAGMA table_info({table})"))?;
        st.query_map([], |r| r.get(1))?.collect()
    }

    /// Met à niveau un cache existant sans perdre fichiers, empreintes ni historique.
    fn migrate(&self) -> Result<()> {
        let version: i64 = self
            .conn
            .pragma_query_value(None, "user_version", |r| r.get(0))?;
        if version >= SCHEMA_VERSION {
            return Ok(());
        }
        if version == 2 {
            self.conn.execute_batch(
                "ALTER TABLE files ADD COLUMN image_hash INTEGER;
                 DROP VIEW IF EXISTS file_paths;", // recréée avec la colonne
            )?;
            return Ok(());
        }
        if version == 3 {
            return self
                .conn
                .execute("UPDATE files SET image_hash = NULL", [])
                .map(drop);
        }
        // v1 → actuel. Base neuve : rien à faire.
        let has_path = |t| self.columns(t).map(|c| c.iter().any(|x| x == "path"));
        if !has_path("files")? && !has_path("dirs")? {
            return Ok(());
        }
        self.conn.execute_batch(V1_TABLES)?;
        if !self.columns("dirs")?.iter().any(|c| c == "skipped") {
            self.conn.execute(
                "ALTER TABLE dirs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0",
                [],
            )?;
        }
        self.conn
            .execute_batch(&MIGRATE_V1.replace("{schema}", SCHEMA))?;
        self.conn.execute_batch("VACUUM") // rend au disque la place libérée
    }

    // --- identifiants de dossiers ---------------------------------------------------

    /// Identifiant du dossier `path`, créé si besoin.
    pub fn dir_id(&mut self, path: &str) -> Result<i64> {
        dir_id(&self.conn, &mut self.ids, path)
    }

    /// Identifiant du dossier `path` s'il est connu.
    pub fn find_dir_id(&self, path: &str) -> Result<Option<i64>> {
        if let Some(&i) = self.ids.get(path) {
            return Ok(Some(i));
        }
        self.conn
            .query_row("SELECT id FROM dirpaths WHERE path=?", [path], |r| r.get(0))
            .optional()
    }

    // --- cycle de vie d'un scan ---------------------------------------------------

    pub fn start_scan(&self, root: &str) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO scans (root, started) VALUES (?, ?)",
            params![root, now()],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn upsert_files(&mut self, scan_id: i64, rows: &[FileRow]) -> Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        for r in rows {
            let dir = dir_id(&tx, &mut self.ids, &r.parent)?;
            tx.prepare_cached(UPSERT_FILE)?.execute(params![
                dir, r.name, r.ext, r.size, r.mtime, r.atime, r.ctime, scan_id
            ])?;
        }
        tx.commit()
    }

    pub fn insert_dirs(&mut self, scan_id: i64, rows: &[DirRow]) -> Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        for r in rows {
            let dir = dir_id(&tx, &mut self.ids, &r.path)?;
            let parent = r
                .parent
                .as_deref()
                .map(|p| dir_id(&tx, &mut self.ids, p))
                .transpose()?;
            tx.prepare_cached(
                "INSERT OR REPLACE INTO dirs (scan_id, dir_id, parent_id, size, file_count, skipped) \
                 VALUES (?, ?, ?, ?, ?, ?)",
            )?
            .execute(params![scan_id, dir, parent, r.size, r.file_count, r.skipped])?;
        }
        tx.commit()
    }

    /// Clôt le scan et purge les fichiers disparus sous `root`.
    pub fn finish_scan(
        &mut self,
        scan_id: i64,
        root: &str,
        total_size: i64,
        file_count: i64,
        dir_count: i64,
        errors: i64,
    ) -> Result<()> {
        let (cond, [a, b, c]) = subtree("path", root);
        let tx = self.conn.unchecked_transaction()?;
        tx.execute(
            &format!("DELETE FROM files WHERE last_scan <> ? AND dir_id IN (SELECT id FROM dirpaths WHERE {cond})"),
            params![scan_id, a, b, c],
        )?;
        prune_dirpaths(&tx, &mut self.ids)?;
        tx.execute(
            "UPDATE scans SET finished=?, status='done', total_size=?, file_count=?, dir_count=?, errors=? WHERE id=?",
            params![now(), total_size, file_count, dir_count, errors, scan_id],
        )?;
        tx.commit()
    }

    /// Scan annulé : on garde les fichiers vus mais on jette les agrégats partiels.
    pub fn cancel_scan(&self, scan_id: i64) -> Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        tx.execute("DELETE FROM dirs WHERE scan_id=?", [scan_id])?;
        tx.execute(
            "UPDATE scans SET finished=?, status='cancelled' WHERE id=?",
            params![now(), scan_id],
        )?;
        tx.commit()
    }

    // --- lecture ------------------------------------------------------------------

    pub fn list_scans(&self, root: Option<&str>) -> Result<Vec<ScanInfo>> {
        let mut st = self.conn.prepare(
            "SELECT id, root, started, finished, status, total_size, file_count, dir_count, errors \
             FROM scans WHERE status='done' AND (?1 IS NULL OR root=?1) ORDER BY id DESC",
        )?;
        st.query_map([root], |r| {
            Ok(ScanInfo {
                id: r.get(0)?,
                root: r.get(1)?,
                started: r.get(2)?,
                finished: r.get(3)?,
                status: r.get(4)?,
                total_size: r.get(5)?,
                file_count: r.get(6)?,
                dir_count: r.get(7)?,
                errors: r.get(8)?,
            })
        })?
        .collect()
    }

    pub fn latest_scan(&self, root: &str) -> Result<Option<ScanInfo>> {
        Ok(self.list_scans(Some(root))?.into_iter().next())
    }

    /// Retire un scan de l'historique (données du cache uniquement, aucun fichier touché).
    pub fn delete_scan(&mut self, scan_id: i64) -> Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        tx.execute("DELETE FROM dirs WHERE scan_id=?", [scan_id])?;
        tx.execute("DELETE FROM scans WHERE id=?", [scan_id])?;
        prune_dirpaths(&tx, &mut self.ids)?;
        tx.commit()
    }

    /// Racines déjà scannées, la plus récente en premier.
    pub fn roots(&self) -> Result<Vec<String>> {
        let mut st = self.conn.prepare(
            "SELECT root FROM scans WHERE status='done' GROUP BY root ORDER BY MAX(id) DESC",
        )?;
        st.query_map([], |r| r.get(0))?.collect()
    }

    /// (taille, nb fichiers) cumulés d'un dossier.
    pub fn dir_info(&self, scan_id: i64, path: &str) -> Result<Option<(i64, i64)>> {
        let Some(i) = self.find_dir_id(path)? else {
            return Ok(None);
        };
        self.conn
            .query_row(
                "SELECT size, file_count FROM dirs WHERE scan_id=? AND dir_id=?",
                [scan_id, i],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
    }

    /// Taille connue d'un fichier ou d'un dossier (0 si inconnu).
    pub fn size_of(&self, scan_id: i64, path: &str) -> Result<i64> {
        let (parent, name) = split(path);
        if let Some(dir) = self.find_dir_id(parent)? {
            let size = self
                .conn
                .query_row(
                    "SELECT size FROM files WHERE dir_id=? AND name=?",
                    params![dir, name],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(size) = size {
                return Ok(size);
            }
        }
        Ok(self.dir_info(scan_id, path)?.map_or(0, |(size, _)| size))
    }

    /// Sous-dossiers directs : (chemin, taille, nb fichiers).
    pub fn child_dirs(&self, scan_id: i64, parent: &str) -> Result<Vec<(String, i64, i64)>> {
        let Some(i) = self.find_dir_id(parent)? else {
            return Ok(Vec::new());
        };
        // Index imposé : sans statistiques, SQLite parcourt tout le scan (x100 plus lent).
        let mut st = self.conn.prepare_cached(
            "SELECT d.path, s.size, s.file_count FROM dirs s INDEXED BY idx_dirs2_parent \
             JOIN dirpaths d ON d.id = s.dir_id WHERE s.scan_id=? AND s.parent_id=?",
        )?;
        st.query_map([scan_id, i], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
            .collect()
    }

    /// Fichiers directs : (chemin, nom, taille, mtime).
    pub fn child_files(&self, parent: &str) -> Result<Vec<(String, String, i64, f64)>> {
        let Some(i) = self.find_dir_id(parent)? else {
            return Ok(Vec::new());
        };
        let mut st = self
            .conn
            .prepare_cached("SELECT name, size, mtime FROM files WHERE dir_id=?")?;
        st.query_map([i], |r| {
            let name: String = r.get(0)?;
            Ok((join(parent, &name), name, r.get(1)?, r.get(2)?))
        })?
        .collect()
    }
}

impl Drop for Cache {
    fn drop(&mut self) {
        let _ = self.conn.execute_batch("PRAGMA optimize"); // statistiques du planificateur à jour
    }
}

/// Identifiant du dossier `path` (créé si besoin), mémorisé dans `ids`.
fn dir_id(conn: &Connection, ids: &mut HashMap<String, i64>, path: &str) -> Result<i64> {
    if let Some(&i) = ids.get(path) {
        return Ok(i);
    }
    let found = conn
        .query_row("SELECT id FROM dirpaths WHERE path=?", [path], |r| r.get(0))
        .optional()?;
    let i = match found {
        Some(i) => i,
        None => {
            conn.execute("INSERT INTO dirpaths (path) VALUES (?)", [path])?;
            conn.last_insert_rowid()
        }
    };
    ids.insert(path.to_string(), i);
    Ok(i)
}

/// Supprime les chemins que plus rien ne référence.
fn prune_dirpaths(conn: &Connection, ids: &mut HashMap<String, i64>) -> Result<()> {
    conn.execute(
        "DELETE FROM dirpaths WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.dir_id = dirpaths.id) \
         AND NOT EXISTS (SELECT 1 FROM dirs s WHERE s.dir_id = dirpaths.id)",
        [],
    )?;
    ids.clear();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(parent: &str, name: &str, size: i64, mtime: f64) -> FileRow {
        let ext = name
            .rfind('.')
            .map_or(String::new(), |i| name[i..].to_lowercase());
        FileRow {
            parent: parent.into(),
            name: name.into(),
            ext,
            size,
            mtime,
            atime: mtime,
            ctime: mtime,
        }
    }

    fn dir(path: &str, parent: Option<&str>, size: i64, count: i64) -> DirRow {
        DirRow {
            path: path.into(),
            parent: parent.map(Into::into),
            size,
            file_count: count,
            skipped: 0,
        }
    }

    fn hashes(c: &Cache, path: &str) -> (Option<String>, Option<String>, Option<i64>) {
        c.conn
            .query_row(
                "SELECT partial_hash, full_hash, image_hash FROM file_paths WHERE path=?",
                [path],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap()
    }

    fn scan(c: &mut Cache, root: &str, files: &[FileRow], dirs: &[DirRow]) -> i64 {
        let id = c.start_scan(root).unwrap();
        c.upsert_files(id, files).unwrap();
        c.insert_dirs(id, dirs).unwrap();
        let size = files.iter().map(|f| f.size).sum();
        c.finish_scan(id, root, size, files.len() as i64, dirs.len() as i64, 0)
            .unwrap();
        id
    }

    #[test]
    fn subtree_bounds() {
        let (sql, args) = subtree("path", r"C:\Data");
        assert_eq!(sql, "(path = ? OR (path >= ? AND path < ?))");
        assert_eq!(
            args,
            [
                r"C:\Data".to_string(),
                r"C:\Data\".into(),
                r"C:\Data]".into()
            ]
        );
        assert_eq!(
            subtree("p", r"C:\").1,
            [r"C:\".to_string(), r"C:\".into(), r"C:]".into()]
        );
    }

    #[test]
    fn scan_cycle_keeps_or_drops_hashes() {
        let tmp = tempfile::tempdir().unwrap();
        let mut c = Cache::open(&tmp.path().join("cache.db")).unwrap();
        let root = r"X:\Data";
        let files = [
            file(root, "a.jpg", 10, 1.0),
            file(r"X:\Data\sub", "b.txt", 20, 2.0),
            file(root, "c.bin", 30, 3.0),
        ];
        let dirs = [
            dir(root, None, 60, 3),
            dir(r"X:\Data\sub", Some(root), 20, 1),
        ];
        let first = scan(&mut c, root, &files, &dirs);
        c.conn
            .execute(
                "UPDATE files SET partial_hash='p', full_hash='f', image_hash=7",
                [],
            )
            .unwrap();

        // 2e scan : a inchangé, b modifié, c disparu.
        let files2 = [
            file(root, "a.jpg", 10, 1.0),
            file(r"X:\Data\sub", "b.txt", 21, 2.5),
        ];
        let second = scan(&mut c, root, &files2, &dirs);
        assert_eq!(
            hashes(&c, r"X:\Data\a.jpg"),
            (Some("p".into()), Some("f".into()), Some(7))
        );
        assert_eq!(hashes(&c, r"X:\Data\sub\b.txt"), (None, None, None));
        assert_eq!(c.size_of(second, r"X:\Data\c.bin").unwrap(), 0);

        assert_eq!(c.size_of(second, r"X:\Data\sub\b.txt").unwrap(), 21);
        assert_eq!(c.size_of(second, r"X:\Data\sub").unwrap(), 20);
        assert_eq!(c.dir_info(first, root).unwrap(), Some((60, 3)));
        assert_eq!(
            c.child_dirs(second, root).unwrap(),
            vec![(r"X:\Data\sub".into(), 20, 1)]
        );
        let mut kids = c.child_files(root).unwrap();
        kids.sort_by(|a, b| a.1.cmp(&b.1));
        assert_eq!(
            kids,
            vec![(r"X:\Data\a.jpg".into(), "a.jpg".into(), 10, 1.0)]
        );
        assert_eq!(
            c.list_scans(None)
                .unwrap()
                .iter()
                .map(|s| s.id)
                .collect::<Vec<_>>(),
            [second, first]
        );
        assert_eq!(c.latest_scan(root).unwrap().unwrap().total_size, 31);
        assert_eq!(c.roots().unwrap(), [root]);

        c.delete_scan(first).unwrap();
        assert_eq!(c.list_scans(None).unwrap().len(), 1);
        assert_eq!(c.dir_info(first, root).unwrap(), None);
    }

    #[test]
    fn cancelled_scan_keeps_files_drops_aggregates() {
        let tmp = tempfile::tempdir().unwrap();
        let mut c = Cache::open(&tmp.path().join("cache.db")).unwrap();
        let id = c.start_scan(r"X:\").unwrap();
        c.upsert_files(id, &[file(r"X:\", "a.txt", 5, 1.0)])
            .unwrap();
        c.insert_dirs(id, &[dir(r"X:\", None, 5, 1)]).unwrap();
        c.cancel_scan(id).unwrap();
        assert!(c.list_scans(None).unwrap().is_empty());
        assert_eq!(c.dir_info(id, r"X:\").unwrap(), None);
        assert_eq!(c.size_of(id, r"X:\a.txt").unwrap(), 5);
    }

    /// Une base laissée par la version Python (v3) : tout est repris, sauf les dHash.
    #[test]
    fn migrates_python_v3_and_v2() {
        for version in [2, 3] {
            let tmp = tempfile::tempdir().unwrap();
            let db = tmp.path().join("cache.db");
            {
                let conn = Connection::open(&db).unwrap();
                conn.execute_batch(SCHEMA).unwrap();
                conn.execute_batch(
                    r"INSERT INTO scans (root, started, status) VALUES ('X:\', 1, 'done');
                      INSERT INTO dirpaths (id, path) VALUES (1, 'X:\');
                      INSERT INTO files VALUES (1, 'a.png', '.png', 9, 1, 1, 1, 'p', 'f', 1, 42);
                      INSERT INTO dirs VALUES (1, 1, NULL, 9, 1, 0);",
                )
                .unwrap();
                if version == 2 {
                    conn.execute_batch(
                        "DROP VIEW file_paths; ALTER TABLE files DROP COLUMN image_hash;",
                    )
                    .unwrap();
                }
                conn.pragma_update(None, "user_version", version).unwrap();
            }
            let c = Cache::open(&db).unwrap();
            assert_eq!(
                c.conn
                    .pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
                    .unwrap(),
                4
            );
            assert_eq!(
                hashes(&c, r"X:\a.png"),
                (Some("p".into()), Some("f".into()), None),
                "v{version}"
            );
            assert_eq!(c.dir_info(1, r"X:\").unwrap(), Some((9, 1)));
        }
    }

    #[test]
    fn migrates_v1() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("cache.db");
        {
            let conn = Connection::open(&db).unwrap();
            conn.execute_batch(V1_TABLES).unwrap();
            conn.execute_batch(
                r"CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL,
                      started REAL NOT NULL, finished REAL, status TEXT NOT NULL DEFAULT 'running',
                      total_size INTEGER DEFAULT 0, file_count INTEGER DEFAULT 0,
                      dir_count INTEGER DEFAULT 0, errors INTEGER DEFAULT 0);
                  INSERT INTO scans (root, started, status) VALUES ('X:\', 1, 'done');
                  INSERT INTO files VALUES ('X:\d\a.txt', 'X:\d', 'a.txt', '.txt', 4, 1, 1, 1, 'p', NULL, 1);
                  INSERT INTO dirs VALUES (1, 'X:\', NULL, 4, 1), (1, 'X:\d', 'X:\', 4, 1);",
            )
            .unwrap();
        }
        let c = Cache::open(&db).unwrap();
        assert_eq!(hashes(&c, r"X:\d\a.txt"), (Some("p".into()), None, None));
        assert_eq!(
            c.child_dirs(1, r"X:\").unwrap(),
            vec![(r"X:\d".into(), 4, 1)]
        );
    }

    #[test]
    fn default_db_path_takes_over_old_folder() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir(tmp.path().join(OLD_APP_DIR)).unwrap();
        std::fs::write(tmp.path().join(OLD_APP_DIR).join("cache.db"), b"x").unwrap();
        // SAFETY : seul test du crate à toucher LOCALAPPDATA.
        unsafe { std::env::set_var("LOCALAPPDATA", tmp.path()) };
        let p = default_db_path().unwrap();
        assert_eq!(p, tmp.path().join(APP_DIR).join("cache.db"));
        assert!(p.exists() && !tmp.path().join(OLD_APP_DIR).exists());
    }
}
