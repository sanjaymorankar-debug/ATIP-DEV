"""
Measurements behind the W25 pre-trade limits (used by execution/risk_engine.py and
orders/risk.py). Each returns None when it cannot be measured -- the caller decides
(the W4 engine fails closed on a limit that is set but unmeasurable).

    symbol_volatility(conn, symbol)           RK-09  annualised stdev of 60 daily returns, %
    average_daily_volume(conn, symbol)        RK-10  mean shares traded over 20 sessions
    correlation_to_book(conn, symbol, pos)    RK-11  value-weighted mean correlation (120 sessions)
                                                     of the symbol with the held positions
    post_trade_var(conn, pos, symbol, value, equity)
                                              RK-12  1-day 95% historical VaR of the book
                                                     after the order, % of equity
"""

from __future__ import annotations

import math

import numpy as np


def _closes(conn, symbol, n):
    rows = conn.execute("SELECT close, volume FROM prices_daily WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT ?",
                        (symbol, int(n) + 1)).fetchall()
    return rows[::-1]


def symbol_volatility(conn, symbol, sessions: int = 60) -> float | None:
    rows = _closes(conn, symbol, sessions)
    if len(rows) < max(20, sessions // 2):
        return None
    c = np.array([float(r[0]) for r in rows])
    return round(float(np.std(c[1:] / c[:-1] - 1, ddof=1) * math.sqrt(252) * 100), 3)


def average_daily_volume(conn, symbol, sessions: int = 20) -> float | None:
    rows = conn.execute("SELECT volume FROM prices_daily WHERE symbol=? AND volume>0 ORDER BY date DESC LIMIT ?",
                        (symbol, int(sessions))).fetchall()
    if len(rows) < max(5, sessions // 2):
        return None
    return float(np.mean([float(r[0]) for r in rows]))


def correlation_to_book(conn, symbol, positions: list, sessions: int = 120) -> dict | None:
    """{avg, max, max_with, n}; positions = [{symbol, value}] (the symbol itself is skipped)."""
    others = [p for p in positions if p["symbol"] != symbol and (p.get("value") or 0) > 0]
    if not others:
        return {"avg": 0.0, "max": None, "max_with": None, "n": 0}
    from portfolio.risk import returns_matrix
    try:
        rm = returns_matrix(conn, [symbol] + [p["symbol"] for p in others], None, sessions)
    except ValueError:
        return None
    if symbol not in rm["symbols"]:
        return None
    R = rm["R"]
    i0 = rm["symbols"].index(symbol)
    vals = {p["symbol"]: p["value"] for p in others}
    num = den = 0.0
    best = (None, -2.0)
    for j, s in enumerate(rm["symbols"]):
        if j == i0:
            continue
        c = float(np.corrcoef(R[:, i0], R[:, j])[0, 1])
        if not math.isfinite(c):
            continue
        num += c * vals[s]
        den += vals[s]
        if c > best[1]:
            best = (s, c)
    if den <= 0:
        return None
    return {"avg": round(num / den, 4), "max": round(best[1], 4), "max_with": best[0], "n": len(rm["symbols"]) - 1}


def post_trade_var(conn, positions: list, symbol: str, add_value: float, equity: float,
                   sessions: int = 250) -> float | None:
    """1-day 95% historical VaR (% of equity) of the book with `add_value` more of `symbol`.
    Names without enough history are excluded (their value is not in the VaR)."""
    if not equity or equity <= 0:
        return None
    book = {p["symbol"]: float(p.get("value") or 0) for p in positions if (p.get("value") or 0) > 0}
    book[symbol] = book.get(symbol, 0.0) + float(add_value or 0)
    from portfolio.risk import returns_matrix
    try:
        rm = returns_matrix(conn, list(book), None, sessions)
    except ValueError:
        return None
    if symbol not in rm["symbols"]:
        return None
    v = np.array([book[s] for s in rm["symbols"]])
    pnl = rm["R"] @ v
    return round(max(float(np.quantile(-pnl, 0.95)), 0.0) / equity * 100, 4)
