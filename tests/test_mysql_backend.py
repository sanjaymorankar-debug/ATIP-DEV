"""
MySQL / MariaDB backend (db/mysql.py).

Two layers, deliberately:

  * Translation tests need no database and always run. They are the contract of
    db.mysql.translate / ddl / trigger_ddl.
  * Live tests run the whole real schema -- every CREATE TABLE and CREATE INDEX in
    every db/schema*.py module -- into an actual MySQL/MariaDB server and then
    round-trip data through it. They are skipped unless ATIP_TEST_MYSQL_URL is set,
    because the server is not part of the default test environment:

        ATIP_TEST_MYSQL_URL=mysql://root@localhost:3306/atip_test pytest tests/test_mysql_backend.py

    The database it names is DROPped and recreated, so never point it at real data.
"""

import os
import re

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
