"""
Trading sessions (UXR-01): up to ten named strategies, each with its own execution mode and limits,
shown on /command and managed through /api/sessions.

    mode    MANUAL   you place every order yourself
            CONFIRM  ATIP asks before sending (the existing confirmation gate) -- the default
            AUTO     ATIP may send within the session's limits without asking
    status  RUNNING | PAUSED | OFF

WHAT IS ENFORCED TODAY, AND WHAT IS NOT. This module is the control plane: it stores the
sessions, their limits and every change to them (session_events, append-only), links order
rules to a session (session_rules) and reports per-session counts. The trigger engine
(orders/rules.py check_triggers / execute_rule) does NOT read it yet: a PAUSED session's rules
still fire, and AUTO does not bypass the confirmation gate. Wiring the engine to these settings
changes when real orders go out, so it is held for the owner's explicit go-ahead. Every reply
from this module carries "engine_enforced": False so no screen can claim otherwise. The kill
switch (orders/risk.py halt / resume) IS enforced everywhere and is the way to stop orders.

AUTO is guarded even as a setting: it needs ack=True (the "I understand" step on the screen),
it is refused while trading is halted, and it is refused when broker_env is LIVE and no SEBI
Algo ID is configured (config.json "sebi_algo_id").

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_rules (
            rule_id     TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            linked_at   TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_rules_sid ON session_rules(session_id)")
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
    return {"sessions": rows, "engine_enforced": False,
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
        from orders.risk import halted
        h, why = halted()
        if h:
            raise ValueError(f"trading is halted ({why}) -- resume before turning on AUTO")
        try:
            from orders.environment import broker_env
            env = broker_env()
        except Exception:
            env = "PAPER"
        if env == "LIVE" and not str(_config().get("sebi_algo_id") or "").strip():
            raise ValueError("LIVE AUTO needs a SEBI Algo ID (config.json sebi_algo_id)")
    conn.execute("UPDATE trading_sessions SET mode = ?, updated_at = ? WHERE id = ?", (mode, _now(), sid))
    _event(conn, sid, "MODE", {"from": cur["mode"], "to": mode, "ack": bool(ack)})
    conn.commit()
    return {**get_session(conn, sid), "engine_enforced": False}


def set_status(conn, sid: str, status: str, reason: str = "") -> dict:
    status = str(status or "").upper()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    cur = get_session(conn, sid)
    conn.execute("UPDATE trading_sessions SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), sid))
    _event(conn, sid, "STATUS", {"from": cur["status"], "to": status, "reason": reason})
    conn.commit()
    return {**get_session(conn, sid), "engine_enforced": False}


def update_session(conn, sid: str, fields: dict) -> dict:
    """Name, strategy, universe and the three limits. Limits must be positive numbers."""
    get_session(conn, sid)
    sets, vals = [], []
    for k in ("name", "strategy", "universe"):
        if k in fields:
            v = str(fields[k] or "").strip()
            if k == "name" and not v:
                raise ValueError("name cannot be empty")
            sets.append(f"{k} = ?"); vals.append(v[:80])
    for k, cast in (("max_trades", int), ("max_loss_value", float), ("capital_cap", float)):
        if k in fields:
            try:
                v = cast(fields[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if v <= 0:
                raise ValueError(f"{k} must be positive")
            sets.append(f"{k} = ?"); vals.append(v)
    if not sets:
        raise ValueError("nothing to update")
    conn.execute(f"UPDATE trading_sessions SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                 (*vals, _now(), sid))
    _event(conn, sid, "EDIT", {k: fields[k] for k in fields})
    conn.commit()
    return get_session(conn, sid)


def link_rule(conn, sid: str, rule_id: str) -> dict:
    get_session(conn, sid)
    try:
        exists = conn.execute("SELECT id FROM order_rules WHERE id = ?", (rule_id,)).fetchone()
    except Exception:
        exists = None
    if not exists:
        raise LookupError(f"no order rule {rule_id!r}")
    conn.execute("DELETE FROM session_rules WHERE rule_id = ?", (rule_id,))
    conn.execute("INSERT INTO session_rules (rule_id, session_id, linked_at) VALUES (?, ?, ?)",
                 (rule_id, sid, _now()))
    _event(conn, sid, "LINK", {"rule_id": rule_id})
    conn.commit()
    return {"rule_id": rule_id, "session_id": sid}


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
