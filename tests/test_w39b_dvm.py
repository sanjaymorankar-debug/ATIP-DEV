"""
W39 Phase 2 (item 7, the DVM view left for later): durability, valuation and momentum, 0-100 each, from the
scorecard, the research model and the technical snapshot -- the scores from hand-set inputs, the zone rules,
the explanation each score carries, missing data, the screener fields and presets, the nightly store and the
/research page.
"""
import datetime as dt

import pytest

from research import dvm as D
from research import screener as SC


def _axis(key, passed, known, n=6):
    checks = [{"key": f"{key}{i}", "label": f"{key} check {i}", "detail": f"{key} detail {i}",
               "pass": (i < passed) if i < known else None} for i in range(n)]
    return {"key": key, "label": key.title(), "passed": passed, "known": known, "checks": checks}


def _row(sym="ACME", industry="Capital Goods", health=(5, 6), past=(4, 6), financial=False, **kw):
    r = {"symbol": sym, "industry": industry, "price": 800.0, "fair_value": 1000.0, "pe": 15.0, "pb": 2.0,
         "eps_ttm": 10.0, "book_value_ps": 100.0, "tech_rating": 0.6, "tech_rating_label": "STRONG_BUY", "rs_rating": 80,
         "_scorecard": {"financial": financial, "axes": [_axis("value", 3, 6), _axis("health", *health),
                                                         _axis("past", *past)]}}
    r.update(kw)
    return r


def _peers(industry="Capital Goods", pe=20.0, pb=4.0, n=3):
    return [{"symbol": f"P{i}", "industry": industry, "pe": pe, "pb": pb} for i in range(n)]


def _eval(row, peers=None, **cfg):
    rows = [row] + (_peers() if peers is None else peers)
    return D.evaluate(row, D.context(rows), dict(D.DEFAULTS, **cfg))


def _comp(x, axis, key):
    a = next(a for a in x["axes"] if a["key"] == axis)
    return next(c for c in a["components"] if c["key"] == key)


# ── the scores from hand-set inputs ───────────────────────────────────────

def test_the_three_scores_from_hand_set_inputs():
    x = _eval(_row())
    assert (x["dvm_d"], x["dvm_v"], x["dvm_m"]) == (75.0, 75.0, 80.0)
    assert x["dvm_zone"] == "STRONG_PERFORMER" and x["zone"]["label"] == "Strong performer"
    assert _comp(x, "durability", "health")["score"] == pytest.approx(83.3) and \
        _comp(x, "durability", "past")["score"] == pytest.approx(66.7), "9 of the 12: 5 health + 4 past"
    fv, pe = _comp(x, "valuation", "fair_value_gap"), _comp(x, "valuation", "multiple_vs_industry")
    assert fv["score"] == 75.0 and fv["input"]["discount_pct"] == 20.0 and fv["weight"] == 0.6, "20 % below: 50 + 25"
    assert pe["score"] == 75.0 and pe["input"]["relative"] == 0.75 and pe["weight"] == 0.4, "15x vs 20x: 100 x 0.75"
    assert _comp(x, "momentum", "tech_rating")["score"] == 80.0 and _comp(x, "momentum", "rs_rating")["score"] == 80.0


@pytest.mark.parametrize("price,score", [(1000.0, 50.0), (600.0, 100.0), (1400.0, 0.0), (1200.0, 25.0),
                                         (300.0, 100.0), (900.0, 62.5)])
def test_the_fair_value_part_maps_the_discount_and_clips(price, score):
    x = _eval(_row(price=price, pe=None))
    c = _comp(x, "valuation", "fair_value_gap")
    assert c["score"] == score and x["dvm_v"] == score and c["weight"] == 1.0, "alone when there is no P/E"


@pytest.mark.parametrize("pe,score", [(20.0, 50.0), (10.0, 100.0), (30.0, 0.0), (5.0, 100.0), (25.0, 25.0)])
def test_the_multiple_part_against_the_industry_median(pe, score):
    x = _eval(_row(pe=pe, fair_value=None))
    assert _comp(x, "valuation", "multiple_vs_industry")["score"] == score and x["dvm_v"] == score


def test_banks_are_valued_on_p_b_and_loss_makers_score_zero_on_the_multiple():
    bank = _eval(_row(industry="Financial Services", financial=True, pb=2.0, fair_value=None),
                 _peers("Financial Services", pb=4.0))
    c = _comp(bank, "valuation", "multiple_vs_industry")
    assert c["label"] == "P/B vs its industry" and c["score"] == 100.0 and "P/B 2.00x vs 4.00x" in c["detail"]
    loss = _eval(_row(eps_ttm=-3.0, pe=None))
    c = _comp(loss, "valuation", "multiple_vs_industry")
    assert c["score"] == 0.0 and c["detail"] == "loss-making: no P/E → 0" and loss["dvm_v"] == pytest.approx(45.0)


def test_momentum_maps_the_rating_and_the_rs_rating():
    x = _eval(_row(tech_rating=-1.0, rs_rating=10))
    assert _comp(x, "momentum", "tech_rating")["score"] == 0.0 and x["dvm_m"] == 5.0
    assert _eval(_row(tech_rating=0.0, rs_rating=50))["dvm_m"] == 50.0
    assert _eval(_row(tech_rating=1.0, rs_rating=99))["dvm_m"] == 99.5


# ── zones ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("d,v,m,zone", [
    (55, 55, 55, "STRONG_PERFORMER"), (90, 70, 60, "STRONG_PERFORMER"),
    (30, 80, 40, "VALUE_TRAP"), (34.9, 55, 90, "VALUE_TRAP"),          # first rule wins: V high and D low
    (20, 40, 70, "MOMENTUM_TRAP"),
    (70, 40, 70, "EXPENSIVE_PERFORMER"), (70, 10, 56, "EXPENSIVE_PERFORMER"),
    (70, 70, 40, "VALUE_UNDER_RADAR"), (55, 60, 54.9, "VALUE_UNDER_RADAR"),
    (20, 40, 20, "WEAK"), (34, 54, 34, "WEAK"),
    (35, 80, 40, "MID_RANGE"), (50, 50, 50, "MID_RANGE"), (70, 20, 20, "MID_RANGE"), (35, 35, 35, "MID_RANGE"),
    (None, 60, 60, None), (60, None, 60, None), (60, 60, None, None),
])
def test_zone_rules(d, v, m, zone):
    assert D.zone(d, v, m) == zone


def test_levels_and_configurable_thresholds():
    assert [D.level(x) for x in (None, 34.9, 35, 54.9, 55, 100)] == [None, "LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH"]
    assert D.zone(60, 60, 60, dict(D.DEFAULTS, high=65.0)) == "MID_RANGE"
    assert set(D.ZONES) == {"STRONG_PERFORMER", "VALUE_TRAP", "MOMENTUM_TRAP", "EXPENSIVE_PERFORMER",
                            "VALUE_UNDER_RADAR", "WEAK", "MID_RANGE"}


# ── the explanation ────────────────────────────────────────────────────────

def test_each_score_explains_itself_with_its_numbers():
    x = _eval(_row())
    dur, val, mom = x["axes"]
    assert dur["basis"] == "9 of 12 financial-health and past-performance checks passed"
    health = _comp(x, "durability", "health")
    assert health["input"] == {"passed": 5, "known": 6, "checks": 6} and len(health["checks"]) == 6
    assert health["checks"][0] == {"label": "health check 0", "pass": True, "detail": "health detail 0"}
    assert health["detail"] == "5 of the 6 financial health checks that could be made passed"
    assert _comp(x, "valuation", "fair_value_gap")["detail"] == \
        "₹800.00 vs a fair value of ₹1,000.00 (research model): 20.0 % below → 75"
    assert _comp(x, "valuation", "multiple_vs_industry")["detail"] == \
        "P/E 15.0x vs 20.0x, the median of 3 Capital Goods peers (0.75x) → 75"
    assert val["basis"] == "Price vs fair value 75 × 60 % + P/E vs its industry 75 × 40 %"
    assert "0.600 on -1..+1 (strong buy" in _comp(x, "momentum", "tech_rating")["detail"]
    assert _comp(x, "momentum", "rs_rating")["detail"].startswith("RS rating 80 of 99")
    assert mom["level"] == "HIGH" and x["levels"] == {"high": 55.0, "low": 35.0}
    assert x["zone"]["rule"] == "durability, valuation and momentum all 55+"
    assert x["zone"]["detail"] == "durability 75, valuation 75, momentum 80: durability, valuation and momentum all 55+"
    trap = _eval(_row(health=(1, 6), past=(1, 6)))
    assert trap["dvm_zone"] == "VALUE_TRAP" and trap["zone"]["rule"] == "valuation 55+ but durability below 35"


# ── missing data ───────────────────────────────────────────────────────────

def test_missing_data_is_never_scored_as_a_pass_or_a_guess():
    bare = _eval({"symbol": "TECHONLY", "industry": None, "price": 100.0})
    assert (bare["dvm_d"], bare["dvm_v"], bare["dvm_m"], bare["dvm_zone"]) == (None, None, None, None)
    assert bare["note"] == "no zone: needs all three scores (durability, valuation, momentum missing)"
    assert bare["axes"][0]["basis"] == "no fundamentals stored: no scorecard"
    assert bare["axes"][1]["basis"] == "no fair value and no industry multiple to compare with"
    assert bare["axes"][2]["basis"] == "no technical rating and no RS rating"
    thin = _eval(_row(health=(1, 1), past=(2, 2)))
    assert thin["dvm_d"] is None and thin["axes"][0]["basis"].startswith("only 3 of the 12") and thin["dvm_zone"] is None
    bank = _eval(_row(health=(1, 1), past=(4, 5)))                  # the debt checks are not meaningful for a bank
    assert bank["dvm_d"] == pytest.approx(83.3) and "(6 of the 12 had no data" in bank["axes"][0]["basis"]
    rs_only = _eval(_row(tech_rating=None, tech_rating_label=None))
    assert rs_only["dvm_m"] == 80.0 and _comp(rs_only, "momentum", "rs_rating")["weight"] == 1.0
    assert _comp(rs_only, "momentum", "tech_rating")["weight"] == 0.0
    few = _eval(_row(fair_value=None), _peers(n=2))
    assert few["dvm_v"] is None and "fewer than 3 Capital Goods peers" in \
        _comp(few, "valuation", "multiple_vs_industry")["detail"]
    own = _eval(_row(fair_value=None), [_row("P0", pe=10.0), _row("P1", pe=None), _row("P2", pe=-5.0)])
    assert own["dvm_v"] is None, "the stock itself and peers without a positive P/E are not peers"


def test_apply_scores_every_row_in_place():
    rows = [_row("A"), _row("B", fair_value=None, pe=None, tech_rating=None, rs_rating=None)] + _peers()
    D.apply(rows, D.DEFAULTS)
    assert rows[0]["dvm_zone"] == "STRONG_PERFORMER" and rows[0]["_dvm"]["symbol"] == "A"
    assert rows[1]["dvm_v"] is None and rows[1]["dvm_m"] is None and rows[1]["dvm_zone"] is None
    assert all(k in rows[-1] for k in D.FIELDS)


# ── screener ───────────────────────────────────────────────────────────────

def test_screener_fields_presets_and_columns():
    rows = [{"symbol": "S", "dvm_d": 80.0, "dvm_v": 70.0, "dvm_m": 75.0, "dvm_zone": "STRONG_PERFORMER"},
            {"symbol": "R", "dvm_d": 70.0, "dvm_v": 90.0, "dvm_m": 30.0, "dvm_zone": "VALUE_UNDER_RADAR"},
            {"symbol": "VT", "dvm_d": 20.0, "dvm_v": 80.0, "dvm_m": 40.0, "dvm_zone": "VALUE_TRAP"},
            {"symbol": "MT", "dvm_d": 10.0, "dvm_v": 30.0, "dvm_m": 80.0, "dvm_zone": "MOMENTUM_TRAP"},
            {"symbol": "N", "dvm_d": None, "dvm_v": None, "dvm_m": 60.0, "dvm_zone": None}]
    p = {x["key"]: x for x in SC.PRESETS}
    r = SC.run_screen(None, p["dvm_strong"]["query"], p["dvm_strong"]["sort"], rows=rows)
    assert [x["symbol"] for x in r["rows"]] == ["S"] and r["columns"] == SC.DVM_COLUMNS
    assert [x["symbol"] for x in SC.run_screen(None, p["dvm_value_radar"]["query"], rows=rows)["rows"]] == ["R"]
    t = SC.run_screen(None, p["dvm_traps"]["query"], p["dvm_traps"]["sort"], p["dvm_traps"]["desc"], rows=rows)
    assert [x["symbol"] for x in t["rows"]] == ["MT", "VT"], "weakest durability first"
    assert [x["symbol"] for x in SC.run_screen(None, "dvm_m >= 60", rows=rows)["rows"]] == ["MT", "S", "N"]
    assert SC.field_key("durability") == "dvm_d" and SC.field_key("dvm_valuation") == "dvm_v"
    assert SC.field_key("momentum_score") == "dvm_m" and SC.field_key("dvm") == "dvm_zone"
    assert {SC.FIELDS[k]["group"] for k in D.FIELDS} == {"DVM"} and SC.FIELDS["dvm_zone"]["kind"] == "text"
    assert all(p[k]["group"] == "combined" for k in ("dvm_strong", "dvm_value_radar", "dvm_traps"))
    assert "DVM" in SC.catalog()["groups"]


# ── stored nightly, the API and the page ──────────────────────────────────

@pytest.fixture
def db(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    from research import scorecard as S
    init_db()
    SC.clear_cache()
    conn = get_connection()
    S.ensure_tables(conn)
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map",
                        lambda: {s: "Capital Goods" for s in ("ACME", "BETA", "GAMA", "DELT")})
    yield conn
    conn.close()
    SC.clear_cache()


def _seed(conn):
    today = dt.date.today()
    for sym, eps in (("ACME", 10.0), ("BETA", 8.0), ("GAMA", 8.0), ("DELT", 8.0)):
        for d in (today - dt.timedelta(days=1), today):
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                         "(?,?,160,160,160,160,1000,'dhan')", (sym, str(d)))
        for back, e in ((60, eps), (425, eps * 0.8)):
            conn.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, eps_ttm, book_value_ps, roe, roce, "
                         "debt_equity, revenue_growth_yoy, eps_growth_yoy, profit_growth_yoy, net_margin, shares_out, "
                         "source) VALUES (?,?,?,?,50,0.2,0.25,0.3,12,15,14,0.12,1e8,'nse_xbrl')",
                         (sym, str(today - dt.timedelta(days=back)), str(today - dt.timedelta(days=back)), e))
    conn.execute("INSERT INTO research_report (report_id, symbol, as_of, price, fair_value, rating) VALUES "
                 "('r1', 'ACME', ?, 160, 200, 'BUY')", (str(today),))
    conn.execute("INSERT INTO technical_snapshot (symbol, date, close, tech_rating, tech_rating_label, rs_rating) "
                 "VALUES ('ACME', ?, 160, 0.5, 'BUY', 90)", (str(today),))
    conn.commit()


def test_the_screener_carries_the_dvm_and_the_nightly_job_stores_it(db):
    _seed(db)
    rows = {r["symbol"]: r for r in SC.build_snapshot(db)}
    a = rows["ACME"]
    assert a["dvm_v"] == pytest.approx(0.6 * 75 + 0.4 * 70), "20 % below 200 -> 75; P/E 16x vs the peers' 20x -> 70"
    assert a["dvm_m"] == pytest.approx(0.5 * 75 + 0.5 * 90) and a["dvm_d"] is not None
    assert a["dvm_zone"] == D.zone(a["dvm_d"], a["dvm_v"], a["dvm_m"])
    assert rows["BETA"]["dvm_m"] is None and rows["BETA"]["dvm_zone"] is None, "no technical snapshot"
    x = D.for_symbol(db, "acme")
    assert x["symbol"] == "ACME" and [ax["key"] for ax in x["axes"]] == ["durability", "valuation", "momentum"]
    assert D.for_symbol(db, "NOPE") is None
    out = SC.run_saved_screens()
    assert out["scorecards"] == 4
    got = db.execute("SELECT dvm_d, dvm_v, dvm_m, dvm_zone FROM fundamental_scorecard WHERE symbol='ACME'").fetchone()
    assert tuple(got) == (a["dvm_d"], a["dvm_v"], a["dvm_m"], a["dvm_zone"])
    assert tuple(db.execute("SELECT dvm_m, dvm_zone FROM fundamental_scorecard WHERE symbol='BETA'").fetchone()) == \
        (None, None)


def test_an_old_scorecard_table_gets_the_dvm_columns(tmp_path):
    import sqlite3
    from db.schema_w39 import W39_COLUMNS
    from research import scorecard as S
    assert set(W39_COLUMNS["fundamental_scorecard"]) == set(D.FIELDS)
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE fundamental_scorecard (symbol TEXT NOT NULL, as_of DATE NOT NULL, price REAL, "
                 "checks_passed INTEGER, checks_known INTEGER, value_checks INTEGER, growth_checks INTEGER, "
                 "past_checks INTEGER, health_checks INTEGER, dividend_checks INTEGER, checks_json TEXT, "
                 "created_at TIMESTAMP, PRIMARY KEY (symbol, as_of))")
    conn.commit()
    sc = {"checks_passed": 10, "checks_known": 20, "axes": [_axis(k, 2, 4) for k in ("value", "growth", "past",
                                                                                     "health", "dividend")]}
    sc.update({f"{k}_checks": 2 for k in ("value", "growth", "past", "health", "dividend")})
    S.store(conn, [{"symbol": "OLD", "price": 1.0, "_scorecard": sc, "dvm_d": 50.0, "dvm_v": 60.0, "dvm_m": 70.0,
                    "dvm_zone": "MID_RANGE"}], dt.date(2026, 10, 6))
    assert conn.execute("SELECT dvm_d, dvm_v, dvm_m, dvm_zone FROM fundamental_scorecard").fetchone() == \
        (50.0, 60.0, 70.0, "MID_RANGE")
    conn.close()


def test_dvm_api_and_the_research_page(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from enterprise.authz import permission_for
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    _seed(db)
    client = TestClient(server.app)
    r = client.get("/api/research/dvm/ACME")
    assert r.status_code == 200 and r.json()["dvm_zone"] == D.zone(r.json()["dvm_d"], r.json()["dvm_v"], r.json()["dvm_m"])
    assert [c["key"] for c in r.json()["axes"][1]["components"]] == ["fair_value_gap", "multiple_vs_industry"]
    assert client.get("/api/research/dvm/NOPE").status_code == 404
    assert client.get("/api/research/dvm/bad sym").status_code == 400
    assert permission_for("GET", "/api/research/dvm/ACME") == "research:read"
    page = client.get("/research").text
    assert "function dvmcard" in page and "/api/research/dvm/" in page and 'id="dvd"' in page
