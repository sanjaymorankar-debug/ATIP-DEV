"""
Create / run / retrieve backtests -- what the CLI and the dashboard API call.

A request is resolved ONCE into an immutable snapshot: every default is filled
in from backtest.config (config.json's "backtest" section), the strategy's
current version is pinned, and the research window is fixed. The snapshot is
stored with the run and is the only thing the engine reads, so a later change
to config.json cannot change what an existing run meant.

Request keys (all but strategy_id optional):
    strategy_id, params, start, end, periods {research, validation, test},
    period_label (research|validation|test|full), allow_test,
    initial_capital, universe ("tracked_current" or a symbol list),
    universe_survivorship_bias, cost_model, cost_overrides, slippage {kind,value},
    liquidity {...}, sizing {...}, risk_free_rate_pct, close_out_at_end, notes
"""

from __future__ import annotations

import copy
import logging
from datetime import date

from backtest import store
from backtest.config import backtest_config
from backtest.costs import cost_model
from backtest.engine import BacktestError, run as run_engine
from backtest.liquidity import LiquidityRule
from backtest.periods import LABELS, ResearchPeriods
from backtest.slippage import SlippageModel
from backtest.strategies import make_strategy
from db.schema import get_connection

log = logging.getLogger("atip.backtest")

REQUEST_KEYS = {"strategy_id", "strategy_version", "params", "start", "end", "periods", "period_label", "allow_test",
                "initial_capital", "universe", "universe_survivorship_bias", "cost_model", "cost_overrides",
                "slippage", "liquidity", "sizing", "risk_free_rate_pct", "close_out_at_end", "notes"}


def _sizing(sizing: dict, strat, requested: dict) -> dict:
    """A W3 strategy's own position rules are its sizing defaults: max_positions,
    target_position_pct (as max_position_pct) and stop_pct (as the default stop).
    Anything the request sets explicitly wins; config.json fills the rest."""
    defn = getattr(strat, "defn", None)
    if not defn:
        return sizing
    from strategy_engine.params import value as pval
    pos = {k: pval(v, strat.params) for k, v in (defn.get("position") or {}).items()}
    for src, dst in (("max_positions", "max_positions"), ("target_position_pct", "max_position_pct"),
                     ("stop_pct", "default_stop_pct")):
        if pos.get(src) is not None and dst not in requested:
            sizing[dst] = pos[src]
    return sizing


def resolve_config(request: dict) -> dict:
    """Validate a request and return the full snapshot a run is executed from."""
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise ValueError(f"unknown backtest settings {sorted(unknown)}")
    if not request.get("strategy_id"):
        raise ValueError("strategy_id is required")
    cfg = backtest_config()
    strat = make_strategy(request["strategy_id"], request.get("params"), request.get("strategy_version"))

    label = request.get("period_label") or "full"
    if label not in LABELS:
        raise ValueError(f"period_label must be one of {LABELS}")
    periods = ResearchPeriods.from_dict(request["periods"]) if request.get("periods") else None
    if periods:
        start, end = periods.window(label)
        if request.get("start") or request.get("end"):
            raise ValueError("give either periods (+ period_label) or start/end, not both")
    else:
        if label != "full":
            raise ValueError(f"period_label {label!r} needs periods defining that window")
        if not request.get("start") or not request.get("end"):
            raise ValueError("start and end are required when no periods are given")
        start, end = date.fromisoformat(str(request["start"])), date.fromisoformat(str(request["end"]))
    if label == "test" and not request.get("allow_test"):
        raise ValueError("the test window is out-of-sample: evaluating on it needs allow_test=true, "
                         "and should happen once the strategy and parameters are fixed")
    if end > date.today():
        raise ValueError(f"end {end} is in the future")

    def merged(key):
        v = copy.deepcopy(cfg[key])
        if isinstance(v, dict):
            v.update(request.get(key) or {})
            return v
        return request[key] if request.get(key) is not None else v

    snap = {
        "strategy_id": strat.strategy_id, "strategy_version": strat.version, "params": strat.params,
        # W3: a stored strategy version pins its definition by hash
        "strategy_definition_hash": getattr(strat, "definition_hash", None),
        "strategy_kind": (getattr(strat, "defn", None) or {}).get("kind", "code"),
        "start": str(start), "end": str(end), "period_label": label,
        "periods": periods.as_dict() if periods else None,
        "data_source": "prices_daily", "timeframe": "1d", "entry_timing": "next_open",
        "initial_capital": float(merged("initial_capital")),
        "universe": request.get("universe") or "tracked_current",
        "universe_survivorship_bias": request.get("universe_survivorship_bias"),
        "cost_model": request.get("cost_model") or cfg["cost_model"],
        "cost_overrides": {**(cfg.get("cost_overrides") or {}), **(request.get("cost_overrides") or {})},
        "slippage": merged("slippage"), "liquidity": merged("liquidity"), "sizing": _sizing(merged("sizing"), strat,
                                                                                           request.get("sizing") or {}),
        "risk_free_rate_pct": float(merged("risk_free_rate_pct") or 0),
        "close_out_at_end": bool(request.get("close_out_at_end", True)),
        "notes": request.get("notes") or "",
    }
    # validate every model now, not halfway through a run
    if snap["initial_capital"] <= 0:
        raise ValueError("initial_capital must be positive")
    cost_model(snap["cost_model"], snap["cost_overrides"] or None)
    SlippageModel(**snap["slippage"])
    LiquidityRule(**snap["liquidity"])
    for k in ("risk_per_trade_pct", "max_position_pct", "max_positions", "default_stop_pct"):
        if not snap["sizing"].get(k) or snap["sizing"][k] <= 0:
            raise ValueError(f"sizing.{k} must be positive")
    snap["cost_model_resolved"] = cost_model(snap["cost_model"], snap["cost_overrides"] or None).as_dict()
    return snap


def _test_window_warnings(conn, snap: dict) -> list:
    if snap["period_label"] != "test":
        return []
    earlier = store.earlier_test_runs(conn, snap["strategy_id"], snap["strategy_version"], snap["start"], snap["end"])
    import json
    mine = json.dumps(snap["params"], sort_keys=True)
    changed = [r["run_id"] for r in earlier if (r["params_json"] or "null") != mine]
    if changed:
        return [f"test window already evaluated with different parameters (runs {', '.join(changed)}): "
                f"parameters chosen after seeing test results are no longer out-of-sample"]
    return []


def create(request: dict, kind: str = "single", parent_run_id: str | None = None,
           window_index: int | None = None) -> str:
    snap = resolve_config(request)
    conn = get_connection()
    try:
        return store.create_run(conn, snap, kind, parent_run_id, window_index)
    finally:
        conn.close()


def execute(run_id: str) -> dict:
    """Run a CREATED (or FAILED) run from its stored snapshot."""
    conn = get_connection()
    try:
        run = store.get_run(conn, run_id)
        if not run:
            raise ValueError(f"no backtest run {run_id}")
        if run["status"] in ("RUNNING", "COMPLETED"):
            raise ValueError(f"run {run_id} is already {run['status']}")
        snap = run["config"]
        store.mark_running(conn, run_id)
        try:
            result = run_engine(snap, conn)
        except Exception as e:
            store.mark_failed(conn, run_id, f"{type(e).__name__}: {e}")
            log.error(f"  backtest {run_id} failed: {e}")
            return {"run_id": run_id, "status": "FAILED", "error": str(e)}
        extra = {"period": {"label": snap["period_label"], "window": [snap["start"], snap["end"]],
                            "periods": snap.get("periods")},
                 "test_window_warnings": _test_window_warnings(conn, snap)}
        store.save_result(conn, run_id, result, bias_extra=extra)
        m = result["metrics"]
        log.info(f"  ✓ backtest {run_id} {snap['strategy_id']} {snap['start']}..{snap['end']} "
                 f"({snap['period_label']}): {m['trades']} trades, return {m['total_return']}, "
                 f"max DD {m['max_drawdown']}")
        return {"run_id": run_id, "status": "COMPLETED", "metrics": m}
    finally:
        conn.close()


def create_and_run(request: dict) -> dict:
    return execute(create(request))


def run_montecarlo(run_id: str, method: str = "trade_shuffle", n_sims: int = 1000, seed: int = 42,
                   block_size: int = 1) -> dict:
    from backtest import montecarlo as MC
    if not 1 <= int(n_sims) <= 100_000:
        raise ValueError("n_sims must be between 1 and 100000")
    conn = get_connection()
    try:
        run = store.get_run(conn, run_id)
        if not run or run["status"] != "COMPLETED":
            raise ValueError(f"run {run_id} is not a completed backtest")
        if method == "trade_shuffle":
            trades = store.get_rows(conn, "backtest_trade", run_id)
            if any(t.get("partial") for t in trades):           # W39 (PF-06): one trade per position
                from backtest.engine import round_trips_from_rows
                trades = round_trips_from_rows(trades)
            eq = {r["date"]: r["equity"] for r in store.get_rows(conn, "backtest_equity", run_id)}
            fracs = [(t["qty"] * t["entry_price"]) / eq[t["entry_date"]] for t in trades
                     if eq.get(t["entry_date"])]
            frac = sum(fracs) / len(fracs) if fracs else 1.0
            res = MC.trade_shuffle([t["return_pct"] for t in trades], n_sims=int(n_sims), seed=int(seed),
                                   position_fraction=round(frac, 6))
        elif method == "return_bootstrap":
            rets = [r["daily_return"] for r in store.get_rows(conn, "backtest_equity", run_id)][1:]
            res = MC.return_bootstrap(rets, n_sims=int(n_sims), seed=int(seed), block_size=int(block_size))
        else:
            raise ValueError("method must be trade_shuffle or return_bootstrap")
        mc_id = store.save_montecarlo(conn, run_id, res, {"method": method, "n_sims": n_sims, "seed": seed,
                                                          "block_size": block_size})
        res["mc_id"] = mc_id
        return res
    finally:
        conn.close()
