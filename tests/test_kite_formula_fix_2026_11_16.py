"""KGF (16 Nov 2026): the order-path fixes from the 9-13 Nov session review and the Kite breakout-retest port.

    tick rounding (orders/ticks.py)            the owner's three cases, the leg directions, Decimal not float
    regime gate (scores/breadth.py)            breadth20/50, gate open / closed / fail closed, rank-fill respects it
    stale reference (orders/autopick.py)       no LTP under 60 s -> waiting_for_ltp, nothing queued
    LIMIT entry                                min(LTP, signal close) x 1.005 tick-rounded down; signal_close, pick_ltp
    falling pick                               skipped_falling, logged once
    14:45 cutoff                               no new pick, and a queued auto-pick entry is held by the gate
    dummy GTT (orders/algo_gtt.py)             confirm_live, statuses, repeated / 20 concurrent / after restart -> one
                                               record, each blocked case, stop only after the buy
    arms (orders/arms.py)                      exits per arm, ATR, risk-budget sizing, control unchanged, time exit
    safety                                     no order-changing broker method is ever reached

Orders run orders.rules -> orders.broker -> orders.paper for real on a throwaway database (the `paper` fixture)."""

import json
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.test_session_pools import api  # noqa: F401  (pytest fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

TODAY = date.today()
YDAY = TODAY - timedelta(days=1)


def _conn():
    from db.schema import get_connection
    return get_connection()


def _do(fn, *a, **k):
    c = _conn()
    try:
        return fn(c, *a, **k)
    finally:
        c.close()


def _ltp(sym, price, age_s=1):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO live_quotes (symbol, ltp, timestamp, source) VALUES (?, ?, ?, 'test')",
              (sym, price, (datetime.now() - timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")))
    c.commit(); c.close()


def _signals(on=TODAY, ltp=True, **rows):
    c = _conn()
    for sym, (score, sig, close) in rows.items():
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES (?, ?, ?, ?)", (sym, str(on), score, sig))
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES (?, ?, ?, ?, ?, ?, 1000, 'EQ')", (sym, str(on), close, close, close, close))
    c.commit(); c.close()
    if ltp:
        for sym, (_, _, close) in rows.items():
            _ltp(sym, close)


def _sessions(*ids):
    from orders import sessions
    _do(sessions.setup_pools, 25000, list(ids), auto_pick=True)
    for sid in ids:
        _do(sessions.set_status, sid, "RUNNING")
        _do(sessions.set_mode, sid, "AUTO", ack=True)
    _do(sessions.set_auto_send, True, ack=True)


def _pick(**k):
    from orders.autopick import run
    k.setdefault("now", datetime.combine(TODAY, datetime.min.time()).replace(hour=10))
    return {e["session"]: e for e in run(**k)}


def _breadth(b20, b50, on=YDAY):
    from scores.breadth import ensure_table
    c = _conn()
    ensure_table(c)
    c.execute("INSERT OR REPLACE INTO market_breadth (date, breadth20, breadth50, n, above20, above50, computed_at) "
              "VALUES (?, ?, ?, 100, ?, ?, ?)", (str(on), b20, b50, int(b20), int(b50), datetime.now().isoformat()))
    c.commit(); c.close()


# -- KGF-04 tick rounding ----------------------------------------------------------------------------------------------

def test_the_owners_three_tick_cases():
    from orders.ticks import mul, round_down, to_tick
    assert round_down(mul(Decimal("3903.3"), Decimal("0.95")), "0.1") == Decimal("3708.1")      # SIEMENS 3708.135
    assert round_down(mul(888.00, 1.005), "0.05") == Decimal("892.40")                         # INDHOTEL 892.44
    assert round_down(159.34, "0.01") == Decimal("159.34")                                     # MOTHERSON stays
    assert to_tick(3708.135, "BUY", "ENTRY", tick="0.1") == 3708.1
    assert to_tick(3708.135, "SELL", "STOP", tick="0.1") == 3708.1                             # a long's stop: down
    assert to_tick(3708.135, "SELL", "TARGET", tick="0.1") == 3708.2                           # a long's target: up


def test_tick_rounding_is_decimal_not_float():
    from orders.ticks import band_tick, round_down, round_up, D
    assert D(0.1) + D(0.2) == Decimal("0.3")                                  # float repr in, exact decimal out
    assert round_down(1234.567, band_tick(1234.567)) == Decimal("1234.50")
    assert round_up(1234.501, "0.10") == Decimal("1234.60")
    assert [str(band_tick(p)) for p in (159, 669, 1408, 5136, 12000, 48900)] == \
        ["0.01", "0.05", "0.10", "0.50", "1.00", "5.00"]
    with pytest.raises(ValueError):
        round_down(100, "0")


def test_rules_and_legs_are_created_on_the_tick(paper):
    from orders import rules
    r = rules.create_rule({"symbol": "ACME", "side": "BUY", "trigger_type": "PRICE", "trigger_value": 892.44,
                           "reference_price": 900.0, "quantity_type": "SHARES", "quantity_value": 10,
                           "order_type": "LIMIT", "limit_price": 892.44, "bracket_target_pct": 3.0, "bracket_stop_pct": 2.0,
                           "require_confirmation": False})
    assert (r["resolved_trigger_price"], r["limit_price"]) == (892.40, 892.40)
    paper.prices["ACME"] = 890.0
    rules.check_triggers()
    legs = {l["role"]: l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"]}
    fill = rules.get_rule(r["id"])["execution_price"]
    assert fill <= 892.40
    for role, l in legs.items():
        assert (Decimal(repr(l["resolved_trigger_price"])) % Decimal("0.05")) == 0, (role, l["resolved_trigger_price"])
    assert legs["STOP"]["resolved_trigger_price"] <= round(fill * 0.98, 2) and \
        legs["TARGET"]["resolved_trigger_price"] >= round(fill * 1.03, 2)


# -- KGF-09 breadth and the regime gate --------------------------------------------------------------------------------

def test_breadth_counts_the_scored_universe_above_its_averages(temp_db):
    from db.schema import init_db
    from scores import breadth
    init_db()
    c = _conn()
    d0 = date(2026, 6, 1)
    days = [d0 + timedelta(days=i) for i in range(120) if (d0 + timedelta(days=i)).weekday() < 5]
    for sym, slope in (("UP", 1.0), ("DOWN", -1.0), ("FLAT", 0.0), ("NEW", 1.0)):
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES (?, ?, 60, 'HOLD')", (sym, str(days[-1])))
        for i, d in enumerate(days if sym != "NEW" else days[-10:]):
            px = 100 + slope * i
            c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                      "VALUES (?, ?, ?, ?, ?, ?, 1000, 'EQ')", (sym, str(d), px, px, px, px))
    c.commit()
    rows = breadth.compute(c, str(days[-1]), str(days[-1]))
    assert len(rows) == 1
    r = rows[0]
    # UP above both; DOWN and FLAT above neither; NEW has 10 closes: above its 20-day? no (too few) -- counts as not above
    assert (r["n"], r["above20"], r["above50"], r["breadth20"], r["breadth50"]) == (4, 1, 1, 25.0, 25.0)
    c.close()


def test_the_gate_opens_closes_and_fails_closed(temp_db):
    from scores.breadth import gate_state
    c = _conn()
    assert gate_state(c, None)["open"] is True                               # no gate (the control)
    g = gate_state(c, {"metric": "breadth50", "min": 50})
    assert g["open"] is False and "no breadth50 stored" in g["reason"]        # nothing stored: closed
    c.close()
    _breadth(80.7, 56.8)
    c = _conn()
    assert gate_state(c, {"metric": "breadth50", "min": 50})["open"] is True
    assert gate_state(c, {"metric": "breadth20", "min": 85})["open"] is False
    c.close()


def test_rank_fill_respects_the_gate_and_the_control_does_not(paper):
    from orders import arms
    _signals(ACME=(80, "BUY", 100.0), BETA=(62, "HOLD", 50.0))
    c = _conn()
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "SELECT symbol, ?, open, high, low, close, 50000000, 'EQ' FROM prices_daily WHERE symbol = 'BETA'",
              (str(TODAY - timedelta(days=1)),))
    c.commit(); c.close()
    _sessions("S1", "S5")
    _do(arms.set_arm, "S1", arms.ARMS["S1"])
    _do(arms.set_arm, "S5", arms.ARMS["S5"])
    _breadth(30.0, 19.3)                                                      # 15 Oct-like: gate closed
    p = _pick()
    assert p["S1"]["type"] == "WAITING" and p["S1"]["status"] == "gate_closed" and "breadth50 19.3%" in p["S1"]["reason"]
    assert p["S5"]["type"] == "PICKED"                                       # the control has no gate
    _pick()                                                                   # a second pass logs the gate once a day
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM session_events WHERE session_id = 'S1' AND event = 'AUTO_PICK_GATE'").fetchone()[0]
    c.close()
    assert n == 1
    _breadth(80.0, 56.8, on=TODAY)
    assert _pick()["S1"]["type"] == "PICKED"


# -- KGF-02 fresh LTP, LIMIT, falling ----------------------------------------------------------------------------------

def test_no_fresh_ltp_means_waiting_for_ltp_and_nothing_queued(paper):
    from orders import rules
    _signals(ltp=False, ACME=(80, "BUY", 100.0))
    _ltp("ACME", 100.0, age_s=600)                                            # 10 minutes old
    _sessions("S1")
    e = _pick()["S1"]
    assert (e["type"], e["status"]) == ("WAITING", "waiting_for_ltp")
    assert not [r for r in rules.list_rules() if r["symbol"] == "ACME"]
    c = _conn()
    assert c.execute("SELECT COUNT(*) FROM session_events WHERE event = 'AUTO_PICK_WAIT_LTP'").fetchone()[0] == 1
    c.close()


def test_a_pick_is_a_tick_rounded_limit_on_the_lower_of_ltp_and_signal_close(paper):
    from orders import rules
    _signals(ltp=False, ACME=(80, "BUY", 888.00))
    _ltp("ACME", 893.0)                                                       # above the close: the close is the base
    _sessions("S1")
    e = _pick()["S1"]
    r = rules.get_rule(e["rule_id"])
    assert (e["trigger"], r["resolved_trigger_price"], r["limit_price"], r["order_type"]) == (892.40, 892.40, 892.40, "LIMIT")
    assert (r["signal_close"], r["pick_ltp"]) == (888.0, 893.0) and r["pick_ltp_ts"]
    paper.prices["ACME"] = 893.0
    rules.check_triggers()                                                    # 893 > 892.40: nothing fires
    assert rules.get_rule(e["rule_id"])["status"] == "ACTIVE"
    paper.prices["ACME"] = 885.0
    rules.check_triggers()
    r = rules.get_rule(e["rule_id"])
    assert r["status"] == "EXECUTED" and r["execution_price"] <= 892.40     # a LIMIT never pays above its limit


def test_a_stock_falling_through_its_signal_close_is_skipped_and_logged(paper):
    _signals(ltp=False, ACME=(80, "BUY", 100.0), BETA=(70, "BUY", 50.0))
    _ltp("ACME", 97.0)                                                        # -3 %: more than 1.5 % under
    _ltp("BETA", 50.0)
    _sessions("S1")
    e = _pick()["S1"]
    assert e["symbol"] == "BETA"
    _pick()
    c = _conn()
    rows = [json.loads(r[0]) for r in c.execute("SELECT detail FROM session_events WHERE event = 'AUTO_PICK_SKIP'")]
    c.close()
    assert len(rows) == 1 and rows[0]["status"] == "skipped_falling" and rows[0]["symbol"] == "ACME"


def test_the_1445_cutoff_stops_new_picks_and_holds_a_queued_one(paper, monkeypatch):
    from orders import rules, sessions
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    late = datetime.combine(TODAY, datetime.min.time()).replace(hour=14, minute=46)
    e = _pick(now=late)["S1"]
    assert (e["type"], e["status"]) == ("WAITING", "cutoff") and "14:45" in e["reason"]
    e = _pick()["S1"]                                                         # 10:00: picked
    assert e["type"] == "PICKED"

    class Late(datetime):
        @classmethod
        def now(cls, tz=None):
            return late
    monkeypatch.setattr(sessions, "datetime", Late)
    held = _do(sessions.gate, rules.get_rule(e["rule_id"]))
    assert held and "no new swing entry after 14:45" in held


# -- KGF-11 / 12 dummy GTT ---------------------------------------------------------------------------------------------

class FakeBroker:
    """Read-only answers, and a record of every attribute asked for (the safety test reads it)."""

    def __init__(self, orders=(), positions=(), holdings=(), forever=()):
        self.data = {"get_order_list": list(orders), "get_positions": list(positions), "get_holdings": list(holdings),
                     "get_forever": list(forever)}
        self.called = []

    def __getattr__(self, item):
        if item.startswith("__") or item in ("data", "called"):
            raise AttributeError(item)
        self.called.append(item)
        if item in self.data:
            return lambda *a, **k: {"status": "success", "data": self.data[item]}
        return lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"order-changing call {item}"))


def _dummy(conn, sym="KALYANKJIL", broker=None, **k):
    from orders import algo_gtt
    k.setdefault("confirm_live", True)
    return algo_gtt.place_dummy_buy(conn, sym, k.pop("quantity", 18), k.pop("buy_price", 669.03), k.pop("stoploss", None),
                                    broker=broker, last_px=k.pop("last_px", 697.0), **k)


def test_confirm_live_missing_or_false_is_rejected(paper):
    c = _conn()
    for v in (None, False, "true", 1):
        out = _dummy(c, confirm_live=v)
        assert out["buy_status"] == "blocked" and "confirm_live=true is required" in out["message"]
    assert c.execute("SELECT COUNT(*) FROM algo_dummy_gtt").fetchone()[0] == 0
    c.close()


def test_a_dummy_buy_is_tick_rounded_and_its_stop_planned_after_the_buy(paper):
    c = _conn()
    out = _dummy(c)
    c.close()
    assert out["buy_status"] == "success" and out["buy_trigger_id"].startswith("DUMMY-")
    assert (out["buy_price"], out["stoploss"], out["tick_size"]) == (669.0, 635.55, 0.05)     # 669.03 -> 669.00, x0.95
    assert (out["stoploss_status"], out["stoploss_trigger_id"]) == ("planned_after_buy", None)
    assert out["message"] == "Dummy buy GTT created. No order sent to the broker. Stoploss planned after buy execution."
    for k in ("symbol", "quantity", "buy_price", "stoploss", "tick_size", "last_price", "account_state", "position_quantity",
              "active_buy_trigger_id", "active_stoploss_trigger_id", "day_buy_order_id", "day_buy_order_status",
              "buy_status", "buy_trigger_id", "stoploss_status", "stoploss_trigger_id", "message"):
        assert k in out


def test_repeated_concurrent_and_restarted_requests_leave_one_record(paper):
    from orders import algo_gtt
    c = _conn()
    first = _dummy(c)
    again = _dummy(c)
    c.close()
    assert again["buy_status"] == "already_active" and again["buy_trigger_id"] == first["buy_trigger_id"]
    results, errs = [], []

    def go():
        cc = _conn()
        try:
            results.append(_dummy(cc, sym="CDSL", buy_price=1408.1)["buy_status"])
        except Exception as ex:
            errs.append(ex)
        finally:
            cc.close()
    ts = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    assert results.count("success") == 1 and set(results) <= {"success", "already_active", "checking"}
    import importlib
    importlib.reload(algo_gtt)                                                # a backend restart
    c = _conn()
    assert c.execute("SELECT COUNT(*) FROM algo_dummy_gtt WHERE symbol = 'CDSL' AND status = 'ACTIVE'").fetchone()[0] == 1
    assert _dummy(c, sym="CDSL", buy_price=1408.1)["buy_status"] == "already_active"
    with pytest.raises(Exception):                                            # the partial unique index itself
        c.execute("INSERT INTO algo_dummy_gtt (id, kind, symbol, transaction_type, status, created_at, updated_at) "
                  "VALUES ('DUMMY-X', 'BUY', 'CDSL', 'BUY', 'ACTIVE', 'now', 'now')")
    c.close()


@pytest.mark.parametrize("case,broker_kw,state", [
    ("order", {"orders": [{"tradingSymbol": "TITAN", "transactionType": "BUY", "productType": "CNC",
                           "exchangeSegment": "NSE_EQ", "orderStatus": "PENDING", "orderId": "1"}]}, "open_order"),
    ("gtt", {"forever": [{"tradingSymbol": "TITAN", "orderStatus": "PENDING", "orderId": "F1"}]}, "active_gtt"),
    ("position", {"positions": [{"tradingSymbol": "TITAN", "netQty": 5}]}, "position"),
    ("holding", {"holdings": [{"tradingSymbol": "TITAN", "totalQty": 3}]}, "holding"),
])
def test_each_broker_side_case_blocks_a_new_dummy(paper, case, broker_kw, state):
    c = _conn()
    b = FakeBroker(**broker_kw)
    out = _dummy(c, sym="TITAN", buy_price=4694.4, broker=b)
    c.close()
    assert out["buy_status"] == "blocked" and out["account_state"] == state, (case, out)
    assert set(b.called) <= {"get_order_list", "get_positions", "get_holdings", "get_forever"}


def test_a_paper_position_or_a_session_holding_blocks_a_dummy(paper):
    from orders.paper import PaperBroker
    pb = PaperBroker()
    pb.conn.execute("INSERT INTO paper_position (symbol, quantity, avg_price, updated_at) VALUES ('ACME', 10, 100, 'x')")
    pb.conn.commit(); pb.close()
    c = _conn()
    out = _dummy(c, sym="ACME", buy_price=95.0)
    c.close()
    assert (out["buy_status"], out["account_state"]) == ("blocked", "position")


# -- KGF-13 arms ---------------------------------------------------------------------------------------------------------

def test_arm_exits_atr_and_risk_budget_sizing(paper):
    from orders import arms, sessions
    c = _conn()
    for i in range(16):                                                       # ATR ~ 4 on a 100 close
        d = str(TODAY - timedelta(days=30 - i))
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES ('ACME', ?, 100, 102, 98, 100, 1000, 'EQ')", (d,))
    c.commit()
    assert arms.atr_pct(c, "ACME") == 4.0
    s6 = arms.exit_params(c, arms.ARMS["S6"], "ACME")                        # 2xATR = 8 (cap 8), trail from 8 by 4
    assert (s6["stop_pct"], s6["trail_after_pct"], s6["trail_pct"]) == (8.0, 8.0, 4.0)
    s1 = arms.exit_params(c, arms.ARMS["S1"], "ACME")                        # max(3, 1.5x4=6) cap 6
    assert (s1["stop_pct"], s1["trail_after_pct"], s1["trail_pct"]) == (6.0, 4.0, 2.0)
    k1 = arms.exit_params(c, arms.ARMS["S3"], "ACME")
    assert (k1["stop_pct"], k1["target_pct"], k1["max_days"], k1["trail_pct"]) == (5.0, 10.0, 20.0, None)
    ctl = arms.exit_params(c, arms.ARMS["S5"], "ACME")                        # the control: today's rules
    assert (ctl["stop_pct"], ctl["trail_after_pct"], ctl["trail_pct"], ctl["target_after_days"]) == (2.0, 3.0, 1.0, 15.0)
    c.close()
    _sessions("S3", "S5")
    _do(arms.set_arm, "S3", arms.ARMS["S3"])
    _do(arms.set_arm, "S5", arms.ARMS["S5"])
    q3 = _do(sessions.pool_quantity, "S3", 100.0, stop_pct=5.0)              # Rs 625 / 5 % = Rs 12,500
    q5 = _do(sessions.pool_quantity, "S5", 100.0, stop_pct=2.0)              # no budget: the whole Rs 25,000
    assert q3["risk_capped"] and 120 <= q3["quantity"] <= 125 and q5["quantity"] == 250
    names = {s["id"]: s["name"] for s in _do(sessions.list_sessions)["sessions"]}
    assert (names["S3"], names["S5"]) == ("Breakouts (retest K1)", "Control")


def test_risk_budget_grows_with_the_session(paper, monkeypatch):
    """Owner 16 Nov 2026: the budget is 2.5 % of the session's cash, so a risk-budget arm compounds like the control."""
    from orders import arms, sessions
    assert arms.value_cap(arms.ARMS["S3"], 5.0, 10000.0, 25000.0) == 12500.0        # Rs 625 / 5 %, as on 16 Nov
    assert arms.value_cap(arms.ARMS["S3"], 5.0, 10000.0, 100000.0) == 50000.0       # Rs 2,500 / 5 %
    assert arms.value_cap(arms.ARMS["S3"], 5.0, 10000.0, 10000.0) == 10000.0        # never under the floor
    assert arms.value_cap({"risk_budget": 625.0}, 5.0, 0.0, 100000.0) == 12500.0    # a fixed budget stays fixed
    assert arms.value_cap(arms.ARMS["S5"], 2.0, 12000.0, 100000.0) is None          # the control: no cap
    assert "~2.5% of the session's cash at risk" in arms.describe(arms.ARMS["S3"])
    with pytest.raises(ValueError):
        arms._validate({**arms.ARMS["S3"], "risk_budget_pct": 0})
    _sessions("S3")
    _do(arms.set_arm, "S3", arms.ARMS["S3"])
    real = sessions.ledger
    monkeypatch.setattr(sessions, "ledger", lambda c, s: {**real(c, s), "cash": 100000.0, "investable_cash": 100000.0})
    q = _do(sessions.pool_quantity, "S3", 100.0, stop_pct=5.0)
    assert q["risk_capped"] and q["budget"] == 50000.0 and 490 <= q["quantity"] <= 500


def test_set_arm_refuses_outside_paper(paper, monkeypatch):
    import orders.environment as E
    from orders import arms
    monkeypatch.setattr(E, "broker_env", lambda: "LIVE")
    with pytest.raises(ValueError, match="PAPER"):
        _do(arms.set_arm, "S1", arms.ARMS["S1"])


def _scan(monkeypatch, rows):
    import types
    import sys
    mod = sys.modules.get("strategy.breakout_retest")
    fake = {"as_of": str(YDAY), "scanned": 500, "rows": rows}
    if mod is not None:
        monkeypatch.setattr(mod, "latest_scan", lambda conn: fake)
    else:
        m = types.ModuleType("strategy.breakout_retest")
        m.latest_scan = lambda conn: fake
        monkeypatch.setitem(sys.modules, "strategy.breakout_retest", m)


def test_a_retest_arm_rests_a_dummy_buy_and_the_stop_comes_only_after_the_fill(paper, monkeypatch):
    from orders import algo_gtt, arms, rules
    from orders.autopick import cancel_stale
    _scan(monkeypatch, [{"symbol": "ACME", "breakoutDate": str(YDAY), "currentPrice": 104.0, "buy_price": 100.0,
                         "stop_price": 95.0, "tick_size": 0.01}])
    _sessions("S3")
    _do(arms.set_arm, "S3", arms.ARMS["S3"])
    _breadth(80.7, 56.8)
    e = _pick()["S3"]
    assert (e["type"], e["source"], e["trigger"], e["stoploss"], e["stoploss_status"]) == \
        ("PICKED", "KITE-RETEST", 100.0, 95.0, "planned_after_buy")
    r = rules.get_rule(e["rule_id"])
    assert (r["order_type"], r["limit_price"], r["bracket_stop_pct"], r["bracket_target_pct"], r["max_hold_days"]) == \
        ("LIMIT", 100.0, 5.0, 10.0, 20.0)
    assert not [l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"]]       # no stop before the buy
    assert _do(cancel_stale, TODAY + timedelta(days=1)) == []                # a resting retest buy is not a stale pick
    paper.prices["ACME"] = 103.0
    rules.check_triggers()
    assert rules.get_rule(r["id"])["status"] == "ACTIVE"
    paper.prices["ACME"] = 99.5
    rules.check_triggers()
    r = rules.get_rule(r["id"])
    assert r["status"] == "EXECUTED" and r["execution_price"] <= 100.0
    legs = {l["role"]: l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"]}
    assert set(legs) == {"STOP", "TARGET"}
    c = _conn()
    algo_gtt.sync(c)
    recs = {d["kind"]: d for d in algo_gtt.listing(c)}
    c.close()
    assert recs["BUY"]["status"] == "TRIGGERED" and recs["OCO"]["status"] == "ACTIVE"
    assert recs["OCO"]["stop_trigger"] == legs["STOP"]["resolved_trigger_price"]


def test_the_time_exit_sells_on_the_nth_trading_day(paper, monkeypatch):
    from orders import rules
    r = rules.create_rule({"symbol": "ACME", "side": "BUY", "trigger_type": "PRICE", "trigger_value": 100.0,
                           "reference_price": 100.0, "quantity_type": "SHARES", "quantity_value": 10,
                           "bracket_stop_pct": 5.0, "bracket_target_pct": 10.0, "max_hold_days": 2,
                           "require_confirmation": False})
    paper.prices["ACME"] = 100.0
    rules.check_triggers()
    stop = next(l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"] and l["role"] == "STOP")
    c = _conn()
    c.execute("UPDATE order_rules SET created_at = ? WHERE id = ?", ((TODAY - timedelta(days=10)).isoformat(), stop["id"]))
    c.commit(); c.close()
    early = datetime.combine(TODAY, datetime.min.time()).replace(hour=11)
    assert rules.run_time_exits(now=early) == []
    out = rules.run_time_exits(now=datetime.combine(TODAY, datetime.min.time()).replace(hour=15, minute=16))
    assert out and out[0]["type"] == "TIME_EXIT"
    assert rules.get_rule(stop["id"])["status"] == "EXECUTED"
    tgt = next(l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"] and l["role"] == "TARGET")
    assert tgt["status"] == "CANCELLED"


# -- KGF-11 the endpoints, and the safety test -------------------------------------------------------------------------

def test_live_gtt_endpoint_rejects_without_confirm_live_and_never_falls_back(api, monkeypatch):
    client, headers = api
    monkeypatch.setattr("orders.algo_gtt.default_broker", lambda: None)
    monkeypatch.setattr("orders.algo_gtt.last_price", lambda conn, s: (697.0, "test"))
    base = {"symbol": "KALYANKJIL", "quantity": 18, "buy_price": 669.0}
    for body in (base, {**base, "confirm_live": False}, {**base, "confirm_live": "true"}):
        r = client.post("/api/algo-orders/live-gtt", json=body, headers=headers)
        assert r.status_code == 409 and r.json()["buy_status"] == "blocked"
    r = client.post("/api/algo-orders/live-gtt", json={**base, "confirm_live": True}, headers=headers)
    assert r.status_code == 200 and r.json()["buy_status"] == "success" and r.json()["buy_trigger_id"].startswith("DUMMY-")
    s = client.post("/api/algo-orders/gtt-state", json={"symbol": "KALYANKJIL"}, headers=headers).json()
    assert s["buy_status"] == "already_active"
    assert client.get("/api/algo-orders").json()["records"][0]["id"].startswith("DUMMY-")
    assert client.get("/ab").status_code == 200


ORDER_CHANGING = ("place_order", "modify_order", "cancel_order", "place_forever", "modify_forever", "cancel_forever",
                  "place_super_order", "modify_super_order", "cancel_super_order", "place_gtt", "modify_gtt", "delete_gtt",
                  "place_slice_order", "place_conditional_order", "modify_conditional_order", "cancel_conditional_order",
                  "exit_all_positions", "kill_switch", "convert_position")


class Recorder:
    """A Dhan / Kite client double: every method exists; the read ones answer empty, the rest record a violation."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, item):
        if item.startswith("__") or item == "calls":
            raise AttributeError(item)
        self.calls.append(item)
        return lambda *a, **k: {"status": "success", "data": []}


def test_no_order_changing_broker_method_is_ever_called(paper, monkeypatch):
    """KGF safety: the dummy-GTT paths, with confirm_live true and false, in every error path, never reach a broker
    method that places, modifies or cancels anything; the read-only wrapper refuses them outright."""
    from orders import algo_gtt, arms
    dhan, kite = Recorder(), Recorder()
    ro = algo_gtt.ReadOnlyBroker(dhan)
    for m in ORDER_CHANGING:
        with pytest.raises(PermissionError):
            getattr(ro, m)
    monkeypatch.setattr(algo_gtt, "default_broker", lambda: algo_gtt.ReadOnlyBroker(dhan))
    c = _conn()
    for conf in (True, False, None):
        _dummy(c, sym="TITAN", buy_price=4694.4, confirm_live=conf, broker=algo_gtt.ReadOnlyBroker(dhan))
        _dummy(c, sym="BAD", buy_price=-1, confirm_live=conf, broker=algo_gtt.ReadOnlyBroker(kite))       # error path
        _dummy(c, sym="NOLTP", buy_price=10, confirm_live=conf, last_px=0, broker=algo_gtt.ReadOnlyBroker(kite))
        algo_gtt.gtt_state(c, "TITAN", broker=algo_gtt.ReadOnlyBroker(dhan))
    algo_gtt.sync(c, {"TITAN": 4600.0})
    algo_gtt.maintain(c, TODAY + timedelta(days=30))
    c.close()
    _scan(monkeypatch, [{"symbol": "ACME", "breakoutDate": str(YDAY), "currentPrice": 104.0, "buy_price": 100.0,
                         "stop_price": 95.0}])
    _sessions("S3")
    _do(arms.set_arm, "S3", arms.ARMS["S3"])
    _breadth(80.0, 60.0)
    _pick()
    called = set(dhan.calls) | set(kite.calls)
    assert not called & set(ORDER_CHANGING), called
    assert called <= algo_gtt.READ_METHODS
