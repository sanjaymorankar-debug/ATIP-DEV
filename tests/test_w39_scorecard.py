"""
W39 Phase 2 (item 7): the explainable fundamental scorecard -- 5 axes x 6 pass / fail checks, each with
the numbers it used; the screener fields; dividends from NSE's corporate-action calendar; the stored
scorecards and their record against the Nifty; the API.
"""
import datetime as dt

import pytest

from research import scorecard as S
from research import screener as SC


def _row(sym, industry="Capital Goods", **kw):
    r = {"symbol": sym, "industry": industry, "price": 100.0, "pe": 25.0, "pb": 4.0, "peg": 2.5, "eps_ttm": 4.0,
         "book_value_ps": 25.0, "eps_growth_pct": 10.0, "revenue_growth_pct": 8.0, "roe_pct": 16.0, "fcf_cr": 100.0,
         "current_ratio": 1.4, "debt_equity": 0.3, "cash_cr": 50.0, "interest_coverage": 8.0, "pledged_pct": 0.0,
         "promoter_pct": 50.0, "dividend_yield_pct": 1.0, "market_cap_cr": 5000.0, "fair_value": None}
    r.update(kw)
    return r


def _universe(*extra):
    peers = [_row(f"P{i:02d}", dividend_yield_pct=0.5 + 0.25 * i) for i in range(12)]
    return peers + list(extra)


def _axis(sc, key):
    a = next(a for a in sc["axes"] if a["key"] == key)
    return {c["key"]: c for c in a["checks"]}, a["passed"]


def _eval(row, rows, hist=(), fund=None, **ctx_kw):
    return S.evaluate(row, list(hist), S.context(rows, **ctx_kw), fund)


# ── the checks ─────────────────────────────────────────────────────────────

def test_value_axis_passes_a_cheap_stock_and_says_why():
    good = _row("GOOD", price=100.0, fair_value=140.0, pe=15.0, pb=2.0, peg=0.75, eps_growth_pct=20.0)
    checks, passed = _axis(_eval(good, _universe(good)), "value")
    assert passed == 6
    assert "28.6 % below" in checks["below_fair_value"]["detail"]
    assert "P/E 15.0x vs 25.0x, the median of 12 Capital Goods peers" == checks["pe_vs_industry"]["detail"]
    dear = _row("DEAR", fair_value=90.0, pe=40.0, pb=6.0, peg=4.0)
    checks, passed = _axis(_eval(dear, _universe(dear)), "value")
    assert passed == 0 and "11.1 % above" in checks["below_fair_value"]["detail"]


def test_a_loss_maker_fails_the_earnings_checks_rather_than_skipping_them():
    loss = _row("LOSS", eps_ttm=-2.0, pe=None, peg=None, eps_growth_pct=-30.0)
    sc = _eval(loss, _universe(loss))
    value, _ = _axis(sc, "value")
    assert value["pe_vs_market"]["pass"] is False and "loss-making" in value["pe_vs_market"]["detail"]
    assert value["peg"]["pass"] is False and value["pe_vs_industry"]["pass"] is False
    growth, _ = _axis(sc, "growth")
    assert growth["self_funded_growth"]["pass"] is False
    div, _ = _axis(sc, "dividend")
    assert div["earnings_cover"]["pass"] is False


def test_growth_axis_compares_with_a_savings_rate_and_the_market():
    fast = _row("FAST", eps_growth_pct=25.0, revenue_growth_pct=22.0, roe_pct=24.0, dividend_yield_pct=0.5, pe=30.0)
    checks, passed = _axis(_eval(fast, _universe(fast)), "growth")
    assert passed == 6
    assert "vs the 7 % risk-free rate" in checks["eps_vs_savings"]["detail"]
    assert "ROE 24.0 % × 85 % of profit kept = 20.4 % a year" == checks["self_funded_growth"]["detail"]
    slow = _row("SLOW", eps_growth_pct=5.0, revenue_growth_pct=3.0, roe_pct=8.0)
    assert _axis(_eval(slow, _universe(slow)), "growth")[1] == 0


def test_dividend_axis_fails_a_non_payer_and_skips_an_unknown():
    nil = _row("NIL", dividend_yield_pct=0.0)
    checks, passed = _axis(_eval(nil, _universe(nil)), "dividend")
    assert passed == 0 and all(c["pass"] is False and c["detail"] == "pays no dividend" for c in checks.values())
    unk = _row("UNK", dividend_yield_pct=None)
    checks, _ = _axis(_eval(unk, _universe(unk)), "dividend")
    assert all(c["pass"] is None for c in checks.values())
    rich = _row("RICH", dividend_yield_pct=3.5, pe=15.0, fcf_cr=400.0)          # payout 52 %, dividends 175 cr
    checks, _ = _axis(_eval(rich, _universe(rich)), "dividend")
    assert checks["notable"]["pass"] and checks["high"]["pass"] and "upper quartile of 13 payers" in checks["high"]["detail"]
    assert checks["earnings_cover"]["pass"] and checks["cash_cover"]["pass"]
    assert checks["steady"]["pass"] is None, "no calendar and no stored quarters"


def test_banks_are_not_scored_on_debt_and_debt_free_companies_pass_the_debt_checks():
    bank = _row("BANKY", industry="Financial Services", debt_equity=8.0, current_ratio=None, pledged_pct=1.0)
    sc = _eval(bank, _universe(bank))
    health, passed = _axis(sc, "health")
    assert sc["financial"] and passed == 1 and health["low_pledge"]["pass"]
    assert all(health[k]["pass"] is None and "bank or NBFC" in health[k]["detail"]
               for k in ("current_ratio", "low_debt", "debt_not_rising", "interest_cover", "fcf_covers_debt"))
    free = _row("FREE", debt_equity=0.0, interest_coverage=None)
    health, passed = _axis(_eval(free, _universe(free)), "health")
    assert passed == 6 and health["interest_cover"]["detail"] == "practically no debt"
    geared = _row("GEAR", debt_equity=1.2, interest_coverage=2.0, pledged_pct=30.0, fcf_cr=10.0)
    health, passed = _axis(_eval(geared, _universe(geared), fund={"debt_cr": 1200.0, "equity_cr": 1000.0}), "health")
    assert passed == 1, "only the current ratio"
    assert health["low_debt"]["detail"] == "net debt ₹1,150 cr = 115 % of equity ₹1,000 cr"
    assert "1 % of debt" in health["fcf_covers_debt"]["detail"]


def test_past_axis_reads_the_stored_quarters():
    d0 = dt.date(2026, 6, 30)
    hist = [{"date": d0, "eps_ttm": 12.0, "net_margin_pct": 15.0, "debt_equity": 0.3, "dividend_yield": 0.01},
            {"date": d0 - dt.timedelta(days=365), "eps_ttm": 8.0, "net_margin_pct": 12.0, "debt_equity": 0.4,
             "dividend_yield": 0.01},
            {"date": d0 - dt.timedelta(days=1096), "eps_ttm": 6.0, "net_margin_pct": 11.0, "debt_equity": 0.5,
             "dividend_yield": 0.01}]
    row = _row("HIST", roe_pct=22.0, eps_growth_pct=50.0)
    checks, passed = _axis(_eval(row, _universe(row), hist), "past")
    assert passed == 6
    assert checks["eps_up_3y"]["detail"] == "EPS (TTM) ₹12.00 vs ₹6.00 three years earlier, 26.0 % a year"
    assert checks["accelerating"]["detail"] == "EPS growth 50.0 % this year vs 26.0 % a year over three"
    hist[1]["eps_ttm"] = 10.0                                    # 20 % this year, below the 26 % three-year rate
    checks, _ = _axis(_eval(row, _universe(row), hist), "past")
    assert checks["accelerating"]["pass"] is False
    health, _ = _axis(_eval(row, _universe(row), hist), "health")
    assert health["debt_not_rising"]["detail"] == "debt / equity 0.30 vs 0.40 a year earlier"
    checks, _ = _axis(_eval(row, _universe(row), []), "past")
    assert checks["eps_up_3y"]["pass"] is None and checks["margin_up"]["pass"] is None


# ── stored data: history, dividends, the screener ─────────────────────────

@pytest.fixture
def db(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    init_db()
    SC.clear_cache()
    conn = get_connection()
    S.ensure_tables(conn)
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: {"ACME": "Capital Goods", "DIVCO": "Capital Goods"})
    yield conn
    conn.close()
    SC.clear_cache()


def _fund(conn, sym, period_end, **kw):
    base = {"quarter": period_end, "period_end": period_end, "eps_ttm": 10.0, "book_value_ps": 50.0, "roe": 0.2,
            "roce": 0.25, "debt_equity": 0.3, "revenue_growth_yoy": 12.0, "eps_growth_yoy": 15.0,
            "profit_growth_yoy": 14.0, "net_margin": 0.12, "shares_out": 1e8, "source": "nse_xbrl"}
    base.update(kw)
    cols = ["symbol"] + list(base)
    conn.execute(f"INSERT INTO fundamental_data ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [sym] + list(base.values()))


def test_load_history_is_point_in_time_and_in_percent(db):
    _fund(db, "ACME", "2026-03-31", net_margin=0.12)
    _fund(db, "ACME", "2026-06-30", net_margin=0.15)
    _fund(db, "ACME", "2026-09-30", net_margin=0.40)             # after the day asked about
    db.commit()
    h = S.load_history(db, ["ACME"], dt.date(2026, 8, 1))["ACME"]
    assert [x["date"] for x in h] == [dt.date(2026, 6, 30), dt.date(2026, 3, 31)]
    assert h[0]["net_margin_pct"] == pytest.approx(15.0)


def test_dividends_from_the_calendar_adjusted_for_a_later_split(db):
    as_of = dt.date(2026, 10, 6)
    ev = [("OLD", as_of - dt.timedelta(days=800), "Annual General Meeting", None, None),
          ("DIVCO", as_of - dt.timedelta(days=400), "Final Dividend - Rs. - 10.0000", None, None),
          ("DIVCO", as_of - dt.timedelta(days=200), "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- "
                                                    "Per Share", "SPLIT", 0.5),
          ("DIVCO", as_of - dt.timedelta(days=100), "Interim Dividend - Rs 2 Per Share / Special Dividend - Rs 3.50 "
                                                    "Per Share", None, None),
          ("DIVCO", as_of - dt.timedelta(days=30), "Dividend - Re 0.50 Per Share", None, None)]
    for sym, d, subject, kind, pf in ev:
        db.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, price_factor) VALUES (?,?,?,?,?)",
                   (sym, str(d), subject, kind, pf))
    db.commit()
    divs, first = S.load_dividends(db, ["DIVCO"], as_of)
    assert first == as_of - dt.timedelta(days=800)
    assert sorted(a for _, a in divs["DIVCO"]) == [0.5, 5.0, 5.5], "10 before a 1:2 split is 5 on today's shares"
    row = _row("DIVCO", price=120.0, dividend_yield_pct=None, pe=12.0)
    sc = _eval(row, _universe(row), divs=divs, div_from=first, as_of=as_of)
    checks, _ = _axis(sc, "dividend")
    assert checks["steady"]["pass"] and checks["growing"]["pass"]
    assert checks["growing"]["detail"].startswith("₹6.00 vs ₹5.00 a share")
    assert "yield 5.00 %" in checks["high"]["detail"], "6 a share over 120, from the calendar"
    sc = _eval(row, _universe(row), divs=divs, div_from=as_of - dt.timedelta(days=200), as_of=as_of)
    checks, _ = _axis(sc, "dividend")
    assert checks["growing"]["pass"] is None and checks["steady"]["pass"] is None, "calendar under 2 years"


def _prices(conn, sym, days, closes):
    for d, c in zip(days, closes):
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "(?,?,?,?,?,?,1000,'dhan')", (sym, str(d), c, c, c, c))


def test_the_screener_carries_the_scorecard_and_the_job_stores_it(db):
    today = dt.date.today()
    for sym, eps in (("ACME", 10.0), ("DIVCO", 20.0)):
        _prices(db, sym, [today - dt.timedelta(days=1), today], [200.0, 200.0])
        _fund(db, sym, str(today - dt.timedelta(days=60)), eps_ttm=eps)
        _fund(db, sym, str(today - dt.timedelta(days=425)), eps_ttm=eps * 0.8, net_margin=0.10, debt_equity=0.5)
    db.commit()
    rows = {r["symbol"]: r for r in SC.build_snapshot(db)}
    a = rows["ACME"]
    assert isinstance(a["checks_passed"], int) and a["checks_passed"] == sum(a[f"{k}_checks"] for k, _ in S.AXES)
    past, _ = _axis(a["_scorecard"], "past")
    assert past["margin_up"]["pass"] and past["margin_up"]["detail"] == "net margin 12.0 % vs 10.0 % a year earlier"
    r = SC.run_screen(db, "checks_passed >= 0", use_cache=False)
    assert r["count"] == 2 and "checks_passed" in r["columns"] and "_scorecard" not in r["rows"][0]
    assert SC.field_key("scorecard") == "checks_passed" and SC.FIELDS["health_checks"]["group"] == "Scorecard"
    assert {"sc_all_rounders", "sc_healthy_growers", "sc_value_quality", "sc_dividend"} <= {p["key"] for p in SC.PRESETS}
    sc = S.for_symbol(db, "acme")
    assert sc["symbol"] == "ACME" and len(sc["axes"]) == 5 and all(len(x["checks"]) == 6 for x in sc["axes"])
    assert S.for_symbol(db, "NOPE") is None
    out = SC.run_saved_screens()
    assert out["scorecards"] == 2 and out["status"] == "SUCCESS"
    stored = db.execute("SELECT symbol, checks_passed, checks_json FROM fundamental_scorecard ORDER BY symbol").fetchall()
    assert [s[0] for s in stored] == ["ACME", "DIVCO"] and stored[0][1] == a["checks_passed"]


def test_record_by_band_one_sample_per_month(db, monkeypatch):
    import pandas as pd
    days = [d.date() for d in pd.bdate_range(end=dt.date.today() - dt.timedelta(days=1), periods=160)]
    _prices(db, "UP", days, [100 * 1.01 ** i for i in range(len(days))])
    _prices(db, "FLAT", days, [100.0] * len(days))
    for d in days:
        db.execute("INSERT INTO market_health (date, nifty_close) VALUES (?, 20000)", (str(d),))
    for d in days[:60]:
        db.execute("INSERT INTO fundamental_scorecard (symbol, as_of, checks_passed) VALUES ('UP', ?, 25), "
                   "('FLAT', ?, 5)", (str(d), str(d)))
    db.commit()
    months = len({str(d)[:7] for d in days[:60]})
    out = S.record(db, 20)
    bands = {b["band"]: b for b in out["bands"]}
    assert bands["21-30"]["n"] == months and bands["0-10"]["n"] == months and bands["11-15"]["n"] == 0
    assert bands["21-30"]["median_excess_pct"] == pytest.approx((1.01 ** 20 - 1) * 100, abs=0.01)
    assert bands["0-10"]["mean_excess_pct"] == 0 and out["top_minus_bottom_pct"] is None, "too few samples"
    monkeypatch.setattr(S, "MIN_RECORD", 2)
    assert S.record(db, 20)["top_minus_bottom_pct"] == pytest.approx((1.01 ** 20 - 1) * 100, abs=0.01)
    with pytest.raises(ValueError):
        S.record(db, 7)


# ── API ───────────────────────────────────────────────────────────────────

def test_scorecard_api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    today = dt.date.today()
    _prices(db, "ACME", [today], [200.0])
    _fund(db, "ACME", str(today - dt.timedelta(days=60)))
    db.commit()
    r = client.get("/api/research/scorecard/ACME")
    assert r.status_code == 200 and [a["key"] for a in r.json()["axes"]] == ["value", "growth", "past", "health",
                                                                            "dividend"]
    assert client.get("/api/research/scorecard/NOPE").status_code == 404
    assert client.get("/api/research/scorecard/bad sym").status_code == 400
    assert client.get("/api/research/scorecard-record", params={"horizon": 7}).status_code == 400
    assert len(client.get("/api/research/scorecard-record").json()["bands"]) == 4
    assert "Fundamental scorecard" in client.get("/research").text


def test_scorecard_permissions_and_classification():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/research/scorecard/ACME") == "research:read"
    assert classify("fundamental_scorecard") is not None and TABLES["fundamental_scorecard"] == "GLOBAL"
