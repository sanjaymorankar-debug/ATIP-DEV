"""
Database backend abstraction (W9, DBS-05 foundation).

    database_url()      ATIP_DATABASE_URL / DATABASE_URL, else sqlite:///<db.schema.DB_PATH>
    backend()           "sqlite" | "postgresql"
    connect(url=None)   a DB-API connection with ATIP's sqlite3-style interface:
                        execute(sql with ? placeholders, params) -> cursor with fetchone /
                        fetchall; rows readable by index and by column name; commit /
                        rollback / close. PostgreSQL needs `psycopg` (v3), which is NOT
                        installed -- connect() says so instead of failing obscurely.
    Pool(url, size)     a small thread-safe connection pool (PostgreSQL); SQLite connections
                        are cheap and stay per-call (db.schema.get_connection)
    translate(sql)      SQLite -> PostgreSQL for the constructs ATIP's DDL / DML use
    compatibility_report(root)  SQLite-specific SQL still in the code base

THE RUNTIME STAYS ON SQLITE. db.schema.get_connection() is unchanged; ops/config
validation reports an error if a postgresql URL is configured for the runtime,
because ~hundreds of queries still use SQLite-only functions (the report counts them).
PostgreSQL is used in W9 only by tools/sqlite_to_postgres.py against COPIES.
"""

from __future__ import annotations

import os
import queue
import re
import threading
from pathlib import Path


def database_url() -> str:
    u = os.environ.get("ATIP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if u:
        return u
    from db.schema import DB_PATH
    return f"sqlite:///{Path(DB_PATH).as_posix()}"


def backend(url: str | None = None) -> str:
    u = (url or database_url()).lower()
    if u.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        return "postgresql"
    if u.startswith("sqlite:"):
        return "sqlite"
    raise ValueError("unsupported database URL scheme (sqlite:/// or postgresql://)")


def masked_url(url: str | None = None) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url or database_url())


# -- SQL translation -----------------------------------------------------------------------

_RULES = [
    (re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT", re.I), "BIGSERIAL PRIMARY KEY"),
    (re.compile(r"\bREAL\b", re.I), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.I), "BYTEA"),
    (re.compile(r"\bINSERT OR IGNORE INTO\b", re.I), "INSERT INTO"),
    (re.compile(r"datetime\('now'\)", re.I), "CURRENT_TIMESTAMP"),
]
SQLITE_ONLY = {
    "PRAGMA": re.compile(r"\bPRAGMA\b"),
    "INSERT OR IGNORE": re.compile(r"INSERT OR IGNORE", re.I),
    "INSERT OR REPLACE": re.compile(r"INSERT OR REPLACE", re.I),
    "sqlite_master": re.compile(r"sqlite_master"),
    "DATE(x, modifier)": re.compile(r"\bDATE\([^)]*,\s*'[+-]", re.I),
    "datetime()/strftime()": re.compile(r"\b(datetime|strftime|julianday)\(", re.I),
    "AUTOINCREMENT": re.compile(r"AUTOINCREMENT", re.I),
}


def translate(sql: str) -> str:
    out = sql
    ignore = bool(re.search(r"\bINSERT OR IGNORE INTO\b", sql, re.I))
    for rx, rep in _RULES:
        out = rx.sub(rep, out)
    if ignore and "ON CONFLICT" not in out.upper():
        out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return _placeholders(out)


def _placeholders(sql: str) -> str:
    """? -> %s outside string literals."""
    res, q = [], False
    for ch in sql:
        if ch == "'":
            q = not q
            res.append(ch)
        elif ch == "?" and not q:
            res.append("%s")
        elif ch == "%" and not q:
            res.append("%%")
        else:
            res.append(ch)
    return "".join(res)


def compatibility_report(root=".") -> dict:
    counts = {k: 0 for k in SQLITE_ONLY}
    files = {}
    for p in Path(root).rglob("*.py"):
        s = str(p).replace("\\", "/")
        if "/tests/" in s or "site-packages" in s:
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for k, rx in SQLITE_ONLY.items():
            n = len(rx.findall(t))
            if n:
                counts[k] += n
                files[s.split("/")[-2] + "/" + p.name] = files.get(s.split("/")[-2] + "/" + p.name, 0) + n
    return {"sqlite_only_constructs": counts, "total": sum(counts.values()),
            "top_files": sorted(files.items(), key=lambda x: -x[1])[:25]}


# -- PostgreSQL connection wrapper ---------------------------------------------------------

class _Row(tuple):
    """Tuple row that also answers row['column'] and keys(), like sqlite3.Row."""
    def __new__(cls, values, names):
        r = super().__new__(cls, values)
        r._names = names
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._names.index(k))
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


class _Cursor:
    def __init__(self, cur):
        self._c = cur

    def _wrap(self, r):
        if r is None:
            return None
        names = [d[0] for d in (self._c.description or [])]
        return _Row(r, names)

    def fetchone(self):
        return self._wrap(self._c.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._c.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._c.rowcount


class PgConnection:
    def __init__(self, url):
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError("PostgreSQL support needs the 'psycopg' package (not installed; install it in the "
                               "environment that will run the migration)") from e
        self._c = psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"))
        self.row_factory = None

    def execute(self, sql, params=()):
        cur = self._c.cursor()
        cur.execute(translate(sql), tuple(params))
        return _Cursor(cur)

    def executemany(self, sql, seq):
        cur = self._c.cursor()
        cur.executemany(translate(sql), [tuple(p) for p in seq])
        return _Cursor(cur)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()


def connect(url: str | None = None):
    url = url or database_url()
    if backend(url) == "postgresql":
        return PgConnection(url)
    import sqlite3
    path = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else url.split("sqlite:", 1)[1]
    c = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    return c


class Pool:
    """Fixed-size pool. get() blocks up to `timeout` s; put() returns a connection."""
    def __init__(self, url=None, size=5, timeout=30):
        self.url, self.size, self.timeout = url or database_url(), size, timeout
        self._q, self._n, self._lock = queue.Queue(), 0, threading.Lock()

    def get(self):
        try:
            return self._q.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._n < self.size:
                    self._n += 1
                    return connect(self.url)
        return self._q.get(timeout=self.timeout)

    def put(self, conn):
        try:
            conn.rollback()
        except Exception:
            pass
        self._q.put(conn)

    def close_all(self):
        while not self._q.empty():
            try:
                self._q.get_nowait().close()
            except Exception:
                pass
        self._n = 0
