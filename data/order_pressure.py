"""
W39 (OB-01..OB-03) — pending-order pressure: how much is waiting to be bought and sold in each
stock's order book, for the whole tracked universe, through the session.

Dhan's market-quote call (dhan.quote_data, up to 1,000 instruments per request) returns, per
stock, the TOTAL pending buy and sell quantity across the whole book plus five levels of depth.
data/dhan.py's quote refresh drops both; data/depth.py keeps only the top five levels for 25
stocks. This keeps the totals for every tracked stock every 15 minutes:

    order_book_pressure   symbol, ts, ltp, change %, volume,
                          total_buy_qty / total_sell_qty and
                          total_imbalance = (buy - sell) / (buy + sell)          -1 .. +1
                          top5_bid_qty / top5_ask_qty, top5_imbalance, spread_bps
                          pressure: STRONG_BUYERS > 0.3 > BUYERS > 0.1 > BALANCED > -0.1 > SELLERS > -0.3 >
                          STRONG_SELLERS (on total_imbalance)

Reading it honestly (research, Cont-Kukanov-Stoikov 2014 and after): book imbalance predicts
returns over seconds to minutes and decays fast; the total quantity includes orders far from
the touch that rarely trade, and visible size can be spoofed or hidden (icebergs). So the page
shows it as context -- "who is waiting where right now" -- and `persistent()` only flags a side
that dominated 3 of the last 4 snapshots, the newest included, which is harder to fake than one reading.

Config (config.json "order_pressure"): {"enabled": true, "interval_minutes": 15, "max_symbols": 1000}
Needs the Dhan Data API subscription. Runs in market hours from pipeline/scheduler.py.
CLI: python -m data.order_pressure snapshot | now [--side buy|sell]
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "interval_minutes": 15, "max_symbols": 1000}
BANDS = ((0.3, "STRONG_BUYERS"), (0.1, "BUYERS"), (-0.1, "BALANCED"), (-0.3, "SELLERS"))

DDL = (
    """CREATE TABLE IF NOT EXISTS order_book_pressure (
        symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, ltp REAL, chg_pct REAL, volume REAL,
        total_buy_qty REAL, total_sell_qty REAL, total_imbalance REAL, top5_bid_qty REAL, top5_ask_qty REAL,
        top5_imbalance REAL, spread_bps REAL, pressure TEXT, PRIMARY KEY (symbol, ts))""",
    "CREATE INDEX IF NOT EXISTS idx_order_book_pressure_ts ON order_book_pressure(ts)",
)


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("order_pressure") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["enabled"] = out.get("enabled") is not False
    return out


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def label(imb) -> str | None:
    if imb is None:
        return None
    for lo, name in BANDS:
        if imb > lo:
            return name
    return "STRONG_SELLERS"


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def parse(q: dict) -> dict | None:
    """One Dhan quote -> the pressure row (None when it carries neither totals nor depth)."""
    from data.depth import parse_quote
    b = _f(q.get("buy_quantity", q.get("total_buy_quantity")))
    s = _f(q.get("sell_quantity", q.get("total_sell_quantity")))
    top = parse_quote(q) or {}
    if b is None and s is None and not top:
        return None
    imb = round((b - s) / (b + s), 4) if b is not None and s is not None and (b + s) > 0 else None
    ltp = _f(q.get("last_price", q.get("LTP")))
    prev = _f((q.get("ohlc") or {}).get("close")) if isinstance(q.get("ohlc"), dict) else None
    return {"ltp": ltp, "chg_pct": round((ltp / prev - 1) * 100, 2) if ltp and prev else None,
            "volume": _f(q.get("volume")), "total_buy_qty": b, "total_sell_qty": s, "total_imbalance": imb,
            "top5_bid_qty": top.get("bid_qty_5"), "top5_ask_qty": top.get("ask_qty_5"),
            "top5_imbalance": top.get("imbalance"), "spread_bps": top.get("spread_bps"), "pressure": label(imb)}


def snapshot(symbols=None, conn=None) -> dict:
    """One poll of the tracked universe (chunks of 1,000 instruments)."""
    from data import dhan as D
    from db.schema import get_connection
    if not D.HAS_DHAN:
        return {"status": "SKIPPED", "reason": "dhanhq not installed", "rows": 0}
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        symbols = symbols or D.get_tracked_symbols(conn)
        symbols = symbols[: int(settings()["max_symbols"])]
        sec_map, instruments = {}, {}
        for sym in symbols:
            sec = D.get_security_id(sym)
            if sec:
                sec_map[str(sec["security_id"])] = sym
                instruments.setdefault(sec["exchange"], []).append(int(sec["security_id"]))
        if not instruments:
            return {"status": "EMPTY", "rows": 0, "reason": "no security ids"}
        try:
            dhan, _ = D.get_dhan_client()
        except RuntimeError as e:
            return {"status": "SKIPPED", "reason": str(e), "rows": 0}
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        for exch, ids in instruments.items():
            for i in range(0, len(ids), 1000):
                resp = D._guarded(dhan.quote_data, {exch: ids[i:i + 1000]})
                data = D._unwrap_feed(resp) if resp else None
                if not data:
                    return {"status": "FAILED", "rows": n, "error": f"empty quote payload {str(resp)[:150]}"}
                for _exch, stocks in data.items():
                    if not isinstance(stocks, dict):
                        continue
                    for sid, q in stocks.items():
                        p = parse(q) if isinstance(q, dict) else None
                        if not p:
                            continue
                        conn.execute("""INSERT OR REPLACE INTO order_book_pressure (symbol, ts, ltp, chg_pct, volume,
                                total_buy_qty, total_sell_qty, total_imbalance, top5_bid_qty, top5_ask_qty,
                                top5_imbalance, spread_bps, pressure) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                     (sec_map.get(str(sid), str(sid)), ts, p["ltp"], p["chg_pct"], p["volume"],
                                      p["total_buy_qty"], p["total_sell_qty"], p["total_imbalance"],
                                      p["top5_bid_qty"], p["top5_ask_qty"], p["top5_imbalance"], p["spread_bps"],
                                      p["pressure"]))
                        n += 1
        conn.commit()
        return {"status": "SUCCESS" if n else "EMPTY", "rows": n, "ts": ts, "requested": len(symbols)}
    finally:
        if own:
            conn.close()


def latest(conn, day=None, side=None, limit=100) -> list:
    """The newest snapshot of the day per symbol, most one-sided first (side buy/sell filters)."""
    ensure_tables(conn)
    day = str(day or date.today())
    sql = ("SELECT p.* FROM order_book_pressure p JOIN (SELECT symbol, MAX(ts) m FROM order_book_pressure "
           "WHERE ts>=? AND ts<? GROUP BY symbol) x ON p.symbol=x.symbol AND p.ts=x.m WHERE p.total_imbalance IS NOT NULL")
    args = [day, day + "~"]
    if side == "buy":
        sql += " AND p.total_imbalance>0 ORDER BY p.total_imbalance DESC"
    elif side == "sell":
        sql += " AND p.total_imbalance<0 ORDER BY p.total_imbalance ASC"
    else:
        sql += " ORDER BY ABS(p.total_imbalance) DESC"
    cur = conn.execute(sql + " LIMIT ?", args + [int(limit)])
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    pers = persistent(conn, day)
    for r in rows:
        r["persistent"] = pers.get(r["symbol"])
    return rows


def persistent(conn, day=None, last_n=4, need=3, threshold=0.2) -> dict:
    """{symbol: BUYERS|SELLERS} where one side held |imbalance| > threshold in >= need of the last last_n polls,
    the newest included (a side that has just flipped is not called persistent)."""
    ensure_tables(conn)
    day = str(day or date.today())
    hist = {}
    for sym, imb in conn.execute("SELECT symbol, total_imbalance FROM order_book_pressure WHERE ts>=? AND ts<? "
                                 "ORDER BY symbol, ts DESC", (day, day + "~")):
        h = hist.setdefault(sym, [])
        if len(h) < last_n:
            h.append(imb)
    out = {}
    for sym, h in hist.items():
        if h[0] is None:
            continue
        if h[0] > threshold and sum(1 for x in h if x is not None and x > threshold) >= need:
            out[sym] = "BUYERS"
        elif h[0] < -threshold and sum(1 for x in h if x is not None and x < -threshold) >= need:
            out[sym] = "SELLERS"
    return out


def intraday(conn, symbol: str, day=None) -> list:
    ensure_tables(conn)
    day = str(day or date.today())
    cur = conn.execute("SELECT ts, ltp, total_buy_qty, total_sell_qty, total_imbalance, top5_imbalance, pressure "
                       "FROM order_book_pressure WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                       (symbol.upper(), day, day + "~"))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m data.order_pressure")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    n = sub.add_parser("now")
    n.add_argument("--side", choices=("buy", "sell"))
    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        print(json.dumps(snapshot(), indent=2, default=str))
        return
    from db.schema import get_connection
    conn = get_connection()
    try:
        print(json.dumps(latest(conn, side=a.side, limit=30), indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
