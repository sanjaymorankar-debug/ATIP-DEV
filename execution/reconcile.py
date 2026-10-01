"""
EOD execution reconciliation (BR-05), W29: the OMS's view against the broker's book.

PAPER (the book of record is the paper broker)
    positions   per symbol: OMS net filled quantity (oms_fill, mode PAPER) vs paper_position.
                A difference is EXPLAINED when paper_order rows that the OMS did not make
                (order rules, manual paper orders: tag not an OMS order id) account for it
                exactly; otherwise it is a BREAK.
    orders      every OMS order that reached the broker vs the paper_order it points at:
                status (FILLED <-> TRADED, CANCELLED, REJECTED, open <-> PENDING) and filled
                quantity must agree; an OMS order with no broker order is a BREAK.
    fills       OMS fill quantity per order = paper_order.filled_qty.

LIVE (no live execution exists): OMS LIVE fills vs the latest synced holdings
    (portfolio_holdings). With no LIVE fills this only lists holdings that ATIP did not
    trade, as INFO -- reported, never a break.

Each run is stored in reconciliation_run (status OK / BREAKS, counts, details) and a BREAKS
run raises an alert (alert_log + Telegram when configured).

    run_reconciliation(trade_date=None, notify=True)   post-market, after the execution cycle
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime

log = logging.getLogger("atip.execution")

OPEN = {"ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING", "SUBMITTED"}
MAP = {"FILLED": {"TRADED"}, "CANCELLED": {"CANCELLED"}, "REJECTED": {"REJECTED"},
       "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "PENDING"}, "ACKNOWLEDGED": {"PENDING"},
       "CANCEL_PENDING": {"PENDING", "CANCELLED"}, "SUBMITTED": {"PENDING"}}


def _net(rows):
    out = {}
    for sym, side, qty in rows:
        out[sym] = out.get(sym, 0) + (int(qty) if side == "BUY" else -int(qty))
    return out


def reconcile_paper(conn) -> dict:
    breaks, explained, info = [], [], []
    oms_ids = {r[0] for r in conn.execute("SELECT order_id FROM oms_order")}
    oms_net = _net(conn.execute("SELECT symbol, side, quantity FROM oms_fill WHERE mode='PAPER' AND order_id NOT IN "
                                "(SELECT order_id FROM oms_order WHERE COALESCE(instrument,'CASH')='FUT')").fetchall())
    other_net = _net([(s, t, q) for s, t, q, tag in conn.execute(
        "SELECT symbol, transaction_type, filled_qty, tag FROM paper_order WHERE status IN ('TRADED',"
        "'PARTIALLY_FILLED') AND filled_qty>0").fetchall() if tag not in oms_ids])
    book = {r[0]: int(r[1]) for r in conn.execute("SELECT symbol, quantity FROM paper_position")}
    for sym in sorted(set(oms_net) | set(book)):
        o, b, x = oms_net.get(sym, 0), book.get(sym, 0), other_net.get(sym, 0)
        if o == b:
            continue
        rec = {"kind": "position", "symbol": sym, "oms_qty": o, "broker_qty": b, "non_oms_qty": x}
        (explained if o + x == b else breaks).append(rec)
    for r in conn.execute("SELECT o.order_id, o.symbol, o.status, o.filled_quantity, o.broker_order_id, "
                          "p.status, p.filled_qty FROM oms_order o LEFT JOIN paper_order p ON "
                          "p.order_id=o.broker_order_id WHERE o.mode='PAPER' AND o.status NOT IN ('CREATED',"
                          "'VALIDATED') AND COALESCE(o.instrument,'CASH')<>'FUT'").fetchall():
        oid, sym, st, fq, boid, pst, pfq = r
        if st == "FAILED" and not boid:
            continue
        if not boid or pst is None:
            if st not in ("REJECTED", "FAILED", "CANCELLED"):
                breaks.append({"kind": "order", "order_id": oid, "symbol": sym, "oms_status": st,
                               "issue": "no broker order"})
            continue
        if pst not in MAP.get(st, {pst}):
            breaks.append({"kind": "order", "order_id": oid, "symbol": sym, "oms_status": st, "broker_status": pst,
                           "issue": "status mismatch" + (" (refresh pending?)" if st in OPEN else "")})
        if int(fq or 0) != int(pfq or 0):
            breaks.append({"kind": "fill", "order_id": oid, "symbol": sym, "oms_filled": fq, "broker_filled": pfq})
        fsum = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM oms_fill WHERE order_id=?", (oid,)).fetchone()[0]
        if int(fsum) != int(fq or 0):
            breaks.append({"kind": "fill", "order_id": oid, "symbol": sym, "oms_filled": fq, "fill_rows_qty": fsum,
                           "issue": "oms_fill rows do not add up to the order's filled quantity"})
    return {"breaks": breaks, "explained": explained, "info": info}


def reconcile_live(conn) -> dict:
    live_net = _net(conn.execute("SELECT symbol, side, quantity FROM oms_fill WHERE mode='LIVE'").fetchall())
    hold = {r[0]: int(r[1] or 0) for r in conn.execute(
        "SELECT symbol, qty FROM portfolio_holdings WHERE date=(SELECT MAX(date) FROM portfolio_holdings)")}
    info = [{"kind": "holding_not_from_oms", "symbol": s, "holding_qty": q} for s, q in sorted(hold.items())
            if s not in live_net]
    breaks = [{"kind": "position", "symbol": s, "oms_qty": q, "holding_qty": hold.get(s, 0)}
              for s, q in sorted(live_net.items()) if hold.get(s, 0) < q]
    return {"breaks": breaks, "explained": [], "info": info}


def run_reconciliation(trade_date=None, notify=True) -> dict:
    from db.schema import get_connection, log_job
    td = str(trade_date or date.today())[:10]
    conn = get_connection()
    try:
        try:
            from orders.paper import ensure_tables
            ensure_tables(conn)
        except Exception:
            pass
        books = {"PAPER": reconcile_paper(conn), "LIVE": reconcile_live(conn)}
        n_breaks = sum(len(b["breaks"]) for b in books.values())
        status = "BREAKS" if n_breaks else "OK"
        rid = "REC" + uuid.uuid4().hex[:12].upper()
        conn.execute("INSERT INTO reconciliation_run (run_id,trade_date,run_at,status,breaks,explained,details_json) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, td, datetime.now(), status, n_breaks,
                                                sum(len(b["explained"]) for b in books.values()),
                                                json.dumps(books, default=str)))
        conn.commit()
        if n_breaks and notify:
            _alert(td, books, n_breaks)
        log_job("execution_reconciliation", "SUCCESS", n_breaks)
        return {"status": "SUCCESS", "run_id": rid, "result": status, "rows": n_breaks, "books": books}
    finally:
        conn.close()


def _alert(td, books, n):
    lines = []
    for book, r in books.items():
        for b in r["breaks"][:5]:
            lines.append(f"{book} {b.get('kind')} {b.get('symbol', '')} {b.get('issue') or ''} "
                         f"{ {k: v for k, v in b.items() if k.endswith(('qty', 'status', 'filled'))} }")
    msg = f"⚠️ <b>Reconciliation</b>: {n} break(s) for {td}\n" + "\n".join(lines)
    try:
        from alerts.telegram import notify            # alert_log always; Telegram when configured
        notify(msg, category="execution", severity="warning", key=f"recon-{td}")
    except Exception as e:
        log.warning(f"  reconciliation alert not sent: {e}")


def latest(conn, limit=10) -> list:
    out = []
    for r in conn.execute("SELECT * FROM reconciliation_run ORDER BY run_at DESC LIMIT ?", (int(limit),)):
        d = dict(r)
        d["details"] = json.loads(d.pop("details_json") or "{}")
        out.append(d)
    return out
