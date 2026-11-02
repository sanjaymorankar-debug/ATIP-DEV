"""
Reconciliation (ACC-07, 2 Nov 2026).

CHARGES   ATIP's charges for each order leg of a session against the broker's contract note. The owner uploads the
          contract note as CSV (Dhan has no contract-note API ATIP can call today); each row is matched to an
          executed leg on date + symbol + side (+ quantity when given). A difference of 5 paise or more is an OPEN
          item the owner marks ACCEPTED or INVESTIGATE, with a note. Matched rows are MATCHED.
CASH      each session's cash two independent ways -- the trading engine's (orders/sessions.py ledger(), which
          gate() and the order sizing read) and the double-entry ledger's Cash:S1 balance -- plus the paper
          account's cash against the sessions' total. A difference is an OPEN item too.

Accepted CSV headers (case and spacing ignored; the first matching name wins):
    date | trade date | trade_date          symbol | scrip | security | tradingsymbol
    side | buy/sell | buy_sell | type       qty | quantity            price | rate | trade price
    brokerage   stt | securities transaction tax   exchange | exchange txn | transaction charges
    sebi | sebi fee   stamp | stamp duty   gst | igst | cgst+sgst   dp | dp charges   total | total charges
Nothing here changes an order, a session's cash or the ledger.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime

from accounting import cycles as C

TOLERANCE = 0.05                       # rupees
STATUSES = ("OPEN", "MATCHED", "ACCEPTED", "INVESTIGATE")
HEADERS = {
    "date": ("date", "trade date", "trade_date", "tradedate"),
    "symbol": ("symbol", "scrip", "security", "tradingsymbol", "trading symbol", "instrument"),
    "side": ("side", "buy/sell", "buy_sell", "type", "transaction type", "b/s"),
    "qty": ("qty", "quantity"),
    "price": ("price", "rate", "trade price", "avg price"),
    "brokerage": ("brokerage",),
    "stt": ("stt", "securities transaction tax"),
    "exchange": ("exchange", "exchange txn", "transaction charges", "exchange transaction charges", "txn charges"),
    "sebi": ("sebi", "sebi fee", "sebi turnover fee"),
    "stamp": ("stamp", "stamp duty"),
    "gst": ("gst", "igst", "cgst+sgst", "gst total"),
    "dp": ("dp", "dp charges", "dp charge"),
    "total": ("total", "total charges", "net charges"),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recon_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id    TEXT NOT NULL,
            kind        TEXT NOT NULL,          -- CHARGES | CASH
            ref         TEXT,                   -- the order rule id, or the session
            session_id  TEXT,
            on_date     TEXT,
            symbol      TEXT,
            side        TEXT,
            atip_value  REAL,
            broker_value REAL,
            diff        REAL,
            detail      TEXT DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'OPEN',
            note        TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""")
    conn.commit()


def _num(v) -> float | None:
    s = str(v or "").replace(",", "").replace("₹", "").replace("Rs", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v) -> str | None:
    s = str(v or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s if fmt != "%d-%b-%Y" else str(v).strip()[:11], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_contract_note(text: str) -> list:
    """The rows of a contract-note CSV as {date, symbol, side, qty, price, <charges>, total}."""
    rdr = csv.DictReader(io.StringIO(str(text or "").lstrip("﻿")))
    if not rdr.fieldnames:
        raise ValueError("The file is empty or not a CSV.")
    norm = {f: " ".join(str(f).lower().replace("_", " ").split()) for f in rdr.fieldnames}
    col = {}
    for key, names in HEADERS.items():
        for f, n in norm.items():
            if n in names or n.replace(" ", "_") in names:
                col[key] = f
                break
    missing = [k for k in ("date", "symbol", "side") if k not in col]
    if missing:
        raise ValueError(f"The CSV needs columns for {', '.join(missing)} (see the help for accepted names).")
    if "total" not in col and not any(k in col for k in C.CHARGE_TYPES):
        raise ValueError("The CSV has no charge columns: add a 'total charges' column or one per charge.")
    out = []
    for raw in rdr:
        side = str(raw.get(col["side"]) or "").strip().upper()[:1]
        r = {"date": _date(raw.get(col["date"])), "symbol": str(raw.get(col["symbol"]) or "").strip().upper(),
             "side": "BUY" if side == "B" else "SELL" if side == "S" else None,
             "qty": _num(raw.get(col["qty"])) if "qty" in col else None,
             "price": _num(raw.get(col["price"])) if "price" in col else None}
        for k in C.CHARGE_TYPES:
            r[k] = _num(raw.get(col[k])) if k in col else None
        tot = _num(raw.get(col["total"])) if "total" in col else None
        r["total"] = tot if tot is not None else round(sum(r[k] or 0 for k in C.CHARGE_TYPES), 2)
        if r["date"] and r["symbol"] and r["side"]:
            out.append(r)
    if not out:
        raise ValueError("No rows with a date, symbol and BUY/SELL side were found.")
    return out


def _atip_legs(conn) -> list:
    """Every executed order leg of every session, with ATIP's charges for it (the fill's own, else the model's)."""
    from orders import sessions as S
    out = []
    paper_m, real_m = C._models()
    for s in S.list_sessions(conn)["sessions"]:
        rules = S._linked_rules(conn, s["id"])
        fees = S._charges(conn, rules)
        for r in rules:
            if r.get("status") != "EXECUTED" or not r.get("execution_price"):
                continue
            value = float(r["execution_price"]) * float(r.get("execution_quantity") or 0)
            items = C.itemise(paper_m, [(r["side"], value)])
            out.append({"rule_id": r["id"], "session_id": s["id"], "date": str(r.get("updated_at") or "")[:10],
                        "symbol": str(r["symbol"]).upper(), "side": r["side"],
                        "qty": float(r.get("execution_quantity") or 0), "value": round(value, 2),
                        "total": round(float(fees.get(r["id"], sum(items.values()))), 2), "items": items})
    return out


def import_contract_note(conn, text: str) -> dict:
    """Match a contract note's rows to ATIP's legs and store the result as one batch."""
    ensure_tables(conn)
    rows = parse_contract_note(text)
    legs = _atip_legs(conn)
    used, batch, now = set(), "CN-" + uuid.uuid4().hex[:10], _now()
    counts = {s: 0 for s in STATUSES} | {"UNMATCHED": 0}
    for r in rows:
        cand = [l for l in legs if l["rule_id"] not in used and l["date"] == r["date"] and l["symbol"] == r["symbol"]
                and l["side"] == r["side"] and (r["qty"] is None or abs(l["qty"] - r["qty"]) < 0.5)]
        leg = cand[0] if cand else None
        if leg:
            used.add(leg["rule_id"])
        diff = round(r["total"] - leg["total"], 2) if leg else None
        status = "MATCHED" if leg and abs(diff) < TOLERANCE else "OPEN"
        parts = {k: {"broker": r[k], "atip": leg["items"][k] if leg else None} for k in C.CHARGE_TYPES
                 if r[k] is not None}
        detail = ("no executed ATIP order on that date for this symbol and side" if not leg else
                  ", ".join(f"{C.CHARGE_LABEL[k]} ₹{v['broker']:,.2f} vs ₹{v['atip']:,.2f}" for k, v in parts.items()
                            if v["atip"] is not None and abs(v["broker"] - v["atip"]) >= 0.01))
        conn.execute("INSERT INTO recon_items (batch_id, kind, ref, session_id, on_date, symbol, side, atip_value, "
                     "broker_value, diff, detail, status, created_at, updated_at) VALUES (?, 'CHARGES', ?, ?, ?, ?, ?, "
                     "?, ?, ?, ?, ?, ?, ?)",
                     (batch, leg and leg["rule_id"], leg and leg["session_id"], r["date"], r["symbol"], r["side"],
                      leg and leg["total"], r["total"], diff, detail, status, now, now))
        counts["UNMATCHED" if not leg else status] += 1
    conn.commit()
    return {"batch_id": batch, "rows": len(rows), **{k.lower(): v for k, v in counts.items()}}


def check_cash(conn) -> dict:
    """Each session's cash, engine vs ledger; the paper account against the sessions. Stores OPEN items."""
    from accounting import ledger as L
    from orders import sessions as S
    ensure_tables(conn)
    C.sync(conn)
    batch, now, items = "CASH-" + uuid.uuid4().hex[:10], _now(), []
    for s in S.list_sessions(conn)["sessions"]:
        p = s.get("pool")
        if not p:
            continue
        held_cost = round(sum(float(h["cost"]) for h in p["open"]), 2)
        book = L.session_ledger(conn, s["id"])["balance"]
        # The ledger posts a cycle when it closes, so the open buy is added back to the engine's cash; the tax set
        # aside under AFTER_TAX_RESERVE has left the ledger's Cash account for Tax reserve.
        engine = round(p["cash"] + held_cost - float(p.get("tax_reserve") or 0), 2)
        diff = round(engine - book, 2)
        status = "MATCHED" if abs(diff) < TOLERANCE else "OPEN"
        detail = ("" if status == "MATCHED" else
                  "engine cash (with the open trade at cost) differs from the ledger's Cash account")
        conn.execute("INSERT INTO recon_items (batch_id, kind, ref, session_id, on_date, atip_value, broker_value, "
                     "diff, detail, status, created_at, updated_at) VALUES (?, 'CASH', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (batch, s["id"], s["id"], str(date.today()), engine, book, diff, detail, status, now, now))
        items.append({"session_id": s["id"], "engine_cash": engine, "ledger_cash": book, "diff": diff,
                      "status": status})
    f = L.funds(conn)["VIRTUAL"]
    conn.commit()
    return {"batch_id": batch, "sessions": items, "paper_cash": f.get("cash"), "pools_cash": f.get("pools_cash"),
            "outside_pools_cash": f.get("outside_pools_cash"),
            "note": "Paper cash outside the pools is money not allocated to a session, or used by trades outside them."}


def items(conn, status: str | None = None, kind: str | None = None, limit: int = 300) -> list:
    ensure_tables(conn)
    q, a = "SELECT * FROM recon_items WHERE 1=1", []
    if status:
        q += " AND status = ?"; a.append(status.upper())
    if kind:
        q += " AND kind = ?"; a.append(kind.upper())
    return [_row(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (*a, int(limit))).fetchall()]


def resolve(conn, item_id: int, status: str, note: str = "") -> dict:
    """The owner's call on a difference: ACCEPTED (the broker is right, nothing to do) or INVESTIGATE."""
    ensure_tables(conn)
    status = str(status or "").upper()
    if status not in ("ACCEPTED", "INVESTIGATE", "OPEN"):
        raise ValueError("Choose accept or investigate.")
    n = conn.execute("UPDATE recon_items SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                     (status, str(note or "")[:300], _now(), int(item_id))).rowcount
    if not n:
        raise LookupError(f"no reconciliation item {item_id}")
    conn.commit()
    return _row(conn.execute("SELECT * FROM recon_items WHERE id = ?", (int(item_id),)).fetchone())
