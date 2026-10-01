"""
Event studies on market_event (QR-09), W30.

    study(conn, event_type, start=None, end=None, horizons=(1, 5, 10, 20), pre=5, split="direction")

For each event, day 0 is the first session on or after known_at -- the sources set
known_at conservatively (an evening results filing is known the next day), so no event
is traded before it was public.
Returns per horizon h and per group (direction BEAT / MISS / BUY / SELL, or value sign):
    n, mean / median CAR % (stock minus NIFTY50 over closes day0 -> day0+h), hit rate
    (CAR in the group's expected direction), t-stat;
and a PRE-event drift (day0-pre -> day0) per group -- a large pre-event move with no
post-event drift says the information leaked or was already priced.
Stored in event_study (study_id, event_type, period, params, result).

Descriptive statistics on a short history; |t| < 2 or n < 30 is not evidence.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta

BENCH = "NIFTY50"
MAX_DAY0_LAG = 4             # calendar days from known_at to day 0
EXPECTED = {"BEAT": 1, "MISS": -1, "BUY": 1, "SELL": -1, "POS": 1, "NEG": -1}


def _winsorize(vals, p=0.01):
    if len(vals) < 20:
        return list(vals)
    srt = sorted(vals)
    lo, hi = srt[int(p * (len(srt) - 1))], srt[int((1 - p) * (len(srt) - 1))]
    return [min(max(v, lo), hi) for v in vals]


def _stats(vals, sign):
    """Raw mean / median, and the 1% / 99% winsorized mean with its t-stat -- the
    significance test runs on the winsorized values, so a handful of extreme moves (bad
    prints, illiquid names) cannot manufacture a t-stat on their own."""
    if not vals:
        return {"n": 0}
    m = sum(vals) / len(vals)
    w = _winsorize(vals)
    mw = sum(w) / len(w)
    sd = math.sqrt(sum((v - mw) ** 2 for v in w) / (len(w) - 1)) if len(w) > 1 else None
    srt = sorted(vals)
    return {"n": len(vals), "mean_car_pct": round(m, 4), "winsorized_mean_pct": round(mw, 4),
            "median_car_pct": round(srt[len(srt) // 2], 4),
            "hit_rate": round(sum(1 for v in vals if (v > 0) == (sign > 0)) / len(vals), 4) if sign else None,
            "t_stat": round(mw / (sd / math.sqrt(len(w))), 3) if sd else None}


def study(conn, event_type: str, start=None, end=None, horizons=(1, 5, 10, 20), pre: int = 5,
          split: str = "direction", store: bool = True) -> dict:
    et = event_type.upper()
    end = date.fromisoformat(str(end)[:10]) if end else date.today()
    start = date.fromisoformat(str(start)[:10]) if start else end - timedelta(days=730)
    ev = conn.execute("SELECT symbol, known_at, direction, value, payload_json FROM market_event WHERE event_type=? "
                      "AND known_at BETWEEN ? AND ?", (et, str(start), str(end))).fetchall()
    if not ev:
        return {"event_type": et, "events": 0, "note": "no events of this type in the period"}
    syms = sorted({e[0] for e in ev} | {BENCH})
    closes = {}
    for i in range(0, len(syms), 500):
        ch = syms[i:i + 500]
        for s, d, c in conn.execute(f"SELECT symbol, date, close FROM prices_daily WHERE date>=? AND close>0 AND "
                                    f"symbol IN ({','.join('?' * len(ch))}) ORDER BY symbol, date",
                                    (str(start - timedelta(days=20)), *ch)):
            closes.setdefault(s, []).append((str(d)[:10], float(c)))
    dates = {s: [d for d, _ in v] for s, v in closes.items()}

    def idx0(sym, known):
        """First session on or after known_at (sources set known_at conservatively) -- and
        no more than MAX_DAY0_LAG calendar days after it: a symbol with no price data
        around its event would otherwise take a 'day 0' months later."""
        ds = dates.get(sym, [])
        k = str(known)[:10]
        for i, d in enumerate(ds):
            if d >= k:
                return i if (date.fromisoformat(d) - date.fromisoformat(k)).days <= MAX_DAY0_LAG else None
        return None

    def contiguous(sym, i, j):
        """The bars i..j span no data gap: at most ~1.6 calendar days per session + 5."""
        ds = dates.get(sym, [])
        if j >= len(ds):
            return False
        return (date.fromisoformat(ds[j]) - date.fromisoformat(ds[i])).days <= (j - i) * 1.6 + 5

    def ret(sym, i, j):
        v = closes.get(sym, [])
        if i is None or j is None or i < 0 or j >= len(v) or i >= len(v):
            return None
        return (v[j][1] / v[i][1] - 1) * 100

    def bench_ret(d_from, d_to):
        ds = dates.get(BENCH, [])
        try:
            i, j = ds.index(d_from), ds.index(d_to)
        except ValueError:
            return None
        return ret(BENCH, i, j)

    groups = {}
    pre_groups = {}
    used = 0
    for sym, known, direction, value, payload in ev:
        i0 = idx0(sym, known)
        if i0 is None:
            continue
        g = (direction or ("POS" if (value or 0) > 0 else "NEG" if (value or 0) < 0 else "FLAT")) \
            if split == "direction" else "ALL"
        ds = dates[sym]
        used += 1
        for h in horizons:
            if not contiguous(sym, i0, i0 + h):
                continue
            r, b = ret(sym, i0, i0 + h), (bench_ret(ds[i0], ds[i0 + h]) if i0 + h < len(ds) else None)
            if r is not None and b is not None:
                groups.setdefault(h, {}).setdefault(g, []).append(r - b)
        if i0 - pre >= 0 and contiguous(sym, i0 - pre, i0):
            r, b = ret(sym, i0 - pre, i0), bench_ret(ds[i0 - pre], ds[i0])
            if r is not None and b is not None:
                pre_groups.setdefault(g, []).append(r - b)
    res = {"event_type": et, "period": f"{start}..{end}", "events": len(ev), "events_priced": used,
           "horizons": {str(h): {g: _stats(v, EXPECTED.get(g, 0)) for g, v in sorted(gs.items())}
                        for h, gs in sorted(groups.items())},
           "pre_event": {g: _stats(v, EXPECTED.get(g, 0)) for g, v in sorted(pre_groups.items())},
           "params": {"horizons": list(horizons), "pre": pre, "split": split}}
    sig = [(h, g, s) for h, gs in res["horizons"].items() for g, s in gs.items()
           if s.get("n", 0) >= 30 and s.get("t_stat") is not None and abs(s["t_stat"]) >= 2]
    res["significant"] = [{"horizon": int(h), "group": g, "t_stat": s["t_stat"],
                           "winsorized_mean_pct": s["winsorized_mean_pct"], "median_car_pct": s["median_car_pct"]}
                          for h, g, s in sig]
    res["verdict"] = "INSUFFICIENT_DATA" if used < 30 else ("EVIDENCE" if sig else "NO_EVIDENCE")
    if store:
        sid = "ES" + uuid.uuid4().hex[:12].upper()
        conn.execute("INSERT INTO event_study (study_id,event_type,period,params_json,result_json,created_at) VALUES "
                     "(?,?,?,?,?,?)", (sid, et, res["period"], json.dumps(res["params"]), json.dumps(res, default=str),
                                       datetime.now()))
        conn.commit()
        res["study_id"] = sid
    return res


def latest(conn, event_type=None) -> list:
    sql, args = "SELECT study_id, event_type, period, result_json, created_at FROM event_study", []
    if event_type:
        sql += " WHERE event_type=?"
        args.append(event_type.upper())
    out = []
    for sid, et, per, rj, at in conn.execute(sql + " ORDER BY created_at DESC LIMIT 20", args):
        r = json.loads(rj or "{}")
        out.append({"study_id": sid, "event_type": et, "period": per, "created_at": str(at)[:19],
                    "verdict": r.get("verdict"), "events": r.get("events"), "significant": r.get("significant"),
                    "result": r})
    return out
