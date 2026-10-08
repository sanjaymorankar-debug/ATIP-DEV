"""
Cross-sectional normalization. Every function takes {symbol: value} for ONE
date (None = missing) and returns {symbol: normalized} (missing stays None).

    winsorize(v, pct)            clip each tail at the pct / 100-pct percentile
    zscore(v)                    (x - mean) / stdev
    percentile(v)                0..100 by rank (ties share the average rank)
    rank(v, ascending=False)     1 = best (highest by default)
    minmax(v)                    (x - min) / (max - min) x 100
    sector_relative(v, sectors, method)   the method applied within each sector
                                 (sectors with < min_group members fall back to
                                 the whole cross-section)
    market_relative(v)           x - cross-sectional median
    neutralize(v, exposures)     residual of an OLS of v on the exposure columns
                                 (+ intercept) -- factor / beta / sector neutralization

apply(values, spec, sectors, exposures) runs a spec such as
    {"method": "percentile", "winsorize": 1.0, "relative_to": "sector"}
so every factor and composite states its own normalization. Steps, in order:
winsorize -> neutralize -> relative_to -> method. W39 (PF-15) spec key
    "neutralize": ["beta_250", "size_log", "sector"]
residualizes on those exposures first (neutralize() above): each name is a column
from exposures[name] ({symbol: value}, e.g. that date's raw factor values) except
"sector", which expands to one-hot industry dummies from `sectors` (the residual is
then demeaned within each industry). A symbol missing any exposure comes out None;
a name with no exposures passed is an error, never silently skipped.
"""

from __future__ import annotations

import math

METHODS = ("zscore", "percentile", "rank", "minmax", "raw")


def _present(v):
    return {k: x for k, x in v.items() if x is not None and not (isinstance(x, float) and math.isnan(x))}


def winsorize(v, pct=1.0):
    p = _present(v)
    if len(p) < 5 or not pct:
        return dict(v)
    xs = sorted(p.values())
    lo = xs[int(pct / 100 * (len(xs) - 1))]
    hi = xs[int((1 - pct / 100) * (len(xs) - 1))]
    return {k: (min(max(x, lo), hi) if k in p else None) for k, x in v.items()}


def zscore(v):
    p = _present(v)
    if len(p) < 2:
        return {k: None for k in v}
    m = sum(p.values()) / len(p)
    sd = math.sqrt(sum((x - m) ** 2 for x in p.values()) / (len(p) - 1))
    return {k: ((p[k] - m) / sd if k in p and sd else (0.0 if k in p else None)) for k in v}


def percentile(v):
    p = _present(v)
    if not p:
        return {k: None for k in v}
    order = sorted(p, key=lambda k: p[k])
    ranks, i = {}, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
            j += 1
        avg = (i + j) / 2
        for k in order[i:j + 1]:
            ranks[k] = avg
        i = j + 1
    n = len(order)
    return {k: (round(ranks[k] / (n - 1) * 100, 4) if n > 1 else 50.0) if k in p else None for k in v}


def rank(v, ascending=False):
    p = _present(v)
    order = sorted(p, key=lambda k: p[k], reverse=not ascending)
    pos = {k: i + 1 for i, k in enumerate(order)}
    return {k: pos.get(k) for k in v}


def minmax(v):
    p = _present(v)
    if not p:
        return {k: None for k in v}
    lo, hi = min(p.values()), max(p.values())
    return {k: ((p[k] - lo) / (hi - lo) * 100 if hi > lo else 50.0) if k in p else None for k in v}


def market_relative(v):
    p = _present(v)
    if not p:
        return dict(v)
    xs = sorted(p.values())
    med = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
    return {k: (p[k] - med) if k in p else None for k in v}


_FN = {"zscore": zscore, "percentile": percentile, "minmax": minmax, "rank": rank, "raw": dict}


def sector_relative(v, sectors, method="zscore", min_group=5):
    fn = _FN[method]
    groups = {}
    for k in v:
        groups.setdefault(sectors.get(k) or "UNKNOWN", {})[k] = v[k]
    out = {}
    whole = fn(v)
    for g, sub in groups.items():
        if g == "UNKNOWN" or len(_present(sub)) < min_group:
            out.update({k: whole[k] for k in sub})
        else:
            out.update(fn(sub))
    return out


def neutralize(v, exposures: dict):
    """exposures: {symbol: [e1, e2, ...]} (e.g. beta, log size, sector dummies)."""
    import numpy as np
    keys = [k for k in _present(v) if k in exposures and all(e is not None for e in exposures[k])]
    if len(keys) < len(next(iter(exposures.values()), [])) + 3:
        return dict(v)
    X = np.array([[1.0] + list(exposures[k]) for k in keys], dtype=float)
    y = np.array([v[k] for k in keys], dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ beta
    out = {k: None for k in v}
    out.update({k: float(r) for k, r in zip(keys, res)})
    return out


def exposure_rows(v, names, sectors=None, exposures=None) -> dict:
    """{symbol: [e1, e2, ...]} for neutralize() over the present symbols of v: one column per name
    from exposures[name], "sector" as one-hot dummies of the industries present (missing ->
    UNKNOWN) less the first, which the intercept spans."""
    exposures, sectors = exposures or {}, sectors or {}
    keys = list(_present(v))
    missing = [x for x in names if x != "sector" and x not in exposures]
    if missing:
        raise ValueError(f"neutralize: no exposures for {missing}; pass exposures={{name: {{symbol: value}}}}")
    inds = sorted({sectors.get(k) or "UNKNOWN" for k in keys})[1:] if "sector" in names else []
    rows = {}
    for k in keys:
        row = []
        for x in names:
            if x == "sector":
                row += [1.0 if (sectors.get(k) or "UNKNOWN") == g else 0.0 for g in inds]
            else:
                e = exposures[x].get(k)
                row.append(None if e is None or (isinstance(e, float) and math.isnan(e)) else float(e))
        rows[k] = row
    return rows


def apply(values: dict, spec: dict | None, sectors: dict | None = None, exposures: dict | None = None) -> dict:
    spec = spec or {}
    method = spec.get("method", "percentile")
    if method not in METHODS:
        raise ValueError(f"normalization method must be one of {METHODS}")
    v = winsorize(values, spec.get("winsorize", 1.0)) if spec.get("winsorize", 1.0) else dict(values)
    neu = spec.get("neutralize")
    if neu:
        neu = [neu] if isinstance(neu, str) else neu
        if not isinstance(neu, (list, tuple)) or not all(isinstance(x, str) and x for x in neu):
            raise ValueError("neutralize must be a list of exposure names, e.g. ['beta_250', 'size_log', 'sector']")
        v = neutralize(v, exposure_rows(v, list(neu), sectors, exposures))
    rel = spec.get("relative_to")
    if rel == "market":
        v = market_relative(v)
    if rel == "sector":
        return sector_relative(v, sectors or {}, method)
    return _FN[method](v)
