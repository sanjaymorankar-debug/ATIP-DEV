"""
W39 (MP / OB): global-cue model, GIFT gap capture and evaluation, FII flow pressure, derivatives
positioning, OI walls, order-book pressure, NSE participant OI parsing, the read-only open-order
view, and the API. Synthetic data with a known answer; nothing reaches Dhan, NSE or Yahoo.
"""
import datetime as dt

import numpy as np
import pytest


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from data import order_pressure as OP, participant_oi as PO
    from research import market_pulse as MP
    init_db()
    conn = get_connection()
    for m in (OP, PO, MP):
        m.ensure_tables(conn)
    yield conn
    conn.close()


# ── global cues ───────────────────────────────────────────────────────────

def _seed_global(conn, n=320, beta=0.5, seed=7):
    """US moves on day d; the Nifty on the next business day = beta x that move + noise."""
    rng = np.random.default_rng(seed)
    days = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(int(n * 1.5))]
    days = [d for d in days if d.weekday() < 5][:n]
    sp, nifty = 4000.0, 20000.0
    us_ret = rng.normal(0, 0.01, n)
    for i, d in enumerate(days):
        sp *= np.exp(us_ret[i])
        conn.execute("INSERT INTO global_market_history (series, date, close) VALUES ('sp500',?,?)", (str(d), sp))
        for s, v in (("nasdaq", 100 + i * 0.01), ("crude_brent", 80 + rng.normal(0, 1)), ("us_10y", 4 + rng.normal(0, .05))):
            conn.execute("INSERT INTO global_market_history (series, date, close) VALUES (?,?,?)", (s, str(d), v))
        if i:
            nifty *= np.exp(beta * us_ret[i - 1] + rng.normal(0, 0.004))
        conn.execute("INSERT INTO global_market_history (series, date, close) VALUES ('nifty50',?,?)", (str(d), nifty))
    conn.commit()
    return days, us_ret


def test_global_model_recovers_a_planted_us_effect(db):
    from research import market_pulse as MP
    days, us = _seed_global(db)
    m = MP.global_cue_model(db)
    assert m["status"] == "OK" and m["observations"] > 250
    sens = m["sensitivity"]["S&P 500"]
    assert sens["value"] == pytest.approx(0.5, abs=0.12) and sens["unit"] == "Nifty % per 1% move"
    assert m["walk_forward"]["beats_zero"] and m["walk_forward"]["direction_hit_rate_pct"] > 70
    top = next(iter(m["contributions_pct"]))
    assert top == "S&P 500", "the planted factor dominates today's contribution"
    assert np.sign(m["contributions_pct"]["S&P 500"]) == np.sign(us[-1])
    assert m["cue_label"] in ("STRONG_POSITIVE", "POSITIVE", "FLAT", "NEGATIVE", "STRONG_NEGATIVE")


def test_a_factor_that_stops_updating_is_left_out_not_repeated(db):
    from research import market_pulse as MP
    days, _ = _seed_global(db)
    db.execute("DELETE FROM global_market_history WHERE series='crude_brent' AND date>?", (str(days[-40]),))
    db.commit()
    m = MP.global_cue_model(db)
    assert m["status"] == "OK" and m["stale_factors"] == ["Brent crude"]
    assert "Brent crude" not in m["contributions_pct"] and "Brent crude" not in m["corr_60d"]
    assert all(v is None or abs(v) <= 1 for v in m["corr_60d"].values())


def test_global_model_says_when_history_is_short(db):
    from research import market_pulse as MP
    _seed_global(db, n=40)
    assert MP.global_cue_model(db)["status"] == "INSUFFICIENT"


def test_gift_gap_is_measured_from_yesterdays_1530_gift_and_checked_at_the_open(db):
    from research import market_pulse as MP
    today, prev = dt.date.today(), dt.date.today() - dt.timedelta(days=1)
    db.execute("INSERT INTO index_levels (date, time, gift_nifty, nifty50) VALUES (?, '15:30', 24100, 24000)", (str(prev),))
    db.execute("INSERT INTO index_levels (date, time, gift_nifty, nifty50) VALUES (?, '23:00', 24500, 24000)", (str(prev),))
    db.commit()
    out = MP.capture_gift(db, quotes={"gift_nifty": {"ltp": 24341.0}, "nifty50": {"prev_close": 24000.0}})
    assert out["gift_move_pct"] == pytest.approx(1.0, abs=1e-6), "against 15:30, not the 23:00 reading or the spot close"
    row = db.execute("SELECT expected_gap_pct, expected_gap_pts, gift_ref_source FROM market_cue").fetchone()
    assert row[0] == pytest.approx(1.0) and row[1] == pytest.approx(240.0) and "15:30" in row[2]
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES (?, '09:15', 24180)", (str(today),))
    db.commit()
    assert MP.evaluate_gaps(db)["rows"] == 1
    assert db.execute("SELECT actual_gap_pct FROM market_cue").fetchone()[0] == pytest.approx(0.75)
    rec = MP.gap_record(db)
    assert rec["gift"]["mornings"] == 1 and rec["gift"]["direction_hit_rate_pct"] == 100.0


# ── FII flows and positioning ─────────────────────────────────────────────

def test_fii_pressure_streak_absorption_and_label(db):
    from research import market_pulse as MP
    start = dt.date(2026, 8, 1)
    for i in range(60):
        d = start + dt.timedelta(days=i)
        fii = 500.0 if i < 45 else -3000.0
        dii = 200.0 if i < 45 else 2400.0
        db.execute("INSERT INTO fii_dii_market (date, fii_net_cr, dii_net_cr) VALUES (?,?,?)", (str(d), fii, dii))
    db.commit()
    f = MP.fii_pressure(db)
    assert f["status"] == "OK" and f["selling_streak_days"] == 15 and f["fii_5d_cr"] == -15000
    assert f["dii_absorption_20d"] == pytest.approx(0.8) and f["pressure_label"] in ("OUTFLOW", "STRONG_OUTFLOW")
    assert f["last_10"][0]["fii_net_cr"] == -3000


def test_buildup_truth_table():
    from research.market_pulse import buildup
    assert buildup(1.0, 5.0) == "LONG_BUILDUP" and buildup(-1.0, 5.0) == "SHORT_BUILDUP"
    assert buildup(1.0, -5.0) == "SHORT_COVERING" and buildup(-1.0, -5.0) == "LONG_UNWINDING"
    assert buildup(0.1, 5.0) == "NONE" and buildup(1.0, 1.0) == "NONE" and buildup(None, 3) == "NONE"


def test_positioning_reads_a_crowded_short_only_as_bullish_when_covering(db):
    from research import market_pulse as MP
    base = dt.date(2026, 9, 1)
    for i, longs in enumerate([10, 10, 10, 10, 10, 11, 17]):          # long % 9.1 ... 14.5: +5.4 pp in 5 days
        db.execute("INSERT INTO fo_participant_oi (date, client_type, fut_idx_long, fut_idx_short, opt_idx_call_long, "
                   "opt_idx_call_short, opt_idx_put_long, opt_idx_put_short) VALUES (?, 'FII', ?, 100, 5, 7, 9, 2)",
                   (str(base + dt.timedelta(days=i)), longs))
    for d, px, oi in (("2026-09-06", 24000, 1_000_000), ("2026-09-07", 24300, 950_000)):
        db.execute("INSERT INTO fo_underlying_daily (date, symbol, kind, fut_close, fut_oi, pcr_oi) VALUES "
                   "(?, 'NIFTY', 'INDEX', ?, ?, 0.9)", (d, px, oi))
    db.commit()
    p = MP.positioning(db)
    assert p["fii"]["index_futures_long_pct"] == pytest.approx(14.5, abs=0.1)
    assert p["nifty_futures"]["buildup"] == "SHORT_COVERING"
    assert p["read"] == "CROWDED_SHORT_COVERING"
    db.execute("UPDATE fo_underlying_daily SET fut_oi=1100000 WHERE date='2026-09-07'")   # now long build-up
    db.commit()
    assert MP.positioning(db)["read"] == "CROWDED_SHORT", "short but not covering: no bounce call"


def test_oi_walls(db):
    from research import market_pulse as MP
    for strike, typ, oi in ((23800, "PE", 900), (23900, "PE", 1500), (24100, "CE", 700), (24300, "CE", 2000),
                            (24500, "CE", 1200), (24200, "PE", 9999)):
        db.execute("INSERT INTO fo_contract_daily (date, symbol, instrument, expiry, strike, option_type, oi, underlying) "
                   "VALUES ('2026-10-06','NIFTY','IDO','2026-10-13',?,?,?,24010)", (strike, typ, oi))
    db.commit()
    w = MP.oi_walls(db)["NIFTY"]
    assert w["support"]["strike"] == 23900, "the 24200 put is above spot: not support"
    assert w["resistance"]["strike"] == 24300


# ── order book ────────────────────────────────────────────────────────────

def test_order_pressure_parse_and_label():
    from data import order_pressure as OP
    q = {"last_price": 101.0, "ohlc": {"close": 100.0}, "volume": 5e5, "buy_quantity": 300000, "sell_quantity": 100000,
         "depth": {"buy": [{"price": 100.9, "quantity": 500, "orders": 3}],
                   "sell": [{"price": 101.1, "quantity": 300, "orders": 2}]}}
    p = OP.parse(q)
    assert p["total_imbalance"] == 0.5 and p["pressure"] == "STRONG_BUYERS" and p["chg_pct"] == 1.0
    assert p["top5_imbalance"] == pytest.approx(0.25)
    assert OP.label(0.0) == "BALANCED" and OP.label(-0.2) == "SELLERS" and OP.label(-0.5) == "STRONG_SELLERS"
    assert OP.parse({"last_price": 5}) is None


def test_persistent_and_latest(db):
    from data import order_pressure as OP
    day = dt.date.today()
    polls = {"BUYCO": [0.3, 0.25, 0.05, 0.4, 0.35],       # last 4 newest first: .35 .4 .05 .25 -> 3 buyers
             "FADECO": [-0.25, -0.3, -0.28, 0.1, 0.0],    # oldest seller reading dropped out: 2 of 4
             "FLIPCO": [-0.3, -0.3, -0.3, -0.3, 0.3],     # 3 of 4 sellers, but the newest has flipped
             "DUMPCO": [-0.1, -0.25, -0.5, -0.3, -0.21]}
    for sym, xs in polls.items():
        for k, x in enumerate(xs):
            db.execute("INSERT INTO order_book_pressure (symbol, ts, total_buy_qty, total_sell_qty, total_imbalance) "
                       "VALUES (?, ?, 1, 1, ?)", (sym, f"{day} 1{k}:00:00", x))
    db.commit()
    assert OP.persistent(db) == {"BUYCO": "BUYERS", "DUMPCO": "SELLERS"}
    top = OP.latest(db, side="buy")
    assert [(r["symbol"], r["total_imbalance"]) for r in top] == [("BUYCO", 0.35), ("FLIPCO", 0.3)]
    assert top[0]["persistent"] == "BUYERS" and top[1]["persistent"] is None
    assert [r["symbol"] for r in OP.latest(db, side="sell")] == ["DUMPCO"]
    assert [r["symbol"] for r in OP.latest(db)][-1] == "FADECO", "most one-sided first; FADECO is 0.0"
    assert OP.latest(db, day=day - dt.timedelta(days=1)) == []


def test_participant_oi_parse():
    from data.participant_oi import parse
    text = ('"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 06, 2026"\n'
            "Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short\t,Option Index Call "
            "Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option "
            "Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts\t,Total Short Contracts\n"
            "Client,1000,2000,3,4,5,6,7,8,9,10,11,12,13,14\n"
            "FII,\"1,500\",15000,3,4,5,6,7,8,9,10,11,12,13,14\nTOTAL,1,1,1,1,1,1,1,1,1,1,1,1,1,1\n")
    rows = {r["client_type"]: r for r in parse(text)}
    assert set(rows) == {"Client", "FII", "TOTAL"}
    assert rows["FII"]["fut_idx_long"] == 1500 and rows["FII"]["fut_idx_short"] == 15000
    assert rows["Client"]["total_long"] == 13 and rows["Client"]["total_short"] == 14
    assert parse("<html>not found</html>") == []


# ── your open orders ──────────────────────────────────────────────────────

def test_open_orders_keeps_only_open_statuses(monkeypatch):
    from data import dhan as D
    from portfolio import open_orders as OO

    class Fake:
        def get_order_list(self):
            return {"status": "success", "data": [
                {"orderId": "1", "tradingSymbol": "TCS", "orderStatus": "PENDING", "quantity": 5, "price": 3500},
                {"orderId": "2", "tradingSymbol": "INFY", "orderStatus": "TRADED"},
                {"orderId": "3", "tradingSymbol": "SBIN", "orderStatus": "PART_TRADED", "filledQty": 10}]}

        def get_forever(self):
            return {"status": "success", "data": [{"orderId": "9", "tradingSymbol": "ITC", "orderStatus": "PENDING",
                                                   "triggerPrice": 400}]}
    monkeypatch.setattr(D, "HAS_DHAN", True)
    out = OO.dhan_open_orders(Fake())
    assert out["status"] == "OK" and [o["order_id"] for o in out["orders"]] == ["1", "3", "9"]
    assert out["orders"][2]["kind"] == "forever" and out["orders"][2]["trigger_price"] == 400

    class Broken:
        def get_order_list(self):
            return {"status": "failure", "remarks": {"error_code": "DH-901"}}
    bad = OO.dhan_open_orders(Broken())
    assert bad["status"] == "FAILED" and "DH-901" in bad["errors"][0]


# ── pulse and API ─────────────────────────────────────────────────────────

def test_pulse_combines_the_parts(db):
    from research import market_pulse as MP
    _seed_global(db)
    db.execute("INSERT INTO market_health (date, regime, mh_score) VALUES (?, 'BEAR', 35)", (str(dt.date.today()),))
    db.commit()
    p = MP.pulse(db)
    assert p["global_model"]["status"] == "OK" and p["context"] in ("RISK_ON", "NEUTRAL", "RISK_OFF")
    assert any("regime BEAR" in r for r in p["reasons"])


@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from portfolio import open_orders as OO
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(OO, "dhan_open_orders", lambda dhan=None: {"broker": "dhan", "status": "UNAVAILABLE",
                                                                    "reason": "test", "orders": []})
    return TestClient(server.app), security


def test_market_pulse_api(api):
    client, security = api
    assert client.get("/api/market-pulse").status_code == 200
    assert client.get("/api/market-pulse/global").json()["status"] == "INSUFFICIENT"
    assert client.get("/api/market-pulse/fii").status_code == 200
    assert client.get("/api/market-pulse/positioning").status_code == 200
    assert client.get("/api/orderbook/pressure").json() == []
    assert client.get("/api/orderbook/pressure", params={"side": "up"}).status_code == 400
    w = client.get("/api/brokers/open-orders").json()
    assert w["broker"]["status"] == "UNAVAILABLE" and "order_rules" in w["atip"]
    for u in ("/api/market-pulse/gift", "/api/market-pulse/refresh", "/api/orderbook/snapshot"):
        assert client.post(u).status_code == 401
    assert client.get("/market-pulse").status_code == 200


def test_permissions_and_tables():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/market-pulse") == "dashboard:read"
    assert permission_for("POST", "/api/market-pulse/gift") == "research:run"
    assert permission_for("POST", "/api/orderbook/snapshot") == "research:run"
    assert permission_for("GET", "/api/brokers/open-orders") == "portfolio:read"
    for t in ("order_book_pressure", "fo_participant_oi", "market_cue"):
        assert classify(t) is not None and TABLES[t] == "GLOBAL"
