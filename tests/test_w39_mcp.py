"""
W39 Phase 4 (item 2): the read-only ATIP MCP server -- the JSON-RPC handshake and version negotiation, the
tool list and its read-only annotations, tool calls on a seeded database, tool errors as results, a
connection that cannot write, a clean stdout, and a real subprocess session over stdio.
"""
import datetime as dt
import io
import json
import os
import subprocess
import sys

import pytest

from tools import atip_mcp as M


def _rpc(method, params=None, mid=1):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


def test_handshake_ping_and_unknown_methods():
    r = M.handle(_rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}))["result"]
    assert r["protocolVersion"] == "2025-03-26" and r["capabilities"] == {"tools": {"listChanged": False}}
    assert r["serverInfo"]["name"] == "atip" and "Nothing here can place" in r["instructions"]
    assert M.handle(_rpc("initialize", {"protocolVersion": "1999-01-01"}))["result"]["protocolVersion"] == M.VERSIONS[0]
    assert M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert M.handle(_rpc("ping")) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert M.handle(_rpc("resources/list"))["error"]["code"] == -32601
    assert M.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600


def test_every_tool_is_annotated_read_only():
    tools = M.handle(_rpc("tools/list"))["result"]["tools"]
    assert {t["name"] for t in tools} == set(M.TOOLS) and len(tools) == 12
    for t in tools:
        assert t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"]
        assert t["inputSchema"]["type"] == "object"
    assert {t["name"] for t in tools if t["annotations"]["openWorldHint"]} == {"open_orders"}
    assert not any(w in " ".join(M.TOOLS) for w in ("order_place", "cancel", "modify", "save", "delete"))


# ── tools on a seeded database ────────────────────────────────────────────

@pytest.fixture
def db(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    from research import screener as SC
    import data.index_constituents as IC
    init_db()
    SC.clear_cache()
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: {"ACME": "Capital Goods", "PEER": "Capital Goods"})
    conn = get_connection()
    today = dt.date.today()
    for sym, eps in (("ACME", 10.0), ("PEER", 20.0)):
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "(?,?,200,200,200,200,1000,'dhan')", (sym, str(today)))
        conn.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, eps_ttm, book_value_ps, roe, roce, "
                     "debt_equity, shares_out, source) VALUES (?, 'Q1', ?, ?, 50, 0.2, 0.25, 0.3, 1e8, 'nse_xbrl')",
                     (sym, str(today - dt.timedelta(days=60)), eps))
    conn.commit()
    yield conn
    conn.close()
    SC.clear_cache()


def _call(name, args=None):
    r = M.handle(_rpc("tools/call", {"name": name, "arguments": args or {}}))
    return r.get("result"), r.get("error")


def test_screener_tools(db):
    res, err = _call("screener_fields", {"group": "Scorecard"})
    assert err is None and list(res["structuredContent"]["groups"]) == ["Scorecard"]
    res, _ = _call("screener_run", {"query": "pe < 15", "limit": 5})
    out = json.loads(res["content"][0]["text"])
    assert not res["isError"] and [r["symbol"] for r in out["rows"]] == ["PEER"]
    res, _ = _call("screener_run", {"query": "pe <"})
    assert res["isError"] and "query rejected" in res["content"][0]["text"]
    res, _ = _call("screener_fields", {"group": "Nope"})
    assert res["isError"] and "groups:" in res["content"][0]["text"]


def test_stock_tools_and_errors(db):
    res, _ = _call("scorecard", {"symbol": "acme"})
    assert not res["isError"] and len(res["structuredContent"]["axes"]) == 5
    res, _ = _call("research_report", {"symbol": "ACME"})
    assert not res["isError"] and res["structuredContent"]["symbol"] == "ACME"
    res, _ = _call("scorecard", {"symbol": "NOPE"})
    assert res["isError"] and "no fundamentals" in res["content"][0]["text"]
    res, _ = _call("scorecard", {})
    assert res["isError"] and "NSE symbol" in res["content"][0]["text"]
    res, _ = _call("signal_track_record", {"horizon": 7})
    assert res["isError"]
    _res, err = _call("place_order", {"symbol": "ACME"})
    assert err["code"] == -32602


def test_market_tools_work_on_a_connection_that_cannot_write(db):
    for name, args in (("market_pulse", {}), ("market_gate", {}), ("event_calendar", {"days": 30}),
                       ("signals_today", {}), ("signal_track_record", {}), ("intraday_signals", {})):
        res, err = _call(name, args)
        assert err is None and not res["isError"], (name, res["content"][0]["text"][:300])
    conn = M.connect()
    try:
        with pytest.raises(Exception, match="readonly"):
            conn.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('X', '2026-01-01', 1)")
    finally:
        conn.close()


def test_stdout_carries_only_protocol_messages(db, monkeypatch):
    real = M.call_tool

    def noisy(conn, name, a):
        print("dhanhq not installed. Run: pip install dhanhq")      # what some modules print on import
        return real(conn, name, a)
    monkeypatch.setattr(M, "call_tool", noisy)
    out = io.StringIO()
    stdin = io.StringIO("\n".join(json.dumps(m) for m in (
        _rpc("initialize", {"protocolVersion": "2025-06-18"}, 1), {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _rpc("tools/call", {"name": "screener_fields", "arguments": {}}, 2))) + "\nnot json\n")
    saved = sys.stdout
    try:
        M.serve(stdin=stdin, stdout=out)
    finally:
        sys.stdout = saved
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x.get("id") for x in lines] == [1, 2, None] and lines[2]["error"]["code"] == -32700


def test_a_real_stdio_session(db, tmp_path):
    import db.schema as S
    env = dict(os.environ, ATIP_DB_PATH=str(S.DB_PATH))
    msgs = [_rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}, 1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, _rpc("tools/list", None, 2),
            _rpc("tools/call", {"name": "screener_run", "arguments": {"query": "roce_pct > 20"}}, 3)]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, os.path.join(root, "tools", "atip_mcp.py")], input="\n".join(map(json.dumps, msgs)) + "\n",
                       capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    lines = [json.loads(x) for x in p.stdout.splitlines()]
    assert [x["id"] for x in lines] == [1, 2, 3], p.stderr[-2000:]
    assert len(lines[1]["result"]["tools"]) == 12
    rows = json.loads(lines[2]["result"]["content"][0]["text"])["rows"]
    assert {r["symbol"] for r in rows} == {"ACME", "PEER"}
