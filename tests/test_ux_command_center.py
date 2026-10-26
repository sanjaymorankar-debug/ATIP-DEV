"""UXR-01: the Command Center page, its summary API, the kill-switch routes and the session registry
(orders/sessions.py). Everything runs against a throwaway database and a temp halt flag."""

import pytest


@pytest.fixture
def api(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from db.schema import init_db
    from dashboard import security, server
    from orders import rules, risk, sessions
    init_db()
    rules.init_orders_table()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(sessions, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    return client, {security.TOKEN_HEADER: security.token()}, risk


def _conn():
    from db.schema import get_connection
    return get_connection()


# ── page + summary ─────────────────────────────────────────────────────────

def test_page_serves_with_token_and_kill_switch(api):
    client, h, _ = api
    r = client.get("/command")
    assert r.status_code == 200
    assert "Halt all trading" in r.text and h["X-ATIP-Token"] in r.text
    assert "__TOKEN__" not in r.text


def test_main_dashboard_links_to_command_center(api):
    client, _, _ = api
    assert 'href="/command"' in client.get("/").text


def test_summary_shape_on_an_empty_install(api):
    client, _, _ = api
    d = client.get("/api/command/summary").json()
    for k in ("market_open", "feed", "broker", "risk", "signals", "pending", "recent", "sessions"):
        assert k in d, k
    assert d["risk"]["halted"] is False
    assert len(d["sessions"]["sessions"]) == 10
    assert d["sessions"]["engine_enforced"] is False
    assert d["signals"]["rows"] == []


def test_summary_pairs_eod_signal_with_live_price(api):
    client, _, _ = api
    c = _conn()
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES ('ACME', '2026-10-23', 82, 'BUY')")
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES ('HOLDCO', '2026-10-23', 90, 'HOLD')")
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "VALUES ('ACME', '2026-10-23', 99, 101, 98, 100, 1000, 'EQ')")
    c.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('ACME', 104, '2026-10-26 11:40:00')")
    c.commit(); c.close()
    rows = client.get("/api/command/summary").json()["signals"]["rows"]
    assert [r["symbol"] for r in rows] == ["ACME"]          # HOLD is not a trade signal
    assert rows[0]["signal_price"] == 100 and rows[0]["live_price"] == 104 and rows[0]["gap_pct"] == 4.0


def test_feed_is_stale_only_while_the_market_is_open():
    from dashboard.command_routes import _feed
    from db.schema import init_db
    init_db()
    c = _conn()
    c.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('ACME', 1, '2020-01-01 10:00:00')")
    c.commit()
    assert _feed(c, market_open=True)["stale"] is True
    assert _feed(c, market_open=False)["stale"] is False
    c.close()


# ── kill switch ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/risk/halt", "/api/risk/resume", "/api/sessions/S1/mode",
                                  "/api/sessions/S1/status", "/api/sessions/S1", "/api/sessions/S1/link"])
def test_every_new_mutating_route_needs_the_token(api, path):
    client, _, _ = api
    assert client.post(path, json={}).status_code == 401


def test_halt_sets_the_real_kill_switch_and_pauses_running_sessions(api):
    client, h, risk = api
    client.post("/api/sessions/S2/status", json={"status": "RUNNING"}, headers=h)
    r = client.post("/api/risk/halt", json={"reason": "test halt"}, headers=h).json()
    assert r["halted"] is True and r["sessions_paused"] == 1
    assert risk.halted() == (True, "test halt")             # the flag every order path checks
    s = {x["id"]: x for x in client.get("/api/sessions").json()["sessions"]}
    assert s["S2"]["status"] == "PAUSED"


def test_resume_needs_the_word_and_leaves_sessions_paused(api):
    client, h, risk = api
    client.post("/api/risk/halt", json={"reason": "x"}, headers=h)
    assert client.post("/api/risk/resume", json={}, headers=h).status_code == 400
    assert risk.halted()[0] is True
    r = client.post("/api/risk/resume", json={"confirm": "RESUME"}, headers=h).json()
    assert r["was_halted"] is True and r["halted"] is False
    assert all(x["status"] != "RUNNING" for x in client.get("/api/sessions").json()["sessions"])


# ── sessions ───────────────────────────────────────────────────────────────

def test_auto_needs_acknowledgement(api):
    client, h, _ = api
    r = client.post("/api/sessions/S1/mode", json={"mode": "AUTO"}, headers=h)
    assert r.status_code == 400 and "ack" in r.json()["error"]
    r = client.post("/api/sessions/S1/mode", json={"mode": "AUTO", "ack": True}, headers=h)
    assert r.status_code == 200 and r.json()["mode"] == "AUTO" and r.json()["engine_enforced"] is False


def test_auto_is_refused_while_halted(api):
    client, h, _ = api
    client.post("/api/risk/halt", json={"reason": "x"}, headers=h)
    r = client.post("/api/sessions/S1/mode", json={"mode": "AUTO", "ack": True}, headers=h)
    assert r.status_code == 400 and "halted" in r.json()["error"]


def test_live_auto_needs_an_algo_id(api, monkeypatch):
    client, h, _ = api
    import orders.environment as envmod
    monkeypatch.setattr(envmod, "broker_env", lambda *a, **k: "LIVE")
    r = client.post("/api/sessions/S1/mode", json={"mode": "AUTO", "ack": True}, headers=h)
    assert r.status_code == 400 and "Algo ID" in r.json()["error"]


def test_bad_mode_status_and_unknown_session(api):
    client, h, _ = api
    assert client.post("/api/sessions/S1/mode", json={"mode": "YOLO"}, headers=h).status_code == 400
    assert client.post("/api/sessions/S1/status", json={"status": "FAST"}, headers=h).status_code == 400
    assert client.post("/api/sessions/S99/mode", json={"mode": "MANUAL"}, headers=h).status_code == 404


def test_limits_edit_validates_and_every_change_is_logged(api):
    client, h, _ = api
    assert client.post("/api/sessions/S3", json={"max_loss_value": -5}, headers=h).status_code == 400
    r = client.post("/api/sessions/S3", json={"name": "Breakouts v2", "max_loss_value": 1800}, headers=h)
    assert r.status_code == 200 and r.json()["max_loss_value"] == 1800
    client.post("/api/sessions/S3/mode", json={"mode": "MANUAL"}, headers=h)
    ev = [e["event"] for e in client.get("/api/sessions/events?sid=S3").json()]
    assert ev[:3] == ["MODE", "EDIT", "CREATED"]


def test_linking_a_rule_counts_it_against_the_session(api, monkeypatch):
    client, h, _ = api
    from orders import rules
    monkeypatch.setattr(rules, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    rule = rules.create_rule({"symbol": "ACME", "side": "BUY", "trigger_type": "PRICE", "trigger_value": 95,
                              "reference_price": 100, "quantity_type": "SHARES", "quantity_value": 10})
    assert client.post("/api/sessions/S4/link", json={"rule_id": "nope"}, headers=h).status_code == 404
    assert client.post("/api/sessions/S4/link", json={"rule_id": rule["id"]}, headers=h).status_code == 200
    s = {x["id"]: x for x in client.get("/api/sessions").json()["sessions"]}
    assert s["S4"]["stats"]["rules"] == 1 and s["S4"]["stats"]["watching"] == 1


def test_seed_is_idempotent():
    from orders import sessions
    c = _conn()
    assert sessions.seed(c) == 10 and sessions.seed(c) == 0
    c.close()
