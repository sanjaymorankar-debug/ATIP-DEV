"""
NSE pre-open capture (data/preopen.py): payload parsing, the stale-page guard, the per-symbol merge,
the evaluation against prices_daily / intraday_bars, the track record and the API.
Synthetic payloads in NSE's shape; nothing reaches NSE.
"""
import datetime as dt

import pytest


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from data import preopen as PO
    init_db()
    conn = get_connection()
    PO.ensure_tables(conn)
    yield conn
    conn.close()


def _item(sym, buy, sell, iep=100.0, prev=99.0, when="09-Oct-2026 09:07:58", **md):
    return {"metadata": {"symbol": sym, "previousClose": prev, "iep": iep, "pChange": None, **md},
            "detail": {"preOpenMarket": {"IEP": iep, "prevClose": prev, "totalBuyQuantity": buy,
                                         "totalSellQuantity": sell, "atoBuyQty": 10, "atoSellQty": 5,
                                         "finalQuantity": 1234, "lastUpdateTime": when}}}


def test_parse_reads_the_auction_block_and_labels_the_book():
    from data.preopen import parse
    p = parse(_item("reliance", 3000, 1000, iep=101.0, prev=100.0))
    assert p["symbol"] == "RELIANCE" and p["imbalance"] == 0.5 and p["pressure"] == "STRONG_BUYERS"
    assert p["iep_chg_pct"] == 1.0 and p["final_qty"] == 1234 and p["ato_buy_qty"] == 10
    assert p["nse_time"] == "2026-10-09 09:07:58"
    # NSE's string numbers with thousands separators, and "-" for nothing
    p = parse({"metadata": {"symbol": "TCS", "previousClose": "4,000.00"},
               "detail": {"preOpenMarket": {"IEP": "3,960.00", "totalBuyQuantity": "1,000",
                                            "totalSellQuantity": "3,000", "perChange": "-1.00", "atoBuyQty": "-"}}})
    assert p["iep"] == 3960 and p["prev_close"] == 4000 and p["imbalance"] == -0.5 and p["iep_chg_pct"] == -1.0
    assert p["ato_buy_qty"] is None and p["pressure"] == "STRONG_SELLERS"
    # no price discovered: IEP 0 -> no IEP and no change, the quantities still count
    p = parse(_item("ILLIQ", 0, 500, iep=0))
    assert p["iep"] is None and p["iep_chg_pct"] is None and p["imbalance"] == -1.0
    assert parse({"metadata": {"symbol": "X"}}) is None, "no auction quantities"
    assert parse({"detail": {}}) is None and parse("junk") is None


def test_capture_merges_keys_flags_nifty50_and_refuses_a_stale_page(db):
    from data import preopen as PO
    day = dt.date(2026, 10, 9)
    pages = {"NIFTY": {"data": [_item("INFY", 200, 100)]},
             "FO": {"data": [_item("INFY", 200, 100), _item("PNB", 100, 400), {"bad": 1}]}}
    out = PO.capture(conn=db, day=day, fetch=pages.get)
    assert out["status"] == "SUCCESS" and out["rows"] == 2 and out["nifty50"] == 1 and out["failed_keys"] == []
    rows = {r["symbol"]: r for r in PO.latest(db, day=day)}
    assert rows["INFY"]["in_nifty50"] == 1 and rows["PNB"]["in_nifty50"] == 0
    assert [r["symbol"] for r in PO.latest(db, day=day, side="sell")] == ["PNB"]
    assert [r["symbol"] for r in PO.latest(db, day=day, nifty50=True)] == ["INFY"]
    assert [r["symbol"] for r in PO.latest(db)] == ["PNB", "INFY"], "newest day, most one-sided first"

    yday = {"NIFTY": {"data": [_item("INFY", 1, 1, when="08-Oct-2026 09:07:58")]}, "FO": {"data": []}}
    out = PO.capture(conn=db, day=day, fetch=yday.get)
    assert out["status"] == "STALE" and out["rows"] == 0
    assert PO.latest(db, day=day)[0]["total_buy_qty"] == 100, "a stale page must not overwrite today's rows"

    assert PO.capture(conn=db, day=day, fetch=lambda k: None) == {
        "status": "FAILED", "rows": 0, "failed_keys": ["NIFTY", "FO"], "error": "no pre-open payload from NSE"}


def test_summary_breadth_uses_per_stock_medians(db):
    from data import preopen as PO
    day = dt.date(2026, 10, 9)
    page = {"data": [_item("A", 300, 100, iep=101, prev=100), _item("B", 100, 300, iep=99, prev=100),
                     _item("C", 9_000_000, 1, iep=100, prev=100), _item("D", 120, 100, iep=100.5, prev=100)]}
    PO.capture(conn=db, day=day, fetch=lambda k: page if k == "NIFTY" else {"data": []})
    s = PO.summary(db, day=day)
    a = s["all"]
    assert (a["stocks"], a["advances"], a["declines"], a["unchanged"]) == (4, 2, 1, 1)
    assert a["buy_side_books"] == 2 and a["sell_side_books"] == 1
    assert a["median_imbalance"] == pytest.approx((0.5 + 0.0909) / 2, abs=1e-3)
    assert s["nifty50"]["stocks"] == 4 and s["top_buy_side"][0]["symbol"] == "C"
    assert [r["symbol"] for r in s["top_sell_side"]] == ["B"]
    assert PO.summary(db, day=dt.date(2026, 1, 1)) == {"status": "NO_DATA"}


def test_evaluate_fills_outcomes_waits_for_prices_and_gives_up(db):
    from data import preopen as PO
    d1, d2, old = "2026-10-08", "2026-10-09", "2026-09-20"
    for d, sym, iep in ((d1, "A", 100.0), (d2, "A", 100.0), (old, "A", 50.0)):
        db.execute("INSERT INTO preopen_snapshot (date, symbol, iep, imbalance) VALUES (?,?,?,0.5)", (d, sym, iep))
    db.execute("INSERT INTO prices_daily (symbol, date, open, close) VALUES ('A', ?, 100.5, 102.51)", (d1,))
    db.execute("INSERT INTO intraday_bars (symbol, ts, interval_min, open, close) VALUES "
               "('A', ?, 15, 100.5, 101.0)", (f"{d1} 09:15:00",))
    db.commit()
    out = PO.evaluate(conn=db, today=dt.date(2026, 10, 9))
    assert out == {"status": "SUCCESS", "rows": 1, "waiting": 1, "given_up": 1}
    r = db.execute("SELECT open_price, close_price, iep_error_bps, first15_pct, open_to_close_pct, evaluated_at "
                   "FROM preopen_snapshot WHERE date=?", (d1,)).fetchone()
    assert r[:5] == (100.5, 102.51, 50.0, 0.498, 2.0) and r[5]
    assert db.execute("SELECT evaluated_at FROM preopen_snapshot WHERE date=?", (d2,)).fetchone()[0] is None
    gone = db.execute("SELECT evaluated_at, open_to_close_pct FROM preopen_snapshot WHERE date=?", (old,)).fetchone()
    assert gone[0] and gone[1] is None
    assert PO.evaluate(conn=db, today=dt.date(2026, 10, 9))["status"] == "EMPTY", "d2 still waits"


def _seed_evaluated(db, n_days, per_day, hit_share):
    """per_day one-sided books a day; the first hit_share of them move with the imbalance's sign."""
    base = dt.date(2026, 6, 1)
    k = 0
    for i in range(n_days):
        d = str(base + dt.timedelta(days=i))
        for j in range(per_day):
            imb = 0.5 if j % 2 == 0 else -0.5
            hit = (k % 10) < hit_share * 10
            move = (1.0 if hit else -1.0) * (1 if imb > 0 else -1)
            db.execute("INSERT INTO preopen_snapshot (date, symbol, iep, iep_chg_pct, imbalance, iep_error_bps, "
                       "first15_pct, open_to_close_pct, evaluated_at) VALUES (?,?,100,?,?,0,?,?,'x')",
                       (d, f"S{j}", 0.5 * (1 if imb > 0 else -1), imb, move, move))
            k += 1
    db.commit()


def test_record_needs_enough_observations_and_two_standard_errors(db):
    from data import preopen as PO
    today = dt.date(2026, 10, 9)
    assert PO.record(db, today=today)["status"] == "NO_DATA"
    _seed_evaluated(db, n_days=5, per_day=10, hit_share=0.9)
    r = PO.record(db, days=200, today=today)
    assert r["status"] == "OK" and r["days"] == 5 and r["rows"] == 50
    assert r["imbalance_vs_open_to_close"]["hit_rate"] == 0.9
    assert r["imbalance_vs_open_to_close"]["edge"] == "INSUFFICIENT", "50 < 200 observations"
    assert r["mean_open_to_close_pct"]["buy_side"] > 0 > r["mean_open_to_close_pct"]["sell_side"]
    db.execute("DELETE FROM preopen_snapshot")
    _seed_evaluated(db, n_days=30, per_day=10, hit_share=0.9)
    r = PO.record(db, days=200, today=today)
    assert r["imbalance_vs_first15"]["n"] == 300 and r["imbalance_vs_first15"]["edge"] == "YES"
    assert r["gap_continues_open_to_close"]["hit_rate"] == 0.9
    db.execute("DELETE FROM preopen_snapshot")
    _seed_evaluated(db, n_days=30, per_day=10, hit_share=0.5)
    r = PO.record(db, days=200, today=today)
    assert r["imbalance_vs_open_to_close"]["edge"] == "NO" and r["imbalance_vs_open_to_close"]["hit_rate"] == 0.5
    assert PO.record(db, days=5, today=today)["status"] == "NO_DATA", "outside the window"


def test_settings_defaults_and_overrides(monkeypatch):
    from data import preopen as PO
    from ops import config as C
    monkeypatch.setattr(C, "load", lambda: {"preopen": {"keys": ["nifty", "banknifty"], "junk": 1}})
    assert PO.settings() == {"enabled": True, "keys": ["NIFTY", "BANKNIFTY"]}
    monkeypatch.setattr(C, "load", lambda: {"preopen": {"enabled": False, "keys": []}})
    assert PO.settings() == {"enabled": False, "keys": ["NIFTY", "FO"]}


@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app)


def test_preopen_api(api, db):
    from data import preopen as PO
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert api.get("/api/orderbook/preopen").json() == {"summary": {"status": "NO_DATA"}, "rows": []}
    PO.capture(conn=db, day=dt.date(2026, 10, 9), fetch=lambda k: {"data": [_item("A", 300, 100)]})
    j = api.get("/api/orderbook/preopen", params={"date": "2026-10-09", "side": "buy"}).json()
    assert j["summary"]["status"] == "OK" and [r["symbol"] for r in j["rows"]] == ["A"]
    assert api.get("/api/orderbook/preopen", params={"side": "up"}).status_code == 400
    assert api.get("/api/orderbook/preopen", params={"date": "09-10-2026"}).status_code == 400
    assert api.get("/api/orderbook/preopen/record").json()["status"] == "NO_DATA"
    assert api.post("/api/orderbook/preopen/capture").status_code == 401
    assert permission_for("POST", "/api/orderbook/preopen/capture") == "research:run"
    assert permission_for("GET", "/api/orderbook/preopen") == "dashboard:read"
    assert classify("preopen_snapshot") is not None and TABLES["preopen_snapshot"] == "GLOBAL"


def test_market_pulse_shows_the_preopen_without_a_vote(db):
    from data import preopen as PO
    from research import market_pulse as MP
    MP.ensure_tables(db)
    PO.capture(conn=db, day=dt.date.today(),
               fetch=lambda k: {"data": [_item("A", 300, 100, when=dt.date.today().strftime("%d-%b-%Y 09:07:58"))]})
    p = MP.pulse(db)
    assert p["preopen"]["status"] == "OK" and p["preopen"]["all"]["buy_side_books"] == 1
    assert not any("pre-open" in r for r in p["reasons"])
