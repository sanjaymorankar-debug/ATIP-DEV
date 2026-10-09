"""
The performance report (PERF-001-11, -12, -13): one period, one portfolio, four
returns side by side, every attribution, and an audit block that says exactly
what the numbers were computed from.

    build(conn, owner, portfolio, start, end, benchmark, opts, strategy=None)   compute (not stored)
    run(...)                                                      compute + store
    get / list_reports / export(report, fmt)                     csv | json
    verify(conn, owner, report_id)                                W39: rebuild a stored report from
                                                                  today's data and diff it

AUDIT block: methodology_version, calculation_version (CALCULATION_VERSION of this
package + the git commit of the running code, if available), generated_at,
assumptions (horizon, slippage, notional, liquidity cap, link window, risk-free rate,
cost model), inputs (ledger rows used with a sha256 of their ids AND of their
contents, signals with a sha256 of their ids and contents, the prices_daily rows used
-- count and sha256 -- and the last price date of every series), and a per-section
`source` line. The stored report is the exact JSON the page showed, so an export
always matches the dashboard; verify() shows whether the inputs have changed since.

STRATEGY (W39, PERF-001-13): `strategy` limits the ACTUAL portfolio to one strategy's
trades (perf_ledger.strategy_id, i.e. OMS fills); MODEL and EXECUTABLE stay the ATIP
signal set.
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
# W39: version of this package's calculations, embedded at build time (the git commit is
# added when available). PERF-1.1: period-scoped P&L / fees, independent fee
# reconciliation, risk contribution, cash account, exact signal links, sizing effect.
CALCULATION_VERSION = "PERF-1.1"
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


def _calc_version():
    c = _commit()
    return f"{CALCULATION_VERSION}+{c}" if c else CALCULATION_VERSION


def build(conn, owner, portfolio: str = "PAPER", start=None, end=None, benchmark: str | None = None,
          opts: dict | None = None, sync_first: bool = True, strategy: str | None = None) -> dict:
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
    if not D.has_prices(conn, bsym):
        raise ValueError(f"benchmark {bsym} has no prices" + (" (run python -m data.total_return)"
                                                               if bsym.endswith("_TR") else " in prices_daily"))
    imported = L.sync(conn, owner) if sync_first else None
    prices = D.Prices(conn, start - timedelta(days=40), end)
    rf = cfg["risk_free_pct"]
    sectors = _sectors()

    strategy = C.text(strategy, "strategy", 80, required=False) or None
    actual = E.run_actual(conn, owner, pf, start, end, prices, bsym, rf, sectors, strategy)
    mdl = MD.build(conn, start, end, prices, opts or {})
    cal = mdl["calendar"]
    bench_ret = []
    prev = None
    before = D.last_date(conn, bsym, before=start)
    if before:
        prev = prices.close(bsym, D._d(before), count_gap=False)
    for d in cal:
        c = prices.close(bsym, d, count_gap=False)
        bench_ret.append(c / prev - 1 if (c and prev) else None)
        prev = c or prev
    m_metrics = M.series_metrics(cal, mdl["model"]["series"], bench_ret, rf)
    e_metrics = M.series_metrics(cal, mdl["executable"]["series"], bench_ret, rf)
    b_metrics = M.series_metrics(cal, bench_ret, None, rf)

    # signal attribution against the chosen portfolio
    fc = {}
    raw = E._txns(conn, owner, pf, end, strategy)
    txns = E._basis(conn, raw, fc)
    links = MD.link_signals(conn, owner, pf, mdl["model"]["trades"], txns, prices, int(mdl["options"]["link_window"]),
                            float(mdl["options"]["notional"]))
    outc = MD.outcomes(conn, [x["signal_id"] for x in links["linked"]])
    for x in links["linked"]:
        x["momentum_outcomes"] = outc.get(x["signal_id"], [])
    sig_pnl = sum(x["actual"]["pnl"] for x in links["linked"])
    act_ok = actual.get("status") == "OK"
    # the period's P&L (value-based gain), the same scope as the linked positions, which all
    # open inside the period (W39: this used to be lifetime realized + unrealized)
    total_pnl = actual["returns"]["gain"] if act_ok else None

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
        row("EXECUTABLE", "same signals at next-session open, " + ("square-root market-impact slippage"
            if mdl["options"].get("slippage_model") == "impact" else "slippage") + ", NSE costs, liquidity cap, "
            "equal weight",
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
    last_px = D.last_date(conn, bsym)
    syms = sorted({t["symbol"] for t in txns if t["symbol"]} | {t["symbol"] for t in mdl["model"]["trades"]} | {bsym})
    px_inputs = _price_inputs(conn, syms, start - timedelta(days=40), end)
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
        "strategy": strategy,
        "audit": {"methodology_version": METHODOLOGY_VERSION, "calculation_version": _calc_version(),
                  "generated_at": str(C.now()),
                  "assumptions": {**mdl["options"], "risk_free_pct": rf, "cost_model": cmodel,
                                  "basis": "today's share basis (corporate_actions.entry_factor)",
                                  "flows": "start of session; positions sleeve (cash not modelled)"},
                  "inputs": {"ledger_rows": len(ledger_rows), "ledger_sha256": C.digest(sorted(ledger_rows)),
                             "ledger_content_sha256": _rows_digest(raw, _LEDGER_FIELDS),
                             "signals": len(signal_ids), "signals_sha256": C.digest(sorted(signal_ids)),
                             "signals_content_sha256": _signals_digest(conn, signal_ids),
                             "prices_rows": px_inputs["rows"], "prices_sha256": px_inputs["sha256"],
                             "last_price_date": px_inputs["last"],
                             "benchmark_last_price_date": str(last_px)[:10] if last_px else None,
                             "basis_fallbacks": fc.get(D.FALLBACK_KEY, [])[:50],
                             "ledger_import": imported},
                  "options": {"opts": opts or {}, "strategy": strategy, "benchmark": benchmark}},
        "disclaimer": C.DISCLAIMER,
    }


_LEDGER_FIELDS = ("txn_id", "portfolio", "source", "source_ref", "trade_date", "ts", "kind", "symbol", "quantity",
                  "price", "gross_value", "fees", "reference_price", "price_quality", "strategy_id", "signal_ref")


def _rows_digest(rows, fields) -> str:
    return C.digest([[r.get(f) for f in fields] for r in rows])


def _signals_digest(conn, ids) -> str | None:
    if not ids:
        return C.digest([])
    out = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out += [list(r) for r in conn.execute(
            f"SELECT id, signal_date, symbol, `signal`, entry_price FROM signal_log WHERE id IN "
            f"({','.join('?' * len(part))})", part)]
    return C.digest(sorted(out, key=lambda r: str(r[0])))


def _price_inputs(conn, syms, start, end) -> dict:
    """The price rows the report read (close / open / volume of every symbol involved,
    from 40 days before the period): their count, sha256 and each series' last date. A
    corporate-action re-adjustment or a late bar changes the hash, so a stored report's
    inputs can be told apart from today's (verify())."""
    rows, last = [], {}
    for s in syms:
        rs = D.rows(conn, s, start, end)           # *_TR benchmarks: index_total_return (W40)
        rows += [[s, str(r[0])[:10], r[1], r[2], r[3]] for r in rs]
        last[s] = str(rs[-1][0])[:10] if rs else None
    return {"rows": len(rows), "sha256": C.digest(rows), "last": last}


def verify(conn, owner, rid) -> dict:
    """W39 (PERF-001-12): rebuild a stored report from the current database with the same
    portfolio, period, benchmark and options, and compare: every input hash and every
    figure of the four-return comparison. match=True means today's data reproduce it."""
    old = get(conn, owner, rid)
    o = (old.get("audit") or {}).get("options") or {}
    new = build(conn, owner, old["portfolio"], old["period"]["start"], old["period"]["end"],
                o.get("benchmark") or old["benchmark"], o.get("opts") or {}, sync_first=False,
                strategy=o.get("strategy"))
    diffs = []
    oi, ni = old["audit"]["inputs"], new["audit"]["inputs"]
    for k in ("ledger_rows", "ledger_sha256", "ledger_content_sha256", "signals", "signals_sha256",
              "signals_content_sha256", "prices_rows", "prices_sha256"):
        if k in oi and oi.get(k) != ni.get(k):
            diffs.append({"field": f"inputs.{k}", "stored": oi.get(k), "now": ni.get(k)})
    for a, b in zip(old["comparison"], new["comparison"]):
        for k, v in a.items():
            if k == "definition":
                continue
            w = b.get(k)
            if isinstance(v, (int, float)) and isinstance(w, (int, float)):
                if abs(v - w) > 1e-6:
                    diffs.append({"field": f"{a['return']}.{k}", "stored": v, "now": w})
            elif v != w:
                diffs.append({"field": f"{a['return']}.{k}", "stored": v, "now": w})
    return {"report_id": rid, "match": not diffs, "differences": diffs,
            "stored_calculation_version": old["audit"].get("calculation_version"),
            "current_calculation_version": new["audit"]["calculation_version"],
            "checked_at": str(C.now()),
            "note": "inputs changed: a ledger entry, signal or price bar differs from when the report was saved"
            if any(d["field"].startswith("inputs.") for d in diffs) else
            ("same inputs, different figures: the calculation changed" if diffs else "reproduced exactly")}


def _diff(a, b):
    x, y = a.get("total_return_pct"), b.get("total_return_pct")
    return round(x - y, 3) if x is not None and y is not None else None


def run(conn, owner, portfolio="PAPER", start=None, end=None, benchmark=None, opts=None, actor="owner",
        strategy=None) -> dict:
    rep = build(conn, owner, portfolio, start, end, benchmark, opts, strategy=strategy)
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
    """(content, media_type). JSON: the report exactly as stored / shown. CSV and HTML: every
    section of the page -- the comparison, the gaps, the actual summary, positions, round
    trips, costs (with components and the statutory estimate), contribution by symbol /
    sector / asset class, risk contribution, the cash account, data quality, model and
    executable trades, signal attribution and the audit block -- rendered from ONE list of
    sections (_sections) with the numbers copied from the report, never recomputed, so both
    match the page and each other. HTML (W39, PERF-001-13) is a self-contained, print-styled
    page: open it and print / "Save as PDF" for the downloadable PDF report."""
    if fmt == "json":
        return C.dumps(rep), "application/json"
    if fmt not in ("csv", "html"):
        raise ValueError("format must be csv, json or html")
    secs = _sections(rep)
    return (_csv(rep, secs), "text/csv") if fmt == "csv" else (_html(rep, secs), "text/html")


def _header(rep) -> list:
    return ["ATIP performance report", rep.get("report_id", "(not stored)"), rep["portfolio"],
            rep["period"]["start"], rep["period"]["end"], "benchmark", rep["benchmark"],
            "strategy", rep.get("strategy") or "(all)"]


def _sections(rep) -> list:
    """[("table", title, cols, rows) | ("kv", title, [(key, value)])] in page order."""
    out = []

    def block(title, cols, rows):
        out.append(("table", title, cols, [[r.get(c) for c in cols] for r in rows]))

    def kv(title, d):
        out.append(("kv", title, list(d.items())))

    block("comparison", ["return", "definition", "total_return_pct", "annualized_pct", "xirr_pct", "volatility_pct",
                         "sharpe", "sortino", "max_drawdown_pct", "alpha_annual_pct", "beta", "days_invested",
                         "trades", "win_rate_pct"], rep["comparison"])
    kv("gaps (percentage points)", rep["gaps"])
    act = rep["actual"]
    if act.get("status") == "OK":
        R = act["returns"]
        kv("actual summary", {"twr_total_pct": R["twr"].get("total_return_pct"),
                              "twr_annualized_pct": R["twr"].get("annualized_pct"), "xirr_pct": R["xirr_pct"],
                              "gain_in_period": R["gain"], "return_on_avg_capital_pct": R["return_on_avg_capital_pct"],
                              "average_invested_capital": R["average_invested_capital"],
                              "realized_pnl_lifetime_to_end": R["realized_pnl"],
                              "realized_pnl_period": R.get("realized_pnl_period"),
                              "dividends_period": R.get("dividends_period"), "unrealized_pnl": R["unrealized_pnl"],
                              "pme_benchmark": R["pme"]["benchmark"], "pme_xirr_pct": R["pme"]["xirr_pct"],
                              "pme_value_if_invested": R["pme"]["value_if_invested"],
                              "opening_value": act["period"]["opening_value"],
                              "closing_value": act["period"]["closing_value"], "net_flows": act["period"]["net_flows"],
                              "pnl_scope": R.get("pnl_scope")})
        block("actual positions", ["symbol", "quantity", "avg_cost", "cost_basis", "price", "market_value",
                                   "unrealized", "realized", "fees", "dividends"], act["positions"])
        block("actual round trips (FIFO)", ["symbol", "entry_date", "exit_date", "days", "quantity", "entry", "exit",
                                            "pnl"], act["trades"])
        K = act["costs"]
        kv("costs", {"fees_total_period": K["fees_total"], "fees_total_lifetime": K.get("fees_total_lifetime"),
                     **{f"fees_{k}": v for k, v in K["by_kind"].items()},
                     **{f"component_{k}": v for k, v in (K.get("components") or {}).items()},
                     "components_cover_pct": K.get("components_cover_pct"),
                     **{f"estimate_{k}": v for k, v in ((K.get("statutory_estimate") or {}).get("by_component")
                                                        or {}).items()},
                     "slippage_vs_reference": K["slippage_vs_reference"],
                     "trades_with_reference_price": K["trades_with_reference_price"],
                     "reconciled": K["reconciliation"]["ok"], "ledger_fees": K["reconciliation"]["ledger_fees"],
                     "engine_fees": K["reconciliation"]["engine_fees"]})
        CB = act["contribution"]
        block("contribution by symbol", ["symbol", "sector", "asset_class", "gain", "contribution_pct"],
              CB["by_symbol"])
        block("contribution by sector", ["sector", "gain", "contribution_pct"], CB["by_sector"])
        block("contribution by asset class", ["asset_class", "gain", "contribution_pct"], CB["by_asset_class"])
        RC = act.get("risk_contribution") or {}
        if RC.get("status") == "OK":
            block(f"risk contribution (portfolio volatility {RC['portfolio_vol_pct']}%)",
                  ["symbol", "risk_share_pct", "vol_contribution_pct", "standalone_vol_pct"], RC["by_symbol"])
        AC = act.get("account") or {}
        if AC.get("twr"):
            kv("cash account (positions + cash)", {k: v for k, v in AC.items() if k not in ("twr",)}
               | {"twr_total_pct": AC["twr"].get("total_return_pct")})
        block("data quality", ["issue", "symbol", "txn_id", "detail"], act["data_quality"])
    block("model trades", ["signal_id", "symbol", "signal_date", "entry", "exit_date", "exit", "exit_reason", "ret",
                           "pnl"], rep["model"]["trades"])
    block("executable trades", ["signal_id", "symbol", "status", "reason", "entry_date", "entry", "exit_date", "exit",
                                "slippage_bps", "exit_slippage_bps", "slippage_source", "costs", "pnl", "ret",
                                "liquidity", "exit_liquidity"], rep["executable"]["trades"])
    S = rep["signal_attribution"]
    kv("signal attribution summary", {"signal_driven_pnl": S["signal_driven_pnl"],
                                      "discretionary_pnl": S["discretionary_pnl"], "total_pnl": S["total_pnl"],
                                      "linked_signals": len(S["linked"]), "discretionary_buys": S["discretionary_buys"],
                                      "source": S["source"]})
    rows = []
    for x in S["linked"]:
        a = x.get("position_attribution") or {}
        vn = x.get("attribution_vs_model_notional") or {}
        rows.append([x["signal_id"], x["symbol"], x["signal_date"], x.get("link_method"), x["entry"]["date"],
                     x["entry"]["price"], x["entry"]["delay_sessions"], len(x.get("add_ons") or []),
                     len(x.get("exits") or []), x.get("open_quantity"), x["model"]["ret_pct"], x["actual"]["pnl"],
                     a.get("entry_timing"), a.get("averaging"), a.get("exit_timing"), a.get("costs"), vn.get("sizing")])
    out.append(("table", "signal attribution",
                ["signal_id", "symbol", "signal_date", "link_method", "entry_date", "entry_price", "delay_sessions",
                 "add_ons", "exits", "open_quantity", "model_ret_pct", "actual_pnl", "entry_timing", "averaging",
                 "exit_timing", "costs", "sizing_vs_model_notional"], rows))
    kv("audit", rep["audit"])
    return out


def _csv(rep, secs) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_header(rep))
    for sec in secs:
        w.writerow([])
        w.writerow([f"# {sec[1]}"])
        if sec[0] == "table":
            w.writerow(sec[2])
            for r in sec[3]:
                w.writerow([_cell(v) for v in r])
        else:
            for k, v in sec[2]:
                w.writerow([k, _cell(v)])
    return buf.getvalue()


_HTML_CSS = """body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:11px;color:#111;margin:18px}
h1{font-size:17px;margin:0 0 4px}h2{font-size:12.5px;margin:16px 0 4px;border-bottom:1px solid #999;
text-transform:capitalize}.meta{color:#444;margin-bottom:8px}.disc{color:#555;font-size:10px;margin-top:16px}
table{border-collapse:collapse;width:100%;margin:2px 0}th,td{border:1px solid #ccc;padding:2px 4px;text-align:left;
vertical-align:top;overflow-wrap:break-word}th{background:#eee}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.kv td:first-child{width:28%;color:#333}.noprint{margin:8px 0}
@media print{.noprint{display:none}body{margin:0}h2{break-after:avoid}tr{break-inside:avoid}}
@page{size:A4 landscape;margin:12mm}"""


def _html(rep, secs) -> str:
    import html as H

    def cell(v):
        v = _cell(v)
        if v is None:
            return "<td>&mdash;</td>"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f'<td class="n">{v:,}</td>' if isinstance(v, int) else f'<td class="n">{v:,.4g}</td>' \
                if abs(v) >= 1e6 or (v and abs(v) < 1e-3) else f'<td class="n">{round(v, 4):,}</td>'
        return f"<td>{H.escape(str(v))}</td>"
    h = _header(rep)
    parts = [f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>ATIP performance report "
             f"{H.escape(str(h[1]))}</title><style>{_HTML_CSS}</style></head><body>",
             '<div class="noprint"><button onclick="window.print()">Print / Save as PDF</button></div>',
             "<h1>ATIP performance report</h1>",
             f'<div class="meta">{H.escape(str(h[2]))} &middot; {H.escape(str(h[3]))} &rarr; {H.escape(str(h[4]))} '
             f"&middot; benchmark {H.escape(str(h[6]))} &middot; strategy {H.escape(str(h[8]))} &middot; report "
             f"{H.escape(str(h[1]))} &middot; methodology {H.escape(str(rep['audit'].get('methodology_version')))} "
             f"&middot; calculation {H.escape(str(rep['audit'].get('calculation_version')))}</div>"]
    for sec in secs:
        parts.append(f"<h2>{H.escape(sec[1])}</h2>")
        if sec[0] == "table":
            if not sec[3]:
                parts.append("<p>none</p>")
                continue
            parts.append("<table><thead><tr>" + "".join(f"<th>{H.escape(c.replace('_', ' '))}</th>" for c in sec[2]) +
                         "</tr></thead><tbody>" + "".join("<tr>" + "".join(cell(v) for v in r) + "</tr>"
                                                         for r in sec[3]) + "</tbody></table>")
        else:
            parts.append('<table class="kv"><tbody>' + "".join(f"<tr><td>{H.escape(str(k))}</td>{cell(v)}</tr>"
                                                              for k, v in sec[2]) + "</tbody></table>")
    parts.append(f'<p class="disc">{H.escape(str(rep.get("disclaimer") or C.DISCLAIMER))}</p></body></html>')
    return "".join(parts)


def _cell(v):
    return C.dumps(v) if isinstance(v, (dict, list)) else v
