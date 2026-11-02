"""
Session reports (RPT-01..07, 2 Nov 2026): periodic and on-demand reports of the sessions' money, with schedules in
IST and an archive. It follows enterprise/reports.py's design -- build the rows (section), render a format, run and
store, run_due on a schedule -- for the owner's own sessions, without needing enterprise sign-in.

    kind                    content                                                         default schedule (IST)
    daily_session_summary   per session: cycles opened / closed, gross, charges, tax (est.),  trading days 17:30, after
                            actual, floor, cash, open trade                                   the 16:45 post-market run
    weekly_performance      sessions compared: win rate, gross vs actual, best / worst cycle, Monday 08:00 (last week)
                            equity curves
    monthly_pnl             gross -> charges by type -> tax -> actual, by session and total;  1st of the month 08:00
                            the charges register                                              (last month)
    quarterly_review        per session: performance, drawdown, rule changes (session_events)  quarter start 08:00 (FY
                                                                                              quarters: Q1 = Apr-Jun)
    fy_tax_statement        gains by tax head, set-offs, LTCG exemption, estimated liability,  1 Apr 08:00 (last FY)
                            every cycle (CA-ready)
    session_cycle_book      every cycle of a session: the full gross / charges / tax / actual  on demand
                            table
    custom                  the sections, sessions and dates the owner picks                   owner-defined

Formats: html (print-ready), csv, xlsx (openpyxl), pdf, json. PDF engines, first that works (config.json "reports":
{"pdf_engine": "auto" | "weasyprint" | "chrome" | "builtin", "chrome_path": ...}): WeasyPrint when installed, a headless
Chrome / Chromium print of the HTML when a browser is found, else enterprise/pdf_writer.py (pure Python, always there). Every report carries the platform disclaimer and the
tax disclaimer, and every money figure its env (VIRTUAL = paper, ACTUAL = broker); totals are per env, never added.

Schedules (report_schedules): daily (trading days) | weekly (weekday) | monthly (day) | quarterly | fy | cron (five
fields, IST), each at time_ist. run_due() is called every 15 minutes (06:00-20:45) by the scheduler (pipeline/w39_jobs.py): a run
missed while the Mac slept is caught up later the same day. The archive (report_archive) keeps every file. A
delivered report goes to the owner's inbox and channels through enterprise/notifications.py. On the view-only web
server (ops/role.py) nothing is scheduled, generated or sent: it shows the Mac's archive.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from accounting import cycles as C
from accounting import tax as TX

log = logging.getLogger("atip.reports")

CONFIG_PATH = Path("atip_data") / "config.json"
try:
    from enterprise.reports import DISCLAIMER as _BASE
except Exception:                                  # pragma: no cover
    _BASE = "For information only. Not financial advice. ATIP is not SEBI registered. Paper-trading figures are simulated."
DISCLAIMER = f"{_BASE} {TX.DISCLAIMER}"
FORMATS = ("html", "csv", "xlsx", "pdf", "json")
FREQS = ("daily", "weekly", "monthly", "quarterly", "fy", "cron")
MIME = {"html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8", "json": "application/json",
        "pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
KINDS = {
    "daily_session_summary": {"label": "Daily session summary", "about": "Per session today: cycles opened and closed, "
                              "gross, charges, tax (est.), actual P&L, floor, cash and the open trade.",
                              "schedule": {"freq": "daily", "time_ist": "17:30"}},
    "weekly_performance": {"label": "Weekly performance", "about": "All sessions compared over the week: win rate, "
                           "gross vs actual P&L, best and worst cycles, equity curves.",
                           "schedule": {"freq": "weekly", "weekday": 0, "time_ist": "08:00"}},
    "monthly_pnl": {"label": "Monthly P&L statement", "about": "Gross P&L, then each charge, then estimated tax, to "
                    "actual P&L, by session and in total; the month's charges register.",
                    "schedule": {"freq": "monthly", "day": 1, "time_ist": "08:00"}},
    "quarterly_review": {"label": "Quarterly review", "about": "Each session's performance and drawdown over the FY "
                         "quarter, and every rule change made in it.",
                         "schedule": {"freq": "quarterly", "time_ist": "08:00"}},
    "fy_tax_statement": {"label": "FY tax statement", "about": "Realised gains by tax head, set-offs, the LTCG "
                         "exemption and the estimated liability for Apr-Mar, with every cycle listed for your CA.",
                         "schedule": {"freq": "fy", "time_ist": "08:00"}},
    "session_cycle_book": {"label": "Session cycle book", "about": "Every cycle of a session with gross P&L, each "
                           "charge, estimated tax and actual P&L.", "schedule": None},
    "custom": {"label": "Custom report", "about": "The sections, sessions and dates you choose.", "schedule": None},
}
CUSTOM_SECTIONS = {"summary": "Session summary", "performance": "Performance", "pnl": "P&L statement",
                   "charges": "Charges register", "tax": "Tax", "cycles": "Cycle book", "events": "Rule changes"}
DEFAULT_FORMATS = ["html", "csv", "xlsx", "pdf"]


def _now() -> datetime:
    try:
        from pipeline.operating_schedule import now_ist
        return now_ist().replace(tzinfo=None)
    except Exception:
        return datetime.now()


def _row(r) -> dict:
    return dict(r) if hasattr(r, "keys") else r


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("reports") or {}
    except Exception:
        return {}


# -- numbers --------------------------------------------------------------------------------------

def inr(v, sign: bool = False, symbol: bool = True) -> str:
    """Indian grouping, paise: 125000 -> ₹1,25,000.00; sign=True puts + / − on P&L."""
    if v is None or v == "":
        return "—"
    v = float(v)
    neg = v < -0.004
    whole, frac = f"{abs(v):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        whole = f"{head},{tail}"
    pre = "−" if neg else ("+" if sign and v > 0.004 else "")
    return f"{pre}{'₹' if symbol else ''}{whole}.{frac}"


def _pct(v, sign=True) -> str:
    if v is None:
        return "—"
    return f"{'+' if sign and v > 0 else '−' if v < 0 else ''}{abs(float(v)):.2f}%"


def col(key: str, label: str, kind: str = "text") -> dict:
    """kind: text | money | pnl (signed money) | pct (signed) | rate (unsigned %) | int | date."""
    return {"key": key, "label": label, "kind": kind, "num": kind in ("money", "pnl", "pct", "rate", "int")}


def _fmt_rows(columns: list, rows: list) -> None:
    for r in rows:
        f = {}
        for c in columns:
            v = r.get(c["key"])
            if c["kind"] == "money":
                f[c["key"]] = inr(v)
            elif c["kind"] == "pnl":
                f[c["key"]] = inr(v, sign=True)
            elif c["kind"] == "pct":
                f[c["key"]] = _pct(v)
            elif c["kind"] == "rate":
                f[c["key"]] = _pct(v, sign=False)
            elif c["kind"] == "int":
                f[c["key"]] = "—" if v is None else f"{int(v):,}"
            else:
                f[c["key"]] = "—" if v in (None, "") else str(v)
        r["_fmt"] = f


def _env_totals(rows: list, keys: list, label_key: str, label="Total") -> list:
    """One total row per env present -- VIRTUAL and ACTUAL are never added together."""
    out = []
    for env in sorted({r.get("env") or C.VIRTUAL for r in rows}):
        mine = [r for r in rows if (r.get("env") or C.VIRTUAL) == env]
        t = {k: round(sum(float(r.get(k) or 0) for r in mine), 2) for k in keys}
        t[label_key] = f"{label} {env}"
        t["env"] = env
        out.append(t)
    return out


def section(heading: str, columns: list, rows: list, totals: list | None = None, note: str = "",
            chart: dict | None = None, kv: list | None = None) -> dict:
    _fmt_rows(columns, rows)
    _fmt_rows(columns, totals or [])
    return {"heading": heading, "columns": columns, "rows": rows, "totals": totals or [], "note": note,
            "chart": chart, "kv": kv}


# -- periods --------------------------------------------------------------------------------------

def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def fy_quarter(d: date) -> tuple[str, date, date]:
    """('FY 2026-27 Q3', 2026-10-01, 2026-12-31) for any day of Oct-Dec 2026."""
    fy = TX.fy_of(d)
    lo, _ = TX.fy_bounds(fy)
    q = ((d.month - 4) % 12) // 3 + 1
    start = date(lo.year + (1 if q == 4 else 0), [4, 7, 10, 1][q - 1], 1)
    end_m = start.month + 2
    end = (date(start.year + (end_m > 12), (end_m - 1) % 12 + 1, 1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return f"{fy} Q{q}", start, end


def period(kind: str, params: dict | None = None, on: date | None = None, scheduled: bool = False) -> dict:
    """The dates a report covers. On a schedule, the last COMPLETE period (the daily summary: that day)."""
    p = params or {}
    on = on or _now().date()
    if p.get("from") and p.get("to"):
        lo, hi = _d(p["from"]), _d(p["to"])
        if hi < lo:
            raise ValueError("The end date is before the start date.")
        return {"from": lo.isoformat(), "to": hi.isoformat(), "label": f"{lo:%d %b %Y} – {hi:%d %b %Y}"}
    if kind == "daily_session_summary":
        d = _d(p.get("date") or on)
        return {"from": d.isoformat(), "to": d.isoformat(), "label": f"{d:%a %d %b %Y}"}
    if kind == "weekly_performance":
        end = _d(p["date"]) if p.get("date") else (on - timedelta(days=on.weekday() + 1) if scheduled else on)
        lo = end - timedelta(days=end.weekday()) if not scheduled else end - timedelta(days=6)
        return {"from": lo.isoformat(), "to": end.isoformat(), "label": f"Week {lo:%d %b} – {end:%d %b %Y}"}
    if kind == "monthly_pnl":
        if p.get("month"):
            y, m = map(int, str(p["month"]).split("-")[:2])
            lo = date(y, m, 1)
        else:
            lo = (on.replace(day=1) - timedelta(days=1)).replace(day=1) if scheduled else on.replace(day=1)
        hi = (lo + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return {"from": lo.isoformat(), "to": min(hi, on).isoformat() if not scheduled else hi.isoformat(),
                "label": f"{lo:%B %Y}"}
    if kind == "quarterly_review":
        ref = on - timedelta(days=1) if scheduled else on
        if p.get("quarter"):
            m = re.match(r"FY (\d{4})-\d{2} Q([1-4])", str(p["quarter"]))
            if not m:
                raise ValueError("Quarter must look like FY 2026-27 Q3.")
            y, q = int(m.group(1)), int(m.group(2))
            ref = date(y + (1 if q == 4 else 0), [4, 7, 10, 1][q - 1], 15)
        label, lo, hi = fy_quarter(ref)
        return {"from": lo.isoformat(), "to": hi.isoformat(), "label": label}
    if kind == "fy_tax_statement":
        fy = p.get("fy") or TX.fy_of(on - timedelta(days=1) if scheduled else on)
        lo, hi = TX.fy_bounds(fy)
        return {"from": lo.isoformat(), "to": hi.isoformat(), "label": fy, "fy": fy}
    # session_cycle_book / custom without dates: the pools' whole history to today
    return {"from": "2000-01-01", "to": on.isoformat(), "label": f"to {on:%d %b %Y}"}


# -- data -----------------------------------------------------------------------------------------

def _closed(conn, frm: str, to: str, sids=None) -> list:
    return [r for r in C.stored(conn) if r.get("closed_at") and frm <= str(r["closed_at"])[:10] <= to
            and (not sids or r["session_id"] in sids)]


def _sids(params: dict) -> list | None:
    s = params.get("sessions") or params.get("sid")
    if not s or s in ("ALL", ["ALL"]):
        return None
    return [str(x).upper() for x in (s if isinstance(s, list) else str(s).split(","))]


def _env_now() -> str:
    try:
        from orders.environment import PAPER, broker_env
        return C.VIRTUAL if broker_env() == PAPER else C.ACTUAL
    except Exception:
        return C.VIRTUAL


MONEY = ["gross_pnl", "charges", "tax_est", "actual_pnl"]


def _summary_section(conn, ov, per, sids) -> dict:
    cyc = _closed(conn, per["from"], per["to"], sids)
    rows = []
    for s in ov["sessions"]:
        if sids and s["id"] not in sids:
            continue
        t = s["tiles"] or {}
        mine = [c for c in cyc if c["session_id"] == s["id"]]
        tot = C.totals(mine)
        opened = sum(1 for c in C.stored(conn, s["id"]) if per["from"] <= str(c["opened_at"])[:10] <= per["to"]) + \
            sum(1 for o in (s["open"] or []) if per["from"] <= str(o["since"])[:10] <= per["to"])
        rows.append({"session": f"{s['id']} {s['name']}", "state": f"{s['status']} · {s['mode']}", "opened": opened,
                     "closed": len(mine), **{k: tot[k] for k in MONEY}, "floor": t.get("floor"), "cash": t.get("cash"),
                     "open_trade": ", ".join(f"{o['symbol']} ×{o['qty']:g} ({inr(o['unrealised'], True)})"
                                             for o in (s["open"] or [])) or "—",
                     "waiting": t.get("waiting") or "", "env": (mine[0]["env"] if mine else _env_now())})
    cols = [col("session", "Session"), col("state", "Status"), col("opened", "Cycles opened", "int"),
            col("closed", "Cycles closed", "int"), col("gross_pnl", "Gross P&L", "pnl"),
            col("charges", "Charges", "money"), col("tax_est", "Tax (est.)", "money"),
            col("actual_pnl", "Actual P&L", "pnl"), col("floor", "Floor", "money"), col("cash", "Cash", "money"),
            col("open_trade", "Open trade"), col("waiting", "Note"), col("env", "Env")]
    return section(f"Sessions · {per['label']}", cols, rows, _env_totals(rows, MONEY + ["cash"], "session"),
                   note="Gross P&L is before any cost; actual P&L is after charges and estimated tax.")


def _performance_section(conn, ov, per, sids, charts=True) -> list:
    cyc = _closed(conn, per["from"], per["to"], sids)
    rows, series = [], []
    for s in ov["sessions"]:
        if sids and s["id"] not in sids or not s["tiles"]:
            continue
        mine = [c for c in cyc if c["session_id"] == s["id"]]
        tot = C.totals(mine)
        best = max(mine, key=lambda c: c["actual_pnl"], default=None)
        worst = min(mine, key=lambda c: c["actual_pnl"], default=None)
        rows.append({"session": f"{s['id']} {s['name']}", "cycles": len(mine),
                     "win_rate": round(tot["wins"] / len(mine) * 100, 1) if mine else None, **{k: tot[k] for k in MONEY},
                     "since_start": s["tiles"]["actual_pnl"],
                     "best": best and f"{best['symbol']} {inr(best['actual_pnl'], True)}",
                     "worst": worst and f"{worst['symbol']} {inr(worst['actual_pnl'], True)}",
                     "env": mine[0]["env"] if mine else _env_now()})
        if charts:
            b = C.book(conn, s["id"], do_sync=False)
            series.append({"id": s["id"], "name": s["name"], "points": b["charts"]["equity"]})
    cols = [col("session", "Session"), col("cycles", "Cycles closed", "int"), col("win_rate", "Win rate", "rate"),
            col("gross_pnl", "Gross P&L", "pnl"), col("charges", "Charges", "money"), col("tax_est", "Tax (est.)", "money"),
            col("actual_pnl", "Actual P&L", "pnl"), col("since_start", "Actual since start", "pnl"),
            col("best", "Best cycle"), col("worst", "Worst cycle"), col("env", "Env")]
    out = [section(f"Performance · {per['label']}", cols, rows, _env_totals(rows, MONEY + ["since_start"], "session"))]
    if charts and series:
        out.append({"heading": "Equity curves (gross and actual, since each pool started)", "columns": [], "rows": [],
                    "totals": [], "note": "One small chart per session on its own scale; the table above has the "
                    "figures.", "chart": {"type": "equity_multiples", "series": series}, "kv": None})
    return out


def _pnl_sections(conn, per, sids) -> list:
    cyc = _closed(conn, per["from"], per["to"], sids)
    rows = []
    for sid in sorted({c["session_id"] for c in cyc}, key=lambda x: int(str(x).lstrip("S") or 0)):
        for env in sorted({c["env"] for c in cyc if c["session_id"] == sid}):
            mine = [c for c in cyc if c["session_id"] == sid and c["env"] == env]
            t = C.totals(mine)
            rows.append({"session": sid, "cycles": len(mine), "gross_pnl": t["gross_pnl"],
                         **{k: t["charges_by_type"][k] for k in (*C.CHARGE_TYPES, "other")}, "charges": t["charges"],
                         "net_pnl": t["net_pnl"], "tax_est": t["tax_est"], "actual_pnl": t["actual_pnl"], "env": env})
    keys = ["gross_pnl", *C.CHARGE_TYPES, "other", "charges", "net_pnl", "tax_est", "actual_pnl"]
    cols = [col("session", "Session"), col("cycles", "Cycles", "int"), col("gross_pnl", "Gross P&L", "pnl"),
            *(col(k, C.CHARGE_LABEL[k], "money") for k in (*C.CHARGE_TYPES, "other")),
            col("charges", "Charges", "money"), col("net_pnl", "Net before tax", "pnl"),
            col("tax_est", "Tax (est.)", "money"), col("actual_pnl", "Actual P&L", "pnl"), col("env", "Env")]
    totals = _env_totals(rows, keys + ["cycles"], "session")
    out = [section(f"P&L statement · {per['label']}", cols, rows, totals,
                   note="Each row: gross P&L, less each charge, is net before tax; less estimated tax, actual P&L.")]
    for t in totals:
        kv = [("Gross P&L (Total P&L)", inr(t["gross_pnl"], True))]
        kv += [(f"  − {C.CHARGE_LABEL[k]}", inr(t[k])) for k in (*C.CHARGE_TYPES, "other") if t.get(k)]
        kv += [("Net P&L before tax", inr(t["net_pnl"], True)), ("  − Tax (est.)", inr(t["tax_est"])),
               ("Actual P&L (net after charges & tax)", inr(t["actual_pnl"], True))]
        out.append({"heading": f"From gross to actual · {t['env']}", "columns": [], "rows": [], "totals": [],
                    "note": "", "chart": {"type": "waterfall", "steps": _waterfall(t)}, "kv": kv})
    return out


def _waterfall(t: dict) -> list:
    steps = [{"label": "Gross", "value": t["gross_pnl"], "kind": "total"}]
    steps += [{"label": C.CHARGE_LABEL[k], "value": -t[k], "kind": "charge"} for k in (*C.CHARGE_TYPES, "other")
              if t.get(k)]
    steps += [{"label": "Tax (est.)", "value": -t["tax_est"], "kind": "tax"},
              {"label": "Actual", "value": t["actual_pnl"], "kind": "total"}]
    return steps


def _charges_section(conn, per, sids) -> dict:
    cyc = _closed(conn, per["from"], per["to"], sids)
    rows = []
    for k in (*C.CHARGE_TYPES, "other"):
        for env in sorted({c["env"] for c in cyc}):
            amt = round(sum(float(c.get(f"ch_{k}") or 0) for c in cyc if c["env"] == env), 2)
            if amt:
                rows.append({"type": C.CHARGE_LABEL[k], "amount": amt, "cycles": sum(
                    1 for c in cyc if c["env"] == env and c.get(f"ch_{k}")), "env": env})
    cols = [col("type", "Charge"), col("cycles", "Cycles", "int"), col("amount", "Amount", "money"), col("env", "Env")]
    return section(f"Charges register · {per['label']}", cols, rows, _env_totals(rows, ["amount"], "type"),
                   note="ATIP's figures: the paper fill's own charges, itemised with the cost model it used. Compare "
                        "them with your broker's contract notes on Accounting → Reconciliation.")


def _cycle_columns() -> list:
    return [col("session_id", "Session"), col("n", "#", "int"), col("symbol", "Symbol"), col("opened", "Opened", "date"),
            col("closed", "Closed", "date"), col("holding_days", "Days", "int"),
            col("start_amount", "Start amount", "money"), col("buy_value", "Buy value", "money"),
            col("sell_value", "Sell value", "money"), col("gross_pnl", "Gross P&L", "pnl"),
            *(col(f"ch_{k}", C.CHARGE_LABEL[k], "money") for k in (*C.CHARGE_TYPES, "other")),
            col("charges", "Charges", "money"), col("net_pnl", "Net before tax", "pnl"),
            col("tax_est", "Tax (est.)", "money"), col("tax_head", "Head"), col("actual_pnl", "Actual P&L", "pnl"),
            col("end_amount", "End amount", "money"), col("gross_pct", "Gross %", "pct"),
            col("actual_pct", "Actual %", "pct"), col("exit_reason", "Exit"), col("floor_at_start", "Floor", "money"),
            col("env", "Env")]


def _cycle_rows(rows: list) -> list:
    out = []
    for r in rows:
        st = float(r.get("start_amount") or 0)
        out.append({**{k: r.get(k) for k in r}, "opened": str(r.get("opened_at") or "")[:10],
                    "closed": str(r.get("closed_at") or "")[:10],
                    "gross_pct": round(float(r["gross_pnl"]) / st * 100, 2) if st else None,
                    "actual_pct": round(float(r["actual_pnl"]) / st * 100, 2) if st else None})
    return out


def _cycles_sections(conn, per, sids) -> list:
    out = []
    rows = _closed(conn, per["from"], per["to"], sids)
    keys = ["buy_value", "sell_value", "gross_pnl", *(f"ch_{k}" for k in (*C.CHARGE_TYPES, "other")), "charges",
            "net_pnl", "tax_est", "actual_pnl"]
    for sid in sorted({r["session_id"] for r in rows}, key=lambda x: int(str(x).lstrip("S") or 0)) or (sids or []):
        mine = _cycle_rows([r for r in rows if r["session_id"] == sid])
        out.append(section(f"Cycle book · session {sid}", _cycle_columns(), mine,
                           _env_totals(mine, keys, "session_id"),
                           note="Tax per cycle is provisional (est.); the FY tax statement sets losses off."))
    if not out:
        out.append(section("Cycle book", _cycle_columns(), [], note="No cycle closed in this period."))
    return out


def _tax_sections(conn, fy: str, sids) -> list:
    rows = [r for r in C.stored(conn) if not sids or r["session_id"] in sids]
    st = TX.fy_statement(rows, fy)
    heads = [{**h, "env": "VIRTUAL+ACTUAL" if False else _env_of(rows)} for h in st["heads"]]
    cols = [col("label", "Head"), col("gains", "Gains", "money"), col("losses", "Losses", "money"),
            col("net", "Net", "pnl"), col("after_set_off", "After set-off", "pnl"), col("taxable", "Taxable", "money"),
            col("rate_pct", "Rate", "rate"), col("tax", "Tax (est.)", "money"), col("env", "Env")]
    out = [section(f"Gains by tax head · {fy} ({st['from']} to {st['to']})", cols, heads,
                   note=f"{st['cycles']} closed cycles. Rates from config.json \"tax\" "
                        f"(effective {st['rates']['effective_from']}). {TX.DISCLAIMER}")]
    kv = [(f"Set-off: {s['from']} loss against {s['against']}", f"{inr(s['amount'])} — {s['rule']}")
          for s in st["set_offs"]]
    kv += [("LTCG exemption used (once a year, all sessions)", inr(st["ltcg_exemption_used"])),
           ("Tax before surcharge and cess", inr(st["tax_before_addons"])),
           (f"Surcharge ({st['rates']['surcharge_pct']:g}%)", inr(st["surcharge"])),
           (f"Health & education cess ({st['rates']['cess_pct']:g}%)", inr(st["cess"])),
           ("Surcharge and cess in the liability", "yes" if st["addons_applied"] else "no (turn on in Settings → Tax)"),
           ("Estimated liability", inr(st["liability"])),
           ("Set aside per cycle during the year (provisional)", inr(st["provisional_set_aside"])),
           ("Provisional minus liability", inr(st["difference"], True))]
    kv += [(f"Carried forward: {h} loss", inr(v)) for h, v in st["carried_forward"].items()]
    out.append({"heading": "Set-offs and liability", "columns": [], "rows": [], "totals": [], "note": "", "chart": None,
                "kv": kv})
    lo, hi = TX.fy_bounds(fy)
    cyc = _cycle_rows([r for r in rows if r.get("closed_at") and lo.isoformat() <= str(r["closed_at"])[:10] <= hi.isoformat()])
    ca = [col("session_id", "Session"), col("symbol", "Symbol"), col("qty", "Qty", "int"), col("opened", "Bought on"),
          col("closed", "Sold on"), col("holding_days", "Days held", "int"), col("buy_value", "Buy value", "money"),
          col("sell_value", "Sell value", "money"), col("charges", "Charges", "money"), col("ch_stt", "of which STT", "money"),
          col("net_pnl", "Net P&L", "pnl"), col("tax_head", "Head"), col("tax_est", "Tax (est.)", "money"), col("env", "Env")]
    out.append(section("Every cycle of the year (for your CA)", ca, cyc,
                       _env_totals(cyc, ["buy_value", "sell_value", "charges", "ch_stt", "net_pnl", "tax_est"], "session_id")))
    return out


def _env_of(rows) -> str:
    envs = {r.get("env") for r in rows if r.get("env")}
    return "/".join(sorted(envs)) if envs else _env_now()


def _events_section(conn, per, sids) -> dict:
    from orders import sessions as S
    rows = []
    for e in S.events(conn, None, 500):
        if e.get("event") == "LINK" or not (per["from"] <= str(e["at"])[:10] <= per["to"]):
            continue
        if sids and e.get("session_id") not in sids:
            continue
        rows.append({"at": str(e["at"]).replace("T", " ")[:16], "session": e.get("session_id") or "all",
                     "event": e["event"], "detail": str(e.get("detail") or "")[:160]})
    cols = [col("at", "When (IST)"), col("session", "Session"), col("event", "Change"), col("detail", "Detail")]
    return section(f"Rule changes · {per['label']}", cols, rows,
                   note="From the sessions' activity log (session_events): setup, mode, status, pools, transfers.")


def _review_sections(conn, ov, per, sids) -> list:
    cyc = _closed(conn, per["from"], per["to"], sids)
    rows = []
    for s in ov["sessions"]:
        if sids and s["id"] not in sids or not s["tiles"]:
            continue
        mine = sorted([c for c in cyc if c["session_id"] == s["id"]], key=lambda c: c["n"])
        tot = C.totals(mine)
        eq, peak, dd = 0.0, 0.0, 0.0
        base = float(mine[0]["start_amount"]) if mine else 0.0
        for c in mine:
            eq += float(c["actual_pnl"])
            peak = max(peak, eq)
            dd = min(dd, (eq - peak) / (base + peak) * 100 if base + peak else 0)
        rows.append({"session": f"{s['id']} {s['name']}", "strategy": s.get("strategy") or "", "cycles": len(mine),
                     "win_rate": round(tot["wins"] / len(mine) * 100, 1) if mine else None,
                     **{k: tot[k] for k in MONEY}, "max_dd": round(dd, 2) if mine else None,
                     "env": mine[0]["env"] if mine else _env_now()})
    cols = [col("session", "Session"), col("strategy", "Strategy"), col("cycles", "Cycles", "int"),
            col("win_rate", "Win rate", "rate"), col("gross_pnl", "Gross P&L", "pnl"), col("charges", "Charges", "money"),
            col("tax_est", "Tax (est.)", "money"), col("actual_pnl", "Actual P&L", "pnl"),
            col("max_dd", "Max drawdown", "pct"), col("env", "Env")]
    return [section(f"Strategy performance · {per['label']}", cols, rows, _env_totals(rows, MONEY, "session")),
            _events_section(conn, per, sids)]


def build(conn, kind: str, params: dict | None = None, on: date | None = None, scheduled: bool = False) -> dict:
    """The report document: title, period, sections (tables, key / value lines, charts), disclaimer."""
    if kind not in KINDS:
        raise ValueError(f"Unknown report: {kind}. Pick one of: {', '.join(KINDS)}.")
    params = dict(params or {})
    per = period(kind, params, on, scheduled)
    sids = _sids(params)
    ov = C.overview(conn)
    secs = []
    if kind == "daily_session_summary":
        secs = [_summary_section(conn, ov, per, sids)]
    elif kind == "weekly_performance":
        secs = _performance_section(conn, ov, per, sids)
    elif kind == "monthly_pnl":
        secs = _pnl_sections(conn, per, sids) + [_charges_section(conn, per, sids)]
    elif kind == "quarterly_review":
        secs = _review_sections(conn, ov, per, sids)
    elif kind == "fy_tax_statement":
        secs = _tax_sections(conn, per["fy"], sids)
    elif kind == "session_cycle_book":
        if not sids:
            sids = [s["id"] for s in ov["sessions"] if s["tiles"]][:1] or ["S1"]
            params["sessions"] = sids
        secs = _cycles_sections(conn, per, sids)
    elif kind == "custom":
        chosen = [s for s in (params.get("sections") or ["summary", "pnl", "charges"]) if s in CUSTOM_SECTIONS]
        if not chosen:
            raise ValueError("Pick at least one section for the custom report.")
        for s in chosen:
            secs += {"summary": lambda: [_summary_section(conn, ov, per, sids)],
                     "performance": lambda: _performance_section(conn, ov, per, sids),
                     "pnl": lambda: _pnl_sections(conn, per, sids), "charges": lambda: [_charges_section(conn, per, sids)],
                     "tax": lambda: _tax_sections(conn, TX.fy_of(_d(per["to"])), sids),
                     "cycles": lambda: _cycles_sections(conn, per, sids),
                     "events": lambda: [_events_section(conn, per, sids)]}[s]()
    title = params.get("title") or KINDS[kind]["label"]
    who = "all sessions" if not sids else ", ".join(sids)
    return {"kind": kind, "title": f"ATIP · {title}", "period": per, "params": params,
            "subtitle": f"{per['label']} · {who} · generated {_now():%d %b %Y %H:%M} IST · money in ₹, Indian "
                        f"grouping · VIRTUAL = paper, ACTUAL = broker (never added together)",
            "generated_at": _now().isoformat(timespec="seconds"), "sections": secs, "disclaimer": DISCLAIMER,
            "rows": sum(len(s.get("rows") or []) for s in secs)}


# -- formats --------------------------------------------------------------------------------------

REPORT_CSS = """
:root{--ink:#101828;--ink2:#475467;--line:#D9DEE5;--head:#F0F2F5;--pos:#067647;--neg:#B42318;--s1:#2a78d6;--s2:#eb6834}
*{box-sizing:border-box}body{font:12px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);margin:28px;background:#fff}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:22px 0 6px}.sub,.note{color:var(--ink2)}.note{font-size:11px;margin:0 0 6px}
table{border-collapse:collapse;width:100%;margin:4px 0 8px}th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;white-space:nowrap}
th{background:var(--head);font-weight:600;position:sticky;top:0}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr.t td{font-weight:700;background:#FAFAFB}.pos{color:var(--pos)}.neg{color:var(--neg)}.scroll{overflow-x:auto}
dl{display:grid;grid-template-columns:minmax(220px,max-content) auto;gap:3px 18px;margin:4px 0}dt{color:var(--ink2)}dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums}
.mult{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}.mult figure{margin:0;border:1px solid var(--line);border-radius:8px;padding:8px}
figcaption{font-size:11px;color:var(--ink2);margin-bottom:4px}.legend{display:flex;gap:14px;font-size:11px;color:var(--ink2);margin:4px 0}
.sw{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:5px}.disc{margin-top:22px;font-size:10.5px;color:var(--ink2);border-top:1px solid var(--line);padding-top:8px}
@media print{body{margin:10mm}th{position:static}h2{break-after:avoid}tr{break-inside:avoid}.scroll{overflow:visible}}
@page{size:A4 landscape;margin:10mm}
"""


def _svg_equity(series: dict) -> str:
    pts = series["points"]
    w, h, pad = 220, 90, 4
    vals = [p[k] for p in pts for k in ("gross", "actual")]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = max(1, len(pts) - 1)

    def path(k):
        return " ".join(f"{'M' if i == 0 else 'L'}{pad + i * (w - 2 * pad) / n:.1f},"
                        f"{h - pad - (p[k] - lo) / span * (h - 2 * pad):.1f}" for i, p in enumerate(pts))
    last = pts[-1]
    tip = html.escape(f"{series['id']}: gross {inr(last['gross'])}, actual {inr(last['actual'])} after cycle {last['n']}")
    return (f"<figure><figcaption>{html.escape(series['id'] + ' ' + series['name'])} · actual "
            f"{inr(last['actual'])}</figcaption><svg viewBox='0 0 {w} {h}' width='100%' role='img' "
            f"aria-label='{tip}'><title>{tip}</title><line x1='0' y1='{h - pad}' x2='{w}' y2='{h - pad}' "
            f"stroke='#D9DEE5'/><path d='{path('gross')}' fill='none' stroke='var(--s2)' stroke-width='2'/>"
            f"<path d='{path('actual')}' fill='none' stroke='var(--s1)' stroke-width='2'/></svg></figure>")


def _svg_waterfall(steps: list) -> str:
    w, h, pad, bw = 640, 170, 22, 0
    run, bars = 0.0, []
    for s in steps:
        if s["kind"] == "total":
            bars.append((s, 0.0, s["value"]))
            run = s["value"]
        else:
            bars.append((s, run, run + s["value"]))
            run += s["value"]
    vals = [0.0] + [v for _, a, b in bars for v in (a, b)]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    bw = (w - 2 * pad) / max(1, len(bars))
    y = lambda v: pad + (hi - v) / span * (h - 2 * pad)
    g = [f"<line x1='{pad}' x2='{w - pad}' y1='{y(0):.1f}' y2='{y(0):.1f}' stroke='#98A2B3'/>"]
    for i, (s, a, b) in enumerate(bars):
        top, bot = y(max(a, b)), y(min(a, b))
        color = "var(--s1)" if s["kind"] == "total" else "var(--s2)"
        tip = html.escape(f"{s['label']}: {inr(s['value'], True)}")
        g.append(f"<g><title>{tip}</title><rect x='{pad + i * bw + 3:.1f}' y='{top:.1f}' width='{bw - 6:.1f}' "
                 f"height='{max(1.0, bot - top):.1f}' rx='2' fill='{color}'/>"
                 f"<text x='{pad + i * bw + bw / 2:.1f}' y='{h - 4}' font-size='9' text-anchor='middle' "
                 f"fill='#475467'>{html.escape(s['label'][:12])}</text></g>")
    return (f"<div class='legend'><span><i class='sw' style='background:var(--s1);height:8px'></i>Gross / actual "
            f"total</span><span><i class='sw' style='background:var(--s2);height:8px'></i>Charge or tax taken off"
            f"</span></div><svg viewBox='0 0 {w} {h}' width='100%' style='max-width:{w}px' role='img' "
            f"aria-label='Waterfall from gross P&L through charges and tax to actual P&L'>{''.join(g)}</svg>")


def to_html(doc: dict) -> str:
    out = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>{html.escape(doc['title'])}</title><style>{REPORT_CSS}"
           f"</style></head><body><h1>{html.escape(doc['title'])}</h1><div class='sub'>{html.escape(doc['subtitle'])}</div>"]
    for s in doc["sections"]:
        out.append(f"<h2>{html.escape(s['heading'])}</h2>")
        if s.get("note"):
            out.append(f"<p class='note'>{html.escape(s['note'])}</p>")
        ch = s.get("chart") or {}
        if ch.get("type") == "equity_multiples":
            out.append("<div class='legend'><span><i class='sw' style='background:var(--s2)'></i>Gross</span>"
                       "<span><i class='sw' style='background:var(--s1)'></i>Actual (after charges &amp; tax)</span></div>"
                       "<div class='mult'>" + "".join(_svg_equity(x) for x in ch["series"]) + "</div>")
        if ch.get("type") == "waterfall":
            out.append(_svg_waterfall(ch["steps"]))
        if s.get("kv"):
            out.append("<dl>" + "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in s["kv"]) + "</dl>")
        if s.get("columns"):
            head = "".join(f"<th class='{'n' if c['num'] else ''}' scope='col'>{html.escape(c['label'])}</th>"
                           for c in s["columns"])

            def tr(r, cls=""):
                tds = []
                for c in s["columns"]:
                    v = r["_fmt"][c["key"]]
                    raw = r.get(c["key"])
                    tone = ""
                    if c["kind"] == "pnl" and isinstance(raw, (int, float)):
                        tone = " pos" if raw > 0.004 else " neg" if raw < -0.004 else ""
                    tds.append(f"<td class='{'n' if c['num'] else ''}{tone}'>{html.escape(v)}</td>")
                return f"<tr class='{cls}'>{''.join(tds)}</tr>"
            body = "".join(tr(r) for r in s["rows"]) or \
                f"<tr><td colspan='{len(s['columns'])}' class='note'>Nothing in this period.</td></tr>"
            body += "".join(tr(t, "t") for t in s.get("totals") or [])
            out.append(f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
    out.append(f"<p class='disc'>{html.escape(doc['disclaimer'])}</p></body></html>")
    return "".join(out)


def to_csv(doc: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([doc["title"]])
    w.writerow([doc["subtitle"]])
    for s in doc["sections"]:
        w.writerow([])
        w.writerow([f"# {s['heading']}"])
        for k, v in s.get("kv") or []:
            w.writerow([k, v])
        if s.get("columns"):
            w.writerow([c["label"] for c in s["columns"]])
            for r in [*s["rows"], *(s.get("totals") or [])]:
                w.writerow(["" if r.get(c["key"]) is None else r.get(c["key"]) for c in s["columns"]])
    w.writerow([])
    w.writerow([f"# {doc['disclaimer']}"])
    return buf.getvalue()


def to_xlsx(doc: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append([doc["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([doc["subtitle"]])
    ws.append([doc["disclaimer"]])
    ws["A3"].alignment = Alignment(wrap_text=False)
    ws.append([])
    ws.append(["Sheet", "Section", "Rows"])
    names = set()
    money = '"₹"#,##0.00;[Red]-"₹"#,##0.00'
    for i, s in enumerate(doc["sections"], 1):
        base = re.sub(r"[\[\]\:\*\?\/\\]", "", s["heading"])[:28] or f"Section {i}"
        name = base
        while name in names:
            name = f"{base[:25]} {i}"
        names.add(name)
        ws.append([name, s["heading"], len(s.get("rows") or [])])
        sh = wb.create_sheet(name)
        sh.append([s["heading"]])
        sh["A1"].font = Font(bold=True, size=12)
        if s.get("note"):
            sh.append([s["note"]])
        for k, v in s.get("kv") or []:
            sh.append([k, v])
        if s.get("columns"):
            sh.append([])
            hdr = sh.max_row + 1
            sh.append([c["label"] for c in s["columns"]])
            for cell in sh[hdr]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="F0F2F5")
            for r in s["rows"]:
                sh.append([r.get(c["key"]) for c in s["columns"]])
            for t in s.get("totals") or []:
                sh.append([t.get(c["key"]) for c in s["columns"]])
                for cell in sh[sh.max_row]:
                    cell.font = Font(bold=True)
            for j, c in enumerate(s["columns"], 1):
                letter = get_column_letter(j)
                sh.column_dimensions[letter].width = max(10, min(32, len(c["label"]) + 4))
                if c["kind"] in ("money", "pnl"):
                    for row in sh.iter_rows(min_row=hdr + 1, min_col=j, max_col=j):
                        row[0].number_format = money
                elif c["kind"] in ("pct", "rate"):
                    for row in sh.iter_rows(min_row=hdr + 1, min_col=j, max_col=j):
                        row[0].number_format = '0.00"%"'
            sh.freeze_panes = sh.cell(row=hdr + 1, column=1)
            if s["rows"]:
                sh.auto_filter.ref = f"A{hdr}:{get_column_letter(len(s['columns']))}{hdr + len(s['rows'])}"
        sh.append([])
        sh.append([doc["disclaimer"]])
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 60
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def chrome_path() -> str | None:
    cfg = _config()
    for p in [cfg.get("chrome_path"), os.environ.get("ATIP_CHROMIUM"),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome")]:
        if p and Path(p).exists():
            return p
    return None


def _chrome_pdf(html_text: str, exe: str) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="atip-pdf-") as tmp:
        src, out = Path(tmp) / "report.html", Path(tmp) / "report.pdf"
        src.write_text(html_text, encoding="utf-8")
        try:
            subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                            f"--user-data-dir={tmp}/profile", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                            src.as_uri()], capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning(f"  Chrome PDF print failed: {e}")
            return None
        data = out.read_bytes() if out.exists() else b""
        return data if data.startswith(b"%PDF") else None


def to_pdf(doc: dict) -> tuple[bytes, str]:
    """(pdf bytes, engine): Chrome's print of the HTML report when it works, else the built-in writer."""
    engine = str(_config().get("pdf_engine") or "auto").lower()
    if engine in ("auto", "weasyprint"):
        try:                                       # optional (requirements.txt): needs Pango on macOS
            from weasyprint import HTML
            return HTML(string=to_html(doc)).write_pdf(), "weasyprint"
        except Exception:
            pass
    if engine in ("auto", "chrome"):
        exe = chrome_path()
        if exe:
            data = _chrome_pdf(to_html(doc), exe)
            if data:
                return data, "chrome"
    from enterprise.pdf_writer import render
    return render(doc), "builtin"


def render(doc: dict, fmt: str) -> tuple[bytes, str]:
    """(content, engine) for one format."""
    if fmt == "html":
        return to_html(doc).encode("utf-8"), "html"
    if fmt == "csv":
        return to_csv(doc).encode("utf-8"), "csv"
    if fmt == "json":
        clean = {**doc, "sections": [{k: v for k, v in s.items()} for s in doc["sections"]]}
        for s in clean["sections"]:
            s["rows"] = [{k: v for k, v in r.items() if k != "_fmt"} for r in s.get("rows") or []]
            s["totals"] = [{k: v for k, v in r.items() if k != "_fmt"} for r in s.get("totals") or []]
        return json.dumps(clean, default=str).encode("utf-8"), "json"
    if fmt == "xlsx":
        return to_xlsx(doc), "openpyxl"
    if fmt == "pdf":
        return to_pdf(doc)
    raise ValueError(f"Format must be one of {', '.join(FORMATS)}.")


# -- storage, schedules, archive ------------------------------------------------------------------

def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            formats_json TEXT NOT NULL DEFAULT '["html"]',
            freq        TEXT NOT NULL,
            time_ist    TEXT NOT NULL DEFAULT '08:00',
            weekday     INTEGER,
            day         INTEGER,
            cron        TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            deliver     INTEGER NOT NULL DEFAULT 1,
            last_run_at TEXT,
            last_period TEXT,
            last_error  TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_archive (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id  INTEGER,
            kind         TEXT NOT NULL,
            name         TEXT NOT NULL,
            format       TEXT NOT NULL,
            period_from  TEXT,
            period_to    TEXT,
            period_label TEXT,
            env          TEXT,
            engine       TEXT,
            `rows`       INTEGER,
            size         INTEGER,
            content      BLOB,
            created_at   TEXT NOT NULL,
            created_by   TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_archive_created ON report_archive(created_at)")
    conn.commit()


def seed(conn) -> int:
    """The default schedules (one per periodic kind) the first time."""
    ensure_tables(conn)
    if conn.execute("SELECT COUNT(*) FROM report_schedules").fetchone()[0]:
        return 0
    n = 0
    for kind, k in KINDS.items():
        if k["schedule"]:
            save_schedule(conn, {"name": k["label"], "kind": kind, "formats": DEFAULT_FORMATS, **k["schedule"]})
            n += 1
    return n


def _hhmm(v) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(v or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Time must be HH:MM in IST, like 08:00.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _cron_field(f: str, v: int, lo: int, hi: int) -> bool:
    for part in f.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-", 1))
        else:
            a = b = int(part)
        if a <= v <= b and (v - a) % step == 0:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Five-field cron (minute hour day-of-month month day-of-week; Sunday = 0), in IST."""
    f = str(expr or "").split()
    if len(f) != 5:
        raise ValueError("Cron needs five fields: minute hour day month weekday, e.g. 30 17 * * 1-5.")
    try:
        return (_cron_field(f[0], dt.minute, 0, 59) and _cron_field(f[1], dt.hour, 0, 23)
                and _cron_field(f[2], dt.day, 1, 31) and _cron_field(f[3], dt.month, 1, 12)
                and _cron_field(f[4], (dt.weekday() + 1) % 7, 0, 6))
    except ValueError:
        raise ValueError(f"Cron {expr!r} is not valid: use numbers, *, ranges (1-5), lists (1,3) and steps (*/15).")


def save_schedule(conn, b: dict, sid: int | None = None) -> dict:
    ensure_tables(conn)
    kind = b.get("kind")
    if kind not in KINDS:
        raise ValueError(f"Pick a report: {', '.join(KINDS)}.")
    freq = str(b.get("freq") or "").lower()
    if freq not in FREQS:
        raise ValueError("How often: daily, weekly, monthly, quarterly, fy or cron.")
    fmts = [str(f).lower() for f in (b.get("formats") or ["html"])]
    bad = [f for f in fmts if f not in FORMATS]
    if bad or not fmts:
        raise ValueError(f"Formats must be from {', '.join(FORMATS)}.")
    t = _hhmm(b.get("time_ist") or "08:00")
    wd = b.get("weekday")
    day = b.get("day")
    if freq == "weekly":
        wd = 0 if wd in (None, "") else int(wd)
        if not 0 <= wd <= 6:
            raise ValueError("Weekday must be 0 (Monday) to 6 (Sunday).")
    if freq == "monthly":
        day = 1 if day in (None, "") else int(day)
        if not 1 <= day <= 28:
            raise ValueError("Day of the month must be 1 to 28, so it happens every month.")
    cron = str(b.get("cron") or "").strip() or None
    if freq == "cron":
        cron_matches(cron or "", _now())
    params = b.get("params") or {}
    if isinstance(params, str):
        params = json.loads(params or "{}")
    name = str(b.get("name") or KINDS[kind]["label"]).strip()[:80]
    now = _now().isoformat(timespec="seconds")
    vals = (name, kind, json.dumps(params), json.dumps(fmts), freq, t, wd, day, cron,
            0 if str(b.get("enabled", True)).lower() in ("0", "false", "off") else 1,
            0 if str(b.get("deliver", True)).lower() in ("0", "false", "off") else 1, now)
    if sid:
        n = conn.execute("UPDATE report_schedules SET name=?, kind=?, params_json=?, formats_json=?, freq=?, time_ist=?, "
                         "weekday=?, day=?, cron=?, enabled=?, deliver=?, updated_at=? WHERE id=?", (*vals, sid)).rowcount
        if not n:
            raise LookupError(f"no schedule {sid}")
    else:
        sid = conn.execute("INSERT INTO report_schedules (name, kind, params_json, formats_json, freq, time_ist, weekday, "
                           "day, cron, enabled, deliver, updated_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (*vals, now)).lastrowid
    conn.commit()
    return get_schedule(conn, sid)


def get_schedule(conn, sid: int) -> dict:
    r = conn.execute("SELECT * FROM report_schedules WHERE id = ?", (int(sid),)).fetchone()
    if not r:
        raise LookupError(f"no schedule {sid}")
    return _schedule_out(_row(r))


def _schedule_out(r: dict) -> dict:
    r["params"] = json.loads(r.pop("params_json") or "{}")
    r["formats"] = json.loads(r.pop("formats_json") or "[]")
    r["when"] = describe(r)
    return r


def describe(r: dict) -> str:
    t = r.get("time_ist") or "08:00"
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {"daily": f"Every trading day at {t} IST",
            "weekly": f"Every {days[int(r.get('weekday') or 0)]} at {t} IST",
            "monthly": f"Day {int(r.get('day') or 1)} of every month at {t} IST",
            "quarterly": f"First day of each FY quarter (1 Apr, 1 Jul, 1 Oct, 1 Jan) at {t} IST",
            "fy": f"1 April at {t} IST, for the year just ended",
            "cron": f"Cron “{r.get('cron')}” (IST)"}.get(r.get("freq"), "")


def schedules(conn) -> list:
    seed(conn)
    return [_schedule_out(_row(r)) for r in conn.execute("SELECT * FROM report_schedules ORDER BY id").fetchall()]


def delete_schedule(conn, sid: int) -> dict:
    ensure_tables(conn)
    n = conn.execute("DELETE FROM report_schedules WHERE id = ?", (int(sid),)).rowcount
    conn.commit()
    if not n:
        raise LookupError(f"no schedule {sid}")
    return {"deleted": int(sid)}


def generate(conn, kind: str, params: dict | None = None, formats=None, schedule_id: int | None = None,
             by: str = "owner", on: date | None = None, scheduled: bool = False) -> dict:
    """Build a report once, render each format, keep every file in the archive."""
    ensure_tables(conn)
    fmts = [str(f).lower() for f in (formats or ["html"])]
    for f in fmts:
        if f not in FORMATS:
            raise ValueError(f"Formats must be from {', '.join(FORMATS)}.")
    doc = build(conn, kind, params, on, scheduled)
    env = _env_of(C.stored(conn)) if kind != "custom" else _env_now()
    out = []
    for f in fmts:
        data, engine = render(doc, f)
        aid = conn.execute("INSERT INTO report_archive (schedule_id, kind, name, format, period_from, period_to, "
                           "period_label, env, engine, `rows`, size, content, created_at, created_by) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (schedule_id, kind, doc["title"], f, doc["period"]["from"], doc["period"]["to"],
                            doc["period"]["label"], env, engine, doc["rows"], len(data), data,
                            _now().isoformat(timespec="seconds"), by)).lastrowid
        out.append({"id": aid, "format": f, "engine": engine, "size": len(data),
                    "url": f"/api/v2/reports/archive/{aid}/download"})
    conn.commit()
    return {"title": doc["title"], "period": doc["period"], "rows": doc["rows"], "files": out}


def archive(conn, kind: str | None = None, limit: int = 200) -> list:
    ensure_tables(conn)
    q, a = ("SELECT id, schedule_id, kind, name, format, period_from, period_to, period_label, env, engine, `rows`, size, "
            "created_at, created_by FROM report_archive"), []
    if kind:
        q += " WHERE kind = ?"; a.append(kind)
    return [_row(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (*a, int(limit))).fetchall()]


def archive_file(conn, aid: int) -> tuple[bytes, str, str]:
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM report_archive WHERE id = ?", (int(aid),)).fetchone()
    if not r:
        raise LookupError(f"no archived report {aid}")
    r = _row(r)
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"{r['name']}_{r['period_label'] or ''}").strip("_")
    data = r["content"]
    return (bytes(data) if not isinstance(data, bytes) else data), MIME[r["format"]], f"{name}.{r['format']}"


def _key(r: dict, now: datetime) -> str | None:
    """The period key a schedule is due for at `now` (IST), else None."""
    if now.strftime("%H:%M") < (r.get("time_ist") or "08:00") and r["freq"] != "cron":
        return None
    d = now.date()
    f = r["freq"]
    if f == "daily":
        try:
            from utils.trading_calendar import is_trading_day
            ok = is_trading_day(d)
        except Exception:
            ok = d.weekday() < 5
        return f"daily:{d}" if ok else None
    if f == "weekly":
        return f"weekly:{d}" if d.weekday() == int(r.get("weekday") or 0) else None
    if f == "monthly":
        return f"monthly:{d}" if d.day == int(r.get("day") or 1) else None
    if f == "quarterly":
        return f"quarterly:{d}" if d.day == 1 and d.month in (1, 4, 7, 10) else None
    if f == "fy":
        return f"fy:{d}" if (d.month, d.day) == (4, 1) else None
    if f == "cron":
        last = r.get("last_run_at")
        start = max(now - timedelta(minutes=15), datetime.fromisoformat(last) if last else now - timedelta(minutes=15))
        t = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while t <= now:
            if cron_matches(r["cron"], t):
                return f"cron:{t:%Y-%m-%dT%H:%M}"
            t += timedelta(minutes=1)
    return None


def run_due(conn, now: datetime | None = None) -> dict:
    """Generate every enabled schedule that is due, once per period, and deliver it. A no-op on a viewer."""
    try:
        from ops.role import is_viewer
        if is_viewer():
            return {"skipped": "view-only server: reports are generated on the Mac"}
    except Exception:
        pass
    now = now or _now()
    seed(conn)
    ran = []
    for r in schedules(conn):
        if not r["enabled"]:
            continue
        key = _key(r, now)
        if not key or key == r.get("last_period"):
            continue
        try:
            out = generate(conn, r["kind"], r["params"], r["formats"], schedule_id=r["id"], by="scheduler",
                           on=now.date(), scheduled=True)
            conn.execute("UPDATE report_schedules SET last_run_at = ?, last_period = ?, last_error = NULL WHERE id = ?",
                         (now.isoformat(timespec="seconds"), key, r["id"]))
            conn.commit()
            if r["deliver"]:
                _deliver(conn, r, out)
            ran.append({"schedule": r["id"], "kind": r["kind"], "files": len(out["files"])})
        except Exception as e:
            log.error(f"  report schedule {r['id']} ({r['kind']}) failed: {e}")
            conn.execute("UPDATE report_schedules SET last_error = ?, last_period = ? WHERE id = ?",
                         (str(e)[:300], key, r["id"]))
            conn.commit()
            ran.append({"schedule": r["id"], "kind": r["kind"], "error": str(e)})
    return {"ran": ran, "at": now.isoformat(timespec="seconds")}


def _deliver(conn, sched: dict, out: dict) -> None:
    """The owner's inbox and channels (enterprise/notifications.py), when enterprise users exist."""
    try:
        from enterprise.config import settings
        from enterprise.notifications import notify
        tenant = settings()["default_tenant"]
        body = "; ".join(f"{f['format'].upper()}: {f['url']}" for f in out["files"])
        notify(conn, tenant, "report", f"Report ready: {out['title']} ({out['period']['label']})", body)
    except Exception as e:
        log.info(f"  report delivery skipped ({e}); it is in the archive")


def tick() -> dict:
    """The scheduler's job (every 15 minutes, 06:00-20:45 IST): store newly closed cycles, then run due report schedules."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        try:
            from ops.role import is_viewer
            if is_viewer():
                return {"skipped": "viewer"}
        except Exception:
            pass
        synced = C.sync(conn)
        return {"cycles": synced, **run_due(conn)}
    finally:
        conn.close()
