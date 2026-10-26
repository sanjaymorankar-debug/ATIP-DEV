"""
P1-6: paper trading without Dhan quotes.

Dhan's Data API plan has been inactive since ~18 Sep 2026, so orders/paper.py refused every order
("no live quote"), and the W4 post-market cycle -- which decides after the close -- could only ever
have filled at the day's last price, the signal day's close (W4-R9). An end-of-day order now waits
for the open of the next session and fills from that session's NSE Bhavcopy bar in prices_daily.
"""

import json
from datetime import date, datetime

import pytest

from orders import paper as P

SID, VER = "atip_zpi_momentum", "1.0.0"          # a shipped library strategy (strategy_engine/library)


def _bar(conn, sym, d, o, h, lo, c):
    conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                 (sym, str(d), o, h, lo, c, 100000))
    conn.commit()


# ── the rules for one daily bar (pure) ─────────────────────────────────────────

BAR = {"open": 100.0, "high": 104.0, "low": 97.0, "close": 101.0}


def test_bar_fill_rules():
    f = P.bar_fill
    assert f("BUY", "MARKET", BAR, slippage_bps=5)[0] == 100.05
    assert f("SELL", "MARKET", BAR, slippage_bps=5)[0] == 99.95
    # LIMIT: already through at the open -> the open, never worse than the limit; reached later -> the limit
    assert f("BUY", "LIMIT", BAR, limit=101, slippage_bps=5)[0] == 100.05
    assert f("BUY", "LIMIT", BAR, limit=100.02, slippage_bps=5)[0] == 100.02
    assert f("BUY", "LIMIT", BAR, limit=98)[0] == 98
    assert f("BUY", "LIMIT", BAR, limit=96) == (None, "LIMIT")
    assert f("SELL", "LIMIT", BAR, limit=103)[0] == 103
    # SL-M: touched inside the bar -> the trigger; gapped through at the open -> the worse open
    assert f("SELL", "SL-M", BAR, trigger=98, slippage_bps=5)[0] == round(98 * 0.9995, 2)
    assert f("SELL", "SL-M", BAR, trigger=102)[0] == 100.0
    assert f("SELL", "SL-M", BAR, trigger=95) == (None, "SL-M")
    assert f("BUY", "SL-M", BAR, trigger=103)[0] == 103
    # SL that gapped beyond its limit: fills only if the bar comes back to the limit, else rests as a LIMIT
    assert f("SELL", "SL", BAR, limit=101, trigger=102)[0] == 101
    assert f("SELL", "SL", {**BAR, "high": 100.5}, limit=101, trigger=102) == (None, "LIMIT")


def test_the_session_after_an_order():
    assert P.open_cutoff(datetime(2026, 10, 27, 9, 14)) == "2026-10-26"     # can still take today's open
    assert P.open_cutoff(datetime(2026, 10, 27, 9, 15)) == "2026-10-27"
    assert P.open_cutoff(datetime(2026, 10, 26, 18, 40)) == "2026-10-26"    # the post-market cycle
    assert P.next_session("2026-10-23") == date(2026, 10, 26)               # Friday -> Monday
    assert P.next_session("2026-10-19") == date(2026, 10, 21)               # 20 Oct is Dussehra


# ── the paper broker ───────────────────────────────────────────────────────────

@pytest.fixture
def broker(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    init_db()
    monkeypatch.setattr(P.PaperBroker, "_ltp", lambda self, s: None)        # no Dhan data plan
    b = P.PaperBroker(conn=get_connection(), overrides={"paper_slippage_bps": 5.0})
    b.reset(100000.0)
    _bar(b.conn, "ACME", "2026-10-23", 99, 101, 98, 100)                     # the signal day (a Friday)
    yield b
    b.close()


def test_an_order_waits_for_the_next_sessions_bhavcopy_and_fills_at_its_open(broker):
    r = broker.place_order(transaction_type="BUY", quantity=10, symbol="ACME", fill_at="next_open",
                           fill_after="2026-10-23")
    oid = r["data"]["orderId"]
    assert r["status"] == "success" and r["data"]["orderStatus"] == "PENDING"
    row = broker.get_order_by_id(oid)["data"]
    assert (row["fill_basis"], row["fill_after"]) == ("NEXT_OPEN", "2026-10-23")
    assert broker.match_pending({"ACME": 100.0}) == [], "the live matcher never fills it, quote or not"
    assert broker.match_next_open(today="2026-10-26") == [], "Monday's Bhavcopy is not in yet"
    _bar(broker.conn, "ACME", "2026-10-26", 102.0, 104, 101, 103)
    assert broker.match_next_open(today="2026-10-26") == [
        {"order_id": oid, "symbol": "ACME", "status": "TRADED", "fill_price": 102.05, "session": "2026-10-26"}]
    row = broker.get_order_by_id(oid)["data"]
    assert (row["status"], row["fill_price"], row["fill_session"]) == ("TRADED", 102.05, "2026-10-26")
    assert broker.balance == pytest.approx(100000 - 1020.5 - row["brokerage"])
    assert broker.get_positions()["data"][0]["netQty"] == 10
    assert broker.match_next_open(today="2026-10-26") == [], "filled once"


def test_a_market_order_is_not_carried_past_its_session(broker):
    broker.place_order(transaction_type="BUY", quantity=5, symbol="ACME", fill_at="next_open", fill_after="2026-10-23")
    _bar(broker.conn, "OTHER", "2026-10-26", 50, 51, 49, 50)                 # the market traded Monday ...
    _bar(broker.conn, "ACME", "2026-10-27", 90, 91, 89, 90)                  # ... ACME only from Tuesday
    out = broker.match_next_open(today="2026-10-27")
    assert out[0]["status"] == "REJECTED" and "no bar for 2026-10-26" in out[0]["reason"]
    assert broker.get_positions()["data"] == []


def test_a_missing_bhavcopy_is_waited_for_then_the_order_lapses(broker):
    broker.place_order(transaction_type="BUY", quantity=5, symbol="ACME", fill_at="next_open", fill_after="2026-10-23")
    _bar(broker.conn, "ACME", "2026-10-27", 90, 91, 89, 90)                  # 26 Oct: a hole for the whole market
    assert broker.match_next_open(today="2026-10-27") == [], "the catch-up may still recover the hole"
    out = broker.match_next_open(today="2026-11-05")
    assert out[0]["status"] == "REJECTED" and "no Bhavcopy for 2026-10-26" in out[0]["reason"]


def test_without_a_quote_only_an_end_of_day_order_waits_for_the_open(broker):
    live = broker.place_order(transaction_type="BUY", quantity=1, symbol="ACME")
    assert live["status"] == "failure" and "no live quote" in live["remarks"]["error_message"], \
        "the default still refuses: strategy/live.py reads a resting order as 'not filled'"
    q = broker.place_order(transaction_type="BUY", quantity=1, symbol="ACME", fill_at="live_or_next_open")
    row = broker.get_order_by_id(q["data"]["orderId"])["data"]
    assert (row["status"], row["fill_basis"]) == ("PENDING", "NEXT_OPEN") and row["reason"].startswith("no live quote")
    bad = broker.place_order(transaction_type="BUY", quantity=1, symbol="NOBARS", fill_at="next_open")
    assert bad["status"] == "failure" and "no daily bars" in bad["remarks"]["error_message"]


def test_a_resting_stop_is_walked_through_the_bars_and_a_gap_fills_at_the_open(broker):
    broker.place_order(transaction_type="BUY", quantity=10, symbol="ACME", fill_at="next_open", fill_after="2026-10-23")
    _bar(broker.conn, "ACME", "2026-10-26", 100, 101, 99.5, 100.5)
    broker.match_next_open(today="2026-10-26")
    stop = broker.place_order(transaction_type="SELL", quantity=10, symbol="ACME", order_type="SL-M",
                              trigger_price=97.0, fill_at="next_open", fill_after="2026-10-26")
    sid = stop["data"]["orderId"]
    _bar(broker.conn, "ACME", "2026-10-27", 99, 100, 97.5, 98)               # not touched
    assert broker.match_next_open(today="2026-10-27") == []
    assert broker.get_order_by_id(sid)["data"]["fill_after"] == "2026-10-27"
    _bar(broker.conn, "ACME", "2026-10-28", 95, 96, 94, 95.5)                # gapped through
    out = broker.match_next_open(today="2026-10-28")
    assert (out[0]["fill_price"], out[0]["session"]) == (round(95 * 0.9995, 2), "2026-10-28")
    assert broker.get_positions()["data"] == []


# ── W4: strategy intent -> risk -> order -> fill ─────────────────────────────────

@pytest.fixture
def w4(temp_db, tmp_path, monkeypatch):
    from execution import config as XC
    from orders import risk
    from strategy_engine import lifecycle as L, registry as REG
    from tests._wealth_seed import fresh

    cfg = tmp_path / "config.json"

    def write(execution=None, limits=None):
        cfg.write_text(json.dumps({
            "execution": {"order_type": "MARKET", "max_market_data_age_sessions": None, **(execution or {})},
            "w4_risk_limits": {"max_sector_exposure_pct": None, "daily_loss_limit_pct": None,
                               "portfolio_drawdown_limit_pct": None, "strategy_drawdown_limit_pct": None,
                               **(limits or {})}}), encoding="utf-8")
    write()
    for mod in (XC, risk):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(P.PaperBroker, "_ltp", lambda self, s: None)        # no Dhan data plan
    conn, sessions = fresh(temp_db)
    now = datetime.now()
    for run_id, label in (("BT_FULL", "full"), ("BT_TEST", "test")):
        conn.execute("INSERT INTO backtest_run (run_id,kind,strategy_id,strategy_version,period_label,start_date,"
                     "end_date,initial_capital,config_json,metrics_json,status,created_at,finished_at) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, "single", SID, VER, label, "2025-01-01", "2025-12-31",
                                                     1000000.0, "{}", json.dumps({"sharpe": 1.4, "trades": 40}),
                                                     "COMPLETED", now, now))
    conn.commit()
    REG.sync_library(conn)
    for st in ("BACKTEST", "VALIDATION", "APPROVED", "PAPER"):
        L.transition(conn, SID, st, "P1-6 test")
    P.ensure_tables(conn)
    conn.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1,?,?)", (1000000.0, str(now)))
    conn.commit()
    yield {"conn": conn, "last": str(sessions[-1]), "write": write}
    conn.close()


def _close(conn, sym, d):
    return conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date=?", (sym, d)).fetchone()[0]


def _intent(conn, iid, sym, qty, last, stop=None):
    """A BUY decision and its intent, written with strategy_engine/engine.py's column lists."""
    close = _close(conn, sym, last)
    conn.execute("INSERT INTO strategy_decision (decision_id,run_id,strategy_id,version,as_of,timestamp,symbol,"
                 "decision,action,confidence,score,regime,reasons_json,parameters_json,risk_requirement,"
                 "target_position_pct,stop_price,target_price,max_hold_sessions,blocked_reason,features_json,"
                 "reason_codes_json,signal_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("D" + iid, "RUN1", SID, VER, last, datetime.now(), sym, "BUY", "ENTER", 0.7, 72.0, "BULL", "[]", "{}",
                  "STANDARD", 5.0, stop, None, 10, None, json.dumps({"close": close}), "[]", "rules"))
    conn.execute("INSERT INTO strategy_position_intent (intent_id, decision_id, strategy_id, version, as_of, symbol, "
                 "side, action, quantity, stop_price, entry_reference, book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (iid, "D" + iid, SID, VER, last, sym, "BUY", "ENTER", qty, stop, close, "PAPER"))
    conn.commit()
    return close


def _paper_order(conn, oms_id):
    return dict(conn.execute("SELECT p.* FROM paper_order p JOIN oms_order o ON o.broker_order_id=p.order_id "
                             "WHERE o.order_id=?", (oms_id,)).fetchone())


def test_the_post_market_cycle_never_fills_at_the_signal_days_close(w4):
    from execution.paper_matching import run_eod_matching
    from execution.pipeline import run_execution_cycle
    conn, last = w4["conn"], w4["last"]
    close = _intent(conn, "I1", "ACME", 10, last)
    out = run_execution_cycle(execute=True, conn=conn)                       # as the scheduler runs it, post-market
    assert out["risk"] == {"APPROVED": 1} and out["orders"][0]["status"] == "ACKNOWLEDGED", out
    o = dict(conn.execute("SELECT * FROM oms_order").fetchone())
    assert (o["filled_quantity"], o["avg_fill_price"]) == (0, None)
    assert conn.execute("SELECT COUNT(*) FROM oms_fill").fetchone()[0] == 0
    po = _paper_order(conn, o["order_id"])
    assert (po["status"], po["fill_basis"]) == ("PENDING", "NEXT_OPEN") and po["fill_after"] >= last
    # the next session's Bhavcopy arrives: the order fills at that open, never at the signal day's close
    first = P.next_session(po["fill_after"])
    _bar(conn, "ACME", first, close * 1.03, close * 1.05, close * 1.01, close * 1.04)
    r = run_eod_matching(first)
    assert (r["status"], r["rows"], r["waiting"]) == ("SUCCESS", 1, 0)
    o = dict(conn.execute("SELECT * FROM oms_order").fetchone())
    assert o["status"] == "FILLED" and o["avg_fill_price"] == round(close * 1.03 * 1.0005, 2) != round(close, 2)
    f = dict(conn.execute("SELECT * FROM oms_fill").fetchone())
    po = _paper_order(conn, o["order_id"])
    assert (f["quantity"], f["price"]) == (10, o["avg_fill_price"])
    assert f["price_source"] == f"next open, NSE Bhavcopy {first}" and f["fees"] == pytest.approx(po["brokerage"])
    assert run_eod_matching(first)["status"] == "SKIPPED"


def test_live_fill_price_takes_the_quote_only_while_the_session_is_open(w4, monkeypatch):
    from execution import order_manager as OM, risk_engine as RE
    from execution.config import execution_settings
    conn, last = w4["conn"], w4["last"]
    w4["write"]({"paper_fill_price": "live"})
    a, g = _intent(conn, "I1", "ACME", 10, last), _intent(conn, "I2", "GOLDBEES", 10, last)
    monkeypatch.setattr(P.PaperBroker, "_ltp", lambda self, s: {"ACME": a, "GOLDBEES": g}[s])   # after hours: the close
    monkeypatch.setattr(OM, "_in_market_session", lambda now=None: False)
    o1 = OM.submit_order(conn, OM.create_order(conn, RE.evaluate(conn, "I1").risk_decision_id)["order_id"])
    assert (o1["status"], o1["filled_quantity"]) == ("ACKNOWLEDGED", 0), "after the close: the next open"
    monkeypatch.setattr(OM, "_in_market_session", lambda now=None: True)
    o2 = OM.submit_order(conn, OM.create_order(conn, RE.evaluate(conn, "I2").risk_decision_id)["order_id"])
    assert (o2["status"], o2["avg_fill_price"]) == ("FILLED", round(g * 1.0005, 2))
    w4["write"]({"paper_fill_price": "reference"})                          # was: fill at the decision close
    assert execution_settings()["paper_fill_price"] == "next_open"
    w4["write"]()
    assert execution_settings()["paper_fill_price"] == "next_open"


def test_unfilled_buys_count_against_cash_and_open_positions(w4):
    from execution import order_manager as OM, risk_engine as RE
    conn, last = w4["conn"], w4["last"]
    loose = {"max_position_pct": 100.0, "max_order_value_pct": 100.0, "max_strategy_exposure_pct": None,
             "per_trade_loss_pct": None, "max_capital_allocation_pct": 95.0}
    w4["write"](limits=loose)
    conn.execute("UPDATE paper_account SET balance=100000 WHERE id=1")
    conn.commit()
    a, g = _close(conn, "ACME", last), _close(conn, "GOLDBEES", last)
    _intent(conn, "I1", "ACME", int(60000 // a), last)
    _intent(conn, "I2", "GOLDBEES", int(60000 // g), last)
    rd1 = RE.evaluate(conn, "I1")
    o1 = OM.submit_order(conn, OM.create_order(conn, rd1.risk_decision_id)["order_id"])
    assert o1["status"] == "ACKNOWLEDGED"
    committed = rd1.approved_quantity * a
    rd2 = RE.evaluate(conn, "I2", store=False)
    checks = {c["check"]: c for c in rd2.risk_checks}
    assert checks["unfilled_buys"]["value"] == pytest.approx(committed)
    assert rd2.risk_status == "APPROVED" and rd2.approved_quantity < int(60000 // g)
    assert rd2.approved_quantity * g <= (100000 - committed) * 0.95, "the cash the first order takes is not offered twice"
    w4["write"](limits={**loose, "max_open_positions": 1})
    rd2 = RE.evaluate(conn, "I2", store=False)
    assert rd2.risk_status == "REJECTED" and "max_open_positions" in rd2.rejection_reason
    w4["write"](limits={**loose, "max_strategy_exposure_pct": 25.0})
    rd2 = RE.evaluate(conn, "I2", store=False)
    assert rd2.risk_status == "REJECTED", "the first order already fills the strategy's 25%"


def test_a_next_open_entry_gets_its_protective_stop_from_its_own_session(w4):
    from execution.paper_matching import run_eod_matching
    from execution.pipeline import run_execution_cycle
    conn, last = w4["conn"], w4["last"]
    w4["write"]({"protective_stops": True})
    close = _close(conn, "ACME", last)
    stop = round(close * 0.97, 2)
    _intent(conn, "I1", "ACME", 10, last, stop=stop)
    out = run_execution_cycle(execute=True, conn=conn)
    assert [o["status"] for o in out["orders"]] == ["ACKNOWLEDGED"], "no stop for an entry that has not filled"
    entry = conn.execute("SELECT order_id FROM oms_order").fetchone()[0]
    first = P.next_session(_paper_order(conn, entry)["fill_after"])
    _bar(conn, "ACME", first, close, close * 1.01, close * 0.95, close * 0.96)   # opens flat, falls through the stop
    r = run_eod_matching(first)
    assert r["orders"][0]["status"] == "FILLED"
    assert [(s["status"], s["trigger_price"]) for s in r["protective_stops"]] == [("FILLED", stop)]
    child = dict(conn.execute("SELECT * FROM oms_order WHERE parent_order_id=?", (entry,)).fetchone())
    assert child["avg_fill_price"] == round(stop * 0.9995, 2)
    assert P.PaperBroker(conn=conn).get_positions()["data"] == []


def test_post_market_fills_next_open_orders_before_the_book_is_valued_or_decided_on(temp_db, monkeypatch):
    from db.schema import init_db
    from pipeline import scheduler as S
    init_db()
    seen = []
    monkeypatch.setattr(S, "run_job", lambda name, fn, *a, **k: seen.append(name) or {"status": "SUCCESS"})
    monkeypatch.setattr(S, "_eod_coverage", lambda td: (True, 1, 1))
    monkeypatch.setattr(S, "_eod_freshness", lambda td: (True, 0, 1))
    S.run_postmarket(force=True, target_date=date(2026, 10, 26))
    i = seen.index("paper_next_open_fills")
    assert seen.index("bhavcopy_eod") < i < seen.index("technical_indicators") < seen.index("daily_pnl")
    assert i < seen.index("strategy_decisions") < seen.index("execution_cycle")
    seen.clear()
    S.run_postmarket(force=True, target_date=date(2026, 10, 23), backfill=True)
    assert "paper_next_open_fills" not in seen, "a backfilled session never moves today's paper book"
