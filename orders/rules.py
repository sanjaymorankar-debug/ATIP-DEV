"""
ATIP — Buy/Sell Target & Stoploss Rules Engine
=================================================
Lets you set a per-stock Buy/Sell target (price or %) with an optional
stoploss (% or ₹ move), and a quantity (shares or ₹ amount). When the
live/last price crosses the target or stoploss, the rule is flagged for
your confirmation — it does NOT place an order by itself.

DESIGN — WHY THIS DOESN'T AUTO-EXECUTE (read orders/broker.py's
docstring first if you haven't):
    orders/broker.py already documents, from your own atip.log, that
    this process has gone silent for 2+ hours (machine sleep/network drop)
    and hit unhandled tracebacks on catch-up. That's exactly the failure
    profile where an unattended auto-trigger-and-execute loop can fire a
    stale or duplicate order. So this module keeps the same split that
    orders.py already established: automatic MONITORING (safe, read-only —
    just flags a rule as PENDING_CONFIRMATION), manual EXECUTION (you tap
    confirm, which calls place_buy_order/place_sell_order with confirm=True,
    funds-checked, logged to order_log same as any other order from this
    system). A rule can be set to require_confirmation=False to skip the
    tap, but that's an explicit per-rule opt-out of the safety default —
    not something to flip without having watched it behave correctly in
    dry-run/confirmed mode first.

Monitoring itself runs from a background thread started by the dashboard
process (see server.py) — separate from pipeline/scheduler.py, so it
doesn't inherit the scheduler's documented catch-up-burst risk, and it
naturally stops if the dashboard isn't running.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta

from db.schema import get_connection
from data.dhan import get_security_id, fetch_live_quotes
from orders.broker import place_buy_order, place_sell_order

log = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
EXECUTED = "EXECUTED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"

CONFIRMATION_WINDOW_SECONDS = 120


# ═════════════════════════════════════════════════════════════════════════
#  SCHEMA (self-migrating, same pattern as db/schema.py)
# ═════════════════════════════════════════════════════════════════════════

def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_rules (
            id                    TEXT PRIMARY KEY,
            symbol                TEXT NOT NULL,
            side                  TEXT NOT NULL,
            trigger_type          TEXT NOT NULL,
            trigger_value         REAL,
            trigger_percent       REAL,
            reference_price       REAL NOT NULL,
            resolved_trigger_price REAL NOT NULL,
            quantity_type         TEXT NOT NULL,
            quantity_value        REAL NOT NULL,
            stoploss_type         TEXT,
            stoploss_value        REAL,
            resolved_stoploss_price REAL,
            product_type          TEXT NOT NULL DEFAULT 'CNC',
            order_type            TEXT NOT NULL DEFAULT 'MARKET',
            limit_price           REAL,
            require_confirmation  INTEGER NOT NULL DEFAULT 1,
            status                TEXT NOT NULL DEFAULT 'ACTIVE',
            notes                 TEXT DEFAULT '',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            triggered_at          TEXT,
            trigger_hit_price     REAL,
            confirmation_expires_at TEXT,
            dhan_order_id         TEXT,
            execution_price       REAL,
            execution_quantity    REAL,
            execution_error       TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_rules_symbol ON order_rules(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_rules_status ON order_rules(status)")
    conn.commit()


def init_orders_table():
    conn = get_connection()
    try:
        _ensure_table(conn)
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════
#  CRUD
# ═════════════════════════════════════════════════════════════════════════

def _resolve_trigger_price(trigger_type, trigger_value, trigger_percent, reference_price):
    if trigger_type == "PRICE":
        return round(float(trigger_value), 2)
    return round(reference_price * (1 + float(trigger_percent) / 100), 2)


def _resolve_stoploss_price(stoploss_type, stoploss_value, trigger_price):
    if not stoploss_type or stoploss_value is None:
        return None
    if stoploss_type == "PERCENT":
        return round(trigger_price * (1 + float(stoploss_value) / 100), 2)
    return round(trigger_price + float(stoploss_value), 2)


def create_rule(payload: dict) -> dict:
    conn = get_connection()
    try:
        _ensure_table(conn)
        symbol = payload["symbol"].upper().strip()

        sec = get_security_id(symbol)
        if not sec:
            raise ValueError(f"security_id not found for {symbol} — check it's a valid NSE symbol")

        trigger_price = _resolve_trigger_price(
            payload["trigger_type"], payload.get("trigger_value"),
            payload.get("trigger_percent"), payload["reference_price"],
        )
        stoploss = payload.get("stoploss") or {}
        stoploss_price = _resolve_stoploss_price(
            stoploss.get("type"), stoploss.get("value"), trigger_price
        )

        now = datetime.now().isoformat()
        rule_id = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO order_rules (
                id, symbol, side, trigger_type, trigger_value, trigger_percent,
                reference_price, resolved_trigger_price, quantity_type, quantity_value,
                stoploss_type, stoploss_value, resolved_stoploss_price,
                product_type, order_type, limit_price, require_confirmation,
                status, notes, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            rule_id, symbol, payload["side"], payload["trigger_type"],
            payload.get("trigger_value"), payload.get("trigger_percent"),
            payload["reference_price"], trigger_price,
            payload["quantity_type"], payload["quantity_value"],
            stoploss.get("type"), stoploss.get("value"), stoploss_price,
            payload.get("product_type", "CNC"), payload.get("order_type", "MARKET"),
            payload.get("limit_price"),
            1 if payload.get("require_confirmation", True) else 0,
            ACTIVE, payload.get("notes", ""), now, now,
        ))
        conn.commit()
        return get_rule(rule_id)
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["require_confirmation"] = bool(d["require_confirmation"])
    return d


def get_rule(rule_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM order_rules WHERE id=?", (rule_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_rules(symbol: str = None, status: str = None) -> list[dict]:
    conn = get_connection()
    try:
        sql = "SELECT * FROM order_rules WHERE 1=1"
        params = []
        if symbol:
            sql += " AND symbol=?"; params.append(symbol.upper())
        if status:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def delete_rule(rule_id: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM order_rules WHERE id=?", (rule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def cancel_rule(rule_id: str) -> dict | None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE order_rules SET status=?, updated_at=? WHERE id=?",
            (CANCELLED, datetime.now().isoformat(), rule_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_rule(rule_id)


# ═════════════════════════════════════════════════════════════════════════
#  MONITORING (read-only — flags PENDING_CONFIRMATION, never executes)
# ═════════════════════════════════════════════════════════════════════════

def _condition_met(rule: dict, price: float) -> str | None:
    if rule["side"] == "BUY":
        if price <= rule["resolved_trigger_price"]:
            return "TARGET"
        if rule["resolved_stoploss_price"] and price <= rule["resolved_stoploss_price"]:
            return "STOPLOSS"
    else:
        if price >= rule["resolved_trigger_price"]:
            return "TARGET"
        if rule["resolved_stoploss_price"] and price >= rule["resolved_stoploss_price"]:
            return "STOPLOSS"
    return None


def expire_stale_confirmations() -> list[str]:
    conn = get_connection()
    try:
        now = datetime.now().isoformat()
        rows = conn.execute(
            "SELECT id FROM order_rules WHERE status=? AND confirmation_expires_at IS NOT NULL AND confirmation_expires_at < ?",
            (PENDING_CONFIRMATION, now),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.executemany(
                "UPDATE order_rules SET status=?, updated_at=? WHERE id=?",
                [(EXPIRED, now, i) for i in ids],
            )
            conn.commit()
        return ids
    finally:
        conn.close()


def check_triggers() -> list[dict]:
    """
    Read-only pass: fetches live quotes for symbols with ACTIVE rules,
    flags any that hit target/stoploss as PENDING_CONFIRMATION (or, for
    rules explicitly marked require_confirmation=False, executes them
    immediately via the confirmed execution path below).

    Safe to call frequently (e.g. every 30-60s) — it places no orders on
    its own for confirmation-gated rules.
    """
    events = []
    expired = expire_stale_confirmations()
    for rid in expired:
        events.append({"type": "EXPIRED", "rule_id": rid})

    active = list_rules(status=ACTIVE)
    if not active:
        return events

    symbols = sorted({r["symbol"] for r in active})
    try:
        quotes = fetch_live_quotes(symbols)
    except Exception as e:
        log.warning(f"  order_rules: live quote fetch failed, skipping this check: {e}")
        return events

    if quotes.empty:
        return events

    price_by_symbol = {row["symbol"]: row["ltp"] for _, row in quotes.iterrows() if row.get("ltp")}

    conn = get_connection()
    try:
        for rule in active:
            price = price_by_symbol.get(rule["symbol"])
            if not price:
                continue
            reason = _condition_met(rule, price)
            if not reason:
                continue

            if rule["require_confirmation"]:
                now = datetime.now()
                expires = (now + timedelta(seconds=CONFIRMATION_WINDOW_SECONDS)).isoformat()
                conn.execute(
                    "UPDATE order_rules SET status=?, triggered_at=?, trigger_hit_price=?, confirmation_expires_at=?, updated_at=? WHERE id=?",
                    (PENDING_CONFIRMATION, now.isoformat(), price, expires, now.isoformat(), rule["id"]),
                )
                conn.commit()
                events.append({"type": "PENDING_CONFIRMATION", "rule_id": rule["id"],
                                "symbol": rule["symbol"], "side": rule["side"],
                                "reason": reason, "price": price})
            else:
                result = execute_rule(rule["id"], price)
                events.append({"type": result["status"], "rule_id": rule["id"], **result})
    finally:
        conn.close()

    return events


# ═════════════════════════════════════════════════════════════════════════
#  EXECUTION (delegates to orders.broker — same funds check, order_log,
#  security_id lookup, and dry-run semantics as every other order path)
# ═════════════════════════════════════════════════════════════════════════

def _resolve_quantity(rule: dict, price: float) -> int:
    if rule["quantity_type"] == "AMOUNT":
        return max(1, math.floor(rule["quantity_value"] / price))
    return int(rule["quantity_value"])


def execute_rule(rule_id: str, price: float = None, confirm: bool = True) -> dict:
    """
    Places the order for a rule via orders.broker.place_buy_order /
    place_sell_order — reusing its funds check and order_log audit trail.
    confirm=True means a REAL order (same semantics as orders.py's own
    --confirm flag); pass confirm=False to dry-run this specific call.
    """
    rule = get_rule(rule_id)
    if not rule:
        return {"status": "FAILED", "error": "rule not found"}

    exec_price = price or rule.get("trigger_hit_price") or rule["resolved_trigger_price"]
    quantity = _resolve_quantity(rule, exec_price)

    placer = place_buy_order if rule["side"] == "BUY" else place_sell_order
    result = placer(
        rule["symbol"], quantity,
        order_type=rule["order_type"], product_type=rule["product_type"],
        price=rule.get("limit_price") or 0, confirm=confirm,
    )

    conn = get_connection()
    try:
        now = datetime.now().isoformat()
        if result.get("status") == "PLACED":
            conn.execute(
                "UPDATE order_rules SET status=?, dhan_order_id=?, execution_price=?, execution_quantity=?, updated_at=? WHERE id=?",
                (EXECUTED, result.get("order_id"), exec_price, quantity, now, rule_id),
            )
        elif result.get("status") == "DRY_RUN_OK":
            # Dry run — leave rule state as-is for the caller to decide next steps.
            pass
        else:
            conn.execute(
                "UPDATE order_rules SET status=?, execution_error=?, updated_at=? WHERE id=?",
                (FAILED, str(result.get("error") or result.get("message") or result), now, rule_id),
            )
        conn.commit()
    finally:
        conn.close()

    return result


def reject_rule(rule_id: str) -> dict | None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE order_rules SET status=?, updated_at=? WHERE id=?",
            (CANCELLED, datetime.now().isoformat(), rule_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_rule(rule_id)
