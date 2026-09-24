"""
Strategy health (SE-11).

compute_health(strategy_id) builds one snapshot with these metrics:

    last_execution       the latest decision run (date, status)
    last_signal          the latest BUY / SELL decision (date, symbol, decision)
    signal_frequency     BUY + SELL decisions per successful run over WINDOW_DAYS
    error_count          FAILED runs + ERROR events over WINDOW_DAYS
    performance_ref      the latest COMPLETED backtest of the same version (an
                         out-of-sample one when there is one): its run id and
                         stored metrics, quoted as-is -- nothing is estimated
    data_availability    evaluated / universe symbols in the latest run
    parameter_validity   whether the version's parameters still resolve

Status, first match wins:

    DISABLED  the strategy is PAUSED / DISABLED / RETIRED / ARCHIVED
    ERROR     the latest run FAILED, or the parameters no longer resolve
    STALE     in PAPER / READY / ACTIVE with no successful run for more than
              STALE_DAYS days (or never)
    WARNING   any issue below
    HEALTHY   otherwise (DRAFT / research states with no issue are HEALTHY:
              they are not expected to run on a schedule)

Warnings (each names its threshold): errors in the window, data availability
below MIN_DATA_AVAILABILITY, no signal in INACTIVE_RUNS runs, more than
EXCESSIVE_SIGNALS_PER_RUN signals per run, no completed backtest of the
version, backtest profit factor < 1 or drawdown worse than MAX_DRAWDOWN_WARN,
more than BLOCKED_SHARE of wanted entries blocked by risk requirements.

Computed on request and once a day by the pipeline; stored in strategy_health.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from strategy_engine import lifecycle

WINDOW_DAYS = 45
STALE_DAYS = 4
INACTIVE_RUNS = 20
EXCESSIVE_SIGNALS_PER_RUN = 25
MIN_DATA_AVAILABILITY = 0.8
MAX_DRAWDOWN_WARN = -0.25
BLOCKED_SHARE = 0.5
STATUSES = ("HEALTHY", "WARNING", "ERROR", "STALE", "DISABLED")


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10]) if v else None


def _latest_backtest(conn, sid, ver):
    rows = [dict(r) for r in conn.execute(
        "SELECT run_id, kind, period_label, metrics_json, finished_at FROM backtest_run WHERE strategy_id=? "
        "AND strategy_version=? AND status='COMPLETED' AND metrics_json IS NOT NULL ORDER BY finished_at DESC",
        (sid, ver))]
    oos = [r for r in rows if r["period_label"] in ("validation", "test") or r["kind"] in ("wf_test", "walk_forward")]
    r = (oos or rows or [None])[0]
    if not r:
        return None
    m = json.loads(r["metrics_json"])
    out = {k: m.get(k) for k in ("total_return", "sharpe", "max_drawdown", "win_rate", "profit_factor", "trades",
                                 "sessions")}
    out.update({"run_id": r["run_id"], "kind": r["kind"], "period_label": r["period_label"],
                "finished_at": str(r["finished_at"])})
    return out


def _param_validity(conn, sid, ver) -> dict:
    from strategy_engine import registry
    from strategy_engine.definition import specs, validate
    from strategy_engine.params import resolve
    try:
        v = registry.get_version(conn, sid, ver)
        if not v:
            return {"valid": False, "error": f"version {ver} not found"}
        validate(v["definition"])
        resolve(specs(v["definition"]))
        return {"valid": True, "error": None}
    except Exception as e:
        return {"valid": False, "error": str(e)[:500]}


def compute_health(conn, strategy_id: str, version: str | None = None, as_of: date | None = None,
                   store: bool = True) -> dict:
    s = conn.execute("SELECT status, current_version FROM strategy WHERE strategy_id=?", (strategy_id,)).fetchone()
    if not s:
        raise ValueError(f"no strategy {strategy_id}")
    lc, ver = s[0], version or s[1]
    as_of = as_of or date.today()
    since = as_of - timedelta(days=WINDOW_DAYS)
    runs = [dict(r) for r in conn.execute(
        "SELECT run_id, as_of, status, error, counts_json, n_universe, n_evaluated, created_at "
        "FROM strategy_decision_run WHERE strategy_id=? AND version=? AND as_of<=? ORDER BY as_of, created_at",
        (strategy_id, ver, str(as_of)))]
    win = [r for r in runs if r["as_of"] and _d(r["as_of"]) >= since]
    ok = [r for r in win if r["status"] != "FAILED"]
    counts = [json.loads(r["counts_json"] or "{}") for r in ok]
    signals = [c.get("BUY", 0) + c.get("ADD", 0) + c.get("REDUCE", 0) + c.get("EXIT", 0) + c.get("SELL", 0)
               for c in counts]
    buys = sum(c.get("BUY", 0) + c.get("ADD", 0) for c in counts)
    blocked = sum(c.get("BLOCKED_BY_RISK", 0) for c in counts)
    last = runs[-1] if runs else None
    last_ok = next((r for r in reversed(runs) if r["status"] != "FAILED"), None)
    sig = conn.execute("SELECT as_of, symbol, decision FROM strategy_decision WHERE strategy_id=? AND version=? "
                       "AND decision IN ('BUY','SELL') AND as_of<=? ORDER BY as_of DESC, timestamp DESC LIMIT 1",
                       (strategy_id, ver, str(as_of))).fetchone()
    n_err_events = conn.execute("SELECT COUNT(*) FROM strategy_engine_event WHERE strategy_id=? AND "
                                "event_type='ERROR' AND at>=?", (strategy_id, datetime.combine(since, datetime.min.time()))
                                ).fetchone()[0]
    avail = (round(last_ok["n_evaluated"] / last_ok["n_universe"], 4)
             if last_ok and last_ok.get("n_universe") else None)
    last_sig_idx = max((i for i, n in enumerate(signals) if n), default=None)
    m = {
        "window_days": WINDOW_DAYS,
        "last_execution": ({"as_of": str(last["as_of"]), "status": last["status"], "run_id": last["run_id"],
                            "error": last["error"]} if last else None),
        "days_since_last_success": (as_of - _d(last_ok["as_of"])).days if last_ok else None,
        "last_signal": ({"as_of": str(sig[0]), "symbol": sig[1], "decision": sig[2]} if sig else None),
        "decision_runs": len(ok),
        "signal_frequency": round(sum(signals) / len(ok), 3) if ok else None,
        "runs_since_last_signal": (len(ok) - 1 - last_sig_idx) if last_sig_idx is not None else (len(ok) or None),
        "error_count": sum(1 for r in win if r["status"] == "FAILED") + 0,
        "error_events": n_err_events,
        "data_availability": ({"evaluated": last_ok["n_evaluated"], "universe": last_ok["n_universe"],
                               "ratio": avail} if last_ok else None),
        "parameter_validity": _param_validity(conn, strategy_id, ver),
        "performance_ref": _latest_backtest(conn, strategy_id, ver),
        "blocked_by_risk": blocked,
    }

    issues = []
    def add(code, msg):
        issues.append({"code": code, "level": "WARNING", "message": msg})
    if m["error_events"] or m["error_count"]:
        add("errors", f"{m['error_count']} failed run(s), {m['error_events']} error event(s) in {WINDOW_DAYS} days")
    if avail is not None and avail < MIN_DATA_AVAILABILITY:
        add("data_availability", f"only {avail:.0%} of the universe had data (< {MIN_DATA_AVAILABILITY:.0%})")
    if len(ok) >= INACTIVE_RUNS and (m["runs_since_last_signal"] or 0) >= INACTIVE_RUNS:
        add("inactive", f"no BUY/SELL decision in the last {m['runs_since_last_signal']} runs")
    if m["signal_frequency"] is not None and m["signal_frequency"] > EXCESSIVE_SIGNALS_PER_RUN:
        add("excessive_signals", f"{m['signal_frequency']} signals per run (> {EXCESSIVE_SIGNALS_PER_RUN})")
    bt = m["performance_ref"]
    if not bt:
        add("never_backtested", f"no completed backtest of version {ver}")
    else:
        if bt.get("profit_factor") is not None and bt["profit_factor"] < 1:
            add("weak_backtest", f"backtest {bt['run_id']} profit factor {bt['profit_factor']:.2f} < 1")
        if bt.get("max_drawdown") is not None and bt["max_drawdown"] < MAX_DRAWDOWN_WARN:
            add("weak_backtest", f"backtest {bt['run_id']} max drawdown {bt['max_drawdown']:.1%} worse than "
                                 f"{MAX_DRAWDOWN_WARN:.0%}")
    if buys + blocked and blocked / (buys + blocked) > BLOCKED_SHARE:
        add("many_blocked", f"{blocked} of {buys + blocked} wanted entries blocked by risk requirements")

    if lc in lifecycle.OFF_STATES:
        status, why = "DISABLED", f"lifecycle state {lc}"
    elif (last and last["status"] == "FAILED") or not m["parameter_validity"]["valid"]:
        status = "ERROR"
        why = (f"latest run failed: {last['error']}" if last and last["status"] == "FAILED"
               else f"parameters invalid: {m['parameter_validity']['error']}")
    elif lc in lifecycle.DECISION_STATES and (m["days_since_last_success"] is None
                                              or m["days_since_last_success"] > STALE_DAYS):
        status = "STALE"
        why = f"{lc} but no successful decision run in {STALE_DAYS} days (last: " \
              f"{last_ok['as_of'] if last_ok else 'never'})"
    elif issues:
        status, why = "WARNING", f"{len(issues)} warning(s)"
    else:
        status, why = "HEALTHY", "no issues"
    out = {"strategy_id": strategy_id, "version": ver, "as_of": str(as_of), "lifecycle": lc,
           "status": status, "status_reason": why, "metrics": m, "issues": issues}
    if store:
        conn.execute("INSERT INTO strategy_health (strategy_id,version,as_of,status,metrics_json,issues_json,created_at) "
                     "VALUES (?,?,?,?,?,?,?) ON CONFLICT(strategy_id,version,as_of) DO UPDATE SET status=excluded.status,"
                     "metrics_json=excluded.metrics_json,issues_json=excluded.issues_json,created_at=excluded.created_at",
                     (strategy_id, ver, str(as_of), status, json.dumps({**m, "status_reason": why}, default=str),
                      json.dumps(issues), datetime.now()))
        conn.commit()
    return out


def run_health_all(trade_date=None) -> dict:
    """Health for every strategy not ARCHIVED (run_job compatible)."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        on = date.fromisoformat(str(trade_date)) if trade_date else None
        ids = [r[0] for r in conn.execute("SELECT strategy_id FROM strategy WHERE status<>'ARCHIVED'")]
        res = {sid: compute_health(conn, sid, as_of=on)["status"] for sid in ids}
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": len(res), "health": res}
