"""W39: TA-08b sector-relative strength and TA-05 swing-anchored Fibonacci (data/technical_ext.py),
stored in technical_ext and shown in the stock panel, never scored. Expected values are worked out
by hand or with plain arithmetic in the test."""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

NEW_RS = ("rs_sector_index", "rs_sector_63", "rs_sector_126", "rs_sector_pctile")
NEW_FIB = ("fib_swing_high", "fib_swing_low", "fib_swing_dir", "fib_382", "fib_500", "fib_618",
           "fib_nearest", "fib_nearest_dist_pct")


def _days(n, end=date(2026, 9, 30)):
    ds, d = [], end
    while len(ds) < n:
        if d.weekday() < 5:
            ds.append(d)
        d -= timedelta(days=1)
    return ds[::-1]


def _ramp(n, start, end, flat_before=0, flat_value=None):
    """n closes: `flat_before` bars at flat_value, then a straight line start -> end over the rest."""
    body = np.linspace(start, end, n - flat_before)
    head = np.full(flat_before, flat_value if flat_value is not None else start)
    return np.concatenate([head, body])


def _frame(closes, dates=None):
    c = np.asarray(closes, dtype=float)
    dates = dates or _days(len(c))
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": np.full(len(c), 1000.0)})


def _series(closes, dates=None):
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": dates or _days(len(c)), "close": c})


def _path(points, n):
    """Piecewise-linear mid prices through (bar, price) points; high / low = mid +/- 1."""
    m = np.interp(np.arange(n), [p[0] for p in points], [p[1] for p in points])
    return pd.DataFrame({"high": m + 1, "low": m - 1, "close": m})


# ── TA-08b mapping ─────────────────────────────────────────────────────

def test_industry_to_sector_index_mapping_and_psu_bank_override():
    from data.dhan import INDEX_SERIES_SYMBOLS
    from data.technical_ext import (FALLBACK_SECTOR_INDEX, SECTOR_INDEX_BY_INDUSTRY,
                                    SECTOR_INDEX_SYMBOL_OVERRIDES, sector_index_for)
    assert sector_index_for("INFY", "Information Technology") == "NIFTYIT"
    assert sector_index_for("INFY", "  INFORMATION TECHNOLOGY ") == "NIFTYIT"        # case / spaces
    assert sector_index_for("SUNPHARMA", "Healthcare") == "NIFTYPHARMA"
    assert sector_index_for("NTPC", "Power") == "NIFTYENERGY"
    assert sector_index_for("HDFCBANK", "Financial Services") == "NIFTYBANK"
    assert sector_index_for("SBIN", "Financial Services") == "NIFTYPSUBANK"           # PSU bank override
    assert sector_index_for("LT", "Capital Goods") is None                             # no stored index
    assert sector_index_for("XYZ", None) is None
    stored = set(INDEX_SERIES_SYMBOLS.values())
    # every index the mapping names is a series data/dhan.py actually stores
    assert set(SECTOR_INDEX_BY_INDUSTRY.values()) | set(SECTOR_INDEX_SYMBOL_OVERRIDES.values()) <= stored
    assert FALLBACK_SECTOR_INDEX == "NIFTY50" and FALLBACK_SECTOR_INDEX in stored


# ── TA-08b relative strength ───────────────────────────────────────────

def test_rs_vs_sector_index_is_stock_return_minus_index_return():
    from data.technical_ext import sector_relative_strength
    n = 140                                   # 13 flat bars first: the windows anchor at the last bar
    df = _frame(_ramp(n, 100, 120, flat_before=13, flat_value=90))
    it = _series(_ramp(n, 1000, 1100, flat_before=13, flat_value=900))
    out = sector_relative_strength(df, "INFY", "Information Technology", {"NIFTYIT": it}, bench=None)
    # 126 sessions back is the first ramp bar: stock 100 -> 120 (+20%), index 1000 -> 1100 (+10%)
    assert out["rs_sector_index"] == "NIFTYIT"
    assert out["rs_sector_126"] == pytest.approx(10.0, abs=1e-9)
    # 63 sessions back is the ramp's midpoint: stock 110 -> 120, index 1050 -> 1100
    assert out["rs_sector_63"] == pytest.approx(round(((120 / 110 - 1) - (1100 / 1050 - 1)) * 100, 3))
    assert out["rs_sector_pctile"] is None                       # needs the per-date batch


def test_rs_is_matched_on_date_not_row_position():
    from data.technical_ext import sector_relative_strength
    n = 140
    dates = _days(n)
    df = _frame(_ramp(n, 100, 120, flat_before=13, flat_value=90), dates)
    it = _series(_ramp(n, 1000, 1100, flat_before=13, flat_value=900), dates)
    # the index also carries 5 dates the stock has no bar for (Saturdays), interleaved in the window
    sats = [d + timedelta(days=1) for d in dates if d.weekday() == 4][10:15]
    assert len(sats) == 5 and not set(sats) & set(dates)
    extra = pd.DataFrame({"date": sats, "close": 5.0})
    it2 = pd.concat([it, extra]).sort_values("date").reset_index(drop=True)
    a = sector_relative_strength(df, "INFY", "Information Technology", {"NIFTYIT": it})
    b = sector_relative_strength(df, "INFY", "Information Technology", {"NIFTYIT": it2})
    assert a == b and a["rs_sector_126"] == pytest.approx(10.0)


def test_sector_without_an_index_falls_back_to_nifty50_and_says_so():
    from data.technical_ext import sector_relative_strength
    n = 130
    df = _frame(_ramp(n, 100, 120, flat_before=3, flat_value=100))
    nifty = _series(_ramp(n, 1000, 1050, flat_before=3, flat_value=1000))
    it = _series(_ramp(n, 1000, 1100, flat_before=3, flat_value=1000))
    # Capital Goods has no sector index -> vs NIFTY50: +20% - +5% = +15.0
    out = sector_relative_strength(df, "LT", "Capital Goods", {"NIFTYIT": it}, bench=nifty)
    assert out["rs_sector_index"] == "NIFTY50" and out["rs_sector_126"] == pytest.approx(15.0)
    # unknown industry (not in the Nifty 500 list): same fallback
    assert sector_relative_strength(df, "XYZ", None, {}, bench=nifty)["rs_sector_index"] == "NIFTY50"
    # an IT stock whose sector series is not stored -> NIFTY50 too
    out = sector_relative_strength(df, "INFY", "Information Technology", {}, bench=nifty)
    assert out["rs_sector_index"] == "NIFTY50" and out["rs_sector_126"] == pytest.approx(15.0)
    # ... or is stale (its last bar 20 sessions before the stock's) -> NIFTY50
    stale = it.iloc[:-20]
    out = sector_relative_strength(df, "INFY", "Information Technology", {"NIFTYIT": stale}, bench=nifty)
    assert out["rs_sector_index"] == "NIFTY50"
    # the benchmark may also come in through the series map instead of `bench`
    out = sector_relative_strength(df, "LT", "Capital Goods", {"NIFTY50": nifty})
    assert out["rs_sector_index"] == "NIFTY50" and out["rs_sector_126"] == pytest.approx(15.0)


def test_short_history_fills_63_but_not_126():
    from data.technical_ext import sector_relative_strength
    n = 80
    df = _frame(_ramp(n, 100, 110))
    it = _series(_ramp(n, 1000, 1000))
    out = sector_relative_strength(df, "INFY", "Information Technology", {"NIFTYIT": it})
    assert out["rs_sector_index"] == "NIFTYIT" and out["rs_sector_126"] is None
    s63 = 100 + 10 * 16 / 79                  # the close 63 sessions before the last (bar 16 of 0..79)
    assert out["rs_sector_63"] == pytest.approx(round((110 / s63 - 1) * 100, 3))


def test_percentile_within_sector():
    from data.technical_ext import sector_percentiles
    industries = {"A": "Information Technology", "B": "Information Technology", "C": "Information Technology",
                  "D": "Information Technology", "LT": "Capital Goods", "SBIN": "Financial Services",
                  "HDFCBANK": "Financial Services", "ICICIBANK": "Financial Services"}
    rows = [("A", "NIFTYIT", 5.0), ("B", "NIFTYIT", -2.0), ("C", "NIFTYIT", 10.0), ("D", "NIFTYIT", 5.0),
            ("LT", "NIFTY50", 3.0),                               # alone in its group
            ("SBIN", "NIFTYPSUBANK", 9.0),                        # its own group (another index)
            ("HDFCBANK", "NIFTYBANK", 1.0), ("ICICIBANK", "NIFTYBANK", 2.0),
            ("UNKNOWN", "NIFTY50", 4.0),                          # no industry -> no peers
            ("E", "NIFTYIT", None)]                               # no value
    p = sector_percentiles(rows, industries)
    # IT, 4 stocks sorted B(-2) < A = D (5) < C(10): ranks 0, 1.5, 1.5, 3 of n-1 = 3
    assert p["B"] == 0.0 and p["A"] == 50.0 and p["D"] == 50.0 and p["C"] == 100.0
    assert p["LT"] is None and p["SBIN"] is None and p["UNKNOWN"] is None and p["E"] is None
    assert p["HDFCBANK"] is None and p["ICICIBANK"] is None       # 2 < SECTOR_PCTILE_MIN_PEERS
    p2 = sector_percentiles(rows, industries, min_peers=2)
    assert p2["HDFCBANK"] == 0.0 and p2["ICICIBANK"] == 100.0 and p2["SBIN"] is None


# ── TA-05 swing-anchored Fibonacci ─────────────────────────────────────

def test_swing_up_levels_measured_down_from_the_high():
    from data.technical_ext import swing_fibonacci
    df = _path([(0, 100), (20, 80), (40, 120), (59, 105)], 60)   # low at bar 20, high at bar 40
    out = swing_fibonacci(df)
    hi, lo = 121.0, 79.0                                          # mid +/- 1
    assert (out["fib_swing_high"], out["fib_swing_low"], out["fib_swing_dir"]) == (hi, lo, "UP")
    for name, r in (("fib_382", 0.382), ("fib_500", 0.5), ("fib_618", 0.618)):
        assert out[name] == pytest.approx(round(hi - (hi - lo) * r, 2))
    # close 105: 38.2% = 104.956 is 0.044 away, 50% = 100 is 5 away
    assert out["fib_nearest"] == "fib_382"
    assert out["fib_nearest_dist_pct"] == pytest.approx(round((105 / (hi - 42 * 0.382) - 1) * 100, 3))


def test_swing_down_levels_measured_up_from_the_low():
    from data.technical_ext import swing_fibonacci
    df = _path([(0, 100), (20, 120), (40, 80), (59, 95)], 60)    # high at bar 20, low at bar 40
    out = swing_fibonacci(df)
    hi, lo = 121.0, 79.0
    assert (out["fib_swing_high"], out["fib_swing_low"], out["fib_swing_dir"]) == (hi, lo, "DOWN")
    for name, r in (("fib_382", 0.382), ("fib_500", 0.5), ("fib_618", 0.618)):
        assert out[name] == pytest.approx(round(lo + (hi - lo) * r, 2))
    # close 95 sits just under 38.2% = 95.044 -> a negative distance
    assert out["fib_nearest"] == "fib_382"
    assert out["fib_nearest_dist_pct"] == pytest.approx(round((95 / (lo + 42 * 0.382) - 1) * 100, 3))
    assert out["fib_nearest_dist_pct"] < 0


def test_a_lower_high_does_not_end_the_swing():
    from data.technical_ext import swing_fibonacci
    # low 80 at bar 20, high 130 at bar 30, a shallow dip (not a swing low), a lower high 129.5 at
    # bar 36, then down to 110: the swing is 79 -> 131, not 79 -> 130.5
    df = _path([(0, 100), (20, 80), (30, 130), (33, 125), (36, 129.5), (59, 110)], 60)
    out = swing_fibonacci(df)
    assert (out["fib_swing_high"], out["fib_swing_low"], out["fib_swing_dir"]) == (131.0, 79.0, "UP")
    assert out["fib_500"] == pytest.approx(105.0)
    # close 110: 38.2% = 131 - 52 x 0.382 = 111.136 is nearest
    assert out["fib_nearest"] == "fib_382"
    assert out["fib_nearest_dist_pct"] == pytest.approx(round((110 / 111.136 - 1) * 100, 3))


def test_swing_only_looks_back_the_lookback():
    from data.technical_ext import swing_fibonacci
    # every swing point is before the last 30 bars, which only trend up
    df = _path([(0, 100), (20, 80), (40, 120), (59, 105), (99, 150)], 100)
    assert swing_fibonacci(df, lookback=30)["fib_swing_dir"] is None
    # over 120 bars the last swing is the high at bar 40 down to the low at bar 59
    out = swing_fibonacci(df)
    assert (out["fib_swing_high"], out["fib_swing_low"], out["fib_swing_dir"]) == (121.0, 104.0, "DOWN")


def test_missing_data_gives_none_without_errors():
    from data.technical_ext import compute, sector_relative_strength, swing_fibonacci
    trend = np.linspace(100, 150, 60)                             # no swing low / high at all
    assert all(v is None for v in swing_fibonacci(pd.DataFrame({"high": trend + 1, "low": trend - 1,
                                                                 "close": trend})).values())
    assert all(v is None for v in swing_fibonacci(_path([(0, 100), (7, 90)], 8)).values())   # too short
    df = _frame(_ramp(150, 100, 120))
    out = compute(df)                                             # no benchmark, no sector inputs
    assert all(out[c] is None for c in NEW_RS)
    sec = {"symbol": "INFY", "industry": "Information Technology", "series": {}}
    out = compute(df, None, None, sector=sec)                     # sector given but no series at all
    assert all(out[c] is None for c in NEW_RS)
    empty = pd.DataFrame({"date": [], "close": []})
    assert all(v is None for v in sector_relative_strength(df, "INFY", "Information Technology",
                                                           {"NIFTYIT": empty}, bench=empty).values())
    short = _series(_ramp(40, 1000, 1100), _days(150)[-40:])      # 40 matched sessions < 64
    assert sector_relative_strength(df, "LT", "Capital Goods", {}, bench=short)["rs_sector_index"] is None
    assert compute(df.head(10), sector=sec) == {}                 # < 20 bars, as before


# ── compute(): pre-existing keys unchanged, new ones not scored ───────────

def _wavy(n=300, seed_amp=0.04):
    c = np.array([100 * 1.001 ** i * (1 + seed_amp * math.sin(i / 7)) for i in range(n)])
    return _frame(c)


def test_compute_pre_existing_keys_are_unchanged_by_the_new_inputs():
    from data.technical_ext import COLUMNS, W21_COLUMNS, compute
    assert len(W21_COLUMNS) == 38 and COLUMNS[:38] == W21_COLUMNS
    assert set(COLUMNS[38:]) == set(NEW_RS) | set(NEW_FIB)
    df = _wavy()
    bench = _series(np.array([1000 * 1.0005 ** i * (1 + 0.01 * math.sin(i / 5)) for i in range(300)]))
    it = _series(np.array([1000 * 1.0008 ** i for i in range(300)]))
    without = compute(df, bench)
    with_sector = compute(df, bench, sector={"symbol": "INFY", "industry": "Information Technology",
                                             "series": {"NIFTYIT": it}})
    assert set(without) == set(with_sector) == set(COLUMNS)
    for k in W21_COLUMNS:
        assert without[k] == with_sector[k], k
    assert all(without[c] is None for c in NEW_RS)
    assert with_sector["rs_sector_index"] == "NIFTYIT" and with_sector["rs_sector_126"] is not None
    # the swing Fibonacci needs only the bars, so it is there either way and identical
    assert without["fib_swing_dir"] in ("UP", "DOWN")
    assert all(without[c] == with_sector[c] for c in NEW_FIB)


def test_new_columns_feed_no_score_or_strategy_feature():
    from data.technical import compute_tech_score
    from strategy_engine.features import TA_EXT_FEATURES
    ind = {"rsi_14": 55, "macd_hist_pct": 0.3, "adx_14": 30, "atr_pct": 2, "ema_9": 3, "ema_21": 2,
           "ema_50": 1, "above_200dma": 1, "volume_ratio": 1.2}
    extra = {"rs_sector_index": "NIFTYIT", "rs_sector_63": 25.0, "rs_sector_126": 40.0, "rs_sector_pctile": 100.0,
             "fib_swing_high": 1, "fib_swing_low": 0, "fib_swing_dir": "UP", "fib_nearest": "fib_618",
             "fib_nearest_dist_pct": -9.0}
    assert compute_tech_score({**ind, **extra}) == compute_tech_score(ind)
    assert not ({c for c, _ in TA_EXT_FEATURES.values()} & (set(NEW_RS) | set(NEW_FIB)))


# ── the storing path: data/technical.py run_technical_pipeline -> technical_ext ──

def test_pipeline_stores_sector_rs_percentile_and_swing_fib(temp_db, monkeypatch):
    from data.technical import HAS_TA
    if not HAS_TA:
        pytest.skip("no TA library installed")
    import data.index_constituents as ic
    from data.technical import run_technical_pipeline
    from db.schema import get_connection, init_db
    init_db()
    n = 200
    dates = _days(n)
    j = np.arange(n) - (n - 127)                  # 0 at the bar 126 sessions back, 126 at the last
    wiggle = 1 + 0.05 * np.sin(2 * np.pi * j / 42)    # 0 at j = 0, 63 and 126: anchors untouched

    def stock(end):
        return _ramp(n, 100, end, flat_before=n - 127, flat_value=100) * wiggle

    closes = {"INFY": stock(130), "TCS": stock(110), "WIPRO": stock(120), "LT": stock(115)}
    index = {"NIFTYIT": _ramp(n, 1000, 1100, flat_before=n - 127, flat_value=1000),
             "NIFTY50": _ramp(n, 1000, 1050, flat_before=n - 127, flat_value=1000)}
    conn = get_connection()
    for sym, c in closes.items():
        conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                         [(sym, str(d), float(x), float(x) * 1.01, float(x) * 0.99, float(x), 100000)
                          for d, x in zip(dates, c)])
    for sym, c in index.items():
        conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         [(sym, str(d), float(x), float(x), float(x), float(x), 0, "dhan_index")
                          for d, x in zip(dates, c)])
    conn.commit()
    conn.close()
    monkeypatch.setattr(ic, "get_symbol_industry_map", lambda: {
        "INFY": "Information Technology", "TCS": "Information Technology", "WIPRO": "Information Technology",
        "LT": "Capital Goods"})
    td = dates[-1]
    res = run_technical_pipeline(td, symbols=list(closes))
    assert res["status"] == "SUCCESS" and res["rows"] == 4

    def stored():
        c = get_connection()
        try:
            return {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM technical_ext WHERE date=?", (str(td),))}
        finally:
            c.close()

    rows = stored()
    # +30 / +10 / +20 % against NIFTYIT's +10 %; LT +15 % against NIFTY50's +5 %
    exp = {"INFY": ("NIFTYIT", 20.0), "TCS": ("NIFTYIT", 0.0), "WIPRO": ("NIFTYIT", 10.0), "LT": ("NIFTY50", 10.0)}
    for sym, (idx, rs) in exp.items():
        assert rows[sym]["rs_sector_index"] == idx
        assert rows[sym]["rs_sector_126"] == pytest.approx(rs, abs=1e-3)
        assert rows[sym]["rs_sector_63"] is not None
    assert (rows["TCS"]["rs_sector_pctile"], rows["WIPRO"]["rs_sector_pctile"],
            rows["INFY"]["rs_sector_pctile"]) == (0.0, 50.0, 100.0)
    assert rows["LT"]["rs_sector_pctile"] is None                 # alone in Capital Goods
    for sym in closes:                                            # the 42-session wave has swings
        r = rows[sym]
        assert r["fib_swing_dir"] in ("UP", "DOWN") and r["fib_swing_high"] > r["fib_swing_low"]
        assert r["fib_nearest"] in ("fib_382", "fib_500", "fib_618")
        lo, hi = r["fib_swing_low"], r["fib_swing_high"]
        if r["fib_swing_dir"] == "UP":
            assert r["fib_618"] == pytest.approx(round(hi - (hi - lo) * 0.618, 2), abs=0.011)
        else:
            assert r["fib_618"] == pytest.approx(round(lo + (hi - lo) * 0.618, 2), abs=0.011)
    # the W21 columns are still written
    assert rows["INFY"]["vwap_20d"] is not None and rows["INFY"]["beta_60"] is not None
    # a one-symbol re-run ranks against the peers already stored for the date
    run_technical_pipeline(td, symbol="TCS")
    assert stored()["TCS"]["rs_sector_pctile"] == 0.0
