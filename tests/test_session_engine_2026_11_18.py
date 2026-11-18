"""Session engine (owner prompt 18 Nov 2026, 03:05 PM IST): independent Rs 25,000 sessions -- orders/session_engine.py
over the cash pools of orders/sessions.py.

Real-path tests run orders.rules -> orders.broker -> orders.paper on a throwaway database (the `paper` fixture: a
Rs 1,00,000 paper account, no slippage; charges switched on where a test says so). The 10 x 500 simulation writes the
fills the engine would have written (executed order rules and paper orders with the real NSE delivery charges) so it
runs in seconds, then checks every ledger to the paisa against a hand-rolled cash walk."""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_w39_qa_suite import _rule, paper  # noqa: F401  (pytest fixture)

IST = timezone(timedelta(hours=5, minutes=30))


def _conn():
    from db.schema import get_connection
    return get_connection()


def _do(fn, *a, **k):
    c = _conn()
    try:
        return fn(c, *a, **k)
    finally:
        c.close()


def _cfg(tmp_path, **kw):
    p = tmp_path / "config.json"
    c = json.loads(p.read_text())
    c.update(kw)
    p.write_text(json.dumps(c))


@pytest.fixture
def eng(paper, tmp_path, monkeypatch):
    """The paper fixture with the engine (and the capital plan) reading the test's config.json."""
    from orders import capital_plan, session_engine
    monkeypatch.setattr(session_engine, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(capital_plan, "CONFIG_PATH", tmp_path / "config.json")
    return paper


def _pool(sid="S1", capital=25000.0, status="RUNNING", **kw):
    from orders import sessions
    _do(sessions.seed)
    _do(sessions.start_pool, sid, capital, **kw)
    if status:
        _do(sessions.set_status, sid, status)


def _entry(symbol="ACME", trigger=100.0, sid="S1"):
    from orders import rules, sessions
    r = _rule(symbol=symbol, trigger_value=trigger, require_confirmation=False)
    _do(sessions.link_rule, sid, r["id"])
    return rules.get_rule(r["id"])


def _tick(feed, **prices):
    from orders import rules
    feed.prices.update(prices)
    return rules.check_triggers()


def _rows(sql, a=()):
    c = _conn()
    try:
        return [dict(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


def _ist_close(ts: str, seconds: int = 120) -> bool:
    now = datetime.now(IST).replace(tzinfo=None)
    return abs((datetime.fromisoformat(ts) - now).total_seconds()) < seconds


# ── 1. creation and the split ──────────────────────────────────────────────────────────────────────

def test_the_split_is_floor_of_funds_over_25k_and_the_rest_is_the_reserve(eng, tmp_path):
    from orders import session_engine as E
    _cfg(tmp_path, paper_opening_balance=112345.0)
    p = _do(E.plan)
    assert (p["env"], p["seed"], p["available"], p["new_sessions"], p["reserve"]) == ("PAPER", 25000.0, 112345.0, 4, 12345.0)
    with pytest.raises(ValueError, match="ack"):
        _do(E.create, None, False)                                   # it moves money: ack first
    with pytest.raises(ValueError, match="only 4"):
        _do(E.create, 5, True)
    r = _do(E.create, None, True)
    assert r["created"] == ["S11", "S12", "S13", "S14"] and r["state"] == "CREATED"
    assert (r["reserve_before"], r["reserve_after"]) == (112345.0, 12345.0)
    for sid in r["created"]:
        st = _do(E.stats, sid)
        assert (st["seed_capital"], st["cash_available"], st["current_balance"], st["state"]) == (25000, 25000, 25000, "CREATED")
        assert st["max_completed_trades"] == 500 and st["floor"] == 20000.0      # 80 % of the seed
        assert _ist_close(st["created_at_ist"])
    assert _do(E.plan)["new_sessions"] == 0                          # Rs 12,345 is below one seed


def test_the_split_keeps_the_capital_plans_cash_buffer(eng, tmp_path):
    """With the owner's paper_capital_plan (10 % buffer, 16 Nov) the split only uses the money above the buffer."""
    from orders import session_engine as E
    _cfg(tmp_path, paper_opening_balance=100000.0, paper_capital_plan={"cash_buffer_pct": 10, "max_session_pct": 50})
    p = _do(E.plan)
    assert (p["buffer"], p["available"], p["new_sessions"], p["reserve"]) == (10000.0, 90000.0, 3, 25000.0)
    _cfg(tmp_path, session_engine={"respect_buffer": False})
    assert _do(E.plan)["new_sessions"] == 4


def test_a_created_session_holds_its_entries_until_activated(eng, tmp_path):
    from orders import sessions as S, session_engine as E
    _do(E.create, 1, True)
    r = _entry(sid="S11")
    assert "CREATED" in _do(S.gate, r, 1000.0)
    with pytest.raises(ValueError, match="ack"):
        _do(E.activate, "S11")
    st = _do(E.activate, "S11", True)
    assert st["state"] == "ACTIVE" and st["mode"] == "AUTO" and st["auto_pick"]
    assert _do(S.gate, r, 1000.0) is None
    ev = [e for e in _do(S.events, "S11", 50) if e["event"] == "STATE"]
    assert [json.loads(e["detail"])["to"] for e in reversed(ev)] == ["CREATED", "ACTIVE"]
    assert all(_ist_close(e["at"]) for e in ev)                      # every state change carries its IST time


# ── 2-4. the wallet, charges, compounding and the counts on the real order path ─────────────────────

def _round_trip(feed, buy=100.0, sell=103.0, sid="S1", symbol="ACME"):
    _entry(symbol=symbol, trigger=buy, sid=sid)
    _tick(feed, **{symbol: buy})
    _tick(feed, **{symbol: sell})


def test_ledger_maths_charges_and_counts_on_one_round_trip(eng, tmp_path):
    from orders import session_engine as E
    _cfg(tmp_path, paper_cost_model="nse_delivery")
    _pool("S1")
    _entry()
    _tick(eng, ACME=100.0)
    st = _do(E.stats, "S1")
    assert (st["legs"], st["completed_trades"], st["open_trades"]) == (1, 0, 1)   # an open position is not a trade
    qty = st["open"][0]["qty"]
    _tick(eng, ACME=103.0)
    fees = _rows("SELECT transaction_type, brokerage, fill_price, filled_qty FROM paper_order ORDER BY created_at, rowid")
    assert [f["transaction_type"] for f in fees] == ["BUY", "SELL"] and all(f["brokerage"] > 0 for f in fees)
    _do(E.sync)
    st = _do(E.stats, "S1")
    buy_v, sell_v = qty * 100.0, qty * 103.0
    net = round(sell_v - buy_v - fees[0]["brokerage"] - fees[1]["brokerage"], 2)
    assert (st["legs"], st["completed_trades"], st["wins"], st["losses"]) == (2, 1, 1, 0)
    assert st["realised_pnl"] == pytest.approx(net, abs=0.01) and st["avg_pnl_per_trade"] == pytest.approx(net, abs=0.01)
    assert st["cash_available"] == pytest.approx(25000 + net, abs=0.01) and st["invested_value"] == 0
    assert st["charges"] == pytest.approx(fees[0]["brokerage"] + fees[1]["brokerage"], abs=0.01)
    led = _do(E.ledger_rows, "S1")
    assert [r["type"] for r in led] == ["SEED", "BUY", "CHARGES", "SELL", "CHARGES", "PNL"]
    assert [r["amount"] for r in led][:5] == pytest.approx([25000, -buy_v, -fees[0]["brokerage"], sell_v,
                                                           -fees[1]["brokerage"]], abs=0.005)
    assert led[-1]["amount"] == pytest.approx(net, abs=0.01) and led[-1]["balance_after"] == pytest.approx(25000 + net, abs=0.005)
    tr = _do(E.trades, "S1")
    assert len(tr) == 1 and tr[0]["status"] == "CLOSED" and tr[0]["buy_leg_id"] and tr[0]["sell_leg_id"]
    assert tr[0]["net_pnl"] == pytest.approx(net, abs=0.01) and tr[0]["gross_pnl"] == pytest.approx(sell_v - buy_v, abs=0.01)
    assert tr[0]["security_id"] == "500001"


def test_profits_are_reinvested_in_the_same_session(eng, tmp_path):
    from orders import session_engine as E
    _pool("S1")
    _round_trip(eng, 100.0, 103.0)
    cash1 = _do(E.stats, "S1")["cash_available"]
    assert cash1 > 25000
    _entry(symbol="BETA", trigger=50.0)
    _tick(eng, BETA=50.0)
    st = _do(E.stats, "S1")
    assert st["invested_value"] == pytest.approx(int(cash1 // 50.0) * 50.0, abs=0.01)   # the whole grown cash buys
    assert st["invested_value"] > 25000


def test_every_order_log_row_carries_its_session(eng):
    import orders.broker as B
    _pool("S1")
    _round_trip(eng)
    B.place_buy_order("GAMMA", 1, confirm=True)                      # outside any session
    rows = _rows("SELECT symbol, transaction_type, session_id FROM order_log ORDER BY id")
    assert [(r["symbol"], r["transaction_type"], r["session_id"]) for r in rows] == [
        ("ACME", "BUY", "S1"), ("ACME", "SELL", "S1"), ("GAMMA", "BUY", "MANUAL")]


def test_the_order_log_column_is_backfilled_from_the_paper_orders_tags(eng):
    """An order_log from before 18 Nov 2026 gets the column, and its rows the session of their paper order's tag."""
    import orders.broker as B
    c = _conn()
    try:
        B._ensure_order_log_table(c)
        c.execute("CREATE TABLE IF NOT EXISTS paper_order (order_id TEXT PRIMARY KEY, tag TEXT)")
        c.execute("DROP TABLE order_log")
        c.execute("CREATE TABLE order_log (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT, "
                  "transaction_type TEXT, quantity INTEGER, order_type TEXT, product_type TEXT, price REAL, "
                  "estimated_value REAL, available_funds REAL, mode TEXT, status TEXT, dhan_order_id TEXT, error TEXT)")
        c.executemany("INSERT INTO paper_order (order_id, tag) VALUES (?, ?)", [("P1", "ATIP-S3"), ("P2", None)])
        c.executemany("INSERT INTO order_log (symbol, status, dhan_order_id) VALUES (?, ?, ?)",
                      [("A", "PLACED", "P1"), ("B", "PLACED", "P2"), ("C", "BLOCKED_RISK_LIMIT", None)])
        c.commit()
        B._ensure_order_log_table(c)
        got = [tuple(r) for r in c.execute("SELECT symbol, session_id FROM order_log ORDER BY id")]
    finally:
        c.close()
    assert got == [("A", "S3"), ("B", "MANUAL"), ("C", "UNTAGGED")]


# ── 3. isolation: one session can never spend another's money ───────────────────────────────────────

def test_a_session_cannot_spend_another_sessions_money(eng):
    import orders.broker as B
    from orders import sessions as S
    _pool("S1", 25000.0)
    _pool("S2", 25000.0)
    r = _entry(sid="S1")
    assert "more than its" in _do(S.gate, r, 30000.0)                # S2's Rs 25,000 and the account's spare do not help
    out = B.place_buy_order("ACME", 300, confirm=True, tag="ATIP-S1", order_type="LIMIT", price=100.0)
    assert out["status"] == "BLOCKED_INSUFFICIENT_FUNDS" and out["session"] == "S1"
    assert "session S1 has Rs.25,000.00" in out["message"]
    ok = B.place_buy_order("ACME", 200, confirm=True, tag="ATIP-S1", order_type="LIMIT", price=100.0)
    assert ok["status"] != "BLOCKED_INSUFFICIENT_FUNDS"
    log = _rows("SELECT status, session_id, available_funds FROM order_log ORDER BY id")
    assert log[0] == {"status": "BLOCKED_INSUFFICIENT_FUNDS", "session_id": "S1", "available_funds": 25000.0}
    assert _do(S.ledger, "S2")["cash"] == 25000.0


def test_no_daily_order_count_cap_inside_a_pool_session(eng):
    """max_trades (5 a day by default) still binds a session without a pool; a pool's only limit is its cash."""
    from orders import sessions as S
    _pool("S1")
    _do(S.update_session, "S1", {"max_trades": 1})
    _round_trip(eng, 100.0, 103.0)
    r = _entry(symbol="BETA", trigger=50.0)
    assert _do(S.gate, r, 1000.0) is None
    _do(S.seed)
    _do(S.set_status, "S2", "RUNNING")
    _do(S.update_session, "S2", {"max_trades": 1})
    _round_trip(eng, 100.0, 103.0, sid="S2", symbol="GAMMA")
    r2 = _entry(symbol="DELTA", trigger=10.0, sid="S2")
    assert "trades today" in (_do(S.gate, r2, 100.0) or "")


# ── 5. auto-stop and the floor ────────────────────────────────────────────────────────────────────

def test_a_session_completes_after_its_last_trade_and_its_ledger_freezes(eng):
    from orders import sessions as S, session_engine as E
    _pool("S1", max_occurrences=2)
    _do(S.update_session, "S1", {"auto_pick": True})
    _round_trip(eng, 100.0, 103.0)
    _entry(symbol="BETA", trigger=50.0)
    _tick(eng, BETA=50.0)
    assert _do(E.check) == []                                       # the 2nd trade is open: not completed yet
    r = _entry(symbol="GAMMA", trigger=10.0)
    assert "all 2 of its occurrences" in _do(S.gate, r, 100.0)       # but no new BUY
    _tick(eng, BETA=51.5)
    assert _do(E.check) == [{"session": "S1", "to": "COMPLETED"}]
    s = _do(S.get_session, "S1")
    assert s["status"] == "COMPLETED" and not s["auto_pick"] and _ist_close(s["completed_at_ist"])
    final = json.loads(s["final_ledger"])
    assert (final["completed_trades"], final["legs"]) == (2, 4)
    before = _do(E.ledger_rows, "S1")
    assert _do(E.sync)["S1"] == "frozen" and _do(E.ledger_rows, "S1") == before
    assert "COMPLETED" in _do(S.gate, r, 100.0)
    assert _rows("SELECT title FROM alert_log WHERE title LIKE '%S1 completed%'")
    assert _do(E.alerts) == [{"session": "S1", "state": "COMPLETED", "tone": "info", "text": "S1 completed 2 trades"}]


def test_a_session_under_its_floor_is_paused_alerted_and_never_refilled(eng):
    from orders import sessions as S, session_engine as E
    _pool("S1")
    _pool("S2")
    _entry()
    _tick(eng, ACME=100.0)
    _tick(eng, ACME=70.0)                                            # a gap through the 2 % stop: sold at 70
    st = _do(E.stats, "S1")
    assert st["current_balance"] < 20000 and st["completed_trades"] == 1 and st["losses"] == 1
    assert _do(E.check) == [{"session": "S1", "to": "PAUSED", "reason": "BELOW_FLOOR"}]
    s = _do(S.get_session, "S1")
    assert s["status"] == "PAUSED" and _do(E.stats, "S1")["state"] == "PAUSED – BELOW FLOOR"
    assert _rows("SELECT severity FROM alert_log WHERE title LIKE '%S1 paused: below floor%'")[0]["severity"] == "warning"
    from accounting.cycles import transfers_for
    assert _do(transfers_for, "S1") == [] and _do(transfers_for, "S2") == []   # nothing moved in
    assert _do(S.ledger, "S2")["cash"] == 25000.0
    with pytest.raises(ValueError, match="under its floor"):
        _do(E.activate, "S1", True)
    assert _do(E.check) == []                                       # paused once, not again every pass
    assert _do(E.alerts)[0]["text"] == "S1 paused: below floor"


def test_the_shell_state_names_paused_and_completed_sessions_for_the_apps(eng):
    from dashboard import shell
    from orders import session_engine as E
    _pool("S1")
    _entry()
    _tick(eng, ACME=100.0)
    _tick(eng, ACME=70.0)
    _do(E.check)
    shell._cache.update(at=0, data=None)
    st = _do(shell.state)
    assert st["session_alert"] == {"tone": "warn", "text": "S1 paused: below floor"}


# ── 10. the simulation: 10 sessions x 500 trades ─────────────────────────────────────────────────────

def _simulate(conn, sessions: int, trades: int, seed: int = 18112026) -> dict:
    """Write what the order engine writes for `trades` round trips in each of `sessions` pools, with the real NSE
    delivery charges per leg, and return each session's expected cash walked by hand (seed + every sale - every buy -
    every charge), independent of the engine."""
    from backtest.costs import cost_model
    from orders import rules, sessions as S
    rules.init_orders_table()
    conn.execute("CREATE TABLE IF NOT EXISTS paper_order (order_id TEXT PRIMARY KEY, created_at TEXT, symbol TEXT, "
                 "security_id TEXT, exchange TEXT, transaction_type TEXT, quantity INTEGER, filled_qty INTEGER DEFAULT 0, "
                 "order_type TEXT, product_type TEXT, limit_price REAL, fill_price REAL, status TEXT, reason TEXT, "
                 "brokerage REAL DEFAULT 0, tag TEXT)")
    model = cost_model("nse_delivery")
    rnd = random.Random(seed)
    S.seed(conn)
    expected = {}
    t0 = datetime(2026, 1, 1, 9, 30)
    for k in range(1, sessions + 1):
        sid = f"S{k}"
        S.start_pool(conn, sid, 25000.0, max_occurrences=trades)
        conn.execute("UPDATE trading_sessions SET status = 'RUNNING', pool_started_at = ? WHERE id = ?",
                     ("2025-12-31T09:00:00", sid))
        cash, rows, links, orders = 25000.0, [], [], []
        px = 100.0 + k
        for n in range(trades):
            at = t0 + timedelta(minutes=10 * n)
            px = round(max(5.0, px * (1 + rnd.uniform(-0.03, 0.03))), 2)
            q = int(cash // (px * 1.002))
            while q > 0 and q * px + model.total("BUY", q * px) > cash:
                q -= 1
            assert q > 0, f"{sid} ran out of money at trade {n}"
            sell = round(px * (1 + rnd.choice([0.03, 0.035, -0.02, -0.023, 0.01, -0.005])), 2)
            fee_b = round(model.total("BUY", q * px), 2)
            fee_s = round(model.total("SELL", q * sell), 2)
            cash = cash - q * px - fee_b + q * sell - fee_s
            e_id, x_id = f"{sid}-E{n:04d}", f"{sid}-X{n:04d}"
            common = ("PRICE", 0.0, px, px, "SESSION", 100.0, "CNC", "LIMIT", 0, "EXECUTED")
            rows.append((e_id, f"SYM{k}", "BUY", *common, "ENTRY", None, at.isoformat(), at.isoformat(), f"PB{e_id}", px, q))
            rows.append((x_id, f"SYM{k}", "SELL", *common, "TARGET" if sell > px else "STOP", e_id,
                         at.isoformat(), (at + timedelta(minutes=5)).isoformat(), f"PS{x_id}", sell, q))
            links.append((e_id, sid, at.isoformat()))
            orders += [(f"PB{e_id}", at.isoformat(), f"SYM{k}", f"9{k:04d}", "BUY", q, q, px, "FILLED", fee_b, f"ATIP-{sid}"),
                       (f"PS{x_id}", (at + timedelta(minutes=5)).isoformat(), f"SYM{k}", f"9{k:04d}", "SELL", q, q, sell,
                        "FILLED", fee_s, f"ATIP-{sid}")]
        conn.executemany(
            "INSERT INTO order_rules (id, symbol, side, trigger_type, trigger_value, reference_price, "
            "resolved_trigger_price, quantity_type, quantity_value, product_type, order_type, require_confirmation, "
            "status, role, parent_rule_id, created_at, updated_at, dhan_order_id, execution_price, execution_quantity) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO session_rules (rule_id, session_id, linked_at) VALUES (?,?,?)", links)
        conn.executemany("INSERT INTO paper_order (order_id, created_at, symbol, security_id, transaction_type, quantity, "
                         "filled_qty, fill_price, status, brokerage, tag) VALUES (?,?,?,?,?,?,?,?,?,?,?)", orders)
        expected[sid] = cash
    conn.commit()
    return expected


def test_ten_sessions_through_500_trades_balance_to_the_paisa(temp_db, tmp_path, monkeypatch):
    import orders.paper as P
    from db.schema import init_db
    from orders import capital_plan, sessions as S, session_engine as E
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"broker_env": "PAPER", "paper_opening_balance": 250000.0}))
    import orders.environment as Env
    for mod in (Env, P, E, capital_plan):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    init_db()
    c = _conn()
    try:
        expected = _simulate(c, 10, 500)
        assert E.check(c) == [{"session": f"S{k}", "to": "COMPLETED"} for k in range(1, 11)]
        for k in range(1, 11):
            sid = f"S{k}"
            s = S.get_session(c, sid)
            final = json.loads(s["final_ledger"])
            led = E.ledger_rows(c, sid)
            trs = E.trades(c, sid)
            want = round(expected[sid], 2)
            # the engine's cash, the frozen final ledger and the cash book all equal the hand walk, to the paisa
            assert final["cash_available"] == want and final["current_balance"] == want, sid
            assert led[-1]["balance_after"] == want, sid
            assert round(sum(r["amount"] for r in led if r["type"] != "PNL"), 2) == want, sid
            # the PNL memo lines and the trade rows both add up to the session's growth
            assert round(sum(r["amount"] for r in led if r["type"] == "PNL"), 2) == pytest.approx(want - 25000, abs=0.01)
            assert round(sum(t["net_pnl"] for t in trs), 2) == pytest.approx(want - 25000, abs=0.01)
            assert (final["completed_trades"], final["legs"], final["wins"] + final["losses"]) == (500, 1000, 500)
            assert len(trs) == 500 and {t["session_id"] for t in trs} == {sid}
            assert all(r["entry_id"].startswith(f"{sid}:") for r in led)            # no other session's line
            # charges: every leg's paper brokerage is in the book, once
            fees = c.execute("SELECT ROUND(SUM(brokerage), 2) FROM paper_order WHERE tag = ?", (f"ATIP-{sid}",)).fetchone()[0]
            assert round(-sum(r["amount"] for r in led if r["type"] == "CHARGES"), 2) == pytest.approx(fees, abs=0.01)
            assert s["status"] == "COMPLETED"
        assert len({round(v, 2) for v in expected.values()}) == 10                     # ten different outcomes
    finally:
        c.close()


# ── 8. the Sessions tab, its drill-down, the API and the CSV exports ─────────────────────────────────

def test_the_sessions_page_shows_the_engine_summary_cards_and_drill_down(eng):
    from dashboard import ux_pages as P
    _pool("S1")
    _round_trip(eng, 100.0, 103.0)
    html = _do(P.sessions)
    for want in ("₹25K session engine", "Combined balance", "Combined P&amp;L", "Reserve", "1 of 500 trades",
                 'data-state="ACTIVE"', "Win rate 100.0%"):
        assert want in html, want
    d = _do(P.session_detail, "S1", "trades")
    for want in ("Trades &amp; ledger", "Ledger history", "/api/v2/engine/S1/trades.csv", "/api/v2/engine/S1/ledger.csv",
                 "Completed trades", "1 / 500", "ACME"):
        assert want in d, want


def test_the_engine_api_and_the_csv_exports(eng, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from dashboard import engine_routes
    _cfg(tmp_path, paper_opening_balance=150000.0)
    _pool("S1")
    _round_trip(eng, 100.0, 103.0)
    app = FastAPI()
    engine_routes.register(app, [], Request, _conn, lambda x: json.loads(json.dumps(x, default=str)))
    with TestClient(app) as client:
        ov = client.get("/api/v2/engine").json()
        assert ov["summary"]["sessions"] == 1 and ov["sessions"][0]["completed_trades"] == 1
        p = client.get("/api/v2/engine/plan").json()
        assert p["new_sessions"] == 5                                # Rs 1,25,000 outside S1's Rs 25,000
        assert client.post("/api/v2/engine/create", json={"n": 2}).status_code == 400         # no ack
        made = client.post("/api/v2/engine/create", json={"n": 2, "ack": True}).json()
        assert made["created"] == ["S11", "S12"] and made["state"] == "CREATED"
        assert client.post("/api/v2/engine/S11/activate", json={"ack": True}).json()["state"] == "ACTIVE"
        one = client.get("/api/v2/engine/S1").json()
        assert [r["type"] for r in one["ledger"]][:2] == ["SEED", "BUY"] and len(one["trades"]) == 1
        csv_t = client.get("/api/v2/engine/S1/trades.csv")
        assert csv_t.headers["content-type"].startswith("text/csv")
        assert 'filename="S1_trades_IST.csv"' in csv_t.headers["content-disposition"]
        lines = csv_t.text.strip().splitlines()
        assert lines[0].startswith("trade_id,session_id,symbol") and len(lines) == 2 and ",CLOSED," in lines[1]
        led = client.get("/api/v2/engine/S1/ledger.csv").text.strip().splitlines()
        assert led[0] == "seq,entry_id,session_id,type,amount,balance_after,memo,ts_ist" and len(led) == 5   # no charges here
        assert client.get("/api/v2/engine/S5").status_code == 400                         # no seed: no wallet


def test_a_whole_cash_limit_buy_above_its_quote_is_not_refused(eng):
    """An auto-pick buy is sized from the quote that fired it and sent as a LIMIT up to 0.5 % above (KGF-02); the paper
    broker fills it at that quote. Valuing it at the limit would refuse a session that buys with all of its cash (S5,
    and every new Rs 25K session, which copies S5)."""
    import orders.broker as B
    from orders import session_engine as E
    _pool("S1", 25000.0)
    eng.prices["ACME"] = 100.0
    out = B.place_buy_order("ACME", 250, order_type="LIMIT", price=100.5, confirm=True, tag="ATIP-S1",
                            reference_price=100.0)
    assert out["status"] == "PLACED", out
    assert _rows("SELECT fill_price FROM paper_order")[0]["fill_price"] == 100.0
    assert _rows("SELECT session_id FROM order_log")[0]["session_id"] == "S1"
    # one rupee more than the session has is still refused
    _pool("S2", 25000.0)
    out = B.place_buy_order("ACME", 251, order_type="LIMIT", price=100.5, confirm=True, tag="ATIP-S2",
                            reference_price=100.0)
    assert out["status"] == "BLOCKED_INSUFFICIENT_FUNDS" and out["session"] == "S2"


def test_a_session_entry_with_a_limit_above_the_trigger_fills_on_the_real_path(eng):
    from orders import rules, sessions as S, session_engine as E
    _pool("S1", 25000.0)
    r = _rule(symbol="ACME", trigger_value=100.0, require_confirmation=False, order_type="LIMIT", limit_price=100.5)
    _do(S.link_rule, "S1", r["id"])
    _tick(eng, ACME=100.0)
    st = _do(E.stats, "S1")
    assert st["open_trades"] == 1 and st["invested_value"] == pytest.approx(25000.0, abs=100.5)
    assert not [x for x in _rows("SELECT status FROM order_log") if x["status"].startswith("BLOCKED")]


def test_a_resumed_session_loses_its_below_floor_reason(eng):
    """The owner resumes a floor-paused session by hand (after moving money in, say): a kill-switch pause later must not
    be shown as PAUSED – BELOW FLOOR."""
    from orders import sessions as S, session_engine as E
    _pool("S1")
    _entry()
    _tick(eng, ACME=100.0)
    _tick(eng, ACME=70.0)
    _do(E.check)
    c = _conn()
    try:
        c.execute("UPDATE trading_sessions SET balance_floor_pct = 50 WHERE id = 'S1'")    # the owner lowers its floor
        c.commit()
    finally:
        c.close()
    _do(S.set_status, "S1", "RUNNING", "owner resumed")
    assert _do(E.check) == []
    _do(S.pause_all, "kill switch test")
    assert _do(E.stats, "S1")["state"] == "PAUSED"
