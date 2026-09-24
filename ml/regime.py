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

The dict gains "regime_source" and, where used, "regime_deterministic",
"regime_ml", "regime_ml_confidence" so decisions show which one applied.
Chosen by config ml.regime_source; anything unusable falls back to deterministic.
"""

from __future__ import annotations

from datetime import date

from strategy_engine.regime import TREND, MarketHealthRegime, RegimeProvider

RISK_ORDER = ["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "HIGH_RISK"]
MIN_CONFIDENCE = 0.5


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


def provider_from_config(conn, start=None, end=None) -> RegimeProvider:
    from ml.config import settings
    s = settings()
    src = s.get("regime_source", "deterministic")
    if src == "deterministic" or not s.get("regime_model"):
        return MarketHealthRegime(conn, start, end)
    return MLRegime(conn, start, end, s["regime_model"], hybrid=(src == "hybrid"))
