"""
The strategy definition (SE-01): one JSON document per strategy VERSION.

    {
      "strategy_id": "atip_zpi_momentum",          # lowercase slug, stable across versions
      "name": "ATIP buy-zone momentum",
      "version": "1.0.0",
      "description": "...",
      "kind": "rule" | "multi_factor" | "quant_rank" | "composite" | "python" | "pairs" | "portfolio",
      "universe": {"type": "tracked_current"} | {"type": "symbols", "symbols": [...]},
      "timeframe": "1d",
      "parameters": [ParamSpec, ...],              # see params.py
      "position": {"target_position_pct": 10, "max_positions": 10, "max_new_per_day": 5,
                   "stop_pct": 3, "target_pct": 6, "max_hold_sessions": 10},
      "risk": {"requirement": "STANDARD", "max_cri": 75, "blocked_regimes": ["HIGH_RISK"]},
      ... kind-specific keys, see kinds.py ...
    }
    kind "option_overlay" (W40): multi-leg OPTION intents -- keys in strategy_engine/option_overlay.py

Any value in position/risk/rules/factors may be {"param": name}.

validate() checks the whole document and returns it normalised with two
derived keys: features_used and inputs (bars / scores / regime / benchmark).
definition_hash() is the SHA-256 of the normalised document: a version's hash
never changes, so "same version" means "same strategy" (registry.py refuses
to store different content under an existing version).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from strategy_engine import features as F
from strategy_engine import rules as R
from strategy_engine.decisions import RISK_REQUIREMENTS
from strategy_engine.params import ParamError, ParamSpec, referenced, resolve

KINDS = ("rule", "multi_factor", "quant_rank", "composite", "python", "pairs", "portfolio")
PAIR_KEYS = {"pair_id", "asset_a", "asset_b", "hedge_ratio", "spread_kind", "lookback", "entry_z", "exit_z", "stop_z",
             "capital_allocation_pct"}
PORTFOLIO_METHODS = ("equal", "score", "inverse_vol", "risk", "factor")
TIMEFRAMES = ("1d",)
COMPOSITE_MODES = ("vote", "weighted", "priority", "regime_select")
POSITION_KEYS = {"target_position_pct", "max_positions", "max_new_per_day", "stop_pct", "target_pct",
                 "max_hold_sessions"}
RISK_KEYS = {"requirement", "max_cri", "blocked_regimes"}
COMMON_KEYS = {"strategy_id", "name", "version", "description", "kind", "category", "universe", "timeframe",
               "parameters", "position", "risk"}
# category: what the strategy is, for grouping (defaults from its kind)
DEFAULT_CATEGORY = {"rule": "technical", "multi_factor": "multi_factor", "quant_rank": "quant",
                    "composite": "combination", "python": "code", "pairs": "stat_arb", "portfolio": "portfolio"}
KIND_KEYS = {
    "rule": {"entry", "exit", "confirmation", "filter", "rank_by"},
    "multi_factor": {"factors", "entry_threshold", "exit_threshold", "reduce_threshold", "add_threshold",
                     "min_confirmations", "top_n", "filter", "min_factor_coverage"},
    "quant_rank": {"score", "filter", "top_n", "exit_rank", "rebalance_every"},
    "composite": {"members", "mode", "min_agree", "min_agree_exit", "entry_threshold", "exit_threshold",
                  "regime_map", "regime_key", "exit_on_unmapped_regime"},
    "python": {"python_class"},
    "pairs": {"pairs", "allow_single_leg", "short_via_futures"},          # W30: short leg via stock futures
    "portfolio": {"score", "top_n", "bottom_n", "method", "vol_feature", "constraints", "long_short",
                  "rebalance_every", "filter", "allow_long_only", "short_via_futures",
                  "reweight_band_pct"},                                    # W25 PF-06 (accepted from W39)
}
_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# W40 (ENT-15): option overlays -- multi-leg OPTION intents; validated by strategy_engine/option_overlay.py
KINDS = KINDS + ("option_overlay",)
DEFAULT_CATEGORY["option_overlay"] = "options"
KIND_KEYS["option_overlay"] = {"underlyings", "template", "strikes", "expiry", "lots", "exits", "option_risk",
                               "allow_naked_short_calls"}


class DefinitionError(ValueError):
    pass


def specs(defn: dict) -> list:
    return [ParamSpec.from_dict(p) for p in defn.get("parameters") or []]


def validate(defn: dict) -> dict:
    """The definition, checked and normalised; DefinitionError on any problem."""
    d = copy.deepcopy(defn)
    d.pop("features_used", None); d.pop("inputs", None)
    for k in ("strategy_id", "name", "version", "kind"):
        if not d.get(k):
            raise DefinitionError(f"{k} is required")
    if not _SLUG.match(d["strategy_id"]):
        raise DefinitionError("strategy_id must be a lowercase slug: letters, digits, _ (2-64 chars)")
    if not _VERSION.match(str(d["version"])):
        raise DefinitionError("version must look like 1.0.0")
    if d["kind"] not in KINDS:
        raise DefinitionError(f"kind must be one of {KINDS}")
    extra = set(d) - COMMON_KEYS - KIND_KEYS[d["kind"]]
    if extra:
        raise DefinitionError(f"keys not used by a {d['kind']} strategy: {sorted(extra)}")
    d.setdefault("description", "")
    d.setdefault("category", DEFAULT_CATEGORY[d["kind"]])
    d.setdefault("timeframe", "1d")
    if d["timeframe"] not in TIMEFRAMES:
        raise DefinitionError(f"timeframe must be one of {TIMEFRAMES} (ATIP stores daily bars)")
    uni = d.setdefault("universe", {"type": "tracked_current"})
    if uni.get("type") not in ("tracked_current", "symbols"):
        raise DefinitionError("universe.type must be tracked_current or symbols")
    if uni["type"] == "symbols" and not uni.get("symbols"):
        raise DefinitionError("universe.symbols must list at least one symbol")
    try:
        sp = specs(d)
        names = [s.name for s in sp]
        if len(names) != len(set(names)):
            raise DefinitionError("parameter names must be unique")
        for spec in sp:                                # every default valid (required ones have none)
            if spec.default is not None:
                spec.coerce(spec.default)
    except ParamError as e:
        raise DefinitionError(str(e))
    declared = set(names)
    undeclared = referenced(d) - declared
    if undeclared:
        raise DefinitionError(f"undeclared parameters referenced: {sorted(undeclared)}")

    pos = d.setdefault("position", {})
    if set(pos) - POSITION_KEYS:
        raise DefinitionError(f"unknown position keys {sorted(set(pos) - POSITION_KEYS)}")
    risk = d.setdefault("risk", {})
    if set(risk) - RISK_KEYS:
        raise DefinitionError(f"unknown risk keys {sorted(set(risk) - RISK_KEYS)}")
    risk.setdefault("requirement", "STANDARD")
    if risk["requirement"] not in RISK_REQUIREMENTS:
        raise DefinitionError(f"risk.requirement must be one of {RISK_REQUIREMENTS}")

    used = set()
    try:
        k = d["kind"]
        if k == "rule":
            used |= R.validate(d.get("entry"), declared, "entry")
            for key in ("exit", "confirmation", "filter"):      # optional rule types
                if d.get(key) is not None:
                    used |= R.validate(d[key], declared, key)
            if d.get("rank_by"):
                _feature(d["rank_by"]); used.add(d["rank_by"])
        elif k == "multi_factor":
            fs = d.get("factors")
            if not fs:
                raise DefinitionError("multi_factor needs factors")
            for i, f in enumerate(fs):
                if set(f) - {"feature", "weight", "direction", "min", "max", "threshold"}:
                    raise DefinitionError(f"factors[{i}]: unknown keys")
                _feature(f.get("feature")); used.add(f["feature"])
                if f.get("direction", "higher") not in ("higher", "lower"):
                    raise DefinitionError(f"factors[{i}].direction must be higher or lower")
            if d.get("entry_threshold") is None:
                raise DefinitionError("multi_factor needs entry_threshold")
            if d.get("filter"):
                used |= R.validate(d["filter"], declared, "filter")
        elif k == "quant_rank":
            sc = d.get("score")
            if not sc or not sc.get("terms"):
                raise DefinitionError("quant_rank needs score.terms: [{feature, weight}]")
            for t in sc["terms"]:
                _feature(t.get("feature")); used.add(t["feature"])
            if sc.get("direction", "desc") not in ("asc", "desc"):
                raise DefinitionError("score.direction must be asc or desc")
            if d.get("top_n") is None:
                raise DefinitionError("quant_rank needs top_n")
            if d.get("filter"):
                used |= R.validate(d["filter"], declared, "filter")
        elif k == "composite":
            if d.get("mode") not in COMPOSITE_MODES:
                raise DefinitionError(f"composite mode must be one of {COMPOSITE_MODES}")
            ms = d.get("members")
            if not ms:
                raise DefinitionError("composite needs members")
            for i, m in enumerate(ms):
                if not m.get("strategy_id") or not m.get("version"):
                    raise DefinitionError(f"members[{i}] needs strategy_id and version")
                if m["strategy_id"] == d["strategy_id"]:
                    raise DefinitionError("a composite cannot contain itself")
            if d["mode"] == "regime_select":
                if not isinstance(d.get("regime_map"), dict) or not d["regime_map"]:
                    raise DefinitionError("regime_select needs regime_map {regime: [member strategy_id, ...]}")
                ids = {m["strategy_id"] for m in ms}
                for reg, lst in d["regime_map"].items():
                    if set(lst) - ids:
                        raise DefinitionError(f"regime_map[{reg}] names non-members {sorted(set(lst) - ids)}")
                d.setdefault("regime_key", "market_trend")
                if d["regime_key"] not in F.MARKET_FEATURES:
                    raise DefinitionError(f"regime_key must be one of {F.MARKET_FEATURES}")
                used |= {d["regime_key"]}
        elif k == "pairs":
            ps = d.get("pairs")
            if not ps:
                raise DefinitionError("pairs needs pairs: [{asset_a, asset_b, ...}]")
            syms = set()
            for i, pr in enumerate(ps):
                if set(pr) - PAIR_KEYS:
                    raise DefinitionError(f"pairs[{i}]: unknown keys {sorted(set(pr) - PAIR_KEYS)}")
                if not pr.get("asset_a") or not pr.get("asset_b") or pr["asset_a"] == pr["asset_b"]:
                    raise DefinitionError(f"pairs[{i}]: two different assets are required")
                pr["asset_a"], pr["asset_b"] = pr["asset_a"].upper(), pr["asset_b"].upper()
                if pr.get("spread_kind", "log") not in ("log", "ratio"):
                    raise DefinitionError(f"pairs[{i}].spread_kind must be log or ratio")
                syms |= {pr["asset_a"], pr["asset_b"]}
            d["universe"] = {"type": "symbols", "symbols": sorted(syms)}     # a pair trades only its legs
            used.add("close")
        elif k == "portfolio":
            sc = d.get("score") or {}
            _feature(sc.get("feature")); used.add(sc["feature"])
            if d.get("top_n") is None:
                raise DefinitionError("portfolio needs top_n")
            if d.get("method", "equal") not in PORTFOLIO_METHODS:
                raise DefinitionError(f"portfolio method must be one of {PORTFOLIO_METHODS}")
            if d.get("long_short") not in (None, "dollar", "beta", "sector"):
                raise DefinitionError("long_short must be null, dollar, beta or sector")
            vf = d.setdefault("vol_feature", "volatility_60")
            _feature(vf); used.add(vf)
            if set(d.get("constraints") or {}) - {"max_weight", "sector_cap", "gross"}:
                raise DefinitionError("constraints: max_weight, sector_cap, gross")
            band = d.get("reweight_band_pct")
            if band is not None and not (isinstance(band, dict) and set(band) == {"param"}) and (
                    isinstance(band, bool) or not isinstance(band, (int, float)) or not 0 < band <= 100):
                raise DefinitionError("reweight_band_pct: percentage points in (0, 100], or {\"param\": name}")
            if d.get("filter"):
                used |= R.validate(d["filter"], declared, "filter")
            if d.get("long_short") == "beta":
                used.add("rel_strength_20")          # needs the benchmark series for beta
        elif k == "python":
            from backtest.strategies import REGISTRY
            if d.get("python_class") not in REGISTRY:
                raise DefinitionError(f"python_class must be one of {sorted(REGISTRY)}")
            unknown = set(names) - set(REGISTRY[d["python_class"]].default_params)
            if unknown:
                raise DefinitionError(f"parameters {sorted(unknown)} are not accepted by {d['python_class']}")
        elif k == "option_overlay":                      # W40 (ENT-15)
            from strategy_engine.option_overlay import OverlayError, validate_overlay
            try:
                used |= validate_overlay(d, declared, {s.name: s for s in sp})
            except OverlayError as e:
                raise DefinitionError(str(e))
    except R.RuleError as e:
        raise DefinitionError(str(e))
    # risk and position may use features only indirectly; the risk gate reads cri/regime
    if risk.get("max_cri") is not None:
        used.add("cri")
    if risk.get("blocked_regimes"):
        used.add("regime")
    d["features_used"] = sorted(used)
    inputs = set()
    for f in used:
        inputs |= set(F.inputs_of(f))
    if d["kind"] in ("rule", "multi_factor", "quant_rank", "pairs", "portfolio"):
        inputs.add("bars")
    d["inputs"] = sorted(inputs)
    return d


def _feature(name):
    if not name or not F.known(name):
        raise DefinitionError(f"unknown feature {name!r}")


def canonical(defn: dict) -> str:
    return json.dumps(defn, sort_keys=True, separators=(",", ":"))


def definition_hash(defn: dict) -> str:
    return hashlib.sha256(canonical(validate(defn)).encode()).hexdigest()
