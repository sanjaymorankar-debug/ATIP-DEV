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
        assert c.cursor().execute("SELECT 1").fetchone()[0] == 1
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, sym TEXT, px REAL)")
        c.execute("INSERT INTO t (id, sym, px) VALUES (?, ?, ?)", (1, "ACME", 12.5))
        c.commit()
        cur = c.cursor().execute("SELECT id, sym, px FROM t WHERE sym=?", ("ACME",))
        assert [d[0] for d in cur.description] == ["id", "sym", "px"]
        row = cur.fetchone()
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


def test_a_backticked_column_is_still_qualified_on_the_right_of_an_upsert():
    """The mirror of the MySQL excluded bug, from the same quoting change.

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


# ── SQLite functions PostgreSQL lacks or types differently (17 Nov, bkesari VPS rehearsal) ──
#
# Found by PREPARE-ing every literal statement in the code base against a PostgreSQL copy
# of the live schema: these were the constructs PostgreSQL rejected.

def test_round_with_places_goes_through_numeric_and_returns_a_float():
    from db.postgres import translate
    assert translate("SELECT ROUND(SUM(px * q), 2) FROM t") == \
        "SELECT CAST(ROUND(CAST(SUM(px * q) AS numeric), 2) AS double precision) FROM t"
    assert translate("UPDATE p SET o=ROUND(o*?,2) WHERE s=?") == \
        "UPDATE p SET o=CAST(ROUND(CAST(o*%s AS numeric), 2) AS double precision) WHERE s=%s"
    assert translate("SELECT CAST(ROUND(v / ?) AS INTEGER) FROM t") == "SELECT CAST(ROUND(v / %s) AS INTEGER) FROM t"


def test_substr_works_on_the_text_of_any_value():
    from db.postgres import translate
    assert translate("SELECT substr(ts,1,10) d FROM b") == "SELECT substr(CAST(ts AS text), 1, 10) d FROM b"


def test_date_and_datetime_modifiers():
    from db.postgres import translate, UnsupportedSQL
    assert translate("SELECT 1 WHERE c >= datetime('now', '-1 day')") == \
        "SELECT 1 WHERE c >= CAST(LOCALTIMESTAMP - INTERVAL '1 day' AS timestamp(0))"
    assert translate("SELECT DATE(MAX(date), ?) FROM q") == \
        "SELECT CAST(CAST(MAX(date) AS date) + CAST(%s AS interval) AS date) FROM q"
    out = translate("SELECT 1 WHERE DATETIME(REPLACE(SUBSTR(ts,1,19),'T',' '), '+' || n || ' minutes')<=?")
    assert out == ("SELECT 1 WHERE CAST(CAST(REPLACE(substr(CAST(ts AS text), 1, 19),'T',' ') AS timestamp) "
                   "+ CAST('+' || n || ' minutes' AS interval) AS timestamp(0))<=%s")
    assert translate("SELECT date(d, 'start of month')") == "SELECT CAST(date_trunc('month', CAST(d AS date)) AS date)"
    assert translate("SELECT date(d)") == "SELECT CAST(d AS date)"
    with pytest.raises(UnsupportedSQL):
        translate("SELECT date(d, 'weekday 1')")
    # a column called date / time is not a call, and neither is a dotted name
    assert translate("INSERT INTO t (date, time) VALUES (?, ?)") == "INSERT INTO t (date, time) VALUES (%s, %s)"
    assert translate("SELECT x.date FROM t x WHERE x.date=date('now')") == \
        "SELECT x.date FROM t x WHERE x.date=CURRENT_DATE"


def test_current_timestamp_is_sqlite_shaped_utc_text():
    from db.postgres import translate, ddl
    assert translate("UPDATE t SET at=CURRENT_TIMESTAMP") == "UPDATE t SET at=LOCALTIMESTAMP(0)"
    assert "DEFAULT LOCALTIMESTAMP(0)" in ddl("CREATE TABLE t (a TEXT DEFAULT CURRENT_TIMESTAMP)")
    assert translate("SELECT 'CURRENT_TIMESTAMP'") == "SELECT 'CURRENT_TIMESTAMP'"


def test_is_with_a_parameter_is_null_safe_equality():
    from db.postgres import translate
    assert translate("SELECT 1 FROM e WHERE sid IS ? AND x IS NOT ? AND y IS NULL") == \
        "SELECT 1 FROM e WHERE sid IS NOT DISTINCT FROM %s AND x IS DISTINCT FROM %s AND y IS NULL"


def test_min_max_of_a_comparison_counts_it_as_0_or_1():
    from db.postgres import translate
    assert translate("SELECT MIN(kind IN ('A','B')), MAX(px) FROM c") == \
        "SELECT MIN(CAST((kind IN ('A','B')) AS INT)), MAX(px) FROM c"


def test_named_parameters_become_pyformat():
    from db.postgres import translate
    assert translate("UPDATE p SET d=:p WHERE s=:s AND t='09:15' AND x::text=:d") == \
        "UPDATE p SET d=%(p)s WHERE s=%(s)s AND t='09:15' AND x::text=%(d)s"


def test_ddl_turns_backticks_into_double_quotes():
    from db.postgres import ddl
    assert ddl("CREATE TABLE t (a TEXT, `signal` TEXT, b TEXT DEFAULT 'x`y')") == \
        "CREATE TABLE t (a TEXT, \"signal\" TEXT, b TEXT DEFAULT 'x`y')"


@live_only
def test_the_connection_behaves_like_sqlite3_where_atip_relies_on_it(live_db):
    """A failed statement does not abort the transaction; lastrowid and total_changes
    are filled; SUM of integers is an int, AVG a float, a comparison 1/0 -- never
    Decimal or bool; pandas.read_sql works; 'now' is UTC."""
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, q INTEGER, at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.commit()
        cur = c.execute("INSERT INTO t (q) VALUES (?)", (3,))
        assert cur.lastrowid == 1 and cur.fetchone() is None
        assert c.cursor().execute("INSERT INTO t (q) VALUES (?)", (4,)).lastrowid == 2
        with pytest.raises(sqlite3.OperationalError):
            c.execute("ALTER TABLE t ADD COLUMN q INTEGER")          # already exists
        with pytest.raises(sqlite3.OperationalError):
            c.execute("SELECT * FROM no_such_table")
        c.execute("UPDATE t SET q=q+1 WHERE id=?", (1,))             # the transaction is still usable
        c.commit()
        assert c.total_changes == 3
        s, a = c.execute("SELECT SUM(q), AVG(q) FROM t WHERE id=1").fetchone()
        b = c.execute("SELECT q>3 FROM t WHERE id=1").fetchone()[0]
        assert (s, type(s), type(a), b, type(b)) == (4, int, float, 1, int)
        assert c.execute("SELECT ROUND(AVG(q)*1.0/3, 2) FROM t").fetchone()[0] == 1.33
        at = c.execute("SELECT at FROM t WHERE id=1").fetchone()[0]
        assert len(at) == 19 and at[10] == " "                       # 'YYYY-MM-DD HH:MM:SS', as SQLite
        pd = pytest.importorskip("pandas")
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = pd.read_sql("SELECT id, q FROM t WHERE q>=? ORDER BY id", c, params=[4])
        assert list(df["id"]) == [1, 2]
        c.execute("UPDATE t SET q=:q WHERE id=:id", {"q": 9, "id": 2})
        assert c.execute("SELECT q FROM t WHERE id IS ?", (2,)).fetchone()[0] == 9
    finally:
        c.close()


def test_end_of_day_sentinels_become_the_last_instant_of_the_day():
    """day + " 99" / "T99" / "~" sort after every time of the day as SQLite TEXT; PostgreSQL rejects
    them as DATE / TIMESTAMP input. Anything else passes through untouched."""
    assert pg._params(("2026-11-17", "2026-11-17 99", "2026-11-17T99", "2026-11-17~")) == \
        ("2026-11-17", "2026-11-17 23:59:59.999999", "2026-11-17 23:59:59.999999", "2026-11-17 23:59:59.999999")
    assert pg._params({"a": "2026-11-17~", "b": "x~", "c": 5}) == {"a": "2026-11-17 23:59:59.999999", "b": "x~", "c": 5}


@live_only
def test_end_of_day_sentinels_bound_date_and_timestamp_columns(live_db):
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE b (ts TIMESTAMP, d DATE)")
        c.executemany("INSERT INTO b (ts, d) VALUES (?, ?)",
                      [("2026-11-17 09:15:00", "2026-11-17"), ("2026-11-17 15:30:00", "2026-11-17"),
                       ("2026-11-18 09:15:00", "2026-11-18")])
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM b WHERE ts>=? AND ts<?", ("2026-11-17", "2026-11-17 99")).fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM b WHERE ts>=? AND ts<?", ("2026-11-17", "2026-11-17~")).fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM b WHERE d<=?", ("2026-11-17T99",)).fetchone()[0] == 2
    finally:
        c.close()


@live_only
def test_vacuum_runs_after_a_commit_and_refuses_inside_a_transaction(live_db):
    """db/purge.py commits its deletes and then runs VACUUM; PostgreSQL rejects VACUUM in a
    transaction block, so the backend runs it in autocommit -- and, like SQLite, refuses it
    while a transaction is open."""
    import sqlite3
    c = pg.PgConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE v (x INTEGER)")
        c.execute("INSERT INTO v (x) VALUES (?)", (1,))
        with pytest.raises(sqlite3.OperationalError):
            c.execute("VACUUM")
        c.commit()
        assert c.execute("VACUUM").fetchall() == []
        c.execute("INSERT INTO v (x) VALUES (?)", (2,))
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM v").fetchone()[0] == 2
    finally:
        c.close()


@live_only
def test_backup_and_restore_drill_use_pg_dump_on_postgres(live_db, monkeypatch, tmp_path):
    """On the PostgreSQL runtime ops/backup.py writes a pg_dump archive, verifies it from the
    archive's own data, and the restore drill restores it into a scratch database (then drops it)
    -- nothing copies a SQLite file."""
    import psycopg
    import db.schema as schema
    from ops import backup as B

    monkeypatch.setattr(schema, "_PG_URL", [LIVE_URL])
    monkeypatch.setattr(schema, "get_connection", lambda: pg.PgConnection(LIVE_URL))
    monkeypatch.setattr(schema, "DB_PATH", tmp_path / "must-not-exist.db")
    monkeypatch.chdir(tmp_path)
    schema.init_db()
    c = pg.PgConnection(LIVE_URL)
    try:
        c.executemany("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                      [("AAA", f"2026-11-{d:02d}", 1, 2, 0.5, 1.5, 100) for d in range(1, 8)])
        c.commit()
    finally:
        c.close()

    res = B.backup("manual")
    assert res["status"] == "VERIFIED", res
    assert res["path"].endswith(".dump") and res["tables"]["prices_daily"] == 7
    assert not (tmp_path / "must-not-exist.db").exists()

    drill = B.restore_drill(notify=False)
    assert drill["status"] == "SUCCESS", drill
    assert drill["source"] == "local" and drill["mismatches"] == {}
    with psycopg.connect(LIVE_URL) as a:
        left = a.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'atip_drill_%'").fetchone()[0]
    assert left == 0, "the scratch database is dropped after the drill"


# ── the SQLite -> PostgreSQL copy tool (tools/sqlite_to_postgres.py) ───────
#
# 18 Nov 2026: after the live copy, 10 tables' identity counters were still at 1, so the first
# new row in each collided with a copied id (auto-pick failed 09:15-09:31 IST on
# session_events_pkey). The tool reset a counter only when the translated DDL read exactly
# "id BIGINT GENERATED"; it missed the two shapes ATIP really has, below.

_COPY_SOURCE = [
    # orders/sessions.py lays its columns out in a column: "id          BIGINT GENERATED ..."
    """CREATE TABLE session_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT,
        at          TEXT)""",
    # data/news_impact.py: the identity column is not called id
    "CREATE TABLE news_impact (article_id INTEGER PRIMARY KEY, headline TEXT)",
    # the one shape the old check did catch
    "CREATE TABLE order_log (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT)",
    # an id that is not an identity: there is no counter to move
    "CREATE TABLE trading_sessions (id TEXT PRIMARY KEY, name TEXT)",
]
_COPY_ROWS = {
    "session_events": [(7, "S1", "a"), (244, "S8", "b")],
    "news_impact": [(3, "x"), (17624, "y")],
    "order_log": [(1, "AAA"), (103, "PHOENIXLTD")],
    "trading_sessions": [("S1", "one"), ("S2", "two")],
}
# what each identity counter must hand out next: MAX + 1
_COPY_NEXT = {("session_events", "id"): 245, ("news_impact", "article_id"): 17625, ("order_log", "id"): 104}


def _copy_tool():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "tools" / "sqlite_to_postgres.py"
    spec = importlib.util.spec_from_file_location("sqlite_to_postgres_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _copy_source(tmp_path):
    src = tmp_path / "copy.db"
    c = sqlite3.connect(src)
    for ddl in _COPY_SOURCE:
        c.execute(ddl)
    for table, rows in _COPY_ROWS.items():
        c.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
    c.commit()
    c.close()
    return src


def test_the_copy_tool_resets_every_identity_column_whatever_its_name_or_layout(tmp_path, monkeypatch):
    """No server needed: a stand-in connection answers identity_of() as PostgreSQL does for the
    translated DDL (the live test below checks that on a real server) and records the resets."""
    import re
    import db.backend

    made = []

    class FakePg:
        IDENTITY = {"session_events": "id", "news_impact": "article_id", "order_log": "id",
                    "trading_sessions": None}

        def __init__(self, url):
            self.rows, self.resets = {}, []
            made.append(self)

        def execute(self, sql, args=()):
            m = re.match(r'SELECT COUNT\(\*\) FROM "(\w+)"', sql)
            n = self.rows.get(m.group(1), 0) if m else None
            return type("Cur", (), {"fetchone": lambda _self: (n,)})()

        def executemany(self, sql, batch):
            name = re.match(r'INSERT INTO "(\w+)"', sql).group(1)
            self.rows[name] = self.rows.get(name, 0) + len(batch)

        def identity_of(self, table):
            return self.IDENTITY[table]

        def reset_identity(self, table, column="id"):
            self.resets.append((table, column))

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(db.backend, "PgConnection", FakePg)
    tool = _copy_tool()
    src = _copy_source(tmp_path)
    r = tool.execute(src, "postgresql://unused/none", tool.plan(src))
    assert r["ok"], r
    assert sorted(made[0].resets) == sorted(_COPY_NEXT), \
        "every identity counter is moved, whatever the column is called or how the DDL is laid out"


@live_only
def test_the_copy_tool_leaves_no_identity_counter_behind_the_copied_ids(live_db, tmp_path):
    """End to end on a real server: after the copy, the next new row in every identity table
    gets MAX + 1, not an id that is already taken."""
    tool = _copy_tool()
    src = _copy_source(tmp_path)
    r = tool.execute(src, LIVE_URL, tool.plan(src))
    assert r["ok"], r
    for (table, col), want in _COPY_NEXT.items():
        got = live_db.execute(f"INSERT INTO {table} DEFAULT VALUES RETURNING {col}").fetchone()[0]
        assert got == want, (table, col, got)
    # nothing else moved: the text-keyed table copied as it was
    assert live_db.execute("SELECT count(*) FROM trading_sessions").fetchone()[0] == 2
