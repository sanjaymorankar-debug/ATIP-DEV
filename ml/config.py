"""
ML configuration: atip_data/config.json section "ml" (all optional).

    "ml": {
        "enabled": false,                  scheduled predictions + monitoring run only when true
        "anomalies_enabled": false,        W24: daily price / volume anomaly scan (ml/unsupervised.py)
        "default_model": null,             model_id whose ACTIVE version feeds the W3 ml_* features
        "feature_set": "atip_core",        default feature-set name for new datasets
        "prediction_horizon": 5,           default label horizon (sessions)
        "model_path": "atip_data/ml",      where artifacts and dataset snapshots are written
        "regime_source": "deterministic",  deterministic | ml | hybrid  (strategy regime)
        "regime_model": null,              model_id of the regime model (ml / hybrid)
        "training": {"validation_fraction": 0.2, "embargo_sessions": null, "min_rows": 200}
    }

Safe defaults: nothing is predicted on a schedule, strategies see no ML
feature values, and the regime stays the deterministic Market Health one
until the owner sets these. Nothing here touches execution or live trading.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"

DEFAULTS = {
    "enabled": False,
    "anomalies_enabled": False,
    "default_model": None,
    "feature_set": "atip_core",
    "prediction_horizon": 5,
    "model_path": "atip_data/ml",
    "regime_source": "deterministic",
    "regime_model": None,
    "training": {"validation_fraction": 0.2, "embargo_sessions": None, "min_rows": 200},
}
REGIME_SOURCES = ("deterministic", "ml", "hybrid")


def settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("ml") or {}
    except Exception:
        raw = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k == "training" and isinstance(v, dict):
            out["training"].update(v)
        elif k in out:
            out[k] = v
    out["enabled"] = out.get("enabled") is True
    out["anomalies_enabled"] = out.get("anomalies_enabled") is True
    if out["regime_source"] not in REGIME_SOURCES:
        out["regime_source"] = "deterministic"
    return out


def model_root() -> Path:
    p = Path(settings()["model_path"])
    p.mkdir(parents=True, exist_ok=True)
    return p
