//! Recherche de fichiers dans le cache : nom (jokers `*` `?`), type, extension, taille, date.
//! Port de `core/search.py`.

use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, Result, params_from_iter, types::Value};

use crate::cache::{now, subtree};
use crate::filetypes::{OTHER, extensions};
use crate::scanner::fnmatch_str;

pub const MAX_RESULTS: usize = 5000;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SearchQuery {
    /// Sous-chaîne, ou motif avec `*` et `?` portant sur le nom entier.
    pub text: String,
    /// Catégorie de `filetypes`.
    pub category: Option<String>,
    /// `[".pdf", "docx"]`.
    pub exts: Vec<String>,
    pub min_size: i64,
    /// 0 = pas de limite.
    pub max_size: i64,
    /// Modifiés depuis moins de N jours.
    pub newer_than_days: i64,
    /// Modifiés il y a plus de N jours.
    pub older_than_days: i64,
}

impl SearchQuery {
    pub fn is_empty(&self) -> bool {
        self.text.trim().is_empty()
            && self.category.is_none()
            && self.exts.is_empty()
            && self.min_size == 0
            && self.max_size == 0
            && self.newer_than_days == 0
            && self.older_than_days == 0
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Hit {
    pub path: String,
    pub name: String,
    pub size: i64,
    pub mtime: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SearchResult {
    pub hits: Vec<Hit>,
    /// Nombre total de correspondances (`hits` est tronqué à `limit`).
    pub count: i64,
    pub total_size: i64,
}

/// `*.ext` seul (sans autre joker ni point) : `Some("ext")`.
fn ext_only(text: &str) -> Option<&str> {
    let ext = text.strip_prefix("*.")?;
    (!ext.is_empty() && !ext.contains(['*', '?', '[', ']', '.'])).then_some(ext)
}

/// Filtre sur le nom. Chemins rapides évalués par SQLite : `*.ext` → colonne `ext` ; texte
/// ASCII → LIKE (insensible à la casse pour l'ASCII). Sinon (accents, classes `[..]`) :
/// fonction `name_match`, sans casse.
fn name_condition(conn: &Connection, text: &str) -> Result<(String, Vec<Value>)> {
    let text = text.trim();
    let wildcard = text.contains(['*', '?']);
    if let Some(ext) = ext_only(text) {
        return Ok((
            "ext = ?".into(),
            vec![format!(".{}", ext.to_lowercase()).into()],
        ));
    }
    if text.is_ascii() && !text.contains('[') {
        let mut like = text
            .replace('!', "!!")
            .replace('%', "!%")
            .replace('_', "!_");
        like = if wildcard {
            like.replace('*', "%").replace('?', "_")
        } else {
            format!("%{like}%")
        };
        return Ok(("name LIKE ? ESCAPE '!'".into(), vec![like.into()]));
    }
    // Comme LIKE : un motif porte sur le nom entier, un texte sans joker sur une partie.
    let pattern = text.to_lowercase();
    conn.create_scalar_function(
        "name_match",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            let name = ctx.get::<String>(0)?.to_lowercase();
            Ok(if wildcard {
                fnmatch_str(&pattern, &name)
            } else {
                name.contains(&pattern)
            })
        },
    )?;
    Ok(("name_match(name)".into(), vec![]))
}

/// Fichiers sous `root` qui répondent à `q`, les plus gros d'abord (à taille égale, par chemin).
pub fn search_files(
    conn: &Connection,
    root: &str,
    q: &SearchQuery,
    limit: usize,
) -> Result<SearchResult> {
    let (scope, sargs) = subtree("parent", root);
    let mut wh = vec![scope];
    let mut args: Vec<Value> = sargs.into_iter().map(Value::from).collect();
    if !q.text.trim().is_empty() {
        let (cond, cargs) = name_condition(conn, &q.text)?;
        wh.push(cond);
        args.extend(cargs);
    }
    let marks = |n: usize| vec!["?"; n].join(",");
    if let Some(cat) = &q.category {
        let exts = extensions(cat);
        let op = if cat == OTHER { "NOT IN" } else { "IN" };
        wh.push(format!("ext {op} ({})", marks(exts.len())));
        args.extend(exts.iter().map(|e| Value::from(e.to_string())));
    }
    if !q.exts.is_empty() {
        wh.push(format!("ext IN ({})", marks(q.exts.len())));
        args.extend(q.exts.iter().map(|e| {
            let e = e.to_lowercase();
            Value::from(if e.starts_with('.') {
                e
            } else {
                format!(".{e}")
            })
        }));
    }
    if q.min_size != 0 {
        wh.push("size >= ?".into());
        args.push(q.min_size.into());
    }
    if q.max_size != 0 {
        wh.push("size <= ?".into());
        args.push(q.max_size.into());
    }
    let now = now();
    if q.newer_than_days != 0 {
        wh.push("mtime >= ?".into());
        args.push((now - q.newer_than_days as f64 * 86400.0).into());
    }
    if q.older_than_days != 0 {
        wh.push("mtime < ?".into());
        args.push((now - q.older_than_days as f64 * 86400.0).into());
    }
    let cond = wh.join(" AND ");
    let (count, total_size) = conn.query_row(
        &format!("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM file_paths WHERE {cond}"),
        params_from_iter(&args),
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let hits = conn
        .prepare(&format!(
            "SELECT path, name, size, mtime FROM file_paths WHERE {cond} ORDER BY size DESC, path LIMIT {limit}"
        ))?
        .query_map(params_from_iter(&args), |r| {
            Ok(Hit { path: r.get(0)?, name: r.get(1)?, size: r.get(2)?, mtime: r.get(3)? })
        })?
        .collect::<Result<_>>()?;
    Ok(SearchResult {
        hits,
        count,
        total_size,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::Cache;
    use crate::testutil::{age, scan_dir, write};

    fn names(conn: &Connection, root: &str, q: SearchQuery) -> Vec<String> {
        let mut v: Vec<String> = search_files(conn, root, &q, MAX_RESULTS)
            .unwrap()
            .hits
            .into_iter()
            .map(|h| h.name)
            .collect();
        v.sort();
        v
    }

    fn text(t: &str) -> SearchQuery {
        SearchQuery {
            text: t.into(),
            ..Default::default()
        }
    }

    #[test]
    fn filters() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        write(&root.join(r"docs\Rapport été 2024.pdf"), 3000);
        age(&root.join(r"docs\Rapport été 2024.pdf"), 400);
        write(&root.join(r"docs\rapport_01.docx"), 500);
        write(&root.join(r"docs\notes.txt"), 20);
        write(&root.join(r"media\vacances.mp4"), 90_000);
        age(&root.join(r"media\vacances.mp4"), 800);
        write(&root.join(r"code\main.py"), 30);
        write(&root.join(r"misc\data.xyz"), 10);
        let db = scan_dir(&root, &tmp.path().join("c.db"));
        let c = Cache::open(&db).unwrap();
        let r = root.to_string_lossy();
        let n = |q| names(&c.conn, &r, q);
        assert_eq!(n(text("ÉTÉ")), ["Rapport été 2024.pdf"]); // casse et accents
        assert_eq!(
            n(text("rapport*")),
            ["Rapport été 2024.pdf", "rapport_01.docx"]
        );
        assert_eq!(n(text("*.py")), ["main.py"]);
        assert_eq!(
            n(SearchQuery {
                category: Some("Vidéos".into()),
                ..Default::default()
            }),
            ["vacances.mp4"]
        );
        assert_eq!(
            n(SearchQuery {
                category: Some("Autres".into()),
                ..Default::default()
            }),
            ["data.xyz"]
        );
        assert_eq!(
            n(SearchQuery {
                exts: vec!["TXT".into(), ".py".into()],
                ..Default::default()
            }),
            ["main.py", "notes.txt"]
        );
        assert_eq!(
            n(SearchQuery {
                min_size: 1000,
                ..Default::default()
            }),
            ["Rapport été 2024.pdf", "vacances.mp4"]
        );
        assert_eq!(
            n(SearchQuery {
                max_size: 25,
                ..Default::default()
            }),
            ["data.xyz", "notes.txt"]
        );
        assert_eq!(
            n(SearchQuery {
                older_than_days: 365,
                ..Default::default()
            }),
            ["Rapport été 2024.pdf", "vacances.mp4"]
        );
        let recent_docs = SearchQuery {
            newer_than_days: 30,
            category: Some("Documents".into()),
            ..Default::default()
        };
        assert_eq!(n(recent_docs), ["notes.txt", "rapport_01.docx"]);
        assert_eq!(n(text("*.PDF")), ["Rapport été 2024.pdf"]); // chemin rapide extension
        assert_eq!(n(text("RAPPORT_0?.docx")), ["rapport_01.docx"]); // joker ASCII via LIKE
        assert_eq!(n(text("*é*")), ["Rapport été 2024.pdf"]); // joker accentué
        let res = search_files(&c.conn, &r, &text("o"), 2).unwrap();
        assert_eq!((res.hits.len(), res.count), (2, 3)); // Rapport…, rapport_01, notes
        assert!(SearchQuery::default().is_empty());
    }

    #[test]
    fn special_characters_are_literal_and_root_is_respected() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        for n in ["100%_fini.txt", "1000_fini.txt", "a!b.txt", "inside.txt"] {
            write(&root.join(n), 1);
        }
        let other = tmp.path().join("data2"); // préfixe commun avec « data »
        write(&other.join("inside2.txt"), 1);
        let db = scan_dir(&root, &tmp.path().join("c.db"));
        scan_dir(&other, &db);
        let c = Cache::open(&db).unwrap();
        let r = root.to_string_lossy();
        let n = |t: &str| names(&c.conn, &r, text(t));
        assert_eq!(n("100%"), ["100%_fini.txt"]); // % n'est pas un joker
        assert_eq!(n("0_f"), ["1000_fini.txt"]); // _ non plus
        assert_eq!(n("a!b"), ["a!b.txt"]);
        assert_eq!(n("*%_*"), ["100%_fini.txt"]);
        assert_eq!(n("inside"), ["inside.txt"]);
    }
}
