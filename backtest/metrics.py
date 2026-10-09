"""
Performance and drawdown metrics (BT-09, BT-11). Pure functions of numbers:
no database, no clock, no randomness -- the same input always gives the same
output, and each can be checked on its own.

Conventions (stated, not invented):
  * returns are simple daily returns of the equity curve, r_t = E_t / E_{t-1} - 1,
    one per trading session;
  * annualisation uses PERIODS_PER_YEAR = 252 sessions;
  * CAGR uses calendar time: (E_end / E_start) ** (365.25 / days) - 1, days
    between the first and last equity dates; undefined (None) under one day;
  * volatility = sample standard deviation of daily returns x sqrt(252);
  * Sharpe = mean(r - rf_daily) / stdev(r) x sqrt(252), where rf_daily is the
    annual risk-free rate compounded down to a session: (1 + rf) ** (1/252) - 1
    (default rf = 0 -- configurable as backtest.risk_free_rate_pct);
  * Sortino = mean(r - rf_daily) / downside deviation x sqrt(252); downside
    deviation = sqrt(mean(min(0, r - rf_daily)^2)) over ALL sessions (not only
    the losing ones), the common "target downside deviation" definition;
  * Calmar = CAGR / |maximum drawdown|, both as fractions;
  * drawdown_t = E_t / max(E_0..E_t) - 1 (0 at a new peak, negative below);
  * a metric that is undefined (no variance, no losses, too little data) is
    None, never 0 or infinity.
  * W23 (BT-12) tail risk on daily returns, as positive loss fractions:
    var_95 / var_99   historical Value-at-Risk: the loss exceeded on 5% / 1% of sessions
                      (the 5th / 1st percentile of returns, sign flipped; linear interpolation)
    cvar_95 / cvar_99 expected shortfall: the mean loss on the sessions at or beyond VaR
    var_95_param      parametric (normal) VaR: -(mean - 1.645 x stdev)
    skew, excess_kurtosis of daily returns; worst_day, best_day
    None with fewer than 20 returns (99% figures need 100).
Trade statistics are per closed trade, on net P&L (after costs):
  win = net P&L > 0; profit factor = gross profits / |gross losses|;
  expectancy = mean net P&L per trade (also given as mean net return %).
"""

from __future__ import annotations

import math
from datetime import date

PERIODS_PER_YEAR = 252


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _stdev(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def daily_returns(equity: list) -> list:
    return [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1]]


def total_return(equity: list) -> float | None:
    if len(equity) < 2 or not equity[0]:
        return None
    return equity[-1] / equity[0] - 1


def cagr(equity: list, dates: list) -> float | None:
    if len(equity) < 2 or not equity[0] or equity[-1] <= 0:
        return None
    days = (dates[-1] - dates[0]).days
    if days < 1:
        return None
    return (equity[-1] / equity[0]) ** (365.25 / days) - 1


def _rf_daily(rf_annual: float, periods_per_year: float = PERIODS_PER_YEAR) -> float:
    return (1 + rf_annual) ** (1 / periods_per_year) - 1


# periods_per_year: 252 sessions unless the caller measured its own series' frequency (data/mf_analytics.py
# annualises NAV returns with the window's actual number of NAV observations a year)

def volatility(returns: list, periods_per_year: float = PERIODS_PER_YEAR) -> float | None:
    sd = _stdev(returns)
    return sd * math.sqrt(periods_per_year) if sd is not None else None


def sharpe(returns: list, rf_annual: float = 0.0, periods_per_year: float = PERIODS_PER_YEAR) -> float | None:
    sd = _stdev(returns)
    if not sd:
        return None
    rf = _rf_daily(rf_annual, periods_per_year)
    return _mean([r - rf for r in returns]) / sd * math.sqrt(periods_per_year)


def sortino(returns: list, rf_annual: float = 0.0, periods_per_year: float = PERIODS_PER_YEAR) -> float | None:
    if len(returns) < 2:
        return None
    rf = _rf_daily(rf_annual, periods_per_year)
    dd = math.sqrt(sum(min(0.0, r - rf) ** 2 for r in returns) / len(returns))
    if not dd:
        return None
    return _mean([r - rf for r in returns]) / dd * math.sqrt(periods_per_year)


def drawdown_series(equity: list) -> list:
    """[(peak, drawdown_fraction)] per point; drawdown <= 0."""
    out, peak = [], None
    for e in equity:
        peak = e if peak is None else max(peak, e)
        out.append((peak, (e / peak - 1) if peak else 0.0))
    return out


def max_drawdown(equity: list) -> float | None:
    """Most negative drawdown as a fraction (e.g. -0.12); None without data."""
    if not equity:
        return None
    return min(dd for _p, dd in drawdown_series(equity))


def calmar(equity: list, dates: list) -> float | None:
    c, m = cagr(equity, dates), max_drawdown(equity)
    if c is None or not m:
        return None
    return c / abs(m)


def drawdown_episodes(dates: list, equity: list) -> list:
    """
    Each fall below a peak until equity regains it:
      peak_date, trough_date, recovery_date (None if not recovered by the end),
      depth (fraction), duration_sessions (peak -> recovery, or -> end),
      recovery_sessions (trough -> recovery; None if not recovered).
    Sorted deepest first. Sessions are positions in the equity list.
    """
    eps, peak_i, trough_i = [], 0, None
    for i in range(1, len(equity)):
        if equity[i] >= equity[peak_i]:
            if trough_i is not None:
                eps.append(_episode(dates, equity, peak_i, trough_i, i))
                trough_i = None
            peak_i = i
        else:
            if trough_i is None or equity[i] < equity[trough_i]:
                trough_i = i
    if trough_i is not None:
        eps.append(_episode(dates, equity, peak_i, trough_i, None))
    return sorted(eps, key=lambda e: (e["depth"], e["peak_date"]))


def _episode(dates, equity, p, t, r):
    end = r if r is not None else len(equity) - 1
    return {"peak_date": dates[p], "trough_date": dates[t],
            "recovery_date": dates[r] if r is not None else None,
            "peak_equity": equity[p], "trough_equity": equity[t],
            "depth": equity[t] / equity[p] - 1,
            "duration_sessions": end - p,
            "recovery_sessions": (r - t) if r is not None else None}


def trade_stats(net_pnls: list, net_returns: list | None = None) -> dict:
    """Win rate, average win/loss, profit factor, expectancy over closed trades."""
    n = len(net_pnls)
    wins = [x for x in net_pnls if x > 0]
    losses = [x for x in net_pnls if x < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    return {
        "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n if n else None,
        "avg_win": _mean(wins), "avg_loss": _mean(losses),
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss else None,
        "expectancy": _mean(net_pnls),
        "expectancy_return": _mean(net_returns) if net_returns else None,
        "gross_profit": gross_win, "gross_loss": gross_loss,
    }


def summarize(dates: list, equity: list, net_pnls: list, net_returns: list | None = None,
              rf_annual: float = 0.0, exposure: list | None = None) -> dict:
    """Every metric for one equity curve and its closed trades."""
    rets = daily_returns(equity)
    out = {
        "start_date": dates[0] if dates else None, "end_date": dates[-1] if dates else None,
        "sessions": len(equity),
        "start_equity": equity[0] if equity else None, "end_equity": equity[-1] if equity else None,
        "total_return": total_return(equity), "cagr": cagr(equity, dates),
        "volatility": volatility(rets), "sharpe": sharpe(rets, rf_annual),
        "sortino": sortino(rets, rf_annual), "calmar": calmar(equity, dates),
        "max_drawdown": max_drawdown(equity), "risk_free_rate": rf_annual,
        "avg_exposure": _mean(exposure) if exposure else None,
    }
    out.update(trade_stats(net_pnls, net_returns))
    out.update(tail_risk(rets))
    return out


def _pctile(sorted_xs, q):
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def tail_risk(returns: list) -> dict:
    r = sorted(x for x in returns if x is not None)
    n = len(r)
    out = {"var_95": None, "cvar_95": None, "var_99": None, "cvar_99": None, "var_95_param": None,
           "skew": None, "excess_kurtosis": None, "worst_day": r[0] if r else None, "best_day": r[-1] if r else None}
    if n < 20:
        return out
    for q, name, need in ((0.05, "95", 20), (0.01, "99", 100)):
        if n < need:
            continue
        v = _pctile(r, q)
        tail = [x for x in r if x <= v] or [r[0]]
        out[f"var_{name}"] = -v
        out[f"cvar_{name}"] = -sum(tail) / len(tail)
    mu, sd = _mean(r), _stdev(r)
    if sd:
        out["var_95_param"] = -(mu - 1.645 * sd)
        out["skew"] = sum(((x - mu) / sd) ** 3 for x in r) / n
        out["excess_kurtosis"] = sum(((x - mu) / sd) ** 4 for x in r) / n - 3
    return out


def jsonable(d):
    """Dates to ISO strings, floats rounded, for storage/API."""
    if isinstance(d, dict):
        return {k: jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [jsonable(v) for v in d]
    if isinstance(d, date):
        return d.isoformat()
    if isinstance(d, float):
        return None if (math.isnan(d) or math.isinf(d)) else round(d, 8)
    return d
