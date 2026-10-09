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

DATABASE PLAN (2026-10-09): PostgreSQL is the server database for the hosted
deployment; MySQL is no longer the target (db/mysql.py is kept as legacy code, not
deployed). SQLite stays the default for a local install. The runtime moves to
PostgreSQL only through db.schema.pg_runtime_url()'s gate (ATIP_DATABASE_URL +
config "database": {"backend": "postgresql", "allow_experimental": true}) --
docs/POSTGRESQL_MIGRATION.md and docs/HOSTED_DEPLOYMENT.md.
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


# Tables whose row ORDER carries meaning, not just their contents. SQLite gets that order
# from its implicit rowid; PostgreSQL and MySQL have no stable equivalent (PostgreSQL's
# ctid moves when a row is updated or the table is vacuumed full), so db.postgres.ddl()
# and db.mysql.ddl() give these tables an auto-numbered column, and entry_order_column()
# tells a query which name to order by on the backend it is actually running against.
#
# perf_ledger is here for a measured reason, recorded in wealth/perf/engine.py: its own
# txn_id is a random id, and ordering by it put a same-day SELL before its BUY about half
# the time, which capped the sell as EXCESS_SELL and lost the round trip. This is P&L
# correctness, not tidiness. ml_dl_benefit wants the latest row when two benefit checks
# share a created_at.
ENTRY_ORDER_TABLES = frozenset(("perf_ledger", "ml_dl_benefit"))
ENTRY_ORDER_COLUMN = "seq"


def entry_order_column(table: str, url: str | None = None) -> str:
    """The column a query should ORDER BY to get rows back in the order they were written.

    SQLite answers with its implicit `rowid`; PostgreSQL and MySQL get a real
    auto-numbered column (ENTRY_ORDER_COLUMN) on the tables that need one, and this
    returns its name. A caller interpolates the result rather than writing `rowid`,
    which keeps the same query correct on every backend.

    Only tables in ENTRY_ORDER_TABLES have such a column: asking for any other is a
    mistake worth hearing about, since the answer would otherwise be a column name
    that does not exist.
    """
    be = backend(url)
    if be == "sqlite":
        return "rowid"
    if table not in ENTRY_ORDER_TABLES:
        raise ValueError(
            f"{table} has no entry-order column; add it to db.backend.ENTRY_ORDER_TABLES "
            f"(and migrate the table) before ordering by one")
    return ENTRY_ORDER_COLUMN


def masked_url(url: str | None = None) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url or database_url())


# -- SQL translation (W38: db/postgres.py; the W9 regex rules are superseded) ---------------

def translate(sql: str, pk_of=None, url: str | None = None, keyed=None) -> str:
    """ddl() for CREATE / ALTER, translate() for everything else, in the dialect of the
    configured backend (db/postgres.py or db/mysql.py).

    `keyed` is db.mysql.keyed_columns() over the WHOLE statement list, and the MySQL
    path needs it to widen a TEXT column that only a separate CREATE INDEX keys --
    MySQL cannot index TEXT without a prefix length. Derived from this one statement
    when it is not given, which is all a single statement can support: a key declared
    elsewhere is then invisible and its index fails with errno 1170. Callers that hold
    the whole schema should pass it; MySQLConnection, which does not, widens such a
    column when the index arrives instead."""
    if backend(url) == "mysql":                                     # legacy backend
        from db import mysql
        if sql.lstrip().upper().startswith(("CREATE ", "ALTER ")):
            keys = keyed if keyed is not None else mysql.keyed_columns([sql])
            return mysql.ddl(sql, keys.get(mysql._table_of(sql), set()))
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
    """The full wrapper lives in db.mysql (ON DUPLICATE KEY upserts, rows by name).
    Legacy: PostgreSQL replaced MySQL as the hosted database (2026-10-09)."""
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
