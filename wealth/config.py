"""
Wealth configuration: atip_data/config.json section "wealth" (all optional).

    "wealth": {
        "enabled": false,              scheduled investor cycle after the post-market run
                                       (snapshots, goal / allocation / drift checks).
                                       The API and the /wealth page work either way.
        "profile_validity_days": 365,  an Investor DNA older than this is STALE
        "default_inflation_pct": 6.0,
        "risk_free_pct": 6.5,          for Sharpe / Sortino / alpha
        "benchmark": "nifty50",        index_levels column used as the default benchmark
        "monte_carlo_paths": 2000,     goal success probability (capped at 20000)
        "rebalance_abs_band_pct": 5.0, drift that triggers a rebalance (absolute, % points)
        "rebalance_rel_band_pct": 25.0,   ... or relative to the target weight
        "rebalance_min_trade": 5000,   rupees; smaller legs are skipped
        "single_stock_cap_pct": 10.0,  of net worth; above it the rebalancer proposes a trim
        "tactical_max_tilt_pct": 10.0, the most a tactical view moves any asset class
        "cma": {...}                   capital market assumptions override (allocation.py)
    }

Nothing here can place an order or change execution settings.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"
DEFAULTS = {
    "enabled": False,
    "profile_validity_days": 365,
    "default_inflation_pct": 6.0,
    "risk_free_pct": 6.5,
    "benchmark": "nifty50",
    "monte_carlo_paths": 2000,
    "rebalance_abs_band_pct": 5.0,
    "rebalance_rel_band_pct": 25.0,
    "rebalance_min_trade": 5000.0,
    "single_stock_cap_pct": 10.0,
    "tactical_max_tilt_pct": 10.0,
    "cma": None,
}
NUMERIC = ("profile_validity_days", "default_inflation_pct", "risk_free_pct", "monte_carlo_paths",
           "rebalance_abs_band_pct", "rebalance_rel_band_pct", "rebalance_min_trade", "single_stock_cap_pct",
           "tactical_max_tilt_pct")


def settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("wealth") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue
        if k in NUMERIC and (isinstance(v, bool) or not isinstance(v, (int, float))):
            continue                      # a malformed value keeps the default
        out[k] = v
    out["enabled"] = out.get("enabled") is True
    out["monte_carlo_paths"] = int(max(100, min(20000, out["monte_carlo_paths"])))
    return out
