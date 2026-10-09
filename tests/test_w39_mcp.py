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
    assert {t["name"] for t in tools} == set(M.TOOLS) and len(tools) == 20
    assert {"dvm", "earnings_surprise", "mf_search", "mf_analytics", "mf_sip", "mf_compare", "backtest_validation",
            "chart_marks"} <= set(M.TOOLS), "W39-MCP: the W39b features"
    for t in tools:
        assert t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"]
        assert t["annotations"]["idempotentHint"] and t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
        assert set(t["inputSchema"]["required"]) <= set(t["inputSchema"]["properties"]), t["name"]
        assert t["name"] in M.__doc__, f"{t['name']} is missing from the module docstring"
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
    assert len(lines[1]["result"]["tools"]) == len(M.TOOLS) == 20
    rows = json.loads(lines[2]["result"]["content"][0]["text"])["rows"]
    assert {r["symbol"] for r in rows} == {"ACME", "PEER"}


# ── W39-MCP: the W39b features, each on a connection that cannot write ─────

W39B = ("dvm", "earnings_surprise", "mf_search", "mf_analytics", "mf_sip", "mf_compare", "backtest_validation",
        "chart_marks")
FLEXI = "Open Ended Schemes(Equity Scheme - Flexi Cap Fund)"
QE = [dt.date(2022, 6, 30), dt.date(2022, 9, 30), dt.date(2022, 12, 31), dt.date(2023, 3, 31),
      dt.date(2023, 6, 30), dt.date(2023, 9, 30), dt.date(2023, 12, 31), dt.date(2024, 3, 31),
      dt.date(2024, 6, 30), dt.date(2024, 9, 30), dt.date(2024, 12, 31), dt.date(2025, 3, 31),
      dt.date(2025, 6, 30)]
EPS = [10, 11, 12, 13, 11, 11, 14, 13, 12, 12, 15, 15, 17]       # tests/test_w39b_earnings_surprise.py's quarters


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _seed_w39b(conn):
    """Quarterly EPS (SURP), three flexi-cap schemes, a stock with chart marks (CHRT), and stored CPCV / PBO runs."""
    import math
    import numpy as np
    from backtest import store
    from backtest.cpcv import cpcv_paths, pbo
    for i, pe in enumerate(QE):                      # each quarter broadcast 39 days after its end, 18:05
        conn.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, report_date, available_from, nature, "
                     "eps_q, revenue_cr, source) VALUES ('SURP', ?, ?, ?, ?, 'CONSOLIDATED', ?, NULL, 'nse_xbrl')",
                     (f"Q{i}", str(pe), str(pe), f"{pe + dt.timedelta(days=39)} 18:05:00", EPS[i]))
    days = _weekdays(dt.date(2023, 1, 2), 780)
    for code, name, g in (("300001", "Alpha Flexi Cap Fund - Direct Plan - Growth", 0.12),
                          ("300002", "Beta Flexi Cap Fund - Direct Plan - Growth", 0.18),
                          ("300003", "Gamma Flexi Cap Fund - Regular Plan - Growth", 0.10)):
        conn.executemany("INSERT INTO mf_nav (scheme_code, date, nav, scheme_name, amc, category) VALUES "
                         "(?,?,?,?,'Test MF',?)",
                         [(code, str(d), 10 * (1 + g) ** ((d - days[0]).days / 365) * (1 + 0.003 * math.sin(i)),
                           name, FLEXI) for i, d in enumerate(days)])
    bars = _weekdays(dt.date(2026, 3, 2), 150)
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "('CHRT',?,?,?,?,?,1000,'test')",
                     [(str(d), 100 + 0.1 * i, 101 + 0.1 * i, 99 + 0.1 * i, 100 + 0.1 * i + math.sin(i / 4))
                      for i, d in enumerate(bars)])
    for sid, i, scan, nm, direction, reason in (
            ("c1", 20, "macd_bear", "MACD bearish crossover", "BEAR", "MACD crossed below its signal line"),
            ("c2", 100, "golden_cross", "Golden cross", "BULL", "SMA 50 crossed above SMA 200 today"),
            ("c3", 120, "double_bottom_breakout", "Double bottom breakout", "BULL", "closed above the neckline 112.50")):
        conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, reason, entry, stop, "
                     "target, confluence, status) VALUES (?, 'CHRT', ?, ?, ?, ?, ?, 100, 96, 108, 3, 'OPEN')",
                     (sid, str(bars[i]), scan, nm, direction, reason))
    conn.execute("INSERT INTO technical_snapshot (symbol, date, close, patterns, chart_patterns) VALUES "
                 "('CHRT', ?, 110, 'Hammer, NR7', 'Double bottom, neckline 112.50')", (str(bars[110]),))
    conn.commit()
    rng = np.random.default_rng(7)
    sess = [dt.date(2025, 1, 6) + dt.timedelta(days=i) for i in range(60)]
    groups = [sess[g * 10:(g + 1) * 10] for g in range(6)]
    series = [[[(d, float(rng.normal(0.0005 * m, 0.01))) for d in grp] for grp in groups] for m in range(3)]
    out = cpcv_paths(series, 6, 2)
    snap = {"strategy_id": "dip", "strategy_version": "1", "start": "2025-01-06", "end": "2025-03-06", "params": {}}
    cid = store.create_run(conn, snap, kind="cpcv")
    store.save_summary(conn, cid, "COMPLETED",
                       {"groups": [{"index": g, "sessions": 10} for g in range(6)], "n_groups": 6, "k_test": 2,
                        "candidates": [{"stop_pct": s} for s in (3, 5, 7)], "select_by": "sharpe", "n_splits": 15,
                        "n_paths": 5, **out, "runs": [{"candidate": m, "group": g, "run_id": f"r{m}{g}",
                                                       "status": "COMPLETED"} for m in range(3) for g in range(6)],
                        "notes": ["no purge / embargo"]},
                       metrics={"paths": 5, "pbo": out["pbo"]["pbo"]}, bias={"purge_embargo": "none"})
    oid = store.create_run(conn, snap, kind="optimize")
    res = pbo(rng.normal(0, 0.01, (64, 30)).tolist(), 8)
    pid = store.create_run(conn, snap, kind="pbo", parent_run_id=oid)
    store.save_summary(conn, pid, "COMPLETED", {"source_run_id": oid, **res,
                                                 "trials": [{"trial": j, "run_id": f"t{j}", "params": {"x": j}}
                                                            for j in range(30)]},
                       metrics={"pbo": res["pbo"], "trials": 30})
    return {"cpcv": cid, "pbo": pid, "optimize": oid, "cpcv_out": out, "pbo_out": res, "bars": bars}


class _Spy:
    """A tool call's connection: M.connect()'s (query_only on), counting the rows it changed when it closes."""
    changes = []

    def __init__(self):
        self._c = M.connect()
        assert self._c.execute("PRAGMA query_only").fetchone()[0] == 1

    def __getattr__(self, k):
        return getattr(self._c, k)

    def close(self):
        _Spy.changes.append(self._c.total_changes)
        self._c.close()


def _ro(name, args=None):
    r = M.handle(_rpc("tools/call", {"name": name, "arguments": args or {}}), conn_factory=_Spy)["result"]
    return r, (r.get("structuredContent") if not r["isError"] else r["content"][0]["text"])


def _tables():
    import sqlite3
    import db.schema as S
    c = sqlite3.connect(str(S.DB_PATH))
    try:
        return sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master"))
    finally:
        c.close()


@pytest.fixture
def w39b(db):
    seeded = _seed_w39b(db)
    _Spy.changes = []
    before = _tables()
    yield seeded
    assert _Spy.changes and set(_Spy.changes) == {0}, "a tool wrote"
    assert _tables() == before, "a tool created a table (a cache?)"


def test_dvm_and_earnings_surprise_tools(w39b, db):
    import math
    from research import dvm
    r, x = _ro("dvm", {"symbol": "acme"})
    assert not r["isError"] and x["symbol"] == "ACME" and len(x["axes"]) == 3
    want = dvm.for_symbol(db, "ACME")
    assert {k: x[k] for k in dvm.FIELDS} == {k: want[k] for k in dvm.FIELDS}
    r, x = _ro("dvm", {"symbol": "NOPE"})
    assert r["isError"] and "not in the screener snapshot" in x
    # SUE of the last quarter (EPS 17 vs 12): the 8 seasonal changes before 1, 0, 2, 0, 1, 1, 1, 2 -> sd sqrt(4/7)
    r, x = _ro("earnings_surprise", {"symbol": "SURP"})
    assert not r["isError"] and x["latest"]["sue"] == pytest.approx(5 / math.sqrt(4 / 7), abs=1e-3)
    assert "no consensus estimates" in x["method"]
    r, x = _ro("earnings_surprise", {"symbol": "SURP", "as_of": "2025-08-01"})    # FY26Q1 is filed 8 Aug 2025
    assert str(x["latest"]["period_end"])[:10] == "2025-03-31" and x["as_of"] == "2025-08-01"
    r, x = _ro("earnings_surprise", {"symbol": "NOPE"})
    assert r["isError"] and "no quarterly results" in x
    r, x = _ro("earnings_surprise", {"symbol": "SURP", "as_of": "8 Aug"})
    assert r["isError"] and "as_of must be YYYY-MM-DD" in x


def test_mutual_fund_tools(w39b, db, monkeypatch):
    from data import mf_analytics as MA
    monkeypatch.setattr(MA, "settings", lambda: dict(MA.DEFAULTS))
    r, x = _ro("mf_search", {"query": "flexi DIRECT"})
    assert not r["isError"] and x["matched"] == 2
    assert [s["scheme_code"] for s in x["schemes"]] == ["300001", "300002"] and x["schemes"][0]["category"] == FLEXI
    assert _ro("mf_search", {"query": "flexi", "limit": 1})[1]["schemes"][0]["scheme_code"] == "300001"
    assert _ro("mf_search", {"query": "nothing like it"})[1]["matched"] == 0
    assert _ro("mf_search", {"query": "a"})[0]["isError"] and _ro("mf_search", {"query": "ab", "limit": 99})[0]["isError"]
    # analytics: the numbers of data/mf_analytics.py, the rolling chart series and long peer tables left out
    r, x = _ro("mf_analytics", {"scheme": "300001", "rf_pct": 6.5})
    want = MA.analytics(db, "300001", rf_pct=6.5)
    assert not r["isError"] and x["returns"]["1Y"]["absolute_pct"] == want["returns"]["1Y"]["absolute_pct"]
    assert x["risk"]["volatility_pct"] == want["risk"]["volatility_pct"] and x["risk"]["periods_per_year"]
    assert "series" not in x["rolling"]["1Y"] and x["rolling"]["1Y"]["series_points"] == len(want["rolling"]["1Y"]["series"])
    assert x["category"]["peers_total"] == 2 and x["category"]["rank"]["return_1y"]["rank"] == 2
    monkeypatch.setattr(M, "MF_PEERS", 1)                 # Beta (18 %) leads; the scheme itself is kept
    peers = _ro("mf_analytics", {"scheme": "300001"})[1]["category"]["peers"]
    assert [p["scheme_code"] for p in peers] == ["300002", "300001"]
    assert "category" not in _ro("mf_analytics", {"scheme": "300001", "peers": False})[1]
    # SIP: the module's figures (step-up, stamp duty, exit load), the schedule only on request
    args = {"scheme": "300001", "amount": 5000, "day": 5, "start": "2024-01-01", "end": "2025-12-31",
            "step_up_pct": 10, "exit_load_pct": 1, "exit_load_days": 365}
    r, x = _ro("mf_sip", args)
    want = MA.sip(db, "300001", 5000, 5, "2024-01-01", "2025-12-31", step_up_pct=10, exit_load_pct=1,
                  exit_load_days=365)
    assert not r["isError"] and x["sip"] == json.loads(json.dumps(want["sip"], default=str))
    assert "schedule" not in x and x["schedule_rows"] == 24 and x["sip"]["last_instalment"] == 5500.0
    assert len(_ro("mf_sip", {**args, "include_schedule": True})[1]["schedule"]) == 24
    off = _ro("mf_sip", {**args, "stamp_duty": False, "round_units": False})[1]
    assert off["sip"]["stamp_duty"] == 0 and off["inputs"]["round_units"] is False
    assert "amount is required" in _ro("mf_sip", {"scheme": "300001"})[1]
    # compare: schemes side by side, or a category
    r, x = _ro("mf_compare", {"schemes": ["300001", "300002"]})
    assert not r["isError"] and [s["scheme_code"] for s in x["schemes"]] == ["300001", "300002"]
    assert x["rank"]["return_1y"] == {"300002": 1, "300001": 2}
    r, x = _ro("mf_compare", {"category_of": "300003", "same_plan": False})
    assert not r["isError"] and x["coverage"]["compared"] == 3 and x["peers_total"] == 3
    for bad in ({}, {"schemes": ["300001", "300002"], "category_of": "300001"}, {"schemes": ["300001"]},
                {"schemes": "300001,300002"}, {"category_of": "abc"}):
        assert _ro("mf_compare", bad)[0]["isError"], bad
    r, x = _ro("mf_analytics", {"scheme": "12x"})
    assert r["isError"] and "AMFI scheme code" in x
    assert _ro("mf_analytics", {"scheme": "999999"})[0]["isError"]


def test_backtest_validation_tool(w39b):
    ids = w39b
    r, x = _ro("backtest_validation")
    assert not r["isError"] and {u["run_id"] for u in x["runs"]} == {ids["cpcv"], ids["pbo"]}, "not the optimisation"
    assert {u["kind"] for u in x["runs"]} == {"cpcv", "pbo"} and all(u["status"] == "COMPLETED" for u in x["runs"])
    assert [u["run_id"] for u in _ro("backtest_validation", {"kind": "pbo"})[1]["runs"]] == [ids["pbo"]]
    assert _ro("backtest_validation", {"strategy": "other"})[1]["runs"] == []
    assert len(_ro("backtest_validation", {"limit": 1})[1]["runs"]) == 1
    # one CPCV run, compact: the distribution and PBO of its splits; the per-split / path / run lists counted
    r, x = _ro("backtest_validation", {"run_id": ids["cpcv"]})
    out = ids["cpcv_out"]
    assert not r["isError"] and x["kind"] == "cpcv" and x["strategy_id"] == "dip"
    assert x["summary"]["pbo"]["pbo"] == out["pbo"]["pbo"] and x["summary"]["n_paths"] == 5
    assert x["summary"]["distribution"]["sharpe"]["median"] == out["distribution"]["sharpe"]["median"]
    assert {k: x["omitted"][k] for k in ("splits", "paths", "runs")} == {"splits": 15, "paths": 5, "runs": 18}
    assert "splits" not in x["summary"] and "config" not in x and x["metrics"]["paths"] == 5
    full = _ro("backtest_validation", {"run_id": ids["cpcv"], "detail": True})[1]
    assert len(full["summary"]["splits"]) == 15 and len(full["summary"]["paths"]) == 5 and full["config"]["strategy_id"]
    # one PBO run: 20 of its 30 trials, the 70 logits counted
    x = _ro("backtest_validation", {"run_id": ids["pbo"]})[1]
    assert x["summary"]["pbo"] == ids["pbo_out"]["pbo"] and len(x["summary"]["trials"]) == M.TRIALS_SHOWN
    assert x["omitted"]["trials"] == 30 and x["omitted"]["logit_values"] == 70 and x["parent_run_id"] == ids["optimize"]
    for bad, msg in (({"run_id": ids["optimize"]}, "not a CPCV / PBO"), ({"run_id": "feedface"}, "no backtest run"),
                     ({"run_id": "x'; --"}, "must be a backtest run id"), ({"kind": "walkforward"}, "cpcv or pbo"),
                     ({"limit": 0}, "limit")):
        r, x = _ro("backtest_validation", bad)
        assert r["isError"] and msg in x, (bad, x)


def test_chart_marks_tool(w39b, db):
    from dashboard.stock_view import chart_marks
    bars = w39b["bars"]
    r, x = _ro("chart_marks", {"symbol": "CHRT", "sessions": 60})       # the last 60 bars: from bars[90]
    assert not r["isError"] and (x["from"], x["to"], x["sessions"]) == (str(bars[90]), str(bars[-1]), 60)
    sig = {s["scan"]: s for s in x["signals"]}
    assert set(sig) == {"golden_cross", "double_bottom_breakout"}, "the MACD signal is before the range"
    assert sig["double_bottom_breakout"]["pattern"] is True and "112.50" in sig["double_bottom_breakout"]["reason"]
    assert sig["golden_cross"]["pattern"] is False and sig["golden_cross"]["reason"] is None
    assert {(c["name"], c["side"]) for c in x["candles"]} == {("Hammer", "BULL"), ("NR7", "NEUTRAL")}
    assert x["in_place"] == [{"date": str(bars[110]), "text": "Double bottom, neckline 112.50"}]
    assert isinstance(x["patterns"], list) and "legend" in x
    px = [{"date": str(d)} for d in bars[90:]]
    assert x["signals"] == json.loads(json.dumps(chart_marks(db, "CHRT", px)["signals"], default=str))
    every = _ro("chart_marks", {"symbol": "CHRT"})[1]                     # 250 by default: all 150 stored
    assert every["sessions"] == 150 and len(every["signals"]) == 3
    for bad, msg in (({"symbol": "NOPE"}, "no price history"), ({"symbol": "CHRT", "sessions": 5}, "sessions"),
                     ({"symbol": "CHRT", "sessions": "many"}, "sessions"), ({}, "NSE symbol")):
        r, x = _ro("chart_marks", bad)
        assert r["isError"] and msg in x, (bad, x)


def test_the_w39b_tools_in_a_real_stdio_session(w39b, tmp_path):
    """Through the script itself (its own query_only connection), every new tool answers."""
    import db.schema as S
    env = dict(os.environ, ATIP_DB_PATH=str(S.DB_PATH))
    calls = [("dvm", {"symbol": "ACME"}), ("earnings_surprise", {"symbol": "SURP"}), ("mf_search", {"query": "flexi"}),
             ("mf_analytics", {"scheme": "300001"}), ("mf_sip", {"scheme": "300001", "amount": 1000}),
             ("mf_compare", {"schemes": ["300001", "300002"]}), ("backtest_validation", {}),
             ("chart_marks", {"symbol": "CHRT"})]
    assert [c[0] for c in calls] == list(W39B)
    msgs = [_rpc("initialize", {"protocolVersion": "2025-06-18"}, 0)] + [
        _rpc("tools/call", {"name": n, "arguments": a}, i + 1) for i, (n, a) in enumerate(calls)]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, os.path.join(root, "tools", "atip_mcp.py")],
                       input="\n".join(map(json.dumps, msgs)) + "\n", capture_output=True, text=True, timeout=180,
                       env=env, cwd=str(tmp_path))
    lines = {x["id"]: x for x in map(json.loads, p.stdout.splitlines())}
    assert sorted(lines) == list(range(len(calls) + 1)), p.stderr[-2000:]
    for i, (n, _a) in enumerate(calls, 1):
        res = lines[i]["result"]
        assert not res["isError"], (n, res["content"][0]["text"][:500])
        assert len(res["content"][0]["text"]) < 60_000, f"{n} is not compact"
    _Spy.changes = [0]                                    # the subprocess used its own connections
