"""UXR-04 and UXR-06..09: the session gate in the trigger engine, and the Signal Explorer, Trade Setup,
Live Monitor and Journal screens with their APIs. Orders run through the real PAPER broker on a
throwaway database (the `paper` fixture from tests/test_w39_qa_suite.py)."""

import pytest

from tests.test_w39_qa_suite import _book, _rule, paper  # noqa: F401  (pytest fixture)


def _held(symbol="ACME"):
    """Paper shares held; 0 before the paper broker has made its tables."""
    try:
        return _book(symbol)[1]
    except Exception:
        return 0


def _conn():
    from db.schema import get_connection
    return get_connection()


def _session(sid="S1", **kw):
    from orders import sessions
    c = _conn()
    try:
        sessions.seed(c)
        if "status" in kw:
            sessions.set_status(c, sid, kw.pop("status"))
        if "mode" in kw:
            sessions.set_mode(c, sid, kw.pop("mode"), ack=True)
        if kw:
            sessions.update_session(c, sid, kw)
    finally:
        c.close()


def _link(rule_id, sid="S1"):
    from orders import sessions
    c = _conn()
    try:
        sessions.link_rule(c, sid, rule_id)
    finally:
        c.close()


def _gate(rule, est=None):
    from orders import sessions
    c = _conn()
    try:
        return sessions.gate(c, rule, est)
    finally:
        c.close()


# ── UXR-04: the gate ───────────────────────────────────────────────────────────────────────────

def test_unlinked_rule_passes_and_paused_session_holds_its_entry(paper):
    r = _rule()
    assert _gate(r) is None                                   # no session: unchanged behaviour
    _session("S1")                                            # default slots start PAUSED
    _link(r["id"])
    assert "PAUSED" in _gate(r)
    _session("S1", status="RUNNING")
    assert _gate(r) is None


def test_manual_session_holds_entries(paper):
    r = _rule()
    _session("S1", status="RUNNING", mode="MANUAL")
    _link(r["id"])
    assert "MANUAL" in _gate(r)


def test_exit_legs_are_never_held(paper):
    r = _rule(side="SELL", role="STOP", trigger_direction="BELOW")
    _session("S1")                                            # paused
    _link(r["id"])
    assert _gate(r) is None


def test_capital_cap_and_trade_count(paper):
    r = _rule()
    _session("S1", status="RUNNING", capital_cap=500.0, max_trades=1)
    _link(r["id"])
    assert "capital cap" in _gate(r, est=1000.0)
    assert _gate(r, est=400.0) is None


def test_trigger_engine_waits_for_a_paused_session_then_fires(paper):
    from orders import rules
    r = _rule(require_confirmation=False, trigger_value=100.0)
    _session("S1")
    _link(r["id"])
    paper.prices["ACME"] = 99.0                               # trigger condition met (BUY, price <= 100)
    ev = rules.check_triggers()
    assert [e["type"] for e in ev] == ["HELD_BY_SESSION"]
    assert rules.get_rule(r["id"])["status"] == rules.ACTIVE  # still watching, nothing sent
    assert _held() == 0
    _session("S1", status="RUNNING")
    rules.check_triggers()
    assert rules.get_rule(r["id"])["status"] == rules.EXECUTED
    assert _held() == 10


def test_max_trades_holds_the_second_entry(paper):
    from orders import rules
    _session("S1", status="RUNNING", max_trades=1)
    a = _rule(require_confirmation=False)
    b = _rule(require_confirmation=False, symbol="BETA")
    _link(a["id"]); _link(b["id"])
    paper.prices.update({"ACME": 99.0})
    rules.check_triggers()
    assert rules.get_rule(a["id"])["status"] == rules.EXECUTED
    paper.prices.update({"BETA": 99.0})
    ev = rules.check_triggers()
    assert any(e["type"] == "HELD_BY_SESSION" and "trades today" in e["reason"] for e in ev)
    assert rules.get_rule(b["id"])["status"] == rules.ACTIVE


def test_journal_counts_a_closed_bracket_trade(paper):
    from orders import rules, sessions
    _session("S1", status="RUNNING")
    r = _rule(require_confirmation=False, bracket_target_pct=5.0, bracket_stop_pct=2.0,
              stoploss={"type": "VALUE", "value": -2.0})
    _link(r["id"])
    paper.prices["ACME"] = 100.0
    rules.check_triggers()                                    # entry fills, legs spawn
    paper.prices["ACME"] = 106.0
    rules.check_triggers()                                    # target leg fires (auto exit)
    c = _conn()
    try:
        s1 = next(s for s in sessions.journal(c)["sessions"] if s["id"] == "S1")
    finally:
        c.close()
    assert s1["trades"] == 1 and s1["closed"] == 1 and s1["successful"] == 1 and s1["win_rate_pct"] == 100.0
    assert s1["pnl"] > 0 and s1["avg_r"] > 0


# ── UXR-06..09: pages and APIs ─────────────────────────────────────────────────────────────────

@pytest.fixture
def api(paper, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from orders import sessions
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(sessions, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), {security.TOKEN_HEADER: security.token()}


def _seed_scores():
    c = _conn()
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score, atip_rank, `signal`, vpi, mri, cri, top_factor_1) "
              "VALUES ('ACME', '2026-10-23', 72, 1, 'BUY', 65, 71, 30, 'VPI:65')")
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score, atip_rank, `signal`) VALUES ('BETA', '2026-10-23', 50, 2, 'HOLD')")
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "VALUES ('ACME', '2026-10-23', 99, 101, 98, 100, 1000, 'EQ')")
    c.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('ACME', 104, '2026-10-26 11:40:00')")
    c.commit(); c.close()


@pytest.mark.parametrize("path,text", [("/command/signals", "Signal Explorer"), ("/command/setup", "Trade Setup"),
                                       ("/command/monitor", "Live Monitor"), ("/command/journal", "Journal")])
def test_pages_serve_with_token_nav_and_kill_switch(api, path, text):
    client, h = api
    r = client.get(path)
    assert r.status_code == 200 and text in r.text and "Halt all trading" in r.text
    assert h["X-ATIP-Token"] in r.text and "__" not in r.text.replace("__proto__", "")
    assert 'aria-current="page"' in r.text


def test_command_center_links_the_new_screens(api):
    client, _ = api
    t = client.get("/command").text
    for p in ("/command/signals", "/command/setup", "/command/monitor", "/command/journal"):
        assert f'href="{p}"' in t


def test_signal_explorer_rows_factors_story_and_gap(api):
    client, _ = api
    _seed_scores()
    d = client.get("/api/command/signals?signal=BUY").json()
    assert d["date"] == "2026-10-23" and [r["symbol"] for r in d["rows"]] == ["ACME"]
    r = d["rows"][0]
    assert r["gap_pct"] == 4.0 and "past the 3% mark" in r["story"] and "VPI:65" in r["story"]
    assert {f["key"]: f["value"] for f in r["factors"]}["mri"] == 71
    assert d["counts"] == {"BUY": 1, "HOLD": 1}


def test_preview_rupee_risk_reward_and_checklist(api):
    client, _ = api
    d = client.get("/api/command/preview?symbol=ACME&side=BUY&qty=10&entry=100&target=110&stop=95").json()
    assert d["risk_rs"] == 50.0 and d["reward_rs"] == 100.0 and d["rr"] == 2.0
    assert d["net_risk_rs"] >= 50.0 and d["value"] == 1000.0
    names = {c["check"]: c for c in d["checks"]}
    assert names["Trading not halted"]["ok"] and names["Funds"]["ok"]
    assert d["ok"] is True


def test_preview_refuses_a_stop_on_the_wrong_side(api):
    client, _ = api
    r = client.get("/api/command/preview?symbol=ACME&side=BUY&qty=10&entry=100&stop=105")
    assert r.status_code == 400 and "below" in r.json()["error"]


def test_preview_flags_a_halt_as_blocking(api):
    from orders import risk
    client, _ = api
    risk.halt("test")
    d = client.get("/api/command/preview?symbol=ACME&qty=1&entry=100&stop=95").json()
    assert d["ok"] is False and "halted" in d["message"].lower()


def test_save_setup_needs_the_token_and_creates_a_linked_watched_rule(api):
    from orders import rules
    client, h = api
    body = {"symbol": "ACME", "side": "BUY", "qty": 10, "entry": 100, "target": 110, "stop": 95, "session": "S2"}
    assert client.post("/api/command/setup", json=body).status_code in (401, 403)
    d = client.post("/api/command/setup", json=body, headers=h).json()
    rule = rules.get_rule(d["rule"]["id"])
    assert rule["status"] == rules.ACTIVE and rule["require_confirmation"] == 1
    assert rule["resolved_stoploss_price"] == 95.0 and rule["bracket_target_pct"] == 10.0 and rule["bracket_stop_pct"] == 5.0
    assert _held() == 0                                    # saving sends nothing
    m = client.get("/api/command/monitor").json()
    w = next(x for x in m["watching"] if x["id"] == rule["id"])
    assert w["session"] == "S2" and "PAUSED" in w["held"]


def test_monitor_and_journal_shapes_on_an_empty_book(api):
    client, _ = api
    m = client.get("/api/command/monitor").json()
    for k in ("env", "feed", "positions", "watching", "log", "fills"):
        assert k in m, k
    j = client.get("/api/command/journal").json()
    assert len(j["sessions"]) == 10 and j["totals"]["trades"] == 0
    assert [b["band"] for b in j["score_bands"]][0] == "< 55"
    assert j["range_2027"]["ready"] is False and "Not a forecast" in j["range_2027"]["note"]


def test_new_pages_are_owner_only():
    from enterprise import authz
    import re
    assert any(re.match(p, "/command/monitor") for p in authz.OWNER_PAGES)
    assert any(re.match(p, "/api/command/preview") for p in authz.OWNER_BOOK)


# ── the owner's AUTO send switch ───────────────────────────────────────────────────────────────

def _auto_send(on, ack=True):
    from orders import sessions
    c = _conn()
    try:
        return sessions.set_auto_send(c, on, ack)
    finally:
        c.close()


def test_auto_session_still_asks_until_the_switch_is_on(paper):
    from orders import rules
    _session("S1", status="RUNNING", mode="AUTO")
    r = _rule()                                               # require_confirmation defaults to True
    _link(r["id"])
    paper.prices["ACME"] = 99.0
    rules.check_triggers()
    assert rules.get_rule(r["id"])["status"] == rules.PENDING_CONFIRMATION and _held() == 0


def test_switch_on_sends_auto_session_entries_without_asking(paper):
    from orders import rules
    _session("S1", status="RUNNING", mode="AUTO")
    _auto_send(True)
    r = _rule()
    _link(r["id"])
    paper.prices["ACME"] = 99.0
    rules.check_triggers()
    assert rules.get_rule(r["id"])["status"] == rules.EXECUTED and _held() == 10


def test_switch_on_leaves_confirm_sessions_and_unlinked_rules_asking(paper):
    from orders import rules
    _session("S2", status="RUNNING")                          # CONFIRM
    _auto_send(True)
    a = _rule()
    b = _rule(symbol="BETA")
    _link(a["id"], "S2")
    paper.prices.update({"ACME": 99.0, "BETA": 99.0})
    rules.check_triggers()
    assert rules.get_rule(a["id"])["status"] == rules.PENDING_CONFIRMATION
    assert rules.get_rule(b["id"])["status"] == rules.PENDING_CONFIRMATION


def test_switch_on_refuses_a_stale_quote(paper):
    from orders import rules
    _session("S1", status="RUNNING", mode="AUTO")
    _auto_send(True)
    r = _rule()
    _link(r["id"])
    paper.prices["ACME"], paper.age_s = 99.0, rules.MAX_QUOTE_AGE_SECONDS + 60
    ev = rules.check_triggers()
    assert any(e["type"] == "SKIPPED_STALE_QUOTE" for e in ev) and _held() == 0


def test_switch_needs_ack_and_is_refused_while_halted(paper):
    from orders import risk
    with pytest.raises(ValueError, match="ack"):
        _auto_send(True, ack=False)
    risk.halt("test")
    with pytest.raises(ValueError, match="halted"):
        _auto_send(True)
    assert _auto_send(False)["auto_send"] is False            # switching off always works


def test_auto_send_api_is_guarded_logged_and_shown(api):
    from orders import sessions
    client, h = api
    assert client.post("/api/command/auto-send", json={"on": True, "ack": True}).status_code in (401, 403)
    assert client.post("/api/command/auto-send", json={"on": True}, headers=h).status_code == 400
    assert client.post("/api/command/auto-send", json={"on": True, "ack": True}, headers=h).json()["auto_send"] is True
    assert client.get("/api/command/summary").json()["sessions"]["auto_bypass"] is True
    c = _conn()
    try:
        assert any(e["event"] == "AUTO_SEND" for e in sessions.events(c, limit=100))
    finally:
        c.close()
    assert "Send without asking" in client.get("/command").text
