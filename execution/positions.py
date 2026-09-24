"""
Positions and exposure for the risk engine and the API.

    book()                the PAPER book from portfolio/pnl.py (W1): equity, cash,
                          positions valued at the latest close
    strategy_positions()  per-strategy attribution built from oms_fill: net
                          quantity, average cost (average-cost method), realised
                          and unrealised P&L. The paper broker keeps ONE book
                          (paper_position); W4 fills say which strategy each
                          share came from.
    sectors()             symbol -> NSE industry from the cached Nifty 500 list
                          (data/index_constituents.py); no network call here
    exposure()            everything above, summed by symbol / sector / strategy
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

log = logging.getLogger("atip.execution")


def latest_close(conn, symbol: str, on: date | None = None):
    r = conn.execute("SELECT close, date FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                     (symbol, str(on or date.today()))).fetchone()
    return (float(r[0]), str(r[1])[:10]) if r and r[0] else (None, None)


def book(conn, env: str = "PAPER") -> dict:
    from portfolio.pnl import portfolio_summary
    return portfolio_summary(conn, env)


def held_quantity(conn, symbol: str) -> int:
    try:
        r = conn.execute("SELECT quantity FROM paper_position WHERE symbol=?", (symbol,)).fetchone()
    except Exception:
        return 0
    return int(r[0] or 0) if r else 0


_SECTORS = {"map": None}


def sectors() -> dict:
    """symbol -> industry; {} when the cached list is unavailable (never fetches)."""
    if _SECTORS["map"] is None:
        try:
            import pandas as pd
            from data.index_constituents import NIFTY500_CACHE
            df = pd.read_csv(NIFTY500_CACHE)
            sym = next(c for c in ("Symbol", "SYMBOL", "symbol") if c in df.columns)
            ind = next(c for c in ("Industry", "INDUSTRY", "industry") if c in df.columns)
            _SECTORS["map"] = {str(s).strip().upper(): str(i).strip() for s, i in zip(df[sym], df[ind])
                               if str(i).strip() and str(i).lower() != "nan"}
        except Exception as e:
            log.warning(f"  sector map unavailable: {e}")
            return {}
    return _SECTORS["map"]


def strategy_positions(conn, strategy_id: str | None = None) -> list:
    q = "SELECT strategy_id, symbol, side, quantity, price, fees FROM oms_fill"
    args = []
    if strategy_id:
        q += " WHERE strategy_id=?"; args.append(strategy_id)
    q += " ORDER BY filled_at, fill_id"
    st = defaultdict(lambda: {"qty": 0, "avg": 0.0, "realised": 0.0, "fees": 0.0})
    for sid, sym, side, qty, px, fees in conn.execute(q, args):
        p = st[(sid, sym)]
        p["fees"] += fees or 0
        if side == "BUY":
            p["avg"] = (p["avg"] * p["qty"] + px * qty) / (p["qty"] + qty) if p["qty"] + qty else 0.0
            p["qty"] += qty
        else:
            sold = min(qty, p["qty"])
            p["realised"] += (px - p["avg"]) * sold
            p["qty"] -= sold
            if p["qty"] == 0:
                p["avg"] = 0.0
    out = []
    for (sid, sym), p in sorted(st.items()):
        mark, mark_date = latest_close(conn, sym)
        value = round(mark * p["qty"], 2) if mark is not None else None
        unreal = round((mark - p["avg"]) * p["qty"], 2) if mark is not None and p["qty"] else 0.0
        out.append({"strategy_id": sid, "symbol": sym, "quantity": p["qty"], "avg_price": round(p["avg"], 4),
                    "mark": mark, "mark_date": mark_date, "value": value, "unrealised": unreal,
                    "realised": round(p["realised"], 2), "fees": round(p["fees"], 2),
                    "pnl": round(p["realised"] + unreal - p["fees"], 2)})
    return out


def exposure(conn) -> dict:
    b = book(conn)
    sec = sectors()
    by_sector = defaultdict(float)
    for p in b["positions"]:
        by_sector[sec.get(p["symbol"], "UNKNOWN")] += p["value"] or 0.0
    sp = strategy_positions(conn)
    by_strategy = defaultdict(lambda: {"value": 0.0, "pnl": 0.0, "positions": 0})
    for p in sp:
        s = by_strategy[p["strategy_id"]]
        s["value"] += p["value"] or 0.0
        s["pnl"] += p["pnl"]
        s["positions"] += 1 if p["quantity"] else 0
    return {"equity": b["equity"], "cash": b["cash"], "positions_value": b["positions_value"],
            "n_positions": b["n_positions"], "positions": b["positions"],
            "by_sector": {k: round(v, 2) for k, v in sorted(by_sector.items())},
            "sector_map_available": bool(sec),
            "by_strategy": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
                            for k, v in sorted(by_strategy.items())},
            "strategy_positions": sp}
