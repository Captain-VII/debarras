//! Aides partagées par les tests unitaires.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::time::{Duration, SystemTime};

use crate::scanner::{ScanOptions, scan};

/// Fichier de `size` octets (dossiers créés au besoin).
pub fn write(p: &Path, size: usize) {
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, vec![b'x'; size]).unwrap();
}

/// Date de modification et d'accès reculée de `days` jours (comme `os.utime(p, (t, t))`).
pub fn age(p: &Path, days: u64) {
    let t = SystemTime::now() - Duration::from_secs(days * 86_400);
    let f = fs::File::options().write(true).open(p).unwrap();
    f.set_times(fs::FileTimes::new().set_modified(t).set_accessed(t))
        .unwrap();
}

/// Scanne `root` (sans exclusion) dans `db` ; renvoie `db`.
pub fn scan_dir(root: &Path, db: &Path) -> PathBuf {
    let o = ScanOptions {
        excluded_paths: vec![],
        excluded_names: vec![],
        ignore_patterns: vec![],
    };
    scan(
        &root.to_string_lossy(),
        db,
        &o,
        &AtomicBool::new(false),
        |_, _, _| {},
    )
    .unwrap();
    db.to_path_buf()
}
