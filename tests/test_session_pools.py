"""Session cash pools (orders/sessions.py start_pool / ledger / pool_quantity) and the paper P&L report
(portfolio/paper_report.py, /command/pnl). Orders run orders.rules -> orders.broker -> orders.paper for
real on a throwaway database: the `paper` fixture's account opens with Rs 1,00,000, no slippage and
no charges unless a test turns them on. Every expected figure is worked out by hand beside it."""

import json
from datetime import date

import pytest

from tests.test_w39_qa_suite import _rule, paper  # noqa: F401  (pytest fixture)


def _conn():
    from db.schema import get_connection
    return get_connection()


def _do(fn, *a, **k):
    c = _conn()
    try:
        return fn(c, *a, **k)
    finally:
        c.close()


def _pool(sid="S1", capital=25000.0, status="RUNNING", **kw):
    from orders import sessions
    _do(sessions.seed)
    _do(sessions.start_pool, sid, capital, **kw)
    if status:
        _do(sessions.set_status, sid, status)


def _entry(symbol="ACME", trigger=100.0, sid="S1"):
    """A BUY watched at `trigger`, 10 shares as typed, sent without a tap, linked to `sid`."""
    from orders import rules, sessions
    r = _rule(symbol=symbol, trigger_value=trigger, require_confirmation=False)
    _do(sessions.link_rule, sid, r["id"])
    return rules.get_rule(r["id"])


def _ledger(sid="S1"):
    from orders import sessions
    return _do(sessions.ledger, sid)


def _tick(feed, **prices):
    from orders import rules
    feed.prices.update(prices)
    return rules.check_triggers()


# ── setting a pool up ──────────────────────────────────────────────────────────────────────────

def test_start_pool_validates_sets_the_owners_rules_and_logs(paper):
    from orders import sessions
    _do(sessions.seed)
    with pytest.raises(ValueError, match="positive"):
        _do(sessions.start_pool, "S1", 0)
    with pytest.raises(ValueError, match="minimum|more than"):
        _do(sessions.start_pool, "S1", 10000)                    # the Rs 12,000 smallest buy would not fit
    s = _do(sessions.start_pool, "S1", 25000)
    assert (s["start_capital"], s["target_pct"], s["stop_pct"], s["min_buy_value"]) == (25000, 3.0, 2.0, 12000)
    assert s["pool"]["cash"] == 25000 and s["pool"]["equity"] == 25000 and s["pool"]["trades"] == 0
    assert _do(sessions.events, "S1")[0]["event"] == "POOL"


def test_setup_pools_gives_every_session_its_own_money(paper):
    from orders import sessions
    r = _do(sessions.setup_pools, 25000)
    assert r["sessions"] == [f"S{i}" for i in range(1, 11)] and r["total"] == 250000 and r["skipped"] == {}
    assert all(s["pool"]["cash"] == 25000 for s in _do(sessions.list_sessions)["sessions"])
    with pytest.raises(ValueError):
        _do(sessions.setup_pools, -1)                             # a bad figure changes nothing


def test_linking_an_entry_applies_whole_cash_sell_all_at_3_and_stop_2(paper):
    _pool("S1")
    r = _entry()
    assert (r["quantity_type"], r["quantity_value"]) == ("SESSION", 100)
    assert r["bracket_target_pct"] == 3.0 and r["bracket_target2_pct"] is None and r["bracket_stop_pct"] == 2.0
    assert r["resolved_stoploss_price"] == 98.0                   # 100 x (1 - 2%)


def test_a_pool_takes_buys_only(paper):
    from orders import sessions
    _pool("S1")
    r = _rule(side="SELL", trigger_direction="ABOVE")
    with pytest.raises(ValueError, match="cash pool"):
        _do(sessions.link_rule, "S1", r["id"])


# ── the cycle: whole cash in, everything out at +3 %, the next stock gets it all ─────────────────

def test_buys_with_all_its_cash_and_sells_everything_at_3_pct(paper):
    from orders import rules
    _pool("S1")
    r = _entry()
    _tick(paper, ACME=100.0)
    e = rules.get_rule(r["id"])
    assert e["status"] == rules.EXECUTED and e["execution_quantity"] == 250      # 25,000 / 100
    legs = {l["role"]: l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"]}
    assert legs["TARGET"]["quantity_value"] == 250 and legs["TARGET"]["resolved_trigger_price"] == 103.0
    assert legs["STOP"]["quantity_value"] == 250 and legs["STOP"]["resolved_trigger_price"] == 98.0
    L = _ledger()
    assert L["cash"] == 0 and L["open"][0]["qty"] == 250

    _tick(paper, ACME=103.0)                                       # target: the whole 250 go
    L = _ledger()
    assert L["open"] == [] and L["cash"] == 25750 and L["booked"] == 750      # 250 x 3
    assert (L["closed"], L["wins"], L["return_pct"]) == (1, 1, 3.0)


def test_the_next_stock_waits_then_gets_the_whole_cash(paper):
    from orders import rules
    _pool("S1")
    a = _entry("ACME", 100.0)
    b = _entry("BETA", 50.0)
    _tick(paper, ACME=100.0)
    ev = _tick(paper, BETA=50.0)                                   # S1's money is in ACME
    held = [e for e in ev if e["type"] == "HELD_BY_SESSION"]
    assert held and "minimum buy" in held[0]["reason"] and "ACME" in held[0]["reason"]
    assert rules.get_rule(b["id"])["status"] == rules.ACTIVE

    _tick(paper, ACME=103.0)                                       # ACME exits: Rs 25,750 free
    _tick(paper, BETA=50.0)
    assert rules.get_rule(a["id"])["status"] == rules.EXECUTED
    assert rules.get_rule(b["id"])["execution_quantity"] == 515    # 25,750 / 50: the profit is reinvested


def test_a_stop_out_makes_the_next_buy_smaller(paper):
    from orders import rules
    _pool("S1")
    _entry("ACME", 100.0)
    b = _entry("BETA", 50.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=98.0)                                        # stop: 250 x -2 = -500
    L = _ledger()
    assert L["cash"] == 24500 and L["booked"] == -500 and L["wins"] == 0
    _tick(paper, BETA=50.0)
    assert rules.get_rule(b["id"])["execution_quantity"] == 490    # 24,500 / 50


def test_pools_are_independent(paper):
    _pool("S1")
    _pool("S2")
    _entry("ACME", 100.0, "S1")
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    assert _ledger("S1")["cash"] == 25750 and _ledger("S2")["cash"] == 25000


def test_capital_cap_does_not_bind_a_pool(paper):
    from orders import sessions
    _pool("S1")
    _do(sessions.update_session, "S1", {"capital_cap": 1000.0})
    r = _entry()
    assert _do(sessions.gate, r, 24000.0) is None
    assert "more than its" in _do(sessions.gate, r, 26000.0)


def test_session_orders_carry_their_tag_and_the_paper_charges_are_netted(paper, tmp_path):
    from orders import rules
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["paper_cost_model"] = "nse_delivery"
    cfg.write_text(json.dumps(c))
    _pool("S1")
    _entry()
    _tick(paper, ACME=100.0)
    L = _ledger()
    bought = L["open"][0]["qty"]
    assert bought < 250 and L["cash"] >= 0                         # room left for the buy's charges
    _tick(paper, ACME=103.0)
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT transaction_type, tag, brokerage FROM paper_order")]
    assert {r["tag"] for r in rows} == {"ATIP-S1"} and all(r["brokerage"] > 0 for r in rows)
    L = _ledger()
    gross = bought * 3.0
    assert L["booked"] == pytest.approx(gross - sum(r["brokerage"] for r in rows), abs=0.02)
    assert L["cash"] == pytest.approx(25000 + L["booked"], abs=0.02)
    assert rules.list_rules(status=rules.ACTIVE) == []             # the stop went with the target


def test_each_occurrence_starts_with_everything_the_last_ended_with(paper):
    from orders import sessions
    _pool("S1")
    _entry("ACME", 100.0)
    _entry("BETA", 50.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)                                       # occurrence 1: 25,000 -> 25,750
    _tick(paper, BETA=50.0)                                        # occurrence 2 starts with all 25,750
    L = _do(sessions.ledger, "S1", history=True)
    o1, o2 = L["occurrences"]
    assert (o1["n"], o1["symbol"], o1["start_amount"], o1["end_amount"], o1["pct"]) == (1, "ACME", 25000, 25750, 3.0)
    assert (o2["n"], o2["symbol"], o2["start_amount"], o2["closed"]) == (2, "BETA", 25750, False)
    assert (L["occurrence"], L["max_occurrences"], L["latest"]["n"]) == (2, 500, 2)


def test_a_session_stops_after_its_last_occurrence(paper):
    from orders import rules
    _pool("S1", max_occurrences=1)
    _entry("ACME", 100.0)
    b = _entry("BETA", 50.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    ev = _tick(paper, BETA=50.0)
    assert any(e["type"] == "HELD_BY_SESSION" and "all 1 of its occurrences" in e["reason"] for e in ev)
    assert rules.get_rule(b["id"])["status"] == rules.ACTIVE
    assert _ledger()["finished"] is True


def test_total_divided_by_the_amount_per_session_sets_how_many_run(paper):
    from orders import sessions
    r = _do(sessions.setup_pools, 25000, total=250000)
    assert r["sessions"] == [f"S{i}" for i in range(1, 11)] and r["total"] == 250000 and r["left_over"] == 0
    r = _do(sessions.setup_pools, 50000, total=250000)              # 5 sessions of 50,000; S6-S10 end
    assert r["sessions"] == ["S1", "S2", "S3", "S4", "S5"] and r["ended"] == ["S6", "S7", "S8", "S9", "S10"]
    assert [s["id"] for s in _do(sessions.list_sessions)["sessions"] if s["pool"]] == r["sessions"]
    with pytest.raises(ValueError, match="at most 10"):
        _do(sessions.setup_pools, 25000, total=300000)              # 12 sessions
    with pytest.raises(ValueError, match="less than one"):
        _do(sessions.setup_pools, 25000, total=20000)
    with pytest.raises(ValueError, match="whole number"):
        _do(sessions.setup_pools, 25000, max_occurrences="many")


def test_an_open_trade_keeps_its_pool_when_the_split_changes(paper):
    from orders import sessions
    _do(sessions.setup_pools, 25000, total=250000)
    _do(sessions.set_status, "S10", "RUNNING")
    _entry("ACME", 100.0, "S10")
    _tick(paper, ACME=100.0)
    r = _do(sessions.setup_pools, 50000, total=250000)
    assert "S10" in r["skipped"] and "S10" not in r["ended"]
    assert _ledger("S10")["open"][0]["qty"] == 250


def _limits(tmp_path, **lim):
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["risk_limits"] = lim
    cfg.write_text(json.dumps(c))


@pytest.fixture
def valued(paper, monkeypatch):
    """The broker values a MARKET order from the feed (the `paper` fixture's Dhan client refuses, which
    leaves every order valued at Rs 0 and so never measured against max_order_value)."""
    monkeypatch.setattr("orders.broker.get_dhan_client", lambda *a, **k: (None, None))
    return paper


def test_no_amount_cap_on_a_pool_as_it_compounds(valued, tmp_path):
    """max_order_value Rs 10,000: the pool still buys Rs 25,000, sells Rs 25,750, then buys Rs 25,750."""
    from orders import rules
    _limits(tmp_path, max_order_value=10000)
    _pool("S1")
    a = _entry("ACME", 100.0)
    b = _entry("BETA", 50.0)
    _tick(valued, ACME=100.0)
    assert rules.get_rule(a["id"])["execution_quantity"] == 250
    _tick(valued, ACME=103.0)                                      # the Rs 25,750 sale is not capped either
    assert _ledger()["cash"] == 25750
    _tick(valued, BETA=50.0)
    assert rules.get_rule(b["id"])["execution_quantity"] == 515    # the grown cash, all of it


def test_orders_outside_a_pool_keep_the_cap(valued, tmp_path):
    from orders import rules
    _limits(tmp_path, max_order_value=10000)
    r = _rule(require_confirmation=False, quantity_value=250)      # Rs 25,000 with no session
    _tick(valued, ACME=100.0)
    got = rules.get_rule(r["id"])
    assert got["status"] == rules.FAILED and "max_order_value" in got["execution_error"]


def test_the_exemption_is_only_max_order_value(paper, tmp_path):
    from orders import risk, sessions
    _limits(tmp_path, max_order_value=10000, max_orders_per_day=1)
    _pool("S1")
    c = _conn()
    try:
        r = risk.pretrade_check(c, "ACME", "BUY", 250, 25000.0, env="PAPER", pool_session="S1")
        assert r["ok"] and r["checks"][0]["reason"].startswith("not applied")
        from orders.broker import _ensure_order_log_table
        _ensure_order_log_table(c)
        c.execute("INSERT INTO order_log (timestamp, symbol, transaction_type, quantity, status) "
                  "VALUES (datetime('now','localtime'), 'ACME', 'BUY', 1, 'PLACED')")
        assert risk.pretrade_check(c, "ACME", "BUY", 250, 25000.0, env="PAPER",
                                   pool_session="S1")["blocked_by"] == "max_orders_per_day"
        assert sessions.pool_of_tag(c, "ATIP-S1") == "S1"
        assert sessions.pool_of_tag(c, "ATIP-S2") is None          # S2 has no pool
        assert sessions.pool_of_tag(c, "basket:x") is None and sessions.pool_of_tag(c, None) is None
    finally:
        c.close()


# ── the P&L report ─────────────────────────────────────────────────────────────────────────────

def _fill(conn, oid, sym, side, qty, price, fees, at, tag=None):
    conn.execute("INSERT INTO paper_order (order_id, created_at, symbol, transaction_type, quantity, filled_qty, "
                 "order_type, product_type, fill_price, status, brokerage, tag) "
                 "VALUES (?, ?, ?, ?, ?, ?, 'MARKET', 'CNC', ?, 'TRADED', ?, ?)",
                 (oid, at, sym, side, qty, qty, price, fees, tag))


@pytest.fixture
def book(temp_db):
    """Buy 10 ACME at 100 on Mon 5 Oct (charges 1), close 101; Tue close 104; sell 10 at 105 on Wed 7
    Oct (charges 1). The account ends at 1,00,048, so it opened with 1,00,000."""
    from db.schema import init_db
    from orders.paper import ensure_tables
    init_db()
    c = _conn()
    ensure_tables(c)
    _fill(c, "P1", "ACME", "BUY", 10, 100.0, 1.0, "2026-10-05T10:00:00")
    _fill(c, "P2", "ACME", "SELL", 10, 105.0, 1.0, "2026-10-07T11:00:00")
    for d, close in (("2026-10-05", 101), ("2026-10-06", 104), ("2026-10-07", 105)):
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES ('ACME', ?, ?, ?, ?, ?, 1000, 'EQ')", (d, close, close, close, close))
    c.execute("INSERT INTO paper_account (id, balance, opened_at) VALUES (1, 100048, '2026-10-05')")
    c.commit()
    yield c
    c.close()


def test_report_rebuilds_the_days_weeks_and_months(book):
    from portfolio.paper_report import report
    r = report(book, "all", "day", end=date(2026, 10, 9))
    assert (r["opening"], r["equity"], r["pnl"], r["booked"], r["charges"]) == (100000, 100048, 48, 48, 2)
    days = {p["period"]: p for p in r["periods"]}
    assert days["2026-10-05"]["pnl"] == 9          # cash 98,999 + 10 x 101 - 1,00,000
    assert days["2026-10-06"]["pnl"] == 30         # 10 x (104 - 101)
    assert days["2026-10-07"]["pnl"] == 9          # sold at 105 less 1 charge: 1,00,048 - 1,00,039
    assert days["2026-10-07"]["booked"] == 48      # 1,050 - 1 - (1,000 + 1)
    assert days["2026-10-08"]["pnl"] == 0 and days["2026-10-09"]["equity"] == 100048

    wk = report(book, "all", "week", end=date(2026, 10, 9))["periods"]
    assert len(wk) == 1 and (wk[0]["period"], wk[0]["pnl"], wk[0]["trades_closed"], wk[0]["wins"]) == ("2026-10-05", 48, 1, 1)
    mo = report(book, "all", "month", end=date(2026, 10, 9))["periods"]
    assert mo[0]["period"] == "2026-10" and mo[0]["return_pct"] == 0.05      # 48 / 1,00,000

    t = r["trade_list"][0]
    assert (t["symbol"], t["bought_qty"], t["avg_buy"], t["avg_sell"], t["charges"], t["net"]) == \
        ("ACME", 10, 100.0, 105.0, 2.0, 48.0)


def test_report_for_one_pool_starts_from_its_capital(book):
    from orders import sessions
    from portfolio.paper_report import report
    sessions.start_pool(book, "S1", 25000)
    book.execute("UPDATE trading_sessions SET pool_started_at = '2026-10-01T00:00:00' WHERE id = 'S1'")
    _fill(book, "P3", "BETA", "BUY", 100, 200.0, 0.0, "2026-10-06T10:00:00", tag="ATIP-S1")
    _fill(book, "P4", "BETA", "SELL", 100, 206.0, 0.0, "2026-10-08T10:00:00", tag="ATIP-S1")
    book.commit()
    r = report(book, "S1", "week", end=date(2026, 10, 9))
    assert (r["opening"], r["equity"], r["pnl"], r["booked"]) == (25000, 25600, 600, 600)
    assert r["occurrences"] == []                                   # the fills here came without order rules
    assert [t["symbol"] for t in r["trade_list"]] == ["BETA"]       # the untagged ACME trade is not S1's
    assert report(book, "pools", "month", end=date(2026, 10, 9))["opening"] == 25000
    with pytest.raises(ValueError, match="no cash pool"):
        report(book, "S2")


# ── the screens ────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(paper, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from orders import sessions
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(sessions, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), {security.TOKEN_HEADER: security.token()}


def test_pools_api_needs_the_token_and_the_pnl_page_shows_them(api):
    client, h = api
    assert client.post("/api/sessions/pools", json={"capital": 25000}).status_code == 401
    r = client.post("/api/sessions/pools", json={"total": 250000, "capital": 25000, "target_pct": 3, "stop_pct": 2,
                                                  "min_buy_value": 12000, "max_occurrences": 500}, headers=h)
    assert r.status_code == 200 and r.json()["total"] == 250000 and len(r.json()["sessions"]) == 10
    assert client.post("/api/sessions/pools", json={"capital": "lots"}, headers=h).status_code == 400
    d = client.get("/api/command/pnl?scope=pools&by=week").json()
    assert d["opening"] == 250000 and len(d["sessions"]) == 10 and d["sessions"][0]["cash"] == 25000
    assert client.get("/api/command/pnl?by=year").status_code == 400
    page = client.get("/command/pnl")
    assert page.status_code == 200 and "Session pools" in page.text and 'href="/command/pnl"' in page.text
    tiles = client.get("/api/sessions").json()["sessions"]
    assert tiles[0]["pool"]["start_capital"] == 25000 and tiles[0]["pool"]["max_occurrences"] == 500
    one = client.get("/api/command/pnl?scope=S1").json()
    assert one["occurrences"] == [] and client.get("/api/command/pnl").json()["occurrences"] is None


def test_trade_setup_sizes_a_pool_trade_from_the_session(api, tmp_path):
    client, h = api
    _limits(tmp_path, max_order_value=10000)
    client.post("/api/sessions/pools", json={"capital": 25000, "ids": ["S1"]}, headers=h)
    p = client.get("/api/command/preview?symbol=ACME&side=BUY&entry=100&target=110&stop=90&session=S1").json()
    assert (p["qty"], p["target"], p["stop"]) == (250, 103.0, 98.0)     # the session's rules, not the typed ones
    assert "all of its" in p["pool"]["note"]
    assert next(c for c in p["checks"] if c["check"] == "Risk limits")["ok"]     # Rs 25,000 > the Rs 10,000 cap
    p = client.get("/api/command/preview?symbol=ACME&side=BUY&entry=100&qty=250&stop=98").json()
    assert not next(c for c in p["checks"] if c["check"] == "Risk limits")["ok"]  # no session: the cap holds
    assert client.get("/api/command/preview?symbol=ACME&side=SELL&entry=100&session=S1").status_code == 400
