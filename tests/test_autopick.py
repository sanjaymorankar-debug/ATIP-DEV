"""Auto-pick (orders/autopick.py): a free cash-pool session queues its next occurrence on the top ATIP
BUY signal no other session holds. Orders run orders.rules -> orders.broker -> orders.paper for real on a
throwaway database (the `paper` fixture: Rs 1,00,000 account, no slippage, no charges)."""

from datetime import date, datetime, timedelta

import pytest

from tests.test_session_pools import api  # noqa: F401  (pytest fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

TODAY = date.today()


def _conn():
    from db.schema import get_connection
    return get_connection()


def _do(fn, *a, **k):
    c = _conn()
    try:
        return fn(c, *a, **k)
    finally:
        c.close()


def _signals(on=TODAY, **rows):
    """ai_scores rows on `on`: SYMBOL=(score, signal, close)."""
    c = _conn()
    for sym, (score, sig, close) in rows.items():
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES (?, ?, ?, ?)",
                  (sym, str(on), score, sig))
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES (?, ?, ?, ?, ?, ?, 1000, 'EQ')", (sym, str(on), close, close, close, close))
    c.commit(); c.close()


def _sessions(*ids, auto_send=True):
    """Pools of Rs 25,000 with auto-pick, RUNNING in AUTO; the owner's auto send switch on."""
    from orders import sessions
    _do(sessions.setup_pools, 25000, list(ids), auto_pick=True)
    for sid in ids:
        _do(sessions.set_status, sid, "RUNNING")
        _do(sessions.set_mode, sid, "AUTO", ack=True)
    if auto_send:
        _do(sessions.set_auto_send, True, ack=True)


def _pick(**k):
    from orders.autopick import run
    return {e["session"]: e for e in run(**k)}


def _tick(feed, **prices):
    from orders import rules
    feed.prices.update(prices)
    return rules.check_triggers()


def test_a_free_session_queues_the_top_buy_signal(paper):
    from orders import rules
    _signals(ACME=(80, "BUY", 100.0), BETA=(70, "BUY", 50.0), GAMMA=(95, "HOLD", 10.0))
    _sessions("S1")
    e = _pick()["S1"]
    assert (e["type"], e["symbol"], e["occurrence"], e["trigger"]) == ("PICKED", "ACME", 1, 100.5)
    r = rules.get_rule(e["rule_id"])
    assert (r["quantity_type"], r["bracket_target_pct"], r["bracket_stop_pct"], r["require_confirmation"]) == \
        ("SESSION", 3.0, 2.0, True)                               # the session's rules; the owner's switch decides
    assert _pick()["S1"]["type"] == "WAITING"                     # one buy queued is enough
    _tick(paper, ACME=100.0)
    assert rules.get_rule(e["rule_id"])["execution_quantity"] == 250


def test_two_sessions_never_pick_the_same_stock(paper):
    _signals(ACME=(80, "BUY", 100.0), BETA=(70, "BUY", 50.0))
    _sessions("S1", "S2", "S3")
    p = _pick()
    assert (p["S1"]["symbol"], p["S2"]["symbol"]) == ("ACME", "BETA")
    assert p["S3"]["type"] == "WAITING" and "ACME is taken" in p["S3"]["reason"]


def test_the_cycle_rotates_into_the_next_signal_with_all_the_money(paper):
    from orders import rules, sessions
    _signals(ACME=(80, "BUY", 100.0), BETA=(70, "BUY", 50.0))
    _sessions("S1")
    _pick(); _tick(paper, ACME=100.0)                            # occurrence 1: 250 ACME
    assert "holds ACME" in _pick()["S1"]["reason"]
    _tick(paper, ACME=103.0)                                      # sold: Rs 25,750
    e = _pick()["S1"]
    assert (e["symbol"], e["occurrence"]) == ("BETA", 2)          # ACME's signal was used
    _tick(paper, BETA=50.0)
    assert rules.get_rule(e["rule_id"])["execution_quantity"] == 515
    L = _do(sessions.ledger, "S1", history=True)
    assert [o["symbol"] for o in L["occurrences"]] == ["ACME", "BETA"]


def test_paused_manual_or_no_signal_sessions_wait_and_say_why(paper):
    from orders import sessions
    _sessions("S1", "S2", "S3")
    _do(sessions.set_status, "S1", "PAUSED")
    _do(sessions.set_mode, "S2", "MANUAL")
    p = _pick()
    assert "PAUSED" in p["S1"]["reason"] and "MANUAL" in p["S2"]["reason"]
    assert "no free BUY signal" in p["S3"]["reason"] and "no ATIP scores" in p["S3"]["reason"]


def test_old_scores_and_a_signal_that_already_ran_are_not_picked(paper):
    _signals(on=TODAY - timedelta(days=9), ACME=(80, "BUY", 100.0))
    _sessions("S1")
    assert "too old" in _pick()["S1"]["reason"]
    _signals(BETA=(70, "BUY", 50.0))
    c = _conn()
    c.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('BETA', 52, ?)",
              (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))           # +4 % on its signal close
    c.commit(); c.close()
    assert "above its signal close" in _pick()["S1"]["reason"]


def test_paper_only_and_nothing_while_halted(paper, monkeypatch):
    from orders import risk, rules
    import orders.environment as envmod
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    risk.halt("test")
    assert "halted" in _pick()["S1"]["reason"]
    risk.resume()
    monkeypatch.setattr(envmod, "broker_env", lambda *a, **k: "LIVE")
    assert _pick()["S1"]["reason"] == "auto-pick is PAPER only"
    assert rules.list_rules() == []


def test_a_pick_still_waiting_next_day_is_cancelled_and_picked_again(paper):
    from orders import rules
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    first = _pick()["S1"]["rule_id"]
    again = _pick(today=TODAY + timedelta(days=1))["S1"]
    assert rules.get_rule(first)["status"] == rules.CANCELLED and again["rule_id"] != first


def test_dry_run_queues_nothing(paper):
    from orders import rules
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    assert _pick(dry_run=True)["S1"]["symbol"] == "ACME" and rules.list_rules() == []


def test_without_the_auto_send_switch_a_pick_asks_first(paper):
    from orders import rules
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1", auto_send=False)
    e = _pick()["S1"]
    _tick(paper, ACME=100.0)
    assert rules.get_rule(e["rule_id"])["status"] == rules.PENDING_CONFIRMATION


def test_the_pnl_page_shows_what_auto_pick_would_do(api):
    client, h = api
    _signals(ACME=(80, "BUY", 100.0))
    r = client.post("/api/sessions/pools", json={"total": 50000, "capital": 25000, "auto_pick": True}, headers=h)
    assert r.status_code == 200 and r.json()["sessions"] == ["S1", "S2"]
    client.post("/api/sessions/S1/status", json={"status": "RUNNING"}, headers=h)
    s = {x["id"]: x for x in client.get("/api/command/pnl?scope=pools").json()["sessions"]}
    assert s["S1"]["auto_pick"] and s["S1"]["pick"]["symbol"] == "ACME"         # a preview: nothing queued
    assert "PAUSED" in s["S2"]["pick"]["reason"]
    from orders import rules
    assert rules.list_rules() == []


# ── the HOLD fallback (off until the owner sets a cut-off) ─────────────────────────────────────

def test_hold_stocks_are_not_picked_while_the_fallback_is_off(paper):
    _signals(GAMMA=(90, "HOLD", 10.0))
    _sessions("S1")
    assert _pick()["S1"]["reason"] == "no free BUY signal: no BUY signal on " + str(TODAY)


def test_the_fallback_takes_hold_stocks_after_the_buy_signals_by_score(paper):
    from orders import autopick, rules
    _signals(ACME=(66, "BUY", 100.0), GAMMA=(64, "HOLD", 40.0), DELTA=(70, "HOLD", 80.0), BETA=(55, "HOLD", 50.0))
    _sessions("S1", "S2", "S3", "S4")
    _do(autopick.set_hold_min_score, 60)
    p = _pick()
    assert [p[s].get("symbol") for s in ("S1", "S2", "S3")] == ["ACME", "DELTA", "GAMMA"]   # BUY, then HOLD by score
    assert "BETA" not in p["S4"]["reason"] and "HOLD stock scoring 60+" in p["S4"]["reason"]  # 55 is under the cut
    assert "ATIP HOLD" in rules.get_rule(p["S2"]["rule_id"])["notes"]
    _do(autopick.set_hold_min_score, "off")
    assert _do(autopick.hold_min_score) is None


def test_the_fallback_setting_is_guarded_validated_and_logged(api):
    client, h = api
    assert client.post("/api/sessions/autopick", json={"hold_min_score": 60}).status_code == 401
    assert client.post("/api/sessions/autopick", json={"hold_min_score": 150}, headers=h).status_code == 400
    r = client.post("/api/sessions/autopick", json={"hold_min_score": 60, "reason": "test"}, headers=h).json()
    assert r == {"hold_min_score": 60.0, "was": None}
    assert client.get("/api/command/pnl").json()["autopick_hold_min_score"] == 60.0
    assert "AUTO_PICK_HOLD" in [e["event"] for e in client.get("/api/sessions/events").json()]
