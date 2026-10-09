"""
Return and risk metrics for W15.5. Pure functions over plain lists; daily
series use 252 sessions a year (backtest/metrics.py convention, reused where it
already has the formula).

    xirr(flows)                  money-weighted annual return; flows [(date, amount)],
                                 negative = money in, positive = money out / terminal value
    twr(values, in, out, v0)     time-weighted daily returns (inflows at the start of a
                                 session, outflows at its end)
    chain(returns)               compounded total return
    annualize(total, days)
    series_metrics(dates, rets, bench_rets, rf_pct)
    trade_stats(trades)          win rate, profit factor, average / median holding days
"""

from __future__ import annotations

import math
from datetime import date

from backtest import metrics as BM

PERIODS = 252


def xirr(flows: list, lo: float = -0.9999, hi: float = 10000.0) -> float | None:
    """Annual IRR of dated cash flows (Actual/365). None when it has no sign change
    or does not converge."""
    fl = [(d, float(a)) for d, a in flows if a]
    if len(fl) < 2 or not (any(a < 0 for _, a in fl) and any(a > 0 for _, a in fl)):
        return None
    t0 = min(d for d, _ in fl)

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in fl)
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round(mid, 6)


def twr(values: list, inflows: list, outflows: list, v0: float = 0.0) -> list:
    """Daily time-weighted returns. values[i] = end-of-session value; inflows (buys,
    fees) arrive at the start of the session, outflows (sells, dividends) leave at its
    end, so an intraday round trip earns its P&L on that day:
        r = (V + out - (V_prev + in)) / (V_prev + in)
    A session that starts empty and has no inflow returns None (not invested)."""
    out = []
    prev = v0
    for v, i, o in zip(values, inflows, outflows):
        base = prev + i
        out.append((v + o - base) / base if base > 1e-9 else None)
        prev = v
    return out


def capital_bases(values: list, inflows: list, v0: float = 0.0) -> list:
    """The capital at work each session (V_prev + inflows): the TWR denominators."""
    out, prev = [], v0
    for v, i in zip(values, inflows):
        out.append(prev + i)
        prev = v
    return out


def chain(rets: list) -> float | None:
    rs = [r for r in rets if r is not None]
    if not rs:
        return None
    t = 1.0
    for r in rs:
        t *= 1 + r
    return t - 1


def annualize(total: float | None, days: int) -> float | None:
    if total is None or days <= 0 or total <= -1:
        return None
    return (1 + total) ** (365.25 / days) - 1


def _beta_alpha(r, b, rf_daily, periods: float = PERIODS):
    n = len(r)
    if n < 20:
        return None, None, None
    mr, mb = sum(r) / n, sum(b) / n
    cov = sum((x - mr) * (y - mb) for x, y in zip(r, b)) / (n - 1)
    vb = sum((y - mb) ** 2 for y in b) / (n - 1)
    if vb <= 0:
        return None, None, None
    beta = cov / vb
    alpha_d = (mr - rf_daily) - beta * (mb - rf_daily)
    te = BM._stdev([x - y for x, y in zip(r, b)])
    return beta, alpha_d * periods, (te * math.sqrt(periods) if te else None)


def series_metrics(dates: list, rets: list, bench: list | None = None, rf_pct: float = 6.5,
                   periods_per_year: float = PERIODS) -> dict:
    """Metrics of a daily return series (None entries = not invested that day, skipped). periods_per_year
    annualises volatility, Sharpe, Sortino, alpha, tracking error and the information ratio: 252 sessions
    unless the caller measured its series' own frequency (data/mf_analytics.py)."""
    ppy = periods_per_year
    pairs = [(d, r, (bench[i] if bench else None)) for i, (d, r) in enumerate(zip(dates, rets)) if r is not None]
    if not pairs:
        return {"days": 0}
    rs = [p[1] for p in pairs]
    tot = chain(rs)
    span = (pairs[-1][0] - pairs[0][0]).days + 1 if isinstance(pairs[0][0], date) else len(rs)
    eq, e = [], 1.0
    for r in rs:
        e *= 1 + r
        eq.append(e)
    rf = rf_pct / 100
    out = {"days": len(rs), "calendar_days": span, "total_return_pct": _pct(tot),
           "annualized_pct": _pct(annualize(tot, span)) if span >= 365 else None,
           "annualized_note": None if span >= 365 else "period under a year: annualized figure withheld",
           "volatility_pct": _pct(BM.volatility(rs, ppy)), "sharpe": _r(BM.sharpe(rs, rf, ppy)),
           "sortino": _r(BM.sortino(rs, rf, ppy)), "max_drawdown_pct": _pct(BM.max_drawdown([1.0] + eq))}
    bp = [(p[1], p[2]) for p in pairs if p[2] is not None]
    if bench is not None and len(bp) >= 20:
        beta, alpha, te = _beta_alpha([x for x, _ in bp], [y for _, y in bp], (1 + rf) ** (1 / ppy) - 1, ppy)
        ex = chain([x for x, _ in bp]), chain([y for _, y in bp])
        out.update({"beta": _r(beta), "alpha_annual_pct": _pct(alpha), "tracking_error_pct": _pct(te),
                    "information_ratio": _r(((sum(x for x, _ in bp) - sum(y for _, y in bp)) / len(bp) * ppy)
                                            / te) if te else None,
                    "excess_return_pct": _pct(ex[0] - ex[1]) if None not in ex else None})
    return out


def trade_stats(trades: list) -> dict:
    """trades: [{pnl, ret, days}]"""
    if not trades:
        return {"trades": 0}
    pn = [t["pnl"] for t in trades if t.get("pnl") is not None]
    wins = [x for x in pn if x > 0]
    losses = [x for x in pn if x <= 0]
    days = sorted(t["days"] for t in trades if t.get("days") is not None)
    rets = [t["ret"] for t in trades if t.get("ret") is not None]
    return {"trades": len(trades), "win_rate_pct": _pct(len(wins) / len(pn)) if pn else None,
            "profit_factor": _r(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "avg_return_pct": _pct(sum(rets) / len(rets)) if rets else None,
            "avg_win_pct": _pct(sum(r for r in rets if r > 0) / max(1, len([r for r in rets if r > 0])))
            if rets else None,
            "avg_loss_pct": _pct(sum(r for r in rets if r <= 0) / max(1, len([r for r in rets if r <= 0])))
            if rets else None,
            "avg_holding_days": round(sum(days) / len(days), 1) if days else None,
            "median_holding_days": days[len(days) // 2] if days else None,
            "total_pnl": round(sum(pn), 2) if pn else None}


def _pct(x):
    return None if x is None else round(x * 100, 3)


def _r(x):
    return None if x is None else round(x, 3)
