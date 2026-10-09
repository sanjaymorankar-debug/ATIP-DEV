"""
Walk-forward validation (BT-02).

    |---- train ----|-- validation --|-- test --|
                  step >|---- train ----|-- validation --|-- test --|
                                   step >| ...

Windows are counted in trading sessions over [start, end]. In each window:

  train       the history the strategy may use to form its view. Nothing is
              fitted automatically in W2 -- the window is reserved and recorded
              so fitting (W3/W5) slots in without changing the framework.
  validation  every candidate parameter set is backtested here; the one with
              the best `select_by` metric is chosen (ties -> the first given).
              With a single candidate there is no choice to make.
  test        the chosen parameters are backtested here, once. Selection never
              saw this window, so these results are out-of-sample.

step_sessions must be >= test_sessions, so test windows never overlap and each
out-of-sample session is counted once. The stitched out-of-sample curve chains
the test windows' daily returns, starting from initial_capital.

Every window's runs are ordinary backtest runs (kinds wf_validation / wf_test)
under one parent run (kind walk_forward), so each can be inspected on its own.
This is selection among candidates you supply, not an optimiser.

PURGE + EMBARGO (BT-18). Back-to-back windows leak: a trade opened on the
last sessions of train / validation would, left to its exit rule, still be
open when the next window starts (the run closes it out at its window's end,
on the very prices the next window starts from), and the next window's first
sessions trade on the same move. Inside each window's fixed slot of
train + validation + test sessions:

    |---- train ----|purge|embargo|-- validation --|purge|embargo|-- test --|

  purge_sessions    dropped from the END of each in-sample window (train, and
                    validation ahead of test). With purge >= the strategy's
                    maximum holding period no in-sample trade's natural exit
                    reaches the next window.
  embargo_sessions  skipped at the START of each out-of-sample window
                    (validation after train, test): history (warm-up) for the
                    strategy, but no trade opens and no return counts there.
With validation 0 there is one boundary (train -> test). Both default to 0,
which gives exactly the back-to-back windows above. Recommended: purge = the
maximum holding period (max_hold_sessions; reported as
recommended_purge_sessions) and an embargo of 1-2 sessions (about 1% of the
span). The slots, and so the step grid, never move -- the effective windows
only shrink -- and the result reports them, with the dates and counts of the
purged / embargoed sessions.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from backtest import metrics as M
from backtest import service, store
from db.schema import get_connection
from utils.trading_calendar import is_trading_day

log = logging.getLogger("atip.backtest")

SELECT_BY = ("sharpe", "sortino", "calmar", "total_return", "cagr", "profit_factor", "expectancy")


def trading_sessions(start, end) -> list:
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def build_windows(sessions: list, train: int, validation: int, test: int, step: int, purge: int = 0,
                  embargo: int = 0) -> list:
    if min(train, test, step) < 1 or validation < 0:
        raise ValueError("train, test and step must be >= 1 session; validation >= 0")
    if step < test:
        raise ValueError(f"step_sessions ({step}) < test_sessions ({test}): test windows would overlap")
    if purge < 0 or embargo < 0:
        raise ValueError("purge_sessions and embargo_sessions must be >= 0")
    if train - purge < 1:
        raise ValueError(f"purge_sessions ({purge}) leaves no train session (train_sessions {train})")
    if test - embargo < 1:
        raise ValueError(f"embargo_sessions ({embargo}) leaves no test session (test_sessions {test})")
    if validation and validation - purge - embargo < 1:
        raise ValueError(f"purge ({purge}) + embargo ({embargo}) leave no validation session "
                         f"(validation_sessions {validation})")
    span = train + validation + test
    wins, i, k = [], 0, 0
    while i + span <= len(sessions):
        v, t = i + train, i + train + validation           # first session of the validation / test slots
        bounds = [v, t] if validation else [t]              # in-sample | out-of-sample boundaries
        tr = (sessions[i], sessions[v - 1 - purge])
        va = (sessions[v + embargo], sessions[t - 1 - purge]) if validation else None
        te = (sessions[t + embargo], sessions[i + span - 1])
        wins.append({"index": k, "train": tr, "validation": va, "test": te,
                     "purged": [(sessions[b - purge], sessions[b - 1]) for b in bounds] if purge else [],
                     "embargoed": [(sessions[b], sessions[b + embargo - 1]) for b in bounds] if embargo else [],
                     "purged_sessions": purge * len(bounds), "embargoed_sessions": embargo * len(bounds)})
        i += step
        k += 1
    if not wins:
        raise ValueError(f"{len(sessions)} sessions cannot fit one window of {span}")
    return wins


def max_holding_sessions(snap: dict) -> int | None:
    """The strategy's maximum holding period in sessions (its max_hold_sessions
    parameter, or a W3 definition's position.max_hold_sessions); None when it
    declares none. The purge that keeps every in-sample trade inside its window."""
    v = (snap.get("params") or {}).get("max_hold_sessions")
    if v is None and snap.get("strategy_definition_hash"):
        try:
            from backtest.strategies import make_strategy
            from strategy_engine.params import value as pval
            strat = make_strategy(snap["strategy_id"], snap.get("params"), snap.get("strategy_version"))
            v = pval(((getattr(strat, "defn", None) or {}).get("position") or {}).get("max_hold_sessions"),
                     strat.params)
        except Exception:                       # advisory only: never fails the walk-forward
            v = None
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def _metric(run_result: dict, key: str):
    m = (run_result or {}).get("metrics") or {}
    return m.get(key)


def run_walk_forward(request: dict, train_sessions: int, validation_sessions: int, test_sessions: int,
                     step_sessions: int, candidates: list | None = None, select_by: str = "sharpe",
                     purge_sessions: int = 0, embargo_sessions: int = 0) -> dict:
    """request: a backtest request with start/end (the whole span) -- see backtest.service.
    purge_sessions / embargo_sessions: the gaps between windows (module docstring); 0 / 0 is no gap."""
    if select_by not in SELECT_BY:
        raise ValueError(f"select_by must be one of {SELECT_BY}")
    if not request.get("start") or not request.get("end") or request.get("periods"):
        raise ValueError("walk-forward needs start and end (the whole span), not periods")
    base = {k: v for k, v in request.items() if k not in ("start", "end", "period_label", "allow_test")}
    cands = candidates or [request.get("params") or {}]
    if validation_sessions == 0 and len(cands) > 1:
        raise ValueError("several candidates need a validation window to choose between them")

    parent_snap = service.resolve_config(request)          # validates the base request
    purge, embargo = int(purge_sessions or 0), int(embargo_sessions or 0)
    recommended = max_holding_sessions(parent_snap)
    parent_snap["walk_forward"] = {"train_sessions": train_sessions, "validation_sessions": validation_sessions,
                                   "test_sessions": test_sessions, "step_sessions": step_sessions,
                                   "candidates": cands, "select_by": select_by,
                                   "purge_sessions": purge, "embargo_sessions": embargo,
                                   "recommended_purge_sessions": recommended}
    wins = build_windows(trading_sessions(request["start"], request["end"]),
                         train_sessions, validation_sessions, test_sessions, step_sessions, purge, embargo)
    conn = get_connection()
    try:
        parent_id = store.create_run(conn, parent_snap, kind="walk_forward")
        store.mark_running(conn, parent_id)
    finally:
        conn.close()

    summary, oos_dates, oos_equity, oos_trades = [], [], [], []
    capital = parent_snap["initial_capital"]
    try:
        for w in wins:
            periods = {"research": [str(w["train"][0]), str(w["train"][1])],
                       "validation": [str(w["validation"][0]), str(w["validation"][1])] if w["validation"] else None,
                       "test": [str(w["test"][0]), str(w["test"][1])]}
            chosen, val_scores = cands[0], []
            if w["validation"] and len(cands) > 1:
                best = None
                for ci, params in enumerate(cands):
                    rid = service.create({**base, "params": params, "periods": periods, "period_label": "validation"},
                                         kind="wf_validation", parent_run_id=parent_id, window_index=w["index"])
                    res = service.execute(rid)
                    score = _metric(res, select_by)
                    val_scores.append({"candidate": ci, "run_id": rid, select_by: score, "status": res["status"]})
                    if score is not None and (best is None or score > best):
                        best, chosen = score, params
            rid = service.create({**base, "params": chosen, "periods": periods, "period_label": "test",
                                  "allow_test": True},
                                 kind="wf_test", parent_run_id=parent_id, window_index=w["index"])
            res = service.execute(rid)
            summary.append({"index": w["index"], "train": w["train"], "validation": w["validation"], "test": w["test"],
                            "purged": w["purged"], "embargoed": w["embargoed"],
                            "purged_sessions": w["purged_sessions"], "embargoed_sessions": w["embargoed_sessions"],
                            "chosen_params": chosen, "validation_scores": val_scores, "test_run_id": rid,
                            "test_status": res["status"], "test_metrics": res.get("metrics")})
            if res["status"] == "COMPLETED":
                conn = get_connection()
                try:
                    eq = store.get_rows(conn, "backtest_equity", rid)
                    oos_trades += store.get_rows(conn, "backtest_trade", rid)
                finally:
                    conn.close()
                # Chain this window onto the stitched curve. Each test run starts
                # from initial_capital, so its first point's return is measured
                # against that; later points carry their own daily_return.
                for j, p in enumerate(eq):
                    r = (p["equity"] / parent_snap["initial_capital"] - 1) if j == 0 else (p["daily_return"] or 0)
                    capital *= 1 + r
                    oos_dates.append(p["date"]); oos_equity.append(round(capital, 2))
        # W39 (PF-06): a position's partial-exit rows count as one trade, as in each run's own metrics;
        # W40: so do a futures short's rolled contract legs
        from backtest.engine import needs_folding, round_trips_from_rows
        if needs_folding(oos_trades):
            oos_trades = round_trips_from_rows(oos_trades)
        stitched = M.summarize(oos_dates, oos_equity, [t["net_pnl"] for t in oos_trades],
                               [t["return_pct"] for t in oos_trades if t["return_pct"] is not None],
                               rf_annual=parent_snap["risk_free_rate_pct"] / 100) if len(oos_equity) > 1 else None
        status = "COMPLETED" if all(s["test_status"] == "COMPLETED" for s in summary) else "FAILED"
        gaps = {"purge_sessions": purge, "embargo_sessions": embargo, "recommended_purge_sessions": recommended,
                "sessions_purged": sum(s["purged_sessions"] for s in summary),
                "sessions_embargoed": sum(s["embargoed_sessions"] for s in summary)}
        leak = (f"{purge} session(s) purged from the end of each in-sample window, {embargo} embargoed at the "
                f"start of each out-of-sample window" if purge or embargo else
                "no purge / embargo: windows are back to back, so a trade opened late in a window holds into the "
                "next" + (f" (recommended purge: {recommended} sessions, the maximum holding period)"
                          if recommended else ""))
        conn = get_connection()
        try:
            store.save_summary(conn, parent_id, status, {"windows": summary, "n_windows": len(summary), **gaps},
                               metrics=stitched,
                               bias={"out_of_sample": "stitched metrics use test windows only; parameters were "
                                                      "chosen on validation windows that precede them",
                                     "purge_embargo": leak})
        finally:
            conn.close()
        return {"run_id": parent_id, "status": status, "windows": summary, "oos_metrics": stitched, **gaps}
    except Exception as e:
        conn = get_connection()
        try:
            store.mark_failed(conn, parent_id, f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        raise
