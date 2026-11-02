"""
Session cycles (SES-02..05, 2 Nov 2026): what every cycle of a cash-pool session made, gross and actual.

A CYCLE is one buy with the session's available cash plus the sale that closes it -- the "occurrence" of
orders/sessions.py. Cycle n starts with what cycle n-1 ended with:

    CASH_AFTER_CHARGES  (default)  end = start + net P&L before tax (the money actually in the account)
    AFTER_TAX_RESERVE              end = start + actual P&L: each cycle's estimated tax is set aside, not reinvested

For every closed cycle (and every total) the figures are kept apart, never blended:

    gross P&L     sell value - buy value, before any cost ("Total P&L")
    charges       brokerage, STT, exchange, SEBI fee, stamp duty, GST, DP -- itemised with the cost model the fill
                  used (backtest/costs.py); "other" is the difference to what the fill really recorded, so the
                  total always equals the charges the books paid
    net P&L       gross - charges (orders/sessions.py's pnl)
    tax (est.)    accounting/tax.py, provisional per cycle; the FY statement does the set-off
    actual P&L    net - estimated tax ("Net after charges & tax")

FLOOR RULES (per session): the minimum a cycle needs to start (trading_sessions.min_buy_value is the base floor)
    FIXED        the base floor (today's behaviour)
    RATCHET_PCT  max(base, floor_value % of the highest cycle-start amount so far): rises as the pool compounds
    STEP         base + Rs floor_value for every floor_step_every profitable cycles
Under the floor the session shows "Waiting: below minimum (Rs x of Rs y)" and orders/sessions.py gate() holds
its entry. With FIXED and CASH_AFTER_CHARGES -- the defaults -- gate() and pool_quantity() behave as before.

session_cycles holds one row per CLOSED cycle, written by sync() (the Sessions pages and API, the 15-minute job
and the reports call it) and backfilled from orders/sessions.py _trades() for history. A row is written once;
sync(force=True) recomputes rows (after a tax-rate change, for example). Transfers between sessions
(session_transfers, accounting/ledger.py transfer) move a pool's cash without restarting its books.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime

from accounting import tax as TX

log = logging.getLogger("atip.cycles")

FIXED, RATCHET_PCT, STEP = "FIXED", "RATCHET_PCT", "STEP"
FLOOR_RULES = (FIXED, RATCHET_PCT, STEP)
CASH_AFTER_CHARGES, AFTER_TAX_RESERVE = "CASH_AFTER_CHARGES", "AFTER_TAX_RESERVE"
BASES = (CASH_AFTER_CHARGES, AFTER_TAX_RESERVE)
CHARGE_TYPES = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp")
CHARGE_LABEL = {"brokerage": "Brokerage", "stt": "STT", "exchange": "Exchange txn", "sebi": "SEBI fee",
                "stamp": "Stamp duty", "gst": "GST", "dp": "DP charge", "other": "Other (fill vs model)"}
SETUP_COLUMNS = {"floor_rule": "TEXT", "floor_value": "REAL", "floor_step_every": "INTEGER", "compound_basis": "TEXT"}
VIRTUAL, ACTUAL = "VIRTUAL", "ACTUAL"
CYCLE_COLUMNS = ("session_id", "n", "rule_id", "symbol", "qty", "buy_price", "sell_price", "start_amount",
                 "buy_value", "sell_value", "gross_pnl", *(f"ch_{k}" for k in CHARGE_TYPES), "ch_other", "charges",
                 "net_pnl", "tax_est", "tax_head", "actual_pnl", "end_amount", "idle_cash", "floor_at_start",
                 "compound_basis", "opened_at", "closed_at", "holding_days", "exit_reason", "env", "pool_started_at",
                 "fy", "written_at")
_COLUMN_TYPES = {"n": "INTEGER", **{c: "TEXT" for c in ("session_id", "rule_id", "symbol", "tax_head",
                                                        "compound_basis", "opened_at", "closed_at", "exit_reason",
                                                        "env", "pool_started_at", "fy", "written_at")}}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


def ensure_tables(conn) -> None:
    from orders.sessions import ensure_tables as sessions_tables
    sessions_tables(conn)                      # trading_sessions, with the SETUP_COLUMNS (orders/sessions.py)
    cols = ", ".join(f"{c} {_COLUMN_TYPES.get(c, 'REAL')}" for c in CYCLE_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS session_cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols}, "
                 "UNIQUE(session_id, rule_id))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_cycles_sid ON session_cycles(session_id, n)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_transfers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT NOT NULL,
            from_sid    TEXT,                 -- NULL: from unallocated money
            to_sid      TEXT,                 -- NULL: back to unallocated
            amount      REAL NOT NULL,
            reason      TEXT NOT NULL,
            env         TEXT NOT NULL DEFAULT 'VIRTUAL'
        )""")
    conn.commit()


# -- the arithmetic (pure) ----------------------------------------------------------------------

def _r2(v: float) -> float:
    return round(float(v) + (1e-9 if v >= 0 else -1e-9), 2)


def itemise(model, legs) -> dict:
    """Charges by type over a cycle's legs [(side, value), ...]: each leg is one order through `model`
    (CostModel.charges), each type summed over the legs then rounded to the paisa, as a contract note shows it."""
    out = {k: 0.0 for k in CHARGE_TYPES}
    for side, value in legs:
        if not value:
            continue
        ch = model.charges(side, value)
        for k in CHARGE_TYPES:
            out[k] += float(ch.get(k) or 0)
    return {k: _r2(v) for k, v in out.items()}


def figures(start_amount: float, buy_value: float, sell_value: float, charges: dict, head: str = "STCG",
            basis: str = CASH_AFTER_CHARGES, cfg: dict | None = None, buy_charges: float = 0.0) -> dict:
    """One cycle's money, gross and actual (the 4.3 table). `charges` by type (CHARGE_TYPES, optional "other")."""
    gross = _r2(sell_value - buy_value)
    total = _r2(sum(float(charges.get(k) or 0) for k in (*CHARGE_TYPES, "other")))
    net = _r2(gross - total)
    tax = TX.cycle_tax(net, head, charges.get("stt") or 0, cfg)
    actual = _r2(net - tax)
    start = float(start_amount)
    end = _r2(start + (actual if basis == AFTER_TAX_RESERVE else net))
    return {"start_amount": _r2(start), "buy_value": _r2(buy_value), "sell_value": _r2(sell_value),
            "gross_pnl": gross, **{f"ch_{k}": _r2(charges.get(k) or 0) for k in (*CHARGE_TYPES, "other")},
            "charges": total, "net_pnl": net, "tax_est": tax, "tax_head": head, "actual_pnl": actual,
            "end_amount": end, "idle_cash": max(0.0, _r2(start - buy_value - buy_charges)),
            "gross_pct": _r2(gross / start * 100) if start else None,
            "actual_pct": _r2(actual / start * 100) if start else None, "compound_basis": basis}


def whole_shares(budget: float, price: float, model, slippage_bps: float = 0.0) -> dict:
    """The whole number of shares a cycle's cash buys at `price`, its charges paid -- the arithmetic of
    orders/sessions.py pool_quantity -- and the cash left idle this cycle."""
    px = float(price) * (1 + float(slippage_bps) / 10000)
    q = int(budget // px) if px > 0 else 0
    while q > 0 and q * px + model.total("BUY", q * px) > budget:
        q -= 1
    value = q * px
    fee = model.total("BUY", value) if q else 0.0
    return {"quantity": q, "price": px, "buy_value": _r2(value), "buy_charges": _r2(fee),
            "idle_cash": _r2(budget - value - fee)}


def floor_for(s: dict, starts: list, profitable: int) -> float:
    """The minimum the next cycle needs (see FLOOR RULES). `starts`: the start capital and every cycle-start amount
    so far; `profitable`: closed cycles with a positive net P&L."""
    base = float(s.get("min_buy_value") or 0)
    rule = str(s.get("floor_rule") or FIXED).upper()
    v = float(s.get("floor_value") or 0)
    if rule == RATCHET_PCT and v > 0 and starts:
        return _r2(max(base, max(starts) * v / 100))
    if rule == STEP and v > 0:
        return _r2(base + v * (int(profitable) // max(1, int(s.get("floor_step_every") or 1))))
    return _r2(base)


def basis_of(s: dict) -> str:
    b = str(s.get("compound_basis") or CASH_AFTER_CHARGES).upper()
    return b if b in BASES else CASH_AFTER_CHARGES


def waiting_text(cash: float, floor: float) -> str:
    return f"Waiting: below minimum (₹{cash:,.2f} of ₹{floor:,.2f})"


# -- from the order rules -----------------------------------------------------------------------

def _exit_reason(entry: dict, done: list) -> str:
    if not done:
        return ""
    last = max(done, key=lambda l: str(l.get("updated_at") or ""))
    role = str(last.get("role") or "").upper()
    if role == "STOP":
        act = last.get("trail_activate_price")
        if last.get("trail_enabled") and (not act or float(last.get("trail_high_water") or 0) >= float(act)):
            return "Trail"
        return "Stop"
    if role == "TARGET":
        return "Time exit" if last.get("not_before") and last.get("bracket_target_after_days") else "Target"
    return "Manual"


def _legs(rules: list) -> dict:
    """Per executed entry: its executed exit legs' values, product type and why it closed."""
    kids = {}
    for r in rules:
        if r.get("parent_rule_id"):
            kids.setdefault(r["parent_rule_id"], []).append(r)
    out = {}
    for e in rules:
        if (e.get("role") or "ENTRY") != "ENTRY" or e.get("status") != "EXECUTED" or not e.get("execution_price"):
            continue
        done = [l for l in kids.get(e["id"], []) if l.get("status") == "EXECUTED" and l.get("execution_price")]
        out[e["id"]] = {"sells": [float(l["execution_price"]) * float(l.get("execution_quantity") or 0) for l in done],
                        "product_type": e.get("product_type") or "CNC", "exit_reason": _exit_reason(e, done),
                        "order_ids": [x.get("dhan_order_id") for x in [e, *done] if x.get("dhan_order_id")],
                        "closed_at": max((str(l.get("updated_at") or "") for l in done), default=None)}
    return out


def _models():
    try:
        from orders.paper import paper_cost_model
        paper = paper_cost_model()
    except Exception:
        paper = None
    from backtest.costs import cost_model_from_config
    try:
        real = cost_model_from_config()
    except Exception:
        from backtest.costs import cost_model
        real = cost_model()
    return paper or real, real


def _paper_ids(conn, ids: list) -> set:
    out = set()
    try:
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            out |= {_row(r)["order_id"] for r in conn.execute(
                f"SELECT order_id FROM paper_order WHERE order_id IN ({','.join('?' * len(part))})", tuple(part))}
    except Exception:
        pass
    return out


def transfers_for(conn, sid: str, since: str | None = None) -> list:
    """This session's transfers since `since`, signed (+ in, - out): [{"at", "amount", "reason", "id"}]."""
    try:
        rows = conn.execute("SELECT * FROM session_transfers WHERE (from_sid = ? OR to_sid = ?)"
                            + (" AND at >= ?" if since else "") + " ORDER BY at, id",
                            (sid, sid, since) if since else (sid, sid)).fetchall()
    except Exception:                          # no table yet: no transfers
        return []
    out = []
    for r in map(_row, rows):
        out.append({"id": r["id"], "at": r["at"], "reason": r["reason"],
                    "amount": float(r["amount"]) * (1 if r["to_sid"] == sid else -1)})
    return out


def chain(s: dict, rules: list, trades: list, transfers: list = (), cfg: dict | None = None,
          paper_ids: set | None = None, models=None) -> dict:
    """Walk a pool session's trades in order as cycles: each cycle's start amount, floor and -- once closed -- its
    4.3 figures. Returns {"cycles": [...], "amount": what the next cycle starts with, "floor": its floor,
    "tax_reserve", "profitable", "highest_start"}."""
    cfg = cfg or TX.settings()
    basis = basis_of(s)
    start = float(s.get("start_capital") or 0)
    legs = _legs(rules)
    paper_m, real_m = models or _models()
    tf = sorted(transfers or [], key=lambda x: x["at"])
    amount, starts, profitable, reserve, ti, out = start, [start], 0, 0.0, 0, []
    for n, t in enumerate(sorted(trades, key=lambda t: t["at"]), 1):
        while ti < len(tf) and tf[ti]["at"] <= t["at"]:
            amount += tf[ti]["amount"]
            ti += 1
        floor = floor_for(s, starts, profitable)
        starts.append(amount)
        lg = legs.get(t["rule_id"], {})
        env = VIRTUAL if paper_ids is None or any(i in paper_ids for i in lg.get("order_ids", [])) \
            or not lg.get("order_ids") else ACTUAL
        c = {"session_id": s.get("id"), "n": n, "rule_id": t["rule_id"], "symbol": t["symbol"], "qty": t["qty"],
             "buy_price": t["entry"], "start_amount": _r2(amount), "buy_value": _r2(t["value"]),
             "floor_at_start": floor, "opened_at": t["at"], "closed": t["closed"], "env": env,
             "compound_basis": basis, "pool_started_at": s.get("pool_started_at"), "risk": t.get("risk"),
             "r": t.get("r")}
        if t["closed"]:
            sells = lg.get("sells") or [float(t["exit_price"] or 0) * float(t["qty"])]
            model = paper_m if env == VIRTUAL else real_m
            items = itemise(model, [("BUY", t["value"]), *(("SELL", v) for v in sells)])
            other = _r2(float(t["charges"]) - sum(items.values()))
            items["other"] = other if abs(other) >= 0.01 else 0.0
            h = TX.head(lg.get("product_type"), t["date"], t["exit_date"])
            buy_fee = model.total("BUY", t["value"])
            f = figures(amount, t["value"], sum(sells), items, h, basis, cfg, buy_charges=buy_fee)
            closed_at = lg.get("closed_at") or t["exit_date"]
            try:
                held = (date.fromisoformat(str(closed_at)[:10]) - date.fromisoformat(str(t["at"])[:10])).days
            except ValueError:
                held = None
            c.update(f, sell_price=t["exit_price"], closed_at=closed_at, holding_days=held,
                     exit_reason=lg.get("exit_reason") or "", fy=TX.fy_of(closed_at))
            amount = f["end_amount"]
            if f["net_pnl"] > 0:
                profitable += 1
            if basis == AFTER_TAX_RESERVE:
                reserve += f["tax_est"]
        out.append(c)
    while ti < len(tf):                         # transfers after the last trade
        amount += tf[ti]["amount"]
        ti += 1
    return {"cycles": out, "amount": _r2(amount), "floor": floor_for(s, starts, profitable),
            "tax_reserve": _r2(reserve), "profitable": profitable, "highest_start": _r2(max(starts)),
            "basis": basis}


def pool_extras(s: dict, rules: list, trades: list, transfers: list = ()) -> dict:
    """What orders/sessions.py ledger() adds for gate() and pool_quantity(): the next cycle's floor, the tax set
    aside under AFTER_TAX_RESERVE (0 otherwise) and the rule names."""
    ch = chain(s, rules, trades, transfers)
    return {"floor": ch["floor"], "floor_rule": str(s.get("floor_rule") or FIXED).upper(),
            "compound_basis": ch["basis"], "tax_reserve": ch["tax_reserve"], "profitable_cycles": ch["profitable"],
            "highest_start": ch["highest_start"]}


# -- stored cycles ------------------------------------------------------------------------------

def sync(conn, sid: str | None = None, force: bool = False) -> dict:
    """Write every closed cycle of every pool session (or `sid`) not stored yet -- backfill included -- and post
    it to the ledger. force=True recomputes the stored rows of the current pools. Returns rows written."""
    from orders import sessions as S
    ensure_tables(conn)
    cfg = TX.settings()
    models = _models()
    written = 0
    sids = [sid] if sid else [_row(r)["id"] for r in conn.execute(
        "SELECT id FROM trading_sessions WHERE start_capital IS NOT NULL AND start_capital > 0").fetchall()]
    for i in sids:
        s = S.get_session(conn, i)
        if not s.get("start_capital"):
            continue
        rules = S._linked_rules(conn, i)
        tr = S._trades(rules, S._charges(conn, rules), since=s.get("pool_started_at"))
        ids = [x for lg in _legs(rules).values() for x in lg["order_ids"]]
        ch = chain(s, rules, tr, transfers_for(conn, i, s.get("pool_started_at")), cfg, _paper_ids(conn, ids), models)
        for c in ch["cycles"]:
            if not c["closed"]:
                continue
            vals = {k: c.get(k) for k in CYCLE_COLUMNS}
            vals["written_at"] = _now()
            verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
            cur = conn.execute(f"{verb} INTO session_cycles ({', '.join(CYCLE_COLUMNS)}) VALUES "
                               f"({', '.join('?' * len(CYCLE_COLUMNS))})", tuple(vals[k] for k in CYCLE_COLUMNS))
            written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    try:
        from accounting import ledger as L
        L.post_all(conn)
    except Exception as e:
        log.warning(f"  ledger posting after cycle sync: {e}")
    return {"written": written, "sessions": len(sids)}


def stored(conn, sid: str | None = None, since: str | None = None) -> list:
    ensure_tables(conn)
    q, a = "SELECT * FROM session_cycles WHERE 1=1", []
    if sid:
        q += " AND session_id = ?"; a.append(sid)
    if since:
        q += " AND pool_started_at = ?"; a.append(since)
    rows = [_row(r) for r in conn.execute(q + " ORDER BY session_id, n", tuple(a)).fetchall()]
    return rows


def _sum(rows: list, k: str) -> float:
    return _r2(sum(float(r.get(k) or 0) for r in rows))


def totals(rows: list) -> dict:
    """Sums of the 4.3 money columns over closed cycles."""
    out = {k: _sum(rows, k) for k in ("buy_value", "sell_value", "gross_pnl", "charges", "net_pnl", "tax_est",
                                      "actual_pnl")}
    out["charges_by_type"] = {k: _sum(rows, f"ch_{k}") for k in (*CHARGE_TYPES, "other")}
    out["cycles"] = len(rows)
    out["wins"] = sum(1 for r in rows if float(r.get("net_pnl") or 0) > 0)
    return out


def _max_drawdown(points: list) -> float | None:
    peak, worst = None, 0.0
    for v in points:
        peak = v if peak is None else max(peak, v)
        if peak:
            worst = min(worst, (v - peak) / peak * 100)
    return _r2(worst) if points else None


def _cagr(start: float, end: float, since: str | None) -> float | None:
    try:
        days = (date.today() - date.fromisoformat(str(since)[:10])).days
    except (TypeError, ValueError):
        return None
    if days < 30 or start <= 0 or end <= 0:
        return None                            # annualising under a month says nothing
    return _r2((math.pow(end / start, 365.25 / days) - 1) * 100)


def book(conn, sid: str, do_sync: bool = True) -> dict:
    """One session's book: setup, the 4.3 table per cycle (closed from session_cycles, the open one live), the
    summary tiles, the chart series and the activity timeline."""
    from orders import sessions as S
    if do_sync:
        sync(conn, sid)
    s = S.get_session(conn, sid)
    L = S.ledger(conn, sid, s=s)
    if not L:
        return {"session": s, "pool": None, "cycles": [], "open": [], "tiles": None, "events": S.events(conn, sid, 30)}
    rows = stored(conn, sid, s.get("pool_started_at"))
    t = totals(rows)
    start = L["start_capital"]
    eq_g, eq_a, eq_n, series = start, start, start, [{"n": 0, "date": str(L["since"] or "")[:10], "gross": start,
                                                     "actual": start, "net": start}]
    for r in rows:
        eq_g += float(r["gross_pnl"] or 0)
        eq_a += float(r["actual_pnl"] or 0)
        eq_n += float(r["net_pnl"] or 0)
        series.append({"n": r["n"], "date": str(r["closed_at"] or "")[:10], "gross": _r2(eq_g),
                       "actual": _r2(eq_a), "net": _r2(eq_n)})
    rs = [float(r["r"]) for r in S._trades(S._linked_rules(conn, sid), since=s.get("pool_started_at"))
          if r.get("closed") and r.get("r") is not None]
    best = max(rows, key=lambda r: float(r["actual_pnl"] or 0), default=None)
    worst = min(rows, key=lambda r: float(r["actual_pnl"] or 0), default=None)
    open_cycles = []
    for h in L["open"]:
        open_cycles.append({"symbol": h["symbol"], "qty": h["qty"], "buy_price": h["entry"], "mark": h["mark"],
                            "buy_value": _r2(h["cost"]), "value": h["value"], "unrealised": h["unrealised"],
                            "since": h["since"], "stop": h["stop"], "trailing": h["trailing"],
                            "sell_at": h["sell_at"], "progress": _progress(h, s)})
    tiles = {"start_capital": start, "equity": L["equity"], "cash": L["cash"],
             "investable_cash": L.get("investable_cash", L["cash"]), "tax_reserve": L.get("tax_reserve", 0.0),
             "gross_pnl": t["gross_pnl"], "charges": t["charges"], "tax_est": t["tax_est"],
             "net_pnl": t["net_pnl"], "actual_pnl": t["actual_pnl"],
             "abs_return_gross_pct": _r2(t["gross_pnl"] / start * 100) if start else None,
             "abs_return_actual_pct": _r2(t["actual_pnl"] / start * 100) if start else None,
             "cagr_gross_pct": _cagr(start, start + t["gross_pnl"], L["since"]),
             "cagr_actual_pct": _cagr(start, start + t["actual_pnl"], L["since"]),
             "cycles_done": t["cycles"], "max_cycles": L["max_occurrences"],
             "win_rate_pct": _r2(t["wins"] / t["cycles"] * 100) if t["cycles"] else None,
             "avg_r": _r2(sum(rs) / len(rs)) if rs else None,
             "max_drawdown_pct": _max_drawdown([p["actual"] for p in series]),
             "best_cycle": best and {"n": best["n"], "symbol": best["symbol"], "actual_pnl": best["actual_pnl"]},
             "worst_cycle": worst and {"n": worst["n"], "symbol": worst["symbol"], "actual_pnl": worst["actual_pnl"]},
             "floor": L.get("floor", s.get("min_buy_value")), "floor_rule": L.get("floor_rule", FIXED),
             "compound_basis": L.get("compound_basis", CASH_AFTER_CHARGES), "waiting": L.get("waiting"),
             "unrealised": L["unrealised"]}
    waterfall = [{"label": "Gross P&L", "value": t["gross_pnl"], "kind": "total"}]
    waterfall += [{"label": CHARGE_LABEL[k], "value": -v, "kind": "charge"}
                  for k, v in t["charges_by_type"].items() if v]
    waterfall += [{"label": "Tax (est.)", "value": -t["tax_est"], "kind": "tax"},
                  {"label": "Actual P&L", "value": t["actual_pnl"], "kind": "total"}]
    return {"session": s, "pool": L, "cycles": rows, "open": open_cycles, "totals": t, "tiles": tiles,
            "charts": {"equity": series, "waterfall": waterfall,
                       "staircase": [{"n": r["n"], "start": r["start_amount"], "floor": r["floor_at_start"]}
                                     for r in rows]},
            "events": S.events(conn, sid, 30), "tax_disclaimer": TX.DISCLAIMER}


def _progress(h: dict, s: dict) -> dict:
    """Where an open cycle sits between its stop and its target, for the progress bar (0-100)."""
    entry, mark = float(h["entry"] or 0), float(h["mark"] or 0)
    tgt = float(s.get("target_pct") or 3.0)
    stop = float(s.get("stop_pct") or 2.0)
    if not entry:
        return {"pct": None}
    move = (mark - entry) / entry * 100
    pos = (move + stop) / (tgt + stop) * 100
    return {"move_pct": _r2(move), "pct": max(0.0, min(100.0, _r2(pos))), "target_pct": tgt, "stop_pct": -stop}


def overview(conn, do_sync: bool = True) -> dict:
    """Every session with its tiles, and the all-sessions totals (gross and actual never blended)."""
    from orders import sessions as S
    if do_sync:
        try:
            sync(conn)
        except Exception as e:
            log.warning(f"  cycle sync: {e}")
    out = []
    for s in S.list_sessions(conn)["sessions"]:
        b = book(conn, s["id"], do_sync=False) if s.get("start_capital") else None
        out.append({"id": s["id"], "name": s["name"], "strategy": s.get("strategy"), "mode": s["mode"],
                    "status": s["status"], "auto_pick": bool(s.get("auto_pick")), "universe": s.get("universe"),
                    "tiles": b and b["tiles"], "open": b and b["open"], "exit_rule": S.exit_rule(s),
                    "spark": b and [p["actual"] for p in b["charts"]["equity"]][-30:],
                    "setup": {k: s.get(k) for k in ("start_capital", "min_buy_value", "floor_rule", "floor_value",
                                                    "floor_step_every", "compound_basis", "target_pct", "stop_pct",
                                                    "trail_pct", "hold_days", "max_occurrences", "pool_started_at")}})
    pools = [x["tiles"] for x in out if x["tiles"]]
    keys = ("start_capital", "equity", "cash", "gross_pnl", "charges", "tax_est", "net_pnl", "actual_pnl", "unrealised")
    total = {k: _r2(sum(float(p.get(k) or 0) for p in pools)) for k in keys}
    total["cycles_done"] = sum(p["cycles_done"] for p in pools)
    total["sessions_with_pool"] = len(pools)
    total["waiting"] = sum(1 for p in pools if p.get("waiting"))
    return {"sessions": out, "total": total, "tax_disclaimer": TX.DISCLAIMER, "as_of": _now()}


def split(total: float, ids: list) -> dict:
    """The "split a total across N sessions" helper: equal shares to the paisa, the remainder on the first. Saves
    nothing -- the Sessions page pre-fills the cards with it and the owner edits each one."""
    try:
        total = float(total)
    except (TypeError, ValueError):
        raise ValueError("Enter the total amount in rupees.")
    if total <= 0:
        raise ValueError("The total must be more than ₹0.")
    ids = [str(i).upper() for i in ids or []]
    if not ids:
        raise ValueError("Pick at least one session to split the money across.")
    each = math.floor(total / len(ids) * 100) / 100
    out = {i: each for i in ids}
    out[ids[0]] = _r2(each + (total - each * len(ids)))
    return {"total": _r2(total), "amounts": out}
