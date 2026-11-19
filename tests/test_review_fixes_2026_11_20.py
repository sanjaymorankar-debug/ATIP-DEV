"""
Regression tests for the independent review of the loss-RCA deploy (e5903e1, live on the VPS since 00:46:11 IST
20 Nov 2026). Each test fails on e5903e1 and passes with the fix.

LRCA-16  a pick left ACTIVE from an earlier day is cancelled before the first trigger check (exits-first, LRCA-02,
         had made it fill on a gap-down open); Kite dummies are maintained before the check too
LRCA-17  the Nifty-weak entry guard does not crash auto-pick when the feed stored the change as NULL (LRCA-13)
LRCA-18  every monitor outage gets its own alert (notify drops a repeated key for the day), and a new day starts afresh
LRCA-19  the exit-blind alert treats a NaN LTP as no price, as check_triggers does (LRCA-05)
LRCA-20  the 15-minute REST index snapshot stores an unknown change as NULL, not 0 %; `--status` prints it
"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from tests.test_autopick import _signals, _sessions, _pick, _conn  # noqa: F401
from tests.test_w39_qa_suite import paper  # noqa: F401  (paper: a throwaway database with a fake broker)
from tests.test_rca13_monitor_lock import _market, _run_loop, srv  # noqa: F401  (srv: the dashboard module)


# ── LRCA-16 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_yesterdays_pick_is_cancelled_before_the_first_trigger_check(paper, srv):
    from orders import rules
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    e = _pick()["S1"]
    assert e["type"] == "PICKED"
    c = _conn()
    c.execute("UPDATE order_rules SET created_at=? WHERE id=?",
              ((datetime.now() - timedelta(days=1)).isoformat(), e["rule_id"]))      # it was queued yesterday
    c.execute("DELETE FROM ai_scores")                                               # today's list has no ACME
    c.commit()
    c.close()
    paper.prices["ACME"] = 97.0                                                      # a gap-down open under the LIMIT
    srv._order_monitor_pass()
    r = rules.get_rule(e["rule_id"])
    assert r["status"] == "CANCELLED", f"yesterday's pick {r['status']} at {r.get('execution_price')}"


def test_the_pass_clears_leftovers_before_it_checks_triggers(srv, monkeypatch):
    import orders.algo_gtt as G
    import orders.autopick as AP
    order = []
    monkeypatch.setattr(AP, "cancel_stale", lambda conn, today=None: order.append("stale") or [])
    monkeypatch.setattr(G, "maintain", lambda conn, today=None: order.append("maintain") or [])
    monkeypatch.setattr(AP, "run", lambda: order.append("autopick") or [])
    monkeypatch.setattr(srv.oe, "check_triggers", lambda: order.append("exits") or [])
    srv._order_monitor_pass()
    assert order[:4] == ["stale", "maintain", "exits", "autopick"]


# ── LRCA-17 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_nifty_weak_guard_survives_an_unknown_change(monkeypatch):
    from orders import entry_guards as EG
    monkeypatch.setattr(EG, "settings", lambda conn: {"guard_entry_not_before": None, "guard_min_score": None,
                                                       "guard_nifty_weak": True, "guard_orb_minutes": None})
    monkeypatch.setattr(EG, "_nifty", lambda conn, day: {"now": 25000.0, "first": 25100.0, "chg": None})
    why = EG.check_entry(None, {"symbol": "ACME", "score": 70, "signal": "BUY"}, now=datetime(2026, 11, 20, 10, 0))
    assert why and "Nifty is weak" in why and "n/a" in why


# ── LRCA-18 ──────────────────────────────────────────────────────────────────────────────────────────────

def _dedupe_notify(sent):
    """notify()'s rule: a key already recorded today is not sent again."""
    def notify(text, **k):
        if k.get("key") in {s[1] for s in sent}:
            return {"deduped": True}
        sent.append((k.get("severity"), k.get("key"), text))
    return notify


def test_a_second_outage_the_same_day_is_alerted_again(srv, monkeypatch, temp_db):
    import alerts.telegram as T
    import ops.jobs as J
    import orders.autopick as AP
    sent = []
    monkeypatch.setattr(T, "notify", _dedupe_notify(sent))
    monkeypatch.setattr(J, "beat", lambda component="scheduler", detail=None, every=60: None)
    monkeypatch.setattr(AP, "run", lambda: [])
    _market(monkeypatch)
    srv._monitor_state.update(errors=0, failed_passes=0, alerted=False, since=None, day=None)
    for _ in range(2):                                   # outage, recovery, outage, recovery
        for _ in range(5):
            srv._monitor_health(False, "boom")
        srv._monitor_health(True)
    kinds = [s[0] for s in sent]
    assert kinds == ["error", "info", "error", "info"], sent
    assert len({s[1] for s in sent}) == 4


def test_a_new_day_alerts_an_outage_that_was_already_alerted_yesterday(srv, monkeypatch, temp_db):
    import alerts.telegram as T
    import ops.jobs as J
    sent = []
    monkeypatch.setattr(T, "notify", _dedupe_notify(sent))
    monkeypatch.setattr(J, "beat", lambda component="scheduler", detail=None, every=60: None)
    srv._monitor_state.update(errors=0, failed_passes=40, alerted=True, since="2026-11-19 15:00:00",
                              day=(date.today() - timedelta(days=1)).isoformat())
    for _ in range(4):
        srv._monitor_health(False, "still broken")
    assert [s[0] for s in sent] == ["error"]
    assert srv._monitor_state["day"] == date.today().isoformat()


# ── LRCA-19 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_nan_ltp_starts_the_exit_blind_clock(monkeypatch):
    from orders import rules
    monkeypatch.setitem(rules._blind, "since", None)
    monkeypatch.setitem(rules._blind, "alerted", False)
    legs = [{"symbol": "ACME", "role": "STOP"}]
    quotes = pd.DataFrame([{"symbol": "ACME", "ltp": float("nan")}])
    t0 = datetime(2026, 11, 20, 10, 0)
    rules._watch_blind(legs, quotes, now=t0)
    assert rules._blind["since"] == t0, "a NaN price is no price: the blind clock must start"


# ── LRCA-20 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_rest_index_snapshot_stores_an_unknown_change_as_null(paper, monkeypatch):
    from data import markets
    first = next(iter(markets.NSE_INDEXES))
    df = pd.DataFrame([{"index": first, "ltp": 25100.0, "chg_pct": None}])
    monkeypatch.setattr(markets, "fetch_index_quotes", lambda cols: df)
    markets.fetch_intraday_indexes(date.today())
    c = _conn()
    try:
        row = c.execute(f"SELECT {first}, {first}_chg FROM index_levels WHERE date=? ORDER BY time DESC LIMIT 1",
                        (str(date.today()),)).fetchone()
    finally:
        c.close()
    assert row is not None and float(row[0]) == 25100.0
    assert row[1] is None, f"an unknown change must be NULL, got {row[1]!r}"
