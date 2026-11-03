"""
The ten-session board (SES-09, 3 Nov 2026): one row per session, always ten, with the reason when fewer
than ten are filled.

Why this exists. The ten sessions S1-S10 are mini-funds; each holds (or has queued) one stock at a time,
picked by orders/autopick.py. On 3 Nov 2026 the day's scoring produced no BUY signal (0 of 499) and auto-pick
used to buy BUY signals only, so every session sat waiting and the screen showed nothing to look at. The count
did not drop in the API, the page or the apps: it dropped at the pick, and nothing said so. autopick now
rank-fills (see its header) and this board states the result:

    "7 of 10 sessions filled -- S3 waits on ..." / "10 of 10 sessions filled (4 on BUY signals, 6 rank-filled)"

Each row: symbol, entry rationale, score snapshot (the scored day's ATIP / VPI / MRI / RRI / ZPI / CRI / ACS and
signal), entry / target / stop, status, P&L and the number of successful (winning, closed) trades. Read-only.

    GET /api/sessions/board        (dashboard/command_routes.py)      python -m orders.session_board
"""

from __future__ import annotations

import json
import re
from datetime import date

TARGET = 10
SCORE_COLS = ("atip_score", "vpi", "mri", "rri", "zpi", "cri", "acs", "signal")
_NOTE = re.compile(r"ATIP (\w+) (\d{4}-\d{2}-\d{2}), score ([\d.]+)(?:, ([A-Z-]+))?")


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def _snapshot(conn, symbol, on) -> dict | None:
    if not symbol:
        return None
    r = conn.execute("SELECT date, atip_score, vpi, mri, rri, zpi, cri, acs, `signal` AS `signal` FROM ai_scores "
                     "WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1", (symbol, str(on or "9999-12-31"))).fetchone()
    if not r:
        return None
    d = _row(r)
    return {"date": str(d["date"])[:10], "atip": d["atip_score"], "vpi": d["vpi"], "mri": d["mri"], "rri": d["rri"],
            "zpi": d["zpi"], "cri": d["cri"], "acs": d["acs"], "signal": d["signal"],
            "cri_note": "lower is better"}


def _rationale(note: str | None, snap: dict | None) -> str:
    m = _NOTE.search(note or "")
    if m:
        sig, d, score, source = m.groups()
        base = {"BUY": f"ATIP BUY signal on {d}, score {score}",
                "RANK-FILL": f"no BUY signal free: next-ranked eligible stock on {d} (ATIP {sig} {score}, lowest CRI among equals)",
                "HOLD-CUT": f"ATIP HOLD {score} on {d}, above the owner's HOLD cut-off"}.get(
                    source or ("BUY" if sig == "BUY" else "HOLD-CUT"), f"ATIP {sig} {score} on {d}")
        return base
    if snap:
        return f"ATIP {snap['signal']} {snap['atip']} on {snap['date']}"
    return ""


def _ranks(conn) -> tuple[dict, int, str | None]:
    """({symbol: rank}, how many were scored, the day): the latest scored day ranked the way auto-pick ranks,
    ATIP highest first, then CRI lowest first among equals (CRI is lower-is-better)."""
    try:
        d = _row(conn.execute("SELECT MAX(date) AS d FROM ai_scores").fetchone())["d"]
        if not d:
            return {}, 0, None
        rows = conn.execute("SELECT symbol FROM ai_scores WHERE date = ? AND atip_score IS NOT NULL "
                            "ORDER BY atip_score DESC, (cri IS NULL), cri ASC, symbol", (d,)).fetchall()
    except Exception:
        return {}, 0, None
    syms = [_row(r)["symbol"] for r in rows]
    return {s: i + 1 for i, s in enumerate(syms)}, len(syms), str(d)[:10]


def board(conn, today: date | None = None, run_dry: bool = True) -> dict:
    from orders import autopick, sessions as S
    today = today or date.today()
    ls = S.list_sessions(conn)["sessions"]
    ranks, n_ranked, rank_day = _ranks(conn)
    plan = {}
    if run_dry:
        try:
            plan = {e["session"]: e for e in autopick.run(conn, dry_run=True, today=today)}
        except Exception as e:                      # the board must still draw
            plan = {"_error": {"reason": f"pick preview unavailable: {e}"}}
    rows, sources = [], {"BUY": 0, "HOLD-CUT": 0, "RANK-FILL": 0}
    for s in ls[:TARGET]:
        pool = s.get("pool") or {}
        held = (pool.get("open") or [None])[0]
        queued = next((r for r in S._linked_rules(conn, s["id"])
                       if (r.get("role") or "ENTRY") == "ENTRY" and r.get("status") in autopick.QUEUED), None)
        pick = plan.get(s["id"]) if not held and not queued else None
        row = {"session": s["id"], "name": s["name"], "mode": s["mode"], "state": s["status"],
               "target_pct": s.get("target_pct"), "stop_pct": s.get("stop_pct"),
               "pnl": pool.get("pnl"), "booked": pool.get("booked"), "unrealised": pool.get("unrealised"),
               "successful_trades": pool.get("wins"), "closed_trades": pool.get("closed"),
               "trades": pool.get("trades"), "win_rate_pct": pool.get("win_rate_pct")}
        tp, sp = float(s.get("target_pct") or 0), float(s.get("stop_pct") or 0)
        note = None
        if held:
            rule = next((r for r in S._linked_rules(conn, s["id"]) if r["id"] == held.get("rule_id")), {})
            note = rule.get("notes")
            e0 = float(held["entry"])
            row.update(symbol=held["symbol"], status="HOLDING",
                       entry=round(e0, 2), target=held.get("sell_at") or round(e0 * (1 + tp / 100), 2),
                       stoploss=held.get("stop") or round(e0 * (1 - sp / 100), 2), mark=held.get("mark"),
                       open_pnl=held.get("unrealised"))
        elif queued:
            note = queued.get("notes")
            e0 = float(queued.get("trigger_value") or queued.get("reference_price") or 0) or None
            row.update(symbol=queued["symbol"], status="QUEUED" if queued["status"] == "ACTIVE" else "AWAITING CONFIRMATION",
                       entry=e0, target=round(e0 * (1 + tp / 100), 2) if e0 else None,
                       stoploss=round(e0 * (1 - sp / 100), 2) if e0 else None)
        elif pick and pick.get("type") == "PICKED":
            e0 = float(pick["price"])
            note = f"ATIP {pick['signal']} {pick['signal_date']}, score {pick['score']}, {pick.get('source')}"
            row.update(symbol=pick["symbol"], status="NEXT PICK" + (" (after the stop window)" if pick.get("held") else ""),
                       entry=round(e0, 2), target=round(e0 * (1 + tp / 100), 2), stoploss=round(e0 * (1 - sp / 100), 2))
        else:
            why = ((pick or {}).get("reason") or (plan.get("_error") or {}).get("reason")
                   or ("no starting amount: set one up on /sessions" if not s.get("pool")
                       else "auto-pick is off for this session" if not s.get("auto_pick") else None)
                   or f"session is {s['status']}")
            row.update(symbol=None, status="WAITING", entry=None, target=None, stoploss=None, reason=why)
        row["score_snapshot"] = _snapshot(conn, row.get("symbol"), None)
        # Task 1 (3 Nov 2026, 10:54 PM brief): the stock's rank on the latest scored day, in auto-pick's order
        row["rank"] = ranks.get(row.get("symbol")) if row.get("symbol") else None
        row["rank_of"] = n_ranked if row["rank"] else None
        row["rationale"] = _rationale(note, row["score_snapshot"]) if row.get("symbol") else row.get("reason", "")
        m = _NOTE.search(note or "")
        src = (m.group(4) if m and m.group(4) else ("BUY" if m and m.group(1) == "BUY" else "HOLD-CUT")) if m else None
        row["source"] = src
        if row.get("symbol") and src in sources:
            sources[src] += 1
        rows.append(row)
    filled = sum(1 for r in rows if r.get("symbol"))
    # "filled" counts a session with a stock assigned: held, queued, or the next pick a dry run plans. Say which, so
    # ten planned picks overnight are never read as ten positions.
    counts = {"holding": sum(1 for r in rows if r["status"] == "HOLDING"),
              "queued": sum(1 for r in rows if r["status"] in ("QUEUED", "AWAITING CONFIRMATION")),
              "planned": sum(1 for r in rows if str(r["status"]).startswith("NEXT PICK")),
              "waiting": sum(1 for r in rows if r["status"] == "WAITING")}
    why = None
    if filled < TARGET:
        waits = [r for r in rows if not r.get("symbol")]
        reasons = {}
        for r in waits:
            reasons.setdefault(r.get("reason") or r["state"], []).append(r["session"])
        why = "; ".join(f"{', '.join(v)}: {k}" for k, v in list(reasons.items())[:3])
    cands = None
    try:
        cands = autopick.candidates(conn, today)
    except Exception:
        pass
    parts = [f"{sources['BUY']} on BUY signals"] if sources["BUY"] else []
    if sources["HOLD-CUT"]:
        parts.append(f"{sources['HOLD-CUT']} on HOLD above the cut-off")
    if sources["RANK-FILL"]:
        parts.append(f"{sources['RANK-FILL']} rank-filled (no BUY signal free)")
    msg = f"{filled} of {TARGET} sessions filled" + (f" ({', '.join(parts)})" if parts and filled else "") + (
        f" — {why}" if why else "")
    held = [f"{counts['holding']} holding", f"{counts['queued']} queued"] + (
        [f"{counts['planned']} to buy at the next pick"] if counts["planned"] else [])
    detail = " · ".join(held)
    return {"target": TARGET, "filled": filled, "message": msg, "detail": detail, "counts": counts,
            "complete": filled >= TARGET, "rows": rows, "rank_day": rank_day, "ranked": n_ranked,
            "signal_day": (cands or {}).get("date"), "buy_signals": (cands or {}).get("n_signal"),
            "rank_fill_candidates": (cands or {}).get("n_fill"), "rank_fill": autopick.rank_fill_enabled(conn),
            "cri_direction": "lower is better (ranked ascending)"}


if __name__ == "__main__":
    from db.schema import get_connection
    c = get_connection()
    try:
        b = board(c)
        print(b["message"])
        for r in b["rows"]:
            print(f"  {r['session']:<4}{(r.get('symbol') or '-'):<12}{r['status']:<24}"
                  f"{r.get('entry') or '':>10}{r.get('target') or '':>10}{r.get('stoploss') or '':>10}  "
                  f"P&L {r.get('pnl')}  wins {r.get('successful_trades')}/{r.get('closed_trades')}  {r['rationale']}")
    finally:
        c.close()
