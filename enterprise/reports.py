"""
Report rendering and scheduling (W9, ENT-12). W7 stored report DEFINITIONS only.

render(conn, report, fmt)  -> {"content", "rows", "format"} for fmt html | csv | json;
    the HTML is print-ready (browser "Save as PDF"; no PDF library is installed).
    Every output carries the disclaimer. Data is always the report's TENANT's own:
        portfolio            the tenant's paper book (owner: the W1 paper book)
        pnl                  daily equity history (tenant_pnl_daily / owner: pnl_daily PAPER)
        strategy_performance per strategy of the tenant: positions, value, realised / unrealised P&L
        risk_exposure        the book's positions with weight % of equity
        factor_ranking       latest quant factor scores (shared market data); params factor, top
        backtest_summary     the tenant's latest backtest runs with headline metrics
        ml_summary           the tenant's models with active version and metrics
        quant_summary        the tenant's quant experiments
        custom               params {"sections": [kinds...]} -> the listed sections in one report
run(conn, report_id, fmt, actor) stores the output (enterprise_report_output).
Schedules: enterprise_report.schedule = daily | weekly (Mondays) | monthly (1st);
run_due(conn) renders due reports in their formats and notifies the owner of the report
(category "report", through the user's channels -- SANDBOX by default).
"""

from __future__ import annotations

import csv
import html
import io
import json
import uuid
from datetime import date, datetime

DISCLAIMER = ("For information only. Not financial advice. ATIP is not SEBI registered. Paper-trading figures are "
              "simulated.")


def _default(tenant):
    from execution.tenant_books import is_default
    return is_default(tenant)


def _book(conn, tenant):
    from execution import tenant_books as TB
    from execution.positions import book
    return book(conn) if _default(tenant) else TB.book(conn, tenant)


def _rows(conn, q, *a):
    return [dict(r) for r in conn.execute(q, a)]


def section(conn, kind, tenant, params) -> list:
    if kind == "portfolio":
        b = _book(conn, tenant)
        return [{k: p.get(k) for k in ("symbol", "qty", "avg_price", "mark", "value", "unrealised")}
                for p in b["positions"]] + [{"symbol": "CASH", "value": b.get("cash")},
                                            {"symbol": "EQUITY", "value": b.get("equity")}]
    if kind == "pnl":
        if _default(tenant):
            return _rows(conn, "SELECT date, equity, day_pnl, drawdown_pct FROM pnl_daily WHERE env='PAPER' ORDER BY "
                               "date DESC LIMIT 90")
        return _rows(conn, "SELECT date, equity, day_pnl, drawdown_pct FROM tenant_pnl_daily WHERE tenant_id=? ORDER "
                           "BY date DESC LIMIT 90", tenant)
    if kind == "strategy_performance":
        from execution.positions import strategy_positions
        out = []
        for s in _rows(conn, "SELECT strategy_id, name, status FROM strategy WHERE COALESCE(tenant_id,'default')=? "
                             "ORDER BY strategy_id", tenant):
            sp = strategy_positions(conn, s["strategy_id"])
            out.append({**s, "open_positions": sum(1 for p in sp if p["quantity"]),
                        "value": round(sum(p["value"] or 0 for p in sp), 2),
                        "realised": round(sum(p["realised"] for p in sp), 2),
                        "unrealised": round(sum(p["unrealised"] for p in sp), 2),
                        "pnl": round(sum(p["pnl"] for p in sp), 2)})
        return out
    if kind == "risk_exposure":
        b = _book(conn, tenant)
        eq = b.get("equity") or 0
        return [{"symbol": p["symbol"], "value": p.get("value"),
                 "weight_pct": round((p.get("value") or 0) / eq * 100, 2) if eq else None} for p in b["positions"]]
    if kind == "factor_ranking":
        f = params.get("factor")
        top = int(params.get("top") or 25)
        q = "SELECT as_of, symbol, factor_key, score, `rank` FROM quant_factor_score WHERE as_of=(SELECT MAX(as_of) FROM " \
            "quant_factor_score)"
        a = []
        if f:
            q += " AND factor_key=?"; a.append(f)
        return _rows(conn, q + " ORDER BY rank LIMIT ?", *a, top)
    if kind == "backtest_summary":
        out = []
        for r in _rows(conn, "SELECT run_id, strategy_id, kind, start_date, end_date, status, metrics_json, created_at "
                             "FROM backtest_run WHERE COALESCE(tenant_id,'default')=? ORDER BY created_at DESC LIMIT 20",
                       tenant):
            m = json.loads(r.pop("metrics_json") or "{}")
            out.append({**r, **{k: m.get(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown")}})
        return out
    if kind == "ml_summary":
        return _rows(conn, "SELECT m.model_id, m.name, m.status, m.active_version, v.metrics_json FROM ml_model m LEFT "
                           "JOIN ml_model_version v ON v.model_id=m.model_id AND v.version=m.active_version WHERE "
                           "COALESCE(m.tenant_id,'default')=? ORDER BY m.model_id", tenant)
    if kind == "quant_summary":
        return _rows(conn, "SELECT experiment_id, name, version, status, created_at FROM quant_experiment WHERE "
                           "COALESCE(tenant_id,'default')=? ORDER BY created_at DESC LIMIT 50", tenant)
    if kind == "custom":
        out = []
        for k in params.get("sections") or []:
            if k == "custom":
                continue
            out += [{"section": k, **r} for r in section(conn, k, tenant, params)]
        return out
    raise ValueError(f"unknown report kind {kind}")


def render(conn, report: dict, fmt="html") -> dict:
    params = report.get("params") or json.loads(report.get("params_json") or "{}")
    rows = section(conn, report["kind"], report["tenant_id"], params)
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    if fmt == "json":
        content = json.dumps({"report": report["name"], "kind": report["kind"], "generated_at":
                              datetime.now().isoformat(timespec="seconds"), "disclaimer": DISCLAIMER, "rows": rows},
                             default=str)
    elif fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols or ["empty"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        content = buf.getvalue() + f"\n# {DISCLAIMER}\n"
    elif fmt == "html":
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{html.escape('' if r.get(c) is None else str(r.get(c)))}</td>"
                                        for c in cols) + "</tr>" for r in rows)
        content = (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(report['name'])}</title>"
                   "<style>body{font:13px system-ui,sans-serif;margin:24px;color:#111}table{border-collapse:collapse;"
                   "width:100%}th,td{border:1px solid #ccc;padding:4px 6px;text-align:left}th{background:#f3f3f3}"
                   ".d{color:#666;font-size:11px;margin-top:16px}@media print{body{margin:0}}</style></head><body>"
                   f"<h2>{html.escape(report['name'])}</h2><div>{html.escape(report['kind'])} · tenant "
                   f"{html.escape(report['tenant_id'])} · generated {datetime.now():%Y-%m-%d %H:%M}</div>"
                   f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
                   f"<p class='d'>{html.escape(DISCLAIMER)}</p></body></html>")
    else:
        raise ValueError("format must be html, csv or json")
    return {"format": fmt, "rows": len(rows), "content": content}


def run(conn, report_id, fmt="html", actor=None) -> dict:
    r = conn.execute("SELECT * FROM enterprise_report WHERE report_id=?", (report_id,)).fetchone()
    if not r:
        raise ValueError(f"no report {report_id}")
    rep = dict(r)
    out = render(conn, rep, fmt)
    oid = "rpo_" + uuid.uuid4().hex[:14]
    conn.execute("INSERT INTO enterprise_report_output (output_id,report_id,tenant_id,user_id,format,content,`rows`,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?)", (oid, report_id, rep["tenant_id"], actor or rep["user_id"], fmt,
                                                          out["content"], out["rows"], datetime.now()))
    conn.execute("UPDATE enterprise_report SET last_run_at=? WHERE report_id=?", (datetime.now(), report_id))
    conn.commit()
    return {"output_id": oid, "report_id": report_id, "format": fmt, "rows": out["rows"]}


def output(conn, output_id) -> dict | None:
    r = conn.execute("SELECT * FROM enterprise_report_output WHERE output_id=?", (output_id,)).fetchone()
    return dict(r) if r else None


def set_schedule(conn, report_id, tenant_id, user_id, schedule, formats=None) -> dict:
    if schedule not in (None, "", "daily", "weekly", "monthly"):
        raise ValueError("schedule must be daily, weekly, monthly or empty")
    fmts = formats or ["html"]
    if set(fmts) - {"html", "csv", "json"}:
        raise ValueError("formats: html, csv, json")
    n = conn.execute("UPDATE enterprise_report SET schedule=?, formats_json=? WHERE report_id=? AND tenant_id=? AND "
                     "user_id=?", (schedule or None, json.dumps(fmts), report_id, tenant_id, user_id)).rowcount
    if not n:
        raise ValueError(f"no report {report_id} owned by you")
    conn.commit()
    return {"report_id": report_id, "schedule": schedule or None, "formats": fmts}


def _due(sched, last, today) -> bool:
    if last and str(last)[:10] == str(today):
        return False
    if sched == "daily":
        return True
    if sched == "weekly":
        return today.weekday() == 0
    if sched == "monthly":
        return today.day == 1
    return False


def run_due(conn, today=None) -> dict:
    from enterprise.notifications import notify
    today = today or date.today()
    n = 0
    for r in [dict(x) for x in conn.execute("SELECT * FROM enterprise_report WHERE schedule IS NOT NULL")]:
        if not _due(r["schedule"], r.get("last_run_at"), today):
            continue
        outs = []
        for fmt in json.loads(r.get("formats_json") or '["html"]'):
            try:
                outs.append(run(conn, r["report_id"], fmt, actor="scheduler"))
            except Exception as e:
                outs.append({"format": fmt, "error": str(e)})
        notify(conn, r["tenant_id"], "report", f"Report ready: {r['name']}",
               "; ".join(f"{o['format']}: /api/reports/{r['report_id']}/outputs/{o['output_id']}" if o.get("output_id")
                         else f"{o['format']} failed: {o.get('error')}" for o in outs), user_ids=[r["user_id"]])
        n += 1
    return {"reports_run": n}
