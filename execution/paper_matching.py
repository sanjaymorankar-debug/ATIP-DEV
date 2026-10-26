"""
Paper matching loop (W29; fixes W4-R4, enables EX-02 stop orders).

A resting paper order -- a LIMIT that had not crossed, or an SL / SL-M waiting for its
trigger -- used to stay PENDING forever, because the paper broker only priced an order
at the moment it was placed. run_matching() re-checks every PENDING paper order against
the latest live_quotes price (no broker call when a quote <= max_quote_age_min old
exists), fills what has crossed, then refreshes every resting OMS order so its state,
fills and audit rows follow.

    run_matching(now=None)   scheduler: every execution.paper_match_minutes (2) in the session

P1-6: orders waiting for a next open (orders/paper.py, fill_basis NEXT_OPEN -- every W4 order
with execution.paper_fill_price "next_open", and "live" ones placed outside the session) are
not live-matched. They fill from the NSE Bhavcopy:

    run_eod_matching(trade_date)   post-market, once that session's Bhavcopy is loaded and before
                                   the book is valued or strategies decide on it
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger("atip.execution")

MAX_QUOTE_AGE_MIN = 3
LIVE_PENDING = "status='PENDING' AND COALESCE(fill_basis,'')<>'NEXT_OPEN'"


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
    from orders.paper import PaperBroker, ensure_tables
    conn = get_connection()
    try:
        ensure_tables(conn)
        pend = [r[0] for r in conn.execute(f"SELECT DISTINCT symbol FROM paper_order WHERE {LIVE_PENDING}")]
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


def run_eod_matching(trade_date=None) -> dict:
    """
    Fill the paper orders waiting for a next open from the Bhavcopy bars up to trade_date, bring
    their OMS orders up to date (fills, fees, audit rows), and -- with execution.protective_stops
    -- give each W4 entry filled this way its protective stop, watching the bars from the entry's
    own session (the execution cycle does that for an immediate fill; a next-open entry fills
    here, a session later).
    """
    from db.schema import get_connection
    from execution import order_manager as OM
    from execution.config import execution_settings
    from orders.paper import NEXT_OPEN, PaperBroker
    conn = get_connection()
    try:
        b = PaperBroker(conn=conn)
        waiting = conn.execute("SELECT COUNT(*) FROM paper_order WHERE status='PENDING' AND fill_basis=?",
                               (NEXT_OPEN,)).fetchone()[0]
        if not waiting:
            return {"status": "SKIPPED", "rows": 0, "reason": "no paper order waits for a next open"}
        changed = b.match_next_open(through=trade_date)
        orders = _refresh_oms(conn, OM, [c["order_id"] for c in changed])
        stops, errors = [], []
        s = execution_settings()
        if s.get("protective_stops"):
            for o in orders:
                if (o["side"] == "BUY" and o["filled_quantity"] and not o.get("parent_order_id")
                        and not o.get("algo_parent_id") and (o.get("instrument") or "CASH") == "CASH"):
                    try:
                        stops.append(OM.place_protective_stop(
                            conn, o["order_id"], limit_offset_pct=s.get("protective_stop_limit_offset_pct")))
                    except Exception as e:
                        errors.append(f"protective stop for {o['order_id']}: {e}")
            ids = [st["broker_order_id"] for st in stops if st.get("broker_order_id")]
            if ids:                                     # the entry's session may already have hit the stop
                b.match_next_open(order_ids=ids, through=trade_date)
                done = {o["order_id"]: o for o in _refresh_oms(conn, OM, ids)}
                stops = [done.get(st["order_id"], st) for st in stops]
        left = conn.execute("SELECT COUNT(*) FROM paper_order WHERE status='PENDING' AND fill_basis=?",
                            (NEXT_OPEN,)).fetchone()[0]
        filled = [c for c in changed if c["status"] == "TRADED"]
        return {"status": "SUCCESS", "rows": len(filled), "changed": changed, "waiting": left,
                "orders": [{k: o.get(k) for k in ("order_id", "symbol", "side", "status", "filled_quantity",
                                                  "avg_fill_price", "fees")} for o in orders],
                "protective_stops": [{k: st.get(k) for k in ("order_id", "symbol", "status", "trigger_price")}
                                     for st in stops],
                "errors": errors, "error": "; ".join(errors[:3]) or None}
    finally:
        conn.close()


def _refresh_oms(conn, OM, broker_ids) -> list:
    """refresh_order() for the open OMS orders behind these paper orders; returns them as they are now."""
    if not broker_ids:
        return []
    out = []
    for (oid,) in conn.execute(f"SELECT order_id FROM oms_order WHERE broker_order_id IN "
                               f"({','.join('?' * len(broker_ids))}) AND status IN ('ACKNOWLEDGED','PARTIALLY_FILLED',"
                               f"'CANCEL_PENDING') ORDER BY created_at", list(broker_ids)).fetchall():
        try:
            out.append(OM.refresh_order(conn, oid))
        except Exception as e:
            log.warning(f"  next-open fills: refresh {oid}: {e}")
    return out
