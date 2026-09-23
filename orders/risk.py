"""
The two things that must stand between a signal and a trade: a kill switch and
pre-trade limits.

Neither existed. Every order path in ATIP funnels through
orders.broker._place_order (orders/rules.py's confirm flow, strategy/live.py's
entry and exit, the broker CLI), and its only check was "are there funds".

KILL SWITCH. halted() is true when either
  * atip_data/TRADING_HALTED exists -- the flag file is the unambiguous one: it
    needs no valid JSON, survives a broken config, and its contents are the
    reason; or
  * config.json says "trading_halted": true.
A halt refuses confirmed orders in every environment, PAPER included: "halted"
means halted, not "halted where it matters". Dry runs still price and preview,
and say that trading is halted -- refusing them would only hide the state.

PRE-TRADE LIMITS. Each limit in config.json's "risk_limits" is enforced only
when it is set, so an install that sets none behaves exactly as before:

    "risk_limits": {
      "max_order_value":          100000,   # rupees, one order
      "max_orders_per_day":       10,       # orders actually placed today
      "max_open_positions":       8,
      "max_symbol_exposure_value": 50000    # held + this order, per symbol
    }

A daily-loss limit is deliberately absent: ATIP stores no daily profit-and-loss
series to measure one against (paper_position.realized_pnl is cumulative, and
unrealised P&L is only ever a live quote), and a limit computed from the wrong
number is worse than a missing one.

    python -m orders.risk --status
    python -m orders.risk --halt "reason"
    python -m orders.risk --resume
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from orders.environment import CONFIG_PATH, LIVE, PAPER, broker_env

log = logging.getLogger("atip.orders")

HALT_FLAG = Path("atip_data") / "TRADING_HALTED"
LIMIT_KEYS = ("max_order_value", "max_orders_per_day", "max_open_positions",
              "max_symbol_exposure_value")


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"  config.json unreadable ({e}) — no configured halt or limits can be read")
        return {}


def halted() -> tuple[bool, str]:
    """(halted, reason). The flag file wins; config.json is the second way in."""
    try:
        if HALT_FLAG.exists():
            return True, (HALT_FLAG.read_text(encoding="utf-8").strip()
                          or f"{HALT_FLAG} exists")
    except Exception as e:                       # unreadable flag file still halts
        return True, f"{HALT_FLAG} exists but could not be read ({e})"
    if _config().get("trading_halted"):
        return True, 'config.json says "trading_halted": true'
    return False, ""


def halt(reason: str = "") -> str:
    HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
    HALT_FLAG.write_text(reason or f"halted {date.today()}", encoding="utf-8")
    log.warning(f"  ⛔ Trading halted: {reason or HALT_FLAG}")
    return str(HALT_FLAG)


def resume() -> bool:
    existed = HALT_FLAG.exists()
    HALT_FLAG.unlink(missing_ok=True)
    log.warning("  ▶ Trading resumed" if existed else "  Trading was not halted")
    return existed


def limits() -> dict:
    """The configured limits, ignoring anything unset, null or not a number."""
    raw = _config().get("risk_limits") or {}
    out = {}
    for k in LIMIT_KEYS:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = v
        elif v not in (None, ""):
            log.warning(f"  risk_limits.{k}={v!r} is not a positive number — not enforced")
    for k in raw:
        if k not in LIMIT_KEYS:
            log.warning(f"  risk_limits.{k} is not a limit ATIP knows — ignored")
    return out


# ── the state each limit is measured against ──────────────────────────────
#
# order_log and the paper tables are created lazily by the code that writes
# them (orders/broker.py _ensure_order_log_table, orders/paper.py
# ensure_tables), so on a fresh install they may not exist yet. A missing table
# genuinely means "nothing placed, nothing held" -- but it must not raise, or
# configuring a limit would break the first order ATIP ever places.

def _query(conn, sql, params, default):
    import sqlite3
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        log.debug(f"  risk state unavailable ({e}) — treating as {default!r}")
        return default


def _orders_today(conn) -> int:
    rows = _query(conn, "SELECT COUNT(*) FROM order_log WHERE status='PLACED' "
                        "AND DATE(timestamp)=?", (str(date.today()),), [(0,)])
    return rows[0][0] if rows else 0


def _positions(conn, env) -> dict:
    """symbol -> value held, in the environment the order is going to."""
    if env == LIVE:
        row = _query(conn, "SELECT MAX(date) FROM portfolio_holdings", (), [(None,)])
        latest = row[0][0] if row else None
        if not latest:
            return {}
        return {r[0]: float(r[1] or 0) for r in _query(
            conn, "SELECT symbol, COALESCE(current_val, qty*avg_price) FROM portfolio_holdings "
                  "WHERE date=? AND qty>0", (str(latest),), [])}
    return {r[0]: float(r[1] or 0) * float(r[2] or 0) for r in _query(
        conn, "SELECT symbol, quantity, avg_price FROM paper_position WHERE quantity>0", (), [])}


def pretrade_check(conn, symbol, transaction_type, quantity, est_value, env=None) -> dict:
    """
    {"ok", "blocked_by", "message", "checks"} for one order against the
    configured limits. Every limit is reported in `checks` whether it bound or
    not, so a block can be read back from the log without re-deriving it.
    """
    env = env or broker_env()
    lim = limits()
    checks, blocked = [], None
    if not lim:
        return {"ok": True, "blocked_by": None, "message": "no limits configured", "checks": []}
    held = None

    def check(name, value, cap, unit=""):
        nonlocal blocked
        if cap is None:
            return
        breach = value is not None and value > cap
        checks.append({"limit": name, "value": value, "cap": cap, "breached": breach})
        if breach and blocked is None:
            blocked = (name, f"{name}: {value:,.0f}{unit} would exceed the configured "
                             f"{cap:,.0f}{unit}")

    check("max_order_value", est_value, lim.get("max_order_value"), " Rs")
    # A SELL reduces exposure and closes a position, so only a BUY is counted
    # against the position and exposure caps.
    if transaction_type == "BUY":
        held = _positions(conn, env)
        if lim.get("max_open_positions") is not None and symbol not in held:
            check("max_open_positions", len(held) + 1, lim.get("max_open_positions"))
        check("max_symbol_exposure_value", held.get(symbol, 0.0) + (est_value or 0.0),
              lim.get("max_symbol_exposure_value"), " Rs")
    check("max_orders_per_day", _orders_today(conn) + 1, lim.get("max_orders_per_day"))

    if blocked:
        return {"ok": False, "blocked_by": blocked[0], "message": blocked[1], "checks": checks}
    return {"ok": True, "blocked_by": None,
            "message": f"{len(checks)} limit(s) checked, none breached", "checks": checks}


def describe_state(conn=None) -> str:
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        is_halted, why = halted()
        lim = limits()
        env = broker_env()
        lines = [f"broker_env      : {env}",
                 f"trading         : {'HALTED — ' + why if is_halted else 'allowed'}",
                 f"limits          : {lim or 'none configured (nothing enforced)'}",
                 f"orders placed today: {_orders_today(conn)}",
                 f"open positions  : {len(_positions(conn, env))}"]
        return "\n".join(lines)
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="ATIP trading kill switch and pre-trade limits")
    ap.add_argument("--halt", nargs="?", const="halted from the command line", metavar="REASON")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.halt is not None:
        print(f"halted — flag file: {halt(args.halt)}")
    if args.resume:
        resume()
    print(describe_state())
