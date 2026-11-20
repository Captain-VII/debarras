//! Compare le cœur Rust aux sorties de référence de la version Python (`tests/golden/`,
//! produites par `tools/make_golden.py` de la 1.6.3, retiré avec la 2.0 : voir l'historique git) :
//! même faux disque, monté sur une lettre libre.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::AtomicBool;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use debarras_core::cache::Cache;
use debarras_core::cleanup::{Target, auto_selection, browser_targets, temp_target};
use debarras_core::duplicates::find_duplicates;
use debarras_core::safety::{Env, Level, analyze};
use debarras_core::samename::find_same_names;
use debarras_core::scanner::{ScanOptions, scan};
use debarras_core::search::{MAX_RESULTS, SearchQuery, search_files};
use debarras_core::similar::find_similar;
use debarras_core::stats::compute_stats;
use serde_json::{Value, json};

fn golden_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("tests")
        .join("golden")
}

fn golden(name: &str) -> Value {
    let text = fs::read_to_string(golden_dir().join(format!("{name}.json"))).unwrap();
    serde_json::from_str(&text).unwrap()
}

/// Faux disque extrait de `fixture.zip` et monté par `subst` ; démonté à la fin.
struct Fixture {
    letter: String,
    _dir: tempfile::TempDir,
}

impl Fixture {
    fn mount() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let disk = dir.path().join("disque");
        let recent: Vec<String> =
            serde_json::from_value(golden("fixture")["recent"].clone()).unwrap();
        // Python : 2020-01-01 12:00 heure locale. Seul l'écart avec « maintenant » compte
        // (fichiers anciens ou récents), donc 12:00 UTC convient partout.
        let old = UNIX_EPOCH + Duration::from_secs(1_577_880_000);
        let now = SystemTime::now() - Duration::from_secs(3600);
        let mut zip =
            zip::ZipArchive::new(fs::File::open(golden_dir().join("fixture.zip")).unwrap())
                .unwrap();
        for i in 0..zip.len() {
            let mut f = zip.by_index(i).unwrap();
            let rel = f.name().replace('/', "\\");
            let path = disk.join(&rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let mut out = fs::File::create(&path).unwrap();
            std::io::copy(&mut f, &mut out).unwrap();
            let t = if recent.contains(&rel) { now } else { old };
            out.set_times(fs::FileTimes::new().set_modified(t).set_accessed(t))
                .unwrap();
        }
        let letter = "XYWVUT"
            .chars()
            .map(|c| format!("{c}:"))
            .find(|l| !Path::new(&format!("{l}\\")).exists())
            .expect("aucune lettre libre");
        assert!(
            Command::new("subst")
                .arg(&letter)
                .arg(&disk)
                .status()
                .unwrap()
                .success()
        );
        Fixture { letter, _dir: dir }
    }

    /// Chemin sans la lettre, comme dans les JSON (`X:\Users` → `\Users`).
    fn rel(&self, path: &str) -> String {
        let rest = &path[self.letter.len()..];
        if rest.is_empty() {
            "\\".into()
        } else {
            rest.into()
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = Command::new("subst").arg(&self.letter).arg("/D").status();
    }
}

/// Un seul test : le faux disque est monté une fois (deux montages en parallèle se
/// disputeraient la même lettre).
#[test]
fn core_matches_python() {
    let fx = Fixture::mount();
    let db = fx._dir.path().join("cache.db");
    let scan_id = check_scan(&fx, &db);
    check_safety(&fx, &db, scan_id);
    // Même ordre que make_golden.py : « même nom » réutilise les empreintes des doublons.
    let cache = Cache::open(&db).unwrap();
    check_duplicates(&fx, &cache);
    check_samename(&fx, &cache);
    check_similar(&fx, &cache);
    check_cleanup(&fx);
    check_stats_and_search(&fx, &cache, scan_id);
}

fn check_scan(fx: &Fixture, db: &Path) -> i64 {
    let root = format!("{}\\", fx.letter);
    let opts = ScanOptions {
        excluded_paths: vec![],
        excluded_names: vec![],
        ignore_patterns: vec![],
    };
    let r = scan(&root, db, &opts, &AtomicBool::new(false), |_, _, _| {}).unwrap();

    let want = golden("scan");
    let got_result = json!({
        "cancelled": r.cancelled, "dir_count": r.dir_count, "errors": r.errors,
        "file_count": r.file_count, "root": fx.rel(&r.root), "scan_id": r.scan_id,
        "total_size": r.total_size,
    });
    assert_eq!(got_result, want["result"]);

    let conn = rusqlite::Connection::open(db).unwrap();
    let rows = |sql: &str, cols: usize| -> Value {
        let mut st = conn.prepare(sql).unwrap();
        let mut out: Vec<Value> = st
            .query_map([], |row| {
                let path: String = row.get(0)?;
                let mut v = vec![json!(fx.rel(&path))];
                for i in 1..cols {
                    v.push(json!(row.get::<_, i64>(i)?));
                }
                Ok(Value::Array(v))
            })
            .unwrap()
            .map(Result::unwrap)
            .collect();
        out.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
        Value::Array(out)
    };
    let dirs = format!(
        "SELECT path, size, file_count FROM dir_sizes WHERE scan_id={}",
        r.scan_id
    );
    assert_eq!(rows(&dirs, 3), want["dirs"]);
    assert_eq!(rows("SELECT path, size FROM file_paths", 2), want["files"]);
    r.scan_id
}

/// Environnement du faux PC, comme le règle `make_golden.py`.
fn fixture_env(fx: &Fixture) -> Env {
    let l = &fx.letter;
    let u = format!(r"{l}\Users\Alice");
    Env {
        system_root: format!(r"{l}\Windows"),
        program_data: format!(r"{l}\ProgramData"),
        program_files: [
            format!(r"{l}\Program Files"),
            format!(r"{l}\Program Files (x86)"),
            format!(r"{l}\Program Files"),
        ],
        home: u.clone(),
        local_appdata: Some(format!(r"{u}\AppData\Local")),
        temp: Some(format!(r"{u}\AppData\Local\Temp")),
    }
}

fn check_safety(fx: &Fixture, db: &Path, scan_id: i64) {
    let l = &fx.letter;
    let u = format!(r"{l}\Users\Alice");
    let env = fixture_env(fx);
    let root = format!("{l}\\");
    let app_dir = format!(r"{u}\AppData\Local\Programs\Debarras");
    let (clf, summary) = analyze(
        db,
        scan_id,
        &root,
        Some(&app_dir),
        &env,
        &AtomicBool::new(false),
    )
    .unwrap()
    .unwrap();
    let want = golden("safety");
    // Les raisons citent le dossier tel quel (« Dossier de Windows (X:\Windows) ») ; les
    // sorties Python ont été produites sur X:.
    let reason = |r: &str| r.replace(l.as_str(), "X:");
    let sorted = |xs: Vec<String>| {
        let mut v: Vec<String> = xs.iter().map(|x| fx.rel(x)).collect();
        v.sort();
        json!(v)
    };

    assert_eq!(
        sorted(clf.software_dirs.values().cloned().collect()),
        want["software_dirs"]
    );
    assert_eq!(
        sorted(clf.containers.iter().cloned().collect()),
        want["containers"]
    );

    let conn = rusqlite::Connection::open(db).unwrap();
    let paths = |sql: &str| -> Vec<String> {
        conn.prepare(sql)
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect()
    };
    let dirs = paths(&format!(
        "SELECT path FROM dir_sizes WHERE scan_id={scan_id} ORDER BY path"
    ));
    let files = paths("SELECT path FROM file_paths ORDER BY path");
    let items: Vec<Value> = dirs
        .iter()
        .map(|p| (p, true))
        .chain(files.iter().map(|p| (p, false)))
        .map(|(p, is_dir)| {
            let v = clf.classify(p, Some(is_dir));
            json!({"dir": is_dir, "level": v.level.as_str(), "path": fx.rel(p), "reason": reason(&v.reason)})
        })
        .collect();
    let want_items = want["items"].as_array().unwrap();
    assert_eq!(items.len(), want_items.len());
    for (got, exp) in items.iter().zip(want_items) {
        assert_eq!(got, exp);
    }

    // Synthèse. À taille égale, l'ordre Python dépend de l'ordre en base : on compare trié.
    let by_size = |mut v: Vec<Value>| {
        v.sort_by(|a, b| {
            b[1].as_i64()
                .cmp(&a[1].as_i64())
                .then_with(|| a[0].as_str().cmp(&b[0].as_str()))
        });
        Value::Array(v)
    };
    let mut totals = serde_json::Map::new();
    let mut top = serde_json::Map::new();
    for lv in Level::ALL {
        totals.insert(lv.as_str().into(), json!(summary.totals[&lv]));
        let got = summary.top[&lv]
            .iter()
            .map(|(p, s, r)| json!([fx.rel(p), s, reason(r)]))
            .collect();
        top.insert(lv.as_str().into(), by_size(got));
    }
    let ws = &want["summary"];
    assert_eq!(Value::Object(totals), ws["totals"]);
    for lv in Level::ALL {
        let exp = by_size(ws["top"][lv.as_str()].as_array().unwrap().clone());
        assert_eq!(top[lv.as_str()], exp, "top {}", lv.as_str());
    }
    assert_eq!(sorted(summary.software.clone()), ws["software"]);
    assert_eq!(json!(summary.scan_id), ws["scan_id"]);
}

fn no_progress() -> impl FnMut(&str, u64, u64) {
    |_, _, _| {}
}

fn check_duplicates(fx: &Fixture, cache: &Cache) {
    let root = format!("{}\\", fx.letter);
    let r = find_duplicates(cache, &root, 1, &AtomicBool::new(false), &mut no_progress()).unwrap();
    let got = json!({
        "cancelled": r.cancelled, "candidates": r.candidates, "hashed_bytes": r.hashed_bytes,
        "min_size": r.min_size, "root": fx.rel(&r.root), "skipped_changed": r.skipped_changed,
        "skipped_cloud": r.skipped_cloud, "skipped_error": r.skipped_error,
        "groups": r.groups.iter().map(|g| json!({
            "digest": g.digest, "size": g.size,
            "files": g.files.iter().map(|f| json!({"path": fx.rel(&f.path), "size": f.size})).collect::<Vec<_>>(),
        })).collect::<Vec<_>>(),
    });
    assert_eq!(got, golden("duplicates"));
}

fn check_samename(fx: &Fixture, cache: &Cache) {
    let root = format!("{}\\", fx.letter);
    let r = find_same_names(
        cache,
        &root,
        1024,
        true,
        true,
        &AtomicBool::new(false),
        &mut no_progress(),
    )
    .unwrap();
    let got = json!({
        "cancelled": r.cancelled, "candidates": r.candidates, "hashed_bytes": r.hashed_bytes,
        "root": fx.rel(&r.root), "skipped_changed": r.skipped_changed, "skipped_cloud": r.skipped_cloud,
        "skipped_error": r.skipped_error, "skipped_generic": r.skipped_generic,
        "groups": r.groups.iter().map(|g| json!({
            "name": g.name,
            "files": g.files.iter().map(|f| json!({"path": fx.rel(&f.path), "size": f.size, "version": f.version})).collect::<Vec<_>>(),
        })).collect::<Vec<_>>(),
    });
    assert_eq!(got, golden("samename"));
}

/// Les empreintes elles-mêmes diffèrent un peu de Pillow (migration v4) : on compare les
/// groupes, leurs fichiers dans l'ordre et leurs dimensions, pas les valeurs d'empreinte.
fn check_similar(fx: &Fixture, cache: &Cache) {
    let root = format!("{}\\", fx.letter);
    let r = find_similar(
        cache,
        &root,
        1,
        5,
        &AtomicBool::new(false),
        &mut no_progress(),
    )
    .unwrap();
    let view = |v: &Value| -> Value {
        json!({
            "cancelled": v["cancelled"], "candidates": v["candidates"], "hashed": v["hashed"],
            "root": v["root"], "skipped_changed": v["skipped_changed"], "skipped_cloud": v["skipped_cloud"],
            "skipped_error": v["skipped_error"], "threshold": v["threshold"],
            "groups": v["groups"].as_array().unwrap().iter().map(|g| g["files"].as_array().unwrap().iter()
                .map(|f| json!([f["path"], f["size"], f["width"], f["height"]])).collect::<Vec<_>>()).collect::<Vec<_>>(),
        })
    };
    let got = json!({
        "cancelled": r.cancelled, "candidates": r.candidates, "hashed": r.hashed, "root": fx.rel(&r.root),
        "skipped_changed": r.skipped_changed, "skipped_cloud": r.skipped_cloud, "skipped_error": r.skipped_error,
        "threshold": r.threshold,
        "groups": r.groups.iter().map(|g| json!({"files": g.files.iter().map(|f| json!({
            "path": fx.rel(&f.path), "size": f.size, "width": f.width, "height": f.height})).collect::<Vec<_>>()}))
            .collect::<Vec<_>>(),
    });
    assert_eq!(view(&got), view(&golden("similar")));
}

fn check_cleanup(fx: &Fixture) {
    let env = fixture_env(fx);
    let no = AtomicBool::new(false);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    let mut targets = vec![temp_target(&env, &no, now)];
    targets.extend(browser_targets(&env, &no, &Default::default()));
    let sizes = |s: &[(String, i64)]| -> Value {
        Value::Object(s.iter().map(|(p, n)| (fx.rel(p), json!(n))).collect())
    };
    let target = |t: &Target| {
        json!({
            "count": t.count, "description": t.description.replace(fx.letter.as_str(), "X:"), "key": t.key,
            "kind": t.kind, "note": t.note, "paths": t.paths.iter().map(|p| fx.rel(p)).collect::<Vec<_>>(),
            "recommended": t.recommended, "size_known": t.size_known, "sizes": sizes(&t.sizes), "title": t.title,
        })
    };
    let (paths, auto_sizes) = auto_selection(&targets);
    let got = json!({
        "targets": targets.iter().map(target).collect::<Vec<_>>(),
        "auto": [paths.iter().map(|p| fx.rel(p)).collect::<Vec<_>>(), sizes(&auto_sizes)],
    });
    assert_eq!(got, golden("cleanup"));
}

/// Listes triées par taille : à taille égale, l'ordre Python dépend de la base ; Rust
/// départage par chemin. On compare donc triées de la même façon.
fn by_size(mut v: Vec<Value>) -> Value {
    v.sort_by(|a, b| {
        b["size"]
            .as_i64()
            .cmp(&a["size"].as_i64())
            .then_with(|| a["path"].as_str().cmp(&b["path"].as_str()))
    });
    Value::Array(v)
}

fn check_stats_and_search(fx: &Fixture, cache: &Cache, scan_id: i64) {
    let root = format!("{}\\", fx.letter);
    let st = compute_stats(&cache.conn, scan_id, &root, 365).unwrap();
    let files = |v: &[debarras_core::stats::FileStat]| {
        by_size(
            v.iter()
                .map(|f| json!({"path": fx.rel(&f.path), "size": f.size}))
                .collect(),
        )
    };
    let got = json!({
        "by_category": Value::Object(st.by_category.iter().map(|(c, s, n)| (c.clone(), json!([s, n]))).collect()),
        "by_ext": st.by_ext.iter().map(|(e, s, n)| json!([e, s, n])).collect::<Vec<_>>(),
        "empty_dirs": st.empty_dirs.iter().map(|p| fx.rel(p)).collect::<Vec<_>>(),
        "file_count": st.file_count, "installers": files(&st.installers), "largest": files(&st.largest),
        "old": files(&st.old), "old_days": st.old_days, "old_total": [st.old_total.0, st.old_total.1],
        "root": fx.rel(&st.root), "scan_id": st.scan_id,
        "temp_dirs": st.temp_dirs.iter().map(|d| json!({"file_count": d.file_count, "path": fx.rel(&d.path), "size": d.size})).collect::<Vec<_>>(),
        "temp_files": files(&st.temp_files), "total_size": st.total_size,
    });
    let mut want = golden("stats");
    for k in ["installers", "largest", "old", "temp_files"] {
        want[k] = by_size(want[k].as_array().unwrap().clone());
    }
    assert_eq!(got, want);

    let queries = [
        (
            "texte_rapport",
            SearchQuery {
                text: "rapport".into(),
                ..Default::default()
            },
        ),
        (
            "motif_jpg",
            SearchQuery {
                text: "*.jpg".into(),
                ..Default::default()
            },
        ),
        (
            "ext_pdf",
            SearchQuery {
                exts: vec![".pdf".into()],
                ..Default::default()
            },
        ),
        (
            "taille_20k_40k",
            SearchQuery {
                min_size: 20000,
                max_size: 40000,
                ..Default::default()
            },
        ),
        (
            "ancien_365j",
            SearchQuery {
                older_than_days: 365,
                ..Default::default()
            },
        ),
    ];
    let mut want = golden("search");
    for (name, q) in queries {
        let r = search_files(&cache.conn, &root, &q, MAX_RESULTS).unwrap();
        let hits = by_size(
            r.hits
                .iter()
                .map(|h| json!({"name": h.name, "path": fx.rel(&h.path), "size": h.size}))
                .collect(),
        );
        want[name]["hits"] = by_size(want[name]["hits"].as_array().unwrap().clone());
        assert_eq!(
            json!({"count": r.count, "hits": hits, "total_size": r.total_size}),
            want[name],
            "{name}"
        );
    }
}
