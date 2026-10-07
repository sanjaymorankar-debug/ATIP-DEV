"""W39: first automated tests for execution / data / platform features the tracker listed with no
test reaching their key files:

    EX-09 / EX-10  execution/analytics.py          measured slippage and execution analytics
    BR-06 / RK-17  execution/broker_health.py      broker connection health and its risk gate
    MON-04         portfolio/live_pnl.py           live P&L at the newest quote, stale prices shown
    DP-14          data/macro.py                   point-in-time macro observations and revisions
    AD-01 / AD-02  altdata/framework.py, sources   alt-data framework and the news-attention source
    DP-16          data/institutional.py           NSE shareholding / insider / SAST parsing
    AD-03          quant/deal_signal.py            bulk / block deal event study
    DB-16          strategy_engine/performance.py  per-strategy book P&L, decisions, backtest
    API-03         enterprise/public_api.py        API-key access to /api/v1, scopes, limits
    DB-19 / OPS-07 publish_snapshot.py             snapshot payload and upload, no upload without config

Nothing touches the network: every fetcher / HTTP layer is replaced by a stub fed a payload built in
the test, and expected values are worked out by hand in the comments next to them.
"""

import json
import math
import sys
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def conn(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    yield c
    c.close()


@pytest.fixture
def lake_root(tmp_path, monkeypatch):
    """The data lake writes files relative to the project; keep them in the test's tmp dir."""
    from data import lake
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    return tmp_path / "lake"


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 15, 11, 0, 0)


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 15)


# ══ EX-09 / EX-10: execution analytics ═════════════════════════════════════════════════════════

def _order(conn, oid, symbol, side, qty, otype, ref, status, filled=0, broker=None, reason=None, trigger=None,
           sid="S1", created="2026-09-15 09:30:00", mode="PAPER"):
    conn.execute("INSERT INTO oms_order (order_id,intent_id,risk_decision_id,strategy_id,symbol,side,quantity,"
                 "order_type,mode,status,broker_order_id,filled_quantity,reference_price,trigger_price,reason,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, "I" + oid, "R" + oid, sid, symbol, side, qty, otype, mode, status, broker, filled, ref,
                  trigger, reason, created))


def _fill(conn, fid, oid, symbol, side, qty, price, at, fees=0.0, sid="S1", mode="PAPER"):
    conn.execute("INSERT INTO oms_fill (fill_id,order_id,strategy_id,symbol,side,quantity,price,fees,mode,filled_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (fid, oid, sid, symbol, side, qty, price, fees, mode, at))


def _events(conn, oid, *pairs):
    for status, at in pairs:
        conn.execute("INSERT INTO oms_order_event (order_id,to_status,at) VALUES (?,?,?)", (oid, status, at))


@pytest.fixture
def oms(conn):
    # O1 BUY 10 INFY, decision close 100, filled at 100.50          -> +50 bps adverse, cost +5.00
    _order(conn, "O1", "INFY", "BUY", 10, "MARKET", 100.0, "FILLED", 10, "B1", sid="S1")
    _fill(conn, "F1", "O1", "INFY", "BUY", 10, 100.5, "2026-09-15 09:30:02", fees=2.0, sid="S1")
    _events(conn, "O1", ("SUBMITTED", "2026-09-15 09:30:00"), ("FILLED", "2026-09-15 09:30:02"))
    # O2 SELL 4 TCS, reference 200, received 199 (less on a sell)     -> +50 bps adverse, cost +4.00
    _order(conn, "O2", "TCS", "SELL", 4, "LIMIT", 200.0, "FILLED", 4, "B2", sid="S2")
    _fill(conn, "F2", "O2", "TCS", "SELL", 4, 199.0, "2026-09-15 09:31:10", fees=1.0, sid="S2")
    _events(conn, "O2", ("SUBMITTED", "2026-09-15 09:31:00"), ("FILLED", "2026-09-15 09:31:10"))
    # O3 SL-M SELL 10 WIPRO: decision close 55 but the stop's trigger is 50 -- the benchmark is the
    # trigger: -1 * (49.5 - 50) / 50 * 1e4 = +100 bps (vs the reference it would read +1000 bps)
    _order(conn, "O3", "WIPRO", "SELL", 10, "SL-M", 55.0, "FILLED", 10, "B3", trigger=50.0, sid="S2")
    _fill(conn, "F3", "O3", "WIPRO", "SELL", 10, 49.5, "2026-09-15 10:05:00", fees=0.5, sid="S2")
    _events(conn, "O3", ("SUBMITTED", "2026-09-15 10:00:00"), ("FILLED", "2026-09-15 10:05:00"))
    # O4 BUY 10 HDFC partly filled: 6 at 999 vs 1000 -> -10 bps (favourable), cost -6.00; never FILLED
    _order(conn, "O4", "HDFC", "BUY", 10, "MARKET", 1000.0, "PARTIALLY_FILLED", 6, "B4", sid="S1")
    _fill(conn, "F4", "O4", "HDFC", "BUY", 6, 999.0, "2026-09-15 11:00:00", fees=0.6, sid="S1")
    _events(conn, "O4", ("SUBMITTED", "2026-09-15 10:59:00"), ("PARTIALLY_FILLED", "2026-09-15 11:00:00"))
    # rejected before the broker / rejected by the broker / cancelled / never sent
    _order(conn, "O5", "X", "BUY", 5, "MARKET", 10.0, "REJECTED", reason="insufficient funds", sid="S3")
    _order(conn, "O6", "Y", "BUY", 5, "MARKET", 10.0, "REJECTED", broker="B6", reason="RMS: price band", sid="S3")
    _order(conn, "O7", "Z", "BUY", 5, "LIMIT", 10.0, "CANCELLED", broker="B7", sid="S3")
    _order(conn, "O8", "Q", "SELL", 5, "MARKET", 10.0, "CREATED", sid="S3")
    # outside the period: must not count anywhere
    _order(conn, "O9", "INFY", "BUY", 1, "MARKET", 100.0, "FILLED", 1, "B9", created="2026-09-01 09:30:00")
    _fill(conn, "F9", "O9", "INFY", "BUY", 1, 150.0, "2026-09-01 09:30:01")
    conn.commit()
    return conn


def test_slippage_sign_convention_for_buys_sells_and_stop_benchmarks(oms):
    from execution.analytics import slippage
    rows = {r["fill_id"]: r for r in slippage(oms, "2026-09-15", "2026-09-15")}
    assert set(rows) == {"F1", "F2", "F3", "F4"}                       # F9 is outside the period
    assert (rows["F1"]["slippage_bps"], rows["F1"]["slippage_cost_rs"]) == (50.0, 5.0)      # paid more on a BUY
    assert (rows["F2"]["slippage_bps"], rows["F2"]["slippage_cost_rs"]) == (50.0, 4.0)      # got less on a SELL
    assert rows["F3"]["benchmark"] == "trigger"
    assert (rows["F3"]["slippage_bps"], rows["F3"]["slippage_cost_rs"]) == (100.0, 5.0)
    assert rows["F1"]["benchmark"] == "reference"
    assert (rows["F4"]["slippage_bps"], rows["F4"]["slippage_cost_rs"]) == (-10.0, -6.0)    # favourable is negative
    assert {r["fill_id"] for r in slippage(oms, "2026-09-15", "2026-09-15", strategy_id="S2")} == {"F2", "F3"}


def test_slippage_without_a_reference_price_is_not_invented(conn):
    from execution.analytics import slippage
    _order(conn, "N1", "INFY", "BUY", 1, "MARKET", None, "FILLED", 1, "B")
    _fill(conn, "NF1", "N1", "INFY", "BUY", 1, 101.0, "2026-09-15 10:00:00")
    conn.commit()
    r = slippage(conn, "2026-09-15", "2026-09-15")[0]
    assert r["slippage_bps"] is None and r["slippage_cost_rs"] is None


def test_execution_analytics_summary_matches_a_hand_count(oms):
    from execution.analytics import analytics
    a = analytics(oms, "2026-09-15", "2026-09-15")
    o = a["overall"]
    assert o["orders"] == 8
    # reached the broker: O1-O4, O6 (rejected WITH a broker id), O7; not O5 (no broker id) nor O8 (CREATED)
    assert o["reached_broker"] == 6 and o["filled_orders"] == 4
    assert o["fill_rate_qty"] == round(30 / 54, 4)                   # 10+4+10+6 filled of 10+4+10+10+5*4 ordered
    assert o["fill_rate_orders"] == round(4 / 6, 4)
    assert (o["rejected"], o["rejection_rate"], o["cancelled"], o["failed"]) == (2, 0.25, 1, 0)
    assert {(x["reason"], x["count"]) for x in o["rejection_reasons"]} == {("insufficient funds", 1),
                                                                           ("RMS: price band", 1)}
    # bps [50, 50, 100, -10]: mean 47.5; sorted [-10, 50, 50, 100] -> median 50, p90 = 50 + 0.7 * 50 = 85
    assert o["slippage_bps"] == {"mean": 47.5, "median": 50.0, "p90": 85.0, "worst": 100.0, "n": 4}
    assert o["slippage_cost_rs"] == 8.0 and o["fees_rs"] == 4.1
    # SUBMITTED -> FILLED: 2 s, 10 s, 300 s (O4 never reached FILLED) -> median 10, p90 = 10 + 0.8 * 290
    assert o["time_to_fill_s"] == {"median": 10.0, "p90": 242.0, "n": 3}
    s1 = a["by_strategy"]["S1"]
    assert s1["orders"] == 2 and s1["slippage_bps"]["mean"] == 20.0 and s1["slippage_cost_rs"] == -1.0
    assert a["by_order_type"]["SL-M"]["slippage_bps"]["worst"] == 100.0
    assert a["by_day"] == {"2026-09-15": {"orders": 8, "filled": 3, "rejected": 2}}
    assert a["note"] is None and a["period"] == "2026-09-15..2026-09-15"


# ══ BR-06 / RK-17: broker connection health ════════════════════════════════════════════════════

IN_SESSION = datetime(2026, 9, 15, 11, 0)         # a Tuesday NSE session
HOLIDAY = datetime(2026, 10, 2, 11, 0)            # Gandhi Jayanti: NSE closed


@pytest.fixture
def bh(conn, monkeypatch):
    """broker_health with its process-local probes (Dhan, WebSocket feeds, Kite token) stubbed;
    quote freshness and the paper-book check run for real against the test database."""
    from execution import broker_health
    from orders.paper import ensure_tables
    ensure_tables(conn)
    probes = {"dhan": [{"check": "dhan_market_data", "status": "OK", "detail": ""},
                       {"check": "dhan_token", "status": "OK", "detail": ""}]}
    monkeypatch.setattr(broker_health, "_dhan_checks", lambda network: [dict(c) for c in probes["dhan"]])
    monkeypatch.setattr(broker_health, "_feeds", lambda now, in_s: [
        {"check": "index_feed", "status": "OK" if in_s else "SKIPPED", "detail": ""},
        {"check": "stock_feed", "status": "SKIPPED", "detail": "stock feed not enabled"}])
    monkeypatch.setattr(broker_health, "_zerodha", lambda: {"check": "zerodha_token", "status": "SKIPPED", "detail": ""})
    sent = []
    import alerts.telegram as tg
    monkeypatch.setattr(tg, "notify", lambda msg, **kw: sent.append((msg, kw)))
    return broker_health, probes, sent


def _quote(conn, sym, ts, ltp=100.0):
    conn.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES (?,?,?)", (sym, ltp, ts))
    conn.commit()


def test_broker_health_overall_is_the_worst_check_and_stale_quotes_read_stale(conn, bh):
    broker_health, probes, _ = bh
    # in the session with no quote at all -> STALE
    r = broker_health.check(network=False, now=IN_SESSION, notify=False)
    fresh = next(c for c in r["checks"] if c["check"] == "quote_freshness")
    assert r["in_session"] is True and fresh["status"] == "STALE" and r["overall"] == "STALE"
    # newest quote 10 minutes old -> OK; the paper book is readable -> OK; skipped checks never count
    _quote(conn, "INFY", "2026-09-15 10:50:00")
    r = broker_health.check(network=False, now=IN_SESSION, notify=False)
    assert r["overall"] == "OK"
    assert next(c for c in r["checks"] if c["check"] == "quote_freshness")["age_minutes"] == 10.0
    assert next(c for c in r["checks"] if c["check"] == "paper_broker")["status"] == "OK"
    # 25 minutes later the same quote is 35 minutes old (> 20) -> STALE
    r = broker_health.check(network=False, now=IN_SESSION + timedelta(minutes=25), notify=False)
    assert r["overall"] == "STALE"
    # a DEGRADED probe is worse than OK, a DOWN probe worse than STALE
    probes["dhan"][1]["status"] = "DEGRADED"
    assert broker_health.check(network=False, now=IN_SESSION, notify=False)["overall"] == "DEGRADED"
    probes["dhan"][0]["status"] = "DOWN"
    assert broker_health.check(network=False, now=IN_SESSION + timedelta(minutes=25), notify=False)["overall"] == "DOWN"
    # on a holiday freshness is SKIPPED, so the stale quote does not count
    probes["dhan"][:] = [{"check": "dhan_market_data", "status": "SKIPPED", "detail": ""}]
    r = broker_health.check(network=False, now=HOLIDAY, notify=False)
    assert r["in_session"] is False and r["overall"] == "OK"
    assert conn.execute("SELECT COUNT(*) FROM broker_health_check").fetchone()[0] == 6
    last = broker_health.latest(conn)
    assert last["overall"] == "OK" and last["in_session"] is False and len(last["checks"]) == len(r["checks"])


def test_dhan_probe_classifies_token_and_quote_failures(monkeypatch):
    import pandas as pd
    import data.dhan as dhan_mod
    from execution.broker_health import _dhan_checks

    class Client:
        def __init__(self, resp):
            self.resp = resp

        def get_fund_limits(self):
            if isinstance(self.resp, Exception):
                raise self.resp
            return self.resp

    state = {}
    monkeypatch.setattr(dhan_mod, "get_dhan_client", lambda: (state["client"], None))
    monkeypatch.setattr(dhan_mod, "fetch_index_quotes", lambda keys, dhan: state["df"])

    def run(df, resp):
        state.update(df=df, client=Client(resp))
        return {c["check"]: c["status"] for c in _dhan_checks(True)}

    quote = pd.DataFrame([{"index": "NIFTY50", "ltp": 25000.0}])
    assert run(quote, {"status": "success", "data": {"availabelBalance": 1}}) == \
        {"dhan_market_data": "OK", "dhan_token": "OK"}
    assert run(pd.DataFrame(), {"status": "failure", "remarks": {"error_code": "DH-901",
                                                                  "error_message": "Invalid Token"}}) == \
        {"dhan_market_data": "DOWN", "dhan_token": "DOWN"}
    assert run(quote, {"status": "failure", "remarks": "too many requests"})["dhan_token"] == "DEGRADED"
    assert run(quote, TimeoutError("read timed out"))["dhan_token"] == "DEGRADED"

    def no_client():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(dhan_mod, "get_dhan_client", no_client)
    assert {c["status"] for c in _dhan_checks(True)} == {"DOWN"}
    assert {c["status"] for c in _dhan_checks(False)} == {"SKIPPED"}


def test_broker_health_alerts_on_going_down_and_on_recovery_only(conn, bh):
    broker_health, probes, sent = bh
    _quote(conn, "INFY", "2026-09-15 10:59:00")
    broker_health.check(network=False, now=IN_SESSION)                              # first check: no previous
    probes["dhan"][0]["status"] = "DOWN"
    broker_health.check(network=False, now=IN_SESSION + timedelta(minutes=1))        # OK -> DOWN: alert
    broker_health.check(network=False, now=IN_SESSION + timedelta(minutes=2))        # DOWN -> DOWN: silent
    probes["dhan"][0]["status"] = "OK"
    broker_health.check(network=False, now=IN_SESSION + timedelta(minutes=3))        # DOWN -> OK: recovery
    assert len(sent) == 2
    assert "OK → DOWN" in sent[0][0] and "dhan_market_data: DOWN" in sent[0][0]
    assert sent[0][1]["severity"] == "warning" and sent[0][1]["category"] == "broker"
    assert "DOWN → OK" in sent[1][0] and sent[1][1]["severity"] == "info"


def test_broker_health_gate_blocks_buys_only_on_a_recent_down_or_stale_check(conn, tmp_path, monkeypatch):
    from execution import config as XC
    from execution.broker_health import gate
    monkeypatch.setattr(XC, "CONFIG_PATH", tmp_path / "no_config.json")     # code defaults: gate on, 30 min

    def record(overall, minutes_ago, checks):
        conn.execute("DELETE FROM broker_health_check")
        conn.execute("INSERT INTO broker_health_check (checked_at,overall,in_session,checks_json) VALUES (?,?,?,?)",
                     (datetime.now() - timedelta(minutes=minutes_ago), overall, 1, json.dumps(checks)))
        conn.commit()

    assert gate(conn, "BUY")[0] == "SKIP"                                    # no check yet: never block on nothing
    record("DOWN", 5, [{"check": "dhan_token", "status": "DOWN"}, {"check": "index_feed", "status": "OK"}])
    st, msg = gate(conn, "BUY")
    assert st == "FAIL" and "dhan_token" in msg and "index_feed" not in msg
    assert gate(conn, "SELL")[0] == "SKIP"                                   # exits are never blocked
    record("STALE", 5, [{"check": "quote_freshness", "status": "STALE"}])
    assert gate(conn, "BUY")[0] == "FAIL"
    record("DEGRADED", 5, [{"check": "stock_feed", "status": "DEGRADED"}])
    assert gate(conn, "BUY")[0] == "PASS"
    record("DOWN", 45, [{"check": "dhan_token", "status": "DOWN"}])         # older than 30 min: not trusted
    assert gate(conn, "BUY")[0] == "SKIP"
    record("DOWN", 5, [{"check": "dhan_token", "status": "DOWN"}])
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"execution": {"block_on_broker_health": False}}), encoding="utf-8")
    monkeypatch.setattr(XC, "CONFIG_PATH", cfg)
    assert gate(conn, "BUY")[0] == "SKIP"


# ══ MON-04: live P&L ═══════════════════════════════════════════════════════════════════════════

def test_live_pnl_values_books_at_the_newest_quote_and_flags_stale_prices(conn, monkeypatch):
    from orders.paper import ensure_tables
    from portfolio import live_pnl
    monkeypatch.setattr(live_pnl, "datetime", _FrozenDateTime)        # now = 2026-09-15 11:00
    monkeypatch.setattr(live_pnl, "date", _FrozenDate)
    ensure_tables(conn)
    for sym, d, c in (("INFY", "2026-09-11", 1500.0), ("TCS", "2026-09-11", 3000.0), ("WIPRO", "2026-09-10", 400.0),
                      ("WIPRO", "2026-09-11", 410.0)):
        conn.execute("INSERT INTO prices_daily (symbol,date,close) VALUES (?,?,?)", (sym, d, c))
    _quote(conn, "INFY", "2026-09-15 10:00:00", 1490.0)
    _quote(conn, "INFY", "2026-09-15 10:50:00", 1510.0)      # newest INFY: 10 minutes old
    _quote(conn, "TCS", "2026-09-15 10:15:00", 3030.0)       # 45 minutes old: used, but flagged stale
    _quote(conn, "WIPRO", "2026-09-11 15:00:00", 999.0)      # not today: ignored -> last close
    # LIVE holdings: only the latest synced date counts, and qty 0 rows do not
    for d, sym, q, a in (("2026-09-14", "INFY", 10, 1400.0), ("2026-09-14", "TCS", 5, 3100.0),
                         ("2026-09-14", "HDFC", 0, 900.0), ("2026-09-01", "WIPRO", 100, 300.0)):
        conn.execute("INSERT INTO portfolio_holdings (date,symbol,qty,avg_price) VALUES (?,?,?,?)", (d, sym, q, a))
    conn.execute("INSERT INTO paper_position (symbol,quantity,avg_price,realized_pnl) VALUES ('WIPRO',20,400,55.5)")
    conn.execute("INSERT INTO paper_position (symbol,quantity,avg_price,realized_pnl) VALUES ('INFY',0,0,100)")
    conn.execute("INSERT INTO paper_account (id,balance) VALUES (1, 98765.43)")
    # strategy S1: BUY 10 @1500, BUY 10 @1540 (avg 1520), SELL 5 -> 15 open at 1520; S2 round trip is flat
    for i, (sid, sym, side, q, px) in enumerate((("S1", "INFY", "BUY", 10, 1500.0), ("S1", "INFY", "BUY", 10, 1540.0),
                                                 ("S1", "INFY", "SELL", 5, 1530.0), ("S2", "TCS", "BUY", 2, 2950.0),
                                                 ("S2", "TCS", "SELL", 2, 3000.0))):
        _fill(conn, f"LF{i}", f"LO{i}", sym, side, q, px, f"2026-09-15 09:{30 + i}:00", sid=sid)
    conn.commit()

    p = live_pnl.live_pnl(conn)
    rows = {r["symbol"]: r for r in p["LIVE"]["rows"]}
    assert set(rows) == {"INFY", "TCS"}
    # INFY 10 @1400 at 1510 vs prev close 1500: value 15100, unrealised 1100, day 100, day% 0.67
    assert (rows["INFY"]["value"], rows["INFY"]["unrealized"], rows["INFY"]["day_pnl"], rows["INFY"]["day_pct"]) == \
        (15100.0, 1100.0, 100.0, 0.67)
    assert rows["INFY"]["price_source"] == "live" and rows["INFY"]["quote_age_min"] == 10.0
    # TCS 5 @3100 at 3030 vs 3000: value 15150, unrealised -350, day 150, day% 1.0
    assert (rows["TCS"]["value"], rows["TCS"]["unrealized"], rows["TCS"]["day_pnl"], rows["TCS"]["day_pct"]) == \
        (15150.0, -350.0, 150.0, 1.0)
    assert rows["TCS"]["quote_age_min"] == 45.0
    assert {k: p["LIVE"][k] for k in ("positions", "value", "unrealized", "day_pnl")} == \
        {"positions": 2, "value": 30250.0, "unrealized": 750.0, "day_pnl": 250.0}
    # PAPER: WIPRO has no quote today -> the last close (410), labelled; 20 @400 -> value 8200, unrealised 200
    (w,) = p["PAPER"]["rows"]
    assert (w["symbol"], w["price"], w["price_source"], w["value"], w["unrealized"]) == \
        ("WIPRO", 410.0, "close 2026-09-11", 8200.0, 200.0)
    assert p["PAPER"]["realized_to_date"] == 155.5 and p["PAPER"]["cash"] == 98765.43
    # strategies: only S1 is open -- 15 INFY at avg 1520 marked 1510: value 22650, unrealised -150, day +150
    assert list(p["strategies"]) == ["S1"]
    (s,) = p["strategies"]["S1"]["rows"]
    assert (s["qty"], s["avg"], s["value"], s["unrealized"], s["day_pnl"]) == (15, 1520.0, 22650.0, -150.0, 150.0)
    assert p["stale_prices"] == ["TCS", "WIPRO"]


# ══ DP-14: macro data ══════════════════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, text=None, js=None):
        self.text, self._js = text, js

    def json(self):
        return self._js


def test_fred_and_world_bank_parsers_on_recorded_looking_payloads(monkeypatch):
    from data import macro
    urls = []
    csv = "observation_date,INDCPIALLMINMEI\n2026-05-01,188.0\n2026-06-01,.\n2026-07-01,190.5\n"
    monkeypatch.setattr(macro, "_http_get", lambda url: urls.append(url) or _Resp(text=csv))
    assert macro.fetch_fred("INDCPIALLMINMEI") == [(date(2026, 5, 1), 188.0), (date(2026, 7, 1), 190.5)]
    assert urls[-1] == "https://fred.stlouisfed.org/graph/fredgraph.csv?id=INDCPIALLMINMEI"
    wb = [{"page": 1, "pages": 1}, [{"date": "2025", "value": 6.5}, {"date": "2024", "value": None},
                                    {"date": "2023", "value": 8.2}, {"date": "", "value": 1.0}]]
    monkeypatch.setattr(macro, "_http_get", lambda url: urls.append(url) or _Resp(js=wb))
    assert macro.fetch_worldbank("NY.GDP.MKTP.KD.ZG") == [(date(2023, 1, 1), 8.2), (date(2025, 1, 1), 6.5)]
    assert "/country/IN/indicator/NY.GDP.MKTP.KD.ZG" in urls[-1]


def test_macro_observations_are_point_in_time_by_release_lag_and_revisions_are_flagged(conn):
    from data import macro
    macro.seed(conn)
    # IN_CPI_INDEX: monthly, 20-day lag -> June 2026 is visible from 30 Jun + 20 = 20 Jul, July from 20 Aug
    r = macro.store_series(conn, "IN_CPI_INDEX", [(date(2026, 6, 1), 190.5), (date(2026, 7, 1), 191.0)])
    assert (r["new"], r["revised"]) == (2, 0)
    assert [o["period"] for o in macro.point_in_time(conn, "IN_CPI_INDEX", "2026-07-19")] == []
    assert [str(o["available_from"]) for o in macro.point_in_time(conn, "IN_CPI_INDEX", "2026-07-20")] == ["2026-07-20"]
    assert len(macro.point_in_time(conn, "IN_CPI_INDEX", "2026-08-19")) == 1
    assert [o["value"] for o in macro.point_in_time(conn, "IN_CPI_INDEX", "2026-08-20")] == [190.5, 191.0]
    # annual World Bank series, 200-day lag: 2025 is known from 31 Dec 2025 + 200 days = 19 Jul 2026
    macro.store_series(conn, "IN_GDP_GROWTH", [(date(2025, 1, 1), 6.5)])
    assert str(macro.point_in_time(conn, "IN_GDP_GROWTH", "2026-07-19")[0]["available_from"]) == "2026-07-19"
    assert macro.point_in_time(conn, "IN_GDP_GROWTH", "2026-07-18") == []
    # daily series, 2-day lag
    macro.store_series(conn, "USD_INR", [(date(2026, 9, 11), 88.1)])
    assert str(macro.point_in_time(conn, "USD_INR", "2026-09-13")[0]["available_from"]) == "2026-09-13"
    # a re-fetch with June revised: replaced and flagged, availability unchanged; an unchanged value is not a revision
    r = macro.store_series(conn, "IN_CPI_INDEX", [(date(2026, 6, 1), 190.7), (date(2026, 7, 1), 191.0)])
    assert (r["new"], r["revised"]) == (0, 1)
    june = macro.point_in_time(conn, "IN_CPI_INDEX", "2026-08-20")[0]
    assert (june["value"], june["revised"], str(june["available_from"])) == (190.7, 1, "2026-07-20")
    assert macro.point_in_time(conn, "IN_CPI_INDEX", "2026-08-20")[1]["revised"] == 0
    snap = {s["series_id"]: s for s in macro.latest_snapshot(conn, "2026-08-20")}
    assert (snap["IN_CPI_INDEX"]["value"], snap["IN_CPI_INDEX"]["previous"]) == (191.0, 190.7)


def test_run_macro_stores_events_vintages_and_marks_a_failed_series(conn, monkeypatch, lake_root):
    from data import macro
    data = {"INDCPIALLMINMEI": [(date(2026, 5, 1), 188.0), (date(2026, 6, 1), 190.5), (date(2026, 7, 1), 190.5)]}

    def fred(code):
        if code == "CPIAUCSL":
            raise ConnectionError("FRED unreachable")
        return data.get(code, [])
    monkeypatch.setattr(macro, "fetch_fred", fred)
    monkeypatch.setattr(macro, "fetch_worldbank", lambda code, country="IN": [])
    r = macro.run_macro(conn)
    assert r["status"] == "PARTIAL" and r["failed"] == ["US_CPI"] and r["rows"] == 3 and r["events"] == 3
    st, err = conn.execute("SELECT status, error FROM macro_series WHERE series_id='US_CPI'").fetchone()
    assert st == "ERROR" and "FRED unreachable" in err
    ev = {row[0]: row[1:] for row in conn.execute(
        "SELECT event_id, direction, known_at FROM market_event WHERE event_type='MACRO' ORDER BY event_id")}
    # direction is the change vs the previous period; known when the observation became available
    assert ev["MCIN_CPI_INDEX2026-05-01"][0] is None
    assert (ev["MCIN_CPI_INDEX2026-06-01"][0], str(ev["MCIN_CPI_INDEX2026-06-01"][1])[:10]) == ("UP", "2026-07-20")
    assert (ev["MCIN_CPI_INDEX2026-07-01"][0], str(ev["MCIN_CPI_INDEX2026-07-01"][1])[:10]) == ("FLAT", "2026-08-20")
    assert conn.execute("SELECT COUNT(*) FROM lake_partition WHERE dataset='macro_vintages'").fetchone()[0] == 1
    assert list(lake_root.glob("macro_vintages/date=*/*"))


# ══ AD-01 / AD-02: alt-data framework and sources ═════════════════════════════════════════════

def _test_source(framework, observations, entities=("AAA", "BBB", "CCC", "DDD", "EEE"), error=None):
    class TinySource(framework.DataSource):
        source_id = "w39_tiny"
        name = "W39 tiny"
        description = "test-only source"
        metrics = ("value",)

        def entities(self, conn):
            return list(entities)

        def fetch(self, conn, as_of, ents):
            if error:
                raise error
            return list(observations)
    return TinySource


def test_alt_source_runs_point_in_time_with_health_and_coverage(conn, monkeypatch, lake_root):
    from altdata import framework as F
    O = F.Observation
    obs = [O("AAA", date(2026, 9, 14), "value", 1.0, datetime(2026, 9, 14, 18)),
           O("AAA", date(2026, 9, 15), "value", 2.0, datetime(2026, 9, 15, 18)),
           O("BBB", date(2026, 9, 15), "value", 4.0, datetime(2026, 9, 15, 18)),
           O("CCC", date(2026, 9, 15), "value", 6.0, datetime(2026, 9, 16, 9)),     # published next morning
           O("DDD", date(2026, 9, 15), "value", 9.0, None),                         # no availability: dropped
           O("AAA", date(2026, 9, 15), "bogus", 1.0, datetime(2026, 9, 15, 18))]    # unknown metric: dropped
    monkeypatch.setitem(F.REGISTRY, "w39_tiny", None)          # removed again at teardown
    F.register(_test_source(F, obs))
    r = F.run_source(conn, "w39_tiny", "2026-09-15")
    assert r == {"source_id": "w39_tiny", "rows": 4, "entities": 5, "covered": 3, "coverage": 0.6}
    h = conn.execute("SELECT last_status, last_rows, coverage, stale_days, error FROM alt_dataset "
                     "WHERE source_id='w39_tiny'").fetchone()
    assert tuple(h[:3]) == ("SUCCESS", 4, 0.6) and h[3] == (date.today() - date(2026, 9, 15)).days
    assert h[4] == "2 observations without availability / unknown metric"
    assert conn.execute("SELECT COUNT(*) FROM lake_partition WHERE dataset='alt_w39_tiny'").fetchone()[0] == 1

    def ents(**kw):
        return sorted((x["entity"], str(x["date"])) for x in F.read(conn, "w39_tiny", "value", **kw))
    # a date means "known by the end of that day": CCC's value only appears from 16 Sep
    assert ents(as_of="2026-09-15") == [("AAA", "2026-09-14"), ("AAA", "2026-09-15"), ("BBB", "2026-09-15")]
    assert ents(as_of="2026-09-15 12:00:00") == [("AAA", "2026-09-14")]
    assert len(ents(as_of="2026-09-16")) == 4
    assert ents(entity="aaa", start="2026-09-15", as_of="2026-09-16") == [("AAA", "2026-09-15")]
    # cross-sectional z-score needs 3 names: none by 15 Sep, by 16 Sep (2, 4, 6) -> mean 4, pstdev sqrt(8/3)
    assert F.zscore(conn, "w39_tiny", "value", "2026-09-15", as_of="2026-09-15") == {}
    z = F.zscore(conn, "w39_tiny", "value", "2026-09-15", as_of="2026-09-16")
    assert z == {"AAA": round(-2 / math.sqrt(8 / 3), 4), "BBB": 0.0, "CCC": round(2 / math.sqrt(8 / 3), 4)}
    pnl = F.panel(conn, "w39_tiny", "value", "2026-09-14", "2026-09-15", as_of="2026-09-15")
    assert pnl.shape == (2, 2) and math.isnan(pnl.loc[date(2026, 9, 14), "BBB"])


def test_alt_source_failure_is_recorded_and_raised(conn, monkeypatch, lake_root):
    from altdata import framework as F
    monkeypatch.setitem(F.REGISTRY, "w39_tiny", _test_source(F, [], error=RuntimeError("vendor 503")))
    with pytest.raises(RuntimeError):
        F.run_source(conn, "w39_tiny", "2026-09-15")
    st, rows, err = conn.execute("SELECT last_status, last_rows, error FROM alt_dataset WHERE "
                                 "source_id='w39_tiny'").fetchone()
    assert (st, rows) == ("FAILED", 0) and "vendor 503" in err
    with pytest.raises(LookupError):
        F.make("no_such_source")


def test_news_attention_counts_z_scores_and_sentiment_from_news_rows(conn):
    from altdata.sources import NewsAttention
    as_of = date(2026, 9, 15)
    rows = []
    # AAA's 30-day history (16 Aug - 14 Sep): 15 days with 1 article, 15 with 3 -> mean 2, pstdev 1
    for i in range(30):
        d = date(2026, 8, 16) + timedelta(days=i)
        rows += [(f"{d} 10:00:00", '["AAA"]', None)] * (1 if i % 2 else 3)
    # today: 5 AAA articles (sentiments 0.5, 0.1, -0.3, 0.7 and one unclassified), 1 BBB article
    for s in (0.5, 0.1, -0.3, 0.7, None):
        rows.append(("2026-09-15 09:00:00", '["AAA", "ZZZ"]', s))
    rows.append(("2026-09-15 12:00:00", '["BBB"]', None))
    rows += [("2026-09-16 09:00:00", '["AAA"]', 0.9),         # after as_of: ignored
             ("2026-09-15 13:00:00", "[]", 0.9),               # no symbols: ignored
             ("2026-09-15 14:00:00", "not json", 0.9)]         # unparseable: ignored
    conn.executemany("INSERT INTO news_articles (fetched_at, headline, symbols_mentioned, sentiment) VALUES "
                     "(?, 'h', ?, ?)", rows)
    conn.commit()
    obs = NewsAttention({}).fetch(conn, as_of, ["AAA", "BBB", "CCC"])
    got = {(o.entity, o.metric): o.value for o in obs}
    assert got == {("AAA", "articles"): 5.0, ("AAA", "articles_z30"): 3.0, ("AAA", "mean_sentiment"): 0.25,
                   ("BBB", "articles"): 1.0, ("BBB", "articles_z30"): 0.0,
                   ("CCC", "articles"): 0.0, ("CCC", "articles_z30"): 0.0}
    # a day's count is only known once the day is over
    assert {o.available_from for o in obs} == {datetime(2026, 9, 15, 23, 59)}


# ══ DP-16: NSE institutional data ══════════════════════════════════════════════════════════════

def _xbrl(mf, fpi, promoter, pledged=None):
    t = lambda tag, ctx, v: (f'<in-bse-shp:{tag} contextRef="{ctx}" unitRef="pure" decimals="4">{v}'
                             f'</in-bse-shp:{tag}>')
    parts = [t("ShareholdingAsAPercentageOfTotalNumberOfShares", "MutualFundsOrUTI_ContextI", mf),
             t("ShareholdingAsAPercentageOfTotalNumberOfShares", "InstitutionsForeign_ContextI", fpi),
             t("ShareholdingAsAPercentageOfTotalNumberOfShares", "ShareholdingOfPromoterAndPromoterGroup_ContextI",
               promoter),
             t("NumberOfShares", "ShareholdingOfPromoterAndPromoterGroup_ContextI", "1000000")]
    if pledged is not None:
        parts.append(t("NumberOfSharesPledgedOrOtherwiseEncumbered",
                       "ShareholdingOfPromoterAndPromoterGroup_ContextI", pledged))
    return "<xbrli:xbrl>" + "".join(parts) + "</xbrli:xbrl>"


class _FakeNSE:
    """The two calls data.nse_api's client makes: json(url) and text(url)."""

    def __init__(self, payloads, docs):
        self.payloads, self.docs, self.text_calls = payloads, docs, []

    def json(self, url):
        return next(v for k, v in self.payloads.items() if k in url)

    def text(self, url):
        self.text_calls.append(url)
        return self.docs[url]


def test_shp_xbrl_parser_reads_category_percent_and_pledge():
    from data.institutional import parse_shp_xbrl
    out = parse_shp_xbrl(_xbrl("0.0825", "0.1534", "0.5", "50000"))
    # fractions -> %, pledged = 50,000 / 1,000,000 promoter shares
    assert out == {"mf_pct": 8.25, "fpi_pct": 15.34, "promoter_x": 50.0, "pledged_pct": 5.0}
    assert parse_shp_xbrl("") == {}


def test_shareholding_insider_and_sast_sync_into_point_in_time_features(conn):
    from data import institutional as I
    shp = [{"date": "30-JUN-2026", "pr_and_prgrp": "50.00", "public_val": "50.00",
            "broadcastDate": "21-Jul-2026 18:30:00", "xbrl": "https://x/jun.xml"},
           {"date": "31-MAR-2026", "pr_and_prgrp": "49.00", "public_val": "51.00",
            "broadcastDate": "20-Apr-2026 17:00:00", "xbrl": "https://x/mar.xml"}]
    pit = {"data": [
        {"secType": "Equity Shares", "buyQuantity": "1000", "sellquantity": "0", "buyValue": "5000000",
         "acqName": "Promoter A", "personCategory": "Promoter", "date": "10-Jul-2026 17:00", "did": "D1",
         "acqfromDt": "08-Jul-2026", "acqtoDt": "09-Jul-2026", "afterAcqSharesPer": "50.1"},
        {"secType": "Equity Shares", "buyQuantity": "0", "sellquantity": "200", "sellValue": "1,000,000",
         "acqName": "Director B", "personCategory": "Director", "date": "12-Jul-2026 10:00", "did": "D2"},
        {"secType": "Warrants", "buyQuantity": "500", "buyValue": "9999999", "date": "12-Jul-2026 10:00",
         "did": "D9"},                                                    # not equity: skipped
        # no buy / sell quantities: classified from the acquisition mode
        {"secType": "Equity Shares", "secAcq": "300", "secVal": "1500000", "acqMode": "Market Sale",
         "acqName": "Promoter C", "date": "15-Jul-2026 09:00", "did": "D3"}]}
    sast = {"data": [
        {"application_no": "A1", "acquirerName": "Promoter Co", "promoterType": "Y", "acqSaleType": "Acquisition",
         "noOfShareAcq": "10,000", "noOfShareSale": "", "totAftShare": "51.2", "timestamp": "12-Jul-2026 18:00:00"},
        {"application_no": "A2", "acquirerName": "Promoter Co", "promoterType": "Y", "acqSaleType": "Sale",
         "noOfShareAcq": "", "noOfShareSale": "4,000", "timestamp": "14-Jul-2026 18:00:00"},
        {"application_no": "A3", "acquirerName": "A Fund", "promoterType": "N", "acqSaleType": "Acquisition",
         "noOfShareAcq": "99999", "timestamp": "14-Jul-2026 18:00:00"}]}
    nse = _FakeNSE({"share-holdings": shp, "corporates-pit": pit, "sast-reg29": sast},
                   {"https://x/jun.xml": _xbrl("0.0825", "0.1534", "0.5", "50000"),
                    "https://x/mar.xml": _xbrl("0.07", "0.16", "0.49")})
    assert I.sync_shareholding(conn, "ACME", nse) == 2 and len(nse.text_calls) == 2
    assert I.sync_shareholding(conn, "ACME", nse) == 0 and len(nse.text_calls) == 2     # incremental: no re-download
    assert I.sync_insider(conn, "ACME", nse) == 3 and I.sync_insider(conn, "ACME", nse) == 0
    assert I.sync_sast(conn, "ACME", nse) == 3
    txn = dict(conn.execute("SELECT disclosure_id, txn_type FROM insider_trade"))
    assert txn == {"D1": "BUY", "D2": "SELL", "D3": "SELL"}

    f = I.features(conn, "ACME", "2026-07-25")
    assert (f["promoter_pct"], f["mf_pct"], f["fpi_pct"], f["pledged_pct"]) == (50.0, 8.25, 15.34, 5.0)
    assert (f["promoter_chg"], f["mf_chg"], f["fpi_chg"]) == (1.0, 1.25, -0.66)
    # insiders: bought 50 lakh, sold 10 + 15 lakh -> net 0.25 crore
    assert (f["insider_net_cr_90d"], f["insider_buys_90d"], f["insider_sells_90d"]) == (0.25, 1, 2)
    assert f["sast_promoter_net_90d"] == 6000.0                          # promoter 10,000 bought - 4,000 sold
    # before the June SHP was broadcast (21 Jul) only March is known, and before 10 Jul no insider trade
    early = I.features(conn, "ACME", "2026-07-01")
    assert early["mf_pct"] == 7.0 and "mf_chg" not in early and "insider_net_cr_90d" not in early


# ══ AD-03: bulk / block deal event study ═══════════════════════════════════════════════════════

def _sessions(n, start=date(2026, 3, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _px(conn, sym, closes, days):
    conn.executemany("INSERT INTO prices_daily (symbol,date,close) VALUES (?,?,?)",
                     [(sym, str(d), c) for d, c in zip(days, closes)])


def test_deal_event_study_measures_abnormal_returns_after_the_session(conn):
    from quant.deal_signal import event_study
    d = _sessions(3)
    _px(conn, "NIFTY50", [1000.0, 1005.0, 1005.0], d)
    _px(conn, "AAA", [100.0, 102.0, 103.0], d)      # +2.0% vs NIFTY +0.5% -> abnormal +1.5 (BUY)
    _px(conn, "BBB", [50.0, 49.0, 49.0], d)         # -2.0% vs +0.5%       -> abnormal -2.5 (SELL)
    _px(conn, "DDD", [190.0, 200.0, 210.0], d)      # day 1 -> 2: +5% vs 0% -> abnormal +5.0 (BUY)
    _px(conn, "CCC", [10.0, 20.0, 30.0], d)
    _px(conn, "EEE", [10.0, 11.0, 12.0], d)
    deals = [("AAA", d[0], 5.0), ("BBB", d[0], -3.0), ("CCC", d[0], 0.5),       # CCC below min_cr: no event
             ("DDD", d[1], 2.0), ("EEE", d[2], 4.0)]                            # EEE: no bar after it
    conn.executemany("INSERT INTO bulk_deals (symbol,date,net_value_cr) VALUES (?,?,?)",
                     [(s, str(x), v) for s, x, v in deals])
    conn.commit()
    r = event_study(conn, d[0], d[2], horizons=(1,), store=False)
    assert r["events"] == 4
    h = r["horizons"]["1"]
    assert h["events_with_forward_window"] == 3 and h["pooled_ic"] is None       # IC needs 20 events
    b, s = h["BUY"], h["SELL"]
    assert b["n"] == 2 and b["mean_abnormal_pct"] == pytest.approx(3.25) and b["hit_rate"] == 1.0
    # t = mean / (sd / sqrt n) with sd(1.5, 5.0) = 3.5 / sqrt 2 -> 3.25 / 1.75
    assert b["t_stat"] == pytest.approx(round(3.25 / 1.75, 3))
    assert s == {"n": 1, "mean_abnormal_pct": pytest.approx(-2.5), "median_abnormal_pct": pytest.approx(-2.5),
                 "hit_rate": 1.0, "t_stat": None}
    assert r["verdict"] == "INSUFFICIENT_DATA" and r["findings"] == []


def test_deal_event_study_verdict_and_stored_result_on_thirty_confirming_events(conn):
    from quant.deal_signal import event_study, latest
    d = _sessions(2)
    _px(conn, "NIFTY50", [1000.0, 1000.0], d)
    for i in range(30):                              # abnormal 1.0 .. 3.9 %, net value rising with it
        _px(conn, f"S{i:02d}", [100.0, 100.0 + 1 + 0.1 * i], d)
        conn.execute("INSERT INTO bulk_deals (symbol,date,net_value_cr) VALUES (?,?,?)", (f"S{i:02d}", str(d[0]),
                                                                                          2.0 + i))
    conn.commit()
    r = event_study(conn, d[0], d[1], horizons=(1,))
    h = r["horizons"]["1"]
    assert h["BUY"]["n"] == 30 and h["BUY"]["hit_rate"] == 1.0 and h["SELL"] == {"n": 0}
    assert h["BUY"]["mean_abnormal_pct"] == pytest.approx(2.45)
    assert h["pooled_ic"] == 1.0                     # bigger net buying, bigger abnormal return
    assert [(f["side"], f["direction"]) for f in r["findings"]] == [("BUY", "CONFIRMS")]
    assert r["verdict"] == "SUPPORTS_INS_DIRECTION"
    assert latest(conn)["verdict"] == "SUPPORTS_INS_DIRECTION"


# ══ DB-16: strategy performance ════════════════════════════════════════════════════════════════

def test_strategy_performance_books_decisions_backtest_and_health(conn):
    from strategy_engine.performance import book_pnl, performance
    now = datetime.now()
    for sid, ver in (("S1", "1.1.0"), ("S2", "1.0.0")):
        conn.execute("INSERT INTO strategy (strategy_id,name,kind,status,current_version) VALUES (?,?,?,?,?)",
                     (sid, sid + " name", "rule", "PAPER", ver))
    # paper book: INFY 10 @100 + 10 @110 (avg 105), sell 15 @120 -> realised 225, 5 left at 105
    #             TCS short 5 @200, buy 8 @190 -> +50 on the short, then long 3 @190
    fills = [("PAPER", "INFY", "BUY", 10, 100.0, 1.0), ("PAPER", "INFY", "BUY", 10, 110.0, 1.0),
             ("PAPER", "INFY", "SELL", 15, 120.0, 2.0), ("PAPER", "TCS", "SELL", 5, 200.0, 0.0),
             ("PAPER", "TCS", "BUY", 8, 190.0, 0.0), ("LIVE", "WIPRO", "BUY", 4, 50.0, 0.5)]
    for i, (mode, sym, side, q, px, fee) in enumerate(fills):
        _order(conn, f"P{i}", sym, side, q, "MARKET", px, "FILLED", q, f"B{i}", mode=mode)
        _fill(conn, f"PF{i}", f"P{i}", sym, side, q, px, f"2026-09-1{i} 10:00:00", fees=fee, mode=mode)
    conn.execute("INSERT INTO oms_order (order_id,intent_id,risk_decision_id,strategy_id,symbol,side,quantity,"
                 "order_type,mode,status,instrument) VALUES ('FUT1','IF','RF','S1','INFY','SELL',300,'MARKET',"
                 "'PAPER','FILLED','FUT')")                                  # futures legs are a separate book
    _fill(conn, "FF1", "FUT1", "INFY", "SELL", 300, 125.0, "2026-09-18 10:00:00")
    for sym, c in (("INFY", 130.0), ("TCS", 195.0)):                       # WIPRO has no close
        conn.execute("INSERT INTO prices_daily (symbol,date,close) VALUES (?,?,?)", (sym, "2026-09-18", c))
    # decisions in the window: 2 ENTER (one blocked), 1 HOLD; one EXIT 60 days ago does not count
    for i, (ago, sym, action, blocked) in enumerate(((1, "INFY", "ENTER", None), (5, "TCS", "ENTER", "max positions"),
                                                     (2, "WIPRO", "HOLD", None), (60, "INFY", "EXIT", None))):
        conn.execute("INSERT INTO strategy_decision (decision_id,strategy_id,version,as_of,symbol,decision,action,"
                     "blocked_reason) VALUES (?,?,?,?,?,?,?,?)", (f"D{i}", "S1", "1.1.0",
                                                                   str(date.today() - timedelta(days=ago)), sym,
                                                                   "BUY", action, blocked))
    bt = [("RUN_OLD", "single", "1.1.0", now - timedelta(days=3), {"sharpe": 0.1}),
          ("RUN_NEW", "single", "1.1.0", now - timedelta(days=1), {"total_return": 12.5, "sharpe": 1.4,
                                                                    "max_drawdown": -8.0, "trades": 40}),
          ("RUN_WF", "wf_test", "1.1.0", now, {"sharpe": 9.9}),
          ("RUN_PREV_VER", "single", "1.0.0", now, {"sharpe": 7.7})]
    for run_id, kind, ver, fin, m in bt:
        conn.execute("INSERT INTO backtest_run (run_id,kind,strategy_id,strategy_version,start_date,end_date,"
                     "config_json,metrics_json,status,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (run_id, kind, "S1", ver, "2025-01-01", "2025-12-31", "{}", json.dumps(m), "COMPLETED", fin))
    conn.execute("INSERT INTO strategy_health (strategy_id,version,as_of,status,issues_json,created_at) VALUES "
                 "('S1','1.1.0','2026-09-01','HEALTHY','[]',?)", (now,))
    conn.execute("INSERT INTO strategy_health (strategy_id,version,as_of,status,issues_json,created_at) VALUES "
                 "('S1','1.1.0','2026-09-10','WARNING',?,?)", (json.dumps([{"code": "DRAWDOWN"}, "junk"]), now))
    conn.commit()

    b = book_pnl(conn, "S1")
    # PAPER: realised 225 + 50 - fees 4 = 271; unrealised (130-105)*5 + (195-190)*3 = 140; basis 525 + 570
    assert b["PAPER"] == {"realized": 271.0, "fees": 4.0, "fills": 5, "unrealized": 140.0, "open_positions": 2,
                          "cost_basis": 1095.0}
    assert b["LIVE"] == {"realized": -0.5, "fees": 0.5, "fills": 1, "unrealized": 0.0, "open_positions": 1,
                         "cost_basis": 200.0}
    rows = {r["strategy_id"]: r for r in performance(conn, days=30)}
    s1 = rows["S1"]
    assert s1["health"] == {"status": "WARNING", "as_of": "2026-09-10", "issues": ["DRAWDOWN"]}
    assert s1["backtest"]["run_id"] == "RUN_NEW" and s1["backtest"]["sharpe"] == 1.4
    assert s1["backtest"]["period"] == "2025-01-01..2025-12-31" and s1["backtest"]["win_rate"] is None
    assert s1["decisions"] == {"days": 30, "by_action": {"ENTER": 2, "HOLD": 1}, "blocked": 1}
    assert s1["books"]["PAPER"]["realized"] == 271.0
    assert rows["S2"]["health"] is None and rows["S2"]["backtest"] is None and rows["S2"]["books"] == {}


# ══ API-03: versioned public API with API keys ═════════════════════════════════════════════════

PASSWORD = "Correct-Horse-Battery-9!"


@pytest.fixture
def v1(tmp_path, monkeypatch, conn):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from enterprise import apikeys, service, users
    from enterprise import config as ecfg
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enterprise": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(ecfg, "CONFIG_PATH", cfg)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", cfg)
    service.ensure_seeded(conn)
    u = users.create_user(conn, "api.viewer", PASSWORD, "default", ["VIEWER"])
    k = apikeys.create(conn, u["user_id"], "default", "ci", ["strategy:read"])
    conn.execute("INSERT INTO strategy (strategy_id,name,kind,status,current_version) VALUES "
                 "('S1','S1','rule','PAPER','1.0.0')")
    conn.execute("INSERT INTO strategy_decision (decision_id,strategy_id,version,as_of,symbol,decision,action) VALUES "
                 "('D1','S1','1.0.0','2026-09-15','INFY','BUY','ENTER')")
    conn.commit()
    return TestClient(server.app), conn, u, k


def test_api_key_reads_v1_within_its_scopes_only(v1):
    client, conn, u, k = v1
    h = {"Authorization": f"ApiKey {k['api_key']}"}
    r = client.get("/api/v1/strategy-decisions", headers=h)
    assert r.status_code == 200 and [d["decision_id"] for d in r.json()] == ["D1"]
    assert r.headers.get("api-version") == "v1" and "deprecation" not in r.headers
    # VIEWER holds execution:read, but the key was scoped to strategy:read only
    r = client.get("/api/v1/oms/orders", headers=h)
    assert r.status_code == 403 and "execution:read" in r.text
    # the unversioned path still answers an API key, marked deprecated with its v1 successor
    r = client.get("/api/strategy-decisions", headers=h)
    assert r.status_code == 200 and r.headers.get("deprecation") == "true"
    assert "</api/v1/strategy-decisions>" in r.headers.get("link", "")
    # usage counts the calls the key was allowed to make (the 403 was refused before metering)
    assert conn.execute("SELECT calls FROM enterprise_api_usage WHERE key_id=?", (k["key_id"],)).fetchone()[0] == 2
    # a key can never carry a permission its user does not hold
    from enterprise import apikeys
    with pytest.raises(ValueError):
        apikeys.create(conn, u["user_id"], "default", "too wide", ["execution:trade"])


def test_missing_wrong_revoked_or_expired_keys_are_refused(v1):
    client, conn, u, k = v1
    from enterprise import apikeys
    assert client.get("/api/v1/strategy-decisions").status_code == 401
    assert client.get("/api/v1/strategy-decisions", headers={"Authorization": "ApiKey atk_deadbeef_not-a-key"}).status_code == 401
    assert client.get("/api/v1/strategy-decisions", headers={"Authorization": "ApiKey " + k["api_key"][:-2] + "xx"}
                      ).status_code == 401
    other = apikeys.create(conn, u["user_id"], "default", "to expire", ["strategy:read"])
    conn.execute("UPDATE enterprise_api_key SET expires_at=? WHERE key_id=?",
                 (datetime.now() - timedelta(minutes=1), other["key_id"]))
    conn.commit()
    assert client.get("/api/v1/strategy-decisions", headers={"Authorization": f"ApiKey {other['api_key']}"}
                      ).status_code == 401
    h = {"Authorization": f"ApiKey {k['api_key']}"}
    assert client.get("/api/v1/strategy-decisions", headers=h).status_code == 200
    apikeys.revoke(conn, u["user_id"], k["key_id"], actor="test")
    assert client.get("/api/v1/strategy-decisions", headers=h).status_code == 401


def test_api_key_rate_limit_and_daily_quota_answer_429(v1):
    client, conn, u, k = v1
    from enterprise import apikeys
    from enterprise.public_api import key_limits
    assert key_limits(conn, k["key_id"]) == {"per_minute": 60, "daily_quota": 10000}       # documented defaults
    conn.execute("UPDATE enterprise_api_key SET rate_limit_per_minute=2 WHERE key_id=?", (k["key_id"],))
    conn.commit()
    h = {"Authorization": f"ApiKey {k['api_key']}"}
    codes = [client.get("/api/v1/strategy-decisions", headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = client.get("/api/v1/strategy-decisions", headers=h)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    q = apikeys.create(conn, u["user_id"], "default", "quota", ["strategy:read"])
    conn.execute("UPDATE enterprise_api_key SET daily_quota=1 WHERE key_id=?", (q["key_id"],))
    conn.commit()
    hq = {"Authorization": f"ApiKey {q['api_key']}"}
    assert client.get("/api/v1/strategy-decisions", headers=hq).status_code == 200
    r = client.get("/api/v1/strategy-decisions", headers=hq)
    assert r.status_code == 429 and "daily quota 1" in r.text


def test_v1_contract_documents_the_permissions_the_middleware_enforces():
    pytest.importorskip("fastapi")
    from dashboard import server
    from enterprise.authz import permission_for
    from enterprise.public_api import RESOURCES, openapi_v1
    spec = openapi_v1(server.app)
    assert spec["servers"] == [{"url": "/api/v1"}] and spec["x-atip-unresolved"] == []
    for method, path, perm, _, _ in RESOURCES:
        op = spec["paths"][path[len("/api"):]][method.lower()]
        assert op["x-atip-permission"] == perm
        assert permission_for(method, path.replace("{", "").replace("}", "")) == perm, (method, path)
        assert {"401", "403", "429"} <= set(op["responses"])


# ══ DB-19 / OPS-07: read-only snapshot publishing ══════════════════════════════════════════════

@pytest.fixture
def pub(monkeypatch):
    import publish_snapshot as P
    snap = {"html": "<!doctype html><html><body>ATIP snapshot</body></html>", "generated_at": "2026-09-15 16:00",
            "trade_date": "2026-09-15"}
    monkeypatch.setattr(P, "build_snapshot", lambda: dict(snap))
    posts = []
    monkeypatch.setattr(P.requests, "post", lambda url, **kw: posts.append((url, kw)) or posts_reply(url))
    replies = {}

    def posts_reply(url):
        r = replies.get(url, 200)
        if isinstance(r, Exception):
            raise r

        class R:
            status_code = r
            ok = r < 400
            text = "err"
        return R()
    return P, snap, posts, replies


def test_publish_refuses_without_a_target_config_and_never_uploads(pub, tmp_path, monkeypatch):
    P, _, posts, _ = pub
    monkeypatch.setattr(P, "CONFIG_PATH", tmp_path / "publish.json")
    with pytest.raises(SystemExit, match="Missing"):
        P.load_targets()
    (tmp_path / "publish.json").write_text(json.dumps({"targets": [{"url": "https://a/x"}, {"token": "t"}]}),
                                           encoding="utf-8")
    with pytest.raises(SystemExit, match="No targets"):
        P.load_targets()
    monkeypatch.setattr(sys, "argv", ["publish_snapshot.py"])
    with pytest.raises(SystemExit):
        P.main()
    assert posts == []
    # a dry run builds the page and needs no config at all
    monkeypatch.setattr(sys, "argv", ["publish_snapshot.py", "--dry-run"])
    assert P.main() == 0 and posts == []


def test_publish_posts_the_snapshot_to_every_target_with_its_own_token(pub, tmp_path, monkeypatch):
    import requests
    P, snap, posts, replies = pub
    cfg = tmp_path / "publish.json"
    cfg.write_text(json.dumps({"targets": [{"url": "https://a/atip/api/snapshot", "token": "tok-a"},
                                           {"url": "https://b/atip/api/snapshot", "token": "tok-b"},
                                           {"url": "https://c/atip/api/snapshot", "token": ""}]}), encoding="utf-8")
    monkeypatch.setattr(P, "CONFIG_PATH", cfg)
    monkeypatch.setattr(sys, "argv", ["publish_snapshot.py"])
    assert P.main() == 0
    assert [u for u, _ in posts] == ["https://a/atip/api/snapshot", "https://b/atip/api/snapshot"]
    assert posts[0][1]["json"] == snap and posts[0][1]["headers"] == {"Authorization": "Bearer tok-a"}
    assert posts[1][1]["headers"] == {"Authorization": "Bearer tok-b"}
    # one failing target (HTTP error or a connection error) makes the run exit 1, the other still gets it
    posts.clear()
    replies["https://a/atip/api/snapshot"] = 500
    assert P.main() == 1 and len(posts) == 2
    replies["https://a/atip/api/snapshot"] = requests.ConnectionError("down")
    assert P.main() == 1


def test_snapshot_payload_is_built_from_the_local_database_without_the_write_token(conn, tmp_path, monkeypatch):
    """build_snapshot renders the real dashboard page from the (empty) test database. The page it
    publishes to the internet must not carry the local dashboard's write token (X-ATIP-Token), which
    authorises order-rule changes on this machine."""
    from dashboard import security, server
    import publish_snapshot as P
    secret = "local-write-token-" + "x" * 30
    (tmp_path / "dashboard_token.txt").write_text(secret, encoding="utf-8")
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(server, "STATE_PATH", tmp_path / "dashboard_state.json")    # not the real cache
    assert secret in server.build_html(server.generate_state(server.latest_scored_date()))   # the local page has it
    snap = P.build_snapshot()
    assert set(snap) == {"html", "generated_at", "trade_date"}
    assert isinstance(snap["html"], str) and "<html" in snap["html"].lower()
    assert secret not in snap["html"]
    json.dumps(snap)                            # the upload body is JSON
