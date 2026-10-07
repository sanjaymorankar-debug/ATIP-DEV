"""
Stock SIP (W39, EX-20 -- Zerodha / Dhan parity): a recurring buy of one stock, by amount or by
quantity, monthly or weekly.

    create(conn, body)        {symbol, amount | quantity, frequency MONTHLY | WEEKLY,
                               day (1-28 for MONTHLY, 0=Mon..4=Fri for WEEKLY), start_date?, end_date?}
    plans / get / set_status(plan_id, ACTIVE | PAUSED | ENDED)
    next_due(frequency, day, after)          the first due date on / after `after`
    run_due(on=None, place=None)             scheduler job (market days, sip_time 09:30): every ACTIVE
                                             plan whose next_due <= on

EXECUTION. PAPER only: run_due places a MARKET CNC BUY through orders.broker._place_order with
confirm only when orders.environment.broker_env() is PAPER; in SANDBOX / LIVE the due date is
recorded SKIPPED_NOT_PAPER and nothing is sent (a real-money SIP needs the owner's explicit
authorization -- tracker EX-20). By amount: quantity = floor(amount / price), price = the latest
live quote, else the last close; a due date where that buys 0 shares is SKIPPED_TOO_SMALL.

IDEMPOTENT. One sip_execution row per (plan, due date): a second run on the same day sends
nothing again. A failed placement is retried by later runs up to MAX_ATTEMPTS, then MISSED; any
final outcome advances next_due. A due date on a holiday / weekend executes on the next market
day the job runs (it is simply overdue then).
"""

from __future__ import annotations

import calendar
import math
import re
import uuid
from datetime import date, datetime, timedelta

FREQUENCIES = ("MONTHLY", "WEEKLY")
STATUSES = ("ACTIVE", "PAUSED", "ENDED")
MAX_ATTEMPTS = 3
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]{0,29}$")
FINAL = ("PLACED", "SKIPPED_TOO_SMALL", "SKIPPED_NOT_PAPER", "MISSED")


def ensure_tables(conn):
    from db.schema_w39 import W39_TABLES
    for t in ("sip_plan", "sip_execution"):
        for ddl in W39_TABLES[t]:
            conn.execute(ddl)


def _d(v):
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def next_due(frequency: str, day: int, after: date) -> date:
    """MONTHLY: day-of-month `day` (1-28) in after's month if not passed, else the next month.
    WEEKLY: the next weekday `day` (0=Mon) on / after `after`."""
    if frequency == "WEEKLY":
        return after + timedelta(days=(int(day) - after.weekday()) % 7)
    d = date(after.year, after.month, int(day))
    if d >= after:
        return d
    y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return date(y, m, min(int(day), calendar.monthrange(y, m)[1]))


def create(conn, body: dict) -> dict:
    ensure_tables(conn)
    b = body or {}
    sym = str(b.get("symbol") or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValueError("invalid symbol")
    amount, qty = b.get("amount"), b.get("quantity")
    if (amount in (None, "")) == (qty in (None, "")):
        raise ValueError("give exactly one of amount (Rs) or quantity (shares)")
    if amount not in (None, ""):
        amount = float(amount)
        if not 100 <= amount <= 10_000_000:
            raise ValueError("amount must be between Rs.100 and Rs.1,00,00,000")
        qty = None
    else:
        qty = int(qty)
        if not 1 <= qty <= 100_000:
            raise ValueError("quantity must be between 1 and 100,000")
        amount = None
    freq = str(b.get("frequency") or "MONTHLY").upper()
    if freq not in FREQUENCIES:
        raise ValueError("frequency must be MONTHLY or WEEKLY")
    day = int(b.get("day") if b.get("day") not in (None, "") else (1 if freq == "MONTHLY" else 0))
    if freq == "MONTHLY" and not 1 <= day <= 28:
        raise ValueError("day must be 1-28 for a MONTHLY SIP (every month has it)")
    if freq == "WEEKLY" and not 0 <= day <= 4:
        raise ValueError("day must be 0 (Mon) - 4 (Fri) for a WEEKLY SIP")
    start = _d(b.get("start_date") or date.today())
    end = _d(b["end_date"]) if b.get("end_date") else None
    if end and end < start:
        raise ValueError("end_date is before start_date")
    pid = f"sip_{uuid.uuid4().hex[:12]}"
    now = datetime.now()
    conn.execute("INSERT INTO sip_plan (plan_id,symbol,amount,quantity,frequency,day,start_date,end_date,status,"
                 "next_due,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, sym, amount, qty, freq, day, str(start), str(end) if end else None, "ACTIVE",
                  str(next_due(freq, day, start)), str(b.get("note") or "")[:300] or None, now, now))
    conn.commit()
    return get(conn, pid)


def get(conn, plan_id: str) -> dict:
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM sip_plan WHERE plan_id=?", (plan_id,)).fetchone()
    if not r:
        raise LookupError("SIP plan not found")
    d = dict(r)
    d["executions"] = [dict(x) for x in conn.execute(
        "SELECT due_date, attempts, last_attempt_at, quantity, price_ref, status, order_status, order_id, detail "
        "FROM sip_execution WHERE plan_id=? ORDER BY due_date DESC LIMIT 60", (plan_id,))]
    return d


def plans(conn) -> list:
    ensure_tables(conn)
    return [dict(r) for r in conn.execute("SELECT plan_id, symbol, amount, quantity, frequency, day, start_date, "
                                          "end_date, status, next_due, last_run_date FROM sip_plan ORDER BY "
                                          "status, next_due")]


def set_status(conn, plan_id: str, status: str) -> dict:
    ensure_tables(conn)
    st = str(status or "").upper()
    if st not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")
    cur = conn.execute("SELECT status FROM sip_plan WHERE plan_id=?", (plan_id,)).fetchone()
    if not cur:
        raise LookupError("SIP plan not found")
    if cur[0] == "ENDED" and st != "ENDED":
        raise ValueError("an ENDED plan cannot be restarted; create a new one")
    conn.execute("UPDATE sip_plan SET status=?, updated_at=? WHERE plan_id=?", (st, datetime.now(), plan_id))
    conn.commit()
    return get(conn, plan_id)


def _price(conn, sym, on: date):
    """(price, source): the latest live quote of the day, else the last close before / on `on`."""
    try:
        r = conn.execute("SELECT ltp FROM live_quotes WHERE symbol=? AND SUBSTR(timestamp,1,10)=? AND ltp>0 "
                         "ORDER BY timestamp DESC LIMIT 1", (sym, str(on))).fetchone()
        if r and r[0]:
            return float(r[0]), "live_quote"
    except Exception:
        pass
    r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 ORDER BY date DESC "
                     "LIMIT 1", (sym, str(on))).fetchone()
    return (float(r[0]), "last_close") if r else (None, None)


def run_due(on: date | None = None, place=None) -> dict:
    """Execute every due plan once. place(symbol, qty, tag) -> broker result (injectable for tests);
    default orders.broker._place_order(..., confirm=True) in PAPER only."""
    from db.schema import get_connection
    from orders.environment import PAPER, broker_env
    on = on or date.today()
    conn = get_connection()
    try:
        ensure_tables(conn)
        due = [dict(r) for r in conn.execute("SELECT * FROM sip_plan WHERE status='ACTIVE' AND next_due<=?",
                                             (str(on),))]
        if not due:
            return {"status": "NO_NEW", "rows": 0, "due": 0}
        env = broker_env()
        if place is None:
            from orders import broker as B

            def place(sym, qty, tag):
                return B._place_order(sym, "BUY", qty, "MARKET", "CNC", 0, confirm=True, tag=tag)
        out = []
        for p in due:
            d = _d(p["next_due"])
            if p["end_date"] and d > _d(p["end_date"]):
                conn.execute("UPDATE sip_plan SET status='ENDED', updated_at=? WHERE plan_id=?",
                             (datetime.now(), p["plan_id"]))
                out.append({"plan_id": p["plan_id"], "due": str(d), "status": "ENDED"})
                continue
            ex = conn.execute("SELECT status, attempts FROM sip_execution WHERE plan_id=? AND due_date=?",
                              (p["plan_id"], str(d))).fetchone()
            if ex and ex["status"] in FINAL:
                _advance(conn, p, d)                 # recorded earlier; only the schedule was behind
                continue
            attempts = (ex["attempts"] if ex else 0) + 1
            px, src = _price(conn, p["symbol"], on)
            qty = p["quantity"] or (math.floor(p["amount"] / px) if px else 0)
            res, status, detail, oid, ost = {}, None, None, None, None
            if env != PAPER:
                status, detail = "SKIPPED_NOT_PAPER", f"broker_env is {env}: a real-money SIP needs the owner's " \
                                                      f"explicit authorization (tracker EX-20)"
            elif not px and not p["quantity"]:
                status, detail = "FAILED", "no price to size the order"
            elif qty <= 0:
                status, detail = "SKIPPED_TOO_SMALL", f"Rs.{p['amount']:,.0f} buys 0 shares at Rs.{px:,.2f}"
            else:
                res = place(p["symbol"], int(qty), f"sip:{p['plan_id']}:{d}") or {}
                ost, oid = res.get("status"), res.get("order_id")
                status = "PLACED" if ost == "PLACED" else "FAILED"
                detail = None if status == "PLACED" else (res.get("reason") or res.get("message") or
                                                          res.get("error") or ost)
            if status == "FAILED" and attempts >= MAX_ATTEMPTS:
                status = "MISSED"
            conn.execute("INSERT INTO sip_execution (plan_id,due_date,attempts,last_attempt_at,symbol,quantity,"
                         "price_ref,status,order_status,order_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(plan_id, due_date) DO UPDATE SET attempts=excluded.attempts, "
                         "last_attempt_at=excluded.last_attempt_at, quantity=excluded.quantity, "
                         "price_ref=excluded.price_ref, status=excluded.status, order_status=excluded.order_status, "
                         "order_id=excluded.order_id, detail=excluded.detail",
                         (p["plan_id"], str(d), attempts, datetime.now(), p["symbol"], int(qty or 0), px, status, ost,
                          oid, (f"{detail} [price from {src}]" if detail and src else detail)))
            if status in FINAL:
                _advance(conn, p, d)
            out.append({"plan_id": p["plan_id"], "symbol": p["symbol"], "due": str(d), "quantity": qty,
                        "status": status, "detail": detail})
        conn.commit()
        placed = sum(1 for x in out if x["status"] == "PLACED")
        failed = [x for x in out if x["status"] in ("FAILED", "MISSED")]
        return {"status": "SUCCESS" if not failed else ("FAILED" if not placed else "PARTIAL"), "rows": placed,
                "due": len(due), "results": out,
                "error": "; ".join(f"{x['plan_id']}: {x['detail']}" for x in failed) or None}
    finally:
        conn.close()


def _advance(conn, p, d):
    nd = next_due(p["frequency"], p["day"], d + timedelta(days=1))
    st = "ENDED" if p["end_date"] and nd > _d(p["end_date"]) else p["status"]
    conn.execute("UPDATE sip_plan SET next_due=?, last_run_date=?, status=?, updated_at=? WHERE plan_id=?",
                 (str(nd), str(d), st, datetime.now(), p["plan_id"]))
