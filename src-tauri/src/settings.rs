//! Paramètres (`%LOCALAPPDATA%\Debarras\settings.json`), même fichier que la 1.6.x.

use std::fs;
use std::path::{Path, PathBuf};

use debarras_core::paths::norm;
use debarras_core::scanner::{ScanOptions, default_excluded_paths};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub excluded_paths: Vec<String>,
    pub excluded_names: Vec<String>,
    /// Ignorés au scan (motifs sur le nom).
    pub blacklist: Vec<String>,
    /// Protégés : jamais traités par les actions.
    pub whitelist: Vec<String>,
    /// `system`, `light` ou `dark`.
    pub theme: String,
    pub check_updates: bool,
    pub auto_install: bool,
    pub last_update_check: f64,
    pub skipped_version: String,
    pub advanced: bool,
    pub version: i64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            excluded_paths: default_excluded_paths(),
            excluded_names: vec![],
            blacklist: vec![],
            whitelist: vec![],
            theme: "system".into(),
            check_updates: true,
            auto_install: true,
            last_update_check: 0.0,
            skipped_version: String::new(),
            advanced: false,
            version: 2,
        }
    }
}

/// Exclusions par défaut jusqu'à la 1.5 (migration des paramètres).
fn old_default_excluded_paths() -> Vec<String> {
    let env = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.into());
    vec![
        env("SystemRoot", r"C:\Windows"),
        env("ProgramFiles", r"C:\Program Files"),
        env("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
}

impl Settings {
    pub fn path(db: &Path) -> PathBuf {
        db.with_file_name("settings.json")
    }

    pub fn load(path: &Path) -> Settings {
        let Ok(text) = fs::read_to_string(path) else {
            return Settings::default();
        };
        let text = text.trim_start_matches('\u{feff}');
        let raw: serde_json::Value = serde_json::from_str(text).unwrap_or_default();
        let mut s: Settings = serde_json::from_value(raw.clone()).unwrap_or_default();
        if raw.get("version").and_then(|v| v.as_i64()).unwrap_or(1) < 2 {
            // 1.5 -> 1.6 : anciennes exclusions par défaut retirées si elles n'avaient pas bougé.
            let sorted = |v: &[String]| {
                let mut v: Vec<String> = v.iter().map(|p| norm(p)).collect();
                v.sort();
                v
            };
            if sorted(&s.excluded_paths) == sorted(&old_default_excluded_paths()) {
                s.excluded_paths = default_excluded_paths();
            }
            if s.excluded_names == ["AppData"] {
                s.excluded_names.clear();
            }
            s.version = 2;
        }
        s
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, serde_json::to_string_pretty(self)?)?;
        fs::rename(tmp, path)
    }

    pub fn scan_options(&self) -> ScanOptions {
        ScanOptions {
            excluded_paths: self.excluded_paths.clone(),
            excluded_names: self.excluded_names.clone(),
            ignore_patterns: self.blacklist.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_and_migration() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("settings.json");
        assert_eq!(Settings::load(&p), Settings::default()); // absent
        let s = Settings {
            whitelist: vec![r"C:\Important".into()],
            theme: "dark".into(),
            ..Default::default()
        };
        s.save(&p).unwrap();
        assert_eq!(Settings::load(&p), s);
        // Fichier 1.5 : exclusions par défaut d'alors, champ inconnu ignoré.
        let old = serde_json::json!({
            "excluded_paths": old_default_excluded_paths(),
            "excluded_names": ["AppData"], "theme": "light", "inconnu": 1,
        });
        fs::write(&p, old.to_string()).unwrap();
        let m = Settings::load(&p);
        assert_eq!(m.excluded_paths, default_excluded_paths());
        assert!(m.excluded_names.is_empty());
        assert_eq!((m.theme.as_str(), m.version), ("light", 2));
        fs::write(&p, "pas du json").unwrap();
        assert_eq!(Settings::load(&p), Settings::default());
    }
}
