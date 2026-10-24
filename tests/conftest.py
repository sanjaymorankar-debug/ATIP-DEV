"""
Shared test fixtures.

The single most important thing here is that no test ever touches the real
atip_data/atip.db. `db.schema.DB_PATH` is a module-level Path, so redirecting it
to a tmp_path for the duration of a test is enough to isolate every module that
calls get_connection() — which is all of them.
"""

import ipaddress
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.schema as schema  # noqa: E402

# Before anything else runs -- module- and session-scoped fixtures included, which start
# before any function-level fixture -- the runtime is pinned to SQLite and ATIP's database
# URL leaves the environment. get_connection() goes to PostgreSQL when ATIP_DATABASE_URL
# (environment, .env or atip_data/secrets/) and config.json switch it, bypassing DB_PATH:
# on a Mac switched to Neon the suite would otherwise write into the production database.
# Tests of the switch itself set _PG_URL and the URL with monkeypatch.
for _name in ("ATIP_DATABASE_URL", "DATABASE_URL"):
    os.environ.pop(_name, None)
schema._PG_URL[:] = [None]

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _is_local(host) -> bool:
    if host is None:
        return True
    h = host.decode() if isinstance(host, bytes) else str(host)
    if h in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _offline():
    """No test reaches another machine: name lookups and connections to anything but
    this machine fail at once. Several code paths fetch from NSE (the Nifty 500 list, a
    session cookie, the index closes) and swallow the error, so tests passed offline and,
    with a network, quietly downloaded live data in the middle of a run. The proxy
    variables go too, or requests would go out through a proxy on this machine."""
    mp = pytest.MonkeyPatch()
    for name in _PROXY_VARS:
        mp.delenv(name, raising=False)
    getaddrinfo, connect, connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def _gai(host, *a, **k):
        if not _is_local(host):
            raise socket.gaierror(socket.EAI_NONAME, f"tests are offline: {host}")
        return getaddrinfo(host, *a, **k)

    def _remote(sock, addr):
        return sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(addr, tuple) \
            and not _is_local(addr[0])

    def _connect(sock, addr):
        if _remote(sock, addr):
            raise OSError(101, f"tests are offline: {addr}")
        return connect(sock, addr)

    def _connect_ex(sock, addr):
        return 101 if _remote(sock, addr) else connect_ex(sock, addr)
    mp.setattr(socket, "getaddrinfo", _gai)
    mp.setattr(socket.socket, "connect", _connect)
    mp.setattr(socket.socket, "connect_ex", _connect_ex)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test, even one that does not ask for temp_db: its own database,
    and no Telegram call. Alerts are now recorded in alert_log on every send,
    so a test that halts trading or fails a job would otherwise write to the
    real atip.db -- or message a configured bot.

    The dashboard's own files are redirected too. dashboard/server.py's
    STATE_PATH and dashboard/security.py's TOKEN_PATH / CONFIG_PATH are
    relative to the working directory, so any test that rendered the page
    (GET /, generate_state) rewrote the checkout's atip_data/dashboard_state.json
    -- on the Mac, the live dashboard's cache -- and read the owner's real
    config.json and dashboard token."""
    monkeypatch.setattr(schema, "DB_PATH", tmp_path / "isolated.db")
    # ...and never the PostgreSQL runtime (see the pin at the top of this file); a test
    # that set a URL or a _PG_URL of its own gets both put back afterwards.
    monkeypatch.delenv("ATIP_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(schema, "_PG_URL", [None])
    import alerts.telegram as tg
    monkeypatch.setattr(tg, "_real_deliver_telegram", tg._deliver_telegram, raising=False)
    monkeypatch.setattr(tg, "_deliver_telegram", lambda message, parse_mode="HTML": (False, "test"))
    from dashboard import security, server
    monkeypatch.setattr(server, "STATE_PATH", tmp_path / "dashboard_state.json")
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """A throwaway SQLite database for one test."""
    monkeypatch.setattr(schema, "DB_PATH", tmp_path / "test.db")
    yield tmp_path / "test.db"


@pytest.fixture
def cfg():
    """Strategy defaults with the feature switched on, isolated from config.json."""
    from strategy.aggressive import DEFAULTS, validate_config
    c = dict(DEFAULTS)
    c["aggressive_enabled"] = True
    return validate_config(c)


@pytest.fixture
def position(temp_db, cfg):
    """A filled 100-share long in ACME at 100.00, before target 1."""
    from strategy.positions import open_position
    return open_position("ACME", entry_price=100.0, quantity=100,
                         atr_pct=1.5, cfg=cfg)
