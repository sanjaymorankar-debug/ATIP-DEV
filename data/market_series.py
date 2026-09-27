"""
ATIP -- daily market series that were only ever intraday (W21: DP-11, DP-13).

    capture_gift_nifty(trade_date)   DP-11: the session's last GIFT Nifty value from
                                     index_levels (the live index feed) stored as a daily
                                     close under the synthetic symbol GIFTNIFTY in
                                     prices_daily (source 'index_levels'), so a pre-open
                                     view and history exist. Dhan's IDX_I history is not
                                     used for GIFT Nifty: it is not a verified series there.
    compute_sector_breadth(date)     DP-13: per NSE industry (Nifty 500 constituent list):
                                     stocks, % above their 50- and 200-session averages,
                                     % advancing, average 1-day return -> sector_breadth.
    run_scheduled(trade_date)        both, post-market (pipeline/scheduler.py).

Sector INDEX history (NIFTYIT, NIFTYPHARMA, ...) comes from Dhan with the other index
series: data/dhan.py INDEX_SERIES_SYMBOLS, synced daily by the scheduler. Back-fill
once with `python -m data.market_series --backfill-sectors`.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime

from db.schema import get_connection

log = logging.getLogger(__name__)
GIFT_SYMBOL = "GIFTNIFTY"


def _d(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def capture_gift_nifty(trade_date=None, conn=None) -> dict:
    own = conn is None
    conn = conn or get_connection()
    try:
        td = str(_d(trade_date or date.today()))
        r = conn.execute("SELECT gift_nifty FROM index_levels WHERE date=? AND gift_nifty>0 ORDER BY time DESC LIMIT 1",
                         (td,)).fetchone()
        if not r:
            return {"status": "SKIPPED", "rows": 0, "reason": f"no GIFT Nifty value in index_levels for {td}"}
        from data.dhan import _store_benchmark_rows
        n = _store_benchmark_rows(conn, GIFT_SYMBOL, [td], [float(r[0])], source="index_levels")
        conn.commit()
        return {"status": "SUCCESS", "rows": n, "close": float(r[0])}
    finally:
        if own:
            conn.close()


def compute_sector_breadth(trade_date=None, conn=None) -> dict:
    own = conn is None
    conn = conn or get_connection()
    try:
        td = str(_d(trade_date or date.today()))
        try:
            from data.index_constituents import get_symbol_industry_map
            ind = get_symbol_industry_map() or {}
        except Exception as e:
            return {"status": "SKIPPED", "rows": 0, "reason": f"industry map unavailable: {e}"}
        if not ind:
            return {"status": "SKIPPED", "rows": 0, "reason": "industry map empty"}
        syms = list(ind)
        stats = {}
        for i in range(0, len(syms), 400):
            chunk = syms[i:i + 400]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT symbol, date, close FROM prices_daily WHERE symbol IN ({ph}) AND date<=? AND "
                                f"date>=date(?, '-320 days') AND close>0 ORDER BY symbol, date", (*chunk, td, td))
            cur, closes = None, []

            def flush(sym, cl):
                if sym and len(cl) >= 2 and cl[-1][0] == td:
                    c = [x[1] for x in cl]
                    stats[sym] = {"ret": c[-1] / c[-2] - 1,
                                  "a50": (c[-1] > sum(c[-50:]) / 50) if len(c) >= 50 else None,
                                  "a200": (c[-1] > sum(c[-200:]) / 200) if len(c) >= 200 else None}
            for sym, d, c in rows:
                if sym != cur:
                    flush(cur, closes)
                    cur, closes = sym, []
                closes.append((str(d)[:10], float(c)))
            flush(cur, closes)
        agg = {}
        for sym, s in stats.items():
            a = agg.setdefault(ind[sym], {"n": 0, "adv": 0, "ret": 0.0, "a50": [0, 0], "a200": [0, 0]})
            a["n"] += 1
            a["adv"] += s["ret"] > 0
            a["ret"] += s["ret"]
            for k in ("a50", "a200"):
                if s[k] is not None:
                    a[k][0] += bool(s[k])
                    a[k][1] += 1
        now = datetime.now()
        for sector, a in agg.items():
            conn.execute("INSERT INTO sector_breadth (date,sector,stocks,pct_advancing,avg_return_pct,pct_above_50dma,"
                         "pct_above_200dma,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(date,sector) DO UPDATE SET "
                         "stocks=excluded.stocks,pct_advancing=excluded.pct_advancing,"
                         "avg_return_pct=excluded.avg_return_pct,pct_above_50dma=excluded.pct_above_50dma,"
                         "pct_above_200dma=excluded.pct_above_200dma,created_at=excluded.created_at",
                         (td, sector, a["n"], round(a["adv"] / a["n"] * 100, 2), round(a["ret"] / a["n"] * 100, 3),
                          round(a["a50"][0] / a["a50"][1] * 100, 2) if a["a50"][1] else None,
                          round(a["a200"][0] / a["a200"][1] * 100, 2) if a["a200"][1] else None, now))
        conn.commit()
        return {"status": "SUCCESS" if agg else "EMPTY", "rows": len(agg), "stocks": len(stats)}
    finally:
        if own:
            conn.close()


def run_scheduled(trade_date=None) -> dict:
    conn = get_connection()
    try:
        g = capture_gift_nifty(trade_date, conn)
        s = compute_sector_breadth(trade_date, conn)
    finally:
        conn.close()
    return {"status": "SUCCESS" if s["status"] in ("SUCCESS", "EMPTY") else s["status"],
            "rows": s.get("rows", 0), "gift_nifty": g, "sector_breadth": s}


def backfill_sectors(days=420) -> dict:
    from data.dhan import INDEX_SERIES_SYMBOLS, sync_index_benchmark_history
    return {k: sync_index_benchmark_history(days=days, index_key=k).get("status") for k in INDEX_SERIES_SYMBOLS
            if k.startswith("nifty_")}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--backfill-sectors", action="store_true", help="download ~420 days of sector-index history")
    ap.add_argument("--days", type=int, default=420)
    a = ap.parse_args()
    print(backfill_sectors(a.days) if a.backfill_sectors else run_scheduled(a.date))
