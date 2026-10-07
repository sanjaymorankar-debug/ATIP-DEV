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
           charge columns  (W39, PERF-001-06) brokerage / STT / exchange transaction charges /
                              SEBI fee / stamp duty / GST (CGST + SGST + IGST) / DP charges / other,
                              matched by FEE_ALIASES, are stored per trade as its fee_breakdown. The
                              trade's fees are the file's total-charges column when there is one (a
                              total above the parts puts the difference in "other"; a total below
                              them is a broken row: total kept, breakdown dropped, reported), else
                              the sum of the parts.
           charges rows    a contract-note charges file (date + charge columns, no buy/sell or
                              quantity): one FEE row per contract note / date carrying the split, for
                              brokers whose tradebook has no charges. A date whose imported trades
                              from that broker already carry fees is refused (double count) unless forced.
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
# W39 (PERF-001-06): contract-note charge columns -> wealth.perf.ledger.FEE_COMPONENTS
FEE_ALIASES = {
    "brokerage": ["brokerage", "brokerage amount", "brokerage charges", "brok", "brokerage amt"],
    "stt": ["stt", "stt/ctt", "stt amount", "stt charges", "securities transaction tax", "ctt"],
    "exchange": ["exchange transaction charges", "exchange txn charges", "exchange charges", "transaction charges",
                 "txn charges", "exch txn charges", "exchange turnover charges", "turnover charges",
                 "nse transaction charges", "exchange transaction charge"],
    "sebi": ["sebi fees", "sebi fee", "sebi turnover fees", "sebi turnover fee", "sebi charges", "sebi turnover charges"],
    "stamp": ["stamp duty", "stamp", "stamp charges", "stamp duty charges"],
    "gst": ["gst", "igst", "cgst", "sgst", "utgst", "total gst", "gst on charges", "gst amount", "igst amount",
            "cgst amount", "sgst amount"],
    "dp": ["dp charges", "dp charge", "depository charges", "demat charges"],
    "other": ["other charges", "clearing charges", "ipft", "ipft charges", "investor protection fund"],
}
NOTE_NO_ALIASES = ["contract note no", "contract note no.", "contract note number", "contract no", "contract no.",
                   "contract note", "cn no", "cn no.", "note no", "note number"]
TOTAL_FEE_ALIASES = ["total charges", "charges", "total fees", "fees", "net charges", "total brokerage and charges",
                     "brokerage incl. gst"]


def _fee_columns(headers) -> tuple:
    """({component: [header, ...]}, total_header | None). GST may come as CGST + SGST + IGST columns:
    all of them sum into "gst"."""
    hn = {_norm(h): h for h in headers}
    comps = {}
    for comp, names in FEE_ALIASES.items():
        hs = [hn[n] for n in names if n in hn]
        if hs:
            comps[comp] = hs if comp == "gst" else hs[:1]
    used = {h for hs in comps.values() for h in hs}
    total = next((hn[n] for n in TOTAL_FEE_ALIASES if n in hn and hn[n] not in used), None)
    return comps, total


def _fees_of(r, comps, total_col, fallback_col, line, warnings):
    """(fees, breakdown) for one row; see the module docstring."""
    bd = {}
    for comp, hs in comps.items():
        vals = [abs(v) for v in (_num(r.get(h)) for h in hs) if v is not None]
        if vals:
            bd[comp] = round(sum(vals), 4)
    total = _num(r.get(total_col)) if total_col else None
    if total is None:
        if bd:
            return round(sum(bd.values()), 4), bd
        return abs(_num(r.get(fallback_col)) or 0.0) if fallback_col else 0.0, {}
    total = abs(total)
    diff = round(total - sum(bd.values()), 4)
    if bd and diff > 0.01:
        bd["other"] = round(bd.get("other", 0.0) + diff, 4)
    elif bd and diff < -0.01:
        warnings.append({"line": line, "problem": f"charges add up to {sum(bd.values()):.2f} but the total is "
                         f"{total:.2f}: total kept, breakdown not stored"})
        bd = {}
    return total, bd


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
    comps, total_col = _fee_columns(headers)
    fallback_fee_col = None if comps or total_col else cols.get("fees")
    kind = "tradebook" if "side" in cols else ("holdings" if "quantity" in cols and "price" in cols else
                                               "charges" if (comps or total_col) and ("date" in cols or
                                                                                       "datetime" in cols) else None)
    if kind is None:
        raise ValueError(f"cannot tell what this file is: need a buy/sell column (tradebook), quantity + average "
                         f"price (holdings) or a date + charge columns (contract-note charges). Columns seen: "
                         f"{headers[:15]}")
    rows, errors, warnings = [], [], []
    note_col = None
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
            fees, bd = _fees_of(r, comps, total_col, fallback_fee_col, n, warnings)
            rows.append({"line": n, "symbol": sym, "date": str(d), "ts": str(r.get(cols.get("datetime")) or "")[:19],
                         "side": side, "quantity": abs(qty), "price": round(px, 4),
                         "fees": fees, "fee_breakdown": bd,
                         "trade_id": (r.get(cols.get("trade_id")) or "").strip() or None,
                         "order_id": (r.get(cols.get("order_id")) or "").strip() or None})
        elif kind == "charges":
            if note_col is None:
                note_col = next((h for h in headers if _norm(h) in NOTE_NO_ALIASES), "") or cols.get("order_id") or \
                    cols.get("trade_id") or ""
            d = _date(r.get(cols.get("date"))) or _date(r.get(cols.get("datetime")))
            fees, bd = _fees_of(r, comps, total_col, None, n, warnings)
            if not d:
                errors.append({"line": n, "problem": "no date", "row": {k: r[k] for k in list(r)[:8]}})
                continue
            if fees <= 0:
                continue
            rows.append({"line": n, "date": str(d), "fees": round(fees, 4), "fee_breakdown": bd,
                         "note_no": (r.get(note_col) or "").strip() or None if note_col else None})
        else:
            if not sym or not qty or qty <= 0:
                errors.append({"line": n, "problem": "symbol not recognised" if not sym else "no quantity",
                               "row": {k: r[k] for k in list(r)[:8]}})
                continue
            rows.append({"line": n, "symbol": sym, "quantity": qty, "avg_price": round(px, 4) if px else None})
    return {"kind": kind, "broker_detected": broker, "columns": cols, "rows": rows, "errors": errors[:200],
            "fee_columns": {"components": comps, "total": total_col}, "warnings": warnings[:200],
            "summary": {"rows": len(rows), "errors": len(errors), "warnings": len(warnings),
                        "symbols": len({r["symbol"] for r in rows if r.get("symbol")}),
                        "fees": round(sum(r.get("fees") or 0 for r in rows), 2),
                        "fees_with_breakdown": round(sum(r.get("fees") or 0 for r in rows if r.get("fee_breakdown")), 2),
                        "date_range": [min((r["date"] for r in rows), default=None),
                                       max((r["date"] for r in rows), default=None)]
                        if kind in ("tradebook", "charges") else None}}


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
                         run_id=run_id, fee_breakdown=r.get("fee_breakdown") or None)
            added, skipped = added + ok, skipped + (not ok)
    elif parsed["kind"] == "charges":
        charged = {str(x[0])[:10] for x in conn.execute(
            "SELECT DISTINCT trade_date FROM perf_ledger WHERE tenant_id=? AND owner_id=? AND portfolio='LIVE' AND "
            "source=? AND kind IN ('BUY','SELL') AND fees>0", (owner["tenant_id"], owner["owner_id"], source))}
        clash = sorted({r["date"] for r in parsed["rows"]} & charged)
        if clash and not force:
            raise ValueError(f"{len(clash)} date(s) already have {broker} trades carrying charges (e.g. {clash[0]}); "
                             f"importing the contract-note charges too would double count. Use force only if those "
                             f"trades' fees are not the contract-note charges.")
        for r in parsed["rows"]:
            ref = f"FEE:{r['note_no']}" if r.get("note_no") else \
                "FEE:" + hashlib.sha1(f"{r['date']}|{r['fees']}|{r['line']}".encode()).hexdigest()[:20]
            ok = _insert(conn, owner, "LIVE", source, ref, r["date"], r["date"], "FEE", None, None, None, 0.0,
                         gross=round(r["fees"], 4), tag=broker, run_id=run_id, fee_breakdown=r.get("fee_breakdown") or None,
                         note=f"contract-note charges imported from {filename or broker}"
                              + (f" (note {r['note_no']})" if r.get("note_no") else ""))
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
