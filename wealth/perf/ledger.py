"""
The performance ledger (PERF-001-01): every transaction the performance engine
uses, immutable, with where it came from.

perf_ledger (append-only; migration 0005 adds the triggers)
    txn_id, tenant_id, owner_id, portfolio, source, source_ref (unique per owner+source),
    trade_date, ts, kind, symbol, quantity, price, gross_value, fees, reference_price,
    price_quality, strategy_id, tag, note, created_at, import_run,
    W39: entry_seq (entry order, the same-day tie-breaker on every database: MySQL has no
    rowid), order_ref (the source order id), signal_ref (the signal_log id the trade
    acted on -- exact signal attribution), fee_breakdown (JSON of brokerage / stt /
    exchange / sebi / stamp / gst / dp / other when the source gives them; they sum to fees)
perf_ledger_void (corrections): a voided txn is ignored by every engine; the row
    itself is never changed or deleted. Correct a mistake by voiding it and adding
    the right transaction.

KINDS   BUY, SELL, OPENING (a position that existed before the ledger starts: price =
        cost basis, reference_price = the market close that day, which is the value
        performance is measured from), DIVIDEND (cash per share x quantity in
        gross_value), FEE (a charge not tied to a trade),
        DEPOSIT / WITHDRAWAL (W39: cash moved into / out of the account, amount in
        gross_value, no symbol). They never touch the positions sleeve; with them the
        engine also reports the whole account (positions + cash).

PORTFOLIOS and their SOURCES (house book = default tenant only)
    PAPER    paper_order rows with status TRADED or PARTIALLY_FILLED (filled_qty, fill_price,
             brokerage) -- a partial fill moved the paper position and balance, so it is a
             real transaction (W39: these used to be lost, and later sells were capped)
    OMS      oms_fill (W4) joined to oms_order.reference_price, strategy_id
    LIVE     inferred from successive broker syncs (portfolio_holdings):
               first sync      OPENING at the broker's average cost, valued at that day's close
               qty up          BUY of the difference at the exact implied price
                               (q2 x avg2 - q1 x avg1) / (q2 - q1)   [average-cost identity]
               qty down / gone SELL at that session's close -- APPROXIMATE (the broker sync
                               has no sell price); price_quality marks it
             plus manual LIVE entries (e.g. from contract notes) -- void an inferred row and
             enter the real one to make it exact
    MANUAL   the owner's own entries for anything else they track

Imports are idempotent: source_ref is unique, so re-running adds only new rows.
"""

from __future__ import annotations

from datetime import date

from wealth import common as C

KINDS = ("BUY", "SELL", "OPENING", "DIVIDEND", "FEE", "DEPOSIT", "WITHDRAWAL")
CASH_KINDS = ("DEPOSIT", "WITHDRAWAL")
FEE_COMPONENTS = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other")
PORTFOLIOS = ("PAPER", "OMS", "LIVE", "MANUAL")
MANUAL_PORTFOLIOS = ("LIVE", "MANUAL")


def _next_seq(conn) -> int:
    r = conn.execute("SELECT MAX(entry_seq) FROM perf_ledger").fetchone()
    return int(r[0] or 0) + 1


def _insert(conn, owner, portfolio, source, ref, trade_date, ts, kind, symbol, qty, price, fees, reference_price=None,
            price_quality="EXACT", strategy_id=None, tag=None, note=None, run_id=None, gross=None, order_ref=None,
            signal_ref=None, fee_breakdown=None) -> bool:
    gross = gross if gross is not None else (round(qty * price, 6) if qty is not None and price is not None else None)
    cur = conn.execute(
        "INSERT OR IGNORE INTO perf_ledger (txn_id,tenant_id,owner_id,portfolio,source,source_ref,trade_date,ts,kind,"
        "symbol,quantity,price,gross_value,fees,reference_price,price_quality,strategy_id,tag,note,created_at,import_run,"
        "entry_seq,order_ref,signal_ref,fee_breakdown) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (C.new_id("txn"), owner["tenant_id"], owner["owner_id"], portfolio, source, str(ref), str(trade_date),
         ts, kind, symbol, qty, price, gross, fees or 0.0, reference_price, price_quality, strategy_id, tag, note,
         C.now(), run_id, _next_seq(conn), order_ref, signal_ref, C.dumps(fee_breakdown) if fee_breakdown else None))
    return cur.rowcount > 0


def order_by(conn, alias="l") -> str:
    """Ledger order: trade date, trade time, then order of entry. On SQLite the entry
    order is rowid (exact for every row, old or new); elsewhere entry_seq (migration
    0006 numbers the rows written before it existed), then created_at, txn_id."""
    a = f"{alias}." if alias else ""
    import sqlite3
    if isinstance(conn, sqlite3.Connection):
        return f"{a}trade_date, {a}ts, {a}rowid"
    return f"{a}trade_date, {a}ts, {a}entry_seq, {a}created_at, {a}txn_id"


def _import_paper(conn, owner, run_id) -> int:
    if not C.table_exists(conn, "paper_order"):
        return 0
    n = 0
    # PARTIALLY_FILLED is terminal in the paper broker (the remainder is never worked), and the
    # filled part already moved the paper position and balance: it is a real transaction.
    for r in conn.execute("SELECT order_id, created_at, symbol, transaction_type, status, "
                          "CASE WHEN status='PARTIALLY_FILLED' THEN filled_qty ELSE COALESCE(filled_qty, quantity) END q,"
                          " quantity, fill_price, brokerage, tag FROM paper_order WHERE status IN "
                          "('TRADED','PARTIALLY_FILLED') AND fill_price>0 AND COALESCE(filled_qty, quantity)>0"):
        ts = str(r["created_at"])
        partial = r["status"] == "PARTIALLY_FILLED"
        n += _insert(conn, owner, "PAPER", "paper_order", r["order_id"], ts[:10], ts, str(r["transaction_type"]).upper(),
                     r["symbol"], float(r["q"]), float(r["fill_price"]), float(r["brokerage"] or 0),
                     tag=r["tag"], run_id=run_id, order_ref=r["order_id"],
                     note=f"partial fill {float(r['q']):g} of {float(r['quantity']):g}" if partial else None,
                     fee_breakdown={"brokerage": float(r["brokerage"] or 0)})
    return n


def _import_oms(conn, owner, run_id) -> int:
    if not C.table_exists(conn, "oms_fill"):
        return 0
    n = 0
    for r in conn.execute("SELECT f.fill_id, f.order_id, f.filled_at, f.symbol, f.side, f.quantity, f.price, f.fees, "
                          "f.strategy_id, o.reference_price, o.decision_id FROM oms_fill f LEFT JOIN oms_order o ON "
                          "o.order_id=f.order_id WHERE f.quantity>0 AND f.price>0"):
        ts = str(r["filled_at"])
        n += _insert(conn, owner, "OMS", "oms_fill", r["fill_id"], ts[:10], ts, str(r["side"]).upper(), r["symbol"],
                     float(r["quantity"]), float(r["price"]), float(r["fees"] or 0), r["reference_price"],
                     strategy_id=r["strategy_id"], run_id=run_id, order_ref=r["order_id"],
                     tag=f"decision:{r['decision_id']}" if r["decision_id"] else None)
    return n


def _import_broker(conn, owner, run_id) -> int:
    if not C.table_exists(conn, "portfolio_holdings"):
        return 0
    # every stored holdings date: rows are written only by a successful sync (portfolio_sync,
    # which records outcomes, is newer than the holdings history)
    dates = sorted({str(r[0])[:10] for r in conn.execute("SELECT DISTINCT date FROM portfolio_holdings")})
    if not dates:
        return 0
    first = conn.execute("SELECT MIN(trade_date) FROM perf_ledger WHERE tenant_id=? AND owner_id=? AND portfolio='LIVE' "
                         "AND source='broker_sync'", (owner["tenant_id"], owner["owner_id"])).fetchone()[0]
    if first is not None:
        # the ledger already starts at `first`: never create a second opening position
        dates = [d for d in dates if d >= str(first)[:10]]
    n = 0
    prev = None
    for d in dates:
        snap = {r["symbol"]: (float(r["qty"]), float(r["avg_price"] or 0)) for r in conn.execute(
            "SELECT symbol, qty, avg_price FROM portfolio_holdings WHERE date=? AND qty>0", (d,))}
        if prev is None:
            if first is None or str(first)[:10] >= d:
                for s, (q, a) in snap.items():
                    px = _close(conn, s, d)
                    n += _insert(conn, owner, "LIVE", "broker_sync", f"{d}:{s}:OPENING", d, d, "OPENING", s, q, a, 0.0,
                                 reference_price=px, price_quality="EXACT" if px else "NO_MARKET_PRICE",
                                 note="opening position from the first broker sync: cost basis = broker average cost; "
                                      "performance is measured from this date's close", run_id=run_id)
            prev = snap
            continue
        for s in sorted(set(prev) | set(snap)):
            q1, a1 = prev.get(s, (0.0, 0.0))
            q2, a2 = snap.get(s, (0.0, 0.0))
            if abs(q2 - q1) < 1e-9:
                continue
            if q2 > q1:
                px = (q2 * a2 - q1 * a1) / (q2 - q1)
                qual = "IMPLIED_FROM_AVG_COST"
                if px <= 0 or (a2 and not 0.2 * a2 <= px <= 5 * a2):
                    px, qual = _close(conn, s, d), "APPROX_CLOSE"
                n += _insert(conn, owner, "LIVE", "broker_sync", f"{d}:{s}:BUY", d, d, "BUY", s, q2 - q1, px, 0.0,
                             price_quality=qual, note="inferred from broker holdings change (fees unknown)",
                             run_id=run_id)
            else:
                px = _close(conn, s, d)
                n += _insert(conn, owner, "LIVE", "broker_sync", f"{d}:{s}:SELL", d, d, "SELL", s, q1 - q2, px, 0.0,
                             price_quality="APPROX_CLOSE", note="inferred from broker holdings change: sell price = "
                                                               "that session's close (approximate; fees unknown)",
                             run_id=run_id)
        prev = snap
    return n


def _close(conn, sym, d):
    r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 ORDER BY date DESC LIMIT 1",
                     (sym, d)).fetchone()
    return float(r[0]) if r else None


def sync(conn, owner) -> dict:
    """Import new transactions from every source this owner may see."""
    run_id = C.new_id("imp")
    out = {"run_id": run_id, "PAPER": 0, "OMS": 0, "LIVE": 0}
    if C.owns_house_book(owner):
        out["PAPER"] = _import_paper(conn, owner, run_id)
        out["OMS"] = _import_oms(conn, owner, run_id)
        out["LIVE"] = _import_broker(conn, owner, run_id)
    conn.commit()
    return out


def add_manual(conn, owner, b: dict, actor="owner") -> dict:
    allowed = {"portfolio", "trade_date", "kind", "symbol", "quantity", "price", "fees", "reference_price",
               "gross_value", "note", "signal_id", "order_ref", "fee_breakdown"}
    unknown = set(b) - allowed
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    pf = str(b.get("portfolio") or "MANUAL").upper()
    if pf not in MANUAL_PORTFOLIOS:
        raise ValueError(f"portfolio must be one of {list(MANUAL_PORTFOLIOS)} for manual entries")
    if pf == "LIVE" and not C.owns_house_book(owner):
        raise ValueError("only the house owner has a LIVE portfolio; use MANUAL")
    kind = str(b.get("kind") or "").upper()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    td = C.parse_date(b.get("trade_date"), "trade_date")
    if td > date.today():
        raise ValueError("trade_date cannot be in the future")
    cash = kind in CASH_KINDS
    if cash and b.get("symbol"):
        raise ValueError(f"{kind} is a cash movement: leave symbol empty")
    sym = None if cash else C.symbol(b.get("symbol"), "symbol", required=kind != "FEE")
    qty = C.num(b.get("quantity"), "quantity", 0, 1e12, required=kind in ("BUY", "SELL", "OPENING", "DIVIDEND"))
    if kind in ("BUY", "SELL", "OPENING") and not qty:
        raise ValueError("quantity must be > 0")
    price = C.num(b.get("price"), "price", 0, 1e9, required=kind in ("BUY", "SELL", "OPENING"))
    fees = C.num(b.get("fees"), "fees", 0, 1e9, required=False) or 0.0
    gross = C.num(b.get("gross_value"), "gross_value", 0, 1e12, required=kind in ("DIVIDEND", "FEE") + CASH_KINDS)
    if cash and not gross:
        raise ValueError(f"{kind} needs gross_value > 0 (the amount)")
    if kind == "FEE" and fees:
        raise ValueError("a FEE row carries its amount in gross_value; leave fees empty")
    breakdown = _fee_breakdown(b.get("fee_breakdown"), fees)
    if breakdown and not b.get("fees"):
        fees = round(sum(breakdown.values()), 6)
    sig = C.text(b.get("signal_id"), "signal_id", 80, required=False) or None
    if sig and kind != "BUY":
        raise ValueError("signal_id links a BUY to the signal it acted on")
    if sig:
        from scores.signal_log import ensure_tables
        ensure_tables(conn)
        if not conn.execute("SELECT 1 FROM signal_log WHERE id=?", (sig,)).fetchone():
            raise ValueError(f"signal_id {sig} is not in signal_log")
    ref = C.num(b.get("reference_price"), "reference_price", 0, 1e9, required=False)
    if kind == "SELL":
        held = _held(conn, owner, pf, sym, td)
        if qty > held + 1e-9:
            raise ValueError(f"cannot sell {qty:g} {sym}: the {pf} ledger holds {held:g} on {td}")
    ref_id = C.new_id("man")
    _insert(conn, owner, pf, "manual", ref_id, td, str(C.now()), kind, sym, qty if not cash else None,
            price if not cash else None, fees, ref, "EXACT",
            note=C.text(b.get("note"), "note", 300, required=False) or f"entered by {actor}",
            gross=gross if kind in ("DIVIDEND", "FEE") + CASH_KINDS else None,
            order_ref=C.text(b.get("order_ref"), "order_ref", 80, required=False) or None, signal_ref=sig,
            fee_breakdown=breakdown or None)
    conn.commit()
    r = conn.execute("SELECT * FROM perf_ledger WHERE source='manual' AND source_ref=? AND tenant_id=? AND owner_id=?",
                     (ref_id, owner["tenant_id"], owner["owner_id"])).fetchone()
    return dict(r)


def _fee_breakdown(raw, fees) -> dict:
    """{component: amount} from the request, components from FEE_COMPONENTS; when fees is
    given too the parts must add up to it (to the paisa)."""
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("fee_breakdown must be an object of component: amount")
    bad = set(raw) - set(FEE_COMPONENTS)
    if bad:
        raise ValueError(f"fee_breakdown components must be among {list(FEE_COMPONENTS)}; got {sorted(bad)}")
    out = {k: C.num(v, f"fee_breakdown.{k}", 0, 1e9) for k, v in raw.items() if v not in (None, "")}
    if fees and abs(sum(out.values()) - fees) > 0.005:
        raise ValueError(f"fee_breakdown adds up to {sum(out.values()):.2f}, fees is {fees:.2f}")
    return out


def _held(conn, owner, pf, sym, on) -> float:
    q = 0.0
    for kind, qty in conn.execute(
            "SELECT kind, quantity FROM perf_ledger l WHERE tenant_id=? AND owner_id=? AND portfolio=? AND symbol=? AND "
            "trade_date<=? AND NOT EXISTS (SELECT 1 FROM perf_ledger_void v WHERE v.txn_id=l.txn_id)",
            (owner["tenant_id"], owner["owner_id"], pf, sym, str(on))):
        if kind in ("BUY", "OPENING"):
            q += qty or 0
        elif kind == "SELL":
            q -= qty or 0
    return q


def void(conn, owner, txn_id, reason, actor="owner") -> dict:
    r = conn.execute("SELECT txn_id FROM perf_ledger WHERE txn_id=? AND tenant_id=? AND owner_id=?",
                     (txn_id, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("transaction not found")
    if conn.execute("SELECT 1 FROM perf_ledger_void WHERE txn_id=?", (txn_id,)).fetchone():
        raise ValueError("transaction is already void")
    conn.execute("INSERT INTO perf_ledger_void (txn_id,tenant_id,owner_id,voided_at,voided_by,reason) VALUES "
                 "(?,?,?,?,?,?)", (txn_id, owner["tenant_id"], owner["owner_id"], C.now(), actor,
                                   C.text(reason, "reason", 300)))
    conn.commit()
    return {"txn_id": txn_id, "voided": True}


def transactions(conn, owner, portfolio=None, start=None, end=None, include_void=False, limit=5000) -> list:
    q = ("SELECT l.*, v.voided_at, v.reason AS void_reason FROM perf_ledger l LEFT JOIN perf_ledger_void v ON "
         "v.txn_id=l.txn_id WHERE l.tenant_id=? AND l.owner_id=?")
    args = [owner["tenant_id"], owner["owner_id"]]
    if portfolio:
        q += " AND l.portfolio=?"
        args.append(portfolio)
    if start:
        q += " AND l.trade_date>=?"
        args.append(str(start))
    if end:
        q += " AND l.trade_date<=?"
        args.append(str(end))
    if not include_void:
        q += " AND v.txn_id IS NULL"
    q += f" ORDER BY {order_by(conn)} LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args)]


def portfolios(conn, owner) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT portfolio, COUNT(*) txns, MIN(trade_date) first_date, MAX(trade_date) last_date FROM perf_ledger l "
        "WHERE tenant_id=? AND owner_id=? AND NOT EXISTS (SELECT 1 FROM perf_ledger_void v WHERE v.txn_id=l.txn_id) "
        "GROUP BY portfolio ORDER BY portfolio", (owner["tenant_id"], owner["owner_id"]))]
