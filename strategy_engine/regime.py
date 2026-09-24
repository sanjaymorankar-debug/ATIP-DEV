"""
Market regime for strategies (SE-08).

ATIP already detects a regime: Market Health (scores/engine.py compute_mh)
labels each session STRONG_BULL / BULL / NEUTRAL / BEAR / HIGH_RISK, stored in
market_health (411 sessions after the W1 backfill). This module reads it --
it does not detect anything new -- and adds a volatility regime from India
VIX (LOW / NORMAL / HIGH). Both are point in time: a date's regime is only
ever read for decisions at or after that date.

    MarketHealthRegime(conn).on(as_of) -> {"regime", "mh_score", "vix", "vol_regime", "market_trend"}

market_trend maps the MH label onto the broader taxonomy strategies can select on:
STRONG_BULL/BULL -> bullish, NEUTRAL -> sideways, BEAR -> bearish,
HIGH_RISK -> uncertain; no row -> unknown.

RegimeProvider is the interface a future model (W5/W6) implements to replace
or extend this -- nothing here is learned.
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
        q = "SELECT date, regime, mh_score, vix_level FROM market_health WHERE mh_score IS NOT NULL"
        args = []
        if start:
            q += " AND date>=?"; args.append(str(start))
        if end:
            q += " AND date<=?"; args.append(str(end))
        self._rows = {}
        for d, regime, mh, vix in conn.execute(q, args):
            d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
            self._rows[d] = (regime, mh, vix)
        self.vix_low, self.vix_high = vix_low, vix_high

    def on(self, as_of: date) -> dict:
        regime, mh, vix = self._rows.get(as_of, (None, None, None))
        vol = None
        if vix is not None:
            vol = "LOW" if vix < self.vix_low else "HIGH" if vix >= self.vix_high else "NORMAL"
        return {"regime": regime, "mh_score": mh, "vix": vix, "vol_regime": vol,
                "market_trend": TREND.get(regime, "unknown") if regime else "unknown"}
