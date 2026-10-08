"""
Regime providers: deterministic, ML and hybrid (the W3 RegimeProvider interface).

    deterministic  strategy_engine.regime.MarketHealthRegime -- the existing
                   Market Health label; the default, unchanged
    ml             the stored prediction of the regime model (config ml.regime_model,
                   purpose "regime", label market_regime) for NIFTY50 on that date,
                   used when its confidence >= min_confidence; otherwise the
                   deterministic label. Every other market field (mh_score, vix,
                   breadth, flows) stays deterministic.
    hybrid         the MORE DEFENSIVE of the deterministic and ML labels
                   (STRONG_BULL < BULL < NEUTRAL < BEAR < HIGH_RISK). ML can make
                   the regime more cautious, never less -- it cannot loosen a
                   deterministic risk regime such as HIGH_RISK -> NO_TRADE.
    hmm            W39 (ML-17): the unsupervised market-state model (ml/hmm.py, a
                   Gaussian HMM of NIFTY50 return / realised vol / VIX), walk-forward:
                   the state for as_of is FILTERED from observations <= as_of with a
                   model fitted on sessions <= its refit anchor <= as_of. Its state
                   (CALM_BULL .. VOLATILE_BEAR) maps onto the label through
                   ml.hmm.HMM_TO_REGIME (CALM_BULL -> BULL ... VOLATILE_BEAR ->
                   HIGH_RISK; never STRONG_BULL) when its probability >= min_confidence;
                   otherwise, or with too little history, the deterministic label.
                   Config ml.regime_hmm (all optional): {"n_states": 3, "lookback": 750,
                   "refit_every": 21, "min_obs": 250, "min_confidence": 0.5, "vix": true,
                   "breadth": false}. No trained model or registry entry is needed.

The dict gains "regime_source" and, where used, "regime_deterministic",
"regime_ml", "regime_ml_confidence" (hmm: "regime_hmm", "regime_hmm_confidence",
"regime_hmm_probabilities", "regime_hmm_fit_end") so decisions show which one applied.
Chosen by config ml.regime_source; anything unusable falls back to deterministic.
ml.config.settings() maps a regime_source it does not list to deterministic and
drops keys it does not know, so "hmm" and ml.regime_hmm are also read from the
raw config section here.
"""

from __future__ import annotations

import json
from datetime import date

from strategy_engine.regime import TREND, MarketHealthRegime, RegimeProvider

RISK_ORDER = ["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "HIGH_RISK"]
MIN_CONFIDENCE = 0.5
HMM_SOURCE = "hmm"
HMM_DEFAULTS = {"n_states": 3, "lookback": 750, "refit_every": 21, "min_obs": 250, "min_confidence": MIN_CONFIDENCE,
                "vix": True, "breadth": False}


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


class MLRegime(RegimeProvider):
    name = "ml"

    def __init__(self, conn, start=None, end=None, model_id=None, min_confidence=MIN_CONFIDENCE, hybrid=False):
        from ml.config import settings
        self.base = MarketHealthRegime(conn, start, end)
        self.model_id = model_id or settings().get("regime_model")
        self.min_confidence, self.hybrid = min_confidence, hybrid
        self.name = "hybrid" if hybrid else "ml"
        self._ml = {}
        if self.model_id:
            q = ("SELECT as_of, prediction, confidence FROM ml_prediction WHERE model_id=? AND symbol='NIFTY50' "
                 "AND version_status='ACTIVE'")
            args = [self.model_id]
            if start:
                q += " AND as_of>=?"; args.append(str(start))
            if end:
                q += " AND as_of<=?"; args.append(str(end))
            for a, p, c in conn.execute(q + " ORDER BY created_at", args):
                self._ml[_d(a)] = (p, c)

    def on(self, as_of: date) -> dict:
        out = dict(self.base.on(as_of))
        det = out.get("regime")
        ml, conf = self._ml.get(as_of, (None, None))
        out.update(regime_source="deterministic", regime_deterministic=det, regime_ml=ml, regime_ml_confidence=conf)
        if ml not in RISK_ORDER or conf is None or conf < self.min_confidence:
            return out
        if self.hybrid:
            pick = max([r for r in (det, ml) if r in RISK_ORDER], key=RISK_ORDER.index)
            src = "hybrid"
        else:
            pick, src = ml, "ml"
        out.update(regime=pick, regime_source=src, market_trend=TREND.get(pick, "unknown"))
        return out


class HMMRegime(RegimeProvider):
    name = HMM_SOURCE

    def __init__(self, conn, start=None, end=None, n_states=3, lookback=750, refit_every=21, min_obs=250,
                 min_confidence=MIN_CONFIDENCE, vix=True, breadth=False):
        from ml.hmm import MarketStates
        self.base = MarketHealthRegime(conn, start, end)
        self.min_confidence = float(min_confidence)
        # history before `start` is what the model is fitted on, so only `end` bounds the load
        self.states = MarketStates(conn, end, n_states, lookback, refit_every, min_obs, bool(vix), bool(breadth))

    def on(self, as_of: date) -> dict:
        out = dict(self.base.on(as_of))
        det = out.get("regime")
        out.update(regime_source="deterministic", regime_deterministic=det, regime_hmm=None,
                   regime_hmm_confidence=None)
        try:
            r = self.states.at(as_of)
        except Exception as e:                                # unusable -> deterministic, said why
            out["regime_hmm_status"] = f"ERROR: {e}"
            return out
        out["regime_hmm_status"] = r["status"]
        if r["status"] != "OK":
            return out
        out.update(regime_hmm=r["state"], regime_hmm_confidence=r["confidence"],
                   regime_hmm_probabilities=r["probabilities"], regime_hmm_fit_end=r["fit_end"])
        if r["regime"] not in RISK_ORDER or r["confidence"] < self.min_confidence:
            return out
        out.update(regime=r["regime"], regime_source=HMM_SOURCE, market_trend=TREND.get(r["regime"], "unknown"))
        return out


def _raw_ml() -> dict:
    from ml.config import CONFIG_PATH
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("ml") or {}
    except Exception:
        return {}


def hmm_settings(s=None, raw=None) -> dict:
    """ml.regime_hmm over HMM_DEFAULTS (unknown keys ignored)."""
    raw = _raw_ml() if raw is None else raw
    given = (s or {}).get("regime_hmm") or raw.get("regime_hmm") or {}
    return {**HMM_DEFAULTS, **{k: v for k, v in (given if isinstance(given, dict) else {}).items()
                               if k in HMM_DEFAULTS}}


def provider_from_config(conn, start=None, end=None) -> RegimeProvider:
    from ml.config import settings
    s = settings()
    src = s.get("regime_source", "deterministic")
    raw = _raw_ml()
    if HMM_SOURCE in (src, raw.get("regime_source")):        # W39: opt-in only
        return HMMRegime(conn, start, end, **hmm_settings(s, raw))
    if src == "deterministic" or not s.get("regime_model"):
        return MarketHealthRegime(conn, start, end)
    return MLRegime(conn, start, end, s["regime_model"], hybrid=(src == "hybrid"))
