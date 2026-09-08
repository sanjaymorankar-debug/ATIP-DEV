"""
Which broker does an order actually reach?

One decision, in one place, so no caller can accidentally get it wrong:

    PAPER    (default)  orders.paper.PaperBroker — simulated fills at live prices
    SANDBOX             a real Dhan client pointed at sandbox.dhan.co
    LIVE                the real Dhan client, real money

Two properties matter more than convenience here:

  1. **PAPER is the default.** Anything unset, misspelled or unreadable resolves
     to PAPER. The failure mode of a config typo must be "nothing was traded",
     never "real money moved".
  2. **LIVE is opt-in twice.** `broker_env` must say LIVE *and* the caller must
     pass confirm=True. Neither alone is enough.

On SANDBOX: Dhan publishes no sandbox and does not document this host. It does
answer the `/v2/*` routes with Dhan's own error JSON rather than a 404, so it is
a real deployment, but it has a separate token namespace — a production token is
rejected with DH-906. Support is here so that a Dhan-issued sandbox token can be
used without a code change; it is not usable without one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("atip.orders")

CONFIG_PATH = Path("atip_data") / "config.json"

PAPER = "PAPER"
SANDBOX = "SANDBOX"
LIVE = "LIVE"
VALID = (PAPER, SANDBOX, LIVE)

SANDBOX_BASE_URL = "https://sandbox.dhan.co/v2"


def broker_env() -> str:
    """The configured environment, defaulting to PAPER on anything unexpected."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        env = str(raw.get("broker_env", PAPER)).strip().upper()
    except Exception:
        return PAPER
    if env not in VALID:
        log.warning(f"  broker_env={env!r} is not one of {VALID} — falling back to PAPER")
        return PAPER
    return env


def is_live() -> bool:
    return broker_env() == LIVE


def describe() -> str:
    env = broker_env()
    return {
        PAPER: "PAPER — simulated fills at live prices, no order reaches Dhan",
        SANDBOX: f"SANDBOX — real Dhan client against {SANDBOX_BASE_URL}",
        LIVE: "LIVE — real orders, real money",
    }[env]


def get_execution_client(quote_source=None):
    """
    Return (client, env). The client always exposes the dhanhq order surface,
    so callers need no branching of their own.

    `quote_source` is a real Dhan client used only for market data — PAPER still
    prices its fills from the live market.
    """
    env = broker_env()

    if env == PAPER:
        from orders.paper import PaperBroker
        return PaperBroker(quote_source=quote_source), env

    from data.dhan import get_dhan_client
    dhan, _ = get_dhan_client()

    if env == SANDBOX:
        # DhanHTTP sets self.base_url from a class constant, per instance, so
        # this redirects only this client and never leaks into the market-data
        # client used elsewhere.
        redirected = False
        for attr in ("dhan_http", "_dhan_http", "http"):
            target = getattr(dhan, attr, None)
            if target is not None and hasattr(target, "base_url"):
                target.base_url = SANDBOX_BASE_URL
                redirected = True
                break
        if not redirected:
            raise RuntimeError(
                "Could not point the Dhan client at the sandbox: this SDK build "
                "exposes no reachable http object with a base_url. Refusing to "
                "continue, because falling through here would send SANDBOX "
                "orders to the LIVE endpoint.")
        log.warning(f"  Broker environment: SANDBOX ({SANDBOX_BASE_URL}) — "
                    f"needs a Dhan-issued sandbox token, not your production one")
        return dhan, env

    log.warning("  Broker environment: LIVE — orders will use real money")
    return dhan, env
