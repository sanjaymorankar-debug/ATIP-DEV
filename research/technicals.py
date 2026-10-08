"""
W39 (TA-01..TA-04) — technical analysis core: indicators, candlestick patterns and scan rules,
computed straight from a stock's daily OHLCV (prices_daily, now kept for 7 years), so every
value can be reproduced from the bars alone. Pure functions over a pandas DataFrame with
columns open, high, low, close, volume (oldest first); no database here.

    indicators(df, bench=None)   adds SMA 20/50/200, EMA 9/21, RSI(14, Wilder), MACD(12,26,9),
                                 ATR(14), ADX(14) with +DI/-DI, Supertrend(10,3), Bollinger(20,2) and
                                 bandwidth, Donchian(20/55), 52-week high / low, volume vs 20-day
                                 average, relative strength vs a benchmark (63-day), the RS line
                                 (close / benchmark) with its prior 52-week high, and NSE delivery %
                                 vs its 20-day average when the frame carries delivery_pct
    candles(df)                  candlestick patterns on the last bar (engulfing, hammer, shooting
                                 star, doji, harami, piercing / dark cloud, morning / evening star,
                                 three soldiers / crows, marubozu, inside bar, NR7)
    SCANS / run_scans(df)        47 named scans (Chartink / Finviz / StockCharts style): crossovers,
                                 breakouts on volume, oscillator turns, trend templates, squeezes and
                                 14 chart-pattern breakouts (research/patterns.py: Darvas box, VCP, double
                                 bottom, ascending and descending triangles, head and shoulders and its
                                 inverse, rising / falling channels both ways, rising and falling
                                 wedges), each with a direction (BULL / BEAR) and a one-line reason
    snapshot(df, bench)          one flat dict per stock for the screener: latest indicator values,
                                 scan hits as 0/1 fields, today's patterns, a technical rating, and
                                 the same rating on weekly bars with the daily / weekly agreement
    weekly_bars(df, as_of)       weekly OHLCV (weeks ending Friday), completed weeks only: a week
                                 counts once its Friday is on or before as_of, so the weekly rating
                                 does not change mid-week (the "completed candles only" rule)

Technical rating (-1..+1, like TradingView's "Technical Ratings"): the mean of moving-average
votes (price vs SMA 20/50/200, EMA 9 vs 21, SMA 50 vs 200, Supertrend direction) and oscillator
votes (RSI, MACD, ADX direction, Bollinger position), each +1 / 0 / -1; labelled STRONG_BUY > 0.5,
BUY > 0.1, NEUTRAL, SELL < -0.1, STRONG_SELL < -0.5. A description of the chart, not advice.
The weekly rating is the same vote on weekly bars (35+ completed weeks needed; the 200-period votes
need ~4 years of history and are simply absent until then). mtf_alignment is BULL when the daily
and weekly ratings are both BUY / STRONG_BUY, BEAR when both are SELL / STRONG_SELL, else MIXED.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research import patterns as P

MIN_BARS = 60          # below this the long indicators are meaningless; snapshot returns None
MIN_WEEKS = 35         # completed weekly bars needed for a weekly rating (MACD 26 + 9)
_UP, _DOWN = ("BUY", "STRONG_BUY"), ("SELL", "STRONG_SELL")


# ── indicators ───────────────────────────────────────────────────────────────

def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return _wilder(tr, n)


def adx(df: pd.DataFrame, n: int = 14) -> tuple:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * _wilder(plus, n) / a
    mdi = 100 * _wilder(minus, n) / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _wilder(dx, n), pdi, mdi


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> tuple:
    """(line, direction) where direction is +1 (up-trend) or -1."""
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    upper, lower = (hl2 + mult * a).to_numpy(), (hl2 - mult * a).to_numpy()
    close = df["close"].to_numpy()
    fu, fl = upper.copy(), lower.copy()
    direction = np.ones(len(df))
    line = np.full(len(df), np.nan)
    for i in range(1, len(df)):
        if np.isnan(upper[i]) or np.isnan(fu[i - 1]):
            continue
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < fl[i] else 1
        else:
            direction[i] = 1 if close[i] > fu[i] else -1
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


def indicators(df: pd.DataFrame, bench: pd.Series | None = None) -> pd.DataFrame:
    """A copy of df with every indicator column added."""
    d = df.copy()
    c, v = d["close"], d["volume"].astype(float)
    for n in (20, 50, 200):
        d[f"sma{n}"] = c.rolling(n).mean()
    d["ema9"], d["ema21"] = c.ewm(span=9, adjust=False).mean(), c.ewm(span=21, adjust=False).mean()
    d["rsi14"] = rsi(c)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    d["macd"] = ema12 - ema26
    d["macd_signal"] = d["macd"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_signal"]
    d["atr14"] = atr(d)
    d["adx14"], d["pdi"], d["mdi"] = adx(d)
    d["st_line"], d["st_dir"] = supertrend(d)
    mid, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    d["bb_up"], d["bb_lo"] = mid + 2 * sd, mid - 2 * sd
    d["bb_width"] = (d["bb_up"] - d["bb_lo"]) / mid
    d["kc_up"] = c.ewm(span=20, adjust=False).mean() + 1.5 * d["atr14"]
    d["kc_lo"] = c.ewm(span=20, adjust=False).mean() - 1.5 * d["atr14"]
    d["dc20_hi"], d["dc20_lo"] = d["high"].rolling(20).max().shift(), d["low"].rolling(20).min().shift()
    d["dc55_hi"] = d["high"].rolling(55).max().shift()
    d["hi252"], d["lo252"] = d["high"].rolling(252, min_periods=120).max(), d["low"].rolling(252, min_periods=120).min()
    d["prior_hi252"] = d["high"].rolling(252, min_periods=120).max().shift()
    d["vol20"] = v.rolling(20).mean()
    d["vol_ratio"] = v / d["vol20"].shift()
    if "delivery_pct" in d:                                   # NSE delivery %, from the bhavcopy
        dp = pd.to_numeric(d["delivery_pct"], errors="coerce")
        d["deliv20"] = dp.rolling(20, min_periods=10).mean().shift()
        d["deliv_ratio"] = dp / d["deliv20"]
    else:
        d["delivery_pct"] = d["deliv20"] = d["deliv_ratio"] = np.nan
    d["range"] = d["high"] - d["low"]
    if bench is not None and len(bench):
        b = bench.reindex(d.index).ffill()
        d["rs63"] = (c / c.shift(63)) / (b / b.shift(63)) - 1
        d["rs_line"] = c / b                                   # IBD's RS line: price relative to the index
        d["rs_line_prior_hi"] = d["rs_line"].rolling(252, min_periods=120).max().shift()
    else:
        d["rs63"] = d["rs_line"] = d["rs_line_prior_hi"] = np.nan
    return d


# ── candlestick patterns (on the last bar) ───────────────────────────────────

# each name candles() can return and its side; technical_snapshot.patterns stores only the names, so the
# stock chart (dashboard/stock_view.py) reads their side from here
CANDLE_SIDES = {"Doji": "NEUTRAL", "Hammer": "BULL", "Hanging man": "BEAR", "Shooting star": "BEAR",
                "Inverted hammer": "BULL", "Bullish engulfing": "BULL", "Bearish engulfing": "BEAR",
                "Bullish harami": "BULL", "Bearish harami": "BEAR", "Piercing line": "BULL",
                "Dark cloud cover": "BEAR", "Morning star": "BULL", "Evening star": "BEAR",
                "Three white soldiers": "BULL", "Three black crows": "BEAR", "Bullish marubozu": "BULL",
                "Bearish marubozu": "BEAR", "Inside bar": "NEUTRAL", "NR7": "NEUTRAL"}


def candles(d: pd.DataFrame) -> list:
    """[(name, BULL|BEAR|NEUTRAL)] for the last bar. Needs indicators() columns for trend context."""
    if len(d) < 25:
        return []
    o, h, l, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
    i = len(d) - 1
    body = lambda k: abs(c[k] - o[k])
    rng = lambda k: max(h[k] - l[k], 1e-9)
    upper = lambda k: h[k] - max(o[k], c[k])
    lower = lambda k: min(o[k], c[k]) - l[k]
    bull = lambda k: c[k] > o[k]
    bear = lambda k: c[k] < o[k]
    avg_body = float(np.mean([body(k) for k in range(i - 10, i)])) or 1e-9
    down = c[i - 1] < d["sma20"].iloc[i - 1] if not math.isnan(d["sma20"].iloc[i - 1]) else c[i - 1] < c[i - 6]
    up = not down
    out = []
    if body(i) <= 0.1 * rng(i):
        out.append(("Doji", "NEUTRAL"))
    if lower(i) >= 2 * body(i) and upper(i) <= 0.3 * max(body(i), 1e-9) + 0.1 * rng(i) and body(i) > 0.05 * rng(i):
        out.append(("Hammer", "BULL") if down else ("Hanging man", "BEAR"))
    if upper(i) >= 2 * body(i) and lower(i) <= 0.3 * max(body(i), 1e-9) + 0.1 * rng(i) and body(i) > 0.05 * rng(i):
        out.append(("Shooting star", "BEAR") if up else ("Inverted hammer", "BULL"))
    if bear(i - 1) and bull(i) and o[i] <= c[i - 1] and c[i] >= o[i - 1] and body(i) > body(i - 1):
        out.append(("Bullish engulfing", "BULL"))
    if bull(i - 1) and bear(i) and o[i] >= c[i - 1] and c[i] <= o[i - 1] and body(i) > body(i - 1):
        out.append(("Bearish engulfing", "BEAR"))
    if bear(i - 1) and bull(i) and o[i] > c[i - 1] and c[i] < o[i - 1] and body(i - 1) > avg_body:
        out.append(("Bullish harami", "BULL"))
    if bull(i - 1) and bear(i) and o[i] < c[i - 1] and c[i] > o[i - 1] and body(i - 1) > avg_body:
        out.append(("Bearish harami", "BEAR"))
    mid_prev = (o[i - 1] + c[i - 1]) / 2
    if bear(i - 1) and bull(i) and o[i] < l[i - 1] and mid_prev < c[i] < o[i - 1]:
        out.append(("Piercing line", "BULL"))
    if bull(i - 1) and bear(i) and o[i] > h[i - 1] and o[i - 1] < c[i] < mid_prev:
        out.append(("Dark cloud cover", "BEAR"))
    if (bear(i - 2) and body(i - 2) > avg_body and body(i - 1) < 0.5 * avg_body
            and bull(i) and c[i] > (o[i - 2] + c[i - 2]) / 2):
        out.append(("Morning star", "BULL"))
    if (bull(i - 2) and body(i - 2) > avg_body and body(i - 1) < 0.5 * avg_body
            and bear(i) and c[i] < (o[i - 2] + c[i - 2]) / 2):
        out.append(("Evening star", "BEAR"))
    if all(bull(k) and c[k] > c[k - 1] and body(k) > 0.6 * rng(k) for k in (i - 2, i - 1, i)):
        out.append(("Three white soldiers", "BULL"))
    if all(bear(k) and c[k] < c[k - 1] and body(k) > 0.6 * rng(k) for k in (i - 2, i - 1, i)):
        out.append(("Three black crows", "BEAR"))
    if body(i) >= 0.95 * rng(i) and body(i) > 1.5 * avg_body:
        out.append(("Bullish marubozu", "BULL") if bull(i) else ("Bearish marubozu", "BEAR"))
    if h[i] < h[i - 1] and l[i] > l[i - 1]:
        out.append(("Inside bar", "NEUTRAL"))
    ranges = d["range"].to_numpy()[i - 6:i + 1]
    if len(ranges) == 7 and ranges[-1] == ranges.min():
        out.append(("NR7", "NEUTRAL"))
    return out


# ── scans ────────────────────────────────────────────────────────────────────

def _v(d, col, k=-1):
    x = d[col].iloc[k]
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _cross_up(d, a, b):
    a0, a1, b0, b1 = _v(d, a, -2), _v(d, a), (_v(d, b, -2) if isinstance(b, str) else b), \
        (_v(d, b) if isinstance(b, str) else b)
    return None not in (a0, a1, b0, b1) and a0 <= b0 and a1 > b1


def _cross_dn(d, a, b):
    a0, a1, b0, b1 = _v(d, a, -2), _v(d, a), (_v(d, b, -2) if isinstance(b, str) else b), \
        (_v(d, b) if isinstance(b, str) else b)
    return None not in (a0, a1, b0, b1) and a0 >= b0 and a1 < b1


def _gt(x, y):
    return x is not None and y is not None and x > y


def _max_down_volume(d, n):
    c, v = d["close"].to_numpy(), d["volume"].to_numpy()
    downs = [v[k] for k in range(len(d) - 1 - n, len(d) - 1) if k > 0 and c[k] < c[k - 1]]
    return float(max(downs)) if downs else None


def _rs_line_new_high(d) -> bool:
    """The RS line closed above its prior 52-week high today, and did not yesterday (the first day)."""
    if "rs_line" not in d:
        return False
    rs, hi = d["rs_line"], d["rs_line_prior_hi"]
    today, before = _gt(rs.iloc[-1], hi.iloc[-1]), _gt(rs.iloc[-2], hi.iloc[-2])
    return bool(today and not before)


def rs_raw(close: pd.Series):
    """IBD-style relative-strength input: 0.4 ROC63 + 0.2 ROC126 + 0.2 ROC189 + 0.2 ROC252 (ranked 1-99 across
    the universe by the caller). None without a year of bars."""
    if len(close) < 253:
        return None
    r = lambda n: float(close.iloc[-1] / close.iloc[-1 - n] - 1)
    return 0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252)


def _trend_template(d):
    """Minervini: price > SMA50 > SMA150 > SMA200, SMA200 rising a month, >= 30% above the 52w low,
    within 25% of the 52w high, RS positive."""
    c, s50, s200 = _v(d, "close"), _v(d, "sma50"), _v(d, "sma200")
    s150 = float(d["close"].rolling(150).mean().iloc[-1]) if len(d) >= 150 else None
    s200_ago = _v(d, "sma200", -22) if len(d) > 222 else None
    lo, hi, rs = _v(d, "lo252"), _v(d, "hi252"), _v(d, "rs63")
    return (None not in (c, s50, s150, s200, s200_ago, lo, hi) and c > s50 > s150 > s200 and s200 > s200_ago
            and c >= 1.3 * lo and c >= 0.75 * hi and (rs is None or rs > 0))


SCANS = {
    # key: (name, direction, rule(d) -> bool, description)
    "golden_cross": ("Golden cross", "BULL", lambda d: _cross_up(d, "sma50", "sma200"),
                     "SMA 50 crossed above SMA 200 today"),
    "death_cross": ("Death cross", "BEAR", lambda d: _cross_dn(d, "sma50", "sma200"),
                    "SMA 50 crossed below SMA 200 today"),
    "ema_9_21_bull": ("EMA 9/21 bullish cross", "BULL", lambda d: _cross_up(d, "ema9", "ema21"),
                      "EMA 9 crossed above EMA 21"),
    "ema_9_21_bear": ("EMA 9/21 bearish cross", "BEAR", lambda d: _cross_dn(d, "ema9", "ema21"),
                      "EMA 9 crossed below EMA 21"),
    "price_above_200": ("Crossed above 200-DMA", "BULL", lambda d: _cross_up(d, "close", "sma200"),
                        "close crossed above SMA 200"),
    "price_below_200": ("Crossed below 200-DMA", "BEAR", lambda d: _cross_dn(d, "close", "sma200"),
                        "close crossed below SMA 200"),
    "macd_bull": ("MACD bullish crossover", "BULL", lambda d: _cross_up(d, "macd", "macd_signal"),
                  "MACD crossed above its signal line"),
    "macd_bear": ("MACD bearish crossover", "BEAR", lambda d: _cross_dn(d, "macd", "macd_signal"),
                  "MACD crossed below its signal line"),
    "macd_zero_up": ("MACD crossed above zero", "BULL", lambda d: _cross_up(d, "macd", 0.0),
                     "MACD line crossed above zero"),
    "rsi_oversold_turn": ("RSI oversold reversal", "BULL", lambda d: _cross_up(d, "rsi14", 30.0),
                          "RSI(14) crossed back up through 30"),
    "rsi_overbought_turn": ("RSI overbought reversal", "BEAR", lambda d: _cross_dn(d, "rsi14", 70.0),
                            "RSI(14) crossed back down through 70"),
    "rsi_50_up": ("RSI crossed above 50", "BULL", lambda d: _cross_up(d, "rsi14", 50.0),
                  "momentum turned positive: RSI(14) through 50"),
    "supertrend_buy": ("Supertrend buy", "BULL",
                       lambda d: _v(d, "st_dir", -2) == -1 and _v(d, "st_dir") == 1, "Supertrend(10,3) flipped up"),
    "supertrend_sell": ("Supertrend sell", "BEAR",
                        lambda d: _v(d, "st_dir", -2) == 1 and _v(d, "st_dir") == -1, "Supertrend(10,3) flipped down"),
    "high_52w_breakout": ("52-week high breakout on volume", "BULL",
                          lambda d: _gt(_v(d, "close"), _v(d, "prior_hi252")) and _gt(_v(d, "vol_ratio"), 1.5),
                          "close above the prior 52-week high with volume over 1.5x its 20-day average"),
    "low_52w_breakdown": ("52-week low breakdown", "BEAR",
                          lambda d: _v(d, "close") is not None and _v(d, "lo252") is not None
                          and _v(d, "close") <= _v(d, "lo252") and _gt(_v(d, "vol_ratio"), 1.2),
                          "close at a new 52-week low on above-average volume"),
    "donchian_20_breakout": ("20-day breakout", "BULL", lambda d: _gt(_v(d, "close"), _v(d, "dc20_hi")),
                             "close above the prior 20-day high (Donchian / turtle)"),
    "donchian_55_breakout": ("55-day breakout", "BULL", lambda d: _gt(_v(d, "close"), _v(d, "dc55_hi")),
                             "close above the prior 55-day high"),
    "donchian_20_breakdown": ("20-day breakdown", "BEAR",
                              lambda d: _v(d, "close") is not None and _v(d, "dc20_lo") is not None
                              and _v(d, "close") < _v(d, "dc20_lo"), "close below the prior 20-day low"),
    "volume_surge_up": ("Volume surge, up day", "BULL",
                        lambda d: _gt(_v(d, "vol_ratio"), 2.0) and _gt(_v(d, "close"), _v(d, "close", -2)),
                        "volume over 2x its 20-day average on an up day"),
    "volume_surge_down": ("Volume surge, down day", "BEAR",
                          lambda d: _gt(_v(d, "vol_ratio"), 2.0) and _gt(_v(d, "close", -2), _v(d, "close")),
                          "volume over 2x its 20-day average on a down day"),
    "bb_squeeze_fire": ("Squeeze fired (TTM)", "BULL",
                        lambda d: len(d) > 2 and _gt(_v(d, "kc_up", -2), _v(d, "bb_up", -2))
                        and not _gt(_v(d, "kc_up"), _v(d, "bb_up")) and _gt(_v(d, "close"), _v(d, "sma20")),
                        "Bollinger bands left the Keltner channel (volatility expansion) with price above SMA 20"),
    "bb_lower_bounce": ("Bollinger lower-band bounce", "BULL",
                        lambda d: _v(d, "close", -2) is not None and _v(d, "bb_lo", -2) is not None
                        and _v(d, "close", -2) < _v(d, "bb_lo", -2) and _gt(_v(d, "close"), _v(d, "bb_lo")),
                        "closed back inside after a close below the lower Bollinger band"),
    "adx_trend_start": ("Strong trend starting", "BULL",
                        lambda d: _cross_up(d, "adx14", 25.0) and _gt(_v(d, "pdi"), _v(d, "mdi")),
                        "ADX(14) rose through 25 with +DI above -DI"),
    "trend_template": ("Minervini trend template", "BULL", _trend_template,
                       "price > SMA50 > SMA150 > SMA200, SMA200 rising, 30% off the low, within 25% of the high"),
    "rs_leader": ("Relative-strength leader", "BULL",
                  lambda d: _gt(_v(d, "rs63"), 0.15) and _gt(_v(d, "close"), _v(d, "sma50")),
                  "outperformed the Nifty by over 15% in 3 months and holds above SMA 50"),
    "pullback_in_uptrend": ("Pullback to 50-DMA in an up-trend", "BULL",
                            lambda d: _gt(_v(d, "sma50"), _v(d, "sma200")) and _v(d, "low") is not None
                            and _v(d, "sma50") is not None and _v(d, "low") <= _v(d, "sma50") * 1.01
                            and _gt(_v(d, "close"), _v(d, "sma50")),
                            "touched SMA 50 and closed above it while SMA 50 > SMA 200"),
    "pocket_pivot": ("Pocket pivot", "BULL",
                     lambda d: len(d) > 12 and _gt(_v(d, "close"), _v(d, "close", -2))
                     and _gt(float(d["volume"].iloc[-1]), _max_down_volume(d, 10))
                     and _gt(_v(d, "close"), _v(d, "sma50")),
                     "up day on volume above every down-day volume of the last 10 sessions, above SMA 50 (O'Neil)"),
    "gap_up": ("Gap up", "BULL", lambda d: _gt(_v(d, "low"), _v(d, "high", -2)) and _gt(_v(d, "vol_ratio"), 1.2),
               "today's low above yesterday's high, on above-average volume"),
    "delivery_spike_up": ("Delivery spike, up day", "BULL",
                          lambda d: _gt(_v(d, "deliv_ratio"), 1.5) and _gt(_v(d, "delivery_pct"), 30)
                          and _gt(_v(d, "close"), _v(d, "close", -2)),
                          "NSE delivery % at 1.5x+ its 20-day average (and 30 %+) on an up day: buyers taking delivery"),
    "rs_line_new_high": ("RS line new high", "BULL", _rs_line_new_high,
                         "relative strength vs the Nifty (the RS line) reached a 52-week high today"),
    "rs_line_leads": ("RS line new high before price", "BULL",
                      lambda d: _rs_line_new_high(d) and not _gt(_v(d, "close"), _v(d, "prior_hi252")),
                      "the RS line made a 52-week high while the price has not: leadership showing early"),
    # chart patterns (research/patterns.py): found on the bars before today, broken on today's close
    "darvas_breakout": ("Darvas box breakout", "BULL", lambda d: P.breakout(d, "darvas_breakout"),
                        "closed above a Darvas box top"),
    "darvas_breakdown": ("Darvas box breakdown", "BEAR", lambda d: P.breakout(d, "darvas_breakdown"),
                         "closed below a Darvas box bottom"),
    "vcp_breakout": ("VCP breakout", "BULL", lambda d: P.breakout(d, "vcp_breakout"),
                     "closed above a volatility-contraction pivot on 1.4x+ volume"),
    "double_bottom_breakout": ("Double bottom breakout", "BULL", lambda d: P.breakout(d, "double_bottom_breakout"),
                               "closed above a double bottom's neckline"),
    "ascending_triangle_breakout": ("Ascending triangle breakout", "BULL",
                                    lambda d: P.breakout(d, "ascending_triangle_breakout"),
                                    "closed above an ascending triangle's flat resistance"),
    "head_shoulders_breakdown": ("Head and shoulders breakdown", "BEAR",
                                 lambda d: P.breakout(d, "head_shoulders_breakdown"),
                                 "closed below a head-and-shoulders neckline"),
    "inverse_head_shoulders_breakout": ("Inverse head and shoulders breakout", "BULL",
                                        lambda d: P.breakout(d, "inverse_head_shoulders_breakout"),
                                        "closed above an inverse head-and-shoulders neckline"),
    "descending_triangle_breakdown": ("Descending triangle breakdown", "BEAR",
                                      lambda d: P.breakout(d, "descending_triangle_breakdown"),
                                      "closed below a descending triangle's flat support"),
    "rising_channel_breakout": ("Rising channel breakout", "BULL", lambda d: P.breakout(d, "rising_channel_breakout"),
                                "closed above a rising channel's upper line"),
    "rising_channel_breakdown": ("Rising channel breakdown", "BEAR",
                                 lambda d: P.breakout(d, "rising_channel_breakdown"),
                                 "closed below a rising channel's lower line"),
    "falling_channel_breakout": ("Falling channel breakout", "BULL",
                                 lambda d: P.breakout(d, "falling_channel_breakout"),
                                 "closed above a falling channel's upper line"),
    "falling_channel_breakdown": ("Falling channel breakdown", "BEAR",
                                  lambda d: P.breakout(d, "falling_channel_breakdown"),
                                  "closed below a falling channel's lower line"),
    "rising_wedge_breakdown": ("Rising wedge breakdown", "BEAR", lambda d: P.breakout(d, "rising_wedge_breakdown"),
                               "closed below a rising wedge's lower line"),
    "falling_wedge_breakout": ("Falling wedge breakout", "BULL", lambda d: P.breakout(d, "falling_wedge_breakout"),
                               "closed above a falling wedge's upper line"),
    "nr7_inside": ("NR7 inside bar", "NEUTRAL",
                   lambda d: d["range"].iloc[-1] == d["range"].iloc[-7:].min()
                   and d["high"].iloc[-1] < d["high"].iloc[-2] and d["low"].iloc[-1] > d["low"].iloc[-2],
                   "narrowest range of 7 days inside yesterday's range: a breakout setup"),
}


PATTERN_SCANS = frozenset(P.SCAN_PATTERN)      # the chart-pattern breakouts: alerts held until proven


def run_scans(d: pd.DataFrame) -> list:
    """[(key, name, direction, reason)] for the scans that hit on the last bar of an indicators() frame.
    A rule may return the reason text itself (the chart patterns name their levels)."""
    out = []
    for key, (name, direction, rule, desc) in SCANS.items():
        try:
            hit = rule(d)
            if hit:
                out.append((key, name, direction, hit if isinstance(hit, str) else desc))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return out


# ── technical rating ─────────────────────────────────────────────────────────

def rating(d: pd.DataFrame) -> tuple:
    """(score -1..+1, label, votes) -- moving-average and oscillator votes on the last bar."""
    c = _v(d, "close")
    votes = {}

    def vote(name, x):
        if x is not None:
            votes[name] = x

    for n in (20, 50, 200):
        s = _v(d, f"sma{n}")
        vote(f"close vs SMA{n}", None if s is None else (1 if c > s else -1))
    e9, e21 = _v(d, "ema9"), _v(d, "ema21")
    vote("EMA9 vs EMA21", None if None in (e9, e21) else (1 if e9 > e21 else -1))
    s50, s200 = _v(d, "sma50"), _v(d, "sma200")
    vote("SMA50 vs SMA200", None if None in (s50, s200) else (1 if s50 > s200 else -1))
    vote("Supertrend", _v(d, "st_dir"))
    r = _v(d, "rsi14")
    vote("RSI", None if r is None else (1 if r < 30 or (50 < r < 70) else -1 if r > 70 or r < 45 else 0))
    m, ms = _v(d, "macd"), _v(d, "macd_signal")
    vote("MACD", None if None in (m, ms) else (1 if m > ms else -1))
    a, p, mi = _v(d, "adx14"), _v(d, "pdi"), _v(d, "mdi")
    vote("ADX / DI", None if None in (a, p, mi) else (0 if a < 20 else (1 if p > mi else -1)))
    bu, bl = _v(d, "bb_up"), _v(d, "bb_lo")
    vote("Bollinger", None if None in (bu, bl) else (1 if c < bl else -1 if c > bu else 0))
    if not votes:
        return None, None, {}
    score = round(sum(votes.values()) / len(votes), 3)
    label = ("STRONG_BUY" if score > 0.5 else "BUY" if score > 0.1 else "STRONG_SELL" if score < -0.5
             else "SELL" if score < -0.1 else "NEUTRAL")
    return score, label, votes


# ── per-stock snapshot for the screener / signal engine ─────────────────────

def weekly_bars(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    """Weekly OHLCV, weeks ending Friday, keeping only weeks complete by as_of (default: the last daily bar)."""
    if df is None or df.empty:
        return df
    w = (df.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
         .dropna(subset=["close"]))
    cut = pd.Timestamp(as_of) if as_of is not None else df.index[-1]
    return w[w.index <= cut]


def weekly_rating(df: pd.DataFrame, as_of=None) -> dict:
    """The technical rating, RSI and Supertrend direction on completed weekly bars."""
    w = weekly_bars(df, as_of)
    out = {"tech_rating_w": None, "tech_rating_w_label": None, "rsi_14_w": None, "supertrend_dir_w": None,
           "week_ending": None}
    if w is None or len(w) < MIN_WEEKS:
        return out
    d = indicators(w)
    score, label, _ = rating(d)
    r = _v(d, "rsi14")
    st = _v(d, "st_dir")
    out.update({"tech_rating_w": score, "tech_rating_w_label": label, "rsi_14_w": round(r, 2) if r is not None else None,
                "supertrend_dir_w": int(st) if st is not None else None, "week_ending": str(w.index[-1].date())})
    return out


def mtf_alignment(daily_label, weekly_label) -> str | None:
    """BULL / BEAR when the daily and weekly ratings agree on a side, MIXED otherwise, None without both."""
    if not daily_label or not weekly_label:
        return None
    if daily_label in _UP and weekly_label in _UP:
        return "BULL"
    if daily_label in _DOWN and weekly_label in _DOWN:
        return "BEAR"
    return "MIXED"


def weekly_agrees(direction, weekly_label) -> int | None:
    """1 when the weekly rating is on the signal's side, 0 when it is not, None without a weekly rating."""
    if not weekly_label or direction not in ("BULL", "BEAR"):
        return None
    return int(weekly_label in (_UP if direction == "BULL" else _DOWN))


def snapshot(df: pd.DataFrame, bench: pd.Series | None = None) -> dict | None:
    """Flat dict of the last bar: indicators, scan hits (scan_<key> = 1/0), patterns, rating."""
    if df is None or len(df) < MIN_BARS:
        return None
    d = indicators(df, bench)
    hits = run_scans(d)
    pats = candles(d)
    score, label, _ = rating(d)
    c = _v(d, "close")
    pct = lambda a, b: round((a / b - 1) * 100, 2) if a is not None and b else None
    out = {
        "tech_rating": score, "tech_rating_label": label,
        "rsi_14": round(_v(d, "rsi14"), 2) if _v(d, "rsi14") is not None else None,
        "macd_hist": round(_v(d, "macd_hist"), 4) if _v(d, "macd_hist") is not None else None,
        "adx_14": round(_v(d, "adx14"), 2) if _v(d, "adx14") is not None else None,
        "supertrend_dir": int(_v(d, "st_dir")) if _v(d, "st_dir") is not None else None,
        "atr_pct": round(_v(d, "atr14") / c * 100, 2) if _v(d, "atr14") and c else None,
        "pct_from_sma50": pct(c, _v(d, "sma50")), "pct_from_sma200": pct(c, _v(d, "sma200")),
        "above_200dma": None if _v(d, "sma200") is None else int(c > _v(d, "sma200")),
        "bb_width_pct": round(_v(d, "bb_width") * 100, 2) if _v(d, "bb_width") is not None else None,
        "vol_ratio": round(_v(d, "vol_ratio"), 2) if _v(d, "vol_ratio") is not None else None,
        "rs_63_pct": round(_v(d, "rs63") * 100, 2) if _v(d, "rs63") is not None else None,
        "return_1m_pct": pct(c, _v(d, "close", -22)) if len(d) > 22 else None,
        "return_3m_pct": pct(c, _v(d, "close", -64)) if len(d) > 64 else None,
        "patterns": ", ".join(p for p, _ in pats) or None,
        "bull_signals": sum(1 for h in hits if h[2] == "BULL"),
        "bear_signals": sum(1 for h in hits if h[2] == "BEAR"),
        "signals": ", ".join(h[1] for h in hits) or None,
        "_rs_raw": rs_raw(d["close"]),
        "_hits": hits, "_patterns": pats,
        "_atr": _v(d, "atr14"), "_close": c, "_low20": float(d["low"].iloc[-20:].min()),
        "_high20": float(d["high"].iloc[-20:].max()),
    }
    dp, dr = _v(d, "delivery_pct"), _v(d, "deliv_ratio")
    out["delivery_pct"] = round(dp, 2) if dp is not None else None
    out["delivery_ratio"] = round(dr, 2) if dr is not None else None
    rl, rh = _v(d, "rs_line"), _v(d, "rs_line_prior_hi")
    out["rs_line_at_high"] = None if rl is None or rh is None else int(rl > rh)
    found = P.setups(d)
    out["chart_patterns"] = "; ".join(t for _, t in found) or None
    out["vcp_setup"] = int(any(lbl == "VCP setup" for lbl, _ in found))
    wk = weekly_rating(df)
    out.update({k: wk[k] for k in ("tech_rating_w", "tech_rating_w_label", "rsi_14_w", "supertrend_dir_w")})
    out["mtf_alignment"] = mtf_alignment(label, wk["tech_rating_w_label"])
    for key in SCANS:
        out[f"scan_{key}"] = int(any(h[0] == key for h in hits))
    return out
