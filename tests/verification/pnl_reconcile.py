"""
Independent P&L reconciliation of the ten cash-pool sessions (profit audit, 5 Nov 2026).

Recomputes realised P&L from the raw paper fills alone -- it imports nothing from orders/, portfolio/ or accounting/
-- and sets it beside every figure the app produces:

    fills        paper_order rows that filled (filled_qty > 0, fill_price set), session from the order tag ATIP-S<n>
    matching     FIFO lots per (session, symbol), from each session's pool_started_at; a sale realises
                 (sell - lot buy price) x matched qty, gross; its charges are the sale's own plus the matched share
                 of the lots' buy charges
    charges      re-derived here from the published NSE delivery rates (STT 0.1 % each side, NSE txn 0.00297 %,
                 SEBI Rs 10/crore, stamp 0.015 % on the buy, GST 18 % on brokerage + txn + SEBI, DP per sale) and
                 compared with what each fill recorded
    tax (est.)   s.196 (was s.111A): (net + STT) x 20 % x 1.04 cess when that is a gain (STT is not deductible, so a
                 net loss smaller than its STT is still a taxable gain), nothing otherwise; per closed trade
    day          the IST calendar day of the sale fill (fills are stamped by the Mac's clock, which runs on IST)

App figures it checks against (read-only):
    the snapshot DB    session_cycles (what Home / Accounting sum), ledger_entries (the general ledger),
                       order_rules (orders/sessions.py's rule-based books), pnl_daily (the paper book's realised_cum)
    --api DIR          JSON saved from the live dashboard: api_v2_periods, api_command_pnl_scope_pools_by_day,
                       api_sessions
    --dhan FILE        Dhan's trade book for the day (GET /v2/trades), saved by the caller

    python tests/verification/pnl_reconcile.py --db SNAPSHOT.db --date 2026-11-05 [--api DIR] [--dhan FILE] [--out DIR]

Read-only: opens the database with mode=ro and writes only its own report files to --out.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

# Published NSE cash-delivery charges (checked 5 Nov 2026 against NSE / SEBI / Indian Stamp Act notifications; Dhan
# charges no brokerage on delivery). DP is ATIP's configured Rs 15.93 per sale unless --dp says otherwise.
STT_PCT = 0.1
TXN_PCT = 0.00297
SEBI_PCT = 0.0001
STAMP_BUY_PCT = 0.015
GST_PCT = 18.0
STCG_PCT, CESS_PCT = 20.0, 4.0


def r2(v: float) -> float:
    return round(float(v) + (1e-9 if v >= 0 else -1e-9), 2)


def charges(side: str, value: float, dp: float) -> dict:
    stt = value * STT_PCT / 100
    txn = value * TXN_PCT / 100
    sebi = value * SEBI_PCT / 100
    stamp = value * STAMP_BUY_PCT / 100 if side == "BUY" else 0.0
    gst = (txn + sebi) * GST_PCT / 100
    d = dp if side == "SELL" else 0.0
    return {"stt": stt, "total": stt + txn + sebi + stamp + gst + d}


def tax_est(net: float, stt: float) -> float:
    gain = net + stt                                   # STT is not a cost of the transfer (s.72(3))
    return r2(gain * STCG_PCT / 100 * (1 + CESS_PCT / 100)) if gain > 0 else 0.0


def connect(db: str):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load(c) -> dict:
    sessions = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, name, start_capital, pool_started_at FROM trading_sessions WHERE start_capital > 0")}
    link = {r["rule_id"]: r["session_id"] for r in c.execute("SELECT rule_id, session_id FROM session_rules")}
    rules = [dict(r) for r in c.execute("SELECT * FROM order_rules")]
    fills = [dict(r) for r in c.execute(
        "SELECT order_id, created_at, symbol, transaction_type AS side, quantity, filled_qty, fill_price, brokerage, "
        "status, tag, fill_session FROM paper_order WHERE filled_qty > 0 AND fill_price IS NOT NULL")]
    for f in fills:
        f["at"] = f"{f['fill_session']}T09:15:00" if f.get("fill_session") else str(f["created_at"])
        tag = str(f.get("tag") or "")
        f["session"] = tag[len("ATIP-"):] if tag.startswith("ATIP-") else None
    fills.sort(key=lambda f: (f["at"], f["order_id"]))
    return {"sessions": sessions, "link": link, "rules": rules, "fills": fills}


def integrity(c, d: dict, dp: float) -> dict:
    """Attribution, duplicates, rule-vs-fill agreement, recorded-vs-statutory charges, NULLs."""
    by_id = {r["id"]: r for r in d["rules"]}
    sid_of_rule = lambda r: d["link"].get(r["id"]) or d["link"].get(r.get("parent_rule_id") or "")
    rule_of_order = defaultdict(list)
    for r in d["rules"]:
        if r.get("dhan_order_id"):
            rule_of_order[r["dhan_order_id"]].append(r)
    out = {"tag_vs_rule": [], "orphans": [], "dup_order_refs": [], "rule_vs_fill": [], "charge_diffs": [],
           "null_charges": [], "executed_null_qty": [], "pending_marked_executed": []}
    starts = {s: str(v["pool_started_at"] or "") for s, v in d["sessions"].items()}
    first_pool = min(starts.values()) if starts else ""
    for f in d["fills"]:
        rs = rule_of_order.get(f["order_id"], [])
        rule_sids = {sid_of_rule(r) for r in rs} - {None}
        if f["session"] or rule_sids:
            if rule_sids != ({f["session"]} if f["session"] else set()):
                out["tag_vs_rule"].append({"order_id": f["order_id"], "tag": f["session"], "rule_sessions": sorted(rule_sids)})
        elif f["at"] >= first_pool:
            out["orphans"].append({"order_id": f["order_id"], "symbol": f["symbol"], "at": f["at"], "tag": f.get("tag")})
        for r in rs:
            dq = float(r.get("execution_quantity") or 0) - float(f["filled_qty"])
            dpx = float(r.get("execution_price") or 0) - float(f["fill_price"])
            if abs(dq) > 0 or abs(dpx) >= 0.005:
                out["rule_vs_fill"].append({"order_id": f["order_id"], "rule": r["id"][:8], "symbol": f["symbol"],
                                            "session": sid_of_rule(r), "rule_px": r.get("execution_price"),
                                            "fill_px": f["fill_price"], "rule_qty": r.get("execution_quantity"),
                                            "fill_qty": f["filled_qty"]})
        if f["brokerage"] is None:
            out["null_charges"].append(f["order_id"])
        else:
            mine = r2(charges(f["side"], f["filled_qty"] * f["fill_price"], dp)["total"])
            if abs(mine - float(f["brokerage"])) >= 0.01:
                out["charge_diffs"].append({"order_id": f["order_id"], "recorded": f["brokerage"], "statutory": mine})
    for oid, rs in rule_of_order.items():
        if len(rs) > 1:
            out["dup_order_refs"].append({"order_id": oid, "rules": [r["id"][:8] for r in rs]})
    for r in d["rules"]:
        if r["status"] == "EXECUTED" and r.get("execution_price") and r.get("execution_quantity") is None:
            out["executed_null_qty"].append(r["id"][:8])
    po = {r["order_id"]: dict(r) for r in c.execute("SELECT order_id, status, filled_qty FROM paper_order")}
    for r in d["rules"]:
        p = po.get(r.get("dhan_order_id") or "")
        if r["status"] == "EXECUTED" and p and (p["status"] not in ("TRADED", "PARTIALLY_FILLED") or not p["filled_qty"]):
            out["pending_marked_executed"].append({"rule": r["id"][:8], "order_id": p["order_id"], "paper_status": p["status"]})
    out["counts"] = {"fills": len(d["fills"]), "session_fills": sum(1 for f in d["fills"] if f["session"]),
                     "paper_order_ids_unique": c.execute("SELECT COUNT(*) = COUNT(DISTINCT order_id) FROM paper_order")
                     .fetchone()[0] == 1,
                     "order_log_dup_ids": [r[0] for r in c.execute(
                         "SELECT dhan_order_id FROM order_log WHERE dhan_order_id IS NOT NULL GROUP BY dhan_order_id "
                         "HAVING COUNT(*) > 1")]}
    return out


def realised(d: dict, dp: float) -> list:
    """Closed trades (one row per sale) from FIFO lots per (session, symbol), each session from its pool start."""
    lots, rows = defaultdict(list), []
    for f in d["fills"]:
        sid = f["session"]
        s = d["sessions"].get(sid)
        if not s or f["at"] < str(s["pool_started_at"] or ""):
            continue
        q, px = int(f["filled_qty"]), float(f["fill_price"])
        ch = charges(f["side"], q * px, dp)
        if f["side"] == "BUY":
            lots[(sid, f["symbol"])].append({"qty": q, "px": px, "fee": ch["total"], "stt": ch["stt"], "q0": q,
                                             "at": f["at"]})
            continue
        left, gross, fee, stt, cost = q, 0.0, ch["total"], ch["stt"], 0.0
        book = lots[(sid, f["symbol"])]
        while left and book:
            lot = book[0]
            m = min(left, lot["qty"])
            gross += (px - lot["px"]) * m
            cost += lot["px"] * m
            fee += lot["fee"] * m / lot["q0"]
            stt += lot["stt"] * m / lot["q0"]
            lot["qty"] -= m
            left -= m
            if not lot["qty"]:
                book.pop(0)
        if left:
            raise SystemExit(f"{sid} {f['symbol']}: sold {left} more than its lots held ({f['order_id']})")
        net = gross - fee
        rows.append({"session": sid, "symbol": f["symbol"], "day": f["at"][:10], "at": f["at"], "qty": q,
                     "buy_value": r2(cost), "sell_value": r2(px * q), "gross": r2(gross), "charges": r2(fee),
                     "net": r2(net), "stt": r2(stt), "tax_est": tax_est(r2(net), r2(stt)), "order_id": f["order_id"]})
    for r in rows:
        r["actual"] = r2(r["net"] - r["tax_est"])
    return rows


def whole_account_gross_by_day(d: dict) -> dict:
    """Gross realised per day over EVERY paper fill (average cost per symbol, as paper_position keeps it)."""
    pos, out = {}, defaultdict(float)
    for f in d["fills"]:
        q, px = int(f["filled_qty"]), float(f["fill_price"])
        held, avg = pos.get(f["symbol"], (0, 0.0))
        if f["side"] == "BUY":
            pos[f["symbol"]] = (held + q, (held * avg + q * px) / (held + q))
        else:
            out[f["at"][:10]] += (px - avg) * q
            pos[f["symbol"]] = (held - q, avg if held - q else 0.0)
    return {k: r2(v) for k, v in out.items()}


def db_aggregates(c) -> dict:
    cyc = [dict(r) for r in c.execute(
        "SELECT sc.session_id, sc.rule_id, substr(sc.closed_at, 1, 10) AS day, sc.gross_pnl, sc.charges, sc.net_pnl, "
        "sc.tax_est, sc.actual_pnl FROM session_cycles sc JOIN trading_sessions t ON t.id = sc.session_id "
        "AND t.pool_started_at = sc.pool_started_at")]
    gl = defaultdict(float)
    try:
        for r in c.execute(
                "SELECT e.session_id, substr(e.at, 1, 10) AS day, SUM(e.credit) - SUM(e.debit) AS v FROM ledger_entries e "
                "JOIN session_cycles sc ON sc.rule_id = e.source_id AND sc.session_id = e.session_id "
                "JOIN trading_sessions t ON t.id = sc.session_id AND t.pool_started_at = sc.pool_started_at "
                "WHERE e.source = 'CYCLE' AND e.kind IN ('SELL', 'CHARGE', 'TAX_PROVISION') AND (e.account LIKE "
                "'Trading %' OR e.account LIKE 'Charges:%' OR e.account = 'Tax expense (est.)') GROUP BY 1, 2"):
            gl[(r["session_id"], r["day"])] += float(r["v"] or 0)
    except sqlite3.OperationalError:
        pass
    daily = [dict(r) for r in c.execute("SELECT date, realised_cum, day_pnl, equity FROM pnl_daily WHERE env = 'PAPER' "
                                        "ORDER BY date")]
    return {"cycles": cyc, "ledger": gl, "pnl_daily": daily}


def _api(api_dir: Path | None, name: str):
    if not api_dir:
        return None
    p = api_dir / name
    if not p.exists():
        return None
    j = json.loads(p.read_text())
    return j.get("data", j) if isinstance(j, dict) and "data" in j and len(j) <= 3 else j


def sum_where(rows, key, **eq):
    return r2(sum(r[key] for r in rows if all(r.get(k) == v for k, v in eq.items())))


def build(db: str, day: str, api_dir: str | None, dhan_file: str | None, dp: float) -> dict:
    c = connect(db)
    d = load(c)
    integ = integrity(c, d, dp)
    trades = realised(d, dp)
    agg = db_aggregates(c)
    api = Path(api_dir) if api_dir else None
    periods = _api(api, "live_api_v2_periods.json")
    periods = (periods or {}).get("periods") if periods else None
    pnl = _api(api, "live_api_command_pnl_scope_pools_by_day.json")
    sess = _api(api, "live_api_sessions.json")
    dhan = json.loads(Path(dhan_file).read_text()) if dhan_file else None
    dhan_trades = (dhan or {}).get("get_trade_book", {}).get("data") or []
    dhan_today = [t for t in dhan_trades if str(t.get("exchangeTime") or t.get("createTime") or "")[:10] == day]

    sids = sorted(d["sessions"], key=lambda s: int(s[1:]))
    out_rows = {"today": [], "since_start": []}
    for scope in ("today", "since_start"):
        pick = (lambda r: r["day"] == day) if scope == "today" else (lambda r: True)
        for sid in sids + ["TOTAL"]:
            mine = [t for t in trades if pick(t) and (sid == "TOTAL" or t["session"] == sid)]
            cyc = [x for x in agg["cycles"] if (scope != "today" or x["day"] == day) and (sid == "TOTAL" or x["session_id"] == sid)]
            gl = r2(sum(v for (s, dd), v in agg["ledger"].items() if (sid == "TOTAL" or s == sid)
                        and (scope != "today" or dd == day)))
            p = None
            if periods:
                p = periods[scope] if sid == "TOTAL" else (periods["sessions"].get(sid) or {}).get(scope)
            row = {"session": sid, "trades": len(mine),
                   "gross_re": sum_where(mine, "gross"), "charges_re": sum_where(mine, "charges"),
                   "net_re": sum_where(mine, "net"), "tax_re": sum_where(mine, "tax_est"),
                   "actual_re": sum_where(mine, "actual"),
                   "gross_db": sum_where(cyc, "gross_pnl"), "charges_db": sum_where(cyc, "charges"),
                   "net_db": sum_where(cyc, "net_pnl"), "actual_db": sum_where(cyc, "actual_pnl"), "cycles_db": len(cyc),
                   "actual_gl": gl,
                   "gross_api": p and p["gross"], "charges_api": p and p["charges"], "actual_api": p and p["realised"],
                   "cycles_api": p and p["cycles"],
                   "dhan": r2(sum((float(t.get("tradedPrice") or 0) * float(t.get("tradedQuantity") or 0))
                                  * (1 if t.get("transactionType") == "SELL" else -1) for t in dhan_today))
                   if sid == "TOTAL" else 0.0}
            if pnl and scope == "today":
                pr = next((x for x in pnl["periods"] if x["period"] == day), None)
                row["net_report"] = pr["booked"] if (pr and sid == "TOTAL") else None
            if pnl and scope == "since_start" and sid == "TOTAL":
                row["net_report"] = pnl["booked"]
            if sess and scope == "since_start":
                ss = {x["id"]: x["pool"] for x in sess["sessions"] if x.get("pool")}
                row["net_ledger"] = r2(sum(v["booked"] for v in ss.values())) if sid == "TOTAL" else (ss.get(sid) or {}).get("booked")
            diffs = [row["gross_db"] - row["gross_re"], row["charges_db"] - row["charges_re"],
                     row["net_db"] - row["net_re"], row["actual_db"] - row["actual_re"], row["actual_gl"] - row["actual_re"]]
            if p:
                diffs += [p["gross"] - row["gross_re"], p["charges"] - row["charges_re"], p["realised"] - row["actual_re"]]
            for k in ("net_report", "net_ledger"):
                if row.get(k) is not None:
                    diffs.append(row[k] - row["net_re"])
            row["max_abs_diff"] = r2(max(abs(x) for x in diffs))
            row["status"] = "OK" if row["max_abs_diff"] == 0 else ("OK (rounding)" if row["max_abs_diff"] <= 1 else "MISMATCH")
            out_rows[scope].append(row)

    # cumulative, day by day, for the pools (net of charges) and the whole paper account (gross, as pnl_daily keeps it)
    days = sorted({t["day"] for t in trades})
    cum, chain = 0.0, []
    rep = {x["period"]: x["booked"] for x in (pnl or {}).get("periods", [])}
    for dd in days:
        v = sum_where([t for t in trades if t["day"] == dd], "net")
        prev = cum
        cum = r2(cum + v)
        gap = None if rep.get(dd) is None else r2(rep[dd] - v)
        chain.append({"day": dd, "net": v, "previous": r2(prev), "cumulative": cum, "report_day": rep.get(dd),
                      "ok": "yes" if not gap else ("rounding" if abs(gap) <= 1 else "NO")})
    # Step 4: in every source, the ten sessions add up to the grand total it reports
    sums = []
    for scope in ("today", "since_start"):
        rows = [x for x in out_rows[scope] if x["session"] != "TOTAL"]
        tot = next(x for x in out_rows[scope] if x["session"] == "TOTAL")
        for key, label in (("actual_api", "dashboard / API /api/v2/periods (after tax)"),
                           ("gross_api", "dashboard / API gross"), ("actual_db", "DB session_cycles (after tax)"),
                           ("net_db", "DB session_cycles net"), ("actual_gl", "general ledger (after tax)"),
                           ("net_re", "recomputed net"), ("net_ledger", "API /api/sessions pool.booked (net)")):
            if tot.get(key) is None:
                continue
            s = r2(sum(x.get(key) or 0 for x in rows))
            sums.append({"scope": scope, "source": label, "sum_of_sessions": s, "grand_total": tot[key],
                         "ok": abs(s - tot[key]) < 0.005})
    acct = whole_account_gross_by_day(d)
    pd_rows, last = [], None
    for r in agg["pnl_daily"]:
        if last is not None and r["realised_cum"] is not None and last["realised_cum"] is not None:
            delta = r2(r["realised_cum"] - last["realised_cum"])
            gross = acct.get(r["date"], 0.0)
            pd_rows.append({"date": r["date"], "realised_cum": r["realised_cum"], "delta": delta,
                            "recomputed_gross": gross, "ok": abs(delta - gross) < 0.01})
        last = r
    c.close()
    return {"db": db, "date": day, "dp_charge_per_sell": dp, "integrity": integ, "trades": trades,
            "rows": out_rows, "cumulative": chain, "pnl_daily": pd_rows, "sums": sums,
            "dhan": {"trades_today": len(dhan_today), "fetched_at_ist": (dhan or {}).get("fetched_at_ist"),
                     "orders_today": len((dhan or {}).get("get_order_list", {}).get("data") or [])}}


def inr(v) -> str:
    if v is None:
        return "—"
    return f"{'−' if v < 0 else ''}₹{abs(v):,.2f}"


def markdown(r: dict) -> str:
    L = [f"# Independent P&L reconciliation — {r['date']}", "",
         f"Source: `{r['db']}` (read-only). Dhan trade book: {r['dhan']['trades_today']} trades, "
         f"{r['dhan']['orders_today']} orders (fetched {r['dhan']['fetched_at_ist'] or 'not given'}).", ""]
    for scope, title in (("today", f"Today ({r['date']}, IST)"), ("since_start", "All sessions since each pool started")):
        L += [f"## {title}", "",
              "| Session | # trades | Realised gross (app) | Realised gross (recomputed) | Realised (Dhan) | Charges | Net | "
              "Tax est. | After tax (app) | Diff | Status |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for x in r["rows"][scope]:
            b = "**" if x["session"] == "TOTAL" else ""
            L.append(f"| {b}{'GRAND TOTAL' if x['session'] == 'TOTAL' else x['session']}{b} | {x['trades']} | "
                     f"{inr(x['gross_api'] if x['gross_api'] is not None else x['gross_db'])} | {inr(x['gross_re'])} | "
                     f"{inr(x['dhan'])} | {inr(x['charges_re'])} | {inr(x['net_re'])} | {inr(x['tax_re'])} | "
                     f"{inr(x['actual_api'] if x['actual_api'] is not None else x['actual_db'])} | "
                     f"{inr(x['max_abs_diff'])} | {x['status']} |")
        L.append("")
    L += ["## Closed trades", "", "| Session | Symbol | Sold (IST) | Qty | Buy value | Sell value | Gross | Charges | Net | "
          "Tax est. |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for t in r["trades"]:
        L.append(f"| {t['session']} | {t['symbol']} | {t['at'][:16].replace('T', ' ')} | {t['qty']} | {inr(t['buy_value'])} | "
                 f"{inr(t['sell_value'])} | {inr(t['gross'])} | {inr(t['charges'])} | {inr(t['net'])} | {inr(t['tax_est'])} |")
    L += ["", "## Cumulative (all ten sessions, net of charges)", "",
          "| Day (IST) | Realised that day | Previous cumulative | Cumulative | /command/pnl that day | OK |",
          "|---|---:|---:|---:|---:|---|"]
    for x in r["cumulative"]:
        L.append(f"| {x['day']} | {inr(x['net'])} | {inr(x['previous'])} | {inr(x['cumulative'])} | {inr(x['report_day'])} | "
                 f"{x['ok']} |")
    L += ["", "## The ten sessions add up to each source's grand total", "",
          "| Period | Source | Sum of S1–S10 | Grand total | OK |", "|---|---|---:|---:|---|"]
    for x in r["sums"]:
        L.append(f"| {x['scope']} | {x['source']} | {inr(x['sum_of_sessions'])} | {inr(x['grand_total'])} | "
                 f"{'yes' if x['ok'] else 'NO'} |")
    L += ["", "## pnl_daily (whole paper account, gross): realised_cum steps vs recomputed", "",
          "| Date | realised_cum | Step | Recomputed gross that day | OK |", "|---|---:|---:|---:|---|"]
    for x in r["pnl_daily"][-10:]:
        L.append(f"| {x['date']} | {inr(x['realised_cum'])} | {inr(x['delta'])} | {inr(x['recomputed_gross'])} | "
                 f"{'yes' if x['ok'] else 'NO'} |")
    i = r["integrity"]
    L += ["", "## Integrity checks", "",
          f"- fills: {i['counts']['fills']} ({i['counts']['session_fills']} tagged to a session); paper order ids unique: "
          f"{i['counts']['paper_order_ids_unique']}; order_log ids logged twice: {i['counts']['order_log_dup_ids'] or 'none'}",
          f"- tag vs rule session disagreements: {i['tag_vs_rule'] or 'none'}",
          f"- untagged fills since the pools started (orphans): {i['orphans'] or 'none'}",
          f"- one order id referenced by two rules: {i['dup_order_refs'] or 'none'}",
          f"- rule execution price/qty differing from the fill: {i['rule_vs_fill'] or 'none'}",
          f"- recorded charges differing from the statutory recompute (≥ ₹0.01): {i['charge_diffs'] or 'none'}",
          f"- fills with NULL charges: {i['null_charges'] or 'none'}; EXECUTED rules with NULL quantity: "
          f"{i['executed_null_qty'] or 'none'}",
          f"- rules EXECUTED whose paper order never filled: {i['pending_marked_executed'] or 'none'}"]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", required=True, help="the IST day to reconcile, YYYY-MM-DD")
    ap.add_argument("--api", help="folder with the saved live API JSON")
    ap.add_argument("--dhan", help="Dhan trade-book JSON saved by the caller")
    ap.add_argument("--dp", type=float, default=15.93, help="DP charge per sale, Rs (ATIP's configured default)")
    ap.add_argument("--out", help="folder for reconciliation.json / reconciliation.md")
    a = ap.parse_args()
    res = build(a.db, a.date, a.api, a.dhan, a.dp)
    md = markdown(res)
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        Path(a.out, "reconciliation.json").write_text(json.dumps(res, indent=1, default=str))
        Path(a.out, "reconciliation.md").write_text(md)
    print(md)
