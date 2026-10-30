"""
Auto-pick (owner, 29 Oct 2026): a free cash-pool session starts its next occurrence by itself.

A session with auto_pick on (orders/sessions.py) is free when it is RUNNING and not MANUAL, its last
occurrence has sold, no buy is queued in it, it has occurrences left and at least its minimum buy in
cash. ATIP then queues its next buy on the top-ranked ATIP BUY signal that no session holds or has
queued: the latest scored day's BUY signals by ATIP score, the order Signal Explorer shows them in.
With the owner's HOLD fallback set (set_hold_min_score; off by default), HOLD stocks scoring at least
that come after the BUY signals, highest score first.

On ATIP's own scores (27 Jul - 29 Oct 2026, each stock bought at the next open, sold at +3 % or
stopped at -2 %) BUY signals and HOLD stocks of every score band won 30-39 % of trades against the
~46 % that +2.7 % / -2.3 % net needs to break even: about -0.5 % a trade. HOLD >= 60 gives a median of
18 stocks a day (4 on the slowest), >= 62 six, >= 65 one.

It only queues the buy, as an entry rule (orders/rules.py) that fires once the price is no more than
PICK_BAND_PCT above where it was when picked. From there it is any other entry of the session: the
session gate, the confirmation tap unless the owner's auto send switch is on for an AUTO session, the
halt, funds and risk checks; sized from the session's whole cash, selling everything at +target and
stopping at -stop (link_rule applies the pool's rules).

Not picked: a stock the paper book or a session already holds or has queued; one an auto-pick has
already bought on the same day's signal (a BUY signal starts one occurrence, so a stock just sold at
+3 % is not bought straight back on yesterday's signal); one trading more than
GAP_PCT above its signal close (it has already run, as Signal Explorer warns); one whose scores are
more than MAX_SIGNAL_AGE_DAYS old; one the session's cash cannot buy its minimum of. A pick still
waiting at the end of its day is cancelled, and the next day's signals pick again.

PAPER only (owner): with any other broker_env nothing is picked. It runs from the dashboard's order
monitor (every 30 s in market hours, before check_triggers), or by hand:

    python -m orders.autopick            # one pass now: what it picked, or why each session waits
    python -m orders.autopick --dry-run  # the same, without queuing anything
"""

from __future__ import annotations

import logging
from datetime import date

from db.schema import get_connection

log = logging.getLogger("atip.autopick")

NOTE = "Auto-pick"                  # order_rules.notes of a pick: how the end-of-day cancel finds them
PICK_BAND_PCT = 0.5                 # the queued buy fires at up to 0.5 % above the price it was picked at
GAP_PCT = 3.0                       # dashboard/command_more.py GAP_WARN_PCT: a signal that has already run
MAX_SIGNAL_AGE_DAYS = 5             # a weekend and a holiday; older scores mean the pipeline stopped
QUEUED = ("ACTIVE", "PENDING_CONFIRMATION")
HOLD_SETTING = "autopick_hold_min_score"   # session_settings: the HOLD fallback's cut-off, OFF = BUY only


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def hold_min_score(conn) -> float | None:
    """The HOLD fallback's ATIP score cut-off, None while it is off (BUY signals only)."""
    from orders.sessions import ensure_tables
    ensure_tables(conn)
    r = conn.execute("SELECT value FROM session_settings WHERE name = ?", (HOLD_SETTING,)).fetchone()
    try:
        return float(_row(r)["value"]) if r else None
    except (TypeError, ValueError):                     # "OFF"
        return None


def set_hold_min_score(conn, value, reason: str = "") -> dict:
    """Switch the HOLD fallback on at an ATIP score cut-off (1-100), or off with None / "" / "off"."""
    from orders import sessions
    v = None if str(value if value is not None else "").strip().lower() in ("", "off", "none") else value
    if v is not None:
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError("hold_min_score must be an ATIP score from 1 to 100, or off")
        if not 0 < v <= 100:
            raise ValueError("hold_min_score must be an ATIP score from 1 to 100, or off")
    was = hold_min_score(conn)
    conn.execute("INSERT OR REPLACE INTO session_settings (name, value, updated_at) VALUES (?, ?, ?)",
                 (HOLD_SETTING, "OFF" if v is None else str(v), sessions._now()))
    sessions._event(conn, None, "AUTO_PICK_HOLD", {"from": was, "to": v, "reason": reason})
    conn.commit()
    return {"hold_min_score": v, "was": was}


def candidates(conn, today: date | None = None) -> dict:
    """{"date", "hold_min_score", "rows": [{symbol, score, signal, signal_price}], "reason"}: the latest
    scored day's BUY signals by ATIP score, then -- with the HOLD fallback on -- its HOLD stocks scoring
    at least the cut-off. _priced() adds the price now to the ones looked at."""
    today = today or date.today()
    cut = hold_min_score(conn)
    try:
        d = _row(conn.execute("SELECT MAX(date) AS d FROM ai_scores").fetchone())["d"]
    except Exception:
        d = None
    if not d:
        return {"date": None, "rows": [], "reason": "no ATIP scores yet"}
    if (today - date.fromisoformat(str(d)[:10])).days > MAX_SIGNAL_AGE_DAYS:
        return {"date": str(d), "rows": [], "reason": f"the latest ATIP scores are from {d}: too old to pick from"}
    hold = " OR (a.`signal` = 'HOLD' AND a.atip_score >= ?)" if cut else ""
    rows = [{"symbol": r["symbol"], "score": r["atip_score"], "signal": r["signal"],
             "signal_price": float(r["close"]) if r["close"] else None}
            for r in map(_row, conn.execute(
                "SELECT a.symbol, a.atip_score, a.`signal` AS `signal`, p.close FROM ai_scores a LEFT JOIN prices_daily p "
                f"ON p.symbol = a.symbol AND p.date = a.date WHERE a.date = ? AND (a.`signal` = 'BUY'{hold}) "
                "ORDER BY CASE a.`signal` WHEN 'BUY' THEN 0 ELSE 1 END, a.atip_score DESC",
                (d, cut) if cut else (d,)).fetchall())]
    what = f"BUY signal or HOLD stock scoring {cut:g}+" if cut else "BUY signal"
    return {"date": str(d), "hold_min_score": cut, "what": what, "rows": rows,
            "reason": None if rows else f"no {what} on {d}"}


def _priced(conn, c: dict) -> dict:
    """A candidate with its price now (live quote, else last close) and its gap on the signal close."""
    if "price" not in c:
        from orders.risk import reference_price
        price, src = reference_price(conn, c["symbol"])
        sp = c["signal_price"]
        c.update(price=price, price_source=src, gap_pct=round((price - sp) / sp * 100, 2) if price and sp else None)
    return c


def _busy(conn) -> set:
    """Symbols the paper book holds, or any session holds or has queued."""
    out = set()
    try:
        out |= {_row(r)["symbol"] for r in conn.execute("SELECT symbol FROM paper_position WHERE quantity > 0")}
    except Exception:                       # no paper trade yet
        pass
    try:
        for r in conn.execute("SELECT r.symbol, r.status, r.role FROM order_rules r "
                              "JOIN session_rules s ON s.rule_id = r.id").fetchall():
            r = _row(r)
            if (r["role"] or "ENTRY") == "ENTRY" and r["status"] in QUEUED:
                out.add(r["symbol"])
    except Exception:                       # order_rules not created yet
        pass
    from orders import sessions
    for s in sessions.list_sessions(conn)["sessions"]:
        for h in (s.get("pool") or {}).get("open") or []:
            out.add(h["symbol"])
    return out


def _traded_on(conn, signal_date) -> set:
    """Symbols an auto-pick has already bought on this signal day's BUY signals."""
    try:
        return {_row(r)["symbol"] for r in conn.execute(
            "SELECT symbol FROM order_rules WHERE status = 'EXECUTED' AND notes LIKE ?",
            (f"{NOTE} %: ATIP % {signal_date},%",)).fetchall()}
    except Exception:
        return set()


def _waiting_on(conn, s: dict) -> str | None:
    """None when the session is free for its next occurrence, else what it is waiting for."""
    from orders import sessions
    L = s["pool"]
    if s["status"] != "RUNNING":
        return f"session is {s['status']}"
    if s["mode"] == "MANUAL":
        return "session is MANUAL"
    if L["open"]:
        return f"occurrence #{L['occurrence']} holds {', '.join(h['symbol'] for h in L['open'])}"
    queued = [r for r in sessions._linked_rules(conn, s["id"])
              if (r.get("role") or "ENTRY") == "ENTRY" and r.get("status") in QUEUED]
    if queued:
        return f"a buy of {queued[0]['symbol']} is queued ({queued[0]['status'].lower().replace('_', ' ')})"
    if L["occurrence"] >= L["max_occurrences"]:
        return f"all {L['max_occurrences']} occurrences run"
    if L["cash"] < float(s.get("min_buy_value") or 0):
        return f"Rs {L['cash']:,.0f} cash, under its Rs {float(s['min_buy_value']):,.0f} minimum buy"
    return None


def cancel_stale(conn, today: date | None = None) -> list:
    """Cancel picks still waiting from an earlier day, so today's signals pick afresh."""
    from orders import rules, sessions
    today = str(today or date.today())
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT id, symbol, created_at FROM order_rules WHERE status = 'ACTIVE' AND notes LIKE ? "
            "AND substr(created_at, 1, 10) < ?", (f"{NOTE}%", today)).fetchall()]
    except Exception:
        return []
    for r in rows:
        rules.cancel_rule(r["id"])
        sid = sessions.session_of_rule(conn, r)
        sessions._event(conn, sid, "AUTO_PICK_CANCELLED", {"rule_id": r["id"], "symbol": r["symbol"],
                                                           "picked": str(r["created_at"])[:10]})
    conn.commit()
    return [r["id"] for r in rows]


def run(conn=None, dry_run: bool = False, today: date | None = None) -> list[dict]:
    """One pass over the auto-pick sessions: [{session, type PICKED | WAITING, ...}]. dry_run queues
    nothing and cancels nothing -- it is what the P&L page shows."""
    from orders import rules, sessions
    from orders.environment import PAPER, broker_env
    from orders.risk import halted
    own = conn is None
    conn = conn or get_connection()
    try:
        mine = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("auto_pick") and s.get("pool")]
        if not mine:
            return []
        if broker_env() != PAPER:
            return [{"session": s["id"], "type": "WAITING", "reason": "auto-pick is PAPER only"} for s in mine]
        h, _ = halted()
        if h:
            return [{"session": s["id"], "type": "WAITING", "reason": "trading is halted"} for s in mine]  # the banner says why
        if not dry_run:
            cancel_stale(conn, today)
            mine = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("auto_pick") and s.get("pool")]
        cands, used, busy, out = None, set(), _busy(conn), []
        for s in mine:
            wait = _waiting_on(conn, s)
            if wait:
                out.append({"session": s["id"], "type": "WAITING", "reason": wait})
                continue
            if cands is None:
                cands = candidates(conn, today)
                used = _traded_on(conn, cands["date"]) if cands["date"] else set()
            pick, passed = None, []
            for c in cands["rows"]:
                if c["symbol"] in busy:
                    passed.append(f"{c['symbol']} is taken")
                    continue
                if c["symbol"] in used:
                    passed.append(f"{c['symbol']} was already bought on this signal")
                    continue
                c = _priced(conn, c)
                if not c["price"]:
                    passed.append(f"{c['symbol']} has no price")
                elif c["gap_pct"] is not None and c["gap_pct"] > GAP_PCT:
                    passed.append(f"{c['symbol']} is {c['gap_pct']:+.1f}% above its signal close")
                else:
                    q = sessions.pool_quantity(conn, s["id"], c["price"])["quantity"]
                    if q < 1 or q * c["price"] < float(s.get("min_buy_value") or 0):
                        passed.append(f"{c['symbol']} at Rs {c['price']:,.0f} does not fit the cash")
                    else:
                        pick = c
                        break
            if not pick:
                more = f"; {len(passed) - 3} more" if len(passed) > 3 else ""
                out.append({"session": s["id"], "type": "WAITING",
                            "reason": f"no free {cands.get('what') or 'BUY signal'}" + (
                                f" ({'; '.join(passed[:3])}{more})" if passed
                                else f": {cands['reason']}" if cands["reason"] else "")})
                continue
            n = s["pool"]["occurrence"] + 1
            ev = {"session": s["id"], "type": "PICKED", "symbol": pick["symbol"], "occurrence": n,
                  "score": pick["score"], "signal": pick["signal"], "signal_date": cands["date"], "price": pick["price"],
                  "trigger": round(pick["price"] * (1 + PICK_BAND_PCT / 100), 2)}
            if not dry_run:
                r = rules.create_rule({
                    "symbol": pick["symbol"], "side": "BUY", "trigger_type": "PRICE", "trigger_value": ev["trigger"],
                    "reference_price": pick["price"], "trigger_direction": "BELOW",
                    "quantity_type": "SESSION", "quantity_value": 100, "product_type": "CNC", "order_type": "MARKET",
                    "require_confirmation": True,
                    "notes": f"{NOTE} {s['id']} occurrence #{n}: ATIP {pick['signal']} {cands['date']}, "
                             f"score {pick['score']}"})
                sessions.link_rule(conn, s["id"], r["id"])
                sessions._event(conn, s["id"], "AUTO_PICK", {**ev, "rule_id": r["id"]})
                conn.commit()
                ev["rule_id"] = r["id"]
                log.info(f"  auto-pick: {s['id']} occurrence #{n} -> {pick['symbol']} (ATIP {pick['score']}, "
                         f"buys at up to Rs {ev['trigger']:,.2f})")
            busy.add(pick["symbol"])
            out.append(ev)
        return out
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="python -m orders.autopick", description="Start free sessions' next occurrence")
    ap.add_argument("--dry-run", action="store_true", help="show what would be picked; queue nothing")
    ap.add_argument("--hold-min-score", metavar="SCORE|off",
                    help="when no BUY signal is free, also pick HOLD stocks scoring at least this (off = BUY only)")
    args = ap.parse_args()
    if args.hold_min_score is not None:
        c = get_connection()
        try:
            print(set_hold_min_score(c, args.hold_min_score, reason="command line"))
        finally:
            c.close()
    for e in run(dry_run=args.dry_run) or [{"session": "-", "type": "-", "reason": "no session has auto-pick on"}]:
        print(f"  {e['session']:<4} {e['type']:<8} " + (
            f"{e['symbol']} for occurrence #{e['occurrence']} (ATIP {e['signal']} {e['score']}), "
            f"buys at up to Rs {e['trigger']:,.2f}"
            if e["type"] == "PICKED" else e.get("reason", "")))
