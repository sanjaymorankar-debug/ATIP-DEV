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
    signal_outcome     WIN / LOSS / TIMEOUT: within h sessions, does the high reach
                       +target_pct before the low reaches -stop_pct (from the close of
                       t)? A bar touching both counts as LOSS (pessimistic). The same
                       question signal_log / signal_outcome answer for ATIP's own
                       signals, asked for every row                         (classification)
    return_rank        cross-sectional percentile (0..100) of the forward return among
                       all rows of the same date -- a ranking target        (regression)
    meta_label         W40: 1 when a stored technical signal reached its target before its
                       stop / horizon (triple barrier), else 0. Built per SIGNAL by
                       ml/meta_label.py, not per bar: label_for refuses it    (classification)

forward return = close[t+h] / close[t] - 1, in %. Entry at the close of t is
the convention of the post-market decision cycle (decide after the close).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

KINDS = {"direction": "classification", "binary_return": "classification", "forward_return": "regression",
         "volatility_regime": "classification", "market_regime": "classification",
         "signal_outcome": "classification", "return_rank": "regression", "meta_label": "classification"}
CROSS_SECTIONAL_LABELS = ("return_rank",)       # finished per date by the dataset builder
CLASSES = {"direction": ["DOWN", "NEUTRAL", "UP"], "binary_return": [0, 1],
           "volatility_regime": ["LOW", "NORMAL", "HIGH"],
           "market_regime": ["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "HIGH_RISK"],
           "signal_outcome": ["LOSS", "TIMEOUT", "WIN"], "meta_label": [0, 1]}


@dataclass
class LabelSpec:
    kind: str = "direction"
    horizon: int = 5
    threshold_pct: float = 1.0
    vol_low: float = 15.0
    vol_high: float = 35.0
    target_pct: float = 5.0
    stop_pct: float = 3.0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"label kind must be one of {sorted(KINDS)}")
        if not isinstance(self.horizon, int) or self.horizon < 1 or self.horizon > 250:
            raise ValueError("label horizon must be an integer 1..250 sessions")
        if self.threshold_pct < 0:
            raise ValueError("threshold_pct cannot be negative")
        if self.target_pct <= 0 or self.stop_pct <= 0:
            raise ValueError("target_pct and stop_pct must be positive")

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
        return cls(**{k: d[k] for k in ("kind", "horizon", "threshold_pct", "vol_low", "vol_high", "target_pct",
                                        "stop_pct") if k in d})


def label_for(bars: list, i: int, spec: LabelSpec, regimes: dict | None = None, sessions: list | None = None):
    """(value, label_date) for the row at bars[i], or (None, None) when the
    horizon has not elapsed in the data. Uses bars[i+1 .. i+h] only."""
    h = spec.horizon
    if spec.kind == "meta_label":
        raise ValueError("meta_label labels are built per stored signal by ml/meta_label.py, not per bar")
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
    if spec.kind == "signal_outcome":
        up, dn = c0 * (1 + spec.target_pct / 100), c0 * (1 - spec.stop_pct / 100)
        for j in range(i + 1, i + h + 1):
            if bars[j].low <= dn:
                return "LOSS", bars[j].date
            if bars[j].high >= up:
                return "WIN", bars[j].date
        return "TIMEOUT", ld
    if spec.kind in ("forward_return", "return_rank"):     # return_rank: ranked per date by the dataset
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


# -- named, versioned label definitions (ml_label) ------------------------------------

def spec_hash(spec: LabelSpec) -> str:
    return hashlib.sha256(json.dumps(asdict(spec), sort_keys=True).encode()).hexdigest()


def save_label(conn, name: str, version: str, spec: dict, description: str = "") -> dict:
    """Store a reusable label definition as name@version (immutable). Datasets
    embed their label spec too, so this registry is for reuse and discovery."""
    ls = LabelSpec.from_dict(spec)
    lid = f"{name}@{version}"
    h = spec_hash(ls)
    ex = conn.execute("SELECT spec_hash FROM ml_label WHERE label_id=?", (lid,)).fetchone()
    if ex and ex[0] != h:
        raise ValueError(f"label {lid} exists with a different definition; use a new version")
    if not ex:
        conn.execute("INSERT INTO ml_label (label_id,name,version,kind,task,spec_json,spec_hash,description,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (lid, name, version, ls.kind, ls.task, json.dumps(ls.as_dict()),
                                                     h, description, datetime.now()))
        conn.commit()
    return get_label(conn, lid)


def get_label(conn, label_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM ml_label WHERE label_id=?", (label_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["spec"] = json.loads(d.pop("spec_json"))
    return d


BUILTIN_LABELS = {
    "direction_5d": {"kind": "direction", "horizon": 5, "threshold_pct": 1.0},
    "fwd_return_10d": {"kind": "forward_return", "horizon": 10},
    "return_rank_20d": {"kind": "return_rank", "horizon": 20},
    "vol_regime_20d": {"kind": "volatility_regime", "horizon": 20, "vol_low": 15.0, "vol_high": 35.0},
    "market_regime_5d": {"kind": "market_regime", "horizon": 5},
    "signal_outcome_10d": {"kind": "signal_outcome", "horizon": 10, "target_pct": 5.0, "stop_pct": 3.0},
}


def ensure_builtin_labels(conn) -> list:
    return [save_label(conn, n, "1", spec, "built-in") for n, spec in BUILTIN_LABELS.items()]
