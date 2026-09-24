"""
The feature catalogue: every input a strategy rule, factor or ranking can use.

Each feature is computed for one symbol at one decision date from a
FeatureContext holding only information available at that date's close:
bars up to it, that date's ATIP scores, that date's market regime, and the
NIFTY50 benchmark up to it. The same code serves live decisions and W2
backtests, so a strategy cannot behave differently in the two.

Technical features are computed from bars rather than read from
technical_indicators, which holds only the recent sessions (~15); prices are
split/bonus adjusted (prices_daily).

  Bars         close open high low volume prev_close change_pct gap_pct
  Parametric   sma_N ema_N rsi_N atr_pct_N ret_N vol_ratio_N zscore_N
               range_pos_N prior_high_N prior_low_N volatility_N rel_strength_N
  ATIP scores  vpi spi rri mri cri msi zpi acs atip_score score_signal
               (score_signal = the signal engine's BUY / SELL / HOLD / WAIT)
  Market       regime (Market Health label) mh_score vix vol_regime (LOW/NORMAL/HIGH)
               market_trend (bullish / sideways / bearish / uncertain / unknown)

Formulas (N = the number in the name, bars ending at the decision date):
  sma_N        mean of the last N closes
  ema_N        EMA with alpha 2/(N+1), seeded with the SMA of the first N closes
  rsi_N        Wilder RSI: seed = mean gain/loss of the first N changes, then
               avg = (prev * (N-1) + current) / N; 100 when there are no losses
  atr_pct_N    mean true range of the last N bars / close x 100
  ret_N        (close / close N bars earlier - 1) x 100
  vol_ratio_N  volume / mean volume of the N bars BEFORE the decision bar
  zscore_N     (close - mean) / sample stdev over the last N closes
  range_pos_N  (close - lowest low) / (highest high - lowest low) x 100, last N bars
  prior_high_N highest high of the N bars before the decision bar (breakouts)
  prior_low_N  lowest low of the N bars before the decision bar
  volatility_N sample stdev of the last N daily returns x sqrt(252) x 100
  rel_strength_N  ret_N of the stock - ret_N of NIFTY50 over the same dates

A feature that cannot be computed (too little history, no score, no
benchmark) is None, and a condition on it is not met.

register_feature() adds a feature -- e.g. a future ML model's prediction --
without touching the engine.
"""

from __future__ import annotations

import math
import re

_PARAMETRIC = re.compile(r"^(sma|ema|rsi|atr_pct|ret|vol_ratio|zscore|range_pos|prior_high|prior_low|"
                         r"volatility|rel_strength)_(\d+)$")
SCORE_FEATURES = ("vpi", "spi", "rri", "mri", "cri", "msi", "zpi", "acs", "atip_score", "score_signal")
MARKET_FEATURES = ("regime", "mh_score", "vix", "vol_regime", "market_trend")
BAR_FEATURES = ("close", "open", "high", "low", "volume", "prev_close", "change_pct", "gap_pct")

_CUSTOM = {}      # name -> (fn(ctx) -> value, inputs tuple)


class FeatureContext:
    """
    What is known about `symbol` at the close of `as_of`.

    bars       Bar objects (backtest.data.Bar) up to and including as_of
    scores     that date's ai_scores row for the symbol, or {}
    market     {"regime", "mh_score", "vix", "vol_regime"} for that date
    benchmark  NIFTY50 closes as {date: close} up to as_of
    """

    def __init__(self, symbol, as_of, bars, scores=None, market=None, benchmark=None, previous=None):
        self.symbol, self.as_of, self.bars = symbol, as_of, bars
        self.scores, self.market, self.benchmark = scores or {}, market or {}, benchmark or {}
        self._previous = previous          # callable -> FeatureContext for the prior session
        self._cache = {}

    def get(self, name):
        if name not in self._cache:
            self._cache[name] = compute(name, self)
        return self._cache[name]

    def previous(self):
        """The same symbol one session earlier (for crosses_above / crosses_below)."""
        return self._previous() if self._previous else None


def register_feature(name: str, fn, inputs=("bars",)):
    """Add a feature computed by fn(ctx). Names may not shadow built-ins."""
    if known(name) and name not in _CUSTOM:
        raise ValueError(f"{name!r} is already a built-in feature")
    _CUSTOM[name] = (fn, tuple(inputs))


def known(name: str) -> bool:
    return (name in BAR_FEATURES or name in SCORE_FEATURES or name in MARKET_FEATURES
            or name in _CUSTOM or bool(_PARAMETRIC.match(name)))


def inputs_of(name: str) -> tuple:
    """Which data a feature needs: bars, scores, regime, benchmark."""
    if name in SCORE_FEATURES:
        return ("scores",)
    if name in MARKET_FEATURES:
        return ("regime",)
    if name in _CUSTOM:
        return _CUSTOM[name][1]
    m = _PARAMETRIC.match(name)
    if m and m.group(1) == "rel_strength":
        return ("bars", "benchmark")
    return ("bars",)


def catalogue() -> dict:
    return {"bars": list(BAR_FEATURES), "scores": list(SCORE_FEATURES), "market": list(MARKET_FEATURES),
            "parametric": sorted({"sma_N", "ema_N", "rsi_N", "atr_pct_N", "ret_N", "vol_ratio_N", "zscore_N",
                                  "range_pos_N", "prior_high_N", "prior_low_N", "volatility_N", "rel_strength_N"}),
            "custom": sorted(_CUSTOM)}


# -- computations ------------------------------------------------------------

def _closes(ctx, n=None):
    c = [b.close for b in ctx.bars]
    return c[-n:] if n else c


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _ema(values, n):
    if len(values) < n:
        return None
    e = sum(values[:n]) / n
    a = 2 / (n + 1)
    for v in values[n:]:
        e = a * v + (1 - a) * e
    return e


def _rsi(values, n):
    if len(values) < n + 1:
        return None
    ch = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(c, 0) for c in ch[:n]) / n
    loss = sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        gain = (gain * (n - 1) + max(c, 0)) / n
        loss = (loss * (n - 1) + max(-c, 0)) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _ret(values, n):
    if len(values) < n + 1 or not values[-1 - n]:
        return None
    return (values[-1] / values[-1 - n] - 1) * 100


def compute(name, ctx):
    if name in _CUSTOM:
        return _CUSTOM[name][0](ctx)
    if name in SCORE_FEATURES:
        key = "signal" if name == "score_signal" else name
        return ctx.scores.get(key)
    if name in MARKET_FEATURES:
        return ctx.market.get(name)
    bars = ctx.bars
    if not bars:
        return None
    last = bars[-1]
    if name in ("close", "open", "high", "low", "volume"):
        return getattr(last, name)
    if name == "prev_close":
        return bars[-2].close if len(bars) > 1 else None
    if name == "change_pct":
        return (last.close / bars[-2].close - 1) * 100 if len(bars) > 1 and bars[-2].close else None
    if name == "gap_pct":
        return (last.open / bars[-2].close - 1) * 100 if len(bars) > 1 and bars[-2].close else None
    m = _PARAMETRIC.match(name)
    if not m:
        raise KeyError(f"unknown feature {name!r}")
    kind, n = m.group(1), int(m.group(2))
    if n < 1:
        return None
    closes = _closes(ctx)
    if kind == "sma":
        return sum(closes[-n:]) / n if len(closes) >= n else None
    if kind == "ema":
        return _ema(closes, n)
    if kind == "rsi":
        return _rsi(closes, n)
    if kind == "ret":
        return _ret(closes, n)
    if kind == "atr_pct":
        if len(bars) < n + 1:
            return None
        trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
               for p, b in zip(bars[-n - 1:-1], bars[-n:])]
        return sum(trs) / n / last.close * 100 if last.close else None
    if kind == "vol_ratio":
        prior = bars[-n - 1:-1]
        if len(prior) < n:
            return None
        avg = sum(b.volume for b in prior) / n
        return last.volume / avg if avg else None
    if kind == "zscore":
        if len(closes) < n:
            return None
        w = closes[-n:]
        sd = _sd(w)
        return (closes[-1] - sum(w) / n) / sd if sd else None
    if kind == "range_pos":
        if len(bars) < n:
            return None
        hi, lo = max(b.high for b in bars[-n:]), min(b.low for b in bars[-n:])
        return (last.close - lo) / (hi - lo) * 100 if hi > lo else None
    if kind == "prior_high":
        return max(b.high for b in bars[-n - 1:-1]) if len(bars) > n else None
    if kind == "prior_low":
        return min(b.low for b in bars[-n - 1:-1]) if len(bars) > n else None
    if kind == "volatility":
        if len(closes) < n + 1:
            return None
        rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - n, len(closes))]
        sd = _sd(rets)
        return sd * math.sqrt(252) * 100 if sd is not None else None
    if kind == "rel_strength":
        if len(bars) < n + 1:
            return None
        d0, d1 = bars[-1 - n].date, bars[-1].date
        b0, b1 = ctx.benchmark.get(d0), ctx.benchmark.get(d1)
        stock = _ret(closes, n)
        if stock is None or not b0 or not b1:
            return None
        return stock - (b1 / b0 - 1) * 100
    return None
