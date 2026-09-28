"""Recherche de fichiers dans le cache : nom (jokers * ?), type, extension, taille, date."""
from __future__ import annotations

import fnmatch
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field

from utils.filetypes import OTHER, extensions

MAX_RESULTS = 5000


@dataclass
class SearchQuery:
    text: str = ""                    # sous-chaîne, ou motif avec * et ?
    category: str | None = None       # catégorie de utils.filetypes
    exts: list[str] = field(default_factory=list)  # ['.pdf', '.docx']
    min_size: int = 0
    max_size: int = 0                 # 0 = pas de limite
    newer_than_days: int = 0          # modifiés depuis moins de N jours
    older_than_days: int = 0          # modifiés il y a plus de N jours

    def is_empty(self) -> bool:
        return not (self.text.strip() or self.category or self.exts or self.min_size or self.max_size
                    or self.newer_than_days or self.older_than_days)


@dataclass(frozen=True, slots=True)
class Hit:
    path: str
    name: str
    size: int
    mtime: float


@dataclass
class SearchResult:
    hits: list[Hit]
    count: int        # nombre total de correspondances (hits est tronqué à MAX_RESULTS)
    total_size: int


def _name_regex(text: str) -> re.Pattern[str]:
    text = text.strip()
    if "*" in text or "?" in text:
        return re.compile(fnmatch.translate(text), re.IGNORECASE)  # motif sur le nom entier
    return re.compile(re.escape(text), re.IGNORECASE)            # sous-chaîne


def search_files(conn: sqlite3.Connection, root: str, q: SearchQuery,
                 limit: int = MAX_RESULTS) -> SearchResult:
    lo = root if root.endswith(os.sep) else root + os.sep
    hi = lo[:-1] + chr(ord(lo[-1]) + 1)
    where, args = ["path >= ?", "path < ?"], [lo, hi]

    if q.text.strip():
        rx = _name_regex(q.text)
        # Fonction Python : insensible à la casse, accents compris (LIKE ne gère que l'ASCII).
        conn.create_function("name_match", 1, lambda n: rx.search(n) is not None, deterministic=True)
        where.append("name_match(name)")
    if q.category:
        exts = extensions(q.category)
        marks = ",".join("?" * len(exts))
        where.append(f"ext {'NOT IN' if q.category == OTHER else 'IN'} ({marks})")
        args += exts
    if q.exts:
        exts = [e.lower() if e.startswith(".") else "." + e.lower() for e in q.exts]
        where.append(f"ext IN ({','.join('?' * len(exts))})")
        args += exts
    if q.min_size:
        where.append("size >= ?")
        args.append(q.min_size)
    if q.max_size:
        where.append("size <= ?")
        args.append(q.max_size)
    now = time.time()
    if q.newer_than_days:
        where.append("mtime >= ?")
        args.append(now - q.newer_than_days * 86400)
    if q.older_than_days:
        where.append("mtime < ?")
        args.append(now - q.older_than_days * 86400)

    cond = " AND ".join(where)
    count, total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE {cond}",
                                args).fetchone()
    hits = [Hit(*r) for r in conn.execute(
        f"SELECT path, name, size, mtime FROM files WHERE {cond} ORDER BY size DESC LIMIT {int(limit)}",
        args)]
    return SearchResult(hits, count, total)
