"""
Quant configuration: atip_data/config.json section "quant" (all optional).

    "quant": {
        "enabled": false,             post-market factor scores, composites, events,
                                      microstructure (off by default)
        "universe": "tracked_current",
        "factor_set": "atip_factors", default factor set computed each day
        "composites": ["vqm", "mom_lowvol_liq"],   composites computed each day
        "winsorize_pct": 1.0,         default winsorization (each tail, %)
        "require_approved_factors": false   W22 (AF-08): a strategy using a qf_ factor that is
                                      not APPROVED cannot be promoted to APPROVED / ACTIVE
    }

Nothing here can place an order or change execution settings.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"
DEFAULTS = {"enabled": False, "require_approved_factors": False, "universe": "tracked_current", "factor_set": "atip_factors",
            "composites": ["vqm", "mom_lowvol_liq"], "winsorize_pct": 1.0}


def settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("quant") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out["enabled"] = out.get("enabled") is True
    out["require_approved_factors"] = out.get("require_approved_factors") is True
    return out
