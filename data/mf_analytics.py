"""
W39b: mutual fund analytics on the AMFI NAVs ATIP already stores (mf_nav, written daily by
data/multi_asset.py; history_mf backfills one scheme's past NAVs on demand).

Read-only research on market data. Nothing here buys, holds or recommends a scheme, and mutual
funds stay out of the wealth ledger: wealth/assets.py still refuses MUTUAL_FUND (the owner's Scope
Exclusions sheet, W11-W20).

    load(conn, scheme_code, as_of=None)       the stored NAV history and the scheme's facts (name, AMC,
                                              AMFI category, plan / option parsed from the name)
    analytics(conn, scheme_code, ...)         point-to-point and since-first-NAV returns, rolling returns,
                                              risk (volatility, drawdown with dates, Sharpe, Sortino), beta /
                                              alpha / tracking error against a stored index, category rank
    sip(conn, scheme_code, amount, day, start, end=None, lump_sum=None, step_up_pct=None, stamp_duty=True,
        round_units=True, exit_load_pct=None, exit_load_days=None)
                                              a monthly SIP (optionally stepping up each year) and a lump sum
                                              on the real NAV history, with XIRR
    purchase(amount, nav, stamp_duty, round_units)   the units one purchase is allotted, and its stamp duty
    periods_per_year(intervals)               NAV observations a year, from the calendar days each return used
                                              spans (annualises the risk figures)
    category_rank(conn, scheme_code, ...)     the scheme against the others ATIP stores in its AMFI category
    compare(conn, codes, as_of=None)          two to ten schemes side by side, on a common as-of date

CONVENTIONS (stated, not invented)
  Look-back NAV   a past point ("1Y ago") uses the last NAV ON OR BEFORE the target date (a holiday ->
                  the previous NAV), and only if it is at most MAX_STALE_DAYS (7) calendar days older than
                  the target; otherwise the figure is None with the reason. Nothing is interpolated.
  Months / years  1M / 3M / 6M = the same day N months earlier, clamped to month end (31 Mar - 1M =
                  28/29 Feb); 1Y / 3Y / 5Y = the same day N years earlier (29 Feb -> 28 Feb).
  Day count       Actual/365 throughout -- the day count of wealth.perf.metrics.xirr -- so a lump sum's
                  CAGR equals its XIRR: CAGR = (NAV_end / NAV_start) ** (365 / days) - 1, days between the
                  two NAV dates actually used.
  Point to point  1M, 3M, 6M and 1Y absolute (NAV_end / NAV_start - 1); 3Y and 5Y also as CAGR (SEBI's
                  factsheet rule: up to a year absolute, beyond a year compounded).
  Since first NAV since the FIRST NAV ATIP STORES -- not necessarily the fund's launch (run history_mf to
                  backfill). Its CAGR is given only when that span is a year or more.
  Rolling         1Y and 3Y windows ending on every stored NAV date whose start NAV passes the look-back
                  rule; each window annualised over its actual days. min / median (statistics.median: the
                  mean of the middle two for an even count) / max with dates, mean, and the % of windows
                  > 0 and > the hurdle (default 7 % a year, FD-like: config mf_analytics.hurdle_pct, or
                  ?hurdle=; a window exactly at the hurdle has not beaten it). At least MIN_WINDOWS (10)
                  windows, else None with the reason. The chart series is thinned to <= 600 points.
  Risk            daily returns between consecutive NAVs over the trailing risk window (default 3 years,
                  the whole stored history when shorter -- said so). A return across a gap of more than
                  MAX_STALE_DAYS is left out (it covers weeks, not a session) and counted. Volatility,
                  Sharpe and Sortino as backtest/metrics.py (sample stdev, the risk-free rate compounded
                  down to one NAV interval, target downside deviation over all intervals), but annualised
                  with the window's ACTUAL number of NAV observations a year, not a fixed 252:
                      periods_per_year = returns used x 365 / the calendar days those returns span
                  (a return left out across a gap takes its days out too, so a hole in ATIP's history does
                  not make the fund look less frequent). Why not 252: an equity fund's NAVs follow NSE
                  sessions (~248-250 a year, close to 252), but liquid and overnight funds publish a NAV
                  for every calendar day (365 a year) -- with 252 their volatility would read sqrt(252/365)
                  = 0.83x of the truth -- and a NAV every weekday is ~261. Alpha, tracking error and the
                  information ratio use the same figure (risk.periods_per_year); beta does not depend on
                  it. Risk-free = wealth.risk_free_pct (6.5 % by default; ?rf= overrides). At least MIN_OBS
                  (60) returns. The category rank's 1Y volatility is annualised the same way.
  Max drawdown    NAV / running peak - 1, with the peak, trough and recovery dates (recovery = the first
                  NAV back at or above the peak; None = not recovered yet), over the risk window and over
                  the whole stored history.
  Benchmark       a prices_daily close series (default mf_analytics.benchmark, else wealth.benchmark =
                  NIFTY50). Matched dates: a NAV interval counts only when the index has a close on BOTH
                  of its dates, so both returns cover the same days. Beta, Jensen's alpha (annual) and
                  tracking error from wealth.perf.metrics.series_metrics. A NAV includes reinvested
                  dividends and a price index does not, so alpha is flattered by about the index's dividend
                  yield (~1-1.5 % a year for the NIFTY 50).
  SIP             an instalment on day D of every month (clamped to month end) from start to end; a date
                  with no NAV (holiday, weekend) buys at the NEXT stored NAV. Value = units x the last NAV
                  on or before end. XIRR on [(allotment date, -amount paid)..., (valuation date, +value
                  after any exit load)] with wealth.perf.metrics.xirr. The lump sum puts the same total (or
                  ?lump_sum=) in at the first NAV on or after start, bought and redeemed by the same rules.
                  A start more than MAX_STALE_DAYS before the first stored NAV is refused (INSUFFICIENT +
                  reason): ATIP does not have the NAVs those instalments would have bought at. No start: the
                  last 3 years.
    step-up       ?step_up_pct= (0 by default): the instalment rises by that % a year on each anniversary
                  of the FIRST instalment (every 12th month from it, not the calendar year), compounding:
                  amount x (1 + p) ** k after k anniversaries, rounded to the paisa.
    stamp duty    ON by default (?stamp_duty=0 turns it off): 0.005 % of every purchase (Indian Stamp Act,
                  on mutual fund purchases since 1 July 2020), taken out of the amount before units are
                  allotted: units = amount x (1 - 0.00005) / NAV. The amount paid (the XIRR outflow) is the
                  full instalment.
    unit rounding ON by default (?round_units=0 turns it off): units are allotted to 3 decimals, rounded
                  DOWN, as the RTAs (CAMS, KFintech) do -- computed in decimal arithmetic, so 99.995 stays
                  99.995 rather than a float's 99.99499... The few paise this leaves out are lost, as they
                  are for an investor.
    exit load     OFF by default: ?exit_load_pct=1&exit_load_days=365 charges that flat % of the
                  redemption value of each instalment's units still held for FEWER than N days at the
                  valuation date (held N days or more: free), per instalment as AMCs apply it (first in,
                  first out). Value stays the market value; value_after_exit_load (= value when off) is
                  what gain, the absolute return and XIRR are measured on.
  Category        the AMFI category stored with the scheme's latest NAVAll row. Peers = schemes ATIP stores
                  in the same category with a NAV within MAX_STALE_DAYS of the as-of date and, by default,
                  the same plan (Direct / Regular) and option (Growth / IDCW) parsed from the name -- a
                  heuristic. IDCW NAVs fall at every payout, so their returns understate the fund's.
                  Coverage is stated: only schemes ATIP stores, and per metric only those with the history.

Config (config.json "mf_analytics", all optional):
    {"hurdle_pct": 7.0, "benchmark": null, "risk_years": 3, "max_peers": 300}
"""

from __future__ import annotations

import math
import re
import statistics
from bisect import bisect_left, bisect_right
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

MAX_STALE_DAYS = 7
STAMP_DUTY_RATE = 0.00005                      # 0.005 % of every purchase (since 1 July 2020)
UNIT_DECIMALS = 3                              # units allotted to 3 decimals, rounded down
DAYS_A_YEAR = 365                              # the module's day count (Actual/365)
MIN_OBS = 60
MIN_WINDOWS = 10
ABOVE_EPS = 1e-9                              # a billionth: float noise is not a return above the hurdle
PERIODS = (("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12), ("3Y", 36), ("5Y", 60))
ROLLING = (("1Y", 1), ("3Y", 3))
DEFAULTS = {"hurdle_pct": 7.0, "benchmark": None, "risk_years": 3, "max_peers": 300}
MAX_CHART_POINTS = 600
NOTE = ("Market-data analytics on AMFI NAVs for personal research. Not SEBI-registered investment advice and "
        "not a recommendation to buy, hold or sell any scheme; past returns do not indicate future returns.")


def settings() -> dict:
    try:
        from ops.config import load as _load
        raw = _load().get("mf_analytics") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k in ("hurdle_pct", "risk_years", "max_peers"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    if isinstance(raw.get("benchmark"), str) and raw["benchmark"].strip():
        out["benchmark"] = raw["benchmark"].strip()
    if not -50 <= out["hurdle_pct"] <= 100:
        out["hurdle_pct"] = DEFAULTS["hurdle_pct"]          # a malformed value keeps the default
    out["risk_years"] = int(max(1, min(10, out["risk_years"])))
    out["max_peers"] = int(max(10, min(2000, out["max_peers"])))
    return out


# ── small helpers ─────────────────────────────────────────────────────────
def _d(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _date_arg(v, name):
    if v is None or v == "":
        return None
    try:
        return _d(v)
    except ValueError:
        raise ValueError(f"{name} must be a date YYYY-MM-DD") from None


def _num(v, lo, hi, name):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if x != x or not lo <= x <= hi:
        raise ValueError(f"{name} must be {lo}..{hi}")
    return x


def _flag(v, default: bool, name: str) -> bool:
    """True / False, 1 / 0, "on" / "off" ... (a query string's flag); None or "" -> the default."""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be 1 / 0 (true / false)")


def _code(v) -> str:
    c = str(v or "").strip()
    if not c.isdigit() or len(c) > 12:
        raise ValueError("scheme must be AMFI's numeric scheme code")
    return c


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _pct(x, nd=3):
    return None if x is None else round(x * 100, nd)


def _cagr(ratio, days):
    if ratio is None or ratio <= 0 or days <= 0:
        return None
    return ratio ** (365.0 / days) - 1


def _rf_pct(rf_pct):
    if rf_pct is not None and rf_pct != "":
        return _num(rf_pct, 0, 30, "rf")
    try:
        from wealth.config import settings as wealth_settings
        return float(wealth_settings()["risk_free_pct"])
    except Exception:
        return 6.5


def _benchmark_symbol(name):
    from wealth.perf.data import benchmark_symbol
    if name:
        if not re.match(r"^[A-Za-z0-9_&.-]{1,40}$", str(name)):
            raise ValueError("benchmark must be a prices_daily symbol")
        return benchmark_symbol(str(name))
    cfg = settings()
    if cfg["benchmark"]:
        return benchmark_symbol(cfg["benchmark"])
    try:
        from wealth.config import settings as wealth_settings
        return benchmark_symbol(wealth_settings()["benchmark"])
    except Exception:
        return "NIFTY50"


# ── scheme facts ──────────────────────────────────────────────────────────
def parse_category(cat):
    """'Open Ended Schemes(Equity Scheme - Large Cap Fund)' ->
    {structure: 'Open Ended Schemes', label: 'Equity Scheme - Large Cap Fund', asset_class: 'Equity Scheme',
     sub_category: 'Large Cap Fund'}"""
    if not cat:
        return None
    m = re.match(r"^\s*(.*?)\s*\((.*)\)\s*$", str(cat))
    structure, inner = (m.group(1) or None, m.group(2).strip()) if m else (None, str(cat).strip())
    parts = [p.strip() for p in inner.split(" - ", 1)]
    return {"structure": structure, "label": inner, "asset_class": parts[0] or None,
            "sub_category": parts[1] if len(parts) > 1 else None}


def plan_option(name):
    """Plan (Direct / Regular) and option (Growth / IDCW / Bonus) from an AMFI scheme name. Words of the
    fund's own name ('Dividend Yield Fund', 'Growth Opportunities') are skipped by reading only the parts
    after the first ' - ' when there are any. A heuristic: None when the name does not say."""
    n = str(name or "")
    segs = [s for s in re.split(r"\s*-\s*", n) if s]
    tail = " ".join(segs[1:]) if len(segs) > 1 else n
    t = tail.lower()
    plan = "Direct" if re.search(r"\bdirect\b", t) else ("Regular" if re.search(r"\bregular\b", t) else None)
    if re.search(r"\bbonus\b", t):
        option = "Bonus"
    elif re.search(r"\b(idcw|dividend|payout|reinvestment|reinvest)\b", t):
        option = "IDCW"
    elif re.search(r"\bgrowth\b", t):
        option = "Growth"
    else:
        option = None
    return plan, option


def facts(conn, code: str) -> dict:
    r = conn.execute("SELECT scheme_name, isin_growth, isin_reinvest FROM mf_nav WHERE scheme_code=? AND "
                     "scheme_name IS NOT NULL ORDER BY date DESC LIMIT 1", (code,)).fetchone()
    c = conn.execute("SELECT category, amc FROM mf_nav WHERE scheme_code=? AND category IS NOT NULL "
                     "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
    name = r[0] if r else None
    plan, option = plan_option(name)
    cat = c[0] if c else None
    return {"scheme_code": code, "scheme_name": name, "isin_growth": r[1] if r else None,
            "isin_reinvest": r[2] if r else None, "amc": c[1] if c else None, "category": cat,
            "category_parsed": parse_category(cat), "plan": plan, "option": option}


# ── NAV history ───────────────────────────────────────────────────────────
class Navs:
    """A scheme's stored NAVs, oldest first, with the rows that could not be used counted."""

    def __init__(self, code, dates, navs, bad_rows=0):
        self.code, self.dates, self.navs, self.bad_rows = code, dates, navs, bad_rows

    def __len__(self):
        return len(self.dates)

    def look_back(self, target: date):
        """(index, None) of the last NAV on or before target, if at most MAX_STALE_DAYS older; else
        (None, reason)."""
        j = bisect_right(self.dates, target) - 1
        if j < 0:
            return None, f"NAV history starts {self.dates[0]}, after the {target} start point"
        if (target - self.dates[j]).days > MAX_STALE_DAYS:
            return None, (f"no NAV within {MAX_STALE_DAYS} days on or before {target} "
                          f"(the nearest earlier NAV is {self.dates[j]})")
        return j, None

    def next_on_or_after(self, target: date):
        k = bisect_left(self.dates, target)
        return k if k < len(self.dates) else None

    def gaps(self):
        return [{"from": self.dates[i - 1], "to": self.dates[i], "days": (self.dates[i] - self.dates[i - 1]).days}
                for i in range(1, len(self.dates)) if (self.dates[i] - self.dates[i - 1]).days > MAX_STALE_DAYS]


def _rows_to_navs(code, rows):
    dates, navs, bad = [], [], 0
    for d, v in rows:
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = None
        if x is None or x != x or x <= 0 or math.isinf(x):
            bad += 1                          # NULL, 'N.A.' or non-positive NAV: never used, counted
            continue
        dates.append(_d(d))
        navs.append(x)
    return Navs(code, dates, navs, bad)


def load(conn, scheme_code, as_of=None) -> Navs:
    code = _code(scheme_code)
    as_of = _date_arg(as_of, "as_of")
    sql, args = "SELECT date, nav FROM mf_nav WHERE scheme_code=?", [code]
    if as_of:
        sql += " AND date<=?"
        args.append(str(as_of))
    s = _rows_to_navs(code, conn.execute(sql + " ORDER BY date", args).fetchall())
    if not len(s):
        if conn.execute("SELECT 1 FROM mf_nav WHERE scheme_code=? LIMIT 1", (code,)).fetchone():
            raise LookupError(f"scheme {code} has no usable NAV" + (f" on or before {as_of}" if as_of else ""))
        raise LookupError(f"no NAVs stored for scheme {code} (data.multi_asset stores AMFI's daily file; "
                          f"python -m data.multi_asset --mf-history {code} <start> backfills one scheme)")
    return s


# ── returns ───────────────────────────────────────────────────────────────
def point_to_point(s: Navs, months: int, i_end: int | None = None) -> dict:
    i_end = len(s) - 1 if i_end is None else i_end
    end_d = s.dates[i_end]
    target = add_months(end_d, -months)
    out = {"months": months, "start_target": target, "end_date": end_d, "end_nav": s.navs[i_end],
           "annualised": months > 12}
    j, why = s.look_back(target)
    if j is None:
        return {**out, "absolute_pct": None, "cagr_pct": None, "reason": why}
    days = (end_d - s.dates[j]).days
    ratio = s.navs[i_end] / s.navs[j]
    return {**out, "start_date": s.dates[j], "start_nav": s.navs[j], "days": days, "absolute_pct": _pct(ratio - 1),
            "cagr_pct": _pct(_cagr(ratio, days)) if months > 12 else None, "reason": None}


def since_first(s: Navs) -> dict:
    days = (s.dates[-1] - s.dates[0]).days
    ratio = s.navs[-1] / s.navs[0]
    out = {"first_date": s.dates[0], "first_nav": s.navs[0], "end_date": s.dates[-1], "end_nav": s.navs[-1],
           "days": days, "absolute_pct": _pct(ratio - 1) if days else None,
           "note": "since the first NAV ATIP stores, not necessarily the fund's launch"}
    if days < 365:
        return {**out, "cagr_pct": None, "reason": f"stored history is {days} days; CAGR needs a year or more"}
    return {**out, "cagr_pct": _pct(_cagr(ratio, days)), "reason": None}


def rolling(s: Navs, years: int, hurdle_pct: float, max_points: int = MAX_CHART_POINTS) -> dict:
    months = 12 * years
    vals, skipped = [], 0
    for i, d in enumerate(s.dates):
        target = add_months(d, -months)
        if target < s.dates[0]:
            continue
        j, _ = s.look_back(target)
        if j is None:
            skipped += 1                      # the start falls in a gap in the stored history
            continue
        vals.append((d, _cagr(s.navs[i] / s.navs[j], (d - s.dates[j]).days)))
    out = {"window_years": years, "windows": len(vals), "skipped_for_gaps": skipped, "hurdle_pct": hurdle_pct}
    if len(vals) < MIN_WINDOWS:
        why = (f"stored history is {(s.dates[-1] - s.dates[0]).days} days: no complete {years}Y window"
               if not vals and not skipped else
               f"only {len(vals)} complete {years}Y windows; at least {MIN_WINDOWS} are needed")
        return {**out, "stats": None, "series": [], "reason": why}
    rs = [r for _, r in vals]
    lo = min(range(len(vals)), key=lambda k: rs[k])
    hi = max(range(len(vals)), key=lambda k: rs[k])
    h = hurdle_pct / 100.0
    step = max(1, math.ceil(len(vals) / max_points))
    idx = list(range(len(vals) - 1, -1, -step))[::-1]           # always keeps the latest window
    # "above" = more than ABOVE_EPS above: 110 / 100 - 1 is 0.10000000000000009 in floating point,
    # and a window that earned exactly the hurdle has not beaten it
    return {**out, "reason": None, "series_step": step,
            "stats": {"min_pct": _pct(rs[lo]), "min_end_date": vals[lo][0], "median_pct": _pct(statistics.median(rs)),
                      "max_pct": _pct(rs[hi]), "max_end_date": vals[hi][0], "mean_pct": _pct(sum(rs) / len(rs)),
                      "pct_positive": round(100.0 * sum(1 for r in rs if r > ABOVE_EPS) / len(rs), 2),
                      "pct_above_hurdle": round(100.0 * sum(1 for r in rs if r > h + ABOVE_EPS) / len(rs), 2),
                      "latest_pct": _pct(rs[-1]), "first_end_date": vals[0][0], "last_end_date": vals[-1][0]},
            "series": [{"date": vals[k][0], "return_pct": _pct(vals[k][1])} for k in idx]}


# ── risk ──────────────────────────────────────────────────────────────────
def drawdown(dates: list, navs: list) -> dict:
    if len(navs) < 2:
        return {"max_drawdown_pct": None, "reason": "fewer than two NAVs"}
    peak, worst = 0, (0.0, None, None)
    for i, v in enumerate(navs):
        if v >= navs[peak]:
            peak = i
        dd = v / navs[peak] - 1
        if dd < worst[0]:
            worst = (dd, peak, i)
    top = max(navs)
    cur = navs[-1] / top - 1
    if worst[1] is None:
        return {"max_drawdown_pct": 0.0, "peak_date": None, "trough_date": None, "recovery_date": None,
                "recovered": True, "current_drawdown_pct": _pct(cur), "reason": "the NAV never fell below a peak"}
    dd, p, t = worst
    rec = next((k for k in range(t + 1, len(navs)) if navs[k] >= navs[p]), None)
    return {"max_drawdown_pct": _pct(dd), "peak_date": dates[p], "peak_nav": navs[p], "trough_date": dates[t],
            "trough_nav": navs[t], "recovery_date": dates[rec] if rec is not None else None,
            "recovered": rec is not None, "days_peak_to_trough": (dates[t] - dates[p]).days,
            "days_trough_to_recovery": (dates[rec] - dates[t]).days if rec is not None else None,
            "current_drawdown_pct": _pct(cur), "reason": None}


def _bench_closes(conn, symbol, start, end) -> dict:
    out = {}
    for d, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date>=? AND date<=? AND close>0 "
                             "ORDER BY date", (symbol, str(start), str(end))):
        out[_d(d)] = float(c)
    return out


def risk(conn, s: Navs, rf_pct: float, years: int, benchmark: str | None = None) -> dict:
    from wealth.perf import metrics as M
    last = s.dates[-1]
    target = add_months(last, -12 * years)
    whole = target < s.dates[0]
    i0 = 0 if whole else max(0, bisect_right(s.dates, target) - 1)
    window = {"start": s.dates[i0], "end": last, "years": years,
              "note": (f"stored history is shorter than {years}Y: the whole history is used" if whole else None)}
    dates, rets, spans, skipped = [], [], [], 0
    for i in range(i0 + 1, len(s)):
        if (s.dates[i] - s.dates[i - 1]).days > MAX_STALE_DAYS:
            skipped += 1
            continue
        dates.append(s.dates[i])
        rets.append(s.navs[i] / s.navs[i - 1] - 1)
        spans.append((s.dates[i] - s.dates[i - 1]).days)
    ppy = periods_per_year(spans)
    window.update({"returns": len(rets), "excluded_gap_returns": skipped, "days_spanned": sum(spans)})
    out = {"window": window, "risk_free_pct": rf_pct,
           "periods_per_year": round(ppy, 4) if ppy else None,
           "periods_basis": "NAV observations a year in the window: returns used x 365 / the calendar days they "
                            "span (not a fixed 252)",
           "max_drawdown": drawdown(s.dates[i0:], s.navs[i0:]),
           "max_drawdown_full_history": drawdown(s.dates, s.navs)}
    if len(rets) < MIN_OBS:
        out.update({"volatility_pct": None, "sharpe": None, "sortino": None,
                    "reason": f"{len(rets)} daily returns in the window; at least {MIN_OBS} are needed"})
        out["benchmark"] = {"symbol": benchmark, "status": "INSUFFICIENT", "reason": out["reason"]}
        return out
    m = M.series_metrics(dates, rets, None, rf_pct, periods_per_year=ppy)
    out.update({"volatility_pct": m.get("volatility_pct"), "sharpe": m.get("sharpe"), "sortino": m.get("sortino"),
                "reason": None})
    out["benchmark"] = _vs_benchmark(conn, s, i0, dates, rets, rf_pct, benchmark, ppy)
    return out


def periods_per_year(intervals: list) -> float | None:
    """NAV observations a year: the number of returns x 365 / the calendar days they span (each return's
    interval in days). 365 for a NAV every calendar day (a liquid fund), ~261 for every weekday, ~248-250 on
    NSE sessions. None without a return."""
    days = sum(intervals)
    return len(intervals) * DAYS_A_YEAR / days if days > 0 else None


def _vs_benchmark(conn, s, i0, dates, rets, rf_pct, symbol, ppy=None):
    from wealth.perf import metrics as M
    if not symbol:
        return {"symbol": None, "status": "UNAVAILABLE", "reason": "no benchmark"}
    closes = _bench_closes(conn, symbol, s.dates[i0], s.dates[-1])
    if not closes:
        return {"symbol": symbol, "status": "UNAVAILABLE",
                "reason": f"no {symbol} closes in prices_daily over {s.dates[i0]}..{s.dates[-1]}"}
    prev = {s.dates[i]: s.dates[i - 1] for i in range(i0 + 1, len(s))}
    bench = []
    for d in dates:
        b0, b1 = closes.get(prev[d]), closes.get(d)
        bench.append(b1 / b0 - 1 if b0 and b1 else None)
    matched = sum(1 for b in bench if b is not None)
    base = {"symbol": symbol, "matched_returns": matched, "fund_returns": len(rets),
            "basis": "NAV (dividends reinvested) vs a price index (dividends excluded): alpha is flattered by "
                     "about the index's dividend yield"}
    if matched < MIN_OBS:
        return {**base, "status": "INSUFFICIENT", "beta": None, "alpha_annual_pct": None, "tracking_error_pct": None,
                "reason": f"{matched} NAV intervals with an index close on both dates; at least {MIN_OBS} needed"}
    m = M.series_metrics(dates, rets, bench, rf_pct, periods_per_year=ppy or M.PERIODS)
    return {**base, "status": "OK", "beta": m.get("beta"), "alpha_annual_pct": m.get("alpha_annual_pct"),
            "tracking_error_pct": m.get("tracking_error_pct"), "information_ratio": m.get("information_ratio"),
            "excess_return_pct": m.get("excess_return_pct"), "reason": None}


# ── the scheme view ───────────────────────────────────────────────────────
def analytics(conn, scheme_code, *, hurdle_pct=None, rf_pct=None, benchmark=None, risk_years=None, as_of=None,
              peers=True) -> dict:
    cfg = settings()
    hurdle = cfg["hurdle_pct"] if hurdle_pct in (None, "") else _num(hurdle_pct, -50, 100, "hurdle")
    rf = _rf_pct(rf_pct)
    ry = cfg["risk_years"] if risk_years in (None, "") else int(_num(risk_years, 1, 10, "risk_years"))
    bsym = _benchmark_symbol(benchmark)
    s = load(conn, scheme_code, as_of)
    f = facts(conn, s.code)
    warnings = []
    if s.bad_rows:
        warnings.append(f"{s.bad_rows} stored rows have no usable NAV (NULL / not positive) and were skipped")
    if not as_of and (date.today() - s.dates[-1]).days > MAX_STALE_DAYS:
        warnings.append(f"the latest stored NAV is {s.dates[-1]}: the daily AMFI fetch (multi_asset) may not be "
                        f"running")
    if f["option"] in ("IDCW", "Bonus"):
        warnings.append("an IDCW / bonus option's NAV falls at every payout: its returns understate the fund's; "
                        "use the Growth option's scheme code for performance")
    gaps = s.gaps()
    if gaps:
        warnings.append(f"{len(gaps)} gaps of more than {MAX_STALE_DAYS} days in the stored NAVs "
                        f"(look-backs into them are None; returns across them are left out of risk)")
    out = {"scheme": f, "as_of": s.dates[-1], "latest_nav": s.navs[-1],
           "history": {"first": s.dates[0], "last": s.dates[-1], "navs": len(s), "unusable_rows": s.bad_rows,
                       "gaps": sorted(gaps, key=lambda g: -g["days"])[:10], "gap_count": len(gaps)},
           "returns": {k: point_to_point(s, m) for k, m in PERIODS},
           "since_first_nav": since_first(s),
           "rolling": {k: rolling(s, y, hurdle) for k, y in ROLLING},
           "risk": risk(conn, s, rf, ry, bsym),
           "warnings": warnings, "note": NOTE,
           "conventions": {"look_back": f"last NAV on or before the target, at most {MAX_STALE_DAYS} days older",
                           "day_count": "Actual/365 (as wealth.perf.metrics.xirr)",
                           "point_to_point": "1M-1Y absolute; 3Y, 5Y also CAGR",
                           "rolling": "each window annualised over its actual days",
                           "risk": "annualised with the window's NAV observations a year (risk.periods_per_year), "
                                   "not a fixed 252; rf compounded per NAV interval; gap returns excluded",
                           "hurdle_pct": hurdle, "risk_free_pct": rf}}
    if peers:
        try:
            out["category"] = category_rank(conn, s.code, as_of=s.dates[-1], _target=s)
        except (LookupError, ValueError) as e:
            out["category"] = {"status": "UNAVAILABLE", "reason": str(e)}
    return out


# ── SIP and lump sum ──────────────────────────────────────────────────────
_UNIT_STEP = Decimal(1).scaleb(-UNIT_DECIMALS)                     # 0.001


def purchase(amount: float, nav: float, stamp_duty: bool = True, round_units: bool = True) -> tuple:
    """(units allotted, stamp duty) for one purchase of `amount` at `nav`:
    units = amount x (1 - 0.00005) / NAV with the stamp duty (amount / NAV without), rounded DOWN to 3
    decimals with round_units -- in decimal arithmetic, so an exact 99.995 is not floored to 99.994 by a
    float's 99.99499999..."""
    duty = amount * STAMP_DUTY_RATE if stamp_duty else 0.0
    if not round_units:
        return (amount - duty) / nav, duty
    net = Decimal(repr(float(amount)))
    if stamp_duty:
        net *= 1 - Decimal(repr(STAMP_DUTY_RATE))
    units = (net / Decimal(repr(float(nav)))).quantize(_UNIT_STEP, rounding=ROUND_DOWN)
    return float(units), duty


def _step_up(a: float, pct: float, first: date, sched: date) -> tuple:
    """(the instalment on `sched`, anniversaries passed): a x (1 + pct) ** k, k = the anniversaries of the
    first instalment on or before sched (every 12th month from it; the schedule keeps one day of the month)."""
    k = ((sched.year - first.year) * 12 + sched.month - first.month) // 12
    return (round(a * (1 + pct / 100.0) ** k, 2) if k and pct else a), k


def sip(conn, scheme_code, amount, day=1, start=None, end=None, lump_sum=None, step_up_pct=None,
        stamp_duty=True, round_units=True, exit_load_pct=None, exit_load_days=None) -> dict:
    from wealth.perf.metrics import xirr
    if amount in (None, ""):
        raise ValueError("amount is required")
    a = _num(amount, 1, 1e9, "amount")
    dd = _num(1 if day in (None, "") else day, 1, 31, "day")
    if dd != int(dd):
        raise ValueError("day must be a whole day of the month 1..31")
    dom = int(dd)
    lump = None if lump_sum in (None, "") else _num(lump_sum, 1, 1e11, "lump_sum")
    step = 0.0 if step_up_pct in (None, "") else _num(step_up_pct, 0, 100, "step_up_pct")
    duty_on = _flag(stamp_duty, True, "stamp_duty")
    round_on = _flag(round_units, True, "round_units")
    load_pct = 0.0 if exit_load_pct in (None, "") else _num(exit_load_pct, 0, 10, "exit_load_pct")
    if exit_load_days not in (None, "") and not load_pct:
        raise ValueError("exit_load_days needs exit_load_pct")
    load_days = None
    if load_pct:
        ld = _num(365 if exit_load_days in (None, "") else exit_load_days, 1, 3650, "exit_load_days")
        if ld != int(ld):
            raise ValueError("exit_load_days must be a whole number of days")
        load_days = int(ld)
    s = load(conn, scheme_code)
    f = facts(conn, s.code)
    st = _date_arg(start, "start")
    en = _date_arg(end, "end") or s.dates[-1]
    defaulted = st is None
    if defaulted:                             # the last 3 years, or the whole stored history when shorter
        st = max(s.dates[0], add_months(en, -36))
    if st >= en:
        raise ValueError("start must be before end")
    inputs = {"amount": a, "day": dom, "start": st, "end": en, "lump_sum": lump,
              "start_defaulted": defaulted, "step_up_pct": step, "stamp_duty": duty_on, "round_units": round_on,
              "exit_load_pct": load_pct or None, "exit_load_days": load_days}
    base = {"scheme": f, "inputs": inputs, "note": NOTE,
            "conventions": "holiday -> next NAV; "
                           + ("0.005 % stamp duty off each purchase; " if duty_on else "no stamp duty; ")
                           + ("units rounded down to 3 decimals; " if round_on else "units unrounded; ")
                           + (f"step-up {step:g} % a year on each anniversary of the first instalment; " if step
                              else "")
                           + (f"exit load {load_pct:g} % on units held under {load_days} days at the valuation "
                              f"date; " if load_pct else "no exit load; ")
                           + "XIRR Actual/365 on allotment dates"}
    # a start up to MAX_STALE_DAYS before the first stored NAV is a holiday start (buys at that first NAV);
    # earlier than that, ATIP does not have the NAVs the instalments would have bought at
    if (s.dates[0] - st).days > MAX_STALE_DAYS or en < s.dates[0]:
        return {**base, "status": "INSUFFICIENT",
                "reason": f"stored NAV history starts {s.dates[0]}; choose a start on or after it "
                          f"(or backfill: python -m data.multi_asset --mf-history {s.code} {st})"}
    v = bisect_right(s.dates, en) - 1
    val_d, val_nav = s.dates[v], s.navs[v]
    warnings = []
    if (en - val_d).days > MAX_STALE_DAYS:
        warnings.append(f"no NAV within {MAX_STALE_DAYS} days of {en}: valued at the last stored NAV, {val_d}")
    nd = UNIT_DECIMALS if round_on else 6
    schedule, skipped, units, flows, first = [], [], 0.0, [], None
    duty_paid = load_total = 0.0
    y, m = st.year, st.month
    while (y, m) <= (en.year, en.month):
        sched = date(y, m, min(dom, monthrange(y, m)[1]))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if sched < st or sched > en:
            continue
        k = s.next_on_or_after(sched)
        if k is None or s.dates[k] > val_d:
            skipped.append({"scheduled": sched, "reason": f"no stored NAV from {sched} to the valuation date {val_d}"})
            continue
        late = (s.dates[k] - sched).days
        if late > MAX_STALE_DAYS:
            warnings.append(f"the {sched} instalment was allotted {late} days later, on {s.dates[k]} "
                            f"(a gap in the stored NAVs)")
        first = first or sched
        amt, years_in = _step_up(a, step, first, sched)
        u, duty = purchase(amt, s.navs[k], duty_on, round_on)
        units += u
        duty_paid += duty
        flows.append((s.dates[k], -amt))
        held = (val_d - s.dates[k]).days
        row = {"scheduled": sched, "nav_date": s.dates[k], "nav": s.navs[k], "amount": amt,
               "stamp_duty": round(duty, 4), "units": round(u, nd), "cumulative_units": round(units, nd),
               "days_late": late, "step_ups": years_in, "days_held": held}
        if load_pct:
            ld = u * val_nav * load_pct / 100.0 if held < load_days else 0.0
            load_total += ld
            row["exit_load"] = round(ld, 4)
        schedule.append(row)
    if not schedule:
        return {**base, "status": "INSUFFICIENT", "skipped": skipped, "warnings": warnings,
                "reason": f"no instalment date from {st} to {en} has a stored NAV"}
    invested = sum(-x for _, x in flows)
    if round_on:
        units = round(units, nd)              # 3-decimal allotments add up to 3 decimals (no float dust)
    value = units * val_nav
    after = value - load_total
    span = (val_d - flows[0][0]).days
    x = xirr(flows + [(val_d, after)])
    if span < 365:
        warnings.append(f"the period is {span} days: XIRR is annualised from under a year, read it with care")
    sip_out = {"instalments": len(schedule), "invested": round(invested, 2), "stamp_duty": round(duty_paid, 2),
               "units": round(units, nd), "value": round(value, 2),
               "exit_load": round(load_total, 2) if load_pct else None,
               "value_after_exit_load": round(after, 2), "gain": round(after - invested, 2),
               "absolute_return_pct": _pct(after / invested - 1), "xirr_pct": _pct(x),
               "first_allotment": flows[0][0], "last_allotment": flows[-1][0],
               "first_instalment": schedule[0]["amount"], "last_instalment": schedule[-1]["amount"]}
    total = lump if lump is not None else invested
    k0 = s.next_on_or_after(st)
    if (s.dates[k0] - st).days > MAX_STALE_DAYS:
        warnings.append(f"the lump sum was allotted on {s.dates[k0]}, {(s.dates[k0] - st).days} days after the start "
                        f"(a gap in the stored NAVs)")
    l_units, l_duty = purchase(total, s.navs[k0], duty_on, round_on)
    l_value = l_units * val_nav
    l_days = (val_d - s.dates[k0]).days
    l_load = l_value * load_pct / 100.0 if load_pct and l_days < load_days else 0.0
    l_after = l_value - l_load
    lump_out = {"invested": round(total, 2), "date": s.dates[k0], "nav": s.navs[k0], "stamp_duty": round(l_duty, 2),
                "units": round(l_units, nd), "value": round(l_value, 2), "exit_load": round(l_load, 2) if load_pct else None,
                "value_after_exit_load": round(l_after, 2), "gain": round(l_after - total, 2),
                "absolute_return_pct": _pct(l_after / total - 1),
                "xirr_pct": _pct(xirr([(s.dates[k0], -total), (val_d, l_after)])) if l_days > 0 else None,
                "cagr_pct": _pct(_cagr(l_after / total, l_days)) if l_days >= 365 else None,
                "basis": "the same total as the SIP" if lump is None else "the lump_sum given"}
    return {**base, "status": "OK", "valuation": {"date": val_d, "nav": val_nav}, "sip": sip_out,
            "lump_sum": lump_out, "schedule": schedule, "skipped": skipped, "warnings": warnings}


# ── peers ─────────────────────────────────────────────────────────────────
def _quick(s: Navs, end_idx: int) -> dict:
    """1Y absolute, 3Y CAGR and the trailing-1Y annualised volatility as of s.dates[end_idx]."""
    from backtest import metrics as BM
    r1 = point_to_point(s, 12, end_idx)
    r3 = point_to_point(s, 36, end_idx)
    t = add_months(s.dates[end_idx], -12)
    vol, why = None, None
    if t < s.dates[0]:
        why = "under a year of NAVs"
    else:
        i0 = max(0, bisect_right(s.dates, t) - 1)
        used = [i for i in range(i0 + 1, end_idx + 1) if (s.dates[i] - s.dates[i - 1]).days <= MAX_STALE_DAYS]
        rs = [s.navs[i] / s.navs[i - 1] - 1 for i in used]
        if len(rs) < MIN_OBS:
            why = f"{len(rs)} daily returns in the last year (at least {MIN_OBS})"
        else:
            vol = BM.volatility(rs, periods_per_year([(s.dates[i] - s.dates[i - 1]).days for i in used]))
    return {"return_1y_pct": r1["absolute_pct"], "return_1y_reason": r1["reason"],
            "cagr_3y_pct": r3["cagr_pct"], "cagr_3y_reason": r3["reason"],
            "volatility_1y_pct": _pct(vol), "volatility_1y_reason": why}


def _rank(rows, key, target, ascending=False):
    have = [r for r in rows if r[key] is not None]
    t = next((r for r in have if r["scheme_code"] == target), None)
    vals = [r[key] for r in have]
    if t is None:
        tr = next((r for r in rows if r["scheme_code"] == target), {})
        return {"rank": None, "of": len(have), "value": None,
                "reason": tr.get(key.replace("_pct", "_reason")) or "not available for this scheme"}
    better = sum(1 for v in vals if (v < t[key] if ascending else v > t[key]))
    return {"rank": better + 1, "of": len(have), "value": t[key],
            "category_median": round(statistics.median(vals), 3), "best": (min if ascending else max)(vals),
            "worst": (max if ascending else min)(vals), "order": "lower is better" if ascending else "higher is better"}


def category_rank(conn, scheme_code, same_plan=True, as_of=None, _target: Navs | None = None) -> dict:
    code = _code(scheme_code)
    f = facts(conn, code)
    if not f["category"]:
        return {"status": "UNAVAILABLE", "scheme_code": code,
                "reason": "no AMFI category stored for this scheme (rows backfilled by history_mf carry none; "
                          "the daily NAVAll fetch sets it)"}
    s = _target or load(conn, code, as_of)
    asof = s.dates[-1]
    cfg = settings()
    # each candidate's LATEST row in the window decides (a recategorised scheme follows its new category)
    latest = {}
    for c, name, cat in conn.execute("SELECT scheme_code, scheme_name, category FROM mf_nav WHERE date>=? AND "
                                     "date<=? AND category IS NOT NULL ORDER BY scheme_code, date",
                                     (str(asof - timedelta(days=MAX_STALE_DAYS)), str(asof))):
        latest[str(c)] = (name, cat)
    in_cat = {c: n for c, (n, cat) in latest.items() if cat == f["category"]}
    in_cat.setdefault(code, f["scheme_name"])
    if same_plan:
        keep = {}
        for c, n in in_cat.items():
            p, o = plan_option(n)
            if (f["plan"] is None or p == f["plan"]) and (f["option"] is None or o == f["option"]):
                keep[c] = n
        keep[code] = in_cat[code]
    else:
        keep = dict(in_cat)
    codes = sorted(keep, key=lambda c: (c != code, str(keep[c] or "")))
    truncated = len(codes) > cfg["max_peers"]
    codes = codes[:cfg["max_peers"]]
    hist = {c: [] for c in codes}
    lo = str(add_months(asof, -36) - timedelta(days=MAX_STALE_DAYS + 1))
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = ",".join("?" * len(chunk))
        for c, d, v in conn.execute(f"SELECT scheme_code, date, nav FROM mf_nav WHERE scheme_code IN ({q}) AND date>=? "
                                    f"AND date<=? ORDER BY scheme_code, date", (*chunk, lo, str(asof))):
            hist[str(c)].append((d, v))
    rows = []
    for c in codes:
        ns = _rows_to_navs(c, hist[c])
        row = {"scheme_code": c, "scheme_name": keep[c], "is_target": c == code}
        if not len(ns) or (asof - ns.dates[-1]).days > MAX_STALE_DAYS:
            why = f"no NAV within {MAX_STALE_DAYS} days of {asof}"
            row.update({"return_1y_pct": None, "cagr_3y_pct": None, "volatility_1y_pct": None,
                        "return_1y_reason": why, "cagr_3y_reason": why, "volatility_1y_reason": why})
        else:
            row.update({"nav_date": ns.dates[-1], **_quick(ns, len(ns) - 1)})
        rows.append(row)
    rows.sort(key=lambda r: (r["return_1y_pct"] is None, -(r["return_1y_pct"] or 0)))
    cov = {"in_category_stored": len(in_cat), "compared": len(rows),
           "truncated_to": cfg["max_peers"] if truncated else None,
           "with_1y": sum(1 for r in rows if r["return_1y_pct"] is not None),
           "with_3y": sum(1 for r in rows if r["cagr_3y_pct"] is not None),
           "with_volatility": sum(1 for r in rows if r["volatility_1y_pct"] is not None),
           "note": ("Only schemes whose NAVs ATIP stores, with a NAV within "
                    f"{MAX_STALE_DAYS} days of {asof}; AMFI lists more. History is what the daily NAVAll fetch has "
                    "collected since ATIP started plus any history_mf backfill, so many peers lack 1Y / 3Y figures.")}
    return {"status": "OK", "scheme_code": code, "as_of": asof, "category": f["category"],
            "category_parsed": f["category_parsed"], "plan": f["plan"], "option": f["option"],
            "peer_filter": ("same category, plan and option (parsed from the name)" if same_plan else "same category"),
            "coverage": cov,
            "rank": {"return_1y": _rank(rows, "return_1y_pct", code),
                     "cagr_3y": _rank(rows, "cagr_3y_pct", code),
                     "volatility_1y": _rank(rows, "volatility_1y_pct", code, ascending=True)},
            "peers": rows, "note": NOTE}


def compare(conn, codes, as_of=None) -> dict:
    cs = []
    for c in codes:
        c = _code(c)
        if c not in cs:
            cs.append(c)
    if not 2 <= len(cs) <= 10:
        raise ValueError("compare needs 2 to 10 distinct scheme codes")
    series = {c: load(conn, c, as_of) for c in cs}
    common = min(s.dates[-1] for s in series.values())
    rf = _rf_pct(None)
    rows = []
    for c in cs:
        full = series[c]
        n = bisect_right(full.dates, common)
        s = Navs(c, full.dates[:n], full.navs[:n], full.bad_rows)
        f = facts(conn, c)
        rk = risk(conn, s, rf, settings()["risk_years"], None)
        rows.append({"scheme_code": c, "scheme_name": f["scheme_name"],
                     "category": (f["category_parsed"] or {}).get("label"), "plan": f["plan"], "option": f["option"], "nav_date": s.dates[-1], "nav": s.navs[-1],
                     "returns": {k: point_to_point(s, m) for k, m in PERIODS}, "since_first_nav": since_first(s),
                     "volatility_pct": rk["volatility_pct"], "sharpe": rk["sharpe"], "sortino": rk["sortino"],
                     "max_drawdown_pct": rk["max_drawdown"].get("max_drawdown_pct"), "risk_window": rk["window"],
                     "risk_reason": rk["reason"],
                     **_quick(s, len(s) - 1)})
    stale = [r["scheme_code"] for r in rows if (common - r["nav_date"]).days > MAX_STALE_DAYS]

    def ranks(key, ascending=False):
        return {r["scheme_code"]: _rank(rows, key, r["scheme_code"], ascending)["rank"] for r in rows}
    return {"as_of": common, "risk_free_pct": rf,
            "as_of_note": "the earliest of the schemes' latest NAV dates, so every figure covers the same days",
            "schemes": rows, "stale": stale,
            "rank": {"return_1y": ranks("return_1y_pct"), "cagr_3y": ranks("cagr_3y_pct"),
                     "volatility_1y": ranks("volatility_1y_pct", ascending=True)},
            "note": NOTE}
