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

# Roles within a bracket. A plain standalone rule is an ENTRY with no siblings.
ROLE_ENTRY = "ENTRY"
ROLE_TARGET = "TARGET"      # take-profit leg of a bracket
ROLE_STOP = "STOP"          # protective stop leg of a bracket

# Trigger direction — which way the price has to cross the trigger to fire.
# This is what makes a protective stop expressible at all: before this column
# existed, direction was implied by side (BUY => BELOW, SELL => ABOVE), so
# "sell if it FALLS to X" — the definition of a stoploss — could not be
# represented, and the old stoploss_* fields were unreachable dead weight.
DIR_BELOW = "BELOW"         # fires when price <= trigger
DIR_ABOVE = "ABOVE"         # fires when price >= trigger

# Was 120s, which meant a trigger you didn't happen to be watching in the
# browser expired ~2 minutes later, silently. Long window + a re-validation
# on confirm (MAX_CONFIRM_SLIPPAGE_PCT) is safer than a short one: you get
# the chance to act, and a confirmation that arrives after the price has run
# away is refused rather than filled at a price you never agreed to.
CONFIRMATION_WINDOW_SECONDS = 1800          # 30 minutes

# On confirming a pending rule, refuse if the live price has moved further
# than this against you versus the price that triggered it.
MAX_CONFIRM_SLIPPAGE_PCT = 1.0

# Auto-execution (require_confirmation=False, incl. bracket exit legs) refuses
# to act on a quote older than this. Directly targets the failure mode
# orders/broker.py documents from atip.log — this process going silent for
# 2+ hours and then acting on stale state the moment it wakes up.
MAX_QUOTE_AGE_SECONDS = 180


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
    _migrate_bracket_columns(conn)
    conn.commit()


def _migrate_bracket_columns(conn):
    """
    Self-healing, additive migration — same non-destructive ALTER TABLE ADD
    COLUMN pattern as db/schema.py. Adds bracket/OCO/direction support to
    order_rules created before it existed. Never drops or rewrites a row.
    """
    try:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(order_rules)").fetchall()}
    except Exception:
        return
    additions = {
        "trigger_direction": "TEXT",       # BELOW | ABOVE
        "role": "TEXT",                    # ENTRY | TARGET | STOP
        "parent_rule_id": "TEXT",          # entry rule this leg protects
        "oco_group_id": "TEXT",            # legs sharing this are mutually exclusive
        "bracket_target_pct": "REAL",      # on an ENTRY: build a TARGET leg at +x%
        "bracket_target2_pct": "REAL",     # optional second target
        "bracket_stop_pct": "REAL",        # on an ENTRY: build a STOP leg at -x%
        "bracket_auto_exit": "INTEGER",    # exit legs execute without a tap
        "bracket_target_split": "REAL",    # fraction of qty exiting at target 1 (0.5 = half)
        # ── trailing stop ──
        "trail_enabled": "INTEGER",
        "trail_type": "TEXT",              # PERCENT | AMOUNT
        "trail_value": "REAL",             # distance kept below the high-water mark
        "trail_jump": "REAL",              # only move the stop in steps this big (0 = continuous)
        "trail_high_water": "REAL",        # best price seen since the leg opened
        "trail_moves": "INTEGER",          # how many times the stop has ratcheted
        # ── broker-side placement (Dhan GTT / bracket / super order) ──
        "broker_leg_type": "TEXT",         # which broker mechanism holds this leg, if any
        "broker_leg_id": "TEXT",           # Dhan's id for it
    }
    added = []
    for col, typ in additions.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE order_rules ADD COLUMN {col} {typ}")
                added.append(col)
            except Exception as e:
                log.warning(f"  order_rules migration skipped {col}: {e}")
    if added:
        # Backfill semantics for pre-existing rules so their behaviour is
        # unchanged: direction implied by side, and they're standalone entries.
        conn.execute("UPDATE order_rules SET trigger_direction=CASE WHEN side='BUY' THEN ? ELSE ? END "
                     "WHERE trigger_direction IS NULL", (DIR_BELOW, DIR_ABOVE))
        conn.execute(f"UPDATE order_rules SET role='{ROLE_ENTRY}' WHERE role IS NULL")
        conn.commit()
        log.info(f"  ✓ order_rules migrated — added: {', '.join(added)}")


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
        side = payload["side"]
        role = payload.get("role", ROLE_ENTRY)
        # Direction defaults to the historical side-implied behaviour so
        # existing callers are unchanged; bracket legs pass it explicitly.
        direction = payload.get("trigger_direction") or (DIR_BELOW if side == "BUY" else DIR_ABOVE)

        conn.execute("""
            INSERT INTO order_rules (
                id, symbol, side, trigger_type, trigger_value, trigger_percent,
                reference_price, resolved_trigger_price, quantity_type, quantity_value,
                stoploss_type, stoploss_value, resolved_stoploss_price,
                product_type, order_type, limit_price, require_confirmation,
                status, notes, created_at, updated_at,
                trigger_direction, role, parent_rule_id, oco_group_id,
                bracket_target_pct, bracket_target2_pct, bracket_stop_pct, bracket_auto_exit,
                bracket_target_split, trail_enabled, trail_type, trail_value, trail_jump,
                trail_high_water, trail_moves
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            rule_id, symbol, side, payload["trigger_type"],
            payload.get("trigger_value"), payload.get("trigger_percent"),
            payload["reference_price"], trigger_price,
            payload["quantity_type"], payload["quantity_value"],
            stoploss.get("type"), stoploss.get("value"), stoploss_price,
            payload.get("product_type", "CNC"), payload.get("order_type", "MARKET"),
            payload.get("limit_price"),
            1 if payload.get("require_confirmation", True) else 0,
            ACTIVE, payload.get("notes", ""), now, now,
            direction, role, payload.get("parent_rule_id"), payload.get("oco_group_id"),
            payload.get("bracket_target_pct"), payload.get("bracket_target2_pct"),
            payload.get("bracket_stop_pct"),
            1 if payload.get("bracket_auto_exit", True) else 0,
            payload.get("bracket_target_split"),
            1 if payload.get("trail_enabled") else 0,
            payload.get("trail_type"), payload.get("trail_value"), payload.get("trail_jump"),
            payload.get("trail_high_water"), 0,
        ))
        conn.commit()
        return get_rule(rule_id)
    finally:
        conn.close()


def _create_bracket_legs(entry: dict, fill_price: float, quantity: int) -> list[dict]:
    """
    After an ENTRY fills, build its protective exits: a TARGET leg (take profit)
    and a STOP leg (capital protection), sharing an oco_group_id so whichever
    fires first cancels the other.

    Percentages are measured from the ACTUAL fill price, not the trigger price —
    a market order rarely fills exactly at the trigger, and a stop measured from
    the wrong anchor is the wrong distance from real risk.

    Exit legs default to bracket_auto_exit=1 (no confirmation tap). That's a
    deliberate departure from this module's confirm-by-default stance: a stop
    that needs you to be watching is not a stop. It's bounded by the
    MAX_QUOTE_AGE_SECONDS freshness guard in execute_rule(), so it will refuse
    to act on stale quotes rather than fire blind after a sleep/outage.
    """
    legs = []
    t1 = entry.get("bracket_target_pct")
    t2 = entry.get("bracket_target2_pct")
    sl = entry.get("bracket_stop_pct")
    if not any([t1, t2, sl]):
        return legs

    exit_side = "SELL" if entry["side"] == "BUY" else "BUY"
    group = str(uuid.uuid4())
    auto = bool(entry.get("bracket_auto_exit", 1))
    long_entry = entry["side"] == "BUY"

    def leg(pct, role, qty):
        # For a long: targets are ABOVE, stop is BELOW. Mirrored for a short.
        if role == ROLE_STOP:
            price = fill_price * (1 - pct / 100) if long_entry else fill_price * (1 + pct / 100)
            direction = DIR_BELOW if long_entry else DIR_ABOVE
        else:
            price = fill_price * (1 + pct / 100) if long_entry else fill_price * (1 - pct / 100)
            direction = DIR_ABOVE if long_entry else DIR_BELOW
        payload = {
            "symbol": entry["symbol"], "side": exit_side,
            "trigger_type": "PRICE", "trigger_value": round(price, 2),
            "reference_price": fill_price,
            "trigger_direction": direction, "role": role,
            "quantity_type": "SHARES", "quantity_value": qty,
            "product_type": entry["product_type"], "order_type": "MARKET",
            "require_confirmation": not auto,
            "parent_rule_id": entry["id"], "oco_group_id": group,
            "notes": f"{role} leg of {entry['symbol']} {entry['side']} @ {fill_price}",
        }
        if role == ROLE_STOP and entry.get("trail_enabled"):
            payload.update({
                "trail_enabled": True,
                "trail_type": entry.get("trail_type") or "PERCENT",
                "trail_value": entry.get("trail_value") or pct,
                "trail_jump": entry.get("trail_jump") or 0,
                # Seed the high-water mark at the fill so the first ratchet is
                # measured from where the position actually opened.
                "trail_high_water": fill_price,
            })
        return payload

    # Split the position across the two targets (default half at T1, rest at
    # T2). Integer share counts, with the remainder going to the LAST target so
    # nothing is left stranded by rounding.
    total = int(quantity)
    split = entry.get("bracket_target_split")
    split = 0.5 if split is None else float(split)
    if t1 and t2:
        q1 = max(1, int(round(total * split)))
        q1 = min(q1, max(1, total - 1))       # always leave at least 1 for T2
        q2 = total - q1
    elif t1:
        q1, q2 = total, 0
    else:
        q1, q2 = 0, total

    if t1 and q1 > 0:
        legs.append(create_rule(leg(t1, ROLE_TARGET, q1)))
    if t2 and q2 > 0:
        legs.append(create_rule(leg(t2, ROLE_TARGET, q2)))
    if sl:
        # The stop covers the WHOLE position; settle_oco_group() shrinks it as
        # targets fill. A stop sized to only part of the position would leave
        # the rest unprotected from the moment it was created.
        legs.append(create_rule(leg(sl, ROLE_STOP, total)))
    return [l for l in legs if l]


def settle_oco_group(rule: dict, filled_qty: float) -> dict:
    """
    Reconcile a bracket after one leg fills.

    A staged exit (50% at +3%, the rest at +6%) means a filled target is a
    PARTIAL close, so blanket-cancelling the group is wrong — that was the v3
    behaviour and it left the remaining shares with no target and no stop. The
    correct reconciliation:

      TARGET filled -> the position shrank. Reduce the STOP leg's quantity by
                       what just sold and leave the other target alone. Only
                       when the stop's quantity reaches zero is the position
                       flat, and then the remaining legs are cancelled.
      STOP filled   -> the stop always covers the whole remaining position, so
                       the position is now flat: cancel every other leg.
    """
    group = rule.get("oco_group_id")
    if not group:
        return {"cancelled": [], "stop_reduced_to": None}
    role = rule.get("role") or ROLE_ENTRY
    now = datetime.now().isoformat()
    conn = get_connection()
    try:
        siblings = [dict(r) for r in conn.execute(
            "SELECT * FROM order_rules WHERE oco_group_id=? AND id!=? AND status=?",
            (group, rule["id"], ACTIVE)).fetchall()]

        if role == ROLE_STOP:
            ids = [s["id"] for s in siblings]
            if ids:
                conn.executemany(
                    "UPDATE order_rules SET status=?, updated_at=?, "
                    "notes=COALESCE(notes,'')||' [stop filled — position flat]' WHERE id=?",
                    [(CANCELLED, now, i) for i in ids])
                conn.commit()
                log.info(f"  {rule['symbol']}: stop filled — cancelled {len(ids)} remaining leg(s)")
            return {"cancelled": ids, "stop_reduced_to": 0}

        # A target filled: shrink the protective stop to what's still held.
        cancelled, new_qty = [], None
        for s in siblings:
            if (s.get("role") or "") != ROLE_STOP:
                continue
            new_qty = max(0.0, float(s["quantity_value"]) - float(filled_qty))
            if new_qty <= 0:
                conn.execute("UPDATE order_rules SET status=?, updated_at=?, "
                             "notes=COALESCE(notes,'')||' [all targets filled — position flat]' WHERE id=?",
                             (CANCELLED, now, s["id"]))
                cancelled.append(s["id"])
                log.info(f"  {rule['symbol']}: position flat — stop leg closed")
            else:
                conn.execute("UPDATE order_rules SET quantity_value=?, updated_at=?, "
                             "notes=COALESCE(notes,'')||? WHERE id=?",
                             (new_qty, now, f" [target filled: stop qty -> {new_qty:g}]", s["id"]))
                log.info(f"  {rule['symbol']}: target filled — stop now covers {new_qty:g} sh")
        conn.commit()
        return {"cancelled": cancelled, "stop_reduced_to": new_qty}
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════
#  TRAILING STOP
# ═════════════════════════════════════════════════════════════════════════

def update_trailing_stops(price_by_symbol: dict) -> list[dict]:
    """
    Ratchet trailing STOP legs upward as price makes new highs (mirrored for a
    short). Called from check_triggers() on the same quote batch that tests
    triggers, so trailing and firing always see the same price.

    Rules:
      - the high-water mark only ever improves;
      - the stop only ever moves in your favour — never widens, which is the
        whole point of a stop;
      - trail_jump, if set, makes the stop move in discrete steps instead of
        continuously, which is how Dhan's own trailing jump behaves and avoids
        rewriting the level on every tick.

    IMPORTANT LIMITATION: this trails only while the process running
    check_triggers() is alive (the dashboard). It is NOT a broker-side stop —
    if this machine sleeps, the trail freezes at its last level. See
    broker_trailing_capability() for placing the stop at Dhan instead, which is
    what actually protects an overnight position.
    """
    moved = []
    conn = get_connection()
    try:
        legs = [dict(r) for r in conn.execute(
            "SELECT * FROM order_rules WHERE status=? AND role=? AND trail_enabled=1",
            (ACTIVE, ROLE_STOP)).fetchall()]
        for leg in legs:
            price = price_by_symbol.get(leg["symbol"])
            if not price:
                continue
            long_pos = leg["side"] == "SELL"      # a SELL stop protects a long
            hw = leg.get("trail_high_water")
            new_hw = max(hw, price) if hw else price
            if not long_pos:
                new_hw = min(hw, price) if hw else price
            dist = (new_hw * float(leg["trail_value"]) / 100.0
                    if (leg.get("trail_type") or "PERCENT") == "PERCENT"
                    else float(leg["trail_value"]))
            candidate = (new_hw - dist) if long_pos else (new_hw + dist)
            current = float(leg["resolved_trigger_price"])
            jump = float(leg.get("trail_jump") or 0)
            improves = (candidate > current) if long_pos else (candidate < current)
            if jump > 0 and improves and abs(candidate - current) < jump:
                improves = False      # not yet a full step — leave it be
            if improves:
                conn.execute(
                    "UPDATE order_rules SET resolved_trigger_price=?, trail_high_water=?, "
                    "trail_moves=COALESCE(trail_moves,0)+1, updated_at=? WHERE id=?",
                    (round(candidate, 2), round(new_hw, 2), datetime.now().isoformat(), leg["id"]))
                moved.append({"rule_id": leg["id"], "symbol": leg["symbol"],
                              "from": current, "to": round(candidate, 2),
                              "high_water": round(new_hw, 2)})
            elif new_hw != hw:
                conn.execute("UPDATE order_rules SET trail_high_water=?, updated_at=? WHERE id=?",
                             (round(new_hw, 2), datetime.now().isoformat(), leg["id"]))
        conn.commit()
    finally:
        conn.close()
    for m in moved:
        log.info(f"  trail: {m['symbol']} stop ₹{m['from']} -> ₹{m['to']} (high ₹{m['high_water']})")
    return moved


def broker_trailing_capability() -> dict:
    """
    Probe the INSTALLED dhanhq SDK for a broker-side stop/trailing mechanism,
    rather than assuming an API shape.

    This matters because requirements.txt pins only `dhanhq>=1.4.0`, and the
    v1 and v2 SDKs differ substantially in how (and whether) bracket orders,
    super orders and GTT are exposed. Returns what is actually importable and
    callable on THIS machine, so the caller can choose broker-side placement
    when it exists and fall back to ATIP-side trailing when it doesn't.

    Run it directly to see what your install supports:
        python -m orders.rules --probe-broker
    """
    caps = {"sdk_version": None, "methods": [], "place_order_params": [],
            "supports_bracket": False, "supports_super_order": False,
            "supports_gtt": False, "notes": []}
    try:
        import dhanhq
        caps["sdk_version"] = getattr(dhanhq, "__version__", "unknown")
        from dhanhq import dhanhq as DhanClass
        import inspect
        names = [m for m in dir(DhanClass) if not m.startswith("_")]
        caps["methods"] = sorted(names)
        try:
            sig = inspect.signature(DhanClass.place_order)
            caps["place_order_params"] = list(sig.parameters.keys())
        except (TypeError, ValueError):
            pass
        p = " ".join(caps["place_order_params"]).lower()
        caps["supports_bracket"] = ("bo_profit_value" in p) or ("bo_stop_loss" in p)
        caps["supports_super_order"] = any("super" in m.lower() for m in names)
        caps["supports_gtt"] = any(("gtt" in m.lower() or "forever" in m.lower()) for m in names)
        if caps["supports_bracket"]:
            caps["notes"].append("place_order accepts bracket-order params — but Dhan bracket/cover "
                                 "products are INTRADAY-only, so they cannot hold a multi-day CNC position.")
        if caps["supports_gtt"] or caps["supports_super_order"]:
            caps["notes"].append("A GTT/super-order style method exists — that is the mechanism that can "
                                 "hold a stop for a delivery (CNC) position while ATIP is offline. "
                                 "Verify its exact parameters against Dhan's current API docs before use.")
        if not any([caps["supports_bracket"], caps["supports_gtt"], caps["supports_super_order"]]):
            caps["notes"].append("No broker-side stop mechanism detected on this SDK version — "
                                 "ATIP-side trailing (dashboard must stay running) is the only option.")
    except ImportError as e:
        caps["notes"].append(f"dhanhq not importable: {e}")
    return caps


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
    """
    Fires on the rule's own trigger_direction rather than inferring direction
    from side.

    The old version inferred it (BUY => price<=trigger, SELL => price>=trigger),
    which had two consequences, both verified against the live code:
      - on a BUY rule the stoploss sat below the buy trigger, so `price <=
        trigger` matched first for every price that could reach it — the
        stoploss branch was unreachable;
      - on a SELL rule the stop was below the trigger but the branch tested
        `price >=`, so a falling price matched nothing — a crash triggered no
        protective exit at all.
    Direction as an explicit field makes all four combinations expressible,
    including the one that matters here: SELL when price FALLS (a real stop).
    """
    trigger = rule["resolved_trigger_price"]
    direction = rule.get("trigger_direction") or (DIR_BELOW if rule["side"] == "BUY" else DIR_ABOVE)
    crossed = (price <= trigger) if direction == DIR_BELOW else (price >= trigger)
    if not crossed:
        return None
    return rule.get("role") or ROLE_ENTRY


def _quote_batch_age_seconds(quotes) -> float | None:
    """
    Age of the newest timestamp in a live-quote batch, in seconds. Returns None
    when the feed carries no usable timestamp — callers treat that as "unknown"
    and fall back to their own judgement rather than assuming it's fresh.
    """
    if quotes is None or getattr(quotes, "empty", True) or "timestamp" not in quotes.columns:
        return None
    try:
        newest = max(t for t in quotes["timestamp"] if t is not None)
    except (ValueError, TypeError):
        return None
    if isinstance(newest, str):
        try:
            newest = datetime.fromisoformat(newest)
        except ValueError:
            return None
    try:
        return (datetime.now() - newest).total_seconds()
    except TypeError:
        return None


def confirm_rule(rule_id: str, force: bool = False) -> dict:
    """
    Confirm a PENDING_CONFIRMATION rule and place the order.

    Re-validates against the CURRENT price first. The confirmation window is
    now 30 minutes (was 2), which is what makes a trigger actually reachable
    when you're away — but it also means a confirmation can arrive long after
    the trigger, so a market order could fill somewhere you never agreed to.
    If price has moved more than MAX_CONFIRM_SLIPPAGE_PCT against the rule's
    direction since it triggered, this refuses and reports both prices; pass
    force=True to override deliberately.
    """
    rule = get_rule(rule_id)
    if not rule:
        return {"status": "FAILED", "error": "rule not found"}
    if rule["status"] != PENDING_CONFIRMATION:
        return {"status": "FAILED",
                "error": f"rule is not pending confirmation (status: {rule['status']})"}

    hit = rule.get("trigger_hit_price") or rule["resolved_trigger_price"]
    if not force:
        try:
            quotes = fetch_live_quotes([rule["symbol"]])
            now_price = next((r["ltp"] for _, r in quotes.iterrows() if r.get("ltp")), None)
        except Exception as e:
            now_price = None
            log.warning(f"  confirm: could not re-check price for {rule['symbol']}: {e}")
        if now_price and hit:
            # Adverse = paying more on a buy, receiving less on a sell.
            drift = ((now_price - hit) / hit * 100) if rule["side"] == "BUY" \
                else ((hit - now_price) / hit * 100)
            if drift > MAX_CONFIRM_SLIPPAGE_PCT:
                return {"status": "REFUSED_SLIPPAGE",
                        "error": (f"price moved {drift:+.2f}% against you since the trigger "
                                  f"(₹{hit} → ₹{now_price}, limit {MAX_CONFIRM_SLIPPAGE_PCT}%). "
                                  f"Re-confirm with force to place anyway."),
                        "trigger_price": hit, "current_price": now_price,
                        "drift_pct": round(drift, 2)}
    return execute_rule(rule_id, confirm=True)


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
        r = get_rule(rid)
        if r:
            notify_order_event("EXPIRED", r)

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
    # Freshness of this quote batch, for the auto-execution guard below.
    quote_age = _quote_batch_age_seconds(quotes)

    # Ratchet trailing stops BEFORE testing triggers, on this same quote batch,
    # so a stop that should have moved up this tick isn't tested at its old,
    # looser level. Order matters: trail first, then fire.
    for mv in update_trailing_stops(price_by_symbol):
        events.append({"type": "TRAIL_MOVED", **mv})
    # Re-read the rules so the trigger test below sees the ratcheted levels.
    active = list_rules(status=ACTIVE)

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
                fresh = get_rule(rule["id"])
                notify_order_event("PENDING_CONFIRMATION", fresh or rule)
            elif quote_age is not None and quote_age > MAX_QUOTE_AGE_SECONDS:
                # Auto-execution on a stale quote is the documented failure mode
                # (process asleep, then acting the instant it wakes). Refuse and
                # say so rather than firing an order at a price from ages ago.
                log.warning(f"  ⚠ {rule['symbol']}: auto-execute SKIPPED — quote is "
                            f"{quote_age:.0f}s old (limit {MAX_QUOTE_AGE_SECONDS}s)")
                events.append({"type": "SKIPPED_STALE_QUOTE", "rule_id": rule["id"],
                               "symbol": rule["symbol"], "quote_age_s": round(quote_age)})
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

    if result.get("status") == "PLACED":
        fresh = get_rule(rule_id) or rule
        # An exit leg that filled reconciles the bracket — a target is a PARTIAL
        # close (shrink the stop), a stop is a full close (cancel the rest). An
        # entry that filled spawns the exits that protect it.
        if (fresh.get("role") or ROLE_ENTRY) != ROLE_ENTRY:
            result["oco"] = settle_oco_group(fresh, quantity)
        legs = []
        if (fresh.get("role") or ROLE_ENTRY) == ROLE_ENTRY:
            try:
                legs = _create_bracket_legs(fresh, exec_price, quantity)
            except Exception as e:
                # A missing bracket is a real risk to flag loudly — the position
                # is open and unprotected — but the entry itself did fill, so
                # don't report the whole execution as failed.
                log.error(f"  ⚠ bracket legs NOT created for {fresh['symbol']}: {e}")
                notify_order_event("BRACKET_FAILED", fresh, extra=str(e))
            result["bracket_legs"] = [
                {"id": l["id"], "role": l["role"], "trigger": l["resolved_trigger_price"],
                 "qty": l["quantity_value"]} for l in legs
            ]
        notify_order_event("EXECUTED", fresh, extra=f"{quantity} @ ~{exec_price}",
                           legs=result.get("bracket_legs"))
    elif result.get("status") not in ("DRY_RUN_OK",):
        notify_order_event("FAILED", rule, extra=str(result.get("error") or result.get("message") or ""))

    return result


# ═════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
#  Before this, a trigger surfaced ONLY in the dashboard's pending box, polled
#  every 5s by an open browser tab. If you weren't looking at that tab the rule
#  expired unnoticed. Telegram makes a trigger reach you when you're away,
#  which is the entire point of having rules.
# ═════════════════════════════════════════════════════════════════════════

def notify_order_event(event: str, rule: dict, extra: str = "", legs=None) -> bool:
    try:
        from alerts.telegram import send_telegram, fmt
    except Exception as e:
        log.debug(f"  telegram unavailable: {e}")
        return False

    sym = rule.get("symbol", "?")
    side = rule.get("side", "?")
    role = rule.get("role") or ROLE_ENTRY
    hit = rule.get("trigger_hit_price") or rule.get("resolved_trigger_price")
    body = (f"<b>{sym}</b> — {side} ({role})\n"
            f"<b>Trigger:</b> ₹{rule.get('resolved_trigger_price')}\n"
            f"<b>Price now:</b> ₹{hit}\n"
            f"<b>Qty:</b> {rule.get('quantity_value')} "
            f"{'shares' if rule.get('quantity_type') == 'SHARES' else '₹ worth'}")
    if extra:
        body += f"\n<b>Detail:</b> {extra}"
    if legs:
        body += "\n<b>Protective legs:</b> " + ", ".join(
            f"{l['role']} @ ₹{l['trigger']}" for l in legs)

    spec = {
        "PENDING_CONFIRMATION": ("⏳", "Order awaiting your confirmation",
                                 f"Confirm in the dashboard within "
                                 f"{CONFIRMATION_WINDOW_SECONDS // 60} minutes, or it expires."),
        "EXECUTED": ("✅", "Order placed", "Placed via Dhan — verify in your broker account."),
        "FAILED": ("❌", "Order FAILED", "No order was placed. Check credentials/funds."),
        "BRACKET_FAILED": ("🚨", "POSITION UNPROTECTED",
                           "Entry filled but the stop/target legs could NOT be created. "
                           "Set an exit manually now."),
        "EXPIRED": ("⌛", "Trigger expired unconfirmed", "No order was placed."),
    }.get(event, ("ℹ️", f"Order {event}", ""))

    return send_telegram(fmt(spec[0], spec[1], body, spec[2]))


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


if __name__ == "__main__":
    import argparse, json as _json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP order-rule engine utilities")
    ap.add_argument("--probe-broker", action="store_true",
                    help="report what stop/trailing mechanisms the installed dhanhq SDK exposes")
    ap.add_argument("--list", action="store_true", help="list order rules")
    ap.add_argument("--status", help="filter --list by status")
    args = ap.parse_args()
    if args.probe_broker:
        caps = broker_trailing_capability()
        print(f"\ndhanhq version      : {caps['sdk_version']}")
        print(f"place_order params  : {', '.join(caps['place_order_params']) or '(could not introspect)'}")
        print(f"bracket params      : {caps['supports_bracket']}")
        print(f"super-order method  : {caps['supports_super_order']}")
        print(f"GTT / forever order : {caps['supports_gtt']}")
        gtt = [m for m in caps["methods"] if any(k in m.lower() for k in ("gtt", "forever", "super", "bracket", "bo_"))]
        if gtt:
            print(f"relevant methods    : {', '.join(gtt)}")
        print("\nNotes:")
        for n in caps["notes"]:
            print(f"  - {n}")
        print()
    elif args.list:
        for r in list_rules(status=args.status):
            print(f"  {r['symbol']:12} {r['side']:4} {(r.get('role') or 'ENTRY'):6} "
                  f"{(r.get('trigger_direction') or ''):5} @ {r['resolved_trigger_price']:<10} "
                  f"qty={r['quantity_value']:<6g} {r['status']:12} "
                  f"{'TRAIL' if r.get('trail_enabled') else ''}")
    else:
        ap.print_help()
