"""
Trading sessions (UXR-01): up to ten named strategies, each with its own execution mode and limits,
shown on /command and managed through /api/sessions.

    mode    MANUAL   you place every order yourself
            CONFIRM  ATIP asks before sending (the existing confirmation gate) -- the default
            AUTO     ATIP may send within the session's limits without asking
    status  RUNNING | PAUSED | OFF

WHAT IS ENFORCED (UXR-04, 27 Oct 2026). The trigger engine (orders/rules.py check_triggers) asks
gate() before an ENTRY rule linked to a session may fire:
    * the session must be RUNNING (PAUSED / OFF hold the rule; it stays ACTIVE and fires once resumed)
    * MANUAL holds it too -- in a MANUAL session you place every order yourself
    * max_trades: entries executed today by the session
    * max_loss_value: the session's realised loss today (its exit legs against their entries)
    * capital_cap: the session's open entry value plus this order
Exit legs (TARGET / STOP) are never held: a protective stop must fire whatever the session says.
Rules not linked to a session are unaffected.

AUTO SEND (the owner's switch, 27 Oct 2026). AUTO alone does not skip the confirmation tap. The owner
turns on one account-wide switch, "auto send" (session_settings, OFF by default, every change logged
in session_events); only then does an entry rule of a RUNNING session in AUTO mode, that the gate
lets through, go straight to the order instead of asking (orders/rules.py check_triggers, which
still refuses a quote older than MAX_QUOTE_AGE_SECONDS). Turning it on needs ack=True and is refused
while trading is halted. PAPER ONLY (owner, 27 Oct 2026): the switch cannot be turned on unless
broker_env is PAPER, and outside PAPER the engine makes every entry ask -- auto send, an AUTO
session or a rule's own require_confirmation=False alike. Exit legs keep their own setting. Every reply carries
"auto_bypass": whether the switch is on. The kill switch (orders/risk.py halt / resume) remains the
way to stop everything.

AUTO is guarded even as a setting: it needs ack=True (the "I understand" step on the screen),
it is refused while trading is halted, and it is refused when broker_env is LIVE and no SEBI
Algo ID is configured (config.json "sebi_algo_id").

CASH POOLS (the owner's plan, 29 Oct 2026). A session given a start_capital (start_pool, e.g.
10 x Rs 25,000) keeps its own money, independent of the other sessions. Each trade is one
"occurrence" of the session; the next one starts with everything the last one ended with, until
the session has run max_occurrences (default 500) -- gate() holds its entries after that. With
auto_pick on, orders/autopick.py queues each next occurrence on an ATIP BUY signal by itself:

    cash  = start_capital + booked P&L (net of charges) - money in its open trade
    entry = sized at fire time from the session's WHOLE cash (rules quantity_type SESSION, 100 %),
            so a profit is reinvested in the next stock and a loss shrinks the next buy
    exits = sell everything at +target_pct (default 3 %), stop at -stop_pct (default 2 %); or, with
            trail_pct set (owner, 30 Oct 2026), no sale at +target_pct: from there the stop trails
            trail_pct under the best price, and after hold_days a price at +target_pct or more
            sells everything (orders/rules.py trail_after_pct, bracket_target_after_days)

link_rule applies those three to an entry linked to a pool session, and gate() holds an entry
while the session's cash is under min_buy_value (default Rs 12,000) -- i.e. while its money is
in an open trade -- or below the order's value. No fixed amount caps a pool (owner, 29 Oct 2026:
it compounds, so its orders grow): capital_cap does not apply, and its orders -- tagged ATIP-S1 --
are not held to the account's max_order_value (orders/risk.py pretrade_check pool_session). Its
own cash is its only size limit; every other account limit still applies.
Entries in other sessions are unchanged. ledger() is the session's money at any moment.

Tables are created on first use with the same additive CREATE IF NOT EXISTS pattern as
orders/rules.py; nothing existing is altered.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("atip.sessions")

MODES = ("MANUAL", "CONFIRM", "AUTO")
STATUSES = ("RUNNING", "PAUSED", "OFF")
MAX_SESSIONS = 10
CONFIG_PATH = Path("atip_data") / "config.json"

# Ten starting slots. Limits are deliberately small; the owner edits them.
DEFAULTS = [
    ("S1", "Momentum", "MRI and ATIP strength"),
    ("S2", "Buy zones", "ZPI entries"),
    ("S3", "Breakouts", "50-DMA breakouts"),
    ("S4", "Swing", "Weekly swing"),
    ("S5", "Options", "Defined-risk option spreads"),
    ("S6", "Recovery", "RRI rebounds"),
    ("S7", "Pairs", "Pair spreads"),
    ("S8", "Sector", "Sector rotation"),
    ("S9", "Dividend", "Dividend capture"),
    ("S10", "Intraday", "Opening-range intraday"),
]
DEFAULT_LIMITS = {"max_trades": 5, "max_loss_value": 1500.0, "capital_cap": 100000.0}
# A cash pool (start_pool): the owner's rules of 26 / 29 Oct 2026.
POOL_DEFAULTS = {"target_pct": 3.0, "stop_pct": 2.0, "min_buy_value": 12000.0, "max_occurrences": 500}
POOL_COLUMNS = {"start_capital": "REAL", "target_pct": "REAL", "stop_pct": "REAL",
                "min_buy_value": "REAL", "max_occurrences": "INTEGER", "pool_started_at": "TEXT",
                "auto_pick": "INTEGER",                 # orders/autopick.py starts the next occurrence
                "trail_pct": "REAL", "hold_days": "REAL"}   # the trailing exit; NULL = sell at +target_pct
# The owner's trailing exit (30 Oct 2026): trail 1 % under the high once up target_pct, and after
# 15 days sell at target_pct or more. Pools start without it unless it is asked for.
OWNER_TRAIL = {"trail_pct": 1.0, "hold_days": 15.0}
EXIT_KEYS = ("target_pct", "stop_pct", "trail_pct", "hold_days")
POOL_TAG = "ATIP-"                      # paper_order.tag of a session's orders: ATIP-S1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_sessions (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            strategy        TEXT DEFAULT '',
            mode            TEXT NOT NULL DEFAULT 'CONFIRM',
            status          TEXT NOT NULL DEFAULT 'PAUSED',
            max_trades      INTEGER,
            max_loss_value  REAL,
            capital_cap     REAL,
            universe        TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )""")
    try:                                    # additive migration, as orders/rules.py does it
        have = {r[1] for r in conn.execute("PRAGMA table_info(trading_sessions)").fetchall()}
        for col, typ in POOL_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE trading_sessions ADD COLUMN {col} {typ}")
    except Exception as e:
        log.warning(f"  trading_sessions migration skipped: {e}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_rules (
            rule_id     TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            linked_at   TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_rules_sid ON session_rules(session_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_settings (
            name        TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            at          TEXT NOT NULL,
            event       TEXT NOT NULL,
            detail      TEXT DEFAULT ''
        )""")
    conn.commit()


def _event(conn, sid, event, detail="") -> None:
    conn.execute("INSERT INTO session_events (session_id, at, event, detail) VALUES (?, ?, ?, ?)",
                 (sid, _now(), event, detail if isinstance(detail, str) else json.dumps(detail)))


def seed(conn) -> int:
    """Create the ten default slots that do not exist yet. Returns how many were added."""
    ensure_tables(conn)
    have = {_row(r)["id"] for r in conn.execute("SELECT id FROM trading_sessions").fetchall()}
    added = 0
    for sid, name, strat in DEFAULTS:
        if sid in have:
            continue
        ts = _now()
        conn.execute("INSERT INTO trading_sessions (id, name, strategy, mode, status, max_trades, "
                     "max_loss_value, capital_cap, universe, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'CONFIRM', 'PAUSED', ?, ?, ?, '', ?, ?)",
                     (sid, name, strat, DEFAULT_LIMITS["max_trades"], DEFAULT_LIMITS["max_loss_value"],
                      DEFAULT_LIMITS["capital_cap"], ts, ts))
        _event(conn, sid, "CREATED", "default slot")
        added += 1
    conn.commit()
    return added


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _stats(conn) -> dict:
    """Per-session counts from the linked order rules: today's entries, open watches,
    awaiting confirmation, executed, and realised value where the rule records a fill."""
    out = {}
    try:
        rows = conn.execute(
            "SELECT s.session_id AS sid, r.status AS status, r.created_at AS created_at "
            "FROM session_rules s JOIN order_rules r ON r.id = s.rule_id").fetchall()
    except Exception:                       # order_rules not created yet
        return out
    today = str(date.today())
    for r in rows:
        r = _row(r)
        st = out.setdefault(r["sid"], {"rules": 0, "today": 0, "watching": 0, "awaiting": 0, "executed": 0})
        st["rules"] += 1
        if str(r["created_at"] or "")[:10] == today:
            st["today"] += 1
        if r["status"] == "ACTIVE":
            st["watching"] += 1
        elif r["status"] == "PENDING_CONFIRMATION":
            st["awaiting"] += 1
        elif r["status"] == "EXECUTED":
            st["executed"] += 1
    return out


def list_sessions(conn) -> dict:
    seed(conn)
    stats = _stats(conn)
    rows = [_row(r) for r in conn.execute("SELECT * FROM trading_sessions").fetchall()]
    rows.sort(key=lambda r: int(str(r["id"]).lstrip("S") or 0))
    empty = {"rules": 0, "today": 0, "watching": 0, "awaiting": 0, "executed": 0}
    for r in rows:
        r["stats"] = stats.get(r["id"], dict(empty))
        r["pool"] = ledger(conn, r["id"], s=r) if r.get("start_capital") else None
    return {"sessions": rows, "engine_enforced": True, "auto_bypass": auto_send_enabled(conn),
            "running": sum(1 for r in rows if r["status"] == "RUNNING"),
            "auto": sum(1 for r in rows if r["mode"] == "AUTO")}


def get_session(conn, sid: str) -> dict:
    seed(conn)
    r = conn.execute("SELECT * FROM trading_sessions WHERE id = ?", (sid,)).fetchone()
    if not r:
        raise LookupError(f"no session {sid!r}")
    return _row(r)


def set_mode(conn, sid: str, mode: str, ack: bool = False) -> dict:
    mode = str(mode or "").upper()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    cur = get_session(conn, sid)
    if mode == "AUTO" and cur["mode"] != "AUTO":
        if not ack:
            raise ValueError("AUTO sends orders without asking: confirm with ack=true")
        _auto_guards("AUTO")
    conn.execute("UPDATE trading_sessions SET mode = ?, updated_at = ? WHERE id = ?", (mode, _now(), sid))
    _event(conn, sid, "MODE", {"from": cur["mode"], "to": mode, "ack": bool(ack)})
    conn.commit()
    return {**get_session(conn, sid), "engine_enforced": True, "auto_bypass": auto_send_enabled(conn)}


def set_status(conn, sid: str, status: str, reason: str = "") -> dict:
    status = str(status or "").upper()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    cur = get_session(conn, sid)
    conn.execute("UPDATE trading_sessions SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), sid))
    _event(conn, sid, "STATUS", {"from": cur["status"], "to": status, "reason": reason})
    conn.commit()
    return {**get_session(conn, sid), "engine_enforced": True, "auto_bypass": auto_send_enabled(conn)}


def update_session(conn, sid: str, fields: dict) -> dict:
    """Name, strategy, universe, the three limits and a pool's exit rules. Numbers must be positive;
    trail_pct and hold_days also take "off". A pool's start_capital is set by start_pool, which also
    starts its books afresh. New exit rules reach the session's entries that have not been sent yet;
    a trade already open keeps the exit it was bought with."""
    s = get_session(conn, sid)
    sets, vals = [], []
    for k in ("name", "strategy", "universe"):
        if k in fields:
            v = str(fields[k] or "").strip()
            if k == "name" and not v:
                raise ValueError("name cannot be empty")
            sets.append(f"{k} = ?"); vals.append(v[:80])
    for k, cast in (("max_trades", int), ("max_loss_value", float), ("capital_cap", float),
                    ("target_pct", float), ("stop_pct", float), ("min_buy_value", float)):
        if k in fields:
            try:
                v = cast(fields[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if v <= 0:
                raise ValueError(f"{k} must be positive")
            sets.append(f"{k} = ?"); vals.append(v)
    for k in ("trail_pct", "hold_days"):
        if k in fields:
            sets.append(f"{k} = ?"); vals.append(_optional(k, fields[k]))
    if "trail_pct" in fields and vals[sets.index("trail_pct = ?")] and not fields.get("stop_pct", s.get("stop_pct")):
        raise ValueError("a trailing exit trails the stop: give the session a stop_pct first")
    if "auto_pick" in fields:
        sets.append("auto_pick = ?"); vals.append(1 if _truthy(fields["auto_pick"]) else 0)
    if not sets:
        raise ValueError("nothing to update")
    conn.execute(f"UPDATE trading_sessions SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                 (*vals, _now(), sid))
    _event(conn, sid, "EDIT", {k: fields[k] for k in fields})
    if s.get("start_capital") and any(k in fields for k in EXIT_KEYS):
        _reapply_pool_rules(conn, sid)
    conn.commit()
    return get_session(conn, sid)


def _optional(k: str, v):
    """trail_pct / hold_days: None, "", "off" or 0 switches it off; else a positive number."""
    if v is None or str(v).strip().lower() in ("", "off", "none", "null"):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{k} must be a number or off")
    if v < 0:
        raise ValueError(f"{k} must be positive (or off)")
    return v or None


def _reapply_pool_rules(conn, sid: str) -> int:
    """The session's exit rules onto its entries that are still waiting to be sent."""
    s = get_session(conn, sid)
    n = 0
    for r in _linked_rules(conn, sid):
        if (r.get("role") or "ENTRY") == "ENTRY" and r.get("status") in ("ACTIVE", "PENDING_CONFIRMATION"):
            _apply_pool_rules(conn, s, r)
            n += 1
    return n


def exit_rule(s: dict) -> str:
    """A pool's exit in words, for the pages and the command line."""
    tgt = float(s.get("target_pct") or POOL_DEFAULTS["target_pct"])
    stop = f"stop −{float(s['stop_pct']):g}%" if s.get("stop_pct") else "no stop"
    if not s.get("trail_pct"):
        return f"sells all at +{tgt:g}%, {stop}"
    days = s.get("hold_days")
    return (f"{stop}; from +{tgt:g}% the stop trails {float(s['trail_pct']):g}% under the high"
            + (f"; after {float(days):g} days sells at +{tgt:g}% or more" if days else ""))


def link_rule(conn, sid: str, rule_id: str) -> dict:
    s = get_session(conn, sid)
    try:
        exists = conn.execute("SELECT * FROM order_rules WHERE id = ?", (rule_id,)).fetchone()
    except Exception:
        exists = None
    if not exists:
        raise LookupError(f"no order rule {rule_id!r}")
    rule = _row(exists)
    pool = None
    if s.get("start_capital") and (rule.get("role") or "ENTRY") == "ENTRY":
        pool = _apply_pool_rules(conn, s, rule)
    conn.execute("DELETE FROM session_rules WHERE rule_id = ?", (rule_id,))
    conn.execute("INSERT INTO session_rules (rule_id, session_id, linked_at) VALUES (?, ?, ?)",
                 (rule_id, sid, _now()))
    _event(conn, sid, "LINK", {"rule_id": rule_id, **({"pool_rules": pool} if pool else {})})
    conn.commit()
    return {"rule_id": rule_id, "session_id": sid, "pool_rules": pool}


def _apply_pool_rules(conn, s: dict, rule: dict) -> dict:
    """An entry joining a pool session buys with the session's whole cash, sells everything at
    +target_pct and stops at -stop_pct (a pool without a stop_pct keeps the rule's own stop).
    With the pool's trail_pct the entry has no sale at +target_pct: its stop trails trail_pct under
    the best price from +target_pct on, and its TARGET leg waits hold_days (none without them).
    Only a BUY that has not been sent yet can join: a pool holds delivery stock."""
    if rule["side"] != "BUY":
        raise ValueError(f"session {s['id']} is a cash pool: it buys delivery stock, not a {rule['side']} entry")
    if rule["status"] not in ("ACTIVE", "PENDING_CONFIRMATION"):
        raise ValueError(f"rule {rule['id'][:8]} is {rule['status']}: only an entry still watching can join a pool")
    from orders.rules import _migrate_bracket_columns
    _migrate_bracket_columns(conn)                      # the trailing / time-bound columns
    tgt = float(s.get("target_pct") or POOL_DEFAULTS["target_pct"])
    stop = s.get("stop_pct")
    trail, days = s.get("trail_pct"), s.get("hold_days")
    if trail and not stop:
        raise ValueError(f"session {s['id']} trails its stop but has no stop_pct")
    conn.execute("UPDATE order_rules SET quantity_type = 'SESSION', quantity_value = 100, product_type = 'CNC', "
                 "bracket_target_pct = ?, bracket_target2_pct = NULL, bracket_target_split = NULL, "
                 "bracket_auto_exit = 1, trail_enabled = ?, trail_type = ?, trail_value = ?, trail_jump = ?, "
                 "trail_after_pct = ?, bracket_target_after_days = ?, updated_at = ? WHERE id = ?",
                 (None if trail and not days else tgt, 1 if trail else 0, "PERCENT" if trail else None,
                  float(trail) if trail else None, 0 if trail else None, tgt if trail else None,
                  float(days) if trail and days else None, _now(), rule["id"]))
    if stop:
        trig = float(rule["resolved_trigger_price"])
        conn.execute("UPDATE order_rules SET bracket_stop_pct = ?, stoploss_type = 'PERCENT', stoploss_value = ?, "
                     "resolved_stoploss_price = ? WHERE id = ?",
                     (float(stop), -float(stop), round(trig * (1 - float(stop) / 100), 2), rule["id"]))
    return {"quantity": "whole session cash", "target_pct": tgt, "stop_pct": float(stop) if stop else None,
            "trail_pct": float(trail) if trail else None, "hold_days": float(days) if trail and days else None,
            "exit": exit_rule(s)}


def events(conn, sid: str | None = None, limit: int = 50) -> list:
    ensure_tables(conn)
    limit = max(1, min(int(limit or 50), 500))
    if sid:
        rows = conn.execute("SELECT * FROM session_events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                            (sid, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM session_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def pause_all(conn, reason: str) -> int:
    """Called by the kill switch so the session list matches the halt. Returns how many changed."""
    ensure_tables(conn)
    running = [_row(r)["id"] for r in
               conn.execute("SELECT id FROM trading_sessions WHERE status = 'RUNNING'").fetchall()]
    for sid in running:
        conn.execute("UPDATE trading_sessions SET status = 'PAUSED', updated_at = ? WHERE id = ?", (_now(), sid))
        _event(conn, sid, "STATUS", {"from": "RUNNING", "to": "PAUSED", "reason": f"kill switch: {reason}"})
    conn.commit()
    return len(running)


# -- UXR-04: the engine gate ------------------------------------------------------------------

def session_of_rule(conn, rule: dict) -> str | None:
    """The session a rule belongs to: its own link, else its parent's (bracket legs inherit)."""
    ensure_tables(conn)
    for rid in (rule.get("id"), rule.get("parent_rule_id")):
        if not rid:
            continue
        r = conn.execute("SELECT session_id FROM session_rules WHERE rule_id = ?", (rid,)).fetchone()
        if r:
            return _row(r)["session_id"]
    return None


def _linked_rules(conn, sid: str) -> list:
    """Every order rule of a session: the linked ones and the bracket legs they spawned."""
    try:
        rows = conn.execute(
            "SELECT r.* FROM order_rules r WHERE r.id IN (SELECT rule_id FROM session_rules WHERE session_id = ?) "
            "OR r.parent_rule_id IN (SELECT rule_id FROM session_rules WHERE session_id = ?)", (sid, sid)).fetchall()
    except Exception:                       # order_rules not created yet
        return []
    return [_row(r) for r in rows]


def _charges(conn, rules: list) -> dict:
    """Rupees charged per executed rule id: the paper fill's own charges (paper_order.brokerage), else
    the statutory estimate the backtests use -- a real broker's contract note is not read here."""
    done = [r for r in rules if r.get("status") == "EXECUTED" and r.get("execution_price")]
    if not done:
        return {}
    paid = {}
    ids = [r["dhan_order_id"] for r in done if r.get("dhan_order_id")]
    try:
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            for p in conn.execute(f"SELECT order_id, brokerage FROM paper_order WHERE order_id IN "
                                  f"({','.join('?' * len(part))})", tuple(part)).fetchall():
                p = _row(p)
                paid[p["order_id"]] = float(p["brokerage"] or 0)
    except Exception:                       # no paper_order table: nothing filled on paper
        pass
    model = None
    out = {}
    for r in done:
        if r.get("dhan_order_id") in paid:
            out[r["id"]] = paid[r["dhan_order_id"]]
            continue
        if model is None:
            from backtest.costs import cost_model_from_config
            model = cost_model_from_config()
        value = float(r["execution_price"]) * float(r.get("execution_quantity") or 0)
        out[r["id"]] = round(model.total(r["side"], value), 2)
    return out


def _trades(rules: list, charges: dict | None = None, since: str | None = None) -> list:
    """One entry per executed ENTRY rule: value, realised P&L from its executed exit legs net of
    `charges` (rule id -> rupees, _charges), risk, closed, the shares still held and their cost, and
    the trade's cash flow so far (money out negative). `since` leaves out entries sent before it."""
    charges = charges or {}
    kids = {}
    for r in rules:
        if r.get("parent_rule_id"):
            kids.setdefault(r["parent_rule_id"], []).append(r)
    out = []
    for e in rules:
        if (e.get("role") or "ENTRY") != "ENTRY" or e.get("status") != "EXECUTED" or not e.get("execution_price"):
            continue
        if since and str(e.get("updated_at") or "") < since:
            continue
        px, qty = float(e["execution_price"]), float(e.get("execution_quantity") or 0)
        sign = 1 if e["side"] == "BUY" else -1
        legs = kids.get(e["id"], [])
        done = [l for l in legs if l.get("status") == "EXECUTED" and l.get("execution_price")]
        closed_qty = min(qty, sum(float(l.get("execution_quantity") or 0) for l in done))
        fee_in = charges.get(e["id"], 0.0)
        fee_out = sum(charges.get(l["id"], 0.0) for l in done)
        fee_share = fee_in / qty if qty else 0.0             # the entry's charges, per share
        pnl = sum(sign * (float(l["execution_price"]) - px) * float(l.get("execution_quantity") or 0)
                  for l in done) - fee_out - fee_share * closed_qty
        held = qty - closed_qty
        cash = sum(sign * float(l["execution_price"]) * float(l.get("execution_quantity") or 0) for l in done) \
            - fee_out - sign * px * qty - fee_in
        stop = e.get("resolved_stoploss_price")
        risk = abs(px - float(stop)) * qty if stop else None
        live = [l for l in legs if l.get("status") == "ACTIVE"]
        stop_leg = next((l for l in live if l.get("role") == "STOP"), None)
        sell_leg = next((l for l in live if l.get("role") == "TARGET"), None)
        act = (stop_leg or {}).get("trail_activate_price")
        out.append({"rule_id": e["id"], "symbol": e["symbol"], "side": e["side"], "entry": px, "qty": qty,
                    "stop_now": stop_leg and stop_leg.get("resolved_trigger_price"),
                    "trailing": bool(stop_leg and stop_leg.get("trail_enabled")
                                     and (not act or float(stop_leg.get("trail_high_water") or 0) >= float(act))),
                    "sell_at": sell_leg and sell_leg.get("resolved_trigger_price"),
                    "sell_from": sell_leg and sell_leg.get("not_before"),
                    "value": px * qty, "date": str(e.get("updated_at") or "")[:10], "at": str(e.get("updated_at") or ""),
                    "closed": bool(qty) and closed_qty >= qty,
                    "exit_date": max((str(l.get("updated_at") or "")[:10] for l in done), default=None),
                    "exit_price": round(sum(float(l["execution_price"]) * float(l.get("execution_quantity") or 0)
                                            for l in done) / closed_qty, 2) if closed_qty else None,
                    "pnl": round(pnl, 2), "charges": round(fee_in + fee_out, 2),
                    "held_qty": held, "held_cost": round(sign * px * held + fee_share * held, 2),
                    "cash_flow": round(cash, 2), "risk": round(risk, 2) if risk else None,
                    "r": round(pnl / risk, 2) if risk and done else None})
    return out


def usage(conn, sid: str, on: str | None = None) -> dict:
    """Today's use of a session's limits: entries executed, realised P&L, open entry value."""
    on = on or str(date.today())
    rules = _linked_rules(conn, sid)
    tr = _trades(rules, _charges(conn, rules))
    return {"trades_today": sum(1 for t in tr if t["date"] == on),
            "realised_today": round(sum(t["pnl"] for t in tr if t["exit_date"] == on), 2),
            "open_value": round(sum(t["value"] for t in tr if not t["closed"]), 2)}


# -- cash pools (29 Oct 2026) -----------------------------------------------------------------
#
# The owner's words for it: Rs 2.5 lakh at Rs 25,000 a session is 10 concurrent sessions. When one
# hits +3 % its whole amount starts the next occurrence of that session, about 2.7 % bigger after
# charges, compounding until the session has run max_occurrences (500) of them. Here an occurrence
# is one trade of the pool: a buy with all of its cash and the sale that ends it.

def _occurrences(start: float, trades: list) -> list:
    """The pool's trades in order as occurrences: #, stock, the amount it started with, the amount it
    ended with (cash after the sale, charges paid) and the change. An open one has no end yet."""
    out, amount = [], float(start)
    for n, t in enumerate(sorted(trades, key=lambda t: t["at"]), 1):
        o = {"n": n, "symbol": t["symbol"], "started": t["date"], "start_amount": round(amount, 2),
             "entry": t["entry"], "qty": t["qty"], "exit_price": t["exit_price"], "closed": t["closed"],
             "ended": t["exit_date"] if t["closed"] else None, "end_amount": None, "pnl": None, "pct": None}
        if t["closed"]:
            o.update(end_amount=round(amount + t["pnl"], 2), pnl=t["pnl"],
                     pct=round(t["pnl"] / amount * 100, 2) if amount else None)
            amount += t["pnl"]
        out.append(o)
    return out


def ledger(conn, sid: str, s: dict | None = None, history: bool = False) -> dict | None:
    """A pool session's money now: start_capital + booked P&L - cost of what it still holds = cash;
    cash + open trades at their marks = equity; and where it is in its run of occurrences (every
    one with `history`, else the latest). None for a session without a start_capital."""
    s = s or get_session(conn, sid)
    start = float(s.get("start_capital") or 0)
    if start <= 0:
        return None
    from portfolio.pnl import mark_price
    rules = _linked_rules(conn, sid)
    tr = _trades(rules, _charges(conn, rules), since=s.get("pool_started_at"))
    held = []
    for t in tr:
        if t["held_qty"] <= 0:
            continue
        mark = mark_price(conn, t["symbol"]) or t["entry"]
        value = (1 if t["side"] == "BUY" else -1) * mark * t["held_qty"]
        held.append({"symbol": t["symbol"], "qty": t["held_qty"], "entry": t["entry"], "mark": mark,
                     "value": round(value, 2), "cost": t["held_cost"], "unrealised": round(value - t["held_cost"], 2),
                     "since": t["date"], "rule_id": t["rule_id"], "stop": t["stop_now"], "trailing": t["trailing"],
                     "sell_at": t["sell_at"], "sell_from": t["sell_from"]})
    cash = start + sum(t["cash_flow"] for t in tr)
    booked = sum(t["pnl"] for t in tr)
    closed = [t for t in tr if t["closed"]]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    equity = cash + sum(h["value"] for h in held)
    occ = _occurrences(start, tr)
    cap = int(s.get("max_occurrences") or POOL_DEFAULTS["max_occurrences"])
    return {"start_capital": round(start, 2), "since": s.get("pool_started_at"),
            "cash": round(cash, 2), "booked": round(booked, 2), "charges": round(sum(t["charges"] for t in tr), 2),
            "open": held, "open_value": round(sum(h["value"] for h in held), 2),
            "unrealised": round(sum(h["unrealised"] for h in held), 2),
            "equity": round(equity, 2), "pnl": round(equity - start, 2),
            "return_pct": round((equity - start) / start * 100, 2),
            "trades": len(tr), "closed": len(closed), "wins": wins,
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "occurrence": len(occ), "max_occurrences": cap, "finished": len(closed) >= cap and not held,
            "latest": occ[-1] if occ else None, **({"occurrences": occ} if history else {}),
            "target_pct": s.get("target_pct"), "stop_pct": s.get("stop_pct"),
            "trail_pct": s.get("trail_pct"), "hold_days": s.get("hold_days"), "exit_rule": exit_rule(s),
            "min_buy_value": s.get("min_buy_value")}


def pool_quantity(conn, sid: str, price: float, pct: float = 100.0) -> dict:
    """Shares a pool session's next buy gets at `price`: as many as pct % of its cash pays for,
    slippage and the buy's charges included, so the session never spends money it does not have."""
    L = ledger(conn, sid)
    if not L:
        return {"quantity": 0, "cash": None, "budget": None, "reason": f"session {sid} has no starting capital"}
    budget = max(0.0, L["cash"]) * min(100.0, max(0.0, float(pct))) / 100
    if _paper():
        from orders.paper import paper_cost_model, settings
        model, bps = paper_cost_model(), float(settings().get("paper_slippage_bps") or 0)
    else:
        from backtest.costs import cost_model_from_config
        model, bps = cost_model_from_config(), 10.0           # a market order can fill a little above
    px = float(price) * (1 + bps / 10000)
    q = int(budget // px) if px > 0 else 0
    while q > 0 and q * px + model.total("BUY", q * px) > budget:
        q -= 1
    return {"quantity": q, "cash": L["cash"], "budget": round(budget, 2),
            "reason": None if q else f"session {sid}'s Rs {budget:,.0f} buys no share at Rs {float(price):,.2f}"}


def _has_open_trade(conn, sid: str, since: str | None) -> bool:
    return any(t["held_qty"] > 0 for t in _trades(_linked_rules(conn, sid), since=since))


def start_pool(conn, sid: str, capital: float, target_pct: float = None, stop_pct: float = None,
               min_buy_value: float = None, max_occurrences: int = None, trail_pct=None, hold_days=None) -> dict:
    """Give a session its own money and start its books from now: its entries buy with the whole
    cash, sell everything at +target_pct (or trail by trail_pct from there, hold_days before the sale
    at +target_pct), stop at -stop_pct, for max_occurrences trades. Refused while it holds a trade."""
    s = get_session(conn, sid)
    capital, vals = _pool_values(capital, target_pct, stop_pct, min_buy_value, max_occurrences, trail_pct, hold_days)
    if s.get("start_capital") and _has_open_trade(conn, sid, s.get("pool_started_at")):
        raise ValueError(f"session {sid} holds an open trade: let it exit before restarting its pool")
    ts = _now()
    conn.execute("UPDATE trading_sessions SET start_capital = ?, target_pct = ?, stop_pct = ?, min_buy_value = ?, "
                 "max_occurrences = ?, trail_pct = ?, hold_days = ?, pool_started_at = ?, updated_at = ? WHERE id = ?",
                 (capital, vals["target_pct"], vals["stop_pct"], vals["min_buy_value"], vals["max_occurrences"],
                  vals["trail_pct"], vals["hold_days"], ts, ts, sid))
    _event(conn, sid, "POOL", {"start_capital": capital, **vals,
                               "replaces": s.get("start_capital") and {"start_capital": s["start_capital"],
                                                                       "since": s.get("pool_started_at")}})
    _reapply_pool_rules(conn, sid)
    conn.commit()
    return {**get_session(conn, sid), "pool": ledger(conn, sid)}


def end_pool(conn, sid: str) -> dict:
    """Take a session's cash pool away (its trades stay in the books). Refused while it holds a trade."""
    s = get_session(conn, sid)
    if not s.get("start_capital"):
        return s
    if _has_open_trade(conn, sid, s.get("pool_started_at")):
        raise ValueError(f"session {sid} holds an open trade: let it exit before ending its pool")
    conn.execute("UPDATE trading_sessions SET start_capital = NULL, pool_started_at = NULL, updated_at = ? WHERE id = ?",
                 (_now(), sid))
    _event(conn, sid, "POOL_END", {"start_capital": s["start_capital"], "since": s.get("pool_started_at")})
    conn.commit()
    return get_session(conn, sid)


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def _pool_values(capital, target_pct=None, stop_pct=None, min_buy_value=None, max_occurrences=None,
                 trail_pct=None, hold_days=None):
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        raise ValueError("capital must be a number")
    if capital <= 0:
        raise ValueError("capital must be positive")
    vals = {"target_pct": target_pct, "stop_pct": stop_pct, "min_buy_value": min_buy_value,
            "max_occurrences": max_occurrences}
    for k, v in vals.items():
        v = POOL_DEFAULTS[k] if v in (None, "") else v
        try:
            vals[k] = int(v) if k == "max_occurrences" else float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a {'whole number' if k == 'max_occurrences' else 'number'}")
        if vals[k] <= 0:
            raise ValueError(f"{k} must be positive")
    if vals["min_buy_value"] > capital:
        raise ValueError(f"min_buy_value Rs {vals['min_buy_value']:,.0f} is more than the Rs {capital:,.0f} capital")
    vals["trail_pct"], vals["hold_days"] = _optional("trail_pct", trail_pct), _optional("hold_days", hold_days)
    return capital, vals


def setup_pools(conn, capital: float, ids=None, target_pct=None, stop_pct=None, min_buy_value=None,
                max_occurrences=None, total=None, auto_pick=None, trail_pct=None, hold_days=None) -> dict:
    """start_pool for every session in `ids` (default all of them). Given `total`, the sessions are
    total // capital of them, S1 upwards -- Rs 2,50,000 at Rs 25,000 is S1-S10 -- and any other
    session's pool is ended. auto_pick True / False switches orders/autopick.py on or off for the
    sessions started. A session that holds an open trade is skipped and named."""
    capital, _ = _pool_values(capital, target_pct, stop_pct, min_buy_value, max_occurrences,
                              trail_pct, hold_days)              # bad figures change nothing
    seed(conn)
    every = sorted((_row(r)["id"] for r in conn.execute("SELECT id FROM trading_sessions").fetchall()),
                   key=lambda i: int(str(i).lstrip("S") or 0))
    ended = []
    if total not in (None, ""):
        try:
            total = float(total)
        except (TypeError, ValueError):
            raise ValueError("total must be a number")
        n = int(total // capital)
        if n < 1:
            raise ValueError(f"Rs {total:,.0f} is less than one Rs {capital:,.0f} session")
        if n > len(every):
            raise ValueError(f"Rs {total:,.0f} at Rs {capital:,.0f} a session is {n} sessions; ATIP runs at most "
                             f"{len(every)} -- raise the amount per session to Rs {total / len(every):,.0f} or more")
        ids = every[:n]
    ids = ids or every
    done, skipped = [], {}
    for sid in ids:
        try:
            done.append(start_pool(conn, sid, capital, target_pct, stop_pct, min_buy_value, max_occurrences,
                                   trail_pct, hold_days)["id"])
            if auto_pick not in (None, ""):
                update_session(conn, sid, {"auto_pick": auto_pick})
        except (ValueError, LookupError) as e:
            skipped[sid] = str(e)
    if total not in (None, ""):
        for sid in every[len(ids):]:
            try:
                if get_session(conn, sid).get("start_capital"):
                    end_pool(conn, sid)
                    ended.append(sid)
            except ValueError as e:
                skipped[sid] = str(e)
    return {"sessions": done, "skipped": skipped, "ended": ended, "each": capital,
            "total": round(capital * len(done), 2), "left_over": round(total - capital * len(ids), 2) if total else None}


def gate(conn, rule: dict, est_value: float | None = None) -> str | None:
    """None when the rule may fire, else the reason its session holds it. Exit legs and rules
    without a session always pass. A session that cannot be read holds its entries (fail closed)."""
    if (rule.get("role") or "ENTRY") != "ENTRY":
        return None
    sid = session_of_rule(conn, rule)
    if not sid:
        return None
    try:
        s = get_session(conn, sid)
        if s["status"] != "RUNNING":
            return f"session {sid} is {s['status']}"
        if s["mode"] == "MANUAL":
            return f"session {sid} is MANUAL -- place this order yourself"
        u = usage(conn, sid)
        if s.get("max_trades") and u["trades_today"] >= int(s["max_trades"]):
            return f"session {sid} has used its {s['max_trades']} trades today"
        if s.get("max_loss_value") and -u["realised_today"] >= float(s["max_loss_value"]):
            return f"session {sid} lost Rs {-u['realised_today']:,.0f} today (limit Rs {s['max_loss_value']:,.0f})"
        if s.get("start_capital"):
            # A cash pool: its own money, not the cap. Under the minimum buy means its cash is in an
            # open trade (or lost); the entry waits for that trade to exit.
            L = ledger(conn, sid, s=s)
            if L["occurrence"] >= L["max_occurrences"]:
                return f"session {sid} has run all {L['max_occurrences']} of its occurrences"
            floor = float(s.get("min_buy_value") or 0)
            if floor and L["cash"] < floor:
                return (f"session {sid} has Rs {L['cash']:,.0f} free, under its Rs {floor:,.0f} minimum buy"
                        + (f" -- waiting for {', '.join(h['symbol'] for h in L['open'])} to exit" if L["open"] else ""))
            if est_value and est_value > L["cash"] + 0.01:
                return f"session {sid}: a Rs {est_value:,.0f} buy is more than its Rs {L['cash']:,.0f} cash"
        elif s.get("capital_cap") and est_value and u["open_value"] + est_value > float(s["capital_cap"]):
            return (f"session {sid}: Rs {u['open_value'] + est_value:,.0f} open would exceed its "
                    f"Rs {s['capital_cap']:,.0f} capital cap")
    except Exception as e:
        log.error(f"  session gate for {sid} could not be read: {e}")
        return f"session {sid} could not be read ({e})"
    return None


def journal(conn) -> dict:
    """Per-session trades, successes, win rate, average R and P&L (UXR-09)."""
    seed(conn)
    out = []
    for s in list_sessions(conn)["sessions"]:
        rules = _linked_rules(conn, s["id"])
        tr = _trades(rules, _charges(conn, rules))
        closed = [t for t in tr if t["closed"]]
        wins = [t for t in closed if t["pnl"] > 0]
        rs = [t["r"] for t in closed if t["r"] is not None]
        out.append({"id": s["id"], "name": s["name"], "mode": s["mode"], "status": s["status"],
                    "trades": len(tr), "closed": len(closed), "open": len(tr) - len(closed),
                    "successful": len(wins), "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
                    "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                    "pnl": round(sum(t["pnl"] for t in tr), 2), "recent": sorted(tr, key=lambda t: t["date"])[-5:],
                    "pool": s.get("pool")})
    return {"sessions": out}


# -- the owner's AUTO send switch -------------------------------------------------------------

def _auto_guards(what: str) -> None:
    from orders.risk import halted
    h, why = halted()
    if h:
        raise ValueError(f"trading is halted ({why}) -- resume before turning on {what}")
    try:
        from orders.environment import broker_env
        env = broker_env()
    except Exception:
        env = "PAPER"
    if env == "LIVE" and not str(_config().get("sebi_algo_id") or "").strip():
        raise ValueError(f"LIVE {what} needs a SEBI Algo ID (config.json sebi_algo_id)")


def _paper() -> bool:
    try:
        from orders.environment import PAPER, broker_env
        return broker_env() == PAPER
    except Exception:
        return False


def auto_send_enabled(conn) -> bool:
    ensure_tables(conn)
    r = conn.execute("SELECT value FROM session_settings WHERE name = 'auto_send'").fetchone()
    return bool(r) and _row(r)["value"] == "ON"


def set_auto_send(conn, on: bool, ack: bool = False, reason: str = "") -> dict:
    """The account-wide switch that lets AUTO sessions send without the confirmation tap."""
    if on:
        if not ack:
            raise ValueError("auto send places orders without asking you: confirm with ack=true")
        if not _paper():
            raise ValueError("auto send is PAPER only: broker_env is not PAPER, so every order asks you")
        _auto_guards("auto send")
    was = auto_send_enabled(conn)
    conn.execute("INSERT OR REPLACE INTO session_settings (name, value, updated_at) VALUES ('auto_send', ?, ?)",
                 ("ON" if on else "OFF", _now()))
    _event(conn, None, "AUTO_SEND", {"from": "ON" if was else "OFF", "to": "ON" if on else "OFF",
                                     "ack": bool(ack), "reason": reason})
    conn.commit()
    return {"auto_send": bool(on), "was": was}


def sends_without_asking(conn, rule: dict) -> bool:
    """True when this rule's trigger should go straight to the order: an ENTRY of a RUNNING
    AUTO session while the owner's auto send switch is on. Exit legs keep their own setting."""
    if (rule.get("role") or "ENTRY") != "ENTRY" or not auto_send_enabled(conn) or not _paper():
        return False
    sid = session_of_rule(conn, rule)
    if not sid:
        return False
    s = get_session(conn, sid)
    return s["status"] == "RUNNING" and s["mode"] == "AUTO"


def order_tag(conn, rule: dict) -> str | None:
    """The tag a session's order carries to the broker (paper_order.tag): ATIP-S1. None outside a session."""
    sid = session_of_rule(conn, rule)
    return f"{POOL_TAG}{sid}" if sid else None


def pool_of_tag(conn, tag) -> str | None:
    """The cash-pool session an order tagged ATIP-S1 belongs to; None for any other order. A pool's
    orders are sized by its own cash, so the account's max_order_value does not cap them."""
    tag = str(tag or "")
    if not tag.startswith(POOL_TAG):
        return None
    try:
        return tag[len(POOL_TAG):] if get_session(conn, tag[len(POOL_TAG):]).get("start_capital") else None
    except Exception:                       # unknown session: no exemption
        return None


if __name__ == "__main__":
    import argparse

    from db.schema import get_connection

    ap = argparse.ArgumentParser(prog="python -m orders.sessions", description="ATIP trading sessions and cash pools")
    ap.add_argument("--pools", type=float, metavar="RUPEES",
                    help="give every session (or --ids) its own cash pool of this many rupees, books from now")
    ap.add_argument("--ids", help="comma-separated sessions for --pools, e.g. S1,S2")
    ap.add_argument("--target", type=float, help=f"sell everything at +x %% (default {POOL_DEFAULTS['target_pct']})")
    ap.add_argument("--stop", type=float, help=f"stop at -x %% (default {POOL_DEFAULTS['stop_pct']})")
    ap.add_argument("--min-buy", type=float, help=f"smallest buy, Rs (default {POOL_DEFAULTS['min_buy_value']:,.0f})")
    ap.add_argument("--occurrences", type=int, help=f"trades each session runs (default {POOL_DEFAULTS['max_occurrences']})")
    ap.add_argument("--total", type=float, help="with --pools: the money in all; sessions = total // per-session amount")
    ap.add_argument("--auto-pick", choices=("on", "off"), help="with --pools: pick each next stock automatically")
    ap.add_argument("--trail", metavar="PCT|off",
                    help=f"no sale at --target: from there trail the stop PCT under the high (owner: {OWNER_TRAIL['trail_pct']:g})")
    ap.add_argument("--hold-days", metavar="DAYS|off",
                    help=f"with --trail: after DAYS, sell at --target or more (owner: {OWNER_TRAIL['hold_days']:g})")
    args = ap.parse_args()
    c = get_connection()
    try:
        if args.pools:
            ids = [i.strip().upper() for i in args.ids.split(",")] if args.ids else None
            print(json.dumps(setup_pools(c, args.pools, ids, args.target, args.stop, args.min_buy,
                                         args.occurrences, args.total, args.auto_pick, args.trail, args.hold_days),
                             indent=2))
        elif args.trail is not None or args.hold_days is not None:
            # The exit alone, books untouched: every pool session, or --ids.
            ids = [i.strip().upper() for i in args.ids.split(",")] if args.ids else \
                [s["id"] for s in list_sessions(c)["sessions"] if s.get("start_capital")]
            change = {k: v for k, v in (("trail_pct", args.trail), ("hold_days", args.hold_days)) if v is not None}
            for sid in ids:
                update_session(c, sid, change)
        for s in list_sessions(c)["sessions"]:
            p = s["pool"]
            print(f"  {s['id']:<4} {s['name']:<12} {s['mode']:<8} {s['status']:<8} " + (
                f"pool Rs {p['start_capital']:,.0f} -> cash {p['cash']:,.0f}, equity {p['equity']:,.0f} "
                f"({p['return_pct']:+.2f}%), occurrence {p['occurrence']} of {p['max_occurrences']}, "
                + exit_rule(s) + (", auto-pick" if s.get("auto_pick") else "")
                if p else f"no pool, capital cap Rs {s['capital_cap'] or 0:,.0f}"))
    finally:
        c.close()
