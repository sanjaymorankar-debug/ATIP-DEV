"""
Derivatives-derived strategy features (W30, QR-10), point in time.

From fo_underlying_daily (data/derivatives.py, the NSE F&O bhavcopy of each session --
published after that session's close, so a value dated D is known at D's close):

    dv_iv_atm         at-the-money implied volatility, %   (stocks / indices with options)
    dv_iv_rank        (iv - min) / (max - min) x 100 over the trailing 252 stored sessions,
                      None with fewer than MIN_HISTORY sessions
    dv_iv_pct         share of the trailing window below today's IV x 100
    dv_vrp            volatility risk premium: atm_iv - realised 20-session close-to-close
                      volatility (annualised %), from the same session's bars
    dv_iv_skew        IV(~95% put) - IV(~105% call), vol points (crash insurance demand)
    dv_pcr_oi         put / call open interest, all expiries
    dv_basis_ann      near future vs spot, annualised % by days to expiry (carry)
    dv_fut_oi_chg_pct futures OI change / futures OI, %
    dv_fno            1 when the symbol has F&O contracts that session (shortable via futures)

DerivHistory(conn, start, end).on(as_of, symbol) serves them; QuantHistory merges them,
so they work anywhere qf_ / ev_ features do (W3 strategies, backtests).
"""

from __future__ import annotations

import math
from datetime import date

FEATURES = ("dv_iv_atm", "dv_iv_rank", "dv_iv_pct", "dv_vrp", "dv_iv_skew", "dv_pcr_oi", "dv_basis_ann",
            "dv_fut_oi_chg_pct", "dv_fno")
WINDOW, MIN_HISTORY = 252, 20


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


class DerivHistory:
    def __init__(self, conn, start=None, end=None):
        self._rows = {}
        self._iv = {}
        q = ("SELECT date, symbol, atm_iv, iv_skew, pcr_oi, fut_close, underlying_price, near_expiry, fut_oi, "
             "fut_oi_chg FROM fo_underlying_daily")
        args = []
        if end:
            q += " WHERE date<=?"
            args.append(str(end))
        try:
            rows = conn.execute(q + " ORDER BY date", args).fetchall()
        except Exception:
            rows = []
        for d, s, iv, skew, pcr, fut, spot, exp, foi, foic in rows:
            d = _d(d)
            self._iv.setdefault(s, []).append((d, iv))
            basis = None
            if fut and spot and exp:
                dte = max(1, (_d(exp) - d).days)
                basis = (fut / spot - 1) * 100 * 365 / dte
            self._rows[(d, s)] = {"dv_iv_atm": iv, "dv_iv_skew": skew, "dv_pcr_oi": pcr, "dv_basis_ann": basis,
                                  "dv_fut_oi_chg_pct": (foic / foi * 100) if foi and foic is not None else None,
                                  "dv_fno": 1}
        self._conn = conn

    def _rank(self, s, d, iv):
        hist = [v for (dd, v) in self._iv.get(s, []) if dd < d and v is not None][-WINDOW:]
        if iv is None or len(hist) < MIN_HISTORY:
            return None, None
        lo, hi = min(hist + [iv]), max(hist + [iv])          # today inside its own range: 0..100
        rank = (iv - lo) / (hi - lo) * 100 if hi > lo else None
        return rank, sum(1 for v in hist if v < iv) / len(hist) * 100

    def _rv20(self, s, d):
        rows = self._conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 ORDER BY "
                                  "date DESC LIMIT 21", (s, str(d))).fetchall()
        c = [r[0] for r in rows][::-1]
        if len(c) < 21:
            return None
        r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
        m = sum(r) / len(r)
        return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252) * 100

    def on(self, as_of, symbol) -> dict:
        d = _d(as_of)
        key = {"NIFTY50": "NIFTY"}.get(symbol, symbol)     # NSE F&O underlying names an index differently
        base = self._rows.get((d, key))
        if base is None:
            return {}
        out = dict(base)
        out["dv_iv_rank"], out["dv_iv_pct"] = self._rank(key, d, out["dv_iv_atm"])
        rv = self._rv20(symbol, d) if out["dv_iv_atm"] is not None else None
        out["dv_vrp"] = (out["dv_iv_atm"] - rv) if rv is not None else None
        return out
