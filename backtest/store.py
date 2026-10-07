"""
Backtest run records (BT-16): what ran, on what, with what result.

A run is identified by run_id and fully described by its configuration
snapshot (config_json) -- strategy, version, parameters, window, capital, cost
/ slippage / liquidity / sizing assumptions -- plus config_hash (SHA-256 of the
snapshot), code_version (git commit, "+dirty" when the tree had uncommitted
changes) and the data fingerprint (backtest.data.PriceHistory.fingerprint).
Re-running the snapshot on the same code and data reproduces the run; the
hashes say whether either has changed.

Status: CREATED -> RUNNING -> COMPLETED | FAILED.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from backtest.metrics import jsonable

ROOT = Path(__file__).resolve().parents[1]


def config_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(jsonable(snapshot), sort_keys=True).encode()).hexdigest()


def code_version() -> str | None:
    """The git commit the code is at, '+dirty' with uncommitted changes."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (head + ("+dirty" if dirty else "")) if head else None
    except Exception:
        return None


def _j(x):
    return json.dumps(jsonable(x), sort_keys=True) if x is not None else None


def create_run(conn, snapshot: dict, kind: str = "single", parent_run_id: str | None = None,
               window_index: int | None = None) -> str:
    run_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO backtest_run (run_id,parent_run_id,kind,window_index,strategy_id,strategy_version,"
        "period_label,start_date,end_date,data_source,timeframe,initial_capital,params_json,config_json,"
        "config_hash,code_version,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CREATED',?)",
        (run_id, parent_run_id, kind, window_index, snapshot["strategy_id"], snapshot.get("strategy_version"),
         snapshot.get("period_label"), snapshot.get("start"), snapshot.get("end"), snapshot.get("data_source"),
         snapshot.get("timeframe"), snapshot.get("initial_capital"), _j(snapshot.get("params")), _j(snapshot),
         config_hash(snapshot), code_version(), datetime.now()))
    conn.commit()
    return run_id


def mark_running(conn, run_id: str):
    conn.execute("UPDATE backtest_run SET status='RUNNING', started_at=?, error=NULL WHERE run_id=?",
                 (datetime.now(), run_id))
    conn.commit()


def mark_failed(conn, run_id: str, error: str):
    conn.execute("UPDATE backtest_run SET status='FAILED', error=?, finished_at=? WHERE run_id=?",
                 (str(error)[:2000], datetime.now(), run_id))
    conn.commit()


def save_result(conn, run_id: str, result: dict, bias_extra: dict | None = None, summary: dict | None = None):
    """Replace the run's trades/equity/drawdowns and mark it COMPLETED."""
    for t in ("backtest_trade", "backtest_equity", "backtest_drawdown"):
        conn.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
    conn.executemany(
        "INSERT INTO backtest_trade (run_id,seq,symbol,entry_date,entry_price,entry_ref_price,qty,exit_date,"
        "exit_price,exit_ref_price,exit_reason,gross_pnl,costs,net_pnl,return_pct,holding_sessions,entry_reason,"
        "partial,adds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, i, t["symbol"], str(t["entry_date"]), t["entry_price"], t["entry_ref_price"], t["qty"],
          str(t["exit_date"]), t["exit_price"], t["exit_ref_price"], t["exit_reason"], t["gross_pnl"],
          t["costs"], t["net_pnl"], t["return_pct"], t["holding_sessions"], t.get("entry_reason"),
          1 if t.get("partial") else None, t.get("adds"))      # W39 (PF-06): partial rows / ADD fills
         for i, t in enumerate(result["trades"], 1)])
    conn.executemany(
        "INSERT INTO backtest_equity (run_id,date,cash,positions_value,equity,exposure_pct,n_positions,"
        "realized_cum,unrealized,daily_return,peak_equity,drawdown_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, str(p["date"]), p["cash"], p["positions_value"], p["equity"], p["exposure_pct"],
          p["n_positions"], p["realized_cum"], p["unrealized"], p["daily_return"], p.get("peak_equity"),
          p.get("drawdown_pct")) for p in result["equity"]])
    conn.executemany(
        "INSERT INTO backtest_drawdown (run_id,seq,peak_date,trough_date,recovery_date,peak_equity,"
        "trough_equity,depth_pct,duration_sessions,recovery_sessions) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(run_id, i, str(e["peak_date"]), str(e["trough_date"]),
          str(e["recovery_date"]) if e["recovery_date"] else None, e["peak_equity"], e["trough_equity"],
          round(e["depth"] * 100, 4), e["duration_sessions"], e["recovery_sessions"])
         for i, e in enumerate(result["drawdowns"], 1)])
    bias = dict(result.get("bias_report") or {})
    if bias_extra:
        bias.update(bias_extra)
    bias["events_sample"] = result.get("events", [])[:25]
    conn.execute("UPDATE backtest_run SET status='COMPLETED', metrics_json=?, bias_report_json=?, "
                 "data_fingerprint_json=?, summary_json=?, finished_at=?, error=NULL WHERE run_id=?",
                 (_j(result["metrics"]), _j(bias), _j(result.get("data_fingerprint")), _j(summary),
                  datetime.now(), run_id))
    conn.commit()


def save_summary(conn, run_id: str, status: str, summary: dict, metrics: dict | None = None,
                 bias: dict | None = None):
    """For a parent run (walk-forward) that has no trades of its own."""
    conn.execute("UPDATE backtest_run SET status=?, summary_json=?, metrics_json=?, bias_report_json=?, "
                 "finished_at=? WHERE run_id=?", (status, _j(summary), _j(metrics), _j(bias), datetime.now(), run_id))
    conn.commit()


def _loads(row: dict) -> dict:
    for k in list(row):
        if k.endswith("_json") and row[k]:
            row[k[:-5]] = json.loads(row.pop(k))
        elif k.endswith("_json"):
            row[k[:-5]] = row.pop(k)
    return row


def get_run(conn, run_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM backtest_run WHERE run_id=?", (run_id,)).fetchone()
    return _loads(dict(r)) if r else None


def list_runs(conn, limit: int = 50, strategy_id: str | None = None) -> list:
    q = ("SELECT run_id,parent_run_id,kind,window_index,strategy_id,strategy_version,period_label,start_date,"
         "end_date,initial_capital,status,created_at,finished_at,metrics_json FROM backtest_run")
    args = []
    if strategy_id:
        q += " WHERE strategy_id=?"; args.append(strategy_id)
    q += " ORDER BY created_at DESC LIMIT ?"; args.append(int(limit))
    return [_loads(dict(r)) for r in conn.execute(q, args)]


def child_runs(conn, parent_run_id: str) -> list:
    return [_loads(dict(r)) for r in conn.execute(
        "SELECT run_id,kind,window_index,period_label,start_date,end_date,status,params_json,metrics_json "
        "FROM backtest_run WHERE parent_run_id=? ORDER BY window_index, kind", (parent_run_id,))]


def get_rows(conn, table: str, run_id: str) -> list:
    order = {"backtest_trade": "seq", "backtest_equity": "date", "backtest_drawdown": "seq"}[table]
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,))]


def save_montecarlo(conn, run_id: str, result: dict, params: dict) -> str:
    mc_id = uuid.uuid4().hex[:16]
    conn.execute("INSERT INTO backtest_montecarlo (mc_id,run_id,method,n_sims,seed,params_json,results_json,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (mc_id, run_id, result["method"], result.get("n_sims"), result.get("seed"), _j(params),
                  _j(result), datetime.now()))
    conn.commit()
    return mc_id


def get_montecarlo(conn, run_id: str) -> list:
    return [_loads(dict(r)) for r in conn.execute(
        "SELECT * FROM backtest_montecarlo WHERE run_id=? ORDER BY created_at DESC", (run_id,))]


def earlier_test_runs(conn, strategy_id: str, strategy_version: str, start: str, end: str) -> list:
    """COMPLETED runs of this strategy version evaluated on the same test window."""
    return [dict(r) for r in conn.execute(
        "SELECT run_id, params_json, created_at FROM backtest_run WHERE strategy_id=? AND strategy_version=? "
        "AND period_label='test' AND start_date=? AND end_date=? AND status='COMPLETED' ORDER BY created_at",
        (strategy_id, strategy_version, str(start), str(end)))]
