"""
W39 Phase 4 (item 2, MC-01..03) — a READ-ONLY MCP server for ATIP, so Claude (Desktop, Code) can use the
screener, signals, market pulse, research reports and scorecards next to the Dhan MCP.

    transport  MCP over stdio: one JSON-RPC 2.0 message per line (initialize, ping, tools/list, tools/call).
               Written to the specification directly, so it needs no new package. Stdout carries only
               protocol messages; anything an imported module prints goes to stderr.
    read-only  every tool reads; there is no tool that writes, orders, cancels or changes a setting. The
               SQLite connection is opened with PRAGMA query_only, so even a bug in a reader cannot write
               (a PostgreSQL deployment: read-only by the tool set alone). `open_orders` reads your
               Dhan order book through the broker API (read-only call). No tool keeps a cache table: what
               is computed on a call (the DVM's screener snapshot, a SIP, an earnings surprise) lives in
               memory only.
    tools      screener_fields, screener_run, signals_today, signal_track_record, intraday_signals,
               stock_technicals, market_pulse, market_gate, research_report, scorecard, event_calendar,
               open_orders, and (W39-MCP, the W39b features):
                 dvm                  durability / valuation / momentum 0-100, the zone and every input
                                      (research/dvm.py, from the screener snapshot)
                 earnings_surprise    SUE, revenue SUE and the EPS-trend proxy as of a day, the quarters,
                                      PEAD signals and their record (research/earnings_surprise.py)
                 mf_search            find an AMFI scheme code by name in the stored NAVs
                 mf_analytics         returns, rolling returns, risk, benchmark and category rank of a
                                      scheme (data/mf_analytics.py; rolling series and the peer table trimmed)
                 mf_sip               a SIP (step-up, stamp duty, unit rounding, exit load) and a lump sum
                                      with XIRR (the instalment schedule only on request)
                 mf_compare           2-10 schemes side by side, or a scheme's category peers
                 backtest_validation  stored CPCV / PBO results (backtest/cpcv.py, kinds cpcv and pbo in
                                      backtest_run): the latest runs, or one run's distribution, PBO and
                                      notes (per-split / per-path / per-trial detail on request)
                 chart_marks          what the stock chart draws: stored technical signals, candle
                                      patterns, the 20:30 run's chart patterns and the patterns in place
                                      with their lines (dashboard/stock_view.py chart_marks)
               Outputs are compact JSON (a tool trims long series and says how much it left out);
               errors are tool results with isError, never a dead server.

Claude Desktop (claude_desktop_config.json) or Claude Code (.mcp.json):
    {"mcpServers": {"atip": {"command": "/path/to/ATIP-DEV/.venv/bin/python",
                             "args": ["/path/to/ATIP-DEV/tools/atip_mcp.py"],
                             "env": {"ATIP_DB_PATH": "/path/to/ATIP-DEV/atip_data/atip.db"}}}}
Run by hand: python tools/atip_mcp.py   (then type JSON-RPC lines).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta

if __package__ in (None, ""):                       # run as a script: make the repository importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("atip_mcp")

SERVER = {"name": "atip", "title": "ATIP (read-only)", "version": "w39"}
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 200_000
INSTRUCTIONS = (
    "ATIP is the user's own analytics platform for NSE stocks. These tools only read ATIP's stored data: "
    "screens, end-of-day and intraday technical signals with their track records, the market pulse and gate, "
    "equity research reports, the fundamental scorecard and the DVM view, earnings surprises, the stock chart's "
    "marks, mutual fund analytics on stored AMFI NAVs, stored CPCV / PBO backtest validations, the macro event "
    "calendar, and open orders. Nothing here can place, change or cancel an order. Report the dates the data is "
    "from; outputs are model results from ATIP's own data, not investment advice. Call screener_fields before "
    "writing a screener query, and mf_search to find a scheme's AMFI code."
)
MF_PEERS = 15                     # category peers returned by mf_analytics / mf_compare (the scheme itself kept)
VALIDATION_LIST = 10              # backtest_validation: runs listed by default
TRIALS_SHOWN = 20                 # backtest_validation: PBO trials shown without detail


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_SYM = {"type": "string", "description": "NSE symbol, e.g. RELIANCE"}
_DATE = {"type": "string", "description": "YYYY-MM-DD; default today"}
_AS_OF = {"type": "string", "description": "YYYY-MM-DD; default the latest"}
_SCHEME = {"type": "string", "description": "AMFI scheme code, e.g. 122639 (mf_search finds it)"}

TOOLS = {
    "screener_fields": ("Screener fields (key, label, unit, group) and ready-made preset queries. Call this first.",
                        _obj({"group": {"type": "string", "description": "only this group, e.g. Valuation, "
                                        "Technical, Scorecard"}})),
    "screener_run": ("Run a stock screen in ATIP's query language, e.g. `roce_pct > 20 AND debt_equity < 0.5 AND "
                     "(pe < 25 OR peg < 1)`; IN, CONTAINS, AND / OR / NOT. Returns the matching stocks.",
                     _obj({"query": {"type": "string"}, "sort": {"type": "string", "description": "a field key"},
                           "desc": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                           "columns": {"type": "array", "items": {"type": "string"}}}, ["query"])),
    "signals_today": ("End-of-day technical signals for a day: scan, direction, entry / stop / target, confluence, the "
                      "market gate at birth, the daily / weekly / 75-minute ratings, and each scan's record in "
                      "today's market.",
                      _obj({"date": _DATE, "min_confluence": {"type": "integer", "minimum": 0, "maximum": 6},
                            "alignment": {"type": "string", "enum": ["WITH", "MIXED", "AGAINST", "not_against"]}})),
    "signal_track_record": ("How each technical scan has done: win rate and average R, and the return against the "
                            "Nifty after 5 / 20 / 60 sessions by market gate and confluence.",
                            _obj({"horizon": {"type": "integer", "enum": [5, 20, 60]},
                                  "min_confluence": {"type": "integer", "minimum": 0, "maximum": 6}})),
    "intraday_signals": ("Intraday scans on 15-minute bars for a day (opening-range breakout, open = low / high, "
                         "squeeze) and each scan's record to the close.", _obj({"date": _DATE})),
    "stock_technicals": ("One stock's latest technical snapshot (ratings, RSI, MACD, patterns, RS) and its recent "
                         "signals.", _obj({"symbol": _SYM}, ["symbol"])),
    "market_pulse": ("Market context now: global cues and the GIFT gap with their records, FII flows and positioning, "
                     "the event calendar, order-book pressure, the market gate, and an overall RISK_ON / NEUTRAL / "
                     "RISK_OFF with reasons.", _obj({})),
    "market_gate": ("The market gate (IBD-style: distribution days, follow-through, 200-DMA): OPEN / CAUTION / "
                    "CLOSED and why.", _obj({})),
    "research_report": ("ATIP's equity research report for a stock: fair value (DCF, multiples), 12-month target, "
                        "rating, thesis, risks, peers, ownership (today's stored report, else built now, not saved).",
                        _obj({"symbol": _SYM}, ["symbol"])),
    "scorecard": ("The fundamental scorecard for a stock: value, growth, past performance, health and dividend, six "
                  "pass / fail checks each, with the numbers behind each check.", _obj({"symbol": _SYM}, ["symbol"])),
    "event_calendar": ("Upcoming Fed, US CPI, US jobs and RBI events, the Indian session each one hits, and today's "
                       "gap-estimate band.", _obj({"days": {"type": "integer", "minimum": 1, "maximum": 120}})),
    "open_orders": ("Your open orders: Dhan open and forever orders (read from the broker) and ATIP's resting paper "
                    "orders and target / stop rules. Read-only.", _obj({})),
    # W39-MCP: the W39b features
    "dvm": ("A stock's DVM view (Trendlyne-style): durability, valuation and momentum scores 0-100, their levels, "
            "the zone (strong performer, value trap, momentum trap ...) and every input with its numbers.",
            _obj({"symbol": _SYM, "as_of": _AS_OF}, ["symbol"])),
    "earnings_surprise": ("A stock's earnings surprise as of a day, point in time from ATIP's own quarterly filings (no "
                          "consensus estimates): SUE, revenue SUE, the EPS-trend revisions proxy, its recent "
                          "quarters, post-earnings-drift (PEAD) signals and the PEAD scans' record.",
                          _obj({"symbol": _SYM, "as_of": _AS_OF}, ["symbol"])),
    "mf_search": ("Find mutual fund schemes by name in ATIP's stored AMFI NAVs: AMFI scheme code, name, latest NAV "
                  "and date, AMC, category. Every word must appear in the name.",
                  _obj({"query": {"type": "string", "description": "words of the scheme name, e.g. 'parag flexi "
                                                                   "direct growth'"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["query"])),
    "mf_analytics": ("A mutual fund scheme's analytics on its stored NAVs: point-to-point and rolling returns (min / "
                     "median / max, % of windows above a hurdle), volatility, drawdown with dates, Sharpe / Sortino, "
                     "beta / alpha / tracking error against an index, and its AMFI-category rank with coverage "
                     "(the top peers only).",
                     _obj({"scheme": _SCHEME, "as_of": _AS_OF,
                           "hurdle_pct": {"type": "number", "description": "rolling-return hurdle, % a year (7)"},
                           "rf_pct": {"type": "number", "description": "risk-free rate, % a year"},
                           "benchmark": {"type": "string", "description": "a prices_daily index symbol (NIFTY50)"},
                           "risk_years": {"type": "integer", "minimum": 1, "maximum": 10},
                           "peers": {"type": "boolean", "description": "include the category rank (default true)"}},
                          ["scheme"])),
    "mf_sip": ("A monthly SIP and a lump sum of the same total on a scheme's stored NAVs: units, value, gain and XIRR. "
               "Optional annual step-up; the 0.005 % stamp duty and units rounded down to 3 decimals are on by "
               "default; an optional exit load on units held under N days.",
               _obj({"scheme": _SCHEME, "amount": {"type": "number", "minimum": 1, "description": "₹ an instalment"},
                     "day": {"type": "integer", "minimum": 1, "maximum": 31},
                     "start": {"type": "string", "description": "YYYY-MM-DD; default 3 years before end"},
                     "end": {"type": "string", "description": "YYYY-MM-DD; default the last stored NAV"},
                     "lump_sum": {"type": "number", "minimum": 1, "description": "default the SIP's total"},
                     "step_up_pct": {"type": "number", "minimum": 0, "maximum": 100,
                                     "description": "% rise a year, on each anniversary of the first instalment"},
                     "stamp_duty": {"type": "boolean"}, "round_units": {"type": "boolean"},
                     "exit_load_pct": {"type": "number", "minimum": 0, "maximum": 10},
                     "exit_load_days": {"type": "integer", "minimum": 1, "maximum": 3650},
                     "include_schedule": {"type": "boolean", "description": "every instalment (default false)"}},
                    ["scheme", "amount"])),
    "mf_compare": ("Two to ten mutual fund schemes side by side on a common as-of date (returns, risk, ranks), or one "
                   "scheme's AMFI-category peers (category_of). Give schemes or category_of, not both.",
                   _obj({"schemes": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10},
                         "category_of": _SCHEME, "same_plan": {"type": "boolean"}, "as_of": _AS_OF})),
    "backtest_validation": ("Stored backtest validations: CPCV (combinatorially purged cross-validation: the "
                            "out-of-sample path distribution and the PBO of its splits) and PBO (probability of "
                            "backtest overfitting) runs. Without run_id: the latest runs; with it: that run's "
                            "results and notes.",
                            _obj({"run_id": {"type": "string"}, "kind": {"type": "string", "enum": ["cpcv", "pbo"]},
                                  "strategy": {"type": "string"},
                                  "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                                  "detail": {"type": "boolean", "description": "per split / path / trial and the "
                                                                               "run's configuration"}})),
    "chart_marks": ("What a stock's chart marks: the stored technical signals (chart-pattern breakouts flagged, with "
                    "their levels), candle patterns per day, the chart patterns the 20:30 run listed, and the patterns "
                    "in place on the last bar with their lines.",
                    _obj({"symbol": _SYM, "sessions": {"type": "integer", "minimum": 20, "maximum": 2500,
                                                       "description": "daily bars back (default 250)"}},
                         ["symbol"])),
}
OPEN_WORLD = {"open_orders"}


class ToolError(Exception):
    pass


def _finite(o):
    if isinstance(o, dict):
        return {str(k): _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_finite(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    if type(o).__module__ == "numpy" and hasattr(o, "item"):     # numpy scalars (the chart patterns' levels)
        return _finite(o.item())
    return o


def connect():
    """A connection that cannot write (SQLite PRAGMA query_only)."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception as e:                         # not SQLite: read-only by the tool set alone
        log.info(f"query_only not applied: {e}")
    return conn


def _sym(a):
    s = str(a.get("symbol") or "").strip().upper()
    if not s or len(s) > 32 or not all(c.isalnum() or c in "-&_" for c in s):
        raise ToolError("symbol must be an NSE symbol such as RELIANCE")
    return s


def _day(a, key="date"):
    v = a.get(key)
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ToolError(f"{key} must be YYYY-MM-DD")


def _int(a, key, default, lo, hi):
    v = a.get(key)
    if v is None or v == "":
        return default
    try:
        x = int(v)
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be a whole number {lo}-{hi}")
    if not lo <= x <= hi or isinstance(v, bool) or (isinstance(v, float) and v != x):
        raise ToolError(f"{key} must be a whole number {lo}-{hi}")
    return x


def _scheme(v) -> str:
    c = str(v if v is not None else "").strip()
    if not c.isdigit() or len(c) > 12:
        raise ToolError("scheme must be an AMFI scheme code (digits), e.g. 122639; mf_search finds it")
    return c


def _trim_peers(cat, keep: int | None = None):
    """A category-rank block with its peer table cut to the first `keep` rows (by 1Y return; MF_PEERS by
    default) and the scheme itself."""
    if not isinstance(cat, dict) or not isinstance(cat.get("peers"), list):
        return cat
    keep = MF_PEERS if keep is None else keep
    peers = cat["peers"]
    top = peers[:keep]
    top += [p for p in peers[keep:] if p.get("is_target")]
    return {**cat, "peers": top, "peers_total": len(peers), "peers_shown": len(top)}


def _validation_run(run: dict, detail: bool) -> dict:
    """One CPCV / PBO run, compact: the summary without its per-split / per-path / per-run / per-trial lists
    unless detail (then also the configuration snapshot)."""
    summ = dict(run.get("summary") or {})
    omitted = {}
    for k in ("splits", "paths", "runs", "logit_values"):
        if k in summ and not detail:
            omitted[k] = len(summ.pop(k) or [])
    if "trials" in summ and not detail and len(summ["trials"] or []) > TRIALS_SHOWN:
        omitted["trials"] = len(summ["trials"])
        summ["trials"] = summ["trials"][:TRIALS_SHOWN]
    out = {k: run.get(k) for k in ("run_id", "parent_run_id", "kind", "strategy_id", "strategy_version",
                                   "period_label", "start_date", "end_date", "status", "error", "created_at",
                                   "finished_at", "params", "metrics", "bias_report")}
    out["summary"] = summ
    if detail:
        out["config"] = run.get("config")
    elif omitted:
        out["omitted"] = {**omitted, "note": "counts of the lists left out; detail=true returns them"}
    return out


def _w39b_tool(conn, name: str, a: dict):
    """The W39b features (W39-MCP). Every one only reads: no writes, no cache tables."""
    if name == "dvm":
        from research.dvm import for_symbol
        s = _sym(a)
        d = _day(a, "as_of")
        x = for_symbol(conn, s, d) if d else for_symbol(conn, s)
        if x is None:
            raise ToolError(f"{s} is not in the screener snapshot (no price, fundamentals or technical snapshot)")
        return x
    if name == "earnings_surprise":
        from research.earnings_surprise import for_symbol
        d = _day(a, "as_of")
        return for_symbol(conn, _sym(a), str(d) if d else None)
    if name == "mf_search":
        from data.mf_analytics import MAX_STALE_DAYS
        q = str(a.get("query") or "").strip()
        if len(q) < 2:
            raise ToolError("query: at least 2 characters of the scheme's name")
        lim = _int(a, "limit", 20, 1, 50)
        last = conn.execute("SELECT MAX(date) FROM mf_nav").fetchone()[0]
        if not last:
            return {"as_of": None, "schemes": [], "note": "no NAVs stored (data.multi_asset stores AMFI's daily file)"}
        since = date.fromisoformat(str(last)[:10]) - timedelta(days=MAX_STALE_DAYS)
        words = q.split()[:8]
        rows = conn.execute("SELECT scheme_code, scheme_name, nav, date, amc, category FROM mf_nav WHERE date>=? "
                            + "AND scheme_name LIKE ? " * len(words) + "ORDER BY scheme_code, date",
                            [str(since)] + [f"%{w}%" for w in words]).fetchall()
        latest = {}
        for r in rows:                              # ordered by date: each scheme's newest row wins
            latest[str(r[0])] = {"scheme_code": str(r[0]), "scheme_name": r[1], "nav": r[2], "date": str(r[3])[:10],
                                 "amc": r[4], "category": r[5]}
        found = sorted(latest.values(), key=lambda x: str(x["scheme_name"] or ""))
        return {"as_of": str(last)[:10], "matched": len(found), "schemes": found[:lim],
                "note": f"schemes with a NAV in the {MAX_STALE_DAYS} days to {str(last)[:10]} whose name has every "
                        f"word; the code is the 'scheme' of mf_analytics / mf_sip / mf_compare"}
    if name in ("mf_analytics", "mf_sip", "mf_compare"):
        from data import mf_analytics as MA
        d = _day(a, "as_of")
        if name == "mf_analytics":
            out = MA.analytics(conn, _scheme(a.get("scheme")), hurdle_pct=a.get("hurdle_pct"), rf_pct=a.get("rf_pct"),
                               benchmark=a.get("benchmark"), risk_years=a.get("risk_years"),
                               as_of=str(d) if d else None, peers=a.get("peers") is not False)
            for r in out["rolling"].values():       # the chart series (up to 600 points a window): its size only
                r["series_points"] = len(r.pop("series", None) or [])
            if "category" in out:
                out["category"] = _trim_peers(out["category"])
            return out
        if name == "mf_sip":
            out = MA.sip(conn, _scheme(a.get("scheme")), a.get("amount"), a.get("day"), a.get("start"), a.get("end"),
                         a.get("lump_sum"), step_up_pct=a.get("step_up_pct"), stamp_duty=a.get("stamp_duty"),
                         round_units=a.get("round_units"), exit_load_pct=a.get("exit_load_pct"),
                         exit_load_days=a.get("exit_load_days"))
            if not a.get("include_schedule") and "schedule" in out:
                out["schedule_rows"] = len(out.pop("schedule"))
            return out
        codes, cat = a.get("schemes"), a.get("category_of")
        if bool(codes) == bool(cat):
            raise ToolError("give either schemes (2-10 AMFI codes) or category_of (one code)")
        if cat:
            return _trim_peers(MA.category_rank(conn, _scheme(cat), same_plan=a.get("same_plan") is not False,
                                                as_of=str(d) if d else None), 2 * MF_PEERS)
        if not isinstance(codes, list):
            raise ToolError("schemes must be a list of AMFI scheme codes")
        return MA.compare(conn, [_scheme(c) for c in codes], as_of=str(d) if d else None)
    if name == "backtest_validation":
        from backtest import store
        kinds = ("cpcv", "pbo")
        rid = str(a.get("run_id") or "").strip()
        if rid:
            if len(rid) > 64 or not rid.isalnum():
                raise ToolError("run_id must be a backtest run id")
            run = store.get_run(conn, rid)
            if not run:
                raise ToolError(f"no backtest run {rid}")
            if run.get("kind") not in kinds:
                raise ToolError(f"run {rid} is a {run.get('kind')} run, not a CPCV / PBO validation")
            return _validation_run(run, bool(a.get("detail")))
        kind = a.get("kind")
        if kind and kind not in kinds:
            raise ToolError("kind must be cpcv or pbo")
        lim = _int(a, "limit", VALIDATION_LIST, 1, 50)
        sql = ("SELECT run_id, parent_run_id, kind, strategy_id, strategy_version, period_label, start_date, end_date, "
               "status, error, created_at, finished_at, metrics_json FROM backtest_run WHERE kind IN (?, ?)")
        args = list(kinds)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if a.get("strategy"):
            sql += " AND strategy_id=?"
            args.append(str(a["strategy"]))
        cur = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", args + [lim])
        cols = [c[0] for c in cur.description]
        runs = []
        for r in cur.fetchall():
            x = dict(zip(cols, r))
            m = x.pop("metrics_json")
            x["metrics"] = json.loads(m) if m else None
            runs.append(x)
        return {"runs": runs, "note": "newest first; backtest_validation with a run_id gives one run's distribution, "
                                      "PBO and notes. CPCV: python -m backtest cpcv ...; PBO: python -m backtest pbo "
                                      "RUN_ID (or POST /api/backtests/cpcv, /api/backtests/{run_id}/pbo)"}
    if name == "chart_marks":
        from dashboard.stock_view import chart_marks
        s = _sym(a)
        n = _int(a, "sessions", 250, 20, 2500)
        cur = conn.execute("SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol=? AND close>0 "
                           "ORDER BY date DESC LIMIT ?", (s, n))
        cols = [c[0] for c in cur.description]
        px = [dict(zip(cols, r)) for r in cur.fetchall()][::-1]
        if not px:
            raise ToolError(f"no price history for {s}")
        for p in px:
            p["date"] = str(p["date"])[:10]
        return {"symbol": s, "from": px[0]["date"], "to": px[-1]["date"], "sessions": len(px), **chart_marks(conn, s, px),
                "legend": "signals: technical_signal rows (pattern = a chart-pattern breakout, its reason names the "
                          "levels); candles: candle patterns per day with their side; in_place: the chart patterns "
                          "the 20:30 run listed that day; patterns: the patterns in place on the last bar now, with "
                          "their lines (from / to dates and values)"}
    raise KeyError(name)


def call_tool(conn, name: str, a: dict):
    from research import screener as SC
    if name == "screener_fields":
        cat = SC.catalog()
        g = a.get("group")
        if g:
            cat["groups"] = {k: v for k, v in cat["groups"].items() if k.lower() == str(g).lower()}
            if not cat["groups"]:
                raise ToolError(f"no group {g!r}; groups: {', '.join(SC.catalog()['groups'])}")
        return cat
    if name == "screener_run":
        try:
            return SC.run_screen(conn, str(a.get("query") or ""), sort=a.get("sort"), desc=a.get("desc", True),
                                 limit=int(a.get("limit") or 50), columns=a.get("columns"))
        except SC.ScreenError as e:
            raise ToolError(f"query rejected: {e}")
    if name == "signals_today":
        from research.tech_signals import todays_signals
        d = _day(a)
        return {"date": str(d or date.today()),
                "signals": todays_signals(conn, str(d) if d else None, min_confluence=int(a.get("min_confluence") or 0),
                                          alignment=a.get("alignment"))}
    if name == "signal_track_record":
        from research.tech_signals import forward_stats, scan_stats
        h = int(a.get("horizon") or 20)
        if h not in (5, 20, 60):
            raise ToolError("horizon must be 5, 20 or 60")
        mc = int(a.get("min_confluence") or 0)
        return {"per_scan": scan_stats(conn, mc), "vs_nifty": forward_stats(conn, h, mc)}
    if name == "intraday_signals":
        from research.intraday_signals import stats, todays
        return {"signals": todays(conn, _day(a)), "record": stats(conn)}
    if name == "stock_technicals":
        from research.tech_signals import latest_snapshot
        s = _sym(a)
        snap = latest_snapshot(conn, [s]).get(s)
        if not snap:
            raise ToolError(f"no technical snapshot for {s}")
        cur = conn.execute("SELECT date, scan, name, direction, entry, stop, target, confluence, status, return_pct, "
                           "r_multiple FROM technical_signal WHERE symbol=? ORDER BY date DESC LIMIT 30", (s,))
        cols = [c[0] for c in cur.description]
        return {"snapshot": snap, "signals": [dict(zip(cols, r)) for r in cur.fetchall()]}
    if name == "market_pulse":
        from research.market_pulse import pulse
        return pulse(conn)
    if name == "market_gate":
        from research.regime_gate import current
        return current(conn)
    if name == "research_report":
        from research.report import build_report, stored_report
        s = _sym(a)
        rep = stored_report(conn, s)
        if rep and rep.get("as_of") == str(date.today()):
            return rep
        rep = build_report(conn, s)
        if rep.get("price") is None:
            raise ToolError(f"no stored prices for {s}")
        return rep
    if name == "scorecard":
        from research.scorecard import for_symbol
        s = _sym(a)
        sc = for_symbol(conn, s)
        if sc is None:
            raise ToolError(f"no fundamentals stored for {s}")
        return sc
    if name == "event_calendar":
        from research.event_calendar import gap_band, upcoming
        days = int(a.get("days") or 30)
        if not 1 <= days <= 120:
            raise ToolError("days must be 1-120")
        return {"upcoming": upcoming(conn, days=days), "today": gap_band(conn)}
    if name == "open_orders":
        from portfolio.open_orders import waiting
        return waiting(conn)
    return _w39b_tool(conn, name, a)


# ── JSON-RPC ─────────────────────────────────────────────────────────────────

def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_list():
    return [{"name": n, "title": n.replace("_", " ").capitalize(), "description": d, "inputSchema": s,
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                             "openWorldHint": n in OPEN_WORLD}} for n, (d, s) in TOOLS.items()]


def handle(msg: dict, conn_factory=connect):
    """One JSON-RPC message in, one response out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:                            # notifications (initialized, cancelled ...) get no reply
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _error(mid, -32602, f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments must be an object")
        conn = conn_factory()
        try:
            out = _finite(call_tool(conn, name, args))
            text = json.dumps(out, default=str)
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + ' ... [truncated: narrow the request]'
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if isinstance(out, dict) and len(text) <= MAX_TEXT:
                result["structuredContent"] = out
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except (ToolError, ValueError, TypeError, LookupError) as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e)}],
                                                             "isError": True}}
        except Exception as e:                    # a failing reader is a tool error, not a dead server
            log.exception(f"tool {name}")
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {str(e)[:300]}"}], "isError": True}}
        finally:
            conn.close()
    return _error(mid, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None):
    """Read JSON-RPC lines from stdin, answer on stdout until EOF."""
    out = stdout or sys.stdout
    sys.stdout = sys.stderr                        # imported modules' prints must not corrupt the protocol
    for line in (stdin or sys.stdin):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = _error(None, -32700, "parse error")
        else:
            resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp, default=str) + "\n")
            out.flush()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    serve()
