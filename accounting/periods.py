"""
P&L by period (UI fixes v2, H1, 3 Nov 2026): the one source of the money figures Home and Accounting show, so the
two pages reconcile by construction.

    periods(conn) -> {"today": P, "month": P, "since_start": P, "sessions": {sid: {"today": .., "since_start": ..}},
                      "as_of": ISO time}
        P = {"realised": actual P&L of the cycles CLOSED in the period (after charges and estimated tax),
             "gross": their gross P&L, "charges", "tax_est", "cycles": how many closed,
             "open": the unrealised P&L of the trades open now (mark to market),
             "total": realised + open}

    "Today" is the calendar day in IST (the machine's clock is IST), "This month" the calendar month, "Since start"
    each session's current books (from its pool_started_at) -- the same rows the Sessions page and the session
    ledger use (accounting/cycles.py stored()). Open trades count in every period at their current mark: an open
    trade's P&L belongs to "now", and leaving it out of Today is exactly how 3 Nov's Home said ₹0.00 beside a
    session that was −₹589.74.
Read-only: nothing here writes, places or changes anything.
"""

from __future__ import annotations

from datetime import date, datetime

PERIODS = (("today", "Today"), ("month", "This month"), ("since_start", "Since start"))


def _r2(v) -> float:
    return round(float(v or 0) + 0.0, 2)


def _bucket(rows: list, unrealised: float) -> dict:
    out = {"realised": _r2(sum(float(r.get("actual_pnl") or 0) for r in rows)),
           "gross": _r2(sum(float(r.get("gross_pnl") or 0) for r in rows)),
           "charges": _r2(sum(float(r.get("charges") or 0) for r in rows)),
           "tax_est": _r2(sum(float(r.get("tax_est") or 0) for r in rows)),
           "cycles": len(rows), "open": _r2(unrealised)}
    out["total"] = _r2(out["realised"] + out["open"])
    return out


def periods(conn, overview: dict | None = None, today: date | None = None) -> dict:
    from accounting import cycles as C
    ov = overview if overview is not None else C.overview(conn)
    d = (today or date.today()).isoformat()
    m = d[:7]
    by_sid, opens = {}, {}
    for s in ov["sessions"]:
        if not s.get("tiles"):
            continue
        since = (s.get("setup") or {}).get("pool_started_at")
        by_sid[s["id"]] = C.stored(conn, s["id"], since)
        opens[s["id"]] = float(s["tiles"].get("unrealised") or 0)
    every = [r for rows in by_sid.values() for r in rows]
    closed_on = lambda r: str(r.get("closed_at") or "")[:10]
    unreal = sum(opens.values())
    out = {"today": _bucket([r for r in every if closed_on(r) == d], unreal),
           "month": _bucket([r for r in every if closed_on(r)[:7] == m], unreal),
           "since_start": _bucket(every, unreal),
           "sessions": {}, "as_of": datetime.now().isoformat(timespec="seconds"), "date": d}
    for sid, rows in by_sid.items():
        out["sessions"][sid] = {"today": _bucket([r for r in rows if closed_on(r) == d], opens[sid]),
                                "since_start": _bucket(rows, opens[sid])}
    return out


def ledger_realised(conn, frm: str | None = None, to: str | None = None, overview: dict | None = None) -> float:
    """The same realised figure read back from the general ledger (accounting/ledger.py): trading gains minus trading
    losses, charges and estimated tax, posted from each current pool's start and within [frm, to] (ISO dates). It
    must equal periods()'s realised for the same span -- the reconciliation Accounting shows and the tests check."""
    from accounting import cycles as C
    from accounting import ledger as LG
    LG.ensure_tables(conn)
    ov = overview if overview is not None else C.overview(conn)
    total = 0.0
    for s in ov["sessions"]:
        if not s.get("tiles"):
            continue
        sid = s["id"]
        since = str((s.get("setup") or {}).get("pool_started_at") or "")
        q = ("SELECT account, SUM(debit) AS d, SUM(credit) AS c FROM ledger_entries WHERE session_id = ? AND source = 'CYCLE' "
             "AND kind IN ('SELL', 'CHARGE', 'TAX_PROVISION') AND (account LIKE 'Trading %' OR account LIKE 'Charges:%' "
             "OR account = 'Tax expense (est.)')")
        a = [sid]
        if since:
            q += " AND source_id IN (SELECT rule_id FROM session_cycles WHERE session_id = ? AND pool_started_at = ?)"
            a += [sid, since]
        if frm:
            q += " AND substr(at, 1, 10) >= ?"; a.append(str(frm)[:10])
        if to:
            q += " AND substr(at, 1, 10) <= ?"; a.append(str(to)[:10])
        for r in conn.execute(q + " GROUP BY account", tuple(a)).fetchall():
            r = dict(r) if hasattr(r, "keys") else {"account": r[0], "d": r[1], "c": r[2]}
            total += float(r["c"] or 0) - float(r["d"] or 0)
    return _r2(total)


def reconcile(conn) -> dict:
    """periods() beside the ledger's realised figure for each period: {"rows": [...], "ok": bool}."""
    from accounting import cycles as C
    ov = C.overview(conn)
    p = periods(conn, ov)
    d = p["date"]
    spans = {"today": (d, d), "month": (d[:7] + "-01", d), "since_start": (None, None)}
    rows = []
    for key, label in PERIODS:
        led = ledger_realised(conn, *spans[key], overview=ov)
        rows.append({"period": label, "key": key, **p[key], "ledger": led,
                     "match": abs(led - p[key]["realised"]) < 0.01})
    return {"rows": rows, "ok": all(r["match"] for r in rows), "periods": p}
