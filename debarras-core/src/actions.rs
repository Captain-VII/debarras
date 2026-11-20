//! Actions sur les fichiers : corbeille, déplacement, archivage ZIP. Port de `core/actions.py`.
//!
//! - Jamais de suppression définitive : tout ce qui disparaît passe par la corbeille, et
//!   l'envoi est refusé si la corbeille ne peut pas tout garder (voir `recycle`).
//! - Chaque action est tracée dans un journal JSON Lines, au format de la 1.6.x : une action
//!   faite avant la mise à jour reste annulable.
//! - Mode simulation : l'action est planifiée et journalisée, rien n'est touché.
//! - La dernière action réelle peut être annulée (restauration depuis la corbeille incluse).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::paths::{dirname, norm, split};
use crate::recycle::{self, BinInfo, absolute};
use crate::safety::{Classifier, Env};
use crate::scanner::fnmatch_str;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Trash,
    Move,
    Archive,
}

impl Kind {
    pub fn label(self) -> &'static str {
        match self {
            Kind::Trash => "Mise à la corbeille",
            Kind::Move => "Déplacement",
            Kind::Archive => "Archivage ZIP",
        }
    }
}

/// Statut d'un élément (textes de la 1.6.x, écrits tels quels dans le journal).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Status {
    #[serde(rename = "en attente")]
    Pending,
    #[serde(rename = "fait")]
    Done,
    #[serde(rename = "simulé")]
    Simulated,
    #[serde(rename = "ignoré")]
    Skipped,
    #[serde(rename = "erreur")]
    Error,
    #[serde(rename = "restauré")]
    Restored,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionItem {
    pub src: String,
    #[serde(default)]
    pub size: i64,
    #[serde(default)]
    pub dst: Option<String>,
    #[serde(default = "pending")]
    pub status: Status,
    #[serde(default)]
    pub error: String,
}

fn pending() -> Status {
    Status::Pending
}

fn yes() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionRecord {
    pub id: String,
    pub kind: Kind,
    pub created: f64,
    pub simulated: bool,
    pub items: Vec<ActionItem>,
    /// Dossier de destination ou fichier ZIP.
    #[serde(default)]
    pub target: String,
    /// Archivage : originaux à la corbeille après vérification.
    #[serde(default = "yes")]
    pub trash_originals: bool,
    #[serde(default)]
    pub undone: bool,
    #[serde(default)]
    pub undone_at: Option<f64>,
    /// Message global (archive invalide, annulation…).
    #[serde(default)]
    pub note: String,
}

impl ActionRecord {
    pub fn count(&self, statuses: &[Status]) -> usize {
        self.items
            .iter()
            .filter(|i| statuses.contains(&i.status))
            .count()
    }

    pub fn size(&self, statuses: &[Status]) -> i64 {
        self.items
            .iter()
            .filter(|i| statuses.contains(&i.status))
            .map(|i| i.size)
            .sum()
    }

    pub fn undoable(&self) -> bool {
        !self.simulated && !self.undone && self.count(&[Status::Done]) > 0
    }
}

/// Journal JSON Lines : une action par ligne, réécrit atomiquement à chaque mise à jour.
pub struct ActionLog {
    pub path: PathBuf,
}

impl ActionLog {
    /// `%LOCALAPPDATA%\Debarras\actions.jsonl`.
    pub fn default_path() -> io::Result<PathBuf> {
        Ok(crate::cache::default_db_path()?.with_file_name("actions.jsonl"))
    }

    pub fn load(&self) -> Vec<ActionRecord> {
        let Ok(text) = fs::read_to_string(&self.path) else {
            return vec![];
        };
        // Ligne abîmée : ignorée plutôt que de tout perdre.
        text.lines()
            .filter_map(|l| serde_json::from_str(l).ok())
            .collect()
    }

    pub fn save(&self, rec: &ActionRecord) -> io::Result<()> {
        let mut records: Vec<ActionRecord> =
            self.load().into_iter().filter(|r| r.id != rec.id).collect();
        records.push(rec.clone());
        records.sort_by(|a, b| a.created.total_cmp(&b.created));
        let mut text = String::new();
        for r in &records {
            text += &serde_json::to_string(r).map_err(io::Error::other)?;
            text.push('\n');
        }
        let tmp = self.path.with_extension("tmp");
        fs::write(&tmp, text)?;
        fs::rename(tmp, &self.path)
    }

    pub fn last_undoable(&self) -> Option<ActionRecord> {
        self.load()
            .into_iter()
            .rfind(|r| !r.simulated)
            .filter(ActionRecord::undoable)
    }
}

// --- garde-fous ------------------------------------------------------------------------

fn exists(p: &str) -> bool {
    fs::symlink_metadata(p).is_ok()
}

/// `path` est `parent` ou se trouve dessous (casse ignorée).
fn is_under(path: &str, parent: &str) -> bool {
    let (p, q) = (path.to_lowercase(), parent.to_lowercase());
    let q = q.trim_end_matches('\\');
    p == q || p.strip_prefix(q).is_some_and(|r| r.starts_with('\\'))
}

/// `os.path.normpath(os.path.abspath(p))`.
fn full(p: &str) -> String {
    absolute(p)
}

/// Raison de refuser `path`, ou `None` s'il peut être traité.
pub fn check_path(path: &str, env: &Env, clf: &Classifier) -> Option<String> {
    if !exists(path) {
        return Some("introuvable".into());
    }
    let rest = if path.len() >= 2 && path.as_bytes()[1] == b':' {
        &path[2..]
    } else {
        path
    };
    if rest.trim_matches(['\\', '/']).is_empty() {
        return Some("racine de lecteur".into());
    }
    if path.to_lowercase() == env.home.to_lowercase() {
        return Some("dossier utilisateur".into());
    }
    // Program Files est géré par le classement (racine et composants Windows bloqués,
    // logiciels et jeux : confirmation explicite).
    if [&env.system_root, &env.program_data]
        .iter()
        .any(|r| is_under(path, &norm(r)))
    {
        return Some("dossier système protégé".into());
    }
    if path.to_lowercase().contains("$recycle.bin") {
        return Some("corbeille".into());
    }
    let v = clf.classify(path, None);
    v.blocked().then(|| format!("protégé : {}", v.reason))
}

/// Liste blanche : un chemin (protège tout ce qu'il contient) ou un motif sur le nom.
pub fn protection(path: &str, whitelist: &[String]) -> Option<String> {
    let name = split(path).1.to_lowercase();
    for entry in whitelist.iter().map(|e| e.trim()).filter(|e| !e.is_empty()) {
        if entry.contains(['\\', '/', ':']) {
            let protected = full(entry);
            if is_under(path, &protected) {
                return Some("protégé (liste blanche)".into());
            }
            if is_under(&protected, path) {
                return Some("contient un élément protégé (liste blanche)".into());
            }
        } else if fnmatch_str(&entry.to_lowercase(), &name) {
            return Some(format!("protégé (liste blanche : {entry})"));
        }
    }
    None
}

// --- planification ---------------------------------------------------------------------

/// `x.txt` → `x (2).txt` tant que le nom est pris (disque ou déjà réservé).
fn unique(path: &str, reserved: &mut HashSet<String>) -> String {
    let (dir, name) = split(path);
    let stem_len = crate::scanner::extension(name).len();
    let (base, ext) = path.split_at(path.len() - stem_len);
    let _ = dir;
    let mut candidate = path.to_string();
    let mut n = 2;
    while exists(&candidate) || reserved.contains(&candidate.to_lowercase()) {
        candidate = format!("{base} ({n}){ext}");
        n += 1;
    }
    reserved.insert(candidate.to_lowercase());
    candidate
}

fn new_id() -> String {
    use std::hash::{BuildHasher, RandomState};
    let s = RandomState::new();
    format!(
        "{:016x}{:016x}",
        s.hash_one(crate::cache::now().to_bits()),
        s.hash_one(std::process::id())
    )
}

/// Ce qu'il faut pour planifier : environnement, classement, liste blanche.
pub struct Context<'a> {
    pub env: &'a Env,
    pub classifier: &'a Classifier,
    pub whitelist: &'a [String],
}

/// Prépare une action : normalise, dédoublonne, retire les éléments inclus dans un autre
/// élément sélectionné, vérifie chaque chemin et calcule les destinations.
pub fn plan(
    kind: Kind,
    paths: &[String],
    sizes: &HashMap<String, i64>,
    target: &str,
    trash_originals: bool,
    simulated: bool,
    ctx: &Context,
) -> ActionRecord {
    let sizes: HashMap<String, i64> = sizes
        .iter()
        .map(|(k, v)| (full(k).to_lowercase(), *v))
        .collect();
    let mut norm_paths: Vec<String> = paths
        .iter()
        .map(|p| full(p))
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    norm_paths.sort_by(|a, b| (a.chars().count(), a).cmp(&(b.chars().count(), b)));
    let refused: HashMap<&String, String> = norm_paths
        .iter()
        .filter_map(|p| {
            check_path(p, ctx.env, ctx.classifier)
                .or_else(|| protection(p, ctx.whitelist))
                .map(|r| (p, r))
        })
        .collect();
    let mut kept: Vec<&String> = Vec::new();
    for p in &norm_paths {
        // Un chemin refusé n'absorbe pas ses sous-éléments : ils sont évalués pour eux-mêmes.
        if refused.contains_key(p)
            || !kept
                .iter()
                .any(|k| !refused.contains_key(k) && is_under(p, k))
        {
            kept.push(p);
        }
    }
    let target = if target.is_empty() {
        String::new()
    } else {
        full(target)
    };
    let mut rec = ActionRecord {
        id: new_id(),
        kind,
        created: crate::cache::now(),
        simulated,
        items: vec![],
        target: target.clone(),
        trash_originals,
        undone: false,
        undone_at: None,
        note: String::new(),
    };
    let mut reserved = HashSet::new();
    for p in kept {
        let mut item = ActionItem {
            src: p.clone(),
            size: sizes.get(&p.to_lowercase()).copied().unwrap_or(0),
            dst: None,
            status: Status::Pending,
            error: String::new(),
        };
        let mut reason = refused.get(p).cloned();
        if kind == Kind::Move && reason.is_none() {
            if is_under(&target, p) {
                reason = Some("destination incluse dans l'élément".into());
            } else if dirname(p).to_lowercase() == target.to_lowercase() {
                reason = Some("déjà dans la destination".into());
            } else {
                item.dst = Some(unique(
                    &crate::paths::join(&target, split(p).1),
                    &mut reserved,
                ));
            }
        }
        if kind == Kind::Archive && reason.is_none() && is_under(&target, p) {
            reason = Some("l'archive serait créée dans l'élément".into());
        }
        if let Some(r) = reason {
            item.status = Status::Skipped;
            item.error = r;
        }
        rec.items.push(item);
    }
    if kind == Kind::Archive && !target.is_empty() && exists(&target) {
        rec.target = unique(&target, &mut HashSet::new());
    }
    rec
}

// --- exécution -------------------------------------------------------------------------

/// Réglages de la corbeille d'un lecteur (`recycle::bin_info`, ou un faux en test).
pub type BinInfoFn<'a> = &'a dyn Fn(&str) -> BinInfo;

/// Exécute l'action planifiée. `progress(fait, total, élément)`.
pub fn execute(
    rec: &mut ActionRecord,
    bin: BinInfoFn,
    progress: &mut dyn FnMut(usize, usize, &str),
    cancelled: &dyn Fn() -> bool,
) {
    let todo: Vec<usize> = (0..rec.items.len())
        .filter(|&i| rec.items[i].status == Status::Pending)
        .collect();
    if rec.simulated {
        todo.iter()
            .for_each(|&i| rec.items[i].status = Status::Simulated);
        return;
    }
    // Garde-fou du cœur (la 1.6.x ne le faisait qu'à l'écran de confirmation) : jamais
    // d'envoi que la corbeille ne pourrait pas garder entièrement.
    if rec.kind == Kind::Trash || (rec.kind == Kind::Archive && rec.trash_originals) {
        let paths: Vec<String> = todo.iter().map(|&i| rec.items[i].src.clone()).collect();
        let sizes = todo
            .iter()
            .map(|&i| (rec.items[i].src.clone(), rec.items[i].size))
            .collect();
        let problems = recycle::capacity_problems(&paths, &sizes, bin);
        if !problems.is_empty() {
            rec.note = problems.join("\n");
            for &i in &todo {
                rec.items[i].status = Status::Skipped;
                rec.items[i].error = "corbeille insuffisante : rien n'a été touché".into();
            }
            return;
        }
    }
    if rec.kind == Kind::Archive {
        archive(rec, &todo, progress, cancelled);
        return;
    }
    if rec.kind == Kind::Move
        && let Err(e) = fs::create_dir_all(&rec.target)
    {
        rec.note = format!("Destination impossible à créer : {e}");
        return;
    }
    let total = todo.len();
    for (n, &i) in todo.iter().enumerate() {
        if cancelled() {
            rec.note = "Interrompue par l'utilisateur.".into();
            break;
        }
        progress(n, total, &rec.items[i].src.clone());
        let item = &mut rec.items[i];
        let result = match rec.kind {
            Kind::Trash => recycle::send_to_trash(&item.src),
            Kind::Move => {
                let mut dst = item.dst.clone().unwrap_or_default();
                if exists(&dst) {
                    // apparu entre la planification et l'exécution
                    dst = unique(&dst, &mut HashSet::new());
                    item.dst = Some(dst.clone());
                }
                move_path(&item.src, &dst)
            }
            Kind::Archive => unreachable!(),
        };
        match result {
            Ok(()) => item.status = Status::Done,
            Err(e) => {
                item.status = Status::Error;
                item.error = e.to_string();
            }
        }
    }
    progress(total, total, "");
}

/// `shutil.move` : renommage, sinon (autre lecteur) copie puis retrait de l'original.
fn move_path(src: &str, dst: &str) -> io::Result<()> {
    if let Some(parent) = Path::new(dst).parent() {
        fs::create_dir_all(parent)?;
    }
    match fs::rename(src, dst) {
        Ok(()) => Ok(()),
        Err(e) if e.raw_os_error() == Some(17) => {
            // ERROR_NOT_SAME_DEVICE : copie complète, puis l'original est retiré (un déplacement).
            copy_tree(Path::new(src), Path::new(dst))?;
            if fs::symlink_metadata(src)?.is_dir() {
                fs::remove_dir_all(src)
            } else {
                fs::remove_file(src)
            }
        }
        Err(e) => Err(e),
    }
}

fn copy_tree(src: &Path, dst: &Path) -> io::Result<()> {
    if fs::symlink_metadata(src)?.is_dir() {
        fs::create_dir_all(dst)?;
        for e in fs::read_dir(src)? {
            let e = e?;
            copy_tree(&e.path(), &dst.join(e.file_name()))?;
        }
        Ok(())
    } else {
        fs::copy(src, dst).map(drop) // CopyFileEx : dates et attributs conservés
    }
}

/// Fichiers d'un dossier (récursif, liens non suivis), comme `os.walk`.
fn walk_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        match e.file_type() {
            Ok(t) if t.is_dir() => walk_files(&e.path(), out),
            Ok(t) if t.is_file() => out.push(e.path()),
            _ => {}
        }
    }
}

/// Plus long chemin commun de dossiers (`os.path.commonpath`, casse ignorée).
fn common_dir(dirs: &[&str]) -> String {
    let Some(first) = dirs.first() else {
        return String::new();
    };
    let mut parts: Vec<&str> = first.split('\\').collect();
    for d in &dirs[1..] {
        let other: Vec<&str> = d.split('\\').collect();
        let n = parts
            .iter()
            .zip(&other)
            .take_while(|(a, b)| a.eq_ignore_ascii_case(b))
            .count();
        parts.truncate(n);
    }
    let joined = parts.join("\\");
    if joined.ends_with(':') {
        format!("{joined}\\")
    } else {
        joined
    }
}

/// Crée le ZIP dans un fichier `.part`, le vérifie (CRC et nombre d'entrées), puis (option)
/// met les originaux à la corbeille. En cas d'échec, originaux intacts et `.part` retiré.
fn archive(
    rec: &mut ActionRecord,
    todo: &[usize],
    progress: &mut dyn FnMut(usize, usize, &str),
    cancelled: &dyn Fn() -> bool,
) {
    let dirs: Vec<&str> = todo.iter().map(|&i| dirname(&rec.items[i].src)).collect();
    let base = common_dir(&dirs);
    let mut entries: Vec<(PathBuf, String)> = Vec::new(); // (chemin disque, nom dans l'archive)
    let arc_name = |p: &Path| -> String {
        let s = p.to_string_lossy();
        let rel = s.get(base.len()..).unwrap_or(&s).trim_start_matches('\\');
        rel.replace('\\', "/")
    };
    for &i in todo {
        let src = PathBuf::from(&rec.items[i].src);
        if src.is_dir() {
            let mut files = Vec::new();
            walk_files(&src, &mut files);
            entries.extend(files.into_iter().map(|f| (f.clone(), arc_name(&f))));
        } else {
            entries.push((src.clone(), arc_name(&src)));
        }
    }
    let part = format!("{}.part", rec.target);
    let outcome = (|| -> Result<(), ArchiveError> {
        if let Some(parent) = Path::new(&rec.target).parent() {
            fs::create_dir_all(parent)?;
        }
        let mut z = zip::ZipWriter::new(fs::File::create(&part)?);
        let opts = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated)
            .compression_level(Some(6))
            .large_file(true);
        let mut buf = vec![0u8; 1 << 20];
        for (n, (path, name)) in entries.iter().enumerate() {
            if cancelled() {
                return Err(ArchiveError::Interrupted);
            }
            progress(n, entries.len(), &path.to_string_lossy());
            z.start_file(name.as_str(), opts)
                .map_err(io::Error::other)?;
            let mut f = fs::File::open(path)?;
            loop {
                let k = f.read(&mut buf)?;
                if k == 0 {
                    break;
                }
                z.write_all(&buf[..k])?;
            }
        }
        z.finish().map_err(io::Error::other)?;
        progress(entries.len(), entries.len(), "Vérification de l'archive…");
        let mut check = zip::ZipArchive::new(fs::File::open(&part)?).map_err(io::Error::other)?;
        if check.len() != entries.len() {
            return Err(io::Error::other("archive incohérente").into());
        }
        for i in 0..check.len() {
            // Lire chaque entrée jusqu'au bout fait vérifier son CRC par la crate zip.
            let mut e = check.by_index(i).map_err(io::Error::other)?;
            io::copy(&mut e, &mut io::sink())
                .map_err(|_| io::Error::other("archive incohérente"))?;
        }
        fs::rename(&part, &rec.target)?;
        Ok(())
    })();
    match outcome {
        Ok(()) => {}
        Err(ArchiveError::Interrupted) => {
            let _ = fs::remove_file(&part); // l'archive incomplète que nous venons de créer
            rec.note = "Interrompue : aucune archive créée, originaux intacts.".into();
            return;
        }
        Err(ArchiveError::Io(e)) => {
            let _ = fs::remove_file(&part);
            rec.note = format!("Archive non créée, originaux intacts : {e}");
            for &i in todo {
                rec.items[i].status = Status::Error;
                rec.items[i].error = "archivage échoué".into();
            }
            return;
        }
    }
    for &i in todo {
        let item = &mut rec.items[i];
        item.dst = Some(rec.target.clone());
        if !rec.trash_originals {
            item.status = Status::Done;
            continue;
        }
        match recycle::send_to_trash(&item.src) {
            Ok(()) => item.status = Status::Done,
            Err(e) => {
                item.status = Status::Error;
                item.error = format!("archivé mais pas mis à la corbeille : {e}");
            }
        }
    }
}

enum ArchiveError {
    Interrupted,
    Io(io::Error),
}

impl From<io::Error> for ArchiveError {
    fn from(e: io::Error) -> Self {
        ArchiveError::Io(e)
    }
}

// --- annulation ------------------------------------------------------------------------

/// Annule une action faite : restaure depuis la corbeille ou redéplace. Ne remplace jamais
/// un élément présent ; l'archive créée part à la corbeille.
pub fn undo(rec: &mut ActionRecord, progress: &mut dyn FnMut(usize, usize, &str)) {
    let since = rec.created - 5.0;
    let done: Vec<usize> = (0..rec.items.len())
        .filter(|&i| rec.items[i].status == Status::Done)
        .collect();
    let total = done.len();
    for (n, &i) in done.iter().enumerate() {
        progress(n, total, &rec.items[i].src.clone());
        let item = &mut rec.items[i];
        let result =
            if rec.kind == Kind::Trash || (rec.kind == Kind::Archive && rec.trash_originals) {
                recycle::restore(&item.src, since)
            } else if rec.kind == Kind::Move {
                if exists(&item.src) {
                    Err(io::Error::new(
                        io::ErrorKind::AlreadyExists,
                        "un élément du même nom existe déjà à l'emplacement d'origine",
                    ))
                } else {
                    move_path(item.dst.as_deref().unwrap_or_default(), &item.src)
                }
            } else {
                Ok(())
            };
        match result {
            Ok(()) => item.status = Status::Restored,
            Err(e) => item.error = format!("annulation impossible : {e}"),
        }
    }
    if rec.kind == Kind::Archive && Path::new(&rec.target).exists() {
        rec.note = match recycle::send_to_trash(&rec.target) {
            Ok(()) => "Archive envoyée à la corbeille.".into(),
            Err(e) => format!("Archive conservée : {e}"),
        };
    }
    rec.undone = true;
    rec.undone_at = Some(crate::cache::now());
    progress(total, total, "");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(root: &Path, rel: &str, content: &[u8]) -> String {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, content).unwrap();
        p.to_string_lossy().into_owned()
    }

    fn files(root: &Path) -> Vec<String> {
        let mut out = Vec::new();
        walk_files(root, &mut out);
        let mut v: Vec<String> = out
            .iter()
            .map(|p| p.strip_prefix(root).unwrap().to_string_lossy().into_owned())
            .collect();
        v.sort();
        v
    }

    struct Fx {
        tmp: tempfile::TempDir,
        env: Env,
        clf: Classifier,
    }

    impl Fx {
        fn new() -> Self {
            let tmp = tempfile::tempdir().unwrap();
            let env = Env::current();
            let clf = Classifier::new(&env, Vec::<String>::new(), None, Vec::<String>::new());
            Fx { tmp, env, clf }
        }
        fn root(&self) -> PathBuf {
            crate::paths::long_path(&self.tmp.path().to_string_lossy()).into()
        }
        fn plan(
            &self,
            kind: Kind,
            paths: &[String],
            target: &str,
            trash: bool,
            sim: bool,
            wl: &[String],
        ) -> ActionRecord {
            let ctx = Context {
                env: &self.env,
                classifier: &self.clf,
                whitelist: wl,
            };
            plan(kind, paths, &HashMap::new(), target, trash, sim, &ctx)
        }
    }

    fn roomy(d: &str) -> BinInfo {
        BinInfo {
            drive: d.into(),
            capacity: None,
            used: 0,
            disabled: false,
        }
    }

    fn run(mut rec: ActionRecord) -> ActionRecord {
        execute(&mut rec, &roomy, &mut |_, _, _| {}, &|| false);
        rec
    }

    fn s(p: PathBuf) -> String {
        p.to_string_lossy().into_owned()
    }

    #[test]
    fn check_path_refusals() {
        let fx = Fx::new();
        let f = write(&fx.root(), "ok.txt", b"x");
        let chk = |p: &str| check_path(p, &fx.env, &fx.clf);
        assert_eq!(chk(&f), None);
        assert_eq!(
            chk(&s(fx.root().join("absent"))).as_deref(),
            Some("introuvable")
        );
        assert_eq!(chk("C:\\").as_deref(), Some("racine de lecteur"));
        assert_eq!(chk(&fx.env.home).as_deref(), Some("dossier utilisateur"));
        assert_eq!(
            chk(&format!(r"{}\notepad.exe", fx.env.system_root)).as_deref(),
            Some("dossier système protégé")
        );
    }

    #[test]
    fn whitelist_protection() {
        let root = r"C:\r";
        let wl = [format!(r"{root}\keep"), "*.KDBX".into()];
        assert!(protection(r"C:\r\keep\a.txt", &wl).is_some());
        assert!(protection(r"C:\r\keep", &wl).is_some());
        assert!(protection(root, &wl).unwrap().contains("contient"));
        assert!(protection(r"C:\r\x\vault.kdbx", &wl).is_some());
        assert_eq!(protection(r"C:\r\x\a.txt", &wl), None);
    }

    #[test]
    fn plan_dedupes_nested_and_refused_paths_do_not_absorb() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, r"src\a.txt", b"x");
        write(&root, r"src\sub\b.txt", b"x");
        let paths = [
            "C:\\".to_string(),
            a,
            s(root.join("src")),
            s(root.join(r"src\sub")),
            s(root.join("nope")),
        ];
        let rec = fx.plan(Kind::Trash, &paths, "", true, false, &[]);
        let mut got: Vec<(String, Status, String)> = rec
            .items
            .iter()
            .map(|i| (split(&i.src).1.to_string(), i.status, i.error.clone()))
            .collect();
        got.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(
            got,
            [
                ("".into(), Status::Skipped, "racine de lecteur".into()),
                ("nope".into(), Status::Skipped, "introuvable".into()),
                ("src".into(), Status::Pending, String::new()),
            ]
        );
    }

    #[test]
    fn plan_applies_whitelist() {
        let fx = Fx::new();
        let root = fx.root();
        let (a, b) = (
            write(&root, r"keep\a.txt", b"x"),
            write(&root, "b.txt", b"x"),
        );
        let rec = fx.plan(
            Kind::Trash,
            &[a, b],
            "",
            true,
            false,
            &[s(root.join("keep"))],
        );
        let got: Vec<(&str, Status)> = rec
            .items
            .iter()
            .map(|i| (split(&i.src).1, i.status))
            .collect();
        assert_eq!(
            got,
            [("b.txt", Status::Pending), ("a.txt", Status::Skipped)]
        );
    }

    #[test]
    fn simulation_touches_nothing() {
        let fx = Fx::new();
        let a = write(&fx.root(), "a.txt", b"x");
        let rec = run(fx.plan(Kind::Trash, std::slice::from_ref(&a), "", true, true, &[]));
        assert_eq!(rec.items[0].status, Status::Simulated);
        assert!(Path::new(&a).exists() && !rec.undoable());
    }

    #[test]
    fn move_renames_on_conflict_and_undo_restores() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, r"src\a.txt", b"mine");
        write(&root, r"dest\a.txt", b"other");
        let b = write(&root, r"src\b.txt", b"x");
        let mut rec = run(fx.plan(Kind::Move, &[a, b], &s(root.join("dest")), true, false, &[]));
        assert_eq!(
            rec.items.iter().map(|i| i.status).collect::<Vec<_>>(),
            [Status::Done, Status::Done]
        );
        assert_eq!(
            files(&root),
            [r"dest\a (2).txt", r"dest\a.txt", r"dest\b.txt"]
        );
        undo(&mut rec, &mut |_, _, _| {});
        assert!(rec.undone);
        assert_eq!(
            rec.items.iter().map(|i| i.status).collect::<Vec<_>>(),
            [Status::Restored, Status::Restored]
        );
        assert_eq!(files(&root), [r"dest\a.txt", r"src\a.txt", r"src\b.txt"]);
        assert_eq!(fs::read(root.join(r"src\a.txt")).unwrap(), b"mine");
    }

    #[test]
    fn move_refuses_into_itself() {
        let fx = Fx::new();
        let root = fx.root();
        write(&root, r"src\a.txt", b"x");
        let rec = fx.plan(
            Kind::Move,
            &[s(root.join("src"))],
            &s(root.join(r"src\inner")),
            true,
            false,
            &[],
        );
        assert_eq!(rec.items[0].status, Status::Skipped);
    }

    #[test]
    fn undo_never_overwrites() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, r"src\a.txt", b"v1");
        let mut rec = run(fx.plan(Kind::Move, &[a], &s(root.join("dest")), true, false, &[]));
        write(&root, r"src\a.txt", b"nouveau"); // un fichier a repris la place
        undo(&mut rec, &mut |_, _, _| {});
        assert_eq!(fs::read(root.join(r"src\a.txt")).unwrap(), b"nouveau");
        assert!(root.join(r"dest\a.txt").exists());
        assert!(rec.items[0].error.contains("annulation impossible"));
    }

    #[test]
    fn log_roundtrip_reads_python_lines_and_last_undoable() {
        let fx = Fx::new();
        let root = fx.root();
        let log = ActionLog {
            path: root.join("log.jsonl"),
        };
        // Ligne écrite par la 1.6.x (format exact de dataclasses.asdict + json.dumps).
        let py = r#"{"id": "abc", "kind": "trash", "created": 1.5, "simulated": false, "items": [{"src": "C:\\x\\a.txt", "size": 3, "dst": null, "status": "fait", "error": ""}], "target": "", "trash_originals": true, "undone": false, "undone_at": null, "note": ""}"#;
        fs::write(&log.path, format!("{py}\n")).unwrap();
        assert_eq!(log.load()[0].items[0].status, Status::Done);
        let sim = run(fx.plan(
            Kind::Trash,
            &[write(&root, "a.txt", b"x")],
            "",
            true,
            true,
            &[],
        ));
        log.save(&sim).unwrap();
        let mut real = run(fx.plan(
            Kind::Move,
            &[write(&root, "b.txt", b"x")],
            &s(root.join("dest")),
            true,
            false,
            &[],
        ));
        log.save(&real).unwrap();
        fs::OpenOptions::new()
            .append(true)
            .open(&log.path)
            .unwrap()
            .write_all("ligne abîmée\n".as_bytes())
            .unwrap();
        let ids: Vec<String> = log.load().into_iter().map(|r| r.id).collect();
        assert_eq!(ids, ["abc".to_string(), sim.id.clone(), real.id.clone()]);
        assert_eq!(log.last_undoable().unwrap().id, real.id);
        undo(&mut real, &mut |_, _, _| {});
        log.save(&real).unwrap();
        assert!(log.last_undoable().is_none());
        // Écrit avec les mêmes textes que la 1.6.x.
        assert!(
            fs::read_to_string(&log.path)
                .unwrap()
                .contains(r#""status":"restauré""#)
        );
    }

    #[test]
    fn archive_without_trash_keeps_originals() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, "a.txt", b"hello");
        write(&root, r"d\b.txt", b"world");
        let z = root.join(r"out\arch.zip");
        let rec = run(fx.plan(
            Kind::Archive,
            &[a.clone(), s(root.join("d"))],
            &s(z.clone()),
            false,
            false,
            &[],
        ));
        assert_eq!(
            rec.items.iter().map(|i| i.status).collect::<Vec<_>>(),
            [Status::Done, Status::Done]
        );
        let mut zf = zip::ZipArchive::new(fs::File::open(&z).unwrap()).unwrap();
        let mut names: Vec<String> = zf.file_names().map(|n| n.unwrap().into_owned()).collect();
        names.sort();
        assert_eq!(names, ["a.txt", "d/b.txt"]);
        let mut content = String::new();
        zf.by_name("d/b.txt")
            .unwrap()
            .read_to_string(&mut content)
            .unwrap();
        assert_eq!(content, "world");
        assert!(Path::new(&a).exists() && !root.join(r"out\arch.zip.part").exists());
    }

    #[test]
    fn archive_failure_leaves_originals() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, "a.txt", b"x");
        // Destination impossible (un fichier occupe la place du dossier) : rien n'est touché.
        write(&root, "bloque", b"x");
        let rec = run(fx.plan(
            Kind::Archive,
            std::slice::from_ref(&a),
            &s(root.join(r"bloque\x.zip")),
            true,
            false,
            &[],
        ));
        assert_eq!(rec.items[0].status, Status::Error);
        assert!(Path::new(&a).exists() && !root.join(r"bloque\x.zip.part").exists());
    }

    #[test]
    fn trash_refused_when_bin_cannot_hold_it() {
        let fx = Fx::new();
        let a = write(&fx.root(), "a.txt", b"contenu");
        let mut rec = fx.plan(Kind::Trash, std::slice::from_ref(&a), "", true, false, &[]);
        rec.items[0].size = 7;
        let nuke = |d: &str| BinInfo {
            drive: d.into(),
            capacity: Some(1000),
            used: 0,
            disabled: true,
        };
        execute(&mut rec, &nuke, &mut |_, _, _| {}, &|| false);
        assert_eq!(rec.items[0].status, Status::Skipped);
        assert!(rec.note.contains("désactivée") && Path::new(&a).exists());
    }

    /// Vraie corbeille Windows : tout ce qui y est envoyé est restauré.
    #[test]
    fn trash_and_undo_via_recycle_bin() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, "a é.txt", b"contenu");
        write(&root, r"d\b.txt", b"b");
        let mut rec = run(fx.plan(
            Kind::Trash,
            &[a.clone(), s(root.join("d"))],
            "",
            true,
            false,
            &[],
        ));
        assert_eq!(
            rec.items.iter().map(|i| i.status).collect::<Vec<_>>(),
            [Status::Done, Status::Done],
            "{rec:?}"
        );
        assert!(files(&root).is_empty());
        undo(&mut rec, &mut |_, _, _| {});
        assert_eq!(
            rec.items.iter().map(|i| i.status).collect::<Vec<_>>(),
            [Status::Restored, Status::Restored],
            "{rec:?}"
        );
        assert_eq!(files(&root), ["a é.txt", r"d\b.txt"]);
        assert_eq!(fs::read(&a).unwrap(), b"contenu");
    }

    #[test]
    fn archive_trash_originals_and_undo() {
        let fx = Fx::new();
        let root = fx.root();
        let a = write(&root, "a.txt", b"hello");
        let z = s(root.join("arch.zip"));
        let mut rec = run(fx.plan(
            Kind::Archive,
            std::slice::from_ref(&a),
            &z,
            true,
            false,
            &[],
        ));
        assert_eq!(rec.items[0].status, Status::Done);
        assert!(!Path::new(&a).exists() && Path::new(&z).exists());
        undo(&mut rec, &mut |_, _, _| {});
        assert_eq!(fs::read(&a).unwrap(), b"hello");
        assert!(!Path::new(&z).exists()); // l'archive part à la corbeille
        // Nettoyage : l'archive envoyée à la corbeille est restaurée dans le dossier de test.
        recycle::restore(&z, rec.created - 5.0).unwrap();
        assert!(Path::new(&z).exists());
    }

    #[test]
    fn helpers() {
        let mut reserved = HashSet::new();
        assert_eq!(
            unique(r"Z:\absent\x.txt", &mut reserved),
            r"Z:\absent\x.txt"
        );
        assert_eq!(
            unique(r"Z:\absent\x.txt", &mut reserved),
            r"Z:\absent\x (2).txt"
        );
        assert_eq!(common_dir(&[r"C:\a\b", r"C:\a\c", r"c:\A\b\d"]), r"C:\a");
        assert_eq!(common_dir(&[r"C:\a", r"C:\b"]), r"C:\");
        assert!(is_under(r"C:\A\b", r"c:\a") && !is_under(r"C:\ab", r"C:\a"));
    }
}
