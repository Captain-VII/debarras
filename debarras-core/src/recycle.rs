//! Corbeille Windows : envoi, capacité et restauration. Port de `core/recycle.py`, de
//! `send2trash` (variante `SHFileOperationW`, celle qu'utilise la 1.6.x) et de la lecture
//! des fichiers `$I` de `core/actions.py`.
//!
//! Jamais de suppression définitive : envoyer à la corbeille plus que sa place libre fait
//! effacer par Windows les éléments les plus anciens, un élément plus gros que sa capacité
//! est effacé directement, et sur un lecteur réglé « ne pas utiliser la corbeille », tout
//! l'est. `capacity_problems` détecte ces cas avant tout envoi.

use std::io;
use std::path::Path;

/// Taille lisible, comme `utils.format.human_size` : `1536` → `1,5 Ko`.
pub fn human_size(size: i64) -> String {
    const UNITS: [&str; 6] = ["o", "Ko", "Mo", "Go", "To", "Po"];
    let mut s = size as f64;
    for (i, unit) in UNITS.iter().enumerate() {
        if s.abs() < 1024.0 || i == UNITS.len() - 1 {
            return if i == 0 {
                format!("{} {unit}", s as i64)
            } else {
                format!("{s:.1} {unit}").replace('.', ",")
            };
        }
        s /= 1024.0;
    }
    unreachable!()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BinInfo {
    pub drive: String,
    /// Octets ; `None` si inconnue.
    pub capacity: Option<i64>,
    pub used: i64,
    /// « Supprimer immédiatement sans utiliser la corbeille ».
    pub disabled: bool,
}

impl BinInfo {
    pub fn free(&self) -> Option<i64> {
        self.capacity.map(|c| (c - self.used).max(0))
    }
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain([0]).collect()
}

/// (octets, éléments) dans la corbeille de `drive` (`C:\`), ou de tous les lecteurs.
#[cfg(windows)]
pub fn usage(drive: Option<&str>) -> (i64, i64) {
    use windows_sys::Win32::UI::Shell::{SHQUERYRBINFO, SHQueryRecycleBinW};
    let w = drive.map(wide);
    // SAFETY : structure C de la taille annoncée ; chaîne terminée par un zéro ou nulle.
    unsafe {
        let mut info: SHQUERYRBINFO = std::mem::zeroed();
        info.cbSize = std::mem::size_of::<SHQUERYRBINFO>() as u32;
        let p = w.as_ref().map_or(std::ptr::null(), |w| w.as_ptr());
        if SHQueryRecycleBinW(p, &mut info) == 0 {
            (info.i64Size, info.i64NumItems)
        } else {
            (0, 0)
        }
    }
}

/// `C:\` → `{d8a4cfe7-…}` (identifiant de volume des réglages de la corbeille).
#[cfg(windows)]
fn volume_guid(drive: &str) -> Option<String> {
    use windows_sys::Win32::Storage::FileSystem::GetVolumeNameForVolumeMountPointW;
    let mut buf = [0u16; 64];
    // SAFETY : chemin terminé par un zéro, tampon de la taille annoncée.
    let ok = unsafe {
        GetVolumeNameForVolumeMountPointW(wide(drive).as_ptr(), buf.as_mut_ptr(), buf.len() as u32)
    };
    if ok == 0 {
        return None;
    }
    let name =
        String::from_utf16_lossy(&buf[..buf.iter().position(|&c| c == 0).unwrap_or(buf.len())]);
    let (start, end) = (name.find('{')?, name.find('}')?);
    (end > start).then(|| name[start..=end].to_lowercase())
}

/// Valeur DWORD de `HKCU\…\BitBucket\Volume\{guid}` (recherche de sous-clé sans casse).
#[cfg(windows)]
fn bitbucket_dword(guid: &str, value: &str) -> Option<u32> {
    use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, RRF_RT_REG_DWORD, RegGetValueW};
    let key = wide(&format!(
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
    ));
    let mut data = 0u32;
    let mut len = 4u32;
    // SAFETY : chaînes terminées par un zéro ; `data` fait les 4 octets annoncés.
    let rc = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            wide(value).as_ptr(),
            RRF_RT_REG_DWORD,
            std::ptr::null_mut(),
            (&mut data as *mut u32).cast(),
            &mut len,
        )
    };
    (rc == 0).then_some(data)
}

/// Réglages et remplissage de la corbeille du lecteur de `drive`.
#[cfg(windows)]
pub fn bin_info(drive: &str) -> BinInfo {
    let drive = format!("{}\\", drive_of(drive).to_uppercase());
    let (mut capacity, mut disabled) = (None, false);
    if let Some(guid) = volume_guid(&drive) {
        capacity = bitbucket_dword(&guid, "MaxCapacity").map(|mb| i64::from(mb) * 1024 * 1024);
        disabled = bitbucket_dword(&guid, "NukeOnDelete").is_some_and(|v| v != 0);
    }
    let used = usage(Some(&drive)).0;
    BinInfo {
        drive,
        capacity,
        used,
        disabled,
    }
}

/// `C:` d'un chemin absolu, `""` pour un chemin sans lecteur, `\\serveur\partage` pour un partage.
fn drive_of(path: &str) -> &str {
    if path.len() >= 2 && path.as_bytes()[1] == b':' {
        &path[..2]
    } else if path.starts_with("\\\\") {
        path
    } else {
        ""
    }
}

/// Raisons de ne pas envoyer `paths` à la corbeille (vide si tout tient). `sizes` : tailles
/// connues par chemin ; `info` : réglages d'un lecteur (`bin_info`, ou un faux en test).
pub fn capacity_problems(
    paths: &[String],
    sizes: &std::collections::HashMap<String, i64>,
    info: &dyn Fn(&str) -> BinInfo,
) -> Vec<String> {
    let key = |p: &str| crate::paths::norm(&absolute(p));
    let sizes: std::collections::HashMap<String, i64> =
        sizes.iter().map(|(k, v)| (key(k), *v)).collect();
    let mut by_drive: Vec<(String, Vec<i64>)> = Vec::new();
    for p in paths {
        let abs = absolute(p);
        let drive = drive_of(&abs).to_uppercase();
        if drive.is_empty() || drive.starts_with("\\\\") {
            continue; // partage réseau : pas de corbeille, Windows refusera
        }
        let size = sizes.get(&key(p)).copied().unwrap_or(0);
        match by_drive.iter_mut().find(|(d, _)| *d == drive) {
            Some((_, v)) => v.push(size),
            None => by_drive.push((drive, vec![size])),
        }
    }
    let h = |n: i64| human_size(n);
    let mut out = Vec::new();
    for (drive, items) in by_drive {
        let b = info(&format!("{drive}\\"));
        let total: i64 = items.iter().sum();
        let biggest = items.iter().copied().max().unwrap_or(0);
        if b.disabled {
            out.push(format!(
                "{drive} : la corbeille est désactivée sur ce lecteur, les éléments seraient effacés \
                 définitivement. Réactivez-la (Propriétés de la Corbeille) ou déplacez-les."
            ));
        } else if let Some(cap) = b.capacity.filter(|&c| biggest > c) {
            out.push(format!(
                "{drive} : un élément ({}) dépasse la capacité de la corbeille ({}). Sélectionnez son \
                 contenu plutôt que le dossier entier.",
                h(biggest),
                h(cap)
            ));
        } else if let Some(free) = b.free().filter(|&f| total > f) {
            out.push(format!(
                "{drive} : la sélection ({}) dépasse la place libre dans la corbeille ({} sur {}). Windows \
                 effacerait définitivement les éléments les plus anciens de la corbeille : videz-la d'abord.",
                h(total),
                h(free),
                h(b.capacity.unwrap_or(0))
            ));
        }
    }
    out
}

/// `os.path.abspath`.
pub fn absolute(p: &str) -> String {
    std::path::absolute(p).map_or_else(|_| p.to_string(), |a| a.to_string_lossy().into_owned())
}

/// Envoie un fichier ou dossier à la corbeille (`send2trash`, variante `SHFileOperationW`).
/// Le chemin est passé tel quel, pour que la corbeille note le vrai nom (l'annulation le
/// retrouve) ; au-delà de la limite de 260 caractères, sous sa forme courte 8.3.
#[cfg(windows)]
pub fn send_to_trash(path: &str) -> io::Result<()> {
    use windows_sys::Win32::UI::Shell::{
        FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_NOERRORUI, FOF_SILENT, SHFILEOPSTRUCTW,
        SHFileOperationW,
    };
    let full = absolute(path);
    if std::fs::symlink_metadata(&full).is_err() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("introuvable : {full}"),
        ));
    }
    let target = if full.encode_utf16().count() >= 260 {
        short_path(&full)?
    } else {
        full
    };
    let from: Vec<u16> = target.encode_utf16().chain([0, 0]).collect(); // liste terminée par deux zéros
    // SAFETY : `from` est doublement terminé par zéro et vit pendant l'appel ; aucun pointeur
    // de sortie n'est demandé (pTo nul, pas de correspondances de noms).
    let (rc, aborted) = unsafe {
        let mut op: SHFILEOPSTRUCTW = std::mem::zeroed();
        op.wFunc = FO_DELETE;
        op.pFrom = from.as_ptr();
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT) as _;
        let rc = SHFileOperationW(&mut op);
        (rc, op.fAnyOperationsAborted)
    };
    if rc != 0 {
        return Err(io::Error::other(format!(
            "la corbeille a refusé l'élément (code {rc:#x})"
        )));
    }
    if aborted != 0 {
        return Err(io::Error::other("opération interrompue par Windows"));
    }
    Ok(())
}

/// Forme courte 8.3 d'un chemin long (préfixe `\\?\` pour dépasser 260 caractères).
#[cfg(windows)]
fn short_path(path: &str) -> io::Result<String> {
    use windows_sys::Win32::Storage::FileSystem::GetShortPathNameW;
    let (prefix, unprefixed) = if let Some(unc) = path.strip_prefix(r"\\") {
        (r"\\?\UNC\", unc)
    } else {
        (r"\\?\", path)
    };
    let long = wide(&format!("{prefix}{unprefixed}"));
    let mut buf = vec![0u16; 32768];
    // SAFETY : chemin terminé par un zéro, tampon de la taille annoncée.
    let n =
        unsafe { GetShortPathNameW(long.as_ptr(), buf.as_mut_ptr(), buf.len() as u32) } as usize;
    if n == 0 || n >= buf.len() {
        return Err(io::Error::last_os_error());
    }
    let s = String::from_utf16_lossy(&buf[..n]);
    Ok(match s.strip_prefix(r"\\?\UNC\") {
        Some(rest) => format!(r"\\{rest}"),
        None => s.strip_prefix(r"\\?\").unwrap_or(&s).to_string(),
    })
}

/// Élément de la corbeille : (date de suppression, chemin d'origine, fichier `$I`, fichier `$R`).
pub struct Entry {
    pub deleted: f64,
    pub original: String,
    pub info: std::path::PathBuf,
    pub data: std::path::PathBuf,
}

/// Éléments de la corbeille du lecteur `drive` (`C:`), lus dans les fichiers `$I`.
pub fn entries(drive: &str) -> Vec<Entry> {
    let mut out = Vec::new();
    let Ok(sids) = std::fs::read_dir(format!("{drive}\\$Recycle.Bin")) else {
        return out;
    };
    for sid in sids
        .flatten()
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
    {
        let Ok(items) = std::fs::read_dir(sid.path()) else {
            continue;
        }; // corbeille d'un autre compte
        for item in items.flatten() {
            let name = item.file_name().to_string_lossy().into_owned();
            if !name.to_uppercase().starts_with("$I") {
                continue;
            }
            let Ok(raw) = std::fs::read(item.path()) else {
                continue;
            };
            let Some((deleted, original)) = parse_info(&raw) else {
                continue;
            };
            out.push(Entry {
                deleted,
                original,
                info: item.path(),
                data: sid.path().join(format!("$R{}", &name[2..])),
            });
        }
    }
    out
}

/// Contenu d'un fichier `$I` : (date de suppression, chemin d'origine).
fn parse_info(raw: &[u8]) -> Option<(f64, String)> {
    let num = |at: usize| {
        raw.get(at..at + 8)
            .map(|b| i64::from_le_bytes(b.try_into().unwrap()))
    };
    let (version, filetime) = (num(0)?, num(16)?);
    let utf16 = |b: &[u8]| -> Option<String> {
        let units: Vec<u16> = b
            .as_chunks::<2>()
            .0
            .iter()
            .map(|c| u16::from_le_bytes(*c))
            .collect();
        String::from_utf16(&units).ok()
    };
    let original = if version == 2 {
        let n = i32::from_le_bytes(raw.get(24..28)?.try_into().ok()?) as usize;
        utf16(raw.get(28..28 + 2 * n)?)?
    } else {
        utf16(raw.get(24..(24 + 520).min(raw.len()))?)?
    };
    let original = original.split('\0').next().unwrap_or("").to_string();
    Some((filetime as f64 / 1e7 - 11_644_473_600.0, original))
}

/// Remet `original` à sa place depuis la corbeille (élément supprimé après `since`).
/// Ne remplace jamais un élément présent.
pub fn restore(original: &str, since: f64) -> io::Result<()> {
    let key = crate::paths::norm(original);
    let best = entries(drive_of(original))
        .into_iter()
        .filter(|e| crate::paths::norm(&e.original) == key && e.deleted >= since)
        .max_by(|a, b| a.deleted.total_cmp(&b.deleted))
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "introuvable dans la corbeille (vidée ?)",
            )
        })?;
    if std::fs::symlink_metadata(original).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "un élément du même nom existe déjà à l'emplacement d'origine",
        ));
    }
    if let Some(parent) = Path::new(original).parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::rename(&best.data, original)?;
    std::fs::remove_file(&best.info) // métadonnées de corbeille de l'élément restauré
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn human_size_like_python() {
        assert_eq!(human_size(0), "0 o");
        assert_eq!(human_size(1023), "1023 o");
        assert_eq!(human_size(1536), "1,5 Ko");
        assert_eq!(human_size(5 * 1024 * 1024 * 1024), "5,0 Go");
    }

    #[test]
    fn capacity_guard() {
        let fake = |capacity: Option<i64>, used: i64, disabled: bool| {
            move |d: &str| BinInfo {
                drive: d.to_string(),
                capacity,
                used,
                disabled,
            }
        };
        let (a, b) = (r"C:\x\a".to_string(), r"C:\x\b".to_string());
        let sizes = HashMap::from([(a.clone(), 400), (b.clone(), 400)]);
        let both = [a.clone(), b.clone()];
        assert!(capacity_problems(&both, &sizes, &fake(Some(1000), 0, false)).is_empty());
        assert!(
            capacity_problems(&both, &sizes, &fake(Some(1000), 300, false))[0]
                .contains("place libre")
        );
        let big = HashMap::from([(a.clone(), 2000)]);
        assert!(
            capacity_problems(std::slice::from_ref(&a), &big, &fake(Some(1000), 0, false))[0]
                .contains("dépasse la capacité")
        );
        assert!(
            capacity_problems(std::slice::from_ref(&a), &sizes, &fake(Some(1000), 0, true))[0]
                .contains("désactivée")
        );
        assert!(
            capacity_problems(std::slice::from_ref(&a), &sizes, &fake(None, 0, false)).is_empty()
        ); // inconnue
        let unc = [r"\\serveur\partage\x".to_string()];
        assert!(capacity_problems(&unc, &sizes, &fake(Some(0), 0, true)).is_empty()); // pas de corbeille
    }

    #[cfg(windows)]
    #[test]
    fn bin_info_reads_real_drive() {
        let drive = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".into());
        let b = bin_info(&format!("{drive}\\"));
        assert!(b.used >= 0 && b.capacity.is_none_or(|c| c > 0));
    }

    #[test]
    fn parse_info_v2() {
        let path = r"C:\x\été.txt";
        let units: Vec<u16> = path.encode_utf16().chain([0]).collect();
        let mut raw = Vec::new();
        raw.extend(2i64.to_le_bytes());
        raw.extend(7i64.to_le_bytes());
        raw.extend(((1_577_836_800i64 + 11_644_473_600) * 10_000_000).to_le_bytes());
        raw.extend((units.len() as i32).to_le_bytes());
        raw.extend(units.iter().flat_map(|u| u.to_le_bytes()));
        assert_eq!(parse_info(&raw), Some((1_577_836_800.0, path.to_string())));
    }
}
