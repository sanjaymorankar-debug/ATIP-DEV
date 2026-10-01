"""
Order book / market depth (W35: DP-05).

Dhan's market-quote REST call (dhan.quote_data, already used for live quotes) returns five
levels of depth per instrument: depth.buy / depth.sell = [{price, quantity, orders}] x 5.
snapshot(symbols) polls it for a small symbol set and stores, per symbol per poll:

    order_book_snapshot   best bid / ask, mid, spread_bps = (ask - bid) / mid x 1e4,
                          bid_qty_5 / ask_qty_5 (summed top-5 quantity),
                          imbalance = (bid_qty_5 - ask_qty_5) / (bid_qty_5 + ask_qty_5)  (-1 .. +1),
                          and the five levels as JSON
    lake "depth"          the same rows, one part per poll (DP-22), for research history

These are the inputs AF-07 (quant/microstructure.py) was missing: a measured quoted spread and
depth imbalance instead of the range-based proxies. features(conn, symbol, day) aggregates a day:
time-weighted mean spread, median imbalance, quote-depth percentiles.

Config (config.json "depth", off by default):
    {"enabled": false, "symbols": [], "max_symbols": 25, "interval_seconds": 60}
symbols empty -> the stock feed's symbol set (data/stock_feed.default_symbols), capped.
Polling runs from the scheduler inside the session only. One REST call covers every symbol.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": False, "symbols": [], "max_symbols": 25, "interval_seconds": 60}


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(cfg.get("depth") or {})}
    except Exception:
        return dict(DEFAULTS)


def _levels(side) -> list:
    out = []
    for lv in side or []:
        try:
            p, q = float(lv.get("price") or 0), float(lv.get("quantity") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if p > 0:
            out.append({"price": p, "qty": q, "orders": int(lv.get("orders") or 0)})
    return out[:5]


def parse_quote(q: dict) -> dict | None:
    d = q.get("depth") if isinstance(q.get("depth"), dict) else None
    if not d:
        return None
    bids, asks = _levels(d.get("buy")), _levels(d.get("sell"))
    if not bids or not asks:
        return None
    bb, ba = bids[0]["price"], asks[0]["price"]
    mid = (bb + ba) / 2
    bq, aq = sum(x["qty"] for x in bids), sum(x["qty"] for x in asks)
    return {"ltp": q.get("last_price", q.get("LTP")), "best_bid": bb, "best_ask": ba, "mid": round(mid, 4),
            "spread_bps": round((ba - bb) / mid * 1e4, 3) if mid else None, "bid_qty_5": bq, "ask_qty_5": aq,
            "imbalance": round((bq - aq) / (bq + aq), 4) if (bq + aq) else None, "bids": bids, "asks": asks}


def _symbols(conn) -> list:
    s = settings()
    syms = [x.upper() for x in (s.get("symbols") or [])]
    if not syms:
        from data.stock_feed import default_symbols
        syms = default_symbols(conn, int(s["max_symbols"]))
    return syms[: int(s["max_symbols"])]


def snapshot(symbols=None, conn=None) -> dict:
    from data.dhan import _guarded, _unwrap_feed, get_dhan_client, get_security_id
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        symbols = symbols or _symbols(conn)
        sec_map, instruments = {}, {}
        for sym in symbols:
            sec = get_security_id(sym)
            if sec:
                sec_map[str(sec["security_id"])] = sym
                instruments.setdefault(sec["exchange"], []).append(int(sec["security_id"]))
        if not instruments:
            return {"status": "EMPTY", "rows": 0, "reason": "no security ids"}
        dhan, _ = get_dhan_client()
        resp = _guarded(dhan.quote_data, instruments)
        data = _unwrap_feed(resp) if resp else None
        if not data:
            return {"status": "FAILED", "rows": 0, "reason": f"empty quote payload {str(resp)[:150]}"}
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for exch, stocks in data.items():
            if not isinstance(stocks, dict):
                continue
            for sid, q in stocks.items():
                if not isinstance(q, dict):
                    continue
                p = parse_quote(q)
                if not p:
                    continue
                sym = sec_map.get(str(sid), str(sid))
                conn.execute("INSERT OR REPLACE INTO order_book_snapshot (symbol,ts,ltp,best_bid,best_ask,mid,spread_bps,"
                             "bid_qty_5,ask_qty_5,imbalance,bids_json,asks_json,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (sym, ts, p["ltp"], p["best_bid"], p["best_ask"], p["mid"], p["spread_bps"], p["bid_qty_5"],
                              p["ask_qty_5"], p["imbalance"], json.dumps(p["bids"]), json.dumps(p["asks"]), "dhan_rest"))
                rows.append({"symbol": sym, "ts": ts, **{k: v for k, v in p.items() if k not in ("bids", "asks")},
                             "bids": json.dumps(p["bids"]), "asks": json.dumps(p["asks"])})
        conn.commit()
        if rows:
            import pandas as pd
            from data import lake
            lake.write("depth", ts[:10], pd.DataFrame(rows), knowledge_time=ts, source="dhan_quote_depth", conn=conn)
        return {"status": "SUCCESS" if rows else "EMPTY", "rows": len(rows), "requested": len(symbols)}
    finally:
        if own:
            conn.close()


def features(conn, symbol: str, day=None) -> dict | None:
    day = str(day or date.today())[:10]
    rows = conn.execute("SELECT ts, spread_bps, imbalance, bid_qty_5, ask_qty_5 FROM order_book_snapshot WHERE symbol=? "
                        "AND ts>=? AND ts<=? ORDER BY ts", (symbol.upper(), f"{day} 00:00:00", f"{day} 23:59:59")).fetchall()
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return None
    ts = [datetime.fromisoformat(str(r[0]).replace(" ", "T")) for r in rows]
    w = [max(1.0, (b - a).total_seconds()) for a, b in zip(ts, ts[1:])] + [60.0]   # time weights
    sp = sum(r[1] * x for r, x in zip(rows, w)) / sum(w)
    imb = [r[2] for r in rows if r[2] is not None]
    depth = sorted((r[3] or 0) + (r[4] or 0) for r in rows)
    return {"symbol": symbol.upper(), "day": day, "snapshots": len(rows), "tw_spread_bps": round(sp, 3),
            "median_imbalance": round(statistics.median(imb), 4) if imb else None,
            "depth_p10": depth[len(depth) // 10], "depth_median": depth[len(depth) // 2]}


def run_tick() -> dict:
    """Scheduler: one snapshot when enabled and in session."""
    from data.dhan_ws import feed_window_open
    if not settings()["enabled"] or not feed_window_open():
        return {"status": "SKIPPED", "rows": 0}
    return snapshot()
