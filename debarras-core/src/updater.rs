//! Mise à jour automatique via les releases GitHub publiques. Port de `core/updater.py`.
//!
//! 1. Vérification : `GET /repos/<dépôt>/releases/latest` (rien d'envoyé hormis le
//!    User-Agent). Une release est retenue si elle contient `Debarras-<version>-win64.zip`.
//! 2. Téléchargement vérifié : taille et SHA-256 (champ `digest` de l'API GitHub, à défaut
//!    fichier `.sha256` joint). Sans empreinte, rien n'est installé.
//! 3. Préparation : extraction dans un dossier temporaire (chemins malveillants refusés).
//! 4. Remplacement : un script PowerShell attend la fermeture de Débarras, renomme l'ancien
//!    dossier en `<dossier>.old` (secours), met le nouveau à sa place et relance l'exe. En
//!    cas d'échec, l'ancien dossier est remis en place et relancé.
//!
//! Même archive, même script et même entrée de désinstallation que la 1.6.x : une 1.6.x
//! installe la 2.0 comme n'importe quelle mise à jour.

use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

pub const GITHUB_REPO: &str = "Captain-VII/debarras";
const API: &str = "https://api.github.com";
pub const EXE_NAME: &str = "Debarras.exe";
const TIMEOUT: Duration = Duration::from_secs(15);
const CHUNK: usize = 1 << 16;
/// Entrée « Applications installées » créée par l'installateur Inno Setup.
pub const UNINSTALL_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{2A8D66B7-37C3-42D3-8B8A-A6B86A51D234}_is1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdateError(pub String);

impl std::fmt::Display for UpdateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for UpdateError {}

fn err<T>(msg: impl Into<String>) -> Result<T, UpdateError> {
    Err(UpdateError(msg.into()))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    pub version: String,
    pub tag: String,
    pub notes: String,
    pub page_url: String,
    pub asset_name: String,
    pub asset_url: String,
    pub asset_size: u64,
    /// `None` : à récupérer dans le fichier `.sha256` joint.
    pub sha256: Option<String>,
    pub sha256_url: Option<String>,
}

/// `v1.10.2` → `[1, 10, 2]` ; zéros finaux ignorés (`1.3` = `1.3.0`), suffixe (`-beta`) aussi.
pub fn parse_version(text: &str) -> Result<Vec<u64>, UpdateError> {
    let t = text.trim();
    let t = t.strip_prefix('v').unwrap_or(t);
    let digits: String = t
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect();
    let mut parts: Vec<u64> = Vec::new();
    for p in digits.split('.') {
        match p.parse() {
            Ok(n) => parts.push(n),
            Err(_) if !parts.is_empty() => break, // « 1.2. » : point final ignoré
            Err(_) => return err(format!("version illisible : {text:?}")),
        }
    }
    while parts.len() > 1 && parts.last() == Some(&0) {
        parts.pop();
    }
    Ok(parts)
}

pub fn is_newer(candidate: &str, current: &str) -> bool {
    matches!((parse_version(candidate), parse_version(current)), (Ok(a), Ok(b)) if a > b)
}

/// API des releases. `DEBARRAS_UPDATE_API` (tests de bout en bout) n'est accepté que pour une
/// adresse locale : impossible de détourner les mises à jour vers un autre serveur.
pub fn api() -> String {
    match std::env::var("DEBARRAS_UPDATE_API") {
        Ok(t) if t.starts_with("http://127.0.0.1:") || t.starts_with("http://localhost:") => {
            t.trim_end_matches('/').to_string()
        }
        _ => API.into(),
    }
}

/// `Debarras-<version>-win64.zip` (casse ignorée) → `<version>`.
fn asset_version(name: &str) -> Option<&str> {
    let lower = name.to_lowercase();
    let v = lower
        .strip_prefix("debarras-")?
        .strip_suffix("-win64.zip")?;
    let ok = !v.is_empty()
        && v.split('.')
            .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()));
    ok.then(|| &name["debarras-".len().."debarras-".len() + v.len()])
}

fn agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(600)))
        .timeout_connect(Some(TIMEOUT))
        .timeout_recv_response(Some(TIMEOUT))
        .http_status_as_error(false)
        .tls_config(
            ureq::tls::TlsConfig::builder()
                .provider(ureq::tls::TlsProvider::NativeTls)
                .build(),
        )
        .build()
        .into()
}

fn get(
    url: &str,
    accept: &str,
    version: &str,
) -> Result<ureq::http::Response<ureq::Body>, UpdateError> {
    agent()
        .get(url)
        .header("Accept", accept)
        .header("User-Agent", &format!("Debarras/{version}"))
        .call()
        .map_err(|e| UpdateError(format!("GitHub injoignable : {e}")))
}

/// Dernière release publiée (hors brouillons et préversions), ou `None` si pas d'archive Windows.
/// `current` : version de l'appli (User-Agent).
pub fn fetch_latest(repo: &str, api: &str, current: &str) -> Result<Option<Release>, UpdateError> {
    let resp = get(
        &format!("{api}/repos/{repo}/releases/latest"),
        "application/vnd.github+json",
        current,
    )?;
    match resp.status().as_u16() {
        404 => return Ok(None), // dépôt sans release
        200 => {}
        code => return err(format!("GitHub a répondu {code}")),
    }
    let mut body = String::new();
    resp.into_body()
        .into_reader()
        .read_to_string(&mut body)
        .map_err(|e| UpdateError(format!("GitHub injoignable : {e}")))?;
    let data: serde_json::Value = serde_json::from_str(&body)
        .map_err(|e| UpdateError(format!("réponse de GitHub illisible : {e}")))?;
    let assets = data["assets"].as_array().cloned().unwrap_or_default();
    let text = |v: &serde_json::Value| v.as_str().unwrap_or("").to_string();
    for a in &assets {
        let name = text(&a["name"]);
        let Some(version) = asset_version(&name) else {
            continue;
        };
        let digest = text(&a["digest"]);
        let side = assets
            .iter()
            .find(|s| s["name"].as_str() == Some(&format!("{name}.sha256")));
        return Ok(Some(Release {
            version: version.to_string(),
            tag: text(&data["tag_name"]),
            notes: text(&data["body"]),
            page_url: text(&data["html_url"]),
            asset_url: text(&a["browser_download_url"]),
            asset_size: a["size"].as_u64().unwrap_or(0),
            sha256: digest.strip_prefix("sha256:").map(str::to_lowercase),
            sha256_url: side.map(|s| text(&s["browser_download_url"])),
            asset_name: name,
        }));
    }
    Ok(None)
}

/// Mise à jour à proposer : la dernière release si elle est plus récente que `current`
/// (ce que faisait `UpdateChecker` en 1.6.x), sinon `None`.
pub fn check(repo: &str, api: &str, current: &str) -> Result<Option<Release>, UpdateError> {
    Ok(fetch_latest(repo, api, current)?.filter(|r| is_newer(&r.version, current)))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Télécharge l'archive dans `dest` et vérifie taille et SHA-256. Renvoie le chemin du zip.
pub fn download(
    rel: &Release,
    dest: &Path,
    current: &str,
    progress: &mut dyn FnMut(u64, u64),
    cancelled: &dyn Fn() -> bool,
) -> Result<PathBuf, UpdateError> {
    let mut expected = rel.sha256.clone();
    if expected.is_none()
        && let Some(url) = &rel.sha256_url
    {
        let mut buf = Vec::new();
        get(url, "application/octet-stream", current)?
            .into_body()
            .into_reader()
            .take(200)
            .read_to_end(&mut buf)
            .map_err(|e| UpdateError(format!("téléchargement impossible : {e}")))?;
        expected = String::from_utf8_lossy(&buf)
            .split_whitespace()
            .next()
            .map(str::to_lowercase);
    }
    let expected = match expected {
        Some(e) if e.len() == 64 && e.bytes().all(|b| b.is_ascii_hexdigit()) => e,
        _ => return err("empreinte SHA-256 absente de la release : téléchargement refusé"),
    };
    let io_err = |e: io::Error| UpdateError(format!("téléchargement impossible : {e}"));
    fs::create_dir_all(dest).map_err(io_err)?;
    let target = dest.join(&rel.asset_name);
    let resp = get(&rel.asset_url, "application/octet-stream", current)?;
    if resp.status().as_u16() != 200 {
        return err(format!(
            "téléchargement impossible : GitHub a répondu {}",
            resp.status().as_u16()
        ));
    }
    let total = if rel.asset_size > 0 {
        rel.asset_size
    } else {
        resp.headers()
            .get("Content-Length")
            .and_then(|v| v.to_str().ok()?.parse().ok())
            .unwrap_or(0)
    };
    let mut reader = resp.into_body().into_reader();
    let mut out = fs::File::create(&target).map_err(io_err)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; CHUNK];
    let mut done = 0u64;
    loop {
        let n = reader.read(&mut buf).map_err(io_err)?;
        if n == 0 {
            break;
        }
        if cancelled() {
            return err("téléchargement interrompu");
        }
        out.write_all(&buf[..n]).map_err(io_err)?;
        h.update(&buf[..n]);
        done += n as u64;
        progress(done, total);
    }
    if rel.asset_size > 0 && done != rel.asset_size {
        return err(format!(
            "taille inattendue ({done} octets au lieu de {})",
            rel.asset_size
        ));
    }
    if hex(&h.finalize()) != expected {
        return err("empreinte SHA-256 incorrecte : fichier corrompu ou altéré");
    }
    Ok(target)
}

/// Extrait l'archive dans `dest` et renvoie le dossier qui contient `Debarras.exe`.
pub fn stage(zip_path: &Path, dest: &Path) -> Result<PathBuf, UpdateError> {
    let io_err = |e: io::Error| UpdateError(format!("archive illisible : {e}"));
    if dest.exists() {
        fs::remove_dir_all(dest).map_err(io_err)?; // dossier de préparation à nous
    }
    fs::create_dir_all(dest).map_err(io_err)?;
    let mut z = zip::ZipArchive::new(fs::File::open(zip_path).map_err(io_err)?)
        .map_err(|e| UpdateError(format!("archive illisible : {e}")))?;
    // Tout vérifier avant d'écrire quoi que ce soit.
    for i in 0..z.len() {
        let f = z
            .by_index(i)
            .map_err(|e| UpdateError(format!("archive illisible : {e}")))?;
        if f.enclosed_name().is_none() {
            return err(format!("archive refusée : chemin suspect {:?}", f.name()));
        }
    }
    let mut exes = Vec::new();
    for i in 0..z.len() {
        let mut f = z
            .by_index(i)
            .map_err(|e| UpdateError(format!("archive illisible : {e}")))?;
        let rel = f.enclosed_name().expect("vérifié plus haut");
        let out = dest.join(&rel);
        if f.is_dir() {
            fs::create_dir_all(&out).map_err(io_err)?;
            continue;
        }
        fs::create_dir_all(out.parent().unwrap_or(dest)).map_err(io_err)?;
        io::copy(&mut f, &mut fs::File::create(&out).map_err(io_err)?).map_err(io_err)?;
        if rel
            .file_name()
            .is_some_and(|n| n.eq_ignore_ascii_case(EXE_NAME))
        {
            exes.push(rel);
        }
    }
    match exes.as_slice() {
        [exe] if exe.components().count() <= 2 => {
            Ok(dest.join(exe.parent().unwrap_or(Path::new(""))))
        }
        _ => err(format!(
            "archive inattendue : {EXE_NAME} introuvable à la racine"
        )),
    }
}

/// Dossier de l'application installée ; `None` pour une version de développement
/// (build de débogage), qui ne doit jamais se remplacer.
pub fn install_dir() -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        return None;
    }
    std::env::current_exe()
        .ok()?
        .parent()
        .map(Path::to_path_buf)
}

/// Droits d'écriture sur le dossier parent (renommage du dossier de l'appli).
pub fn can_self_update() -> bool {
    let Some(parent) = install_dir().and_then(|d| d.parent().map(Path::to_path_buf)) else {
        return false;
    };
    let probe = parent.join(format!(".debarras-test-{}", std::process::id()));
    let ok = fs::File::create(&probe).is_ok();
    let _ = fs::remove_file(&probe);
    ok
}

/// Chaîne PowerShell entre apostrophes.
fn ps(value: &Path) -> String {
    format!("'{}'", value.to_string_lossy().replace('\'', "''"))
}

/// Script PowerShell de remplacement (exécuté après la fermeture de Débarras), identique à
/// celui de la 1.6.x. `started` : fichier témoin créé dès le démarrage du script ; Débarras
/// attend de le voir avant de se fermer (sinon l'utilisateur se retrouverait sans application
/// ni mise à jour).
pub fn write_swap_script(
    pid: u32,
    install: &Path,
    staged: &Path,
    exe_name: &str,
    folder: &Path,
    started: Option<&Path>,
    relaunch: bool,
) -> io::Result<PathBuf> {
    let backup = install.with_file_name(format!(
        "{}.old",
        install.file_name().unwrap_or_default().to_string_lossy()
    ));
    let start = if relaunch {
        "Start-Process -FilePath (Join-Path $install $exe)"
    } else {
        "# installation à la fermeture : pas de relance"
    };
    let signal = started
        .map(|s| {
            format!(
                "New-Item -ItemType File -Force -Path {} | Out-Null\n",
                ps(s)
            )
        })
        .unwrap_or_default();
    let exe = ps(Path::new(exe_name));
    let (install_ps, staged_ps, backup_ps) = (ps(install), ps(staged), ps(&backup));
    let script = format!(
        "$ErrorActionPreference = 'Stop'
{signal}$install = {install_ps}; $staged = {staged_ps}; $backup = {backup_ps}; $exe = {exe}
Set-Location ([IO.Path]::GetTempPath())
try {{ Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue }} catch {{}}
$moved = $false
try {{
    for ($i = 0; $i -lt 40 -and -not $moved; $i++) {{
        try {{
            if (Test-Path -LiteralPath $backup) {{ Remove-Item -LiteralPath $backup -Recurse -Force }}
            Rename-Item -LiteralPath $install -NewName (Split-Path $backup -Leaf)
            $moved = $true
        }} catch {{ Start-Sleep -Milliseconds 500 }}
    }}
    if (-not $moved) {{ throw 'dossier de Débarras verrouillé' }}
    # Désinstalleur de l'installateur (absent de l'archive) : conservé dans la nouvelle version.
    Get-ChildItem -LiteralPath $backup -Filter 'unins*' -File | Copy-Item -Destination $staged -Force
    Move-Item -LiteralPath $staged -Destination $install
    {start}
}} catch {{
    if ($moved -and -not (Test-Path -LiteralPath $install)) {{
        Rename-Item -LiteralPath $backup -NewName (Split-Path $install -Leaf)
    }}
    {start}
    exit 1
}}
"
    );
    let path = folder.join("debarras_update.ps1");
    // BOM : accents lus correctement par PowerShell 5.
    fs::write(
        &path,
        [b"\xEF\xBB\xBF".as_slice(), script.as_bytes()].concat(),
    )?;
    Ok(path)
}

/// Lance le script en arrière-plan ; il survit à la fermeture de Débarras. Pas de
/// DETACHED_PROCESS : sans console, PowerShell 5 s'arrête avant d'exécuter le script (constaté
/// avec la 1.6.x). CREATE_NO_WINDOW lui donne une console invisible.
#[cfg(windows)]
pub fn launch_swap(script: &Path) -> io::Result<()> {
    use std::os::windows::process::CommandExt;
    use std::process::{Command, Stdio};
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    Command::new("powershell")
        .args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
        ])
        .arg(script)
        .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(drop)
}

/// Attend le fichier témoin du script de remplacement (`tick` : garder l'interface vivante).
pub fn wait_started(marker: &Path, timeout: Duration, tick: &mut dyn FnMut()) -> bool {
    let end = Instant::now() + timeout;
    while Instant::now() < end {
        if marker.exists() {
            return true;
        }
        tick();
        std::thread::sleep(Duration::from_millis(50));
    }
    marker.exists()
}

/// Dossier de préparation des mises à jour (à côté du cache).
pub fn work_dir() -> io::Result<PathBuf> {
    Ok(crate::cache::default_db_path()?.with_file_name("update"))
}

#[cfg(windows)]
fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain([0]).collect()
}

/// Chaîne `REG_SZ` d'une clé de `HKCU`.
#[cfg(windows)]
fn reg_string(key: &str, value: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, RRF_RT_REG_SZ, RegGetValueW};
    let mut buf = vec![0u16; 2048];
    let mut len = (buf.len() * 2) as u32;
    // SAFETY : chaînes terminées par un zéro ; tampon de `len` octets.
    let rc = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            wide(key).as_ptr(),
            wide(value).as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buf.as_mut_ptr().cast(),
            &mut len,
        )
    };
    if rc != 0 {
        return None;
    }
    let n = (len as usize / 2).saturating_sub(1); // sans le zéro final
    Some(String::from_utf16_lossy(&buf[..n.min(buf.len())]))
}

/// Après une mise à jour automatique, met à jour la version affichée dans « Applications
/// installées » (entrée créée par l'installateur). `true` si l'entrée a été modifiée.
#[cfg(windows)]
pub fn sync_uninstall_entry(folder: &Path, version: &str, key: &str) -> bool {
    use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, REG_SZ, RegSetKeyValueW};
    let (Some(location), Some(current)) = (
        reg_string(key, "InstallLocation"),
        reg_string(key, "DisplayVersion"),
    ) else {
        return false; // installé sans l'installateur (archive ZIP) : rien à faire
    };
    let same_place = match (fs::canonicalize(&location), fs::canonicalize(folder)) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    };
    if !same_place || current == version {
        return false;
    }
    let data = wide(version);
    // SAFETY : chaînes terminées par un zéro ; `data` fait la taille annoncée en octets.
    let rc = unsafe {
        RegSetKeyValueW(
            HKEY_CURRENT_USER,
            wide(key).as_ptr(),
            wide("DisplayVersion").as_ptr(),
            REG_SZ,
            data.as_ptr().cast(),
            (data.len() * 2) as u32,
        )
    };
    rc == 0
}

/// Au démarrage : supprime les restes d'une mise à jour terminée (préparation, script) et met
/// à jour l'entrée de désinstallation. Retire aussi `<dossier>.old` : la nouvelle version
/// démarre, le secours ne sert plus (la 1.6.x le laissait en place indéfiniment).
#[cfg(windows)]
pub fn cleanup_after_update(version: &str) {
    if let Ok(w) = work_dir() {
        let _ = fs::remove_dir_all(w);
    }
    let _ = fs::remove_file(std::env::temp_dir().join("debarras_update.ps1"));
    if let Some(dir) = install_dir() {
        let old = dir.with_file_name(format!(
            "{}.old",
            dir.file_name().unwrap_or_default().to_string_lossy()
        ));
        // Seulement le secours laissé par le script : un dossier qui contient Debarras.exe.
        if old.join(EXE_NAME).is_file() {
            let _ = fs::remove_dir_all(old);
        }
        sync_uninstall_entry(&dir, version, UNINSTALL_KEY);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    /// Routes du faux GitHub : chemin → (code, corps).
    type Routes = Arc<Mutex<HashMap<String, (u16, Vec<u8>)>>>;

    /// Faux GitHub local. Aucun accès réseau.
    /// Faux GitHub local : routes → (code, corps). Aucun accès réseau.
    struct Github {
        base: String,
        routes: Routes,
    }

    impl Github {
        fn start() -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let base = format!("http://{}", listener.local_addr().unwrap());
            let routes: Routes = Arc::default();
            let r = routes.clone();
            std::thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let r = r.clone();
                    std::thread::spawn(move || {
                        let mut stream = stream;
                        let mut buf = [0u8; 4096];
                        let n = stream.read(&mut buf).unwrap_or(0);
                        let req = String::from_utf8_lossy(&buf[..n]);
                        let path = req.split_whitespace().nth(1).unwrap_or("/").to_string();
                        let (code, body) = r
                            .lock()
                            .unwrap()
                            .get(&path)
                            .cloned()
                            .unwrap_or((404, b"{}".to_vec()));
                        let head = format!(
                            "HTTP/1.1 {code} X\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body.len()
                        );
                        let _ = stream.write_all(head.as_bytes());
                        let _ = stream.write_all(&body);
                    });
                }
            });
            Github { base, routes }
        }

        fn publish(
            &self,
            version: &str,
            payload: &[u8],
            digest: Option<&str>,
            sidecar: bool,
            name: Option<&str>,
        ) {
            let name = name.map_or_else(|| format!("Debarras-{version}-win64.zip"), str::to_string);
            let sha = hex(&Sha256::digest(payload));
            let mut asset = serde_json::json!({"name": name, "size": payload.len(), "browser_download_url": format!("{}/dl/{name}", self.base)});
            match digest {
                Some("auto") => asset["digest"] = format!("sha256:{sha}").into(),
                Some(d) => asset["digest"] = d.into(),
                None => {}
            }
            let mut routes = self.routes.lock().unwrap();
            routes.insert(format!("/dl/{name}"), (200, payload.to_vec()));
            let mut assets = vec![asset];
            if sidecar {
                assets.push(
                    serde_json::json!({"name": format!("{name}.sha256"), "size": 64,
                    "browser_download_url": format!("{}/dl/{name}.sha256", self.base)}),
                );
                routes.insert(
                    format!("/dl/{name}.sha256"),
                    (200, format!("{sha}  {name}\n").into_bytes()),
                );
            }
            let release = serde_json::json!({"tag_name": format!("v{version}"), "body": "## Nouveautés\n- tout",
                "html_url": format!("{}/page", self.base), "assets": assets});
            routes.insert(
                "/repos/me/app/releases/latest".into(),
                (200, release.to_string().into_bytes()),
            );
        }
    }

    fn make_zip(files: &[(&str, &[u8])]) -> Vec<u8> {
        let mut buf = io::Cursor::new(Vec::new());
        let mut z = zip::ZipWriter::new(&mut buf);
        for (name, data) in files {
            z.start_file(*name, zip::write::SimpleFileOptions::default())
                .unwrap();
            z.write_all(data).unwrap();
        }
        z.finish().unwrap();
        buf.into_inner()
    }

    #[test]
    fn versions() {
        assert_eq!(parse_version("v1.10.2").unwrap(), [1, 10, 2]);
        assert_eq!(
            parse_version("1.3").unwrap(),
            parse_version("1.3.0").unwrap()
        );
        assert_eq!(parse_version("v2.0.0-beta").unwrap(), [2]);
        assert!(parse_version("beta").is_err());
        assert!(
            is_newer("1.10.0", "1.9.9")
                && is_newer("v1.3.1", "1.3.0")
                && is_newer("2.0.0", "1.6.3")
        );
        assert!(!is_newer("1.3.0", "1.3.0") && !is_newer("1.2.9", "1.3.0"));
        assert_eq!(asset_version("Debarras-2.0.0-win64.zip"), Some("2.0.0"));
        assert_eq!(asset_version("debarras-2.0-WIN64.ZIP"), Some("2.0"));
        assert_eq!(asset_version("Debarras-2.0.0-source.zip"), None);
        assert_eq!(asset_version("Debarras--win64.zip"), None);
    }

    #[test]
    fn only_local_test_api_is_accepted() {
        // Pas de variable dans cet environnement de test : l'adresse officielle.
        if std::env::var("DEBARRAS_UPDATE_API").is_err() {
            assert_eq!(api(), "https://api.github.com");
        }
    }

    #[test]
    fn fetch_latest_and_download_verify_integrity() {
        let gh = Github::start();
        let tmp = tempfile::tempdir().unwrap();
        let latest = || fetch_latest("me/app", &gh.base, "1.0.0").unwrap();
        assert_eq!(latest(), None); // aucune release
        gh.publish(
            "9.1.0",
            b"zip",
            Some("auto"),
            false,
            Some("Debarras-9.1.0-source.tar.gz"),
        );
        assert_eq!(latest(), None); // pas d'archive Windows
        gh.publish("9.1.0", b"zip", Some("auto"), false, None);
        let rel = latest().unwrap();
        assert_eq!(
            (rel.version.as_str(), rel.tag.as_str(), rel.asset_size),
            ("9.1.0", "v9.1.0", 3)
        );
        assert_eq!(
            rel.sha256.as_deref(),
            Some(hex(&Sha256::digest(b"zip")).as_str())
        );
        assert!(rel.notes.contains("Nouveautés"));
        // Version déjà installée ou plus ancienne : rien à proposer.
        assert!(check("me/app", &gh.base, "9.1.0").unwrap().is_none());
        assert!(check("me/app", &gh.base, "10.0").unwrap().is_none());
        assert_eq!(
            check("me/app", &gh.base, "9.0.9").unwrap().unwrap().version,
            "9.1.0"
        );

        let payload = make_zip(&[("Debarras/Debarras.exe", b"new")]);
        let dl = |rel: &Release, dir: &str| {
            download(rel, &tmp.path().join(dir), "1.0.0", &mut |_, _| {}, &|| {
                false
            })
        };
        gh.publish("9.2.0", &payload, None, true, None); // empreinte via fichier .sha256
        let rel = latest().unwrap();
        assert!(rel.sha256.is_none() && rel.sha256_url.is_some());
        assert_eq!(fs::read(dl(&rel, "dl").unwrap()).unwrap(), payload);
        gh.publish(
            "9.2.1",
            &payload,
            Some(&format!("sha256:{}", "0".repeat(64))),
            false,
            None,
        ); // empreinte fausse
        assert!(
            dl(&latest().unwrap(), "dl2")
                .unwrap_err()
                .0
                .contains("SHA-256 incorrecte")
        );
        gh.publish("9.2.2", &payload, None, false, None); // aucune empreinte : refusé
        assert!(
            dl(&latest().unwrap(), "dl3")
                .unwrap_err()
                .0
                .contains("absente")
        );
        let mut bad = rel.clone();
        bad.asset_size += 5;
        bad.sha256 = Some(hex(&Sha256::digest(&payload)));
        assert!(dl(&bad, "dl4").unwrap_err().0.contains("taille inattendue"));
    }

    #[test]
    fn stage_accepts_expected_layouts_and_rejects_zip_slip() {
        let tmp = tempfile::tempdir().unwrap();
        let layouts: [&[(&str, &[u8])]; 2] = [
            &[
                ("Debarras/Debarras.exe", b"x"),
                ("Debarras/_internal/a.dll", b"y"),
            ],
            &[("Debarras.exe", b"x"), ("_internal/a.dll", b"y")],
        ];
        for (i, files) in layouts.iter().enumerate() {
            let z = tmp.path().join(format!("ok{i}.zip"));
            fs::write(&z, make_zip(files)).unwrap();
            let staged = stage(&z, &tmp.path().join(format!("st{i}"))).unwrap();
            assert_eq!(fs::read(staged.join(EXE_NAME)).unwrap(), b"x");
            assert!(staged.join(r"_internal\a.dll").exists());
        }
        let evil = tmp.path().join("evil.zip");
        fs::write(
            &evil,
            make_zip(&[("../../pwned.txt", b"!"), ("Debarras.exe", b"x")]),
        )
        .unwrap();
        assert!(
            stage(&evil, &tmp.path().join("st_evil"))
                .unwrap_err()
                .0
                .contains("suspect")
        );
        assert!(!tmp.path().parent().unwrap().join("pwned.txt").exists());
        let empty = tmp.path().join("empty.zip");
        fs::write(&empty, make_zip(&[("readme.txt", b"?")])).unwrap();
        assert!(
            stage(&empty, &tmp.path().join("st_empty"))
                .unwrap_err()
                .0
                .contains("introuvable")
        );
    }

    #[cfg(windows)]
    #[test]
    fn swap_script_replaces_folder_and_relaunches() {
        let tmp = tempfile::tempdir().unwrap();
        let t = PathBuf::from(crate::paths::long_path(&tmp.path().to_string_lossy()));
        let install = t.join("Mon appli (test)"); // espaces et parenthèses dans le chemin
        fs::create_dir(&install).unwrap();
        fs::write(install.join("version.txt"), "old").unwrap();
        fs::write(install.join("unins000.exe"), "uninstaller").unwrap(); // posé par l'installateur
        let staged = t.join("staged");
        fs::create_dir(&staged).unwrap();
        fs::write(staged.join("version.txt"), "new").unwrap();
        let marker = t.join("relaunched.txt");
        // L'« exe » relancé est ici un .cmd qui laisse une trace.
        fs::write(
            staged.join("run.cmd"),
            format!("@echo relance> \"{}\"\r\n", marker.display()),
        )
        .unwrap();
        let mut dead = std::process::Command::new("cmd")
            .args(["/c", "exit"])
            .spawn()
            .unwrap();
        dead.wait().unwrap(); // processus déjà terminé : le script n'attend pas
        let started = t.join("started.flag");
        let script = write_swap_script(
            dead.id(),
            &install,
            &staged,
            "run.cmd",
            &t,
            Some(&started),
            true,
        )
        .unwrap();
        let ok = std::process::Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
            .arg(&script)
            .status()
            .unwrap();
        assert!(ok.success());
        assert!(wait_started(&marker, Duration::from_secs(5), &mut || {})); // Start-Process est asynchrone
        assert_eq!(
            fs::read_to_string(install.join("version.txt")).unwrap(),
            "new"
        );
        assert_eq!(
            fs::read_to_string(install.join("unins000.exe")).unwrap(),
            "uninstaller"
        ); // conservé
        assert_eq!(
            fs::read_to_string(t.join(r"Mon appli (test).old\version.txt")).unwrap(),
            "old"
        );
        assert!(!staged.exists() && started.exists());
    }

    #[cfg(windows)]
    #[test]
    fn launch_swap_runs_in_background() {
        let tmp = tempfile::tempdir().unwrap();
        let marker = tmp.path().join("ran.flag");
        let script = tmp.path().join("probe.ps1");
        let text = format!(
            "Start-Sleep -Milliseconds 300; New-Item -ItemType File -Path '{}' | Out-Null\n",
            marker.display()
        );
        fs::write(
            &script,
            [b"\xEF\xBB\xBF".as_slice(), text.as_bytes()].concat(),
        )
        .unwrap();
        launch_swap(&script).unwrap();
        assert!(wait_started(&marker, Duration::from_secs(20), &mut || {}));
    }

    #[test]
    fn swap_script_without_relaunch() {
        let tmp = tempfile::tempdir().unwrap();
        let script = write_swap_script(
            1,
            &tmp.path().join("app"),
            &tmp.path().join("staged"),
            EXE_NAME,
            tmp.path(),
            None,
            false,
        )
        .unwrap();
        let text = fs::read_to_string(script).unwrap();
        assert!(
            text.starts_with('\u{feff}')
                && !text.contains("Start-Process")
                && text.contains("Move-Item")
        );
    }

    #[cfg(windows)]
    #[test]
    fn sync_uninstall_entry_updates_version() {
        use windows_sys::Win32::System::Registry::{
            HKEY_CURRENT_USER, REG_SZ, RegDeleteTreeW, RegSetKeyValueW,
        };
        let tmp = tempfile::tempdir().unwrap();
        let key = format!(
            r"Software\DebarrasTest\Uninstall_is1_{}",
            std::process::id()
        );
        let set = |name: &str, value: &str| {
            let data = wide(value);
            // SAFETY : chaînes terminées par un zéro ; taille en octets.
            unsafe {
                RegSetKeyValueW(
                    HKEY_CURRENT_USER,
                    wide(&key).as_ptr(),
                    wide(name).as_ptr(),
                    REG_SZ,
                    data.as_ptr().cast(),
                    (data.len() * 2) as u32,
                )
            }
        };
        set("InstallLocation", &tmp.path().to_string_lossy());
        set("DisplayVersion", "1.0.0");
        assert!(sync_uninstall_entry(tmp.path(), "2.0.0", &key));
        assert_eq!(reg_string(&key, "DisplayVersion").as_deref(), Some("2.0.0"));
        assert!(!sync_uninstall_entry(tmp.path(), "2.0.0", &key)); // déjà à jour
        assert!(!sync_uninstall_entry(
            &tmp.path().join("ailleurs"),
            "3.0.0",
            &key
        )); // autre dossier
        assert!(!sync_uninstall_entry(
            tmp.path(),
            "3.0.0",
            r"Software\DebarrasTest\Absente"
        ));
        // SAFETY : chaîne terminée par un zéro.
        unsafe { RegDeleteTreeW(HKEY_CURRENT_USER, wide(&key).as_ptr()) };
    }
}
