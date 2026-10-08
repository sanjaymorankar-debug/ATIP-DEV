"""
PostgreSQL backend (db/postgres.py).

Companion to tests/test_mysql_backend.py, and added for the same reason: until
now db/postgres.py was only ever tested through its translated STRINGS (see
tests/test_w38_platform_compliance.py). Nothing executed them, so two defects
sat in PgConnection -- it had no cursor() at all, and it raised psycopg's
exception classes at callers written to catch sqlite3's.

  * Mapping tests need no server and always run.
  * Live tests are skipped unless ATIP_TEST_POSTGRES_URL is set, because the
    server is not part of the default test environment:

        ATIP_TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/atip_test \
            pytest tests/test_postgres_backend.py

    The database it names is DROPped and recreated, so never point it at real data.
"""

import os
import sqlite3

import pytest

from db import postgres as pg
from db.postgres import UnsupportedSQL

LIVE_URL = os.environ.get("ATIP_TEST_POSTGRES_URL")
live_only = pytest.mark.skipif(not LIVE_URL, reason="set ATIP_TEST_POSTGRES_URL to run live PostgreSQL tests")


# ── the error mapping ─────────────────────────────────────────────────────

def test_the_mapping_uses_sqlstate_class_23_for_a_constraint_violation():
    """PostgreSQL reports the SQL standard's SQLSTATE, and class 23 *is* "integrity
    constraint violation" -- exactly the set SQLite raises IntegrityError for. So the
    prefix is the rule, rather than a table of codes that can fall out of date.
    All sqlstates measured against PostgreSQL 16.14."""
    psycopg = pytest.importorskip("psycopg")

    def mapped(sqlstate):
        exc = psycopg.errors.Error("boom")
        exc.sqlstate = sqlstate
        return type(pg.as_sqlite_error(exc))

    for sqlstate in ("23502", "23503", "23505", "23514", "23001", "23P01"):
        assert mapped(sqlstate) is sqlite3.IntegrityError, sqlstate
    # everything else a statement can get wrong, as SQLite categorises it
    for sqlstate in ("42701",      # duplicate column
                     "42P07",      # duplicate table / index
                     "42P01",      # undefined table
                     "42703",      # undefined column
                     "42601",      # syntax error
                     "08006"):     # connection failure
        assert mapped(sqlstate) is sqlite3.OperationalError, sqlstate


def test_the_mapping_keeps_the_message_and_the_sqlstate():
    psycopg = pytest.importorskip("psycopg")
    exc = psycopg.errors.Error('column "a" of relation "t" already exists')
    exc.sqlstate = "42701"
    out = pg.as_sqlite_error(exc)
    # psycopg puts the message in args[0], which is sqlite3's own convention
    # (PyMySQL puts the errno there instead -- each driver's convention is kept)
    assert out.args == ('column "a" of relation "t" already exists',)
    assert out.sqlstate == "42701"


def test_the_mapping_leaves_everything_else_alone():
    """What keeps UnsupportedSQL and ordinary programming mistakes from arriving
    disguised as database trouble."""
    for exc in (UnsupportedSQL("no translation"), ValueError("nope"), KeyError("k")):
        assert pg.as_sqlite_error(exc) is exc


# ── live server ───────────────────────────────────────────────────────────

@pytest.fixture
def live_db():
    """A freshly created, empty database on the live server."""
    psycopg = pytest.importorskip("psycopg")
    from urllib.parse import urlparse, urlunparse
    u = urlparse(LIVE_URL)
    name = (u.path or "/atip_test").lstrip("/")
    admin = urlunparse(u._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    conn = psycopg.connect(LIVE_URL, autocommit=True)
    yield conn
    conn.close()


@live_only
def test_a_duplicate_column_is_catchable_as_sqlite3_operationalerror(live_db):
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a REAL)")
        c.commit()
        with pytest.raises(sqlite3.OperationalError) as e:
            c.execute("ALTER TABLE t ADD COLUMN a REAL")
        assert e.value.sqlstate == "42701"
    finally:
        c.close()


@live_only
def test_constraint_violations_are_catchable_as_sqlite3_integrityerror(live_db):
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT UNIQUE, "
                  "n INTEGER NOT NULL CHECK (n > 0), pid INTEGER REFERENCES p(id))")
        c.execute("INSERT INTO p VALUES (1)")
        c.execute("INSERT INTO t VALUES (1, 'x', 5, 1)")
        c.commit()
        for params in (
            (2, "x", 5, 1),        # unique
            (3, "y", None, 1),     # not null
            (4, "z", -1, 1),       # check
            (5, "w", 5, 99),       # foreign key
        ):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO t VALUES (?, ?, ?, ?)", params)
            c.rollback()
    finally:
        c.close()


@live_only
def test_a_missing_table_lets_the_risk_check_fall_back(live_db):
    """The consequence that makes this worth fixing. orders/risk.py says it plainly:
    order_log and the paper tables are created lazily by whatever writes them, so a
    missing table means "nothing placed, nothing held" and MUST NOT raise -- or
    "configuring a limit would break the first order ATIP ever places". That
    fallback is an `except sqlite3.OperationalError`, which psycopg never raised."""
    from orders.risk import _query
    c = pg.PgConnection(LIVE_URL)
    try:
        assert _query(c, "SELECT COUNT(*) FROM order_log", (), [(0,)]) == [(0,)]
    finally:
        c.close()


@live_only
def test_the_connection_is_sqlite_shaped(live_db):
    """? placeholders, rows by index and by name, and a cursor() -- init_db() is
    hundreds of conn.cursor().execute() calls and failed with AttributeError
    without it."""
    c = pg.PgConnection(LIVE_URL)
    try:
        assert c.cursor() is c
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, sym TEXT, px REAL)")
        c.execute("INSERT INTO t (id, sym, px) VALUES (?, ?, ?)", (1, "ACME", 12.5))
        c.commit()
        row = c.cursor().execute("SELECT id, sym, px FROM t WHERE sym=?", ("ACME",)).fetchone()
        assert row[1] == "ACME"            # by index
        assert row["sym"] == "ACME"        # by name, like sqlite3.Row
        assert float(row["px"]) == 12.5
    finally:
        c.close()


@live_only
def test_the_connection_commits_and_rolls_back_as_a_context_manager(live_db):
    """`with conn:` commits on success and rolls back on an exception, as sqlite3
    and MySQLConnection both do."""
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        c.commit()
        with c:
            c.execute("INSERT INTO t VALUES (1)")
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

        with pytest.raises(RuntimeError):
            with c:
                c.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("abandon")
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        c.close()


@live_only
def test_init_db_creates_the_whole_schema_on_postgres_and_is_idempotent(live_db):
    """What the cursor() and error-class fixes are for: ATIP's real init_db(),
    against a real server, twice. It runs on every start, so idempotence is the
    requirement rather than a nicety."""
    import db.schema as schema

    original = schema.get_connection
    held = []

    def _conn():
        c = pg.PgConnection(LIVE_URL)
        held.append(c)
        return c

    schema.get_connection = _conn
    try:
        schema.init_db()
        schema.init_db()
    finally:
        schema.get_connection = original
        for c in held:
            try:
                c.commit()
                c.close()
            except Exception:
                pass

    cur = live_db.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    assert cur.fetchone()[0] > 200


def test_backticked_identifiers_become_double_quotes():
    """ATIP's shared SQL backticks the columns MySQL 8 reserves (signal, key,
    rank, rows, change, trigger). SQLite accepts backticks for MySQL
    compatibility; PostgreSQL does not, so they become the standard quote."""
    from db.postgres import translate
    assert translate("SELECT `signal`, a FROM t WHERE `key`=?") == \
        'SELECT "signal", a FROM t WHERE "key"=%s'
    # a backtick inside a string literal is data, not a quote
    assert translate("SELECT a FROM t WHERE b='a`b'") == "SELECT a FROM t WHERE b='a`b'"
