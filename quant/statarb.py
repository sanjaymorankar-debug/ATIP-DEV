"""
Statistical arbitrage toolkit (research + the W3 "pairs" strategy kind).

    hedge_ratio(a, b)           OLS slope of log(a) on log(b) (with intercept)
    spread(a, b, beta, kind)    "log": ln a - beta ln b ; "ratio": a / b
    zscore(series, n)           (last - mean) / stdev over the last n points
    correlation(a, b)           Pearson correlation of daily log returns
    adf(series, lags=1)         augmented Dickey-Fuller t-statistic (constant, `lags`
                                lagged differences), numpy OLS
    cointegration(a, b)         Engle-Granger two-step: ADF on the OLS residual of
                                log(a) on log(b); critical values for 2 variables
                                with a constant (MacKinnon 2010, asymptotic):
                                1% -3.90, 5% -3.34, 10% -3.04
    half_life(series)           -ln 2 / ln(1 + phi), phi from d(s) = a + phi s(-1)
    analyze(a_bars, b_bars, n)  all of the above for the last n common sessions

These are statistics. A p-value or "cointegrated" flag describes the sample; it
is not a validated trading edge.
"""

from __future__ import annotations

import math

import numpy as np

EG_CRITICAL = {"1%": -3.90, "5%": -3.34, "10%": -3.04}


def hedge_ratio(a, b) -> float | None:
    a, b = np.log(np.asarray(a, float)), np.log(np.asarray(b, float))
    if len(a) < 10 or np.std(b) == 0:
        return None
    X = np.column_stack([np.ones(len(b)), b])
    coef, *_ = np.linalg.lstsq(X, a, rcond=None)
    return float(coef[1])


def spread(a, b, beta=1.0, kind="log"):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if kind == "ratio":
        return (a / b).tolist()
    return (np.log(a) - beta * np.log(b)).tolist()


def zscore(series, n=None):
    s = np.asarray(series[-n:] if n else series, float)
    if len(s) < 3 or np.std(s, ddof=1) == 0:
        return None
    return float((s[-1] - s.mean()) / s.std(ddof=1))


def correlation(a, b):
    ra, rb = np.diff(np.log(np.asarray(a, float))), np.diff(np.log(np.asarray(b, float)))
    if len(ra) < 5 or np.std(ra) == 0 or np.std(rb) == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def adf(series, lags=1) -> float | None:
    s = np.asarray(series, float)
    ds = np.diff(s)
    if len(ds) < lags + 10:
        return None
    y = ds[lags:]
    cols = [np.ones(len(y)), s[lags:-1]]
    for k in range(1, lags + 1):
        cols.append(ds[lags - k:-k])
    X = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    dof = len(y) - X.shape[1]
    if dof <= 0:
        return None
    s2 = resid @ resid / dof
    cov = s2 * np.linalg.inv(X.T @ X)
    se = math.sqrt(cov[1, 1]) if cov[1, 1] > 0 else None
    return float(coef[1] / se) if se else None


def cointegration(a, b, lags=1) -> dict:
    beta = hedge_ratio(a, b)
    if beta is None:
        return {"beta": None, "adf_t": None, "cointegrated_5pct": None}
    la, lb = np.log(np.asarray(a, float)), np.log(np.asarray(b, float))
    X = np.column_stack([np.ones(len(lb)), lb])
    coef, *_ = np.linalg.lstsq(X, la, rcond=None)
    resid = la - X @ coef
    t = adf(resid, lags)
    return {"beta": beta, "intercept": float(coef[0]), "adf_t": t, "critical": EG_CRITICAL,
            "cointegrated_5pct": (t is not None and t < EG_CRITICAL["5%"])}


def half_life(series) -> float | None:
    s = np.asarray(series, float)
    if len(s) < 10:
        return None
    ds, lag = np.diff(s), s[:-1]
    X = np.column_stack([np.ones(len(lag)), lag])
    coef, *_ = np.linalg.lstsq(X, ds, rcond=None)
    phi = coef[1]
    if phi >= 0 or 1 + phi <= 0:
        return None                      # not mean-reverting in this sample
    return float(-math.log(2) / math.log(1 + phi))


def aligned(a_bars, b_bars, n):
    """Closes on the last n sessions both symbols traded, oldest first."""
    bd = {x.date: x.close for x in b_bars}
    pairs = [(x.date, x.close, bd[x.date]) for x in a_bars if x.date in bd and x.close and bd[x.date]]
    return pairs[-n:]


def analyze(a_bars, b_bars, n=120, kind="log", beta=None) -> dict:
    rows = aligned(a_bars, b_bars, n)
    if len(rows) < max(30, n // 2):
        return {"ok": False, "reason": f"only {len(rows)} common sessions (< {max(30, n // 2)})"}
    a = [r[1] for r in rows]; b = [r[2] for r in rows]
    co = cointegration(a, b)
    hb = beta if beta is not None else (co["beta"] if co["beta"] is not None else 1.0)
    sp = spread(a, b, hb, kind)
    return {"ok": True, "as_of": str(rows[-1][0]), "sessions": len(rows), "hedge_ratio": hb, "kind": kind,
            "spread": sp[-1], "zscore": zscore(sp), "correlation": correlation(a, b),
            "adf_t": co["adf_t"], "cointegrated_5pct": co["cointegrated_5pct"], "half_life": half_life(sp),
            "mean": float(np.mean(sp)), "std": float(np.std(sp, ddof=1))}
