"""
Bulk / block deal signal and its validation (AD-03), W27.

The signal: a stock's net bulk + block deal value (Rs crore) on a session, from
bulk_deals (data/bhavcopy.run_bulk_deals_pipeline). It already feeds INS's BulkDeals
component (trailing 10 days, scores/engine.get_bulk); this module measures whether it
carries information before anyone weights it more.

    event_study(conn, start, end, horizons=(1,5,10,20), min_cr=1.0)
        every (symbol, session) with |net| >= min_cr is an event, known after that
        session's close. Forward return close(t) -> close(t+h) (bars AFTER t only),
        abnormal = stock - NIFTY50 over the same bars. Per horizon and per side
        (BUY = net > 0, SELL = net < 0): n, mean / median abnormal %, hit rate (abnormal
        in the deal's direction), t-stat; and the pooled Spearman IC of net value vs
        abnormal return across all events (a cross-sectional IC per date is not
        possible -- most sessions have only a handful of deal stocks).
    run_scheduled()  last 365 days, stored in quant_factor_research
        (factor_key 'deal_flow', kind 'event_study').

Sample statistics on a short, sparse history: they describe, they do not validate.
Treat |t| < 2 or n < 30 as no evidence either way.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

KEY = "deal_flow"
BENCH = "NIFTY50"


def _spearman(x, y):
    from quant.research import _spearman as sp
    return sp(x, y)


def _stats(vals, sign):
    if not vals:
        return {"n": 0}
    a = np.array(vals, dtype=float)
    sd = float(a.std(ddof=1)) if len(a) > 1 else None
    return {"n": int(len(a)), "mean_abnormal_pct": round(float(a.mean()), 4),
            "median_abnormal_pct": round(float(np.median(a)), 4),
            "hit_rate": round(float(np.mean(np.sign(a) == sign)), 4),
            "t_stat": round(float(a.mean()) / (sd / math.sqrt(len(a))), 3) if sd else None}


def event_study(conn, start=None, end=None, horizons=(1, 5, 10, 20), min_cr=1.0, store=True) -> dict:
    end = date.fromisoformat(str(end)[:10]) if end else date.today()
    start = date.fromisoformat(str(start)[:10]) if start else end - timedelta(days=365)
    ev = conn.execute("SELECT symbol, date, net_value_cr FROM bulk_deals WHERE date BETWEEN ? AND ? "
                      "AND ABS(COALESCE(net_value_cr,0))>=?", (str(start), str(end), float(min_cr))).fetchall()
    if not ev:
        return {"key": KEY, "events": 0, "note": "no bulk/block deals in range"}
    syms = sorted({e[0] for e in ev} | {BENCH})
    # Closes straight from prices_daily: backtest.data.PriceHistory drops index series
    # (never tradeable), and the benchmark here IS one.
    closes = {}
    for i in range(0, len(syms), 500):
        chunk = syms[i:i + 500]
        for sym, d, c in conn.execute(f"SELECT symbol, date, close FROM prices_daily WHERE date>=? AND close>0 AND "
                                      f"symbol IN ({','.join('?' * len(chunk))}) ORDER BY symbol, date",
                                      (str(start), *chunk)):
            closes.setdefault(sym, []).append((str(d)[:10], float(c)))
    index = {s: {d: i for i, (d, _) in enumerate(v)} for s, v in closes.items()}

    def fwd(sym, d, k):
        idx = index.get(sym, {}).get(str(d)[:10])
        bars = closes.get(sym, [])
        if idx is None or idx + k >= len(bars):
            return None
        return (bars[idx + k][1] / bars[idx][1] - 1) * 100

    res = {"key": KEY, "period": f"{start}..{end}", "min_cr": min_cr, "events": len(ev), "horizons": {}}
    for k in horizons:
        buy, sell, xs, ys = [], [], [], []
        for sym, d, net in ev:
            r, b = fwd(sym, d, k), fwd(BENCH, d, k)
            if r is None or b is None:
                continue
            ab = r - b
            (buy if net > 0 else sell).append(ab)
            xs.append(net)
            ys.append(ab)
        res["horizons"][str(k)] = {"BUY": _stats(buy, 1), "SELL": _stats(sell, -1),
                                   "pooled_ic": (round(_spearman(xs, ys), 4) if len(xs) >= 20 and
                                                 _spearman(xs, ys) is not None else None),
                                   "events_with_forward_window": len(xs)}
    # Verdict per side: a side "confirms" when its abnormal return is significant
    # (|t| >= 2, n >= 30) IN the deal's direction, "contradicts" when significant
    # AGAINST it. INS's BulkDeals component assumes net buying is bullish; a
    # contradicting SELL side says that assumption is wrong for this sample.
    findings = []
    for k, v in res["horizons"].items():
        for side, sign in (("BUY", 1), ("SELL", -1)):
            st = v[side]
            if st.get("n", 0) >= 30 and st.get("t_stat") is not None and abs(st["t_stat"]) >= 2:
                findings.append({"horizon": int(k), "side": side,
                                 "direction": "CONFIRMS" if np.sign(st["mean_abnormal_pct"]) == sign else "CONTRADICTS",
                                 "t_stat": st["t_stat"], "mean_abnormal_pct": st["mean_abnormal_pct"]})
    res["findings"] = findings
    enough = any(v["BUY"].get("n", 0) >= 30 or v["SELL"].get("n", 0) >= 30 for v in res["horizons"].values())
    dirs = {f["direction"] for f in findings}
    res["verdict"] = ("INSUFFICIENT_DATA" if not enough else "NO_EVIDENCE" if not findings else
                      "MIXED" if len(dirs) > 1 else
                      "SUPPORTS_INS_DIRECTION" if dirs == {"CONFIRMS"} else "CONTRADICTS_INS_DIRECTION")
    if store:
        from quant.research import _store
        _store(conn, KEY, "event_study", start, end, res)
    return res


def latest(conn) -> dict | None:
    import json
    r = conn.execute("SELECT result_json, created_at FROM quant_factor_research WHERE factor_key=? AND "
                     "kind='event_study' ORDER BY id DESC LIMIT 1", (KEY,)).fetchone()
    return {**json.loads(r[0]), "computed_at": str(r[1])} if r else None


def run_scheduled(trade_date=None) -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        r = event_study(conn, end=trade_date)
        return {"status": "SUCCESS", "rows": r.get("events", 0), "verdict": r.get("verdict")}
    finally:
        conn.close()
