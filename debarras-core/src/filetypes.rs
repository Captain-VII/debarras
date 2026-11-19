//! Catégories de fichiers par extension (ordre fixe), comme `utils/filetypes.py`.
//! Les couleurs sont à l'interface.

const EXTENSIONS: [(&str, &[&str]); 7] = [
    (
        "Images",
        &[
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".raw",
            ".cr2", ".nef", ".arw", ".dng", ".svg", ".ico", ".psd", ".xcf",
        ],
    ),
    (
        "Vidéos",
        &[
            ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
            ".ts", ".3gp",
        ],
    ),
    (
        "Audio",
        &[
            ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".aiff",
        ],
    ),
    (
        "Documents",
        &[
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
            ".txt", ".rtf", ".md", ".csv", ".epub", ".mobi",
        ],
    ),
    (
        "Archives",
        &[
            ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".iso", ".img", ".vhd",
            ".vhdx", ".cab",
        ],
    ),
    (
        "Code",
        &[
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go",
            ".rs", ".rb", ".php", ".html", ".css", ".json", ".xml", ".yml", ".yaml", ".toml",
            ".sql", ".sh", ".ps1", ".bat", ".ipynb", ".pyc", ".class", ".o", ".obj",
        ],
    ),
    (
        "Exécutables",
        &[
            ".exe", ".msi", ".dll", ".sys", ".appx", ".msix", ".jar", ".apk", ".bin", ".so",
            ".dylib",
        ],
    ),
];

pub const OTHER: &str = "Autres";
pub const CATEGORIES: [&str; 8] = [
    "Images",
    "Vidéos",
    "Audio",
    "Documents",
    "Archives",
    "Code",
    "Exécutables",
    OTHER,
];

/// Extensions d'une catégorie ; pour « Autres », toutes les extensions connues (à exclure).
/// Pour « Autres », `.ts` (vidéo et code) figure deux fois, contre une en Python : sans effet
/// dans un `IN (…)` SQL.
pub fn extensions(cat: &str) -> Vec<&'static str> {
    EXTENSIONS
        .iter()
        .filter(|(c, _)| cat == OTHER || *c == cat)
        .flat_map(|(_, exts)| exts.iter().copied())
        .collect()
}

/// `.JPG` → `Images`. La dernière catégorie qui cite l'extension gagne, comme le dict Python.
pub fn category(ext: &str) -> &'static str {
    let ext = ext.to_lowercase();
    EXTENSIONS
        .iter()
        .rev()
        .find(|(_, exts)| exts.contains(&ext.as_str()))
        .map_or(OTHER, |(c, _)| c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn categories() {
        assert_eq!(category(".JPG"), "Images");
        assert_eq!(category(".inconnue"), OTHER);
        assert_eq!(category(""), OTHER);
        // `.ts` est à la fois vidéo et code : le dict Python garde la dernière (Code).
        assert_eq!(category(".ts"), "Code");
        assert_eq!(extensions("Audio").len(), 9);
        assert!(extensions(OTHER).contains(&".dll"));
        assert!(extensions("Inexistante").is_empty());
        assert_eq!(CATEGORIES.len(), EXTENSIONS.len() + 1);
    }
}
