"""
A VIEWER (ops/role.py): the same code on a web server, showing the Mac's data and unable to trade -- and the
copy of the Mac's database it shows (ops/viewer_sync.py). docs/VIEWER_SERVER.md.
"""

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

import db.schema as schema
import ops.role as role
import ops.viewer_sync as vs

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(role, "CONFIG_PATH", path)
    monkeypatch.delenv("ATIP_ROLE", raising=False)
    return path


@pytest.fixture
def viewer(no_config, monkeypatch):
    monkeypatch.setenv("ATIP_ROLE", "viewer")


# ── which copy this is ─────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    (None, role.TRADER), ("", role.TRADER), ("trader", role.TRADER), (" Trader ", role.TRADER),
    ("viewer", role.VIEWER), ("VIEWER", role.VIEWER),
    ("veiwer", role.VIEWER), ("read-only", role.VIEWER),       # a typo can only stop trading, never start it
])
def test_role_from_the_environment(no_config, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("ATIP_ROLE", value)
    assert role.role() == expected


def test_config_role_viewer_wins_over_an_environment_trader(no_config, monkeypatch):
    no_config.write_text(json.dumps({"role": "viewer"}))
    monkeypatch.setenv("ATIP_ROLE", "trader")
    assert role.is_viewer()


def test_an_unreadable_config_leaves_the_mac_a_trader(no_config):
    no_config.write_text("{not json")
    assert role.role() == role.TRADER


def test_the_viewer_refuses_the_commands_that_fetch_alert_or_trade(no_config, monkeypatch):
    args = Namespace(init=False, dashboard=False, status=False, scheduler=True, run="postmarket", live_feed=False,
                     login_zerodha=False, test_telegram=True, morning_brief=False, dhan_securities=False,
                     dhan_history=False, dhan_quote=None, purge_db=False)
    assert role.blocked_commands(args) == []
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    assert role.blocked_commands(args) == ["--scheduler", "--run", "--test-telegram"]
    assert role.blocked_commands(Namespace(init=True, dashboard=True, status=True)) == []


# ── no route to a broker ───────────────────────────────────────────────────
def test_the_viewer_gets_no_dhan_client_even_with_credentials(viewer, monkeypatch):
    import data.dhan as D
    monkeypatch.setattr(D, "load_dhan_config", lambda: {"dhan_client_id": "1000000001", "dhan_access_token": "t"})
    with pytest.raises(role.ViewerRefused, match="view only"):
        D.get_dhan_client()
    assert issubclass(role.ViewerRefused, RuntimeError)     # callers that handle missing credentials handle this


def test_the_viewer_gets_no_execution_client_not_even_paper(viewer):
    from orders import environment as E
    with pytest.raises(role.ViewerRefused):
        E.get_execution_client()
    with pytest.raises(role.ViewerRefused):
        E.sandbox_client()


def test_the_viewer_paper_broker_places_nothing(viewer, temp_db):
    from orders.paper import PaperBroker
    with pytest.raises(role.ViewerRefused):
        PaperBroker().place_order(transaction_type="BUY", quantity=1, symbol="ACME")


def test_the_viewer_reaches_no_broker_connector(viewer):
    from brokers.connectors import DhanConnector, ZerodhaConnector
    with pytest.raises(role.ViewerRefused):
        DhanConnector({"client_id": "1", "access_token": "t"})
    with pytest.raises(role.ViewerRefused):
        ZerodhaConnector({"api_key": "k", "access_token": "t"})


def test_the_trader_still_gets_its_paper_client(no_config):
    from orders import environment as E
    client, env = E.get_execution_client()
    assert env == "PAPER" and type(client).__name__ == "PaperBroker"


@pytest.mark.parametrize("as_viewer,delivered", [(True, []), (False, ["hello"])])
def test_alerts_are_recorded_but_only_the_trader_delivers(no_config, monkeypatch, as_viewer, delivered):
    import alerts.telegram as tg
    if as_viewer:
        monkeypatch.setenv("ATIP_ROLE", "viewer")
    calls = []
    monkeypatch.setattr(tg, "_deliver_telegram", lambda message, parse_mode="HTML": (calls.append(message), (True, None))[1])
    out = tg.notify("hello")
    assert out["recorded"] and calls == delivered and out["sent"] is (not as_viewer)


def test_the_viewer_never_renews_the_dhan_token(viewer, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("dtr_viewer", REPO / "tools" / "dhan_token_refresh.py")
    dtr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dtr)
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(json.dumps({"dhan_client_id": "1", "dhan_access_token": "t"}))
    monkeypatch.setattr(dtr, "ROOT", tmp_path)
    monkeypatch.setattr(dtr, "CFG", tmp_path / "atip_data" / "config.json")
    monkeypatch.setattr(dtr, "LOG", tmp_path / "atip_data" / "dhan_token_refresh.log")
    monkeypatch.setattr(sys, "argv", ["dhan_token_refresh.py", "--force"])
    monkeypatch.chdir(tmp_path)

    def network(*a, **k):
        raise AssertionError("a viewer must not call Dhan")
    monkeypatch.setattr(dtr, "http", network)
    assert dtr.main() == 2
    assert "VIEWER" in (tmp_path / "atip_data" / "dhan_token_refresh.log").read_text()


# ── the dashboard ──────────────────────────────────────────────────────────
@pytest.fixture
def srv():
    import dashboard.server as srv
    return srv


WRITES = (("POST", "/api/orders"), ("DELETE", "/api/orders/1"), ("POST", "/api/orders/1/confirm"),
          ("POST", "/api/risk/halt"), ("POST", "/api/command/auto-send"), ("PUT", "/api/risk/limits"),
          ("POST", "/api/refresh"), ("POST", "/api/status/override"), ("POST", "/api/auth/register"))


def test_the_viewer_refuses_every_write_but_sign_in(viewer, srv):
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    for method, path in WRITES:
        r = c.request(method, path, json={})
        assert r.status_code == 403 and r.json() == {"error": role.MESSAGE, "read_only": True, "role": "VIEWER"}, path
    r = c.post("/api/auth/login", json={})
    assert r.status_code != 403 or r.json().get("role") != "VIEWER"
    assert c.get("/api/status").status_code == 200


def test_the_trader_dashboard_is_unchanged(no_config, srv):
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    assert c.post("/api/orders", json={}).status_code == 401           # the token guard, as before
    assert "role" not in c.get("/api/status").json()


def test_the_viewer_status_says_where_its_data_is_from(viewer, srv):
    from fastapi.testclient import TestClient
    vs._save_state(installed={"at": "2026-10-30T14:06:10", "sha256": "ab" * 32, "size": 1,
                              "generated_at": "2026-10-30T14:05:00", "release": "abc1234"})
    c = TestClient(srv.app)
    st = c.get("/api/status").json()
    assert st["role"] == "VIEWER" and st["viewer"]["data_from"] == "2026-10-30T14:05:00"
    assert st["viewer"]["mac_release"] == "abc1234"
    assert "dhan_token" not in st                                        # the viewer holds no token to warn about
    page = c.get("/global-asia").text
    assert 'id="atipRoleBanner"' in page and "View only" in page and '"role": "VIEWER"' in page


def _started_monitors(srv, monkeypatch):
    started = []
    monkeypatch.setattr(srv, "_order_monitor_loop", lambda: started.append(1))
    from fastapi.testclient import TestClient
    with TestClient(srv.app):
        deadline = time.monotonic() + 2
        while not started and time.monotonic() < deadline:
            time.sleep(0.02)
    return started


def test_the_viewer_starts_no_order_monitor(viewer, srv, monkeypatch):
    assert _started_monitors(srv, monkeypatch) == []


def test_the_trader_starts_its_order_monitor(no_config, srv, monkeypatch):
    assert _started_monitors(srv, monkeypatch) == [1]


# ── the copy from the Mac ──────────────────────────────────────────────────
def _mac_db(path: Path) -> sqlite3.Connection:
    """A live, WAL-mode database with rows and a sign-in session, left open the way ATIP holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE prices (symbol TEXT, close REAL)")
    c.executemany("INSERT INTO prices VALUES (?, ?)", [("ACME", 101.5), ("BETA", 55.0), ("GAMMA", 12.25)])
    c.execute("CREATE TABLE enterprise_session (token TEXT)")
    c.execute("INSERT INTO enterprise_session VALUES ('sess-secret-123')")
    c.commit()
    return c


@pytest.fixture
def mac(tmp_path, monkeypatch):
    path = tmp_path / "mac" / "atip.db"
    conn = _mac_db(path)
    monkeypatch.setattr(schema, "DB_PATH", path)
    yield path
    conn.close()


def test_the_snapshot_is_one_file_without_sign_in_credentials(mac):
    snap = vs.snapshot()
    db = snap["db"]
    assert db == mac.parent / "viewer_push" / "atip.db" and not db.with_name("atip.db-wal").exists()
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert c.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM enterprise_session").fetchone()[0] == 0
    c.close()
    assert b"sess-secret-123" not in db.read_bytes()                     # zeroed, not just unlinked
    manifest = json.loads(snap["manifest"].read_text())
    assert manifest["sha256"] == hashlib.sha256(db.read_bytes()).hexdigest() and manifest["size"] == db.stat().st_size
    assert manifest["emptied"] == {"enterprise_session": 1}
    live = sqlite3.connect(mac)
    assert live.execute("SELECT COUNT(*) FROM enterprise_session").fetchone()[0] == 1   # the Mac's own is untouched
    live.close()


def test_a_push_sends_the_database_before_its_manifest(mac, monkeypatch):
    sent = []
    monkeypatch.setattr(vs, "_send", lambda cfg, source: sent.append(source.name))
    out = vs.push(cfg={"target": "atip@203.0.113.10:/home/atip/atip/viewer_incoming/"})
    assert out["status"] == "PUSHED" and sent == ["atip.db", "manifest.json"]
    assert vs._state()["pushed"]["sha256"] == out["sha256"]


def test_while_stopped_a_push_waits_an_hour(mac, monkeypatch):
    monkeypatch.setattr(vs, "_send", lambda cfg, source: None)
    cfg = {"target": "atip@host:/in/"}
    vs.push(cfg=cfg)
    monkeypatch.setattr(vs, "_stopped", lambda: True)
    assert vs.push(cfg=cfg)["status"] == "SKIPPED"
    assert vs.push(cfg=cfg, force=True)["status"] == "PUSHED"


def test_a_dry_run_needs_no_config_and_sends_nothing(mac, monkeypatch):
    monkeypatch.setattr(vs, "_send", lambda cfg, source: pytest.fail("a dry run must not send"))
    assert vs.push(dry_run=True)["status"] == "BUILT"


def test_the_rsync_command(tmp_path):
    cmd = vs.rsync_command({"target": "atip@203.0.113.10:/srv/in/", "ssh_key": "~/.ssh/atip_viewer", "ssh_port": 2222},
                           Path("/a/atip.db"))
    assert cmd[:2] == ["rsync", "-az"] and cmd[-2:] == ["/a/atip.db", "atip@203.0.113.10:/srv/in/"]
    ssh = cmd[cmd.index("-e") + 1]
    assert "BatchMode=yes" in ssh and "-p 2222" in ssh and str(Path("~/.ssh/atip_viewer").expanduser()) in ssh


def _arrive(snap: dict, incoming: Path, manifest: dict | None = None) -> None:
    """What rsync leaves on the server."""
    incoming.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snap["db"], incoming / "atip.db")
    if manifest is None:
        shutil.copyfile(snap["manifest"], incoming / "manifest.json")
    else:
        (incoming / "manifest.json").write_text(json.dumps(manifest))


def _viewer_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE old_copy (x)")
    c.commit()
    return c


def test_install_replaces_the_viewer_database_under_an_open_reader(mac, tmp_path, monkeypatch):
    snap = vs.snapshot()
    incoming = tmp_path / "server" / "viewer_incoming"
    _arrive(snap, incoming)
    viewer_db = tmp_path / "server" / "atip_data" / "atip.db"
    reader = _viewer_db(viewer_db)                       # a dashboard request holding the database open
    monkeypatch.setattr(schema, "DB_PATH", viewer_db)

    out = vs.install_once(incoming)
    assert out["status"] == "INSTALLED" and out["sha256"] == snap["sha256"]
    assert reader.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3
    assert reader.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='old_copy'").fetchone()[0] == 0
    assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert sqlite3.connect(viewer_db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not (viewer_db.parent / "viewer_install.db").exists()
    assert vs.install_once(incoming)["status"] == "CURRENT"
    assert vs.status()["data_from"] == snap["generated_at"]
    reader.close()


def test_install_waits_for_the_matching_manifest(mac, tmp_path, monkeypatch):
    snap = vs.snapshot()
    incoming = tmp_path / "server" / "viewer_incoming"
    _arrive(snap, incoming, manifest={"format": 1, "sha256": "0" * 64, "size": snap["size"],
                                      "generated_at": "2026-10-30T09:00:00"})
    viewer_db = tmp_path / "server" / "atip_data" / "atip.db"
    _viewer_db(viewer_db).close()
    monkeypatch.setattr(schema, "DB_PATH", viewer_db)
    assert vs.install_once(incoming)["status"] == "MISMATCH"
    c = sqlite3.connect(viewer_db)
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='prices'").fetchone()[0] == 0
    c.close()
    assert not (viewer_db.parent / "viewer_install.db").exists()


def test_install_waits_for_the_first_push(tmp_path):
    assert vs.install_once(tmp_path / "nothing_yet")["status"] == "WAITING"
