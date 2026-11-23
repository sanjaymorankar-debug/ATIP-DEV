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

16 Nov 2026 (KGF, the 9-13 Nov review): in 87 % of the picks the "price" was the signal day's close, and a MARKET
buy fired anywhere up to 0.5 % above it however far below the stock traded (HCLTECH -2.81 %). Now a pick needs an LTP
under 60 s old (else `waiting_for_ltp`, nothing queued), buys with a LIMIT at min(LTP, signal close) + 0.5 % rounded
down to the tick, skips a stock already max_intraday_drop_pct (1.5 %) under its signal close (`skipped_falling`), and
queues nothing after the session's cut-off (14:45 IST) or before the 08:45 pre-open health gate passes. Each session
follows its arm (orders/arms.py): the breadth regime gate, its stop / trail / target, a risk-budget size, an industry
cap, and for the Kite-retest arms a DUMMY- resting buy at a setup's breakout low (orders/algo_gtt.py) instead of rank-fill.

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
NOTE_RETEST = "Algo retest"         # a Kite retest entry: rests for days (orders/algo_gtt.py), never cancelled at the open
PICK_BAND_PCT = 0.5                 # the queued buy fires at up to 0.5 % above the price it was picked at
LTP_MAX_AGE_S = 60                  # KGF-02: a pick is priced on an LTP at most this old, never on the signal-day close
SOURCE_RETEST = "KITE-RETEST"
GAP_PCT = 3.0                       # dashboard/command_more.py GAP_WARN_PCT: a signal that has already run
MAX_SIGNAL_AGE_DAYS = 5             # a weekend and a holiday; older scores mean the pipeline stopped
QUEUED = ("ACTIVE", "PENDING_CONFIRMATION")
HOLD_SETTING = "autopick_hold_min_score"   # session_settings: the HOLD fallback's cut-off, OFF = BUY only
# SES-09 (3 Nov 2026): rank-fill. A session is never left empty because the day had fewer BUY signals than
# sessions (3 Nov 2026: 0 BUY of 499 scored, so all ten waited). After the BUY signals (and the HOLD cut-off
# above, when the owner has set one) the next-ranked ELIGIBLE stocks fill the free sessions: ATIP score
# highest first, lower CRI first among equals (CRI is lower-is-better), never a SELL or WAIT signal, a CRI at
# or above SELL_CRI_DANGER, a price under FILL_MIN_PRICE, or a 20-day turnover under FILL_MIN_TURNOVER.
# Every such pick is labelled RANK-FILL in its rule note and on the board; the board says "N of 10 sessions
# filled" and why. Off with session_settings autopick_rank_fill = OFF.
FILL_SETTING = "autopick_rank_fill"
FILL_MIN_PRICE = 20.0
FILL_MIN_TURNOVER = 2e7             # Rs 2 crore a day (20-day average close x volume)
SOURCE_BUY, SOURCE_HOLD, SOURCE_FILL = "BUY", "HOLD-CUT", "RANK-FILL"


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


def rank_fill_enabled(conn) -> bool:
    """On unless the owner set session_settings autopick_rank_fill to OFF."""
    from orders.sessions import ensure_tables
    ensure_tables(conn)
    r = conn.execute("SELECT value FROM session_settings WHERE name = ?", (FILL_SETTING,)).fetchone()
    return not (r and str(_row(r)["value"]).strip().upper() == "OFF")


def _fill_rows(conn, d, skip: set) -> list:
    """The next-ranked eligible stocks on score day d (see the rank-fill note above), best first."""
    from scores.engine import SELL_CRI_DANGER
    out = []
    for r in map(_row, conn.execute(
            "SELECT a.symbol, a.atip_score, a.cri, a.`signal` AS `signal`, p.close, "
            "(SELECT AVG(q.close * q.volume) FROM (SELECT close, volume FROM prices_daily WHERE symbol = a.symbol "
            " AND date <= a.date AND series = 'EQ' ORDER BY date DESC LIMIT 20) q) AS turnover "
            "FROM ai_scores a LEFT JOIN prices_daily p ON p.symbol = a.symbol AND p.date = a.date AND p.series = 'EQ' "
            "WHERE a.date = ? AND a.`signal` = 'HOLD' AND a.atip_score IS NOT NULL "
            "ORDER BY a.atip_score DESC, (a.cri IS NULL), a.cri ASC, a.symbol", (d,)).fetchall()):
        if r["symbol"] in skip or not r["close"]:
            continue
        if r["cri"] is not None and r["cri"] >= SELL_CRI_DANGER:       # CRI: lower is better
            continue
        if float(r["close"]) < FILL_MIN_PRICE or float(r["turnover"] or 0) < FILL_MIN_TURNOVER:
            continue
        out.append({"symbol": r["symbol"], "score": r["atip_score"], "signal": r["signal"], "signal_price": float(r["close"]),
                    "cri": r["cri"], "source": SOURCE_FILL})
    return out


def _sv2_nifty_gap(conn) -> str | None:
    """Strategy v2's Nifty opening-gap filter (orders/strategy_v2.py); None while v2 is off."""
    try:
        from orders import strategy_v2 as SV2
        return SV2.nifty_gap_block(conn, date.today().isoformat())
    except Exception as e:                          # noqa: BLE001 -- a filter that cannot be read never blocks
        log.warning(f"  strategy v2 Nifty gap filter unavailable: {e}")
        return None


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
    # LRCA-07 (19 Nov 2026, strategy v2 freshness_gate, OFF unless config.json turns it on): 5 calendar days let a stalled
    # pipeline trade three sessions on one day's signals; v2 takes only the previous session's (or newer) scores.
    from orders import strategy_v2 as SV2
    if SV2.on("freshness_gate"):
        why = SV2.stale(d, SV2.value("max_signal_age_sessions") or 1, today)
        if why:
            return {"date": str(d), "rows": [], "reason": f"strategy v2: the latest ATIP scores are stale ({why})"}
    hold = " OR (a.`signal` = 'HOLD' AND a.atip_score >= ?)" if cut else ""
    rows = [{"symbol": r["symbol"], "score": r["atip_score"], "signal": r["signal"],
             "signal_price": float(r["close"]) if r["close"] else None,
             "source": SOURCE_BUY if r["signal"] == "BUY" else SOURCE_HOLD}
            for r in map(_row, conn.execute(
                "SELECT a.symbol, a.atip_score, a.`signal` AS `signal`, p.close FROM ai_scores a LEFT JOIN prices_daily p "
                f"ON p.symbol = a.symbol AND p.date = a.date WHERE a.date = ? AND (a.`signal` = 'BUY'{hold}) "
                "ORDER BY CASE a.`signal` WHEN 'BUY' THEN 0 ELSE 1 END, a.atip_score DESC",
                (d, cut) if cut else (d,)).fetchall())]
    what = f"BUY signal or HOLD stock scoring {cut:g}+" if cut else "BUY signal"
    n_signal = len(rows)
    filled = []
    fill_on = rank_fill_enabled(conn)
    if fill_on:
        filled = _fill_rows(conn, d, {r["symbol"] for r in rows})
        rows = rows + filled
    return {"date": str(d), "hold_min_score": cut, "what": what, "rows": rows, "n_signal": n_signal,
            "n_fill": len(filled), "rank_fill": fill_on,
            "fill_note": (f"rank-fill: {len(filled)} eligible stock(s) on {d} (HOLD, CRI under the sell level, "
                          f"price Rs {FILL_MIN_PRICE:g}+, 20-day turnover Rs {FILL_MIN_TURNOVER / 1e7:g} crore+)")
                         if fill_on else "rank-fill is off",
            "reason": None if rows else f"no {what} on {d}"}


def fresh_ltp(conn, symbols, max_age_s: int = LTP_MAX_AGE_S, fetch: bool = True) -> dict:
    """{symbol: (ltp, timestamp, source)} for LTPs at most max_age_s old: the newest live_quotes row (the Dhan
    WebSocket and the REST snapshots write it), else one quote call for the rest. KGF-02 (16 Nov 2026): in 87 % of the
    auto-picks the "price" was the signal day's close (risk.reference_price falls back to it with no fresh quote), so
    the buy fired at anything up to 0.5 % over yesterday's close however far under it the stock traded."""
    from datetime import datetime, timedelta
    syms = sorted({s for s in symbols if s})
    if not syms:
        return {}
    since = (datetime.now() - timedelta(seconds=max_age_s)).strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    try:
        for r in conn.execute(
                f"SELECT q.symbol, q.ltp, q.timestamp FROM live_quotes q JOIN (SELECT symbol, MAX(timestamp) ts FROM "
                f"live_quotes WHERE symbol IN ({','.join('?' * len(syms))}) AND REPLACE(SUBSTR(timestamp,1,19),'T',' ') >= ? "
                f"GROUP BY symbol) m ON m.symbol = q.symbol AND m.ts = q.timestamp", (*syms, since)).fetchall():
            r = _row(r)
            if r["ltp"]:
                out[r["symbol"]] = (float(r["ltp"]), str(r["timestamp"]), "live_quotes")
    except Exception as e:                  # no live_quotes table yet
        log.debug(f"  fresh LTP from live_quotes: {e}")
    missing = [s for s in syms if s not in out]
    if missing and fetch:
        try:
            from data.dhan import fetch_live_quotes
            q = fetch_live_quotes(missing)
            now = datetime.now().isoformat(timespec="seconds")
            for _, r in (q.iterrows() if q is not None and not q.empty else []):
                if r.get("ltp"):
                    ts = r.get("timestamp") if "timestamp" in q.columns else None
                    ts = str(ts) if ts is not None and str(ts) not in ("", "NaT", "None") else now
                    if ts.replace("T", " ")[:19] >= since:
                        out[str(r["symbol"])] = (float(r["ltp"]), ts, "quote")
        except Exception as e:
            log.debug(f"  fresh LTP quote call failed: {e}")
    return out


def _priced(conn, c: dict, ltps: dict | None = None, stale_ok: bool = False) -> dict:
    """A candidate with its price now and its gap on the signal close. KGF-02: the price is a fresh LTP (<= 60 s) or
    nothing; stale_ok (the dry run, the board) shows the last reference price instead, flagged ltp_fresh False."""
    if "price" not in c:
        sp = c["signal_price"]
        hit = (ltps or {}).get(c["symbol"])
        if hit is None and ltps is None:
            hit = fresh_ltp(conn, [c["symbol"]]).get(c["symbol"])
        if hit:
            price, ts, src = hit
            c.update(ltp_fresh=True, pick_ltp=price, pick_ltp_ts=ts)
        elif stale_ok:
            from orders.risk import reference_price
            price, src = reference_price(conn, c["symbol"])
            c.update(ltp_fresh=False, pick_ltp=None, pick_ltp_ts=None)
        else:
            price, src = None, None
            c.update(ltp_fresh=False, pick_ltp=None, pick_ltp_ts=None)
        c.update(price=price, price_source=src, gap_pct=round((price - sp) / sp * 100, 2) if price and sp else None)
    return c


def entry_limit(symbol: str, ltp: float, signal_close: float | None) -> float:
    """KGF-02: the rank-fill buy is a LIMIT at min(LTP, signal close) x (1 + PICK_BAND_PCT %), tick-rounded down."""
    from orders.ticks import mul, to_tick
    base = min(ltp, signal_close) if signal_close else ltp
    return to_tick(mul(base, 1 + PICK_BAND_PCT / 100), "BUY", "ENTRY", symbol=symbol)


def _note_once(conn, sid, event: str, key: str, detail: dict) -> None:
    """A session event at most once a day per (session, event, key): the gate and the skips are measured by the
    weekly A/B report (portfolio/ab_report.py), not logged every 30 s."""
    from orders import sessions
    day = date.today().isoformat()
    try:
        hit = conn.execute("SELECT 1 FROM session_events WHERE session_id IS ? AND event = ? AND substr(at, 1, 10) = ? "
                           "AND detail LIKE ? LIMIT 1", (sid, event, day, f'%"key": "{key}"%')).fetchone()
    except Exception:
        hit = None
    if not hit:
        sessions._event(conn, sid, event, {**detail, "key": key})
        conn.commit()


def _note_skip(conn, sid, kind: str, symbol: str, why: str, dry_run: bool) -> None:
    """A sector-cap or cooldown skip, once a day per (session, symbol), so the weekly A/B report counts it with the
    falling and no-LTP skips (16 Nov 2026: these two were only in the WAITING reason)."""
    if not dry_run:
        _note_once(conn, sid, "AUTO_PICK_SKIP", f"{kind}:{symbol}",
                   {"status": f"skipped_{kind}", "symbol": symbol, "reason": why})


def _cooled(conn, symbol: str, minutes) -> str | None:
    """KGF-19: the symbol's stop that fired less than `minutes` ago (INDHOTEL was bought again 31 s after its stop on
    11 Nov; S7 ICICIBANK 31 s after its stop on 23 Nov). `minutes` comes from arms.setting: 375 (the rest of the
    day) for rank-fill sessions unless the arm sets reentry_cooldown_min; 0 / None = off."""
    if not minutes:
        return None
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(minutes=float(minutes))).isoformat()
    try:
        r = conn.execute("SELECT updated_at FROM order_rules WHERE symbol = ? AND role = 'STOP' AND status = 'EXECUTED' "
                         "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1", (symbol, since)).fetchone()
    except Exception:
        return None
    return f"{symbol} was stopped out at {str(_row(r)['updated_at'])[11:16]} (cooldown {float(minutes):g} min)" if r else None


def _sector_full(symbol: str, busy: set, cap) -> str | None:
    """KGF-13 S8: at most `cap` open or queued positions per NSE industry across the sessions."""
    if not cap:
        return None
    from orders.entry_guards import _industry
    ind = _industry(symbol)
    if not ind:
        return None
    same = sorted(s for s in busy if s != symbol and _industry(s) == ind)
    return f"{symbol}: {ind} already has {len(same)} ({', '.join(same)}; cap {int(cap)})" if len(same) >= int(cap) else None


def _health(conn) -> dict:
    """KGF-06: the 08:45 IST pre-open health gate (ops/preopen_health.py); open when that module is not installed."""
    try:
        from ops.preopen_health import gate_status
    except ImportError:
        return {"ok": True, "reason": None}
    try:
        return gate_status(conn) or {"ok": True, "reason": None}
    except Exception as e:
        return {"ok": False, "reason": f"pre-open health gate unreadable ({e})"}


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
    try:                                    # a DUMMY- resting buy made from the API, not linked to a session
        out |= {_row(r)["symbol"] for r in conn.execute(
            "SELECT symbol FROM algo_dummy_gtt WHERE kind = 'BUY' AND status = 'ACTIVE'").fetchall()}
    except Exception:
        pass
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
    # Review 3 Nov 2026 (orders M3): the same free cash and floor sessions.gate() holds an entry to (the floor rule's
    # floor; the cash less any tax reserve), so a pick is never queued that the gate would then hold all day.
    floor = float(L.get("floor", s.get("min_buy_value")) or 0)
    free = float(L.get("investable_cash", L["cash"]))
    if floor and free < floor:
        return f"Rs {free:,.0f} free cash, under its Rs {floor:,.0f} minimum buy"
    # Review 3 Nov 2026 (orders M2): an entry that FAILED today (a risk limit, sizing, no price) is not re-picked every
    # 30 s -- that queued the same failing buy ~750 times a day. The session waits for tomorrow and says why.
    today = date.today().isoformat()
    failed = [r for r in sessions._linked_rules(conn, s["id"])
              if (r.get("role") or "ENTRY") == "ENTRY" and r.get("status") == "FAILED"
              and str(r.get("updated_at") or "")[:10] == today]
    if failed:
        return (f"today's buy of {failed[-1]['symbol']} failed ({str(failed[-1].get('execution_error') or 'no reason')[:90]}); "
                "auto-pick tries again on the next trading day")
    # Verification 4 Nov 2026 (VF-04): with the day's BUY count used up (risk_limits.max_orders_per_day), the risk check
    # refuses every buy at the send. On 4 Nov the ten 09:15 buys used the 10, and three sessions that sold at a profit
    # each queued NEULANDLAB, which FAILED. Wait instead of queuing a buy that cannot be placed. The limit is unchanged.
    from orders.risk import _orders_today, limits
    cap = limits().get("max_orders_per_day")
    if cap and _orders_today(conn) >= cap:
        return (f"today's {cap:g} buys are used (risk limit max_orders_per_day); "
                "auto-pick tries again on the next trading day")
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
        # RCA-13 (6 Nov 2026): commit each event before the next cancel_rule, which writes on its own connection.
        # With the commit after the loop, two picks left from 5 Nov made the second cancel_rule wait the whole busy
        # timeout (60 s) behind this connection's uncommitted event and raise 'database is locked': the order
        # monitor's 09:15 pass lost a minute, check_triggers with it, and the first event was rolled back.
        conn.commit()
    return [r["id"] for r in rows]


def run(conn=None, dry_run: bool = False, today: date | None = None, now=None) -> list[dict]:
    """One pass over the auto-pick sessions: [{session, type PICKED | WAITING, ...}]. dry_run queues
    nothing and cancels nothing -- it is what the P&L page shows.

    16 Nov 2026 (KGF-02/03/06/09/13): before a pick, the pre-open health gate (08:45 IST), the session's entry cut-off
    (14:45 IST) and its regime gate (breadth, orders/arms.py) must let it through; a rank-fill pick needs a fresh LTP
    and buys with a tick-rounded LIMIT at min(LTP, signal close) + 0.5 %, skipping a stock already falling more than
    max_intraday_drop_pct under its signal close; a Kite-retest arm rests a DUMMY- buy at a setup's breakout low."""
    from datetime import datetime
    from orders import arms, entry_guards, rules, sessions
    from orders.environment import PAPER, broker_env
    from orders.risk import halted
    own = conn is None
    conn = conn or get_connection()
    now = now or datetime.now()
    try:
        mine = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("auto_pick") and s.get("pool")]
        if not mine:
            return []
        if broker_env() != PAPER and not dry_run:
            return [{"session": s["id"], "type": "WAITING", "reason": "auto-pick is PAPER only"} for s in mine]
        h, why = halted()
        if h and not dry_run:
            return [{"session": s["id"], "type": "WAITING", "reason": "trading is halted"} for s in mine]  # the banner says why
        # dry_run (the board, the P&L page, --dry-run) still works out who WOULD be picked while halted, so "10 of 10"
        # is not hidden behind the overnight stop window; the held flag says the buy waits for the halt to lift.
        halted_note = why if h else None
        if not dry_run:
            cancel_stale(conn, today)
            mine = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("auto_pick") and s.get("pool")]
        cands, used, busy, out, ltps, health = None, set(), _busy(conn), [], None, None
        for s in mine:
            sid = s["id"]
            wait = _waiting_on(conn, s)
            if wait:
                out.append({"session": sid, "type": "WAITING", "reason": wait})
                continue
            arm = arms.get(conn, sid)
            if health is None:
                health = _health(conn)
            if not health.get("ok", True):
                out.append({"session": sid, "type": "WAITING", "status": "health_gate",
                            "reason": f"pre-open health gate: {health.get('reason')}"})
                continue
            cut = str(arms.setting(arm, "no_entry_after"))
            if now.strftime("%H:%M") >= cut:
                out.append({"session": sid, "type": "WAITING", "status": "cutoff",
                            "reason": f"no new swing entry after {cut} IST"})
                continue
            g = arms.gate(conn, arm)
            if not g["open"]:
                out.append({"session": sid, "type": "WAITING", "status": "gate_closed", "gate": g, "reason": g["reason"]})
                if not dry_run:
                    _note_once(conn, sid, "AUTO_PICK_GATE", "gate", {"gate": g})
                continue
            if arms.setting(arm, "entry") == arms.KITE_RETEST:
                ev = _pick_retest(conn, s, arm, busy, dry_run, g)
                if ev.get("type") == "PICKED":
                    busy.add(ev["symbol"])
                    if halted_note:
                        ev["held"] = halted_note
                out.append(ev)
                continue
            if cands is None:
                cands = candidates(conn, today)
                used = _traded_on(conn, cands["date"]) if cands["date"] else set()
            if ltps is None:          # one LTP read (and at most one quote call) for the whole pass
                ltps = fresh_ltp(conn, [c["symbol"] for c in cands["rows"][:60] if c["symbol"] not in busy])
            drop = float(arms.setting(arm, "max_intraday_drop_pct") or 0)
            pick, passed, no_ltp, looked = None, [], 0, 0
            for c in cands["rows"]:
                if c["symbol"] in busy:
                    passed.append(f"{c['symbol']} is taken")
                    continue
                if c["symbol"] in used:
                    passed.append(f"{c['symbol']} was already bought on this signal")
                    continue
                if (why_s := _sector_full(c["symbol"], busy, (arm or {}).get("sector_cap"))):
                    passed.append(why_s)
                    _note_skip(conn, sid, "sector", c["symbol"], why_s, dry_run)
                    continue
                if (why_c := _cooled(conn, c["symbol"], arms.setting(arm, "reentry_cooldown_min"))):
                    passed.append(why_c)
                    _note_skip(conn, sid, "cooldown", c["symbol"], why_c, dry_run)
                    continue
                looked += 1
                c = _priced(conn, dict(c), ltps, stale_ok=dry_run)
                if not c["price"]:
                    no_ltp += 1
                    passed.append(f"{c['symbol']} has no LTP under {LTP_MAX_AGE_S} s old (waiting_for_ltp)")
                    continue
                sp = c["signal_price"]
                if c["gap_pct"] is not None and c["gap_pct"] > GAP_PCT:
                    passed.append(f"{c['symbol']} is {c['gap_pct']:+.1f}% above its signal close")
                    continue
                if drop and sp and c["price"] < sp * (1 - drop / 100):
                    passed.append(f"{c['symbol']} is {c['gap_pct']:+.1f}% under its signal close (skipped_falling)")
                    if not dry_run:
                        _note_once(conn, sid, "AUTO_PICK_SKIP", f"falling:{c['symbol']}",
                                   {"status": "skipped_falling", "symbol": c["symbol"], "ltp": c["price"],
                                    "signal_close": sp, "drop_pct": drop})
                    continue
                if guard := entry_guards.check_entry(conn, c, busy):   # 6 Nov 2026 RCA guards: each OFF until set
                    passed.append(guard)
                    continue
                if gap_block := _sv2_nifty_gap(conn):          # LRCA strategy v2 (OFF unless config.json turns it on)
                    passed.append(gap_block)
                    continue
                limit = entry_limit(c["symbol"], c["price"], sp)
                stop_pct = arms.exit_params(conn, arm, c["symbol"])["stop_pct"] if arm else None
                q = sessions.pool_quantity(conn, sid, limit, stop_pct=stop_pct)["quantity"]
                fl = float(s["pool"].get("floor", s.get("min_buy_value")) or 0)
                if q < 1 or q * limit < fl:
                    passed.append(f"{c['symbol']} at Rs {limit:,.0f} does not fit the cash")
                    continue
                pick = {**c, "limit": limit, "quantity": q, "stop_pct": stop_pct}
                break
            if not pick:
                more = f"; {len(passed) - 3} more" if len(passed) > 3 else ""
                # VF-07 (verification 4 Nov 2026): say "rank-fill" too when its stocks were looked at and all taken
                what = (cands.get("what") or "BUY signal") + (" or rank-fill stock" if cands.get("n_fill") else "")
                ev = {"session": sid, "type": "WAITING",
                      "reason": f"no free {what}" + (f" ({'; '.join(passed[:3])}{more})" if passed
                                                       else f": {cands['reason']}" if cands["reason"] else "")}
                if looked and no_ltp == looked:
                    ev["status"] = "waiting_for_ltp"
                    ev["reason"] = f"waiting_for_ltp: no LTP under {LTP_MAX_AGE_S} s old for the {looked} free candidate(s)"
                    if not dry_run:
                        _note_once(conn, sid, "AUTO_PICK_WAIT_LTP", "ltp", {"status": "waiting_for_ltp", "looked": looked})
                out.append(ev)
                continue
            n = s["pool"]["occurrence"] + 1
            ev = {"session": sid, "type": "PICKED", "symbol": pick["symbol"], "occurrence": n,
                  "score": pick["score"], "signal": pick["signal"], "signal_date": cands["date"], "price": pick["price"],
                  "source": pick.get("source", SOURCE_BUY), "trigger": pick["limit"], "order_type": "LIMIT",
                  "signal_close": pick["signal_price"], "ltp_fresh": pick.get("ltp_fresh"),
                  "pick_ltp_ts": pick.get("pick_ltp_ts"), "quantity_est": pick["quantity"],
                  "stop_pct": pick.get("stop_pct"), "gate": g if g.get("metric") else None, "arm": (arm or {}).get("label")}
            if not dry_run:
                r = rules.create_rule({
                    "symbol": pick["symbol"], "side": "BUY", "trigger_type": "PRICE", "trigger_value": pick["limit"],
                    "reference_price": pick["price"], "trigger_direction": "BELOW",
                    "quantity_type": "SESSION", "quantity_value": 100, "product_type": "CNC", "order_type": "LIMIT",
                    "limit_price": pick["limit"], "require_confirmation": True,
                    "signal_close": pick["signal_price"], "pick_ltp": pick.get("pick_ltp"),
                    "pick_ltp_ts": pick.get("pick_ltp_ts"),
                    "notes": f"{NOTE} {sid} occurrence #{n}: ATIP {pick['signal']} {cands['date']}, "
                             f"score {pick['score']}, {pick.get('source', SOURCE_BUY)}"})
                sessions.link_rule(conn, sid, r["id"])
                sessions._event(conn, sid, "AUTO_PICK", {**ev, "rule_id": r["id"]})
                conn.commit()
                ev["rule_id"] = r["id"]
                log.info(f"  auto-pick: {sid} occurrence #{n} -> {pick['symbol']} (ATIP {pick['score']}, "
                         f"LIMIT {ev['trigger']:,.2f} on LTP {pick['price']:,.2f})")
            elif not pick.get("ltp_fresh"):
                ev["status"] = "waiting_for_ltp"
                ev["note"] = "dry run on a stale price: a real pass waits for an LTP under 60 s old"
            busy.add(pick["symbol"])
            if halted_note:
                ev["held"] = halted_note
            out.append(ev)
        return out
    finally:
        if own:
            conn.close()


def _retest_traded(conn, symbol: str, breakout_date) -> bool:
    """A setup is traded once: a dummy of the same breakout that filled (or closed) is not bought again."""
    try:
        for r in conn.execute("SELECT setup FROM algo_dummy_gtt WHERE symbol = ? AND kind = 'BUY' "
                              "AND status IN ('TRIGGERED', 'CLOSED')", (symbol,)).fetchall():
            import json
            if json.loads(_row(r)["setup"] or "{}").get("breakoutDate") == breakout_date:
                return True
    except Exception:
        pass
    return False


def _pick_retest(conn, s: dict, arm: dict, busy: set, dry_run: bool, g: dict) -> dict:
    """KGF-10/11/13: the session's next entry is the best-ranked breakout-retest setup of the latest scan
    (strategy/breakout_retest.py) that nobody holds: a DUMMY- resting buy at the breakout-candle low (orders/algo_gtt.py)
    mirrored by a LIMIT entry rule at that price, which fills in PAPER when the LTP comes down to it. The stop and target
    legs are created only after the buy executes (planned_after_buy)."""
    from orders import algo_gtt, arms, rules, sessions
    sid = s["id"]
    try:
        from strategy.breakout_retest import latest_scan
        scan = latest_scan(conn) or {}
    except Exception as e:
        return {"session": sid, "type": "WAITING", "status": "not_available", "reason": f"breakout-retest scanner unavailable ({e})"}
    rows = scan.get("rows") or []
    if not rows:
        return {"session": sid, "type": "WAITING", "status": "not_available",
                "reason": "no breakout-retest setup in the latest scan" + (f" ({scan.get('as_of')})" if scan.get("as_of") else "")}
    passed = []
    for r in rows:
        sym = str(r["symbol"]).upper()
        if sym in busy:
            passed.append(f"{sym} is taken")
            continue
        if _retest_traded(conn, sym, r.get("breakoutDate")):
            passed.append(f"{sym}: its {r.get('breakoutDate')} setup was already traded")
            continue
        if (why_s := _sector_full(sym, busy, arm.get("sector_cap"))):
            passed.append(why_s)
            _note_skip(conn, sid, "sector", sym, why_s, dry_run)
            continue
        if (why_c := _cooled(conn, sym, arms.setting(arm, "reentry_cooldown_min"))):
            passed.append(why_c)
            _note_skip(conn, sid, "cooldown", sym, why_c, dry_run)
            continue
        buy, stop = r.get("buy_price"), r.get("stop_price")
        if not buy or not stop:
            passed.append(f"{sym} has no buy price")
            continue
        ex = arms.exit_params(conn, arm, sym)
        q = sessions.pool_quantity(conn, sid, float(buy), stop_pct=ex["stop_pct"])["quantity"]
        fl = float(s["pool"].get("floor", s.get("min_buy_value")) or 0)
        if q < 1 or q * float(buy) < fl:
            passed.append(f"{sym} at Rs {float(buy):,.0f} does not fit the cash")
            continue
        n = s["pool"]["occurrence"] + 1
        ev = {"session": sid, "type": "PICKED", "symbol": sym, "occurrence": n, "source": SOURCE_RETEST,
              "signal": "RETEST", "signal_date": r.get("breakoutDate"), "score": None, "price": r.get("currentPrice"),
              "trigger": float(buy), "stoploss": float(stop), "order_type": "LIMIT", "quantity_est": q,
              "stoploss_status": "planned_after_buy", "gate": g if g.get("metric") else None, "arm": arm.get("label")}
        if dry_run:
            return ev
        live = fresh_ltp(conn, [sym]).get(sym)
        last = (live[0] if live else None) or algo_gtt.last_price(conn, sym)[0] or r.get("currentPrice")
        d = algo_gtt.place_dummy_buy(conn, sym, q, buy, stop, confirm_live=True, session_id=sid, setup=r, last_px=last)
        if d["buy_status"] != "success":
            passed.append(f"{sym}: {d['buy_status']} ({d['message'][:60]})")
            continue
        rule = rules.create_rule({
            "symbol": sym, "side": "BUY", "trigger_type": "PRICE", "trigger_value": d["buy_price"],
            "reference_price": float(r.get("currentPrice") or d["last_price"] or d["buy_price"]),
            "trigger_direction": "BELOW", "quantity_type": "SESSION", "quantity_value": 100, "product_type": "CNC",
            "order_type": "LIMIT", "limit_price": d["buy_price"], "require_confirmation": True,
            "algo_ref": d["buy_trigger_id"],
            "notes": f"{NOTE_RETEST} {sid} occurrence #{n}: breakout {r.get('breakoutDate')} {sym}, buy {d['buy_price']}, "
                     f"stop {d['stoploss']} after the buy, {d['buy_trigger_id']}"})
        sessions.link_rule(conn, sid, rule["id"])
        algo_gtt.link_rule(conn, d["buy_trigger_id"], rule["id"])
        ev.update(rule_id=rule["id"], dummy_id=d["buy_trigger_id"], trigger=d["buy_price"], stoploss=d["stoploss"])
        sessions._event(conn, sid, "AUTO_PICK", ev)
        conn.commit()
        log.info(f"  auto-pick: {sid} occurrence #{n} -> {sym} retest, {d['buy_trigger_id']} resting buy at "
                 f"{d['buy_price']} (stop {d['stoploss']} after the buy)")
        return ev
    more = f"; {len(passed) - 3} more" if len(passed) > 3 else ""
    return {"session": sid, "type": "WAITING", "status": "not_placed",
            "reason": f"no free breakout-retest setup ({'; '.join(passed[:3])}{more})" if passed
            else "no breakout-retest setup"}


def _print_gates() -> None:
    """--dry-run: the regime gate, the pre-open health gate and each arm, before the picks."""
    from orders import arms
    from scores.breadth import latest
    c = get_connection()
    try:
        b = latest(c)
        print(f"  breadth on {b['date']}: breadth20 {b['breadth20']:.1f}%, breadth50 {b['breadth50']:.1f}%" if b
              else "  breadth: none stored (python -m scores.breadth)")
        h = _health(c)
        print(f"  pre-open health gate: {'OK' if h.get('ok', True) else 'WAIT -- ' + str(h.get('reason'))}")
        for a in arms.board(c):
            g = a["gate"] or {}
            print(f"  {a['id']:<4} {a['name']:<22} gate {'-' if not g.get('metric') else ('OPEN' if g['open'] else 'CLOSED')}"
                  f"  {a['describe'] or '(no arm)'}")
    finally:
        c.close()


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
    if args.dry_run:
        _print_gates()
    for e in run(dry_run=args.dry_run) or [{"session": "-", "type": "-", "reason": "no session has auto-pick on"}]:
        if e["type"] == "PICKED" and e.get("source") == SOURCE_RETEST:
            line = (f"{e['symbol']} for occurrence #{e['occurrence']} (retest of the {e['signal_date']} breakout): "
                    f"DUMMY resting LIMIT buy {e['trigger']:,.2f}, stop {e['stoploss']:,.2f} after the buy, ~{e['quantity_est']} sh")
        elif e["type"] == "PICKED":
            fresh = "fresh LTP" if e.get("ltp_fresh") else "STALE price (would wait for a fresh LTP)"
            line = (f"{e['symbol']} for occurrence #{e['occurrence']} (ATIP {e['signal']} {e['score']}): LIMIT {e['trigger']:,.2f} "
                    f"on {fresh} {e['price']:,.2f} vs signal close {e['signal_close']:,.2f}, ~{e['quantity_est']} sh"
                    + (f", stop {e['stop_pct']:g}%" if e.get("stop_pct") else ""))
        else:
            line = e.get("reason", "")
        print(f"  {e['session']:<4} {e['type']:<8} {('[' + e['arm'] + '] ') if e.get('arm') else ''}{line}")
