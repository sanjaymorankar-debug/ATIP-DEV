"""
Versioned, point-in-time ML datasets.

DatasetSpec
    name, version, feature_set (name@version), label (LabelSpec), universe
    ("tracked_current" | [symbols] | "market" for market-level rows), start, end,
    frequency "daily", sampling {"every_n_sessions", "max_symbols"}, source.
    dataset_id = name@version; spec_hash = sha256 of the canonical spec.

build(conn, spec) -> Dataset
    For every sampled session t in [start, end] and symbol:
      features  EvalEnv.context(symbol, t) -- the W3 point-in-time view: bars
                dated <= t (W2 PointInTimeView raises LookAheadError otherwise),
                that date's ATIP scores (W2 ScoresHistory guard), that date's
                regime, the benchmark up to t
      label     labels.label_for(bars, i) from bars AFTER t, separately
      kept only when the label's date (t + h) is <= end  -> no label leaks past
                the dataset's end, and a model trained on it has seen nothing
                after `end`
    Rows are ordered by date. Categorical features are one-hot encoded with the
    fixed categories in feature_registry.CATEGORICAL; missing values stay NaN
    (training imputes with TRAIN-period medians only).

time_split(ds, validation_fraction, embargo)
    Chronological: the last fraction of DATES is validation. Train rows whose
    label date falls on or after the first validation date are purged (their
    labels overlap the validation period), plus `embargo` extra sessions
    (default = horizon). No random shuffling anywhere.

The matrix is saved as an .npz snapshot (hash recorded in ml_dataset) so a
training run can be reproduced from the exact rows it used.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from ml.feature_registry import CATEGORICAL, get_feature_set
from ml.labels import LabelSpec, label_for

log = logging.getLogger("atip.ml")


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


@dataclass
class DatasetSpec:
    name: str
    version: str
    feature_set: str                       # "name@version"
    label: dict
    start: str
    end: str
    universe: object = "tracked_current"
    frequency: str = "daily"
    sampling: dict = field(default_factory=lambda: {"every_n_sessions": 1, "max_symbols": None})
    source: str = "prices_daily + ai_scores + market_health + fii_dii_market"
    description: str = ""

    def __post_init__(self):
        LabelSpec.from_dict(self.label)
        if "@" not in self.feature_set:
            raise ValueError("feature_set must be 'name@version'")
        if _d(self.start) >= _d(self.end):
            raise ValueError("start must be before end")
        if _d(self.end) > date.today():
            raise ValueError("end cannot be in the future")
        if self.frequency != "daily":
            raise ValueError("only daily frequency is supported")

    @property
    def dataset_id(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def spec_hash(self) -> str:
        d = asdict(self); d.pop("description", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(dataset_id=self.dataset_id, spec_hash=self.spec_hash)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DatasetSpec":
        keys = ("name", "version", "feature_set", "label", "start", "end", "universe", "frequency", "sampling",
                "source", "description")
        return cls(**{k: d[k] for k in keys if k in d})


class Dataset:
    def __init__(self, spec, columns, X, y, dates, label_dates, symbols, coverage, feature_names):
        self.spec, self.columns = spec, columns
        self.X, self.y = X, y
        self.dates, self.label_dates, self.symbols = dates, label_dates, symbols
        self.coverage, self.feature_names = coverage, feature_names

    def summary(self) -> dict:
        lab = LabelSpec.from_dict(self.spec.label)
        dist = None
        if lab.task == "classification":
            vals, cnt = np.unique(np.array(self.y, dtype=object).astype(str), return_counts=True)
            dist = {str(v): int(c) for v, c in zip(vals, cnt)}
        else:
            yy = np.asarray(self.y, dtype=float)
            dist = {"mean": float(np.mean(yy)), "std": float(np.std(yy))} if len(yy) else {}
        return {"rows": len(self.y), "columns": len(self.columns), "symbols": len(set(self.symbols)),
                "first_date": str(min(self.dates)) if self.dates else None,
                "last_date": str(max(self.dates)) if self.dates else None,
                "last_label_date": str(max(self.label_dates)) if self.label_dates else None,
                "label_distribution": dist, "feature_coverage": self.coverage}


def encode_row(values: dict, feature_names: list) -> tuple:
    """(columns, row) -- numeric as float (NaN when missing), categoricals one-hot."""
    cols, row = [], []
    for f in feature_names:
        v = values.get(f)
        if f in CATEGORICAL:
            for c in CATEGORICAL[f]:
                cols.append(f"{f}={c}"); row.append(1.0 if v == c else 0.0)
        else:
            cols.append(f)
            try:
                row.append(float(v) if v is not None else np.nan)
            except (TypeError, ValueError):
                row.append(np.nan)
    return cols, row


def _symbols(conn, universe, max_symbols):
    if universe == "market":
        return ["NIFTY50"]
    if isinstance(universe, (list, tuple)):
        syms = sorted({s.upper() for s in universe})
    else:
        from backtest.data import tracked_universe
        syms = list(tracked_universe(conn).symbols)
    return syms[:max_symbols] if max_symbols else syms


def build(conn, spec: DatasetSpec, progress=None) -> Dataset:
    from backtest.data import PriceHistory, ScoresHistory
    from strategy_engine.kinds import EvalEnv
    from strategy_engine.regime import MarketHealthRegime
    fs_name, fs_ver = spec.feature_set.split("@", 1)
    fs = get_feature_set(conn, fs_name, fs_ver)
    if not fs:
        raise ValueError(f"unknown feature set {spec.feature_set}")
    feats = fs["features"]
    lab = LabelSpec.from_dict(spec.label)
    start, end = _d(spec.start), _d(spec.end)
    syms = _symbols(conn, spec.universe, (spec.sampling or {}).get("max_symbols"))
    hist = PriceHistory.load(conn, syms, start, end, warmup_days=420)
    if spec.universe == "market":                   # market-level rows: the NIFTY50 index series itself
        from backtest.data import Bar
        nb = []
        for d, o, h, l, c, v in conn.execute(
                "SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol='NIFTY50' AND date>=? "
                "AND date<=? ORDER BY date", (str(start - timedelta(days=420)), str(end))):
            if c and (not nb or _d(d) > nb[-1].date):
                nb.append(Bar(_d(d), float(o or c), float(h or c), float(l or c), float(c), float(v or 0)))
        hist = PriceHistory({"NIFTY50": nb}, [], (str(start), str(end), 420))
    scores = ScoresHistory(conn, start - timedelta(days=10), end)
    regime = MarketHealthRegime(conn, start - timedelta(days=10), end)   # deterministic: never an ML input
    bench = {}
    for d, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND date<=?", (str(end),)):
        bench[_d(d)] = c
    env = EvalEnv(hist, syms, scores, regime, bench)
    sessions = [s for s in hist.sessions if start <= s <= end]
    step = max(1, int((spec.sampling or {}).get("every_n_sessions") or 1))
    sampled = sessions[::step]
    regimes = {d: regime.on(d).get("regime") for d in hist.sessions} if lab.kind == "market_regime" else None
    all_sessions = hist.sessions

    columns, X, y, dates, ldates, symbols = None, [], [], [], [], []
    present = {f: 0 for f in feats}
    n_seen = 0
    for k, t in enumerate(sampled):
        for sym in syms:
            bars = hist.bars.get(sym) or []
            i = hist._index.get(sym, {}).get(t)
            if i is None:
                continue
            value, ld = label_for(bars, i, lab, regimes, all_sessions)
            if value is None or ld is None or ld > end:
                continue                               # label unknown by `end`: not in the dataset
            ctx = env.context(sym, t)
            if ctx is None:
                continue
            vals = {f: ctx.get(f) for f in feats}
            n_seen += 1
            for f in feats:
                if vals[f] is not None:
                    present[f] += 1
            cols, row = encode_row(vals, feats)
            columns = columns or cols
            X.append(row); y.append(value); dates.append(t); ldates.append(ld); symbols.append(sym)
        env._ctx.clear()                               # contexts are per date; free memory
        if progress:
            progress(k + 1, len(sampled))
    coverage = {f: round(present[f] / n_seen, 4) if n_seen else 0.0 for f in feats}
    return Dataset(spec, columns or encode_row({}, feats)[0], np.array(X, dtype=float).reshape(len(X), -1)
                   if X else np.zeros((0, len(encode_row({}, feats)[0]))), y, dates, ldates, symbols, coverage, feats)


def time_split(ds: Dataset, validation_fraction: float = 0.2, embargo: int | None = None) -> tuple:
    """(train_idx, valid_idx, info). Chronological; purges train rows whose label
    date reaches into validation, plus `embargo` sessions."""
    lab = LabelSpec.from_dict(ds.spec.label)
    embargo = lab.horizon if embargo is None else int(embargo)
    udates = sorted(set(ds.dates))
    if len(udates) < 3:
        raise ValueError("too few dates to split in time")
    cut = max(1, int(len(udates) * (1 - validation_fraction)))
    v_start = udates[cut]
    emb_start = udates[max(0, cut - embargo)]
    train, valid, purged = [], [], 0
    for idx, (d, ld) in enumerate(zip(ds.dates, ds.label_dates)):
        if d >= v_start:
            valid.append(idx)
        elif ld >= v_start or d >= emb_start:
            purged += 1
        else:
            train.append(idx)
    return np.array(train, dtype=int), np.array(valid, dtype=int), {
        "method": "chronological, purged + embargo", "validation_start": str(v_start),
        "embargo_sessions": embargo, "purged_rows": purged, "train_rows": len(train), "validation_rows": len(valid),
        "train_end": str(max((ds.dates[i] for i in train), default=None)) if train else None}


def save_snapshot(ds: Dataset, root) -> tuple:
    """Write X / y / dates / symbols to an .npz; returns (path, sha256)."""
    from pathlib import Path
    p = Path(root) / "datasets"
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{ds.spec.name}_{ds.spec.version}_{ds.spec.spec_hash[:10]}.npz"
    np.savez_compressed(f, X=ds.X, y=np.array([str(v) for v in ds.y]), dates=np.array([str(d) for d in ds.dates]),
                        label_dates=np.array([str(d) for d in ds.label_dates]), symbols=np.array(ds.symbols),
                        columns=np.array(ds.columns))
    h = hashlib.sha256(f.read_bytes()).hexdigest()
    return str(f), h


def register(conn, spec: DatasetSpec, ds: Dataset | None = None, snapshot=None) -> dict:
    ex = conn.execute("SELECT spec_hash FROM ml_dataset WHERE dataset_id=?", (spec.dataset_id,)).fetchone()
    if ex and ex[0] != spec.spec_hash:
        raise ValueError(f"dataset {spec.dataset_id} exists with a different spec; use a new version")
    summ = ds.summary() if ds else None
    now = datetime.now()
    conn.execute("INSERT INTO ml_dataset (dataset_id,name,version,spec_json,spec_hash,feature_set,label_json,"
                 "start_date,end_date,universe_json,frequency,sampling_json,source,status,summary_json,snapshot_path,"
                 "snapshot_hash,created_at,built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(dataset_id) DO UPDATE SET status=excluded.status,summary_json=excluded.summary_json,"
                 "snapshot_path=excluded.snapshot_path,snapshot_hash=excluded.snapshot_hash,built_at=excluded.built_at",
                 (spec.dataset_id, spec.name, spec.version, json.dumps(spec.as_dict(), default=str), spec.spec_hash,
                  spec.feature_set, json.dumps(spec.label), spec.start, spec.end, json.dumps(spec.universe),
                  spec.frequency, json.dumps(spec.sampling), spec.source, "BUILT" if ds else "DEFINED",
                  json.dumps(summ, default=str) if summ else None, snapshot[0] if snapshot else None,
                  snapshot[1] if snapshot else None, now, now if ds else None))
    conn.commit()
    return get(conn, spec.dataset_id)


def get(conn, dataset_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM ml_dataset WHERE dataset_id=?", (dataset_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("spec_json", "label_json", "universe_json", "sampling_json", "summary_json"):
        d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
    return d
