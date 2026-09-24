"""
Label generation -- computed ONLY from information after the row's date.

A label for (symbol, t) looks at bars t+1 .. t+h of that symbol (or, for the
market-regime label, the Market Health row h sessions later). It is computed
by this module from the raw bar list, separately from feature calculation
(which only ever sees a point-in-time view ending at t), and it is never
passed to a model as an input. Each label records its label_date -- the date
it became known (t+h) -- so the dataset can drop rows whose label is not yet
known at the dataset's end date and purge train rows that overlap validation.

Kinds (LabelSpec.kind):
    direction          UP / DOWN / NEUTRAL: forward return vs +/- threshold_pct   (classification)
    binary_return      1 when forward return > threshold_pct, else 0              (classification)
    forward_return     forward return % over h sessions                           (regression)
    volatility_regime  LOW / NORMAL / HIGH: annualised stdev of the next h daily
                       returns vs vol_low / vol_high                              (classification)
    market_regime      Market Health regime h sessions later (market-level rows) (classification)

forward return = close[t+h] / close[t] - 1, in %. Entry at the close of t is
the convention of the post-market decision cycle (decide after the close).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

KINDS = {"direction": "classification", "binary_return": "classification", "forward_return": "regression",
         "volatility_regime": "classification", "market_regime": "classification"}
CLASSES = {"direction": ["DOWN", "NEUTRAL", "UP"], "binary_return": [0, 1],
           "volatility_regime": ["LOW", "NORMAL", "HIGH"],
           "market_regime": ["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "HIGH_RISK"]}


@dataclass
class LabelSpec:
    kind: str = "direction"
    horizon: int = 5
    threshold_pct: float = 1.0
    vol_low: float = 15.0
    vol_high: float = 35.0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"label kind must be one of {sorted(KINDS)}")
        if not isinstance(self.horizon, int) or self.horizon < 1 or self.horizon > 250:
            raise ValueError("label horizon must be an integer 1..250 sessions")
        if self.threshold_pct < 0:
            raise ValueError("threshold_pct cannot be negative")

    @property
    def task(self) -> str:
        return KINDS[self.kind]

    @property
    def classes(self):
        return CLASSES.get(self.kind)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(task=self.task, classes=self.classes)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LabelSpec":
        return cls(**{k: d[k] for k in ("kind", "horizon", "threshold_pct", "vol_low", "vol_high") if k in d})


def label_for(bars: list, i: int, spec: LabelSpec, regimes: dict | None = None, sessions: list | None = None):
    """(value, label_date) for the row at bars[i], or (None, None) when the
    horizon has not elapsed in the data. Uses bars[i+1 .. i+h] only."""
    h = spec.horizon
    if spec.kind == "market_regime":
        if sessions is None or regimes is None:
            return None, None
        d = bars[i].date if hasattr(bars[i], "date") else bars[i]
        try:
            k = sessions.index(d)
        except ValueError:
            return None, None
        if k + h >= len(sessions):
            return None, None
        ld = sessions[k + h]
        return regimes.get(ld), ld
    if i + h >= len(bars):
        return None, None
    c0, ch = bars[i].close, bars[i + h].close
    if not c0:
        return None, None
    fwd = (ch / c0 - 1) * 100
    ld = bars[i + h].date
    if spec.kind == "forward_return":
        return round(fwd, 6), ld
    if spec.kind == "binary_return":
        return (1 if fwd > spec.threshold_pct else 0), ld
    if spec.kind == "direction":
        return ("UP" if fwd > spec.threshold_pct else "DOWN" if fwd < -spec.threshold_pct else "NEUTRAL"), ld
    rets = [bars[j].close / bars[j - 1].close - 1 for j in range(i + 1, i + h + 1) if bars[j - 1].close]
    if len(rets) < 2:
        return None, None
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252) * 100
    return ("LOW" if vol < spec.vol_low else "HIGH" if vol >= spec.vol_high else "NORMAL"), ld
