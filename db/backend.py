"""
Database backend abstraction (W9, DBS-05 foundation).

    database_url()      ATIP_DATABASE_URL / DATABASE_URL, else sqlite:///<db.schema.DB_PATH>
    backend()           "sqlite" | "postgresql"
    connect(url=None)   a DB-API connection with ATIP's sqlite3-style interface:
                        execute(sql with ? placeholders, params) -> cursor with fetchone /
                        fetchall; rows readable by index and by column name; commit /
                        rollback / close. PostgreSQL needs `psycopg` (v3), not installed by
                        default -- connect() says so instead of failing obscurely.
    Pool(url, size)     a small thread-safe connection pool (PostgreSQL); SQLite
                        connections are cheap and stay per-call (db.schema.get_connection)
    translate(sql)      SQLite -> PostgreSQL, for the constructs ATIP's DDL / DML use
                        (db/postgres.py)
    compatibility_report(root)  SQLite-specific SQL still in the code base

PostgreSQL is ATIP's one server database target (2026-10-09: the MySQL backend was
removed; a mysql:// URL is refused). THE RUNTIME STAYS ON SQLITE by default:
db.schema.get_connection() moves to PostgreSQL only behind db.schema.pg_runtime_url()'s
gates, and ops/config validation reports an error if a postgresql URL is configured
without them, because some queries still use SQLite-only constructs
(python -m db.dialect_scan --details lists them).
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


# The MySQL backend (db/mysql.py) was removed when PostgreSQL became the one server
# target; a URL in one of these schemes gets an error that says so.
RETIRED_SCHEMES = ("mysql://", "mysql+pymysql://", "mariadb://")


def backend(url: str | None = None) -> str:
    u = (url or database_url()).lower()
    if u.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        return "postgresql"
    if u.startswith("sqlite:"):
        return "sqlite"
    if u.startswith(RETIRED_SCHEMES):
        raise ValueError("MySQL / MariaDB is no longer supported: PostgreSQL is ATIP's server database "
                         "(postgresql://...; docs/POSTGRESQL_MIGRATION.md)")
    raise ValueError("unsupported database URL scheme (sqlite:/// or postgresql://)")


def masked_url(url: str | None = None) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url or database_url())


# -- SQL translation (W38: db/postgres.py; the W9 regex rules are superseded) ---------------

def translate(sql: str, pk_of=None) -> str:
    """ddl() for CREATE / ALTER, translate() for everything else (db/postgres.py)."""
    from db import postgres
    if sql.lstrip().upper().startswith(("CREATE ", "ALTER ")):
        return postgres.ddl(sql)
    return postgres.translate(sql, pk_of)


def compatibility_report(root=".") -> dict:
    """W38: the AST-based scan of every SQL statement (db/dialect_scan.py)."""
    from db.dialect_scan import scan
    r = scan(root)
    return {"sqlite_only_constructs": r["by_kind"], "total": r["needs_change"], "statements": r["statements"],
            "pct_ready": r["pct_ready"], "top_files": list(r["by_file"].items())}


# -- PostgreSQL connection -------------------------------------------------------------------
def PgConnection(url):
    """W38: the full wrapper lives in db.postgres (unique-key-aware upserts, rows by name)."""
    from db.postgres import PgConnection as _Pg
    return _Pg(url.replace("postgresql+psycopg://", "postgresql://"))


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
