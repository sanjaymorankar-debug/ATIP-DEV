"""
W4 configuration: execution mode, live-trading safety gates and risk limits.

Everything lives in the existing configuration system, atip_data/config.json,
under two optional sections; anything missing takes the SAFE DEFAULT below.

    "execution": {
        "mode": "PAPER",                   PAPER | LIVE
        "live_trading_enabled": false,     the live gate -- see live_gate()
        "auto_execute_paper": false,       scheduled cycle sends approved PAPER orders
        "require_manual_review": false,    every approval becomes REVIEW_REQUIRED
        "execute_books": ["PAPER"],        which W3 books' intents are evaluated
        "order_type": "MARKET",
        "product_type": "CNC",
        "max_intent_age_days": 5,          older intents are REJECTED as stale
        "paper_fill_price": "live"         live (Dhan LTP) | reference (the decision close)
    },
    "w4_risk_limits": { ...RISK_DEFAULTS keys... }

Risk limits then take overrides from the risk_limit table (PUT /api/risk/limits),
so the effective value of a limit is: DB override > config.json > default.
A value of null disables that limit. The live-trading switches are deliberately
NOT settable through the API: only an edit of config.json by the owner can
change them.

The W1 limits in config.json "risk_limits" (max_order_value, max_orders_per_day,
max_open_positions, max_symbol_exposure_value, max_daily_loss_value,
max_drawdown_pct) still apply on top: the risk engine runs orders/risk.py
pretrade_check() as one of its checks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from execution.errors import RiskLimitError

log = logging.getLogger("atip.execution")

CONFIG_PATH = Path("atip_data") / "config.json"
PAPER, LIVE = "PAPER", "LIVE"
MODES = (PAPER, LIVE)

EXECUTION_DEFAULTS = {
    "mode": PAPER,
    "live_trading_enabled": False,
    "auto_execute_paper": False,
    "require_manual_review": False,
    "execute_books": [PAPER],
    "order_type": "MARKET",
    "product_type": "CNC",
    "max_intent_age_days": 5,
    "paper_fill_price": "live",
}

# (default, description). Percentages are of current equity unless stated.
RISK_DEFAULTS = {
    "max_position_pct":             (10.0, "one symbol's position value, % of equity"),
    "max_portfolio_exposure_pct":   (100.0, "all positions' value, % of equity (100 = no leverage)"),
    "max_sector_exposure_pct":      (30.0, "one NSE industry's position value, % of equity"),
    "max_strategy_exposure_pct":    (25.0, "one strategy's position value, % of equity"),
    "max_capital_allocation_pct":   (95.0, "an order may use at most this % of available cash"),
    "max_open_positions":           (20, "open positions after the order"),
    "max_order_quantity":           (5000, "shares in one order"),
    "max_order_value_pct":          (10.0, "one order's value, % of equity"),
    "max_daily_trades":             (20, "W4 orders created today (all strategies)"),
    "per_trade_loss_pct":           (1.0, "loss at the stop, % of equity (sizes the order)"),
    "default_stop_pct":             (5.0, "stop assumed when an intent has none, % below entry"),
    "daily_loss_limit_pct":         (3.0, "today's loss, % of equity; beyond it no BUY"),
    "portfolio_drawdown_limit_pct": (15.0, "drawdown from peak equity; beyond it no BUY"),
    "strategy_drawdown_limit_pct":  (10.0, "a strategy's P&L loss, % of its exposure cap; beyond it no BUY for it"),
}
INT_LIMITS = {"max_open_positions", "max_order_quantity", "max_daily_trades"}


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def execution_settings() -> dict:
    raw = _config().get("execution") or {}
    out = dict(EXECUTION_DEFAULTS)
    for k, v in raw.items():
        if k in out:
            out[k] = v
        else:
            log.warning(f"  config execution.{k} is not a known setting — ignored")
    out["mode"] = str(out.get("mode") or PAPER).upper()
    if out["mode"] not in MODES:
        log.warning(f"  execution.mode={out['mode']!r} is not PAPER/LIVE — using PAPER")
        out["mode"] = PAPER
    # the gate is True only for a real JSON true -- never a string or a number
    out["live_trading_enabled"] = out.get("live_trading_enabled") is True
    out["auto_execute_paper"] = out.get("auto_execute_paper") is True
    out["require_manual_review"] = out.get("require_manual_review") is True
    return out


def live_gate() -> tuple[bool, str]:
    """(allowed, reason). LIVE execution is allowed only when execution.mode is
    LIVE AND execution.live_trading_enabled is true. W4 ships both off, and the
    W4 Dhan adapter refuses regardless (adapters.py)."""
    s = execution_settings()
    if s["mode"] != LIVE:
        return False, "execution.mode is PAPER"
    if not s["live_trading_enabled"]:
        return False, "execution.live_trading_enabled is false"
    return True, "execution.mode LIVE and live_trading_enabled true"


# -- risk limits -----------------------------------------------------------------

def _valid(key, value):
    if key not in RISK_DEFAULTS:
        raise RiskLimitError(f"unknown risk limit {key!r}; known: {sorted(RISK_DEFAULTS)}")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskLimitError(f"{key} must be a number or null")
    if value < 0:
        raise RiskLimitError(f"{key} cannot be negative")
    if key.endswith("_pct") and key != "max_portfolio_exposure_pct" and value > 100:
        raise RiskLimitError(f"{key} is a percentage (0..100)")
    return int(value) if key in INT_LIMITS else float(value)


def risk_limits(conn=None) -> dict:
    """{key: {"value", "source", "default", "description"}} -- effective limits."""
    cfg = _config().get("w4_risk_limits") or {}
    out = {}
    for k, (default, desc) in RISK_DEFAULTS.items():
        out[k] = {"value": default, "source": "default", "default": default, "description": desc}
        if k in cfg:
            try:
                out[k].update(value=_valid(k, cfg[k]), source="config.json")
            except RiskLimitError as e:
                log.warning(f"  config w4_risk_limits: {e} — default kept")
    if conn is not None:
        try:
            for key, vj in conn.execute("SELECT key, value_json FROM risk_limit"):
                if key in out:
                    out[key].update(value=json.loads(vj), source="override")
        except Exception as e:
            log.warning(f"  risk_limit overrides unavailable: {e}")
    return out


def limit_values(conn=None) -> dict:
    return {k: v["value"] for k, v in risk_limits(conn).items()}


def set_risk_limits(conn, changes: dict, actor: str = "owner", note: str = "") -> dict:
    """Validate and store overrides. {"key": null} disables a limit; {"key": "default"}
    removes the override. All-or-nothing: one bad key rejects the whole change."""
    if not isinstance(changes, dict) or not changes:
        raise RiskLimitError("send {limit_key: value} with at least one key")
    current = risk_limits(conn)
    staged = {}
    for k, v in changes.items():
        staged[k] = "default" if v == "default" else _valid(k, v)
    now = datetime.now()
    for k, v in staged.items():
        old = current[k]["value"]
        if v == "default":
            conn.execute("DELETE FROM risk_limit WHERE key=?", (k,))
        else:
            conn.execute("INSERT INTO risk_limit (key,value_json,note,updated_by,updated_at) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,note=excluded.note,"
                         "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                         (k, json.dumps(v), note, actor, now))
        conn.execute("INSERT INTO risk_limit_history (key,old_json,new_json,actor,at) VALUES (?,?,?,?,?)",
                     (k, json.dumps(old), json.dumps(v), actor, now))
    conn.commit()
    return risk_limits(conn)
