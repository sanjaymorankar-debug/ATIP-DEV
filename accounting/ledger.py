"""
The session ledger (ACC-01..06, 2 Nov 2026): double-entry style lines for every rupee a session touched.

Each transaction (txn_id) has two or more lines whose debits equal their credits. Accounts:

    Cash:S1               the session's cash                     Holdings:S1      its shares, at cost
    Owner capital         money allocated to / returned from the sessions (the unallocated pot)
    Trading gain:S1 / Trading loss:S1     gross P&L of a sale    Charges:STT ...  one account per charge type
    Tax expense (est.) / Tax payable (est.)   the provisional tax of a cycle
    Tax reserve:S1        tax set aside under AFTER_TAX_RESERVE (not reinvested)
    Adjustments           the other side of a manual adjustment

Kinds: ALLOCATION, BUY, SELL, CHARGE, TAX_PROVISION, TAX_RESERVE, TRANSFER, ADJUSTMENT. Every line carries the
session, symbol, order id, env (VIRTUAL = paper, ACTUAL = broker -- never added together) and the FY.

post_all() is idempotent (UNIQUE txn_id + line): the current pool's allocation and every stored cycle
(accounting/cycles.py session_cycles) are posted once. A cycle's charges are posted on the day it closed.
Transfers and adjustments are posted when made, and logged in session_events.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime

from accounting import cycles as C
from accounting import tax as TX

log = logging.getLogger("atip.ledger")

KINDS = ("ALLOCATION", "BUY", "SELL", "CHARGE", "TAX_PROVISION", "TAX_RESERVE", "TRANSFER", "ADJUSTMENT")
OWNER = "Owner capital"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


def ensure_tables(conn) -> None:
    C.ensure_tables(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id      TEXT NOT NULL,
            line        INTEGER NOT NULL,
            at          TEXT NOT NULL,
            fy          TEXT,
            session_id  TEXT,
            symbol      TEXT,
            order_id    TEXT,
            rule_id     TEXT,
            kind        TEXT NOT NULL,
            account     TEXT NOT NULL,
            debit       REAL NOT NULL DEFAULT 0,
            credit      REAL NOT NULL DEFAULT 0,
            charge_type TEXT,
            env         TEXT NOT NULL DEFAULT 'VIRTUAL',
            memo        TEXT DEFAULT '',
            source      TEXT,
            source_id   TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(txn_id, line)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_session ON ledger_entries(session_id, at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account, at)")
    conn.commit()


def _post(conn, txn_id: str, at: str, kind: str, lines: list, **common) -> int:
    """lines: [(account, debit, credit, extra dict)]. Refused unless debits equal credits."""
    dr = round(sum(l[1] for l in lines), 2)
    cr = round(sum(l[2] for l in lines), 2)
    if abs(dr - cr) > 0.005:
        raise ValueError(f"{txn_id}: debits ₹{dr:,.2f} and credits ₹{cr:,.2f} do not balance")
    n = 0
    for i, (account, debit, credit, extra) in enumerate(lines, 1):
        vals = {"session_id": None, "symbol": None, "order_id": None, "rule_id": None, "charge_type": None,
                "env": C.VIRTUAL, "memo": "", "source": None, "source_id": None, **common, **(extra or {})}
        cur = conn.execute(
            "INSERT OR IGNORE INTO ledger_entries (txn_id, line, at, fy, session_id, symbol, order_id, rule_id, kind, "
            "account, debit, credit, charge_type, env, memo, source, source_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (txn_id, i, at, TX.fy_of(at), vals["session_id"], vals["symbol"], vals["order_id"], vals["rule_id"], kind,
             account, round(debit, 2), round(credit, 2), vals["charge_type"], vals["env"], vals["memo"],
             vals["source"], vals["source_id"], _now()))
        n += max(0, cur.rowcount or 0)
    return n


def post_cycle(conn, c: dict) -> int:
    """A closed cycle: its buy, sale, charges (one line per type) and estimated tax (and reserve)."""
    sid, rid = c["session_id"], c["rule_id"]
    common = {"session_id": sid, "symbol": c["symbol"], "rule_id": rid, "env": c.get("env") or C.VIRTUAL,
              "source": "CYCLE", "source_id": rid}
    cash, hold = f"Cash:{sid}", f"Holdings:{sid}"
    buy, sell, gross = float(c["buy_value"]), float(c["sell_value"]), float(c["gross_pnl"])
    n = _post(conn, f"CYC:{sid}:{rid}:BUY", c["opened_at"], "BUY",
              [(hold, buy, 0, None), (cash, 0, buy, None)], memo=f"cycle {c['n']} buy {c['symbol']}", **common)
    lines = [(cash, sell, 0, None), (hold, 0, buy, None)]
    if gross >= 0:
        lines.append((f"Trading gain:{sid}", 0, gross, None))
    else:
        lines.append((f"Trading loss:{sid}", -gross, 0, None))
    n += _post(conn, f"CYC:{sid}:{rid}:SELL", c["closed_at"], "SELL", lines,
               memo=f"cycle {c['n']} sale {c['symbol']} ({c.get('exit_reason') or 'exit'})", **common)
    ch = [(k, float(c.get(f"ch_{k}") or 0)) for k in (*C.CHARGE_TYPES, "other")]
    ch = [(k, v) for k, v in ch if abs(v) >= 0.005]
    if ch:
        total = round(sum(v for _, v in ch), 2)
        n += _post(conn, f"CYC:{sid}:{rid}:CHG", c["closed_at"], "CHARGE",
                   [(f"Charges:{C.CHARGE_LABEL[k]}", v, 0, {"charge_type": k}) for k, v in ch]
                   + [(cash, 0, total, None)], memo=f"cycle {c['n']} charges", **common)
    tax = float(c.get("tax_est") or 0)
    if tax > 0:
        n += _post(conn, f"CYC:{sid}:{rid}:TAX", c["closed_at"], "TAX_PROVISION",
                   [("Tax expense (est.)", tax, 0, None), ("Tax payable (est.)", 0, tax, None)],
                   memo=f"cycle {c['n']} {c.get('tax_head') or 'STCG'} est.", **common)
        if c.get("compound_basis") == C.AFTER_TAX_RESERVE:
            n += _post(conn, f"CYC:{sid}:{rid}:RSV", c["closed_at"], "TAX_RESERVE",
                       [(f"Tax reserve:{sid}", tax, 0, None), (cash, 0, tax, None)],
                       memo=f"cycle {c['n']} tax set aside, not reinvested", **common)
    return n


def post_all(conn) -> dict:
    """Post every current pool's allocation and its stored cycles not posted yet. The ledger follows each session's
    CURRENT pool: when a pool is restarted (a new starting amount), what the old pool's Cash account held goes back
    to Owner capital and the new amount is allocated. Cycles of pools ended before the ledger existed stay in
    session_cycles (the cycle book history and the tax register) but are not posted."""
    ensure_tables(conn)
    n = 0
    for s in map(_row, conn.execute("SELECT id, start_capital, pool_started_at FROM trading_sessions "
                                    "WHERE start_capital IS NOT NULL AND start_capital > 0").fetchall()):
        sid, at = s["id"], s["pool_started_at"] or _now()
        cash = f"Cash:{sid}"
        for old in [_row(r)["source_id"] for r in conn.execute(
                "SELECT DISTINCT source_id FROM ledger_entries WHERE kind = 'ALLOCATION' AND source = 'POOL' "
                "AND session_id = ? AND source_id != ?", (sid, f"{sid}:{at}")).fetchall()]:
            if conn.execute("SELECT 1 FROM ledger_entries WHERE txn_id = ?", (f"POOLCLOSE:{old}",)).fetchone():
                continue
            bal = round(float(conn.execute("SELECT COALESCE(SUM(debit - credit), 0) FROM ledger_entries "
                                           "WHERE account = ? AND at < ?", (cash, at)).fetchone()[0]), 2)
            lines = [(OWNER, bal, 0, None), (cash, 0, bal, None)] if bal >= 0 else \
                [(cash, -bal, 0, None), (OWNER, 0, -bal, None)]
            n += _post(conn, f"POOLCLOSE:{old}", at, "ALLOCATION", lines, session_id=sid, source="POOL_CLOSE",
                       source_id=old, memo=f"pool {old} ended: its cash goes back before the new starting amount",
                       env=_env())
        amt = float(s["start_capital"])
        n += _post(conn, f"POOL:{sid}:{at}", at, "ALLOCATION", [(cash, amt, 0, None), (OWNER, 0, amt, None)],
                   session_id=sid, memo=f"starting amount for session {sid}", source="POOL",
                   source_id=f"{sid}:{at}", env=_env())
        for c in C.stored(conn, sid, since=s["pool_started_at"]):
            n += post_cycle(conn, c)
    conn.commit()
    return {"posted_lines": n}


def _env() -> str:
    try:
        from orders.environment import PAPER, broker_env
        return C.VIRTUAL if broker_env() == PAPER else C.ACTUAL
    except Exception:
        return C.VIRTUAL


def _has_open(conn, sid: str) -> bool:
    from orders import sessions as S
    s = S.get_session(conn, sid)
    return S._has_open_trade(conn, sid, s.get("pool_started_at"))


def transfer(conn, from_sid: str | None, to_sid: str | None, amount, reason: str) -> dict:
    """Move money between sessions, or to / from the unallocated pot (None). Refused while the source session holds
    an open trade, or for more than its free cash. Changes no order and restarts no books."""
    from orders import sessions as S
    ensure_tables(conn)
    from_sid = str(from_sid).upper() if from_sid else None
    to_sid = str(to_sid).upper() if to_sid else None
    reason = str(reason or "").strip()
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("Enter the amount to move in rupees.")
    if amount <= 0:
        raise ValueError("The amount must be more than ₹0.")
    if not reason:
        raise ValueError("Say why the money is moving (it goes into the ledger and the session's activity).")
    if from_sid == to_sid:
        raise ValueError("Pick two different places to move money between.")
    if not from_sid and not to_sid:
        raise ValueError("Pick at least one session.")
    for sid in (from_sid, to_sid):
        if sid and not S.get_session(conn, sid).get("start_capital"):
            raise ValueError(f"Session {sid} has no starting amount yet: give it one on the Sessions page first.")
    if from_sid:
        if _has_open(conn, from_sid):
            raise ValueError(f"Session {from_sid} holds an open trade. Money can leave it only when the trade has "
                             f"closed, so the trade is never short of cash.")
        free = S.ledger(conn, from_sid)["investable_cash"]
        if amount > free + 0.005:
            raise ValueError(f"Session {from_sid} has ₹{free:,.2f} free; ₹{amount:,.2f} is more than that.")
    at = _now()
    env = _env()
    cur = conn.execute("INSERT INTO session_transfers (at, from_sid, to_sid, amount, reason, env) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (at, from_sid, to_sid, amount, reason[:200], env))
    tid = cur.lastrowid
    _post(conn, f"TRF:{tid}", at, "TRANSFER",
          [(f"Cash:{to_sid}" if to_sid else OWNER, amount, 0, {"session_id": to_sid}),
           (f"Cash:{from_sid}" if from_sid else OWNER, 0, amount, {"session_id": from_sid})],
          memo=reason[:200], source="TRANSFER", source_id=str(tid), env=env)
    detail = {"transfer_id": tid, "from": from_sid or "unallocated", "to": to_sid or "unallocated",
              "amount": amount, "reason": reason[:200]}
    for sid in {from_sid, to_sid} - {None}:
        S._event(conn, sid, "TRANSFER", detail)
    conn.commit()
    return {**detail, "at": at, "env": env}


def adjust(conn, sid: str | None, account: str, amount, reason: str) -> dict:
    """A manual adjustment: + debits `account` (Cash:S1 by default), - credits it; the other side is Adjustments."""
    ensure_tables(conn)
    reason = str(reason or "").strip()
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("Enter the adjustment in rupees (negative to reduce).")
    if not amount:
        raise ValueError("The adjustment cannot be ₹0.")
    if not reason:
        raise ValueError("Say why you are adjusting the books.")
    account = str(account or (f"Cash:{sid}" if sid else "")).strip()
    if not account:
        raise ValueError("Pick the account to adjust.")
    at = _now()
    txn = f"ADJ:{uuid.uuid4().hex[:14]}"
    a = abs(amount)
    lines = [(account, a, 0, None), ("Adjustments", 0, a, None)] if amount > 0 else \
        [(account, 0, a, None), ("Adjustments", a, 0, None)]
    _post(conn, txn, at, "ADJUSTMENT", lines, session_id=sid, memo=reason[:200], source="ADJUSTMENT", env=_env())
    if sid:
        from orders import sessions as S
        S._event(conn, sid, "ADJUSTMENT", {"account": account, "amount": amount, "reason": reason[:200],
                                           "note": "books only -- the session's trading cash is unchanged"})
    conn.commit()
    return {"txn_id": txn, "account": account, "amount": amount, "at": at}


# -- views --------------------------------------------------------------------------------------

def entries(conn, session_id=None, account=None, kind=None, env=None, fy=None, frm=None, to=None,
            limit: int = 500) -> dict:
    """The general ledger (or one session's), newest first, with debit / credit totals."""
    ensure_tables(conn)
    q, a = "SELECT * FROM ledger_entries WHERE 1=1", []
    for col, v in (("session_id", session_id), ("account", account), ("kind", kind), ("env", env), ("fy", fy)):
        if v:
            q += f" AND {col} = ?"; a.append(v)
    if frm:
        q += " AND at >= ?"; a.append(str(frm))
    if to:
        q += " AND at <= ?"; a.append(str(to) + "T23:59:59")
    rows = [_row(r) for r in conn.execute(q + " ORDER BY at DESC, txn_id, line LIMIT ?",
                                          (*a, max(1, min(int(limit or 500), 5000)))).fetchall()]
    dr = round(sum(r["debit"] for r in rows), 2)
    cr = round(sum(r["credit"] for r in rows), 2)
    return {"rows": rows, "debit": dr, "credit": cr, "count": len(rows)}


def trial_balance(conn, env: str | None = None, fy: str | None = None) -> dict:
    ensure_tables(conn)
    q, a = "SELECT account, ROUND(SUM(debit), 2) AS debit, ROUND(SUM(credit), 2) AS credit FROM ledger_entries " \
           "WHERE 1=1", []
    if env:
        q += " AND env = ?"; a.append(env)
    if fy:
        q += " AND fy = ?"; a.append(fy)
    rows = [_row(r) for r in conn.execute(q + " GROUP BY account ORDER BY account", tuple(a)).fetchall()]
    for r in rows:
        r["balance"] = round(r["debit"] - r["credit"], 2)
    dr = round(sum(r["debit"] for r in rows), 2)
    cr = round(sum(r["credit"] for r in rows), 2)
    return {"rows": rows, "debit": dr, "credit": cr, "balanced": abs(dr - cr) < 0.01}


def session_ledger(conn, sid: str) -> dict:
    """The session's cash account line by line, oldest first, with a running balance."""
    ensure_tables(conn)
    rows = [_row(r) for r in conn.execute("SELECT * FROM ledger_entries WHERE account = ? ORDER BY at, txn_id, line",
                                          (f"Cash:{sid}",)).fetchall()]
    bal = 0.0
    for r in rows:
        bal = round(bal + r["debit"] - r["credit"], 2)
        r["balance"] = bal
    return {"session_id": sid, "rows": rows, "balance": bal}


def charges_register(conn, by: str = "type", fy: str | None = None, env: str | None = None) -> dict:
    """Charges by type, by session or by month (with the type split in each)."""
    ensure_tables(conn)
    if by not in ("type", "session", "month"):
        raise ValueError("by must be type, session or month")
    q, a = "SELECT session_id, substr(at, 1, 7) AS month, charge_type, ROUND(SUM(debit), 2) AS amount " \
           "FROM ledger_entries WHERE kind = 'CHARGE' AND charge_type IS NOT NULL", []
    if fy:
        q += " AND fy = ?"; a.append(fy)
    if env:
        q += " AND env = ?"; a.append(env)
    rows = [_row(r) for r in conn.execute(q + " GROUP BY session_id, month, charge_type", tuple(a)).fetchall()]
    key = {"type": "charge_type", "session": "session_id", "month": "month"}[by]
    out = {}
    for r in rows:
        g = out.setdefault(r[key], {by: r[key] if by != "type" else C.CHARGE_LABEL.get(r[key], r[key]),
                                    **{t: 0.0 for t in (*C.CHARGE_TYPES, "other")}, "total": 0.0})
        g[r["charge_type"]] = round(g.get(r["charge_type"], 0) + r["amount"], 2)
        g["total"] = round(g["total"] + r["amount"], 2)
    groups = sorted(out.values(), key=lambda g: str(g[by]))
    return {"by": by, "rows": groups, "total": round(sum(g["total"] for g in groups), 2),
            "labels": C.CHARGE_LABEL}


def tax_register(conn, fy: str | None = None) -> dict:
    """Closed cycles by tax head for an FY, with the FY statement (set-offs, exemption, liability)."""
    fy = fy or TX.fy_of(date.today())
    rows = C.stored(conn)
    st = TX.fy_statement(rows, fy)
    lo, hi = TX.fy_bounds(fy)
    cyc = [r for r in rows if r.get("closed_at") and lo.isoformat() <= str(r["closed_at"])[:10] <= hi.isoformat()]
    return {"fy": fy, "statement": st, "cycles": cyc,
            "fys": sorted({r.get("fy") for r in rows if r.get("fy")} | {fy}, reverse=True)}


def funds(conn) -> dict:
    """Virtual (paper) and actual (broker) money, kept strictly apart."""
    from orders import sessions as S
    from portfolio.pnl import mark_price
    out = {"VIRTUAL": {}, "ACTUAL": {}}
    v = out["VIRTUAL"]
    try:
        r = conn.execute("SELECT balance FROM paper_account WHERE id = 1").fetchone()
        v["cash"] = round(float(_row(r)["balance"]), 2) if r else None
    except Exception:
        v["cash"] = None
    cost = mkt = 0.0
    try:
        for p in map(_row, conn.execute("SELECT symbol, quantity, avg_price FROM paper_position WHERE quantity != 0")):
            cost += float(p["quantity"]) * float(p["avg_price"] or 0)
            mkt += float(p["quantity"]) * float(mark_price(conn, p["symbol"]) or p["avg_price"] or 0)
    except Exception:
        pass
    v["invested_at_cost"], v["invested_at_market"] = round(cost, 2), round(mkt, 2)
    try:
        r = conn.execute("SELECT ROUND(SUM(fill_price * filled_qty), 2) FROM paper_order WHERE transaction_type = 'BUY' "
                         "AND status IN ('FILLED', 'TRADED', 'COMPLETE')").fetchone()
        v["cumulative_deployed"] = float(r[0] or 0) if r else 0.0
    except Exception:
        v["cumulative_deployed"] = None
    pools = [x for x in S.list_sessions(conn)["sessions"] if x.get("pool")]
    v["allocated_to_pools"] = round(sum(float(x["start_capital"] or 0) for x in pools), 2)
    v["pools_cash"] = round(sum(float(x["pool"]["cash"]) for x in pools), 2)
    v["pools_equity"] = round(sum(float(x["pool"]["equity"]) for x in pools), 2)
    v["outside_pools_cash"] = round(v["cash"] - v["pools_cash"], 2) if v.get("cash") is not None else None
    a = out["ACTUAL"]
    try:
        d = conn.execute("SELECT MAX(date) FROM portfolio_holdings").fetchone()[0]
        a["holdings_date"] = d
        rows = [_row(r) for r in conn.execute("SELECT qty, avg_price, cmp, current_val FROM portfolio_holdings "
                                              "WHERE date = ?", (d,)).fetchall()] if d else []
        a["invested_at_cost"] = round(sum(float(r["qty"] or 0) * float(r["avg_price"] or 0) for r in rows), 2)
        a["invested_at_market"] = round(sum(float(r["current_val"] or 0) for r in rows), 2)
        a["holdings"] = len(rows)
    except Exception:
        a.update(holdings_date=None, invested_at_cost=None, invested_at_market=None, holdings=0)
    a["available_cash"] = None
    a["note"] = ("Broker cash is read live from Dhan on the Brokers page; ATIP does not store it. Holdings are from "
                 "the latest Dhan portfolio sync.")
    return out


def by_fy_options(conn) -> list:
    ensure_tables(conn)
    have = {_row(r)["fy"] for r in conn.execute("SELECT DISTINCT fy FROM ledger_entries WHERE fy IS NOT NULL")}
    return sorted(have | {TX.fy_of(date.today())}, reverse=True)


def to_json(obj) -> str:
    return json.dumps(obj, default=str)
