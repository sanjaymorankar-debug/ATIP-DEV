"""
PostgreSQL backend (db/postgres.py).

PostgreSQL is ATIP's one server database (the MySQL backend was removed). Until
these tests, db/postgres.py was only ever tested through its translated STRINGS (see
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
    assert out.args == ('column "a" of relation "t" already exists',)
    assert out.sqlstate == "42701"


def test_the_mapping_leaves_everything_else_alone():
    """What keeps UnsupportedSQL and ordinary programming mistakes from arriving
    disguised as database trouble."""
    for exc in (UnsupportedSQL("no translation"), ValueError("nope"), KeyError("k")):
        assert pg.as_sqlite_error(exc) is exc


# ── PostgreSQL is the one server database ─────────────────────────────────

def test_a_mysql_url_is_refused_with_the_reason():
    """The MySQL backend was removed: a leftover mysql:// URL must say so, not fall
    through to a generic 'unsupported scheme' or, worse, to SQLite."""
    from db.backend import backend
    for url in ("mysql://u:p@h/atip", "mysql+pymysql://u:p@h/atip", "mariadb://u:p@h/atip"):
        with pytest.raises(ValueError, match="no longer supported.*PostgreSQL"):
            backend(url)
    assert backend("postgresql://u@h/atip") == "postgresql" and backend("sqlite:///x.db") == "sqlite"


def test_a_runtime_still_switched_to_mysql_refuses_to_start(monkeypatch, tmp_path):
    """Configured for the old MySQL runtime, ATIP must not come up on an empty local
    SQLite file and look healthy."""
    import db.schema as S
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(
        '{"database": {"backend": "mysql", "allow_experimental": true}}', encoding="utf-8")
    monkeypatch.setenv("ATIP_DATABASE_URL", "mysql://atip:pw@127.0.0.1:3306/atip")
    monkeypatch.setattr(S, "_PG_URL", [])
    with pytest.raises(RuntimeError, match="MySQL runtime, which was removed"):
        S.get_connection()
    # the URL alone (no runtime switch) is not a startup failure: the runtime stays on SQLite
    (tmp_path / "atip_data" / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(S, "_PG_URL", [])
    assert S.pg_runtime_url() is None


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
    does."""
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
    """ATIP's shared SQL backticks some columns (signal, key, rank, rows, change,
    trigger) -- a leftover of the removed MySQL backend, where they are reserved.
    SQLite accepts backticks; PostgreSQL does not, so they become the standard quote."""
    from db.postgres import translate
    assert translate("SELECT `signal`, a FROM t WHERE `key`=?") == \
        'SELECT "signal", a FROM t WHERE "key"=%s'
    # a backtick inside a string literal is data, not a quote
    assert translate("SELECT a FROM t WHERE b='a`b'") == "SELECT a FROM t WHERE b='a`b'"


def test_a_backticked_column_is_still_qualified_on_the_right_of_an_upsert():
    """A bug from the same backtick-quoting change.

    PostgreSQL needs the target-row reference qualified -- a bare one is
    "column reference is ambiguous" -- and the name is matched with a non-word
    lookahead rather than \\b, which cannot match after a closing double quote.
    Without it "rows"="rows"+excluded."rows" reached the server and was rejected.
    """
    from db.postgres import translate
    out = translate("INSERT INTO s (day,`rows`) VALUES (?,?) ON CONFLICT(day) "
                    "DO UPDATE SET `rows`=`rows`+excluded.`rows`", lambda t: [["day"]])
    assert 's."rows"+excluded."rows"' in out, out
    # the unquoted form still qualifies exactly as before
    assert "ticks=s.ticks+excluded.ticks" in translate(
        "INSERT INTO s (day, ticks) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET ticks=ticks+excluded.ticks",
        lambda t: [["day"]])


# ── PostgreSQL as the runtime database ─────────────────────────────────────
#
# What the removed MySQL runtime had fixed for MySQL only, brought to PostgreSQL, plus
# what a fresh-database run of init_db() + ops.migrations.apply() + every GET route found.

def test_entry_order_tables_get_an_identity_column_on_postgres():
    """perf_ledger and ml_dl_benefit need rows back in the order they were written
    (a same-day BUY before its SELL). SQLite has rowid; PostgreSQL gets `seq`."""
    from db.backend import ENTRY_ORDER_COLUMN
    out = pg.ddl("CREATE TABLE IF NOT EXISTS perf_ledger (txn_id TEXT PRIMARY KEY, trade_date DATE)")
    assert f"{ENTRY_ORDER_COLUMN} BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE" in out, out
    assert ENTRY_ORDER_COLUMN not in pg.ddl("CREATE TABLE other (a TEXT PRIMARY KEY)")
    # a table that already declares the column is left alone
    assert pg.ddl("CREATE TABLE ml_dl_benefit (check_id TEXT PRIMARY KEY, seq BIGINT)").count("seq") == 1


def test_the_entry_order_tables_are_the_ones_that_order_by_rowid():
    """ENTRY_ORDER_TABLES lists the tables whose code orders by rowid; on PostgreSQL
    that rowid reads `seq`. ml/deep.allowed_to_activate's query is checked as written,
    so the source and the table list cannot drift apart unnoticed."""
    import inspect
    import re
    from db.backend import ENTRY_ORDER_COLUMN, ENTRY_ORDER_TABLES
    import ml.models  # noqa: F401  (ml.deep and ml.models import each other; models goes first)
    from ml import deep
    assert ENTRY_ORDER_TABLES == {"perf_ledger", "ml_dl_benefit"} and ENTRY_ORDER_COLUMN == "seq"
    src = inspect.getsource(deep.allowed_to_activate)
    sql = "".join(re.findall(r'"([^"]*)"', src[src.index("conn.execute("):src.index("(dataset_id,)")]))
    assert "rowid" in sql
    assert pg.translate(sql).endswith("ORDER BY created_at DESC, seq DESC LIMIT 1")


def test_rowid_on_an_entry_order_table_becomes_seq():
    """Migration 0006 numbers perf_ledger from rowid; on PostgreSQL that is `seq`.
    rowid anywhere else still refuses to translate, and so does a join, where the
    rowid could belong to the other table."""
    assert pg.translate("UPDATE perf_ledger SET entry_seq=rowid WHERE entry_seq IS NULL") == \
        "UPDATE perf_ledger SET entry_seq=seq WHERE entry_seq IS NULL"
    assert pg.translate("SELECT a FROM ml_dl_benefit ORDER BY created_at DESC, rowid DESC LIMIT 1").endswith(
        "ORDER BY created_at DESC, seq DESC LIMIT 1")
    with pytest.raises(UnsupportedSQL):
        pg.translate("SELECT rowid FROM order_log")
    with pytest.raises(UnsupportedSQL):
        pg.translate("SELECT l.rowid FROM perf_ledger l JOIN perf_ledger_void v ON v.txn_id=l.txn_id")


def test_an_if_not_exists_trigger_is_created_or_replaced():
    """PostgreSQL has no CREATE TRIGGER IF NOT EXISTS; ops/migrations.py's guards use it."""
    out = pg.trigger_ddl("CREATE TRIGGER IF NOT EXISTS trg_x BEFORE DELETE ON perf_ledger WHEN OLD.tenant_id <> 'uat' "
                         "BEGIN SELECT RAISE(ABORT, 'perf_ledger is append-only'); END")
    assert out.startswith("CREATE OR REPLACE TRIGGER trg_x BEFORE DELETE ON perf_ledger FOR EACH ROW "
                          "WHEN (OLD.tenant_id <> 'uat')"), out
    assert pg.trigger_ddl("CREATE TRIGGER t BEFORE UPDATE ON a BEGIN SELECT RAISE(ABORT, 'm'); END") \
        .startswith("CREATE TRIGGER t ")


def test_the_session_time_zone_is_pinned_to_utc_keeping_other_options():
    pytest.importorskip("psycopg")
    assert pg._session_options("postgresql://u@h/d") == {"options": "-c TimeZone=UTC"}
    assert pg._session_options("postgresql://u@h/d?options=-c%20statement_timeout%3D5000") == \
        {"options": "-c statement_timeout=5000 -c TimeZone=UTC"}


@live_only
def test_the_session_is_utc_whatever_the_server_is_set_to(live_db):
    """A VPS installed from packages runs PostgreSQL in the OS zone (Asia/Kolkata).
    DEFAULT CURRENT_TIMESTAMP on a TIMESTAMP column is read in the session zone, so
    without the pin every defaulted row was stamped 5h30m away from SQLite's UTC."""
    from urllib.parse import urlparse
    name = urlparse(LIVE_URL).path.lstrip("/")
    live_db.execute(f"ALTER DATABASE \"{name}\" SET timezone TO 'Asia/Kolkata'")
    c = pg.PgConnection(LIVE_URL)
    try:
        assert c.execute("SHOW timezone").fetchone()[0] == "UTC"
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO t (id) VALUES (1)")
        drift = c.execute("SELECT EXTRACT(EPOCH FROM (at - (now() AT TIME ZONE 'UTC'))) FROM t").fetchone()[0]
        assert abs(float(drift)) < 60
    finally:
        c.close()


@live_only
def test_a_failed_read_does_not_poison_the_connection(live_db):
    """In PostgreSQL a failed statement aborts the transaction; in SQLite it fails
    alone. portfolio/live_pnl.py probes the lazily created paper tables in a
    try / except and then carries on, and on PostgreSQL every later query died with
    "current transaction is aborted"."""
    c = pg.PgConnection(LIVE_URL)
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("SELECT * FROM no_such_table")
        assert c.execute("SELECT 1").fetchone()[0] == 1
    finally:
        c.close()


@live_only
def test_a_failed_statement_inside_a_write_keeps_the_earlier_writes(live_db):
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        c.commit()
        c.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.OperationalError):
            c.execute("SELECT * FROM no_such_table")
        c.execute("INSERT INTO t VALUES (2)")
        c.commit()
        assert [r[0] for r in c.execute("SELECT id FROM t ORDER BY id").fetchall()] == [1, 2]
    finally:
        c.close()


@live_only
def test_begin_immediate_takes_a_write_lock(live_db):
    """ops/jobs.acquire() and ops/migrations.apply() open with BEGIN IMMEDIATE, a
    syntax error on PostgreSQL -- so every job lock raised. It must also still mean
    what it means in SQLite: a second IMMEDIATE section waits for the first."""
    a, b = pg.PgConnection(LIVE_URL), pg.PgConnection(LIVE_URL)
    try:
        a.execute("BEGIN IMMEDIATE")
        b._conn.execute("SET lock_timeout = '300ms'")
        b.commit()
        with pytest.raises(sqlite3.OperationalError):
            b.execute("BEGIN IMMEDIATE")
        # a failed read inside the section must not release the lock
        with pytest.raises(sqlite3.OperationalError):
            a.execute("SELECT * FROM no_such_table")
        with pytest.raises(sqlite3.OperationalError):
            b.execute("BEGIN IMMEDIATE")
        a.execute("COMMIT")
        b.execute("BEGIN IMMEDIATE")
        b.execute("ROLLBACK")
    finally:
        a.close()
        b.close()


@live_only
def test_job_locks_work_on_postgres(live_db):
    import db.schema as schema
    from ops import jobs
    c = pg.PgConnection(LIVE_URL)
    try:
        for stmt in schema.W8_TABLES.get("ops_job_lock", ()):
            c.execute(stmt)
        c.commit()
        assert jobs.acquire(c, "pg-test-job") is True
        jobs.release(c, "pg-test-job")
        c.commit()
    finally:
        c.close()


@live_only
def test_drop_trigger_without_a_table_and_lastrowid(live_db):
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT)")
        c.execute("CREATE TRIGGER IF NOT EXISTS trg_t BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT, 't'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS trg_t BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT, 't'); END")
        cur = c.execute("INSERT INTO t (a) VALUES (?)", ("x",))
        assert cur.lastrowid == 1
        assert c.execute("INSERT INTO t (a) VALUES (?)", ("y",)).lastrowid == 2
        c.execute("DROP TRIGGER IF EXISTS trg_t")
        c.execute("DROP TRIGGER IF EXISTS trg_t")              # gone: a no-op, as in SQLite
        c.execute("DELETE FROM t WHERE id=1")
        c.commit()
    finally:
        c.close()


@live_only
def test_an_entry_order_table_from_an_earlier_release_gains_seq(live_db):
    """A database migrated before this change has perf_ledger without `seq`; the
    CREATE TABLE IF NOT EXISTS of the next start adds it, numbering the rows in
    the order they were written."""
    live_db.execute("CREATE TABLE ml_dl_benefit (check_id TEXT PRIMARY KEY, created_at TIMESTAMP)")
    live_db.execute("INSERT INTO ml_dl_benefit VALUES ('b', '2026-10-01'), ('a', '2026-10-01')")
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS ml_dl_benefit (check_id TEXT PRIMARY KEY, created_at TIMESTAMP)")
        c.commit()
        rows = c.execute("SELECT check_id FROM ml_dl_benefit ORDER BY created_at, rowid").fetchall()
        assert [r[0] for r in rows] == ["b", "a"]
    finally:
        c.close()


@live_only
def test_every_migration_applies_on_a_fresh_postgres_database(live_db):
    """init_db() then ops.migrations.apply(), as `main.py --init` and start-up do.
    Every migration used to fail at its first statement (BEGIN IMMEDIATE), which
    left the append-only guards uninstalled on a database built this way."""
    import db.schema as schema
    from ops import migrations

    original = schema.get_connection
    held = []

    def _conn():
        c = pg.PgConnection(LIVE_URL)
        held.append(c)
        return c

    schema.get_connection = _conn
    try:
        schema.init_db()
        c = _conn()
        results = migrations.apply(c)
        assert results and all(r["status"] == "APPLIED" for r in results), results
        assert migrations.apply(c) == []                      # idempotent: nothing left pending
        assert not [v for v in migrations.validate(c) if v["level"] == "error"]
    finally:
        schema.get_connection = original
        for c in held:
            try:
                c.close()
            except Exception:
                pass
    trig = {r[0] for r in live_db.execute("SELECT trigger_name FROM information_schema.triggers WHERE "
                                          "event_object_table='perf_ledger'").fetchall()}
    assert trig == {"trg_perf_ledger_no_update", "trg_perf_ledger_no_delete"}, trig


@live_only
def test_query_only_makes_the_connection_read_only(live_db):
    """tools/atip_mcp.py opens its connection with PRAGMA query_only = ON so the MCP
    tools cannot write. On PostgreSQL that PRAGMA used to fail to translate, and the
    tool carried on with a writable connection."""
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        c.commit()
        c.execute("PRAGMA query_only = ON")
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            c.execute("INSERT INTO t VALUES (1)")
        c.rollback()
        c.execute("PRAGMA query_only = OFF")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
    finally:
        c.close()
