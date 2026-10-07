"""
MySQL / MariaDB backend (db/mysql.py).

Two layers, deliberately:

  * Translation tests need no database and always run. They are the contract of
    db.mysql.translate / ddl / trigger_ddl.
  * Live tests run the whole real schema -- every CREATE TABLE and CREATE INDEX in
    every db/schema*.py module, and ATIP's own init_db() twice over -- into an
    actual MySQL/MariaDB server and then round-trip data through it. They are skipped unless ATIP_TEST_MYSQL_URL is set,
    because the server is not part of the default test environment:

        ATIP_TEST_MYSQL_URL=mysql://root@localhost:3306/atip_test pytest tests/test_mysql_backend.py

    The database it names is DROPped and recreated, so never point it at real data.
"""

import os
import re
import sqlite3

import pytest

from db import mysql as my
from db.mysql import UnsupportedSQL

LIVE_URL = os.environ.get("ATIP_TEST_MYSQL_URL")
live_only = pytest.mark.skipif(not LIVE_URL, reason="set ATIP_TEST_MYSQL_URL to run live MySQL tests")


# ── translation: DML ──────────────────────────────────────────────────────

def test_placeholders_and_percent_literals():
    # ? -> %s outside literals; % doubled everywhere (PyMySQL %-formats the statement)
    assert my.translate("SELECT * FROM t WHERE a=? AND b LIKE '%x?%'") == \
        "SELECT * FROM t WHERE a=%s AND b LIKE '%%x?%%'"


def test_constructs_that_are_native_mysql_are_left_alone():
    """The whole point of a separate MySQL path: these need no rewrite, and the
    PostgreSQL rewrites for them would be syntax errors here."""
    assert my.translate("SELECT GROUP_CONCAT(DISTINCT f) FROM t") == "SELECT GROUP_CONCAT(DISTINCT f) FROM t"
    assert my.translate("SELECT IFNULL(a, 0) FROM t") == "SELECT IFNULL(a, 0) FROM t"
    # LIKE stays LIKE: MySQL's default collation is already case-insensitive and
    # there is no ILIKE keyword.
    out = my.translate("SELECT 1 FROM t WHERE n NOT LIKE ?")
    assert "ILIKE" not in out and "NOT LIKE %s" in out
    # comparisons already yield 0/1, so no CAST wrapper
    assert my.translate("SELECT SUM(a > b) FROM t") == "SELECT SUM(a > b) FROM t"


def test_greatest_least():
    assert my.translate("SELECT MAX(a), MAX(a, b), MIN(x, 0) FROM t") == \
        "SELECT MAX(a), GREATEST(a, b), LEAST(x, 0) FROM t"


def test_date_modifiers():
    assert my.translate("SELECT 1 WHERE d >= date(?, '-30 days')") == \
        "SELECT 1 WHERE d >= DATE_SUB(CAST(%s AS DATE), INTERVAL 30 DAY)"
    assert my.translate("SELECT 1 WHERE d <= date('now', '+2 months')") == \
        "SELECT 1 WHERE d <= DATE_ADD(CURRENT_DATE, INTERVAL 2 MONTH)"
    assert my.translate("SELECT date('now')") == "SELECT CURRENT_DATE"
    assert my.translate("SELECT datetime('now')") == "SELECT CURRENT_TIMESTAMP"
    with pytest.raises(UnsupportedSQL):
        my.translate("SELECT date(d, '+1 fortnight')")


def test_insert_or_ignore_becomes_insert_ignore():
    out = my.translate("INSERT OR IGNORE INTO t (a) VALUES (?)")
    assert out == "INSERT IGNORE INTO t (a) VALUES (%s)"


def test_insert_or_replace_becomes_on_duplicate_key_update():
    """MySQL's ON DUPLICATE KEY fires on ANY unique key, which is SQLite's
    INSERT OR REPLACE rule -- so unlike the PostgreSQL path, no key has to be
    chosen and no pk_of() lookup is needed."""
    out = my.translate("INSERT OR REPLACE INTO fii (date, net) VALUES (?, ?)")
    assert out == ("INSERT INTO fii (date, net) VALUES (%s, %s) "
                   "ON DUPLICATE KEY UPDATE `date`=VALUES(`date`), `net`=VALUES(`net`)")


def test_on_conflict_do_update_becomes_values_form():
    out = my.translate("INSERT INTO s (day, ticks, n) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET "
                       "ticks=excluded.ticks, n=n+excluded.n")
    assert "ON DUPLICATE KEY UPDATE" in out
    assert "ticks=VALUES(ticks)" in out
    # the target-row reference stays bare; only `excluded.` becomes VALUES()
    assert "n=n+VALUES(n)" in out
    assert "ON CONFLICT" not in out


def test_on_conflict_do_nothing_becomes_insert_ignore():
    out = my.translate("INSERT INTO t (a) VALUES (?) ON CONFLICT DO NOTHING")
    assert out == "INSERT IGNORE INTO t (a) VALUES (%s)"
    out2 = my.translate("INSERT INTO t (a, b) VALUES (?, ?) ON CONFLICT (a) DO NOTHING")
    assert out2 == "INSERT IGNORE INTO t (a, b) VALUES (%s, %s)"


def test_pragma_and_catalog():
    assert my.translate("PRAGMA journal_mode=WAL") == "SELECT 'wal' AS journal_mode"
    assert my.translate("PRAGMA foreign_keys=ON") == "SELECT 1"
    assert my.translate("PRAGMA integrity_check") == "SELECT 'ok' AS integrity_check"
    ti = my.translate('PRAGMA table_info("prices")')
    assert "information_schema.COLUMNS" in ti and "TABLE_NAME='prices'" in ti
    m = my.translate("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?")
    assert "information_schema.TABLES" in m and "DATABASE()" in m


def test_unsupported_constructs_raise():
    # MySQL has no rowid/ctid equivalent, so entry-order tie-breaking cannot be
    # reproduced -- refuse rather than silently reorder results.
    with pytest.raises(UnsupportedSQL):
        my.translate("SELECT * FROM led ORDER BY d, rowid")
    with pytest.raises(UnsupportedSQL):
        my.translate("SELECT strftime('%Y', d) FROM t")


# ── translation: DDL ──────────────────────────────────────────────────────

def test_integer_primary_key_becomes_auto_increment():
    out = my.ddl("CREATE TABLE t (id INTEGER PRIMARY KEY, v REAL)")
    assert "BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY" in out
    assert "DOUBLE" in out and "REAL" not in out
    assert out.endswith(my.TABLE_SUFFIX)


def test_text_in_a_key_becomes_varchar():
    """MySQL cannot index TEXT without a prefix length, and ATIP's schema is full of
    TEXT primary keys."""
    out = my.ddl("CREATE TABLE s (strategy_id TEXT PRIMARY KEY, name TEXT NOT NULL)",
                 keyed={"strategy_id"})
    assert f"VARCHAR({my.KEYED_TEXT_LEN})" in out
    # a non-key TEXT column stays TEXT
    assert re.search(r"`name` TEXT NOT NULL", out)


def test_text_with_a_default_becomes_varchar():
    """MySQL: "BLOB, TEXT ... can't have a default value"."""
    out = my.ddl("CREATE TABLE s (id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'DRAFT')")
    assert f"VARCHAR({my.DEFAULTED_TEXT_LEN}) NOT NULL DEFAULT 'DRAFT'" in out
    assert "TEXT NOT NULL DEFAULT" not in out


def test_keyed_columns_sees_a_later_create_index():
    """A CREATE INDEX can key a column the CREATE TABLE says nothing about, so the
    keyed set has to be gathered over the whole statement list first."""
    stmts = ["CREATE TABLE p (sym TEXT, d DATE, close REAL)",
             "CREATE UNIQUE INDEX ix ON p(sym, d)"]
    keyed = my.keyed_columns(stmts)
    assert keyed["p"] == {"sym", "d"}
    out = my.ddl(stmts[0], keyed["p"])
    assert f"`sym` VARCHAR({my.KEYED_TEXT_LEN})" in out


def test_composite_primary_key_columns_are_keyed():
    stmt = ("CREATE TABLE backtest_trade (run_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "symbol TEXT NOT NULL, PRIMARY KEY (run_id, seq))")
    keyed = my.keyed_columns([stmt])["backtest_trade"]
    assert keyed == {"run_id", "seq"}
    out = my.ddl(stmt, keyed)
    assert f"`run_id` VARCHAR({my.KEYED_TEXT_LEN}) NOT NULL" in out
    assert "`symbol` TEXT NOT NULL" in out          # not in the key -> stays TEXT


def test_every_identifier_is_quoted():
    """Reserved words differ across MySQL 8 / MariaDB 10.6 / 10.11; quoting
    everything is version-proof. ATIP really does have columns called `rows`,
    `status`, `key`, `rank` and `format`."""
    out = my.ddl("CREATE TABLE audit_export (export_id TEXT PRIMARY KEY, rows INTEGER, "
                 "status TEXT, `key` TEXT)", keyed={"export_id"})
    for col in ("export_id", "rows", "status", "key"):
        assert f"`{col}`" in out


def test_sqlite_only_ddl_noise_is_dropped():
    out = my.ddl("CREATE TABLE t (a TEXT COLLATE NOCASE, b INTEGER PRIMARY KEY AUTOINCREMENT) WITHOUT ROWID")
    assert "COLLATE NOCASE" not in out and "AUTOINCREMENT" not in out and "WITHOUT ROWID" not in out


def test_expression_defaults():
    out = my.ddl("CREATE TABLE t (id INTEGER PRIMARY KEY, c TIMESTAMP DEFAULT (datetime('now')), "
                 "d DATE DEFAULT (date('now')))")
    assert "DEFAULT CURRENT_TIMESTAMP" in out
    assert "DEFAULT (CURRENT_DATE)" in out


def test_create_index_drops_if_not_exists_and_quotes():
    out = my.ddl("CREATE UNIQUE INDEX IF NOT EXISTS ix_a ON prices(sym, d DESC)")
    assert "IF NOT EXISTS" not in out
    assert out == "CREATE UNIQUE INDEX `ix_a` ON `prices` (`sym`, `d` DESC)"


def test_trigger_append_only_guard():
    out = my.trigger_ddl("CREATE TRIGGER g BEFORE UPDATE ON perf_ledger "
                         "BEGIN SELECT RAISE(ABORT, 'append only'); END")
    assert "SIGNAL SQLSTATE '45000'" in out and "append only" in out
    # a WHEN clause has no MySQL trigger equivalent -> becomes an IF in the body
    out2 = my.trigger_ddl("CREATE TRIGGER g2 BEFORE DELETE ON t WHEN old.locked=1 "
                          "BEGIN SELECT RAISE(ABORT, 'locked'); END")
    assert "IF old.locked=1 THEN" in out2 and "END IF;" in out2
    with pytest.raises(UnsupportedSQL):
        my.trigger_ddl("CREATE TRIGGER x AFTER INSERT ON t BEGIN UPDATE t SET a=1; END")


def test_reserved_columns_reports_hand_written_query_risk():
    rep = my.reserved_columns(["CREATE TABLE audit_export (export_id TEXT PRIMARY KEY, rows INTEGER)"])
    assert rep["audit_export"] == ["rows"]


# ── live server: the whole real schema ────────────────────────────────────

def _all_schema_ddl():
    """Every CREATE TABLE / CREATE INDEX string in every schema module."""
    import db.schema as S
    stmts = []
    for name in dir(S):
        if re.fullmatch(r"(W\d+\w*|WEALTH)_TABLES", name):
            for _t, ddls in getattr(S, name).items():
                stmts.extend(ddls)
    for mod in ("schema_w28b", "schema_w34", "schema_w35", "schema_w36", "schema_w37", "schema_w38"):
        m = __import__(f"db.{mod}", fromlist=["x"])
        for n in dir(m):
            if n.endswith("_TABLES"):
                for _t, ddls in getattr(m, n).items():
                    stmts.extend(ddls)
    return [s for s in stmts if isinstance(s, str) and s.strip()]


def test_whole_schema_translates_without_error():
    """Runs with no server: every statement must at least translate."""
    stmts = _all_schema_ddl()
    assert len(stmts) > 200, "schema modules did not load"
    keyed = my.keyed_columns(stmts)
    for s in stmts:
        my.ddl(s, keyed.get(my._table_of(s), set()))     # must not raise


@pytest.fixture
def live_db():
    """A freshly created, empty database on the live server."""
    import pymysql
    from urllib.parse import urlparse
    u = urlparse(LIVE_URL)
    name = (u.path or "/atip_test").lstrip("/")
    conn = pymysql.connect(host=u.hostname or "localhost", port=u.port or 3306,
                           user=u.username or "root", password=u.password or "",
                           unix_socket=os.environ.get("ATIP_TEST_MYSQL_SOCKET") or None,
                           charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
    cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    cur.execute(f"USE `{name}`")
    yield conn, cur
    conn.close()


@live_only
def test_whole_schema_executes_on_a_real_server(live_db):
    """The real proof: 186 tables and 67 indexes created by MySQL itself."""
    conn, cur = live_db
    stmts = _all_schema_ddl()
    keyed = my.keyed_columns(stmts)
    creates = [s for s in stmts if re.match(r"\s*CREATE\s+TABLE", s, re.I)]
    indexes = [s for s in stmts if re.match(r"\s*CREATE\s+(UNIQUE\s+)?INDEX", s, re.I)]
    for s in creates + indexes:
        cur.execute(my.ddl(s, keyed.get(my._table_of(s), set())))
    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()")
    assert cur.fetchone()[0] == len(creates)


@live_only
def test_upsert_round_trip_on_a_real_server(live_db):
    """INSERT OR REPLACE / OR IGNORE semantics must match SQLite's on real MySQL."""
    conn, cur = live_db
    cur.execute(my.ddl("CREATE TABLE fii (date DATE PRIMARY KEY, net REAL, note TEXT)",
                       keyed={"date"}))

    cur.execute(my.translate("INSERT OR REPLACE INTO fii (date, net) VALUES (?, ?)"), ("2026-01-02", 1.5))
    cur.execute(my.translate("INSERT OR REPLACE INTO fii (date, net) VALUES (?, ?)"), ("2026-01-02", 9.75))
    cur.execute("SELECT net FROM fii WHERE date='2026-01-02'")
    assert float(cur.fetchone()[0]) == 9.75, "INSERT OR REPLACE must overwrite on the key"

    # OR IGNORE must keep the existing row
    cur.execute(my.translate("INSERT OR IGNORE INTO fii (date, net) VALUES (?, ?)"), ("2026-01-02", 0.0))
    cur.execute("SELECT net FROM fii WHERE date='2026-01-02'")
    assert float(cur.fetchone()[0]) == 9.75

    cur.execute("SELECT COUNT(*) FROM fii")
    assert cur.fetchone()[0] == 1


@live_only
def test_on_conflict_accumulate_on_a_real_server(live_db):
    """`n=n+excluded.n` must accumulate against the stored row, not the new one."""
    conn, cur = live_db
    cur.execute(my.ddl("CREATE TABLE s (day DATE PRIMARY KEY, ticks INTEGER, n INTEGER)", keyed={"day"}))
    sql = my.translate("INSERT INTO s (day, ticks, n) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET "
                       "ticks=excluded.ticks, n=n+excluded.n")
    cur.execute(sql, ("2026-01-02", 10, 5))
    cur.execute(sql, ("2026-01-02", 20, 7))
    cur.execute("SELECT ticks, n FROM s WHERE day='2026-01-02'")
    ticks, n = cur.fetchone()
    assert (ticks, n) == (20, 12)


@live_only
def test_append_only_trigger_blocks_update_on_a_real_server(live_db):
    import pymysql
    conn, cur = live_db
    cur.execute(my.ddl("CREATE TABLE perf_ledger (id INTEGER PRIMARY KEY, v REAL)"))
    cur.execute(my.trigger_ddl("CREATE TRIGGER g BEFORE UPDATE ON perf_ledger "
                               "BEGIN SELECT RAISE(ABORT, 'append only'); END"))
    cur.execute("INSERT INTO perf_ledger (v) VALUES (1.0)")
    with pytest.raises(pymysql.err.DatabaseError) as e:
        cur.execute("UPDATE perf_ledger SET v=2.0 WHERE id=1")
    assert "append only" in str(e.value)


@live_only
def test_connection_wrapper_is_sqlite_shaped(live_db):
    """MySQLConnection must behave like the sqlite3 connection the code expects:
    ? placeholders, rows readable by index and by column name."""
    conn, cur = live_db
    from urllib.parse import urlparse
    u = urlparse(LIVE_URL)
    name = (u.path or "/atip_test").lstrip("/")
    cur.execute(my.ddl("CREATE TABLE t (id INTEGER PRIMARY KEY, sym TEXT, px REAL)"))
    cur.execute("INSERT INTO t (sym, px) VALUES ('ACME', 12.5)")

    c = my.MySQLConnection(LIVE_URL.rsplit("/", 1)[0] + "/" + name)
    try:
        row = c.execute("SELECT id, sym, px FROM t WHERE sym=?", ("ACME",)).fetchone()
        assert row[1] == "ACME"            # by index
        assert row["sym"] == "ACME"        # by name, like sqlite3.Row
        assert float(row["px"]) == 12.5
        assert row.keys() == ["id", "sym", "px"]
    finally:
        c.close()


# ── a key declared by a separate CREATE INDEX ─────────────────────────────

def test_index_target_reads_the_table_and_columns():
    assert my.index_target("CREATE INDEX IF NOT EXISTS i ON alert_log (dedupe_key)") \
        == ("alert_log", ["dedupe_key"])
    # a prefix length or a direction is not part of WHICH columns are keyed
    assert my.index_target("CREATE UNIQUE INDEX i ON t (`a`(191) DESC, b ASC)") == ("t", ["a", "b"])
    assert my.index_target("CREATE TABLE t (a TEXT)") is None
    assert my.index_target("INSERT INTO t VALUES (1)") is None


def test_one_statement_at_a_time_cannot_see_a_later_key():
    """The reason _widen_keyed_text() has to exist.

    ddl() widens a TEXT column it is told is keyed, but a connection is handed one
    statement at a time, so this CREATE TABLE cannot know the CREATE INDEX below it
    will key dedupe_key. MySQL then rejects the index with errno 1170.
    """
    create = "CREATE TABLE alert_log (id INTEGER PRIMARY KEY, dedupe_key TEXT)"
    index = "CREATE INDEX idx_dedupe ON alert_log (dedupe_key)"

    alone = my.ddl(create, my.keyed_columns([create]).get("alert_log", set()))
    assert re.search(r"`dedupe_key`\s+TEXT\b", alone, re.I), alone

    whole = my.ddl(create, my.keyed_columns([create, index]).get("alert_log", set()))
    assert f"VARCHAR({my.KEYED_TEXT_LEN})" in whole


@live_only
def test_the_connection_creates_an_index_on_a_text_column(live_db):
    """What used to fail: a CREATE TABLE and its CREATE INDEX arriving separately,
    as every one of init_db()'s hundreds of execute() calls does."""
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE alert_log (id INTEGER PRIMARY KEY, dedupe_key TEXT, note TEXT)")
        c.execute("CREATE INDEX idx_dedupe ON alert_log (dedupe_key)")     # errno 1170 before
        c.commit()
        cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='alert_log' "
                    "AND COLUMN_NAME IN ('dedupe_key','note')")
        types = dict(cur.fetchall())
        assert types["dedupe_key"] == f"varchar({my.KEYED_TEXT_LEN})"
        assert types["note"] == "text", "only the keyed column is widened"
        cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND INDEX_NAME='idx_dedupe'")
        assert cur.fetchone()[0] == 1
    finally:
        c.close()


@live_only
def test_widening_keeps_not_null_the_collation_and_the_default(live_db):
    """MODIFY COLUMN replaces the whole definition, so anything not repeated is
    dropped. The definition is taken from SHOW CREATE TABLE for that reason."""
    conn, cur = live_db
    cur.execute("CREATE TABLE t (id INT, a TEXT NOT NULL, "
                "b TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin, "
                "c TEXT DEFAULT ('x') COMMENT 'kept')")
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE INDEX i ON t (a, b, c)")
        c.commit()
    finally:
        c.close()
    cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLLATION_NAME, "
                "COLUMN_DEFAULT, COLUMN_COMMENT FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='t' AND COLUMN_NAME<>'id' "
                "ORDER BY COLUMN_NAME")
    rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert all(r[0] == f"varchar({my.KEYED_TEXT_LEN})" for r in rows.values())
    assert rows["a"][1] == "NO", "NOT NULL must survive"
    assert rows["b"][2] == "utf8mb4_bin", "the collation must survive"
    assert rows["c"][3] is not None and rows["c"][4] == "kept", "default and comment must survive"


@live_only
def test_a_unique_index_still_constrains_the_whole_value(live_db):
    """Why the column is widened rather than the index given a prefix length:
    `col(191)` would enforce uniqueness over the first 191 characters only."""
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT)")
        c.execute("CREATE UNIQUE INDEX i ON t (k)")
        c.commit()
    finally:
        c.close()
    cur.execute("SELECT SUB_PART FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND INDEX_NAME='i'")
    assert cur.fetchone()[0] is None, "a prefix length would show as SUB_PART"


@live_only
def test_widening_refuses_rather_than_truncating_existing_data(live_db):
    """Narrowing raises 1406 under a strict sql_mode and TRUNCATES under a
    permissive one, so the check is made before the ALTER, where it can say what
    it found."""
    conn, cur = live_db
    cur.execute("CREATE TABLE t (id INT, k TEXT)")
    cur.execute("INSERT INTO t VALUES (1, REPEAT('z', 300))")
    c = my.MySQLConnection(LIVE_URL)
    try:
        with pytest.raises(UnsupportedSQL) as e:
            c.execute("CREATE INDEX i ON t (k)")
        assert "t.k" in str(e.value) and "300" in str(e.value)
    finally:
        c.close()
    cur.execute("SELECT CHAR_LENGTH(k) FROM t")
    assert cur.fetchone()[0] == 300, "the data must be untouched"


@live_only
def test_an_index_on_a_missing_table_reports_the_missing_table(live_db):
    """The widening must not invent an error of its own for a missing table: the
    caller gets MySQL's errno, as the sqlite3 class SQLite raises for it."""
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        with pytest.raises(sqlite3.OperationalError) as e:
            c.execute("CREATE INDEX i ON nope (k)")
        assert e.value.args[0] == 1146            # ER_NO_SUCH_TABLE
    finally:
        c.close()


@live_only
def test_the_runtime_path_builds_the_same_schema_as_the_whole_list_path(live_db):
    """The point of widening the column rather than prefixing the index: a database
    built one statement at a time by MySQLConnection must match one built by
    tools/sqlite_to_mysql.py, which translates the whole schema at once."""
    conn, cur = live_db
    stmts = _all_schema_ddl()

    def columns(db):
        cur.execute("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s", (db,))
        return {(t, c): (ty, n) for t, c, ty, n in cur.fetchall()}

    cur.execute("SELECT DATABASE()")        # the fixture USEs it rather than connecting to it
    here = cur.fetchone()[0]
    other = f"{here}_whole"
    cur.execute(f"DROP DATABASE IF EXISTS `{other}`")
    cur.execute(f"CREATE DATABASE `{other}` CHARACTER SET utf8mb4")

    # one statement at a time, through the connection -- the runtime path
    c = my.MySQLConnection(LIVE_URL)
    try:
        for s in stmts:
            try:
                c.execute(s)
            except Exception as e:
                if e.args[0] not in (1060, 1061):
                    raise
        c.commit()
    finally:
        c.close()

    # the whole list up front -- the migration-tool path
    keyed = my.keyed_columns(stmts)
    cur.execute(f"USE `{other}`")
    for s in stmts:
        try:
            cur.execute(my.ddl(s, keyed.get(my._table_of(s), set())))
        except Exception as e:
            if e.args[0] not in (1060, 1061):
                raise
    cur.execute(f"USE `{here}`")

    runtime, whole = columns(here), columns(other)
    cur.execute(f"DROP DATABASE IF EXISTS `{other}`")
    assert runtime and runtime == whole, \
        f"{len(set(runtime.items()) ^ set(whole.items()))} columns differ"


# ── errors: the sqlite3 classes the migration helpers catch ───────────────

def test_the_error_mapping_follows_sqlites_categories_not_the_class_names():
    """db/schema.py's additive migrations are built on `except
    sqlite3.OperationalError`. PyMySQL's hierarchy is unrelated to sqlite3's, and
    it also sorts the same conditions into different classes, so the mapping has
    to go by condition. All errnos measured against MySQL 8.0.46."""
    pymysql = pytest.importorskip("pymysql")

    def mapped(cls, errno):
        return type(my.as_sqlite_error(cls(errno, "msg")))

    # a violated constraint -> IntegrityError, as SQLite raises
    assert mapped(pymysql.err.IntegrityError, 1062) is sqlite3.IntegrityError   # duplicate key
    assert mapped(pymysql.err.IntegrityError, 1048) is sqlite3.IntegrityError   # NOT NULL
    assert mapped(pymysql.err.IntegrityError, 1452) is sqlite3.IntegrityError   # foreign key
    # PyMySQL calls a CHECK violation Operational; SQLite calls it Integrity
    assert mapped(pymysql.err.OperationalError, 3819) is sqlite3.IntegrityError

    # everything else a statement can get wrong -> OperationalError, as SQLite raises
    assert mapped(pymysql.err.OperationalError, 1060) is sqlite3.OperationalError   # dup column
    assert mapped(pymysql.err.OperationalError, 1061) is sqlite3.OperationalError   # dup key name
    assert mapped(pymysql.err.OperationalError, 1170) is sqlite3.OperationalError   # TEXT in a key
    # PyMySQL calls a missing table and bad syntax Programming; SQLite calls both Operational
    assert mapped(pymysql.err.ProgrammingError, 1146) is sqlite3.OperationalError
    assert mapped(pymysql.err.ProgrammingError, 1064) is sqlite3.OperationalError


def test_the_error_mapping_keeps_the_errno_and_leaves_other_errors_alone():
    pymysql = pytest.importorskip("pymysql")
    out = my.as_sqlite_error(pymysql.err.OperationalError(1060, "Duplicate column name 'a'"))
    assert out.args == (1060, "Duplicate column name 'a'"), "callers switch on args[0]"
    assert "1060" in str(out) and "Duplicate column name" in str(out)

    # not a database error: returned untouched, so UnsupportedSQL and ordinary
    # programming mistakes are not disguised as database trouble
    for exc in (UnsupportedSQL("no translation"), ValueError("nope"), KeyError("k")):
        assert my.as_sqlite_error(exc) is exc


@live_only
def test_a_duplicate_column_is_catchable_as_sqlite3_operationalerror(live_db):
    """What db/schema.py's _add_missing_columns relies on to make ALTER TABLE ADD
    COLUMN idempotent. Before the mapping it raised pymysql.err.OperationalError,
    which that `except sqlite3.OperationalError` clause does not catch, so every
    additive migration crashed init_db() on MySQL."""
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a REAL)")
        with pytest.raises(sqlite3.OperationalError) as e:
            c.execute("ALTER TABLE t ADD COLUMN a REAL")
        assert e.value.args[0] == 1060
    finally:
        c.close()


@live_only
def test_a_constraint_violation_is_catchable_as_sqlite3_integrityerror(live_db):
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k VARCHAR(10) UNIQUE, n INTEGER NOT NULL)")
        c.execute("INSERT INTO t (k, n) VALUES (?, ?)", ("x", 1))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO t (k, n) VALUES (?, ?)", ("x", 2))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO t (k, n) VALUES (?, ?)", ("y", None))
    finally:
        c.close()


@live_only
def test_create_index_if_not_exists_is_a_no_op_the_second_time(live_db):
    """MySQL has no IF NOT EXISTS for CREATE INDEX, so ddl() strips it and the
    connection honours it instead -- init_db() runs the whole schema on every
    start with no try/except around those statements."""
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a REAL)")
        c.execute("CREATE INDEX IF NOT EXISTS i ON t (a)")
        again = c.execute("CREATE INDEX IF NOT EXISTS i ON t (a)")      # errno 1061 before
        # shaped like sqlite3's cursor for a DDL statement that did nothing
        assert again.fetchone() is None and again.fetchall() == [] and list(again) == []
        assert again.description is None

        # without the clause, a duplicate index name is still an error
        with pytest.raises(sqlite3.OperationalError) as e:
            c.execute("CREATE INDEX i ON t (a)")
        assert e.value.args[0] == 1061
        c.commit()
    finally:
        c.close()


@live_only
def test_sqlite_master_queries_run_on_the_server(live_db):
    """`sql` is a reserved word in MySQL 8, so the substituted subquery's own alias
    and any caller reference to that column have to be backticked. Translating the
    string was not enough: this executes it, which is how errno 1064 was found."""
    conn, cur = live_db
    c = my.MySQLConnection(LIVE_URL)
    try:
        c.execute("CREATE TABLE prices_daily (id INTEGER PRIMARY KEY, sym TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sym ON prices_daily (sym)")
        c.commit()

        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                         ("prices_daily",)).fetchone() is not None
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                         ("nope",)).fetchone() is None
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                         ("idx_sym",)).fetchone() is not None
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "prices_daily" in names
        # the sql column itself, which is what tripped errno 1064
        rows = c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                         "WHERE type='table' AND name=?", ("prices_daily",)).fetchall()
        assert len(rows) == 1 and rows[0]["name"] == "prices_daily"
    finally:
        c.close()


@live_only
def test_init_db_creates_the_whole_schema_on_mysql_and_is_idempotent(live_db):
    """The end of the line for all of this: ATIP's real init_db(), against a real
    MySQL server, twice. It is hundreds of bare execute() calls, so every gap above
    -- the TEXT keys, the error classes, the stripped IF NOT EXISTS, the reserved
    `sql` alias -- showed up here first."""
    conn, cur = live_db
    import db.schema as schema

    original = schema.get_connection
    held = []

    def _conn():
        c = my.MySQLConnection(LIVE_URL)
        held.append(c)
        return c

    schema.get_connection = _conn
    try:
        schema.init_db()
        schema.init_db()            # every start runs it again
    finally:
        schema.get_connection = original
        for c in held:
            try:
                c.commit()
                c.close()
            except Exception:
                pass

    cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
    assert cur.fetchone()[0] > 200
    cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS s "
                "JOIN information_schema.COLUMNS c USING (TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME) "
                "WHERE s.TABLE_SCHEMA=DATABASE() AND c.DATA_TYPE LIKE '%text'")
    assert cur.fetchone()[0] == 0, "no TEXT column may be left in a key"


@live_only
def test_a_missing_table_lets_the_risk_check_fall_back(live_db):
    """The consequence that makes the error mapping worth having at run time, as
    opposed to during init_db(). orders/risk.py says it plainly: order_log and the
    paper tables are created lazily by whatever writes them, so a missing table
    means "nothing placed, nothing held" and MUST NOT raise -- or "configuring a
    limit would break the first order ATIP ever places". That fallback is an
    `except sqlite3.OperationalError`, which PyMySQL never raised."""
    conn, cur = live_db
    from orders.risk import _query
    c = my.MySQLConnection(LIVE_URL)
    try:
        assert _query(c, "SELECT COUNT(*) FROM order_log", (), [(0,)]) == [(0,)]
    finally:
        c.close()


# ── what --init reports ───────────────────────────────────────────────────

@pytest.fixture
def runtime_url_cache():
    """Let a test change the gated runtime URL without poisoning the process.

    pg_runtime_url() and mysql_runtime_url() memoise into module-level lists, and
    monkeypatch does not restore those -- a test that leaves one populated sends
    every later get_connection() in the session to that host. An earlier version of
    the test below did exactly that, and 62 tests downstream failed trying to
    resolve its fake DSN, so this puts both caches back the way it found them.
    """
    import db.schema as schema
    pg, my = list(schema._PG_URL), list(schema._MYSQL_URL)
    try:
        yield schema
    finally:
        schema._PG_URL[:] = pg
        schema._MYSQL_URL[:] = my


def test_describe_target_names_the_database_that_was_actually_initialised(
        tmp_path, monkeypatch, runtime_url_cache):
    """init_db() used to log and return DB_PATH no matter which backend it had just
    built, so `main.py --init` against a server told the operator they were on
    SQLite and named a file it had not written.

    The description has to come from the GATED urls get_connection() dispatches on:
    db.backend.database_url() reads ATIP_DATABASE_URL whether or not the config lets
    it through, so it would name an engine the runtime is not using -- the same class
    of wrong answer, in the other direction.
    """
    import json
    schema = runtime_url_cache

    cfg_dir = tmp_path / "atip_data"
    cfg_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    def describe(url, database_cfg):
        (cfg_dir / "config.json").write_text(json.dumps({"database": database_cfg}))
        if url is None:
            monkeypatch.delenv("ATIP_DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("ATIP_DATABASE_URL", url)
        schema._PG_URL.clear()
        schema._MYSQL_URL.clear()
        return schema.describe_target()

    gated = {"allow_experimental": True}
    my_dsn = "mysql://atip:s3cr3t@db.example.com/atip"
    pg_dsn = "postgresql://atip:s3cr3t@db.example.com/atip"

    assert describe(None, {}).startswith("SQLite")
    assert describe(my_dsn, {"backend": "mysql", **gated}).startswith("MySQL")
    assert describe(pg_dsn, {"backend": "postgresql", **gated}).startswith("PostgreSQL")

    # a URL the gates do NOT let through is not the runtime's database
    assert describe(my_dsn, {"backend": "mysql", "allow_experimental": False}).startswith("SQLite")
    assert describe(my_dsn, {"backend": "postgresql", **gated}).startswith("SQLite")

    # a DSN carries a password and this string is both logged and printed
    for dsn, cfg in ((my_dsn, {"backend": "mysql", **gated}),
                     (pg_dsn, {"backend": "postgresql", **gated})):
        out = describe(dsn, cfg)
        assert "s3cr3t" not in out, out
        assert ":***@" in out, out


def test_the_describe_target_test_left_no_dsn_cached():
    """Runs straight after the test above, in file order, and checks the thing that
    actually broke: a fake DSN left in either memoised cache redirects every later
    get_connection() in the session. It once cost 62 downstream failures."""
    import db.schema as schema
    for cache in (schema._PG_URL, schema._MYSQL_URL):
        for url in cache:
            assert url is None or "db.example.com" not in url, \
                f"a test DSN is still cached and will redirect the rest of the suite: {url}"
