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

compute(df_daily, bench_daily, bars_intraday=None) -> dict of the columns above.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

COLUMNS = ("vwap_20d", "vwap_session", "vwap_dev_pct", "vp_poc", "vp_vah", "vp_val", "vp_basis",
           "sr_support", "sr_support_touches", "sr_support_dist_pct", "sr_resistance", "sr_resistance_touches",
           "sr_resistance_dist_pct", "beta_60", "beta_downside", "beta_long", "beta_long_sessions", "bri",
           "weekly_rsi", "weekly_trend", "monthly_trend", "mtf_alignment",
           "supertrend", "supertrend_dir", "ichimoku_tenkan", "ichimoku_kijun", "ichimoku_span_a",
           "ichimoku_span_b", "keltner_upper", "keltner_lower", "donchian_upper", "donchian_lower", "mfi_14",
           "cmf_20", "roc_10", "aroon_up", "aroon_down", "psar")


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


def compute(df, bench=None, bars=None) -> dict:
    """df: daily bars (date, open, high, low, close, volume) ascending, >= 20 rows."""
    df = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)
    if len(df) < 20:
        return {}
    out = {}
    for fn in (lambda: vwap(df, bars), lambda: volume_profile(df, bars), lambda: support_resistance(df),
               lambda: betas(df, bench), lambda: multi_timeframe(df), lambda: extended(df)):
        try:
            out.update(fn())
        except Exception:                               # one family failing never blocks the rest
            continue
    return {k: out.get(k) for k in COLUMNS}
