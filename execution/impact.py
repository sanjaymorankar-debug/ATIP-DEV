"""
Market impact model (W34: EX-12).

Pre-trade estimate of what an order of Q shares costs beyond the quoted price, using the
square-root law (Almgren et al. 2005; Torre / Barra; widely replicated on equities):

    impact_bps   = Y x sigma_daily_bps x sqrt(Q / ADV)            (permanent + temporary, one number)
    spread_bps   = half the estimated quoted spread               (crossing cost)
    total_bps    = spread_bps + impact_bps
    cost_rs      = total_bps / 1e4 x Q x price

    sigma_daily  stdev of daily log returns over SIGMA_WINDOW sessions (prices_daily)
    ADV          median daily traded shares over ADV_WINDOW sessions (median: one block day
                 does not make a stock liquid)
    spread       not stored by ATIP; estimated from the daily range with the Corwin-Schultz
                 (2012) high-low estimator over the last 20 sessions, floored at MIN_SPREAD_BPS
    Y            coefficient. DEFAULT_Y = 0.7 (the literature's 0.5-1.0 band). calibrate() fits it
                 from LIVE fills only (EX-09 measured slippage, reference price as benchmark):
                 paper fills are priced by a fixed-bps model, so fitting them would only recover
                 that constant. Until there are MIN_CALIBRATION_FILLS live fills the default holds,
                 and the estimate says so (y_source).

Participation: Q / ADV is reported with the estimate. Above MAX_PARTICIPATION the estimate is
marked out-of-model -- the square-root law is fitted on small participations and a single
order of a large fraction of a day's volume is not something it describes.

Used by: execution algos (EX-11) to pick a horizon, the event-driven backtester (BT-17) as its
fill-cost model, and GET /api/execution/impact for a pre-trade check. It is an estimate, not a
risk limit: nothing is blocked on it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

SIGMA_WINDOW = 60
ADV_WINDOW = 20
DEFAULT_Y = 0.7
MIN_SPREAD_BPS = 2.0
MAX_PARTICIPATION = 0.25
MIN_CALIBRATION_FILLS = 30


def _bars(conn, symbol, n, as_of=None):
    sql = "SELECT date, high, low, close, volume FROM prices_daily WHERE symbol=? AND close>0"
    args = [symbol]
    if as_of:
        sql += " AND date<=?"
        args.append(str(as_of)[:10])
    rows = conn.execute(sql + " ORDER BY date DESC LIMIT ?", args + [n]).fetchall()
    return list(reversed(rows))


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def corwin_schultz_spread(bars) -> float | None:
    """Mean Corwin-Schultz spread (fraction of price) over consecutive bar pairs; negatives -> 0."""
    vals = []
    for (_, h0, l0, _, _), (_, h1, l1, _, _) in zip(bars, bars[1:]):
        if not (h0 and l0 and h1 and l1) or l0 <= 0 or l1 <= 0:
            continue
        beta = math.log(h0 / l0) ** 2 + math.log(h1 / l1) ** 2
        hh, ll = max(h0, h1), min(l0, l1)
        gamma = math.log(hh / ll) ** 2
        k = 3 - 2 * math.sqrt(2)
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        s = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
        vals.append(max(0.0, s))
    return sum(vals) / len(vals) if vals else None


def stock_inputs(conn, symbol, as_of=None) -> dict:
    bars = _bars(conn, symbol, max(SIGMA_WINDOW, ADV_WINDOW) + 1, as_of)
    if len(bars) < 10:
        return {"symbol": symbol, "ok": False, "reason": f"only {len(bars)} daily bars"}
    closes = [b[3] for b in bars]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
    rets = rets[-SIGMA_WINDOW:]
    mu = sum(rets) / len(rets)
    sigma = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1))
    adv = _median([b[4] for b in bars[-ADV_WINDOW:] if b[4]])
    spread = corwin_schultz_spread(bars[-21:])
    return {"symbol": symbol, "ok": bool(adv and sigma), "price": closes[-1], "as_of": str(bars[-1][0])[:10],
            "sigma_daily_bps": round(sigma * 1e4, 2), "adv_shares": adv,
            "spread_bps": round(max(MIN_SPREAD_BPS, (spread or 0) * 1e4), 2),
            "spread_source": "corwin_schultz" if spread else "floor"}


def current_y(conn) -> tuple:
    try:
        r = conn.execute("SELECT y, n_fills, created_at FROM execution_impact_calibration ORDER BY created_at DESC "
                         "LIMIT 1").fetchone()
        if r and r[1] and r[1] >= MIN_CALIBRATION_FILLS and r[0] is not None:
            return float(r[0]), f"calibrated on {r[1]} live fills ({str(r[2])[:10]})"
    except Exception:
        pass
    return DEFAULT_Y, "default (no live-fill calibration yet)"


def estimate(conn, symbol, quantity, side="BUY", price=None, as_of=None, y=None, inputs=None) -> dict:
    q = abs(int(quantity))
    inp = inputs or stock_inputs(conn, symbol, as_of)
    if not inp.get("ok"):
        return {"symbol": symbol, "quantity": q, "ok": False, "reason": inp.get("reason", "no volume / volatility")}
    yy, ysrc = (y, "given") if y is not None else current_y(conn)
    px = float(price or inp["price"])
    part = q / inp["adv_shares"] if inp["adv_shares"] else None
    impact = yy * inp["sigma_daily_bps"] * math.sqrt(part) if part is not None else None
    total = inp["spread_bps"] / 2 + (impact or 0.0)
    return {"symbol": symbol, "side": side.upper(), "quantity": q, "price": round(px, 2), "ok": True,
            "participation": round(part, 5) if part is not None else None,
            "out_of_model": bool(part is not None and part > MAX_PARTICIPATION),
            "half_spread_bps": round(inp["spread_bps"] / 2, 2), "impact_bps": round(impact or 0.0, 2),
            "total_bps": round(total, 2), "cost_rs": round(total / 1e4 * q * px, 2),
            "y": round(yy, 4), "y_source": ysrc, "inputs": inp}


def fill_price(side, ref_price, est: dict) -> float:
    """The reference price moved against the trade by the estimate's total bps."""
    bps = est.get("total_bps") or 0.0
    sign = 1 if side.upper() == "BUY" else -1
    return round(max(0.01, ref_price * (1 + sign * bps / 1e4)), 4)


def horizon_for(est: dict, max_participation=0.1) -> int:
    """Sessions to spread an order over so each day's slice stays under max_participation of ADV."""
    p = est.get("participation") or 0.0
    return max(1, math.ceil(p / max_participation)) if p else 1


def calibrate(conn, days=180) -> dict:
    """Least squares through the origin of (measured bps - half spread) on sigma x sqrt(participation),
    LIVE fills only. Stored in execution_impact_calibration; < MIN_CALIBRATION_FILLS -> not adopted."""
    from execution.analytics import slippage
    xs, ys = [], []
    fills = [f for f in slippage(conn, days=days) if f.get("mode") == "LIVE" and f.get("slippage_bps") is not None]
    for f in fills:
        inp = stock_inputs(conn, f["symbol"], str(f["filled_at"])[:10])
        if not inp.get("ok") or not inp["adv_shares"]:
            continue
        x = inp["sigma_daily_bps"] * math.sqrt(f["quantity"] / inp["adv_shares"])
        if x <= 0:
            continue
        xs.append(x)
        ys.append(f["slippage_bps"] - inp["spread_bps"] / 2)
    n = len(xs)
    y = (sum(a * b for a, b in zip(xs, ys)) / sum(a * a for a in xs)) if n else None
    resid = (math.sqrt(sum((b - y * a) ** 2 for a, b in zip(xs, ys)) / max(1, n - 1)) if n > 1 else None)
    conn.execute("INSERT INTO execution_impact_calibration (created_at,n_fills,y,residual_bps,adopted,detail_json) "
                 "VALUES (?,?,?,?,?,?)", (datetime.now(), n, None if y is None else round(y, 4),
                                          None if resid is None else round(resid, 2), int(n >= MIN_CALIBRATION_FILLS),
                                          json.dumps({"days": days, "live_fills_considered": len(fills)})))
    conn.commit()
    return {"n_fills": n, "y": y, "residual_bps": resid, "adopted": n >= MIN_CALIBRATION_FILLS,
            "note": None if n >= MIN_CALIBRATION_FILLS else
            f"{n} live fills (< {MIN_CALIBRATION_FILLS}); default Y={DEFAULT_Y} stays in use"}


def run_scheduled(trade_date=None) -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        r = calibrate(conn)
        return {"status": "SUCCESS", "rows": r["n_fills"], **r}
    finally:
        conn.close()
