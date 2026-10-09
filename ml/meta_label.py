"""
W40 -- meta-labelling of ATIP's technical signals (docs/ATIP_GAP_ANALYSIS_2026-10.md §4 item 9;
Lopez de Prado, Advances in Financial Machine Learning (AFML), ch. 3, 4, 7 and 10).

The PRIMARY model is the signal engine (research/tech_signals.py): it chooses the side and the levels.
The SECONDARY model built here answers one question per signal, from what was known at its close:
how likely is it to reach its target first?  P(label = 1).  It never changes a side, never places an
order, never activates itself, and scores nothing unless config "meta_label": {"enabled": true}.

1. TRIPLE-BARRIER LABELS (AFML 3.4-3.6), one per stored BULL / BEAR signal with an entry, stop and target
       upper barrier (profit taking)  the signal's target   (4 x ATR from the close: +2 R)
       lower barrier (stop loss)      the signal's stop     (2 x ATR: -1 R)
       vertical barrier               its horizon           (20 sessions; 60 for the event scans)
   walked over the stock's sessions AFTER the signal's close (t0) by research/tech_signals.first_touch --
   the same function that closes signals for the engine's track record, so there is one definition:
       label 1  the target was touched first                            barrier "PT"
       label 0  the stop was touched first                              barrier "SL"
                or neither within the horizon (closed at its close)     barrier "VB"
       t1       the session of the deciding bar (the label's end); sessions = bars from t0 to t1
       a bar touching both levels counts as the stop (a daily bar cannot order them: conservative)
   GAPS: a bar that OPENS beyond a barrier is decided by that barrier like any other touch -- a gap
   down through a long's stop is label 0 on that session, a gap up through its target label 1. The
   engine's R takes the level as the exit (r_multiple: exactly -1 at a stop, +2 at a target); the
   realistic fill at that bar's open is kept beside it (r_fill, gap = 1), and the report shows the
   expectancy both ways. A signal still open (fewer than `horizon` sessions, nothing touched) has no
   label and is never trained on. Signals without a stop or target (no ATR) are skipped (no R).

2. FEATURES -- only what was stored by the signal's close (FEATURES / FEATURE_SOURCES below):
       the signal's own row      side, scan (code in the stored scan catalogue, chart-pattern / event
                                 flags), confluence 0-6, market gate, alignment, weekly agreement,
                                 ATR % of the entry, stop distance in ATR, target distance in R,
                                 horizon, other scans hitting the same stock and side that day
       technical_snapshot(t0)    RSI, ADX, volume ratio, RS rating, and signed for the side (x +1 long /
                                 -1 short, so "on the signal's side" is positive): the daily, weekly and
                                 75-minute ratings, distance from the 50 / 200-DMA, 63-day RS, 1-month
                                 return, Supertrend; Bollinger width; delivery ratio
       market at t0              market_health (score, regime signed for the side, Nifty trend, VIX
                                 level) and market_regime_gate (Nifty vs its 50-DMA, above the 200-DMA,
                                 drawdown, distribution days): the last row on or before t0, at most
                                 MAX_STALE_DAYS old, else missing
       the scan's own record     its win rate among its signals whose label ended on or before t0
                                 (t1 <= t0), shrunk toward the all-scan rate with PRIOR_K pseudo-signals:
                                 (wins_s + K p_all) / (n_s + K), p_all = (wins + 1) / (n + 2); and n_s
   Every query reads rows dated <= t0, so the features are the same on a database truncated at t0
   (tests/test_w40_meta_label.py proves it). Columns missing in over half the rows, or constant, are
   dropped before fitting (unsupervised: no label is read to choose them).

3. SAMPLE WEIGHTS (AFML 4.4-4.5) and CROSS-VALIDATION (AFML 7.4)
       concurrency   c_t = number of the SAME stock's labels alive on bar t (life = bars t0+1 .. t1):
                     labels of one stock share its returns; other stocks share only the market, which
                     the purge below handles
       uniqueness    u_i = mean over i's bars of 1 / c_t    (average uniqueness; 1 = no overlap)
       weights       "uniqueness" (default): w_i = u_i;  "return_attribution": w_i = |sum over i's bars
                     of r_t / c_t| with r_t the stock's log return (AFML 4.5);  "none": 1. Rescaled to
                     mean 1 inside the fit (ml/models.py sample_weight)
       purged k-fold ml/validation.purged_kfold on the [t0, t1] spans: n_splits contiguous blocks of t0
                     dates; a training label overlapping a test fold's [min t0, max t1] is purged (on
                     any stock: market-wide leakage), and labels starting within embargo_sessions after
                     it are embargoed. Every signal is predicted out of fold exactly once.

4. THE SECONDARY MODEL AND ITS REPORT
       model         native_gbm (default), logistic_regression or native_random_forest (numpy, ml/), fitted
                     with the weights; classes "0" / "1"
       out of fold   base rate (= the primary's precision; its recall is 1), precision / recall / F1 at
                     the threshold, log loss vs predicting each fold's training base rate, AUC (rank based,
                     ties averaged)
       expectancy    mean R of the signals the meta-model KEEPS (p >= threshold) vs ALL signals, out of
                     fold; t = Welch t-statistic of kept vs rejected mean R, with each group's size
                     replaced by its effective size sum(u_i) (overlapping labels are not independent).
                     kept - all = (1 - kept share) x (kept - rejected), so t tests the improvement
       threshold     fixed in the config BEFORE training (default 0.5); the report adds a sweep of other
                     thresholds for information only -- choosing one from it would be fitting to the test
       bet size      AFML 10.1:  z = (p - 0.5) / sqrt(p (1 - p)),  m = 2 Phi(z) - 1, kept >= 0 (the side is
                     the primary's; the meta-model only sizes it down or out), discretised m* = round(m /
                     step) x step (AFML 10.3, step 0.1). REPORTED ONLY: no order is ever sized from it

5. ADOPTION GATE (ml_meta_label_run.verdict)
       ADOPTABLE  >= min_oos (200) out-of-fold signals, >= min_group (30) kept AND rejected, kept mean R
                  above all signals' mean R, and t >= min_t (2.0)
       NO_EDGE    otherwise, with the reason
   train() stores the model as its own kind in ml/registry.py (purpose "meta_label", label kind
   "meta_label") as a TRAINED version; it never moves it further. ml/registry.transition refuses
   APPROVED / ACTIVE for a version whose run is not ADOPTABLE (allowed_to_activate). The ADOPTED model is
   an ACTIVE meta-label version with an ADOPTABLE run; only it is used to score.

6. WIRING
       python -m ml.meta_label train [--force] [--no-register] | score [--date D] | report | labels
       weekly training (Saturday 09:30) and nightly scoring (20:45, after the 20:30 signals and the 20:35
       post-earnings-drift scan) are scheduled by pipeline/scheduler.py through schedule_jobs(); both are
       no-ops unless "meta_label": {"enabled": true}. With an adopted model and scoring enabled, today's
       signals on /signals show the meta-probability and the suggested size (attach_scores, called by
       GET /api/signals/technical); GET /api/research/meta-label/report returns report().

Config (atip_data/config.json, all optional): "meta_label": {"enabled": false, "model_type": "native_gbm",
"params": null, "threshold": 0.5, "n_splits": 5, "embargo_sessions": 5, "weighting": "uniqueness",
"min_oos": 200, "min_group": 30, "min_t": 2.0, "size_step": 0.1, "model_id": null}

Tables (registered in db/schema_w39b.py): ml_meta_label (one label per signal), ml_meta_label_run (every
training run: the report and its verdict), ml_meta_label_score (the nightly scores of the day's signals).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import math
import uuid
from datetime import date, datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

FEATURE_SET, FEATURE_VERSION = "meta_label", "1"
LABEL_KIND = PURPOSE = "meta_label"
CLASSES = ["0", "1"]
MAX_STALE_DAYS = 7             # a market_health / market_regime_gate row older than this at t0 is "missing"
PRIOR_K = 10                   # pseudo-signals shrinking a scan's own record toward the all-scan rate
MIN_LABELS = 50                # below this many labelled signals nothing is trained

DEFAULTS = {"enabled": False, "model_type": "native_gbm", "params": None, "threshold": 0.5, "n_splits": 5,
            "embargo_sessions": 5, "weighting": "uniqueness", "min_oos": 200, "min_group": 30, "min_t": 2.0,
            "size_step": 0.1, "model_id": None}
MODEL_PARAMS = {
    "native_gbm": {"n_estimators": 120, "learning_rate": 0.05, "max_depth": 3, "min_samples_leaf": 20, "l2": 1.0,
                   "subsample": 0.8, "colsample": 0.8, "max_bins": 32, "seed": 7},
    "logistic_regression": {"l2": 1.0, "learning_rate": 0.1, "iterations": 500},
    "native_random_forest": {"n_estimators": 150, "max_depth": 5, "min_samples_leaf": 20, "max_features": "sqrt",
                             "bootstrap": True, "max_bins": 32, "seed": 7},
}
WEIGHTINGS = ("uniqueness", "return_attribution", "none")

DDL = {
    "ml_meta_label": (
        """CREATE TABLE IF NOT EXISTS ml_meta_label (
            signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, t0 DATE NOT NULL, t1 DATE NOT NULL, scan TEXT,
            direction TEXT, barrier TEXT NOT NULL, label INTEGER NOT NULL, sessions INTEGER, exit_price REAL,
            r_multiple REAL, r_fill REAL, gap INTEGER, return_pct REAL, engine_status TEXT, computed_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ml_meta_label_t0 ON ml_meta_label(t0)",
    ),
    "ml_meta_label_run": (
        """CREATE TABLE IF NOT EXISTS ml_meta_label_run (
            run_id TEXT PRIMARY KEY, model_id TEXT, version TEXT, verdict TEXT NOT NULL, reason TEXT,
            signals INTEGER, oos_signals INTEGER, kept_signals INTEGER, expectancy_all REAL, expectancy_kept REAL,
            t_stat REAL, threshold REAL, dataset_id TEXT, config_hash TEXT, report_json TEXT, created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ml_meta_label_run_model ON ml_meta_label_run(model_id, version)",
    ),
    "ml_meta_label_score": (
        """CREATE TABLE IF NOT EXISTS ml_meta_label_score (
            signal_id TEXT PRIMARY KEY, signal_date DATE NOT NULL, symbol TEXT NOT NULL, scan TEXT, direction TEXT,
            model_id TEXT NOT NULL, version TEXT NOT NULL, probability REAL, bet_size REAL, kept INTEGER,
            threshold REAL, features_json TEXT, scored_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ml_meta_label_score_date ON ml_meta_label_score(signal_date)",
    ),
}

# name -> where it comes from, and why it is known at the signal's close (t0)
_SIG = "technical_signal row, written at 20:30 on t0 from bars up to t0's close"
_SNAP = "technical_snapshot of (symbol, t0), written at 20:30 on t0 from bars up to t0's close"
_MH = f"market_health, last row on or before t0 (post-market run of that session; <= {MAX_STALE_DAYS} days old)"
_RG = f"market_regime_gate, last row on or before t0 (computed at 20:30 on t0; <= {MAX_STALE_DAYS} days old)"
FEATURE_SOURCES = {
    "side": f"+1 BULL / -1 BEAR: {_SIG}",
    "scan_code": f"position of the scan in the scan catalogue stored with the model: {_SIG}",
    "scan_pattern": f"1 for a chart-pattern scan (research/technicals.PATTERN_SCANS): {_SIG}",
    "scan_event": f"1 for an event scan (post-earnings drift): {_SIG}",
    "confluence": f"agreeing evidence 0-6: {_SIG}",
    "gate": f"market gate at birth OPEN 1 / CAUTION 0 / CLOSED -1: {_SIG}",
    "alignment": f"WITH 1 / MIXED 0 / AGAINST -1 the market: {_SIG}",
    "weekly_agrees": f"weekly rating on the signal's side 1 / 0: {_SIG}",
    "atr_pct": f"ATR / entry x 100: {_SIG}",
    "stop_atr": f"|entry - stop| / ATR: {_SIG}",
    "target_r": f"|target - entry| / |entry - stop|: {_SIG}",
    "horizon": f"vertical barrier in sessions: {_SIG}",
    "same_day_hits": f"signals on the same stock, date and side (all written at t0's close): {_SIG}",
    "rsi_14": _SNAP, "adx_14": _SNAP, "vol_ratio": _SNAP, "rs_rating": _SNAP, "bb_width_pct": _SNAP,
    "delivery_ratio": _SNAP,
    "rating_signed": f"tech_rating x side: {_SNAP}",
    "weekly_rating_signed": f"tech_rating_w (completed weeks) x side: {_SNAP}",
    "rating_75_signed": f"tech_rating_75 (the day's completed 75-minute bars) x side, where present: {_SNAP}",
    "pct_from_sma50_signed": f"pct_from_sma50 x side: {_SNAP}",
    "pct_from_sma200_signed": f"pct_from_sma200 x side: {_SNAP}",
    "rs_63_signed": f"rs_63_pct x side: {_SNAP}",
    "return_1m_signed": f"return_1m_pct x side: {_SNAP}",
    "supertrend_agrees": f"supertrend_dir x side: {_SNAP}",
    "mh_score": _MH,
    "regime_signed": f"regime STRONG_BULL 2 .. HIGH_RISK -2, x side: {_MH}",
    "nifty_trend": _MH,
    "vix": f"vix_level: {_MH}",
    "nifty_vs_sma50_pct": f"(nifty_close / sma50 - 1) x 100: {_RG}",
    "nifty_above_200dma": _RG,
    "nifty_drawdown_pct": f"drawdown_pct: {_RG}",
    "distribution_days": f"dd_count: {_RG}",
    "scan_prior_win_rate": "ml_meta_label of the same scan with t1 <= t0 (labels decided by t0's close), shrunk",
    "scan_prior_n": "count of the same scan's labels with t1 <= t0",
}
FEATURES = list(FEATURE_SOURCES)

_GATE = {"OPEN": 1.0, "CAUTION": 0.0, "CLOSED": -1.0}
_ALIGN = {"WITH": 1.0, "MIXED": 0.0, "AGAINST": -1.0}
_REGIME = {"STRONG_BULL": 2.0, "BULL": 1.0, "NEUTRAL": 0.0, "BEAR": -1.0, "HIGH_RISK": -2.0}
_SIG_COLS = ("signal_id", "symbol", "date", "scan", "name", "direction", "entry", "stop", "target", "atr", "horizon",
             "confluence", "market_gate", "alignment", "weekly_agrees", "status", "outcome_date")
_SNAP_COLS = ("tech_rating", "rsi_14", "adx_14", "vol_ratio", "rs_rating", "rs_63_pct", "pct_from_sma50",
              "pct_from_sma200", "bb_width_pct", "return_1m_pct", "supertrend_dir", "tech_rating_w", "tech_rating_75",
              "delivery_ratio")


# ── configuration and tables ──────────────────────────────────────────────

def settings() -> dict:
    """The "meta_label" section of atip_data/config.json (ml.config.CONFIG_PATH) over DEFAULTS, validated."""
    from ml import config as MC
    try:
        raw = json.loads(Path(MC.CONFIG_PATH).read_text(encoding="utf-8")).get("meta_label") or {}
    except Exception:
        raw = {}
    return check_config({k: v for k, v in raw.items() if k in DEFAULTS} if isinstance(raw, dict) else {})


def check_config(cfg: dict | None) -> dict:
    out = {**DEFAULTS, **(cfg or {})}
    out["enabled"] = out.get("enabled") is True
    if out["model_type"] not in MODEL_PARAMS:
        raise ValueError(f"meta_label.model_type must be one of {sorted(MODEL_PARAMS)}")
    if out["weighting"] not in WEIGHTINGS:
        raise ValueError(f"meta_label.weighting must be one of {WEIGHTINGS}")
    if not 0 < float(out["threshold"]) < 1:
        raise ValueError("meta_label.threshold must be between 0 and 1")
    if not 2 <= int(out["n_splits"]) <= 20:
        raise ValueError("meta_label.n_splits must be 2..20")
    if not 0 < float(out["size_step"]) <= 1:
        raise ValueError("meta_label.size_step must be in (0, 1]")
    out["params"] = {**MODEL_PARAMS[out["model_type"]], **(out.get("params") or {})}
    for k in ("n_splits", "embargo_sessions", "min_oos", "min_group"):
        out[k] = int(out[k])
    for k in ("threshold", "min_t", "size_step"):
        out[k] = float(out[k])
    return out


def ensure_tables(conn):
    from research import tech_signals as TS
    TS.ensure_tables(conn)
    for ddls in DDL.values():
        for d in ddls:
            conn.execute(d)


def _ds(v) -> str | None:
    return None if v is None else str(v)[:10]


def _f(v) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else math.nan
    except (TypeError, ValueError):
        return math.nan


def scan_catalogue() -> list:
    """Every scan the engine can emit (research/technicals.SCANS + the event scans), sorted: scan_code's index."""
    from research import technicals as T, tech_signals as TS
    return sorted(set(T.SCANS) | set(TS.EVENT_SCANS))


# ── 1. triple-barrier labels ──────────────────────────────────────────────

def triple_barrier(direction, entry, stop, target, horizon, after) -> dict | None:
    """The label of one signal. after: [(date, open, high, low, close), ...] -- the stock's sessions after the
    signal's close, oldest first. None while the signal is still open."""
    from research import tech_signals as TS
    h = int(horizon or TS.HORIZON)
    after = list(after[:h])
    status, i, price = TS.first_touch(direction, stop, target, h, [(b[0], b[2], b[3], b[4]) for b in after])
    if status is None:
        return None
    sign = 1 if direction == "BULL" else -1
    fill, gap, o = price, 0, after[i][1]
    if status != "EXPIRED" and o is not None:
        lvl = stop if status == "STOPPED" else target
        beyond = sign * (o - lvl) <= 0 if status == "STOPPED" else sign * (o - lvl) >= 0
        if beyond:                              # opened through the barrier: the real fill is the open
            fill, gap = float(o), 1
    r, rf = TS.r_multiple(direction, entry, stop, price), TS.r_multiple(direction, entry, stop, fill)
    return {"label": int(status == "TARGET"), "barrier": {"TARGET": "PT", "STOPPED": "SL", "EXPIRED": "VB"}[status],
            "status": status, "t1": _ds(after[i][0]), "sessions": i + 1, "exit_price": float(price),
            "r_multiple": None if r is None else round(r, 6), "r_fill": None if rf is None else round(rf, 6),
            "gap": gap, "return_pct": round(sign * (price / entry - 1) * 100, 6)}


def _signals(conn, where: str = "", args=()) -> list:
    ensure_tables(conn)
    cur = conn.execute(f"SELECT {', '.join(_SIG_COLS)} FROM technical_signal WHERE direction IN ('BULL','BEAR') AND "
                       f"entry>0 AND stop IS NOT NULL AND target IS NOT NULL AND stop<>entry{where} "
                       f"ORDER BY date, symbol, scan", tuple(args))
    out = []
    for r in cur.fetchall():
        s = dict(zip(_SIG_COLS, r))
        s["date"] = _ds(s["date"])
        out.append(s)
    return out


def _bars(conn, first: dict) -> dict:
    """{symbol: [(date, open, high, low, close), ...]} from each symbol's earliest needed date on, oldest first."""
    out = {}
    for sym, d0 in first.items():
        rows = conn.execute("SELECT date, open, high, low, close FROM prices_daily WHERE symbol=? AND date>=? "
                            "ORDER BY date", (sym, d0)).fetchall()
        out[sym] = [(_ds(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
    return out


def build_labels(conn, store: bool = True, only_missing: bool = False) -> dict:
    """Triple-barrier labels of every stored signal that has closed. Returns {"labels": {signal_id: label},
    "signals": [signal rows labelled], "spans": {signal_id: (symbol, first bar index, last bar index)},
    "bars": {symbol: bars}, "open": n, "engine_mismatch": n}. store: upsert into ml_meta_label.
    only_missing: label only signals without a stored label (the nightly refresh; training recomputes all)."""
    sigs = _signals(conn)
    if only_missing:
        have = {r[0] for r in conn.execute("SELECT signal_id FROM ml_meta_label")}
        sigs = [s for s in sigs if s["signal_id"] not in have]
    first = {}
    for s in sigs:
        first[s["symbol"]] = min(first.get(s["symbol"], s["date"]), s["date"])
    bars = _bars(conn, first)
    labels, spans, done, n_open, mismatch = {}, {}, [], 0, 0
    now = datetime.now()
    for s in sigs:
        b = bars.get(s["symbol"]) or []
        dates = [x[0] for x in b]
        i0 = bisect.bisect_right(dates, s["date"])            # the first session AFTER the signal's close
        lab = triple_barrier(s["direction"], float(s["entry"]), float(s["stop"]), float(s["target"]), s["horizon"],
                             b[i0:])
        if lab is None:
            n_open += 1
            continue
        if s["status"] in ("TARGET", "STOPPED", "EXPIRED") and (
                s["status"] != lab["status"] or _ds(s["outcome_date"]) != lab["t1"]):
            mismatch += 1                                   # prices changed after the engine closed it
        labels[s["signal_id"]] = lab
        spans[s["signal_id"]] = (s["symbol"], i0, i0 + lab["sessions"] - 1)
        done.append(s)
        if store:
            conn.execute(
                "INSERT INTO ml_meta_label (signal_id,symbol,t0,t1,scan,direction,barrier,label,sessions,exit_price,"
                "r_multiple,r_fill,gap,return_pct,engine_status,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(signal_id) DO UPDATE SET t1=excluded.t1, barrier=excluded.barrier, label=excluded.label, "
                "sessions=excluded.sessions, exit_price=excluded.exit_price, r_multiple=excluded.r_multiple, "
                "r_fill=excluded.r_fill, gap=excluded.gap, return_pct=excluded.return_pct, "
                "engine_status=excluded.engine_status, computed_at=excluded.computed_at",
                (s["signal_id"], s["symbol"], s["date"], lab["t1"], s["scan"], s["direction"], lab["barrier"],
                 lab["label"], lab["sessions"], lab["exit_price"], lab["r_multiple"], lab["r_fill"], lab["gap"],
                 lab["return_pct"], s["status"], now))
    if store:
        conn.commit()
    return {"labels": labels, "signals": done, "spans": spans, "bars": bars, "open": n_open,
            "engine_mismatch": mismatch}


def stored_labels(conn) -> list:
    """[(scan, t1, label)] of every stored label: the scan records the features read (t1 <= t0)."""
    ensure_tables(conn)
    return [(r[0], _ds(r[1]), int(r[2])) for r in conn.execute("SELECT scan, t1, label FROM ml_meta_label")]


# ── 2. features at t0 ─────────────────────────────────────────────────────

def _asof(conn, sql: str, until: str) -> tuple:
    """(dates, rows) of a date-keyed table up to `until`, oldest first; ([], []) when the table is missing."""
    try:
        rows = conn.execute(sql, (until,)).fetchall()
    except Exception:
        return [], []
    rows = [(_ds(r[0]),) + tuple(r[1:]) for r in rows]
    return [r[0] for r in rows], rows


def _last_on_or_before(dates, rows, d):
    i = bisect.bisect_right(dates, d) - 1
    if i < 0:
        return None
    if (date.fromisoformat(d) - date.fromisoformat(dates[i])).days > MAX_STALE_DAYS:
        return None
    return rows[i]


def _scan_record(labels) -> tuple:
    """Per scan and overall: t1 dates sorted and the running win count, for "labels decided by t0"."""
    by = {}
    for scan, t1, y in sorted(labels, key=lambda x: x[1]):
        by.setdefault(scan, []).append((t1, y))
        by.setdefault(None, []).append((t1, y))
    out = {}
    for k, xs in by.items():
        out[k] = ([t for t, _ in xs], np.cumsum([y for _, y in xs]).tolist())
    return out


def _decided(rec, key, d) -> tuple:
    if key not in rec:
        return 0, 0
    ts, wins = rec[key]
    n = bisect.bisect_right(ts, d)
    return n, (wins[n - 1] if n else 0)


def features(conn, sigs: list, labels=None, catalogue: list | None = None) -> np.ndarray:
    """The FEATURES matrix (rows = sigs, NaN = missing) from data stored by each signal's close (t0).
    labels: [(scan, t1, label)] for the scan records (default: the stored ml_meta_label rows)."""
    X = np.full((len(sigs), len(FEATURES)), np.nan)
    if not sigs:
        return X
    col = {f: j for j, f in enumerate(FEATURES)}
    cat = {s: i for i, s in enumerate(catalogue or scan_catalogue())}
    from research import technicals as T, tech_signals as TS
    t0s = sorted({s["date"] for s in sigs})
    snaps = {}
    for i in range(0, len(t0s), 400):
        part = t0s[i:i + 400]
        for r in conn.execute(f"SELECT symbol, date, {', '.join(_SNAP_COLS)} FROM technical_snapshot WHERE date IN "
                              f"({','.join('?' * len(part))})", part):
            snaps[(r[0], _ds(r[1]))] = dict(zip(_SNAP_COLS, r[2:]))
    hits = {}
    for i in range(0, len(t0s), 400):
        part = t0s[i:i + 400]
        for sym, d, direction, n in conn.execute(
                f"SELECT symbol, date, direction, COUNT(*) FROM technical_signal WHERE date IN "
                f"({','.join('?' * len(part))}) GROUP BY symbol, date, direction", part):
            hits[(sym, _ds(d), direction)] = n
    last = t0s[-1]
    mh_d, mh = _asof(conn, "SELECT date, mh_score, regime, nifty_trend, vix_level FROM market_health WHERE date<=? "
                           "ORDER BY date", last)
    rg_d, rg = _asof(conn, "SELECT date, nifty_close, sma50, above_200dma, drawdown_pct, dd_count FROM "
                           "market_regime_gate WHERE date<=? ORDER BY date", last)
    rec = _scan_record(stored_labels(conn) if labels is None else labels)
    for i, s in enumerate(sigs):
        side = 1.0 if s["direction"] == "BULL" else -1.0
        entry, stop, target, atr = (_f(s[k]) for k in ("entry", "stop", "target", "atr"))
        risk = abs(entry - stop)
        v = {"side": side, "scan_code": float(cat[s["scan"]]) if s["scan"] in cat else math.nan,
             "scan_pattern": float(s["scan"] in T.PATTERN_SCANS), "scan_event": float(s["scan"] in TS.EVENT_SCANS),
             "confluence": _f(s["confluence"]), "gate": _GATE.get(s["market_gate"], math.nan),
             "alignment": _ALIGN.get(s["alignment"], math.nan), "weekly_agrees": _f(s["weekly_agrees"]),
             "atr_pct": atr / entry * 100 if atr > 0 and entry > 0 else math.nan,
             "stop_atr": risk / atr if atr > 0 else math.nan,
             "target_r": abs(target - entry) / risk if risk > 0 else math.nan,
             "horizon": _f(s["horizon"] or TS.HORIZON),
             "same_day_hits": float(hits.get((s["symbol"], s["date"], s["direction"]), 1))}
        sn = snaps.get((s["symbol"], s["date"]))
        if sn:
            for k in ("rsi_14", "adx_14", "vol_ratio", "rs_rating", "bb_width_pct", "delivery_ratio"):
                v[k] = _f(sn[k])
            for k, src in (("rating_signed", "tech_rating"), ("weekly_rating_signed", "tech_rating_w"),
                           ("rating_75_signed", "tech_rating_75"), ("pct_from_sma50_signed", "pct_from_sma50"),
                           ("pct_from_sma200_signed", "pct_from_sma200"), ("rs_63_signed", "rs_63_pct"),
                           ("return_1m_signed", "return_1m_pct"), ("supertrend_agrees", "supertrend_dir")):
                v[k] = _f(sn[src]) * side
        m = _last_on_or_before(mh_d, mh, s["date"])
        if m:
            v.update(mh_score=_f(m[1]), regime_signed=_REGIME.get(str(m[2] or "").upper(), math.nan) * side,
                     nifty_trend=_f(m[3]), vix=_f(m[4]))
        g = _last_on_or_before(rg_d, rg, s["date"])
        if g:
            nc, sma50 = _f(g[1]), _f(g[2])
            v.update(nifty_vs_sma50_pct=(nc / sma50 - 1) * 100 if sma50 > 0 else math.nan,
                     nifty_above_200dma=_f(g[3]), nifty_drawdown_pct=_f(g[4]), distribution_days=_f(g[5]))
        n_all, w_all = _decided(rec, None, s["date"])
        n_s, w_s = _decided(rec, s["scan"], s["date"])
        p_all = (w_all + 1) / (n_all + 2)
        v["scan_prior_win_rate"] = (w_s + PRIOR_K * p_all) / (n_s + PRIOR_K)
        v["scan_prior_n"] = float(n_s)
        for k, x in v.items():
            X[i, col[k]] = x
    return X


# ── 3. sample weights ─────────────────────────────────────────────────────

def _concurrency(spans) -> tuple:
    lo = min(a for a, _ in spans)
    hi = max(b for _, b in spans)
    c = np.zeros(hi - lo + 2)
    for a, b in spans:
        c[a - lo] += 1
        c[b - lo + 1] -= 1
    return np.cumsum(c)[:-1], lo


def average_uniqueness(spans) -> np.ndarray:
    """AFML 4.4 on one price path. spans: [(first bar, last bar)] inclusive indices of each label's life.
    c_t = labels alive on bar t; u_i = mean over i's bars of 1 / c_t."""
    if not len(spans):
        return np.zeros(0)
    c, lo = _concurrency(spans)
    cs = np.concatenate([[0.0], np.cumsum(np.where(c > 0, 1.0 / np.maximum(c, 1), 0.0))])
    return np.array([(cs[b - lo + 1] - cs[a - lo]) / (b - a + 1) for a, b in spans])


def uniqueness_weights(groups, spans) -> np.ndarray:
    """Average uniqueness per label, concurrency counted within its group (the stock: labels sharing its returns)."""
    w = np.zeros(len(spans))
    by = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    for idx in by.values():
        w[idx] = average_uniqueness([spans[i] for i in idx])
    return w


def return_attribution_weights(groups, spans, log_returns: dict) -> np.ndarray:
    """AFML 4.5: w_i = |sum over i's bars of r_t / c_t| (r_t: the group's log return on bar t, c_t its
    concurrency), rescaled to sum to the number of labels. log_returns: {group: array indexed like the spans}."""
    w = np.zeros(len(spans))
    by = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    for g, idx in by.items():
        c, lo = _concurrency([spans[i] for i in idx])
        r = np.asarray(log_returns[g], dtype=float)
        for i in idx:
            a, b = spans[i]
            w[i] = abs(float(np.nansum(r[a:b + 1] / c[a - lo:b - lo + 1])))
    s = w.sum()
    return w * (len(w) / s) if s > 0 else np.ones(len(w))


# ── 4. the model, its out-of-fold report, bet sizing ───────────────────────

def bet_size(p, step: float = 0.1) -> np.ndarray:
    """AFML 10.1 + 10.3: z = (p - 0.5) / sqrt(p (1 - p)), m = 2 Phi(z) - 1, floored at 0, rounded to `step`."""
    p = np.clip(np.atleast_1d(np.asarray(p, dtype=float)), 1e-9, 1 - 1e-9)
    z = (p - 0.5) / np.sqrt(p * (1 - p))
    m = np.array([math.erf(x / math.sqrt(2)) for x in z])        # 2 Phi(z) - 1 = erf(z / sqrt 2)
    m = np.maximum(m, 0.0)
    return np.round(np.round(m / step) * step, 10)


def _make(cfg, cols):
    from ml.models import make_model
    return make_model(cfg["model_type"], "classification", cfg["params"], cols, CLASSES)


def _p1(model, X) -> np.ndarray:
    P = model.predict_proba(X)
    return P[:, model.classes.index("1")] if "1" in model.classes else np.zeros(len(X))


def _fit(cfg, cols, X, y, w):
    m = _make(cfg, cols)
    m.fit(X, [str(int(v)) for v in y], sample_weight=w)
    return m


def _auc(y, p) -> float | None:
    y, p = np.asarray(y), np.asarray(p, dtype=float)
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    if not n1 or not n0:
        return None
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p))
    sp = p[order]
    i = 0
    while i < len(p):                                    # average ranks over ties
        j = i
        while j + 1 < len(p) and sp[j + 1] == sp[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _logloss(y, p) -> float:
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1 - 1e-12)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _group(r, y, u) -> dict:
    r = np.asarray(r, dtype=float)
    return {"n": int(len(r)), "n_effective": round(float(np.sum(u)), 2),
            "mean_r": None if not len(r) else round(float(np.mean(r)), 4),
            "win_rate": None if not len(r) else round(float(np.mean(y)), 4)}


def _welch(a, ua, b, ub) -> float | None:
    """Welch t of mean(a) - mean(b), each group's n replaced by its effective size sum(u)."""
    if len(a) < 2 or len(b) < 2:
        return None
    na, nb = max(float(np.sum(ua)), 1.0), max(float(np.sum(ub)), 1.0)
    se = math.sqrt(np.var(a, ddof=1) / na + np.var(b, ddof=1) / nb)
    return None if se == 0 else float((np.mean(a) - np.mean(b)) / se)


def evaluate(y, p, r, u, cfg, fold_base=None, r_fill=None) -> dict:
    """The out-of-fold report: classification vs the primary's base rate, expectancy of kept vs all signals,
    bet sizes, a threshold sweep, and the verdict (module docstring, section 5)."""
    y, p, r, u = (np.asarray(v, dtype=float) for v in (y, p, r, u))
    thr = cfg["threshold"]
    kept = p >= thr
    base = float(y.mean())
    tp = float((y[kept] == 1).sum())
    prec = tp / kept.sum() if kept.sum() else None
    rec_ = tp / y.sum() if y.sum() else None
    f1 = 2 * prec * rec_ / (prec + rec_) if prec and rec_ else None
    fb = np.full(len(y), base) if fold_base is None else np.asarray(fold_base, dtype=float)
    auc = _auc(y, p)
    cls = {"base_rate": round(base, 4), "log_loss": round(_logloss(y, p), 4),
           "baseline_log_loss": round(_logloss(y, fb), 4), "auc": None if auc is None else round(auc, 4),
           "precision": None if prec is None else round(prec, 4), "recall": None if rec_ is None else round(rec_, 4),
           "f1": None if f1 is None else round(f1, 4), "accuracy": round(float(np.mean((p >= thr) == (y == 1))), 4),
           "primary": {"precision": round(base, 4), "recall": 1.0,
                       "f1": round(2 * base / (1 + base), 4) if base else None,
                       "note": "the primary takes every signal: precision = the base rate, recall = 1"}}
    t = _welch(r[kept], u[kept], r[~kept], u[~kept])
    allg, kg, rg = _group(r, y, u), _group(r[kept], y[kept], u[kept]), _group(r[~kept], y[~kept], u[~kept])
    diff = None if kg["mean_r"] is None else round(kg["mean_r"] - allg["mean_r"], 4)
    exp = {"all": allg, "kept": kg, "rejected": rg, "kept_minus_all": diff,
           "t_stat": None if t is None else round(t, 3), "kept_share": round(float(kept.mean()), 4),
           "note": "mean R out of fold; t = Welch t of kept vs rejected with effective sizes sum(uniqueness)"}
    if r_fill is not None:
        rf = np.asarray(r_fill, dtype=float)
        exp["gap_adjusted"] = {"all": round(float(np.nanmean(rf)), 4),
                               "kept": round(float(np.nanmean(rf[kept])), 4) if kept.any() else None,
                               "note": "R with a gap through a barrier filled at that session's open"}
    sz = bet_size(p, cfg["size_step"])
    dist = {f"{v:.1f}": int((np.isclose(sz, v)).sum()) for v in sorted(set(np.round(sz, 1)))}
    sizing = {"formula": "z = (p - 0.5) / sqrt(p (1 - p)); size = max(0, 2 Phi(z) - 1), rounded to the step",
              "step": cfg["size_step"], "distribution": dist, "mean_size": round(float(sz.mean()), 4),
              "size_weighted_mean_r": round(float((sz * r).sum() / sz.sum()), 4) if sz.sum() > 0 else None,
              "note": "reported only: no order is ever sized from it"}
    sweep = []
    for th in np.round(np.arange(0.30, 0.71, 0.05), 2):
        k = p >= th
        sweep.append({"threshold": float(th), "kept": int(k.sum()),
                      "mean_r_kept": round(float(r[k].mean()), 4) if k.any() else None,
                      "precision": round(float(y[k].mean()), 4) if k.any() else None})
    n = len(y)
    if n < cfg["min_oos"]:
        verdict, why = "NO_EDGE", f"{n} out-of-fold signals; the rule needs {cfg['min_oos']}"
    elif kg["n"] < cfg["min_group"] or rg["n"] < cfg["min_group"]:
        verdict, why = "NO_EDGE", (f"the model keeps {kg['n']} and rejects {rg['n']} signals at p >= {thr}; each side "
                                   f"needs {cfg['min_group']}")
    elif diff is None or diff <= 0:
        verdict, why = "NO_EDGE", f"kept signals average {kg['mean_r']:+.3f} R vs {allg['mean_r']:+.3f} R for all"
    elif t is None or t < cfg["min_t"]:
        verdict, why = "NO_EDGE", (f"kept {kg['mean_r']:+.3f} R vs all {allg['mean_r']:+.3f} R, but t = "
                                   f"{'n/a' if t is None else round(t, 2)} < {cfg['min_t']}: within noise")
    else:
        verdict, why = "ADOPTABLE", (f"kept {kg['n']} of {n} signals out of fold: {kg['mean_r']:+.3f} R vs "
                                     f"{allg['mean_r']:+.3f} R for all ({diff:+.3f} R), t = {t:.2f}")
    return {"verdict": verdict, "reason": why, "threshold": thr, "oos_signals": n, "classification": cls,
            "expectancy": exp, "sizing": sizing, "threshold_sweep": sweep,
            "rule": (f"ADOPTABLE when >= {cfg['min_oos']} out-of-fold signals, >= {cfg['min_group']} kept and "
                     f"rejected at p >= {thr}, kept mean R above all signals' mean R, and t >= {cfg['min_t']} "
                     "(Welch, kept vs rejected, effective sizes from uniqueness); otherwise NO_EDGE"),
            "note": "the threshold is fixed in the config before training; the sweep is information, not a choice"}


def dataset(conn, cfg: dict) -> dict:
    """Labels, features at t0, spans and weights of every closed signal, ordered by t0."""
    lab = build_labels(conn, store=True)
    sigs = lab["signals"]
    labels = lab["labels"]
    rec = [(s["scan"], labels[s["signal_id"]]["t1"], labels[s["signal_id"]]["label"]) for s in sigs]
    cat = scan_catalogue()
    X = features(conn, sigs, rec, cat)
    y = np.array([labels[s["signal_id"]]["label"] for s in sigs], dtype=float)
    r = np.array([labels[s["signal_id"]]["r_multiple"] for s in sigs], dtype=float)
    rf = np.array([labels[s["signal_id"]]["r_fill"] for s in sigs], dtype=float)
    groups = [s["symbol"] for s in sigs]
    spans = [lab["spans"][s["signal_id"]][1:] for s in sigs]
    u = uniqueness_weights(groups, spans) if sigs else np.zeros(0)
    if cfg["weighting"] == "return_attribution" and sigs:
        lr = {}
        for g in set(groups):
            c = np.array([_f(b[4]) for b in lab["bars"][g]])
            lr[g] = np.r_[0.0, np.log(c[1:] / c[:-1])]
        w = return_attribution_weights(groups, spans, lr)
    elif cfg["weighting"] == "none":
        w = np.ones(len(sigs))
    else:
        w = u.copy()
    t0 = [s["date"] for s in sigs]
    t1 = [labels[s["signal_id"]]["t1"] for s in sigs]
    cal = sorted({b[0] for bs in lab["bars"].values() for b in bs})
    return {"signals": sigs, "labels": labels, "X": X, "y": y, "r": r, "r_fill": rf, "u": u, "w": w, "t0": t0,
            "t1": t1, "calendar": cal, "catalogue": cat, "open": lab["open"], "engine_mismatch": lab["engine_mismatch"]}


def _columns(X) -> tuple:
    """Columns kept: missing in at most half the rows and not constant (no label is read)."""
    keep, dropped = [], []
    for j, f in enumerate(FEATURES):
        c = X[:, j]
        ok = ~np.isnan(c)
        if ok.mean() <= 0.5 or np.nanstd(c[ok]) < 1e-12:
            dropped.append(f)
        else:
            keep.append(j)
    return keep, dropped


def cross_validate(ds: dict, cfg: dict, keep: list) -> dict:
    """Purged k-fold with embargo (ml/validation.purged_kfold): out-of-fold P(label = 1) for every signal."""
    from ml.validation import purge_report, purged_kfold
    cols = [FEATURES[j] for j in keep]
    X, y, w = ds["X"][:, keep], ds["y"], ds["w"]
    folds = purged_kfold(ds["t0"], ds["t1"], cfg["n_splits"], cfg["embargo_sessions"], ds["calendar"])
    p, base = np.full(len(y), np.nan), np.full(len(y), np.nan)
    rep = purge_report(ds["t0"], ds["t1"], folds)
    for (tr, te), fr in zip(folds, rep):
        b = float(y[tr].mean()) if len(tr) else float(y.mean())
        base[te] = b
        if len(tr) < 30 or len(set(y[tr].tolist())) < 2:
            p[te] = b
            fr["model"] = "constant (too few training rows or one class)"
        else:
            p[te] = _p1(_fit(cfg, cols, X[tr], y[tr], w[tr]), X[te])
            fr["model"] = cfg["model_type"]
        fr["train_base_rate"] = round(b, 4)
        fr["test_base_rate"] = round(float(y[te].mean()), 4)
        fr["kept"] = int((p[te] >= cfg["threshold"]).sum())
        fr["mean_r_all"] = round(float(ds["r"][te].mean()), 4)
        k = p[te] >= cfg["threshold"]
        fr["mean_r_kept"] = round(float(ds["r"][te][k].mean()), 4) if k.any() else None
    return {"p": p, "fold_base": base, "folds": rep}


# ── registry, storage ─────────────────────────────────────────────────────

def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def feature_set_row(conn) -> dict:
    """ml_feature_set meta_label@FEATURE_VERSION (FEATURES and their sources), written once."""
    versions = {f: FEATURE_VERSION for f in FEATURES}
    h = _hash({"features": FEATURES, "versions": versions})
    ex = conn.execute("SELECT content_hash FROM ml_feature_set WHERE name=? AND version=?",
                      (FEATURE_SET, FEATURE_VERSION)).fetchone()
    if ex and ex[0] != h:
        raise ValueError(f"feature set {FEATURE_SET}@{FEATURE_VERSION} is stored with different features: bump "
                         "ml/meta_label.FEATURE_VERSION")
    if not ex:
        conn.execute("INSERT INTO ml_feature_set (name,version,features_json,feature_versions_json,description,"
                     "content_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                     (FEATURE_SET, FEATURE_VERSION, json.dumps(FEATURES), json.dumps(versions),
                      "W40 meta-label features at the signal's close (ml/meta_label.py FEATURE_SOURCES)", h,
                      datetime.now()))
        conn.commit()
    return {"name": FEATURE_SET, "version": FEATURE_VERSION, "features": FEATURES, "content_hash": h}


def _dataset_row(conn, ds, cfg, did, spec_hash) -> dict:
    from ml.config import model_root
    p = Path(model_root()) / "datasets"
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"meta_label_{did.split('@')[1]}.npz"
    np.savez_compressed(f, X=ds["X"].astype(np.float32), y=ds["y"], r=ds["r"], w=ds["w"], u=ds["u"],
                        t0=np.array(ds["t0"]), t1=np.array(ds["t1"]),
                        signal_ids=np.array([s["signal_id"] for s in ds["signals"]]), columns=np.array(FEATURES))
    snap = hashlib.sha256(f.read_bytes()).hexdigest()
    spec = {"source": "technical_signal", "label": {"kind": LABEL_KIND, "barriers": "target / stop / horizon",
                                                    "rule": "research/tech_signals.first_touch"},
            "feature_set": f"{FEATURE_SET}@{FEATURE_VERSION}", "weighting": cfg["weighting"],
            "signals": len(ds["y"]), "start": ds["t0"][0], "end": ds["t1"][-1] if ds["t1"] else None}
    now = datetime.now()
    conn.execute("INSERT INTO ml_dataset (dataset_id,name,version,spec_json,spec_hash,feature_set,label_json,start_date,"
                 "end_date,universe_json,frequency,sampling_json,source,status,summary_json,snapshot_path,snapshot_hash,"
                 "created_at,built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dataset_id) DO UPDATE "
                 "SET snapshot_path=excluded.snapshot_path, snapshot_hash=excluded.snapshot_hash, "
                 "built_at=excluded.built_at",
                 (did, "meta_label", did.split("@")[1], json.dumps(spec, default=str), spec_hash,
                  f"{FEATURE_SET}@{FEATURE_VERSION}", json.dumps(spec["label"]), min(ds["t0"]), max(ds["t1"]),
                  json.dumps(sorted({s["symbol"] for s in ds["signals"]})), "signal", json.dumps({}),
                  "technical_signal", "BUILT", json.dumps({"rows": len(ds["y"]), "base_rate": float(ds["y"].mean())}),
                  str(f), snap, now, now))
    conn.commit()
    return {"dataset_id": did, "spec_hash": spec_hash, "snapshot_hash": snap, "snapshot_path": str(f),
            "start_date": min(ds["t0"]), "end_date": max(ds["t1"])}


def _register(conn, ds, cfg, keep, rep, run_id, did, spec_hash, actor) -> tuple:
    from ml import artifacts, registry as REG
    model_id = cfg.get("model_id") or f"meta_label_{cfg['model_type']}"
    m = REG.get_model(conn, model_id)
    if m and (m["purpose"] != PURPOSE or m["model_type"] != cfg["model_type"]):
        raise ValueError(f"model {model_id} exists as a {m['purpose']} {m['model_type']} model; "
                         "set meta_label.model_id")
    if not m:
        REG.create_model(conn, model_id, "Meta-label: technical signals", cfg["model_type"], LABEL_KIND,
                         f"{FEATURE_SET}@{FEATURE_VERSION}", "W40 secondary model: P(a technical signal reaches "
                         "its target first) -- ml/meta_label.py", owner=actor, purpose=PURPOSE)
    fs = feature_set_row(conn)
    dsrow = _dataset_row(conn, ds, cfg, did, spec_hash)
    tcfg = {k: cfg[k] for k in ("model_type", "params", "threshold", "n_splits", "embargo_sessions", "weighting",
                                "min_oos", "min_group", "min_t", "size_step")}
    version = REG.start_version(conn, model_id, dsrow, fs, tcfg, actor, links={"meta_label_run_id": run_id})
    try:
        cols = [FEATURES[j] for j in keep]
        Xk = ds["X"][:, keep]
        model = _fit(cfg, cols, Xk, ds["y"], ds["w"])
        imp = model.importances() or {}
        ref = {"columns": cols, "mean": np.nanmean(Xk, axis=0).tolist(), "std": np.nanstd(Xk, axis=0).tolist(),
               "missing": np.isnan(Xk).mean(axis=0).tolist(),
               "quantiles": np.nanpercentile(Xk, [10, 25, 50, 75, 90], axis=0).tolist(),
               "prediction_mean": model.predict_proba(Xk).mean(axis=0).tolist()}
        period = {"start": min(ds["t0"]), "end": max(ds["t1"])}
        art = artifacts.save(model, model_id, version, {
            "feature_set": fs, "feature_columns": FEATURES, "selected_columns": cols, "scan_catalogue": ds["catalogue"],
            "threshold": cfg["threshold"], "size_step": cfg["size_step"], "training_config": tcfg,
            "dataset": dsrow, "label": {"kind": LABEL_KIND, "classes": CLASSES}, "reference_stats": ref,
            "training_period": period, "meta_label_run_id": run_id, "verdict": rep["verdict"]})
        summary = {"validation": {"verdict": rep["verdict"], "oos_signals": rep["oos_signals"],
                                  "auc": rep["classification"]["auc"],
                                  "expectancy_all": rep["expectancy"]["all"]["mean_r"],
                                  "expectancy_kept": rep["expectancy"]["kept"]["mean_r"],
                                  "t_stat": rep["expectancy"]["t_stat"]},
                   "meta_label_run_id": run_id, "note": "out of fold, purged k-fold (ml/meta_label.py)"}
        REG.finish_version(conn, model_id, version, True, art, summary, period)
        if imp:
            conn.execute("INSERT INTO ml_model_explanation (model_id,version,kind,explanation_version,payload_json,"
                         "created_at) VALUES (?,?,?,?,?,?)", (model_id, version, "global_importance", "1",
                                                              json.dumps(imp), datetime.now()))
            conn.commit()
        return model_id, version, imp
    except Exception as e:
        REG.finish_version(conn, model_id, version, False, error=str(e)[:2000])
        raise


def _store_run(conn, run_id, model_id, version, rep, did, chash):
    e = rep.get("expectancy") or {}
    kept, allg = e.get("kept") or {}, e.get("all") or {}
    conn.execute("INSERT INTO ml_meta_label_run (run_id,model_id,version,verdict,reason,signals,oos_signals,"
                 "kept_signals,expectancy_all,expectancy_kept,t_stat,threshold,dataset_id,config_hash,report_json,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, model_id, version, rep["verdict"], rep["reason"], rep.get("signals"), rep.get("oos_signals"),
                  kept.get("n"), allg.get("mean_r"), kept.get("mean_r"), e.get("t_stat"), rep.get("threshold"), did, chash, json.dumps(rep, default=str), datetime.now()))
    conn.commit()


def train(conn, cfg: dict | None = None, actor: str = "owner", force: bool = False, register: bool = True) -> dict:
    """Label, featurise, cross-validate (purged k-fold), report and -- with register -- store the model fitted on
    every labelled signal as a TRAINED version of its own kind. Never activates anything. A rerun on an unchanged
    dataset and config returns the stored report (status UNCHANGED) unless force."""
    from ml.registry import config_hash
    cfg = check_config(cfg if cfg is not None else settings())
    ensure_tables(conn)
    ds = dataset(conn, cfg)
    n = len(ds["y"])
    chash = config_hash({k: v for k, v in cfg.items() if k != "enabled"})
    did = None
    if n:
        ids = [(s["signal_id"], ds["t0"][i], ds["t1"][i], int(ds["y"][i]), round(float(ds["r"][i]), 6))
               for i, s in enumerate(ds["signals"])]
        spec_hash = _hash({"labels": ids, "features": FEATURES, "version": FEATURE_VERSION,
                           "X": hashlib.sha256(np.nan_to_num(ds["X"], nan=-9.99e99).tobytes()).hexdigest()})
        did = f"meta_label@{spec_hash[:12]}"
        if not force:
            prev = conn.execute("SELECT run_id FROM ml_meta_label_run WHERE dataset_id=? AND config_hash=? AND "
                                "(model_id IS NOT NULL OR ?=0) ORDER BY created_at DESC, run_id DESC LIMIT 1",
                                (did, chash, int(register))).fetchone()
            if prev:
                return {**get_run(conn, prev[0]), "status": "UNCHANGED"}
    run_id = f"ML{datetime.now():%Y%m%d%H%M%S%f}{uuid.uuid4().hex[:4].upper()}"     # sorts in creation order
    base = {"run_id": run_id, "signals": n, "open_signals": ds["open"], "engine_mismatch": ds["engine_mismatch"],
            "model": {"model_type": cfg["model_type"], "params": cfg["params"]}, "config": {
                k: cfg[k] for k in ("threshold", "n_splits", "embargo_sessions", "weighting", "min_oos", "min_group",
                                    "min_t", "size_step")}, "feature_version": FEATURE_VERSION}
    if n < max(MIN_LABELS, 10 * cfg["n_splits"]) or len(set(ds["t0"])) < cfg["n_splits"] or len(set(ds["y"])) < 2:
        rep = {**base, "verdict": "NO_EDGE", "oos_signals": 0, "threshold": cfg["threshold"],
               "reason": (f"{n} labelled signals over {len(set(ds['t0']))} dates"
                          f"{'' if len(set(ds['y'])) > 1 else ', one class'}: too few to train "
                          f"(needs {max(MIN_LABELS, 10 * cfg['n_splits'])})")}
        _store_run(conn, run_id, None, None, rep, did, chash)
        return {**rep, "status": "TOO_FEW"}
    keep, dropped = _columns(ds["X"])
    if not keep:
        raise ValueError("no usable feature column (all missing or constant)")
    cv = cross_validate(ds, cfg, keep)
    rep = {**base, **evaluate(ds["y"], cv["p"], ds["r"], ds["u"], cfg, cv["fold_base"], ds["r_fill"])}
    labs = list(ds["labels"].values())
    rep.update({"period": {"start": min(ds["t0"]), "end": max(ds["t1"])},
                "barriers": {b: sum(1 for x in labs if x["barrier"] == b) for b in ("PT", "SL", "VB")},
                "gaps": sum(x["gap"] for x in labs), "folds": cv["folds"],
                "weights": {"scheme": cfg["weighting"], "mean_uniqueness": round(float(ds["u"].mean()), 4),
                            "min_uniqueness": round(float(ds["u"].min()), 4)},
                "features": {"columns": [FEATURES[j] for j in keep], "dropped": dropped},
                "dataset_id": did})
    model_id = version = None
    if register:
        model_id, version, imp = _register(conn, ds, cfg, keep, rep, run_id, did, spec_hash, actor)
        rep["features"]["importances"] = {k: round(v, 4) for k, v in sorted(imp.items(), key=lambda kv: -kv[1])}
        rep.update(model_id=model_id, version=version,
                   adoption=("ADOPTABLE: the owner may move it VALIDATION -> APPROVED -> ACTIVE (/ml or python -m ml "
                             "lifecycle); nothing is activated automatically" if rep["verdict"] == "ADOPTABLE" else
                             "NO_EDGE: the registry refuses APPROVED / ACTIVE for this version"))
    _store_run(conn, run_id, model_id, version, rep, did, chash)
    return {**rep, "status": "TRAINED" if register else "EVALUATED"}


def get_run(conn, run_id) -> dict:
    r = conn.execute("SELECT report_json, created_at, model_id, version FROM ml_meta_label_run WHERE run_id=?",
                     (run_id,)).fetchone()
    if not r:
        raise LookupError(f"no meta-label run {run_id}")
    return {**json.loads(r[0]), "run_id": run_id, "created_at": str(r[1])[:19], "model_id": r[2], "version": r[3]}


def allowed_to_activate(conn, model_id, version) -> tuple:
    """(ok, why) for moving a meta-label version to APPROVED / ACTIVE: its own latest run must be ADOPTABLE."""
    ensure_tables(conn)
    r = conn.execute("SELECT verdict, reason FROM ml_meta_label_run WHERE model_id=? AND version=? ORDER BY created_at "
                     "DESC, run_id DESC LIMIT 1", (model_id, version)).fetchone()
    if not r:
        return False, f"no meta-label run recorded for {model_id} {version}"
    return r[0] == "ADOPTABLE", f"its run is {r[0]}: {r[1]}"


def adopted_model(conn) -> dict | None:
    """The ACTIVE meta-label version whose run is ADOPTABLE (meta_label.model_id pins one model), newest first."""
    ensure_tables(conn)
    pin = settings().get("model_id")
    rows = conn.execute("SELECT v.model_id, v.version, v.artifact_path, v.artifact_hash, v.activated_at FROM "
                        "ml_model_version v JOIN ml_model m ON m.model_id=v.model_id WHERE m.purpose=? AND "
                        "v.status='ACTIVE' ORDER BY v.activated_at DESC", (PURPOSE,)).fetchall()
    for mid, ver, path, h, at in rows:
        if pin and mid != pin:
            continue
        ok, _ = allowed_to_activate(conn, mid, ver)
        if ok and path:
            return {"model_id": mid, "version": ver, "artifact_path": path, "artifact_hash": h,
                    "activated_at": str(at)[:19] if at else None}
    return None


# ── scoring today's signals ───────────────────────────────────────────────

def score(conn, as_of=None, cfg: dict | None = None) -> dict:
    """Score the day's signals with the adopted model into ml_meta_label_score. A no-op (SKIPPED, nothing
    written) unless meta_label.enabled and an adopted model exists."""
    from ml import artifacts
    cfg = check_config(cfg if cfg is not None else settings())
    if not cfg["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "meta_label.enabled is false"}
    ensure_tables(conn)
    ad = adopted_model(conn)
    if not ad:
        return {"status": "SKIPPED", "rows": 0, "reason": "no adopted meta-label model (an ACTIVE version whose run "
                                                          "is ADOPTABLE)"}
    d = _ds(as_of) or _ds((conn.execute("SELECT MAX(date) FROM technical_signal").fetchone() or [None])[0])
    if not d:
        return {"status": "EMPTY", "rows": 0, "reason": "no technical signals stored"}
    build_labels(conn, store=True, only_missing=True)                 # the scan records read up to today
    sigs = _signals(conn, " AND date=?", (d,))
    if not sigs:
        return {"status": "EMPTY", "rows": 0, "date": d}
    model, doc = artifacts.load(ad["artifact_path"], ad["artifact_hash"])
    X = features(conn, sigs, None, doc.get("scan_catalogue"))
    sel = doc["selected_columns"]
    Xk = np.column_stack([X[:, FEATURES.index(c)] if c in FEATURES else np.full(len(sigs), np.nan) for c in sel])
    p = _p1(model, Xk)
    thr, step = float(doc.get("threshold", cfg["threshold"])), float(doc.get("size_step", cfg["size_step"]))
    sz = bet_size(p, step)
    now = datetime.now()
    for s, pi, si, row in zip(sigs, p, sz, Xk):
        conn.execute(
            "INSERT INTO ml_meta_label_score (signal_id,signal_date,symbol,scan,direction,model_id,version,probability,"
            "bet_size,kept,threshold,features_json,scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) "
            "DO UPDATE SET model_id=excluded.model_id, version=excluded.version, probability=excluded.probability, "
            "bet_size=excluded.bet_size, kept=excluded.kept, threshold=excluded.threshold, "
            "features_json=excluded.features_json, scored_at=excluded.scored_at",
            (s["signal_id"], d, s["symbol"], s["scan"], s["direction"], ad["model_id"], ad["version"],
             round(float(pi), 6), float(si), int(pi >= thr), thr,
             json.dumps({c: (None if math.isnan(v) else round(float(v), 6)) for c, v in zip(sel, row)}), now))
    conn.commit()
    return {"status": "SUCCESS", "rows": len(sigs), "date": d, "kept": int((p >= thr).sum()),
            "model": f"{ad['model_id']}@{ad['version']}", "threshold": thr}


def attach_scores(conn, rows: list) -> list:
    """Add meta_prob / meta_size / meta_kept / meta_model to today's signal rows (research/tech_signals.
    todays_signals) when scoring is enabled and the stored score is the adopted model's; else unchanged."""
    if not rows:
        return rows
    try:
        if not settings()["enabled"]:
            return rows
        ad = adopted_model(conn)
    except Exception as e:                               # a broken config must not break /signals
        log.warning(f"  meta-label scores not attached: {e}")
        return rows
    if not ad:
        return rows
    dates = sorted({_ds(r.get("date")) for r in rows if r.get("date")})
    got = {}
    for i in range(0, len(dates), 400):
        part = dates[i:i + 400]
        for d, sym, scan, p, sz, k in conn.execute(
                f"SELECT signal_date, symbol, scan, probability, bet_size, kept FROM ml_meta_label_score WHERE "
                f"model_id=? AND version=? AND signal_date IN ({','.join('?' * len(part))})",
                [ad["model_id"], ad["version"]] + part):
            got[(_ds(d), sym, scan)] = (p, sz, k)
    for r in rows:
        x = got.get((_ds(r.get("date")), r.get("symbol"), r.get("scan")))
        if x:
            r.update(meta_prob=x[0], meta_size=x[1], meta_kept=bool(x[2]),
                     meta_model=f"{ad['model_id']}@{ad['version']}")
    return rows


def report(conn) -> dict:
    """GET /api/research/meta-label/report: the settings, the adopted model, the latest run's full report, the
    recent runs and the latest scores."""
    ensure_tables(conn)
    s = settings()
    runs = [dict(zip(("run_id", "model_id", "version", "verdict", "reason", "signals", "oos_signals", "kept_signals",
                      "expectancy_all", "expectancy_kept", "t_stat", "created_at"), r)) for r in conn.execute(
        "SELECT run_id, model_id, version, verdict, reason, signals, oos_signals, kept_signals, expectancy_all, "
        "expectancy_kept, t_stat, created_at FROM ml_meta_label_run ORDER BY created_at DESC, run_id DESC LIMIT 20")]
    for r in runs:
        r["created_at"] = str(r["created_at"])[:19]
    latest = get_run(conn, runs[0]["run_id"]) if runs else None
    sc = conn.execute("SELECT signal_date, COUNT(*), SUM(kept), model_id, version FROM ml_meta_label_score WHERE "
                      "signal_date=(SELECT MAX(signal_date) FROM ml_meta_label_score) GROUP BY signal_date, model_id, "
                      "version").fetchall()
    labels = conn.execute("SELECT COUNT(*), SUM(label), MIN(t0), MAX(t1) FROM ml_meta_label").fetchone()
    ad = adopted_model(conn)
    warn = None
    if ad and latest and latest.get("verdict") != "ADOPTABLE" and (latest.get("model_id"), latest.get("version")) != (
            ad["model_id"], ad["version"]):
        warn = (f"the latest run ({latest['run_id']}, {latest.get('verdict')}) no longer finds the edge the active "
                f"model {ad['model_id']}@{ad['version']} was adopted on: consider pausing it (/ml)")
    return {"settings": {k: v for k, v in s.items()}, "adopted": ad, "warning": warn,
            "scoring": ("on" if s["enabled"] and ad else "off: meta_label.enabled is false" if not s["enabled"]
                        else "off: no adopted model (an ACTIVE meta-label version whose run is ADOPTABLE)"),
            "labels": {"signals": labels[0] or 0, "targets_first": labels[1] or 0, "first_t0": _ds(labels[2]),
                       "last_t1": _ds(labels[3])},
            "latest_scores": [{"date": _ds(r[0]), "scored": r[1], "kept": r[2] or 0, "model": f"{r[3]}@{r[4]}"}
                              for r in sc],
            "latest": latest, "runs": runs,
            "note": ("Meta-labelling (AFML ch. 3-4, 7, 10): a secondary model estimates P(a technical signal reaches "
                     "its target first) from what was known at its close. Research only: it never changes a "
                     "signal's side, never sizes an order, and never activates itself.")}


# ── jobs and CLI ──────────────────────────────────────────────────────────

def run_weekly() -> dict:
    """Saturday: retrain when meta_label.enabled (pipeline/scheduler.py via schedule_jobs); else SKIPPED."""
    s = settings()
    if not s["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "meta_label.enabled is false"}
    from db.schema import get_connection
    conn = get_connection()
    try:
        out = train(conn, s, actor="scheduler")
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": out.get("signals", 0), "verdict": out.get("verdict"),
            "outcome": out.get("status"), "model": out.get("model_id"), "version": out.get("version")}


def run_nightly() -> dict:
    """20:45 on market days: score the day's signals when meta_label.enabled and a model is adopted; else SKIPPED."""
    from utils.trading_calendar import is_trading_day
    if not is_trading_day(date.today()):
        return {"status": "SKIPPED", "rows": 0, "reason": "not a market day"}
    s = settings()
    if not s["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "meta_label.enabled is false"}
    from db.schema import get_connection
    conn = get_connection()
    try:
        return score(conn, cfg=s)
    finally:
        conn.close()


SCORE_TIME, TRAIN_TIME = "20:45", "09:30"


def schedule_jobs(schedule, run_job) -> list:
    """Register the nightly scoring and the weekly training (both no-ops while meta_label.enabled is false, so the
    switch needs no restart)."""
    schedule.every().day.at(SCORE_TIME).do(run_job, "meta_label_score", run_nightly)
    schedule.every().saturday.at(TRAIN_TIME).do(run_job, "meta_label_train", run_weekly)
    return [f"meta_label_score daily {SCORE_TIME} (market days; needs meta_label.enabled and an adopted model)",
            f"meta_label_train Saturday {TRAIN_TIME} (needs meta_label.enabled)"]


def _summary(rep: dict) -> dict:
    keep = ("status", "run_id", "verdict", "reason", "signals", "oos_signals", "model_id", "version", "adoption",
            "classification", "expectancy", "sizing", "rule")
    return {k: rep[k] for k in keep if k in rep}


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m ml.meta_label", description="meta-labelling of the technical signals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train", help="label, cross-validate, report and register a TRAINED version")
    t.add_argument("--force", action="store_true", help="retrain even when the dataset and config are unchanged")
    t.add_argument("--no-register", action="store_true", help="report only; store no model version")
    t.add_argument("--full", action="store_true", help="print the whole report (folds, sweep, features)")
    sc = sub.add_parser("score", help="score the day's signals (needs meta_label.enabled and an adopted model)")
    sc.add_argument("--date")
    sub.add_parser("report", help="the latest run, the adopted model and the latest scores")
    sub.add_parser("labels", help="refresh the triple-barrier labels and print their counts")
    a = ap.parse_args(argv)
    from db.schema import get_connection
    conn = get_connection()
    try:
        ensure_tables(conn)
        if a.cmd == "train":
            out = train(conn, force=a.force, register=not a.no_register)
            out = out if a.full else _summary(out)
        elif a.cmd == "score":
            out = score(conn, a.date)
        elif a.cmd == "labels":
            lab = build_labels(conn)
            ls = list(lab["labels"].values())
            out = {"labelled": len(ls), "open": lab["open"], "engine_mismatch": lab["engine_mismatch"],
                   "barriers": {b: sum(1 for x in ls if x["barrier"] == b) for b in ("PT", "SL", "VB")},
                   "gaps": sum(x["gap"] for x in ls)}
        else:
            out = report(conn)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":
    main()
