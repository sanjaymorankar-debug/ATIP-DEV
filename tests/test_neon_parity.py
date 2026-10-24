"""
Neon parity: ATIP on PostgreSQL the way it will run on Neon.

On Neon (docs/POSTGRESQL_MIGRATION.md, "Neon") ATIP connects with TLS and
channel binding (`?sslmode=require&channel_binding=require`), as its own
non-superuser role that owns its database, to a compute that drops idle
connections when it suspends. Its URL is the direct endpoint; the pooled one is a
PgBouncer in transaction mode. These tests check each of those against a local
stand-in. They never contact a hosted database.

  * No server needed (always run): the DSN reaches libpq unchanged, so neither
    sslmode nor channel_binding can be dropped on the way; ATIP needs no
    PostgreSQL extension.
  * Live, skipped unless both are set:

        ATIP_TEST_PG_TLS_URL     a DIRECT server that offers TLS, e.g.
            postgresql://t_atip:<pw>@127.0.0.1:5432/t_atip_pool?sslmode=require&channel_binding=require
        ATIP_TEST_PG_POOLER_URL  a PgBouncer in transaction mode, TLS required, in
                                 front of the SAME database, e.g.
            postgresql://t_atip:<pw>@127.0.0.1:6432/t_atip_pool?sslmode=require

    The role should be an ordinary login role that owns the database (on Neon: a
    member of neon_superuser). Its public schema is DROPped and recreated, so both
    URLs must point at this machine, at a database of their own -- not the one in
    ATIP_TEST_POSTGRES_URL: a PgBouncer with auth_query keeps a superuser session
    open in every database it has served, and a non-superuser cannot DROP DATABASE
    ... WITH (FORCE) past it, which tests/test_postgres_backend.py does.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
TLS_URL = os.environ.get("ATIP_TEST_PG_TLS_URL")
POOLER_URL = os.environ.get("ATIP_TEST_PG_POOLER_URL")
live = pytest.mark.skipif(not (TLS_URL and POOLER_URL),
                          reason="set ATIP_TEST_PG_TLS_URL and ATIP_TEST_PG_POOLER_URL to run the Neon parity checks")
NEON_STYLE = "postgresql://bk_atip_app:pw@ep-x.ap-south-1.aws.neon.tech/bk_atip?sslmode=require&channel_binding=require"


# ── no server needed ──────────────────────────────────────────────────────

def test_the_neon_connection_string_reaches_libpq_unchanged(monkeypatch):
    """sslmode=require and channel_binding=require must arrive at libpq as written: the
    connection adds only its UTC session time zone, and keeps any options already there
    (Neon's SNI-less clients put `endpoint=...` in options)."""
    psycopg = pytest.importorskip("psycopg")
    from db import backend, postgres
    seen = []

    class _Conn:
        read_only = False

        def close(self):
            pass
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: seen.append((dsn, kw)) or _Conn())
    postgres.PgConnection(NEON_STYLE)
    backend.connect(NEON_STYLE)
    backend.connect(NEON_STYLE.replace("postgresql://", "postgresql+psycopg://"))
    for dsn, kw in seen:
        assert dsn == NEON_STYLE
        assert kw == {"options": "-c TimeZone=UTC"}
    seen.clear()
    with_endpoint = NEON_STYLE + "&options=endpoint%3Dep-x"
    postgres.PgConnection(with_endpoint)
    assert seen == [(with_endpoint, {"options": "endpoint=ep-x -c TimeZone=UTC"})]


def test_close_rolls_back_the_transaction_a_read_opened(monkeypatch):
    """psycopg opens a transaction on the first statement, a SELECT included. Closing with
    it still open made a transaction-mode pooler discard the server connection, so through
    Neon's pooler no server connection was ever reused."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.pq import TransactionStatus
    from db import postgres
    calls = []

    class _Info:
        transaction_status = TransactionStatus.INTRANS

    class _Conn:
        info = _Info()
        read_only = False

        def rollback(self):
            calls.append("rollback")
            _Info.transaction_status = TransactionStatus.IDLE

        def close(self):
            calls.append("close")
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: _Conn())
    c = postgres.PgConnection(NEON_STYLE)
    c.close()
    assert calls == ["rollback", "close"]
    calls.clear()
    postgres.PgConnection(NEON_STYLE).close()               # nothing open: no extra round trip
    assert calls == ["close"]


def test_atip_needs_no_postgresql_extension():
    """Neon lets the owner create trusted extensions; ATIP does not need any, so a new
    Neon database needs nothing installed before `main.py --init`."""
    script = (ROOT / "db" / "sql" / "atip_schema.postgresql.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION" not in script.upper()


# ── live: a local TLS server and a transaction-mode PgBouncer ─────────────

def _local(url):
    host = urlparse(url).hostname or "localhost"
    if host not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail(f"refusing {host}: these checks DROP their database, so they only run against this machine")


@contextmanager
def _runtime(url, tmp_path):
    """ATIP's own runtime switch (db.schema.pg_runtime_url), pointed at `url`."""
    import db.schema as S
    mp = pytest.MonkeyPatch()
    try:
        (tmp_path / "atip_data").mkdir(exist_ok=True)
        (tmp_path / "atip_data" / "config.json").write_text(
            '{"database": {"backend": "postgresql", "allow_experimental": true}}', encoding="utf-8")
        mp.chdir(tmp_path)
        mp.setenv("ATIP_DATABASE_URL", url)
        mp.setattr(S, "_PG_URL", [])
        yield S
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A fresh database, its schema built over the direct TLS URL by ATIP's start-up path."""
    psycopg = pytest.importorskip("psycopg")
    _local(TLS_URL)
    _local(POOLER_URL)
    if urlparse(TLS_URL).path == urlparse(os.environ.get("ATIP_TEST_POSTGRES_URL") or "x://h/").path:
        pytest.fail("give ATIP_TEST_PG_TLS_URL a database of its own (see the module docstring)")
    with psycopg.connect(TLS_URL, autocommit=True) as c:          # empty it: the owner may drop its schema
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
    from ops import migrations
    with _runtime(TLS_URL, tmp_path_factory.mktemp("neon")) as S:
        S.init_db()
        S.seed_weights()
        conn = S.get_connection()
        try:
            first = migrations.apply(conn)
        finally:
            conn.close()
        S.init_db()                                           # a second start-up adds nothing
        conn = S.get_connection()
        try:
            again, problems = migrations.apply(conn), migrations.validate(conn)
        finally:
            conn.close()
    return {"first": first, "again": again, "problems": problems}


@live
def test_the_schema_is_built_over_tls_by_a_non_superuser_and_a_rerun_adds_nothing(built, tmp_path):
    assert built["first"], "no migration applied on an empty database"
    assert built["again"] == [] and built["problems"] == []
    with _runtime(TLS_URL, tmp_path) as S:
        conn = S.get_connection()
        try:
            ssl, tz, superuser = conn.execute(
                "SELECT (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), current_setting('TimeZone'), "
                "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)").fetchone()
            assert ssl is True and tz == "UTC"
            assert superuser is False, "run these as the app's own role, not a superuser"
            tables = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = "
                                  "current_schema() AND table_type = 'BASE TABLE'").fetchone()[0]
            triggers = conn.execute("SELECT COUNT(*) FROM pg_trigger WHERE NOT tgisinternal").fetchone()[0]
            assert tables > 200 and triggers >= 16
            assert conn.execute("SELECT COUNT(*) FROM weight_config").fetchone()[0] > 100
        finally:
            conn.close()


@live
def test_the_owner_can_create_the_trusted_extensions_without_superuser(built):
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(TLS_URL, autocommit=True) as c:
        for ext in ("citext", "btree_gist", "pgcrypto"):
            c.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            c.execute(f"DROP EXTENSION {ext}")


@live
def test_the_runtime_works_through_a_transaction_pooler(built, tmp_path):
    """Transaction pooling hands each transaction whichever server connection is free.
    psycopg prepares a statement on the server after its 5th execution; PgBouncer with
    max_prepared_statements (as Neon's pooler runs) must make that safe across server
    connections, and the session time zone must hold in every transaction."""
    with _runtime(POOLER_URL, tmp_path) as S:
        a, b = S.get_connection(), S.get_connection()
        try:
            for i in range(20):                               # well past psycopg's prepare_threshold (5)
                for c in (a, b):
                    n = c.execute("SELECT COUNT(*) FROM weight_config WHERE weight >= ?", (0.0,)).fetchone()[0]
                    assert n > 100
                    assert c.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"
                    c.commit()
            # a write under BEGIN IMMEDIATE: a transaction-scoped advisory lock, which a pooler allows
            a.execute("BEGIN IMMEDIATE")
            a.execute("INSERT INTO weight_config (index_name, variable, weight) VALUES (?, ?, ?)",
                      ("NEON_PARITY", "probe", 0.5))
            a.commit()
            assert b.execute("SELECT weight FROM weight_config WHERE index_name = ?",
                             ("NEON_PARITY",)).fetchone()[0] == 0.5
            b.execute("DELETE FROM weight_config WHERE index_name = ?", ("NEON_PARITY",))
            b.commit()
        finally:
            a.close()
            b.close()


_MINE = "usename = current_user AND datname = current_database() AND pid <> pg_backend_pid()"


def _my_other_backends(terminate=False):
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(TLS_URL, autocommit=True) as c:
        what = "COUNT(*) FILTER (WHERE pg_terminate_backend(pid))" if terminate else "COUNT(*)"
        return c.execute(f"SELECT {what} FROM pg_stat_activity WHERE {_MINE}").fetchone()[0]


@live
@pytest.mark.parametrize("which", ["direct", "pooler"])
def test_the_next_request_succeeds_after_the_server_drops_idle_connections(built, tmp_path, which):
    """What a Neon compute does when it suspends. ATIP opens a connection per call
    (db.schema.get_connection), so the next call gets a fresh one; a connection held
    across the drop fails with the sqlite3 error class ATIP's callers catch."""
    url = TLS_URL if which == "direct" else POOLER_URL
    with _runtime(url, tmp_path) as S:
        if which == "pooler":
            # a closed ATIP connection leaves its server connection idle in the pool
            used = S.get_connection()
            assert used.execute("SELECT COUNT(*) FROM weight_config").fetchone()[0] > 100
            used.close()
            assert _my_other_backends() >= 1, "the pooler kept no server connection"
            assert _my_other_backends(terminate=True) >= 1
        else:
            held = S.get_connection()
            assert held.execute("SELECT 1").fetchone()[0] == 1
            held.commit()
            assert _my_other_backends(terminate=True) >= 1
            with pytest.raises(sqlite3.OperationalError):
                held.execute("SELECT 1")
            held.close()
        for _ in range(3):                                   # the pooler drops the dead one and reconnects
            fresh = S.get_connection()
            try:
                assert fresh.execute("SELECT COUNT(*) FROM weight_config").fetchone()[0] > 100
            finally:
                fresh.close()
