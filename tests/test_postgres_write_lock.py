"""
SQLite's BEGIN IMMEDIATE on the PostgreSQL runtime (db/postgres.py, 18 Nov 2026).

Found by the 18 Nov post-market check of the VPS: three places start their check-then-write transaction with
`BEGIN IMMEDIATE` -- ops/jobs.acquire (job locks and the scheduler leader lease), ops/migrations.apply and
orders/algo_gtt.place_dummy_buy -- and PostgreSQL rejected it as a syntax error. Job locks failed open without a
word, every Kite-retest dummy buy (S2 / S3 / S4 / S10) came back "Busy, try again" and placed nothing, and the next
new migration would have been recorded FAILED. PgConnection now carries the statement out: BEGIN IMMEDIATE takes one
database-wide advisory write lock for the transaction, waiting like SQLite's busy timeout.

  * The statement recognition needs no server and always runs.
  * The rest runs the real call sites on a live server, and is skipped unless ATIP_TEST_POSTGRES_URL is set (see
    tests/test_postgres_backend.py). The database it names is DROPped and recreated: never point it at real data.
"""

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest

from db import postgres as pg

LIVE_URL = os.environ.get("ATIP_TEST_POSTGRES_URL")
live_only = pytest.mark.skipif(not LIVE_URL, reason="set ATIP_TEST_POSTGRES_URL to run live PostgreSQL tests")


# ── recognition (no server) ───────────────────────────────────────────────
@pytest.mark.parametrize("sql, kind", [
    ("BEGIN IMMEDIATE", ("BEGIN", "IMMEDIATE")), ("begin immediate;", ("BEGIN", "IMMEDIATE")),
    ("  BEGIN EXCLUSIVE TRANSACTION ", ("BEGIN", "EXCLUSIVE")), ("BEGIN", ("BEGIN", None)),
    ("BEGIN DEFERRED", ("BEGIN", "DEFERRED")), ("COMMIT", "COMMIT"), ("END TRANSACTION", "END"),
    ("ROLLBACK", "ROLLBACK"), ("rollback;", "ROLLBACK"),
])
def test_sqlite_transaction_statements_are_recognised(sql, kind):
    m = pg._TX_CONTROL.fullmatch(sql)
    assert m, sql
    begin, mode, commit, rollback = m.groups()
    got = (begin.upper(), mode.upper() if mode else None) if begin else (commit or rollback).upper()
    assert got == kind


@pytest.mark.parametrize("sql", [
    "ROLLBACK TO SAVEPOINT atip_stmt", "RELEASE SAVEPOINT atip_stmt", "BEGIN IMMEDIATE; INSERT INTO t VALUES (1)",
    "CREATE TRIGGER trg BEFORE UPDATE ON t BEGIN SELECT RAISE(ABORT, 'x'); END", "SELECT 'BEGIN IMMEDIATE'",
    "COMMITTED", "END_DATE",
])
def test_other_statements_are_not_taken_for_transaction_control(sql):
    assert pg._TX_CONTROL.fullmatch(sql) is None


# ── live server ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def atip_pg():
    """A fresh database with ATIP's whole schema, and db.schema pointed at it for the module."""
    psycopg = pytest.importorskip("psycopg")
    from urllib.parse import urlparse, urlunparse
    import db.schema as schema
    u = urlparse(LIVE_URL)
    name = (u.path or "/atip_test").lstrip("/")
    with psycopg.connect(urlunparse(u._replace(path="/postgres")), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(schema, "_PG_URL", [LIVE_URL])
        mp.setattr(schema, "get_connection", lambda: pg.PgConnection(LIVE_URL))
        schema.init_db()
        yield schema


def _advisory_locks(raw):
    return raw.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted").fetchone()[0]


@pytest.fixture
def raw(atip_pg):
    import psycopg
    c = psycopg.connect(LIVE_URL, autocommit=True)
    c.execute("CREATE TABLE IF NOT EXISTS wl_t (n INTEGER)")
    c.execute("TRUNCATE wl_t")
    yield c
    c.close()


@live_only
def test_begin_immediate_commit_and_rollback_on_postgres(raw):
    a = pg.PgConnection(LIVE_URL)
    try:
        a.execute("BEGIN IMMEDIATE")
        assert _advisory_locks(raw) == 1, "the write lock is held for the transaction"
        a.execute("INSERT INTO wl_t VALUES (?)", (1,))
        a.execute("COMMIT")
        assert _advisory_locks(raw) == 0, "and released by the COMMIT"
        a.execute("BEGIN IMMEDIATE")
        a.execute("INSERT INTO wl_t VALUES (?)", (2,))
        a.execute("ROLLBACK")
        assert _advisory_locks(raw) == 0
        assert [r[0] for r in raw.execute("SELECT n FROM wl_t ORDER BY n")] == [1]
        a.execute("COMMIT")                                   # nothing open: a no-op, not an error
        a.execute("BEGIN")                                    # DEFERRED: no lock until a write, as in SQLite
        assert _advisory_locks(raw) == 0
        a.execute("END")
    finally:
        a.close()


@live_only
def test_a_second_writer_waits_then_gets_database_is_locked(raw):
    a, b = pg.PgConnection(LIVE_URL), pg.PgConnection(LIVE_URL)
    try:
        a.execute("BEGIN IMMEDIATE")
        b.write_lock_wait = 0.3
        t = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            b.execute("BEGIN IMMEDIATE")
        assert time.monotonic() - t >= 0.3
        assert b.execute("SELECT 1").fetchone()[0] == 1, "the refused connection stays usable"
        a.execute("COMMIT")
        b.execute("BEGIN IMMEDIATE")                          # free now
        b.execute("ROLLBACK")
    finally:
        a.close()
        b.close()


@live_only
def test_a_waiting_writer_gets_the_lock_when_the_holder_commits(raw):
    a = pg.PgConnection(LIVE_URL)
    got, errs = [], []

    def second():
        b = pg.PgConnection(LIVE_URL)
        try:
            t = time.monotonic()
            b.execute("BEGIN IMMEDIATE")
            got.append(time.monotonic() - t)
            b.execute("INSERT INTO wl_t VALUES (?)", (2,))
            b.execute("COMMIT")
        except Exception as e:                                # pragma: no cover - reported below
            errs.append(e)
        finally:
            b.close()
    try:
        a.execute("BEGIN IMMEDIATE")
        a.execute("INSERT INTO wl_t VALUES (?)", (1,))
        th = threading.Thread(target=second)
        th.start()
        time.sleep(0.4)
        assert not got, "the second writer waits while the first holds the lock"
        a.execute("COMMIT")
        th.join(10)
    finally:
        a.close()
    assert not errs and got and got[0] >= 0.35
    assert [r[0] for r in raw.execute("SELECT n FROM wl_t ORDER BY n")] == [1, 2]


@live_only
def test_begin_after_an_uncommitted_write_is_refused_as_sqlite_refuses_it(raw):
    lite = sqlite3.connect(":memory:")
    lite.execute("CREATE TABLE wl_t (n INTEGER)")
    lite.execute("INSERT INTO wl_t VALUES (1)")
    with pytest.raises(sqlite3.OperationalError) as e_lite:
        lite.execute("BEGIN IMMEDIATE")
    a = pg.PgConnection(LIVE_URL)
    try:
        a.execute("INSERT INTO wl_t VALUES (?)", (1,))
        with pytest.raises(sqlite3.OperationalError) as e_pg:
            a.execute("BEGIN IMMEDIATE")
        assert str(e_pg.value) == str(e_lite.value) == "cannot start a transaction within a transaction"
        a.rollback()
        a.execute("SELECT count(*) FROM wl_t").fetchone()     # reads only: no transaction in SQLite terms
        a.execute("BEGIN IMMEDIATE")
        a.execute("ROLLBACK")
    finally:
        a.close()


@live_only
def test_job_locks_and_the_leader_lease_really_lock_on_postgres(atip_pg, raw):
    """Before the fix acquire() raised, job_lock() yielded True having written nothing, and nothing stopped a
    second runner."""
    from ops import jobs
    rows = lambda job: raw.execute("SELECT owner, pid FROM ops_job_lock WHERE job=%s", (job,)).fetchall()
    with jobs.job_lock("wl_job") as held:
        assert held is True
        assert [r[1] for r in rows("wl_job")] == [os.getpid()], "the lock row is written"
    assert rows("wl_job") == [], "and removed afterwards"

    # held by another live runner -> refused, and job_lock says so
    now = datetime.now()
    raw.execute("INSERT INTO ops_job_lock (job,owner,pid,acquired_at,heartbeat_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s)",
                ("wl_busy", "elsewhere:1:1", os.getppid(), now, now, now + timedelta(hours=1)))
    c = pg.PgConnection(LIVE_URL)
    try:
        assert jobs.acquire(c, "wl_busy") is False
    finally:
        c.close()
    with jobs.job_lock("wl_busy") as held:
        assert held is False
    assert [r[0] for r in rows("wl_busy")] == ["elsewhere:1:1"], "another runner's lock is left alone"

    assert jobs.leader_lease(60) is True
    assert [r[1] for r in rows(jobs.LEADER)] == [os.getpid()], "the lease is a real row, not a fail-open True"
    assert _advisory_locks(raw) == 0


@live_only
def test_a_new_migration_applies_on_postgres(atip_pg, raw, monkeypatch):
    from ops import migrations as M
    c = pg.PgConnection(LIVE_URL)
    try:
        # the VPS state: every shipped migration already recorded APPLIED (copied over from SQLite)
        for v, name, fn, note in M.MIGRATIONS:
            c.execute("INSERT INTO schema_migrations (version,name,checksum,applied_at,duration_ms,status,rollback_note) "
                      "VALUES (?,?,?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET status='APPLIED'",
                      (v, name, M._checksum(v, name, fn), datetime.now(), 0, "APPLIED", note))
        c.commit()

        def _wl_new(conn):
            conn.execute("CREATE TABLE wl_mig (n INTEGER)")
            conn.execute("INSERT INTO wl_mig VALUES (1)")

        def _wl_broken(conn):
            raise RuntimeError("broken on purpose")

        monkeypatch.setattr(M, "MIGRATIONS", M.MIGRATIONS + (("9901", "wl_new", _wl_new, "DROP TABLE wl_mig"),
                                                             ("9902", "wl_broken", _wl_broken, "none")))
        res = M.apply(c)
    finally:
        c.close()
    assert [(r["version"], r["status"]) for r in res] == [("9901", "APPLIED"), ("9902", "FAILED")]
    assert res[1]["error"] == "broken on purpose", "a failure is the migration's own, not the BEGIN"
    assert raw.execute("SELECT n FROM wl_mig").fetchall() == [(1,)]
    st = dict(raw.execute("SELECT version, status FROM schema_migrations WHERE version LIKE '99%%'").fetchall())
    assert st == {"9901": "APPLIED", "9902": "FAILED"}
    assert _advisory_locks(raw) == 0


@live_only
def test_kite_dummy_buys_place_one_record_per_symbol_on_postgres(atip_pg, raw, monkeypatch):
    """The Kite-retest arms' resting buy: success on PostgreSQL, then one ACTIVE record per symbol however many
    requests race for it."""
    from orders import algo_gtt
    monkeypatch.setattr(algo_gtt, "default_broker", lambda: None)

    def dummy(conn, sym, price):
        return algo_gtt.place_dummy_buy(conn, sym, 10, price, None, confirm_live=True, broker=None, last_px=price * 1.04)

    c = pg.PgConnection(LIVE_URL)
    try:
        first = dummy(c, "KALYANKJIL", 669.03)
        again = dummy(c, "KALYANKJIL", 669.03)
    finally:
        c.close()
    assert first["buy_status"] == "success", first["message"]
    assert first["buy_trigger_id"].startswith("DUMMY-")
    assert again["buy_status"] == "already_active" and again["buy_trigger_id"] == first["buy_trigger_id"]

    results, errs = [], []

    def go():
        cc = pg.PgConnection(LIVE_URL)
        try:
            results.append(dummy(cc, "CDSL", 1408.1)["buy_status"])
        except Exception as e:                                # pragma: no cover - reported below
            errs.append(e)
        finally:
            cc.close()
    ts = [threading.Thread(target=go) for _ in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert not errs, errs
    assert sorted(results) == ["already_active"] * 11 + ["success"]
    n = raw.execute("SELECT symbol, count(*) FROM algo_dummy_gtt WHERE status='ACTIVE' GROUP BY symbol ORDER BY 1").fetchall()
    assert n == [("CDSL", 1), ("KALYANKJIL", 1)]
    assert _advisory_locks(raw) == 0
