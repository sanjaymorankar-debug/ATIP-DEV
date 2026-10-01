"""
Built-in OMS event handlers (W34: EX-16). Imported by execution.events before a dispatch.

    algo_progress      order.fill / order.state of an algo child -> the parent's filled quantity and
                       average price are recomputed, and the parent is worked again right away
                       (a filled ICEBERG slice gets its next slice without waiting for the minute tick)
    fill_latency       order.fill -> EX-15 stage order.submit_to_fill (first fill of an order only)
    audit_line         order.fill and REJECTED / FAILED states -> one log line each (atip.execution)

Handlers are idempotent per (event, handler) through oms_event_delivery, and each reads the current
tables rather than trusting the payload, so a late or replayed event does no harm.
"""

from __future__ import annotations

import logging
from datetime import datetime

from execution.events import subscribe

log = logging.getLogger("atip.execution.events")


def _parent_of(conn, order_id, payload):
    pid = payload.get("algo_parent_id")
    if pid:
        return pid
    r = conn.execute("SELECT algo_parent_id FROM oms_order WHERE order_id=?", (order_id,)).fetchone()
    return r[0] if r else None


@subscribe("order.fill", "algo_progress")
@subscribe("order.state", "algo_progress")
def algo_progress(conn, ev):
    pl = ev["payload"]
    if ev["topic"] == "order.state" and pl.get("to") not in ("FILLED", "PARTIALLY_FILLED", "REJECTED", "FAILED",
                                                             "CANCELLED"):
        return
    pid = _parent_of(conn, ev["key"], pl)
    if not pid:
        return
    from execution import algos
    try:
        p = algos.get_parent(conn, pid)
    except LookupError:
        return
    if p["status"] not in (algos.WAITING, algos.WORKING):
        return
    from utils.trading_calendar import is_trading_day
    now = datetime.now()
    if is_trading_day(now.date()) and algos.SESSION_OPEN <= now.time() <= algos.SESSION_CLOSE:
        algos.work_parent(conn, p, now)


@subscribe("order.fill", "fill_latency")
def fill_latency(conn, ev):
    oid = ev["key"]
    n = conn.execute("SELECT COUNT(*) FROM oms_fill WHERE order_id=?", (oid,)).fetchone()[0]
    if n != 1:
        return                                            # first fill only
    r = conn.execute("SELECT MIN(at) FROM oms_order_event WHERE order_id=? AND to_status='SUBMITTED'", (oid,)).fetchone()
    f = conn.execute("SELECT filled_at FROM oms_fill WHERE order_id=?", (oid,)).fetchone()
    if not r or not r[0] or not f:
        return
    from ops.latency import record
    def dt(v):
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:26].replace(" ", "T"))
    record("order.submit_to_fill", (dt(f[0]) - dt(r[0])).total_seconds() * 1000.0)


@subscribe("order.fill", "audit_line")
@subscribe("order.state", "audit_line")
def audit_line(conn, ev):
    pl = ev["payload"]
    if ev["topic"] == "order.fill":
        log.info(f"  [event] fill {ev['key']} {pl.get('side')} {pl.get('fill_qty')} {pl.get('symbol')} @ {pl.get('price')}"
                 + (f" (algo {pl['algo_parent_id']})" if pl.get("algo_parent_id") else ""))
    elif pl.get("to") in ("REJECTED", "FAILED"):
        log.warning(f"  [event] order {ev['key']} {pl.get('symbol')} -> {pl.get('to')}: {pl.get('message')}")
