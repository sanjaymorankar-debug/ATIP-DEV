"""
The performance report (PERF-001-11, -12, -13): one period, one portfolio, four
returns side by side, every attribution, and an audit block that says exactly
what the numbers were computed from.

    build(conn, owner, portfolio, start, end, benchmark, opts)   compute (not stored)
    run(...)                                                      compute + store
    get / list_reports / export(report, fmt)                     csv | json

AUDIT block: methodology_version, calculation_version (git commit of the running
code, if available), generated_at, assumptions (horizon, slippage, notional,
liquidity cap, link window, risk-free rate, cost model), inputs (ledger rows used
and their sha256, signal ids and their sha256, last price date per series), and a
per-section `source` line. The stored report is the exact JSON the page showed,
so an export always matches the dashboard.
"""

from __future__ import annotations

import csv
import io
import subprocess
from datetime import date, timedelta

from wealth import common as C
from wealth.config import settings
from wealth.perf import data as D
from wealth.perf import engine as E
from wealth.perf import ledger as L
from wealth.perf import metrics as M
from wealth.perf import model as MD

METHODOLOGY_VERSION = "PERF-1.0"
_COMMIT = {}


def _commit():
    if "v" not in _COMMIT:
        try:
            _COMMIT["v"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                          timeout=5).stdout.strip() or None
        except Exception:
            _COMMIT["v"] = None
    return _COMMIT["v"]


def _sectors():
    try:
        from data.index_constituents import get_symbol_industry_map
        return get_symbol_industry_map() or {}
    except Exception:
        return {}


def build(conn, owner, portfolio: str = "PAPER", start=None, end=None, benchmark: str | None = None,
          opts: dict | None = None, sync_first: bool = True) -> dict:
    cfg = settings()
    pf = str(portfolio or "PAPER").upper()
    if pf not in L.PORTFOLIOS:
        raise ValueError(f"portfolio must be one of {list(L.PORTFOLIOS)}")
    end = C.parse_date(end, "end", required=False) or date.today()
    start = C.parse_date(start, "start", required=False) or (end - timedelta(days=365))
    if start >= end:
        raise ValueError("start must be before end")
    if (end - start).days > 3660:
        raise ValueError("period longer than 10 years")
    bsym = D.benchmark_symbol(benchmark or cfg["benchmark"])
    if not conn.execute("SELECT 1 FROM prices_daily WHERE symbol=? LIMIT 1", (bsym,)).fetchone():
        raise ValueError(f"benchmark {bsym} has no prices in prices_daily")
    imported = L.sync(conn, owner) if sync_first else None
    prices = D.Prices(conn, start - timedelta(days=40), end)
    rf = cfg["risk_free_pct"]
    sectors = _sectors()

    actual = E.run_actual(conn, owner, pf, start, end, prices, bsym, rf, sectors)
    mdl = MD.build(conn, start, end, prices, opts or {})
    cal = mdl["calendar"]
    bench_ret = []
    prev = None
    rows = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol=? AND date<?", (bsym, str(start))).fetchone()
    if rows and rows[0]:
        prev = prices.close(bsym, D._d(rows[0]), count_gap=False)
    for d in cal:
        c = prices.close(bsym, d, count_gap=False)
        bench_ret.append(c / prev - 1 if (c and prev) else None)
        prev = c or prev
    m_metrics = M.series_metrics(cal, mdl["model"]["series"], bench_ret, rf)
    e_metrics = M.series_metrics(cal, mdl["executable"]["series"], bench_ret, rf)
    b_metrics = M.series_metrics(cal, bench_ret, None, rf)

    # signal attribution against the chosen portfolio
    fc = {}
    txns = E._basis(conn, E._txns(conn, owner, pf, end), fc)
    links = MD.link_signals(conn, owner, pf, mdl["model"]["trades"], txns, prices, int(mdl["options"]["link_window"]))
    outc = MD.outcomes(conn, [x["signal_id"] for x in links["linked"]])
    for x in links["linked"]:
        x["momentum_outcomes"] = outc.get(x["signal_id"], [])
        x["sizing"]["model_notional"] = mdl["options"]["notional"]
    sig_pnl = sum(x["actual"]["pnl"] for x in links["linked"])
    act_ok = actual.get("status") == "OK"
    total_pnl = (actual["returns"]["realized_pnl"] + actual["returns"]["unrealized_pnl"]) if act_ok else None

    def row(name, desc, met, extra=None):
        return {"return": name, "definition": desc, "total_return_pct": met.get("total_return_pct"),
                "annualized_pct": met.get("annualized_pct"), "volatility_pct": met.get("volatility_pct"),
                "sharpe": met.get("sharpe"), "sortino": met.get("sortino"),
                "max_drawdown_pct": met.get("max_drawdown_pct"), "alpha_annual_pct": met.get("alpha_annual_pct"),
                "beta": met.get("beta"), "days_invested": met.get("days"), **(extra or {})}
    at = actual["returns"]["twr"] if act_ok else {}
    comparison = [
        row("MODEL", "ATIP BUY signals at the signal price, model exit rule, gross, equal weight", m_metrics,
            {"xirr_pct": None, "trades": mdl["model"]["trade_stats"].get("trades"),
             "win_rate_pct": mdl["model"]["trade_stats"].get("win_rate_pct")}),
        row("EXECUTABLE", "same signals at next-session open, slippage, NSE costs, liquidity cap, equal weight",
            e_metrics, {"xirr_pct": None, "trades": mdl["executable"]["trade_stats"].get("trades"),
                        "win_rate_pct": mdl["executable"]["trade_stats"].get("win_rate_pct")}),
        row("ACTUAL", f"your {pf} portfolio: ledger quantities, prices, fees, cash flows (time-weighted)", at,
            {"xirr_pct": actual["returns"]["xirr_pct"] if act_ok else None,
             "trades": actual["trade_stats"].get("trades") if act_ok else None,
             "win_rate_pct": actual["trade_stats"].get("win_rate_pct") if act_ok else None}),
        row("BENCHMARK", f"{bsym} price return over the same sessions", b_metrics,
            {"xirr_pct": actual["returns"]["pme"]["xirr_pct"] if act_ok else None, "trades": None,
             "win_rate_pct": None, "xirr_note": "XIRR here = your cash flows invested in the benchmark (PME)"}),
    ]
    ledger_rows = [t["txn_id"] for t in txns]
    signal_ids = [t["signal_id"] for t in mdl["model"]["trades"]]
    last_px = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol=?", (bsym,)).fetchone()[0]
    try:
        cmodel = MD._cost_model().name
    except Exception:
        cmodel = "nse_delivery"
    return {
        "methodology_version": METHODOLOGY_VERSION, "portfolio": pf, "owner": owner,
        "period": {"start": str(start), "end": str(end), "sessions": len(cal)}, "benchmark": bsym,
        "comparison": comparison,
        "gaps": {"model_minus_executable_pp": _diff(m_metrics, e_metrics),
                 "executable_minus_actual_pp": _diff(e_metrics, at),
                 "actual_minus_benchmark_pp": _diff(at, b_metrics),
                 "note": "percentage-point differences of total return over the same period; the model-to-executable "
                         "gap is the cost of trading the signals, the executable-to-actual gap is the investor's "
                         "own timing, sizing and selection"},
        "model": {"trade_stats": mdl["model"]["trade_stats"], "trades": mdl["model"]["trades"],
                  "signals_in_period": mdl["signals"], "repeats_ignored": mdl["repeats_ignored"],
                  "source": "signal_log (duplicate_of IS NULL) + prices_daily"},
        "executable": {"trade_stats": mdl["executable"]["trade_stats"], "trades": mdl["executable"]["trades"],
                       "not_executable": mdl["executable"]["not_executable"],
                       "source": "signal_log + prices_daily (open / close / volume) + backtest cost model"},
        "actual": actual,
        "signal_attribution": {**links, "signal_driven_pnl": round(sig_pnl, 2),
                               "discretionary_pnl": round(total_pnl - sig_pnl, 2) if total_pnl is not None else None,
                               "total_pnl": round(total_pnl, 2) if total_pnl is not None else None,
                               "source": f"perf_ledger ({pf}) matched to signal_log within "
                                         f"{mdl['options']['link_window']} sessions"},
        "audit": {"methodology_version": METHODOLOGY_VERSION, "calculation_version": _commit(),
                  "generated_at": str(C.now()),
                  "assumptions": {**mdl["options"], "risk_free_pct": rf, "cost_model": cmodel,
                                  "basis": "today's share basis (corporate_actions.entry_factor)",
                                  "flows": "start of session; positions sleeve (cash not modelled)"},
                  "inputs": {"ledger_rows": len(ledger_rows), "ledger_sha256": C.digest(sorted(ledger_rows)),
                             "signals": len(signal_ids), "signals_sha256": C.digest(sorted(signal_ids)),
                             "benchmark_last_price_date": str(last_px)[:10] if last_px else None,
                             "ledger_import": imported}},
        "disclaimer": C.DISCLAIMER,
    }


def _diff(a, b):
    x, y = a.get("total_return_pct"), b.get("total_return_pct")
    return round(x - y, 3) if x is not None and y is not None else None


def run(conn, owner, portfolio="PAPER", start=None, end=None, benchmark=None, opts=None, actor="owner") -> dict:
    rep = build(conn, owner, portfolio, start, end, benchmark, opts)
    rid = C.new_id("perf")
    conn.execute("INSERT INTO perf_report_run (report_id,tenant_id,owner_id,portfolio,period_start,period_end,benchmark,"
                 "methodology_version,calculation_version,inputs_hash,result_json,created_at,created_by) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, owner["tenant_id"], owner["owner_id"], rep["portfolio"], rep["period"]["start"],
                  rep["period"]["end"], rep["benchmark"], METHODOLOGY_VERSION, rep["audit"]["calculation_version"],
                  C.digest(rep["audit"]["inputs"]), C.dumps(rep), C.now(), actor))
    conn.commit()
    return {"report_id": rid, **rep}


def get(conn, owner, rid) -> dict:
    r = conn.execute("SELECT report_id, result_json, created_at FROM perf_report_run WHERE report_id=? AND tenant_id=? "
                     "AND owner_id=?", (rid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("report not found")
    d = C.loads(r["result_json"], {})
    d.update({"report_id": r["report_id"], "created_at": r["created_at"]})
    return d


def list_reports(conn, owner, limit=50) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT report_id, portfolio, period_start, period_end, benchmark, methodology_version, calculation_version, "
        "created_at, created_by FROM perf_report_run WHERE tenant_id=? AND owner_id=? ORDER BY created_at DESC LIMIT ?",
        (owner["tenant_id"], owner["owner_id"], int(limit)))]


def export(rep: dict, fmt: str = "csv") -> tuple[str, str]:
    """(content, media_type). CSV: the comparison, then the actual positions, the round
    trips, the signal attribution and the audit block, as labelled sections."""
    if fmt == "json":
        return C.dumps(rep), "application/json"
    if fmt != "csv":
        raise ValueError("format must be csv or json")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ATIP performance report", rep.get("report_id", "(not stored)"), rep["portfolio"],
                rep["period"]["start"], rep["period"]["end"], "benchmark", rep["benchmark"]])
    w.writerow([])
    cols = ["return", "definition", "total_return_pct", "annualized_pct", "xirr_pct", "volatility_pct", "sharpe",
            "sortino", "max_drawdown_pct", "alpha_annual_pct", "beta", "trades", "win_rate_pct"]
    w.writerow(["# comparison"])
    w.writerow(cols)
    for r in rep["comparison"]:
        w.writerow([r.get(c) for c in cols])
    act = rep["actual"]
    if act.get("status") == "OK":
        w.writerow([])
        w.writerow(["# actual positions"])
        pc = ["symbol", "quantity", "avg_cost", "cost_basis", "price", "market_value", "unrealized", "realized", "fees",
              "dividends"]
        w.writerow(pc)
        for p in act["positions"]:
            w.writerow([p.get(c) for c in pc])
        w.writerow([])
        w.writerow(["# actual round trips (FIFO)"])
        tc = ["symbol", "entry_date", "exit_date", "days", "quantity", "entry", "exit", "pnl"]
        w.writerow(tc)
        for t in act["trades"]:
            w.writerow([t.get(c) for c in tc])
        w.writerow([])
        w.writerow(["# costs", "fees_total", act["costs"]["fees_total"], "slippage_vs_reference",
                    act["costs"]["slippage_vs_reference"], "reconciled", act["costs"]["reconciliation"]["ok"]])
        w.writerow(["# contribution by symbol"])
        w.writerow(["symbol", "sector", "asset_class", "gain", "contribution_pct"])
        for c in act["contribution"]["by_symbol"]:
            w.writerow([c["symbol"], c.get("sector"), c.get("asset_class"), c["gain"], c["contribution_pct"]])
    w.writerow([])
    w.writerow(["# signal attribution"])
    w.writerow(["signal_id", "symbol", "signal_date", "entry_date", "entry_price", "delay_sessions", "model_ret_pct",
                "actual_pnl", "entry_timing", "averaging", "exit_timing", "costs"])
    for x in rep["signal_attribution"]["linked"]:
        a = x.get("position_attribution") or {}
        w.writerow([x["signal_id"], x["symbol"], x["signal_date"], x["entry"]["date"], x["entry"]["price"],
                    x["entry"]["delay_sessions"], x["model"]["ret_pct"], x["actual"]["pnl"], a.get("entry_timing"),
                    a.get("averaging"), a.get("exit_timing"), a.get("costs")])
    w.writerow([])
    w.writerow(["# audit"])
    for k, v in rep["audit"].items():
        w.writerow([k, C.dumps(v) if isinstance(v, (dict, list)) else v])
    return buf.getvalue(), "text/csv"
