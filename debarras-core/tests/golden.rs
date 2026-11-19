//! Compare le cœur Rust aux sorties de référence de la version Python (`tests/golden/`,
//! produites par `tools/make_golden.py`) : même faux disque, monté sur une lettre libre.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::AtomicBool;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use debarras_core::scanner::{ScanOptions, scan};
use serde_json::{Value, json};

fn golden_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("tests")
        .join("golden")
}

fn golden(name: &str) -> Value {
    let text = fs::read_to_string(golden_dir().join(format!("{name}.json"))).unwrap();
    serde_json::from_str(&text).unwrap()
}

/// Faux disque extrait de `fixture.zip` et monté par `subst` ; démonté à la fin.
struct Fixture {
    letter: String,
    _dir: tempfile::TempDir,
}

impl Fixture {
    fn mount() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let disk = dir.path().join("disque");
        let recent: Vec<String> =
            serde_json::from_value(golden("fixture")["recent"].clone()).unwrap();
        // Python : 2020-01-01 12:00 heure locale. Seul l'écart avec « maintenant » compte
        // (fichiers anciens ou récents), donc 12:00 UTC convient partout.
        let old = UNIX_EPOCH + Duration::from_secs(1_577_880_000);
        let now = SystemTime::now() - Duration::from_secs(3600);
        let mut zip =
            zip::ZipArchive::new(fs::File::open(golden_dir().join("fixture.zip")).unwrap())
                .unwrap();
        for i in 0..zip.len() {
            let mut f = zip.by_index(i).unwrap();
            let rel = f.name().replace('/', "\\");
            let path = disk.join(&rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let mut out = fs::File::create(&path).unwrap();
            std::io::copy(&mut f, &mut out).unwrap();
            let t = if recent.contains(&rel) { now } else { old };
            out.set_times(fs::FileTimes::new().set_modified(t).set_accessed(t))
                .unwrap();
        }
        let letter = "XYWVUT"
            .chars()
            .map(|c| format!("{c}:"))
            .find(|l| !Path::new(&format!("{l}\\")).exists())
            .expect("aucune lettre libre");
        assert!(
            Command::new("subst")
                .arg(&letter)
                .arg(&disk)
                .status()
                .unwrap()
                .success()
        );
        Fixture { letter, _dir: dir }
    }

    /// Chemin sans la lettre, comme dans les JSON (`X:\Users` → `\Users`).
    fn rel(&self, path: &str) -> String {
        let rest = &path[self.letter.len()..];
        if rest.is_empty() {
            "\\".into()
        } else {
            rest.into()
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = Command::new("subst").arg(&self.letter).arg("/D").status();
    }
}

#[test]
fn scan_matches_python() {
    let fx = Fixture::mount();
    let db = fx._dir.path().join("cache.db");
    let root = format!("{}\\", fx.letter);
    let opts = ScanOptions {
        excluded_paths: vec![],
        excluded_names: vec![],
        ignore_patterns: vec![],
    };
    let r = scan(&root, &db, &opts, &AtomicBool::new(false), |_, _, _| {}).unwrap();

    let want = golden("scan");
    let got_result = json!({
        "cancelled": r.cancelled, "dir_count": r.dir_count, "errors": r.errors,
        "file_count": r.file_count, "root": fx.rel(&r.root), "scan_id": r.scan_id,
        "total_size": r.total_size,
    });
    assert_eq!(got_result, want["result"]);

    let conn = rusqlite::Connection::open(&db).unwrap();
    let rows = |sql: &str, cols: usize| -> Value {
        let mut st = conn.prepare(sql).unwrap();
        let mut out: Vec<Value> = st
            .query_map([], |row| {
                let path: String = row.get(0)?;
                let mut v = vec![json!(fx.rel(&path))];
                for i in 1..cols {
                    v.push(json!(row.get::<_, i64>(i)?));
                }
                Ok(Value::Array(v))
            })
            .unwrap()
            .map(Result::unwrap)
            .collect();
        out.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
        Value::Array(out)
    };
    assert_eq!(
        rows(
            &format!(
                "SELECT path, size, file_count FROM dir_sizes WHERE scan_id={}",
                r.scan_id
            ),
            3
        ),
        want["dirs"]
    );
    assert_eq!(rows("SELECT path, size FROM file_paths", 2), want["files"]);
}
