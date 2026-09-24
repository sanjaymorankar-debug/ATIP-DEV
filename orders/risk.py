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

      "max_daily_loss_value":     5000,     # rupees lost today (RK-07)
      "max_drawdown_pct":         10        # % below the equity peak (RK-08)

The two loss limits are measured by portfolio/pnl.py (risk_state): today's P&L
against the last pnl_daily row, the drawdown against the highest stored
equity, both marked to the latest prices. They block new BUYs only -- a SELL
reduces exposure, and refusing it in a drawdown would lock the loss in; the
kill switch is the tool that stops everything. A loss limit that is set but
cannot be measured (no P&L history yet, LIVE funds unreachable) blocks the
BUY: an unmeasured loss limit is not a limit.

POSITION SIZING (RK-13). size_position() is the one sizing rule, a pure
function any caller can use (strategy engine, order rules, predictions):
risk risk_per_trade_pct of capital on the distance to the stop, capped at
max_position_pct of capital. Defaults, overridable in config.json:

    "position_sizing": {
      "capital":             null,   # rupees; null = the account's equity
      "risk_per_trade_pct":  1.0,
      "max_position_pct":    10.0
    }

An order rule with quantity_type "RISK" is sized this way at execution, from
its own stoploss (orders/rules.py).

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
              "max_symbol_exposure_value", "max_daily_loss_value", "max_drawdown_pct")

# RK-13 defaults -- the same figures scores/predictions.py has used for its
# position_size_pct since the trade planner was written.
SIZING_DEFAULTS = {"capital": None, "risk_per_trade_pct": 1.0, "max_position_pct": 10.0}


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"  config.json unreadable ({e}) — no configured halt or limits can be read")
        return {}


def risk_alert(title: str, body: str, key: str | None = None, severity: str = "error") -> None:
    """Raise a risk alert (dashboard + Telegram, alerts.telegram.notify). Never
    raises: an alerting fault must not change what happens to an order."""
    try:
        from alerts.telegram import notify, fmt, _html
        notify(fmt("⛔", title, _html(body)), category="risk", severity=severity, key=key)
    except Exception as e:
        log.warning(f"  risk alert not recorded ({title}): {e}")


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
    risk_alert("Trading Halted", f"Kill switch on: {reason or HALT_FLAG}. No order will be placed "
               f"until  python -m orders.risk --resume")
    return str(HALT_FLAG)


def resume() -> bool:
    existed = HALT_FLAG.exists()
    HALT_FLAG.unlink(missing_ok=True)
    log.warning("  ▶ Trading resumed" if existed else "  Trading was not halted")
    if existed:
        risk_alert("Trading Resumed", "Kill switch off — orders can be placed again.", severity="warning")
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
    if transaction_type == "BUY" and (lim.get("max_daily_loss_value") or lim.get("max_drawdown_pct")):
        _loss_checks(conn, env, lim, checks, check)
        if blocked is None:
            unmeasured = [c for c in checks if c.get("unmeasured")]
            if unmeasured:
                blocked = (unmeasured[0]["limit"], unmeasured[0]["reason"])

    if blocked:
        return {"ok": False, "blocked_by": blocked[0], "message": blocked[1], "checks": checks}
    return {"ok": True, "blocked_by": None,
            "message": f"{len(checks)} limit(s) checked, none breached", "checks": checks}


def _pnl_env(env) -> str:
    """portfolio/pnl.py keeps PAPER and LIVE books; SANDBOX orders are
    simulated, so they are measured against the paper book, as _positions does."""
    return LIVE if env == LIVE else PAPER


def _loss_checks(conn, env, lim, checks, check):
    """RK-07 / RK-08 against portfolio.pnl.risk_state(). A limit that cannot
    be measured is recorded as unmeasured -- pretrade_check then blocks."""
    try:
        from portfolio.pnl import risk_state
        st = risk_state(conn, _pnl_env(env), ask_broker=(env == LIVE))
    except Exception as e:
        st = {"day_pnl": None, "drawdown_pct": None, "error": str(e)}
    cap = lim.get("max_daily_loss_value")
    if cap is not None:
        if st.get("day_pnl") is None:
            checks.append({"limit": "max_daily_loss_value", "value": None, "cap": cap, "breached": False,
                           "unmeasured": True,
                           "reason": "max_daily_loss_value is set but today's P&L cannot be measured "
                                     "(no pnl_daily row before today, or no prices/funds) - BUY refused"})
        else:
            check("max_daily_loss_value", max(0.0, -st["day_pnl"]), cap, " Rs lost today")
    cap = lim.get("max_drawdown_pct")
    if cap is not None:
        if st.get("drawdown_pct") is None:
            checks.append({"limit": "max_drawdown_pct", "value": None, "cap": cap, "breached": False,
                           "unmeasured": True,
                           "reason": "max_drawdown_pct is set but the drawdown cannot be measured "
                                     "(equity unknown) - BUY refused"})
        else:
            check("max_drawdown_pct", st["drawdown_pct"], cap, "% drawdown")


# -- Position sizing (RK-13) ------------------------------------------------

def sizing_config() -> dict:
    """SIZING_DEFAULTS overlaid with config.json's "position_sizing"; bad
    values are reported and ignored."""
    out = dict(SIZING_DEFAULTS)
    raw = _config().get("position_sizing") or {}
    for k, v in raw.items():
        if k not in SIZING_DEFAULTS:
            log.warning(f"  position_sizing.{k} is not a sizing setting ATIP knows - ignored")
        elif v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0):
            out[k] = v
        else:
            log.warning(f"  position_sizing.{k}={v!r} is not a positive number - default kept")
    return out


def size_position(entry_price: float, stop_price: float | None = None, *, capital: float,
                  risk_per_trade_pct: float | None = None, max_position_pct: float | None = None,
                  stop_pct: float | None = None) -> dict:
    """
    Shares to buy so that a stop-out loses risk_per_trade_pct of capital,
    capped at max_position_pct of capital. Pure: no I/O, so a strategy,
    backtest or order rule can call it with its own numbers.

    Give the stop as a price (stop_price) or as a % below entry (stop_pct).
    Returns {quantity, position_value, risk_amount, stop_distance_pct,
    capped_by, reason}; quantity 0 with a reason when no position can be
    sized (no stop, stop not below entry, capital too small for one share).
    """
    risk_pct = risk_per_trade_pct if risk_per_trade_pct is not None else SIZING_DEFAULTS["risk_per_trade_pct"]
    max_pct = max_position_pct if max_position_pct is not None else SIZING_DEFAULTS["max_position_pct"]

    def none(why):
        return {"quantity": 0, "position_value": 0.0, "risk_amount": 0.0,
                "stop_distance_pct": None, "capped_by": None, "reason": why}

    if not entry_price or entry_price <= 0:
        return none("no entry price")
    if not capital or capital <= 0:
        return none("no capital to size against")
    if stop_price is not None:
        dist = entry_price - stop_price
    elif stop_pct is not None:
        dist = entry_price * stop_pct / 100
    else:
        return none("no stop - risk-based sizing needs one")
    if dist <= 0:
        return none(f"stop {stop_price} is not below entry {entry_price}")
    by_risk = int((capital * risk_pct / 100) // dist)
    by_cap = int((capital * max_pct / 100) // entry_price)
    qty, capped = (by_cap, "max_position_pct") if by_cap < by_risk else (by_risk, "risk_per_trade_pct")
    if qty < 1:
        return none(f"capital {capital:,.0f} is too small for one share at {entry_price} within the limits")
    return {"quantity": qty, "position_value": round(qty * entry_price, 2),
            "risk_amount": round(qty * dist, 2), "stop_distance_pct": round(dist / entry_price * 100, 3),
            "capped_by": capped, "reason": None}


def size_for_account(conn, entry_price: float, stop_price: float | None = None, env: str | None = None,
                     stop_pct: float | None = None, risk_per_trade_pct: float | None = None) -> dict:
    """size_position() with capital and percentages from config.json's
    position_sizing, capital defaulting to the account's equity
    (portfolio/pnl.py). Adds "capital" and "capital_source" to the result."""
    cfg = sizing_config()
    env = env or broker_env()
    capital, source = cfg["capital"], "config position_sizing.capital"
    if not capital:
        try:
            from portfolio.pnl import portfolio_summary
            capital = portfolio_summary(conn, _pnl_env(env), ask_broker=(env == LIVE))["equity"]
            source = f"{_pnl_env(env)} equity"
        except Exception as e:
            capital, source = None, f"equity unavailable: {e}"
    out = size_position(entry_price, stop_price, capital=capital or 0, stop_pct=stop_pct,
                        risk_per_trade_pct=risk_per_trade_pct or cfg["risk_per_trade_pct"],
                        max_position_pct=cfg["max_position_pct"])
    out.update({"capital": capital, "capital_source": source})
    return out


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
                 f"open positions  : {len(_positions(conn, env))}",
                 f"position sizing : {sizing_config()}"]
        try:
            from portfolio.pnl import risk_state
            st = risk_state(conn, _pnl_env(env))
            lines.append(f"today's P&L     : {st['day_pnl']}  drawdown: {st['drawdown_pct']}%  "
                         f"(equity {st['equity']}, peak {st['peak_equity']})")
        except Exception as e:
            lines.append(f"today's P&L     : unavailable ({e})")
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
