"""W39 QA suite: first automated tests for features the tracker listed with no test reaching them.

  EX-01   orders/rules.py -- order-rule creation and validation, trigger evaluation
          (check_triggers), bracket / OCO legs (settle_oco_group) and trailing stops, end to end
          through the PAPER broker (orders/paper.py). No broker is ever called: the quote feed is
          a fixed table and the network is refused.
  OPS-02  the post-market job (pipeline/scheduler.run_postmarket) end to end on seeded prices:
          the offline steps run for real, the download steps are recorded as skipped.
  QR-11   backtest/sensitivity.py -- one-at-a-time parameter sensitivity, on real tiny backtests
          and on a stubbed trial whose metric is a known function of the parameters.
  ML-05   ml/presets.py -- the regime model preset and the first-models bootstrap.
  MON-06  main.py SafeRotatingFileHandler -- rotation, and a refused rename that keeps logging.
  DB-17   the risk dashboard: /trading (dashboard/execution_page.py) and every data route it calls.
  OPS-11  ops/rollback_drill.py -- the pure helpers _db_into and _isolate (no clone, no server).

Every expected value is worked out by hand in the test that asserts it.
"""

import ast
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tools import load_test as LT

ROOT = Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════════
#  EX-01  order rules (orders/rules.py) through the PAPER broker
# ═════════════════════════════════════════════════════════════════════════════

KNOWN = {"ACME", "BETA", "GAMMA", "DELTA"}


class Feed:
    """The live-quote feed: whatever prices the test sets, stamped `age_s` seconds old."""

    def __init__(self):
        self.prices, self.age_s = {}, 0.0

    def quotes(self, symbols, *a, **k):
        import pandas as pd
        ts = datetime.now() - timedelta(seconds=self.age_s)
        return pd.DataFrame([{"symbol": s, "ltp": self.prices[s], "prev_close": None, "timestamp": ts}
                             for s in symbols if s in self.prices],
                            columns=["symbol", "ltp", "prev_close", "timestamp"])


@pytest.fixture
def paper(temp_db, tmp_path, monkeypatch):
    """broker_env PAPER (explicitly), a 1,00,000 paper account with no slippage and no brokerage,
    no halt flag, no risk limits; security ids for KNOWN only; every quote from Feed; the real
    Dhan client unreachable. Orders go orders.rules -> orders.broker -> orders.paper for real."""
    import data.dhan as D
    import orders.broker as B
    import orders.environment as E
    import orders.paper as P
    import orders.risk as R
    from db.schema import init_db
    from orders import rules
    init_db()
    rules.init_orders_table()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"broker_env": "PAPER", "paper_opening_balance": 100000.0,
                               "paper_slippage_bps": 0.0, "paper_brokerage_pct": 0.0}), encoding="utf-8")
    for mod in (E, R, P):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(R, "HALT_FLAG", tmp_path / "TRADING_HALTED")

    def security_id(symbol):
        return {"security_id": "500001", "exchange": "NSE_EQ"} if str(symbol).upper() in KNOWN else None

    def no_dhan(*a, **k):
        raise RuntimeError("no Dhan client in tests")

    monkeypatch.setattr(rules, "get_security_id", security_id)
    monkeypatch.setattr(B, "get_security_id", security_id)
    monkeypatch.setattr(B, "get_dhan_client", no_dhan)
    feed = Feed()
    for mod in (rules, B, D):
        monkeypatch.setattr(mod, "fetch_live_quotes", feed.quotes)
    monkeypatch.setattr(P.PaperBroker, "_quote_cache", {})
    monkeypatch.setattr(P.PaperBroker, "QUOTE_TTL_SECONDS", 0.0)
    assert E.broker_env() == E.PAPER
    with LT.block_network():
        yield feed


def _rule(**kw):
    p = {"symbol": "ACME", "side": "BUY", "trigger_type": "PRICE", "trigger_value": 100.0, "reference_price": 102.0,
         "quantity_type": "SHARES", "quantity_value": 10}
    p.update(kw)
    from orders.rules import create_rule
    return create_rule(p)


def _book(symbol="ACME"):
    """(paper cash, quantity held, realized P&L) -- read straight from the paper tables."""
    from db.schema import get_connection
    c = get_connection()
    try:
        bal = c.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
        pos = c.execute("SELECT quantity, realized_pnl FROM paper_position WHERE symbol=?", (symbol,)).fetchone()
        return (round(bal[0], 2) if bal else None, int(pos[0]) if pos else 0, round(pos[1], 2) if pos else 0.0)
    finally:
        c.close()


def _paper_orders():
    from db.schema import get_connection
    c = get_connection()
    try:
        return [dict(r) for r in c.execute("SELECT symbol, transaction_type, quantity, filled_qty, fill_price, status "
                                           "FROM paper_order ORDER BY created_at, rowid")]
    except Exception:
        return []
    finally:
        c.close()


def test_rule_creation_resolves_triggers_and_stops_and_refuses_bad_rules(paper):
    from orders import rules as OR
    buy = _rule(trigger_type="PERCENT", trigger_value=None, trigger_percent=-2.5, reference_price=200.0,
                stoploss={"type": "PERCENT", "value": -3})
    assert buy["resolved_trigger_price"] == 195.0                      # 200 * (1 - 2.5%)
    assert buy["resolved_stoploss_price"] == 189.15                    # 195 * (1 - 3%)
    assert (buy["trigger_direction"], buy["role"], buy["status"], buy["require_confirmation"]) == \
        ("BELOW", "ENTRY", "ACTIVE", True)                             # a BUY fires on a fall by default
    sell = _rule(side="SELL", trigger_value=210.456, stoploss={"type": "AMOUNT", "value": 5})
    assert (sell["resolved_trigger_price"], sell["resolved_stoploss_price"], sell["trigger_direction"]) == \
        (210.46, 215.46, "ABOVE")
    stop = _rule(side="SELL", trigger_value=95, trigger_direction="BELOW", role="STOP", symbol="beta ")
    assert (stop["symbol"], stop["trigger_direction"], stop["role"]) == ("BETA", "BELOW", "STOP")

    bad = [
        ({"symbol": "NOPE"}, ValueError, "security_id not found"),
        ({"side": "SELL", "quantity_type": "RISK", "quantity_value": 1.0}, ValueError, "sizes an entry"),
        ({"quantity_type": "RISK", "quantity_value": 1.0}, ValueError, "needs a stoploss below"),
        ({"quantity_type": "RISK", "quantity_value": 1.0, "stoploss": {"type": "PERCENT", "value": 2}},
         ValueError, "needs a stoploss below"),                       # a stop ABOVE a buy trigger
        ({"quantity_type": "RISK", "quantity_value": 0, "stoploss": {"type": "PERCENT", "value": -2}},
         ValueError, "% of capital to risk"),
    ]
    for kw, exc, msg in bad:
        with pytest.raises(exc, match=re.escape(msg)):
            _rule(**kw)
    with pytest.raises(KeyError):
        OR.create_rule({"symbol": "ACME", "side": "BUY"})           # no trigger at all
    assert len(OR.list_rules()) == 3, "a refused rule is never stored"


@pytest.mark.parametrize("side,direction,role,trigger_price,price,fires", [
    ("BUY", None, None, 100.0, 100.0, "ENTRY"),          # side-implied BELOW: at the trigger fires
    ("BUY", None, None, 100.0, 100.01, None),
    ("SELL", None, None, 110.0, 110.0, "ENTRY"),         # side-implied ABOVE
    ("SELL", None, None, 110.0, 109.99, None),
    ("SELL", "BELOW", "STOP", 95.0, 94.0, "STOP"),       # a real stop: SELL when the price FALLS
    ("SELL", "BELOW", "STOP", 95.0, 96.0, None),
    ("BUY", "ABOVE", "STOP", 105.0, 106.0, "STOP"),      # a short's stop: BUY back when it RISES
])
def test_condition_follows_the_rules_own_direction(side, direction, role, trigger_price, price, fires):
    from orders.rules import _condition_met
    rule = {"side": side, "trigger_direction": direction, "role": role, "resolved_trigger_price": trigger_price}
    assert _condition_met(rule, price) == fires


def test_check_triggers_only_flags_a_confirmation_rule_and_places_nothing(paper):
    from orders import rules as OR
    r = _rule()                                                      # BUY ACME <= 100, confirmation required
    paper.prices["ACME"] = 101.0
    assert OR.check_triggers() == [] and OR.get_rule(r["id"])["status"] == "ACTIVE"
    paper.prices["ACME"] = 99.0
    t0 = datetime.now()
    ev = OR.check_triggers()
    assert [(e["type"], e["rule_id"], e["reason"], e["price"]) for e in ev] == \
        [("PENDING_CONFIRMATION", r["id"], "ENTRY", 99.0)]
    got = OR.get_rule(r["id"])
    assert (got["status"], got["trigger_hit_price"]) == ("PENDING_CONFIRMATION", 99.0)
    expires = datetime.fromisoformat(got["confirmation_expires_at"])
    assert abs((expires - t0).total_seconds() - OR.CONFIRMATION_WINDOW_SECONDS) < 60      # 30 minutes
    assert OR.check_triggers() == [], "a flagged rule is not flagged again"
    assert _paper_orders() == [], "monitoring never places an order"
    # past its window it expires, unconfirmed and unplaced
    from db.schema import get_connection
    c = get_connection()
    c.execute("UPDATE order_rules SET confirmation_expires_at=? WHERE id=?",
              ((datetime.now() - timedelta(minutes=1)).isoformat(), r["id"]))
    c.commit()
    c.close()
    assert OR.check_triggers() == [{"type": "EXPIRED", "rule_id": r["id"]}]
    assert OR.get_rule(r["id"])["status"] == "EXPIRED" and _paper_orders() == []


def test_bracket_entry_targets_and_stop_settle_as_one_oco_group_in_the_paper_book(paper):
    """BUY 10 ACME at <= 100 with targets +5% (half) / +10% (rest) and a 4% stop.
    Fills at 99 -> legs from 99: T1 103.95 x5, T2 108.90 x5, STOP 95.04 x10.
    104 -> T1 sells 5 (stop shrinks to 5); 96 -> nothing; 95 -> the stop sells 5 and cancels T2.
    Cash: 1,00,000 - 990 + 520 + 475 = 1,00,005; realized: 5 x (104 - 99) + 5 x (95 - 99) = +5."""
    from orders import rules as OR
    entry = _rule(bracket_target_pct=5, bracket_target2_pct=10, bracket_stop_pct=4, bracket_target_split=0.5)
    paper.prices["ACME"] = 99.0
    OR.check_triggers()
    res = OR.confirm_rule(entry["id"])                               # price unchanged since the trigger
    assert res["status"] == "PLACED"
    e = OR.get_rule(entry["id"])
    assert (e["status"], e["execution_price"], e["execution_quantity"]) == ("EXECUTED", 99.0, 10)
    assert _book() == (99010.0, 10, 0.0)
    legs = {(l["role"], l["trigger"]): l for l in res["bracket_legs"]}
    assert sorted((l["role"], l["trigger"], l["qty"]) for l in res["bracket_legs"]) == \
        [("STOP", 95.04, 10), ("TARGET", 103.95, 5), ("TARGET", 108.9, 5)]
    rows = {l["id"]: OR.get_rule(l["id"]) for l in res["bracket_legs"]}
    assert len({r["oco_group_id"] for r in rows.values()}) == 1
    assert all(r["parent_rule_id"] == entry["id"] and r["side"] == "SELL" and r["require_confirmation"] is False
               for r in rows.values()), "exit legs execute without a tap"
    t1, t2, stop = legs[("TARGET", 103.95)]["id"], legs[("TARGET", 108.9)]["id"], legs[("STOP", 95.04)]["id"]
    assert (rows[t1]["trigger_direction"], rows[t2]["trigger_direction"], rows[stop]["trigger_direction"]) == \
        ("ABOVE", "ABOVE", "BELOW")

    paper.prices["ACME"] = 104.0
    ev = OR.check_triggers()
    assert [(x["type"], x["rule_id"]) for x in ev] == [("PLACED", t1)]
    assert ev[0]["oco"] == {"cancelled": [], "stop_reduced_to": 5.0}
    assert OR.get_rule(stop)["quantity_value"] == 5.0 and OR.get_rule(t2)["status"] == "ACTIVE"
    assert _book() == (99530.0, 5, 25.0)

    paper.prices["ACME"] = 96.0
    assert OR.check_triggers() == []

    paper.prices["ACME"] = 95.0
    ev = OR.check_triggers()
    assert [(x["type"], x["rule_id"]) for x in ev] == [("PLACED", stop)]
    assert ev[0]["oco"] == {"cancelled": [t2], "stop_reduced_to": 0}
    assert {i: OR.get_rule(i)["status"] for i in (entry["id"], t1, t2, stop)} == \
        {entry["id"]: "EXECUTED", t1: "EXECUTED", t2: "CANCELLED", stop: "EXECUTED"}
    assert _book() == (100005.0, 0, 5.0)
    assert [(o["transaction_type"], o["quantity"], o["fill_price"], o["status"]) for o in _paper_orders()] == \
        [("BUY", 10, 99.0, "TRADED"), ("SELL", 5, 104.0, "TRADED"), ("SELL", 5, 95.0, "TRADED")]
    paper.prices["ACME"] = 200.0
    assert OR.check_triggers() == [], "nothing of the bracket is left to fire"


def test_settle_oco_group_without_a_group_is_a_no_op():
    from orders import rules as OR
    assert OR.settle_oco_group({"id": "x", "symbol": "ACME"}, 5) == {"cancelled": [], "stop_reduced_to": None}


def test_settle_oco_group_when_the_targets_cover_the_whole_position(paper):
    """Stop covers 10; one target fills all 10 -> the position is flat and the stop is closed."""
    from orders import rules as OR
    group = "G1"
    tgt = _rule(side="SELL", trigger_value=110, role="TARGET", oco_group_id=group, require_confirmation=False)
    stp = _rule(side="SELL", trigger_value=90, trigger_direction="BELOW", role="STOP", oco_group_id=group,
                require_confirmation=False)
    out = OR.settle_oco_group(OR.get_rule(tgt["id"]), 10)
    assert out == {"cancelled": [stp["id"]], "stop_reduced_to": 0.0}
    assert OR.get_rule(stp["id"])["status"] == "CANCELLED"
    assert "all targets filled" in OR.get_rule(stp["id"])["notes"]


def test_an_auto_exit_refuses_a_stale_quote(paper):
    from orders import rules as OR
    leg = _rule(side="SELL", trigger_value=95, trigger_direction="BELOW", role="STOP", require_confirmation=False)
    paper.prices["ACME"], paper.age_s = 94.0, 600
    ev = OR.check_triggers()
    assert [(e["type"], e["rule_id"], e["quote_age_s"]) for e in ev] == [("SKIPPED_STALE_QUOTE", leg["id"], 600)]
    assert OR.get_rule(leg["id"])["status"] == "ACTIVE" and _paper_orders() == []


def test_confirm_refuses_a_price_that_ran_away_unless_forced(paper):
    from orders import rules as OR
    r = _rule()
    paper.prices["ACME"] = 99.0
    OR.check_triggers()
    paper.prices["ACME"] = 100.4                       # (100.4 - 99) / 99 = +1.41% against a buyer, limit 1%
    out = OR.confirm_rule(r["id"])
    assert (out["status"], out["trigger_price"], out["current_price"], out["drift_pct"]) == \
        ("REFUSED_SLIPPAGE", 99.0, 100.4, 1.41)
    assert OR.get_rule(r["id"])["status"] == "PENDING_CONFIRMATION" and _paper_orders() == []
    assert OR.confirm_rule("no-such-rule")["error"] == "rule not found"
    forced = OR.confirm_rule(r["id"], force=True)
    assert forced["status"] == "PLACED" and _book() == (100000.0 - 1004.0, 10, 0.0)      # filled at 100.40
    assert OR.confirm_rule(r["id"])["status"] == "FAILED", "an executed rule cannot be confirmed twice"


def test_bracket_legs_are_measured_from_the_actual_fill(paper):
    """_create_bracket_legs: 'Percentages are measured from the ACTUAL fill price, not the trigger
    price'. Triggered at 99, confirmed (forced) when the paper broker fills at 100.40: the stop
    must sit 4% under 100.40 = 96.38, the target 5% over = 105.42 -- not 95.04 / 103.95, which
    is 4% / 5% from a price nobody paid."""
    from orders import rules as OR
    r = _rule(bracket_target_pct=5, bracket_stop_pct=4)
    paper.prices["ACME"] = 99.0
    OR.check_triggers()
    paper.prices["ACME"] = 100.4
    out = OR.confirm_rule(r["id"], force=True)
    assert out["status"] == "PLACED"
    assert OR.get_rule(r["id"])["execution_price"] == 100.4
    assert sorted((l["role"], l["trigger"], l["qty"]) for l in out["bracket_legs"]) == \
        [("STOP", 96.38, 10), ("TARGET", 105.42, 10)]


def test_trailing_stops_ratchet_only_in_the_positions_favour(paper):
    from orders import rules as OR
    from orders.rules import update_trailing_stops as trail
    a = _rule(side="SELL", trigger_value=95, trigger_direction="BELOW", role="STOP",
              trail_enabled=True, trail_type="PERCENT", trail_value=5, trail_jump=0, trail_high_water=100)
    b = _rule(symbol="BETA", side="SELL", trigger_value=97, trigger_direction="BELOW", role="STOP",
              trail_enabled=True, trail_type="AMOUNT", trail_value=3, trail_jump=2, trail_high_water=100)
    c = _rule(symbol="GAMMA", side="BUY", trigger_value=105, trigger_direction="ABOVE", role="STOP",
              trail_enabled=True, trail_type="PERCENT", trail_value=4, trail_jump=0, trail_high_water=100)
    plain = _rule(symbol="DELTA", side="SELL", trigger_value=90, trigger_direction="BELOW", role="STOP")

    def state(rid):
        g = OR.get_rule(rid)
        return g["resolved_trigger_price"], g["trail_high_water"], g["trail_moves"]

    # long, 5% under the high: 110 -> 104.50; 105 is no new high; 120 -> 114.00
    assert [(m["rule_id"], m["from"], m["to"], m["high_water"]) for m in trail({"ACME": 110.0})] == \
        [(a["id"], 95.0, 104.5, 110.0)]
    assert trail({"ACME": 105.0}) == [] and state(a["id"]) == (104.5, 110.0, 1)
    assert [m["to"] for m in trail({"ACME": 120.0})] == [114.0] and state(a["id"]) == (114.0, 120.0, 2)
    # Rs 3 under the high, in steps of at least Rs 2: 97 -> (97.5 no, 98.5 no) -> 99.0 yes
    assert trail({"BETA": 100.5}) == [] and state(b["id"]) == (97.0, 100.5, 0)       # the high still moves
    assert trail({"BETA": 101.5}) == [] and state(b["id"]) == (97.0, 101.5, 0)
    assert [m["to"] for m in trail({"BETA": 102.0})] == [99.0] and state(b["id"]) == (99.0, 102.0, 1)
    # a short's stop trails 4% ABOVE the low: 90 -> 93.60; a bounce to 95 changes nothing
    assert [m["to"] for m in trail({"GAMMA": 90.0})] == [93.6]
    assert trail({"GAMMA": 95.0}) == [] and state(c["id"]) == (93.6, 90.0, 1)
    # never widens on a fall; a stop without trailing never moves
    assert trail({"ACME": 80.0, "DELTA": 200.0}) == []
    assert state(a["id"])[0] == 114.0 and OR.get_rule(plain["id"])["resolved_trigger_price"] == 90.0


def test_check_triggers_trails_before_it_tests_the_stop(paper):
    """Stop 90 trailing 5% from 100. One tick at 110 moves it to 104.50 (and does not fire);
    104 then fires it -- against the OLD level (90) it never would have."""
    from orders import rules as OR
    s = _rule(symbol="DELTA", side="SELL", trigger_value=90, trigger_direction="BELOW", role="STOP",
              trail_enabled=True, trail_type="PERCENT", trail_value=5, trail_high_water=100)
    paper.prices["DELTA"] = 110.0
    ev = OR.check_triggers()
    assert [(e["type"], e["to"]) for e in ev] == [("TRAIL_MOVED", 104.5)]
    paper.prices["DELTA"] = 104.0
    ev = OR.check_triggers()
    assert [(e["type"], e["rule_id"], e["reason"]) for e in ev] == [("PENDING_CONFIRMATION", s["id"], "STOP")]


# ═════════════════════════════════════════════════════════════════════════════
#  OPS-02  the post-market job, end to end, offline
# ═════════════════════════════════════════════════════════════════════════════

OFFLINE_JOBS = {"data_quality", "technical_indicators", "ai_scoring_engine", "accuracy_update", "portfolio_health",
                "signal_log", "signal_outcomes", "dashboard_rebuild"}
DOWNLOAD_JOBS = {"bhavcopy_eod", "bulk_block_deals", "fo_bhavcopy", "corporate_actions", "dhan_historical_eod",
                 "benchmark_history", "nse_index_closes", "market_series", "news_weights"}
TD = date(2026, 9, 18)                          # a Friday session in the NSE calendar


@pytest.fixture
def market(temp_db, tmp_path, monkeypatch):
    """260 sessions up to TD: NIFTY50 and three tracked stocks -- AAA rising 0.3% a session,
    BBB falling 0.3%, CCC flat with a 6% wave -- with delivery data and the session's FII/DII,
    so the job's 'evening data' gate lets the signals be logged. seed_weights() is what
    `python main.py --init` runs on an install."""
    from db.schema import get_connection, init_db, seed_weights
    from utils.trading_calendar import is_trading_day
    import data.dhan
    monkeypatch.chdir(tmp_path)                 # dashboard_state.json and caches land here
    init_db()
    seed_weights()
    days, d = [], TD
    while len(days) < 260:
        if is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.sort()
    series = {"NIFTY50": (20000.0, 0.0004, 0.0), "AAA": (100.0, 0.003, 0.0), "BBB": (100.0, -0.003, 0.0),
              "CCC": (100.0, 0.0, 0.06)}
    rows = [(s, str(day), c * 0.995, c * 1.01, c * 0.99, c, 1_000_000 + i * 1000, 45.0)
            for s, (p0, g, a) in series.items() for i, day in enumerate(days)
            for c in [p0 * (1 + g) ** i * (1 + a * math.sin(i / 5))]]
    c = get_connection()
    c.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,delivery_pct) "
                  "VALUES (?,?,?,?,?,?,?,?)", rows)
    c.execute("INSERT INTO fii_dii_market (date, fii_net_cr, dii_net_cr) VALUES (?,?,?)", (str(TD), 500.0, 300.0))
    c.commit()
    c.close()
    monkeypatch.setattr(data.dhan, "get_tracked_symbols", lambda conn=None: ["AAA", "BBB", "CCC"])
    with LT.block_network():
        yield days


def _run_postmarket(monkeypatch):
    from pipeline import scheduler as S
    real, ran, skipped = S.run_job, [], []

    def run_job(name, fn, *a, **k):
        if name in OFFLINE_JOBS:
            ran.append(name)
            return real(name, fn, *a, **k)
        skipped.append(name)
        return {"status": "SKIPPED", "reason": "offline test"}

    monkeypatch.setattr(S, "run_job", run_job)
    monkeypatch.setattr(S, "_run_dhan_quotes", lambda *a, **k: None)          # live quotes: network
    monkeypatch.setattr(S, "_run_portfolio_sync", lambda *a, **k: None)       # broker holdings: network
    return S.run_postmarket(force=True, target_date=TD), ran, skipped


def test_postmarket_scores_a_seeded_session_end_to_end(market, monkeypatch):
    from db.schema import get_connection
    out, ran, skipped = _run_postmarket(monkeypatch)
    assert out == {"status": "SCORED", "date": str(TD)}
    assert ran == ["data_quality", "technical_indicators", "ai_scoring_engine", "accuracy_update",
                   "portfolio_health", "signal_log", "signal_outcomes", "dashboard_rebuild"], \
        "indicators before scores, scores before the signal log, the dashboard last"
    assert DOWNLOAD_JOBS <= set(skipped) and not (set(skipped) & OFFLINE_JOBS)
    c = get_connection()
    try:
        scores = {r["symbol"]: dict(r) for r in c.execute(
            "SELECT symbol, atip_score, vpi, mri, rri, cri, zpi, acs, tod_score, `signal`, atip_rank, regime "
            "FROM ai_scores WHERE date=?", (str(TD),))}
        assert set(scores) == {"AAA", "BBB", "CCC"}, "the tracked universe, not NIFTY50"
        for sym, r in scores.items():
            for k in ("atip_score", "vpi", "mri", "rri", "cri", "zpi", "acs", "tod_score"):
                assert r[k] is not None and 0.0 <= r[k] <= 100.0, (sym, k, r[k])
            assert r["signal"] in {"BUY", "SELL", "HOLD", "WAIT"}
        ranked = sorted(scores, key=lambda s: -scores[s]["atip_score"])
        assert [scores[s]["atip_rank"] for s in ranked] == [1, 2, 3]
        assert scores["AAA"]["atip_score"] > scores["BBB"]["atip_score"]   # the riser outranks the faller
        assert scores["AAA"]["rri"] > scores["BBB"]["rri"]                  # relative strength says the same
        assert c.execute("SELECT COUNT(*) FROM technical_indicators WHERE date=?", (str(TD),)).fetchone()[0] == 4
        mh = dict(c.execute("SELECT mh_score, regime, breadth_universe, advances, declines FROM market_health "
                            "WHERE date=?", (str(TD),)).fetchone())
        assert 0 <= mh["mh_score"] <= 100 and mh["breadth_universe"] == 3 and mh["advances"] + mh["declines"] == 3
        assert {r["regime"] for r in scores.values()} == {mh["regime"]}
        runs = {r[0]: r[1] for r in c.execute("SELECT job_name, status FROM pipeline_log WHERE kind='run'")}
        assert all(runs[j] == "SUCCESS" for j in ("technical_indicators", "ai_scoring_engine", "signal_log")), runs
    finally:
        c.close()
    # a re-run of the same session rewrites, never duplicates
    out2, _, _ = _run_postmarket(monkeypatch)
    c = get_connection()
    try:
        again = {r["symbol"]: r["atip_score"] for r in c.execute("SELECT symbol, atip_score FROM ai_scores WHERE date=?",
                                                                 (str(TD),))}
    finally:
        c.close()
    assert out2["status"] == "SCORED" and again == {s: r["atip_score"] for s, r in scores.items()}


def test_postmarket_refuses_a_session_that_is_a_copy_of_the_previous_one(market, monkeypatch):
    """The stale-EOD guard: TD's bars identical to the session before -> no indicators, no scores."""
    from db.schema import get_connection
    prev = str(market[-2])
    c = get_connection()
    c.execute("DELETE FROM prices_daily WHERE date=?", (str(TD),))
    c.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,delivery_pct) SELECT symbol,?,open,"
              "high,low,close,volume,delivery_pct FROM prices_daily WHERE date=?", (str(TD), prev))
    c.commit()
    c.close()
    out, ran, _ = _run_postmarket(monkeypatch)
    assert out["status"] == "SKIPPED_STALE_EOD" and out["identical"] == out["checked"] == 3
    assert "technical_indicators" not in ran and "ai_scoring_engine" not in ran and "signal_log" not in ran
    c = get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM ai_scores WHERE date=?", (str(TD),)).fetchone()[0] == 0
    finally:
        c.close()


# ═════════════════════════════════════════════════════════════════════════════
#  QR-11  parameter sensitivity (backtest/sensitivity.py)
# ═════════════════════════════════════════════════════════════════════════════

START, END = "2025-01-01", "2025-09-30"
WAVES = ("AAA", "BBB", "CCC")


@pytest.fixture
def waves(temp_db, tmp_path, monkeypatch):
    """Three sine-wave stocks the dip strategy trades often (as in test_w39_backtest.py). Daily
    turnover is close x 2,000,000 >= 93 x 2e6 = 1.86e8 on every bar."""
    import backtest.config as bc
    from backtest.walkforward import trading_sessions
    from db.schema import get_connection, init_db
    monkeypatch.setattr(bc, "CONFIG_PATH", tmp_path / "no_config.json")
    init_db()
    days = trading_sessions(START, END)
    rows = []
    for k, sym in enumerate(WAVES):
        prev = None
        for t, d in enumerate(days):
            cl = round(100 * (1 + 0.0008 * t) * (1 + 0.07 * math.sin(2 * math.pi * (t + 5 * k) / 17)), 2)
            o = prev or cl
            rows.append((sym, str(d), o, round(max(o, cl) * 1.004, 2), round(min(o, cl) * 0.996, 2), cl, 2_000_000))
            prev = cl
    c = get_connection()
    c.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return days


def _dip(**kw):
    return {"strategy_id": "dip", "universe": list(WAVES), "start": START, "end": END, **kw}


@pytest.mark.parametrize("spec,base,steps,expected", [
    ({"min": 1.0, "max": 10.0}, 5.0, 2, [4.0, 4.5, 5.0, 5.5, 6.0]),           # +-10% / 20% of the base
    ({"min": 1, "max": 30}, 20, 2, [16, 18, 20, 22, 24]),                      # integer range stays integer
    ({"min": 1, "max": 10}, 10, 2, [8, 9, 10]),                                # clipped at max, no duplicates
    ({"min": 0.0, "max": 10.0}, 0, 2, [3.0, 4.0, 5.0, 6.0, 7.0]),              # base 0: mid-range +-10% of range
    ({"values": [1, 2, 3, 4, 5]}, 1, 2, [1, 2, 3]),                            # grid window clipped at the start
    ({"min": 0, "max": 10, "step": 2}, 5, 1, [2, 4, 6]),                       # off-grid base: nearest point (4)
])
def test_neighbourhood_of_the_chosen_value(spec, base, steps, expected):
    from backtest.sensitivity import _around
    assert _around(spec, base, steps) == expected


def test_sensitivity_on_real_backtests_finds_a_plateau(waves):
    """min_avg_turnover never binds below 1.86e8, so 5e7 / 1e8 / 1.5e8 trade identically:
    a flat curve, stability 1.0, no knife edge, 3 distinct trials (the base is reused)."""
    from backtest import store
    from backtest.sensitivity import sensitivity
    from db.schema import get_connection
    out = sensitivity(_dip(params={"min_avg_turnover": 1e8}), {"min_avg_turnover": {"values": [5e7, 1e8, 1.5e8]}},
                      select_by="trades", steps=1, min_trades=1)
    p = out["parameters"]["min_avg_turnover"]
    assert out["status"] == "COMPLETED" and out["base_params"]["min_avg_turnover"] == 1e8
    assert [pt["value"] for pt in p["curve"]] == [5e7, 1e8, 1.5e8]
    trades = [pt["trades"] for pt in p["curve"]]
    assert trades[0] == trades[1] == trades[2] == out["base_trades"] and out["base_trades"] >= 1
    assert (p["stability"], p["knife_edge"], out["robust_share"], out["knife_edges"], out["trials"]) == \
        (1.0, False, 1.0, [], 3)
    assert p["curve"][1]["run_id"] == out["base_run_id"]
    c = get_connection()
    try:
        parent = store.get_run(c, out["run_id"])
        kids = store.child_runs(c, out["run_id"])
    finally:
        c.close()
    assert (parent["kind"], parent["status"]) == ("sensitivity", "COMPLETED")
    assert sorted(k["kind"] for k in kids) == ["sens_trial"] * 3 and all(k["status"] == "COMPLETED" for k in kids)


A = {1.0: 0.2, 2.0: 0.9, 3.0: 1.0, 4.0: 0.8, 5.0: -0.1}        # metric factor by stop_pct  (base 3.0)
B = {4.0: 0.3, 5.0: 0.4, 6.0: 1.0, 7.0: 1.1, 8.0: 1.2}         # metric factor by target_pct (base 6.0)


@pytest.fixture
def stub_trials(monkeypatch):
    """Replace the backtest of each trial: sharpe = A[stop_pct] * B[target_pct], 50 trades."""
    import backtest.sensitivity as SEN
    calls = []

    def trial(base, params, kind, parent, idx):
        calls.append(dict(params))
        return f"T{idx}", {"status": "COMPLETED",
                           "metrics": {"sharpe": A[params["stop_pct"]] * B[params["target_pct"]], "trades": 50}}
    monkeypatch.setattr(SEN, "_trial", trial)
    return calls


SPACE = {"stop_pct": {"values": [1.0, 2.0, 3.0, 4.0, 5.0]}, "target_pct": {"values": [4.0, 5.0, 6.0, 7.0, 8.0]}}


def test_stability_and_knife_edge_by_hand(waves, stub_trials):
    """stop_pct: neighbours 0.9 / 0.8 of a base 1.0 -> stability 0.8, a plateau.
    target_pct: neighbours 0.4 / 1.1 -> stability 0.4 < 0.5 -> a knife edge.
    9 distinct parameter sets: the base, 4 for each parameter."""
    from backtest.sensitivity import sensitivity
    out = sensitivity(_dip(), SPACE, select_by="sharpe", steps=2, min_trades=10)
    s, t = out["parameters"]["stop_pct"], out["parameters"]["target_pct"]
    assert out["base_sharpe"] == 1.0
    assert [pt["sharpe"] for pt in s["curve"]] == pytest.approx([0.2, 0.9, 1.0, 0.8, -0.1])
    assert [pt["sharpe"] for pt in t["curve"]] == pytest.approx([0.3, 0.4, 1.0, 1.1, 1.2])
    assert (s["stability"], s["knife_edge"], t["stability"], t["knife_edge"]) == (0.8, False, 0.4, True)
    assert (out["robust_share"], out["knife_edges"], out["trials"], out["heatmap"]) == (0.5, ["target_pct"], 9, None)
    assert len(stub_trials) == 9, "each parameter set is backtested once"


def test_pairwise_heatmap_and_sign_flips(waves, stub_trials, monkeypatch):
    from backtest.sensitivity import sensitivity
    out = sensitivity(_dip(), SPACE, steps=2, pairwise=["stop_pct", "target_pct"])
    h = out["heatmap"]
    assert (h["x"], h["y"], h["x_values"], h["y_values"]) == ("stop_pct", "target_pct", list(A), list(B))
    assert h["grid"] == [[pytest.approx(A[x] * B[y]) for x in A] for y in B]
    assert out["trials"] == 25 and len(stub_trials) == 25, "the 9 one-at-a-time sets are inside the 5 x 5 grid"
    # a negative base: no stability ratio, and a neighbour that turns positive is a knife edge
    A.update({3.0: -0.5, 2.0: -0.6, 4.0: 0.2})
    try:
        out = sensitivity(_dip(), {"stop_pct": SPACE["stop_pct"]}, steps=1)
        st = out["parameters"]["stop_pct"]
        assert (out["base_sharpe"], st["stability"], st["knife_edge"]) == (-0.5, None, True)
        A.update({4.0: -0.4})                     # both neighbours negative too: no flip, not a knife edge
        st = sensitivity(_dip(), {"stop_pct": SPACE["stop_pct"]}, steps=1)["parameters"]["stop_pct"]
        assert (st["stability"], st["knife_edge"]) == (None, False)
    finally:
        A.update({3.0: 1.0, 2.0: 0.9, 4.0: 0.8})


def test_a_neighbour_that_stops_trading_is_a_thin_edge_not_a_knife_edge(waves, monkeypatch):
    """W39b QR-11 decision: stop_pct=4.0 trades only 3 times (< min_trades 10), so it has no metric.
    It used to vanish silently; now it is listed in thin_neighbours / thin_edges, while the knife-edge
    test and robust_share still look only at neighbours that have a metric."""
    import backtest.sensitivity as SEN

    def trial(base, params, kind, parent, idx):
        trades = 3 if params["stop_pct"] == 4.0 else 50
        return f"T{idx}", {"status": "COMPLETED",
                           "metrics": {"sharpe": A[params["stop_pct"]] * B[params["target_pct"]], "trades": trades}}
    monkeypatch.setattr(SEN, "_trial", trial)
    out = SEN.sensitivity(_dip(), SPACE, steps=1, min_trades=10)
    s, t = out["parameters"]["stop_pct"], out["parameters"]["target_pct"]
    assert [pt["sharpe"] for pt in s["curve"]] == [pytest.approx(0.9), 1.0, None]
    assert (s["stability"], s["knife_edge"], s["thin_neighbours"]) == (0.9, False, [4.0])
    assert t["thin_neighbours"] == []
    assert (out["thin_edges"], out["knife_edges"], out["robust_share"]) == (["stop_pct"], ["target_pct"], 0.5)


def test_sensitivity_refuses_bad_requests_and_marks_a_crashed_run_failed(waves, stub_trials, monkeypatch):
    import backtest.sensitivity as SEN
    from db.schema import get_connection
    for req, space, steps, msg in [
        (_dip(period_label="test"), SPACE, 2, "test window"),
        (_dip(allow_test=True), SPACE, 2, "test window"),
        (_dip(), SPACE, 0, "steps must be 1..5"),
        (_dip(), SPACE, 6, "steps must be 1..5"),
        (_dip(), {"nope": {"values": [1, 2]}}, 1, "nope is not a parameter of dip"),
        (_dip(), {}, 1, "non-empty"),
    ]:
        with pytest.raises(ValueError, match=re.escape(msg)):
            SEN.sensitivity(req, space, steps=steps)
    real = SEN._trial

    def crash(base, params, kind, parent, idx):
        if idx == 2:
            raise RuntimeError("engine fell over")
        return real(base, params, kind, parent, idx)
    monkeypatch.setattr(SEN, "_trial", crash)
    with pytest.raises(RuntimeError, match="engine fell over"):
        SEN.sensitivity(_dip(), SPACE, steps=1)
    c = get_connection()
    try:
        rows = [tuple(r) for r in c.execute("SELECT status, error FROM backtest_run WHERE kind='sensitivity'")]
    finally:
        c.close()
    assert rows == [("FAILED", "RuntimeError: engine fell over")], "only the crashed run was created, and it says why"


# ═════════════════════════════════════════════════════════════════════════════
#  ML-05  model presets (ml/presets.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ml_market(temp_db, tmp_path, monkeypatch):
    """400 weekday sessions from 2024-01-01: NIFTY50 rising 0.2% a session for 40 sessions, then
    falling 0.2% for 40, and so on, with Market Health BULL / BEAR in the same 40-session blocks;
    four tracked stocks with distinct drifts and waves. Model artifacts go to tmp_path/ml."""
    from db.schema import get_connection, init_db
    from ml import config
    import data.dhan
    init_db()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ml": {"model_path": str(tmp_path / "ml")}}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.chdir(tmp_path)
    days, d = [], date(2024, 1, 1)
    while len(days) < 400:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    c = get_connection()
    px = 20000.0
    for i, day in enumerate(days):
        bull = (i // 40) % 2 == 0
        if i:
            px *= 1.002 if bull else 0.998
        c.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                  ("NIFTY50", str(day), px, px * 1.005, px * 0.995, px, 1e6))
        c.execute("INSERT INTO market_health (date, mh_score, regime) VALUES (?,?,?)",
                  (str(day), 70 if bull else 30, "BULL" if bull else "BEAR"))
    for k, sym in enumerate(("AAA", "BBB", "CCC", "DDD")):
        for i, day in enumerate(days):
            p = 100 * (1 + 0.001 * (k - 1.5)) ** i * (1 + 0.05 * math.sin((i + 7 * k) / 6))
            c.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                      (sym, str(day), p, p * 1.01, p * 0.99, p, 1e6))
    c.commit()
    monkeypatch.setattr(data.dhan, "get_tracked_symbols", lambda conn=None: ["AAA", "BBB", "CCC", "DDD"])
    with LT.block_network():
        yield c, days, tmp_path
    c.close()


def test_regime_preset_trains_a_draft_market_regime_model_on_point_in_time_labels(ml_market):
    """300 sessions from days[100] to days[399]; the label is the regime 5 sessions later, so the
    last 5 have none: 295 rows. Nothing is activated."""
    from ml import dataset as DS
    from ml import presets, registry as REG
    c, days, tmp = ml_market
    start, end = days[100], days[399]
    out = presets.regime(c, start, end, family="native_gbm", horizon=5, n_windows=3)
    assert (out["model_id"], out["dataset_rows"], out["version"], out["status"]) == ("regime_gbm", 295, "v1", "TRAINED")
    assert out["validation_report"] and out["verdict"] and out["pooled"]["rows"] > 0
    m = REG.get_model(c, "regime_gbm")
    assert (m["purpose"], m["label_kind"], m["feature_set"], m["model_type"], m["status"], m["active_version"]) == \
        ("regime", "market_regime", "atip_technical@1", "native_gbm", "DRAFT", None)
    v = REG.get_version(c, "regime_gbm", "v1")
    assert v["status"] == "TRAINED" and v["dataset_id"] == f"regime_market@{start}_{end}_h5"
    assert Path(v["artifact_path"]).resolve().is_relative_to((tmp / "ml").resolve())
    assert REG.get_version(c, "regime_gbm") is None, "no ACTIVE version"
    spec = json.loads(c.execute("SELECT spec_json FROM ml_dataset WHERE dataset_id=?", (v["dataset_id"],)).fetchone()[0])
    assert (spec["universe"], spec["label"], spec["sampling"]) == \
        ("market", {"kind": "market_regime", "horizon": 5}, {"every_n_sessions": 1})
    # every label is Market Health's regime exactly 5 sessions after its row -- never the same day's
    ds = DS.build(c, DS.DatasetSpec.from_dict(spec))
    pos = {d: i for i, d in enumerate(days)}
    assert len(ds.y) == 295 and set(ds.symbols) == {"NIFTY50"}
    for d, y, ld in zip(ds.dates, ds.y, ds.label_dates):
        later = days[pos[d] + 5]
        assert ld == later and y == ("BULL" if (pos[later] // 40) % 2 == 0 else "BEAR")
    # run again: same model, a new version; a different model under the same id is refused
    assert presets.regime(c, start, end, n_windows=3)["version"] == "v2"
    REG.create_model(c, "regime_logistic_regression", "taken", "native_gbm", "market_regime", "atip_technical@1")
    with pytest.raises(ValueError, match="different type / feature set"):
        presets.regime(c, start, end, family="logistic_regression", n_windows=3)


def test_bootstrap_builds_one_dataset_and_a_draft_model_per_family(ml_market):
    from ml import presets, registry as REG
    c, days, _ = ml_market
    start, end = days[100], days[399]
    with pytest.raises(ValueError, match="family must be among"):
        presets.bootstrap(c, start, end, families=("logistic_regression", "xgboost"))
    assert c.execute("SELECT COUNT(*) FROM ml_dataset").fetchone()[0] == 0, "an unknown family is refused first"
    out = presets.bootstrap(c, start, end, families=("logistic_regression",), n_windows=3)
    ds = out["dataset"]
    # every 3rd of 300 sessions whose 5-session label falls by `end` (at most 99 dates), x the 4 tracked stocks
    assert 0 < ds["dates"] <= 99 and ds["rows"] == 4 * ds["dates"]
    [row] = out["models"]
    assert (row["model_id"], row["family"], row["version"], row["status"]) == \
        ("dir5_logistic", "logistic_regression", "v1", "TRAINED")
    m = REG.get_model(c, "dir5_logistic")
    assert (m["purpose"], m["label_kind"], m["feature_set"], m["status"], m["active_version"]) == \
        ("signal", "direction", "atip_core@1", "DRAFT", None)
    spec = json.loads(c.execute("SELECT spec_json FROM ml_dataset WHERE name='dir5_atip_core'").fetchone()[0])
    assert spec["label"] == presets.DEFAULT_LABEL == {"kind": "direction", "horizon": 5, "threshold_pct": 1.0}
    assert spec["sampling"] == {"every_n_sessions": 3, "max_symbols": 150}
    # train=False: the validation report only, no version
    out2 = presets.bootstrap(c, start, end, families=("native_gbm",), n_windows=3, train=False)
    assert out2["models"][0]["model_id"] == "dir5_gbm" and "version" not in out2["models"][0]
    assert REG.list_versions(c, "dir5_gbm") == []


# ═════════════════════════════════════════════════════════════════════════════
#  MON-06  main.py SafeRotatingFileHandler
# ═════════════════════════════════════════════════════════════════════════════
# Importing main.py has side effects (it creates atip_data/ next to itself and installs a
# handler on the root logger that opens atip_data/atip.log), so the class is taken from the
# source with ast and compiled on its own; the module-level wiring is checked by importing a
# COPY of main.py in a subprocess whose folder is a temp dir.

def _safe_handler_class():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.ClassDef) and n.name == "SafeRotatingFileHandler")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "LOG_ROTATE_RETRY_S" for t in n.targets))]
    assert len(keep) == 2, "main.py no longer defines SafeRotatingFileHandler / LOG_ROTATE_RETRY_S at module level"
    ns = {"logging": logging, "__name__": "main_extract"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(ROOT / "main.py"), "exec"), ns)
    return ns["SafeRotatingFileHandler"], ns["LOG_ROTATE_RETRY_S"]


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _logger(handler, name):
    lg = logging.getLogger(name)
    lg.handlers[:] = [handler]
    lg.propagate, lg.level = False, logging.INFO
    handler.setFormatter(logging.Formatter("%(message)s"))
    return lg


def _msg(i):
    return f"line {i:02d} ".ljust(49, ".")              # 49 chars + newline = 50 bytes per record


def test_rotation_keeps_backup_count_files_of_at_most_max_bytes(tmp_path):
    """maxBytes 200 with 50-byte records: a file takes 3 (150 + 50 >= 200 rolls over).
    10 records, 2 backups: .2 = 3-5, .1 = 6-8, current = 9; 0-2 rotated away."""
    Safe, _ = _safe_handler_class()
    log = tmp_path / "atip.log"
    h = Safe(str(log), maxBytes=200, backupCount=2, encoding="utf-8")
    lg = _logger(h, "w39.rotation")
    try:
        for i in range(10):
            lg.info(_msg(i))
    finally:
        h.close()
    assert [ln[:7] for ln in _lines(log)] == ["line 09"]
    assert [ln[:7] for ln in _lines(Path(f"{log}.1"))] == ["line 06", "line 07", "line 08"]
    assert [ln[:7] for ln in _lines(Path(f"{log}.2"))] == ["line 03", "line 04", "line 05"]
    assert not Path(f"{log}.3").exists()
    assert all(p.stat().st_size <= 200 for p in tmp_path.iterdir())


def test_a_refused_rename_keeps_logging_and_retries_after_the_window(tmp_path, monkeypatch):
    """Windows refuses to rename a log another process holds open. The plain handler then loses
    every record (each one retries the rollover and fails); the safe one appends to the current
    file, waits LOG_ROTATE_RETRY_S (600 s), and rotates on the first record after that."""
    Safe, retry_s = _safe_handler_class()
    assert retry_s == 600
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def refuse(src, dst):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    def run(cls, name):
        log = tmp_path / name
        h = cls(str(log), maxBytes=200, backupCount=2, encoding="utf-8")
        h.rotator = refuse
        errors = []
        h.handleError = lambda record: errors.append(record.getMessage()[:7])
        lg = _logger(h, f"w39.{name}")
        for i in range(10):
            lg.info(_msg(i))
        return h, lg, log, errors

    plain, _, plain_log, plain_errors = run(logging.handlers.RotatingFileHandler, "plain.log")
    plain.close()
    assert [ln[:7] for ln in _lines(plain_log)] == ["line 00", "line 01", "line 02"]
    assert plain_errors == [f"line {i:02d}" for i in range(3, 10)], "the stdlib handler drops records 3-9"

    h, lg, log, errors = run(Safe, "atip.log")
    try:
        assert errors == [] and h._retry_after == 1000.0 + 600
        assert [ln[:7] for ln in _lines(log)] == [f"line {i:02d}" for i in range(10)], "nothing lost"
        assert not Path(f"{log}.1").exists()
        clock[0] = 1599.0                                  # still inside the window: keep appending
        lg.info(_msg(10))
        assert len(_lines(log)) == 11 and not Path(f"{log}.1").exists()
        clock[0] = 1601.0                                  # window over, rename allowed again
        h.rotator = None
        lg.info(_msg(11))
    finally:
        h.close()
    assert [ln[:7] for ln in _lines(Path(f"{log}.1"))] == [f"line {i:02d}" for i in range(11)]
    assert [ln[:7] for ln in _lines(log)] == ["line 11"]


def test_main_installs_the_safe_handler_with_its_limits(tmp_path):
    """Import a copy of main.py from a temp folder in a fresh interpreter: the root logger gets
    the SafeRotatingFileHandler on <folder>/atip_data/atip.log, 20 MB x 10 backups."""
    shutil.copy(ROOT / "main.py", tmp_path / "main.py")
    code = ("import json, logging, sys; sys.path.insert(0, sys.argv[1]); import main; "
            "print(json.dumps([[type(h).__name__, getattr(h, 'baseFilename', None), getattr(h, 'maxBytes', None), "
            "getattr(h, 'backupCount', None)] for h in logging.getLogger().handlers] + [main.LOG_ROTATE_RETRY_S]))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=tmp_path, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    *handlers, retry = json.loads(r.stdout.strip().splitlines()[-1])
    assert retry == 600
    assert ["StreamHandler", None, None, None] in handlers
    assert ["SafeRotatingFileHandler", str(tmp_path / "atip_data" / "atip.log"), 20 * 1024 * 1024, 10] in handlers
    assert (tmp_path / "atip_data" / "atip.log").exists()


# ═════════════════════════════════════════════════════════════════════════════
#  DB-17  risk dashboard (/trading, dashboard/execution_page.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def api(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import init_db
    init_db()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    with LT.block_network():
        yield TestClient(server.app, raise_server_exceptions=False), security


def test_risk_dashboard_renders_with_its_portfolio_risk_section(api):
    from dashboard.execution_page import render
    client, security = api
    r = client.get("/trading")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    for el in ('id="prisk"', 'id="pbook"', 'id="phead"', 'id="pvar"', 'id="pconc"', 'id="prc"', 'id="pcorr"',
               'id="pperf"', 'id="pemx"', 'id="lim"'):
        assert el in html, f"{el} missing from /trading"
    assert "Portfolio risk (W25)" in html and security.token() in html
    page = render("TOKEN-X")
    assert "TOKEN-X" in page and "<script" in page and "/api/risk/portfolio?book=" in page


def _page_api_calls(html):
    """Every read the page's JavaScript makes, with the book / days argument filled in."""
    calls = set()
    for u in re.findall(r"j\('(/api/[^']+)'", html):
        if u.startswith("/api/zerodha"):
            continue                                # broker login: not part of the risk dashboard
        if u.endswith("book="):
            calls |= {u + "PAPER", u + "LIVE"}
        elif u.endswith("days="):
            calls.add(u + "30")
        else:
            calls.add(u)
    return sorted(calls)


def test_every_risk_dashboard_data_route_answers_200_on_an_empty_database(api):
    client, _ = api
    calls = _page_api_calls(client.get("/trading").text)
    for must in ("/api/risk/portfolio?book=PAPER", "/api/risk/portfolio?book=LIVE", "/api/risk/limits",
                 "/api/risk/exposure", "/api/risk/decisions?limit=100", "/api/risk/emergency-exit?book=PAPER"):
        assert must in calls, f"the page no longer calls {must}"
    calls += ["/api/risk/portfolio/snapshots", "/api/risk/emergency-exit/history"]
    bad = {}
    for u in calls:
        r = client.get(u)
        if r.status_code != 200 or not r.headers["content-type"].startswith("application/json"):
            bad[u] = (r.status_code, r.text[:200])
    assert bad == {}
    p = client.get("/api/risk/portfolio?book=PAPER").json()
    assert p["book"] == "PAPER" and p["exposure"]["n_positions"] == 0 and "holds nothing" in p["note"]
    em = client.get("/api/risk/emergency-exit?book=PAPER").json()
    assert (em["positions"], em["value"], em["open_orders"], em["kill_switch_on"]) == ([], 0, [], False)
    assert client.get("/api/risk/portfolio/snapshots").json() == []
    lim = client.get("/api/risk/limits").json()["limits"]
    assert lim["max_position_pct"]["source"] == "default"
    # a bad book is the caller's error, never a 500
    assert client.get("/api/risk/portfolio?book=NOPE").status_code == 400
    assert client.get("/api/risk/emergency-exit?book=NOPE").status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
#  OPS-11  rollback drill helpers (ops/rollback_drill.py)
# ═════════════════════════════════════════════════════════════════════════════

def test_isolate_renames_the_production_mutex_only_in_a_clone_that_needs_it(tmp_path):
    from ops.rollback_drill import DRILL_MUTEX, PROD_MUTEX, _isolate
    old = tmp_path / "old_release"
    old.mkdir()
    src = f'import sys\nMUTEX = "{PROD_MUTEX}"\nprint("{PROD_MUTEX}")\n'
    (old / "main.py").write_text(src, encoding="utf-8")
    assert _isolate(old) is True
    assert (old / "main.py").read_text(encoding="utf-8") == src.replace(PROD_MUTEX, DRILL_MUTEX)
    assert _isolate(old) is False, "already isolated"
    new = tmp_path / "new_release"                  # W31+: main.py honours ATIP_INSTANCE_NAME
    new.mkdir()
    shutil.copy(ROOT / "main.py", new / "main.py")
    before = (new / "main.py").read_bytes()
    assert PROD_MUTEX.encode() in before and b"ATIP_INSTANCE_NAME" in before
    assert _isolate(new) is False and (new / "main.py").read_bytes() == before


@pytest.fixture
def ops_db(temp_db, tmp_path, monkeypatch):
    """cwd = tmp_path (backups land in tmp_path/atip_data/backups), no encryption key from any
    source, the schema created, three price rows."""
    for k in list(os.environ):
        if k.startswith("ATIP__") or k in ("ATIP_ENV", "ATIP_ENCRYPTION_KEY"):
            monkeypatch.delenv(k)
    import ops.secrets as S
    monkeypatch.setattr(S, "_ENV_LOADED", {"done": True, "values": {}})
    monkeypatch.chdir(tmp_path)
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    c.executemany("INSERT INTO prices_daily (symbol, date, close) VALUES (?,?,?)",
                  [("ACME", "2026-09-01", 100.0), ("ACME", "2026-09-02", 101.5), ("BETA", "2026-09-01", 50.25)])
    c.commit()
    c.close()
    return temp_db


def _rows(path):
    import sqlite3
    c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return c.execute("SELECT symbol, date, close FROM prices_daily ORDER BY symbol, date").fetchall()
    finally:
        c.close()


def _add_gamma():
    from db.schema import get_connection
    c = get_connection()
    c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('GAMMA', '2026-09-03', 9.0)")
    c.commit()
    c.close()


SEEDED = [("ACME", "2026-09-01", 100.0), ("ACME", "2026-09-02", 101.5), ("BETA", "2026-09-01", 50.25)]


def test_db_into_without_a_verified_backup_takes_an_online_copy(ops_db, tmp_path):
    from ops.rollback_drill import _db_into
    dest = tmp_path / "clone" / "atip_data" / "atip.db"            # parent folders do not exist yet
    out = _db_into(dest)
    assert out == {"source": "online copy of the live database (no VERIFIED backup)", "verified": None}
    assert _rows(dest) == SEEDED
    _add_gamma()
    assert _rows(dest) == SEEDED and len(_rows(ops_db)) == 4, "a copy, not a link: the live DB moves on alone"


def test_db_into_restores_the_newest_verified_backup_over_an_old_clone_db(ops_db, tmp_path):
    from ops import backup as B
    from ops.rollback_drill import _db_into
    res = B.backup("manual")
    assert res["status"] == "VERIFIED"
    _add_gamma()                                                    # after the backup: must not appear
    dest = tmp_path / "clone" / "atip_data" / "atip.db"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"left over from the previous drill step")
    out = _db_into(dest)
    assert out == {"source": f"backup {res['backup_id']}", "verified": True}
    assert _rows(dest) == SEEDED, "the backup's point in time, not the live database"
    # a pruned backup, or one whose file is gone, is not used: back to an online copy
    from db.schema import get_connection
    c = get_connection()
    c.execute("UPDATE ops_backup SET pruned_at=? WHERE backup_id=?", (datetime.now(), res["backup_id"]))
    c.commit()
    c.close()
    assert _db_into(dest)["source"].startswith("online copy") and len(_rows(dest)) == 4
    c = get_connection()
    c.execute("UPDATE ops_backup SET pruned_at=NULL WHERE backup_id=?", (res["backup_id"],))
    c.commit()
    c.close()
    Path(res["path"]).unlink()
    assert _db_into(dest)["source"].startswith("online copy")
