//! Chemins Windows : forme normalisée pour les comparaisons et noms longs.

/// Comme `_n` de `safety.py` : `os.path.normcase(os.path.normpath(p)).rstrip("\\")`.
/// Minuscules, `\` partout, `.`/`..`/séparateurs doublés résolus, sans `\` final.
pub fn norm(path: &str) -> String {
    let p = path.replace('/', "\\");
    // Préfixe : lecteur (`C:`) ou partage UNC (`\\serveur\partage`).
    let (prefix, rest) = if let Some(unc) = p.strip_prefix("\\\\") {
        let mut it = unc.splitn(3, '\\');
        let server = it.next().unwrap_or("");
        let share = it.next().unwrap_or("");
        let cut = 2 + server.len() + if share.is_empty() { 0 } else { 1 + share.len() };
        p.split_at(cut.min(p.len()))
    } else if p.len() >= 2 && p.as_bytes()[1] == b':' {
        p.split_at(2)
    } else {
        ("", p.as_str())
    };
    let rooted = rest.starts_with('\\');
    let mut parts: Vec<&str> = Vec::new();
    for part in rest.split('\\') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|l| *l != "..") => {
                parts.pop();
            }
            ".." if rooted => {}
            _ => parts.push(part),
        }
    }
    let mut out = String::from(prefix);
    if rooted {
        out.push('\\');
    }
    out.push_str(&parts.join("\\"));
    if out.is_empty() {
        out.push('.');
    }
    out.to_lowercase().trim_end_matches('\\').to_string()
}

/// Nom long d'un chemin (`C:\Users\RUNNER~1` → `C:\Users\runneradmin`), sans suivre les liens.
/// Les variables d'environnement peuvent contenir des noms courts 8.3 alors que le scan
/// produit des noms longs. Chemin inchangé si Windows ne le connaît pas.
#[cfg(windows)]
pub fn long_path(path: &str) -> String {
    use windows_sys::Win32::Storage::FileSystem::GetLongPathNameW;
    let wide: Vec<u16> = path.encode_utf16().chain([0]).collect();
    let mut buf = vec![0u16; 32768];
    // SAFETY : `wide` est terminé par un zéro, `buf` fait bien la taille annoncée.
    let n = unsafe { GetLongPathNameW(wide.as_ptr(), buf.as_mut_ptr(), buf.len() as u32) } as usize;
    if 0 < n && n < buf.len() {
        String::from_utf16_lossy(&buf[..n])
    } else {
        path.to_string()
    }
}

#[cfg(not(windows))]
pub fn long_path(path: &str) -> String {
    path.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn norm_like_python() {
        // Valeurs relevées avec ntpath.normcase(ntpath.normpath(p)).rstrip("\\").
        for (p, want) in [
            (r"C:\Windows\", r"c:\windows"),
            (r"C:\", "c:"),
            ("C:/Users//Alice/./Docs/../Images", r"c:\users\alice\images"),
            (r"\\Serveur\Partage\Dossier\", r"\\serveur\partage\dossier"),
            (r"C:\..\Windows", r"c:\windows"),
            (r"..\a\..\..\b", r"..\..\b"),
            ("", "."),
            (r"D:\Été\Photo.JPG", r"d:\été\photo.jpg"),
        ] {
            assert_eq!(norm(p), want, "{p}");
        }
    }

    #[test]
    fn long_path_keeps_unknown_and_resolves_existing() {
        assert_eq!(long_path(r"Z:\n'existe\pas"), r"Z:\n'existe\pas");
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().to_str().unwrap();
        assert_eq!(norm(&long_path(p)), norm(&long_path(&long_path(p))));
    }
}
