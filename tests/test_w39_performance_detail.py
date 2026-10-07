"""W39: the PERF-001 detail sheet (PERF-001-01 .. -14) -- one or more tests per requirement.

Every expected number here is computed by hand (or with plain arithmetic in the test)
from the deterministic seed in tests/_wealth_seed.py, never by calling the code under
test a second way.
"""

import csv
import io
import math
from datetime import timedelta

import pytest

from tests._wealth_seed import OWNER, fresh
from wealth.perf import data as D
from wealth.perf import engine as E
from wealth.perf import ledger as L
from wealth.perf import metrics as M
from wealth.perf import model as MD
from wealth.perf import report as R


def _close(conn, sym, d):
    return conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date=?", (sym, str(d))).fetchone()[0]


def _run(conn, ds, pf="MANUAL", start=None, end=None):
    start, end = start or ds[0], end or ds[-1]
    prices = D.Prices(conn, start - timedelta(days=40), end)
    return E.run_actual(conn, OWNER, pf, start, end, prices, "NIFTY50")


def _txn(conn, d, kind, qty=None, price=None, fees=0, sym="ACME", **kw):
    b = {"portfolio": "MANUAL", "trade_date": str(d), "kind": kind, "quantity": qty, "price": price, "fees": fees, **kw}
    if sym is not None:
        b["symbol"] = sym
    return L.add_manual(conn, OWNER, b)


def _signal(conn, sid, d, sym="ACME", signal="BUY", price=None):
    conn.execute("INSERT INTO signal_log (id,run_id,logged_at,signal_date,symbol,signal,entry_price) VALUES "
                 "(?,?,?,?,?,?,?)", (sid, "r", str(d), str(d), sym, signal, price or _close(conn, sym, d)))
    conn.commit()


# ── PERF-001-01 data model ───────────────────────────────────────────────

def test_paper_partial_fills_reach_the_ledger(temp_db):
    conn, ds = fresh(temp_db)
    from orders.paper import ensure_tables
    ensure_tables(conn)
    for oid, side, q, filled, st, d in (("P1", "BUY", 10, 5, "PARTIALLY_FILLED", ds[10]),
                                        ("P2", "SELL", 5, 5, "TRADED", ds[20])):
        conn.execute("INSERT INTO paper_order (order_id,created_at,symbol,security_id,exchange,transaction_type,"
                     "quantity,filled_qty,order_type,product_type,limit_price,fill_price,status,reason,brokerage,tag) "
                     "VALUES (?,?,'ACME','1','NSE_EQ',?,?,?,'MARKET','CNC',0,100,?,NULL,1,NULL)",
                     (oid, str(d) + "T10:00:00", side, q, filled, st))
    conn.commit()
    assert L.sync(conn, OWNER)["PAPER"] == 2
    rows = L.transactions(conn, OWNER, "PAPER")
    assert [r["quantity"] for r in rows] == [5, 5] and rows[0]["order_ref"] == "P1"
    a = _run(conn, ds, "PAPER")
    assert not [q for q in a["data_quality"] if q["issue"] == "EXCESS_SELL"]      # the sell is not capped


def test_entry_order_is_portable_and_numbered(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[5], "BUY", 10, 100)
    _txn(conn, ds[5], "SELL", 10, 101)
    seqs = [r["entry_seq"] for r in L.transactions(conn, OWNER, "MANUAL")]
    assert seqs == sorted(seqs) and seqs[0] >= 1 and len(set(seqs)) == 2

    class NotSqlite:                     # what MySQL / PostgreSQL connections look like to order_by()
        pass
    assert "rowid" in L.order_by(conn) and "rowid" not in L.order_by(NotSqlite())
    assert "entry_seq" in L.order_by(NotSqlite())


def test_migration_0006_numbers_old_rows_and_the_ledger_stays_append_only(temp_db):
    conn, ds = fresh(temp_db)
    for i in range(3):          # rows written before W39: no entry_seq
        conn.execute("INSERT INTO perf_ledger (txn_id,tenant_id,owner_id,portfolio,source,source_ref,trade_date,kind,"
                     "symbol,quantity,price,fees) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"old{i}", "default", "owner", "MANUAL", "manual", f"o{i}", str(ds[3]), "BUY", "ACME", 1, 100, 0))
    conn.commit()
    from ops import migrations
    res = migrations.apply(conn)
    assert all(r["status"] == "APPLIED" for r in res), res
    got = conn.execute("SELECT entry_seq, rowid FROM perf_ledger ORDER BY rowid").fetchall()
    assert [g[0] for g in got] == [g[1] for g in got]
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE perf_ledger SET price=1 WHERE txn_id='old0'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM perf_ledger WHERE txn_id='old0'")
    assert not migrations.validate(conn)


def test_cash_rows_and_fee_breakdown_are_validated(temp_db):
    conn, ds = fresh(temp_db)
    with pytest.raises(ValueError, match="leave symbol empty"):
        _txn(conn, ds[1], "DEPOSIT", gross_value=1000)
    with pytest.raises(ValueError, match="gross_value"):
        _txn(conn, ds[1], "DEPOSIT", sym=None)
    with pytest.raises(ValueError, match="leave fees empty"):
        _txn(conn, ds[1], "FEE", sym=None, fees=5, gross_value=5)
    with pytest.raises(ValueError, match="adds up"):
        _txn(conn, ds[2], "BUY", 1, 100, fees=10, fee_breakdown={"brokerage": 5, "stt": 1})
    with pytest.raises(ValueError, match="components"):
        _txn(conn, ds[2], "BUY", 1, 100, fee_breakdown={"tip": 1})
    t = _txn(conn, ds[2], "BUY", 1, 100, fee_breakdown={"brokerage": 5, "stt": 1})
    assert t["fees"] == pytest.approx(6)
    with pytest.raises(ValueError, match="not in signal_log"):
        _txn(conn, ds[3], "BUY", 1, 100, signal_id="nope")


# ── PERF-001-02 / -03 model and executable ──────────────────────────────

def test_model_return_is_reproducible_and_matches_a_hand_calculation(temp_db):
    conn, ds = fresh(temp_db)
    _signal(conn, "S1", ds[60])
    p = D.Prices(conn, ds[0], ds[-1])
    a = MD.build(conn, ds[0], ds[-1], p, {})
    b = MD.build(conn, ds[0], ds[-1], D.Prices(conn, ds[0], ds[-1]), {})
    assert a["model"]["series"] == b["model"]["series"] and a["model"]["trades"] == b["model"]["trades"]
    t = a["model"]["trades"][0]
    entry, exit_ = _close(conn, "ACME", ds[60]), _close(conn, "ACME", ds[80])       # 20-session horizon
    assert t["exit_date"] == str(ds[80]) and t["ret"] == pytest.approx(exit_ / entry - 1, rel=1e-9)
    # the daily series telescopes to the same trade return (the stored exit is rounded to 4 dp)
    assert M.chain(a["model"]["series"]) == pytest.approx(exit_ / entry - 1, rel=1e-4)


def test_executable_applies_slippage_costs_and_the_liquidity_cap(temp_db):
    conn, ds = fresh(temp_db)
    _signal(conn, "S1", ds[60])
    m = MD.build(conn, ds[0], ds[-1], D.Prices(conn, ds[0], ds[-1]), {"slippage_bps": 25})
    e = m["executable"]["trades"][0]
    nxt_open = conn.execute("SELECT open FROM prices_daily WHERE symbol='ACME' AND date=?", (str(ds[61]),)).fetchone()[0]
    assert e["entry"] == pytest.approx(round(nxt_open * 1.0025, 4))
    assert e["costs"] > 0 and e["status"] == "EXECUTABLE" and e.get("liquidity") is None
    # 5% of ~Rs 10 crore traded a day is ~Rs 50 lakh: a Rs 1 crore notional is refused
    big = MD.build(conn, ds[0], ds[-1], D.Prices(conn, ds[0], ds[-1]), {"notional": 1e7})
    assert big["executable"]["trades"][0]["status"] == "NOT_EXECUTABLE"
    conn.execute("UPDATE prices_daily SET volume=NULL WHERE symbol='ACME'")
    conn.commit()
    u = MD.build(conn, ds[0], ds[-1], D.Prices(conn, ds[0], ds[-1]), {})
    assert u["executable"]["trades"][0]["liquidity"] == "UNKNOWN" and u["executable"]["liquidity_unknown"] == 1


# ── PERF-001-04 actual, -05 benchmark, -08 metrics ──────────────────────

def test_period_scope_is_separate_from_lifetime(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 20, 100, fees=4)
    _txn(conn, ds[20], "SELL", 10, 110, fees=2)          # before the period
    _txn(conn, ds[120], "SELL", 5, 130, fees=1)          # inside
    a = _run(conn, ds, start=ds[100])
    R_ = a["returns"]
    avg = (2000 + 4) / 20
    assert R_["realized_pnl"] == pytest.approx(10 * (110 - avg) - 2 + 5 * (130 - avg) - 1, abs=0.01)
    assert R_["realized_pnl_period"] == pytest.approx(5 * (130 - avg) - 1, abs=0.01)
    assert a["costs"]["fees_total"] == pytest.approx(1) and a["costs"]["fees_total_lifetime"] == pytest.approx(7)
    assert a["costs"]["reconciliation"]["ok"]


def test_xirr_and_pme_match_hand_computed_flows(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 100, 100)
    _txn(conn, ds[200], "SELL", 40, 140)
    a = _run(conn, ds)
    v_end = 60 * _close(conn, "ACME", ds[-1])
    flows = [(ds[10], -10000.0), (ds[200], 5600.0), (ds[-1], v_end)]
    assert a["returns"]["xirr_pct"] == pytest.approx(M.xirr(flows) * 100, abs=1e-3)
    n10, n200, nend = (_close(conn, "NIFTY50", d) for d in (ds[10], ds[200], ds[-1]))
    units = 10000 / n10 - 5600 / n200
    assert a["returns"]["pme"]["value_if_invested"] == pytest.approx(units * nend, abs=0.05)


def test_benchmark_return_is_the_price_return_over_the_sessions(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 1, 100)
    rep = R.build(conn, OWNER, "MANUAL", ds[50], ds[-1], sync_first=False)
    b = next(c for c in rep["comparison"] if c["return"] == "BENCHMARK")
    exp = _close(conn, "NIFTY50", ds[-1]) / _close(conn, "NIFTY50", ds[49]) - 1
    assert b["total_return_pct"] == pytest.approx(exp * 100, abs=1e-3)
    assert b["days_invested"] == len(ds) - 50


def test_risk_metrics_match_independent_formulas():
    import statistics
    rets = [0.01 * math.sin(i / 3) + 0.0004 for i in range(120)]
    bench = [0.008 * math.sin(i / 3 + 0.2) + 0.0003 for i in range(120)]
    dates = [None] * 120
    from datetime import date
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    m = M.series_metrics(dates, rets, bench, rf_pct=6.5)
    rf = 1.065 ** (1 / 252) - 1
    sd = statistics.stdev(rets)
    assert m["volatility_pct"] == pytest.approx(round(sd * math.sqrt(252) * 100, 3), abs=1e-3)
    assert m["sharpe"] == pytest.approx(round(statistics.mean([r - rf for r in rets]) / sd * math.sqrt(252), 3), abs=1e-3)
    down = math.sqrt(sum(min(0.0, r - rf) ** 2 for r in rets) / len(rets))
    assert m["sortino"] == pytest.approx(round(statistics.mean([r - rf for r in rets]) / down * math.sqrt(252), 3),
                                         abs=1e-3)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    assert m["max_drawdown_pct"] == pytest.approx(round(mdd * 100, 3), abs=1e-3)
    beta = statistics.covariance(rets, bench) / statistics.variance(bench)
    alpha = ((statistics.mean(rets) - rf) - beta * (statistics.mean(bench) - rf)) * 252
    assert m["beta"] == pytest.approx(round(beta, 3), abs=1e-3)
    assert m["alpha_annual_pct"] == pytest.approx(round(alpha * 100, 3), abs=1e-3)


def test_cagr_annualises_a_full_year():
    from datetime import date
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]
    rets = [None] + [0.0005] * 399
    m = M.series_metrics(dates, rets)
    total = 1.0005 ** 399 - 1
    span = (dates[-1] - dates[1]).days + 1
    assert m["annualized_pct"] == pytest.approx(round(((1 + total) ** (365.25 / span) - 1) * 100, 3), abs=1e-3)


# ── PERF-001-06 costs ────────────────────────────────────────────────────

def test_costs_reconcile_with_dividend_tax_fee_rows_and_a_capped_sell(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 10, 100, fees=3, fee_breakdown={"brokerage": 2, "stt": 1})
    _txn(conn, ds[30], "DIVIDEND", 10, gross_value=50, fees=5)          # 10% TDS recorded as the fee
    L.add_manual(conn, OWNER, {"portfolio": "MANUAL", "trade_date": str(ds[40]), "kind": "FEE", "gross_value": 20})
    # a broker-sync style oversell (add_manual refuses it, imports do not)
    L._insert(conn, OWNER, "MANUAL", "test", "x1", ds[50], str(ds[50]), "SELL", "ACME", 12, 120, 6)
    conn.commit()
    a = _run(conn, ds)
    K = a["costs"]
    assert K["reconciliation"]["ok"], K["reconciliation"]
    assert K["fees_total"] == pytest.approx(3 + 5 + 20 + 6)
    assert K["by_kind"]["DIVIDEND"] == pytest.approx(5) and K["components"] == {"brokerage": 2, "stt": 1}
    acme = next(p for p in a["positions"] if p["symbol"] == "ACME")
    assert acme["dividends"] == pytest.approx(50)
    assert acme["realized"] == pytest.approx(10 * (120 - 100.3) - 6 + 45, abs=0.01)


def test_statutory_estimate_itemises_paper_brokerage_only_trades(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 100, 100, fees=10)
    est = _run(conn, ds)["costs"]["statutory_estimate"]
    assert est and est["by_component"]["stt"] > 0 and est["by_component"]["stamp"] > 0


# ── PERF-001-07 / -09 position and signal attribution ───────────────────

def test_sizing_effect_completes_the_decomposition_and_exact_links_win(temp_db):
    conn, ds = fresh(temp_db)
    _signal(conn, "S1", ds[100])
    _signal(conn, "S2", ds[100], sym="GOLDBEES")
    # bought 10 sessions after the signal: outside the 5-session window, linked by its signal id
    _txn(conn, ds[110], "BUY", 10, 150, fees=2, signal_id="S1")
    _txn(conn, ds[112], "BUY", 7, 151, fees=1)                      # an add-on
    _txn(conn, ds[130], "SELL", 17, 160, fees=3)
    _txn(conn, ds[101], "BUY", 30, 61, sym="GOLDBEES")             # no id: the window rule links it
    rep = R.build(conn, OWNER, "MANUAL", ds[0], ds[-1], sync_first=False)
    links = {x["signal_id"]: x for x in rep["signal_attribution"]["linked"]}
    assert links["S1"]["link_method"] == "EXACT" and links["S1"]["entry"]["delay_sessions"] == 10
    assert len(links["S1"]["add_ons"]) == 1 and links["S1"]["exits"][0]["quantity"] == 17
    assert links["S2"]["link_method"] == "WINDOW"
    for x in links.values():
        v = x["attribution_vs_model_notional"]
        assert v["total"] == pytest.approx(v["check"], abs=0.02)
        assert x["position_attribution"]["total"] == pytest.approx(x["position_attribution"]["check"], abs=0.02)
    S = rep["signal_attribution"]
    assert S["discretionary_buys"] == 0
    assert S["signal_driven_pnl"] + S["discretionary_pnl"] == pytest.approx(S["total_pnl"], abs=0.01)
    assert S["total_pnl"] == pytest.approx(rep["actual"]["returns"]["gain"], abs=0.01)


# ── PERF-001-10 portfolio attribution ───────────────────────────────────

def test_risk_contribution_reconciles_and_groups_sum_to_the_total(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[5], "BUY", 10, 100)
    _txn(conn, ds[5], "BUY", 20, 60, sym="GOLDBEES")
    _txn(conn, ds[90], "SELL", 4, 110, fees=2)
    L.add_manual(conn, OWNER, {"portfolio": "MANUAL", "trade_date": str(ds[120]), "kind": "FEE", "gross_value": 30})
    a = _run(conn, ds)
    rc = a["risk_contribution"]
    assert rc["status"] == "OK" and rc["reconciles"]
    assert sum(x["risk_share_pct"] for x in rc["by_symbol"]) == pytest.approx(100, abs=1e-2)
    total = a["returns"]["gain"]
    for key in ("by_sector", "by_asset_class"):
        assert sum(x["gain"] for x in a["contribution"][key]) == pytest.approx(total, abs=0.05)


def test_cash_account_return_and_the_negative_cash_flag(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[5], "DEPOSIT", sym=None, gross_value=20000)
    _txn(conn, ds[10], "BUY", 100, 100)
    a = _run(conn, ds)
    ac = a["account"]
    exp_end = 10000 + 100 * _close(conn, "ACME", ds[-1])
    assert ac["status"] == "OK" and ac["closing_cash"] == pytest.approx(10000)
    assert ac["closing_value"] == pytest.approx(exp_end, abs=0.01)
    assert ac["twr"]["total_return_pct"] == pytest.approx((exp_end / 20000 - 1) * 100, abs=1e-3)
    _txn(conn, ds[20], "WITHDRAWAL", sym=None, gross_value=15000)
    a2 = _run(conn, ds)
    assert a2["account"]["status"] == "NEGATIVE_CASH"
    assert any(q["issue"] == "NEGATIVE_CASH" for q in a2["data_quality"])
    assert _run(conn, ds)["returns"]["twr"] == a2["returns"]["twr"]         # cash never moves the sleeve


# ── PERF-001-11 / -12 / -13 dashboard, audit, export ────────────────────

def test_export_carries_every_section_with_the_reports_numbers(temp_db):
    conn, ds = fresh(temp_db)
    _signal(conn, "S1", ds[100])
    _txn(conn, ds[101], "BUY", 10, 150, fees=2)
    _txn(conn, ds[130], "SELL", 4, 160, fees=3)
    rep = R.run(conn, OWNER, "MANUAL", ds[0], ds[-1])
    stored = R.get(conn, OWNER, rep["report_id"])
    text, media = R.export(stored, "csv")
    for sec in ("# comparison", "# gaps", "# actual summary", "# costs", "# contribution by sector",
                "# contribution by asset class", "# risk contribution", "# data quality", "# model trades",
                "# executable trades", "# signal attribution summary", "# audit"):
        assert sec in text, sec
    rows = list(csv.reader(io.StringIO(text)))
    i = rows.index(["# comparison"])
    hdr = rows[i + 1]
    for k, c in enumerate(stored["comparison"]):
        got = dict(zip(hdr, rows[i + 2 + k]))
        assert got["return"] == c["return"]
        assert got["total_return_pct"] == ("" if c["total_return_pct"] is None else str(c["total_return_pct"]))
    j, jm = R.export(stored, "json")
    import json
    assert jm == "application/json" and json.loads(j)["comparison"] == stored["comparison"]


def test_verify_reproduces_and_detects_changed_inputs(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 10, 100)
    rep = R.run(conn, OWNER, "MANUAL", ds[0], ds[-1])
    au = rep["audit"]
    assert au["calculation_version"].startswith(R.CALCULATION_VERSION)
    assert au["inputs"]["prices_rows"] > 0 and au["inputs"]["last_price_date"]["ACME"] == str(ds[-1])
    assert R.verify(conn, OWNER, rep["report_id"])["match"]
    conn.execute("UPDATE prices_daily SET close=close*1.01 WHERE symbol='ACME' AND date=?", (str(ds[-1]),))
    conn.commit()
    v = R.verify(conn, OWNER, rep["report_id"])
    assert not v["match"] and any(d["field"] == "inputs.prices_sha256" for d in v["differences"])


def test_strategy_filter_limits_the_actual_portfolio(temp_db):
    conn, ds = fresh(temp_db)
    L._insert(conn, OWNER, "MANUAL", "t", "a", ds[10], str(ds[10]), "BUY", "ACME", 10, 100, 0, strategy_id="alpha")
    L._insert(conn, OWNER, "MANUAL", "t", "b", ds[10], str(ds[10]), "BUY", "GOLDBEES", 10, 60, 0, strategy_id="beta")
    conn.commit()
    rep = R.build(conn, OWNER, "MANUAL", ds[0], ds[-1], sync_first=False, strategy="alpha")
    assert rep["strategy"] == "alpha" and [p["symbol"] for p in rep["actual"]["positions"]] == ["ACME"]


def test_routes_export_a_preview_and_verify_a_saved_report(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from orders import rules
    conn, ds = fresh(temp_db)
    rules.init_orders_table()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    assert c.post("/api/wealth/performance/ledger", headers=h, json={
        "portfolio": "MANUAL", "trade_date": str(ds[10]), "kind": "BUY", "symbol": "ACME", "quantity": 5,
        "price": 100}).status_code == 200
    body = {"portfolio": "MANUAL", "start": str(ds[0]), "end": str(ds[-1])}
    pv = c.post("/api/wealth/performance/report/export", headers=h, json={**body, "format": "csv"})
    assert pv.status_code == 200 and pv.headers["content-type"].startswith("text/csv") and "# comparison" in pv.text
    assert c.post("/api/wealth/performance/report/export", json=body).status_code == 401
    rid = c.post("/api/wealth/performance/report", headers=h, json=body).json()["report_id"]
    assert c.get(f"/api/wealth/performance/reports/{rid}/verify").json()["match"] is True
    ex = c.get(f"/api/wealth/performance/reports/{rid}/export?format=json")
    assert ex.status_code == 200 and ex.json()["report_id"] == rid


# ── PERF-001-14 edge cases ──────────────────────────────────────────────

def test_a_trade_on_a_market_holiday_counts_on_the_next_session(temp_db):
    conn, ds = fresh(temp_db)
    holiday = ds[50]
    conn.execute("DELETE FROM prices_daily WHERE date=?", (str(holiday),))      # the exchange was shut
    conn.commit()
    _txn(conn, holiday, "BUY", 10, 100)
    a = _run(conn, ds)
    assert str(holiday) not in [x["date"] for x in a["daily"]]
    nxt = next(x for x in a["daily"] if x["date"] == str(ds[51]))
    assert nxt["inflow"] == pytest.approx(1000)


def test_missing_bars_are_reported_not_hidden(temp_db):
    conn, ds = fresh(temp_db)
    conn.execute("DELETE FROM prices_daily WHERE symbol='ACME' AND date>? AND date<=?", (str(ds[100]), str(ds[110])))
    conn.commit()
    _txn(conn, ds[90], "BUY", 10, 100)
    a = _run(conn, ds)
    gaps = [q for q in a["data_quality"] if q["issue"] == "PRICE_GAPS"]
    assert gaps and gaps[0]["detail"].get("ACME", 0) >= 10


def test_a_bonus_issue_keeps_value_and_cost(temp_db):
    conn, ds = fresh(temp_db)
    buy = ds[20]
    px = _close(conn, "ACME", buy)
    conn.execute("INSERT INTO corporate_actions (symbol,ex_date,subject,kind,factor,status,price_factor) VALUES "
                 "('ACME',?,'Bonus 1:1','BONUS',0.5,'adjusted',0.5)", (str(ds[40]),))
    conn.commit()
    _txn(conn, buy, "BUY", 10, px / 0.5)                  # paid the pre-bonus price for pre-bonus shares
    acme = next(p for p in _run(conn, ds)["positions"] if p["symbol"] == "ACME")
    assert acme["quantity"] == pytest.approx(20) and acme["cost_basis"] == pytest.approx(10 * px / 0.5, abs=0.01)


def test_an_opening_position_is_measured_from_that_days_close(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[30], "OPENING", 10, 50, reference_price=_close(conn, "ACME", ds[30]))
    a = _run(conn, ds, start=ds[100])
    exp = _close(conn, "ACME", ds[-1]) / _close(conn, "ACME", ds[99]) - 1
    assert a["period"]["opening_value"] == pytest.approx(10 * _close(conn, "ACME", ds[99]), abs=0.01)
    assert a["returns"]["twr"]["total_return_pct"] == pytest.approx(exp * 100, abs=1e-3)
