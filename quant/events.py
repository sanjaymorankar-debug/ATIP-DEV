"""
Event-driven foundation: market_event from the sources ATIP actually has.

    source              event types                        known_at (point in time)
    corporate_actions   SPLIT BONUS RIGHTS DEMERGER         ex_date (the announcement date
                        CONSOLIDATION SCHEME                is not stored, so the event is
                                                            treated as known only on its ex
                                                            date -- conservative, never early)
    bulk_deals          BULK_DEAL (net value Rs cr)         the deal date
    (pending)           EARNINGS DIVIDEND ANNOUNCEMENT      -- no source stored: the interface
                        MACRO INDEX_CHANGE                  and table accept them; none are
                                                            fabricated

Classification: category CORPORATE_ACTION / FLOW / FUNDAMENTAL / MACRO / INDEX,
plus a direction hint where the data says it (bulk deal net buy / sell).

Strategy features (W3, via quant.strategy_features, point in time):
    ev_days_since_split / _bonus / _rights / _demerger / _bulk_deal
                        calendar days since the latest such event
                        known by the decision date (None if none)
    ev_bulk_net_5d      sum of bulk-deal net value (Rs cr) over the last 5 days
"""

from __future__ import annotations

import json
from datetime import date, datetime

CATEGORY = {"SPLIT": "CORPORATE_ACTION", "BONUS": "CORPORATE_ACTION", "RIGHTS": "CORPORATE_ACTION",
            "DEMERGER": "CORPORATE_ACTION", "CONSOLIDATION": "CORPORATE_ACTION", "SCHEME": "CORPORATE_ACTION",
            "BULK_DEAL": "FLOW", "EARNINGS": "FUNDAMENTAL", "DIVIDEND": "CORPORATE_ACTION",
            "ANNOUNCEMENT": "FUNDAMENTAL", "MACRO": "MACRO", "INDEX_CHANGE": "INDEX"}
PENDING_TYPES = ("EARNINGS", "DIVIDEND", "ANNOUNCEMENT", "MACRO", "INDEX_CHANGE")
FEATURE_TYPES = ("split", "bonus", "rights", "demerger", "bulk_deal", "earnings", "dividend", "insider_buy")


def sync_events(conn) -> dict:
    """Idempotent: (source, source_id) is unique."""
    n = 0
    now = datetime.now()
    for rid, sym, ex, subj, kind, factor in conn.execute(
            "SELECT id, symbol, ex_date, subject, kind, factor FROM corporate_actions WHERE ex_date IS NOT NULL"):
        k = (kind or "").upper()
        cur = conn.execute("INSERT OR IGNORE INTO market_event (event_id,source,source_id,symbol,event_type,category,"
                           "event_date,known_at,direction,value,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"CA{rid}", "corporate_actions", str(rid), sym, k, CATEGORY.get(k, "CORPORATE_ACTION"),
                            ex, ex, None, factor, json.dumps({"subject": subj}), now))
        n += cur.rowcount
    for rid, sym, d, val, cnt in conn.execute("SELECT id, symbol, date, net_value_cr, deal_count FROM bulk_deals"):
        cur = conn.execute("INSERT OR IGNORE INTO market_event (event_id,source,source_id,symbol,event_type,category,"
                           "event_date,known_at,direction,value,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"BD{rid}", "bulk_deals", str(rid), sym, "BULK_DEAL", "FLOW", d, d,
                            "BUY" if (val or 0) > 0 else "SELL" if (val or 0) < 0 else None, val,
                            json.dumps({"deal_count": cnt}), now))
        n += cur.rowcount
    conn.commit()
    total = conn.execute("SELECT event_type, COUNT(*) FROM market_event GROUP BY event_type").fetchall()
    return {"inserted": n, "by_type": dict(total), "pending_types": PENDING_TYPES}


def add_event(conn, symbol, event_type, event_date, known_at, source="manual", value=None, payload=None) -> str:
    """Interface for future sources (earnings calendar, dividends, macro, index changes)."""
    et = event_type.upper()
    if et not in CATEGORY:
        raise ValueError(f"event_type must be one of {sorted(CATEGORY)}")
    if str(known_at) > str(date.today()):
        raise ValueError("known_at cannot be in the future")
    import hashlib
    eid = "EV" + hashlib.sha256(f"{source}|{symbol}|{et}|{event_date}".encode()).hexdigest()[:16].upper()
    conn.execute("INSERT OR IGNORE INTO market_event (event_id,source,source_id,symbol,event_type,category,event_date,"
                 "known_at,direction,value,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (eid, source, eid, symbol, et, CATEGORY[et], str(event_date), str(known_at), None, value,
                  json.dumps(payload or {}), datetime.now()))
    conn.commit()
    return eid


class EventHistory:
    """Point-in-time event features for W3 (only events with known_at <= as_of)."""

    def __init__(self, conn, end=None):
        self._by = {}
        q = "SELECT symbol, event_type, known_at, value FROM market_event" + (" WHERE known_at<=?" if end else "")
        for sym, et, k, v in conn.execute(q, [str(end)] if end else []):
            self._by.setdefault(sym, []).append((date.fromisoformat(str(k)[:10]), et, v))
        for v in self._by.values():
            v.sort(key=lambda e: e[0])          # values may be None: sort by date only

    def on(self, as_of, symbol) -> dict:
        ev = [e for e in self._by.get(symbol, []) if e[0] <= as_of]
        out = {}
        for t in FEATURE_TYPES:
            last = [e[0] for e in ev if e[1] == t.upper()]
            out[f"ev_days_since_{t}"] = (as_of - last[-1]).days if last else None
        out["ev_bulk_net_5d"] = sum((e[2] or 0) for e in ev if e[1] == "BULK_DEAL" and (as_of - e[0]).days <= 5) \
            if ev else None
        # W30 (QR-09): earnings / insider features (quant/event_sources.py)
        er = [e for e in ev if e[1] == "EARNINGS"]
        out["ev_earnings_growth"] = er[-1][2] if er else None
        ins = [e for e in ev if e[1] in ("INSIDER_BUY", "INSIDER_SELL") and (as_of - e[0]).days <= 90]
        out["ev_insider_net_90d"] = round(sum(e[2] or 0 for e in ins), 4) if ins else None
        return out
