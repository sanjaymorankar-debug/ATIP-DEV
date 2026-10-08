"""
W39 (RK-21): the central pre-trade gateway's two gaps.

  * price band -- nothing compared an order's price with the market, in either the
    W4 risk engine or the manual path (orders/risk.py pretrade_check), so a mistyped
    LIMIT went out as typed;
  * gross exposure -- the W4 book left out the paper futures notional (W30 shorts),
    and the manual path had no leverage limit at all.
"""

import json
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def cfgfile(temp_db, tmp_path, monkeypatch):
    """One config.json for orders/risk.py and execution/config.py, the halt flag
    isolated, and a database with the paper tables in place."""
    from db.schema import init_db
    from execution import config as XC
    from orders import risk
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", cfg)
    monkeypatch.setattr(XC, "CONFIG_PATH", cfg)
    init_db()

    def write(**keys):
        cfg.write_text(json.dumps(keys), encoding="utf-8")
    write()
    return write


@pytest.fixture
def conn(cfgfile):
    from db.schema import get_connection
    from orders.paper import ensure_tables
    c = get_connection()
    ensure_tables(c)
    c.commit()
    yield c
    c.close()


def _quote(conn, sym, ltp, minutes_ago=1):
    ts = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES (?,?,?)", (sym, ltp, ts))
    conn.commit()


def _close(conn, sym, close, on=None):
    conn.execute("INSERT INTO prices_daily (symbol, date, close) VALUES (?,?,?)", (sym, str(on or date.today()), close))
    conn.commit()


def _cash(conn, balance):
    conn.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1,?,?)",
                 (balance, str(datetime.now())))
    conn.commit()


def _short(conn, underlying, lots, lot_size, price):
    conn.execute("INSERT INTO paper_futures_position (strategy_id, underlying, expiry, lots, lot_size, avg_price) "
                 "VALUES (?,?,?,?,?,?)", ("PAIRS", underlying, str(date.today() + timedelta(days=20)), -lots,
                                          lot_size, price))
    conn.commit()


def _band(r):
    return next(g for g in r["gateway"] if g["limit"] == "max_price_band_pct")


# ── defaults and config ───────────────────────────────────────────────────

def test_gateway_limits_are_on_by_default_and_null_disables(cfgfile):
    from orders.risk import gateway_limits, limits
    assert gateway_limits() == {"max_price_band_pct": 20.0, "max_gross_exposure_pct": 100.0}
    cfgfile(risk_limits={"max_price_band_pct": None, "max_gross_exposure_pct": 150})
    assert gateway_limits() == {"max_gross_exposure_pct": 150}
    assert limits() == {}, "the opt-in W1 limits are unchanged: still nothing enforced"
    cfgfile(risk_limits={"max_price_band_pct": "twenty", "max_gross_exposure_pct": 0})
    assert gateway_limits() == {"max_price_band_pct": 20.0, "max_gross_exposure_pct": 100.0}, \
        "a bad value keeps the default rather than switching the check off"


# ── price band: the manual path (orders/risk.py) ──────────────────────────

def test_manual_limit_30pct_above_ltp_is_blocked(conn):
    from orders.risk import pretrade_check
    _quote(conn, "ACME", 100.0)
    r = pretrade_check(conn, "ACME", "BUY", 10, 1300.0, env="PAPER", order_type="LIMIT", price=130.0)
    assert (r["ok"], r["blocked_by"]) == (False, "max_price_band_pct")
    assert "30.0%" in r["message"] and "live LTP" in r["message"] and "20% band" in r["message"]
    b = _band(r)
    assert b["breached"] and b["value"] == 30.0 and b["reference"] == 100.0
    # a SELL is banded too: dumping at 70 on a 100 stock walks the book down
    r = pretrade_check(conn, "ACME", "SELL", 10, 700.0, env="PAPER", order_type="LIMIT", price=70.0)
    assert (r["ok"], r["blocked_by"]) == (False, "max_price_band_pct")


def test_a_manual_order_carries_its_price_to_the_band(conn, monkeypatch):
    """orders.broker._place_order passes order_type / price, so a mistyped LIMIT is refused there too."""
    from orders import broker as B
    monkeypatch.setattr(B, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    monkeypatch.setattr(B, "available_balance", lambda: 1e7)
    _quote(conn, "ACME", 100.0)
    r = B._place_order("ACME", "BUY", 10, "LIMIT", "CNC", 130.0, confirm=False)
    assert r["status"] == "DRY_RUN_OK" and r["risk"]["ok"] is False
    assert r["risk"]["blocked_by"] == "max_price_band_pct"
    ok = B._place_order("ACME", "BUY", 10, "LIMIT", "CNC", 105.0, confirm=False)
    assert ok["risk"]["ok"] is True and _band(ok["risk"])["value"] == 5.0


def test_manual_limit_within_the_band_passes(conn):
    from orders.risk import pretrade_check
    _quote(conn, "ACME", 100.0)
    r = pretrade_check(conn, "ACME", "BUY", 10, 1100.0, env="PAPER", order_type="LIMIT", price=110.0)
    assert r["ok"] is True
    b = _band(r)
    assert b["breached"] is False and b["value"] == 10.0 and not b.get("skipped")
    # an SL-M's trigger is its price
    r = pretrade_check(conn, "ACME", "SELL", 10, 0.0, env="PAPER", order_type="SL-M", trigger_price=95.0)
    assert r["ok"] is True and _band(r)["value"] == 5.0


def test_manual_market_order_skips_the_band(conn):
    from orders.risk import pretrade_check
    _quote(conn, "ACME", 100.0)
    r = pretrade_check(conn, "ACME", "BUY", 10, 1000.0, env="PAPER", order_type="MARKET", price=500.0)
    assert r["ok"] is True
    b = _band(r)
    assert b["skipped"] is True and b["breached"] is False and "MARKET" in b["reason"]


def test_manual_no_reference_price_is_skipped_not_blocked(conn):
    from orders.risk import pretrade_check
    r = pretrade_check(conn, "NOQUOTE", "BUY", 10, 99990.0, env="PAPER", order_type="LIMIT", price=9999.0)
    assert r["ok"] is True
    b = _band(r)
    assert b["skipped"] is True and "no reference price" in b["reason"]
    assert "max_price_band_pct skipped" in r["message"]


def test_a_stale_quote_falls_back_to_the_last_close(conn):
    from orders.risk import pretrade_check
    _quote(conn, "ACME", 200.0, minutes_ago=120)         # older than BAND_QUOTE_MAX_AGE_MIN
    _close(conn, "ACME", 100.0)
    r = pretrade_check(conn, "ACME", "BUY", 1, 130.0, env="PAPER", order_type="LIMIT", price=130.0)
    assert (r["ok"], r["blocked_by"]) == (False, "max_price_band_pct")
    assert _band(r)["reference_source"].startswith("close ")


def test_a_null_band_disables_it(conn, cfgfile):
    from orders.risk import pretrade_check
    cfgfile(risk_limits={"max_price_band_pct": None})
    _quote(conn, "ACME", 100.0)
    r = pretrade_check(conn, "ACME", "BUY", 10, 1300.0, env="PAPER", order_type="LIMIT", price=130.0)
    assert r["ok"] is True and not any(g["limit"] == "max_price_band_pct" for g in r["gateway"])


# ── gross exposure: the manual path ───────────────────────────────────────

def test_manual_gross_exposure_limit_blocks_when_exceeded(conn):
    from orders.risk import pretrade_check
    _cash(conn, 100000.0)
    conn.execute("INSERT INTO paper_position (symbol, quantity, avg_price) VALUES ('HELD', 500, 90.0)")
    _close(conn, "HELD", 100.0)                           # 50,000 held -> equity 150,000
    conn.commit()
    # no futures and the default 100% cap: the funds check covers it (no risk alert for a cash shortfall)
    r = pretrade_check(conn, "ACME", "BUY", 1200, 120000.0, env="PAPER")
    g = next(g for g in r["gateway"] if g["limit"] == "max_gross_exposure_pct")
    assert r["ok"] is True and g["skipped"] is True and "funds check" in g["reason"]
    # the paper futures shorts are gross exposure: 50,000 + 30,000 + 60,000 = 93.3% of 150,000 fits ...
    _short(conn, "BIGCO", 1, 300, 100.0)
    r = pretrade_check(conn, "ACME", "BUY", 600, 60000.0, env="PAPER")
    g = next(g for g in r["gateway"] if g["limit"] == "max_gross_exposure_pct")
    assert r["ok"] is True and g["value"] == pytest.approx(93.3333, abs=1e-3) and g["equity"] == 150000.0
    # ... 50,000 + 30,000 + 90,000 does not
    r = pretrade_check(conn, "ACME", "BUY", 900, 90000.0, env="PAPER")
    assert (r["ok"], r["blocked_by"]) == (False, "max_gross_exposure_pct")
    assert "futures notional 30,000" in r["message"]
    # a SELL reduces exposure: never counted
    r = pretrade_check(conn, "HELD", "SELL", 500, 50000.0, env="PAPER")
    assert r["ok"] is True and not any(g["limit"] == "max_gross_exposure_pct" for g in r["gateway"])


def test_manual_gross_limit_is_configurable(conn, cfgfile):
    from orders.risk import pretrade_check
    _cash(conn, 100000.0)
    cfgfile(risk_limits={"max_gross_exposure_pct": 150})
    assert pretrade_check(conn, "ACME", "BUY", 1200, 120000.0, env="PAPER")["ok"] is True
    cfgfile(risk_limits={"max_gross_exposure_pct": 50})
    assert pretrade_check(conn, "ACME", "BUY", 600, 60000.0, env="PAPER")["blocked_by"] == "max_gross_exposure_pct"


def test_manual_gross_is_skipped_without_a_reliable_equity(conn):
    from orders.risk import pretrade_check
    # no paper account yet
    r = pretrade_check(conn, "ACME", "BUY", 10, 1e9, env="PAPER")
    g = next(g for g in r["gateway"] if g["limit"] == "max_gross_exposure_pct")
    assert r["ok"] is True and g["skipped"] is True and "equity unknown" in g["reason"]
    # LIVE: broker funds include collateral / margin -- skipped, never blocked
    _cash(conn, 1000.0)
    r = pretrade_check(conn, "ACME", "BUY", 10, 1e9, env="LIVE")
    g = next(g for g in r["gateway"] if g["limit"] == "max_gross_exposure_pct")
    assert r["ok"] is True and g["skipped"] is True and "LIVE" in g["reason"]


# ── the W4 risk engine ────────────────────────────────────────────────────

W4_OFF = {"max_sector_exposure_pct": None, "daily_loss_limit_pct": None, "portfolio_drawdown_limit_pct": None,
          "strategy_drawdown_limit_pct": None}


@pytest.fixture
def w4(conn, cfgfile):
    """A PAPER strategy, 10 lakh of paper cash, and a BUY intent factory. Limits that need
    history this test does not build (sector map, P&L series) are switched off."""
    conn.execute("INSERT INTO strategy (strategy_id, name, kind, status, current_version) "
                 "VALUES ('S1','gateway test','rule','PAPER','v1')")
    _cash(conn, 1000000.0)
    n = [0]

    def intent(symbol="ACME", entry=100.0, quantity=800, side="BUY", action="ENTER", order_type="LIMIT"):
        cfgfile(execution={"order_type": order_type, "max_market_data_age_sessions": None},
                w4_risk_limits=W4_OFF)
        n[0] += 1
        did, iid = f"D{n[0]}", f"I{n[0]}"
        conn.execute("INSERT INTO strategy_decision (decision_id, strategy_id, version, as_of, symbol, decision, "
                     "action, features_json) VALUES (?,?,?,?,?,?,?,?)",
                     (did, "S1", "v1", str(date.today()), symbol, side, action, "{}"))
        conn.execute("INSERT INTO strategy_position_intent (intent_id, decision_id, strategy_id, version, as_of, "
                     "symbol, side, action, quantity, stop_price, entry_reference, book) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (iid, did, "S1", "v1", str(date.today()), symbol, side, action, quantity, entry * 0.95, entry,
                      "PAPER"))
        conn.commit()
        return iid
    return intent


def _evaluate(conn, iid):
    from execution.risk_engine import evaluate
    rd = evaluate(conn, iid, store=False)
    return rd, {c["check"]: c for c in rd.risk_checks}


def test_w4_limit_30pct_above_ltp_is_rejected(conn, w4):
    _quote(conn, "ACME", 100.0)
    rd, checks = _evaluate(conn, w4(entry=130.0))        # a stale decision close, carried as the LIMIT
    assert rd.risk_status == "REJECTED"
    assert rd.rejection_reason.startswith("price band: LIMIT 130.00 is 30.0% from live LTP 100.00")
    assert checks["max_price_band_pct"]["status"] == "FAIL"
    assert (checks["max_price_band_pct"]["value"], checks["max_price_band_pct"]["limit"]) == (30.0, 20.0)


def test_w4_limit_within_the_band_is_approved(conn, w4):
    _quote(conn, "ACME", 100.0)
    rd, checks = _evaluate(conn, w4(entry=105.0))
    assert checks["max_price_band_pct"]["status"] == "PASS" and checks["max_price_band_pct"]["value"] == 5.0
    assert rd.risk_status == "APPROVED" and rd.approved_quantity == 800
    assert rd.limits["max_price_band_pct"] == 20.0, "the band used is on the decision's record"


def test_w4_market_order_skips_the_band(conn, w4):
    _quote(conn, "ACME", 100.0)
    rd, checks = _evaluate(conn, w4(entry=130.0, order_type="MARKET"))
    assert checks["max_price_band_pct"]["status"] == "SKIP" and "MARKET" in checks["max_price_band_pct"]["message"]
    assert rd.risk_status == "APPROVED"


def test_w4_no_reference_price_skips_the_band(conn, w4):
    rd, checks = _evaluate(conn, w4(symbol="NOQUOTE", entry=130.0))
    assert checks["max_price_band_pct"]["status"] == "SKIP"
    assert "no reference price" in checks["max_price_band_pct"]["message"]
    assert rd.risk_status == "APPROVED"


def test_w4_futures_notional_counts_toward_gross_exposure(conn, w4):
    rd, checks = _evaluate(conn, w4(order_type="MARKET"))
    assert rd.approved_quantity == 800 and checks["max_portfolio_exposure_pct"]["status"] == "PASS"
    # a 950,000 paper futures short leaves 50,000 of room under 100% of 10 lakh: 500 shares at 100
    _short(conn, "BIGCO", 10, 950, 100.0)
    rd, checks = _evaluate(conn, w4(symbol="ACME2", order_type="MARKET"))     # one decision per symbol a day
    assert rd.risk_status == "APPROVED" and rd.approved_quantity == 500
    c = checks["max_portfolio_exposure_pct"]
    assert c["status"] == "WARN" and "futures notional 950,000" in c["message"]


def test_w4_futures_short_leg_is_capped_by_gross_exposure(conn, w4, monkeypatch):
    from execution import futures_paper as FP
    monkeypatch.setattr(FP, "settings", lambda: {**FP.DEFAULTS, "enabled": True})
    conn.execute("INSERT INTO fo_underlying_daily (date, symbol, kind, fut_close, near_expiry, lot_size) "
                 "VALUES (?,?,?,?,?,?)", (str(date.today()), "SHRT", "STOCK", 100.0,
                                          str(date.today() + timedelta(days=20)), 500))
    conn.commit()
    _short(conn, "BIGCO", 10, 960, 100.0)                 # 960,000 of 10 lakh already used
    rd, checks = _evaluate(conn, w4(symbol="SHRT", side="SELL", action="SHORT", quantity=None))
    assert rd.risk_status == "REJECTED" and rd.rejection_reason == "no room under max_portfolio_exposure_pct"
    assert checks["max_portfolio_exposure_pct"]["status"] == "FAIL"


def test_gross_notional_marks_at_the_futures_close(conn):
    from execution.futures_paper import gross_notional
    assert gross_notional(conn) == 0.0
    _short(conn, "BIGCO", 2, 100, 50.0)
    assert gross_notional(conn) == 10000.0                # no stored contract: the entry price
    conn.execute("INSERT INTO fo_underlying_daily (date, symbol, kind, fut_close, near_expiry, lot_size) "
                 "VALUES (?,?,?,?,?,?)", (str(date.today()), "BIGCO", "STOCK", 60.0,
                                          str(date.today() + timedelta(days=20)), 100))
    conn.commit()
    assert gross_notional(conn) == 12000.0


# ── circuit limits (W39, RK-21) ──────────────────────────────────────────

def _circuit(conn, sym, ltp, upper, lower, minutes_ago=1, day=None):
    ts = ((day and datetime.combine(day, datetime.now().time())) or datetime.now()) - timedelta(minutes=minutes_ago)
    conn.execute("INSERT INTO live_quotes (symbol, ltp, timestamp, upper_circuit, lower_circuit) VALUES (?,?,?,?,?)",
                 (sym, ltp, ts.strftime("%Y-%m-%d %H:%M:%S"), upper, lower))
    conn.commit()


def _circ(r):
    return next(g for g in r["gateway"] if g["limit"] == "circuit_limit")


def test_dhan_quote_circuit_fields_are_parsed_tolerantly():
    from data.dhan import _circuit_field
    assert _circuit_field({"upper_circuit_limit": 110.5, "lower_circuit_limit": "90.4"}, "upper") == 110.5
    assert _circuit_field({"upper_circuit_limit": 110.5, "lower_circuit_limit": "90.4"}, "lower") == 90.4
    assert _circuit_field({"upperCircuitLimit": 12}, "upper") == 12.0
    assert _circuit_field({"upper_circuit_limit": 0, "lower_circuit_limit": "n/a"}, "upper") is None
    assert _circuit_field({}, "lower") is None


def test_live_quote_refresh_stores_the_circuit(conn, monkeypatch):
    import pandas as pd
    from data import dhan
    q = {"last_price": 100.0, "ohlc": {"open": 99, "high": 101, "low": 98, "close": 97},
         "upper_circuit_limit": 106.7, "lower_circuit_limit": 87.3}
    monkeypatch.setattr(dhan, "fetch_live_quotes", lambda symbols, d=None: pd.DataFrame(
        [{"symbol": "ACME", "ltp": 100.0, "prev_close": 97, "chg_pct": 3.09,
          "upper_circuit": dhan._circuit_field(q, "upper"), "lower_circuit": dhan._circuit_field(q, "lower")}]))
    monkeypatch.setattr(dhan, "HAS_DHAN", True)
    monkeypatch.setattr(dhan, "get_dhan_client", lambda: (object(), None))
    monkeypatch.setattr(dhan, "log_job", lambda *a, **k: None, raising=False)
    assert dhan.run_live_quote_refresh(["ACME"])["rows"] == 1
    from orders.risk import circuit_limits
    cl = circuit_limits(conn, "ACME")
    assert (cl["upper"], cl["lower"]) == (106.7, 87.3)


def test_a_limit_above_the_upper_circuit_is_blocked_even_inside_the_band(conn):
    from orders.risk import pretrade_check
    _circuit(conn, "ACME", 100.0, 105.0, 95.0)                        # a 5% circuit stock
    r = pretrade_check(conn, "ACME", "BUY", 10, 1080.0, env="PAPER", order_type="LIMIT", price=108.0)
    assert (r["ok"], r["blocked_by"]) == (False, "circuit_limit")
    assert _band(r)["breached"] is False, "8% is inside the 20% band: only the circuit catches it"
    c = _circ(r)
    assert c["breached"] and c["value"] == 108.0 and (c["upper"], c["lower"]) == (105.0, 95.0)
    assert "95.00 - 105.00" in r["message"]
    r = pretrade_check(conn, "ACME", "SELL", 10, 0.0, env="PAPER", order_type="SL-M", trigger_price=94.0)
    assert r["blocked_by"] == "circuit_limit" and _circ(r)["value"] == 94.0
    ok = pretrade_check(conn, "ACME", "BUY", 10, 1040.0, env="PAPER", order_type="LIMIT", price=104.0)
    assert ok["ok"] is True and _circ(ok)["breached"] is False and "circuit_limit ok" in ok["message"]


def test_circuit_is_skipped_for_market_orders_and_unknown_or_old_limits(conn, cfgfile):
    from orders.risk import pretrade_check
    _circuit(conn, "ACME", 100.0, 105.0, 95.0)
    assert _circ(pretrade_check(conn, "ACME", "BUY", 1, 100.0, env="PAPER", order_type="MARKET"))["skipped"]
    _quote(conn, "OTHER", 100.0)                                      # a websocket row: no circuit
    assert _circ(pretrade_check(conn, "OTHER", "BUY", 1, 108.0, env="PAPER", order_type="LIMIT",
                                price=108.0))["skipped"]
    _circuit(conn, "OLD", 100.0, 105.0, 95.0, day=date.today() - timedelta(days=1))
    r = pretrade_check(conn, "OLD", "BUY", 1, 108.0, env="PAPER", order_type="LIMIT", price=108.0)
    assert _circ(r)["skipped"] and r["ok"] is True, "yesterday's circuit is not today's"
    # the newest row of today that carries limits wins over a later websocket row without them
    _quote(conn, "ACME", 101.0, minutes_ago=0)
    assert pretrade_check(conn, "ACME", "BUY", 1, 108.0, env="PAPER", order_type="LIMIT",
                          price=108.0)["blocked_by"] == "circuit_limit"
    cfgfile(risk_limits={"enforce_circuit_limits": False})
    r = pretrade_check(conn, "ACME", "BUY", 1, 108.0, env="PAPER", order_type="LIMIT", price=108.0)
    assert r["ok"] is True and all(g["limit"] != "circuit_limit" for g in r["gateway"])


def test_w4_limit_outside_the_circuit_is_rejected(conn, w4):
    for s in ("CA", "CB", "CC"):                     # one intent per symbol and day
        _circuit(conn, s, 100.0, 105.0, 95.0)
    rd, checks = _evaluate(conn, w4(symbol="CA", entry=108.0))
    assert checks["max_price_band_pct"]["status"] == "PASS"
    assert rd.risk_status == "REJECTED" and checks["circuit_limit"]["status"] == "FAIL"
    assert "outside today's circuit 95.00 - 105.00" in rd.rejection_reason
    rd, checks = _evaluate(conn, w4(symbol="CB", entry=104.0))
    assert rd.risk_status == "APPROVED" and checks["circuit_limit"]["status"] == "PASS"
    rd, checks = _evaluate(conn, w4(symbol="CC", entry=108.0, order_type="MARKET"))
    assert checks["circuit_limit"]["status"] == "SKIP"
