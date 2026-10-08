"""
Combinatorially purged cross-validation (CPCV) and the probability of backtest
overfitting (PBO): research hardening (docs/ATIP_GAP_ANALYSIS_2026-10.md §4 item 9).

A backtest, or a walk-forward, is ONE path through history: the parameters that look
best on it may only be the luckiest of the many tried. Both tools measure how much of
an in-sample choice survives out of sample, as a distribution rather than one number.

CPCV (Lopez de Prado 2018, Advances in Financial Machine Learning, ch. 12)

    run_cpcv(request, n_groups=6, k_test=2, candidates=None, select_by="sharpe",
             purge_sessions=0, embargo_sessions=0) -> parent run_id + path distribution

  GROUPS   the trading sessions of [start, end] (backtest.walkforward.trading_sessions)
           are cut into N contiguous groups of nearly equal size: the first T mod N
           groups are one session longer.
  RUNS     every candidate parameter set is backtested on every group ONCE: an ordinary
           engine run (kind cpcv_trial, under a parent of kind cpcv) over the group's
           window -- warmed up on the history before it, closed out at its end (unless
           the request says close_out_at_end false), so no position crosses a group
           boundary. M candidates x N groups runs; nothing else is run.
  SPLITS   every combination of k test groups: C(N, k) splits; the other N - k groups
           train. Per split:
             purge    the last purge_sessions sessions of a training group FOLLOWED by a
                      test group leave the training returns (a trade opened there would
                      hold into the test group)
             embargo  the first embargo_sessions sessions of a training group that FOLLOWS
                      a test group leave them too (serial correlation: its warm-up and
                      first trades read the test group's prices)
             select   each candidate's select_by (sharpe | sortino | total_return) on what
                      is left, chained across the training groups; the best is chosen
                      (ties -> the first; no candidate scored -> candidate 0)
             test     the chosen candidate's returns on each test group (untrimmed)
           Purge / embargo count a group run's own sessions (its equity rows). Unlike
           backtest/walkforward.py, where test always follows train and the embargo opens
           each out-of-sample window, a training group can follow a test group here, so
           the embargo is applied to that training group, as in AFML 7.4.
  PATHS    each group is tested in C(N-1, k-1) splits; path j takes, for every group, the
           j-th split (in combination order) that tests it, and that split's chosen
           candidate's returns there. phi = k/N x C(N, k) = C(N-1, k-1) paths, each an
           out-of-sample return series over the whole timeline, chained across groups.
  REPORT   per path: total return, CAGR, Sharpe, Sortino, volatility, max drawdown; their
           DISTRIBUTION over the phi paths (n, mean, std, min, p05, p25, median, p75, p95,
           max, share_positive); how often each candidate was chosen; and, with two or more
           candidates, the CSCV statistics of the splits themselves: the logit of the chosen
           candidate's out-of-sample rank in each split and pbo = the share <= 0.
           With one candidate nothing is selected: every path is the same series.

PBO via CSCV (Bailey, Borwein, Lopez de Prado & Zhu 2015, "The probability of backtest
overfitting", Journal of Computational Finance 20(4)) -- pure numpy:

    pbo(matrix, n_partitions=16, metric="sharpe") -> dict
    pbo_for_run(run_id, n_partitions=16, metric="sharpe") -> stored run of kind pbo
        the matrix is the daily returns of a parent run's trials (an optimisation's
        opt_trial runs: every completed child over the parent's window), aligned by
        session, the first session dropped (measured against initial capital) as the
        optimiser's deflated Sharpe does

  matrix   T x N: T sessions (rows, oldest first) x N trials (columns) of returns
  S        an even number of row blocks, 2..16; the first T mod S rows are dropped
  for each of the C(S, S/2) combinations c of S/2 blocks:
           J  = those blocks, in-sample; J' = the other S/2, out-of-sample
           R  = metric of every trial on J;  R' = on J'
           n* = argmax R                      (the in-sample best; ties -> the lowest index)
           w  = rank of R'[n*] in R' / (N+1)  (rank 1 = worst .. N = best, ties averaged)
           logit = ln(w / (1 - w))            (> 0: the IS best beats the OOS median)
  pbo          share of combinations with logit <= 0
  degradation  least-squares line R'[n*] = intercept + slope x R[n*] across combinations,
               with r2 (slope well below 1, or negative: in-sample results do not carry)
  prob_loss    share of combinations with R'[n*] < 0
  metric       sharpe: mean / sample stdev x sqrt(252), a zero-variance column scoring 0
               (or +-inf with a non-zero mean); mean: per session; total_return: compound
  One trial best in every block -> pbo 0; trials of pure noise -> pbo near 0.5; a ranking
  that reverses out of sample -> pbo near 1. Not computed: the paper's stochastic-dominance
  test.

Bounds -- errors, never silent truncation: CPCV N 2..12 groups, k 1..N-1, 1..20 candidates
(at most 240 engine runs), >= 5 sessions per group and purge + embargo <= group - 2; PBO
2..1000 trials, <= 50,000 sessions, S even 2..16 with >= 2 rows per block. CPCV selects
parameters, so it is refused on the test window like the optimiser; PBO only reads trials that
already ran (and the optimiser never runs on test).

    python -m backtest cpcv --strategy dip --start D --end D --groups 6 --test-groups 2
                            --candidates '[{"stop_pct":3},{"stop_pct":5}]' [--purge 10 --embargo 1]
    python -m backtest pbo OPTIMIZATION_RUN_ID [--partitions 16] [--metric sharpe]
    python -m backtest pbo --matrix returns.csv [--partitions 16]
"""

from __future__ import annotations

import csv
import itertools
import json
import math
from datetime import date
from pathlib import Path

import numpy as np

from backtest import metrics as M

MAX_GROUPS = 12
MAX_CANDIDATES = 20
MIN_GROUP_SESSIONS = 5
CPCV_SELECT_BY = ("sharpe", "sortino", "total_return")
PBO_METRICS = ("sharpe", "mean", "total_return")
MAX_PARTITIONS = 16
MAX_PBO_TRIALS = 1000
MAX_PBO_SESSIONS = 50_000
LOGIT_BINS = (-2.0, -1.0, 0.0, 1.0, 2.0)
LOGIT_LABELS = ("<= -2", "(-2, -1]", "(-1, 0]", "(0, 1]", "(1, 2]", "> 2")
_CHUNK = 2048                         # combinations evaluated per numpy block (bounds memory)


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _r(x, n=6):
    return None if x is None else (x if isinstance(x, float) and math.isinf(x) else round(float(x), n))


def distribution(values) -> dict:
    """n, mean, sample std, min, p05 / p25 / median / p75 / p95 (linear interpolation), max
    and the share above zero of the finite values; None / non-finite values are left out."""
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(v))
    n = len(xs)
    if not n:
        return {"n": 0}
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else None
    return {"n": n, "mean": _r(mean), "std": _r(sd), "min": _r(xs[0]), "p05": _r(M._pctile(xs, 0.05)),
            "p25": _r(M._pctile(xs, 0.25)), "median": _r(M._pctile(xs, 0.5)), "p75": _r(M._pctile(xs, 0.75)),
            "p95": _r(M._pctile(xs, 0.95)), "max": _r(xs[-1]), "share_positive": _r(sum(x > 0 for x in xs) / n, 4)}


def _logit(rank: float, n: int) -> float:
    w = rank / (n + 1)
    return math.log(w / (1 - w))


def _avg_rank(values: list, i: int) -> float:
    """Rank of values[i] among values, 1 = lowest, ties averaged; None counts as -inf."""
    x = [(-math.inf if v is None else v) for v in values]
    less = sum(v < x[i] for v in x)
    equal = sum(v == x[i] for v in x)
    return less + (equal + 1) / 2


# ── CPCV: pure parts ───────────────────────────────────────────────────────

def split_groups(sessions: list, n_groups: int) -> list:
    """N contiguous groups of nearly equal size; the first len % N are one longer."""
    base, extra = divmod(len(sessions), int(n_groups))
    out, i = [], 0
    for g in range(int(n_groups)):
        size = base + (1 if g < extra else 0)
        out.append(list(sessions[i:i + size]))
        i += size
    return out


def cpcv_splits(n_groups: int, k_test: int) -> list:
    """Every combination of k test groups, in itertools.combinations order."""
    return list(itertools.combinations(range(int(n_groups)), int(k_test)))


def n_paths(n_groups: int, k_test: int) -> int:
    """phi = k / N x C(N, k) = C(N-1, k-1)."""
    return math.comb(int(n_groups) - 1, int(k_test) - 1)


def assemble_paths(n_groups: int, splits: list) -> list:
    """Path j -> [the split index that supplies group 0, group 1, ...]: the j-th split
    (in order) among those that test that group."""
    by_group = [[s for s, test in enumerate(splits) if g in test] for g in range(int(n_groups))]
    return [[by_group[g][j] for g in range(int(n_groups))] for j in range(len(by_group[0]))]


def training_slices(group_sizes: list, test: tuple, purge: int = 0, embargo: int = 0) -> list:
    """Per group, the (lo, hi) slice of its sessions that trains in this split, or None for a
    test group: purge trims the end of a group followed by a test group, embargo the start of
    a group that follows one."""
    t = set(test)
    out = []
    for g, size in enumerate(group_sizes):
        if g in t:
            out.append(None)
            continue
        out.append((embargo if g - 1 in t else 0, size - purge if g + 1 in t else size))
    return out


def _score(rets: list, how: str, rf: float):
    if len(rets) < 2:
        return None
    if how == "sharpe":
        return M.sharpe(rets, rf)
    if how == "sortino":
        return M.sortino(rets, rf)
    g = 1.0
    for r in rets:
        g *= 1 + r
    return g - 1


def series_stats(dates: list, rets: list, rf: float = 0.0) -> dict:
    """A return series chained from 1: total return, CAGR, Sharpe, Sortino, volatility,
    max drawdown (backtest.metrics conventions)."""
    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    dts = [_d(dates[0])] + [_d(x) for x in dates] if dates else []
    return {"sessions": len(rets), "start": str(dts[0]) if dts else None, "end": str(dts[-1]) if dts else None,
            "total_return": _r(eq[-1] - 1), "cagr": _r(M.cagr(eq, dts)) if dts else None,
            "sharpe": _r(M.sharpe(rets, rf)), "sortino": _r(M.sortino(rets, rf)),
            "volatility": _r(M.volatility(rets)), "max_drawdown": _r(M.max_drawdown(eq))}


PATH_KEYS = ("sharpe", "total_return", "cagr", "sortino", "volatility", "max_drawdown")


def cpcv_paths(series: list, n_groups: int, k_test: int, purge: int = 0, embargo: int = 0,
               select_by: str = "sharpe", rf_annual: float = 0.0) -> dict:
    """
    The CPCV selection and paths from returns, no database. series[m][g] is candidate m's
    [(date, return), ...] on group g (each group its own run; every candidate's group g
    covers the same sessions). Returns {"splits", "paths", "distribution",
    "selection_frequency", "pbo"} -- see the module docstring.
    """
    if select_by not in CPCV_SELECT_BY:
        raise ValueError(f"select_by must be one of {CPCV_SELECT_BY}")
    n, k = int(n_groups), int(k_test)
    if not series or any(len(s) != n for s in series):
        raise ValueError(f"series must give every candidate's returns on each of the {n} groups")
    sizes = [len(series[0][g]) for g in range(n)]
    for s in series[1:]:
        for g in range(n):
            if [d for d, _ in s[g]] != [d for d, _ in series[0][g]]:
                raise ValueError(f"candidates' runs on group {g} cover different sessions")
    if min(sizes) < 2:
        raise ValueError("every group needs at least two sessions of returns")
    if purge + embargo > min(sizes) - 2:
        raise ValueError(f"purge ({purge}) + embargo ({embargo}) leave fewer than two training sessions in a "
                         f"group of {min(sizes)}")
    splits = cpcv_splits(n, k)
    n_cand = len(series)
    recs, chosen = [], []
    for si, test in enumerate(splits):
        keep = training_slices(sizes, test, purge, embargo)
        train = [[r for g, kp in enumerate(keep) if kp for _d, r in s[g][kp[0]:kp[1]]] for s in series]
        scores = [_score(t, select_by, rf_annual) for t in train]
        best, pick = None, 0
        for m, sc in enumerate(scores):
            if sc is not None and (best is None or sc > best):
                best, pick = sc, m
        oos = [_score([r for g in test for _d, r in s[g]], select_by, rf_annual) for s in series]
        rec = {"index": si, "test_groups": list(test), "train_sessions": len(train[0]),
               "purged_sessions": sum(purge for g in range(n) if keep[g] and g + 1 in test),
               "embargoed_sessions": sum(embargo for g in range(n) if keep[g] and g - 1 in test),
               "chosen": pick, "selection": "best " + select_by if best is not None else "no score: first candidate",
               "train_scores": [_r(x) for x in scores], "test_score": _r(oos[pick])}
        if n_cand > 1:
            rank = _avg_rank(oos, pick)
            rec.update({"test_scores": [_r(x) for x in oos], "oos_rank": rank,
                        "logit": _r(_logit(rank, n_cand))})
        recs.append(rec)
        chosen.append(pick)
    paths = []
    for j, row in enumerate(assemble_paths(n, splits)):
        dates, rets = [], []
        for g, si in enumerate(row):
            for d, r in series[chosen[si]][g]:
                dates.append(d)
                rets.append(r)
        paths.append({"index": j, "splits": row, "candidates": [chosen[si] for si in row],
                      **series_stats(dates, rets, rf_annual)})
    freq = [{"candidate": m, "chosen": chosen.count(m), "share": round(chosen.count(m) / len(splits), 4)}
            for m in range(n_cand)]
    out = {"splits": recs, "paths": paths, "distribution": {key: distribution([p[key] for p in paths])
                                                              for key in PATH_KEYS},
           "selection_frequency": freq, "pbo": None}
    if n_cand > 1:
        lam = [r["logit"] for r in recs]
        out["pbo"] = {"pbo": round(sum(x <= 0 for x in lam) / len(lam), 6), "n_splits": len(lam),
                      "logits": distribution(lam),
                      "prob_loss": round(sum((r["test_score"] or 0) < 0 for r in recs) / len(recs), 6),
                      "note": f"CSCV on the CPCV splits: the logit of the chosen candidate's out-of-sample "
                              f"{select_by} rank among the {n_cand} candidates on each split's test groups"}
    return out


# ── CPCV: engine runs ──────────────────────────────────────────────────────

def prepare_cpcv(request: dict, n_groups: int = 6, k_test: int = 2, candidates: list | None = None,
                 select_by: str = "sharpe", purge_sessions: int = 0, embargo_sessions: int = 0) -> tuple:
    """Validate a CPCV study without running anything: (groups of sessions, candidates)."""
    from backtest import service
    from backtest.walkforward import trading_sessions
    if request.get("period_label") == "test" or request.get("allow_test"):
        raise ValueError("CPCV on the test window is refused: it selects parameters, so run it on research "
                         "(or validation) and evaluate the result once on test")
    if not request.get("start") or not request.get("end") or request.get("periods"):
        raise ValueError("CPCV needs start and end (the whole span), not periods")
    if select_by not in CPCV_SELECT_BY:
        raise ValueError(f"select_by must be one of {CPCV_SELECT_BY}")
    n, k = int(n_groups), int(k_test)
    if not 2 <= n <= MAX_GROUPS:
        raise ValueError(f"n_groups must be 2..{MAX_GROUPS} (C(N, k) splits; {MAX_GROUPS} groups already give up "
                         f"to {math.comb(MAX_GROUPS, MAX_GROUPS // 2)})")
    if not 1 <= k < n:
        raise ValueError(f"k_test must be 1..{n - 1} (at least one group must train)")
    cands = list(candidates) if candidates else [dict(request.get("params") or {})]
    if not all(isinstance(c, dict) for c in cands):
        raise ValueError("candidates must be a list of parameter objects")
    if len(cands) > MAX_CANDIDATES:
        raise ValueError(f"{len(cands)} candidates > {MAX_CANDIDATES}: {len(cands)} x {n} groups would be "
                         f"{len(cands) * n} backtests -- narrow the set (e.g. an optimiser's top trials)")
    purge, embargo = int(purge_sessions or 0), int(embargo_sessions or 0)
    if purge < 0 or embargo < 0:
        raise ValueError("purge_sessions and embargo_sessions must be >= 0")
    service.resolve_config(request)                         # validates the base request
    for c in cands:
        service.resolve_config({**request, "params": c})    # and every candidate's parameters
    sessions = trading_sessions(request["start"], request["end"])
    groups = split_groups(sessions, n)
    small = min(len(g) for g in groups)
    if small < MIN_GROUP_SESSIONS:
        raise ValueError(f"{len(sessions)} sessions in {n} groups leaves {small} per group: need >= "
                         f"{MIN_GROUP_SESSIONS} (fewer groups or a longer span)")
    if purge + embargo > small - 2:
        raise ValueError(f"purge ({purge}) + embargo ({embargo}) leave fewer than two training sessions in a group "
                         f"of {small}")
    return groups, cands


def run_cpcv(request: dict, n_groups: int = 6, k_test: int = 2, candidates: list | None = None,
             select_by: str = "sharpe", purge_sessions: int = 0, embargo_sessions: int = 0) -> dict:
    """request: a backtest request with start / end (the whole span). Every candidate x group
    is one engine run (kind cpcv_trial) under a parent run (kind cpcv) whose summary holds
    the splits, the paths and their distribution."""
    from backtest import store
    from backtest.optimize import _finish, _parent, _trial
    from backtest.walkforward import max_holding_sessions
    from db.schema import get_connection
    groups, cands = prepare_cpcv(request, n_groups, k_test, candidates, select_by, purge_sessions, embargo_sessions)
    n, k = int(n_groups), int(k_test)
    purge, embargo = int(purge_sessions or 0), int(embargo_sessions or 0)
    meta = {"n_groups": n, "k_test": k, "candidates": cands, "select_by": select_by, "purge_sessions": purge,
            "embargo_sessions": embargo, "n_splits": math.comb(n, k), "n_paths": n_paths(n, k)}
    pid, snap = _parent(request, "cpcv", meta)
    recommended = max_holding_sessions(snap)
    rf = float(snap.get("risk_free_rate_pct") or 0) / 100
    base = {key: v for key, v in request.items() if key not in ("params", "start", "end")}
    try:
        runs, series = [], []
        for m, params in enumerate(cands):
            row = []
            for g, grp in enumerate(groups):
                rid, res = _trial({**base, "start": str(grp[0]), "end": str(grp[-1])}, params, "cpcv_trial", pid,
                                  m * n + g)
                runs.append({"candidate": m, "group": g, "run_id": rid, "status": res["status"],
                             **({"error": res.get("error")} if res["status"] != "COMPLETED" else {})})
                if res["status"] != "COMPLETED":
                    row.append(None)
                    continue
                conn = get_connection()
                try:
                    eq = store.get_rows(conn, "backtest_equity", rid)
                finally:
                    conn.close()
                row.append([(_d(p["date"]), p["daily_return"] or 0.0) for p in eq])
            series.append(row)
        ok = [m for m, row in enumerate(series) if all(r is not None for r in row)]
        group_info = [{"index": g, "start": str(grp[0]), "end": str(grp[-1]), "sessions": len(grp)}
                      for g, grp in enumerate(groups)]
        notes = []
        if len(ok) < len(cands):
            notes.append(f"candidates {[m for m in range(len(cands)) if m not in ok]} left out: a group run did not "
                         f"complete")
        if not ok:
            summary = {"groups": group_info, "runs": runs, **meta, "notes": notes}
            _finish(pid, "FAILED", summary)
            return {"run_id": pid, "status": "FAILED", **summary}
        out = cpcv_paths([series[m] for m in ok], n, k, purge, embargo, select_by, rf)
        for rec in out["splits"]:                               # back to the caller's candidate numbers
            rec["chosen"] = ok[rec["chosen"]]
        for p in out["paths"]:
            p["candidates"] = [ok[c] for c in p["candidates"]]
        for f in out["selection_frequency"]:
            f["candidate"] = ok[f["candidate"]]
        if len(ok) == 1:
            notes.append("one candidate: nothing is selected, so every path is the same series -- give several "
                         "candidates (e.g. an optimiser's top trials) for a distribution")
        if recommended and purge < recommended:
            notes.append(f"purge_sessions {purge} < the strategy's maximum holding period ({recommended} sessions)")
        d = out["distribution"]
        summary = {"groups": group_info, **meta, "recommended_purge_sessions": recommended, "candidates_used": ok,
                   **out, "runs": runs, "notes": notes}
        metrics = {"paths": len(out["paths"]), "sharpe": d["sharpe"].get("median"),
                   "total_return": d["total_return"].get("median"), "max_drawdown": d["max_drawdown"].get("median"),
                   "sharpe_p05": d["sharpe"].get("p05"),
                   "share_positive_paths": d["total_return"].get("share_positive"),
                   "pbo": (out["pbo"] or {}).get("pbo")}
        leak = (f"{purge} session(s) purged from the end of each training group before a test group, {embargo} "
                f"embargoed at the start of each training group after one" if purge or embargo else
                "no purge / embargo: training groups touch the test groups"
                + (f" (recommended purge: {recommended} sessions, the maximum holding period)" if recommended else ""))
        _finish(pid, "COMPLETED", summary, metrics=metrics,
                bias={"out_of_sample": "every path session is out-of-sample for the candidate chosen on that split's "
                                       "training groups; path metrics are medians over paths",
                      "group_runs": "each group is its own run, closed out at its end: no position crosses a group",
                      "purge_embargo": leak})
        return {"run_id": pid, "status": "COMPLETED", **summary}
    except Exception as e:
        conn = get_connection()
        try:
            store.mark_failed(conn, pid, f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        raise


# ── PBO via CSCV ───────────────────────────────────────────────────────────

def _stat(sums, sumsq, logs, n: int, metric: str, ppy: int):
    """The metric of every (combination, trial) from block sums over n rows."""
    mean = sums / n
    if metric == "mean":
        return mean
    if metric == "total_return":
        return np.expm1(logs)
    var = (sumsq - n * mean * mean) / (n - 1)
    zero = var <= 64 * np.finfo(float).eps * (sumsq / n)          # cancellation noise: no variance
    sr = mean / np.sqrt(np.where(zero, 1.0, var)) * math.sqrt(ppy)
    flat = np.where(mean > 0, np.inf, np.where(mean < 0, -np.inf, 0.0))
    return np.where(zero, flat, sr)


def validate_matrix(matrix, n_partitions: int = 16, metric: str = "sharpe") -> np.ndarray:
    if metric not in PBO_METRICS:
        raise ValueError(f"metric must be one of {PBO_METRICS}")
    s = int(n_partitions)
    if s % 2 or not 2 <= s <= MAX_PARTITIONS:
        raise ValueError(f"n_partitions must be even, 2..{MAX_PARTITIONS} (C(16, 8) = 12,870 combinations)")
    try:
        x = np.asarray(matrix, dtype=float)
    except (TypeError, ValueError) as e:
        raise ValueError(f"matrix must be numbers: {e}") from None
    if x.ndim != 2:
        raise ValueError("matrix must be 2-D: sessions (rows) x trials (columns)")
    t, n = x.shape
    if not 2 <= n <= MAX_PBO_TRIALS:
        raise ValueError(f"{n} trials: PBO needs 2..{MAX_PBO_TRIALS} (columns)")
    if t > MAX_PBO_SESSIONS:
        raise ValueError(f"{t} sessions > {MAX_PBO_SESSIONS}")
    if t < 2 * s:
        raise ValueError(f"{t} sessions cannot fill {s} partitions of >= 2 rows: fewer partitions or more sessions")
    if not np.isfinite(x).all():
        raise ValueError("matrix has missing or non-finite returns")
    if metric == "total_return" and (x <= -1).any():
        raise ValueError("a return <= -100% cannot be compounded (metric total_return)")
    return x


def pbo(matrix, n_partitions: int = 16, metric: str = "sharpe", periods_per_year: int = M.PERIODS_PER_YEAR,
        labels: list | None = None) -> dict:
    """PBO by combinatorially symmetric cross-validation (module docstring). Pure: the
    same matrix gives the same result."""
    x = validate_matrix(matrix, n_partitions, metric)
    s = int(n_partitions)
    t, n = x.shape
    if labels is not None and len(labels) != n:
        raise ValueError(f"{len(labels)} labels for {n} trials")
    rows = t // s
    drop = t - rows * s
    blocks = x[drop:].reshape(s, rows, n)
    sums, sumsq = blocks.sum(axis=1), (blocks * blocks).sum(axis=1)
    logs = np.log1p(blocks).sum(axis=1) if metric == "total_return" else np.zeros_like(sums)
    combos = list(itertools.combinations(range(s), s // 2))
    w_all = np.zeros((len(combos), s))
    for c, comb in enumerate(combos):
        w_all[c, list(comb)] = 1.0
    half = rows * s // 2
    is_best, oos_best, lam, star_all = [], [], [], []
    for a in range(0, len(combos), _CHUNK):
        w = w_all[a:a + _CHUNK]
        wb = 1.0 - w
        r_is = _stat(w @ sums, w @ sumsq, w @ logs, half, metric, periods_per_year)
        r_oos = _stat(wb @ sums, wb @ sumsq, wb @ logs, half, metric, periods_per_year)
        star = r_is.argmax(axis=1)
        idx = np.arange(len(w))
        sel = r_oos[idx, star]
        rank = (r_oos < sel[:, None]).sum(axis=1) + ((r_oos == sel[:, None]).sum(axis=1) + 1) / 2.0
        omega = rank / (n + 1)
        is_best.append(r_is[idx, star])
        oos_best.append(sel)
        lam.append(np.log(omega / (1 - omega)))
        star_all.append(star)
    xs, ys, lam, star = (np.concatenate(v) for v in (is_best, oos_best, lam, star_all))
    fin = np.isfinite(xs) & np.isfinite(ys)
    deg = {"slope": None, "intercept": None, "r2": None, "n": int(fin.sum())}
    if fin.sum() >= 2:
        xf, yf = xs[fin], ys[fin]
        sxx = float(((xf - xf.mean()) ** 2).sum())
        syy = float(((yf - yf.mean()) ** 2).sum())
        if sxx > 0:
            sxy = float(((xf - xf.mean()) * (yf - yf.mean())).sum())
            slope = sxy / sxx
            deg.update({"slope": _r(slope), "intercept": _r(float(yf.mean()) - slope * float(xf.mean())),
                        "r2": _r(sxy * sxy / (sxx * syy)) if syy > 0 else None})
    hist = np.bincount(np.searchsorted(np.asarray(LOGIT_BINS), lam, side="left"), minlength=len(LOGIT_LABELS))
    counts = np.bincount(star, minlength=n)
    top = sorted(range(n), key=lambda j: (-counts[j], j))[:10]
    out = {"pbo": _r(float((lam <= 0).mean())), "prob_loss": _r(float((ys < 0).mean())), "metric": metric,
           "n_partitions": s, "n_combinations": len(combos), "n_trials": n, "sessions": t,
           "sessions_used": rows * s, "sessions_dropped": drop, "rows_per_partition": rows,
           "logits": distribution(lam.tolist()),
           "logit_histogram": [{"bin": lb, "count": int(c)} for lb, c in zip(LOGIT_LABELS, hist)],
           "degradation": deg, "is_best": distribution(xs.tolist()), "oos_of_is_best": distribution(ys.tolist()),
           "selected_trials": [{"trial": j, **({"label": labels[j]} if labels else {}),
                                "share": round(float(counts[j]) / len(combos), 4)} for j in top if counts[j]]}
    if len(combos) <= 500:
        out["logit_values"] = [_r(v) for v in lam.tolist()]
    return out


def trial_matrix(run_id: str) -> tuple:
    """(matrix T x N, [trial run], dates) of a stored parent run: the daily returns of its
    completed children over the parent's own window (not kind pbo), aligned on the sessions
    they all have, the first session left out."""
    from backtest import store
    from db.schema import get_connection
    conn = get_connection()
    try:
        run = store.get_run(conn, run_id)
        if not run:
            raise ValueError(f"no backtest run {run_id}")
        if run["status"] in ("CREATED", "RUNNING"):
            raise ValueError(f"run {run_id} is still {run['status']}")
        window = (str(run["start_date"])[:10], str(run["end_date"])[:10])
        kids = [c for c in store.child_runs(conn, run_id) if c["status"] == "COMPLETED" and c["kind"] != "pbo"
                and (str(c["start_date"])[:10], str(c["end_date"])[:10]) == window]
        if len(kids) < 2:
            raise ValueError(f"run {run_id} has {len(kids)} completed trial(s) over its window {window[0]}..{window[1]}"
                             f": PBO needs at least two (an optimisation's trials)")
        series = [{str(r["date"])[:10]: (r["daily_return"] or 0.0)
                   for r in store.get_rows(conn, "backtest_equity", k["run_id"])} for k in kids]
    finally:
        conn.close()
    dates = sorted(set.intersection(*(set(s) for s in series)))[1:]
    return [[s[d] for s in series] for d in dates], kids, dates


def pbo_for_run(run_id: str, n_partitions: int = 16, metric: str = "sharpe") -> dict:
    """PBO of a stored parent run's trials, saved as a run of kind pbo under that parent."""
    from backtest import store
    from db.schema import get_connection
    mat, kids, dates = trial_matrix(run_id)
    res = pbo(mat, n_partitions, metric, labels=[json.dumps(k.get("params"), sort_keys=True) for k in kids])
    summary = {"source_run_id": run_id, "trials": [{"trial": j, "run_id": k["run_id"], "kind": k["kind"],
                                                    "params": k.get("params")} for j, k in enumerate(kids)],
               "first_session": dates[0] if dates else None, "last_session": dates[-1] if dates else None, **res}
    conn = get_connection()
    try:
        snap = dict(store.get_run(conn, run_id)["config"])
        snap["pbo"] = {"source_run_id": run_id, "n_partitions": int(n_partitions), "metric": metric}
        pid = store.create_run(conn, snap, kind="pbo", parent_run_id=run_id)
        store.mark_running(conn, pid)
        store.save_summary(conn, pid, "COMPLETED", summary,
                           metrics={"pbo": res["pbo"], "prob_loss": res["prob_loss"], "trials": res["n_trials"],
                                    "degradation_slope": res["degradation"]["slope"]},
                           bias={"selection": f"CSCV over {res['n_combinations']} combinations of "
                                              f"{res['n_partitions']} blocks of {res['rows_per_partition']} sessions; "
                                              f"the in-sample best {metric} trial's out-of-sample rank -- pbo is the "
                                              f"share at or below the median"})
    finally:
        conn.close()
    return {"run_id": pid, "status": "COMPLETED", **summary}


def load_matrix(path) -> tuple:
    """(matrix, labels) from a CSV (rows = sessions, columns = trials; a non-numeric first
    row is the header) or JSON ([[...], ...] or {"matrix": [[...]], "labels": [...]})."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            return data.get("matrix"), data.get("labels")
        return data, None
    rows = [r for r in csv.reader(text.splitlines()) if r and any(c.strip() for c in r)]
    labels = None
    if rows:
        try:
            [float(c) for c in rows[0]]
        except ValueError:
            labels, rows = [c.strip() for c in rows[0]], rows[1:]
    return [[float(c) for c in r] for r in rows], labels
