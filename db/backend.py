"""
Database backend abstraction (W9, DBS-05 foundation).

    database_url()      ATIP_DATABASE_URL / DATABASE_URL, else sqlite:///<db.schema.DB_PATH>
    backend()           "sqlite" | "postgresql" | "mysql"
    connect(url=None)   a DB-API connection with ATIP's sqlite3-style interface:
                        execute(sql with ? placeholders, params) -> cursor with fetchone /
                        fetchall; rows readable by index and by column name; commit /
                        rollback / close. PostgreSQL needs `psycopg` (v3) and MySQL needs
                        `PyMySQL`, neither installed by default -- connect() says so
                        instead of failing obscurely.
    Pool(url, size)     a small thread-safe connection pool (PostgreSQL / MySQL); SQLite
                        connections are cheap and stay per-call (db.schema.get_connection)
    translate(sql)      SQLite -> the configured backend's dialect, for the constructs
                        ATIP's DDL / DML use (db/postgres.py, db/mysql.py)
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
    if u.startswith(("mysql://", "mysql+pymysql://", "mariadb://")):
        return "mysql"
    if u.startswith("sqlite:"):
        return "sqlite"
    raise ValueError("unsupported database URL scheme (sqlite:///, postgresql:// or mysql://)")


def masked_url(url: str | None = None) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url or database_url())


# -- SQL translation (W38: db/postgres.py; the W9 regex rules are superseded) ---------------

def translate(sql: str, pk_of=None, url: str | None = None) -> str:
    """ddl() for CREATE / ALTER, translate() for everything else, in the dialect of the
    configured backend (db/postgres.py or db/mysql.py)."""
    if backend(url) == "mysql":
        from db import mysql
        if sql.lstrip().upper().startswith(("CREATE ", "ALTER ")):
            return mysql.ddl(sql, mysql.keyed_columns([sql]).get(mysql._table_of(sql), set()))
        return mysql.translate(sql, pk_of)
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


def MySQLConnection(url):
    """The full wrapper lives in db.mysql (ON DUPLICATE KEY upserts, rows by name)."""
    from db.mysql import MySQLConnection as _My
    return _My(url.replace("mysql+pymysql://", "mysql://").replace("mariadb://", "mysql://"))


def connect(url: str | None = None):
    url = url or database_url()
    if backend(url) == "postgresql":
        return PgConnection(url)
    if backend(url) == "mysql":
        return MySQLConnection(url)
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
