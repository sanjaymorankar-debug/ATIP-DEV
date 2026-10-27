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
Rules not linked to a session are unaffected. AUTO does NOT bypass the confirmation gate: a rule
still asks unless it was itself created with require_confirmation=False. Letting AUTO skip the
tap changes when real orders go out and stays held for the owner's go-ahead ("auto_bypass": False
in every reply). The kill switch (orders/risk.py halt / resume) remains the way to stop everything.

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
    return {"sessions": rows, "engine_enforced": True, "auto_bypass": False,
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
    return {**get_session(conn, sid), "engine_enforced": True, "auto_bypass": False}


def set_status(conn, sid: str, status: str, reason: str = "") -> dict:
    status = str(status or "").upper()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    cur = get_session(conn, sid)
    conn.execute("UPDATE trading_sessions SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), sid))
    _event(conn, sid, "STATUS", {"from": cur["status"], "to": status, "reason": reason})
    conn.commit()
    return {**get_session(conn, sid), "engine_enforced": True, "auto_bypass": False}


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


def _trades(rules: list) -> list:
    """One entry per executed ENTRY rule: value, realised P&L from its executed exit legs, risk, closed."""
    kids = {}
    for r in rules:
        if r.get("parent_rule_id"):
            kids.setdefault(r["parent_rule_id"], []).append(r)
    out = []
    for e in rules:
        if (e.get("role") or "ENTRY") != "ENTRY" or e.get("status") != "EXECUTED" or not e.get("execution_price"):
            continue
        px, qty = float(e["execution_price"]), float(e.get("execution_quantity") or 0)
        sign = 1 if e["side"] == "BUY" else -1
        legs = kids.get(e["id"], [])
        done = [l for l in legs if l.get("status") == "EXECUTED" and l.get("execution_price")]
        pnl = sum(sign * (float(l["execution_price"]) - px) * float(l.get("execution_quantity") or 0) for l in done)
        closed_qty = sum(float(l.get("execution_quantity") or 0) for l in done)
        stop = e.get("resolved_stoploss_price")
        risk = abs(px - float(stop)) * qty if stop else None
        out.append({"rule_id": e["id"], "symbol": e["symbol"], "side": e["side"], "entry": px, "qty": qty,
                    "value": px * qty, "date": str(e.get("updated_at") or "")[:10],
                    "closed": bool(qty) and closed_qty >= qty,
                    "exit_date": max((str(l.get("updated_at") or "")[:10] for l in done), default=None),
                    "pnl": round(pnl, 2), "risk": round(risk, 2) if risk else None,
                    "r": round(pnl / risk, 2) if risk and done else None})
    return out


def usage(conn, sid: str, on: str | None = None) -> dict:
    """Today's use of a session's limits: entries executed, realised P&L, open entry value."""
    on = on or str(date.today())
    tr = _trades(_linked_rules(conn, sid))
    return {"trades_today": sum(1 for t in tr if t["date"] == on),
            "realised_today": round(sum(t["pnl"] for t in tr if t["exit_date"] == on), 2),
            "open_value": round(sum(t["value"] for t in tr if not t["closed"]), 2)}


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
        if s.get("capital_cap") and est_value and u["open_value"] + est_value > float(s["capital_cap"]):
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
        tr = _trades(_linked_rules(conn, s["id"]))
        closed = [t for t in tr if t["closed"]]
        wins = [t for t in closed if t["pnl"] > 0]
        rs = [t["r"] for t in closed if t["r"] is not None]
        out.append({"id": s["id"], "name": s["name"], "mode": s["mode"], "status": s["status"],
                    "trades": len(tr), "closed": len(closed), "open": len(tr) - len(closed),
                    "successful": len(wins), "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
                    "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                    "pnl": round(sum(t["pnl"] for t in tr), 2), "recent": sorted(tr, key=lambda t: t["date"])[-5:]})
    return {"sessions": out}
