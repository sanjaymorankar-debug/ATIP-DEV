"""W39: first automated tests for modules the tracker listed with no test reaching them
(TA-04 / TA-06 / TA-09 / TA-10 / SC-16, EX-12, BT-13, SG-05, DP-21, DP-22). Expected values
are worked out by hand or with plain arithmetic in the test."""

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest


def _daily(n=300, drift=0.001, amp=0.0, seed=None, start=date(2025, 1, 1)):
    ds, d = [], start
    while len(ds) < n:
        if d.weekday() < 5:
            ds.append(d)
        d += timedelta(days=1)
    c = np.array([100 * (1 + drift) ** i * (1 + amp * math.sin(i / 7)) for i in range(n)])
    return pd.DataFrame({"date": ds, "open": c * 0.998, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": np.full(n, 1000.0)})


# ── technical extensions (data/technical_ext.py) ───────────────────────

def test_vwap_is_the_volume_weighted_typical_price():
    from data.technical_ext import vwap, vwap_score
    df = _daily(30)
    df.loc[df.index[-1], "volume"] = 3000.0                       # weight the last bar 3x
    tail = df.tail(20)
    tp = (tail.high + tail.low + tail.close) / 3
    exp = float((tp * tail.volume).sum() / tail.volume.sum())
    out = vwap(df)
    assert out["vwap_20d"] == pytest.approx(round(exp, 2))
    assert out["vwap_dev_pct"] == pytest.approx(round((df.close.iloc[-1] / out["vwap_20d"] - 1) * 100, 3), abs=1e-3)
    assert vwap_score({"vwap_dev_pct": 0}) == 60 and vwap_score({"vwap_dev_pct": -5}) == 35
    assert vwap_score({"vwap_dev_pct": 10}) == pytest.approx(42.0) and vwap_score({}) is None


def test_volume_profile_puts_the_poc_where_the_volume_traded():
    from data.technical_ext import volume_profile
    df = _daily(20, drift=0.0)
    df["low"], df["high"] = 90.0, 110.0
    df.loc[5, ["low", "high", "volume"]] = [99.0, 101.0, 1_000_000.0]     # one heavy session at 100
    vp = volume_profile(df)
    # the heavy session's volume is spread evenly over the 0.83-wide bins it spans (98.3-101.7)
    assert vp["vp_basis"] == "daily" and abs(vp["vp_poc"] - 100) <= 2 * 20 / 24
    assert vp["vp_val"] <= vp["vp_poc"] <= vp["vp_vah"]


def test_support_and_resistance_from_repeated_pivots():
    from data.technical_ext import support_resistance
    n = 120
    base = [100 + 10 * math.sin(i / 5) for i in range(n)]              # swings between ~90 and ~110
    df = pd.DataFrame({"high": [b + 1 for b in base], "low": [b - 1 for b in base], "close": base})
    df.loc[n - 1, "close"] = 100.0
    sr = support_resistance(df)
    assert 88 <= sr["sr_support"] <= 92 and sr["sr_support_touches"] >= 2
    assert 108 <= sr["sr_resistance"] <= 112 and sr["sr_resistance_touches"] >= 2
    assert sr["sr_support_dist_pct"] == pytest.approx((100 / sr["sr_support"] - 1) * 100, abs=0.01)


def test_beta_of_a_twice_as_volatile_stock_is_two():
    from data.technical_ext import betas
    rng = np.random.default_rng(7)
    rb = rng.normal(0, 0.01, 520)
    b = 100 * np.cumprod(1 + rb)
    s = 100 * np.cumprod(1 + 2 * rb)
    ds = pd.bdate_range("2024-01-01", periods=520).date
    out = betas(pd.DataFrame({"date": ds, "close": s}), pd.DataFrame({"date": ds, "close": b}))
    assert out["beta_60"] == pytest.approx(2.0, abs=1e-6) and out["beta_long"] == pytest.approx(2.0, abs=1e-6)
    assert out["beta_downside"] == pytest.approx(2.0, abs=1e-6)
    assert out["bri"] == pytest.approx(0.5 * 100 + 0.3 * 100 + 0.2 * 0, abs=0.01)   # beta 2: level / downside capped


def test_multi_timeframe_alignment_of_a_steady_uptrend():
    from data.technical_ext import multi_timeframe
    up = multi_timeframe(_daily(320, drift=0.002))
    assert up["weekly_trend"] == "UP" and up["monthly_trend"] == "UP" and up["mtf_alignment"] == 3
    down = multi_timeframe(_daily(320, drift=-0.002))
    assert down["mtf_alignment"] == -3 and down["weekly_rsi"] < 30


# ── EX-12 market impact ────────────────────────────────────────────────

def test_impact_follows_the_square_root_law(temp_db):
    from execution import impact as I
    inp = {"ok": True, "price": 100.0, "sigma_daily_bps": 200.0, "adv_shares": 1_000_000, "spread_bps": 10.0}
    a = I.estimate(None, "X", 10_000, inputs=inp, y=0.7)
    b = I.estimate(None, "X", 40_000, inputs=inp, y=0.7)
    assert a["impact_bps"] == pytest.approx(0.7 * 200 * math.sqrt(0.01), abs=0.01)
    assert b["impact_bps"] == pytest.approx(2 * a["impact_bps"], abs=0.02)       # 4x size -> 2x impact
    assert a["total_bps"] == pytest.approx(5 + a["impact_bps"], abs=0.01)
    assert a["cost_rs"] == pytest.approx(a["total_bps"] / 1e4 * 10_000 * 100, abs=0.05)
    assert I.fill_price("BUY", 100, a) > 100 > I.fill_price("SELL", 100, a)
    assert I.horizon_for(I.estimate(None, "X", 300_000, inputs=inp, y=0.7)) == 3
    assert I.estimate(None, "X", 300_000, inputs=inp, y=0.7)["out_of_model"] is True


def test_corwin_schultz_is_zero_without_a_range_and_positive_with_one():
    from execution.impact import corwin_schultz_spread
    flat = [(None, 100, 100, 100, 1)] * 5
    assert corwin_schultz_spread(flat) == 0.0
    wide = [(None, 101, 99, 100, 1), (None, 101.2, 99.1, 100, 1), (None, 100.9, 98.9, 100, 1)]
    assert 0 < corwin_schultz_spread(wide) < 0.05


# ── BT-13 Monte Carlo ──────────────────────────────────────────────────

def test_trade_shuffle_keeps_the_final_return_and_changes_the_path():
    from backtest.montecarlo import return_bootstrap, trade_shuffle
    rets = [5, -3, 4, -2, 6, -4, 3]
    r = trade_shuffle(rets, n_sims=200, seed=1)
    final = math.prod(1 + x / 100 for x in rets) - 1
    assert r["final_return"]["min"] == pytest.approx(final) and r["final_return"]["max"] == pytest.approx(final)
    assert r["max_drawdown"]["min"] < r["max_drawdown"]["max"] <= 0
    assert trade_shuffle(rets, n_sims=50, seed=9) == trade_shuffle(rets, n_sims=50, seed=9)     # reproducible
    bs = return_bootstrap([0.01] * 10, n_sims=20, seed=3)
    assert bs["final_return"]["p50"] == pytest.approx(1.01 ** 10 - 1) and bs["prob_loss"] == 0


# ── SG-05 trade plan ───────────────────────────────────────────────────

def test_trade_plan_levels_and_sizing():
    from scores.predictions import compute_trade_plan
    p = compute_trade_plan("BUY", 200.0, 4.0)        # ATR 2% -> stop 3% (1.5x), inside the 2-6% band
    assert p == {"stop_loss": 194.0, "target_1": 206.0, "target_2": 212.0, "risk_reward": 1.0,
                 "position_size_pct": round(min(10.0, 1.0 / 3.0 * 100), 2)}
    tight = compute_trade_plan("BUY", 200.0, 0.2)    # stop clamped up to 2%, size capped at 10%
    assert tight["stop_loss"] == 196.0 and tight["position_size_pct"] == 10.0
    s = compute_trade_plan("SELL", 200.0, None)      # no ATR -> the minimum stop, mirrored
    assert s["stop_loss"] == 204.0 and s["target_1"] == 194.0
    assert compute_trade_plan("HOLD", 200.0, 4.0)["stop_loss"] is None


# ── DP-21 AMFI NAVs ────────────────────────────────────────────────────

def test_amfi_nav_file_is_parsed_with_amc_and_category():
    from data.multi_asset import parse_amfi
    text = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n\n"
            "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n\nAxis Mutual Fund\n\n"
            "120465;INF846K01DP8;-;Axis Bluechip Fund - Direct Plan - Growth;58.12;06-Oct-2026\n"
            "120466;INF846K01DQ6;;Axis Bluechip Fund - Direct Plan - IDCW;N.A.;06-Oct-2026\n")
    rows = parse_amfi(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["scheme_code"] == "120465" and r["nav"] == 58.12 and r["date"] == "2026-10-06"
    assert r["amc"] == "Axis Mutual Fund" and "Large Cap" in r["category"]


# ── DP-22 point-in-time lake ───────────────────────────────────────────

def test_lake_reads_the_version_known_at_the_time(temp_db, tmp_path, monkeypatch):
    from data import lake
    from db.schema import get_connection, init_db
    init_db()
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    conn = get_connection()
    d = date(2026, 9, 1)
    lake.write("prices_test", d, pd.DataFrame({"sym": ["A"], "px": [100.0]}), knowledge_time=datetime(2026, 9, 1, 18),
               conn=conn)
    lake.write("prices_test", d, pd.DataFrame({"sym": ["A"], "px": [101.0]}), knowledge_time=datetime(2026, 9, 3, 9),
               replace=True, conn=conn)                            # a correction two days later
    assert lake.read("prices_test", conn=conn)["px"].tolist() == [101.0]
    assert lake.read("prices_test", as_of=datetime(2026, 9, 2), conn=conn)["px"].tolist() == [100.0]
    assert lake.read("prices_test", as_of=datetime(2026, 8, 31), conn=conn).empty
    v = lake.verify("prices_test", conn=conn)
    assert v.get("ok", v.get("status") in ("OK", None)) is not False
    with pytest.raises(ValueError):
        lake.write("bad name!", d, pd.DataFrame({"a": [1]}), conn=conn)


# ── DP-05 order-book depth ─────────────────────────────────────────────

def test_depth_quote_parsing_and_daily_features(temp_db):
    from data.depth import features, parse_quote
    from db.schema import get_connection, init_db
    q = {"last_price": 100.05, "depth": {
        "buy": [{"price": 100.0, "quantity": 300, "orders": 3}, {"price": 99.95, "quantity": 100, "orders": 1}],
        "sell": [{"price": 100.10, "quantity": 100, "orders": 2}, {"price": 0, "quantity": 999}]}}
    p = parse_quote(q)
    assert p["best_bid"] == 100.0 and p["best_ask"] == 100.10 and p["mid"] == pytest.approx(100.05)
    assert p["spread_bps"] == pytest.approx(round(0.10 / 100.05 * 1e4, 3))
    assert p["bid_qty_5"] == 400 and p["ask_qty_5"] == 100 and p["imbalance"] == pytest.approx(0.6)
    assert len(p["asks"]) == 1                                    # a zero-price level is dropped
    assert parse_quote({"depth": {"buy": [], "sell": []}}) is None and parse_quote({}) is None
    init_db()
    conn = get_connection()
    for ts, sp, imb in (("2026-10-06 10:00:00", 10.0, 0.2), ("2026-10-06 10:03:00", 20.0, 0.4),
                        ("2026-10-06 10:04:00", 40.0, -0.1)):
        conn.execute("INSERT INTO order_book_snapshot (symbol,ts,spread_bps,imbalance,bid_qty_5,ask_qty_5) VALUES "
                     "('ACME',?,?,?,100,100)", (ts, sp, imb))
    conn.commit()
    f = features(conn, "acme", "2026-10-06")
    # time weights: 180 s, 60 s, and 60 s for the last snapshot
    assert f["tw_spread_bps"] == pytest.approx(round((10 * 180 + 20 * 60 + 40 * 60) / 300, 3))
    assert f["median_imbalance"] == 0.2 and f["snapshots"] == 3
    assert features(conn, "ACME", "2026-10-05") is None


# ── DP-04 ticks -> 1-minute bars ───────────────────────────────────────

def test_ticks_become_minute_bars(temp_db, tmp_path, monkeypatch):
    from data import lake, ticks as T
    from db.schema import get_connection, init_db
    init_db()
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    monkeypatch.setattr(T, "_buf", [])
    conn = get_connection()
    day = str(date.today())
    for t, ltp, cum in (("10:00:05", 100, 1000), ("10:00:40", 102, 1500), ("10:00:59", 101, 1600),
                        ("10:01:10", 99, 2000), ("10:01:50", 100, 2600)):
        T.capture("ACME", {"LTT": t, "LTP": ltp, "LTQ": 1, "volume": cum})
    assert T.flush(conn)["rows"] == 5
    r = T.build_minute_bars(day, conn=conn)
    assert r["status"] == "SUCCESS" and r["rows"] == 2
    bars = conn.execute("SELECT ts, open, high, low, close, volume FROM intraday_bars WHERE symbol='ACME' AND "
                        "interval_min=1 ORDER BY ts").fetchall()
    assert [tuple(b)[1:5] for b in bars] == [(100, 102, 100, 101), (99, 100, 99, 100)]
    assert [b[5] for b in bars] == [0, 1000]          # cumulative volume differenced (first bar has no base)


# ── BT-04 deflated Sharpe ──────────────────────────────────────────────

def test_deflated_sharpe_penalises_many_trials():
    from backtest.optimize import deflated_sharpe
    rng = np.random.default_rng(3)
    best = list(rng.normal(0.001, 0.01, 250))
    few = deflated_sharpe(best, [1.0, 0.5])
    many = deflated_sharpe(best, list(np.linspace(-1.5, 1.5, 200)))
    assert 0 <= many["dsr"] < few["dsr"] <= 1
    assert many["expected_max_sharpe_per_session"] > few["expected_max_sharpe_per_session"]
    assert deflated_sharpe(best[:10], [1, 2])["dsr"] is None and deflated_sharpe(best, [1.0])["dsr"] is None
