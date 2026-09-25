"""
Microstructure foundation.

What ATIP has: live_quotes -- intraday LTP / OHLC / cumulative volume snapshots
(every ~15 minutes in market hours, via the Dhan quote API). What it does NOT
have: tick data (live_ticks is empty), bid / ask, market depth, order-book
imbalance. So:

  computed from live_quotes, per symbol per day (microstructure_feature):
    n_snapshots          snapshots stored that day
    intraday_rv          annualised stdev of snapshot-to-snapshot log returns x
                         sqrt(snapshots per day x 252) -- short-term volatility
    intraday_range_pct   (max ltp - min ltp) / first ltp x 100
    trade_intensity      mean volume added per snapshot interval (shares)
    last_hour_move_pct   ltp change over the last 4 snapshots
  interface only (DATA_PENDING): spread, depth, order_imbalance, tick counts,
    market-impact inputs beyond participation

  participation(order_qty, adv)   order size as % of average daily volume -- the
                                  basic market-impact input, usable today
"""

from __future__ import annotations

import math
from datetime import date, datetime

PENDING = {"spread": "needs bid/ask quotes", "depth": "needs market depth (Dhan 20-level feed not integrated)",
           "order_imbalance": "needs order book", "tick_count": "live_ticks is empty (tick feed not scheduled)"}


def participation(order_qty, adv):
    return None if not adv else order_qty / adv * 100


def compute_day(conn, day=None) -> dict:
    day = str(day or date.today())[:10]
    rows = conn.execute("SELECT symbol, ltp, volume, timestamp FROM live_quotes WHERE substr(timestamp,1,10)=? "
                        "ORDER BY symbol, timestamp", (day,)).fetchall()
    by = {}
    for s, ltp, vol, ts in rows:
        if ltp:
            by.setdefault(s, []).append((ts, float(ltp), float(vol or 0)))
    now = datetime.now()
    n = 0
    for s, snaps in by.items():
        if len(snaps) < 3:
            continue
        p = [x[1] for x in snaps]
        rets = [math.log(p[i] / p[i - 1]) for i in range(1, len(p)) if p[i - 1] > 0]
        rv = None
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            rv = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(len(snaps) * 252) * 100
        dv = [snaps[i][2] - snaps[i - 1][2] for i in range(1, len(snaps)) if snaps[i][2] >= snaps[i - 1][2]]
        feats = {"n_snapshots": len(snaps), "intraday_rv": rv,
                 "intraday_range_pct": (max(p) - min(p)) / p[0] * 100 if p[0] else None,
                 "trade_intensity": sum(dv) / len(dv) if dv else None,
                 "last_hour_move_pct": (p[-1] / p[-5] - 1) * 100 if len(p) >= 5 and p[-5] else None}
        for k, v in feats.items():
            conn.execute("INSERT INTO microstructure_feature (symbol,date,feature,value,source,created_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol,date,feature) DO UPDATE SET value=excluded.value,"
                         "created_at=excluded.created_at", (s, day, k, v, "live_quotes", now))
        n += 1
    conn.commit()
    return {"date": day, "symbols": n, "pending": PENDING}
