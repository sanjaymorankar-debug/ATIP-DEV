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


def build_windows(sessions: list, train: int, validation: int, test: int, step: int) -> list:
    if min(train, test, step) < 1 or validation < 0:
        raise ValueError("train, test and step must be >= 1 session; validation >= 0")
    if step < test:
        raise ValueError(f"step_sessions ({step}) < test_sessions ({test}): test windows would overlap")
    span = train + validation + test
    wins, i, k = [], 0, 0
    while i + span <= len(sessions):
        tr = (sessions[i], sessions[i + train - 1])
        va = (sessions[i + train], sessions[i + train + validation - 1]) if validation else None
        te = (sessions[i + train + validation], sessions[i + span - 1])
        wins.append({"index": k, "train": tr, "validation": va, "test": te})
        i += step
        k += 1
    if not wins:
        raise ValueError(f"{len(sessions)} sessions cannot fit one window of {span}")
    return wins


def _metric(run_result: dict, key: str):
    m = (run_result or {}).get("metrics") or {}
    return m.get(key)


def run_walk_forward(request: dict, train_sessions: int, validation_sessions: int, test_sessions: int,
                     step_sessions: int, candidates: list | None = None, select_by: str = "sharpe") -> dict:
    """request: a backtest request with start/end (the whole span) -- see backtest.service."""
    if select_by not in SELECT_BY:
        raise ValueError(f"select_by must be one of {SELECT_BY}")
    if not request.get("start") or not request.get("end") or request.get("periods"):
        raise ValueError("walk-forward needs start and end (the whole span), not periods")
    base = {k: v for k, v in request.items() if k not in ("start", "end", "period_label", "allow_test")}
    cands = candidates or [request.get("params") or {}]
    if validation_sessions == 0 and len(cands) > 1:
        raise ValueError("several candidates need a validation window to choose between them")

    parent_snap = service.resolve_config(request)          # validates the base request
    parent_snap["walk_forward"] = {"train_sessions": train_sessions, "validation_sessions": validation_sessions,
                                   "test_sessions": test_sessions, "step_sessions": step_sessions,
                                   "candidates": cands, "select_by": select_by}
    wins = build_windows(trading_sessions(request["start"], request["end"]),
                         train_sessions, validation_sessions, test_sessions, step_sessions)
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
        stitched = M.summarize(oos_dates, oos_equity, [t["net_pnl"] for t in oos_trades],
                               [t["return_pct"] for t in oos_trades if t["return_pct"] is not None],
                               rf_annual=parent_snap["risk_free_rate_pct"] / 100) if len(oos_equity) > 1 else None
        status = "COMPLETED" if all(s["test_status"] == "COMPLETED" for s in summary) else "FAILED"
        conn = get_connection()
        try:
            store.save_summary(conn, parent_id, status, {"windows": summary, "n_windows": len(summary)},
                               metrics=stitched,
                               bias={"out_of_sample": "stitched metrics use test windows only; parameters were "
                                                      "chosen on validation windows that precede them"})
        finally:
            conn.close()
        return {"run_id": parent_id, "status": status, "windows": summary, "oos_metrics": stitched}
    except Exception as e:
        conn = get_connection()
        try:
            store.mark_failed(conn, parent_id, f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        raise
