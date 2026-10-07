"""
W39 Phase 2 (item 4) — chart patterns from swing pivots: Darvas box, VCP (volatility contraction),
double bottom, ascending triangle, head-and-shoulders top. Pure functions over an indicators()
frame (open, high, low, close, volume, vol_ratio, sma50, sma200), oldest first; no database.

Every pattern is found on the bars BEFORE today and only today's bar decides a breakout, so a
pattern is never shaped by the bar that triggers it, and a stored signal is reproducible from the
bars of its day. Pivots are confirmed swing points: a swing high at i is higher than the 3 bars
before it and not exceeded by the 3 bars after it (so it is known 3 sessions later); lows mirrored.

    darvas            the latest new high (highest of 20 sessions, and within 3 % of the 52-week high:
                      Darvas only traded stocks making new highs) not exceeded since, with at least 3
                      sessions of confirmation; the box bottom is the lowest low since, which must
                      itself have held 3 sessions. 5+ sessions in the box, at most 25 % tall.
                      Breakout: today's close above the top (yesterday's not); breakdown: below the bottom.
    vcp               Minervini's volatility contraction: 2-4 pullbacks (swing high to the lowest swing
                      low before the next swing high), each shallower than the one before, the last
                      at most 12 % deep, the first at most 40 %, their highs within 15 % of the base
                      top; volume drying up (10-session average below 85 % of the 50-session one); the
                      stock in an up-trend (above its 50-DMA, 50-DMA above the 200-DMA when known).
                      Pivot = the last pullback's high. Breakout: close above it on >= 1.4x volume.
    double_bottom     two swing lows within 3 % of each other, 10-60 sessions apart, the second within the
                      last 30 sessions, after a 10 %+ decline, with a middle peak 6 %+ above them (the
                      neckline) not closed above since.
                      Breakout: close above the neckline.
    ascending_triangle  2+ swing highs within 1.5 % of each other (flat resistance) spread over 10+
                      sessions, and 2+ rising swing lows since the first of them. Breakout: close
                      above the resistance.
    head_shoulders    three swing highs, the middle (head) 3 %+ above both shoulders, the shoulders
                      within 8 % of each other; the neckline joins the two troughs and is extended to
                      today. Breakdown (bearish): close below the neckline.

These are rule-based approximations of patterns traders draw by eye (the thresholds are ATIP's,
documented above). Each breakout is a scan in research/technicals.py SCANS, so it gets signals,
levels, the forward record and screener fields like any other scan; research/tech_signals.py holds
its alerts back until the scan has 30 closed signals with a positive average R.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SWING = 3                       # bars each side of a confirmed swing point
_CACHE = "_chart_patterns"


def swings(high: np.ndarray, low: np.ndarray, k: int = SWING) -> tuple:
    """(swing-high indices, swing-low indices), each confirmed by k bars on both sides."""
    n = len(high)
    hi, lo = [], []
    for i in range(k, n - k):
        if high[i] > high[i - k:i].max() and high[i] >= high[i + 1:i + k + 1].max():
            hi.append(i)
        if low[i] < low[i - k:i].min() and low[i] <= low[i + 1:i + k + 1].min():
            lo.append(i)
    return hi, lo


def _base(d: pd.DataFrame, lookback: int):
    b = d.iloc[:-1].iloc[-lookback:]
    return b, b["high"].to_numpy(float), b["low"].to_numpy(float), b["close"].to_numpy(float)


def _date(b, i):
    return str(b.index[i].date()) if hasattr(b.index[i], "date") else str(b.index[i])


# ── the patterns (found on the bars before today) ───────────────────────────

def darvas(d: pd.DataFrame, lookback: int = 60, min_days: int = 5, max_height: float = 0.25) -> dict | None:
    b, h, lo, _c = _base(d, lookback + 25)
    n = len(b)
    if n < 30:
        return None
    year_high = float(d["high"].iloc[:-1].iloc[-250:].max())
    for t in range(n - 4, max(n - lookback, 20) - 1, -1):
        if h[t] < h[t - 20:t + 1].max() or h[t + 1:].max() >= h[t]:
            continue
        if h[t] < year_high * 0.97:                 # not at a new high: not a Darvas box
            return None
        u = t + 1 + int(np.argmin(lo[t + 1:]))
        if u > n - 4:                           # the bottom has not held 3 sessions yet: box still forming
            return None
        top, bottom, days = float(h[t]), float(lo[u]), n - t
        if days < min_days or top / bottom - 1 > max_height:
            return None
        return {"top": round(top, 2), "bottom": round(bottom, 2), "days": days, "top_date": _date(b, t),
                "height_pct": round((top / bottom - 1) * 100, 1)}
    return None


def vcp(d: pd.DataFrame, lookback: int = 120, max_last: float = 0.12, max_first: float = 0.40) -> dict | None:
    b, h, lo, c = _base(d, lookback)
    n = len(b)
    if n < 60:
        return None
    sh, sl = swings(h, lo)
    if len(sh) < 2:
        return None
    pulls = []
    for a, i in enumerate(sh):
        nxt = sh[a + 1] if a + 1 < len(sh) else n
        lows = [j for j in sl if i < j < nxt]
        if lows:
            j = min(lows, key=lambda x: lo[x])
            pulls.append((i, j, (h[i] - lo[j]) / h[i]))
    best = None
    for k in (4, 3, 2):
        if len(pulls) < k:
            continue
        seq = pulls[-k:]
        depths = [p[2] for p in seq]
        tops = [h[p[0]] for p in seq]
        if not all(depths[m + 1] < depths[m] for m in range(k - 1)):
            continue
        if depths[-1] > max_last or depths[0] > max_first or min(tops) < max(tops) * 0.85:
            continue
        best = seq
        break
    if not best:
        return None
    pivot = float(h[best[-1][0]])
    if c[best[-1][0] + 1:].max(initial=-np.inf) > pivot:        # already closed above the pivot: not a setup
        return None
    vol = b["volume"].to_numpy(float)
    dry = vol[-10:].mean() / vol[-50:].mean() if vol[-50:].mean() > 0 else None
    if dry is None or dry >= 0.85:
        return None
    s50, s200 = b["sma50"].iloc[-1] if "sma50" in b else np.nan, b["sma200"].iloc[-1] if "sma200" in b else np.nan
    if pd.isna(s50) or c[-1] < s50 or (not pd.isna(s200) and s50 < s200):
        return None
    return {"pivot": round(pivot, 2), "contractions_pct": [round(p[2] * 100, 1) for p in best], "count": len(best),
            "base_days": n - best[0][0], "volume_dry_up": round(float(dry), 2)}


def double_bottom(d: pd.DataFrame, lookback: int = 100) -> dict | None:
    b, h, lo, c = _base(d, lookback)
    n = len(b)
    if n < 40:
        return None
    _sh, sl = swings(h, lo)
    for a in range(len(sl) - 1, 0, -1):
        i2 = sl[a]
        if n - i2 > 30:                            # the second low must be recent
            break
        for i1 in reversed(sl[:a]):
            gap = i2 - i1
            if gap < 10:
                continue
            if gap > 60:
                break
            l1, l2 = lo[i1], lo[i2]
            if abs(l2 / l1 - 1) > 0.03:
                continue
            m = i1 + int(np.argmax(h[i1:i2 + 1]))
            neck = float(h[m])
            if neck < max(l1, l2) * 1.06:
                continue
            before = c[max(0, i1 - 40):i1]
            if not len(before) or before.max() < l1 * 1.10:
                continue
            if c[i2:].max() > neck:              # already broken out
                return None
            return {"low1": round(float(l1), 2), "low2": round(float(l2), 2), "neckline": round(neck, 2),
                    "days": gap, "low2_date": _date(b, i2)}
    return None


def ascending_triangle(d: pd.DataFrame, lookback: int = 80, flat: float = 0.015) -> dict | None:
    b, h, lo, c = _base(d, lookback)
    n = len(b)
    if n < 30:
        return None
    sh, sl = swings(h, lo)
    if len(sh) < 2:
        return None
    res = max(h[i] for i in sh[-3:])
    touch = [i for i in sh if h[i] >= res * (1 - flat)]
    if len(touch) < 2 or touch[-1] - touch[0] < 10 or touch[-1] != sh[-1]:
        return None
    lows = [lo[j] for j in sl if j > touch[0]]
    if len(lows) < 2 or not all(lows[m + 1] > lows[m] * 1.01 for m in range(len(lows) - 1)):
        return None
    if c[touch[0]:].max() > res:
        return None
    return {"resistance": round(float(res), 2), "touches": len(touch), "rising_lows": [round(float(x), 2) for x in lows],
            "days": n - touch[0]}


def head_shoulders(d: pd.DataFrame, lookback: int = 120) -> dict | None:
    b, h, lo, c = _base(d, lookback)
    n = len(b)
    if n < 40:
        return None
    sh, _sl = swings(h, lo)
    if len(sh) < 3:
        return None
    a, m, z = sh[-3:]
    ls, head, rs = h[a], h[m], h[z]
    if head < ls * 1.03 or head < rs * 1.03 or abs(ls / rs - 1) > 0.08:
        return None
    before = c[max(0, a - 40):a]
    if not len(before) or before.min() > ls * 0.92:      # a top needs an advance into it
        return None
    t1 = a + int(np.argmin(lo[a:m + 1]))
    t2 = m + int(np.argmin(lo[m:z + 1]))
    slope = (lo[t2] - lo[t1]) / (t2 - t1) if t2 != t1 else 0.0
    neck = lambda i: float(lo[t2] + slope * (i - t2))     # the neckline, extended
    if any(c[i] < neck(i) for i in range(z + 1, n)):
        return None                                       # already broke the neckline before today
    neck_now, neck_prev = neck(n), neck(n - 1)
    return {"left_shoulder": round(float(ls), 2), "head": round(float(head), 2), "right_shoulder": round(float(rs), 2),
            "neckline_today": round(neck_now, 2), "neckline_prev": round(neck_prev, 2)}


# ── today's breakouts ───────────────────────────────────────────────────────

def detect(d: pd.DataFrame) -> dict:
    """All patterns on the bars before today, cached on the frame (the scans and the snapshot share it)."""
    key = (len(d), str(d.index[-1]), float(d["close"].iloc[-1]))
    cached = d.attrs.get(_CACHE)
    if cached is not None and cached[0] == key:        # pandas copies attrs onto slices: check it is this frame's
        return cached[1]
    out = {}
    for name, fn in (("darvas", darvas), ("vcp", vcp), ("double_bottom", double_bottom),
                     ("ascending_triangle", ascending_triangle), ("head_shoulders", head_shoulders)):
        try:
            out[name] = fn(d)
        except (IndexError, KeyError, ValueError, TypeError):
            out[name] = None
    d.attrs[_CACHE] = (key, out)
    return out


def _today(d):
    return float(d["close"].iloc[-1]), float(d["close"].iloc[-2]), d["vol_ratio"].iloc[-1] if "vol_ratio" in d else None


def breakout(d: pd.DataFrame, key: str) -> str | None:
    """The reason text when pattern breakout `key` fires on today's bar, else None."""
    if len(d) < 3:
        return None
    p = detect(d)
    c, prev, vr = _today(d)
    vtxt = f" on {vr:.1f}x volume" if vr is not None and not pd.isna(vr) else ""
    if key == "darvas_breakout" and p["darvas"]:
        x = p["darvas"]
        if c > x["top"] >= prev:
            return (f"Darvas box {x['bottom']:.2f}-{x['top']:.2f} over {x['days']} sessions "
                    f"({x['height_pct']}% tall); closed above the top{vtxt}")
    elif key == "darvas_breakdown" and p["darvas"]:
        x = p["darvas"]
        if c < x["bottom"] <= prev:
            return f"Darvas box {x['bottom']:.2f}-{x['top']:.2f} over {x['days']} sessions; closed below the bottom"
    elif key == "vcp_breakout" and p["vcp"]:
        x = p["vcp"]
        if c > x["pivot"] >= prev and vr is not None and not pd.isna(vr) and vr >= 1.4:
            steps = " -> ".join(f"{v}%" for v in x["contractions_pct"])
            return f"VCP: {x['count']} contractions ({steps}), pivot {x['pivot']:.2f}; closed above it{vtxt}"
    elif key == "double_bottom_breakout" and p["double_bottom"]:
        x = p["double_bottom"]
        if c > x["neckline"] >= prev:
            return (f"Double bottom {x['low1']:.2f} / {x['low2']:.2f}, {x['days']} sessions apart; "
                    f"closed above the neckline {x['neckline']:.2f}{vtxt}")
    elif key == "ascending_triangle_breakout" and p["ascending_triangle"]:
        x = p["ascending_triangle"]
        if c > x["resistance"] >= prev:
            return (f"Ascending triangle: flat resistance {x['resistance']:.2f} ({x['touches']} touches) over rising "
                    f"lows; closed above it{vtxt}")
    elif key == "head_shoulders_breakdown" and p["head_shoulders"]:
        x = p["head_shoulders"]
        if c < x["neckline_today"] and prev >= x["neckline_prev"]:
            return (f"Head and shoulders: shoulders {x['left_shoulder']:.2f} / {x['right_shoulder']:.2f}, head "
                    f"{x['head']:.2f}; closed below the neckline {x['neckline_today']:.2f}")
    return None


def setups(d: pd.DataFrame) -> list:
    """Patterns in place before today that have not triggered yet and whose price is near the trigger
    (the upper half of a box / base, within 3 % of a triangle's resistance): [(label, text)] for the screener."""
    p = detect(d)
    c = float(d["close"].iloc[-1])
    out = []
    x = p["darvas"]
    if x and (x["top"] + x["bottom"]) / 2 <= c <= x["top"]:
        out.append(("Darvas box", f"Darvas box {x['bottom']:.2f}-{x['top']:.2f} ({x['days']} sessions)"))
    if p["vcp"] and c <= p["vcp"]["pivot"]:
        x = p["vcp"]
        out.append(("VCP setup", f"VCP setup, pivot {x['pivot']:.2f} ({x['count']} contractions)"))
    x = p["double_bottom"]
    if x and (x["low2"] + x["neckline"]) / 2 <= c <= x["neckline"]:
        out.append(("Double bottom", f"Double bottom, neckline {x['neckline']:.2f}"))
    x = p["ascending_triangle"]
    if x and x["resistance"] * 0.97 <= c <= x["resistance"]:
        out.append(("Ascending triangle", f"Ascending triangle, resistance {x['resistance']:.2f}"))
    if p["head_shoulders"] and c >= p["head_shoulders"]["neckline_today"]:
        out.append(("Head and shoulders", f"Head and shoulders, neckline {p['head_shoulders']['neckline_today']:.2f}"))
    return out
