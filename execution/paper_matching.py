"""
Paper matching loop (W29; fixes W4-R4, enables EX-02 stop orders).

A resting paper order -- a LIMIT that had not crossed, or an SL / SL-M waiting for its
trigger -- used to stay PENDING forever, because the paper broker only priced an order
at the moment it was placed. run_matching() re-checks every PENDING paper order against
the latest live_quotes price (no broker call when a quote <= max_quote_age_min old
exists), fills what has crossed, then refreshes every resting OMS order so its state,
fills and audit rows follow.

    run_matching(now=None)   scheduler: every execution.paper_match_minutes (2) in the session
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger("atip.execution")

MAX_QUOTE_AGE_MIN = 3


def latest_prices(conn, symbols, now=None, max_age_min=MAX_QUOTE_AGE_MIN) -> dict:
    if not symbols:
        return {}
    now = now or datetime.now()
    since = (now - timedelta(minutes=max_age_min)).strftime("%Y-%m-%d %H:%M:%S")
    syms = sorted(set(symbols))
    out = {}
    for sym, ltp in conn.execute(
            f"SELECT q.symbol, q.ltp FROM live_quotes q JOIN (SELECT symbol, MAX(timestamp) ts FROM live_quotes WHERE "
            f"symbol IN ({','.join('?' * len(syms))}) AND REPLACE(SUBSTR(timestamp,1,19),'T',' ')>=? GROUP BY symbol) "
            f"m ON m.symbol=q.symbol AND m.ts=q.timestamp", (*syms, since)):
        if ltp:
            out[sym] = float(ltp)
    return out


def run_matching(now=None) -> dict:
    from db.schema import get_connection
    from execution import order_manager as OM
    from orders.paper import PaperBroker
    conn = get_connection()
    try:
        pend = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM paper_order WHERE status='PENDING'")]
        changed = []
        if pend:
            b = PaperBroker(conn=conn)
            changed = b.match_pending(latest_prices(conn, pend, now))
        refreshed = 0
        for (oid,) in conn.execute("SELECT order_id FROM oms_order WHERE mode='PAPER' AND status IN "
                                   "('ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_PENDING') AND broker_order_id IS NOT "
                                   "NULL").fetchall():
            try:
                OM.refresh_order(conn, oid)
                refreshed += 1
            except Exception as e:
                log.warning(f"  paper matching: refresh {oid}: {e}")
        return {"status": "SUCCESS" if (pend or refreshed) else "SKIPPED", "rows": len(changed),
                "pending_symbols": len(pend), "changed": changed, "oms_refreshed": refreshed}
    finally:
        conn.close()
