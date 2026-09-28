"""
Emergency exit (W25, RK-16): flatten a book with one command, behind a confirmation.

    plan(conn, book)                     what a flatten would do; returns a confirm code
    execute(conn, book, confirm, ...)    do it -- only with the code from a plan made in
                                         the last 10 minutes for the same positions

execute(), in order:
  1. turns the kill switch ON (orders/risk.py halt): no new order of any kind while the
     book is being unwound and after it. Resuming is a separate, deliberate act
     (python -m orders.risk --resume).
  2. cancels every open W4 order (oms_order CREATED / VALIDATED / ACKNOWLEDGED /
     PARTIALLY_FILLED) through the order manager.
  3. PAPER book: sells every position through the paper broker (a MARKET sell at the
     live quote; outside market hours, or with price="last_close", at the last close).
     A position that fails to sell is reported, not retried silently.
     LIVE book: NO ORDER IS SENT. ATIP never places live orders from here: the result
     is the exact sell list (symbol, quantity, last price) to execute at the broker.
     The kill switch and the cancellations still apply.
  4. records the run in risk_emergency_exit and raises a risk alert.

The confirm code is a hash of the book and its positions, so a plan made before the
book changed cannot be executed by mistake.

    python -m portfolio.emergency plan  --book PAPER
    python -m portfolio.emergency flatten --book PAPER --confirm <code> [--last-close]
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("atip.portfolio")

OPEN_STATES = ("CREATED", "VALIDATED", "ACKNOWLEDGED", "PARTIALLY_FILLED")
PLAN_TTL = timedelta(minutes=10)


def _positions(conn, book):
    from portfolio.pnl import positions
    return [p for p in positions(conn, book) if (p.get("qty") or 0) > 0]


def _code(book, pos, minute: str) -> str:
    key = json.dumps([book, minute, sorted((p["symbol"], int(p["qty"])) for p in pos)])
    return hashlib.sha256(key.encode()).hexdigest()[:8].upper()


def _open_orders(conn):
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT order_id, symbol, side, quantity, status FROM oms_order WHERE status IN "
            f"({','.join('?' * len(OPEN_STATES))})", OPEN_STATES)]
    except Exception:
        return []


def plan(conn, book: str = "PAPER") -> dict:
    book = book.upper()
    if book not in ("PAPER", "LIVE"):
        raise ValueError("book must be PAPER or LIVE")
    pos = _positions(conn, book)
    minute = datetime.now().strftime("%Y%m%d%H%M")
    from orders.risk import halted
    h, why = halted()
    return {"book": book, "positions": [{"symbol": p["symbol"], "qty": p["qty"], "mark": p["mark"],
                                          "value": p["value"]} for p in pos],
            "value": round(sum(p["value"] or 0 for p in pos), 2), "open_orders": _open_orders(conn),
            "kill_switch_on": h, "kill_switch_reason": why,
            "confirm": _code(book, pos, minute) if pos or _open_orders(conn) else None,
            "valid_until": (datetime.now() + PLAN_TTL).isoformat(timespec="seconds"),
            "will": ["turn the kill switch ON", "cancel the open W4 orders listed",
                     "sell every PAPER position" if book == "PAPER" else
                     "NOT send any order: LIVE positions are listed for you to sell at the broker"]}


def _valid_code(book, pos, confirm) -> bool:
    now = datetime.now()
    for k in range(int(PLAN_TTL.total_seconds() // 60) + 1):
        if _code(book, pos, (now - timedelta(minutes=k)).strftime("%Y%m%d%H%M")) == str(confirm or "").upper():
            return True
    return False


def execute(conn, book: str, confirm: str, actor: str = "owner", reason: str = "", price: str = "live") -> dict:
    book = book.upper()
    pos = _positions(conn, book)
    if not _valid_code(book, pos, confirm):
        raise ValueError("confirm code does not match a plan for the current positions made in the last "
                         "10 minutes -- make a new plan")
    from orders.risk import halt, risk_alert
    started = datetime.now()
    halt(f"EMERGENCY EXIT ({book}) by {actor}: {reason or 'no reason given'}")
    cancelled, cancel_errors = [], []
    from execution import order_manager as OM
    for o in _open_orders(conn):
        try:
            OM.cancel_order(conn, o["order_id"], reason="emergency exit")
            cancelled.append(o["order_id"])
        except Exception as e:
            cancel_errors.append({"order_id": o["order_id"], "error": str(e)})
    sold, failed, manual = [], [], []
    if book == "PAPER" and pos:
        from orders.paper import PaperBroker
        br = PaperBroker(conn=conn)
        for p in pos:
            ref = p["mark"] if price == "last_close" else None
            try:
                r = br.place_order(transaction_type="SELL", quantity=int(p["qty"]), symbol=p["symbol"],
                                   order_type="MARKET", tag="EMERGENCY_EXIT", reference_price=ref)
                d = r.get("data") or {}
                if r.get("status") == "success" and d.get("filledQty"):
                    sold.append({"symbol": p["symbol"], "qty": d["filledQty"], "of": p["qty"],
                                 "price": d.get("averageTradedPrice"), "order_id": d.get("orderId")})
                    if d["filledQty"] < p["qty"]:
                        failed.append({"symbol": p["symbol"], "error": f"partial fill {d['filledQty']}/{p['qty']}"})
                else:
                    failed.append({"symbol": p["symbol"], "error": r.get("remarks") or d.get("reason") or str(r)[:200]})
            except Exception as e:
                failed.append({"symbol": p["symbol"], "error": str(e)})
    elif book == "LIVE":
        manual = [{"symbol": p["symbol"], "qty": p["qty"], "last_price": p["mark"]} for p in pos]
    status = ("MANUAL_ACTION_REQUIRED" if manual else "FAILED" if failed and not sold else
              "PARTIAL" if failed else "DONE")
    out = {"run_id": "EX" + started.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4], "book": book, "status": status,
           "kill_switch": "ON", "cancelled_orders": cancelled, "cancel_errors": cancel_errors, "sold": sold,
           "failed": failed, "manual_sells": manual, "started_at": started.isoformat(timespec="seconds"),
           "note": ("LIVE: no order was sent; sell the listed positions at the broker" if manual else
                    "trading stays halted until python -m orders.risk --resume")}
    conn.execute("INSERT INTO risk_emergency_exit (run_id, book, actor, reason, status, result_json, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (out["run_id"], book, actor, reason, status, json.dumps(out), started))
    conn.commit()
    risk_alert(f"Emergency exit {book}: {status}",
               f"kill switch ON; {len(cancelled)} order(s) cancelled; {len(sold)} sold; {len(failed)} failed; "
               f"{len(manual)} LIVE position(s) to sell manually", key=f"emx-{out['run_id']}")
    return out


def history(conn, limit: int = 20) -> list:
    return [{**json.loads(r[0]), "actor": r[1], "reason": r[2]} for r in conn.execute(
        "SELECT result_json, actor, reason FROM risk_emergency_exit ORDER BY created_at DESC LIMIT ?", (int(limit),))]


def _cli():
    import argparse
    from db.schema import get_connection
    ap = argparse.ArgumentParser(prog="python -m portfolio.emergency")
    ap.add_argument("cmd", choices=["plan", "flatten", "history"])
    ap.add_argument("--book", default="PAPER")
    ap.add_argument("--confirm")
    ap.add_argument("--reason", default="")
    ap.add_argument("--last-close", action="store_true", help="PAPER: fill at the last close, not a live quote")
    a = ap.parse_args()
    conn = get_connection()
    try:
        if a.cmd == "plan":
            print(json.dumps(plan(conn, a.book), indent=2, default=str))
        elif a.cmd == "history":
            print(json.dumps(history(conn), indent=2, default=str))
        else:
            print(json.dumps(execute(conn, a.book, a.confirm, "cli", a.reason,
                                     "last_close" if a.last_close else "live"), indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    _cli()
