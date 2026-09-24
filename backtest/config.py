"""
The one place backtest defaults come from: atip_data/config.json's "backtest"
section over DEFAULTS. Engines and strategies never carry their own copies.

    "backtest": {
      "initial_capital": 1000000,
      "cost_model": "nse_delivery",   "cost_overrides": {},
      "slippage": {"kind": "pct", "value": 5.0},
      "liquidity": {"max_participation_pct": 10.0, "min_avg_turnover": 50000000, "on_breach": "cap"},
      "sizing": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0, "max_positions": 10,
                 "default_stop_pct": 5.0},
      "risk_free_rate_pct": 0.0
    }
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

log = logging.getLogger("atip.backtest")

CONFIG_PATH = Path("atip_data") / "config.json"

DEFAULTS = {
    "initial_capital": 1_000_000.0,
    "cost_model": "nse_delivery",
    "cost_overrides": {},
    # 5 bps adverse: the paper broker's paper_slippage_bps default, so a
    # backtest and a paper trade assume the same thing unless configured apart
    "slippage": {"kind": "pct", "value": 5.0},
    "liquidity": {"max_participation_pct": 10.0, "min_avg_turnover": None, "on_breach": "cap",
                  "adv_lookback": 20},
    "sizing": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0, "max_positions": 10,
               "default_stop_pct": 5.0},
    "risk_free_rate_pct": 0.0,
}


def backtest_config(path: Path | None = None) -> dict:
    """DEFAULTS overlaid (one level deep) with config.json's "backtest"."""
    out = copy.deepcopy(DEFAULTS)
    p = path or CONFIG_PATH
    try:
        raw = (json.loads(p.read_text(encoding="utf-8")) or {}).get("backtest") or {}
    except FileNotFoundError:
        raw = {}
    except Exception as e:
        log.warning(f"  config.json unreadable ({e}) — backtest defaults used")
        raw = {}
    for k, v in raw.items():
        if k not in DEFAULTS:
            log.warning(f"  backtest.{k} is not a backtest setting — ignored")
        elif isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    return out
