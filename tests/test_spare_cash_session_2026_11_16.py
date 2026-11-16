"""A new PAPER session out of the cash outside the sessions (owner 16 Nov 2026: "keep the option open to start new
session for remaining amount in funds, paper only"): orders/sessions.py spare_paper_cash / add_session,
POST /api/v2/sessions/new, and the board counting it.

The `paper` fixture opens a Rs 1,00,000 paper account; S1 and S2 take Rs 25,000 each, so Rs 50,000 is spare."""

import pytest

from tests.test_kite_formula_fix_2026_11_16 import _breadth, _do, _no_dhan, _pick, _sessions, _signals  # noqa: F401
from tests.test_session_pools import api  # noqa: F401  (pytest fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)


def _two_pools():
    from orders import arms
    _sessions("S1", "S5")
    _do(arms.set_arm, "S5", arms.ARMS["S5"])


def test_spare_cash_is_the_paper_balance_less_each_sessions_cash(paper):
    from orders import sessions
    _two_pools()
    sp = _do(sessions.spare_paper_cash)
    assert (sp["paper_balance"], sp["sessions_cash"], sp["spare"]) == (100000.0, 50000.0, 50000.0)
    assert sp["sessions"] == {"S1": 25000.0, "S5": 25000.0}


def test_a_new_session_takes_the_spare_cash_and_copies_the_control(paper):
    from orders import arms, sessions
    _two_pools()
    s = _do(sessions.add_session)
    assert s["id"] == "S11" and s["start_capital"] == 50000.0 and s["like"] == "S5"
    assert (s["status"], s["mode"], s["auto_pick"]) == ("PAUSED", "CONFIRM", None)           # making it trades nothing
    assert s["name"] == "Control (spare cash)"
    assert (s["stop_pct"], s["target_pct"], s["trail_pct"], s["hold_days"], s["min_buy_value"]) == (2.0, 3.0, 1.0, 15.0, 12000.0)
    assert s["max_loss_value"] == 3000.0                                      # S5's Rs 1,500 on Rs 25,000, scaled
    a = _do(arms.get, "S11")
    assert a["label"] == "Control (spare cash)" and a["regime_gate"] is None and a["stop"] == arms.ARMS["S5"]["stop"]
    assert s["spare_after"] == 0.0 and _do(sessions.spare_paper_cash)["spare"] == 0.0
    with pytest.raises(ValueError, match="no paper cash outside the sessions"):
        _do(sessions.add_session)
    assert _do(sessions.get_session, "S5")["start_capital"] == 25000.0       # the others are untouched


def test_part_of_the_spare_cash_a_name_and_another_arm(paper):
    from orders import arms, sessions
    _two_pools()
    _do(arms.set_arm, "S1", arms.ARMS["S1"])
    s = _do(sessions.add_session, "Momentum II", 20000, "S1")
    assert (s["id"], s["name"], s["start_capital"]) == ("S11", "Momentum II", 20000.0)
    assert _do(arms.get, "S11")["regime_gate"] == arms.ARMS["S1"]["regime_gate"]
    s12 = _do(sessions.add_session, "", None, None)                          # the rest, no copied rules: pool defaults
    assert (s12["id"], s12["start_capital"], s12["name"]) == ("S12", 30000.0, "Spare cash")
    assert (s12["stop_pct"], s12["target_pct"]) == (sessions.POOL_DEFAULTS["stop_pct"], sessions.POOL_DEFAULTS["target_pct"])
    assert _do(arms.get, "S12") is None


def test_what_a_new_session_refuses(paper, monkeypatch):
    import orders.environment as E
    from orders import sessions
    _two_pools()
    with pytest.raises(ValueError, match="more than the Rs 50,000.00"):
        _do(sessions.add_session, "", 60000)
    with pytest.raises(ValueError, match="ack=true"):
        _do(sessions.add_session, "", 20000, run=True)
    with pytest.raises(ValueError, match="min_buy_value"):
        _do(sessions.add_session, "", 5000)                                  # under S5's Rs 12,000 minimum buy
    with pytest.raises(LookupError):
        _do(sessions.add_session, "", 20000, "S99")
    assert [s["id"] for s in _do(sessions.list_sessions)["sessions"]][-1] == "S10"     # nothing was made
    monkeypatch.setattr(E, "broker_env", lambda: "LIVE")
    with pytest.raises(ValueError, match="PAPER only"):
        _do(sessions.add_session)


def test_a_running_new_session_picks_with_its_own_cash(paper):
    from orders import sessions
    from orders.session_board import board
    _two_pools()
    _signals(ACME=(82, "BUY", 100.0), BETA=(81, "BUY", 100.0), GAMMA=(80, "BUY", 100.0))   # one each for S1, S5, S11
    s = _do(sessions.add_session, "", None, "S5", run=True, ack=True)
    assert (s["status"], s["mode"], s["auto_pick"]) == ("RUNNING", "AUTO", 1)
    _breadth(80.7, 56.8)
    e = _pick()["S11"]
    assert e["type"] == "PICKED", e
    assert e["symbol"] == "GAMMA" and e["quantity_est"] >= 490                # Rs 50,000 at ~Rs 100.50 (whole cash)
    b = _do(board, run_dry=False)
    assert b["target"] == 11 and " of 11 sessions filled" in b["message"]
    assert any(r["session"] == "S11" for r in b["rows"])


def test_the_new_session_api_and_page(api):
    client, h = api
    from orders import sessions
    _two_pools()
    assert client.get("/api/v2/sessions/spare").json()["spare"] == 50000.0
    assert client.post("/api/v2/sessions/new", json={}).status_code == 401                 # needs the token
    r = client.post("/api/v2/sessions/new", json={"capital": 60000}, headers=h)
    assert r.status_code == 400 and "more than" in r.json()["error"]
    r = client.post("/api/v2/sessions/new", json={"capital": 20000, "run": True}, headers=h)
    assert r.status_code == 400 and "ack=true" in r.json()["error"]                       # AUTO needs the ack
    page = client.get("/sessions").text
    assert "New paper session" in page and 'id="ax-new"' in page and "₹50,000" in page
    r = client.post("/api/v2/sessions/new", json={"name": "Spare", "capital": "20,000"}, headers=h)
    assert r.status_code == 200 and (r.json()["id"], r.json()["start_capital"], r.json()["like"]) == ("S11", 20000.0, "S5")
    r = client.post("/api/v2/sessions/new", json={"like": "none"}, headers=h)
    assert r.status_code == 200 and (r.json()["id"], r.json()["start_capital"], r.json()["like"]) == ("S12", 30000.0, None)
    assert _do(sessions.spare_paper_cash)["spare"] == 0.0
    assert "New paper session" in client.get("/sessions").text                             # shown, disabled
