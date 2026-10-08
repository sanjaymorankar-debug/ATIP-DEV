"""
W39 (RS): institutional-style valuation, research reports, calls and their hit rate.
Pure maths first, then a report built end to end on a seeded database, then the API.
"""
import datetime as dt
import json
import math

import pytest

from research import valuation as V


# ── valuation maths ───────────────────────────────────────────────────────

def test_constant_growth_dcf_equals_the_gordon_formula():
    """With growth flat at the terminal rate, the two-stage model must collapse to Gordon growth."""
    eps, roe, g, ke = 40.0, 0.20, 0.06, 0.125
    got = V.dcf_value(eps, roe, g, ke, g, years=10)
    want = eps * (1 + g) * (1 - g / roe) / (ke - g)
    assert got == pytest.approx(want, abs=0.01)          # rounded to paise


def test_dcf_refuses_what_it_cannot_value():
    assert V.dcf_value(-5, 0.2, 0.1, 0.12, 0.05) is None, "loss-making"
    assert V.dcf_value(10, 0.2, 0.1, 0.055, 0.05) is None, "ke too close to g"
    assert V.dcf_value(None, 0.2, 0.1, 0.12, 0.05) is None


def test_scenarios_are_ordered_and_weighted():
    f = V.normalise_fundamentals({"eps_ttm": 50, "roe": 0.2, "eps_growth_yoy": 15, "revenue_growth_yoy": 12,
                                  "source": "nse_xbrl"})
    s = V.scenarios(f, 0.125, V.DEFAULTS)
    assert s["bear"]["value"] < s["base"]["value"] < s["bull"]["value"]
    w = V.DEFAULTS["scenario_weights"]
    want = (s["bear"]["value"] * w["bear"] + s["base"]["value"] * w["base"] + s["bull"]["value"] * w["bull"])
    assert s["weighted"] == pytest.approx(want, abs=0.02)


def test_sensitivity_falls_with_cost_of_equity_and_rises_with_terminal_growth():
    f = V.normalise_fundamentals({"eps_ttm": 50, "roe": 0.2, "eps_growth_yoy": 15, "source": "nse_xbrl"})
    g = V.sensitivity(f, 0.125, V.DEFAULTS)
    rows = g["values"]
    assert all(rows[i][2] > rows[i + 1][2] for i in range(4)), "higher ke, lower value"
    assert all(rows[2][j] < rows[2][j + 1] for j in range(4)), "higher terminal growth, higher value"


def test_growth_is_capped_by_roe_and_the_maximum():
    cfg = V.DEFAULTS
    assert V.stage1_growth({"eps_growth_yoy": 80, "roe": 0.5}, cfg) == pytest.approx(0.25)
    assert V.stage1_growth({"eps_growth_yoy": 20, "roe": 0.12}, cfg) == pytest.approx(0.12)
    assert V.stage1_growth({"eps_growth_yoy": -30}, cfg) == 0.0
    assert V.stage1_growth({}, cfg) is None


def test_justified_pb_formula():
    cfg = {**V.DEFAULTS, "terminal_growth_pct": 6.0}
    got = V.justified_pb({"roe": 0.16, "book_value_ps": 500}, 0.13, cfg)
    assert got == pytest.approx((0.16 - 0.06) / (0.13 - 0.06) * 500, abs=0.01)


def test_units_are_normalised_by_source():
    scr = V.normalise_fundamentals({"roe": 18.5, "dividend_yield": 1.2, "source": "screener"})
    assert scr["roe"] == pytest.approx(0.185) and scr["dividend_yield"] == pytest.approx(0.012)
    av = V.normalise_fundamentals({"roe": 0.185, "dividend_yield": 0.012, "eps_growth_yoy": 0.15,
                                   "source": "alpha_vantage"})
    assert av["roe"] == pytest.approx(0.185) and av["dividend_yield"] == pytest.approx(0.012)
    assert av["eps_growth_yoy"] == pytest.approx(15.0)
    xb = V.normalise_fundamentals({"roe": 0.185, "eps_growth_yoy": 1.2, "source": "nse_xbrl"})
    assert xb["eps_growth_yoy"] == pytest.approx(1.2), "XBRL growth is already a percentage"


def test_rating_bands_raise_the_buy_hurdle_with_uncertainty():
    cfg = V.DEFAULTS
    assert V.rating_for(12, "LOW", cfg) == "BUY"
    assert V.rating_for(12, "HIGH", cfg) == "ADD"
    assert V.rating_for(25, "VERY_HIGH", cfg) == "ADD"
    assert V.rating_for(31, "VERY_HIGH", cfg) == "BUY"
    assert V.rating_for(0, "LOW", cfg) == "REDUCE"
    assert V.rating_for(-6, "LOW", cfg) == "SELL"
    assert V.rating_for(None, "LOW", cfg) == "NOT_RATED"


def test_peer_multiple_needs_three_peers_and_ignores_outliers():
    f = {"eps_ttm": 10}
    assert V.peer_multiple_value(f, [{"pe": 20}, {"pe": 22}], False, V.DEFAULTS) is None
    p = V.peer_multiple_value(f, [{"pe": 20}, {"pe": 22}, {"pe": 24}, {"pe": 900}, {"pe": -5}], False, V.DEFAULTS)
    assert p["peers"] == 3 and p["peer_median"] == 22 and p["value"] == 220


def test_financials_use_justified_pb_not_dcf():
    f = V.normalise_fundamentals({"eps_ttm": 80, "book_value_ps": 500, "roe": 0.16, "eps_growth_yoy": 12,
                                  "source": "nse_xbrl"})
    r = V.value_stock(f, 800, industry="Financial Services", beta=1.0)
    assert r["financial"] and "justified_pb" in r["methods"] and "dcf" not in r["methods"]
    assert r["target_price"] == pytest.approx(r["fair_value"] * (1 + r["cost_of_equity_pct"] / 100), abs=0.02)


def test_value_stock_blends_and_rates():
    f = V.normalise_fundamentals({"eps_ttm": 50, "book_value_ps": 300, "roe": 0.2, "eps_growth_yoy": 15,
                                  "revenue_growth_yoy": 12, "source": "nse_xbrl"})
    peers = [{"pe": x} for x in (18, 22, 25, 30, 20)]
    hist = [{"pe": x} for x in (19, 21, 23, 24, 22)]
    r = V.value_stock(f, 600, industry="Capital Goods", beta=1.0, peers=peers, history=hist)
    assert set(r["methods"]) == {"dcf", "peer", "history"}
    assert sum(m["weight"] for m in r["methods"].values()) == pytest.approx(1.0, abs=0.01)
    lo, hi = min(m["value"] for m in r["methods"].values()), max(m["value"] for m in r["methods"].values())
    assert lo <= r["fair_value"] <= hi
    assert r["rating"] in V.RATINGS and r["uncertainty"] in ("LOW", "MEDIUM", "HIGH", "VERY_HIGH")
    empty = V.value_stock({}, 100)
    assert empty["rating"] == "NOT_RATED" and empty["fair_value"] is None


def test_quality_profile_needs_four_quarters():
    assert V.quality_profile([{"roce": 0.3}] * 3)["moat_proxy"] is None
    wide = V.quality_profile([{"roce": 0.28, "debt_equity": 0.1}] * 8)
    assert wide["moat_proxy"] == "WIDE" and wide["quality_score"] > 80
    assert V.quality_profile([{"roce": 0.08}] * 8)["moat_proxy"] == "NONE"


# ── reports on a seeded database ──────────────────────────────────────────

def _seed(conn, sym="ACME", close=500.0, days=800, eps=25.0, bv=150.0, growth=18.0, start=None):
    start = start or dt.date.today() - dt.timedelta(days=days)
    px = close * 0.6
    for i in range(days + 1):
        d = start + dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        px = px * (1 + (close / (close * 0.6)) ** (1 / (days * 5 / 7)) - 1) * (1 + 0.004 * math.sin(i))
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "(?,?,?,?,?,?,?,'dhan')", (sym, str(d), px, px * 1.01, px * 0.99, px, 10000))
    for q in range(8):
        pe = dt.date.today() - dt.timedelta(days=90 * (q + 1))
        conn.execute("INSERT INTO fundamental_data (symbol, quarter, report_date, period_end, eps_ttm, book_value_ps, "
                     "roe, roce, eps_growth_yoy, revenue_growth_yoy, profit_growth_yoy, debt_equity, revenue_cr, "
                     "profit_cr, interest_coverage, shares_out, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sym, f"Q{q}", str(pe), str(pe), eps * (1 - 0.04 * q), bv * (1 - 0.03 * q), 0.18, 0.22,
                      growth, 14.0, 16.0, 0.2, 1200.0, 180.0, 12.0, 1e8, "nse_xbrl"))


@pytest.fixture
def seeded(temp_db):
    from db.schema import get_connection, init_db
    from research.report import ensure_tables
    init_db()
    conn = get_connection()
    ensure_tables(conn)
    _seed(conn, "ACME")
    for i, s in enumerate(("PEER1", "PEER2", "PEER3", "PEER4")):
        _seed(conn, s, close=300 + 40 * i, days=60, eps=15 + i, bv=100)
    conn.execute("INSERT INTO shareholding_pattern (symbol, as_of, promoter_pct, fpi_pct, mf_pct, pledged_pct) "
                 "VALUES ('ACME', ?, 52.0, 20.0, 10.0, 8.0)", (str(dt.date.today() - dt.timedelta(days=10)),))
    conn.execute("INSERT INTO shareholding_pattern (symbol, as_of, promoter_pct, fpi_pct, mf_pct, pledged_pct) "
                 "VALUES ('ACME', ?, 50.0, 17.0, 9.0, 8.0)", (str(dt.date.today() - dt.timedelta(days=200)),))
    conn.commit()
    yield conn
    conn.close()


IND = {"ACME": "Capital Goods", "PEER1": "Capital Goods", "PEER2": "Capital Goods", "PEER3": "Capital Goods",
       "PEER4": "Capital Goods"}


def test_report_end_to_end(seeded):
    from research import report as R
    uni = R.Universe(seeded, industry_map=IND)
    rep = R.build_report(seeded, "acme", uni, cfg={})
    assert rep["symbol"] == "ACME" and rep["price"] and rep["industry"] == "Capital Goods"
    assert {"dcf", "peer", "history"} <= set(rep["valuation"]["methods"]), rep["valuation"]["methods"]
    assert len(rep["peers"]) == 4 and all(p["pe"] for p in rep["peers"])
    assert rep["rating"] in V.RATINGS and rep["target_price"] and rep["fair_value"]
    assert rep["price_stats"]["history_years"] >= 2 and rep["price_stats"]["return_1y_pct"] is not None
    assert any("pledged" in r for r in rep["risks"]), rep["risks"]
    assert any("Promoters raised" in t for t in rep["thesis"]), rep["thesis"]
    assert any("not registered with SEBI" in d for d in rep["disclosures"])
    assert any("does not hold" in d for d in rep["disclosures"])
    json.dumps(rep, default=str)
    hist = [h for h in R.gather(seeded, "ACME", uni)["history"] if h["pe"]]
    assert len(hist) >= 4, "own-history P/E is priced at each quarter end"


def test_a_single_report_reads_only_the_stock_and_its_peers(seeded):
    from research import report as R
    _seed(seeded, "OTHER", days=30)
    uni = R.for_symbol(seeded, "acme", industry_map={**IND, "OTHER": "Banks"})
    assert set(uni.prices) == set(uni.fund) == {"ACME", "PEER1", "PEER2", "PEER3", "PEER4"}
    alone = R.for_symbol(seeded, "OTHER", industry_map={})
    assert set(alone.prices) == {"OTHER"}


def test_calls_are_rating_or_target_changes_and_targets_are_tracked(seeded):
    from research import report as R
    today = dt.date.today()
    base = {"symbol": "ACME", "price": 100.0, "fair_value": 110.0, "upside_pct": 20.0, "uncertainty": "LOW",
            "valuation": {"model_version": "t"}, "quality": {}, "atip_scores": {}}
    d0 = today - dt.timedelta(days=400)
    assert R.save_report(seeded, {**base, "as_of": str(d0), "rating": "BUY", "target_price": 120.0}) is True
    assert R.save_report(seeded, {**base, "as_of": str(d0 + dt.timedelta(days=1)), "rating": "BUY",
                                  "target_price": 122.0}) is False, "target moved < 5%: not a call"
    assert R.save_report(seeded, {**base, "as_of": str(d0 + dt.timedelta(days=2)), "rating": "SELL",
                                  "target_price": 90.0}) is True
    seeded.execute("DELETE FROM prices_daily WHERE symbol='ACME'")
    for i, (hi, lo, cl) in enumerate([(105, 95, 100), (110, 99, 105), (125, 104, 121)]):
        seeded.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                       "('ACME',?,?,?,?,?,1,'dhan')", (str(d0 + dt.timedelta(days=5 + i)), cl, hi, lo, cl))
    seeded.commit()
    out = R.evaluate_targets(seeded, today)
    rows = {r[0]: r[1:] for r in seeded.execute(
        "SELECT rating, outcome_status, outcome_return_pct FROM research_report WHERE is_call=1")}
    assert rows["BUY"][0] == "HIT" and rows["BUY"][1] == pytest.approx(21.0)
    assert rows["SELL"][0] == "MISSED", "never fell to 90 within the year"
    assert out == {"HIT": 1, "MISSED": 1}
    hr = R.hit_rate(seeded)
    assert hr["BUY"]["success_rate_pct"] == 100.0 and hr["SELL"]["success_rate_pct"] == 0.0
    assert [h["rating"] for h in R.history(seeded, "ACME")] == ["SELL", "BUY"]


def test_a_split_after_the_call_does_not_fake_a_miss(seeded):
    from research import report as R
    d0 = dt.date.today() - dt.timedelta(days=30)
    R.save_report(seeded, {"symbol": "SPLT", "as_of": str(d0), "price": 200.0, "fair_value": 220.0,
                           "target_price": 240.0, "upside_pct": 20.0, "rating": "BUY", "uncertainty": "LOW",
                           "valuation": {"model_version": "t"}, "quality": {}, "atip_scores": {}})
    seeded.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, factor, status, price_factor) "
                   "VALUES ('SPLT', ?, 'Split 2:1', 'SPLIT', 0.5, 'adjusted', 0.5)", (str(d0 + dt.timedelta(days=3)),))
    seeded.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                   "('SPLT',?,121,122,119,121,1,'dhan')", (str(d0 + dt.timedelta(days=10)),))
    seeded.commit()
    R.evaluate_targets(seeded)
    st = seeded.execute("SELECT outcome_status FROM research_report WHERE symbol='SPLT'").fetchone()[0]
    assert st == "HIT", "122 on the post-split basis is 244 before it"


def test_run_reports_writes_a_row_per_priced_symbol(seeded, monkeypatch):
    from research import report as R
    monkeypatch.setattr(R, "settings", lambda: {"reports_enabled": True, "valuation": {}})
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: IND)
    out = R.run_reports(["ACME", "PEER1", "NOPRICE"])
    assert out["status"] == "SUCCESS" and out["rows"] == 2 and out["calls"] >= 1
    assert {r["symbol"] for r in R.latest_ratings(seeded)} == {"ACME", "PEER1"}
    assert R.stored_report(seeded, "ACME")["symbol"] == "ACME"


# ── API ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch, seeded):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: IND)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def test_research_api(api):
    client, security = api
    h = {security.TOKEN_HEADER: security.token()}
    r = client.get("/api/research/equity/ACME")
    assert r.status_code == 200 and r.json()["symbol"] == "ACME"
    assert client.get("/api/research/equity/NOSUCH").status_code == 404
    assert client.get("/api/research/equity/bad%20sym").status_code == 400
    assert client.post("/api/research/equity/ACME/refresh").status_code == 401, "token required"
    r = client.post("/api/research/equity/ACME/refresh", headers=h)
    assert r.status_code == 200 and r.json()["is_call"] is True
    assert client.get("/api/research/equity").json()[0]["symbol"] == "ACME"
    assert client.get("/api/research/equity/ACME/history").json()[0]["rating"] == r.json()["rating"]
    assert client.get("/api/research/hit-rate").status_code == 200
    assert client.get("/research").status_code == 200


def test_w39_routes_map_to_the_intended_permissions():
    from enterprise.authz import permission_for
    assert permission_for("GET", "/api/research/equity/ACME") == "research:read"
    assert permission_for("POST", "/api/research/equity/ACME/refresh") == "research:run"
    assert permission_for("GET", "/api/data/history/coverage") == "dashboard:read"
    assert permission_for("POST", "/api/data/history/backfill") == "research:run"
    assert permission_for("POST", "/api/options/analyse") == "research:run"
    assert permission_for("GET", "/api/options/templates") == "dashboard:read"


def test_new_tables_are_classified():
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    for t in ("research_report", "prices_daily_backfill"):
        assert classify(t) is not None and TABLES[t] == "GLOBAL"
