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

  vol_surface(conn, symbol, as_of=None)      W39 (AF-10): the session's implied-volatility surface from
                                             fo_contract_daily (per-contract IV of every strike of the
                                             stored -- nearest -- expiries, data/derivatives_store.py):
      smile          per expiry, the out-of-the-money side's IV at each strike (puts below the
                     underlying, calls at / above; the other side when that one has none)
      surface        IV by moneyness K/S (MONEYNESS_GRID 0.80 .. 1.20) x expiry, linear in
                     log-moneyness between stored strikes; None outside them (no extrapolation)
      term_structure ATM IV (the smile at K = S) per expiry, its slope (far - near, vol points)
                     and shape CONTANGO / FLAT / BACKWARDATION
      skew           per expiry: skew_95_105 = IV(0.95 S) - IV(1.05 S) (W30's iv_skew definition;
                     > 0 = put skew) and rr_25d = IV(25-delta call) - IV(25-delta put)
      strikes        per strike and side: close, OI, volume, the stored IV and greeks() at it
                     (the underlying close, T = calendar days to expiry / 365, r = the RISK_FREE
                     the IVs were solved with, q = 0)
      status         OK | NO_DATA (nothing stored for the symbol / date) | NO_UNDERLYING | NO_IV
                     (contracts but no usable implied vol), with a reason

Index options on NSE are European, so Black-Scholes applies; stock options are
also European-style settled on NSE. Nothing here invents a price or a volatility.
"""

from __future__ import annotations

import math
from bisect import bisect_left

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


# ── W39 (AF-10): volatility surface and term structure ──
MONEYNESS_GRID = tuple(round(0.80 + 0.05 * i, 2) for i in range(9))      # strike / underlying
TERM_FLAT_BAND = 0.25                                                   # vol points


def _interp(x, xs, ys):
    """Linear interpolation on sorted xs; None outside [xs[0], xs[-1]] -- never extrapolated."""
    if not xs or x < xs[0] - 1e-9 or x > xs[-1] + 1e-9:
        return None
    x = min(max(x, xs[0]), xs[-1])
    j = bisect_left(xs, x)
    if xs[j] == x or j == 0:
        return ys[j]
    return ys[j - 1] + (ys[j] - ys[j - 1]) * (x - xs[j - 1]) / (xs[j] - xs[j - 1])


def _r3(x):
    return None if x is None else round(x, 3)


def vol_surface(conn, symbol: str, as_of=None, grid=MONEYNESS_GRID, r=None) -> dict:
    from datetime import date
    from data.derivatives import RISK_FREE
    r = RISK_FREE if r is None else float(r)
    sym = symbol.upper()
    opt = "option_type IN ('CE','PE') AND strike>0"
    d = as_of or conn.execute(f"SELECT MAX(date) FROM fo_contract_daily WHERE symbol=? AND {opt}", (sym,)).fetchone()[0]
    if not d:
        return {"symbol": sym, "as_of": None, "status": "NO_DATA",
                "reason": f"no option contracts stored for {sym} (fo_contract_daily, filled by the F&O pipeline)"}
    td = date.fromisoformat(str(d)[:10])
    out = {"symbol": sym, "as_of": str(td)}
    rows = conn.execute(f"SELECT expiry, strike, option_type, close, oi, volume, underlying, iv FROM fo_contract_daily "
                        f"WHERE symbol=? AND date=? AND {opt} ORDER BY expiry, strike", (sym, str(td))).fetchall()
    if not rows:
        return {**out, "status": "NO_DATA", "reason": f"no option contracts stored for {sym} on {td}"}
    und = sorted(float(x[6]) for x in rows if x[6])
    if not und:
        return {**out, "status": "NO_UNDERLYING", "reason": "the stored contracts carry no underlying price"}
    S = und[len(und) // 2]
    by_exp = {}
    for x in rows:
        by_exp.setdefault(str(x[0])[:10], []).append(x)
    expiries = []
    for e in sorted(by_exp):
        dte = (date.fromisoformat(e) - td).days
        if dte <= 0:                                         # expiry day: no time value left to price
            continue
        T = dte / 365.0
        strikes = {}
        for _, K, ot, close, oi, vol, _u, iv in by_exp[e]:
            K, kind = float(K), "call" if ot == "CE" else "put"
            x = strikes.setdefault(K, {"strike": K, "moneyness": round(K / S, 4),
                                       "log_moneyness": round(math.log(K / S), 5)})
            g = greeks(S, K, T, r, iv / 100, kind) if iv and iv > 0 else {}
            x[kind] = {"iv": iv, "close": close, "oi": oi, "volume": vol, **g}
        ks, ivs, deltas = [], [], []
        for K in sorted(strikes):
            x = strikes[K]
            sides = ("put", "call") if K < S else ("call", "put")
            iv = next((x[s_]["iv"] for s_ in sides if (x.get(s_) or {}).get("iv")), None)
            x["smile_iv"] = iv
            if iv:
                ks.append(math.log(K / S))
                ivs.append(iv)
                deltas.append((greeks(S, K, T, r, iv / 100, "call")["delta"], iv))
        atm = _interp(0.0, ks, ivs)
        p95, c105 = _interp(math.log(0.95), ks, ivs), _interp(math.log(1.05), ks, ivs)
        deltas.sort()
        dx, dy = [a for a, _ in deltas], [b for _, b in deltas]
        c25, p25 = _interp(0.25, dx, dy), _interp(0.75, dx, dy)      # a 25-delta put = a 75-delta call (q = 0)
        expiries.append({"expiry": e, "dte": dte, "atm_iv": _r3(atm),
                         "skew_95_105": _r3(p95 - c105) if p95 is not None and c105 is not None else None,
                         "rr_25d": _r3(c25 - p25) if c25 is not None and p25 is not None else None,
                         "iv_by_moneyness": [_r3(_interp(math.log(m), ks, ivs)) for m in grid],
                         "points": len(ks), "strikes": [strikes[K] for K in sorted(strikes)]})
    if not any(x["points"] for x in expiries):
        return {**out, "status": "NO_IV", "underlying": S,
                "reason": "contracts stored but none with an implied volatility on a live expiry (prices outside "
                          "no-arbitrage bounds give none)"}
    term = [{k: x[k] for k in ("expiry", "dte", "atm_iv", "skew_95_105", "rr_25d")} for x in expiries]
    atms = [t for t in term if t["atm_iv"] is not None]
    slope = round(atms[-1]["atm_iv"] - atms[0]["atm_iv"], 3) if len(atms) >= 2 else None
    shape = None if slope is None else ("CONTANGO" if slope > TERM_FLAT_BAND else "BACKWARDATION"
                                        if slope < -TERM_FLAT_BAND else "FLAT")
    return {**out, "status": "OK", "underlying": S, "risk_free": r, "iv_unit": "% annualised",
            "surface": {"moneyness": list(grid), "expiries": [x["expiry"] for x in expiries],
                        "dte": [x["dte"] for x in expiries], "iv": [x["iv_by_moneyness"] for x in expiries]},
            "term_structure": term, "term_slope": slope, "term_shape": shape, "expiries": expiries,
            "method": "OTM-side smile per expiry, linear in log-moneyness (no extrapolation); Black-Scholes greeks at "
                      "the stored IV, q = 0"}
