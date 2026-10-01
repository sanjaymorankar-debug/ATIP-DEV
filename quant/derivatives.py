"""
Derivatives foundation: analytics are implemented; DATA IS PENDING.

ATIP stores no futures or options data today (no table, no feed). This module
provides the maths and the storage interface so a future feed (e.g. the NSE
F&O bhavcopy, or Dhan's option chain) only has to fill derivatives_quote:

  Black-Scholes (European, continuous dividend yield q):
    bs_price(S, K, T, r, sigma, kind, q)     T in years, r / q / sigma as decimals
    greeks(S, K, T, r, sigma, kind, q)       delta, gamma, theta (per day), vega
                                             (per 1 vol point), rho (per 1% rate)
    implied_vol(price, S, K, T, r, kind, q)  bisection on [1e-4, 5]; None if the price
                                             is outside the no-arbitrage bounds
  iv_rank(history, today)                    (today - min) / (max - min) x 100
  iv_percentile(history, today)              share of history below today x 100
  futures_basis(F, S, days)                  basis %, annualised basis %
  roll_yield(near, next_, days_between)      annualised near-to-next spread
  rollover_pct(oi_near, oi_next)             share of open interest in the next expiry

Index options on NSE are European, so Black-Scholes applies; stock options are
also European-style settled on NSE. Nothing here invents a price or a volatility.
"""

from __future__ import annotations

import math

DATA_STATUS = ("NSE F&O bhavcopy integrated (W27 data/derivatives.py summary, W35 data/derivatives_store.py contracts + "
               "option chains); derivatives factors in quant/factors.py (W36, AF-06)")


def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _n(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1d2(S, K, T, r, sigma, q=0.0):
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def bs_price(S, K, T, r, sigma, kind="call", q=0.0):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if kind == "call" else (K - S))
    d1, d2 = _d1d2(S, K, T, r, sigma, q)
    if kind == "call":
        return S * math.exp(-q * T) * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * math.exp(-q * T) * _N(-d1)


def greeks(S, K, T, r, sigma, kind="call", q=0.0) -> dict:
    if T <= 0 or sigma <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None}
    d1, d2 = _d1d2(S, K, T, r, sigma, q)
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    gamma = dq * _n(d1) / (S * sigma * math.sqrt(T))
    vega = S * dq * _n(d1) * math.sqrt(T) / 100
    if kind == "call":
        delta = dq * _N(d1)
        theta = (-S * dq * _n(d1) * sigma / (2 * math.sqrt(T)) - r * K * dr * _N(d2) + q * S * dq * _N(d1)) / 365
        rho = K * T * dr * _N(d2) / 100
    else:
        delta = dq * (_N(d1) - 1)
        theta = (-S * dq * _n(d1) * sigma / (2 * math.sqrt(T)) + r * K * dr * _N(-d2) - q * S * dq * _N(-d1)) / 365
        rho = -K * T * dr * _N(-d2) / 100
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


def implied_vol(price, S, K, T, r, kind="call", q=0.0, tol=1e-6):
    if T <= 0 or price is None or price <= 0:
        return None
    lo_b = max(0.0, (S * math.exp(-q * T) - K * math.exp(-r * T)) if kind == "call"
               else (K * math.exp(-r * T) - S * math.exp(-q * T)))
    hi_b = S * math.exp(-q * T) if kind == "call" else K * math.exp(-r * T)
    if not lo_b <= price <= hi_b:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, kind, q) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def iv_rank(history, today):
    h = [x for x in history if x is not None]
    if not h or today is None or max(h) == min(h):
        return None
    return (today - min(h)) / (max(h) - min(h)) * 100


def iv_percentile(history, today):
    h = [x for x in history if x is not None]
    return None if not h or today is None else sum(1 for x in h if x < today) / len(h) * 100


def futures_basis(F, S, days):
    if not S or not days:
        return None
    b = (F / S - 1) * 100
    return {"basis_pct": b, "annualised_pct": b * 365 / days}


def roll_yield(near, next_, days_between):
    return None if not near or not days_between else (next_ / near - 1) * 100 * 365 / days_between


def rollover_pct(oi_near, oi_next):
    tot = (oi_near or 0) + (oi_next or 0)
    return None if not tot else (oi_next or 0) / tot * 100


def analytics_for_quote(q: dict, spot: float, r: float = 0.065) -> dict:
    """Given a stored option quote row (strike, expiry_days, option_type, price), compute
    IV and greeks. Used once derivatives_quote is populated."""
    T = (q.get("days_to_expiry") or 0) / 365
    iv = implied_vol(q.get("price"), spot, q["strike"], T, r, q["option_type"])
    g = greeks(spot, q["strike"], T, r, iv, q["option_type"]) if iv else {}
    return {"iv": iv, **g}
