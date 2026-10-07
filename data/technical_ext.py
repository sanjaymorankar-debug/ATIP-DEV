"""
ATIP -- extended technical analysis (W21): the indicators the tracker lists as
missing, computed with pandas / numpy only (no new dependency) and stored in
technical_ext (symbol, date). Nothing here changes the Technical Score unless
config technical.vwap_in_score is true (see vwap_score()).

    TA-03 VWAP              vwap_20d: 20-session volume-weighted typical price (daily bars)
                            vwap_session: the session VWAP from stored intraday bars
                            (intraday_bars, DP-03) when present; vwap_dev_pct: close vs VWAP
    TA-04 Volume profile    vp_poc / vp_vah / vp_val over 20 sessions (24 price bins, 70%
                            value area); intraday bars when stored, else daily bars with
                            each bar's volume spread evenly over its high-low range
    TA-06 Support/resistance  swing pivots (5-bar fractals) over 120 sessions, clustered
                            within 1.5%; nearest level below / above the close, its touches
                            and distance (%)
    TA-09 Beta              beta_60 (60-session), beta_downside (down days of the benchmark),
                            beta_long (all stored history, >= 500 sessions; the tracker's
                            "3y beta" needs ~750 -- limited by prices_daily retention,
                            db/purge.py DEFAULT_LONG_DAYS=600) with beta_long_sessions
    SC-16 Beta Risk Index   bri 0-100: 50% beta level (0.5 -> 20, 1.0 -> 50, 2.0 -> 100),
                            30% downside beta, 20% instability (std of 60-session betas)
    TA-10 Multi-timeframe   weekly RSI(14), weekly trend (close vs 10-week EMA), monthly
                            trend (close vs 10-month SMA), mtf_alignment -3..+3 (daily 50-EMA,
                            weekly, monthly: +1 above, -1 below)
    TA-11 Extended catalogue  Supertrend(10,3) level + direction, Ichimoku tenkan / kijun /
                            span A / span B, Keltner(20, 2 ATR), Donchian(20), MFI(14),
                            CMF(20), ROC(10), Aroon(25) up / down, Parabolic SAR
                            (the stoch / Williams %R / CCI columns of technical_indicators
                            are now filled by data/technical.py as well)

W39 additions -- STORED AND SHOWN ONLY: neither feeds any score, signal or strategy feature
(the owner decides later whether they should):

    TA-08b Sector-relative strength  rs_sector_63 / rs_sector_126: the stock's return over 63 /
                            126 sessions minus its sector index's return over the same sessions,
                            in % (date-matched, like scores/engine.compute_relative_strength's
                            20-session RS vs NIFTY50). The sector index comes from the NSE
                            industry (data/index_constituents.get_symbol_industry_map) through
                            SECTOR_INDEX_BY_INDUSTRY; a stock whose sector has no stored index
                            is measured against NIFTY50 instead and rs_sector_index says
                            "NIFTY50". rs_sector_pctile: the stock's percentile (0 = weakest,
                            100 = strongest) of rs_sector_126 among the stocks of the same
                            industry and sector index on that date -- needs the whole session,
                            so compute() leaves it None and store_sector_percentiles() fills it
                            after the per-date batch.
    TA-05  Swing Fibonacci  the last confirmed swing over 120 sessions from fractal pivots (the
                            support_resistance approach, SWING_K bars each side), consecutive
                            pivots of one kind collapsed to the more extreme one (zig-zag);
                            fib_swing_dir UP when the swing low precedes the swing high (levels
                            measured down from the high), DOWN otherwise (measured up from the
                            low); fib_382 / fib_500 / fib_618 and the level nearest the close.
                            The 52-week Fibonacci of technical_indicators (data/technical.py,
                            same fib_* names, a different table) is unchanged.

compute(df_daily, bench_daily, bars_intraday=None, sector=None) -> dict of the columns above.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

W21_COLUMNS = ("vwap_20d", "vwap_session", "vwap_dev_pct", "vp_poc", "vp_vah", "vp_val", "vp_basis",
               "sr_support", "sr_support_touches", "sr_support_dist_pct", "sr_resistance", "sr_resistance_touches",
               "sr_resistance_dist_pct", "beta_60", "beta_downside", "beta_long", "beta_long_sessions", "bri",
               "weekly_rsi", "weekly_trend", "monthly_trend", "mtf_alignment",
               "supertrend", "supertrend_dir", "ichimoku_tenkan", "ichimoku_kijun", "ichimoku_span_a",
               "ichimoku_span_b", "keltner_upper", "keltner_lower", "donchian_upper", "donchian_lower", "mfi_14",
               "cmf_20", "roc_10", "aroon_up", "aroon_down", "psar")
# W39 (db/schema_w39.py W39_COLUMNS["technical_ext"]): stored and shown, never scored
SECTOR_RS_COLUMNS = ("rs_sector_index", "rs_sector_63", "rs_sector_126", "rs_sector_pctile")
SWING_FIB_COLUMNS = ("fib_swing_high", "fib_swing_low", "fib_swing_dir", "fib_382", "fib_500", "fib_618",
                     "fib_nearest", "fib_nearest_dist_pct")
COLUMNS = W21_COLUMNS + SECTOR_RS_COLUMNS + SWING_FIB_COLUMNS

# ── TA-08b: NSE industry -> sector index ───────────────────────────────────
# Keys are the Industry column of NSE's ind_nifty500list.csv (lower-cased; matching is
# case-insensitive), values the synthetic symbols data/dhan.py stores the sector index
# history under in prices_daily (INDEX_SERIES_SYMBOLS). Only indices ATIP actually stores
# are used, so some pairings are the closest stored proxy rather than an exact match:
#   Healthcare          -> NIFTYPHARMA   (Nifty Healthcare is not stored; pharma dominates it)
#   Power               -> NIFTYENERGY   (Nifty Energy carries the power utilities)
#   Financial Services  -> NIFTYBANK     (the only financial index stored; banks dominate the
#                                         industry) -- the PSU banks go to NIFTYPSUBANK through
#                                         SECTOR_INDEX_SYMBOL_OVERRIDES below
# Industries with no stored index -- Capital Goods, Chemicals, Construction, Construction
# Materials, Consumer Durables, Consumer Services, Diversified, Forest Materials, Media
# Entertainment & Publication, Services, Telecommunication, Textiles -- and stocks whose
# industry is unknown (not in the Nifty 500 list), or whose sector series is missing / stale,
# are measured against FALLBACK_SECTOR_INDEX, and rs_sector_index records that.
FALLBACK_SECTOR_INDEX = "NIFTY50"
SECTOR_INDEX_BY_INDUSTRY = {
    "information technology": "NIFTYIT",
    "healthcare": "NIFTYPHARMA",
    "automobile and auto components": "NIFTYAUTO",
    "fast moving consumer goods": "NIFTYFMCG",
    "metals & mining": "NIFTYMETAL",
    "realty": "NIFTYREALTY",
    "oil gas & consumable fuels": "NIFTYENERGY",
    "power": "NIFTYENERGY",
    "financial services": "NIFTYBANK",
    # labels of NSE's pre-2021 industry classification, should an old list be cached
    "it": "NIFTYIT", "pharma": "NIFTYPHARMA", "automobile": "NIFTYAUTO", "consumer goods": "NIFTYFMCG",
    "metals": "NIFTYMETAL", "oil & gas": "NIFTYENERGY", "energy": "NIFTYENERGY",
}
# The Nifty PSU Bank constituents: NSE files them under Financial Services with every other
# lender, but their own index is stored, so they are measured against it.
SECTOR_INDEX_SYMBOL_OVERRIDES = {s: "NIFTYPSUBANK" for s in (
    "SBIN", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "INDIANB", "BANKINDIA", "IOB", "CENTRALBK",
    "UCOBANK", "MAHABANK", "PSB")}
RS_SECTOR_WINDOWS = (63, 126)          # sessions (~3 and ~6 months)
SECTOR_MAX_LAG_SESSIONS = 5            # an index series whose last bar is older than this is stale
SECTOR_PCTILE_MIN_PEERS = 3            # fewer stocks in the group -> no percentile

# ── TA-05: swing-anchored Fibonacci ────────────────────────────────────────
FIB_SWING_LOOKBACK = 120               # sessions searched for the last swing (as support_resistance)
SWING_K = 5                            # a swing point is the extreme of 2k+1 = 11 sessions, so a
                                       # 2-3 day wiggle is not a swing; confirmed k sessions later
FIB_RATIOS = (("fib_382", 0.382), ("fib_500", 0.5), ("fib_618", 0.618))


def _f(x, nd=4):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else round(v, nd)


def _rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ── TA-03 VWAP ─────────────────────────────────────────────────────────────
def vwap(df, bars=None, n=20) -> dict:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].fillna(0)
    w = v.tail(n).sum()
    out = {"vwap_20d": _f((tp.tail(n) * v.tail(n)).sum() / w, 2) if w > 0 else None, "vwap_session": None}
    if bars is not None and len(bars):
        last_day = bars["datetime"].dt.date.max()
        b = bars[bars["datetime"].dt.date == last_day]
        bv = b["volume"].fillna(0)
        if bv.sum() > 0:
            out["vwap_session"] = _f((((b["high"] + b["low"] + b["close"]) / 3) * bv).sum() / bv.sum(), 2)
    ref = out["vwap_session"] or out["vwap_20d"]
    c = float(df["close"].iloc[-1])
    out["vwap_dev_pct"] = _f((c / ref - 1) * 100, 3) if ref else None
    return out


def vwap_score(ind: dict) -> float | None:
    """The Technical Score's VWAP component (0-100): above VWAP is constructive, far
    above is extended. Only used when config technical.vwap_in_score is true."""
    d = ind.get("vwap_dev_pct")
    if d is None:
        return None
    if d < -3:
        return 35.0
    if d < 0:
        return 50.0 + d * 5
    if d <= 3:
        return 60.0 + d * 8
    return max(30.0, 84.0 - (d - 3) * 6)


# ── TA-04 volume profile ───────────────────────────────────────────────────
def volume_profile(df, bars=None, n=20, bins=24, area=0.70) -> dict:
    if bars is not None and len(bars):
        days = sorted(bars["datetime"].dt.date.unique())[-n:]
        src = bars[bars["datetime"].dt.date.isin(days)]
        basis = "intraday"
        lows, highs, vols = src["low"].values, src["high"].values, src["volume"].fillna(0).values
    else:
        src = df.tail(n)
        basis = "daily"
        lows, highs, vols = src["low"].values, src["high"].values, src["volume"].fillna(0).values
    if len(lows) == 0:
        return {"vp_poc": None, "vp_vah": None, "vp_val": None, "vp_basis": None}
    lo, hi = float(np.nanmin(lows)), float(np.nanmax(highs))
    if not (hi > lo) or np.nansum(vols) <= 0:
        return {"vp_poc": None, "vp_vah": None, "vp_val": None, "vp_basis": basis}
    edges = np.linspace(lo, hi, bins + 1)
    hist = np.zeros(bins)
    for l_, h_, v_ in zip(lows, highs, vols):
        if not v_ or np.isnan(l_) or np.isnan(h_):
            continue
        a, b = np.searchsorted(edges, l_, "right") - 1, np.searchsorted(edges, h_, "left")
        a, b = max(0, min(bins - 1, a)), max(1, min(bins, b))
        if b <= a:
            b = a + 1
        hist[a:b] += v_ / (b - a)
    poc = int(hist.argmax())
    lo_i = hi_i = poc
    tot, acc = hist.sum(), hist[poc]
    while acc < area * tot and (lo_i > 0 or hi_i < bins - 1):
        down = hist[lo_i - 1] if lo_i > 0 else -1
        up = hist[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            acc += hist[hi_i]
        else:
            lo_i -= 1
            acc += hist[lo_i]
    mid = lambda i: (edges[i] + edges[i + 1]) / 2                                          # noqa: E731
    return {"vp_poc": _f(mid(poc), 2), "vp_vah": _f(edges[hi_i + 1], 2), "vp_val": _f(edges[lo_i], 2),
            "vp_basis": basis}


# ── TA-06 support / resistance ─────────────────────────────────────────────
def support_resistance(df, lookback=120, k=2, tol_pct=1.5) -> dict:
    d = df.tail(lookback).reset_index(drop=True)
    h, l_ = d["high"].values, d["low"].values
    piv = []
    for i in range(k, len(d) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            piv.append(float(h[i]))
        if l_[i] == min(l_[i - k:i + k + 1]):
            piv.append(float(l_[i]))
    close = float(df["close"].iloc[-1])
    out = {"sr_support": None, "sr_support_touches": None, "sr_support_dist_pct": None,
           "sr_resistance": None, "sr_resistance_touches": None, "sr_resistance_dist_pct": None}
    if not piv:
        return out
    piv.sort()
    clusters = [[piv[0]]]
    for p in piv[1:]:
        # compare with the cluster's mean, not its last member: chaining would let a
        # slow drift of pivots merge into one band many percent wide
        if (p / (sum(clusters[-1]) / len(clusters[-1])) - 1) * 100 <= tol_pct / 2:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = [(sum(c) / len(c), len(c)) for c in clusters]
    # a level tested at least twice is support / resistance; a lone pivot only counts
    # when that side has no confirmed level
    below = [x for x in levels if x[0] < close and x[1] >= 2] or [x for x in levels if x[0] < close]
    above = [x for x in levels if x[0] > close and x[1] >= 2] or [x for x in levels if x[0] > close]
    if below:
        lv, t = max(below, key=lambda x: x[0])
        out.update({"sr_support": _f(lv, 2), "sr_support_touches": t, "sr_support_dist_pct": _f((close / lv - 1) * 100, 3)})
    if above:
        lv, t = min(above, key=lambda x: x[0])
        out.update({"sr_resistance": _f(lv, 2), "sr_resistance_touches": t,
                    "sr_resistance_dist_pct": _f((lv / close - 1) * 100, 3)})
    return out


# ── TA-09 beta + SC-16 Beta Risk Index ─────────────────────────────────────
def betas(df, bench) -> dict:
    out = {"beta_60": None, "beta_downside": None, "beta_long": None, "beta_long_sessions": None, "bri": None}
    if bench is None or len(bench) < 62:
        return out
    m = pd.merge(df[["date", "close"]], bench[["date", "close"]], on="date", suffixes=("_s", "_b")).dropna()
    r = m[["close_s", "close_b"]].pct_change().dropna()
    if len(r) < 60:
        return out

    def beta(x):
        vb = x["close_b"].var()
        return float(np.cov(x["close_s"], x["close_b"])[0, 1] / vb) if vb > 0 else None
    out["beta_60"] = _f(beta(r.tail(60)), 3)
    dn = r[r["close_b"] < 0]
    out["beta_downside"] = _f(beta(dn.tail(250)), 3) if len(dn) >= 30 else None
    out["beta_long_sessions"] = len(r)
    b1 = beta(r.tail(250))
    if len(r) >= 500:
        out["beta_long"] = _f(beta(r), 3)
    roll = [beta(r.iloc[i - 60:i]) for i in range(60, len(r) + 1, 5)][-50:]
    roll = [x for x in roll if x is not None]
    if b1 is not None:
        lvl = max(0.0, min(100.0, 50 + (b1 - 1.0) * 50 if b1 >= 1 else 20 + (b1 - 0.5) * 60))
        dsb = out["beta_downside"] if out["beta_downside"] is not None else b1
        dsc = max(0.0, min(100.0, 50 + (dsb - 1.0) * 50 if dsb >= 1 else 20 + (dsb - 0.5) * 60))
        inst = max(0.0, min(100.0, float(np.std(roll)) * 200)) if len(roll) >= 5 else 50.0
        out["bri"] = _f(0.5 * lvl + 0.3 * dsc + 0.2 * inst, 2)
    return out


# ── TA-10 multi-timeframe ──────────────────────────────────────────────────
def multi_timeframe(df) -> dict:
    s = df.set_index(pd.to_datetime(df["date"]))["close"]
    wk = s.resample("W-FRI").last().dropna()
    try:
        mo = s.resample("ME").last().dropna()
    except ValueError:                                  # pandas < 2.2 spells month-end "M"
        mo = s.resample("M").last().dropna()
    out = {"weekly_rsi": None, "weekly_trend": None, "monthly_trend": None, "mtf_alignment": None}
    score, parts = 0, 0
    if len(s) >= 50:
        score += 1 if s.iloc[-1] > s.ewm(span=50, adjust=False).mean().iloc[-1] else -1
        parts += 1
    if len(wk) >= 15:
        out["weekly_rsi"] = _f(_rsi(wk).iloc[-1], 2)
        up = wk.iloc[-1] > wk.ewm(span=10, adjust=False).mean().iloc[-1]
        out["weekly_trend"] = "UP" if up else "DOWN"
        score += 1 if up else -1
        parts += 1
    if len(mo) >= 10:
        up = mo.iloc[-1] > mo.rolling(10).mean().iloc[-1]
        out["monthly_trend"] = "UP" if up else "DOWN"
        score += 1 if up else -1
        parts += 1
    out["mtf_alignment"] = score if parts else None
    return out


# ── TA-11 extended catalogue ───────────────────────────────────────────────
def extended(df) -> dict:
    h, l_, c, v = df["high"], df["low"], df["close"], df["volume"].fillna(0)
    out = {}
    atr10 = _atr(h, l_, c, 10).values
    mid = ((h + l_) / 2).values
    ub, lb, ca = mid + 3 * atr10, mid - 3 * atr10, c.values
    fu, fl = ub.copy(), lb.copy()
    st, dirn = np.full(len(df), np.nan), np.ones(len(df), dtype=int)
    for i in range(1, len(df)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or ca[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or ca[i - 1] < fl[i - 1]) else fl[i - 1]
        dirn[i] = 1 if ca[i] > fu[i - 1] else -1 if ca[i] < fl[i - 1] else dirn[i - 1]
        st[i] = fl[i] if dirn[i] == 1 else fu[i]
    out["supertrend"], out["supertrend_dir"] = _f(st[-1], 2), int(dirn[-1])
    hh = lambda n: h.rolling(n).max()                                                      # noqa: E731
    ll = lambda n: l_.rolling(n).min()                                                     # noqa: E731
    ten, kij = (hh(9) + ll(9)) / 2, (hh(26) + ll(26)) / 2
    out["ichimoku_tenkan"], out["ichimoku_kijun"] = _f(ten.iloc[-1], 2), _f(kij.iloc[-1], 2)
    out["ichimoku_span_a"] = _f(((ten + kij) / 2).iloc[-1], 2)
    out["ichimoku_span_b"] = _f(((hh(52) + ll(52)) / 2).iloc[-1], 2)
    ema20, atr14 = c.ewm(span=20, adjust=False).mean(), _atr(h, l_, c, 14)
    out["keltner_upper"], out["keltner_lower"] = _f((ema20 + 2 * atr14).iloc[-1], 2), _f((ema20 - 2 * atr14).iloc[-1], 2)
    out["donchian_upper"], out["donchian_lower"] = _f(hh(20).iloc[-1], 2), _f(ll(20).iloc[-1], 2)
    tp = (h + l_ + c) / 3
    mf = tp * v
    pos = mf.where(tp > tp.shift(), 0).rolling(14).sum()
    neg = mf.where(tp < tp.shift(), 0).rolling(14).sum()
    out["mfi_14"] = _f((100 - 100 / (1 + pos / neg.replace(0, np.nan))).iloc[-1], 2)
    mfm = ((c - l_) - (h - c)) / (h - l_).replace(0, np.nan)
    out["cmf_20"] = _f(((mfm * v).rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)).iloc[-1], 4)
    out["roc_10"] = _f((c / c.shift(10) - 1).iloc[-1] * 100, 3)
    if len(df) >= 26:
        w = df.tail(26)
        out["aroon_up"] = _f((25 - (25 - int(np.argmax(w["high"].values)))) / 25 * 100, 2)
        out["aroon_down"] = _f((25 - (25 - int(np.argmin(w["low"].values)))) / 25 * 100, 2)
    else:
        out["aroon_up"] = out["aroon_down"] = None
    # Parabolic SAR (0.02 step, 0.2 max)
    if len(df) >= 5:
        ha, la = h.values.astype(float), l_.values.astype(float)
        up, af, ep, sar = True, 0.02, ha[0], la[0]
        for i in range(1, len(df)):
            sar = sar + af * (ep - sar)
            if up:
                sar = min(sar, la[i - 1], la[max(0, i - 2)])
                if la[i] < sar:
                    up, sar, ep, af = False, ep, la[i], 0.02
                elif ha[i] > ep:
                    ep, af = ha[i], min(0.2, af + 0.02)
            else:
                sar = max(sar, ha[i - 1], ha[max(0, i - 2)])
                if ha[i] > sar:
                    up, sar, ep, af = True, ep, ha[i], 0.02
                elif la[i] < ep:
                    ep, af = la[i], min(0.2, af + 0.02)
        out["psar"] = _f(sar, 2)
    else:
        out["psar"] = None
    return out


# ── TA-08b sector-relative strength (stored, not scored) ───────────────────
def sector_index_for(symbol=None, industry=None) -> str | None:
    """The stored sector index a stock is measured against: the symbol override (PSU banks),
    else SECTOR_INDEX_BY_INDUSTRY for its NSE industry; None when its sector has no index."""
    sym = str(symbol or "").strip().upper()
    if sym in SECTOR_INDEX_SYMBOL_OVERRIDES:
        return SECTOR_INDEX_SYMBOL_OVERRIDES[sym]
    return SECTOR_INDEX_BY_INDUSTRY.get(str(industry or "").strip().lower())


def _date_key(s):
    return s.astype(str).str[:10]


def _rs_vs(df, idx) -> dict | None:
    """{63: rs, 126: rs} of the stock vs one index series (date, close), or None when the series
    is missing, stale or too short for even the shorter window. As compute_relative_strength:
    the two series are matched on date, and N sessions back is N rows back in the matched set."""
    if idx is None or len(idx) == 0 or "date" not in idx.columns or "close" not in idx.columns:
        return None
    s = pd.DataFrame({"k": _date_key(df["date"]), "s": pd.to_numeric(df["close"], errors="coerce")})
    b = pd.DataFrame({"k": _date_key(idx["date"]), "b": pd.to_numeric(idx["close"], errors="coerce")})
    s, b = s[s["s"] > 0], b[b["b"] > 0]
    m = s.merge(b, on="k").drop_duplicates("k", keep="last").sort_values("k").reset_index(drop=True)
    if len(m) < min(RS_SECTOR_WINDOWS) + 1:
        return None
    if m["k"].iloc[-1] not in set(s.sort_values("k")["k"].tail(SECTOR_MAX_LAG_SESSIONS + 1)):
        return None                                     # the index stopped updating: not a comparison
    out = {}
    for n in RS_SECTOR_WINDOWS:
        if len(m) < n + 1:
            out[n] = None
            continue
        s0, sn, b0, bn = m["s"].iloc[-1], m["s"].iloc[-1 - n], m["b"].iloc[-1], m["b"].iloc[-1 - n]
        out[n] = _f(((s0 / sn - 1) - (b0 / bn - 1)) * 100, 3)
    return out


def sector_relative_strength(df, symbol=None, industry=None, series=None, bench=None) -> dict:
    """rs_sector_index / rs_sector_63 / rs_sector_126 for one stock. `series` maps a sector index
    symbol to its stored daily series (date, close); `bench` is the NIFTY50 series. A stock whose
    sector has no index, or whose index series is missing or stale, falls back to NIFTY50 and
    rs_sector_index says so. rs_sector_pctile is always None here (store_sector_percentiles)."""
    out = {c: None for c in SECTOR_RS_COLUMNS}
    series = series or {}
    idx = sector_index_for(symbol, industry)
    rs = _rs_vs(df, series.get(idx)) if idx else None
    if rs is None:
        idx = FALLBACK_SECTOR_INDEX
        for cand in (bench, series.get(FALLBACK_SECTOR_INDEX)):
            rs = _rs_vs(df, cand)
            if rs is not None:
                break
    if rs is None:
        return out
    out.update({"rs_sector_index": idx, "rs_sector_63": rs.get(63), "rs_sector_126": rs.get(126)})
    return out


def sector_percentiles(rows, industries, min_peers=SECTOR_PCTILE_MIN_PEERS) -> dict:
    """rows: (symbol, rs_sector_index, rs_sector_126) for one date -> {symbol: percentile or None}.
    Peers are the stocks of the same NSE industry measured against the same index (so a PSU bank
    is ranked among PSU banks, a fallback stock among its own industry vs NIFTY50). Percentile as
    quant/normalize.percentile: 0-based rank (ties share the average) / (n - 1) x 100, so the
    weakest is 0 and the strongest 100. Unknown industry or fewer than min_peers -> None."""
    out, groups = {}, {}
    for sym, idx, v in rows:
        out[sym] = None
        ind = str((industries or {}).get(sym) or "").strip().lower()
        v = _f(v, 6)
        if not ind or not idx or v is None:
            continue
        groups.setdefault((ind, idx), []).append((sym, v))
    for members in groups.values():
        if len(members) < max(2, min_peers):
            continue
        members.sort(key=lambda x: x[1])
        i = 0
        while i < len(members):
            j = i
            while j + 1 < len(members) and members[j + 1][1] == members[i][1]:
                j += 1
            for sym, _ in members[i:j + 1]:
                out[sym] = round((i + j) / 2 / (len(members) - 1) * 100, 2)
            i = j + 1
    return out


def load_sector_context(conn, trade_date, industries=None, rows=400) -> dict:
    """What compute() needs for TA-08b, loaded once per run by the caller that loads the NIFTY50
    benchmark: {"industries": symbol -> NSE industry, "series": sector index symbol -> its stored
    daily series up to trade_date}. Never raises: missing pieces just mean the NIFTY50 fallback."""
    if industries is None:
        try:
            from data.index_constituents import get_symbol_industry_map
            industries = get_symbol_industry_map() or {}
        except Exception:
            industries = {}
    series = {}
    wanted = set(SECTOR_INDEX_BY_INDUSTRY.values()) | set(SECTOR_INDEX_SYMBOL_OVERRIDES.values())
    for idx in sorted(wanted):
        try:
            s = pd.read_sql("SELECT date, close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 "
                            "ORDER BY date DESC LIMIT ?", conn, params=(idx, str(trade_date), int(rows)))
        except Exception:
            continue
        if not s.empty:
            series[idx] = s.iloc[::-1].reset_index(drop=True)
    return {"industries": industries, "series": series}


def sector_input(ctx, symbol) -> dict | None:
    """compute()'s `sector` argument for one symbol from a load_sector_context() result."""
    if not ctx:
        return None
    return {"symbol": symbol, "industry": (ctx.get("industries") or {}).get(symbol), "series": ctx.get("series")}


def store_sector_percentiles(conn, trade_date, industries) -> int:
    """After the per-date batch: rs_sector_pctile for every technical_ext row of trade_date (all
    the rows stored for that date, so a one-symbol re-run still ranks against its stored peers).
    Rows with no peer group are set to NULL. Returns how many got a percentile."""
    d = str(trade_date)
    rows = conn.execute("SELECT symbol, rs_sector_index, rs_sector_126 FROM technical_ext WHERE date=?",
                        (d,)).fetchall()
    pct = sector_percentiles([(r[0], r[1], r[2]) for r in rows], industries or {})
    for sym, v in pct.items():
        conn.execute("UPDATE technical_ext SET rs_sector_pctile=? WHERE symbol=? AND date=?", (v, sym, d))
    return sum(1 for v in pct.values() if v is not None)


# ── TA-05 swing-anchored Fibonacci (stored, not scored) ────────────────────
def swing_fibonacci(df, lookback=FIB_SWING_LOOKBACK, k=SWING_K) -> dict:
    """The last confirmed swing in `lookback` sessions and its 38.2 / 50 / 61.8 % retracements.
    Pivots are k-bar fractals on high / low (support_resistance's test); runs of one kind are
    collapsed to their most extreme member (a lower high after a high, with no swing low between,
    does not end the swing), and the last two alternating pivots are the swing. UP when the low
    came first: levels = high - (high - low) x ratio; DOWN: levels = low + (high - low) x ratio.
    fib_nearest names the level closest to the last close; fib_nearest_dist_pct is the close's
    distance from it, (close / level - 1) x 100, positive when the close is above it."""
    out = {c: None for c in SWING_FIB_COLUMNS}
    d = df.tail(lookback).reset_index(drop=True)
    h, l_ = d["high"].astype(float).values, d["low"].astype(float).values
    piv = []
    for i in range(k, len(d) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            piv.append((i, "H", float(h[i])))
        if l_[i] == min(l_[i - k:i + k + 1]):
            piv.append((i, "L", float(l_[i])))
    zz = []
    for p in piv:
        if zz and zz[-1][1] == p[1]:
            if (p[1] == "H" and p[2] >= zz[-1][2]) or (p[1] == "L" and p[2] <= zz[-1][2]):
                zz[-1] = p                              # ties keep the more recent pivot
        else:
            zz.append(p)
    if len(zz) < 2:
        return out
    a, b = zz[-2], zz[-1]
    hi, lo = (a, b) if a[1] == "H" else (b, a)
    high, low = hi[2], lo[2]
    if not high > low:
        return out
    up = lo[0] < hi[0]
    rng = high - low
    levels = {name: (high - rng * r) if up else (low + rng * r) for name, r in FIB_RATIOS}
    close = float(df["close"].iloc[-1])
    out.update({"fib_swing_high": _f(high, 2), "fib_swing_low": _f(low, 2), "fib_swing_dir": "UP" if up else "DOWN"})
    out.update({name: _f(v, 2) for name, v in levels.items()})
    if close == close and close > 0:
        name = min(levels, key=lambda x: abs(close - levels[x]))
        out["fib_nearest"] = name
        out["fib_nearest_dist_pct"] = _f((close / levels[name] - 1) * 100, 3) if levels[name] > 0 else None
    return out


def compute(df, bench=None, bars=None, sector=None) -> dict:
    """df: daily bars (date, open, high, low, close, volume) ascending, >= 20 rows.
    sector: optional {"symbol", "industry", "series"} (sector_input()) for TA-08b; without it
    the rs_sector_* keys are None. Every other key is computed exactly as before."""
    df = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)
    if len(df) < 20:
        return {}
    out = {}
    sec = sector or {}
    for fn in (lambda: vwap(df, bars), lambda: volume_profile(df, bars), lambda: support_resistance(df),
               lambda: betas(df, bench), lambda: multi_timeframe(df), lambda: extended(df),
               lambda: (sector_relative_strength(df, sec.get("symbol"), sec.get("industry"), sec.get("series"),
                                                 bench) if sector is not None else {}),
               lambda: swing_fibonacci(df)):
        try:
            out.update(fn())
        except Exception:                               # one family failing never blocks the rest
            continue
    return {k: out.get(k) for k in COLUMNS}
