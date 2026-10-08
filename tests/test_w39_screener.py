"""
W39 (SC-20): fundamental screener -- the query language, the per-symbol snapshot, presets,
the magic-formula rank, saved screens with new-match alerts, and the API.
"""
import datetime as dt

import pytest

from research import screener as SC


# ── query language ────────────────────────────────────────────────────────

def test_and_binds_tighter_than_or_and_parentheses_override():
    row = {"roce_pct": 25.0, "debt_equity": 2.0, "pe": 10.0}
    assert SC.matches(SC.parse("pe < 5 OR roce_pct > 20 AND debt_equity < 0.5"), row) is False
    assert SC.matches(SC.parse("(pe < 5 OR roce_pct > 20) AND debt_equity > 1"), row) is True
    assert SC.matches(SC.parse("NOT debt_equity > 1"), row) is False


def test_aliases_strings_and_in():
    row = {"roce_pct": 25.0, "pe": 18.0, "market_cap_cr": 5000.0, "industry": "Capital Goods",
           "research_rating": "BUY", "above_200dma": 1}
    assert SC.matches(SC.parse('ROCE > 20 AND PE <= 18 AND mcap >= 5000'), row)
    assert SC.matches(SC.parse('industry IN ("Banks", "capital goods")'), row), "case-insensitive"
    assert SC.matches(SC.parse("rating = BUY AND above_200dma = 1"), row)
    assert not SC.matches(SC.parse('research_rating != "BUY"'), row)
    assert SC.fields_in(SC.parse("roe > 1 AND (pe < 2 OR roe > 3)")) == ["roe_pct", "pe"]


def test_missing_values_never_match():
    node = SC.parse("roce_pct > 20")
    assert not SC.matches(node, {"roce_pct": None}) and not SC.matches(node, {})
    assert not SC.matches(SC.parse("roce_pct < 20"), {"roce_pct": None})


@pytest.mark.parametrize("bad", ["", "roce_pct >", "nosuch > 1", "roce_pct > abc", "roce_pct > 1 AND",
                                 "(roce_pct > 1", "roce_pct > 1; DROP TABLE prices_daily", "__import__('os')"])
def test_bad_queries_are_refused_not_evaluated(bad):
    with pytest.raises(SC.ScreenError):
        SC.parse(bad)


def test_every_preset_parses_and_sorts_on_a_known_field():
    for p in SC.PRESETS:
        node = SC.parse(p["query"])
        assert all(f in SC.FIELDS for f in SC.fields_in(node)), p["key"]
        assert SC.field_key(p["sort"]) in SC.FIELDS


# ── snapshot on a seeded database ─────────────────────────────────────────

def _prices(conn, sym, closes_by_days_ago):
    today = dt.date.today()
    for days_ago, c in closes_by_days_ago:
        d = today - dt.timedelta(days=days_ago)
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "(?,?,?,?,?,?,1000,'dhan')", (sym, str(d), c, c * 1.02, c * 0.98, c))


def _fund(conn, sym, **kw):
    base = {"quarter": "Q1", "period_end": str(dt.date.today() - dt.timedelta(days=60)), "eps_ttm": 10.0,
            "book_value_ps": 50.0, "roe": 0.2, "roce": 0.25, "debt_equity": 0.3, "revenue_growth_yoy": 12.0,
            "eps_growth_yoy": 15.0, "profit_growth_yoy": 14.0, "net_margin": 0.12, "shares_out": 1e8,
            "source": "nse_xbrl"}
    base.update(kw)
    cols = ["symbol"] + list(base)
    conn.execute(f"INSERT INTO fundamental_data ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [sym] + list(base.values()))


@pytest.fixture
def db(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    init_db()
    SC.clear_cache()
    conn = get_connection()
    SC.ensure_tables(conn)
    _prices(conn, "ACME", [(400, 150.0), (365, 160.0), (200, 260.0), (5, 210.0), (0, 200.0)])
    _fund(conn, "ACME")
    _prices(conn, "CHEAP", [(365, 100.0), (0, 80.0)])
    _fund(conn, "CHEAP", eps_ttm=20.0, roce=0.12, roe=0.13, debt_equity=0.8)
    _prices(conn, "BANKX", [(0, 400.0)])
    _fund(conn, "BANKX", eps_ttm=40.0, roce=None, roe=0.16)
    _prices(conn, "NOFUND", [(0, 50.0)])
    conn.execute("INSERT INTO shareholding_pattern (symbol, as_of, promoter_pct, pledged_pct) VALUES "
                 "('ACME', ?, 55.0, 0.0), ('ACME', ?, 52.5, 0.0)",
                 (str(dt.date.today() - dt.timedelta(days=20)), str(dt.date.today() - dt.timedelta(days=110))))
    conn.commit()
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map",
                        lambda: {"ACME": "Capital Goods", "CHEAP": "Capital Goods", "BANKX": "Financial Services"})
    yield conn
    conn.close()
    SC.clear_cache()


def test_snapshot_fields(db):
    rows = {r["symbol"]: r for r in SC.build_snapshot(db)}
    assert set(rows) == {"ACME", "CHEAP", "BANKX"}, "only stocks with fundamentals and a price"
    a = rows["ACME"]
    assert a["price"] == 200 and a["pe"] == 20.0 and a["pb"] == 4.0 and a["market_cap_cr"] == 2000.0
    assert a["roce_pct"] == 25.0 and a["roe_pct"] == 20.0 and a["net_margin_pct"] == 12.0
    assert a["peg"] == pytest.approx(20 / 15, abs=0.01) and a["earnings_yield_pct"] == 5.0
    assert a["return_1y_pct"] == 25.0, "200 against 160 a year ago"
    assert a["from_52w_high_pct"] == pytest.approx((200 / (260 * 1.02) - 1) * 100, abs=0.01)
    assert a["promoter_pct"] == 55.0 and a["promoter_change_pts"] == 2.5
    assert a["industry"] == "Capital Goods"


def test_magic_rank_excludes_financials_and_combines_both_ranks(db):
    rows = {r["symbol"]: r for r in SC.build_snapshot(db)}
    assert rows["BANKX"]["magic_rank"] is None
    # CHEAP: earnings yield 25% (best), ROCE 12% (worst); ACME: 5% / 25%. Tie on rank sum -> by symbol.
    assert {rows["ACME"]["magic_rank"], rows["CHEAP"]["magic_rank"]} == {1, 2}


def test_run_screen_filters_sorts_and_adds_used_columns(db):
    r = SC.run_screen(db, "roe_pct > 12 AND pe > 0", sort="pe", desc=False, use_cache=False)
    assert [x["symbol"] for x in r["rows"]] == ["CHEAP", "BANKX", "ACME"]
    assert r["count"] == 3 and r["universe"] == 3
    assert "pe" in r["columns"] and "roe_pct" in r["columns"]
    r = SC.run_screen(db, "roce_pct > 20 AND debt_equity < 0.5", use_cache=False)
    assert [x["symbol"] for x in r["rows"]] == ["ACME"]
    r = SC.run_screen(db, "pe > 0", sort="roce_pct", use_cache=False)
    assert r["rows"][-1]["symbol"] == "BANKX", "a missing sort value goes last"
    assert "Symbol" in SC.to_csv(r).splitlines()[0]


def test_the_snapshot_is_cached_per_database(db):
    rows, _ = SC.snapshot(db)
    db.execute("DELETE FROM fundamental_data WHERE symbol='CHEAP'")
    db.commit()
    assert len(SC.snapshot(db)[0]) == len(rows), "served from the cache"
    assert len(SC.snapshot(db, use_cache=False)[0]) == len(rows) - 1


def test_saved_screens_report_new_matches_and_alert(db):
    s = SC.save_screen(db, "Cheap and profitable", "pe < 15 AND roe_pct > 10", sort="pe", desc=False, notify=True)
    with pytest.raises(SC.ScreenError):
        SC.save_screen(db, "broken", "pe <")
    first = SC.run_saved(db, s["screen_id"], use_cache=False)
    assert first["count"] == 2 and first["new"] == [], "first run sets the baseline"
    _prices(db, "NEWCO", [(0, 30.0)])
    _fund(db, "NEWCO", eps_ttm=5.0, roe=0.18)
    db.commit()
    SC.clear_cache()
    out = SC.run_saved_screens()
    assert out == {"status": "SUCCESS", "rows": 1, "alerted": 1, "scorecards": 4, "reason": None}
    msg = db.execute("SELECT message FROM alert_log WHERE category='screener'").fetchone()[0]
    assert "NEWCO" in msg and "Cheap and profitable" in msg
    again = SC.run_saved(db, s["screen_id"], use_cache=False)
    assert again["new"] == [] and again["dropped"] == []
    upd = SC.save_screen(db, "Renamed", "pe < 15", screen_id=s["screen_id"])
    assert upd["name"] == "Renamed" and len(SC.list_screens(db)) == 1
    SC.delete_screen(db, s["screen_id"])
    with pytest.raises(LookupError):
        SC.get_screen(db, s["screen_id"])


# ── API ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def test_screener_api(api):
    client, security = api
    h = {security.TOKEN_HEADER: security.token()}
    cat = client.get("/api/screener/fields").json()
    assert "Valuation" in cat["groups"] and len(cat["presets"]) == len(SC.PRESETS)
    r = client.get("/api/screener/run", params={"query": "roce_pct > 20", "sort": "pe"})
    assert r.status_code == 200 and [x["symbol"] for x in r.json()["rows"]] == ["ACME"]
    bad = client.get("/api/screener/run", params={"query": "nosuch > 1"})
    assert bad.status_code == 400 and "unknown field" in bad.json()["error"]
    assert client.get("/api/screener/run").status_code == 400
    csv = client.get("/api/screener/run.csv", params={"query": "pe > 0"})
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert client.post("/api/screener/saved", json={"name": "x", "query": "pe > 0"}).status_code == 401
    s = client.post("/api/screener/saved", headers=h, json={"name": "x", "query": "pe > 0", "notify": True}).json()
    assert client.get("/api/screener/saved").json()[0]["screen_id"] == s["screen_id"]
    assert client.get(f"/api/screener/saved/{s['screen_id']}/run").json()["count"] == 3
    assert client.post(f"/api/screener/saved/{s['screen_id']}/delete", headers=h).status_code == 200
    assert client.get("/api/screener/saved/nope/run").status_code == 404
    assert client.get("/screener").status_code == 200


def test_screener_permissions_and_classification():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/screener/run") == "dashboard:read"
    assert permission_for("POST", "/api/screener/saved") == "workspace:write"
    assert classify("research_screen") is not None and TABLES["research_screen"] == "OWNER"
