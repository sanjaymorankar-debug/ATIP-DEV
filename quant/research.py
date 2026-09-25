"""
Factor research tools (QR-01..04): run on stored factor scores and prices.

    factor_ic(conn, key, start, end, horizon)
        per date: Spearman rank correlation between the factor score on t and the
        forward return t -> t+h (bars AFTER t only); summary mean / stdev / t-stat /
        hit rate. Only dates whose forward window has fully elapsed are used.
    ic_decay(conn, key, start, end, horizons)   mean IC per horizon (decay / half-life)
    quantile_returns(conn, key, start, end, horizon, q=5)   mean forward return per
        score quintile per date, averaged (spread = top - bottom)
    factor_correlation(conn, keys, as_of)       cross-sectional Spearman correlation
        matrix of factor scores on one date (redundancy check)

Results are stored in quant_factor_research. These are sample statistics on
limited history (prices from 2025-01-27); they describe, they do not validate.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime

import numpy as np


def _spearman(x, y):
    if len(x) < 5:
        return None
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _scores_by_date(conn, key, start, end):
    out = {}
    for d, s, sc in conn.execute("SELECT as_of, symbol, score FROM quant_factor_score WHERE factor_key=? AND as_of>=? "
                                 "AND as_of<=? AND score IS NOT NULL", (key, str(start), str(end))):
        out.setdefault(str(d)[:10], {})[s] = sc
    return out


def _forward(conn, symbols, start, end, horizon):
    from backtest.data import PriceHistory
    h = PriceHistory.load(conn, symbols, start, date.today(), warmup_days=5)

    def fwd(sym, d):
        idx = h._index.get(sym, {}).get(date.fromisoformat(d))
        bars = h.bars.get(sym, [])
        if idx is None or idx + horizon >= len(bars) or not bars[idx].close:
            return None
        return (bars[idx + horizon].close / bars[idx].close - 1) * 100
    return fwd


def factor_ic(conn, key, start, end, horizon=5, store=True) -> dict:
    by = _scores_by_date(conn, key, start, end)
    syms = sorted({s for d in by.values() for s in d})
    if not syms:
        return {"key": key, "dates": 0, "note": "no stored scores in range"}
    fwd = _forward(conn, syms, start, end, horizon)
    ics = {}
    for d, sc in by.items():
        pairs = [(v, fwd(s, d)) for s, v in sc.items()]
        pairs = [p for p in pairs if p[1] is not None]
        ic = _spearman([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 20 else None
        if ic is not None:
            ics[d] = ic
    vals = list(ics.values())
    res = {"key": key, "horizon": horizon, "dates": len(vals)}
    if vals:
        m, sd = float(np.mean(vals)), float(np.std(vals, ddof=1)) if len(vals) > 1 else None
        res.update(mean_ic=round(m, 4), ic_std=None if sd is None else round(sd, 4),
                   t_stat=round(m / (sd / math.sqrt(len(vals))), 3) if sd else None,
                   hit_rate=round(sum(1 for v in vals if v > 0) / len(vals), 4))
    if store:
        _store(conn, key, "ic", start, end, {**res, "by_date": ics})
    return res


def ic_decay(conn, key, start, end, horizons=(1, 5, 10, 20)) -> dict:
    out = {h: factor_ic(conn, key, start, end, h, store=False).get("mean_ic") for h in horizons}
    _store(conn, key, "ic_decay", start, end, out)
    return {"key": key, "mean_ic_by_horizon": out}


def quantile_returns(conn, key, start, end, horizon=5, q=5) -> dict:
    by = _scores_by_date(conn, key, start, end)
    syms = sorted({s for d in by.values() for s in d})
    if not syms:
        return {"key": key, "dates": 0}
    fwd = _forward(conn, syms, start, end, horizon)
    acc = {i: [] for i in range(q)}
    for d, sc in by.items():
        pts = sorted((v, fwd(s, d)) for s, v in sc.items())
        pts = [p for p in pts if p[1] is not None]
        if len(pts) < q * 5:
            continue
        for i in range(q):
            chunk = pts[i * len(pts) // q:(i + 1) * len(pts) // q]
            acc[i].append(sum(p[1] for p in chunk) / len(chunk))
    means = {f"Q{i + 1}": (round(float(np.mean(v)), 4) if v else None) for i, v in acc.items()}
    res = {"key": key, "horizon": horizon, "quantile_mean_fwd_return_pct": means,
           "spread_top_minus_bottom": (means[f"Q{q}"] - means["Q1"]) if means["Q1"] is not None and
                                      means[f"Q{q}"] is not None else None}
    _store(conn, key, "quantiles", start, end, res)
    return res


def factor_correlation(conn, keys, as_of) -> dict:
    data = {k: {s: sc for s, sc in conn.execute("SELECT symbol, score FROM quant_factor_score WHERE as_of=? AND "
                                                "factor_key=? AND score IS NOT NULL", (str(as_of), k))} for k in keys}
    out = {}
    for a in keys:
        out[a] = {}
        for b in keys:
            common = sorted(set(data[a]) & set(data[b]))
            out[a][b] = _spearman([data[a][s] for s in common], [data[b][s] for s in common]) if common else None
    return out


def _store(conn, key, kind, start, end, payload):
    conn.execute("INSERT INTO quant_factor_research (factor_key,kind,start_date,end_date,result_json,created_at) "
                 "VALUES (?,?,?,?,?,?)", (key, kind, str(start), str(end), json.dumps(payload, default=str),
                                          datetime.now()))
    conn.commit()
