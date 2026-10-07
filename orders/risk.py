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

      "max_adv_participation_pct": 5        # W25 RK-10: order shares, % of the
                                            # 20-session average daily volume
                                            # (BUY and SELL; unmeasurable -> blocked)

RK-21 (W39) GATEWAY. Two more keys in the same "risk_limits" section that, unlike
the ones above, are ON by default (GATEWAY_DEFAULTS) -- they only stop what no
sane order does. null disables one:

      "max_price_band_pct":      20,      # the order's price (LIMIT / SL limit,
                                          # SL / SL-M trigger) vs the reference:
                                          # the freshest live_quotes LTP (<= 30 min
                                          # old), else the last prices_daily close.
                                          # 20 = NSE's widest circuit band: fat fingers
      "max_gross_exposure_pct":  100      # BUY: held value + paper futures notional
                                          # + this order, % of equity (100 = no leverage)

The band applies to BUY and SELL; a MARKET order (no price) or a symbol with no
reference price is recorded as skipped, never blocked. The gross limit is
measured on the paper book; a LIVE BUY skips it (see _gross_exposure). Both are
reported in pretrade_check()'s "gateway" list. The W4 risk engine runs the band
itself and has its own gross limit (max_portfolio_exposure_pct), so it calls
pretrade_check(gateway=False).

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
              "max_symbol_exposure_value", "max_daily_loss_value", "max_drawdown_pct",
              "max_adv_participation_pct")
# RK-21: on unless config.json sets them to null
GATEWAY_DEFAULTS = {"max_price_band_pct": 20.0, "max_gross_exposure_pct": 100.0}
# a live_quotes LTP at most this old is the price-band reference (the session
# refreshes quotes every 15 minutes); older, the last close is used
BAND_QUOTE_MAX_AGE_MIN = 30

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
        if k not in LIMIT_KEYS and k not in GATEWAY_DEFAULTS:
            log.warning(f"  risk_limits.{k} is not a limit ATIP knows — ignored")
    return out


def gateway_limits() -> dict:
    """RK-21: GATEWAY_DEFAULTS overlaid with config.json's risk_limits. null
    disables a limit; any other non-positive value is reported and the default
    kept -- a typo must not switch a fat-finger check off."""
    raw = _config().get("risk_limits") or {}
    out = {}
    for k, default in GATEWAY_DEFAULTS.items():
        v = raw.get(k, default)
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = v
        else:
            log.warning(f"  risk_limits.{k}={v!r} is not a positive number or null - default {default} kept")
            out[k] = default
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


def pretrade_check(conn, symbol, transaction_type, quantity, est_value, env=None, order_type=None,
                   price=None, trigger_price=None, gateway=True) -> dict:
    """
    {"ok", "blocked_by", "message", "checks", "gateway"} for one order against
    the configured limits. Every limit is reported in `checks` whether it bound
    or not, so a block can be read back from the log without re-deriving it.

    `gateway` lists the RK-21 checks (price band, gross exposure), on by
    default; each entry is shaped like a `checks` one, or carries "skipped"
    and a "reason" when it could not be measured. The band needs the order's
    order_type / price / trigger_price -- a caller that passes none gets it
    recorded as skipped.
    """
    env = env or broker_env()
    lim = limits()
    gate = _gateway(conn, symbol, transaction_type, est_value, env, order_type, price,
                    trigger_price) if gateway else []
    # a fat-fingered price or a leveraged book is named ahead of any configured limit
    checks, blocked = [], next(((g["limit"], g["reason"]) for g in gate if g["breached"]), None)
    if not lim:
        if blocked:
            return {"ok": False, "blocked_by": blocked[0], "message": blocked[1], "checks": [], "gateway": gate}
        return {"ok": True, "blocked_by": None, "message": "no limits configured" + _gate_note(gate),
                "checks": [], "gateway": gate}
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
    if lim.get("max_adv_participation_pct") is not None:       # W25 RK-10
        try:
            from portfolio.limits import average_daily_volume
            adv = average_daily_volume(conn, symbol)
        except Exception:
            adv = None
        if adv is None:
            checks.append({"limit": "max_adv_participation_pct", "value": None,
                           "cap": lim["max_adv_participation_pct"], "breached": True,
                           "reason": "average daily volume cannot be measured"})
            if blocked is None:
                blocked = ("max_adv_participation_pct", "max_adv_participation_pct is set but the symbol's "
                                                        "average daily volume cannot be measured - order refused")
        else:
            check("max_adv_participation_pct", float(quantity or 0) / adv * 100,
                  lim["max_adv_participation_pct"], "% of ADV")
    if transaction_type == "BUY" and (lim.get("max_daily_loss_value") or lim.get("max_drawdown_pct")):
        _loss_checks(conn, env, lim, checks, check)
        if blocked is None:
            unmeasured = [c for c in checks if c.get("unmeasured")]
            if unmeasured:
                blocked = (unmeasured[0]["limit"], unmeasured[0]["reason"])

    if blocked:
        return {"ok": False, "blocked_by": blocked[0], "message": blocked[1], "checks": checks, "gateway": gate}
    return {"ok": True, "blocked_by": None,
            "message": f"{len(checks)} limit(s) checked, none breached" + _gate_note(gate),
            "checks": checks, "gateway": gate}


# -- RK-21 (W39): the gateway checks -------------------------------------------

def _gate_note(gate) -> str:
    if not gate:
        return ""
    return "; gateway: " + ", ".join(f"{g['limit']} skipped" if g.get("skipped") else
                                     f"{g['limit']} {g['value']:g}% (cap {g['cap']:g}%)" for g in gate)


def _gateway(conn, symbol, transaction_type, est_value, env, order_type, price, trigger_price) -> list:
    """The RK-21 entries for pretrade_check(). Never raises: a check that
    cannot run is recorded as skipped."""
    gl = gateway_limits()
    out = []
    if "max_price_band_pct" in gl:
        try:
            out.append(price_band(conn, symbol, order_type, price, trigger_price, gl["max_price_band_pct"]))
        except Exception as e:
            out.append({"limit": "max_price_band_pct", "value": None, "cap": gl["max_price_band_pct"],
                        "breached": False, "skipped": True, "reason": f"price band not measured ({e})"})
    # as with the W1 exposure caps, a SELL reduces exposure: only a BUY is counted
    if "max_gross_exposure_pct" in gl and transaction_type == "BUY":
        out.append(_gross_exposure(conn, env, est_value, gl["max_gross_exposure_pct"]))
    return out


def reference_price(conn, symbol) -> tuple:
    """(price, source) an order's price is banded against: the freshest
    live_quotes LTP at most BAND_QUOTE_MAX_AGE_MIN old, else the last
    prices_daily close; (None, None) when there is neither."""
    try:
        from execution.paper_matching import latest_prices
        ltp = latest_prices(conn, [symbol], max_age_min=BAND_QUOTE_MAX_AGE_MIN).get(symbol)
    except Exception as e:                       # no live_quotes table on a fresh install
        log.debug(f"  live quote for the price band unavailable ({e})")
        ltp = None
    if ltp:
        return ltp, "live LTP"
    try:
        from execution.positions import latest_close
        close, on = latest_close(conn, symbol)
    except Exception as e:
        log.debug(f"  last close for the price band unavailable ({e})")
        close, on = None, None
    return (close, f"close {on}") if close else (None, None)


def price_band(conn, symbol, order_type=None, price=None, trigger_price=None, cap=None) -> dict:
    """
    RK-21 fat-finger check, shared with the W4 risk engine: the order's price
    furthest from reference_price() -- the limit price of a LIMIT / SL, the
    trigger of an SL / SL-M -- as % of the reference; breached beyond `cap`.
    A MARKET order, an order with no price, or a symbol with no reference is
    "skipped" (never breached). {"limit", "value", "cap", "breached",
    "skipped"?, "price"?, "reference"?, "reference_source"?, "reason"}.

    NSE's own circuit limits are not checked: nothing in ATIP stores them --
    live_quotes keeps ltp / ohlc / prev_close / volume, and data/dhan.py's quote
    parsing does not read the quote API's circuit fields. The exchange enforces
    the circuit; this band is the backstop for a mistyped price.
    """
    out = {"limit": "max_price_band_pct", "value": None, "cap": cap, "breached": False}
    t = str(order_type or "").upper()
    if t == "MARKET":
        return {**out, "skipped": True, "reason": "MARKET order: no price to band"}
    priced = [float(p) for p in (price if t != "SL-M" else None, trigger_price if t != "LIMIT" else None)
              if p and float(p) > 0]
    if not priced:
        return {**out, "skipped": True, "reason": "no order price given"}
    ref, src = reference_price(conn, symbol)
    if not ref:
        return {**out, "skipped": True,
                "reason": f"no reference price for {symbol} (no recent live quote, no close) - not checked"}
    px = max(priced, key=lambda p: abs(p - ref))
    off = round(abs(px - ref) / ref * 100, 4)
    breached = cap is not None and off > cap
    why = f"{t or 'order'} price {px:,.2f} is {off:.1f}% from the reference {ref:,.2f} ({src})"
    return {**out, "value": off, "breached": breached, "price": px, "reference": ref, "reference_source": src,
            "reason": f"max_price_band_pct: {why}, outside the {cap:g}% band" if breached else why}


def _gross_exposure(conn, env, est_value, cap) -> dict:
    """
    RK-21 leverage: held value + open paper futures notional + this BUY, % of
    equity, on the paper book (PAPER, and SANDBOX as _positions does). A
    position with no mark counts at cost, in the held value and the equity
    alike, so the two stay comparable.

    A LIVE BUY is skipped, not blocked: the broker's available balance can
    include collateral and margin (pledged holdings, MTF), so holdings + funds
    is no reliable equity figure to cap leverage against -- the broker's own
    margin check bounds a LIVE order, and asking for funds here would be a
    second broker call on every BUY.
    """
    out = {"limit": "max_gross_exposure_pct", "value": None, "cap": cap, "breached": False}
    if env == LIVE:
        return {**out, "skipped": True, "reason": "LIVE: no reliable equity figure (broker funds include "
                                                  "collateral / margin) - not checked"}
    try:
        from execution.futures_paper import gross_notional
        from portfolio.pnl import portfolio_summary
        s = portfolio_summary(conn, PAPER)
        fut = gross_notional(conn)
    except Exception as e:
        return {**out, "skipped": True, "reason": f"paper book unreadable ({e}) - not checked"}
    if s["cash"] is None:
        return {**out, "skipped": True, "reason": "no paper account yet: equity unknown - not checked"}
    held = sum(p["value"] if p["value"] is not None else p["cost"] for p in s["positions"])
    equity = s["cash"] + held
    if not fut and cap >= 100:
        # a cash BUY with no futures open cannot lever the book: held + order <= equity
        # exactly when order <= cash, which the funds check already enforces (as
        # BLOCKED_INSUFFICIENT_FUNDS, without a risk alert)
        return {**out, "skipped": True, "reason": f"no futures notional and a {cap:g}% cap: covered by the "
                                                  f"funds check"}
    if equity <= 0:
        return {**out, "skipped": True, "reason": f"paper equity {equity:,.0f} - not checked"}
    gross = held + fut + float(est_value or 0)
    pct = round(gross / equity * 100, 4)
    why = (f"held {held:,.0f} + futures notional {fut:,.0f} + order {float(est_value or 0):,.0f} "
           f"= {pct:.1f}% of paper equity {equity:,.0f}")
    return {**out, "value": pct, "breached": pct > cap, "gross": round(gross, 2), "equity": round(equity, 2),
            "futures_notional": fut,
            "reason": f"max_gross_exposure_pct: {why} would exceed the configured {cap:g}%" if pct > cap else why}


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
                 f"gateway (RK-21) : {gateway_limits() or 'disabled'}",
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
