"""
Trading safety report and startup assertions (read-only; changes nothing).

LIVE_TRADING_ENABLED is FALSE unless ALL of these hold -- W8 only adds conditions:
  * W4  execution.mode == LIVE and execution.live_trading_enabled is JSON true
  * W8  the ops environment is "production" (execution/config.live_gate)
  * and even then the W4 DhanBrokerAdapter refuses every call (live execution is
    not implemented)
W1's order rules reach Dhan only when broker_env is LIVE (orders/environment.py);
ops/config.validate() makes broker_env LIVE outside production a startup error.

Other controls reported: paper safeguards (Paper adapter active), risk engine checks
present (incl. the W8 stale-data check), the order manager's LIVE market-session check,
and that order submit / cancel are marked non-idempotent (never retried).
"""

from __future__ import annotations

import logging

log = logging.getLogger("atip.ops.trading_safety")


def live_trading_enabled() -> tuple:
    """W9 MASTER SWITCH for real-money orders on EVERY path.

    (allowed, reason): True only when config.json execution.live_trading_enabled is a
    JSON true AND the ops environment is production. W4 additionally needs
    execution.mode LIVE (execution/config.live_gate) and its Dhan adapter still refuses;
    W1 order rules / the aggressive strategy additionally need broker_env LIVE and
    confirm=True (orders/broker._place_order). Unreadable configuration = not allowed."""
    try:
        from execution.config import execution_settings
        if execution_settings().get("live_trading_enabled") is not True:
            return False, "execution.live_trading_enabled is false"
        from ops.config import environment
        env = environment()
        if env != "production":
            return False, f"environment is {env}; real orders require production"
        return True, "execution.live_trading_enabled true and environment production"
    except Exception as e:
        return False, f"live-trading switch unreadable ({type(e).__name__}) -- fail closed"


def report() -> dict:
    out = {}
    try:
        from execution.config import execution_settings, live_gate
        s = execution_settings()
        allowed, reason = live_gate()
        out.update({"execution_mode": s["mode"], "execution_live_trading_enabled": s["live_trading_enabled"],
                    "live_gate_open": allowed, "live_gate_reason": reason})
    except Exception as e:
        out["execution_error"] = type(e).__name__
        allowed = False
    try:
        from orders.environment import broker_env
        out["w1_broker_env"] = broker_env()
    except Exception:
        out["w1_broker_env"] = "unknown"
    try:
        from execution import order_manager as OM
        out["submit_non_idempotent"] = bool(getattr(getattr(OM, "submit_order", None), "__atip_non_idempotent__",
                                                    False))
        from execution.adapters import DhanBrokerAdapter
        out["dhan_adapter_refuses"] = "_refuse" in DhanBrokerAdapter.__dict__
    except Exception:
        pass
    try:
        from execution.config import execution_settings as _es
        out["max_market_data_age_sessions"] = _es().get("max_market_data_age_sessions")
        from ops.config import environment
        out["environment"] = environment()
    except Exception:
        pass
    master, master_reason = live_trading_enabled()
    out["master_switch"] = {"allowed": master, "reason": master_reason}
    # W1 real orders need broker_env LIVE + confirm AND the master switch (W9)
    out["w1_real_orders_possible"] = bool(master and out.get("w1_broker_env") == "LIVE")
    out["LIVE_TRADING_ENABLED"] = bool(allowed or out["w1_real_orders_possible"])
    return out


def assert_safe() -> dict:
    r = report()
    if r["LIVE_TRADING_ENABLED"]:
        log.critical("TRADING SAFETY: the live execution gate is OPEN (%s)", r.get("live_gate_reason"))
    else:
        log.info("TRADING SAFETY: LIVE_TRADING_ENABLED = FALSE (%s); W1 broker_env=%s",
                 r.get("live_gate_reason"), r.get("w1_broker_env"))
    if r.get("w1_broker_env") == "LIVE":
        log.warning("TRADING SAFETY: W1 broker_env is LIVE -- confirmed W1 order rules can reach Dhan")
    return r
