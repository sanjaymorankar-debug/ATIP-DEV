"""
Market regime for strategies (SE-08).

ATIP already detects a regime: Market Health (scores/engine.py compute_mh)
labels each session STRONG_BULL / BULL / NEUTRAL / BEAR / HIGH_RISK, stored in
market_health (411 sessions after the W1 backfill). This module reads it --
it does not detect anything new -- and adds a volatility regime from India
VIX (LOW / NORMAL / HIGH). Both are point in time: a date's regime is only
ever read for decisions at or after that date.

    MarketHealthRegime(conn).on(as_of) -> {"regime", "mh_score", "vix", "vol_regime", "market_trend",
                                           "breadth_pct", "adv_decline", "fii_net_cr", "dii_net_cr"}

breadth_pct / adv_decline come from the same market_health row; fii_net_cr /
dii_net_cr from fii_dii_market for that date (None when not stored).

market_trend maps the MH label onto the broader taxonomy strategies can select on:
STRONG_BULL/BULL -> bullish, NEUTRAL -> sideways, BEAR -> bearish,
HIGH_RISK -> uncertain; no row -> unknown.

RegimeProvider is the interface. W5 adds ml/regime.py: MLRegime (a trained
regime model) and HybridRegime (the more defensive of the deterministic and ML
labels). get_provider() picks one from config ml.regime_source; the default is
this deterministic provider, so nothing changes unless the owner opts in.
"""

from __future__ import annotations

from datetime import date

TREND = {"STRONG_BULL": "bullish", "BULL": "bullish", "NEUTRAL": "sideways", "BEAR": "bearish",
         "HIGH_RISK": "uncertain"}
VIX_LOW, VIX_HIGH = 13.0, 20.0       # below / at-or-above: LOW / HIGH volatility regime


class RegimeProvider:
    """Interface: regime information known at the close of `as_of`."""
    name = "base"

    def on(self, as_of: date) -> dict:
        raise NotImplementedError


class MarketHealthRegime(RegimeProvider):
    name = "market_health"

    def __init__(self, conn, start=None, end=None, vix_low=VIX_LOW, vix_high=VIX_HIGH):
        q = ("SELECT date, regime, mh_score, vix_level, pct_advancing, adv_decline FROM market_health "
             "WHERE mh_score IS NOT NULL")
        args = []
        if start:
            q += " AND date>=?"; args.append(str(start))
        if end:
            q += " AND date<=?"; args.append(str(end))
        self._rows = {}
        for d, regime, mh, vix, breadth, adv in conn.execute(q, args):
            d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
            self._rows[d] = (regime, mh, vix, breadth, adv)
        self._flows = {}
        try:
            fq = "SELECT date, fii_net_cr, dii_net_cr FROM fii_dii_market"
            for d, f, di in conn.execute(fq + (" WHERE date<=?" if end else ""), [str(end)] if end else []):
                self._flows[d if isinstance(d, date) else date.fromisoformat(str(d)[:10])] = (f, di)
        except Exception:
            pass
        self.vix_low, self.vix_high = vix_low, vix_high

    def on(self, as_of: date) -> dict:
        regime, mh, vix, breadth, adv = self._rows.get(as_of, (None, None, None, None, None))
        fii, dii = self._flows.get(as_of, (None, None))
        vol = None
        if vix is not None:
            vol = "LOW" if vix < self.vix_low else "HIGH" if vix >= self.vix_high else "NORMAL"
        return {"regime": regime, "mh_score": mh, "vix": vix, "vol_regime": vol,
                "market_trend": TREND.get(regime, "unknown") if regime else "unknown",
                "breadth_pct": breadth, "adv_decline": adv, "fii_net_cr": fii, "dii_net_cr": dii}


def get_provider(conn, start=None, end=None) -> RegimeProvider:
    """The configured regime provider (config ml.regime_source): deterministic
    (default) | ml | hybrid. Falls back to deterministic when ML is unavailable."""
    try:
        from ml.regime import provider_from_config
        return provider_from_config(conn, start, end)
    except Exception:
        return MarketHealthRegime(conn, start, end)
