"""
The test suite never writes into the checkout's atip_data/.

On the owner's Mac the checkout's atip_data/ is the live one: the running
dashboard's cache (dashboard_state.json), its write token and the real
config.json with the Dhan credentials. Rendering the dashboard in a test used
to rewrite that cache with an empty test database's state, because
dashboard/server.py's STATE_PATH and dashboard/security.py's TOKEN_PATH /
CONFIG_PATH are relative to the working directory. tests/conftest.py now
points them at each test's tmp_path; these tests hold it to that.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA = (ROOT / "atip_data").resolve()


def _outside_the_checkout(p: Path) -> bool:
    p = Path(p).resolve()
    return p != LIVE_DATA and LIVE_DATA not in p.parents


def test_the_dashboard_files_point_at_the_test_s_own_directory(tmp_path):
    import db.schema as schema
    from dashboard import security, server
    for p in (schema.DB_PATH, server.STATE_PATH, security.TOKEN_PATH, security.CONFIG_PATH):
        assert _outside_the_checkout(p), f"{p} is in the checkout's atip_data/"
        assert Path(p).resolve().parent == tmp_path.resolve()


def test_rendering_the_dashboard_writes_its_cache_and_token_under_tmp_path(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import init_db
    from orders import rules
    init_db()
    rules.init_orders_table()
    r = TestClient(server.app).get("/")
    assert r.status_code == 200
    assert (tmp_path / "dashboard_state.json").is_file(), "generate_state wrote its cache somewhere else"
    assert (tmp_path / "dashboard_token.txt").read_text(encoding="utf-8").strip() == security.token()
    assert security.token() in r.text


def test_no_test_reaches_a_postgresql_runtime_even_when_one_is_configured(tmp_path, monkeypatch):
    """get_connection() bypasses DB_PATH once the PostgreSQL runtime is switched on
    (ATIP_DATABASE_URL + config.json database.backend / allow_experimental). On a Mac
    moved to Neon, running the suite from the project folder therefore sent every test's
    writes to the production database. conftest pins the runtime to SQLite and drops the
    URL from the environment, so even a fully switched configuration stays local."""
    import os
    import sqlite3
    import db.schema as schema
    assert "ATIP_DATABASE_URL" not in os.environ and "DATABASE_URL" not in os.environ
    assert schema._PG_URL == [None]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(
        '{"database": {"backend": "postgresql", "allow_experimental": true}}', encoding="utf-8")
    (tmp_path / ".env").write_text("ATIP_DATABASE_URL=postgresql://bk_atip_app:pw@127.0.0.1:1/bk_atip\n", encoding="utf-8")
    monkeypatch.setenv("ATIP_DATABASE_URL", "postgresql://bk_atip_app:pw@127.0.0.1:1/bk_atip")
    conn = schema.get_connection()
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == Path(schema.DB_PATH)
    finally:
        conn.close()


def test_no_test_can_reach_another_machine():
    """tests/conftest.py keeps the suite offline. Code that fetches from NSE swallows the
    failure, so a test with network access quietly downloaded live data mid-run (the
    Nifty 500 list, a session cookie, the index closes) and passed either way."""
    import os
    import socket
    import time
    assert not [k for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
                if k in os.environ], "a proxy on this machine would carry requests out"
    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 80), timeout=1)                 # TEST-NET-1
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("archives.nseindia.com", 443)
    requests = pytest.importorskip("requests")
    t = time.monotonic()
    with pytest.raises(requests.exceptions.RequestException):
        requests.get("https://archives.nseindia.com/content/indices/ind_nifty500list.csv", timeout=20)
    assert time.monotonic() - t < 5, "refused at once, not after a timeout"
    with socket.socket() as srv:                                               # this machine still works
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        socket.create_connection(srv.getsockname(), timeout=2).close()
