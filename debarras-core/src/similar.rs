//! Images similaires : empreinte visuelle (dHash 64 bits) + regroupement par distance de
//! Hamming. Port de `core/similar.py`.
//!
//! - Robuste au redimensionnement, à la recompression et aux petites retouches ; pas aux
//!   recadrages importants ni aux rotations (l'orientation EXIF est appliquée).
//! - Les empreintes sont mémorisées dans le cache (`files.image_hash`) et invalidées quand le
//!   fichier change : un second passage ne relit presque rien.
//! - Fichiers « en ligne uniquement » ignorés (les lire les téléchargerait).
//!
//! Les empreintes ne sont pas identiques au bit près à celles de Pillow (décodage JPEG
//! réduit, arrondis du filtre) : la migration v4 du cache les remet à zéro.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use image::{DynamicImage, GrayImage, ImageDecoder, ImageReader, imageops::FilterType};
use rayon::prelude::*;
use rusqlite::{Result, params, params_from_iter, types::Value};

use crate::cache::{Cache, subtree};
use crate::finder::{Progress, Stale, fresh_stat};

pub const IMAGE_EXTS: [&str; 8] = [
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp",
];
/// Seuils proposés (bits différents sur 64).
pub const THRESHOLDS: [(&str, u32); 3] = [
    ("Identiques à l'œil", 0),
    ("Très proches", 5),
    ("Proches", 10),
];
pub const DEFAULT_THRESHOLD: u32 = 5;
const THUMB: u32 = 96;
/// Garde-fou contre les images piégées (« bombes ») : Pillow refuse au-delà de 2 × 200 Mpx.
const MAX_PIXELS: u64 = 400_000_000;
const SAVE_EVERY: usize = 500;

// --- empreinte -------------------------------------------------------------------------

/// Décode `path` avec l'orientation EXIF appliquée (`ImageOps.exif_transpose`).
fn open(path: &str) -> image::ImageResult<DynamicImage> {
    let mut dec = ImageReader::open(path)?
        .with_guessed_format()?
        .into_decoder()?;
    let (w, h) = dec.dimensions();
    if u64::from(w) * u64::from(h) > MAX_PIXELS {
        return Err(image::ImageError::Limits(
            image::error::LimitError::from_kind(image::error::LimitErrorKind::DimensionError),
        ));
    }
    let orientation = dec.orientation()?;
    let mut img = DynamicImage::from_decoder(dec)?;
    img.apply_orientation(orientation);
    Ok(img)
}

/// Niveaux de gris comme `convert("L")` de Pillow (ITU-R 601-2 : 299/587/114), alpha ignoré.
fn luma(img: &DynamicImage) -> GrayImage {
    let rgb = img.to_rgb8();
    GrayImage::from_fn(rgb.width(), rgb.height(), |x, y| {
        let [r, g, b] = rgb.get_pixel(x, y).0;
        let l = (u32::from(r) * 19595 + u32::from(g) * 38470 + u32::from(b) * 7471 + 0x8000) >> 16;
        image::Luma([l as u8])
    })
}

/// dHash 64 bits : compare chaque pixel à son voisin de droite sur une vignette 9×8.
pub fn dhash_image(img: &DynamicImage) -> u64 {
    let small = image::imageops::resize(&luma(img), 9, 8, FilterType::Lanczos3);
    let px = small.as_raw();
    let mut bits = 0u64;
    for row in 0..8 {
        for col in 0..8 {
            let i = row * 9 + col;
            bits = (bits << 1) | u64::from(px[i] > px[i + 1]);
        }
    }
    bits
}

pub fn dhash(path: &str) -> image::ImageResult<u64> {
    open(path).map(|img| dhash_image(&img))
}

pub fn distance(a: u64, b: u64) -> u32 {
    (a ^ b).count_ones()
}

/// (largeur, hauteur, vignette JPEG) : taille affichée (après orientation EXIF).
pub fn image_info(path: &str) -> image::ImageResult<(u32, u32, Vec<u8>)> {
    let img = open(path)?;
    let (w, h) = (img.width(), img.height());
    let mut buf = Vec::new();
    let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 80);
    img.thumbnail(THUMB, THUMB)
        .to_rgb8()
        .write_with_encoder(enc)?;
    Ok((w, h, buf))
}

// --- regroupement ----------------------------------------------------------------------

/// Arbre BK sur la distance de Hamming : trouve les voisins sans tout comparer.
#[derive(Default)]
pub struct BkTree {
    nodes: Vec<(u64, HashMap<u32, usize>)>,
}

impl BkTree {
    pub fn add(&mut self, value: u64) {
        if self.nodes.is_empty() {
            self.nodes.push((value, HashMap::new()));
            return;
        }
        let mut node = 0;
        loop {
            let d = distance(value, self.nodes[node].0);
            if d == 0 {
                return;
            }
            match self.nodes[node].1.get(&d) {
                Some(&child) => node = child,
                None => {
                    let id = self.nodes.len();
                    self.nodes.push((value, HashMap::new()));
                    self.nodes[node].1.insert(d, id);
                    return;
                }
            }
        }
    }

    pub fn search(&self, value: u64, radius: u32) -> Vec<u64> {
        let mut out = Vec::new();
        let mut stack = if self.nodes.is_empty() {
            vec![]
        } else {
            vec![0]
        };
        while let Some(n) = stack.pop() {
            let (val, children) = &self.nodes[n];
            let d = distance(value, *val);
            if d <= radius {
                out.push(*val);
            }
            stack.extend(
                children
                    .iter()
                    .filter(|(k, _)| d.saturating_sub(radius) <= **k && **k <= d + radius)
                    .map(|(_, c)| *c),
            );
        }
        out
    }
}

/// Composantes connexes « distance ≤ radius » (union-find) ; index par groupe (≥ 2).
pub fn group_hashes(hashes: &[u64], radius: u32) -> Vec<Vec<usize>> {
    let by_hash = crate::finder::group_by((0..hashes.len()).collect(), |&i| hashes[i]);
    let pos: HashMap<u64, usize> = by_hash
        .iter()
        .enumerate()
        .map(|(i, (h, _))| (*h, i))
        .collect();
    let mut parent: Vec<usize> = (0..by_hash.len()).collect();
    fn find(parent: &mut [usize], mut x: usize) -> usize {
        while parent[x] != x {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        x
    }
    if radius > 0 {
        let mut tree = BkTree::default();
        by_hash.iter().for_each(|(h, _)| tree.add(*h));
        for (i, (h, _)) in by_hash.iter().enumerate() {
            for other in tree.search(*h, radius) {
                let (ra, rb) = (find(&mut parent, i), find(&mut parent, pos[&other]));
                if ra != rb {
                    parent[ra] = rb;
                }
            }
        }
    }
    let roots: Vec<usize> = (0..by_hash.len()).map(|i| find(&mut parent, i)).collect();
    let groups = crate::finder::group_by((0..by_hash.len()).collect(), |&i| roots[i]);
    groups
        .into_iter()
        .map(|(_, members)| {
            members
                .into_iter()
                .flat_map(|m| by_hash[m].1.clone())
                .collect::<Vec<_>>()
        })
        .filter(|g| g.len() > 1)
        .collect()
}

// --- résultats -------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct SimilarFile {
    pub path: String,
    pub size: i64,
    pub mtime: f64,
    pub hash: u64,
    pub width: u32,
    pub height: u32,
    pub thumb: Vec<u8>,
    /// Distance à l'image de référence du groupe (la meilleure résolution).
    pub distance: u32,
}

impl SimilarFile {
    pub fn pixels(&self) -> u64 {
        u64::from(self.width) * u64::from(self.height)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct SimilarGroup {
    pub files: Vec<SimilarFile>,
}

impl SimilarGroup {
    /// Espace libéré si l'on ne garde que la meilleure version (plus grande résolution).
    pub fn wasted(&self) -> i64 {
        let best = self
            .files
            .iter()
            .max_by_key(|f| (f.pixels(), f.size))
            .map_or(0, |f| f.size);
        self.files.iter().map(|f| f.size).sum::<i64>() - best
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SimilarResult {
    pub root: String,
    pub threshold: u32,
    pub groups: Vec<SimilarGroup>,
    pub candidates: usize,
    /// Empreintes calculées (les autres venaient du cache).
    pub hashed: usize,
    pub skipped_cloud: u64,
    pub skipped_changed: u64,
    /// Formats non lisibles, fichiers corrompus.
    pub skipped_error: u64,
    pub cancelled: bool,
    pub duration: f64,
}

impl SimilarResult {
    pub fn wasted(&self) -> i64 {
        self.groups.iter().map(SimilarGroup::wasted).sum()
    }
}

struct Cand {
    path: String,
    dir_id: i64,
    name: String,
    size: i64,
    mtime: f64,
    hash: Option<u64>,
}

enum Outcome {
    Hash(u64),
    Stale(Stale),
    Error,
    Cancelled,
}

/// Images similaires sous `root` (fichiers d'au moins `min_size` octets), regroupées si leurs
/// empreintes diffèrent d'au plus `threshold` bits.
pub fn find_similar(
    cache: &Cache,
    root: &str,
    min_size: i64,
    threshold: u32,
    cancel: &AtomicBool,
    progress: &mut dyn FnMut(&str, u64, u64),
) -> Result<SimilarResult> {
    let t0 = Instant::now();
    let mut res = SimilarResult {
        root: root.to_string(),
        threshold,
        ..Default::default()
    };
    let mut prog = Progress::new(progress);
    let stop = || cancel.load(Ordering::Relaxed);
    let finish = |mut res: SimilarResult, cancelled| {
        res.cancelled = cancelled;
        res.duration = t0.elapsed().as_secs_f64();
        Ok(res)
    };

    let (scope, sargs) = subtree("parent", root);
    let marks = vec!["?"; IMAGE_EXTS.len()].join(",");
    let sql = format!(
        "SELECT path, dir_id, name, size, mtime, image_hash FROM file_paths \
         WHERE {scope} AND ext IN ({marks}) AND size >= ?"
    );
    let args: Vec<Value> = sargs
        .iter()
        .cloned()
        .map(Value::from)
        .chain(IMAGE_EXTS.iter().map(|e| Value::from(e.to_string())))
        .chain([Value::from(min_size.max(1))])
        .collect();
    let mut cands: Vec<Cand> = cache
        .conn
        .prepare(&sql)?
        .query_map(params_from_iter(args), |r| {
            Ok(Cand {
                path: r.get(0)?,
                dir_id: r.get(1)?,
                name: r.get(2)?,
                size: r.get(3)?,
                mtime: r.get(4)?,
                hash: r.get::<_, Option<i64>>(5)?.map(|h| h as u64), // SQLite : entier signé
            })
        })?
        .collect::<Result<_>>()?;
    cands.sort_by(|a, b| a.path.cmp(&b.path));
    res.candidates = cands.len();

    // 1. Empreintes manquantes, calculées en parallèle par paquets (enregistrés au fur et à mesure).
    let (mut ready, todo): (Vec<Cand>, Vec<Cand>) =
        cands.into_iter().partition(|c| c.hash.is_some());
    let total = todo.len() as u64;
    let mut done = 0u64;
    let mut todo = todo.into_iter().peekable();
    while todo.peek().is_some() && !stop() {
        let mut batch: Vec<Cand> = todo.by_ref().take(SAVE_EVERY).collect();
        let outcomes: Vec<Outcome> = batch
            .par_iter()
            .map(|c| {
                if stop() {
                    return Outcome::Cancelled;
                }
                if let Err(why) = fresh_stat(&c.path, c.size, c.mtime) {
                    return Outcome::Stale(why);
                }
                dhash(&c.path).map_or(Outcome::Error, Outcome::Hash)
            })
            .collect();
        let tx = cache.conn.unchecked_transaction()?;
        {
            let mut st = tx.prepare_cached(
                "UPDATE files SET image_hash = ? WHERE dir_id = ? AND name = ? AND size = ? AND mtime = ?",
            )?;
            for (mut c, outcome) in batch.drain(..).zip(outcomes) {
                match outcome {
                    Outcome::Hash(h) => {
                        st.execute(params![h as i64, c.dir_id, c.name, c.size, c.mtime])?;
                        c.hash = Some(h);
                        ready.push(c);
                        res.hashed += 1;
                    }
                    Outcome::Stale(Stale::Cloud) => res.skipped_cloud += 1,
                    Outcome::Stale(Stale::Changed) => res.skipped_changed += 1,
                    Outcome::Error => res.skipped_error += 1,
                    Outcome::Cancelled => {}
                }
                done += 1;
            }
        }
        tx.commit()?; // même en cas d'annulation : le travail fait est gardé
        prog.emit("Empreintes visuelles", done, total, false);
    }
    if stop() {
        return finish(res, true);
    }

    // 2. Regroupement.
    prog.emit("Regroupement", 0, 1, true);
    let hashes: Vec<u64> = ready.iter().map(|c| c.hash.unwrap_or_default()).collect();
    let groups = group_hashes(&hashes, threshold);

    // 3. Résolution + vignette des images retenues.
    let members: Vec<usize> = groups.iter().flatten().copied().collect();
    let infos: Vec<Option<(u32, u32, Vec<u8>)>> = members
        .par_iter()
        .map(|&i| {
            if stop() {
                None
            } else {
                image_info(&ready[i].path).ok()
            }
        })
        .collect();
    if stop() {
        return finish(res, true);
    }
    prog.emit(
        "Vignettes",
        members.len() as u64,
        members.len() as u64,
        true,
    );
    let mut info: HashMap<usize, (u32, u32, Vec<u8>)> = members
        .into_iter()
        .zip(infos)
        .filter_map(|(i, x)| x.map(|x| (i, x)))
        .collect();
    for g in groups {
        let mut files: Vec<SimilarFile> = g
            .into_iter()
            .map(|i| {
                let c = &ready[i];
                let (width, height, thumb) = info.remove(&i).unwrap_or_default();
                SimilarFile {
                    path: c.path.clone(),
                    size: c.size,
                    mtime: c.mtime,
                    hash: c.hash.unwrap_or_default(),
                    width,
                    height,
                    thumb,
                    distance: 0,
                }
            })
            .collect();
        let reference = files
            .iter()
            .max_by_key(|f| (f.pixels(), f.size))
            .map_or(0, |f| f.hash);
        for f in &mut files {
            f.distance = distance(f.hash, reference);
        }
        files.sort_by(|a, b| {
            (b.pixels(), b.size)
                .cmp(&(a.pixels(), a.size))
                .then_with(|| a.path.to_lowercase().cmp(&b.path.to_lowercase()))
        });
        res.groups.push(SimilarGroup { files });
    }
    res.groups.sort_by(|a, b| {
        b.wasted()
            .cmp(&a.wasted())
            .then_with(|| a.files[0].path.cmp(&b.files[0].path))
    });
    finish(res, false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scanner::{ScanOptions, scan};
    use image::{Rgb, RgbImage};
    use std::path::Path;

    /// Image « photo » déterministe : dégradé + disques pseudo-aléatoires.
    fn picture(seed: u64, w: u32, h: u32) -> RgbImage {
        let mut s = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let mut rnd = move |n: u32| {
            s = s
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            ((s >> 33) % u64::from(n)) as u32
        };
        let mut img = RgbImage::from_fn(w, h, |x, _| {
            let v = (x * 255 / w) as u8;
            Rgb([v, v, v])
        });
        for _ in 0..12 {
            let (cx, cy, r) = (rnd(w) as i64, rnd(h) as i64, i64::from(20 + rnd(140)));
            let color = Rgb([rnd(256) as u8, rnd(256) as u8, rnd(256) as u8]);
            for (x, y, p) in img.enumerate_pixels_mut() {
                let (dx, dy) = (i64::from(x) - cx, i64::from(y) - cy);
                if dx * dx + dy * dy <= r * r {
                    *p = color;
                }
            }
        }
        img
    }

    fn save(img: &RgbImage, path: &Path) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        img.save(path).unwrap();
    }

    fn jpeg(img: &RgbImage, quality: u8) -> Vec<u8> {
        let mut buf = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, quality)
            .encode_image(img)
            .unwrap();
        buf
    }

    /// Ajoute un segment EXIF « orientation » à un JPEG (comme un téléphone).
    fn with_orientation(jpeg: &[u8], orientation: u16) -> Vec<u8> {
        let mut tiff = b"II*\0\x08\0\0\0\x01\0\x12\x01\x03\0\x01\0\0\0".to_vec();
        tiff.extend(orientation.to_le_bytes());
        tiff.extend([0, 0, 0, 0, 0, 0]);
        let payload = [b"Exif\0\0".as_slice(), &tiff].concat();
        let len = (payload.len() + 2) as u16;
        [
            &jpeg[..2],
            &[0xFF, 0xE1],
            &len.to_be_bytes(),
            &payload,
            &jpeg[2..],
        ]
        .concat()
    }

    #[test]
    fn dhash_survives_resize_recompression_and_exif_rotation() {
        let tmp = tempfile::tempdir().unwrap();
        let a = picture(1, 640, 480);
        let p = |n: &str| tmp.path().join(n);
        save(&a, &p("ref.png"));
        save(
            &image::imageops::resize(&a, 200, 150, FilterType::Lanczos3),
            &p("small.png"),
        );
        std::fs::write(p("low.jpg"), jpeg(&a, 35)).unwrap();
        // Pixels tournés + balise EXIF « à redresser » : même image affichée.
        let rotated = image::imageops::rotate270(&a);
        std::fs::write(p("rot.jpg"), with_orientation(&jpeg(&rotated, 92), 6)).unwrap();
        save(&picture(2, 640, 480), &p("other.png"));
        let h = |n: &str| dhash(&p(n).to_string_lossy()).unwrap();
        let reference = h("ref.png");
        assert!(distance(reference, h("small.png")) <= 3);
        assert!(distance(reference, h("low.jpg")) <= 5);
        assert!(distance(reference, h("rot.jpg")) <= 5);
        assert!(distance(reference, h("other.png")) > 15);
        let (w, hh, thumb) = image_info(&p("rot.jpg").to_string_lossy()).unwrap();
        assert_eq!((w, hh), (640, 480)); // taille affichée, redressée
        assert!(thumb.starts_with(&[0xFF, 0xD8]));
    }

    #[test]
    fn signed_roundtrip() {
        for h in [0u64, 1, (1 << 63) - 1, 1 << 63, u64::MAX] {
            assert_eq!((h as i64) as u64, h);
        }
    }

    #[test]
    fn bktree_matches_brute_force() {
        let mut s = 7u64;
        let mut next = || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            s
        };
        let mut values: Vec<u64> = (0..400).map(|_| next()).collect();
        let near: Vec<u64> = values[..50]
            .iter()
            .map(|v| v ^ (1 << (next() % 64)))
            .collect();
        values.extend(near);
        let mut tree = BkTree::default();
        values.iter().for_each(|v| tree.add(*v));
        for q in &values[..40] {
            for radius in [0, 3, 10] {
                let mut got = tree.search(*q, radius);
                got.sort();
                let mut want: Vec<u64> = values
                    .iter()
                    .copied()
                    .filter(|v| distance(*q, *v) <= radius)
                    .collect();
                want.sort();
                want.dedup();
                assert_eq!(got, want);
            }
        }
    }

    #[test]
    fn group_hashes_connected_components() {
        let (a, b) = (0u64, 0b111); // distance 3
        let c = 0b111 | (0b1111 << 20); // 4 de b, 7 de a
        let far = u64::MAX;
        let sorted = |mut g: Vec<Vec<usize>>| {
            g.iter_mut().for_each(|x| x.sort());
            g.sort();
            g
        };
        assert_eq!(
            sorted(group_hashes(&[a, b, c, far, a], 4)),
            [vec![0, 1, 2, 4]]
        );
        assert_eq!(sorted(group_hashes(&[a, b, c, far, a], 0)), [vec![0, 4]]);
    }

    #[test]
    fn finder_groups_variants_and_uses_cache() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        let (a, b) = (picture(10, 640, 480), picture(20, 640, 480));
        save(&a, &root.join(r"orig\a.png"));
        std::fs::create_dir_all(root.join("web")).unwrap();
        std::fs::write(
            root.join(r"web\a_small.jpg"),
            jpeg(
                &image::imageops::resize(&a, 320, 240, FilterType::Lanczos3),
                80,
            ),
        )
        .unwrap();
        std::fs::create_dir_all(root.join("copie")).unwrap();
        std::fs::write(root.join(r"copie\a.jpg"), jpeg(&a, 50)).unwrap();
        save(&b, &root.join("b.png"));
        save(
            &image::imageops::resize(&b, 400, 300, FilterType::Lanczos3),
            &root.join("b_mini.webp"),
        );
        save(&picture(30, 640, 480), &root.join("seule.png"));
        std::fs::write(root.join("casse.jpg"), b"pas une image").unwrap();
        std::fs::write(root.join("notes.txt"), b"ignore").unwrap();
        let db = tmp.path().join("cache.db");
        let rs = root.to_string_lossy().into_owned();
        let o = ScanOptions {
            excluded_paths: vec![],
            excluded_names: vec![],
            ignore_patterns: vec![],
        };
        let rescan = || scan(&rs, &db, &o, &AtomicBool::new(false), |_, _, _| {}).unwrap();
        rescan();
        let cache = Cache::open(&db).unwrap();
        let find = || {
            find_similar(
                &cache,
                &rs,
                1,
                5,
                &AtomicBool::new(false),
                &mut |_, _, _| {},
            )
            .unwrap()
        };
        let names = |r: &SimilarResult| {
            let mut g: Vec<Vec<String>> = r
                .groups
                .iter()
                .map(|g| {
                    let mut v: Vec<String> = g
                        .files
                        .iter()
                        .map(|f| f.path[rs.len() + 1..].to_string())
                        .collect();
                    v.sort();
                    v
                })
                .collect();
            g.sort();
            g
        };
        let res = find();
        assert_eq!(
            names(&res),
            [
                vec!["b.png", "b_mini.webp"],
                vec![r"copie\a.jpg", r"orig\a.png", r"web\a_small.jpg"]
            ]
        );
        assert_eq!((res.skipped_error, res.candidates, res.hashed), (1, 7, 6));
        let big = res.groups.iter().find(|g| g.files.len() == 3).unwrap();
        assert_eq!((big.files[0].width, big.files[0].height), (640, 480)); // meilleure version en tête
        assert_eq!(big.files[0].distance, 0);
        assert!(big.files[0].thumb.starts_with(&[0xFF, 0xD8]));
        assert_eq!(
            big.wasted(),
            big.files[1..].iter().map(|f| f.size).sum::<i64>()
        );

        let again = find();
        assert_eq!(again.hashed, 0); // empreintes en cache
        assert_eq!(names(&again), names(&res));

        // Fichier modifié -> empreinte invalidée par le rescan, puis recalculée.
        save(&picture(99, 400, 300), &root.join("b_mini.webp"));
        rescan();
        let h: Option<i64> = cache
            .conn
            .query_row(
                "SELECT image_hash FROM files WHERE name='b_mini.webp'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(h, None);
        let third = find();
        assert_eq!(third.hashed, 1);
        assert_eq!(
            names(&third),
            [vec![r"copie\a.jpg", r"orig\a.png", r"web\a_small.jpg"]]
        );
    }
}
