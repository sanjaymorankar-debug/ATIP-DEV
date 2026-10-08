"""
The SQLite -> MySQL migration tool (tools/sqlite_to_mysql.py).

Two layers, like tests/test_mysql_backend.py:

  * Planning tests need no database. They cover the two properties that are easy
    to get wrong and invisible until a server rejects the schema: keys are
    collected across *every* statement before any is translated, and MySQL's
    CREATE INDEX takes no IF NOT EXISTS.
  * Live tests copy data into a real server and compare it back, and are skipped
    unless ATIP_TEST_MYSQL_URL is set. The database it names is dropped, so never
    point it at real data.
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sqlite_to_mysql import blocking, lossy_total, plan  # noqa: E402

LIVE_URL = os.environ.get("ATIP_TEST_MYSQL_URL")
live_only = pytest.mark.skipif(not LIVE_URL, reason="set ATIP_TEST_MYSQL_URL to run live MySQL tests")


def _db(tmp_path, *statements, rows=()):
    p = tmp_path / "src.db"
    c = sqlite3.connect(p)
    for s in statements:
        c.execute(s)
    for sql, params in rows:
        c.executemany(sql, params)
    c.commit()
    c.close()
    return p


# ── planning ────────────────────────────────────────────────────────────────

def test_a_later_create_index_widens_a_text_column(tmp_path):
    """The property the whole tool depends on.

    MySQL cannot key a TEXT column without a prefix length. `dedupe_key` is TEXT
    and nothing in its CREATE TABLE says it is keyed -- only the separate CREATE
    INDEX does -- so a tool that translated one statement at a time would emit
    `dedupe_key TEXT` and the index would then fail with errno 1170.
    """
    src = _db(tmp_path,
              "CREATE TABLE alert_log (id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT, created_at TIMESTAMP)",
              "CREATE INDEX idx_alert_log_key ON alert_log(dedupe_key, created_at)")
    ddl = "\n".join(plan(src, scan=False)["ddl"])
    assert "`dedupe_key` VARCHAR(191)" in ddl
    assert "`dedupe_key` TEXT" not in ddl


def test_a_text_primary_key_is_widened(tmp_path):
    src = _db(tmp_path, "CREATE TABLE u (user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, bio TEXT)")
    ddl = "\n".join(plan(src, scan=False)["ddl"])
    assert "`user_id` VARCHAR(191) PRIMARY KEY" in ddl
    assert "`username` VARCHAR(191) NOT NULL UNIQUE" in ddl
    assert "`bio` TEXT" in ddl, "a column in no key stays TEXT"


def test_create_index_has_no_if_not_exists(tmp_path):
    """MySQL has no IF NOT EXISTS for CREATE INDEX (MariaDB does); re-running is
    made idempotent by tolerating errno 1061 instead."""
    src = _db(tmp_path,
              "CREATE TABLE t (a INTEGER, b TEXT)",
              "CREATE INDEX IF NOT EXISTS idx_t_a ON t(a)")
    ddl = plan(src, scan=False)["ddl"]
    index = [s for s in ddl if "INDEX" in s]
    assert index and all("IF NOT EXISTS" not in s for s in index), index
    assert any("CREATE TABLE IF NOT EXISTS" in s for s in ddl), "tables keep it: MySQL accepts it there"


def test_the_scan_classifies_what_mysql_would_reject(tmp_path):
    """SQLite lets any column hold any type; these are the four shapes that matters."""
    src = _db(tmp_path,
              "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, d DATE)",
              rows=[("INSERT INTO t (id, n, d) VALUES (?,?,?)", [
                  (1, 10, "2026-01-01"),            # fine
                  (2, 1234.56, "2026-01-02"),       # fractional float in INTEGER
                  (3, "not a number", "2026-01-03"),  # text in INTEGER
                  (4, 5, ""),                        # empty-string date
                  (5, 6, "not-a-date"),              # unparseable date
              ])])
    c = plan(src)["coercions"]["t"]
    assert c["n"]["fractional_float"] == 1
    assert c["n"]["non_numeric"] == 1
    assert c["d"]["empty_string"] == 1
    assert c["d"]["unparseable"] == 1


def test_a_not_null_column_blocks_rather_than_coerces(tmp_path):
    """SQLite accepted '' in a NOT NULL DATE because '' is not NULL. MySQL has no
    value for a date that is not a date, and the column forbids the only one it
    would take -- so this is not a question of precision and --allow-lossy must
    not wave it through."""
    src = _db(tmp_path,
              "CREATE TABLE t (id INTEGER PRIMARY KEY, d DATE NOT NULL)",
              rows=[("INSERT INTO t (id, d) VALUES (?,?)", [(1, "2026-01-01"), (2, ""), (3, "nope")])])
    p = plan(src)
    assert p["coercions"]["t"]["d"]["blocking"] == 2
    b = blocking(p["coercions"])
    assert len(b) == 1 and b[0]["table"] == "t" and b[0]["column"] == "d" and b[0]["rows"] == 2
    # the query it offers has to actually find them
    c = sqlite3.connect(src)
    assert {r[0] for r in c.execute(b[0]["find"].rstrip(";"))} == {2, 3}
    c.close()


def test_a_nullable_column_with_the_same_values_is_only_lossy(tmp_path):
    src = _db(tmp_path,
              "CREATE TABLE t (id INTEGER PRIMARY KEY, d DATE)",
              rows=[("INSERT INTO t (id, d) VALUES (?,?)", [(1, ""), (2, "nope")])])
    p = plan(src)
    assert not blocking(p["coercions"]), "nullable: NULL is a legitimate answer"
    assert lossy_total(p["coercions"]) == 2


def test_the_real_atip_schema_translates_completely(tmp_path):
    """Every CREATE TABLE and CREATE INDEX this release creates, with nothing
    skipped and no TEXT column left in a key."""
    import os
    import subprocess
    src = tmp_path / "atip.db"
    env = {**os.environ, "ATIP_DB_PATH": str(src)}
    r = subprocess.run([sys.executable, "-c", "from db.schema import init_db; init_db()"], env=env,
                       cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]

    p = plan(src, scan=False)
    assert p["skipped"] == [], p["skipped"]
    assert len(p["tables"]) > 150, f"expected the whole schema, got {len(p['tables'])} tables"

    keyed_text = []
    for stmt in p["ddl"]:
        if "CREATE TABLE" not in stmt:
            continue
        for line in stmt.split("\n"):
            if " TEXT" in line and ("PRIMARY KEY" in line or "UNIQUE" in line):
                keyed_text.append(line.strip())
    assert keyed_text == [], f"TEXT columns left in a key: {keyed_text}"


def test_every_tool_parses_on_the_supported_python():
    """setup.py declares python_requires=">=3.11" and CI pins 3.12, so a file using
    3.12-only syntax passes CI and fails for anyone on the declared minimum.

    tools/export_mysql.py did exactly that -- a backslash inside an f-string
    expression, legal only from 3.12 -- and nothing imported or tested it, so it
    sat broken. Compiling every tool here is cheap and catches the whole class.
    """
    import py_compile
    root = Path(__file__).resolve().parents[1]
    broken = []
    for f in sorted((root / "tools").glob("*.py")):
        try:
            py_compile.compile(str(f), doraise=True, cfile=str(tmp_cfile(f)))
        except py_compile.PyCompileError as e:
            broken.append(f"{f.name}: {e.msg.strip().splitlines()[-1]}")
    assert broken == [], "tools that do not compile:\n  " + "\n  ".join(broken)


def tmp_cfile(f):
    import tempfile
    return Path(tempfile.gettempdir()) / (f.stem + ".pyc")


# ── live ────────────────────────────────────────────────────────────────────

@live_only
def test_migrating_data_preserves_it_exactly(tmp_path):
    from tools.sqlite_to_mysql import execute
    src = _db(tmp_path,
              "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, s TEXT, n INTEGER, d DATE)",
              "CREATE INDEX idx_t_s ON t(s)",
              rows=[("INSERT INTO t (id, s, n, d) VALUES (?,?,?,?)", [
                  (1, "plain", 1, "2026-01-01"),
                  (2, "टाटा मोटर्स 🚗", 2, "2026-01-02"),          # 4-byte utf8mb4
                  (3, "quote ' double \" back \\ slash", 3, "2026-01-03"),
                  (4, None, 2 ** 53 + 1, "2026-01-04"),            # past a float's exact range
                  (900, "high id", 5, "2026-01-05"),               # AUTO_INCREMENT must pass it
              ])])
    import pymysql
    from urllib.parse import urlparse
    u = urlparse(LIVE_URL.replace("mysql+pymysql://", "mysql://"))
    db = (u.path or "").lstrip("/")
    admin = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username or "root",
                            password=u.password or "", charset="utf8mb4")
    admin.cursor().execute(f"DROP DATABASE IF EXISTS `{db}`")
    admin.cursor().execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    admin.close()

    p = plan(src)
    assert not blocking(p["coercions"])
    r = execute(src, LIVE_URL, p)
    assert r["ok"], r

    sc = sqlite3.connect(src)
    my = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username or "root",
                         password=u.password or "", database=db, charset="utf8mb4")
    try:
        want = sc.execute("SELECT id, s, n, d FROM t ORDER BY id").fetchall()
        cur = my.cursor()
        cur.execute("SELECT id, s, n, CAST(d AS CHAR) FROM t ORDER BY id")
        got = cur.fetchall()
        assert len(got) == len(want)
        for a, b in zip(want, got):
            assert a[0] == b[0] and a[1] == b[1] and a[2] == b[2], f"{a!r} != {b!r}"
            assert a[3] == b[3], f"date {a[3]!r} != {b[3]!r}"
        # a copied explicit id must not be handed out again
        cur.execute("SELECT AUTO_INCREMENT FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_name='t'")
        assert cur.fetchone()[0] == 901
    finally:
        sc.close()
        my.close()


@live_only
def test_running_it_twice_does_not_fail_on_the_index(tmp_path):
    """MySQL has no CREATE INDEX IF NOT EXISTS, so the second run must tolerate
    errno 1061 rather than abort."""
    from tools.sqlite_to_mysql import execute
    src = _db(tmp_path,
              "CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT)",
              "CREATE INDEX idx_t_s ON t(s)",
              rows=[("INSERT INTO t (id, s) VALUES (?,?)", [(1, "a")])])
    import pymysql
    from urllib.parse import urlparse
    u = urlparse(LIVE_URL.replace("mysql+pymysql://", "mysql://"))
    db = (u.path or "").lstrip("/")
    admin = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username or "root",
                            password=u.password or "", charset="utf8mb4")
    admin.cursor().execute(f"DROP DATABASE IF EXISTS `{db}`")
    admin.cursor().execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    admin.close()

    p = plan(src)
    assert execute(src, LIVE_URL, p)["ok"]
    # the schema half must be re-runnable; the rows would double, so only the DDL
    # is replayed here, which is the part errno 1061 guards
    from db.backend import MySQLConnection
    my = MySQLConnection(LIVE_URL)
    try:
        for stmt in p["ddl"]:
            try:
                my.execute(stmt)
            except Exception as e:
                assert getattr(e, "args", (None,))[0] == 1061, e
        my.commit()
    finally:
        my.close()


@live_only
def test_migrating_perf_ledger_keeps_the_entry_order(tmp_path):
    """perf_ledger's MySQL-only `seq` replaces SQLite's rowid as the tie-breaker the
    P&L ordering depends on, and it is assigned as rows are inserted -- so a migrated
    table must come back in the source's entry order, not in any index's order.

    The copy names every column, so SQLite serves it with a table scan and rowid
    order follows without being asked for; the tool asks anyway, because SQLite
    promises no order without ORDER BY. This test pins the property the P&L needs,
    whichever way the planner leans: it passes today with or without that clause.
    """
    from tools.sqlite_to_mysql import execute
    # inserted in an order that disagrees with every index on the table
    entry = [("z-first", "2026-03-01", "09:00", 10.0),
             ("a-second", "2026-01-01", "10:00", 20.0),
             ("m-third", "2026-02-01", "11:00", 30.0)]
    src = _db(tmp_path,
              "CREATE TABLE perf_ledger (txn_id TEXT PRIMARY KEY, trade_date DATE, ts TEXT, qty REAL)",
              "CREATE INDEX idx_pl_date ON perf_ledger(trade_date, txn_id)",
              rows=[("INSERT INTO perf_ledger (txn_id, trade_date, ts, qty) VALUES (?,?,?,?)", entry)])

    import pymysql
    from urllib.parse import urlparse
    u = urlparse(LIVE_URL.replace("mysql+pymysql://", "mysql://"))
    db = (u.path or "").lstrip("/")
    conn_args = dict(host=u.hostname, port=u.port or 3306, user=u.username or "root",
                     password=u.password or "", charset="utf8mb4")
    admin = pymysql.connect(**conn_args)
    admin.cursor().execute(f"DROP DATABASE IF EXISTS `{db}`")
    admin.cursor().execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4")
    admin.close()

    p = plan(src)
    assert execute(src, LIVE_URL, p)["ok"]

    my = pymysql.connect(database=db, **conn_args)
    try:
        cur = my.cursor()
        cur.execute("SELECT txn_id FROM perf_ledger ORDER BY seq")
        assert [r[0] for r in cur.fetchall()] == [r[0] for r in entry], \
            "seq must follow the source's entry order, not an index's order"
    finally:
        my.close()


def test_sqlite_promises_no_order_without_an_order_by(tmp_path):
    """Why the copy spells out ORDER BY for the entry-order tables: the same table
    hands back a different order depending on which columns are asked for."""
    src = _db(tmp_path,
              "CREATE TABLE pl (txn_id TEXT, trade_date DATE, ts TEXT, qty REAL)",
              "CREATE INDEX idx_pl_date ON pl(trade_date, txn_id)",
              rows=[("INSERT INTO pl (txn_id, trade_date, ts, qty) VALUES (?,?,?,?)",
                     [("z-first", "2026-03-01", "09:00", 1.0),
                      ("a-second", "2026-01-01", "10:00", 2.0),
                      ("m-third", "2026-02-01", "11:00", 3.0)])])
    c = sqlite3.connect(src)
    try:
        entry = ["z-first", "a-second", "m-third"]
        every = [r[0] for r in c.execute("SELECT txn_id, trade_date, ts, qty FROM pl")]
        subset = [r[0] for r in c.execute("SELECT txn_id, trade_date FROM pl")]
        assert every == entry, "a full column list is a table scan: rowid order"
        assert subset == sorted(entry), "a covered subset comes back in index order"
        assert [r[0] for r in c.execute("SELECT txn_id, trade_date FROM pl ORDER BY rowid")] == entry
    finally:
        c.close()
