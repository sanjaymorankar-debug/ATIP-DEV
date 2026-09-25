"""
Portfolio construction (pure functions + persistence).

construct(scores, method, *, vols, betas, sectors, factor_exposure, constraints, long_short)
    scores            {symbol: score} for the LONG candidates (and `short_scores` for
                      the short book when long_short)
    method            equal | score | inverse_vol | risk (equal risk contribution,
                      diagonal approximation: w ~ 1/vol) | factor (w ~ factor exposure)
    constraints       max_weight (per name, fraction of gross), sector_cap (per
                      industry), gross (long + |short|, default 1.0), net (long - short)
    long_short        None (long only) | "dollar" (long sum = short sum) |
                      "beta" (long beta exposure = short beta exposure) |
                      "sector" (long = short within every industry present on both sides)
    -> {symbol: weight}; shorts are negative.

Weights are capped iteratively (excess redistributed pro rata) so constraints hold
after renormalisation where feasible; an infeasible cap (e.g. max_weight x n < 1)
leaves cash, which is reported.

exposures(weights, betas, sectors, factor_scores) -> long / short / gross / net,
beta (net), sector net weights, factor exposure (weighted mean score per factor).

Shorting: ATIP's execution path (W4, PAPER cash book) cannot sell what it does
not hold. A short leg therefore reaches the W3 strategy as a decision but is not
turned into an order -- see the W3 "portfolio" and "pairs" kinds. The
construction maths are ready for a shortable instrument (futures / SLB).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

METHODS = ("equal", "score", "inverse_vol", "risk", "factor")
NEUTRALITY = (None, "dollar", "beta", "sector")


def _raw(names, method, scores, vols, factor_exposure):
    if method == "equal":
        return {s: 1.0 for s in names}
    if method == "score":
        m = min(scores[s] for s in names)
        return {s: max(scores[s] - m, 0) + 1e-9 for s in names}
    if method in ("inverse_vol", "risk"):
        return {s: 1.0 / vols[s] for s in names if vols.get(s)}
    if method == "factor":
        return {s: max(factor_exposure.get(s) or 0, 0) + 1e-9 for s in names}
    raise ValueError(f"method must be one of {METHODS}")


def _cap(w, max_weight=None, sector_cap=None, sectors=None, total=1.0, iters=50):
    w = {s: v / sum(w.values()) * total for s, v in w.items()} if w and sum(w.values()) else {}
    for _ in range(iters):
        changed = False
        if max_weight:
            over = {s: v for s, v in w.items() if v > max_weight * total + 1e-12}
            if over:
                changed = True
                excess = sum(v - max_weight * total for v in over.values())
                for s in over:
                    w[s] = max_weight * total
                free = {s: v for s, v in w.items() if s not in over and v < max_weight * total}
                fs = sum(free.values())
                for s in free:
                    w[s] += excess * free[s] / fs if fs else 0
        if sector_cap and sectors:
            by = {}
            for s, v in w.items():
                by.setdefault(sectors.get(s) or "UNKNOWN", []).append(s)
            for g, members in by.items():
                tot = sum(w[s] for s in members)
                if g != "UNKNOWN" and tot > sector_cap * total + 1e-12:
                    changed = True
                    k = sector_cap * total / tot
                    for s in members:
                        w[s] *= k
        if not changed:
            break
    return w


def construct(scores, method="equal", vols=None, betas=None, sectors=None, factor_exposure=None,
              constraints=None, long_short=None, short_scores=None) -> dict:
    if long_short not in NEUTRALITY:
        raise ValueError(f"long_short must be one of {NEUTRALITY}")
    c = constraints or {}
    vols, betas, sectors = vols or {}, betas or {}, sectors or {}
    gross = float(c.get("gross", 1.0))
    longs = [s for s in scores if scores[s] is not None]
    side = gross if not long_short else gross / 2
    wl = _cap(_raw(longs, method, scores, vols, factor_exposure or {}), c.get("max_weight"), c.get("sector_cap"),
              sectors, side)
    if not long_short:
        return {"weights": wl, "cash": round(max(0.0, 1 - sum(wl.values())), 6), "method": method}
    shorts = [s for s in (short_scores or {}) if short_scores[s] is not None and s not in wl]
    inv = {s: -short_scores[s] for s in shorts}
    ws = _cap(_raw(shorts, method, inv, vols, {s: -(factor_exposure or {}).get(s, 0) for s in shorts}),
              c.get("max_weight"), c.get("sector_cap"), sectors, side)
    if long_short == "beta":
        bl = sum(wl[s] * (betas.get(s) or 1.0) for s in wl)
        bs = sum(ws[s] * (betas.get(s) or 1.0) for s in ws)
        if bs:
            ws = {s: v * bl / bs for s, v in ws.items()}
    if long_short == "sector":
        secs = {sectors.get(s) for s in wl} & {sectors.get(s) for s in ws}
        wl = {s: v for s, v in wl.items() if sectors.get(s) in secs}
        ws = {s: v for s, v in ws.items() if sectors.get(s) in secs}
        for g in secs:
            L = sum(v for s, v in wl.items() if sectors.get(s) == g)
            S = sum(v for s, v in ws.items() if sectors.get(s) == g)
            if S:
                for s in ws:
                    if sectors.get(s) == g:
                        ws[s] *= L / S
    w = dict(wl)
    w.update({s: -v for s, v in ws.items()})
    return {"weights": w, "cash": None, "method": method, "long_short": long_short}


def exposures(weights, betas=None, sectors=None, factor_scores=None) -> dict:
    betas, sectors = betas or {}, sectors or {}
    long = sum(v for v in weights.values() if v > 0)
    short = -sum(v for v in weights.values() if v < 0)
    sec = {}
    for s, v in weights.items():
        g = sectors.get(s) or "UNKNOWN"
        sec[g] = round(sec.get(g, 0.0) + v, 6)
    fac = {}
    for f, sc in (factor_scores or {}).items():
        num = sum(v * sc[s] for s, v in weights.items() if sc.get(s) is not None)
        den = sum(abs(v) for s, v in weights.items() if sc.get(s) is not None)
        fac[f] = round(num / den, 4) if den else None
    return {"long": round(long, 6), "short": round(short, 6), "gross": round(long + short, 6),
            "net": round(long - short, 6),
            "beta": round(sum(v * (betas.get(s) or 1.0) for s, v in weights.items()), 6),
            "sector_net": sec, "factor_exposure": fac}


def build(conn, as_of, spec: dict, store=True) -> dict:
    """spec: {name, key (factor/composite key to rank on), top_n, bottom_n?, method,
    constraints, long_short}. Reads stored scores for as_of; persists
    quant_portfolio + positions + exposures."""
    from quant.engine import sectors as _sectors
    key, top = spec["key"], int(spec.get("top_n", 20))
    rows = conn.execute("SELECT symbol, score FROM quant_factor_score WHERE as_of=? AND factor_key=? "
                        "ORDER BY score DESC", (str(as_of), key)).fetchall()
    if not rows:
        raise ValueError(f"no scores for {key} on {as_of} (run the factor engine first)")
    longs = {s: sc for s, sc in rows[:top]}
    shorts = {s: sc for s, sc in rows[-int(spec.get("bottom_n", 0)):]} if spec.get("bottom_n") else None
    vols = {s: v for s, v in conn.execute("SELECT symbol, raw FROM quant_factor_score WHERE as_of=? AND factor_key=?",
                                          (str(as_of), "vol_60@1"))}
    betas = {s: v for s, v in conn.execute("SELECT symbol, raw FROM quant_factor_score WHERE as_of=? AND factor_key=?",
                                           (str(as_of), "beta_250@1"))}
    sec = _sectors()
    res = construct(longs, spec.get("method", "equal"), vols, betas, sec, longs, spec.get("constraints"),
                    spec.get("long_short"), shorts)
    exp = exposures(res["weights"], betas, sec, {key: dict(rows)})
    out = {"portfolio_id": "PF" + uuid.uuid4().hex[:14].upper(), "as_of": str(as_of), "spec": spec, **res,
           "exposures": exp}
    if store:
        now = datetime.now()
        conn.execute("INSERT INTO quant_portfolio (portfolio_id,name,as_of,spec_json,method,long_short,cash,"
                     "created_at) VALUES (?,?,?,?,?,?,?,?)", (out["portfolio_id"], spec.get("name", key), str(as_of),
                                                             json.dumps(spec), res["method"], spec.get("long_short"),
                                                             res.get("cash"), now))
        conn.executemany("INSERT INTO quant_portfolio_position (portfolio_id,symbol,weight,side,sector) "
                         "VALUES (?,?,?,?,?)", [(out["portfolio_id"], s, w, "LONG" if w > 0 else "SHORT", sec.get(s))
                                                for s, w in res["weights"].items()])
        conn.execute("INSERT INTO quant_exposure (portfolio_id,as_of,exposure_json,created_at) VALUES (?,?,?,?)",
                     (out["portfolio_id"], str(as_of), json.dumps(exp), now))
        conn.commit()
    return out
