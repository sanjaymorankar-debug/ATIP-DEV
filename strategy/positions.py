"""
Persistent state for aggressively-managed positions.

`strategy.aggressive` decides; this module remembers. Keeping them apart means
every decision can be tested with plain numbers and no database, and every
persistence path can be tested without a broker.

Idempotency is the point of this module. A price feed can deliver the same tick
twice, a scan can overlap with the previous one, and a process can restart
mid-sequence — none of which may sell the same shares twice. Two mechanisms
enforce that:

  1. `strategy_event` has UNIQUE (position_id, event_key). Claiming an event is
     an INSERT, so the database rejects the second attempt. A check-then-act
     guard in Python would leave a window; this does not.
  2. Every state UPDATE additionally carries its precondition in the WHERE
     clause (t1_state='PENDING', status!='CLOSED'), so even a claim that somehow
     slipped through cannot apply a transition twice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from db.schema import get_connection
from strategy.aggressive import (
    Momentum, checkpoint_decision, config, is_live, partial_exit_quantity,
    ratchet, realised_pnl, trail_distance_pct, unrealised_pnl,
    ST_OPEN, ST_RUNNER, ST_CLOSED, TARGET_1_PARTIAL_EXIT,
)

log = logging.getLogger("atip.strategy")


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_position (
            id             TEXT PRIMARY KEY,
            entry_rule_id  TEXT,
            symbol         TEXT NOT NULL,
            side           TEXT NOT NULL DEFAULT 'BUY',
            entry_price    REAL NOT NULL,
            initial_qty    INTEGER NOT NULL,
            remaining_qty  INTEGER NOT NULL,
            t1_state       TEXT DEFAULT 'PENDING',
            t1_qty         INTEGER DEFAULT 0,
            t1_price       REAL,
            t1_at          TEXT,
            t2_state       TEXT DEFAULT 'PENDING',
            t2_at          TEXT,
            t2_verdict     TEXT,
            high_water     REAL,
            trail_pct      REAL,
            trail_stop     REAL,
            trail_moves    INTEGER DEFAULT 0,
            realized_pnl   REAL DEFAULT 0,
            unrealized_pnl REAL DEFAULT 0,
            cost_pct       REAL DEFAULT 0,
            status         TEXT DEFAULT 'OPEN',
            exit_reason    TEXT,
            exit_price     REAL,
            closed_at      TEXT,
            mode           TEXT DEFAULT 'PAPER',
            created_at     TEXT,
            updated_at     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stratpos_symbol ON strategy_position(symbol,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stratpos_entry ON strategy_position(entry_rule_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_event (
            id           TEXT PRIMARY KEY,
            position_id  TEXT NOT NULL,
            event_key    TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            price        REAL,
            quantity     INTEGER,
            detail       TEXT,
            created_at   TEXT,
            UNIQUE (position_id, event_key)
        )
    """)
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat()


def _claim_event(conn, position_id: str, event_key: str, event_type: str,
                 price=None, quantity=None, detail=None) -> bool:
    """
    Claim an event exactly once. True means this call owns it and should act;
    False means it was already handled and the caller must do nothing.
    """
    try:
        conn.execute(
            "INSERT INTO strategy_event (id, position_id, event_key, event_type, "
            "price, quantity, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), position_id, event_key, event_type,
             price, quantity, detail, _now()))
        return True
    except Exception:
        return False


def open_position(symbol: str, entry_price: float, quantity: int, side: str = "BUY",
                  entry_rule_id: str = None, atr_pct: float = None,
                  cost_pct: float = 0.0, conn=None, cfg: dict = None) -> dict:
    """Track a filled entry. Trailing stays dormant until T1 books."""
    cfg = cfg or config()
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pid = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO strategy_position (id, entry_rule_id, symbol, side, entry_price,
                initial_qty, remaining_qty, high_water, trail_pct, cost_pct, status,
                mode, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (pid, entry_rule_id, symbol.upper(), side, float(entry_price),
              int(quantity), int(quantity), float(entry_price),
              trail_distance_pct(atr_pct, cfg), float(cost_pct), ST_OPEN,
              "LIVE" if is_live(cfg) else "PAPER", _now(), _now()))
        conn.commit()
        return get_position(pid, conn=conn)
    finally:
        if own:
            conn.close()


def get_position(position_id: str, conn=None) -> dict | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        r = conn.execute("SELECT * FROM strategy_position WHERE id=?", (position_id,)).fetchone()
        return dict(r) if r else None
    finally:
        if own:
            conn.close()


def open_positions(symbol: str = None, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        q, p = "SELECT * FROM strategy_position WHERE status!=?", [ST_CLOSED]
        if symbol:
            q += " AND symbol=?"
            p.append(symbol.upper())
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at", p).fetchall()]
    finally:
        if own:
            conn.close()


def record_partial_exit(position_id: str, fill_price: float, quantity: int = None,
                        conn=None, cfg: dict = None) -> dict:
    """
    Book the T1 partial and switch the position to RUNNER.

    The exit quantity is computed from the ORIGINAL size, never the remaining
    one, so it cannot compound if this is somehow reached twice.
    """
    cfg = cfg or config()
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pos = get_position(position_id, conn=conn)
        if not pos:
            return {"applied": False, "reason": "no such position"}
        if pos["t1_state"] != "PENDING":
            return {"applied": False, "reason": "target 1 already booked", "position": pos}
        if not _claim_event(conn, position_id, "T1", TARGET_1_PARTIAL_EXIT,
                            fill_price, quantity):
            return {"applied": False, "reason": "duplicate T1 event", "position": pos}

        long_side = pos["side"] == "BUY"
        qty = int(quantity) if quantity else partial_exit_quantity(pos["initial_qty"], cfg)
        qty = max(0, min(qty, pos["remaining_qty"]))
        hw = max(pos["high_water"] or fill_price, fill_price) if long_side \
            else min(pos["high_water"] or fill_price, fill_price)
        stop = ratchet(pos["trail_stop"], hw, pos["trail_pct"], long_side)

        if qty == 0:
            # A single share cannot be split in half. Rather than exiting the
            # whole position (which would silently turn the aggressive strategy
            # into a plain 3% target) or selling nothing and never trailing,
            # promote it to a runner and let the trail manage it.
            conn.execute("UPDATE strategy_position SET t1_state='SKIPPED_TOO_SMALL', "
                         "status=?, high_water=?, trail_stop=?, updated_at=? "
                         "WHERE id=? AND t1_state='PENDING'",
                         (ST_RUNNER, hw, stop, _now(), position_id))
            conn.commit()
            return {"applied": True, "quantity": 0, "trail_stop": stop,
                    "note": "position too small to split — whole position rides the trail",
                    "position": get_position(position_id, conn=conn)}

        pnl = realised_pnl(pos["entry_price"], fill_price, qty, long_side,
                           pos["cost_pct"] or 0.0)
        remaining = pos["remaining_qty"] - qty
        conn.execute("""
            UPDATE strategy_position SET t1_state='DONE', t1_qty=?, t1_price=?, t1_at=?,
                remaining_qty=?, realized_pnl=COALESCE(realized_pnl,0)+?, status=?,
                high_water=?, trail_stop=?, updated_at=?
            WHERE id=? AND t1_state='PENDING'
        """, (qty, fill_price, _now(), remaining, pnl, ST_RUNNER, hw, stop,
              _now(), position_id))
        conn.commit()
        return {"applied": True, "quantity": qty, "realized_pnl": pnl,
                "remaining_qty": remaining, "trail_stop": stop,
                "position": get_position(position_id, conn=conn)}
    finally:
        if own:
            conn.close()


def update_high_water(position_id: str, price: float, conn=None) -> dict:
    """Track the best price seen and ratchet the trail. Never loosens the stop."""
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pos = get_position(position_id, conn=conn)
        if not pos or pos["status"] == ST_CLOSED:
            return {"moved": False}
        long_side = pos["side"] == "BUY"
        prev_hw = pos["high_water"]
        better = (price > prev_hw) if long_side else (price < prev_hw)
        hw = price if (prev_hw is None or better) else prev_hw
        stop = ratchet(pos["trail_stop"], hw, pos["trail_pct"], long_side)
        moved = pos["trail_stop"] is None or abs(stop - pos["trail_stop"]) > 1e-9
        conn.execute("UPDATE strategy_position SET high_water=?, trail_stop=?, "
                     "trail_moves=COALESCE(trail_moves,0)+?, updated_at=? WHERE id=?",
                     (hw, stop, 1 if moved else 0, _now(), position_id))
        conn.commit()
        return {"moved": moved, "high_water": hw, "trail_stop": stop}
    finally:
        if own:
            conn.close()


def record_checkpoint(position_id: str, price: float, momentum: Momentum,
                      atr_pct: float = None, conn=None, cfg: dict = None) -> dict:
    """
    Run the +6% momentum checkpoint, record the verdict, apply it to the trail.

    It does not sell. The caller places any exit order, which keeps the decision
    testable without a broker and keeps this module free of execution concerns.
    """
    cfg = cfg or config()
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pos = get_position(position_id, conn=conn)
        if not pos:
            return {"applied": False, "reason": "no such position"}
        if pos["status"] == ST_CLOSED:
            return {"applied": False, "reason": "position already closed", "position": pos}
        if pos["t2_state"] != "PENDING":
            return {"applied": False, "reason": "checkpoint already run",
                    "verdict": pos["t2_verdict"], "position": pos}
        if not _claim_event(conn, position_id, "T2", "CHECKPOINT", price):
            return {"applied": False, "reason": "duplicate checkpoint event", "position": pos}

        d = checkpoint_decision(momentum, cfg, pos["trail_pct"], atr_pct)
        long_side = pos["side"] == "BUY"
        hw = max(pos["high_water"] or price, price) if long_side \
            else min(pos["high_water"] or price, price)
        trail_pct = d.trail_pct if d.trail_pct is not None else pos["trail_pct"]
        stop = ratchet(pos["trail_stop"], hw, trail_pct, long_side)
        conn.execute("""UPDATE strategy_position SET t2_state='DONE', t2_at=?, t2_verdict=?,
                        trail_pct=?, trail_stop=?, high_water=?, updated_at=?
                        WHERE id=? AND t2_state='PENDING'""",
                     (_now(), d.verdict, trail_pct, stop, hw, _now(), position_id))
        conn.commit()
        return {"applied": True, "decision": d.as_dict(), "trail_stop": stop,
                "position": get_position(position_id, conn=conn)}
    finally:
        if own:
            conn.close()


def close_position(position_id: str, fill_price: float, reason: str,
                   quantity: int = None, conn=None) -> dict:
    """Close the remainder. A repeated close event is a no-op."""
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pos = get_position(position_id, conn=conn)
        if not pos:
            return {"applied": False, "reason": "no such position"}
        if pos["status"] == ST_CLOSED:
            return {"applied": False, "reason": "already closed", "position": pos}
        if not _claim_event(conn, position_id, f"CLOSE:{reason}", reason,
                            fill_price, quantity):
            return {"applied": False, "reason": "duplicate close event", "position": pos}

        qty = int(quantity) if quantity else int(pos["remaining_qty"])
        qty = max(0, min(qty, pos["remaining_qty"]))
        pnl = realised_pnl(pos["entry_price"], fill_price, qty,
                           pos["side"] == "BUY", pos["cost_pct"] or 0.0)
        remaining = pos["remaining_qty"] - qty
        status = ST_CLOSED if remaining <= 0 else pos["status"]
        conn.execute("""UPDATE strategy_position SET remaining_qty=?, status=?,
                        realized_pnl=COALESCE(realized_pnl,0)+?, unrealized_pnl=0,
                        exit_reason=?, exit_price=?, closed_at=?, updated_at=?
                        WHERE id=? AND status!=?""",
                     (remaining, status, pnl, reason, fill_price,
                      _now() if status == ST_CLOSED else None, _now(),
                      position_id, ST_CLOSED))
        conn.commit()
        return {"applied": True, "quantity": qty, "realized_pnl": pnl,
                "remaining_qty": remaining, "exit_reason": reason,
                "position": get_position(position_id, conn=conn)}
    finally:
        if own:
            conn.close()


def mark_to_market(position_id: str, price: float, conn=None) -> dict:
    """Refresh unrealised P&L on whatever is still open."""
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pos = get_position(position_id, conn=conn)
        if not pos:
            return {}
        u = unrealised_pnl(pos["entry_price"], price, pos["remaining_qty"],
                           pos["side"] == "BUY")
        conn.execute("UPDATE strategy_position SET unrealized_pnl=?, updated_at=? WHERE id=?",
                     (u, _now(), position_id))
        conn.commit()
        return {"unrealized_pnl": u,
                "total_pnl": round((pos["realized_pnl"] or 0) + u, 2)}
    finally:
        if own:
            conn.close()


def total_pnl(position: dict) -> float:
    return round((position.get("realized_pnl") or 0) + (position.get("unrealized_pnl") or 0), 2)
