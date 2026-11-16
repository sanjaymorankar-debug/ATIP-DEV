"""
Session arms (KGF-13, 16 Nov 2026): the PAPER A/B test of entry, exit and regime settings across S1-S10.

Until 15 Nov 2026 all ten sessions ran the same picker (rank-fill on the ATIP score) and the same exit (stop -2 %, trail
1 % under the high from +3 %, S1 from +4 %), whatever their names said: S5 "Options" bought cash equities, S10
"Intraday" held CNC positions for days. The replay of ATIP's own data (.ux_build/kite-gap-2026-11-15,
research/session_ab_replay.py) found that 2 % stop inside one ordinary day's range (median ATR14 of the picks 3.81 %),
every one of 14 exit variants negative over the whole rank-fill population, and the Kite breakout-retest entry
(strategy/breakout_retest.py) positive behind a breadth gate. Each arm below tests one of those findings against S5,
the control, which keeps exactly the rules of 15 Nov.

An arm is session_settings `arm:<id>` (JSON). New entries of the session follow it; a trade already open keeps the legs
it was bought with. Keys (absent = off / the session's own column):

    label, strategy          the session's name and description (written to trading_sessions so every page says it)
    entry                    "rank_fill" (orders/autopick.py: BUY signals, then ATIP rank) | "kite_retest" (a resting
                             DUMMY- GTT buy at the breakout-candle low, orders/algo_gtt.py)
    regime_gate              {"metric": "breadth20" | "breadth50", "min": 50}: no new entry under it (scores/breadth.py)
    stop                     {"type": "pct", "value": 3} | {"type": "atr", "mult": 2, "min": 2, "max": 8}  (% of entry)
    trail                    {"after": <pct|atr spec>, "by": <pct|atr spec>}: the stop trails `by` under the high once
                             the price is `after` above the fill
    target_pct               a fixed take-profit, live from the fill (the bracket arms)
    hold_days                with a trail: after this many days a price at +after% or more sells (today's 15-day sale)
    max_days                 time exit: sell what is still held at the close of the Nth trading day after the fill
    risk_budget_pct          % of the session's cash lost at the stop: the buy is min(cash, max(min_buy_value,
                             cash x pct / stop%)), so it grows as the session compounds (owner 16 Nov 2026: 2.5 %, i.e.
                             Rs 625 on Rs 25,000 -- a 5 % stop buys Rs 12,500, and Rs 50,000 once the cash is Rs 1,00,000)
    risk_budget              a fixed rupee budget instead (the arms of 16 Nov 00:52 IST; it does not grow with the session)
    min_buy_value            the smallest buy (Rs 10,000 for the risk-budget arms)
    sector_cap               at most N open or queued positions per NSE industry across all sessions
    no_entry_after           no new CNC entry after HH:MM IST (default 14:45, KGF-03)
    max_intraday_drop_pct    a rank-fill pick is skipped when the LTP is this far under its signal close (default 1.5)
    reentry_cooldown_min     after a stop, the same symbol is not bought again for N minutes (default off, KGF-19)

    python -m orders.arms                    # every session's arm
    python -m orders.arms --apply-defaults   # write ARMS below to S1-S10 (PAPER only; logged as session events)
"""

from __future__ import annotations

import json
import logging
from datetime import date

log = logging.getLogger("atip.arms")

PREFIX = "arm:"
NO_ENTRY_AFTER = "14:45"
MAX_INTRADAY_DROP_PCT = 1.5
RISK_BUDGET_PCT = 2.5          # of the session's cash: Rs 625 on Rs 25,000, compounding with the session
GATE50 = {"metric": "breadth50", "min": 50}
GATE20 = {"metric": "breadth20", "min": 50}
RANK_FILL, KITE_RETEST = "rank_fill", "kite_retest"

# The owner's table of 15 Nov 2026 (prompt §3 item 13). Evidence: §1.3 (real entries since 29 Oct replayed under each exit;
# the wider stops win only in the November rebound, so they go behind the gate) and §1.4 (Kite K1/K2/K4 with breadth50).
ARMS = {
    "S1": {"label": "Momentum", "entry": RANK_FILL, "regime_gate": GATE50,
           "strategy": "Rank-fill on ATIP score + breadth50 gate; stop max(3%, 1.5xATR14) capped 6%; trails 2% from +4%",
           "stop": {"type": "atr", "mult": 1.5, "min": 3.0, "max": 6.0},
           "trail": {"after": {"type": "pct", "value": 4.0}, "by": {"type": "pct", "value": 2.0}}, "hold_days": 15,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S2": {"label": "Buy zones (retest K2)", "entry": KITE_RETEST, "regime_gate": GATE50,
           "strategy": "Kite breakout-retest: resting buy at the breakout low, stop 5%, target 5%, 20 days; breadth50 gate",
           "stop": {"type": "pct", "value": 5.0}, "target_pct": 5.0, "max_days": 20,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S3": {"label": "Breakouts (retest K1)", "entry": KITE_RETEST, "regime_gate": GATE50,
           "strategy": "Kite breakout-retest: resting buy at the breakout low, stop 5%, target 10%, 20 days; breadth50 gate",
           "stop": {"type": "pct", "value": 5.0}, "target_pct": 10.0, "max_days": 20,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S4": {"label": "Swing (retest K4)", "entry": KITE_RETEST, "regime_gate": GATE50,
           "strategy": "Kite breakout-retest: resting buy at the breakout low, stop 5%, trails 3% from +5%, 40 days; breadth50 gate",
           "stop": {"type": "pct", "value": 5.0},
           "trail": {"after": {"type": "pct", "value": 5.0}, "by": {"type": "pct", "value": 3.0}}, "max_days": 40,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S5": {"label": "Control", "entry": RANK_FILL, "regime_gate": None,
           "strategy": "Control arm: today's rules (15 Nov) -- rank-fill on ATIP score, stop 2%, trails 1% from +3%, no gate",
           "stop": {"type": "pct", "value": 2.0},
           "trail": {"after": {"type": "pct", "value": 3.0}, "by": {"type": "pct", "value": 1.0}}, "hold_days": 15},
    "S6": {"label": "Recovery (ATR)", "entry": RANK_FILL, "regime_gate": GATE50,
           "strategy": "Rank-fill on ATIP score + breadth50 gate; stop 2xATR14 (2-8%); trails 1xATR from +2xATR",
           "stop": {"type": "atr", "mult": 2.0, "min": 2.0, "max": 8.0},
           "trail": {"after": {"type": "atr", "mult": 2.0, "min": 2.0, "max": 8.0},
                     "by": {"type": "atr", "mult": 1.0, "min": 1.0, "max": 4.0}}, "hold_days": 15,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S7": {"label": "Wide-stop", "entry": RANK_FILL, "regime_gate": GATE50,
           "strategy": "Rank-fill on ATIP score + breadth50 gate; stop 4%; trails 2% from +4%",
           "stop": {"type": "pct", "value": 4.0},
           "trail": {"after": {"type": "pct", "value": 4.0}, "by": {"type": "pct", "value": 2.0}}, "hold_days": 15,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S8": {"label": "Sector", "entry": RANK_FILL, "regime_gate": GATE50, "sector_cap": 2,
           "strategy": "Rank-fill on ATIP score + breadth50 gate; at most 2 open per industry; stop 3%; trails 2% from +4%",
           "stop": {"type": "pct", "value": 3.0},
           "trail": {"after": {"type": "pct", "value": 4.0}, "by": {"type": "pct", "value": 2.0}}, "hold_days": 15,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S9": {"label": "Bracket 3/6", "entry": RANK_FILL, "regime_gate": GATE50,
           "strategy": "Rank-fill on ATIP score + breadth50 gate; fixed stop 3%, target 6% live from the fill",
           "stop": {"type": "pct", "value": 3.0}, "target_pct": 6.0,
           "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
    "S10": {"label": "Retest fast-gate", "entry": KITE_RETEST, "regime_gate": GATE20,
            "strategy": "Kite breakout-retest (K1 exits: stop 5%, target 10%, 20 days) behind the faster breadth20 gate",
            "stop": {"type": "pct", "value": 5.0}, "target_pct": 10.0, "max_days": 20,
            "risk_budget_pct": RISK_BUDGET_PCT, "min_buy_value": 10000.0},
}


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def get(conn, sid: str) -> dict | None:
    """The session's arm, None when it has none (then the session's own columns rule, as before 16 Nov)."""
    from orders.sessions import ensure_tables
    ensure_tables(conn)
    r = conn.execute("SELECT value FROM session_settings WHERE name = ?", (PREFIX + str(sid),)).fetchone()
    if not r:
        return None
    try:
        a = json.loads(_row(r)["value"])
        return a if isinstance(a, dict) else None
    except (TypeError, ValueError):
        log.warning(f"  arm of {sid} is not JSON; ignored")
        return None


def all_arms(conn) -> dict:
    from orders.sessions import ensure_tables
    ensure_tables(conn)
    out = {}
    for r in conn.execute("SELECT name, value FROM session_settings WHERE name LIKE ?", (PREFIX + "%",)).fetchall():
        r = _row(r)
        try:
            out[r["name"][len(PREFIX):]] = json.loads(r["value"])
        except (TypeError, ValueError):
            pass
    return out


def setting(arm: dict | None, key: str, default=None):
    """An entry setting with its default (no_entry_after 14:45, max_intraday_drop_pct 1.5, cooldown off)."""
    defaults = {"no_entry_after": NO_ENTRY_AFTER, "max_intraday_drop_pct": MAX_INTRADAY_DROP_PCT,
                "reentry_cooldown_min": None, "entry": RANK_FILL}
    v = (arm or {}).get(key)
    return v if v is not None else defaults.get(key, default)


def _validate(arm: dict) -> dict:
    if not isinstance(arm, dict):
        raise ValueError("an arm is a JSON object")
    if setting(arm, "entry") not in (RANK_FILL, KITE_RETEST):
        raise ValueError(f"entry must be {RANK_FILL} or {KITE_RETEST}")
    g = arm.get("regime_gate")
    if g and (g.get("metric") not in ("breadth20", "breadth50") or not 0 <= float(g.get("min", 50)) <= 100):
        raise ValueError("regime_gate is {metric: breadth20|breadth50, min: 0-100}")
    for k in ("stop",):
        if not arm.get(k):
            raise ValueError("an arm needs a stop")
    if arm.get("risk_budget_pct") is not None and not 0 < float(arm["risk_budget_pct"]) <= 100:
        raise ValueError("risk_budget_pct is a % of the session's cash, above 0 and at most 100")
    for spec in (arm.get("stop"), (arm.get("trail") or {}).get("after"), (arm.get("trail") or {}).get("by")):
        if spec is None:
            continue
        if spec.get("type") not in ("pct", "atr"):
            raise ValueError(f"a stop / trail spec is pct or atr, got {spec!r}")
    t = str(setting(arm, "no_entry_after"))
    if len(t) != 5 or t[2] != ":":
        raise ValueError("no_entry_after is HH:MM (IST)")
    return arm


def set_arm(conn, sid: str, arm: dict, reason: str = "") -> dict:
    """Write a session's arm, its label and its display columns (PAPER pools only), logged as an ARM session event."""
    from ops.role import refuse
    refuse("Session arm change")
    from orders import sessions
    from orders.environment import PAPER, broker_env
    if broker_env() != PAPER:
        raise ValueError("session arms are a PAPER A/B test: broker_env is not PAPER")
    s = sessions.get_session(conn, sid)
    arm = _validate(dict(arm))
    was = get(conn, sid)
    conn.execute("INSERT OR REPLACE INTO session_settings (name, value, updated_at) VALUES (?, ?, ?)",
                 (PREFIX + sid, json.dumps(arm, sort_keys=True), sessions._now()))
    # The columns every page already shows: the name and description, and an exit in the session's own fields that
    # says the same as the arm (the arm's ATR stop shows as its floor). min_buy_value is the floor gate() and
    # auto-pick hold the pool to.
    stop = arm["stop"]
    trail = arm.get("trail") or {}
    after = trail.get("after") or {}
    cols = {"name": arm.get("label") or s["name"], "strategy": arm.get("strategy") or s.get("strategy") or "",
            "stop_pct": float(stop.get("value") if stop["type"] == "pct" else stop.get("min") or 2.0),
            "target_pct": float(arm.get("target_pct") or (after.get("value") if after.get("type") == "pct"
                                                         else after.get("min") or 0) or s.get("target_pct") or 3.0),
            "trail_pct": (float(trail["by"]["value"] if trail["by"]["type"] == "pct" else trail["by"].get("min") or 1.0)
                          if trail else None),
            "hold_days": float(arm["hold_days"]) if trail and arm.get("hold_days") else None}
    if arm.get("min_buy_value") and s.get("start_capital") and float(arm["min_buy_value"]) <= float(s["start_capital"]):
        cols["min_buy_value"] = float(arm["min_buy_value"])
    conn.execute(f"UPDATE trading_sessions SET {', '.join(k + ' = ?' for k in cols)}, updated_at = ? WHERE id = ?",
                 (*cols.values(), sessions._now(), sid))
    sessions._event(conn, sid, "ARM", {"from": was, "to": arm, "reason": reason})
    if s.get("start_capital"):
        sessions._reapply_pool_rules(conn, sid)   # entries not sent yet take the new exit; open trades keep theirs
    conn.commit()
    return {"session": sid, "arm": arm, "was": was}


def clear_arm(conn, sid: str, reason: str = "") -> dict:
    from ops.role import refuse
    refuse("Session arm change")
    from orders import sessions
    was = get(conn, sid)
    conn.execute("DELETE FROM session_settings WHERE name = ?", (PREFIX + sid,))
    sessions._event(conn, sid, "ARM", {"from": was, "to": None, "reason": reason})
    conn.commit()
    return {"session": sid, "arm": None, "was": was}


def apply_defaults(conn, ids=None, reason: str = "KGF-13 A/B arms (owner prompt 15 Nov 2026)") -> dict:
    done, skipped = [], {}
    for sid, arm in ARMS.items():
        if ids and sid not in ids:
            continue
        try:
            set_arm(conn, sid, arm, reason)
            done.append(sid)
        except (ValueError, LookupError) as e:
            skipped[sid] = str(e)
    return {"applied": done, "skipped": skipped}


# -- exits ------------------------------------------------------------------------------------------------------------

def atr_pct(conn, symbol: str, on: str | None = None, n: int = 14) -> float | None:
    """ATR14 as % of the close on the last bar at or before `on`: the simple mean of the last n true ranges, as the
    15 Nov study computed it (median 3.81 % for the picks)."""
    on = on or date.today().isoformat()
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT date, high, low, close FROM prices_daily WHERE symbol = ? AND series = 'EQ' AND date <= ? "
            "ORDER BY date DESC LIMIT ?", (symbol, on + "T99", n + 1)).fetchall()]
    except Exception:
        return None
    rows = list(reversed(rows))
    if len(rows) < n + 1:
        return None
    trs = []
    for prev, r in zip(rows, rows[1:]):
        h, l, pc = float(r["high"]), float(r["low"]), float(prev["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    close = float(rows[-1]["close"])
    return round(sum(trs[-n:]) / n / close * 100, 4) if close else None


def resolve(spec: dict | None, atrp: float | None) -> float | None:
    """A pct spec's value, or an ATR spec's mult x ATR% clamped to [min, max] (min when ATR is unknown)."""
    if not spec:
        return None
    if spec["type"] == "pct":
        return float(spec["value"])
    lo, hi = float(spec.get("min") or 0), float(spec.get("max") or 100)
    x = float(spec["mult"]) * atrp if atrp else lo
    return round(min(max(x, lo), hi), 4)


def exit_params(conn, arm: dict, symbol: str, on: str | None = None) -> dict:
    """The arm's exit for one stock: {stop_pct, target_pct, target_after_days, trail_after_pct, trail_pct, max_days,
    atr_pct}. ATR specs read the stock's ATR14 on the signal day."""
    trail = arm.get("trail") or {}
    needs_atr = any(s and s.get("type") == "atr" for s in (arm.get("stop"), trail.get("after"), trail.get("by")))
    atrp = atr_pct(conn, symbol, on) if needs_atr else None
    stop = resolve(arm.get("stop"), atrp)
    after, by = resolve(trail.get("after"), atrp), resolve(trail.get("by"), atrp)
    if trail and after:
        tgt, tgt_days = (after, float(arm["hold_days"])) if arm.get("hold_days") else (None, None)
    else:
        tgt, tgt_days = (float(arm["target_pct"]) if arm.get("target_pct") else None), None
    return {"stop_pct": stop, "target_pct": tgt, "target_after_days": tgt_days,
            "trail_after_pct": after if trail else None, "trail_pct": by if trail else None,
            "max_days": float(arm["max_days"]) if arm.get("max_days") else None, "atr_pct": atrp}


def describe(arm: dict | None) -> str | None:
    """The arm in words, for the session board and the apps."""
    if not arm:
        return None
    def spec(s):
        if not s:
            return ""
        if s["type"] == "pct":
            return f"{float(s['value']):g}%"
        return f"{float(s['mult']):g}xATR ({float(s.get('min') or 0):g}-{float(s.get('max') or 0):g}%)"
    parts = ["Kite retest (resting buy at the breakout low)" if setting(arm, "entry") == KITE_RETEST
             else "rank-fill on ATIP score"]
    g = arm.get("regime_gate")
    parts.append(f"gate {g['metric']} >= {float(g.get('min', 50)):g}%" if g else "no regime gate")
    parts.append(f"stop {spec(arm['stop'])}")
    t = arm.get("trail")
    if t:
        parts.append(f"trails {spec(t['by'])} from +{spec(t['after'])}")
    if arm.get("target_pct") and not t:
        parts.append(f"target +{float(arm['target_pct']):g}%")
    if arm.get("max_days"):
        parts.append(f"{int(arm['max_days'])} days max")
    if arm.get("sector_cap"):
        parts.append(f"<= {int(arm['sector_cap'])} per industry")
    if arm.get("risk_budget_pct"):
        parts.append(f"~{float(arm['risk_budget_pct']):g}% of the session's cash at risk")
    elif arm.get("risk_budget"):
        parts.append(f"~Rs {float(arm['risk_budget']):,.0f} at risk")
    return "; ".join(parts)


def value_cap(arm: dict | None, stop_pct: float | None, floor: float = 0.0, cash: float | None = None) -> float | None:
    """The rupee size of a risk-budget arm's buy before the session's cash: max(floor, budget / stop%), the budget
    being risk_budget_pct of the session's `cash` (so it compounds with the session) or a fixed risk_budget.
    None = no cap."""
    if not arm or not stop_pct:
        return None
    if arm.get("risk_budget_pct") and cash is not None:
        budget = max(0.0, float(cash)) * float(arm["risk_budget_pct"]) / 100
    elif arm.get("risk_budget"):
        budget = float(arm["risk_budget"])
    else:
        return None
    return max(float(floor or 0), budget / (float(stop_pct) / 100))


def gate(conn, arm: dict | None, on: str | None = None) -> dict:
    from scores.breadth import gate_state
    return gate_state(conn, (arm or {}).get("regime_gate"), on)


def board(conn) -> list:
    """Every session's arm for the pages: [{id, name, arm, describe, gate}]."""
    from orders import sessions
    out = []
    for s in sessions.list_sessions(conn)["sessions"]:
        a = get(conn, s["id"])
        out.append({"id": s["id"], "name": s["name"], "arm": a, "describe": describe(a),
                    "gate": gate(conn, a) if a else None})
    return out


if __name__ == "__main__":
    import argparse
    from db.schema import get_connection
    ap = argparse.ArgumentParser(prog="python -m orders.arms", description="Session arms (PAPER A/B)")
    ap.add_argument("--apply-defaults", action="store_true", help="write the 16 Nov 2026 arms to S1-S10")
    ap.add_argument("--ids", help="comma-separated sessions for --apply-defaults")
    a = ap.parse_args()
    c = get_connection()
    try:
        if a.apply_defaults:
            print(json.dumps(apply_defaults(c, [i.strip().upper() for i in a.ids.split(",")] if a.ids else None), indent=2))
        for b in board(c):
            g = b["gate"]
            print(f"  {b['id']:<4} {b['name']:<22} {b['describe'] or '(no arm: the session columns)'}"
                  + (f"  [gate {'OPEN' if g['open'] else 'CLOSED'}: {g.get('value')}]" if g and g.get("metric") else ""))
    finally:
        c.close()
