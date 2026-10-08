"""
W39 Phase 2 (item 7, FS-01..03) — an explainable fundamental scorecard: five axes of six pass / fail
checks each, every check a sentence carrying the numbers it used. The layout is Simply Wall St's
Snowflake, rebuilt on what ATIP stores.

    Value      price below the research model's fair value, and 20 %+ below it; P/E below the market's
               median and its industry's; PEG below 1; P/B below its industry's
    Growth     the latest reported growth (ATIP holds no analyst forecasts): EPS growth above a savings
               rate (the risk-free rate in research.valuation), above the market's median, 20 %+;
               revenue growth above the market's median, 20 %+; growth it can fund itself
               (ROE x the share of profit kept) of 10 %+
    Past       EPS up over 3 years; growth accelerating (this year's above the 3-year rate); EPS growth
               above the industry's median; net margin up on a year ago; ROE 20 %+; profit backed by
               positive free cash flow
    Health     current ratio 1+; net debt under 40 % of equity; debt not rising; interest covered 3x+;
               free cash flow covering 20 %+ of debt (the last four pass outright when it has no debt);
               promoter pledge under 5 %
    Dividend   yield in the top 75 % of dividend payers, and the top 25 %; paid in each of the last two
               years; the per-share dividend up on the year before; covered by earnings (payout under
               75 %) and by free cash flow

Each check is True (pass), False (fail) or None (no data, or not meaningful: the debt checks for banks
and NBFCs, whose borrowing is their raw material). None never counts as a pass, so compare banks with
banks. A loss-maker fails the P/E, PEG and payout checks; a company paying no dividend fails the
dividend axis, as on the Snowflake.

    apply(conn, rows, as_of, funds)   adds checks_passed (0-30) and one 0-6 count per axis to the
                                      screener's rows (research/screener.py), and the detail as
                                      row["_scorecard"]
    for_symbol(conn, symbol)          the detail for one stock, from the cached screener snapshot
    store(conn, rows, as_of)          the day's counts into fundamental_scorecard (the 20:50 saved-screens
                                      job), so the scorecard builds its own record
    record(conn, horizon)             return vs the Nifty over the next `horizon` sessions by score band,
                                      one sample per stock per month

The comparisons with the market and the industry use the screener's snapshot of every stock; the
history checks use the stored quarters, point in time (period end on or before the day). Dividends
come from NSE's corporate-action calendar (data/corporate_actions.py), adjusted for later splits and
bonuses; the two-year checks stay unknown until that calendar covers two years.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, timedelta

from research import valuation as V

AXES = (("value", "Value"), ("growth", "Growth"), ("past", "Past performance"), ("health", "Financial health"),
        ("dividend", "Dividend"))
COUNT_FIELDS = ("checks_passed",) + tuple(f"{k}_checks" for k, _ in AXES)

DEFAULTS = dict(margin_of_safety_pct=20.0, high_growth_pct=20.0, self_funded_growth_pct=10.0, high_roe_pct=20.0,
                max_net_debt_equity=0.4, min_interest_cover=3.0, min_fcf_to_debt=0.2, max_pledge_pct=5.0,
                max_payout_pct=75.0, debt_free_de=0.05, min_peers=3, min_market=10)
BANDS = (("0-10", 0, 10), ("11-15", 11, 15), ("16-20", 16, 20), ("21-30", 21, 30))
MIN_RECORD = 30                      # a band with fewer samples is shown but marked "too few"
HORIZONS = (20, 60, 120, 250)
DIV_RE = re.compile(r"dividend\s*-?\s*R[es]\.?\s*-?\s*(\d+(?:\.\d+)?)", re.I)

DDL = (
    """CREATE TABLE IF NOT EXISTS fundamental_scorecard (
        symbol TEXT NOT NULL, as_of DATE NOT NULL, price REAL, checks_passed INTEGER, checks_known INTEGER,
        value_checks INTEGER, growth_checks INTEGER, past_checks INTEGER, health_checks INTEGER,
        dividend_checks INTEGER, checks_json TEXT, created_at TIMESTAMP, PRIMARY KEY (symbol, as_of))""",
    "CREATE INDEX IF NOT EXISTS idx_fundamental_scorecard_asof ON fundamental_scorecard(as_of)",
)


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("scorecard") or {}
    except Exception:
        raw = {}
    return {k: type(v)(raw[k]) if k in raw else v for k, v in DEFAULTS.items()}


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _d(v):
    if v is None or isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _num(v):
    return V._num(v)


def _chunks(xs, n=500):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _pctv(v):
    """A margin or return stored as a fraction (XBRL, Alpha Vantage) or already in % (above 1.5)."""
    v = _num(v)
    return None if v is None else (v if abs(v) > 1.5 else v * 100)


def _f(v, nd=1):
    return f"{v:,.{nd}f}"


# ── inputs ───────────────────────────────────────────────────────────────────

def load_history(conn, symbols, as_of) -> dict:
    """symbol -> its stored quarters, newest first: date, eps_ttm, net margin %, debt / equity, dividend yield."""
    out = {}
    for part in _chunks(sorted(symbols)):
        ins = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT symbol, period_end, report_date, eps_ttm, net_margin, debt_equity, "
                              f"dividend_yield, source FROM fundamental_data WHERE symbol IN ({ins}) "
                              f"ORDER BY symbol, COALESCE(period_end, report_date) DESC, id DESC", part):
            d = _d(r[1] or r[2])
            if d is None or d > as_of:
                continue
            dy = V.normalise_fundamentals({"dividend_yield": r[6], "source": r[7]})["dividend_yield"]
            out.setdefault(r[0], []).append({"date": d, "eps_ttm": _num(r[3]), "net_margin_pct": _pctv(r[4]),
                                             "debt_equity": _num(r[5]), "dividend_yield": dy})
    return out


def _at(hist, target, tol=50):
    """The stored quarter closest to `target` (within tol days), or None."""
    best = None
    for h in hist:
        gap = abs((h["date"] - target).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, h)
    return best[1] if best else None


def load_dividends(conn, symbols, as_of) -> tuple:
    """({symbol: [(ex_date, rupees per share on today's share count)]}, first date the calendar covers)."""
    try:
        first = _d(conn.execute("SELECT MIN(ex_date) FROM corporate_actions").fetchone()[0])
    except Exception:
        return {}, None
    if first is None:
        return {}, None
    share = {}
    for sym, ex, pf in conn.execute("SELECT symbol, ex_date, MAX(price_factor) FROM corporate_actions WHERE kind IN "
                                    "('SPLIT','BONUS','CONSOLIDATION') AND price_factor>0 AND ex_date<=? "
                                    "GROUP BY symbol, ex_date", (str(as_of),)):
        share.setdefault(sym, []).append((_d(ex), float(pf)))
    out, keep = {}, set(symbols)
    for sym, ex, subject in conn.execute("SELECT symbol, ex_date, subject FROM corporate_actions WHERE ex_date<=? AND "
                                         "ex_date>? AND LOWER(subject) LIKE '%dividend%'",
                                         (str(as_of), str(as_of - timedelta(days=740)))):
        if sym not in keep:
            continue
        amt = sum(float(m) for m in DIV_RE.findall(subject or ""))
        if amt <= 0:
            continue
        exd = _d(ex)
        for d, pf in share.get(sym, []):
            if d > exd:
                amt *= pf                       # a later 1:2 split halves the old per-share amount
        out.setdefault(sym, []).append((exd, amt))
    return out, first


# ── the checks ───────────────────────────────────────────────────────────────

def _chk(key, label, ok, detail):
    return {"key": key, "label": label, "pass": None if ok is None else bool(ok), "detail": detail}


def context(rows, divs=None, div_from=None, as_of=None, cfg=None) -> dict:
    """Market medians, industry members and dividend-yield quartiles over every row."""
    cfg = cfg or DEFAULTS
    ctx = {"market": {}, "industry": {}, "div": divs or {}, "div_from": div_from, "as_of": as_of, "cfg": cfg}
    for key in ("pe", "eps_growth_pct", "revenue_growth_pct"):
        xs = [r.get(key) for r in rows if r.get(key) is not None and (key != "pe" or r[key] > 0)]
        ctx["market"][key] = (statistics.median(xs), len(xs)) if len(xs) >= cfg["min_market"] else None
    for r in rows:
        if r.get("industry"):
            ctx["industry"].setdefault(r["industry"], []).append(r)
    ylds = sorted(y for y in (dividend_yield(r, ctx) for r in rows) if y and y > 0)
    if len(ylds) >= cfg["min_market"]:
        q = statistics.quantiles(ylds, n=4, method="inclusive")
        ctx["yield_q"] = (q[0], q[2], len(ylds))
    else:
        ctx["yield_q"] = None
    return ctx


def _peer_median(ctx, row, key, positive=False):
    peers = [p.get(key) for p in ctx["industry"].get(row.get("industry"), [])
             if p["symbol"] != row["symbol"] and p.get(key) is not None and (not positive or p[key] > 0)]
    return (statistics.median(peers), len(peers)) if len(peers) >= ctx["cfg"]["min_peers"] else None


def _dps(ctx, sym):
    """(last 12 months, the 12 before) per-share dividends from the calendar; None where it does not reach."""
    as_of, first = ctx.get("as_of"), ctx.get("div_from")
    if not as_of or not first:
        return None, None
    ev = ctx["div"].get(sym, [])
    y1 = sum(a for d, a in ev if d > as_of - timedelta(days=365)) if first <= as_of - timedelta(days=330) else None
    y0 = (sum(a for d, a in ev if as_of - timedelta(days=730) < d <= as_of - timedelta(days=365))
          if first <= as_of - timedelta(days=700) else None)
    return y1, y0


def dividend_yield(row, ctx) -> float | None:
    """Yield in %: the stored figure, else the calendar's last 12 months over the price."""
    if row.get("dividend_yield_pct") is not None:
        return row["dividend_yield_pct"]
    y1, _ = _dps(ctx, row["symbol"])
    if y1 is not None and row.get("price"):
        return round(y1 / row["price"] * 100, 2)
    return None


def _value(row, ctx):
    c, px, fv, pe, pb = ctx["cfg"], row.get("price"), row.get("fair_value"), row.get("pe"), row.get("pb")
    loss = row.get("eps_ttm") is not None and row["eps_ttm"] <= 0
    out = []
    if fv and px:
        gap = (1 - px / fv) * 100
        txt = f"₹{_f(px, 2)} vs a fair value of ₹{_f(fv, 2)} (research model): " + (
            f"{_f(gap)} % below" if gap >= 0 else f"{_f(-gap)} % above")
        out += [_chk("below_fair_value", "Below fair value", px < fv, txt),
                _chk("well_below_fair_value", f"{c['margin_of_safety_pct']:.0f} %+ below fair value",
                     gap >= c["margin_of_safety_pct"], txt)]
    else:
        out += [_chk("below_fair_value", "Below fair value", None, "no research fair value yet"),
                _chk("well_below_fair_value", f"{c['margin_of_safety_pct']:.0f} %+ below fair value", None,
                     "no research fair value yet")]
    m = ctx["market"].get("pe")
    if loss:
        out.append(_chk("pe_vs_market", "P/E below the market's", False, "loss-making: no P/E"))
    elif pe is None or m is None:
        out.append(_chk("pe_vs_market", "P/E below the market's", None, "no P/E" if pe is None else "too few P/Es"))
    else:
        out.append(_chk("pe_vs_market", "P/E below the market's", pe < m[0],
                        f"P/E {_f(pe)}x vs a market median of {_f(m[0])}x ({m[1]} stocks)"))
    p = _peer_median(ctx, row, "pe", positive=True)
    if loss:
        out.append(_chk("pe_vs_industry", "P/E below its industry's", False, "loss-making: no P/E"))
    elif pe is None or p is None:
        out.append(_chk("pe_vs_industry", "P/E below its industry's", None,
                        "no P/E" if pe is None else f"fewer than {c['min_peers']} industry peers with a P/E"))
    else:
        out.append(_chk("pe_vs_industry", "P/E below its industry's", pe < p[0],
                        f"P/E {_f(pe)}x vs {_f(p[0])}x, the median of {p[1]} {row['industry']} peers"))
    eg = row.get("eps_growth_pct")
    if loss:
        out.append(_chk("peg", "PEG below 1", False, "loss-making: no PEG"))
    elif pe is not None and eg is not None and eg <= 0:
        out.append(_chk("peg", "PEG below 1", False, f"EPS not growing ({_f(eg)} % YoY)"))
    elif row.get("peg") is None:
        out.append(_chk("peg", "PEG below 1", None, "no P/E or EPS growth"))
    else:
        out.append(_chk("peg", "PEG below 1", row["peg"] < 1,
                        f"PEG {_f(row['peg'], 2)} (P/E {_f(pe)}x ÷ EPS growth {_f(eg)} %)"))
    p = _peer_median(ctx, row, "pb", positive=True)
    bv = row.get("book_value_ps")
    if bv is not None and bv <= 0:
        out.append(_chk("pb_vs_industry", "P/B below its industry's", False, "negative book value"))
    elif pb is None or p is None:
        out.append(_chk("pb_vs_industry", "P/B below its industry's", None,
                        "no P/B" if pb is None else f"fewer than {c['min_peers']} industry peers with a P/B"))
    else:
        out.append(_chk("pb_vs_industry", "P/B below its industry's", pb < p[0],
                        f"P/B {_f(pb, 2)}x vs {_f(p[0], 2)}x, the median of {p[1]} {row['industry']} peers"))
    return out


def _payout(row, ctx):
    """Dividend / EPS as a fraction (yield x P/E); 0 for a non-payer; None if unknown or loss-making."""
    y, pe = dividend_yield(row, ctx), row.get("pe")
    if y is not None and y <= 0:
        return 0.0
    if y is None or pe is None:
        return None
    return y / 100 * pe


def _growth(row, ctx):
    c, out = ctx["cfg"], []
    eg, rg, rf = row.get("eps_growth_pct"), row.get("revenue_growth_pct"), V.DEFAULTS["risk_free_pct"]
    none = "no EPS growth figure"
    out.append(_chk("eps_vs_savings", f"EPS growth above a savings rate ({rf:.0f} %)", None if eg is None else eg > rf,
                    none if eg is None else f"EPS growth {_f(eg)} % YoY vs the {rf:.0f} % risk-free rate"))
    m = ctx["market"].get("eps_growth_pct")
    out.append(_chk("eps_vs_market", "EPS growth above the market's", None if eg is None or m is None else eg > m[0],
                    none if eg is None else "too few stocks" if m is None else
                    f"EPS growth {_f(eg)} % vs a market median of {_f(m[0])} % ({m[1]} stocks)"))
    out.append(_chk("high_eps_growth", f"EPS growth {c['high_growth_pct']:.0f} %+",
                    None if eg is None else eg >= c["high_growth_pct"], none if eg is None else f"EPS growth {_f(eg)} % YoY"))
    m = ctx["market"].get("revenue_growth_pct")
    none = "no revenue growth figure"
    out.append(_chk("revenue_vs_market", "Revenue growth above the market's",
                    None if rg is None or m is None else rg > m[0],
                    none if rg is None else "too few stocks" if m is None else
                    f"revenue growth {_f(rg)} % vs a market median of {_f(m[0])} % ({m[1]} stocks)"))
    out.append(_chk("high_revenue_growth", f"Revenue growth {c['high_growth_pct']:.0f} %+",
                    None if rg is None else rg >= c["high_growth_pct"],
                    none if rg is None else f"revenue growth {_f(rg)} % YoY"))
    roe, eps = row.get("roe_pct"), row.get("eps_ttm")
    label = f"Can fund {c['self_funded_growth_pct']:.0f} %+ growth itself"
    if eps is not None and eps <= 0:
        out.append(_chk("self_funded_growth", label, False, "loss-making: nothing to reinvest"))
    else:
        pay = _payout(row, ctx)
        if roe is None or pay is None:
            out.append(_chk("self_funded_growth", label, None, "no ROE" if roe is None else "no dividend or P/E figure"))
        else:
            keep = min(max(1 - pay, 0.0), 1.0)
            g = roe * keep
            out.append(_chk("self_funded_growth", label, g >= c["self_funded_growth_pct"],
                            f"ROE {_f(roe)} % × {_f(keep * 100, 0)} % of profit kept = {_f(g)} % a year"))
    return out


def _past(row, hist, ctx, fin):
    c, out = ctx["cfg"], []
    h0 = hist[0] if hist else None
    e0 = h0["eps_ttm"] if h0 else None
    h1 = _at(hist, h0["date"] - timedelta(days=365)) if h0 else None
    h3 = _at(hist, h0["date"] - timedelta(days=round(3 * 365.25))) if h0 else None
    e1, e3 = (h1 or {}).get("eps_ttm"), (h3 or {}).get("eps_ttm")
    if e0 is None or e3 is None:
        out.append(_chk("eps_up_3y", "EPS up over 3 years", None, "fewer than 3 years of stored quarters"))
    else:
        cagr = ((e0 / e3) ** (1 / 3) - 1) * 100 if e0 > 0 and e3 > 0 else None
        out.append(_chk("eps_up_3y", "EPS up over 3 years", e0 > e3 and e0 > 0,
                        f"EPS (TTM) ₹{_f(e0, 2)} vs ₹{_f(e3, 2)} three years earlier" +
                        (f", {_f(cagr)} % a year" if cagr is not None else "")))
    if e0 is not None and e0 <= 0:
        out.append(_chk("accelerating", "Growth accelerating", False, "loss-making now"))
    elif None in (e0, e1, e3) or e1 <= 0 or e3 <= 0:
        out.append(_chk("accelerating", "Growth accelerating", None, "needs positive EPS a year and three years back"))
    else:
        yoy, cagr = (e0 / e1 - 1) * 100, ((e0 / e3) ** (1 / 3) - 1) * 100
        out.append(_chk("accelerating", "Growth accelerating", yoy > cagr and yoy > 0,
                        f"EPS growth {_f(yoy)} % this year vs {_f(cagr)} % a year over three"))
    eg, p = row.get("eps_growth_pct"), _peer_median(ctx, row, "eps_growth_pct")
    out.append(_chk("eps_vs_industry", "EPS growth above its industry's", None if eg is None or p is None else eg > p[0],
                    "no EPS growth figure" if eg is None else
                    f"fewer than {c['min_peers']} industry peers" if p is None else
                    f"EPS growth {_f(eg)} % vs {_f(p[0])} %, the median of {p[1]} {row['industry']} peers"))
    m0, m1 = (h0 or {}).get("net_margin_pct"), (h1 or {}).get("net_margin_pct")
    out.append(_chk("margin_up", "Net margin up on a year ago", None if m0 is None or m1 is None else m0 > m1,
                    "no net margin a year back" if m0 is None or m1 is None else
                    f"net margin {_f(m0)} % vs {_f(m1)} % a year earlier"))
    roe = row.get("roe_pct")
    out.append(_chk("high_roe", f"ROE {c['high_roe_pct']:.0f} %+", None if roe is None else roe >= c["high_roe_pct"],
                    "no ROE" if roe is None else f"ROE {_f(roe)} %"))
    fcf = row.get("fcf_cr")
    if fin:
        out.append(_chk("cash_backed", "Profit backed by free cash flow", None, "not meaningful for a bank or NBFC"))
    else:
        out.append(_chk("cash_backed", "Profit backed by free cash flow", None if fcf is None else fcf > 0,
                        "no free cash flow figure" if fcf is None else f"free cash flow ₹{_f(fcf, 0)} cr (FY)"))
    return out


def _health(row, hist, ctx, fin, fund):
    c = ctx["cfg"]
    labels = [("current_ratio", "Current ratio 1 or more"),
              ("low_debt", f"Net debt under {c['max_net_debt_equity'] * 100:.0f} % of equity"),
              ("debt_not_rising", "Debt not rising"),
              ("interest_cover", f"Interest covered {c['min_interest_cover']:.0f}x+"),
              ("fcf_covers_debt", f"Free cash flow covers {c['min_fcf_to_debt'] * 100:.0f} %+ of debt")]
    out = []
    if fin:
        out += [_chk(k, lab, None, "not meaningful for a bank or NBFC: borrowing is its raw material")
                for k, lab in labels]
    else:
        cr = row.get("current_ratio")
        out.append(_chk(labels[0][0], labels[0][1], None if cr is None else cr >= 1,
                        "no current ratio" if cr is None else f"current ratio {_f(cr, 2)}"))
        de, cash = row.get("debt_equity"), row.get("cash_cr")
        debt, eq = _num(fund.get("debt_cr")), _num(fund.get("equity_cr"))
        free = de is not None and de <= c["debt_free_de"]
        if debt is not None and eq and eq > 0:
            nd = (debt - (cash or 0)) / eq
            out.append(_chk(labels[1][0], labels[1][1], nd < c["max_net_debt_equity"],
                            f"net debt ₹{_f(debt - (cash or 0), 0)} cr = {_f(nd * 100, 0)} % of equity ₹{_f(eq, 0)} cr"))
        elif de is not None:
            out.append(_chk(labels[1][0], labels[1][1], de < c["max_net_debt_equity"],
                            f"debt / equity {_f(de, 2)} (gross: no debt and cash split stored)"))
        else:
            out.append(_chk(labels[1][0], labels[1][1], None, "no debt figure"))
        h0 = hist[0] if hist else None
        h1 = _at(hist, h0["date"] - timedelta(days=365)) if h0 else None
        de1 = (h1 or {}).get("debt_equity")
        if free:
            out.append(_chk(labels[2][0], labels[2][1], True, f"practically no debt (debt / equity {_f(de, 2)})"))
        elif de is None or de1 is None:
            out.append(_chk(labels[2][0], labels[2][1], None, "no debt / equity a year back"))
        else:
            out.append(_chk(labels[2][0], labels[2][1], de <= de1,
                            f"debt / equity {_f(de, 2)} vs {_f(de1, 2)} a year earlier"))
        ic = row.get("interest_coverage")
        if free:
            out.append(_chk(labels[3][0], labels[3][1], True, "practically no debt"))
        else:
            out.append(_chk(labels[3][0], labels[3][1], None if ic is None else ic >= c["min_interest_cover"],
                            "no interest cover figure" if ic is None else f"EBIT covers interest {_f(ic)}x"))
        fcf = row.get("fcf_cr")
        if debt is None and de is not None and eq:
            debt = de * eq
        if free:
            out.append(_chk(labels[4][0], labels[4][1], True, "practically no debt"))
        elif fcf is None or not debt:
            out.append(_chk(labels[4][0], labels[4][1], None, "no free cash flow or debt amount"))
        else:
            out.append(_chk(labels[4][0], labels[4][1], fcf / debt >= c["min_fcf_to_debt"],
                            f"free cash flow ₹{_f(fcf, 0)} cr = {_f(fcf / debt * 100, 0)} % of debt ₹{_f(debt, 0)} cr"))
    pl, pr = row.get("pledged_pct"), row.get("promoter_pct")
    lab = f"Promoter pledge under {c['max_pledge_pct']:.0f} %"
    if pl is None or not pr:
        out.append(_chk("low_pledge", lab, None, "no promoter or pledge figure"))
    else:
        out.append(_chk("low_pledge", lab, pl < c["max_pledge_pct"], f"{_f(pl)} % of promoter shares pledged"))
    return out


def _dividend(row, ctx, fin):
    c, sym = ctx["cfg"], row["symbol"]
    y = dividend_yield(row, ctx)
    q = ctx.get("yield_q")
    labels = [("notable", "Yield in the top 75 % of payers"), ("high", "Yield in the top 25 % of payers"),
              ("steady", "Paid in each of the last 2 years"), ("growing", "Dividend per share up on the year before"),
              ("earnings_cover", f"Covered by earnings (payout under {c['max_payout_pct']:.0f} %)"),
              ("cash_cover", "Covered by free cash flow")]
    if y is None:
        return [_chk(k, lab, None, "no dividend figure") for k, lab in labels]
    if y <= 0:
        return [_chk(k, lab, False, "pays no dividend") for k, lab in labels]
    out = []
    if q is None:
        out += [_chk(k, lab, None, f"yield {_f(y, 2)} %; too few dividend payers to rank") for k, lab in labels[:2]]
    else:
        out.append(_chk(*labels[0], y >= q[0], f"yield {_f(y, 2)} % vs {_f(q[0], 2)} %, the lower quartile of {q[2]} payers"))
        out.append(_chk(*labels[1], y >= q[1], f"yield {_f(y, 2)} % vs {_f(q[1], 2)} %, the upper quartile of {q[2]} payers"))
    y1, y0 = _dps(ctx, sym)
    if y1 is not None and y0 is not None:
        out.append(_chk(*labels[2], y1 > 0 and y0 > 0,
                        f"₹{_f(y1, 2)} a share in the last 12 months, ₹{_f(y0, 2)} in the 12 before (NSE calendar)"))
        out.append(_chk(*labels[3], y1 > y0 > 0 if y0 > 0 else False,
                        f"₹{_f(y1, 2)} vs ₹{_f(y0, 2)} a share (adjusted for later splits and bonuses)"))
    else:
        hist = ctx.get("_hist") or []
        recent = [h for h in hist if h["dividend_yield"] is not None and h["date"] > hist[0]["date"] - timedelta(days=760)]
        span = (recent[0]["date"] - recent[-1]["date"]).days if len(recent) >= 2 else 0
        if len(recent) >= 7 and span >= 630:
            paid = sum(1 for h in recent if h["dividend_yield"] > 0)
            out.append(_chk(*labels[2], paid == len(recent),
                            f"a dividend yield in {paid} of the {len(recent)} stored quarters over 2 years"))
        else:
            out.append(_chk(*labels[2], None, "the dividend calendar does not reach back 2 years yet"))
        out.append(_chk(*labels[3], None, "the dividend calendar does not reach back 2 years yet"))
    pay = _payout(row, ctx)
    eps = row.get("eps_ttm")
    if eps is not None and eps <= 0:
        out.append(_chk(*labels[4], False, "loss-making: the dividend is not covered by earnings"))
    else:
        out.append(_chk(*labels[4], None if pay is None else pay * 100 < c["max_payout_pct"],
                        "no P/E" if pay is None else f"payout {_f(pay * 100, 0)} % of earnings"))
    fcf, mc = row.get("fcf_cr"), row.get("market_cap_cr")
    if fin:
        out.append(_chk(*labels[5], None, "not meaningful for a bank or NBFC"))
    elif fcf is None or not mc:
        out.append(_chk(*labels[5], None, "no free cash flow or market cap"))
    else:
        paid = y / 100 * mc
        out.append(_chk(*labels[5], fcf >= paid, f"dividends ≈ ₹{_f(paid, 0)} cr a year vs free cash flow ₹{_f(fcf, 0)} cr"))
    return out


def evaluate(row: dict, hist: list, ctx: dict, fund: dict | None = None) -> dict:
    """The five axes for one screener row: {axes: [{key, label, passed, known, checks}], checks_passed, ...}."""
    fund = fund or {}
    fin = V.is_financial(row.get("industry"), fund)
    ctx = dict(ctx, _hist=hist)
    axes = []
    for (key, label), checks in zip(AXES, (_value(row, ctx), _growth(row, ctx), _past(row, hist, ctx, fin),
                                           _health(row, hist, ctx, fin, fund), _dividend(row, ctx, fin))):
        axes.append({"key": key, "label": label, "checks": checks,
                     "passed": sum(1 for x in checks if x["pass"]),
                     "known": sum(1 for x in checks if x["pass"] is not None)})
    out = {"symbol": row["symbol"], "financial": fin, "axes": axes,
           "checks_passed": sum(a["passed"] for a in axes), "checks_known": sum(a["known"] for a in axes)}
    for a in axes:
        out[f"{a['key']}_checks"] = a["passed"]
    return out


def apply(conn, rows: list, as_of, funds: dict | None = None, cfg: dict | None = None) -> list:
    """Score every screener row in place (count fields + the detail under "_scorecard")."""
    cfg = cfg or settings()
    syms = [r["symbol"] for r in rows]
    try:
        hist = load_history(conn, syms, as_of)
    except Exception:
        hist = {}
    try:
        divs, first = load_dividends(conn, syms, as_of)
    except Exception:
        divs, first = {}, None
    ctx = context(rows, divs, first, as_of, cfg)
    funds = funds or {}
    for r in rows:
        if not (funds.get(r["symbol"]) or hist.get(r["symbol"])):      # technical-only row: nothing to score
            for k in COUNT_FIELDS:
                r[k] = None
            continue
        sc = evaluate(r, hist.get(r["symbol"], []), ctx, funds.get(r["symbol"]))
        for k in COUNT_FIELDS:
            r[k] = sc[k]
        r["_scorecard"] = sc
    return rows


def for_symbol(conn, symbol: str, as_of=None) -> dict | None:
    from research import screener as SC
    kw = {"as_of": as_of} if as_of else {}
    rows, built = SC.snapshot(conn, **kw)
    row = next((r for r in rows if r["symbol"] == symbol.upper()), None)
    if not row or not row.get("_scorecard"):
        return None
    return dict(row["_scorecard"], as_of=str(as_of or date.today()), price=row.get("price"), industry=row.get("industry"),
                research_rating=row.get("research_rating"), snapshot_at=built)


# ── storing the day's scorecards and their record ───────────────────────────

def store(conn, rows: list, as_of=None) -> int:
    ensure_tables(conn)
    as_of = str(as_of or date.today())
    n = 0
    for r in rows:
        sc = r.get("_scorecard")
        if not sc:
            continue
        flags = {a["key"]: [None if x["pass"] is None else int(x["pass"]) for x in a["checks"]] for a in sc["axes"]}
        conn.execute("""INSERT INTO fundamental_scorecard (symbol, as_of, price, checks_passed, checks_known, value_checks,
                            growth_checks, past_checks, health_checks, dividend_checks, checks_json, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(symbol, as_of) DO UPDATE SET price=excluded.price,
                            checks_passed=excluded.checks_passed, checks_known=excluded.checks_known,
                            value_checks=excluded.value_checks, growth_checks=excluded.growth_checks,
                            past_checks=excluded.past_checks, health_checks=excluded.health_checks,
                            dividend_checks=excluded.dividend_checks, checks_json=excluded.checks_json""",
                     (r["symbol"], as_of, r.get("price"), sc["checks_passed"], sc["checks_known"], sc["value_checks"],
                      sc["growth_checks"], sc["past_checks"], sc["health_checks"], sc["dividend_checks"],
                      json.dumps(flags), datetime.now()))
        n += 1
    conn.commit()
    return n


def _band(n):
    return next((b for b, lo, hi in BANDS if lo <= n <= hi), None)


def record(conn, horizon: int = 60) -> dict:
    """Return minus the Nifty's over the next `horizon` sessions after each stored scorecard, by band of
    checks passed. One sample per stock per calendar month (the month's first stored day), so a slow-moving
    score is not counted twenty times."""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    from research import regime_gate as RG
    from research.tech_signals import _value_at
    ensure_tables(conn)
    rows = conn.execute("SELECT symbol, as_of, checks_passed FROM fundamental_scorecard WHERE checks_passed IS NOT NULL "
                        "ORDER BY symbol, as_of").fetchall()
    first = {}
    for sym, d, n in rows:
        first.setdefault((sym, str(d)[:7]), (_d(d), n))
    bands = {b: [] for b, _, _ in BANDS}
    if first:
        start = min(d for d, _ in first.values())
        nifty, _src = RG.nifty_closes(conn, date.today(), (date.today() - start).days + 10)
        by_sym = {}
        for (sym, _m), v in first.items():
            by_sym.setdefault(sym, []).append(v)
        for sym, samples in by_sym.items():
            s0 = min(d for d, _ in samples)
            bars = conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date>=? AND close>0 ORDER BY date",
                                (sym, str(s0))).fetchall()
            days, closes = [_d(b[0]) for b in bars], [float(b[1]) for b in bars]
            for d0, n in samples:
                i0 = next((i for i, d in enumerate(days) if d >= d0), None)
                if i0 is None or days[i0] != d0 or i0 + horizon >= len(days):
                    continue
                n0, n1 = _value_at(nifty, d0), _value_at(nifty, days[i0 + horizon])
                if not (n0 and n1):
                    continue
                bands[_band(n)].append((closes[i0 + horizon] / closes[i0] - (n1 / n0)) * 100)
    out = []
    for b, _, _ in BANDS:
        xs = bands[b]
        out.append({"band": b, "n": len(xs), "enough": len(xs) >= MIN_RECORD,
                    "beat_nifty_pct": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1) if xs else None,
                    "median_excess_pct": round(statistics.median(xs), 2) if xs else None,
                    "mean_excess_pct": round(sum(xs) / len(xs), 2) if xs else None})
    lo, hi = out[0], out[-1]
    spread = (round(hi["mean_excess_pct"] - lo["mean_excess_pct"], 2)
              if lo["enough"] and hi["enough"] else None)
    return {"horizon": horizon, "bands": out, "top_minus_bottom_pct": spread, "min_record": MIN_RECORD,
            "stored_days": len({str(r[1])[:10] for r in rows})}


def main(argv=None):
    import argparse
    from db.schema import get_connection
    p = argparse.ArgumentParser(description="Explainable fundamental scorecard")
    p.add_argument("cmd", choices=("show", "record"))
    p.add_argument("symbol", nargs="?")
    p.add_argument("--horizon", type=int, default=60)
    a = p.parse_args(argv)
    conn = get_connection()
    try:
        if a.cmd == "show":
            sc = for_symbol(conn, a.symbol or "")
            if not sc:
                print("no scorecard (no fundamentals stored for this symbol?)")
                return
            print(f"{sc['symbol']}: {sc['checks_passed']} of 30 checks passed ({sc['checks_known']} known)")
            for ax in sc["axes"]:
                print(f"  {ax['label']}: {ax['passed']}/6")
                for x in ax["checks"]:
                    mark = "✓" if x["pass"] else "·" if x["pass"] is None else "✗"
                    print(f"    {mark} {x['label']}: {x['detail']}")
        else:
            print(json.dumps(record(conn, a.horizon), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
