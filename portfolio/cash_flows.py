"""
External cash flows of a book (owner item 8, 16 Nov 2026): deposits, withdrawals and transfers, kept apart from
trading P&L.

Why. portfolio/pnl.py measured day P&L as "equity today - equity at the last pnl_daily row" and the drawdown against
the highest stored equity. Nothing told money the owner moved from money the market moved, so a Rs 50,000 withdrawal
read as a Rs 50,000 day loss and tripped orders/risk.py's max_daily_loss_value (Rs 10,000) and, from 10 %, its
max_drawdown_pct. (The 41.8 % LIVE drawdown of 13 Nov was not a withdrawal: it is the PA-01 paper cash, still written
because the Mac was not restarted after the 5 Nov fix -- .ux_build/kite-fix-2026-11-16/accounting/
LIVE_CASHFLOW_FINDINGS.md.)

The record: portfolio_cash_flows, one row per flow --
    env       PAPER or LIVE (the pnl_daily book it belongs to)
    date      the IST trading day the money moved (YYYY-MM-DD); it belongs to the window (previous pnl_daily row, date]
    kind      DEPOSIT, WITHDRAWAL, TRANSFER_IN, TRANSFER_OUT (sign fixed by the kind) or ADJUSTMENT (signed as entered,
              e.g. a basis correction)
    amount    signed rupees: + into the book, - out of it
    status    CONFIRMED (counted), PENDING (detected, not counted until confirmed), VOID (kept, never counted)
    source    manual | detected | api ; ref = the dedupe key of a detected flow ("LIVE:2026-11-17")
Rows are never deleted: a wrong entry is voided with a reason.

How P&L uses it (portfolio/pnl.py):
    day P&L       = equity - previous equity - net CONFIRMED flows in (previous row, today]      (rupees, exact)
    peak          a flow scales the running peak by (equity before + flow) / equity before, so the drawdown in %
                  is the same just after a deposit or withdrawal as just before it (time-weighted: a withdrawal is
                  never a loss, a deposit never hides one)
    drawdown_pct  = (peak - equity) / peak, from the stored equity series and the flows (recomputed on read, so a flow
                  entered later corrects the view; no stored history is rewritten)
A book with no confirmed flow keeps the exact pre-change arithmetic (MAX(peak_equity)) -- PAPER today.

Detection (LIVE only; PAPER cash moves only with paper fills): at the 16:49 IST pnl_daily job, a cash change of at
least detect_min_value that trades cannot explain -- the LIVE holdings (symbol -> qty) are the same at both rows and
no LIVE order was placed in between -- is stored as a PENDING flow. It is NOT counted until the owner confirms it:
a manual intraday or F&O loss at Dhan outside ATIP also moves cash with the holdings unchanged, and hiding a loss as a
"withdrawal" would switch the loss limits off. config.json can opt in:

    "cash_flows": {"detect_min_value": 1000, "auto_confirm_detected": false}

Manual path (also POST /api/v2/cash-flows on the dashboard):

    python -m portfolio.cash_flows add --env LIVE --kind WITHDRAWAL --amount 50000 --date 2026-11-17 --note "to bank"
    python -m portfolio.cash_flows list [--env LIVE] [--status PENDING]
    python -m portfolio.cash_flows confirm ID [--kind WITHDRAWAL]       a detected flow, after checking the bank
    python -m portfolio.cash_flows void ID --reason "..."
    python -m portfolio.cash_flows detect --env LIVE [--from 2026-09-25]   report only: unexplained cash changes in
                                                                          the stored rows (nothing written)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import date, datetime

log = logging.getLogger(__name__)

TABLE = "portfolio_cash_flows"
KINDS = {"DEPOSIT": 1, "WITHDRAWAL": -1, "TRANSFER_IN": 1, "TRANSFER_OUT": -1, "ADJUSTMENT": 0}
CONFIRMED, PENDING, VOID = "CONFIRMED", "PENDING", "VOID"
STATUSES = (CONFIRMED, PENDING, VOID)
ENVS = ("PAPER", "LIVE")
DEFAULTS = {"detect_min_value": 1000.0, "auto_confirm_detected": False}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _day(v) -> str | None:
    """'YYYY-MM-DD' of a date, datetime or ISO text; None for nothing."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return date.fromisoformat(str(v)[:10]).isoformat()


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


def _exists(conn, name=TABLE) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    except Exception:
        return False


def ensure_table(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            env         TEXT NOT NULL,
            date        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            amount      REAL NOT NULL,
            status      TEXT NOT NULL DEFAULT 'CONFIRMED',
            source      TEXT NOT NULL DEFAULT 'manual',
            ref         TEXT,
            note        TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_env_date ON {TABLE}(env, date)")


def settings(cfg: dict | None = None) -> dict:
    """config.json "cash_flows" over DEFAULTS."""
    if cfg is None:
        try:
            from orders.environment import CONFIG_PATH
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    raw = cfg.get("cash_flows") or {}
    out = dict(DEFAULTS)
    try:
        v = float(raw.get("detect_min_value", DEFAULTS["detect_min_value"]))
        out["detect_min_value"] = v if math.isfinite(v) and v > 0 else DEFAULTS["detect_min_value"]
    except (TypeError, ValueError):
        pass
    out["auto_confirm_detected"] = raw.get("auto_confirm_detected") is True
    return out


def signed(kind: str, amount) -> float:
    """The signed amount of a flow: DEPOSIT / TRANSFER_IN +, WITHDRAWAL / TRANSFER_OUT -, ADJUSTMENT as entered."""
    kind = str(kind or "").strip().upper()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    try:
        a = float(str(amount).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        raise ValueError("amount must be a number of rupees")
    if not math.isfinite(a) or a == 0:
        raise ValueError("amount must be a non-zero number of rupees")
    return round(abs(a) * KINDS[kind] if KINDS[kind] else a, 2)


def record(conn, env: str, kind: str, amount, on=None, note: str = "", source: str = "manual",
           status: str = CONFIRMED, ref: str | None = None) -> dict:
    """Store one flow; returns the row. A detected flow with a `ref` already stored is not stored twice (the stored
    row comes back with "_existing": True)."""
    env = str(env or "").strip().upper()
    if env not in ENVS:
        raise ValueError("env must be PAPER or LIVE")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    amt = signed(kind, amount)
    day = _day(on) or date.today().isoformat()
    ensure_table(conn)
    if ref:
        r = conn.execute(f"SELECT * FROM {TABLE} WHERE ref = ?", (ref,)).fetchone()
        if r:
            return {**_row(r), "_existing": True}
    conn.execute(f"INSERT INTO {TABLE} (env, date, kind, amount, status, source, ref, note, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (env, day, str(kind).upper(), amt, status, source, ref,
                                                str(note or "")[:500], _now()))
    conn.commit()
    r = conn.execute(f"SELECT * FROM {TABLE} WHERE env = ? AND date = ? ORDER BY id DESC LIMIT 1", (env, day)).fetchone()
    out = _row(r)
    log.info(f"  cash flow {env} {day}: {out['kind']} {amt:+,.2f} ({status}, {source})")
    return out


def set_status(conn, flow_id: int, status: str, note: str = "", kind: str | None = None) -> dict:
    """Confirm or void a flow (a PENDING detected one, or a wrong entry). `kind` may re-name a confirmed detected flow
    (a detected cash fall is stored as WITHDRAWAL; the owner may confirm it as TRANSFER_OUT); the amount keeps its sign."""
    if status not in (CONFIRMED, VOID):
        raise ValueError("status must be CONFIRMED or VOID")
    ensure_table(conn)
    r = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (int(flow_id),)).fetchone()
    if not r:
        raise ValueError(f"no cash flow {flow_id}")
    r = _row(r)
    new_kind = str(kind or r["kind"]).upper()
    if new_kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if KINDS[new_kind] and KINDS[new_kind] * float(r["amount"]) < 0:
        raise ValueError(f"{new_kind} does not match the flow's direction ({float(r['amount']):+,.2f})")
    text = (str(r.get("note") or "") + (f" | {status.lower()}: {note}" if note else "")).strip(" |")[:500]
    conn.execute(f"UPDATE {TABLE} SET status = ?, kind = ?, note = ?, updated_at = ? WHERE id = ?",
                 (status, new_kind, text, _now(), int(flow_id)))
    conn.commit()
    return _row(conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (int(flow_id),)).fetchone())


def listing(conn, env: str | None = None, status: str | None = None, limit: int = 200) -> list:
    if not _exists(conn):
        return []
    q, a = f"SELECT * FROM {TABLE} WHERE 1=1", []
    if env:
        q += " AND env = ?"; a.append(str(env).upper())
    if status:
        q += " AND status = ?"; a.append(str(status).upper())
    return [_row(r) for r in conn.execute(q + " ORDER BY date DESC, id DESC LIMIT ?", (*a, int(limit))).fetchall()]


def confirmed(conn, env: str, through=None) -> list:
    """[(date, amount)] of the CONFIRMED flows of `env` dated on or before `through` (default: all), oldest first.
    No table yet = no flows (read-only: nothing is created)."""
    if not _exists(conn):
        return []
    q, a = f"SELECT date, amount FROM {TABLE} WHERE env = ? AND status = ?", [str(env).upper(), CONFIRMED]
    if through is not None:
        q += " AND date <= ?"; a.append(_day(through))
    return [(str(_row(r)["date"])[:10], float(_row(r)["amount"] or 0))
            for r in conn.execute(q + " ORDER BY date, id", tuple(a)).fetchall()]


def net_flow(conn, env: str, after=None, through=None) -> float:
    """Net CONFIRMED flow of `env` dated in (after, through]; after None = from the beginning."""
    lo, hi = _day(after), _day(through)
    return round(sum(a for d, a in confirmed(conn, env, hi) if lo is None or d > lo), 2)


def pending(conn, env: str, after=None, through=None) -> list:
    """PENDING (detected, unconfirmed) flows of `env` in (after, through]."""
    lo, hi = _day(after), _day(through)
    return [r for r in listing(conn, env, PENDING)
            if (lo is None or str(r["date"])[:10] > lo) and (hi is None or str(r["date"])[:10] <= hi)]


# -- the arithmetic of a flow-adjusted peak (pure) --------------------------------------------------------------

def scale_peak(peak, equity_before, flow):
    """The running peak after an external `flow`, so that the drawdown in % is unchanged by it:
    peak x (equity_before + flow) / equity_before. Without a usable equity before the flow, the peak moves by the
    flow in rupees; a book emptied by a withdrawal starts a new peak (None)."""
    if peak is None or not flow:
        return peak
    if equity_before is not None and equity_before > 0:
        after = equity_before + flow
        return round(peak * after / equity_before, 2) if after > 0 else None
    p = peak + flow
    return round(p, 2) if p > 0 else None


def adjusted_series(rows: list, flows: list, equity_now=None, on=None) -> dict:
    """The flow-adjusted equity series of one book. `rows` are its pnl_daily rows (date, equity), oldest first, dated
    before `on`; `flows` [(date, amount)] its confirmed flows; `equity_now` today's equity (on `on`), if measured.
    A flow dated d belongs to the first row dated on or after d. Returns
    {"series": [{date, equity, flow, day_pnl, peak, drawdown_pct}], "peak", "drawdown_pct", "flow_today",
     "last_equity", "last_date"} -- peak / drawdown_pct as of today when equity_now is given, else the last row."""
    fl = sorted((str(d)[:10], float(a)) for d, a in flows)
    i, carry, peak, last_eq, last_date, series = 0, 0.0, None, None, None, []
    for r in rows:
        d = str(r["date"])[:10]
        while i < len(fl) and fl[i][0] <= d:
            carry += fl[i][1]; i += 1
        eq = r.get("equity")
        if eq is None:
            continue                                   # cash unknown that day: the flow waits for the next equity
        eq = float(eq)
        f = round(carry, 2)
        carry = 0.0
        peak = scale_peak(peak, last_eq, f)
        peak = eq if peak is None else max(peak, eq)
        series.append({"date": d, "equity": round(eq, 2), "flow": f,
                       "day_pnl": round(eq - last_eq - f, 2) if last_eq is not None else None,
                       "peak": round(peak, 2), "drawdown_pct": round((peak - eq) / peak * 100, 2) if peak > 0 else None})
        last_eq, last_date = eq, d
    hi = _day(on)
    while i < len(fl) and (hi is None or fl[i][0] <= hi):
        carry += fl[i][1]; i += 1
    out = {"series": series, "flow_today": round(carry, 2), "last_equity": last_eq, "last_date": last_date,
           "peak": peak, "drawdown_pct": series[-1]["drawdown_pct"] if series else None}
    if equity_now is not None:
        p = scale_peak(peak, last_eq, round(carry, 2))
        p = float(equity_now) if p is None else max(p, float(equity_now))
        out["peak"] = round(p, 2)
        out["drawdown_pct"] = round((p - equity_now) / p * 100, 2) if p > 0 else None
    return out


# -- detection: a cash change trades cannot explain (LIVE) ------------------------------------------------------

def _holdings(conn, on) -> dict:
    """symbol -> qty of the LIVE book on `on` (the portfolio_holdings sync portfolio/pnl.py values)."""
    from portfolio.pnl import live_book_date
    d = live_book_date(conn, date.fromisoformat(_day(on)))
    if not d:
        return {}
    return {str(_row(r)["symbol"]): int(_row(r)["qty"] or 0) for r in conn.execute(
        "SELECT symbol, qty FROM portfolio_holdings WHERE date = ? AND qty > 0", (str(d),)).fetchall()}


def _live_orders(conn, after, through) -> int:
    """LIVE orders placed in (after, through] (order_log.mode; PAPER and SANDBOX orders never touch the real cash)."""
    try:
        r = conn.execute("SELECT COUNT(*) FROM order_log WHERE UPPER(COALESCE(mode, '')) = 'LIVE' "
                         "AND substr(timestamp, 1, 10) > ? AND substr(timestamp, 1, 10) <= ?",
                         (_day(after), _day(through))).fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0                                       # no order_log yet: nothing was ever placed


def detect(conn, env: str, prev: dict | None, cash_now, on=None, known_flow: float = 0.0,
           cfg: dict | None = None) -> dict | None:
    """The cash change since `prev` (a pnl_daily row) that trades cannot explain, as a candidate flow, or None.
    LIVE only. Needs a cash figure at both ends, the same holdings (symbol -> qty) at both, no LIVE order in between,
    and |change - flows already recorded| >= detect_min_value."""
    if str(env).upper() != "LIVE" or not prev or prev.get("cash") is None or cash_now is None:
        return None
    cfg = cfg or settings()
    on = _day(on) or date.today().isoformat()
    frm = _day(prev["date"])
    change = round(float(cash_now) - float(prev["cash"]) - float(known_flow or 0), 2)
    if abs(change) < cfg["detect_min_value"]:
        return None
    if _holdings(conn, frm) != _holdings(conn, on) or _live_orders(conn, frm, on):
        return None                                    # trades in the window: the change cannot be attributed
    return {"env": "LIVE", "date": on, "from": frm, "amount": change,
            "kind": "DEPOSIT" if change > 0 else "WITHDRAWAL", "ref": f"LIVE:{on}",
            "note": (f"detected: cash {float(prev['cash']):,.2f} on {frm} -> {float(cash_now):,.2f} on {on}, "
                     f"holdings unchanged, no LIVE order")}


def record_detected(conn, cand: dict, cfg: dict | None = None) -> dict:
    """Store a detect() candidate: PENDING unless config.json cash_flows.auto_confirm_detected is true."""
    cfg = cfg or settings()
    return record(conn, cand["env"], cand["kind"], cand["amount"], cand["date"], cand["note"], source="detected",
                  status=CONFIRMED if cfg["auto_confirm_detected"] else PENDING, ref=cand["ref"])


def detect_history(conn, env: str = "LIVE", frm=None, cfg: dict | None = None) -> list:
    """Report only: detect() over consecutive stored pnl_daily rows of `env` (nothing is written)."""
    cfg = cfg or settings()
    rows = [_row(r) for r in conn.execute("SELECT date, cash FROM pnl_daily WHERE env = ? ORDER BY date",
                                          (str(env).upper(),)).fetchall()]
    out, prev = [], None
    for r in rows:
        if frm and _day(r["date"]) < _day(frm):
            prev = r
            continue
        if prev is not None:
            known = net_flow(conn, env, prev["date"], r["date"])
            c = detect(conn, env, prev, r["cash"], r["date"], known, cfg)
            if c:
                out.append(c)
        prev = r
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ATIP external cash flows (deposits, withdrawals, transfers)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--env", default="LIVE")
    a.add_argument("--kind", required=True, choices=list(KINDS))
    a.add_argument("--amount", required=True)
    a.add_argument("--date", default=None)
    a.add_argument("--note", default="")
    li = sub.add_parser("list")
    li.add_argument("--env", default=None)
    li.add_argument("--status", default=None)
    for name in ("confirm", "void"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("--reason", default="")
        p.add_argument("--kind", default=None)
    d = sub.add_parser("detect")
    d.add_argument("--env", default="LIVE")
    d.add_argument("--from", dest="frm", default=None)
    args = ap.parse_args(argv)
    from db.schema import get_connection
    conn = get_connection()
    try:
        if args.cmd == "add":
            print(record(conn, args.env, args.kind, args.amount, args.date, args.note))
        elif args.cmd == "list":
            for r in listing(conn, args.env, args.status):
                print(f"{r['id']:>4} {r['date']} {r['env']:<5} {r['kind']:<12} {float(r['amount']):>14,.2f} "
                      f"{r['status']:<9} {r['source']:<8} {r.get('note') or ''}")
        elif args.cmd in ("confirm", "void"):
            print(set_status(conn, args.id, CONFIRMED if args.cmd == "confirm" else VOID, args.reason, args.kind))
        elif args.cmd == "detect":
            found = detect_history(conn, args.env, args.frm)
            print(f"{len(found)} unexplained cash change(s) in the stored {args.env} rows (report only, nothing written)")
            for c in found:
                print(f"  {c['from']} -> {c['date']}  {c['amount']:>14,.2f}  {c['kind']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
