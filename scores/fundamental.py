"""
Fundamental Score (SC-13) and Stability Profit Index (SC-02), W27.

Both read one fundamental_data row (data/nse_filings.py builds them from NSE
integrated-filing XBRL) plus the stock's close on the scoring day, and both are
"measured or left out": a component whose input is missing is dropped and
weighted_score renormalises over the rest; fewer than MIN_COMPONENTS present
returns None. The old compute_fs() returned 50.0 when nothing parsed -- a
constant wearing the name of a measurement -- and SPI was the SAME number as FS
(scores/engine.py passed fund["fundamental_score"] twice, 0.25 of ATIP on one
input). They are now two different formulas over two different input sets:

SPI -- Stability Profit Index (weight_config index "SPI", seeded since W1):
    ROE      TTM profit / equity                  0 .. 30 %
    ROCE     TTM EBIT / (equity + debt)           0 .. 35 %
    EPS      EPS growth YoY (%)                   -30 .. 60
    Revenue  revenue growth YoY (%)               -10 .. 40
    FCF      cash conversion: FY FCF / FY profit  0 .. 1.2
    Debt     debt / equity (inverted)             0 .. 2
    PEG      P/E / EPS growth (inverted)          0.5 .. 3   (positive growth and P/E only)
    Quality  earnings stability over up to 8 quarters: mean of
             (share of profitable quarters) and (1 - CV of net margin)

FS -- Fundamental Score (weight_config index "FS", seeded here, ensure_weights()):
    Valuation  P/E (inverted)                     8 .. 60    (loss-making -> 0)
    PB         P/B (inverted)                     1 .. 10
    Margin     operating margin                   0 .. 35 %
    Momentum   profit change QoQ (%)              -30 .. 30
    Coverage   interest coverage (x)              1 .. 15    (no finance cost -> 100)
    Liquidity  current ratio                      0.8 .. 2.5

Growth and return measures sit in SPI only and valuation / momentum / balance-sheet
liquidity in FS only, so the two no longer duplicate an input. Their correlation
should still be measured (quant.research.factor_correlation) before both are
given weight in the ATIP composite.

Valuation is computed at SCORING time from the session's close and the row's
eps_ttm / book_value_ps, never from a P/E frozen at ingestion.
"""

from __future__ import annotations

import json
import math

MIN_COMPONENTS = 3

FS_DEFAULT_WEIGHTS = {"Valuation": 0.25, "PB": 0.15, "Margin": 0.20, "Momentum": 0.15,
                      "Coverage": 0.15, "Liquidity": 0.10}
FS_DESCRIPTIONS = {"Valuation": "P/E inverted", "PB": "P/B inverted", "Margin": "Operating margin",
                   "Momentum": "Profit QoQ", "Coverage": "Interest coverage", "Liquidity": "Current ratio"}
SPI_DEFAULT_WEIGHTS = {"ROE": 0.20, "ROCE": 0.15, "EPS": 0.15, "Revenue": 0.10, "FCF": 0.10, "Debt": 0.10,
                       "PEG": 0.10, "Quality": 0.10}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _mm(v, lo, hi, invert=False):
    if v is None or hi == lo:
        return None
    s = max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))
    return round(100.0 - s if invert else s, 2)


def _weighted(c: dict, w: dict):
    tw = ts = 0.0
    for k, wt in w.items():
        v = c.get(k)
        if v is not None:
            ts += v * wt
            tw += wt
    return round(ts / tw, 2) if tw > 0 else None


def ensure_weights(conn) -> int:
    """Seed the FS weight set on databases created before W27 (INSERT OR IGNORE)."""
    n = 0
    for var, wt in FS_DEFAULT_WEIGHTS.items():
        cur = conn.execute("INSERT OR IGNORE INTO weight_config (index_name,variable,weight,description,regime) "
                           "VALUES ('FS',?,?,?,'ALL')", (var, wt, FS_DESCRIPTIONS[var]))
        n += cur.rowcount or 0
    if n:
        conn.commit()
    return n


def _load(conn, index_name, default):
    if conn is None:
        return dict(default)
    rows = conn.execute("SELECT variable, weight FROM weight_config WHERE index_name=? AND active=1",
                        (index_name,)).fetchall()
    return {r[0]: r[1] for r in rows} or dict(default)


def valuation(fund: dict, price) -> dict:
    """P/E, P/B, PEG at `price` from the row's TTM EPS / book value / EPS growth."""
    price = _num(price)
    eps = _num(fund.get("eps_ttm"))
    bv = _num(fund.get("book_value_ps"))
    g = _num(fund.get("eps_growth_yoy"))
    out = {"pe": None, "pb": None, "peg": None, "loss_making": eps is not None and eps <= 0}
    if price and eps and eps > 0:
        out["pe"] = round(price / eps, 2)
    if price and bv and bv > 0:
        out["pb"] = round(price / bv, 2)
    if out["pe"] and g and g > 0:
        out["peg"] = round(out["pe"] / g, 3)
    return out


def quality_from_history(quarters: list):
    """quarters: [{"profit":..., "revenue":...}] newest first. None below 4 quarters."""
    qs = [q for q in quarters[:8] if _num(q.get("revenue")) and _num(q.get("profit")) is not None]
    if len(qs) < 4:
        return None
    profitable = sum(1 for q in qs if q["profit"] > 0) / len(qs) * 100.0
    margins = [q["profit"] / q["revenue"] for q in qs]
    mean = sum(margins) / len(margins)
    sd = math.sqrt(sum((m - mean) ** 2 for m in margins) / (len(margins) - 1))
    stability = _mm(sd / abs(mean), 0, 1, invert=True) if mean else 0.0
    return round((profitable + stability) / 2, 2)


def spi_components(fund: dict, price=None, history=None) -> dict:
    v = valuation(fund, price)
    profit_fy, fcf = _num(fund.get("profit_fy_cr")), _num(fund.get("fcf_cr"))
    c = {
        "ROE": _mm(_num(fund.get("roe")), 0, 0.30),
        "ROCE": _mm(_num(fund.get("roce")), 0, 0.35),
        "EPS": _mm(_num(fund.get("eps_growth_yoy")), -30, 60),
        "Revenue": _mm(_num(fund.get("revenue_growth_yoy")), -10, 40),
        "FCF": _mm(fcf / profit_fy, 0, 1.2) if (fcf is not None and profit_fy and profit_fy > 0) else None,
        "Debt": _mm(_num(fund.get("debt_equity")), 0, 2, invert=True),
        "PEG": _mm(v["peg"], 0.5, 3, invert=True),
        "Quality": quality_from_history(history) if history else None,
    }
    return {k: x for k, x in c.items() if x is not None}


def fs_components(fund: dict, price=None) -> dict:
    v = valuation(fund, price)
    cov = _num(fund.get("interest_coverage"))
    c = {
        "Valuation": 0.0 if v["loss_making"] else _mm(v["pe"], 8, 60, invert=True),
        "PB": _mm(v["pb"], 1, 10, invert=True),
        "Margin": _mm(_num(fund.get("operating_margin")), 0, 0.35),
        "Momentum": _mm(_num(fund.get("qoq_profit_chg")), -30, 30),
        "Coverage": 100.0 if (cov is None and _num(fund.get("debt_cr")) == 0) else _mm(cov, 1, 15),
        # (banks: no debt_cr / coverage / D/E by design -- see data/nse_filings.py)
        "Liquidity": _mm(_num(fund.get("current_ratio")), 0.8, 2.5),
    }
    return {k: x for k, x in c.items() if x is not None}


def compute_spi(fund: dict, price=None, history=None, conn=None, weights=None):
    if not fund:
        return None
    c = spi_components(fund, price, history)
    if len(c) < MIN_COMPONENTS:
        return None
    return _weighted(c, weights or _load(conn, "SPI", SPI_DEFAULT_WEIGHTS))


def compute_fs(fund: dict, price=None, conn=None, weights=None):
    if not fund:
        return None
    c = fs_components(fund, price)
    if len(c) < MIN_COMPONENTS:
        return None
    return _weighted(c, weights or _load(conn, "FS", FS_DEFAULT_WEIGHTS))


def history(conn, symbol: str, as_of=None, nature=None) -> list:
    """Stored quarters (newest first) available by `as_of` -- the Quality input."""
    sql = ("SELECT revenue_cr AS revenue, profit_cr AS profit, period_end FROM fundamental_data "
           "WHERE symbol=? AND period_end IS NOT NULL")
    args = [symbol]
    if as_of is not None:
        sql += " AND (available_from IS NULL OR DATE(available_from)<=?)"
        args.append(str(as_of)[:10])
    if nature:
        sql += " AND nature=?"
        args.append(nature)
    sql += " ORDER BY period_end DESC LIMIT 8"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def score_row(conn, fund: dict, price=None, as_of=None) -> dict:
    """Both scores for one row, with the inputs each used (for score_inputs)."""
    hist = history(conn, fund["symbol"], as_of, fund.get("nature")) if fund.get("symbol") else []
    spi = compute_spi(fund, price, hist, conn)
    fs = compute_fs(fund, price, conn)
    return {"spi": spi, "fs": fs,
            "inputs": json.dumps({"spi": sorted(spi_components(fund, price, hist)),
                                  "fs": sorted(fs_components(fund, price)), "price": _num(price)})}
