"""
Session engine (owner prompt 18 Nov 2026, 03:05 PM IST): independent Rs 25,000 trading sessions.

This is a layer over the cash pools of orders/sessions.py (29 Oct 2026), which already give every session its own
money, compound it, net each leg's charges and stop a session after max_occurrences (500) trades. What this module adds:

    split      available paper funds -> floor(available / Rs 25,000) new sessions; the rest stays as the reserve
    wallet     seed, cash available, invested value, realised and unrealised P&L, current balance -- per session
    counts     legs (1 BUY = 1 leg, 1 SELL = 1 leg), completed trades (BUY + its SELL), wins, losses, win rate,
               average net P&L per completed trade; an open position is not a completed trade
    auto-stop  after max_occurrences completed trades: no new BUY (orders/sessions.py gate() already holds it), the open
               position closes by its own SELL / stop, then the session is COMPLETED and its final ledger frozen
    floor      a session whose balance falls under floor_pct (default 80 %) of its capital is PAUSED with the reason
               BELOW_FLOOR and an alert; it is never refilled from another session
    states     CREATED -> ACTIVE -> (PAUSED) -> COMPLETED, each change a session_events row (event STATE) with its
               IST time and reason
    tables     session_trades (one row per round trip) and session_ledger (the session's cash book: SEED, TRANSFER,
               BUY, CHARGES, SELL, and a PNL memo line per completed trade), refreshed by sync()

Where the owner's names meet ATIP's:
    sessions                 trading_sessions (one row per session, plus the snapshot columns below)
    session_id               trading_sessions.id (S1, S2, ...); order_log.session_id (orders/broker.py; MANUAL outside one)
    status ACTIVE            stored as RUNNING (the whole order engine reads that word); OFF is shown as PAUSED
    seed capital             trading_sessions.start_capital; money moved in later is a TRANSFER, and the session's
                             capital = seed + transfers is what growth and the floor are measured on (CAP-03, 16 Nov)

Runs from the dashboard's order monitor (dashboard/server.py, every 30 s), never from pipeline/scheduler.py.
PAPER by default: creating sessions is PAPER only, as orders/sessions.py add_session is; LIVE orders stay
confirmation-gated by the existing engine and need the SEBI Algo ID before any automatic sending.

    python -m orders.session_engine plan               what the split would make now (writes nothing)
    python -m orders.session_engine create --n 2 --ack make 2 new Rs 25,000 sessions (CREATED; --run to start them)
    python -m orders.session_engine rebase [--ack]     return each idle session's transfers to the reserve (owner only)
    python -m orders.session_engine status             every session's wallet, counts and state
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("atip.session_engine")

IST = timezone(timedelta(hours=5, minutes=30))
CONFIG_PATH = Path("atip_data") / "config.json"
KEY = "session_engine"
DEFAULTS = {"seed_capital": 25000.0, "floor_pct": 80.0, "max_completed_trades": 500, "respect_buffer": True,
            "allow_live": False}
STATE_OF = {"CREATED": "CREATED", "RUNNING": "ACTIVE", "PAUSED": "PAUSED", "OFF": "PAUSED", "COMPLETED": "COMPLETED"}
BELOW_FLOOR = "BELOW_FLOOR"
LEDGER_TYPES = ("SEED", "TRANSFER", "BUY", "CHARGES", "SELL", "PNL")
SNAPSHOT_COLUMNS = {"current_balance": "REAL", "cash_available": "REAL", "invested_value": "REAL",
                    "realised_pnl": "REAL", "unrealised_pnl": "REAL", "completed_trades": "INTEGER", "legs": "INTEGER",
                    "wins": "INTEGER", "losses": "INTEGER", "balance_floor_pct": "REAL", "pause_reason": "TEXT",
                    "created_at_ist": "TEXT", "completed_at_ist": "TEXT", "final_ledger": "TEXT",
                    "engine_synced_at": "TEXT"}


# -- time, config, tables ---------------------------------------------------------------------------

def ist_now() -> str:
    """Now in IST as ATIP stores times: 2026-11-18T15:20:00 (no offset suffix)."""
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def to_ist(ts) -> str | None:
    """A stored ATIP time (naive, the server's local clock) in IST. The Mac and the VPS keep IST, so this is
    usually the same text; on a server set to UTC it moves the time by +5:30 instead of mislabelling it."""
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace(" ", "T")[:19])
    except ValueError:
        return str(ts)
    return d.astimezone(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(KEY) or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    out.update({k: raw[k] for k in DEFAULTS if k in raw and raw[k] is not None})
    out["seed_capital"] = float(out["seed_capital"])
    out["floor_pct"] = float(out["floor_pct"])
    out["max_completed_trades"] = int(out["max_completed_trades"])
    return out


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


_READY: set = set()                 # databases this process has already given the engine's tables and columns


def _db_key() -> str:
    import os
    from db import schema
    return f"{os.environ.get('ATIP_DATABASE_URL', '')}|{schema.DB_PATH}"


def ensure_tables(conn) -> None:
    """The engine's tables and trading_sessions columns, once per database per process: the order monitor calls the
    engine every 30 s, and a CREATE INDEX IF NOT EXISTS still asks PostgreSQL for a table lock each time."""
    from orders import sessions as S
    S.ensure_tables(conn)
    key = _db_key()
    if key in _READY:
        return
    try:                                            # additive, as orders/sessions.py does it
        have = {r[1] for r in conn.execute("PRAGMA table_info(trading_sessions)").fetchall()}
        for col, typ in SNAPSHOT_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE trading_sessions ADD COLUMN {col} {typ}")
    except Exception as e:
        log.warning(f"  trading_sessions engine columns skipped: {e}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_trades (
            trade_id       TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            symbol         TEXT,
            security_id    TEXT,
            buy_leg_id     TEXT,
            sell_leg_id    TEXT,
            qty            REAL,
            buy_price      REAL,
            sell_price     REAL,
            buy_value      REAL,
            sell_value     REAL,
            charges        REAL,
            gross_pnl      REAL,
            net_pnl        REAL,
            status         TEXT,
            opened_at_ist  TEXT,
            closed_at_ist  TEXT,
            updated_at_ist TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_trades_sid ON session_trades(session_id, opened_at_ist)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_ledger (
            entry_id       TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            seq            INTEGER NOT NULL,
            type           TEXT NOT NULL,
            amount         REAL NOT NULL,
            balance_after  REAL NOT NULL,
            ref            TEXT,
            memo           TEXT,
            ts_ist         TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_ledger_sid ON session_ledger(session_id, seq)")
    conn.commit()
    _READY.add(key)


def _event(conn, sid: str, event: str, detail: dict) -> None:
    """A session_events row stamped in IST (orders/sessions.py's own rows use the server clock)."""
    conn.execute("INSERT INTO session_events (session_id, at, event, detail) VALUES (?, ?, ?, ?)",
                 (sid, ist_now(), event, json.dumps(detail)))


def state_of(s: dict) -> str:
    """CREATED / ACTIVE / PAUSED / COMPLETED for a trading_sessions row; "PAUSED – BELOW FLOOR" when the floor paused it."""
    st = STATE_OF.get(str(s.get("status") or "").upper(), str(s.get("status") or ""))
    if st == "PAUSED" and s.get("pause_reason") == BELOW_FLOOR:
        return "PAUSED – BELOW FLOOR"
    return st


def set_state(conn, sid: str, status: str, reason: str, pause_reason: str | None = None) -> None:
    """Change a session's stored status and log it (event STATE, IST time, from / to / reason)."""
    from orders import sessions as S
    cur = S.get_session(conn, sid)
    status = status.upper()
    conn.execute("UPDATE trading_sessions SET status = ?, pause_reason = ?, updated_at = ? WHERE id = ?",
                 (status, pause_reason, S._now(), sid))
    _event(conn, sid, "STATE", {"from": state_of(cur), "to": state_of({"status": status, "pause_reason": pause_reason}),
                                "reason": reason})
    conn.commit()


# -- the wallet and the counts -------------------------------------------------------------------------

def _fills(conn, sid: str, s: dict) -> tuple[list, dict, list]:
    """(the session's executed rules, charges per rule id, its trades) since its pool started."""
    from orders import sessions as S
    rules = S._linked_rules(conn, sid)
    charges = S._charges(conn, rules)
    trades = S._trades(rules, charges, since=s.get("pool_started_at"))
    return rules, charges, trades


def stats(conn, sid: str, s: dict | None = None) -> dict | None:
    """One session's wallet, counts and state. None for a session without a seed (no cash pool)."""
    from orders import sessions as S
    s = s or S.get_session(conn, sid)
    L = S.ledger(conn, sid, s=s)
    if not L:
        return None
    rules, charges, trades = _fills(conn, sid, s)
    ids = {t["rule_id"] for t in trades}
    exits = [r for r in rules if r.get("parent_rule_id") in ids and r.get("status") == "EXECUTED"
             and r.get("execution_price")]
    closed = [t for t in trades if t["closed"]]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    capital = float(L["capital"])
    cfg = settings()
    floor_pct = float(s.get("balance_floor_pct") or cfg["floor_pct"])
    cap = int(L["max_occurrences"])
    invested = round(sum(h["cost"] for h in L["open"]), 2)
    return {"session_id": sid, "name": s.get("name"), "status": s.get("status"), "state": state_of(s),
            "mode": s.get("mode"), "auto_pick": bool(s.get("auto_pick")),
            "seed_capital": L["start_capital"], "transfers": L["transfers"], "capital": L["capital"],
            "cash_available": L["cash"], "invested_value": invested, "realised_pnl": L["booked"],
            "unrealised_pnl": L["unrealised"], "current_balance": L["equity"], "charges": L["charges"],
            "growth_pct": round((L["equity"] - capital) / capital * 100, 2) if capital else None,
            "legs": len(trades) + len(exits), "completed_trades": len(closed),
            "open_trades": sum(1 for t in trades if not t["closed"]), "wins": wins, "losses": len(closed) - wins,
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "avg_pnl_per_trade": round(sum(t["pnl"] for t in closed) / len(closed), 2) if closed else None,
            "max_completed_trades": cap, "progress_pct": round(min(100.0, len(closed) / cap * 100), 1) if cap else None,
            "floor_pct": floor_pct, "floor": round(capital * floor_pct / 100, 2),
            "open": [{"symbol": h["symbol"], "qty": h["qty"], "entry": h["entry"], "mark": h["mark"],
                      "unrealised": h["unrealised"]} for h in L["open"]],
            "created_at_ist": s.get("created_at_ist") or to_ist(s.get("created_at")),
            "completed_at_ist": s.get("completed_at_ist"), "pause_reason": s.get("pause_reason")}


# -- session_trades and session_ledger -----------------------------------------------------------------

def _book(conn, sid: str, s: dict) -> tuple[list, list, dict]:
    """(session_trades rows, session_ledger rows, check): the session's round trips and its cash book, in time order.
    The cash book's last balance is the engine's cash (orders/sessions.py ledger()); `check` says whether they agree."""
    from orders import sessions as S
    from accounting.cycles import transfers_for
    rules, charges, trades = _fills(conn, sid, s)
    sec = {}
    try:
        ids = [r["dhan_order_id"] for r in rules if r.get("dhan_order_id")]
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            for p in conn.execute(f"SELECT order_id, security_id FROM paper_order WHERE order_id IN "
                                  f"({','.join('?' * len(part))})", tuple(part)).fetchall():
                p = _row(p)
                sec[p["order_id"]] = p["security_id"]
    except Exception:                               # no paper_order table, or no security_id column
        pass
    kids = {}
    for r in rules:
        if r.get("parent_rule_id") and r.get("status") == "EXECUTED" and r.get("execution_price"):
            kids.setdefault(r["parent_rule_id"], []).append(r)
    by_id = {r["id"]: r for r in rules}
    now = ist_now()
    t_rows, events = [], []
    start = float(s.get("start_capital") or 0)
    since = s.get("pool_started_at")
    events.append((str(since or s.get("created_at") or ""), 0, "SEED", start, f"seed:{since}", "seed capital"))
    for m in transfers_for(conn, sid, since):
        events.append((str(m["at"]), 1, "TRANSFER", float(m["amount"]), f"transfer:{m['id']}", m.get("reason") or ""))
    for t in trades:
        e = by_id.get(t["rule_id"], {})
        legs = sorted(kids.get(t["rule_id"], []), key=lambda l: str(l.get("updated_at") or ""))
        sign = 1 if t["side"] == "BUY" else -1
        fee_in = float(charges.get(t["rule_id"], 0.0))
        at = str(e.get("updated_at") or t["at"])
        events.append((at, 2, "BUY", round(-sign * t["entry"] * t["qty"], 2), f"{t['rule_id']}:BUY",
                       f"{t['side']} {t['qty']:g} {t['symbol']} @ {t['entry']:.2f}"))
        if fee_in:
            events.append((at, 3, "CHARGES", -fee_in, f"{t['rule_id']}:BUY:CHARGES", f"charges on the {t['side']} leg"))
        sell_value = 0.0
        for l in legs:
            q, px = float(l.get("execution_quantity") or 0), float(l["execution_price"])
            sell_value += px * q
            la = str(l.get("updated_at") or "")
            events.append((la, 4, "SELL", round(sign * px * q, 2), f"{l['id']}:SELL",
                           f"{l.get('role') or 'EXIT'} {q:g} {t['symbol']} @ {px:.2f}"))
            fee = float(charges.get(l["id"], 0.0))
            if fee:
                events.append((la, 5, "CHARGES", -fee, f"{l['id']}:SELL:CHARGES", f"charges on the {l.get('role') or 'exit'} leg"))
        last = legs[-1] if legs else None
        if t["closed"] and last:
            events.append((str(last.get("updated_at") or ""), 6, "PNL", t["pnl"], f"{t['rule_id']}:PNL",
                           f"{t['symbol']} round trip, net of charges"))
        t_rows.append({"trade_id": t["rule_id"], "session_id": sid, "symbol": t["symbol"],
                       "security_id": sec.get(e.get("dhan_order_id")) or e.get("security_id"),
                       "buy_leg_id": t["rule_id"], "sell_leg_id": ",".join(l["id"] for l in legs) or None,
                       "qty": t["qty"], "buy_price": t["entry"], "sell_price": t["exit_price"],
                       "buy_value": round(t["entry"] * t["qty"], 2), "sell_value": round(sell_value, 2) if legs else None,
                       "charges": t["charges"],
                       "gross_pnl": round(sign * (sell_value - t["entry"] * sum(float(l.get("execution_quantity") or 0)
                                                                               for l in legs)), 2) if legs else None,
                       "net_pnl": t["pnl"] if t["closed"] else None, "status": "CLOSED" if t["closed"] else "OPEN",
                       "opened_at_ist": to_ist(at), "closed_at_ist": to_ist(last.get("updated_at")) if t["closed"] and last
                       else None, "updated_at_ist": now})
    events.sort(key=lambda x: (x[0].replace(" ", "T"), x[1]))
    bal, l_rows = 0.0, []
    for seq, (at, _, typ, amount, ref, memo) in enumerate(events, 1):
        if typ != "PNL":                            # the PNL line is a memo: BUY, SELL and CHARGES already moved the cash
            bal = round(bal + amount, 2)
        l_rows.append({"entry_id": f"{sid}:{ref}", "session_id": sid, "seq": seq, "type": typ,
                       "amount": round(amount, 2), "balance_after": bal, "ref": ref, "memo": memo, "ts_ist": to_ist(at) or now})
    cash = S.ledger(conn, sid, s=s)["cash"]
    return t_rows, l_rows, {"ledger_balance": bal, "engine_cash": cash, "ok": abs(bal - cash) < 0.005}


def _replace(conn, table: str, key: str, sid: str, rows: list) -> None:
    conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))
    if rows:
        cols = list(rows[0])
        conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [tuple(r[c] for c in cols) for r in rows])


def sync(conn, sid: str | None = None) -> dict:
    """Refresh session_trades, session_ledger and the snapshot columns of every cash-pool session (or `sid`). A session
    is rewritten only when its book changed; a COMPLETED session's frozen book is left alone."""
    from orders import sessions as S
    ensure_tables(conn)
    out = {}
    for s in [_row(r) for r in conn.execute("SELECT * FROM trading_sessions").fetchall()]:
        if sid and s["id"] != sid or not s.get("start_capital"):
            continue
        if s.get("status") == "COMPLETED" and s.get("final_ledger"):
            out[s["id"]] = "frozen"
            continue
        t_rows, l_rows, check = _book(conn, s["id"], s)
        have = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0), COALESCE(MAX(seq), 0) FROM session_ledger "
                            "WHERE session_id = ?", (s["id"],)).fetchone()
        changed = (int(have[0]), round(float(have[1]), 2)) != (len(l_rows), round(sum(r["amount"] for r in l_rows), 2))
        if changed or conn.execute("SELECT COUNT(*) FROM session_trades WHERE session_id = ?",
                                   (s["id"],)).fetchone()[0] != len(t_rows):
            _replace(conn, "session_trades", "trade_id", s["id"], t_rows)
            _replace(conn, "session_ledger", "entry_id", s["id"], l_rows)
        st = stats(conn, s["id"], s)
        snap = {k: st[k] for k in ("current_balance", "cash_available", "invested_value", "realised_pnl",
                                   "unrealised_pnl", "completed_trades", "legs", "wins", "losses")}
        old = {k: s.get(k) for k in snap}
        if any(old[k] is None or abs(float(old[k]) - float(snap[k])) >= (1.0 if k == "current_balance" else 0.005)
               for k in snap) or not s.get("created_at_ist"):
            conn.execute("UPDATE trading_sessions SET " + ", ".join(f"{k} = ?" for k in snap)
                         + ", created_at_ist = COALESCE(created_at_ist, ?), engine_synced_at = ? WHERE id = ?",
                         (*snap.values(), to_ist(s.get("created_at")), ist_now(), s["id"]))
        if not check["ok"]:
            log.warning(f"  session {s['id']}: cash book Rs {check['ledger_balance']:,.2f} != engine cash "
                        f"Rs {check['engine_cash']:,.2f}")
        out[s["id"]] = {"written": changed, **check}
    conn.commit()
    return out


# -- the checks: auto-stop and the floor -------------------------------------------------------------

def _alert(text: str, severity: str, key: str) -> None:
    try:
        from alerts.telegram import notify
        notify(text, category="session", severity=severity, key=key)
    except Exception as e:                          # an alert that cannot be sent never stops the check
        log.warning(f"  session alert not sent: {e}")


def check(conn, do_sync: bool = True) -> list:
    """One pass of the engine's own rules over every cash-pool session. Returns what changed.
        * completed trades >= its max and nothing open: COMPLETED, auto-pick off, final ledger frozen, alert
        * ACTIVE with its balance under the floor: PAUSED (BELOW_FLOOR), alert; never refilled from another session
    New BUYs of a session at its max are already held by orders/sessions.py gate() (occurrence cap); its open
    position keeps its SELL / stop, which gate() never holds."""
    from orders import sessions as S
    ensure_tables(conn)
    if do_sync:
        sync(conn)
    changes = []
    for s in [_row(r) for r in conn.execute("SELECT * FROM trading_sessions").fetchall()]:
        if not s.get("start_capital") or s.get("status") == "COMPLETED":
            continue
        if s.get("status") == "RUNNING" and s.get("pause_reason"):      # resumed (by the owner): the old reason is gone,
            conn.execute("UPDATE trading_sessions SET pause_reason = NULL WHERE id = ?", (s["id"],))   # so a kill-switch
            conn.commit()                                                 # pause later is not shown as BELOW FLOOR
            s["pause_reason"] = None
        st = stats(conn, s["id"], s)
        if not st:
            continue
        if st["completed_trades"] >= st["max_completed_trades"] and not st["open_trades"]:
            final = {k: st[k] for k in ("seed_capital", "transfers", "capital", "cash_available", "realised_pnl",
                                        "current_balance", "charges", "growth_pct", "legs", "completed_trades", "wins",
                                        "losses", "win_rate_pct", "avg_pnl_per_trade")}
            final["frozen_at_ist"] = ist_now()
            conn.execute("UPDATE trading_sessions SET auto_pick = 0, completed_at_ist = ?, final_ledger = ? WHERE id = ?",
                         (final["frozen_at_ist"], json.dumps(final), s["id"]))
            set_state(conn, s["id"], "COMPLETED",
                      f"{st['completed_trades']} completed trades ({st['legs']} legs); final balance "
                      f"Rs {st['current_balance']:,.2f}")
            _alert(f"<b>Session {s['id']} completed</b>\n{st['completed_trades']} trades, final balance "
                   f"₹{st['current_balance']:,.2f} ({st['growth_pct']:+.2f}% on ₹{st['capital']:,.0f}). Its ledger is frozen.",
                   "info", f"session_completed:{s['id']}")
            changes.append({"session": s["id"], "to": "COMPLETED"})
            continue
        if s.get("status") == "RUNNING" and st["current_balance"] < st["floor"]:
            set_state(conn, s["id"], "PAUSED",
                      f"balance Rs {st['current_balance']:,.2f} under its floor Rs {st['floor']:,.2f} "
                      f"({st['floor_pct']:g} % of Rs {st['capital']:,.0f}); not refilled from another session",
                      pause_reason=BELOW_FLOOR)
            _alert(f"<b>Session {s['id']} paused: below floor</b>\nBalance ₹{st['current_balance']:,.2f} is under its "
                   f"floor ₹{st['floor']:,.2f} ({st['floor_pct']:g}% of ₹{st['capital']:,.0f}). No new buys; an open "
                   f"position still exits by its own stop or target. Nothing is moved in from another session.",
                   "warning", f"session_floor:{s['id']}")
            changes.append({"session": s["id"], "to": "PAUSED", "reason": BELOW_FLOOR})
    return changes


# -- creating sessions ------------------------------------------------------------------------------

def _paper() -> bool:
    from orders import sessions as S
    return S._paper()


def plan(conn, seed: float | None = None) -> dict:
    """What the split would do now. Writes nothing.
    PAPER: the money no session owns (spare paper cash), less the capital plan's cash buffer when one is configured
    (the owner's 90 % rule, 16 / 18 Nov 2026), divided into floor(available / seed) sessions; the rest is the reserve.
    LIVE: Dhan's available balance, read only -- creating LIVE sessions is refused."""
    from orders import sessions as S
    cfg = settings()
    seed = float(seed or cfg["seed_capital"])
    if seed <= 0:
        raise ValueError("the seed must be more than Rs 0")
    if not _paper():
        from orders.broker import available_balance
        avail = float(available_balance(account=True) or 0)
        n = int(avail // seed)
        return {"env": "LIVE", "available": round(avail, 2), "seed": seed, "new_sessions": n,
                "reserve": round(avail - n * seed, 2), "can_create": False,
                "reason": "LIVE sessions are not created by the engine: it is PAPER only, and automatic LIVE sending "
                          "needs the SEBI Algo ID (config sebi_algo_id) and the owner's switch"}
    spare = S.spare_paper_cash(conn)
    available, buffer = spare["spare"], 0.0
    if cfg["respect_buffer"]:
        from orders import capital_plan
        cp = capital_plan.configured()
        if cp:
            h = capital_plan.headroom(conn, cp)
            buffer = float(h.get("buffer") or 0)
            available = min(available, float(h["headroom"]))
    n = int(math.floor(available / seed + 1e-9))
    rows = [st for st in (stats(conn, s["id"], s) for s in S.list_sessions(conn)["sessions"]
                          if s.get("start_capital")) if st]
    topped = [{"session": r["session_id"], "transfers": r["transfers"], "idle": not r["open_trades"]}
              for r in rows if r["transfers"] > 0.005]
    return {"env": "PAPER", "seed": seed, "paper_balance": spare["paper_balance"], "sessions_cash": spare["sessions_cash"],
            "spare": spare["spare"], "buffer": round(buffer, 2), "available": round(available, 2), "new_sessions": n,
            "reserve": round(spare["spare"] - n * seed, 2), "can_create": n > 0,
            "sessions": len(rows), "sessions_capital": round(sum(r["capital"] for r in rows), 2),
            "topped_up": topped,
            "if_topups_returned": {
                "returnable_now": round(sum(t["transfers"] for t in topped if t["idle"]), 2),
                "waiting_on_open_positions": [t["session"] for t in topped if not t["idle"]],
                "new_sessions": int(math.floor((available + sum(t["transfers"] for t in topped)) / seed + 1e-9))}}


def create(conn, n: int | None = None, ack: bool = False, run: bool = False, like: str | None = "S5") -> dict:
    """Make `n` new sessions of the seed each (default: as many as plan() allows), PAPER only, out of the money no
    session owns. Each starts CREATED (held: no order) unless run=True, which makes it ACTIVE in AUTO with auto-pick
    on, as S1-S10 run. ack=True is required: it moves paper money into the new sessions."""
    from orders import sessions as S
    if not _paper():
        raise ValueError("creating sessions is PAPER only: broker_env is not PAPER")
    if not ack:
        raise ValueError("creating sessions moves paper cash into them: confirm with ack=true")
    p = plan(conn)
    n = p["new_sessions"] if n in (None, "") else int(n)
    if n < 1:
        raise ValueError(f"no Rs {p['seed']:,.0f} session fits: Rs {p['available']:,.2f} available"
                         + (f" above the Rs {p['buffer']:,.2f} cash buffer" if p["buffer"] else ""))
    if n > p["new_sessions"]:
        raise ValueError(f"only {p['new_sessions']} session(s) of Rs {p['seed']:,.0f} fit in Rs {p['available']:,.2f}")
    ensure_tables(conn)
    made = []
    for _ in range(n):
        r = S.add_session(conn, f"₹{p['seed']:,.0f} session", p["seed"], like, run=False, ack=False,
                          max_occurrences=settings()["max_completed_trades"])
        sid = r["id"]
        conn.execute("UPDATE trading_sessions SET created_at_ist = ? WHERE id = ?", (ist_now(), sid))
        set_state(conn, sid, "CREATED", f"split of the reserve into Rs {p['seed']:,.0f} sessions")
        if run:
            S.update_session(conn, sid, {"auto_pick": True})
            S.set_status(conn, sid, "RUNNING", "session engine: started on creation")
            S.set_mode(conn, sid, "AUTO", ack=True)
            _event(conn, sid, "STATE", {"from": "CREATED", "to": "ACTIVE", "reason": "started on creation"})
            conn.commit()
        made.append(sid)
    sync(conn)
    after = plan(conn)
    return {"created": made, "seed": p["seed"], "reserve_before": p["spare"], "reserve_after": after["spare"],
            "state": "ACTIVE" if run else "CREATED"}


def activate(conn, sid: str, ack: bool = False) -> dict:
    """CREATED (or PAUSED) -> ACTIVE: RUNNING in AUTO with auto-pick on. A session under its floor is refused."""
    from orders import sessions as S
    if not ack:
        raise ValueError("ACTIVE sends this session's entries without asking (AUTO): confirm with ack=true")
    s = S.get_session(conn, sid)
    if s["status"] == "COMPLETED":
        raise ValueError(f"{sid} is COMPLETED: its ledger is frozen")
    st = stats(conn, sid, s)
    if st and st["current_balance"] < st["floor"]:
        raise ValueError(f"{sid}'s balance Rs {st['current_balance']:,.2f} is under its floor Rs {st['floor']:,.2f}")
    S.update_session(conn, sid, {"auto_pick": True})
    S.set_status(conn, sid, "RUNNING", "session engine: activated")
    if s["mode"] != "AUTO":
        S.set_mode(conn, sid, "AUTO", ack=True)
    conn.execute("UPDATE trading_sessions SET pause_reason = NULL WHERE id = ?", (sid,))
    _event(conn, sid, "STATE", {"from": state_of(s), "to": "ACTIVE", "reason": "activated by the owner"})
    conn.commit()
    return stats(conn, sid)


def rebase(conn, ack: bool = False) -> dict:
    """The owner's option (not run by the engine): move each idle session's net transfers (the 16 Nov top-ups) back to
    the reserve, so the money can be split into new seed-sized sessions. A session holding a position is skipped and
    listed; run it again after that position exits. Dry run unless ack=True."""
    from accounting.ledger import transfer
    p = plan(conn)
    moves = [t for t in p["topped_up"] if t["idle"]]
    if not ack:
        return {"dry_run": True, "would_return": moves, "skipped_open": p["if_topups_returned"]["waiting_on_open_positions"],
                "new_sessions_after": p["if_topups_returned"]["new_sessions"]}
    if not _paper():
        raise ValueError("rebase is PAPER only")
    done = []
    for t in moves:
        transfer(conn, t["session"], None, round(t["transfers"], 2), "session engine rebase: top-up back to the reserve")
        done.append(t)
    return {"returned": done, "skipped_open": p["if_topups_returned"]["waiting_on_open_positions"], "plan": plan(conn)}


# -- what the pages and the apps read ------------------------------------------------------------------

def overview(conn, do_sync: bool = True) -> dict:
    """Every cash-pool session's card data and the summary row: sessions, combined balance, combined P&L, reserve."""
    from orders import sessions as S
    if do_sync:
        try:
            sync(conn)
        except Exception as e:
            log.warning(f"  session engine sync: {e}")
    rows = [st for st in (stats(conn, s["id"], s) for s in S.list_sessions(conn)["sessions"] if s.get("start_capital"))
            if st]
    try:
        p = plan(conn)
    except Exception as e:
        p = {"reserve": None, "spare": None, "new_sessions": 0, "error": str(e)}
    cap = round(sum(r["capital"] for r in rows), 2)
    bal = round(sum(r["current_balance"] for r in rows), 2)
    by_state = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    return {"sessions": rows, "as_of_ist": ist_now(), "settings": settings(),
            "summary": {"sessions": len(rows), "by_state": by_state, "capital": cap, "combined_balance": bal,
                        "combined_pnl": round(bal - cap, 2), "realised_pnl": round(sum(r["realised_pnl"] for r in rows), 2),
                        "unrealised_pnl": round(sum(r["unrealised_pnl"] for r in rows), 2),
                        "reserve": p.get("spare"), "buffer": p.get("buffer"), "fits_new_sessions": p.get("new_sessions")}}


def alerts(conn) -> list:
    """The sessions the apps' banner should name: paused below their floor, and completed."""
    out = []
    try:
        for s in [_row(r) for r in conn.execute("SELECT id, status, pause_reason, completed_trades, current_balance "
                                                "FROM trading_sessions WHERE start_capital IS NOT NULL").fetchall()]:
            if s["status"] == "PAUSED" and s.get("pause_reason") == BELOW_FLOOR:
                out.append({"session": s["id"], "state": "PAUSED – BELOW FLOOR", "tone": "warn",
                            "text": f"{s['id']} paused: below floor"})
            elif s["status"] == "COMPLETED":
                out.append({"session": s["id"], "state": "COMPLETED", "tone": "info",
                            "text": f"{s['id']} completed {s.get('completed_trades') or 0} trades"})
    except Exception:                               # columns not there yet: nothing to say
        return []
    return out


def trades(conn, sid: str) -> list:
    ensure_tables(conn)
    return [_row(r) for r in conn.execute("SELECT * FROM session_trades WHERE session_id = ? ORDER BY opened_at_ist, "
                                          "trade_id", (sid.upper(),)).fetchall()]


def ledger_rows(conn, sid: str) -> list:
    ensure_tables(conn)
    return [_row(r) for r in conn.execute("SELECT * FROM session_ledger WHERE session_id = ? ORDER BY seq",
                                          (sid.upper(),)).fetchall()]


def to_csv(rows: list, columns: list) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    return buf.getvalue()


TRADE_COLUMNS = ["trade_id", "session_id", "symbol", "security_id", "buy_leg_id", "sell_leg_id", "qty", "buy_price",
                 "sell_price", "buy_value", "sell_value", "charges", "gross_pnl", "net_pnl", "status", "opened_at_ist",
                 "closed_at_ist"]
LEDGER_COLUMNS = ["seq", "entry_id", "session_id", "type", "amount", "balance_after", "memo", "ts_ist"]


if __name__ == "__main__":
    import argparse

    from db.schema import get_connection

    ap = argparse.ArgumentParser(prog="python -m orders.session_engine", description="ATIP Rs 25,000 session engine")
    ap.add_argument("action", choices=("plan", "create", "rebase", "status", "sync", "check"))
    ap.add_argument("--n", type=int, help="create: how many sessions (default: as many as fit)")
    ap.add_argument("--ack", action="store_true", help="create / rebase: really move the paper money")
    ap.add_argument("--run", action="store_true", help="create: start them ACTIVE (AUTO, auto-pick on)")
    a = ap.parse_args()
    c = get_connection()
    try:
        if a.action == "plan":
            out = plan(c)
        elif a.action == "create":
            out = create(c, a.n, a.ack, a.run)
        elif a.action == "rebase":
            out = rebase(c, a.ack)
        elif a.action == "sync":
            out = sync(c)
        elif a.action == "check":
            out = check(c)
        else:
            out = overview(c)
        print(json.dumps(out, indent=2, default=str))
    finally:
        c.close()
