"""
W39b (gap analysis §4 item 7) — earnings surprise, an EPS-trend revisions proxy and post-earnings drift
(PEAD), all from ATIP's own quarterly results (fundamental_data, derived from NSE XBRL filings by
data/nse_filings.py). ATIP has NO consensus-estimates feed: the "expected" figure is a time-series one,
the seasonal random walk (the same quarter a year earlier). Nothing here is an analyst estimate.

    SUE            standardised unexpected earnings of quarter q:
                       SUE_q = (EPS_q - EPS_{q-4}) / sd(EPS_p - EPS_{p-4} for the 8 quarters p before q)
                   sd is the sample standard deviation of those seasonal changes; at least 4 of the 8 must
                   be known (the quarter a year earlier stored, and of the same nature: consolidated vs
                   standalone), else SUE is None and sue_reason says why. sue_revenue is the same on
                   quarterly revenue. Foster, Olsen & Shevlin (1984); Bernard & Thomas (1989).
    EPS trend      a REVISIONS PROXY, not analyst revisions: trailing-4-quarter EPS growth
                       g_q = (TTM_q - TTM_{q-4}) / |TTM_{q-4}| x 100,   TTM_q = EPS_{q-3} + ... + EPS_q
                   and its change over the last two filings, eps_trend_pts = g_q - g_{q-1}:
                   ACCELERATING when above 0, DECELERATING below (STEADY at exactly 0). Needs 9 quarters.
    point in time  a quarter counts from its knowledge date: the filing's broadcast time
                   (fundamental_data.available_from) when stored, else period end + 45 days, else when the row
                   was stored (quant.factors.fundamentals_as_of's rule) -- never from its period end. Each
                   quarter's figures use only the quarters known by its own knowledge date, so a quarter
                   filed later is invisible to everything computed before it.
    earnings_surprise   one row per (symbol, period end) with the values above, recomputed nightly from the
                   stored quarters (an idempotent upsert); latest() reads it as of any day.
    PEAD scan      on the first session after the filing's broadcast date (the first full session with the
                   result public) a quarter with SUE >= +2 is a BULL signal (scan pead_bull), SUE <= -2 a
                   BEAR one (pead_bear). The scans are registered with the technical signal engine
                   (research/tech_signals.py EVENT_SCANS) and stored in technical_signal exactly as a scan hit
                   is: entry the close, stop 2 x ATR, target 4 x ATR, the confluence count of 6, the market
                   gate and alignment, weekly agreement; the engine then evaluates them (TARGET / STOPPED /
                   EXPIRED, the forward record 5 / 20 / 60 sessions vs the Nifty, scan stats). Horizon 60
                   sessions, the drift window, instead of the technical scans' 20. Like the chart patterns,
                   they alert only once their own record has 30 closed signals with a positive average R.
                   Only an exact broadcast time counts (an estimated date is no event date); only the newest
                   quarter of a filing day fires. Filings reach ATIP with the weekly fundamentals job, so the
                   nightly scan looks back CATCHUP_SESSIONS sessions: a filing found late still gets its
                   signal on its own first session (it builds the record) but is never alerted; older
                   filings are stale and skipped (`backfill` records the whole history once).
    screener       sue, sue_revenue, eps_trend, days_since_result (research/screener.py) and the preset
                   "Positive earnings surprise"

Scheduled 20:35 on market days (after the 20:30 technical signals, before the 20:40 research reports and
the 20:50 saved screens). Nothing here orders.
CLI: python -m research.earnings_surprise run [--date D] [--symbols ...] | backfill | symbol SYM [--date D]
"""

from __future__ import annotations

import html
import json
import logging
import math
import statistics
import uuid
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

SUE_TRIGGER = 2.0               # |SUE| at or above this on a fresh filing is a PEAD signal
PRIOR_QUARTERS, MIN_PRIOR = 8, 4
YEAR_DAYS, QUARTER_DAYS, TOL = 365, 91, 20     # matching quarters by period end (data/nse_filings.py _find)
CATCHUP_SESSIONS = 10           # the nightly scan's reach: fundamentals arrive weekly (Saturday)
PEAD_SCANS = ("pead_bull", "pead_bear")
FIELDS = ("sue", "sue_revenue", "eps_trend", "days_since_result")
METHOD = ("SUE = (EPS - EPS of the same quarter a year earlier) / the standard deviation of that change over the "
          "8 quarters before (4 needed): a surprise against the seasonal random walk on ATIP's own filings, "
          "counted from each filing's broadcast. ATIP holds no consensus estimates. The EPS trend compares "
          "trailing-4-quarter EPS growth with the filing before: a proxy for estimate revisions, not analyst "
          "revisions.")

COLS = ["quarter", "known_on", "available_from", "exact_date", "nature", "eps_q", "eps_year_ago", "eps_sd", "sue",
        "sue_n", "sue_reason", "revenue_cr", "revenue_year_ago", "revenue_sd", "sue_revenue", "sue_revenue_n",
        "sue_revenue_reason", "ttm_eps", "ttm_growth_pct", "ttm_growth_prev_pct", "eps_trend", "eps_trend_pts",
        "eps_trend_reason"]

DDL = (
    """CREATE TABLE IF NOT EXISTS earnings_surprise (
        symbol TEXT NOT NULL, period_end DATE NOT NULL, quarter TEXT, known_on DATE, available_from TEXT,
        exact_date INTEGER, nature TEXT, eps_q REAL, eps_year_ago REAL, eps_sd REAL, sue REAL, sue_n INTEGER,
        sue_reason TEXT, revenue_cr REAL, revenue_year_ago REAL, revenue_sd REAL, sue_revenue REAL,
        sue_revenue_n INTEGER, sue_revenue_reason TEXT, ttm_eps REAL, ttm_growth_pct REAL, ttm_growth_prev_pct REAL,
        eps_trend TEXT, eps_trend_pts REAL, eps_trend_reason TEXT, computed_at TIMESTAMP,
        PRIMARY KEY (symbol, period_end))""",
    "CREATE INDEX IF NOT EXISTS idx_earnings_surprise_known ON earnings_surprise(known_on)",
)


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _chunks(xs, n=500):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ── the stored quarters, point in time ───────────────────────────────────────

def knowledge_date(row: dict, period_end) -> tuple:
    """(the day the quarter became public, exact?) -- quant.factors.fundamentals_as_of's rule: the filing's
    broadcast time when stored (exact), else report date + FUNDAMENTAL_LAG_DAYS, else the day it was stored.
    A broadcast before the period even ended is not believed."""
    from quant.factors import FUNDAMENTAL_LAG_DAYS
    d = _date(row.get("available_from"))
    if d is not None and (period_end is None or d >= period_end):
        return d, True
    d = _date(row.get("report_date"))
    if d is not None:
        return d + timedelta(days=FUNDAMENTAL_LAG_DAYS), False
    return _date(row.get("created_at")), False


def load_quarters(conn, symbols=None) -> dict:
    """{symbol: [quarter, oldest first]} from fundamental_data rows carrying EPS or revenue. One quarter per
    period end (a row with a broadcast time is preferred over an estimated one)."""
    base = ("SELECT symbol, quarter, period_end, report_date, available_from, created_at, nature, eps_q, revenue_cr "
            "FROM fundamental_data WHERE (eps_q IS NOT NULL OR revenue_cr IS NOT NULL)")
    try:
        if symbols is None:
            cur = conn.execute(base)
            batches = [(cur.fetchall(), [c[0] for c in cur.description])]
        else:
            batches = []
            for part in _chunks(sorted(set(symbols))):
                cur = conn.execute(base + f" AND symbol IN ({','.join('?' * len(part))})", part)
                batches.append((cur.fetchall(), [c[0] for c in cur.description]))
    except Exception as e:
        log.debug(f"fundamental_data: {e}")
        return {}
    by = {}
    for rows, cols in batches:
        for r in rows:
            x = dict(zip(cols, r))
            pe = _date(x["period_end"]) or _date(x["report_date"])
            if pe is None:
                continue
            known, exact = knowledge_date(x, pe)
            if known is None:
                continue
            av = x["available_from"]
            q = {"quarter": x["quarter"], "period_end": pe, "known_on": known, "exact": exact,
                 "available_from": str(av)[:19] if exact and av is not None else None, "nature": x["nature"],
                 "eps": _num(x["eps_q"]), "revenue": _num(x["revenue_cr"])}
            cur_q = by.setdefault(x["symbol"], {}).get(pe)
            if cur_q is None or (exact and not cur_q["exact"]) or \
                    (exact == cur_q["exact"] and cur_q["eps"] is None and q["eps"] is not None):
                by[x["symbol"]][pe] = q
    return {s: [qs[pe] for pe in sorted(qs)] for s, qs in by.items()}


# ── the arithmetic (pure) ────────────────────────────────────────────────────

def _match(qs, pe, days, tol=TOL):
    """The quarter whose period ended closest to `days` before pe (within tol), or None."""
    if pe is None:
        return None
    target = pe - timedelta(days=days)
    best = None
    for q in qs:
        gap = abs((q["period_end"] - target).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, q)
    return best[1] if best else None


def _comparable(a, b) -> bool:
    """Consolidated is not compared with standalone (a company that started consolidating would show a
    surprise that is only a change of scope)."""
    na, nb = (a.get("nature") or "").upper(), (b.get("nature") or "").upper()
    return not na or not nb or na == nb


def _seasonal(known, p, key):
    ya = _match(known, p["period_end"], YEAR_DAYS)
    if ya is None or p[key] is None or ya[key] is None or not _comparable(p, ya):
        return None
    return p[key] - ya[key]


def surprise(known: list, q: dict, key: str = "eps") -> dict:
    """q[key] against the seasonal random walk: {value, year_ago, sd, n, sue, reason}. `known` is every
    quarter public by q's knowledge date (q included)."""
    what = "EPS" if key == "eps" else "revenue"
    out = {"value": q[key], "year_ago": None, "sd": None, "n": 0, "sue": None, "reason": None}
    if q[key] is None:
        out["reason"] = f"no {what} reported for {q['quarter']}"
        return out
    ya = _match(known, q["period_end"], YEAR_DAYS)
    if ya is None or ya[key] is None:
        out["reason"] = f"{what} of the quarter a year before {q['quarter']} is not known"
        return out
    if not _comparable(q, ya):
        out["reason"] = (f"{q['quarter']} is {str(q['nature']).lower()} and the quarter a year before "
                         f"{str(ya['nature']).lower()}: not comparable")
        return out
    out["year_ago"] = ya[key]
    prior = [p for p in known if QUARTER_DAYS - TOL <= (q["period_end"] - p["period_end"]).days
             <= PRIOR_QUARTERS * QUARTER_DAYS + TOL][-PRIOR_QUARTERS:]
    diffs = [d for d in (_seasonal(known, p, key) for p in prior) if d is not None]
    out["n"] = len(diffs)
    if len(diffs) < MIN_PRIOR:
        out["reason"] = (f"only {len(diffs)} of the {PRIOR_QUARTERS} earlier year-on-year {what} changes are known "
                         f"(needs {MIN_PRIOR})")
        return out
    sd = statistics.stdev(diffs)
    out["sd"] = round(sd, 4)
    if sd <= 0:
        out["reason"] = f"the {len(diffs)} earlier year-on-year {what} changes are all equal (standard deviation 0)"
        return out
    out["sue"] = round((q[key] - ya[key]) / sd, 3)
    return out


def _ttm(known, p):
    if p is None:
        return None
    four = [r for r in known if 0 <= (p["period_end"] - r["period_end"]).days < 360][-4:]
    if len(four) < 4 or any(r["eps"] is None or not _comparable(p, r) for r in four):
        return None
    return sum(r["eps"] for r in four)


def _growth(known, p):
    """Trailing-4-quarter EPS growth at p, %; None without 8 quarters or on a zero base."""
    if p is None:
        return None
    t, t0 = _ttm(known, p), _ttm(known, _match(known, p["period_end"], YEAR_DAYS))
    if t is None or t0 is None or t0 == 0:
        return None
    return (t - t0) / abs(t0) * 100


def eps_trend(known: list, q: dict) -> dict:
    """The revisions proxy: the change in trailing-4-quarter EPS growth between q and the filing before."""
    ttm = _ttm(known, q)
    g = _growth(known, q)
    prev = _match(known, q["period_end"], QUARTER_DAYS)
    gp = _growth(known, prev)
    out = {"ttm_eps": round(ttm, 4) if ttm is not None else None,
           "ttm_growth_pct": round(g, 2) if g is not None else None,
           "ttm_growth_prev_pct": round(gp, 2) if gp is not None else None,
           "eps_trend": None, "eps_trend_pts": None, "eps_trend_reason": None}
    if g is None:
        out["eps_trend_reason"] = (f"trailing-4-quarter EPS growth for {q['quarter']} needs EPS for 8 quarters in a "
                                   "row (and a non-zero base)")
    elif gp is None:
        out["eps_trend_reason"] = (f"no trailing-4-quarter EPS growth for the filing before {q['quarter']} "
                                   "(needs 9 quarters in a row)")
    else:
        pts = round(g - gp, 2)
        out["eps_trend_pts"] = pts
        out["eps_trend"] = "ACCELERATING" if pts > 0 else "DECELERATING" if pts < 0 else "STEADY"
    return out


def compute(quarters: list) -> list:
    """One row per quarter (oldest first), each from the quarters public by that quarter's own knowledge date."""
    out = []
    for q in quarters:
        known = [p for p in quarters if p["known_on"] <= q["known_on"] and p["period_end"] <= q["period_end"]]
        e, r = surprise(known, q, "eps"), surprise(known, q, "revenue")
        out.append({"quarter": q["quarter"], "period_end": q["period_end"], "known_on": q["known_on"],
                    "available_from": q["available_from"], "exact_date": int(q["exact"]), "nature": q["nature"],
                    "eps_q": q["eps"], "eps_year_ago": e["year_ago"], "eps_sd": e["sd"], "sue": e["sue"],
                    "sue_n": e["n"], "sue_reason": e["reason"], "revenue_cr": q["revenue"],
                    "revenue_year_ago": r["year_ago"], "revenue_sd": r["sd"], "sue_revenue": r["sue"],
                    "sue_revenue_n": r["n"], "sue_revenue_reason": r["reason"], **eps_trend(known, q)})
    return out


# ── the stored table ─────────────────────────────────────────────────────────

def _db(v):
    return str(v) if isinstance(v, date) else v


def refresh(conn, symbols=None) -> dict:
    """Recompute every stored quarter's surprise and trend into earnings_surprise (idempotent: the same quarters
    give the same rows; quarters no longer stored are dropped)."""
    ensure_tables(conn)
    qs = load_quarters(conn, symbols)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    sql = (f"INSERT INTO earnings_surprise (symbol, period_end, {', '.join(COLS)}, computed_at) VALUES "
           f"({','.join('?' * (len(COLS) + 3))}) ON CONFLICT(symbol, period_end) DO UPDATE SET " +
           ", ".join(f"{c}=excluded.{c}" for c in COLS) + ", computed_at=excluded.computed_at")
    for sym, quarters in qs.items():
        rows = compute(quarters)
        keep = [str(r["period_end"]) for r in rows]
        conn.execute(f"DELETE FROM earnings_surprise WHERE symbol=? AND period_end NOT IN ({','.join('?' * len(keep))})",
                     [sym] + keep)
        for r in rows:
            conn.execute(sql, [sym, str(r["period_end"])] + [_db(r[c]) for c in COLS] + [now])
            n += 1
    gone = []
    if symbols is None:
        gone = sorted({r[0] for r in conn.execute("SELECT DISTINCT symbol FROM earnings_surprise")} - set(qs))
    elif symbols:
        gone = sorted(set(symbols) - set(qs))
    for s in gone:
        conn.execute("DELETE FROM earnings_surprise WHERE symbol=?", (s,))
    conn.commit()
    return {"rows": n, "symbols": len(qs), "dropped_symbols": len(gone)}


def latest(conn, symbols=None, as_of=None) -> dict:
    """{symbol: the newest quarter public on as_of, with days_since_result} from earnings_surprise."""
    as_of = _date(as_of) or date.today()
    try:
        ensure_tables(conn)
        cur = conn.execute("SELECT * FROM earnings_surprise WHERE known_on<=? ORDER BY symbol, period_end",
                           (str(as_of),))
    except Exception as e:
        log.debug(f"earnings_surprise: {e}")
        return {}
    cols = [c[0] for c in cur.description]
    keep = set(symbols) if symbols is not None else None
    out = {}
    for r in cur.fetchall():
        x = dict(zip(cols, r))
        if keep is None or x["symbol"] in keep:
            out[x["symbol"]] = x                     # ordered by period end: the newest wins
    for x in out.values():
        k = _date(x["known_on"])
        x["days_since_result"] = (as_of - k).days if k else None
    return out


def apply(conn, rows: list, as_of) -> None:
    """The screener's fields (research/screener.py build_snapshot): sue, sue_revenue, eps_trend, days_since_result."""
    got = latest(conn, [r["symbol"] for r in rows], as_of)
    for r in rows:
        x = got.get(r["symbol"]) or {}
        for k in FIELDS:
            r[k] = x.get(k)


# ── PEAD: the scan, stored in the technical signal engine ────────────────────

def _reason(c, late) -> str:
    parts = [f"{c['quarter']} EPS {c['eps_q']:.2f} vs {c['eps_year_ago']:.2f} a year earlier: SUE {c['sue']:+.2f} "
             f"(sd {c['eps_sd']:.2f} of {c['sue_n']} earlier year-on-year changes)"]
    if c.get("sue_revenue") is not None:
        parts.append(f"revenue SUE {c['sue_revenue']:+.2f}")
    if c.get("eps_trend") and c.get("eps_trend_pts") is not None:
        parts.append(f"trailing EPS growth {c['eps_trend'].lower()} ({c['eps_trend_pts']:+.1f} pts)")
    parts.append(f"filed {str(c['available_from'] or c['known_on'])[:16]}")
    if late:
        parts.append(f"found {late} session{'s' if late > 1 else ''} after its first session: recorded, not alerted")
    return "; ".join(parts)


def scan(conn, as_of=None, symbols=None, catchup_sessions=CATCHUP_SESSIONS) -> dict:
    """PEAD signals for filings whose first session after the broadcast is on or before as_of and within the
    last `catchup_sessions` sessions (None: any), dated on that first session. Point in time: only quarters
    public before that session; a signal already stored is left alone."""
    from research import regime_gate as RG
    from research import tech_signals as TS
    from research import technicals as T
    ensure_tables(conn)
    TS.ensure_tables(conn)
    as_of = _date(as_of)
    if as_of is None:
        r = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()
        as_of = _date(r[0]) if r and r[0] else None
    out = {"as_of": str(as_of) if as_of else None, "candidates": 0, "signals": 0, "late": 0, "stale": 0,
           "existing": 0, "not_yet": 0, "no_prices": 0, "short_history": 0, "superseded": 0}
    if as_of is None:
        out["reason"] = "no prices"
        return out
    cur = conn.execute("SELECT * FROM earnings_surprise WHERE known_on<? ORDER BY symbol, period_end", (str(as_of),))
    cols = [c[0] for c in cur.description]
    allq = [dict(zip(cols, r)) for r in cur.fetchall()]
    keep = set(symbols) if symbols else None
    by = {}
    for x in allq:
        if keep is None or x["symbol"] in keep:
            x["known_on"], x["period_end"] = _date(x["known_on"]), _date(x["period_end"])
            by.setdefault(x["symbol"], []).append(x)
    bench, ctx = {}, {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for sym, qs in by.items():
        cands = [x for x in qs if x["exact_date"] and x["sue"] is not None and abs(x["sue"]) >= SUE_TRIGGER]
        if not cands:
            continue
        out["candidates"] += len(cands)
        start = min(c["known_on"] for c in cands)
        days = [_date(r[0]) for r in conn.execute("SELECT date FROM prices_daily WHERE symbol=? AND date>? AND date<=? "
                                                  "AND close>0 ORDER BY date", (sym, str(start), str(as_of)))]
        for c in cands:
            if any(x["period_end"] > c["period_end"] and x["known_on"] <= c["known_on"] for x in qs):
                out["superseded"] += 1              # a newer quarter was already public: not the news of the day
                continue
            first = next((d for d in days if d > c["known_on"]), None)
            if first is None:
                out["not_yet"] += 1
                continue
            if not conn.execute("SELECT 1 FROM prices_daily WHERE symbol=? AND date<=? AND close>0 LIMIT 1",
                                (sym, str(c["known_on"]))).fetchone():
                out["no_prices"] += 1               # the stored history starts after the filing: no true first session
                continue
            late = sum(1 for d in days if d > first)
            if catchup_sessions is not None and late >= int(catchup_sessions):
                out["stale"] += 1
                continue
            key = "pead_bull" if c["sue"] > 0 else "pead_bear"
            name, direction, horizon = TS.EVENT_SCANS[key]
            if conn.execute("SELECT 1 FROM technical_signal WHERE symbol=? AND date=? AND scan=?",
                            (sym, str(first), key)).fetchone():
                out["existing"] += 1
                continue
            bars = TS.load_bars(conn, [sym], first).get(sym)
            if first not in bench:
                bench[first] = TS.benchmark(conn, first)
            snap = T.snapshot(bars, bench[first]) if bars is not None and bars.index[-1].date() == first else None
            if not snap or not snap.get("_atr"):
                out["short_history"] += 1           # under 60 bars: no ATR, no levels
                continue
            if first not in ctx:
                ctx[first] = TS._context(conn, first)
            stop, target = TS.levels(direction, snap["_close"], snap["_atr"])
            cnt, ev = TS.confluence(direction, snap, ctx[first]["regime"], ctx[first]["research"].get(sym))
            g = RG.gate_on(conn, first)
            g = g if g and g.get("date") == str(first) else {}
            conn.execute("""INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, reason, entry,
                                stop, target, atr, horizon, confluence, evidence_json, status, created_at, market_gate,
                                alignment, market_status, weekly_agrees)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?)""",
                         (uuid.uuid4().hex[:16], sym, str(first), key, name, direction, _reason(c, late),
                          snap["_close"], stop, target, snap["_atr"], horizon, cnt, json.dumps(ev), now, g.get("gate"),
                          RG.alignment(direction, g.get("gate")), g.get("status"),
                          T.weekly_agrees(direction, snap.get("tech_rating_w_label"))))
            out["signals"] += 1
            out["late"] += bool(late)
    conn.commit()
    return out


def alert(conn, as_of=None, min_confluence: int = 0, limit: int = 10) -> dict:
    """Alert as_of's PEAD signals: only a scan whose own record has earned it (30 closed signals, average R
    above 0: tech_signals.proven_scans), never one against the market gate, never one found late."""
    from research import tech_signals as TS
    as_of = _date(as_of)
    if as_of is None:
        return {"alerted": 0}
    proven = TS.proven_scans(conn)
    sig = [s for s in TS.todays_signals(conn, str(as_of), min_confluence=min_confluence, alignment="not_against",
                                         record_horizon=60)
           if s["scan"] in PEAD_SCANS and s["scan"] in proven]
    if not sig:
        return {"alerted": 0, "held": sorted(set(PEAD_SCANS) - proven)}
    from alerts.telegram import notify

    def rec(s):
        r = s.get("record")
        if not r or not r.get("enough"):
            return ""
        return (f"; record: beat the Nifty {r['beat_nifty_pct']:.0f}% of {r['n']} times over 60 sessions, median "
                f"{r['median_excess_pct']:+.1f}%")
    lines = [f"{'▲' if s['direction'] == 'BULL' else '▼'} {s['symbol']}: {html.escape(s['reason'].split(';')[0])} "
             f"(entry {s['entry']:.2f}, stop {s['stop']:.2f}, target {s['target']:.2f}, confluence "
             f"{s['confluence']}/6{rec(s)})" for s in sig[:limit]]
    head = ("<b>Post-earnings drift</b> (EOD, for the next session; surprise vs the same quarter a year earlier, "
            "not vs consensus; not advice)")
    notify(head + "\n" + "\n".join(lines), category="signals", severity="info", key=f"pead:{as_of}")
    return {"alerted": len(lines)}


# ── the nightly job ──────────────────────────────────────────────────────────

def run(conn=None, as_of=None, symbols=None, catchup_sessions=CATCHUP_SESSIONS) -> dict:
    """Refresh earnings_surprise, scan for PEAD signals, evaluate the new ones with the signal engine."""
    from db.schema import get_connection
    from research import tech_signals as TS
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        ref = refresh(conn, symbols)
        if not ref["symbols"]:
            return {"status": "SKIPPED", "rows": 0, "reason": "no quarterly results stored (fundamental_data)"}
        sc = scan(conn, as_of, symbols, catchup_sessions)
        out = {"status": "SUCCESS", "rows": ref["rows"], "symbols": ref["symbols"], "as_of": sc["as_of"], "scan": sc}
        if sc["signals"]:
            out["evaluated"] = TS.evaluate_signals(conn)
            out["forward"] = TS.evaluate_forward(conn)
        return out
    finally:
        if own:
            conn.close()


def run_job() -> dict:
    out = run()
    if out.get("as_of"):
        try:
            from db.schema import get_connection
            conn = get_connection()
            try:
                out["alert"] = alert(conn, out["as_of"])
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"  earnings-drift alert: {e}")
    return out


# ── one stock (the research page) ────────────────────────────────────────────

def for_symbol(conn, symbol: str, as_of=None) -> dict:
    """The stock's surprise as of a day, computed now from the stored quarters, its quarters (newest first), its
    PEAD signals and the PEAD scans' record."""
    from research import tech_signals as TS
    if as_of not in (None, "") and _date(as_of) is None:
        raise ValueError("as_of must be YYYY-MM-DD")
    day = _date(as_of) or date.today()
    qs = load_quarters(conn, [symbol]).get(symbol)
    if not qs:
        raise LookupError(f"no quarterly results stored for {symbol}")
    rows = [r for r in compute(qs) if r["known_on"] <= day]
    if not rows:
        raise LookupError(f"no result of {symbol} was public on {day}")
    last = dict(rows[-1])
    last["days_since_result"] = (day - last["known_on"]).days
    TS.ensure_tables(conn)
    cur = conn.execute("SELECT date, scan, name, direction, reason, entry, stop, target, confluence, status, "
                       "r_multiple, market_gate, alignment, ret_20d, excess_20d, ret_60d, excess_60d FROM technical_signal "
                       f"WHERE symbol=? AND scan IN ({','.join('?' * len(PEAD_SCANS))}) AND date<=? "
                       "ORDER BY date DESC LIMIT 20", [symbol, *PEAD_SCANS, str(day)])
    names = [c[0] for c in cur.description]
    signals = [dict(zip(names, r)) for r in cur.fetchall()]
    record = [o for o in TS.scan_stats(conn) if o["scan"] in PEAD_SCANS]
    return {"symbol": symbol, "as_of": str(day), "latest": last, "quarters": rows[::-1][:12], "signals": signals,
            "record": record, "trigger": SUE_TRIGGER, "method": METHOD}


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research.earnings_surprise")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--date")
    r.add_argument("--symbols", nargs="+")
    b = sub.add_parser("backfill", help="record PEAD signals for every stored filing, however old")
    b.add_argument("--symbols", nargs="+")
    s = sub.add_parser("symbol")
    s.add_argument("symbol")
    s.add_argument("--date")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        out = run(as_of=a.date, symbols=a.symbols)
    elif a.cmd == "backfill":
        out = run(symbols=a.symbols, catchup_sessions=None)
    else:
        from db.schema import get_connection
        conn = get_connection()
        try:
            out = for_symbol(conn, a.symbol.upper(), a.date)
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
