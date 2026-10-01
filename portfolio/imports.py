"""
Multi-broker import (W37: PF-12) -- trades and holdings from any broker into the wealth ledger.

Two ways in:

  FILE   parse(content, filename)  -> preview (nothing written)
         commit(conn, owner, parsed, broker=None, as_of=None) -> ledger rows
         Accepts the CSV exports brokers give their clients (tradebook / order history / holdings) --
         Zerodha Console, Upstox, Groww, ICICI Direct, Angel One, HDFC / Kotak-style, or any CSV with
         recognisable headers. Columns are matched by ALIASES (case / punctuation insensitive), so a
         renamed column or an extra one does not break the import; the detected broker is a label only.
           tradebook rows  -> BUY / SELL ledger transactions (executed rows only: a status column
                              saying cancelled / rejected / failed is skipped)
           holdings rows   -> OPENING positions on `as_of` at the file's average cost, ONLY for
                              symbols with no earlier ledger history from that broker (so a later
                              holdings file never opens a position twice)
  API    import_from_broker(conn, owner, tenant, user, broker) -> today's trade book (+ holdings the first
         time) through the read-only connector (BR-07) and the user's vault credential (ENT-06).

Ledger: portfolio LIVE, source "import:<broker>", source_ref = the broker's trade id (else a hash of
date|symbol|side|qty|price|row number) -- perf_ledger's UNIQUE(source, source_ref) makes every import
idempotent: importing the same file twice adds nothing. Symbols: the file's symbol column, else the ISIN
mapped through the newest NSE bhavcopy in atip_data/raw (ISIN -> TckrSymb), else the company name the same
way; rows that cannot be resolved are reported, never guessed. Dhan is the broker ATIP already syncs
(broker_sync) -- importing Dhan files on top would double count, so it is refused unless forced.
Each import is recorded in broker_import_run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path

ALIASES = {
    "symbol": ["symbol", "tradingsymbol", "trading symbol", "scrip", "scrip name", "stock symbol", "instrument",
               "nse symbol", "security", "stock code", "script name"],
    "name": ["company", "company name", "stock name", "stock", "scrip name", "security name", "name"],
    "isin": ["isin", "isin code", "isin no"],
    "date": ["trade date", "trade_date", "date", "order date", "execution date", "transaction date", "trade dt"],
    "datetime": ["order execution time", "execution date and time", "trade time", "order_execution_time",
                 "exchange timestamp", "time", "fill time"],
    "side": ["trade type", "trade_type", "type", "buy/sell", "side", "transaction type", "action", "b/s", "buy sell"],
    "quantity": ["quantity", "qty", "traded quantity", "filled quantity", "qty.", "quantity available", "shares"],
    "price": ["price", "trade price", "rate", "average price", "avg price", "avg. cost", "average cost", "avg cost",
              "buy average", "average buy price", "traded price"],
    "value": ["value", "trade value", "amount", "net amount", "turnover"],
    "trade_id": ["trade id", "trade_id", "trade num", "trade no", "trade number", "fill id", "exchange trade id"],
    "order_id": ["order id", "order_id", "order ref", "order ref.", "exchange order id", "order no", "order number"],
    "status": ["order status", "status"],
    "fees": ["brokerage", "brokerage incl. gst", "total charges", "charges", "fees"],
    "exchange": ["exchange", "exch"],
}
SIGNATURES = [("zerodha", {"trade_type", "order_execution_time"}), ("groww", {"execution date and time"}),
              ("icici", {"order ref.", "settlement"}), ("upstox", {"trade num", "scrip code"}),
              ("angel", {"buy/sell", "trade id"})]
VALID_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&_-]{0,19}$")
BAD_STATUS = re.compile(r"cancel|reject|fail|expired", re.I)


def _norm(h):
    return re.sub(r"[^a-z0-9/.&_ ]+", "", (h or "").strip().lower()).strip()


def _map_columns(headers) -> dict:
    hn = {_norm(h): h for h in headers}
    out = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in hn and hn[n] not in out.values():
                out[field] = hn[n]
                break
    return out


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d", "%d-%b-%y", "%d/%m/%y",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%d %b %Y %I:%M %p", "%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p")


def _date(v):
    s = str(v or "").strip()
    if not s:
        return None
    for cand in (s, s[:19], s[:10], s.split(" ")[0], s.split("T")[0]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return date.fromisoformat(m.group(1)) if m else None


_ISIN, _NAME = None, None


def _isin_maps():
    """ISIN -> symbol and normalised company name -> symbol from the newest raw NSE bhavcopy."""
    global _ISIN, _NAME
    if _ISIN is not None:
        return _ISIN, _NAME
    _ISIN, _NAME = {}, {}
    files = sorted(Path("atip_data/raw").glob("bhavcopy_cm_*.csv"))
    if files:
        with open(files[-1], encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                if r.get("SctySrs") in ("EQ", "BE", "SM", "BZ") and r.get("ISIN"):
                    _ISIN[r["ISIN"].strip().upper()] = r["TckrSymb"].strip().upper()
                    _NAME[_norm(r.get("FinInstrmNm"))] = r["TckrSymb"].strip().upper()
    return _ISIN, _NAME


def _symbol(row, cols):
    from brokers.base import clean_symbol
    if cols.get("symbol") and row.get(cols["symbol"]):
        sym = clean_symbol(row[cols["symbol"]])
        if VALID_SYMBOL.match(sym):
            return sym                    # an invalid symbol cell falls through to ISIN / name
    isins, names = _isin_maps()
    if cols.get("isin") and row.get(cols["isin"]):
        s = isins.get(row[cols["isin"]].strip().upper())
        if s:
            return s
    if cols.get("name") and row.get(cols["name"]):
        n = _norm(row[cols["name"]])
        return names.get(n) or names.get(n + " limited") or names.get(n + " ltd")
    return None


def _side(v):
    s = str(v or "").strip().upper()
    return "BUY" if s.startswith("B") or s == "PURCHASE" else "SELL" if s.startswith("S") else None


def parse(content: bytes | str, filename: str = "") -> dict:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    if filename.lower().endswith((".xlsx", ".xls")):
        raise ValueError("save the broker's Excel export as CSV first (File > Save as > CSV)")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines[:30]) if len(re.findall(r"[,;\t]", l)) >= 2), 0)  # skip title rows
    sample = "\n".join(lines[start:start + 5])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), dialect=dialect)
    headers = reader.fieldnames or []
    cols = _map_columns(headers)
    hn = {_norm(h) for h in headers}
    broker = next((b for b, sig in SIGNATURES if sig <= hn), "generic")
    kind = "tradebook" if "side" in cols else ("holdings" if "quantity" in cols and "price" in cols else None)
    if kind is None:
        raise ValueError(f"cannot tell what this file is: need a buy/sell column (tradebook) or quantity + average "
                         f"price (holdings). Columns seen: {headers[:15]}")
    rows, errors = [], []
    for n, r in enumerate(reader, start=start + 2):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue
        sym = _symbol(r, cols)
        qty = _num(r.get(cols.get("quantity")))
        px = _num(r.get(cols.get("price")))
        if px is None and cols.get("value") and qty:
            v = _num(r.get(cols["value"]))
            px = v / qty if v else None
        if kind == "tradebook":
            if cols.get("status") and BAD_STATUS.search(str(r.get(cols["status"]) or "")):
                continue
            side = _side(r.get(cols["side"]))
            d = _date(r.get(cols.get("datetime"))) or _date(r.get(cols.get("date")))
            problem = ("symbol / ISIN not recognised" if not sym else "no BUY/SELL" if not side else
                       "no date" if not d else "bad quantity" if not qty or qty <= 0 else "bad price" if not px or px <= 0
                       else None)
            if problem:
                errors.append({"line": n, "problem": problem, "row": {k: r[k] for k in list(r)[:8]}})
                continue
            rows.append({"line": n, "symbol": sym, "date": str(d), "ts": str(r.get(cols.get("datetime")) or "")[:19],
                         "side": side, "quantity": abs(qty), "price": round(px, 4),
                         "fees": _num(r.get(cols.get("fees"))) or 0.0,
                         "trade_id": (r.get(cols.get("trade_id")) or "").strip() or None,
                         "order_id": (r.get(cols.get("order_id")) or "").strip() or None})
        else:
            if not sym or not qty or qty <= 0:
                errors.append({"line": n, "problem": "symbol not recognised" if not sym else "no quantity",
                               "row": {k: r[k] for k in list(r)[:8]}})
                continue
            rows.append({"line": n, "symbol": sym, "quantity": qty, "avg_price": round(px, 4) if px else None})
    return {"kind": kind, "broker_detected": broker, "columns": cols, "rows": rows, "errors": errors[:200],
            "summary": {"rows": len(rows), "errors": len(errors),
                        "symbols": len({r["symbol"] for r in rows}),
                        "date_range": [min((r["date"] for r in rows), default=None),
                                       max((r["date"] for r in rows), default=None)] if kind == "tradebook" else None}}


def _ref(r):
    if r.get("trade_id"):
        return f"T:{r['trade_id']}"
    raw = f"{r['date']}|{r['symbol']}|{r['side']}|{r['quantity']}|{r['price']}|{r.get('order_id')}|{r['line']}"
    return "H:" + hashlib.sha1(raw.encode()).hexdigest()[:20]


def commit(conn, owner: dict, parsed: dict, broker: str | None = None, as_of=None, force: bool = False,
           filename: str = "") -> dict:
    from wealth.perf.ledger import _insert
    broker = (broker or parsed.get("broker_detected") or "generic").lower()
    if not re.match(r"^[a-z0-9_]{2,20}$", broker):
        raise ValueError("broker: 2-20 lowercase letters / digits")
    if broker == "dhan" and not force and conn.execute(
            "SELECT 1 FROM perf_ledger WHERE tenant_id=? AND owner_id=? AND source='broker_sync' LIMIT 1",
            (owner["tenant_id"], owner["owner_id"])).fetchone():
        raise ValueError("Dhan holdings are already synced into this ledger (broker_sync); importing Dhan files too "
                         "would double count. Use force only if the sync is not in use.")
    run_id = "IMP" + uuid.uuid4().hex[:12].upper()
    source = f"import:{broker}"
    added = skipped = 0
    if parsed["kind"] == "tradebook":
        for r in sorted(parsed["rows"], key=lambda x: (x["date"], x["ts"], x["line"])):
            ok = _insert(conn, owner, "LIVE", source, _ref(r), r["date"], r["ts"] or r["date"], r["side"], r["symbol"],
                         r["quantity"], r["price"], r["fees"], tag=broker, note=f"imported from {filename or broker}",
                         run_id=run_id)
            added, skipped = added + ok, skipped + (not ok)
    else:
        d = str(as_of or date.today())[:10]
        for r in parsed["rows"]:
            seen = conn.execute("SELECT 1 FROM perf_ledger WHERE tenant_id=? AND owner_id=? AND source=? AND symbol=? "
                                "LIMIT 1", (owner["tenant_id"], owner["owner_id"], source, r["symbol"])).fetchone()
            if seen or r["avg_price"] is None:
                skipped += 1
                continue
            ok = _insert(conn, owner, "LIVE", source, f"OPEN:{r['symbol']}:{d}", d, d, "OPENING", r["symbol"],
                         r["quantity"], r["avg_price"], 0.0, tag=broker,
                         note=f"opening position from a {broker} holdings file (cost basis = broker average)",
                         run_id=run_id)
            added, skipped = added + ok, skipped + (not ok)
    conn.execute("INSERT INTO broker_import_run (run_id,tenant_id,owner_id,broker,kind,filename,rows_in,added,skipped,"
                 "errors,created_at,detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, owner["tenant_id"], owner["owner_id"], broker, parsed["kind"], filename[:120],
                  len(parsed["rows"]), added, skipped, len(parsed["errors"]), datetime.now(),
                  json.dumps({"columns": parsed["columns"], "summary": parsed["summary"]})))
    conn.commit()
    return {"run_id": run_id, "broker": broker, "kind": parsed["kind"], "added": added,
            "already_imported": skipped, "errors": len(parsed["errors"])}


def import_from_broker(conn, owner: dict, tenant: str, user: str, broker: str) -> dict:
    """Today's trade book (and, the first time, the holdings) through the read-only connector."""
    from brokers.registry import for_user
    c, src = for_user(conn, tenant, user, broker, "ledger import (read-only)")
    trades = [{"line": i, "symbol": t.symbol, "date": (t.ts or str(date.today()))[:10], "ts": (t.ts or "")[:19],
               "side": t.side, "quantity": t.quantity, "price": t.price, "fees": 0.0, "trade_id": t.trade_id,
               "order_id": t.order_id} for i, t in enumerate(c.trades()) if t.side in ("BUY", "SELL") and t.quantity]
    r1 = commit(conn, owner, {"kind": "tradebook", "rows": trades, "errors": [], "columns": {}, "summary": {}},
                broker, filename=f"{broker} API trade book")
    first = not conn.execute("SELECT 1 FROM perf_ledger WHERE tenant_id=? AND owner_id=? AND source=? AND kind='OPENING' "
                             "LIMIT 1", (owner["tenant_id"], owner["owner_id"], f"import:{broker}")).fetchone()
    r2 = None
    if first:
        hs = [{"line": i, "symbol": h.symbol, "quantity": h.quantity, "avg_price": h.avg_price}
              for i, h in enumerate(c.holdings())]
        r2 = commit(conn, owner, {"kind": "holdings", "rows": hs, "errors": [], "columns": {}, "summary": {}}, broker,
                    filename=f"{broker} API holdings")
    return {"broker": broker, "credential_source": src, "trades": r1, "holdings": r2}


def runs(conn, owner: dict, limit=50) -> list:
    return [dict(r) for r in conn.execute("SELECT run_id, broker, kind, filename, rows_in, added, skipped, errors, "
                                          "created_at FROM broker_import_run WHERE tenant_id=? AND owner_id=? ORDER BY "
                                          "created_at DESC LIMIT ?", (owner["tenant_id"], owner["owner_id"], int(limit)))]
