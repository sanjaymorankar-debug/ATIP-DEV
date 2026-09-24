"""
The ML feature registry and versioned feature sets.

Every ML feature IS a W3 strategy feature (strategy_engine/features.py): the
same point-in-time computation serves strategies, backtests and models, so
there is one implementation of RSI, MACD, ATR, ... and nothing is duplicated.
This module adds the metadata ML needs:

    feature_id  name  description  category  data_type  calculation_method
    version  dependencies (inputs)  availability  lookback  created_at

stored in ml_feature (synced from FEATURE_METADATA). FEATURE_VERSION changes
when a formula changes; a feature set records the version of every member,
so a model states exactly which definitions produced its inputs.

A FeatureSet is (name, version, [features]) with a content hash over the
member names and versions; ml_feature_set stores it immutably. Categorical
features (regime, vol_regime, market_trend, score_signal) are one-hot encoded
by the dataset builder with fixed, recorded categories.

Availability: when the underlying data starts. ATIP scores (vpi .. acs,
score_signal) exist only from 2026-07-27, FII/DII flows from 2026-07-28,
Market Health from 2025-01-27; bars go back further. A dataset reports the
coverage of every feature it uses.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from strategy_engine import features as F

FEATURE_VERSION = "1"

CATEGORICAL = {
    "regime": ["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "HIGH_RISK"],
    "vol_regime": ["LOW", "NORMAL", "HIGH"],
    "market_trend": ["bullish", "sideways", "bearish", "uncertain"],
    "score_signal": ["BUY", "SELL", "HOLD", "WAIT"],
}

_DESC = {
    "sma": ("technical", "mean of the last N closes"), "ema": ("technical", "EMA alpha 2/(N+1), SMA-seeded"),
    "rsi": ("technical", "Wilder RSI over N"), "atr_pct": ("volatility", "mean true range N / close x 100"),
    "ret": ("momentum", "N-session return %"), "vol_ratio": ("volume", "volume / mean volume of prior N"),
    "zscore": ("technical", "(close - mean N) / stdev N"), "range_pos": ("technical", "position in N-bar range %"),
    "prior_high": ("technical", "highest high of the N bars before"), "prior_low": ("technical", "lowest low of the N bars before"),
    "volatility": ("volatility", "annualised stdev of N daily returns %"),
    "rel_strength": ("relative_strength", "ret_N minus NIFTY50 ret_N"), "adx": ("trend", "Wilder ADX over N"),
    "vwap": ("volume", "rolling daily-bar VWAP over N"), "bb_pctb": ("volatility", "Bollinger %B over N (2 sd)"),
    "bb_width": ("volatility", "Bollinger width over N, % of SMA"), "nifty_ret": ("market", "NIFTY50 N-session return %"),
}
FIXED = {
    "close": ("price", "decision-day close"), "open": ("price", "open"), "high": ("price", "high"),
    "low": ("price", "low"), "volume": ("volume", "shares traded"), "prev_close": ("price", "previous close"),
    "change_pct": ("momentum", "1-day change %"), "gap_pct": ("momentum", "open vs previous close %"),
    "macd": ("trend", "EMA12 - EMA26"), "macd_signal": ("trend", "EMA9 of MACD"), "macd_hist": ("trend", "MACD - signal"),
    "vpi": ("atip_score", "ATIP VPI"), "spi": ("atip_score", "ATIP SPI"), "rri": ("atip_score", "ATIP RRI"),
    "mri": ("atip_score", "ATIP MRI"), "cri": ("atip_score", "ATIP CRI (crash risk)"), "msi": ("atip_score", "ATIP MSI"),
    "zpi": ("atip_score", "ATIP ZPI"), "acs": ("atip_score", "ATIP ACS"), "atip_score": ("atip_score", "ATIP master score"),
    "score_signal": ("atip_score", "signal engine BUY/SELL/HOLD/WAIT"),
    "regime": ("market", "Market Health regime label"), "mh_score": ("market", "Market Health score"),
    "vix": ("market", "India VIX level"), "vol_regime": ("market", "VIX regime LOW/NORMAL/HIGH"),
    "market_trend": ("market", "regime mapped to bullish/sideways/bearish/uncertain"),
    "breadth_pct": ("market", "% of universe advancing"), "adv_decline": ("market", "advance/decline ratio"),
    "fii_net_cr": ("flows", "FII net cash, Rs cr"), "dii_net_cr": ("flows", "DII net cash, Rs cr"),
}
AVAILABILITY = {"atip_score": "2026-07-27", "flows": "2026-07-28", "market": "2025-01-27"}

# The default ML feature set ("atip_core"): technical + market + ATIP scores.
CORE_FEATURES = [
    "rsi_14", "macd_hist", "adx_14", "atr_pct_14", "bb_pctb_20", "bb_width_20", "ret_5", "ret_20", "ret_60",
    "volatility_20", "vol_ratio_20", "zscore_20", "range_pos_20", "rel_strength_20", "gap_pct", "change_pct",
    "nifty_ret_5", "nifty_ret_20", "mh_score", "vix", "breadth_pct", "regime", "vol_regime",
    "vpi", "spi", "rri", "mri", "cri", "msi", "zpi", "acs", "atip_score",
]
# Technical + market only: usable over the full price history (ATIP scores start 2026-07-27).
TECH_FEATURES = [f for f in CORE_FEATURES if f not in ("vpi", "spi", "rri", "mri", "cri", "msi", "zpi", "acs",
                                                        "atip_score")]
BUILTIN_SETS = {"atip_core": CORE_FEATURES, "atip_technical": TECH_FEATURES}


def describe(name: str) -> dict:
    if not F.known(name) or name in F.ML_FEATURES:
        raise ValueError(f"{name!r} is not an ML-usable feature (ml_* outputs cannot be model inputs)")
    m = F._PARAMETRIC.match(name)
    if m:
        cat, how = _DESC.get(m.group(1), ("technical", m.group(1)))
        lookback = int(m.group(2)) + (1 if m.group(1) in ("ret", "rel_strength", "prior_high", "prior_low",
                                                          "vol_ratio", "volatility", "nifty_ret") else 0)
        if m.group(1) == "adx":
            lookback = 2 * int(m.group(2)) + 1
        how = how.replace("N", m.group(2))
    else:
        cat, how = FIXED.get(name, ("custom", "registered custom feature"))
        lookback = {"macd": 34, "macd_signal": 34, "macd_hist": 34, "prev_close": 2, "change_pct": 2,
                    "gap_pct": 2}.get(name, 1)
    return {"feature_id": f"{name}@v{FEATURE_VERSION}", "name": name, "description": how, "category": cat,
            "data_type": "categorical" if name in CATEGORICAL else "numeric",
            "calculation_method": "strategy_engine.features.compute (point in time)",
            "version": FEATURE_VERSION, "dependencies": list(F.inputs_of(name)),
            "availability": AVAILABILITY.get(cat, "from prices_daily history"), "lookback": lookback,
            "categories": CATEGORICAL.get(name)}


def sync_features(conn) -> int:
    """Upsert metadata for every feature in the built-in sets (and any already stored)."""
    names = set(CORE_FEATURES) | set(FIXED) | {r[0] for r in conn.execute("SELECT name FROM ml_feature")}
    now = datetime.now()
    n = 0
    for name in sorted(names):
        try:
            d = describe(name)
        except ValueError:
            continue
        conn.execute("INSERT INTO ml_feature (feature_id,name,description,category,data_type,calculation_method,"
                     "version,dependencies_json,availability,lookback,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(feature_id) DO NOTHING",
                     (d["feature_id"], name, d["description"], d["category"], d["data_type"],
                      d["calculation_method"], d["version"], json.dumps(d["dependencies"]), d["availability"],
                      d["lookback"], now))
        n += 1
    conn.commit()
    return n


@dataclass
class FeatureSet:
    name: str
    version: str
    features: list
    description: str = ""
    feature_versions: dict = field(default_factory=dict)

    def __post_init__(self):
        if not re.match(r"^[a-z][a-z0-9_]{1,63}$", self.name or ""):
            raise ValueError("feature set name: lowercase letters, digits, underscore")
        if not self.features or len(set(self.features)) != len(self.features):
            raise ValueError("feature set needs a non-empty list of distinct features")
        for f in self.features:
            describe(f)                       # raises on unknown / ml_* features
        self.feature_versions = {f: FEATURE_VERSION for f in self.features}

    @property
    def content_hash(self) -> str:
        blob = json.dumps({"features": self.features, "versions": self.feature_versions}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "features": self.features,
                "feature_versions": self.feature_versions, "description": self.description,
                "content_hash": self.content_hash}


def save_feature_set(conn, fs: FeatureSet) -> dict:
    ex = conn.execute("SELECT content_hash FROM ml_feature_set WHERE name=? AND version=?",
                      (fs.name, fs.version)).fetchone()
    if ex:
        if ex[0] != fs.content_hash:
            raise ValueError(f"feature set {fs.key} already exists with different content; use a new version")
        return get_feature_set(conn, fs.name, fs.version)
    conn.execute("INSERT INTO ml_feature_set (name,version,features_json,feature_versions_json,description,"
                 "content_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                 (fs.name, fs.version, json.dumps(fs.features), json.dumps(fs.feature_versions), fs.description,
                  fs.content_hash, datetime.now()))
    conn.commit()
    return get_feature_set(conn, fs.name, fs.version)


def get_feature_set(conn, name: str, version: str | None = None) -> dict | None:
    q = "SELECT * FROM ml_feature_set WHERE name=?" + (" AND version=?" if version else "") + \
        " ORDER BY created_at DESC LIMIT 1"
    r = conn.execute(q, (name, version) if version else (name,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["features"] = json.loads(d.pop("features_json"))
    d["feature_versions"] = json.loads(d.pop("feature_versions_json"))
    return d


def ensure_builtin_sets(conn) -> list:
    out = []
    for name, feats in BUILTIN_SETS.items():
        out.append(save_feature_set(conn, FeatureSet(name, "1", list(feats),
                                                     f"built-in {name} feature set")))
    return out
