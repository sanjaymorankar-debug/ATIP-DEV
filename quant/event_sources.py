"""
Event sources for event-driven research (QR-09), W30 -- feeding market_event.

The W6 event store had corporate actions and bulk deals; EARNINGS, DIVIDEND and the
rest were "pending: no source stored". These come from data ATIP now stores (W27) or
fetches here, each with an honest knowledge time (known_at):

    EARNINGS       fundamental_filing (NSE results XBRL): one event per symbol x quarter at
                   the FIRST broadcast; value = profit growth YoY % (fundamental_data),
                   direction BEAT / MISS = growth > 0 / < 0. known_at = broadcast date,
                   or the NEXT day for a filing after 15:30 (most results land in the
                   evening), so neither a W3 decision nor the study's day 0 can use a
                   result before it was public
    DIVIDEND       NSE corporate-actions listing (dividend subjects), value = Rs per share;
                   known_at = ex-date (the announcement date is not stored: conservative)
    INSIDER_BUY /  insider_trade (SEBI PIT) BUY / SELL by promoters / directors / KMP;
    INSIDER_SELL   value = Rs crore; known_at = disclosure date
    SAST           sast_disclosure promoter acquisition (+) / sale (-) in shares;
                   known_at = disclosure date
MACRO / INDEX_CHANGE stay pending: no free, reliable calendar source.

    sync_all(conn, dividends_days=730)   idempotent ((source, source_id) unique)
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from quant.events import CATEGORY

CATEGORY.update({"INSIDER_BUY": "FLOW", "INSIDER_SELL": "FLOW", "SAST": "FLOW"})
INSIDER_CATEGORIES = ("promoter", "promoter group", "director", "kmp", "key managerial")


def _ins(conn, eid, source, sid, sym, et, ev_date, known, direction, value, payload):
    return conn.execute("INSERT OR IGNORE INTO market_event (event_id,source,source_id,symbol,event_type,category,"
                        "event_date,known_at,direction,value,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, source, sid, sym, et, CATEGORY[et], str(ev_date)[:10], str(known)[:10], direction,
                         value, json.dumps(payload, default=str), datetime.now())).rowcount or 0


def sync_earnings(conn) -> int:
    n = 0
    for sym, pe, first in conn.execute("SELECT symbol, period_end, MIN(broadcast_at) FROM fundamental_filing WHERE "
                                       "status='PARSED' AND broadcast_at IS NOT NULL GROUP BY symbol, period_end"):
        f = conn.execute("SELECT profit_growth_yoy, revenue_growth_yoy, eps_growth_yoy, quarter FROM fundamental_data "
                         "WHERE symbol=? AND period_end=?", (sym, str(pe)[:10])).fetchone()
        g = f[0] if f else None
        bt = datetime.fromisoformat(str(first)[:19].replace(" ", "T"))
        after = bt.hour * 60 + bt.minute > 15 * 60 + 30
        # known_at: an evening filing is usable from the NEXT day (decisions and the study's day 0)
        _ins_n = _ins(conn, f"ER{sym}{str(pe)[:10]}", "nse_filings", f"{sym}|{str(pe)[:10]}", sym, "EARNINGS",
                      bt.date(), bt.date() + timedelta(days=1) if after else bt.date(), "BEAT" if (g or 0) > 0 else "MISS" if (g or 0) < 0 else None, g,
                      {"quarter": f[3] if f else None, "period_end": str(pe)[:10], "broadcast_at": str(bt),
                       "after_close": bt.hour * 60 + bt.minute > 15 * 60 + 30,
                       "revenue_growth_yoy": f[1] if f else None, "eps_growth_yoy": f[2] if f else None})
        n += _ins_n
    conn.commit()
    return n


def sync_insider(conn) -> int:
    n = 0
    for did, sym, person, cat, txn, val, disclosed in conn.execute(
            "SELECT disclosure_id, symbol, person, person_category, txn_type, value_rs, disclosed_at FROM insider_trade "
            "WHERE txn_type IN ('BUY','SELL') AND disclosed_at IS NOT NULL"):
        if not any(k in str(cat or "").lower() for k in INSIDER_CATEGORIES):
            continue
        et = "INSIDER_BUY" if txn == "BUY" else "INSIDER_SELL"
        v = round((val or 0) / 1e7 * (1 if txn == "BUY" else -1), 4)
        n += _ins(conn, f"IN{did}", "insider_trade", did, sym, et, disclosed, disclosed, txn, v,
                  {"person": person, "category": cat})
    conn.commit()
    return n


def sync_sast(conn) -> int:
    n = 0
    for did, sym, acq, txn, a, s_, post, disclosed in conn.execute(
            "SELECT disclosure_id, symbol, acquirer, txn_type, shares_acq, shares_sold, post_pct, disclosed_at FROM "
            "sast_disclosure WHERE is_promoter=1 AND disclosed_at IS NOT NULL"):
        v = (a or 0) - (s_ or 0)
        n += _ins(conn, f"SA{did}", "sast_disclosure", did, sym, "SAST", disclosed, disclosed,
                  "BUY" if v > 0 else "SELL" if v < 0 else None, v, {"acquirer": acq, "post_pct": post, "type": txn})
    conn.commit()
    return n


_DIV = re.compile(r"dividend[^0-9]*?(?:rs|re)\.?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def sync_dividends(conn, days: int = 730, chunk_days: int = 90) -> int:
    from data.corporate_actions import CA_URL
    from data.nse_api import client
    nse = client()
    n = 0
    end = date.today() + timedelta(days=45)
    start = end - timedelta(days=int(days))
    d = start
    while d < end:
        e = min(end, d + timedelta(days=chunk_days))
        rows = nse.json(CA_URL.format(a=d.strftime("%d-%m-%Y"), b=e.strftime("%d-%m-%Y"))) or []
        for r in rows if isinstance(rows, list) else []:
            subj = str(r.get("subject") or "")
            m = _DIV.search(subj)
            if not m or not r.get("symbol") or not r.get("exDate"):
                continue
            try:
                ex = datetime.strptime(str(r["exDate"]).title(), "%d-%b-%Y").date()
            except ValueError:
                continue
            if ex > date.today():
                continue                      # not known as an event until its ex-date (conservative)
            sym = r["symbol"]
            sid = f"{sym}|{ex}|{m.group(1)}"
            n += _ins(conn, "DV" + re.sub(r"[^A-Z0-9]", "", sid.upper())[:28], "nse_corporate_actions", sid, sym,
                      "DIVIDEND", ex, ex, None, float(m.group(1)), {"subject": subj})
        d = e
    conn.commit()
    return n


def sync_all(conn, dividends_days: int = 730, dividends: bool = True) -> dict:
    out = {"earnings": sync_earnings(conn), "insider": sync_insider(conn), "sast": sync_sast(conn)}
    if dividends:
        try:
            out["dividends"] = sync_dividends(conn, dividends_days)
        except Exception as e:
            out["dividends_error"] = str(e)
    return out


def run_scheduled() -> dict:
    from db.schema import get_connection
    from quant.events import sync_events
    conn = get_connection()
    try:
        base = sync_events(conn)
        extra = sync_all(conn, dividends_days=60)
        return {"status": "SUCCESS", "rows": sum(v for v in extra.values() if isinstance(v, int)), **extra,
                "base_inserted": base.get("inserted")}
    finally:
        conn.close()
