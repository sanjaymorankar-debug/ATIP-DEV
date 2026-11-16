"""Paper capital plan and allocation guard (owner prompt 16 Nov 2026, 16:39 IST): orders/capital_plan.py, the guard in
accounting/ledger.py transfer / orders/sessions.py start_pool and add_session, a session's P&L that no longer counts a
transfer as profit (CAP-03), the plan's API, and the job lock that survives a host-name change (CAP-02).

The `paper` fixture opens a Rs 1,00,000 paper account; S1 and S5 take Rs 25,000 each, so Rs 50,000 belongs to no session."""

import json
import os
import threading

import pytest

from tests.test_kite_formula_fix_2026_11_16 import _do, _no_dhan, _sessions  # noqa: F401
from tests.test_session_pools import api  # noqa: F401  (pytest fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

PLAN = {"cash_buffer_pct": 10, "max_session_pct": 50, "sessions": "running", "round_to": 1000,
        "session_loss_limit_pct": 6, "pool": "equity"}


@pytest.fixture
def plan_cfg(tmp_path, monkeypatch):
    """The owner's plan block in a config.json of its own (the guard is on); returns a writer for other values."""
    import orders.capital_plan as CP
    p = tmp_path / "plan_config.json"

    def write(block=PLAN):
        p.write_text(json.dumps({"broker_env": "PAPER", CP.KEY: block} if block is not None else {"broker_env": "PAPER"}),
                     encoding="utf-8")
    write()
    monkeypatch.setattr(CP, "CONFIG_PATH", p)
    return write


def _two():
    _sessions("S1", "S5")


def test_the_plan_tops_up_each_running_session_equally_and_keeps_the_buffer(paper, plan_cfg):
    from orders import capital_plan as CP
    _two()
    p = _do(CP.plan)
    assert p["paper"] and p["guard_active"]
    assert (p["pool"]["value"], p["spare_before"], p["buffer"], p["headroom"]) == (100000.0, 50000.0, 10000.0, 40000.0)
    assert p["per_session"] == 20000.0 and [r["top_up"] for r in p["rows"]] == [20000.0, 20000.0]
    assert p["spare_after"] == 10000.0 and p["buffer_after_pct"] == 10.0
    assert (p["before"]["allocated_pct"], p["after"]["allocated_pct"]) == (50.0, 90.0)
    g = p["guardrails"]
    assert g["cash_buffer"] == {"pct": 10.0, "rupees": 10000.0} and g["max_open_positions"]["per_session"] == 1
    assert g["daily_loss"]["per_session_rupees"] == {"S1": 2700.0, "S5": 2700.0}           # 6 % of Rs 45,000


def test_apply_moves_the_cash_without_counting_it_as_profit(paper, plan_cfg):
    from orders import capital_plan as CP
    from orders import sessions
    _two()
    with pytest.raises(ValueError, match="ack=true"):
        _do(CP.apply)
    out = _do(CP.apply, ack=True)
    assert out["moved"] == 40000.0 and [s["session"] for s in out["sessions"]] == ["S1", "S5"]
    L = _do(sessions.ledger, "S1")
    assert (L["start_capital"], L["transfers"], L["capital"], L["cash"]) == (25000.0, 20000.0, 45000.0, 45000.0)
    assert (L["equity"], L["pnl"], L["return_pct"]) == (45000.0, 0.0, 0.0)             # CAP-03: was +20,000 / +80 %
    assert _do(sessions.get_session, "S1")["max_loss_value"] == 2700.0
    assert _do(sessions.spare_paper_cash)["spare"] == 10000.0
    again = _do(CP.apply, ack=True)                                                     # nothing above the buffer is left
    assert again["moved"] == 0 and again["sessions"] == []
    ev = _do(lambda c: [dict(r) for r in c.execute("SELECT * FROM session_events WHERE event = ?", (CP.EVENT,))])
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["moved"] == 40000.0


def test_the_next_buy_is_sized_from_the_topped_up_cash(paper, plan_cfg):
    from orders import capital_plan as CP
    from orders import sessions
    _two()
    _do(CP.apply, ack=True)
    q = _do(sessions.pool_quantity, "S5", 100.0)
    assert q["cash"] == 45000.0 and q["quantity"] == 450                                # zero charges and slippage here


def test_a_transfer_in_never_takes_more_than_the_cash_no_session_owns(paper, plan_cfg):
    from accounting import ledger as LG
    _two()
    plan_cfg(None)                                                                      # no plan block: the hard limit only
    with pytest.raises(ValueError, match="more than the Rs 50,000.00 of paper cash that belongs to no session"):
        _do(LG.transfer, None, "S1", 50000.01, "too much")
    assert _do(LG.transfer, None, "S1", 50000, "all of it")["amount"] == 50000.0
    plan_cfg()                                                                          # the plan: the buffer is kept
    _do(LG.transfer, "S1", None, 20000, "back")
    with pytest.raises(ValueError, match="10 % cash buffer"):
        _do(LG.transfer, None, "S5", 15000, "into the buffer")
    assert _do(LG.transfer, None, "S5", 10000, "within the headroom")["amount"] == 10000.0


def test_no_session_above_max_session_pct(paper, plan_cfg):
    from accounting import ledger as LG
    _two()
    plan_cfg({**PLAN, "max_session_pct": 30})                                          # Rs 30,000 a session at most
    with pytest.raises(ValueError, match="more than 30 % of the paper pool"):
        _do(LG.transfer, None, "S1", 6000, "over the cap")
    assert _do(LG.transfer, None, "S1", 5000, "to the cap")["amount"] == 5000.0


def test_a_pool_restart_cannot_over_allocate_once_the_plan_is_set(paper, plan_cfg):
    from orders import sessions
    _two()
    with pytest.raises(ValueError, match="more than 50 % of the paper pool"):
        _do(sessions.start_pool, "S1", 60000)                                          # 35,000 new money fits; 60,000 > 50,000
    with pytest.raises(ValueError, match="cash buffer"):
        _do(sessions.start_pool, "S2", 45000)                                          # 45,000 of a 40,000 headroom
    s = _do(sessions.start_pool, "S2", 40000)
    assert s["start_capital"] == 40000.0
    plan_cfg(None)                                                                     # without the block: as before
    assert _do(sessions.start_pool, "S3", 25000)["start_capital"] == 25000.0


def test_a_new_session_defaults_to_the_headroom_above_the_buffer(paper, plan_cfg):
    from orders import sessions
    _two()
    s = _do(sessions.add_session)
    assert (s["id"], s["start_capital"]) == ("S11", 40000.0) and s["spare_after"] == 10000.0
    with pytest.raises(ValueError, match="above the cash buffer"):
        _do(sessions.add_session)


def test_spare_cash_shows_an_over_allocation(paper, plan_cfg):
    from orders import sessions
    plan_cfg(None)
    _sessions("S1", "S2", "S3", "S4", "S5")                                             # 1,25,000 on a 1,00,000 account
    sp = _do(sessions.spare_paper_cash)
    assert (sp["spare"], sp["over_allocated"]) == (0.0, 25000.0)


def test_live_has_no_plan_and_refuses_to_apply(paper, plan_cfg, monkeypatch):
    import orders.environment as E
    from orders import capital_plan as CP
    _two()
    monkeypatch.setattr(E, "broker_env", lambda: "LIVE")
    assert _do(CP.plan)["paper"] is False
    with pytest.raises(ValueError, match="PAPER only"):
        _do(CP.apply, ack=True)


def test_occurrences_start_from_the_amount_after_a_transfer():
    from orders.sessions import _occurrences
    trades = [{"symbol": "A", "date": "2026-11-13", "at": "2026-11-13T10:00", "entry": 100, "qty": 250, "exit_price": 103,
               "closed": True, "exit_date": "2026-11-14", "pnl": 700.0},
              {"symbol": "B", "date": "2026-11-17", "at": "2026-11-17T10:00", "entry": 100, "qty": 450, "exit_price": None,
               "closed": False, "exit_date": None, "pnl": 0.0}]
    occ = _occurrences(25000, trades, [{"at": "2026-11-16T17:30:00", "amount": 20000.0}])
    assert occ[0]["start_amount"] == 25000.0 and occ[0]["end_amount"] == 25700.0
    assert occ[1]["start_amount"] == 45700.0                                            # the top-up is not its profit


def test_the_plan_api_and_the_sessions_dialog(api, paper, plan_cfg):
    client, h = api
    _two()
    r = client.get("/api/v2/capital/plan")
    assert r.status_code == 200 and r.json()["per_session"] == 20000.0
    page = client.get("/sessions").text
    assert 'id="ax-cap"' in page and "Top up sessions" in page
    assert client.post("/api/v2/capital/apply", json={"ack": True}).status_code == 401           # the token is needed
    r = client.post("/api/v2/capital/apply", json={"ack": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 40000.0


# -- CAP-02: the job lock and the scheduler lease survive a host-name change ----------------------------------------

def test_a_lock_written_under_the_old_host_name_is_still_ours(temp_db):
    from db.schema import get_connection, init_db
    from ops import jobs
    init_db()
    c = get_connection()
    try:
        old = f"smoru.local:{os.getpid()}:{threading.get_ident()}"                     # before the network change
        c.execute("INSERT INTO ops_job_lock (job, owner, pid, acquired_at, heartbeat_at, expires_at) VALUES "
                  "('dhan_15min_bars', ?, ?, datetime('now'), datetime('now'), datetime('now', '+6 hours'))",
                  (old, os.getpid()))
        c.commit()
        assert jobs.acquire(c, "dhan_15min_bars") is True                              # 16 Nov: SKIPPED all day
        jobs.release(c, "dhan_15min_bars")
        assert c.execute("SELECT COUNT(*) FROM ops_job_lock WHERE job='dhan_15min_bars'").fetchone()[0] == 0
        c.execute("INSERT INTO ops_job_lock (job, owner, pid, acquired_at, heartbeat_at, expires_at) VALUES "
                  "('x', ?, ?, datetime('now'), datetime('now'), datetime('now', '+6 hours'))", (old, os.getpid()))
        c.commit()
        jobs.release(c, "x")                                                           # released whatever the host is now
        assert c.execute("SELECT COUNT(*) FROM ops_job_lock WHERE job='x'").fetchone()[0] == 0
    finally:
        c.close()


def test_another_live_runner_still_holds_its_lock(temp_db):
    from db.schema import get_connection, init_db
    from ops import jobs
    init_db()
    c = get_connection()
    try:
        c.execute("INSERT INTO ops_job_lock (job, owner, pid, acquired_at, heartbeat_at, expires_at) VALUES "
                  "('j', ?, ?, datetime('now'), datetime('now'), datetime('now', '+6 hours'))",
                  (f"otherhost:{os.getppid()}:1", os.getppid()))                       # a live process, not this one
        c.commit()
        assert jobs.acquire(c, "j") is False
        assert jobs.owner().startswith(jobs._HOST + ":")                               # the host part is fixed at import
    finally:
        c.close()


# -- CAP-05: the exit monitor says when it has no prices for open stops ------------------------------------------------

def test_no_price_for_an_open_stop_for_two_minutes_is_alerted_once(monkeypatch):
    from datetime import datetime, timedelta
    import pandas as pd
    import alerts.telegram as T
    from orders import rules
    sent = []
    monkeypatch.setattr(T, "notify", lambda msg, **k: sent.append((msg, k)) or {"recorded": True, "sent": False})
    monkeypatch.setattr(rules, "_blind", {"since": None, "alerted": False})
    legs = [{"symbol": "ATHERENERG", "role": "STOP"}, {"symbol": "KALYANKJIL", "role": "ENTRY"}]
    t0 = datetime(2026, 11, 16, 10, 4)
    assert rules._watch_blind(legs, None, t0) is None                                   # the gap starts
    assert rules._watch_blind(legs, pd.DataFrame(), t0 + timedelta(seconds=90)) is None
    assert rules._watch_blind(legs, None, t0 + timedelta(seconds=121)) == "alerted"
    assert rules._watch_blind(legs, None, t0 + timedelta(seconds=300)) is None          # once
    assert len(sent) == 1 and "ATHERENERG" in sent[0][0] and sent[0][1]["severity"] == "error"
    back = pd.DataFrame([{"symbol": "ATHERENERG", "ltp": 1984.2}])
    assert rules._watch_blind(legs, back, t0 + timedelta(seconds=330)) == "recovered"
    assert rules._blind == {"since": None, "alerted": False}
    assert rules._watch_blind([{"symbol": "X", "role": "ENTRY"}], None, t0) is None     # entries alone never alert
