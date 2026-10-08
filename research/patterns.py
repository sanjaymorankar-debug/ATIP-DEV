"""
W39 Phase 2 (item 4) — chart patterns from swing pivots: Darvas box, VCP (volatility contraction),
double bottom, ascending triangle, head-and-shoulders top, and (plan §7 "rest: later") the inverse
head and shoulders, descending triangle, rising / falling channels and rising / falling wedges.
Pure functions over an indicators() frame (open, high, low, close, volume, vol_ratio, sma50,
sma200), oldest first; no database.

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
    inverse_head_shoulders  the mirror: the last three swing lows, the middle (head) 3 %+ below both
                      shoulders, the shoulders within 8 % of each other, after a decline (a close 8 %+
                      above the left shoulder in the 40 sessions before it); the neckline joins the two
                      peaks between them and is extended to today, not closed above since the right
                      shoulder. Breakout (bullish): close above the neckline.
    descending_triangle  the mirror of the ascending one: 2+ swing lows within 1.5 % of each other (flat
                      support) spread over 10+ sessions, the latest swing low among them, and 2+ falling
                      swing highs since the first (each 1 %+ below the one before), no close below the
                      support since. Breakdown (bearish): close below the support.
    channel           two parallel trendlines: least-squares lines through the last 3 swing highs (upper)
                      and the last 3 swing lows (lower), every one of those pivots within 15 % of the
                      channel's height of its line, and no close beyond a line (by more than that 15 %
                      before the last pivot, at all after it). Parallel: the height at the end is
                      0.75-1.33x the height at the start. Rising: both lines up 4 %+ over the 20+
                      sessions the pattern spans; falling: both down 4 %+. 3-25 % tall.
                      Breakout: close above the upper line; breakdown: close below the lower line
                      (both directions are scans for both channels, each with its own record).
    wedge             the same two lines, converging instead: the height at the end at most 60 % of the
                      height at the start, and still above zero today (the apex is ahead), at most 25 %
                      tall at the start, 20+ sessions. Rising wedge: both lines up, the flatter upper
                      line by 3 %+ (a flat top is an ascending triangle); breakdown (bearish) below the
                      lower line. Falling wedge: both down, the flatter lower line by 3 %+; breakout
                      (bullish) above the upper line.

These are rule-based approximations of patterns traders draw by eye (the thresholds are ATIP's,
documented above). Each breakout is a scan in research/technicals.py SCANS, so it gets signals,
levels, the forward record and screener fields like any other scan; research/tech_signals.py holds
its alerts back until the scan has 30 closed signals with a positive average R.

Calibration (random walks, which have no patterns to find: 2 % daily volatility, independent volume;
200 walks x 250 sessions = 50,000 stock-days): every breakout fires on 0.02-1.2 % of stock-days, the
later eight on 0.02-0.1 % (inverse H&S 0.078 %, descending triangle 0.038 %, rising channel 0.032 % up
/ 0.092 % down, falling channel 0.098 % up / 0.020 % down, rising wedge 0.046 %, falling wedge
0.084 %), and their setups are listed on 0.15-0.69 % (tests/test_w39b_patterns_more.py).

active(d) gives every pattern in place with its lines (box top / bottom, neckline, resistance or
support, channel or wedge lines, from where the pattern starts to today's bar), for the stock chart
(dashboard/stock_view.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SWING = 3                       # bars each side of a confirmed swing point
_CACHE = "_chart_patterns"


def swings(high: np.ndarray, low: np.ndarray, k: int = SWING) -> tuple:
    """(swing-high indices, swing-low indices), each confirmed by k bars on both sides: high[i] above the k
    highs before it and not below the k after it (lows mirrored). One window of 2k+1 bars per i."""
    n = len(high)
    if n < 2 * k + 1:
        return [], []
    win = np.lib.stride_tricks.sliding_window_view
    hw, lw = win(np.asarray(high, dtype=float), 2 * k + 1), win(np.asarray(low, dtype=float), 2 * k + 1)
    mh, ml = hw[:, k], lw[:, k]
    hi = (mh > hw[:, :k].max(axis=1)) & (mh >= hw[:, k + 1:].max(axis=1))
    lo = (ml < lw[:, :k].min(axis=1)) & (ml <= lw[:, k + 1:].min(axis=1))
    return (np.flatnonzero(hi) + k).tolist(), (np.flatnonzero(lo) + k).tolist()


class _Memo(tuple):
    """detect()'s cached (key, result) in DataFrame.attrs. pandas deep-copies attrs onto every slice and column
    it hands out; this passes itself on instead of being copied (the key check keeps a slice from using it)."""

    def __deepcopy__(self, memo):
        return self


def _base(d: pd.DataFrame, lookback: int):
    b = d.iloc[:-1].iloc[-lookback:]
    return b, b["high"].to_numpy(float), b["low"].to_numpy(float), b["close"].to_numpy(float)


def _date(b, i):
    return str(b.index[i].date()) if hasattr(b.index[i], "date") else str(b.index[i])


def _line(label, b, i, start, today=None) -> dict:
    """A level to draw on the chart: from bar i of the base at `start` to today's bar at `today`
    (the same value when flat; a neckline or trendline extended to today when not)."""
    return {"label": label, "from": _date(b, i), "from_value": round(float(start), 2),
            "to_value": round(float(start if today is None else today), 2)}


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
                "height_pct": round((top / bottom - 1) * 100, 1),
                "lines": [_line("Box top", b, t, top), _line("Box bottom", b, t, bottom)]}
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
            "base_days": n - best[0][0], "volume_dry_up": round(float(dry), 2),
            "lines": [_line("VCP pivot", b, best[-1][0], pivot)]}


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
                    "days": gap, "low2_date": _date(b, i2), "lines": [_line("Neckline", b, i1, neck)]}
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
            "days": n - touch[0], "lines": [_line("Resistance", b, touch[0], res)]}


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
            "neckline_today": round(neck_now, 2), "neckline_prev": round(neck_prev, 2),
            "lines": [_line("Neckline", b, t1, lo[t1], neck_now)]}


def inverse_head_shoulders(d: pd.DataFrame, lookback: int = 120) -> dict | None:
    """head_shoulders mirrored: a bottom of three swing lows, broken upward through the neckline."""
    b, h, lo, c = _base(d, lookback)
    n = len(b)
    if n < 40:
        return None
    _sh, sl = swings(h, lo)
    if len(sl) < 3:
        return None
    a, m, z = sl[-3:]
    ls, head, rs = lo[a], lo[m], lo[z]
    if head > ls * 0.97 or head > rs * 0.97 or abs(ls / rs - 1) > 0.08:
        return None
    before = c[max(0, a - 40):a]
    if not len(before) or before.max() < ls * 1.08:      # a bottom needs a decline into it
        return None
    p1 = a + int(np.argmax(h[a:m + 1]))
    p2 = m + int(np.argmax(h[m:z + 1]))
    slope = (h[p2] - h[p1]) / (p2 - p1) if p2 != p1 else 0.0
    neck = lambda i: float(h[p2] + slope * (i - p2))      # the neckline, extended
    if any(c[i] > neck(i) for i in range(z + 1, n)):
        return None                                       # already broke out before today
    neck_now, neck_prev = neck(n), neck(n - 1)
    return {"left_shoulder": round(float(ls), 2), "head": round(float(head), 2), "right_shoulder": round(float(rs), 2),
            "neckline_today": round(neck_now, 2), "neckline_prev": round(neck_prev, 2),
            "lines": [_line("Neckline", b, p1, h[p1], neck_now)]}


def descending_triangle(d: pd.DataFrame, lookback: int = 80, flat: float = 0.015) -> dict | None:
    """ascending_triangle mirrored: flat support under falling highs."""
    b, h, lo, c = _base(d, lookback)
    n = len(b)
    if n < 30:
        return None
    sh, sl = swings(h, lo)
    if len(sl) < 2:
        return None
    sup = min(lo[j] for j in sl[-3:])
    touch = [j for j in sl if lo[j] <= sup * (1 + flat)]
    if len(touch) < 2 or touch[-1] - touch[0] < 10 or touch[-1] != sl[-1]:
        return None
    highs = [h[i] for i in sh if i > touch[0]]
    if len(highs) < 2 or not all(highs[m + 1] < highs[m] * 0.99 for m in range(len(highs) - 1)):
        return None
    if c[touch[0]:].min() < sup:
        return None
    return {"support": round(float(sup), 2), "touches": len(touch), "falling_highs": [round(float(x), 2) for x in highs],
            "days": n - touch[0], "lines": [_line("Support", b, touch[0], sup)]}


def _trendlines(h: np.ndarray, lo: np.ndarray, c: np.ndarray, k: int = 3, tol: float = 0.15) -> dict | None:
    """Least-squares lines through the last k swing highs (upper) and the last k swing lows (lower) of a
    base, or None unless every one of those pivots is within tol x the gap between the lines of its own
    line and no close went beyond a line (by more than that before the last pivot, at all after it)."""
    n = len(h)
    sh, sl = swings(h, lo)
    if len(sh) < k or len(sl) < k:
        return None
    hi_i, lo_i = np.asarray(sh[-k:]), np.asarray(sl[-k:])
    su, cu = np.polyfit(hi_i, h[hi_i], 1)
    sd, cd = np.polyfit(lo_i, lo[lo_i], 1)
    up = lambda i: cu + su * np.asarray(i, dtype=float)
    dn = lambda i: cd + sd * np.asarray(i, dtype=float)
    start, last = int(min(hi_i[0], lo_i[0])), int(max(hi_i[-1], lo_i[-1]))
    idx = np.arange(start, n)
    upper, lower = up(idx), dn(idx)
    gap = upper - lower
    if (gap <= 0).any() or up(n) <= dn(n):               # the lines cross before today: no pattern
        return None
    if (np.abs(h[hi_i] - up(hi_i)) > tol * (up(hi_i) - dn(hi_i))).any():
        return None
    if (np.abs(lo[lo_i] - dn(lo_i)) > tol * (up(lo_i) - dn(lo_i))).any():
        return None
    cc, done = c[start:], idx > last
    slack = np.where(done, 0.0, tol * gap)
    if (cc > upper + slack).any() or (cc < lower - slack).any():
        return None
    return {"up": up, "dn": dn, "start": start, "n": n, "gap0": float(gap[0]), "gap1": float(gap[-1]),
            "rise_up": float(upper[-1] / upper[0] - 1), "rise_dn": float(lower[-1] / lower[0] - 1)}


def _lines_out(b, t: dict, kind: str) -> dict:
    up, dn, n, s = t["up"], t["dn"], t["n"], t["start"]
    return {"kind": kind, "upper_today": round(float(up(n)), 2), "upper_prev": round(float(up(n - 1)), 2),
            "lower_today": round(float(dn(n)), 2), "lower_prev": round(float(dn(n - 1)), 2), "days": n - s,
            "lines": [_line("Upper line", b, s, up(s), up(n)), _line("Lower line", b, s, dn(s), dn(n))]}


def channel(d: pd.DataFrame, lookback: int = 120, min_move: float = 0.04, min_height: float = 0.03,
            max_height: float = 0.25) -> dict | None:
    """A rising or falling parallel channel on the bars before today (kind "rising" / "falling")."""
    b, h, lo, c = _base(d, lookback)
    if len(b) < 40:
        return None
    t = _trendlines(h, lo, c)
    if not t or t["n"] - 1 - t["start"] < 20:
        return None
    if not 0.75 <= t["gap1"] / t["gap0"] <= 1.33:          # not parallel
        return None
    if t["rise_up"] >= min_move and t["rise_dn"] >= min_move:
        kind = "rising"
    elif t["rise_up"] <= -min_move and t["rise_dn"] <= -min_move:
        kind = "falling"
    else:
        return None
    height = t["gap1"] / float(t["dn"](t["n"] - 1))
    if not min_height <= height <= max_height:
        return None
    out = _lines_out(b, t, kind)
    out["height_pct"] = round(height * 100, 1)
    return out


def wedge(d: pd.DataFrame, lookback: int = 120, min_move: float = 0.03, converge: float = 0.6,
          max_height: float = 0.25) -> dict | None:
    """A rising or falling wedge on the bars before today (kind "rising" / "falling")."""
    b, h, lo, c = _base(d, lookback)
    if len(b) < 40:
        return None
    t = _trendlines(h, lo, c)
    if not t or t["n"] - 1 - t["start"] < 20:
        return None
    if t["gap1"] > converge * t["gap0"] or t["gap0"] / float(t["dn"](t["start"])) > max_height:
        return None
    if t["rise_up"] >= min_move and t["rise_dn"] > t["rise_up"]:
        kind = "rising"                                    # both up, the lower line steeper
    elif t["rise_dn"] <= -min_move and t["rise_up"] < t["rise_dn"]:
        kind = "falling"                                   # both down, the upper line steeper
    else:
        return None
    out = _lines_out(b, t, kind)
    out["narrowing_pct"] = round((1 - t["gap1"] / t["gap0"]) * 100, 1)
    return out


# ── today's breakouts ───────────────────────────────────────────────────────

DETECTORS = (("darvas", darvas), ("vcp", vcp), ("double_bottom", double_bottom),
             ("ascending_triangle", ascending_triangle), ("head_shoulders", head_shoulders),
             ("inverse_head_shoulders", inverse_head_shoulders), ("descending_triangle", descending_triangle),
             ("channel", channel), ("wedge", wedge))
NAMES = {"darvas": "Darvas box", "vcp": "VCP", "double_bottom": "Double bottom",
         "ascending_triangle": "Ascending triangle", "head_shoulders": "Head and shoulders",
         "inverse_head_shoulders": "Inverse head and shoulders", "descending_triangle": "Descending triangle",
         "channel": "channel", "wedge": "wedge"}
# the side a pattern resolves to (BOTH: a box or a channel can break either way; a wedge's follows its kind)
SIDES = {"darvas": "BOTH", "vcp": "BULL", "double_bottom": "BULL", "ascending_triangle": "BULL",
         "head_shoulders": "BEAR", "inverse_head_shoulders": "BULL", "descending_triangle": "BEAR", "channel": "BOTH",
         "wedge": {"rising": "BEAR", "falling": "BULL"}}
# breakout scan key -> the pattern it breaks (research/technicals.py SCANS / PATTERN_SCANS)
SCAN_PATTERN = {"darvas_breakout": "darvas", "darvas_breakdown": "darvas", "vcp_breakout": "vcp",
                "double_bottom_breakout": "double_bottom", "ascending_triangle_breakout": "ascending_triangle",
                "head_shoulders_breakdown": "head_shoulders",
                "inverse_head_shoulders_breakout": "inverse_head_shoulders",
                "descending_triangle_breakdown": "descending_triangle",
                "rising_channel_breakout": "channel", "rising_channel_breakdown": "channel",
                "falling_channel_breakout": "channel", "falling_channel_breakdown": "channel",
                "rising_wedge_breakdown": "wedge", "falling_wedge_breakout": "wedge"}
# the two-line patterns: scan key -> (pattern, kind, which line it breaks)
_LINE_SCANS = {"rising_channel_breakout": ("channel", "rising", "up"),
               "rising_channel_breakdown": ("channel", "rising", "down"),
               "falling_channel_breakout": ("channel", "falling", "up"),
               "falling_channel_breakdown": ("channel", "falling", "down"),
               "rising_wedge_breakdown": ("wedge", "rising", "down"),
               "falling_wedge_breakout": ("wedge", "falling", "up")}


def name_of(pattern: str, x: dict | None) -> str:
    """The display name: "Rising channel" / "Falling wedge" for the two-line patterns."""
    return f"{x['kind'].capitalize()} {NAMES[pattern]}" if x and x.get("kind") else NAMES[pattern]


def detect(d: pd.DataFrame) -> dict:
    """All patterns on the bars before today, cached on the frame (the scans and the snapshot share it)."""
    key = (len(d), str(d.index[-1]), float(d["close"].iloc[-1]))
    cached = d.attrs.get(_CACHE)
    if cached is not None and cached[0] == key:        # pandas copies attrs onto slices: check it is this frame's
        return cached[1]
    out = {}
    for name, fn in DETECTORS:
        try:
            out[name] = fn(d)
        except (IndexError, KeyError, ValueError, TypeError):
            out[name] = None
    d.attrs[_CACHE] = _Memo((key, out))
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
    elif key == "inverse_head_shoulders_breakout" and p["inverse_head_shoulders"]:
        x = p["inverse_head_shoulders"]
        if c > x["neckline_today"] and prev <= x["neckline_prev"]:
            return (f"Inverse head and shoulders: shoulders {x['left_shoulder']:.2f} / {x['right_shoulder']:.2f}, "
                    f"head {x['head']:.2f}; closed above the neckline {x['neckline_today']:.2f}{vtxt}")
    elif key == "descending_triangle_breakdown" and p["descending_triangle"]:
        x = p["descending_triangle"]
        if c < x["support"] <= prev:
            return (f"Descending triangle: flat support {x['support']:.2f} ({x['touches']} touches) under falling "
                    f"highs; closed below it")
    elif key in _LINE_SCANS:
        pat, kind, side = _LINE_SCANS[key]
        x = p[pat]
        if x and x["kind"] == kind:
            span = (f"{name_of(pat, x)} over {x['days']} sessions, lines {x['lower_today']:.2f} / "
                    f"{x['upper_today']:.2f} today")
            if side == "up" and c > x["upper_today"] and prev <= x["upper_prev"]:
                return f"{span}; closed above the upper line{vtxt}"
            if side == "down" and c < x["lower_today"] and prev >= x["lower_prev"]:
                return f"{span}; closed below the lower line"
    return None


def setups(d: pd.DataFrame) -> list:
    """Patterns in place before today that have not triggered yet and whose price is near the trigger
    (the upper half of a box / base, within 3 % of a triangle's resistance or support, between a channel's or
    a wedge's lines): [(label, text)] for the screener."""
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
    x = p["inverse_head_shoulders"]
    if x and (x["right_shoulder"] + x["neckline_today"]) / 2 <= c <= x["neckline_today"]:
        out.append(("Inverse head and shoulders", f"Inverse head and shoulders, neckline {x['neckline_today']:.2f}"))
    x = p["descending_triangle"]
    if x and x["support"] <= c <= x["support"] * 1.03:
        out.append(("Descending triangle", f"Descending triangle, support {x['support']:.2f}"))
    for pat in ("channel", "wedge"):
        x = p[pat]
        if x and x["lower_today"] <= c <= x["upper_today"]:
            name = name_of(pat, x)
            out.append((name, f"{name} {x['lower_today']:.2f}-{x['upper_today']:.2f} ({x['days']} sessions)"))
    return out


def active(d: pd.DataFrame) -> list:
    """Every pattern in place on the bars before today, for the stock chart: [{pattern, name, side, triggered,
    reason, lines}]. side is BULL / BEAR / BOTH, triggered the scan today's close fired (None while it waits),
    and each line {label, from, from_value, to, to_value} runs from where the pattern drew it to today's bar."""
    if d is None or len(d) < 3:
        return []
    p = detect(d)
    today = _date(d, -1)
    out = []
    for pat, _fn in DETECTORS:
        x = p.get(pat)
        if not x:
            continue
        side = SIDES[pat]
        side = side[x["kind"]] if isinstance(side, dict) else side
        fired = next(((k, r) for k, q in SCAN_PATTERN.items() if q == pat for r in (breakout(d, k),) if r),
                     (None, None))
        out.append({"pattern": pat, "name": name_of(pat, x), "side": side, "triggered": fired[0], "reason": fired[1],
                    "lines": [dict(ln, to=today) for ln in x.get("lines", [])]})
    return out
