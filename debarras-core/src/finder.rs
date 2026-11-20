//! Socle des recherches (doublons, même nom, images similaires) : fichier inchangé et lisible
//! sans téléchargement, identité de fichier, empreintes xxh3-128 mémorisées dans le cache,
//! progression. Port de `core/finder.py` et de `HashFinder` (`core/duplicates.py`).

use std::fs::File;
use std::io::{self, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use rusqlite::{Result, params};

use crate::cache::Cache;

pub const PARTIAL_SIZE: u64 = 4096;
const CHUNK: usize = 1 << 20;
const PROGRESS_INTERVAL: Duration = Duration::from_millis(100);
/// Fichiers « en ligne uniquement » (OneDrive, Proton Drive…) : les lire les téléchargerait.
/// OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS.
const CLOUD_ATTRS: u32 = 0x1000 | 0x40000 | 0x40_0000;

/// Ce que `os.stat` dit d'un fichier (liens suivis).
#[derive(Debug, Clone, PartialEq)]
pub struct FileStat {
    pub size: i64,
    /// Comme `st_mtime` : `sec + nsec * 1e-9`.
    pub mtime: f64,
    pub attributes: u32,
    /// (numéro de série du volume, index du fichier) = (`st_dev`, `st_ino`).
    pub id: (u32, u64),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stale {
    /// Disparu, illisible ou modifié depuis le scan.
    Changed,
    /// En ligne uniquement : le lire le téléchargerait.
    Cloud,
}

#[cfg(windows)]
pub fn stat(path: &str) -> io::Result<FileStat> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_READ_ATTRIBUTES,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, GetFileInformationByHandle,
        OPEN_EXISTING,
    };
    let wide: Vec<u16> = path.encode_utf16().chain([0]).collect();
    // SAFETY : chemin terminé par un zéro ; poignée fermée sur tous les chemins ; `info` est
    // une structure C remplie par l'appel.
    unsafe {
        // Attributs seulement : n'hydrate pas un fichier en ligne (comme os.stat).
        let h = CreateFileW(
            wide.as_ptr(),
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            std::ptr::null_mut(),
        );
        if h == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let mut info: BY_HANDLE_FILE_INFORMATION = std::mem::zeroed();
        let ok = GetFileInformationByHandle(h, &mut info);
        CloseHandle(h);
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        let ft = (u64::from(info.ftLastWriteTime.dwHighDateTime) << 32)
            | u64::from(info.ftLastWriteTime.dwLowDateTime);
        Ok(FileStat {
            size: ((u64::from(info.nFileSizeHigh) << 32) | u64::from(info.nFileSizeLow)) as i64,
            mtime: filetime_to_py(ft),
            attributes: info.dwFileAttributes,
            id: (
                info.dwVolumeSerialNumber,
                (u64::from(info.nFileIndexHigh) << 32) | u64::from(info.nFileIndexLow),
            ),
        })
    }
}

#[cfg(not(windows))]
pub fn stat(path: &str) -> io::Result<FileStat> {
    use std::os::unix::fs::MetadataExt;
    let md = std::fs::metadata(path)?;
    Ok(FileStat {
        size: md.len() as i64,
        mtime: md.mtime() as f64 + md.mtime_nsec() as f64 * 1e-9,
        attributes: 0,
        id: (md.dev() as u32, md.ino()),
    })
}

/// FILETIME (centaines de ns depuis 1601) → `st_mtime` de Python, au bit près.
fn filetime_to_py(ft: u64) -> f64 {
    const SECS_1601_1970: i64 = 11_644_473_600;
    let sec = (ft / 10_000_000) as i64 - SECS_1601_1970;
    let nsec = (ft % 10_000_000) * 100;
    sec as f64 + nsec as f64 * 1e-9
}

/// Le fichier est-il lisible sans téléchargement et tel qu'au scan ?
pub fn fresh_stat(path: &str, size: i64, mtime: f64) -> std::result::Result<FileStat, Stale> {
    let st = stat(path).map_err(|_| Stale::Changed)?;
    if st.size != size || (st.mtime - mtime).abs() > 1e-3 {
        return Err(Stale::Changed);
    }
    if st.attributes & CLOUD_ATTRS != 0 {
        return Err(Stale::Cloud);
    }
    Ok(st)
}

/// xxh3-128 des `limit` premiers octets (ou du fichier entier) → (empreinte, octets lus).
/// Empreinte `None` si `cancel` est levé pendant une lecture complète. Format partagé avec la
/// version Python (colonnes `partial_hash` / `full_hash`) : 32 chiffres hexadécimaux.
pub fn xxh_file(
    path: &str,
    limit: Option<u64>,
    cancel: &AtomicBool,
    on_read: &mut dyn FnMut(u64),
) -> io::Result<(Option<String>, u64)> {
    let mut h = xxhash_rust::xxh3::Xxh3::new();
    let mut f = File::open(path)?;
    let mut buf = vec![0u8; CHUNK];
    let mut read = 0u64;
    loop {
        let want = limit.map_or(CHUNK as u64, |l| (CHUNK as u64).min(l - read)) as usize;
        if want == 0 {
            break;
        }
        let n = match f.read(&mut buf[..want]) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        };
        h.update(&buf[..n]);
        read += n as u64;
        if limit.is_none() {
            if cancel.load(Ordering::Relaxed) {
                return Ok((None, read));
            }
            on_read(read);
        }
    }
    Ok((Some(format!("{:032x}", h.digest128())), read))
}

/// Progression limitée à une émission toutes les 100 ms (sauf `force`).
pub struct Progress<'a> {
    f: &'a mut dyn FnMut(&str, u64, u64),
    last: Option<Instant>,
}

impl<'a> Progress<'a> {
    pub fn new(f: &'a mut dyn FnMut(&str, u64, u64)) -> Self {
        Progress { f, last: None }
    }

    pub fn emit(&mut self, phase: &str, done: u64, total: u64, force: bool) {
        if force || self.last.is_none_or(|t| t.elapsed() >= PROGRESS_INTERVAL) {
            self.last = Some(Instant::now());
            (self.f)(phase, done, total);
        }
    }
}

/// Fichier candidat, lu dans le cache.
#[derive(Debug, Clone)]
pub struct Cand {
    pub path: String,
    pub dir_id: i64,
    pub name: String,
    pub size: i64,
    pub mtime: f64,
    pub partial: Option<String>,
    pub full: Option<String>,
    /// Empreinte à écrire dans le cache.
    pub dirty: bool,
}

/// Compteurs communs des recherches par empreinte.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HashStats {
    pub hashed_bytes: u64,
    pub skipped_error: u64,
}

/// Empreinte de `c` (4 Ko si `limit`, sinon complète) ; `None` si illisible ou annulé.
pub fn hash_cand(
    c: &mut Cand,
    limit: Option<u64>,
    stats: &mut HashStats,
    cancel: &AtomicBool,
    on_read: &mut dyn FnMut(u64),
) -> Option<String> {
    match xxh_file(&c.path, limit, cancel, on_read) {
        Err(_) => {
            stats.skipped_error += 1;
            None
        }
        Ok((None, _)) => None,
        Ok((Some(d), read)) => {
            stats.hashed_bytes += read;
            c.dirty = true;
            Some(d)
        }
    }
}

/// Mémorise les empreintes (seulement si le fichier n'a pas changé entre-temps).
pub fn save_hashes<'a>(cache: &Cache, cands: impl IntoIterator<Item = &'a Cand>) -> Result<()> {
    let tx = cache.conn.unchecked_transaction()?;
    {
        let mut st = tx.prepare_cached(
            "UPDATE files SET partial_hash = ?, full_hash = ? \
             WHERE dir_id = ? AND name = ? AND size = ? AND mtime = ?",
        )?;
        for c in cands.into_iter().filter(|c| c.dirty) {
            st.execute(params![
                c.partial, c.full, c.dir_id, c.name, c.size, c.mtime
            ])?;
        }
    }
    tx.commit()
}

/// Lit les candidats d'une requête `SELECT path, dir_id, name, size, mtime, partial_hash,
/// full_hash …`, triés par chemin : l'ordre ne dépend pas de l'ordre en base.
pub fn load_cands(cache: &Cache, sql: &str, args: impl rusqlite::Params) -> Result<Vec<Cand>> {
    let mut st = cache.conn.prepare(sql)?;
    let mut out: Vec<Cand> = st
        .query_map(args, |r| {
            Ok(Cand {
                path: r.get(0)?,
                dir_id: r.get(1)?,
                name: r.get(2)?,
                size: r.get(3)?,
                mtime: r.get(4)?,
                partial: r.get(5)?,
                full: r.get(6)?,
                dirty: false,
            })
        })?
        .collect::<Result<_>>()?;
    out.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

/// Regroupe en gardant l'ordre de première apparition (comme un dict Python).
pub fn group_by<T, K: Eq + std::hash::Hash>(
    items: Vec<T>,
    key: impl Fn(&T) -> K,
) -> Vec<(K, Vec<T>)> {
    let mut index: std::collections::HashMap<K, usize> = std::collections::HashMap::new();
    let mut out: Vec<(K, Vec<T>)> = Vec::new();
    for it in items {
        let k = key(&it);
        match index.get(&k) {
            Some(&i) => out[i].1.push(it),
            None => {
                index.insert(k, out.len());
                let k2 = key(&it);
                out.push((k2, vec![it]));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn xxh3_128_known_values() {
        // python -c "import xxhash; print(xxhash.xxh3_128(b'').hexdigest(), xxhash.xxh3_128(b'abc').hexdigest())"
        let tmp = tempfile::tempdir().unwrap();
        let (empty, abc) = (tmp.path().join("vide"), tmp.path().join("abc"));
        std::fs::write(&empty, b"").unwrap();
        std::fs::write(&abc, b"abc").unwrap();
        let h = |p: &std::path::Path, limit| {
            xxh_file(
                &p.to_string_lossy(),
                limit,
                &AtomicBool::new(false),
                &mut |_| {},
            )
            .unwrap()
        };
        assert_eq!(
            h(&empty, None),
            (Some("99aa06d3014798d86001c324468d497f".into()), 0)
        );
        assert_eq!(
            h(&abc, None),
            (Some("06b05ab6733a618578af5f94892f3950".into()), 3)
        );
        assert_eq!(h(&abc, Some(2)).1, 2);
    }

    #[test]
    fn fresh_stat_detects_changes() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("a.bin");
        std::fs::write(&p, b"12345").unwrap();
        let p = p.to_string_lossy().into_owned();
        let st = stat(&p).unwrap();
        assert_eq!(st.size, 5);
        assert!(fresh_stat(&p, 5, st.mtime).is_ok());
        assert_eq!(fresh_stat(&p, 6, st.mtime), Err(Stale::Changed));
        assert_eq!(fresh_stat(&p, 5, st.mtime - 1.0), Err(Stale::Changed));
        assert_eq!(
            fresh_stat(&format!("{p}.absent"), 5, st.mtime),
            Err(Stale::Changed)
        );
        // Un lien physique a la même identité que sa cible.
        let link = format!("{p}.lien");
        std::fs::hard_link(&p, &link).unwrap();
        assert_eq!(stat(&link).unwrap().id, st.id);
    }

    #[test]
    fn filetime_like_python() {
        // 2020-01-01 00:00:00.1234567 UTC en FILETIME.
        let ft = (1_577_836_800 + 11_644_473_600) * 10_000_000 + 1_234_567;
        assert_eq!(
            filetime_to_py(ft).to_bits(),
            (1_577_836_800f64 + 123_456_700f64 * 1e-9).to_bits()
        );
    }

    #[test]
    fn group_by_keeps_first_seen_order() {
        let g = group_by(vec![3, 1, 3, 2, 1], |x| *x);
        assert_eq!(g, vec![(3, vec![3, 3]), (1, vec![1, 1]), (2, vec![2])]);
    }
}
