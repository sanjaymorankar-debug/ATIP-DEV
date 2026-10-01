"""
Fundamentals from NSE filings (DP-15), W27.

SOURCE
    NSE "Integrated Filing - Financials" (SEBI's integrated filing, from the March
    2025 quarter on): https://www.nseindia.com/api/integrated-filing-results lists
    each company's filings with an XBRL link on nsearchives. The older
    corporates-financial-results listing (pre-2025 quarters) is read too when
    `history=True`, so YoY and TTM figures exist for the first integrated quarters.
    First-party, free, no scraping of a third-party site (owner decision 2026-10-01).

WHAT IS STORED
    fundamental_filing   one row per XBRL: the facts ATIP uses, per context
                         (OneD quarter, OneI balance-sheet instant, FourD year-to-date),
                         broadcast time (point in time), status PARSED / FAILED.
    fundamental_data     one row per (symbol, quarter) DERIVED from the stored filings:
                         revenue / profit / EPS, YoY and QoQ, margins, TTM EPS / profit,
                         ROE / ROCE / ROA, D/E, current ratio, interest coverage, FY FCF,
                         book value per share, shares, promoter / MF / FPI holding,
                         and FS + SPI (scores/fundamental.py) at the latest close.
                         available_from = the filing's broadcast time: scoring reads
                         only rows broadcast on or before the session it scores.

    Values in XBRL are absolute rupees; stored as crore (/1e7). Consolidated is
    preferred per quarter, standalone when no consolidated filing exists.
    A quarter whose OneD context spans more than ~4 months (a company filing only
    annual figures in Q4) becomes FY minus the three earlier quarters, or is skipped.

RUN
    run_fundamentals_pipeline(symbols=None, history=False, max_new=None)
        incremental: only XBRLs not stored yet are downloaded; derivation is local.
    python -m data.nse_filings --symbols INFY TCS [--history]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

IF_LIST = ("https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol={sym}"
           "&type=Integrated%20Filing-%20Financials")
LEGACY_LIST = ("https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={sym}"
               "&period=Quarterly")
CR = 1e7

TAGS = {
    "revenue": ("RevenueFromOperations", "Income", "InterestEarned", "TotalIncome"),
    "other_income": ("OtherIncome",),
    "profit_owners": ("ProfitOrLossAttributableToOwnersOfParent",
                      "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"),
    "profit": ("ProfitLossForPeriod", "ProfitLossForThePeriod"),
    "pbt": ("ProfitBeforeTax", "ProfitLossBeforeTax"),
    "finance_costs": ("FinanceCosts", "InterestExpended"),
    "depreciation": ("DepreciationDepletionAndAmortisationExpense",),
    "eps_basic": ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShareAfterExtraordinaryItems"),
    "paid_up": ("PaidUpValueOfEquityShareCapital", "EquityShareCapital"),
    "face_value": ("FaceValueOfEquityShareCapital",),
    "equity_owners": ("EquityAttributableToOwnersOfParent",),
    "equity": ("Equity", "TotalEquity"),
    "assets": ("Assets", "CapitalAndLiabilities"),
    # banking taxonomy (in-bse-fin banks): equity = Capital + ReservesAndSurplus
    "capital": ("Capital",),
    "reserves": ("ReservesAndSurplus",),
    "deposits": ("Deposits",),
    "op_profit_bank": ("OperatingProfitBeforeProvisionAndContingencies",),
    "borrowings_cur": ("BorrowingsCurrent",),
    "borrowings_noncur": ("BorrowingsNoncurrent",),
    "current_assets": ("CurrentAssets",),
    "current_liabilities": ("CurrentLiabilities",),
    "cash": ("CashAndCashEquivalents",),
    "cfo": ("CashFlowsFromUsedInOperatingActivities",),
    "capex": ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
              "PurchaseOfPropertyPlantAndEquipment"),
    "period_start": ("DateOfStartOfReportingPeriod",),
    "period_end": ("DateOfEndOfReportingPeriod",),
    "nature": ("NatureOfReportStandaloneConsolidated",),
}
CONTEXTS = ("OneD", "OneI", "FourD", "FourI")
_FACT = re.compile(r"<[A-Za-z0-9\-]+:([A-Za-z]+)\b([^>]*)>([^<]*)<")
_CTX = re.compile(r'contextRef="([^"]+)"')


# ── parsing ────────────────────────────────────────────────────────────────
def parse_xbrl(text: str) -> dict:
    """{field: {context: value}} for the TAGS fields; numbers as float, dates / text as str."""
    want = {t: f for f, ts in TAGS.items() for t in ts}
    raw = {}
    for tag, attrs, val in _FACT.findall(text or ""):
        f = want.get(tag)
        if not f:
            continue
        m = _CTX.search(attrs)
        if not m or m.group(1) not in CONTEXTS:
            continue
        val = val.strip()
        if f in ("period_start", "period_end", "nature"):
            raw.setdefault(f, {}).setdefault(m.group(1), val)
            continue
        try:
            num = float(val)
        except ValueError:
            continue
        prio = TAGS[f].index(tag)
        cur = raw.setdefault(f, {}).get(m.group(1))
        if cur is None or prio < cur[1]:
            raw[f][m.group(1)] = (num, prio)
    return {f: {c: (v[0] if isinstance(v, tuple) else v) for c, v in ctx.items()} for f, ctx in raw.items()}


def _d(s):
    s = str(s or "").strip().title()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def quarter_label(period_end) -> str:
    d = period_end if isinstance(period_end, date) else _d(period_end).date()
    fy = d.year + 1 if d.month >= 4 else d.year
    q = ((d.month - 4) % 12) // 3 + 1
    return f"FY{fy % 100:02d}Q{q}"


# ── listing + fetch ────────────────────────────────────────────────────────
def list_filings(symbol: str, history: bool = False, nse=None) -> list:
    """[{symbol, period_end (date), nature CONSOLIDATED|STANDALONE, audited, broadcast_at, xbrl}]"""
    from data.nse_api import client
    nse = nse or client()
    out = []
    j = nse.json(IF_LIST.format(sym=symbol)) or {}
    for r in (j.get("data") or []) if isinstance(j, dict) else []:
        pe, x = _d(r.get("qe_Date")), r.get("xbrl")
        if not pe or not x or not str(x).lower().endswith(".xml"):
            continue
        nat = "CONSOLIDATED" if str(r.get("consolidated", "")).lower().startswith("consol") else "STANDALONE"
        out.append({"symbol": symbol, "period_end": pe.date(), "nature": nat, "audited": r.get("audited"),
                    "broadcast_at": _d(r.get("broadcast_Date")), "xbrl": x})
    if history:
        lj = nse.json(LEGACY_LIST.format(sym=symbol)) or []
        for r in lj if isinstance(lj, list) else []:
            pe, x = _d(r.get("toDate")), r.get("xbrl")
            if not pe or not x or not str(x).lower().endswith(".xml"):
                continue
            nat = "STANDALONE" if "non" in str(r.get("consolidated", "")).lower() else "CONSOLIDATED"
            out.append({"symbol": symbol, "period_end": pe.date(), "nature": nat, "audited": r.get("audited"),
                        "broadcast_at": _d(r.get("broadCastDate")), "xbrl": x})
    seen, uniq = set(), []
    for f in out:
        if f["xbrl"] not in seen:
            seen.add(f["xbrl"])
            uniq.append(f)
    return uniq


def store_filing(conn, f: dict, nse=None) -> str:
    from data.nse_api import client
    nse = nse or client()
    text = nse.text(f["xbrl"])
    status, err, facts = "PARSED", None, {}
    if not text:
        status, err = "FAILED", "download failed"
    else:
        facts = parse_xbrl(text)
        if not facts.get("revenue") and not facts.get("profit"):
            status, err = "FAILED", "no revenue / profit facts in XBRL"
    conn.execute("INSERT OR REPLACE INTO fundamental_filing (xbrl_url,symbol,period_end,nature,audited,broadcast_at,"
                 "facts_json,status,error,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (f["xbrl"], f["symbol"], str(f["period_end"]), f["nature"], f.get("audited"),
                  f.get("broadcast_at"), json.dumps(facts), status, err, datetime.now()))
    conn.commit()
    return status


# ── derivation ─────────────────────────────────────────────────────────────
def _v(facts, field, ctx="OneD"):
    x = (facts.get(field) or {}).get(ctx)
    return x if isinstance(x, (int, float)) else None


def _days(facts):
    s, e = _d((facts.get("period_start") or {}).get("OneD")), _d((facts.get("period_end") or {}).get("OneD"))
    return (e - s).days + 1 if s and e else None


def _quarters(conn, symbol) -> dict:
    """{nature: [quarter dict ascending by period_end]} from stored PARSED filings."""
    by = {}
    for r in conn.execute("SELECT period_end, nature, broadcast_at, facts_json FROM fundamental_filing WHERE symbol=? "
                          "AND status='PARSED' ORDER BY period_end, broadcast_at", (symbol,)):
        facts = json.loads(r["facts_json"] or "{}")
        pe = r["period_end"] if isinstance(r["period_end"], date) else _d(r["period_end"]).date()
        by.setdefault(r["nature"], {})[pe] = {"period_end": pe, "broadcast_at": r["broadcast_at"], "facts": facts}
    out = {}
    for nature, qs in by.items():
        rows = []
        for pe in sorted(qs):
            f = qs[pe]["facts"]
            q = {"period_end": pe, "broadcast_at": qs[pe]["broadcast_at"], "days": _days(f),
                 "revenue": _v(f, "revenue"), "other_income": _v(f, "other_income"),
                 "profit": _v(f, "profit_owners") if _v(f, "profit_owners") is not None else _v(f, "profit"),
                 "pbt": _v(f, "pbt"), "finance_costs": _v(f, "finance_costs"), "eps": _v(f, "eps_basic"),
                 "paid_up": _v(f, "paid_up"), "face_value": _v(f, "face_value"),
                 "equity": _v(f, "equity_owners", "OneI") or _v(f, "equity", "OneI"),
                 "assets": _v(f, "assets", "OneI"),
                 "debt": (None if _v(f, "borrowings_cur", "OneI") is None and _v(f, "borrowings_noncur", "OneI") is None
                          else (_v(f, "borrowings_cur", "OneI") or 0) + (_v(f, "borrowings_noncur", "OneI") or 0)),
                 "current_assets": _v(f, "current_assets", "OneI"),
                 "current_liabilities": _v(f, "current_liabilities", "OneI"),
                 "cash": _v(f, "cash", "OneI"), "cfo_ytd": _v(f, "cfo", "FourD"), "capex_ytd": _v(f, "capex", "FourD"),
                 "ytd_days": None}
            # Banks: deposits are the business, not leverage, and interest expended is the
            # cost of funds -- so no D/E, no interest coverage, no ROCE; operating margin is
            # operating profit before provisions / total income.
            q["bank"] = bool(_v(f, "deposits", "OneI") is not None or (rows and rows[-1].get("bank")))
            if q["equity"] is None and _v(f, "reserves", "OneI") is not None:
                q["equity"] = (_v(f, "capital", "OneI") or 0) + _v(f, "reserves", "OneI")
            if q["bank"]:
                q["debt"] = None
                q["finance_costs"] = None
                q["op_profit_bank"] = _v(f, "op_profit_bank")
            rows.append(q)
        # Annual-only Q4 filings: quarter = FY - previous three quarters, when they exist
        for i, q in enumerate(rows):
            if q["days"] and q["days"] > 125:
                prev = [p for p in rows[:i] if (q["period_end"] - p["period_end"]).days < 300 and
                        (p["days"] or 0) <= 125][-3:]
                if len(prev) == 3:
                    for k in ("revenue", "profit", "pbt", "finance_costs", "other_income", "eps"):
                        if q[k] is not None and all(p[k] is not None for p in prev):
                            q[k] = q[k] - sum(p[k] for p in prev)
                    q["days"] = 91
                else:
                    q["skip"] = True
        out[nature] = [q for q in rows if not q.get("skip")]
    return out


def _find(rows, pe, days_back, tol=20):
    target = pe - timedelta(days=days_back)
    for r in rows:
        if abs((r["period_end"] - target).days) <= tol:
            return r
    return None


def _pct(a, b):
    return round((a - b) / abs(b) * 100, 2) if (a is not None and b not in (None, 0)) else None


def derive(rows: list) -> list:
    """Per-quarter fundamental_data dicts (crore where money) from one nature's quarters."""
    out = []
    for i, q in enumerate(rows):
        pe = q["period_end"]
        yago, qago = _find(rows, pe, 365), _find(rows, pe, 91)
        last4 = [r for r in rows[:i + 1] if (pe - r["period_end"]).days < 360][-4:]
        ttm_ok = len(last4) == 4
        bs = next((r for r in reversed(rows[:i + 1]) if r["equity"] is not None), None)
        fy = next((r for r in reversed(rows[:i + 1]) if r["period_end"].month == 3 and r["cfo_ytd"] is not None), None)
        rev, prof = q["revenue"], q["profit"]
        ebit = (q["pbt"] + (q["finance_costs"] or 0) - (q["other_income"] or 0)) if q["pbt"] is not None else None
        if q.get("bank"):
            ebit = q.get("op_profit_bank")
        prof_ttm = sum(r["profit"] for r in last4) if ttm_ok and all(r["profit"] is not None for r in last4) else None
        ebit_ttm = None
        if ttm_ok and all(r["pbt"] is not None for r in last4):
            ebit_ttm = sum(r["pbt"] + (r["finance_costs"] or 0) - (r["other_income"] or 0) for r in last4)
        eps_ttm = sum(r["eps"] for r in last4) if ttm_ok and all(r["eps"] is not None for r in last4) else None
        equity = bs["equity"] if bs else None
        debt = bs["debt"] if bs else None
        shares = None
        src = q if q["paid_up"] and q["face_value"] else bs
        if src and src.get("paid_up") and src.get("face_value"):
            shares = src["paid_up"] / src["face_value"]
        fy_profit = None
        if fy:
            fy4 = [r for r in rows if 0 <= (fy["period_end"] - r["period_end"]).days < 360][-4:]
            if len(fy4) == 4 and all(r["profit"] is not None for r in fy4):
                fy_profit = sum(r["profit"] for r in fy4)
        d = {
            "period_end": pe, "quarter": quarter_label(pe), "report_date": pe, "available_from": q["broadcast_at"],
            "revenue_cr": rev / CR if rev is not None else None, "profit_cr": prof / CR if prof is not None else None,
            "eps_q": q["eps"], "eps_ttm": round(eps_ttm, 2) if eps_ttm is not None else None,
            "revenue_growth_yoy": _pct(rev, yago and yago["revenue"]),
            "profit_growth_yoy": _pct(prof, yago and yago["profit"]),
            "eps_growth_yoy": _pct(q["eps"], yago and yago["eps"]),
            "qoq_revenue_chg": _pct(rev, qago and qago["revenue"]),
            "qoq_profit_chg": _pct(prof, qago and qago["profit"]),
            "net_margin": round(prof / rev, 4) if (prof is not None and rev) else None,
            "operating_margin": round(ebit / rev, 4) if (ebit is not None and rev) else None,
            "interest_coverage": (round((q["pbt"] + q["finance_costs"]) / q["finance_costs"], 2)
                                  if q["pbt"] is not None and q["finance_costs"] else None),
            "roe": round(prof_ttm / equity, 4) if (prof_ttm is not None and equity and equity > 0) else None,
            "roce": (round(ebit_ttm / (equity + (debt or 0)), 4)
                     if (ebit_ttm is not None and equity and equity + (debt or 0) > 0 and not q.get("bank"))
                     else None),
            "roa": round(prof_ttm / bs["assets"], 4) if (prof_ttm is not None and bs and bs["assets"]) else None,
            "debt_equity": round(debt / equity, 3) if (debt is not None and equity and equity > 0) else None,
            "current_ratio": (round(bs["current_assets"] / bs["current_liabilities"], 3)
                              if bs and bs["current_assets"] and bs["current_liabilities"] else None),
            "cash_cr": bs["cash"] / CR if bs and bs["cash"] is not None else None,
            "fcf_cr": ((fy["cfo_ytd"] - abs(fy["capex_ytd"] or 0)) / CR) if fy else None,
            "profit_fy_cr": fy_profit / CR if fy_profit is not None else None,
            "equity_cr": equity / CR if equity is not None else None,
            "debt_cr": debt / CR if debt is not None else None,
            "shares_out": shares,
            "book_value_ps": round(equity / shares, 2) if (equity and shares) else None,
        }
        out.append(d)
    return out


def _price(conn, symbol, on=None):
    sql, args = "SELECT close FROM prices_daily WHERE symbol=? AND close>0", [symbol]
    if on:
        sql += " AND date<=?"
        args.append(str(on)[:10])
    r = conn.execute(sql + " ORDER BY date DESC LIMIT 1", args).fetchone()
    return float(r[0]) if r else None


def rebuild_symbol(conn, symbol: str) -> int:
    """Re-derive every fundamental_data row of `symbol` from its stored filings."""
    from scores.fundamental import score_row
    qs = _quarters(conn, symbol)
    if not qs:
        return 0
    # Consolidated where filed, standalone otherwise: one row per quarter
    chosen = {}
    for nature in ("STANDALONE", "CONSOLIDATED"):
        for d in derive(qs.get(nature, [])):
            d["nature"] = nature
            chosen[d["quarter"]] = d
    price = _price(conn, symbol)
    shp = conn.execute("SELECT promoter_pct, mf_pct, fpi_pct, pledged_pct FROM shareholding_pattern WHERE symbol=? "
                       "ORDER BY as_of DESC LIMIT 1", (symbol,)).fetchone()
    n = 0
    for d in sorted(chosen.values(), key=lambda x: x["period_end"]):
        d["symbol"] = symbol
        if shp:
            d["promoter_hold"], d["mf_hold"], d["fpi_hold"], d["promoter_pledge"] = (shp[0], shp[1], shp[2], shp[3])
        if price:
            from scores.fundamental import valuation
            v = valuation(d, price)
            d["pe_ratio"], d["pb_ratio"], d["peg_ratio"] = v["pe"], v["pb"], v["peg"]
        sc = score_row(conn, d, price)
        d["fundamental_score"], d["spi_score"], d["score_inputs"] = sc["fs"], sc["spi"], sc["inputs"]
        d["source"] = "nse_xbrl"
        cols = [k for k in d if k != "symbol"]
        conn.execute(f"INSERT INTO fundamental_data (symbol,{','.join(cols)}) VALUES (?,{','.join('?' * len(cols))}) "
                     f"ON CONFLICT(symbol,quarter) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols),
                     [symbol] + [str(d[c]) if isinstance(d[c], date) and not isinstance(d[c], datetime) else d[c]
                                 for c in cols])
        n += 1
    conn.commit()
    return n


def ingest_symbol(conn, symbol: str, history: bool = False, nse=None, max_new=None, reparse=False) -> dict:
    if reparse:      # stored facts keep only the TAGS known when parsed; re-download after adding tags
        conn.execute("DELETE FROM fundamental_filing WHERE symbol=?", (symbol,))
        conn.commit()
    have = {r[0] for r in conn.execute("SELECT xbrl_url FROM fundamental_filing WHERE symbol=? AND status='PARSED'",
                                       (symbol,))}
    new = [f for f in list_filings(symbol, history, nse) if f["xbrl"] not in have]
    new.sort(key=lambda f: f["period_end"], reverse=True)
    if max_new is not None:
        new = new[:int(max_new)]
    stored = sum(1 for f in new if store_filing(conn, f, nse) == "PARSED")
    rows = rebuild_symbol(conn, symbol) if (stored or have) else 0
    return {"symbol": symbol, "new_filings": len(new), "parsed": stored, "rows": rows}


def run_fundamentals_pipeline(symbols=None, history: bool = False, max_new=None, reparse=False) -> dict:
    from db.schema import get_connection, log_job
    from scores.fundamental import ensure_weights
    from data.nse_api import client
    conn = get_connection()
    started = datetime.now()
    try:
        ensure_weights(conn)
        if not symbols:
            from data.dhan import get_tracked_symbols
            symbols = get_tracked_symbols(conn)
        nse = client()
        done = rows = parsed = failed = 0
        for sym in symbols:
            try:
                r = ingest_symbol(conn, sym, history, nse, max_new, reparse)
                done += 1
                rows += r["rows"]
                parsed += r["parsed"]
            except Exception as e:
                failed += 1
                log.warning(f"  fundamentals {sym}: {e}")
        res = {"status": "SUCCESS" if not failed else "PARTIAL", "symbols": done, "failed": failed,
               "filings_parsed": parsed, "rows": rows}
        log.info(f"  ✓ NSE fundamentals: {done} symbols, {parsed} new filings, {rows} quarter rows")
        log_job("fundamentals_nse", res["status"], rows, start_time=started)
        return res
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--history", action="store_true", help="also read the pre-2025 results listing")
    ap.add_argument("--max-new", type=int, default=None)
    ap.add_argument("--reparse", action="store_true", help="re-download and re-parse stored filings")
    a = ap.parse_args()
    print(run_fundamentals_pipeline(a.symbols, a.history, a.max_new, a.reparse))
