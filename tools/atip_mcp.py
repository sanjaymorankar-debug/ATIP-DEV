"""
W39 Phase 4 (item 2, MC-01..03) — a READ-ONLY MCP server for ATIP, so Claude (Desktop, Code) can use the
screener, signals, market pulse, research reports and scorecards next to the Dhan MCP.

    transport  MCP over stdio: one JSON-RPC 2.0 message per line (initialize, ping, tools/list, tools/call).
               Written to the specification directly, so it needs no new package. Stdout carries only
               protocol messages; anything an imported module prints goes to stderr.
    read-only  every tool reads; there is no tool that writes, orders, cancels or changes a setting. The
               SQLite connection is opened with PRAGMA query_only, so even a bug in a reader cannot write
               (MySQL / PostgreSQL deployments: read-only by the tool set alone). `open_orders` reads your
               Dhan order book through the broker API (read-only call).
    tools      screener_fields, screener_run, signals_today, signal_track_record, intraday_signals,
               stock_technicals, market_pulse, market_gate, research_report, scorecard, event_calendar,
               open_orders

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
from datetime import date, datetime

if __package__ in (None, ""):                       # run as a script: make the repository importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("atip_mcp")

SERVER = {"name": "atip", "title": "ATIP (read-only)", "version": "w39"}
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 200_000
INSTRUCTIONS = (
    "ATIP is the user's own analytics platform for NSE stocks. These tools only read ATIP's stored data: "
    "screens, end-of-day and intraday technical signals with their track records, the market pulse and gate, "
    "equity research reports and the fundamental scorecard, the macro event calendar, and open orders. Nothing "
    "here can place, change or cancel an order. Report the dates the data is from; outputs are model results "
    "from ATIP's own data, not investment advice. Call screener_fields before writing a screener query."
)


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_SYM = {"type": "string", "description": "NSE symbol, e.g. RELIANCE"}
_DATE = {"type": "string", "description": "YYYY-MM-DD; default today"}

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
                      "market gate at birth, and each scan's record in today's market.",
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


def _day(a):
    v = a.get("date")
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ToolError("date must be YYYY-MM-DD")


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
    raise KeyError(name)


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
