//! Disques du PC (lettres, nom, taille, place libre) et droits administrateur.
//! Port de `core/drives.py`.

use std::io;

use windows_sys::Win32::Storage::FileSystem::{
    GetDiskFreeSpaceExW, GetDriveTypeW, GetLogicalDrives, GetVolumeInformationW,
};

// GetDriveTypeW
const REMOVABLE: u32 = 2;
const FIXED: u32 = 3;
const REMOTE: u32 = 4;
const RAMDISK: u32 = 6;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Drive {
    /// `C:\`
    pub root: String,
    /// Nom du volume (« Windows », « Jeux »…).
    pub label: String,
    /// « Disque local », « Clé USB / amovible », « Lecteur réseau ».
    pub kind: String,
    pub total: u64,
    pub free: u64,
}

impl Drive {
    pub fn used(&self) -> u64 {
        self.total.saturating_sub(self.free)
    }

    pub fn title(&self) -> String {
        let letter = self.root.trim_end_matches('\\');
        if self.label.is_empty() {
            format!("{} ({letter})", self.kind)
        } else {
            format!("{} ({letter})", self.label)
        }
    }
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain([0]).collect()
}

fn space(root: &str) -> Option<(u64, u64)> {
    let (mut total, mut free) = (0u64, 0u64);
    // SAFETY : chemin terminé par un zéro ; pointeurs vers des u64 locaux.
    let ok = unsafe {
        GetDiskFreeSpaceExW(
            wide(root).as_ptr(),
            std::ptr::null_mut(),
            &mut total,
            &mut free,
        )
    };
    (ok != 0).then_some((total, free))
}

fn label(root: &str) -> String {
    let mut buf = [0u16; 261];
    // SAFETY : chemin terminé par un zéro, tampon de la taille annoncée, sorties facultatives nulles.
    let ok = unsafe {
        GetVolumeInformationW(
            wide(root).as_ptr(),
            buf.as_mut_ptr(),
            buf.len() as u32,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            0,
        )
    };
    if ok == 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buf[..buf.iter().position(|&c| c == 0).unwrap_or(buf.len())])
}

/// Disques locaux, amovibles et réseau prêts (lecteurs de CD et disques vides ignorés).
pub fn list_drives() -> Vec<Drive> {
    // SAFETY : appels sans paramètre de sortie.
    let mask = unsafe { GetLogicalDrives() };
    let mut out = Vec::new();
    for i in 0..26u8 {
        if mask & (1 << i) == 0 {
            continue;
        }
        let root = format!("{}:\\", char::from(b'A' + i));
        // SAFETY : chemin terminé par un zéro.
        let kind = unsafe { GetDriveTypeW(wide(&root).as_ptr()) };
        if ![FIXED, REMOVABLE, REMOTE, RAMDISK].contains(&kind) {
            continue;
        }
        let Some((total, free)) = space(&root) else {
            continue;
        }; // lecteur non prêt (carte SD absente…)
        if total == 0 {
            continue;
        }
        let name = match kind {
            REMOVABLE => "Clé USB / amovible",
            REMOTE => "Lecteur réseau",
            _ => "Disque local",
        };
        out.push(Drive {
            label: label(&root),
            root,
            kind: name.into(),
            total,
            free,
        });
    }
    out
}

/// Disque dont `path` est la racine (`None` pour un simple dossier).
pub fn drive_of(path: &str) -> Option<Drive> {
    let abs = crate::recycle::absolute(path);
    if abs.len() < 2 || abs.as_bytes()[1] != b':' || !abs[2..].trim_matches(['\\', '/']).is_empty()
    {
        return None;
    }
    let root = format!("{}\\", abs[..2].to_uppercase());
    list_drives().into_iter().find(|d| d.root == root)
}

pub fn is_admin() -> bool {
    // SAFETY : appel sans paramètre.
    unsafe { windows_sys::Win32::UI::Shell::IsUserAnAdmin() != 0 }
}

/// Relance Débarras avec les droits administrateur (fenêtre UAC). `Ok(true)` si accepté.
pub fn relaunch_as_admin(args: &[String]) -> io::Result<bool> {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    let exe = std::env::current_exe()?;
    let line: String = args
        .iter()
        .map(|a| format!("\"{a}\""))
        .collect::<Vec<_>>()
        .join(" ");
    // SAFETY : chaînes terminées par un zéro, valides pendant l'appel.
    let rc = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            wide("runas").as_ptr(),
            wide(&exe.to_string_lossy()).as_ptr(),
            wide(&line).as_ptr(),
            std::ptr::null(),
            1, // SW_SHOWNORMAL
        )
    };
    Ok(rc as isize > 32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_drives_and_finds_system_drive() {
        let drives = list_drives();
        assert!(!drives.is_empty() && drives.iter().all(|d| d.total > 0 && d.free <= d.total));
        let system = format!(
            "{}\\",
            std::env::var("SystemDrive").unwrap_or_else(|_| "C:".into())
        );
        assert!(drive_of(&system).is_some());
        assert!(drive_of(&format!(r"{system}Windows")).is_none()); // un dossier n'est pas un disque
        let d = Drive {
            root: r"D:\".into(),
            label: String::new(),
            kind: "Disque local".into(),
            total: 10,
            free: 4,
        };
        assert_eq!((d.used(), d.title()), (6, "Disque local (D:)".into()));
    }
}
