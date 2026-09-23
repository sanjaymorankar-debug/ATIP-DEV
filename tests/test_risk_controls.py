"""
The kill switch and the pre-trade limits.

Before these, every order path (orders/rules.py's confirm flow,
strategy/live.py, the broker CLI) reached orders.broker._place_order and its
only check was "are there funds" -- nothing could stop trading, and no limit
bounded an order's size, a day's order count, or exposure to one symbol.
"""

import datetime as dt
import json

import pytest


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    """Isolate the halt flag and config.json from the real atip_data."""
    from orders import risk
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", cfg)

    def write(**keys):
        cfg.write_text(json.dumps(keys), encoding="utf-8")
    return write


# ── kill switch ───────────────────────────────────────────────────────────

def test_nothing_is_halted_by_default(cfgfile):
    from orders.risk import halted
    assert halted() == (False, "")


def test_the_flag_file_halts_and_carries_the_reason(cfgfile):
    from orders.risk import halt, halted, resume
    halt("stop: broker acting up")
    assert halted() == (True, "stop: broker acting up")
    assert resume() is True
    assert halted()[0] is False
    assert resume() is False, "resuming twice is not an error"


def test_config_can_halt_too(cfgfile):
    from orders.risk import halted
    cfgfile(trading_halted=True)
    assert halted()[0] is True
    cfgfile(trading_halted=False)
    assert halted()[0] is False


def test_a_broken_config_does_not_hide_a_flag_file_halt(cfgfile, monkeypatch):
    from orders import risk
    risk.CONFIG_PATH.write_text("{ this is not json", encoding="utf-8")
    risk.halt("halted anyway")
    assert risk.halted() == (True, "halted anyway")


# ── limits read from config ───────────────────────────────────────────────

def test_no_limits_configured_means_nothing_is_enforced(cfgfile):
    from orders.risk import limits
    assert limits() == {}
    cfgfile(risk_limits={"max_order_value": None, "max_orders_per_day": 0})
    assert limits() == {}, "null and zero are not limits"


def test_only_sane_limits_are_kept(cfgfile):
    from orders.risk import limits
    cfgfile(risk_limits={"max_order_value": 100000, "max_open_positions": "eight",
                         "max_trades_per_hour": 3})
    assert limits() == {"max_order_value": 100000}


# ── pre-trade checks ──────────────────────────────────────────────────────

def _db(tables=True):
    """A database with the lazily-created order and paper tables in place."""
    from db.schema import init_db, get_connection
    init_db()
    conn = get_connection()
    if tables:
        from orders.broker import _ensure_order_log_table
        from orders.paper import ensure_tables
        _ensure_order_log_table(conn)
        ensure_tables(conn)
        conn.commit()
    return conn


def _placed(conn, n, day=None, status="PLACED"):
    for i in range(n):
        conn.execute("INSERT INTO order_log (timestamp, symbol, transaction_type, quantity, "
                     "status) VALUES (?,?,?,?,?)",
                     (f"{day or dt.date.today()} 10:0{i}:00", f"S{i}", "BUY", 1, status))
    conn.commit()


def test_an_order_within_every_limit_passes(temp_db, cfgfile):
    from orders.risk import pretrade_check
    cfgfile(risk_limits={"max_order_value": 100000, "max_orders_per_day": 5,
                         "max_open_positions": 3, "max_symbol_exposure_value": 60000})
    conn = _db()
    try:
        r = pretrade_check(conn, "ACME", "BUY", 10, 50000.0, env="PAPER")
        assert r["ok"] is True and r["blocked_by"] is None
        assert {c["limit"] for c in r["checks"]} == {
            "max_order_value", "max_orders_per_day", "max_open_positions",
            "max_symbol_exposure_value"}, "every configured limit is reported, not just breaches"
    finally:
        conn.close()


def test_an_order_over_the_value_limit_is_blocked(temp_db, cfgfile):
    from orders.risk import pretrade_check
    cfgfile(risk_limits={"max_order_value": 50000})
    conn = _db()
    try:
        assert pretrade_check(conn, "ACME", "BUY", 10, 50000.0, env="PAPER")["ok"] is True
        r = pretrade_check(conn, "ACME", "BUY", 10, 50001.0, env="PAPER")
        assert (r["ok"], r["blocked_by"]) == (False, "max_order_value")
    finally:
        conn.close()


def test_the_day_count_counts_orders_actually_placed(temp_db, cfgfile):
    from orders.risk import pretrade_check
    cfgfile(risk_limits={"max_orders_per_day": 2})
    conn = _db()
    try:
        _placed(conn, 1)
        _placed(conn, 5, status="DRY_RUN_OK")                 # dry runs are not orders
        _placed(conn, 5, day=dt.date.today() - dt.timedelta(days=1))   # nor yesterday's
        assert pretrade_check(conn, "ACME", "BUY", 1, 10.0, env="PAPER")["ok"] is True
        _placed(conn, 1)
        r = pretrade_check(conn, "ACME", "BUY", 1, 10.0, env="PAPER")
        assert (r["ok"], r["blocked_by"]) == (False, "max_orders_per_day")
    finally:
        conn.close()


def test_position_and_exposure_limits_read_the_paper_book(temp_db, cfgfile):
    from orders.risk import pretrade_check
    cfgfile(risk_limits={"max_open_positions": 2, "max_symbol_exposure_value": 30000})
    conn = _db()
    try:
        for sym, qty, px in (("HELD1", 100, 100.0), ("HELD2", 50, 200.0)):
            conn.execute("INSERT INTO paper_position (symbol, quantity, avg_price) VALUES (?,?,?)",
                         (sym, qty, px))
        conn.commit()
        r = pretrade_check(conn, "NEW", "BUY", 1, 1000.0, env="PAPER")
        assert (r["ok"], r["blocked_by"]) == (False, "max_open_positions"), "a third name"
        # adding to a name already held is not a new position
        r = pretrade_check(conn, "HELD1", "BUY", 1, 1000.0, env="PAPER")
        assert r["ok"] is True
        # ...but its exposure still counts: 10,000 held + 25,000 > 30,000
        r = pretrade_check(conn, "HELD1", "BUY", 1, 25000.0, env="PAPER")
        assert (r["ok"], r["blocked_by"]) == (False, "max_symbol_exposure_value")
        # a SELL reduces exposure, so neither cap applies to it
        assert pretrade_check(conn, "NEW", "SELL", 1, 99999.0, env="PAPER")["ok"] is True
    finally:
        conn.close()


# ── through the real order path ───────────────────────────────────────────

@pytest.fixture
def order_path(temp_db, cfgfile, monkeypatch):
    """_place_order with the broker, quote and funds lookups stubbed."""
    from orders import broker
    sent = []

    class Stub:
        accepts_symbol = True

        def place_order(self, **kw):
            sent.append(kw)
            return {"status": "success", "data": {"orderId": "OID1"}}
    monkeypatch.setattr(broker, "get_execution_client", lambda quote_source=None: (Stub(), "PAPER"))
    monkeypatch.setattr(broker, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    monkeypatch.setattr(broker, "_estimate_order_value", lambda *a: 10000.0)
    monkeypatch.setattr(broker, "check_funds", lambda v: {"ok": True, "available": 1e9, "message": "ok"})
    monkeypatch.setattr(broker, "available_balance", lambda: 1e9)
    monkeypatch.setattr(broker, "get_dhan_client", lambda: (None, None))
    return broker, sent, cfgfile


def _status(conn):
    return [r[0] for r in conn.execute("SELECT status FROM order_log ORDER BY id")]


def test_a_confirmed_order_is_refused_while_trading_is_halted(order_path):
    from db.schema import get_connection
    from orders.risk import halt
    broker, sent, _ = order_path
    halt("market looks wrong")

    res = broker.place_buy_order("ACME", 10, confirm=True)

    assert res["status"] == "BLOCKED_HALTED" and res["reason"] == "market looks wrong"
    assert sent == [], "nothing may reach the broker while halted"
    conn = get_connection()
    try:
        assert _status(conn) == ["BLOCKED_HALTED"], "the refusal is on the record"
    finally:
        conn.close()


def test_a_dry_run_still_previews_and_reports_the_halt(order_path):
    from orders.risk import halt
    broker, sent, _ = order_path
    halt("halted")
    res = broker.place_buy_order("ACME", 10)
    assert res["status"] == "DRY_RUN_OK" and res["halted"] is True
    assert res["estimated_value"] == 10000.0
    assert sent == []


def test_a_confirmed_order_is_refused_when_a_limit_would_break(order_path):
    from db.schema import get_connection
    broker, sent, cfg = order_path
    cfg(risk_limits={"max_order_value": 5000})

    res = broker.place_buy_order("ACME", 10, confirm=True)

    assert res["status"] == "BLOCKED_RISK_LIMIT" and res["blocked_by"] == "max_order_value"
    assert sent == []
    conn = get_connection()
    try:
        assert _status(conn) == ["BLOCKED_RISK_LIMIT"]
    finally:
        conn.close()


def test_with_nothing_configured_the_order_goes_through_as_before(order_path):
    """The default install must behave exactly as it did: no halt, no limits."""
    broker, sent, _ = order_path
    res = broker.place_buy_order("ACME", 10, confirm=True)
    assert res["status"] == "PLACED" and res["order_id"] == "OID1"
    assert len(sent) == 1 and sent[0]["transaction_type"] == "BUY"


def test_limits_work_before_the_order_and_paper_tables_exist(temp_db, cfgfile):
    """A fresh install has no order_log or paper_position yet; a configured
    limit must not turn the first order into an exception."""
    from orders.risk import pretrade_check
    cfgfile(risk_limits={"max_order_value": 50000, "max_orders_per_day": 5,
                         "max_open_positions": 3, "max_symbol_exposure_value": 60000})
    conn = _db(tables=False)
    try:
        assert pretrade_check(conn, "ACME", "BUY", 10, 1000.0, env="PAPER")["ok"] is True
        assert pretrade_check(conn, "ACME", "BUY", 10, 1000.0, env="LIVE")["ok"] is True
    finally:
        conn.close()


# ── the dashboard API ─────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch, temp_db):
    """A test client plus the install's token, both isolated from atip_data."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from db.schema import init_db
    from dashboard import security, server
    from orders import rules
    init_db()
    rules.init_orders_table()      # created at import against the real database
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def test_a_mutating_route_refuses_an_unauthenticated_caller(api):
    """POST /api/orders could place a trade; it was open on 0.0.0.0."""
    client, _ = api
    r = client.post("/api/orders", json={"symbol": "ACME"})
    assert r.status_code == 401 and "X-ATIP-Token" in r.json()["detail"]


@pytest.mark.parametrize("method,path", [
    ("post", "/api/refresh"),
    ("post", "/api/orders"),
    ("post", "/api/orders/abc/confirm"),
    ("post", "/api/orders/abc/reject"),
    ("delete", "/api/orders/abc"),
])
def test_every_mutating_route_is_guarded(api, method, path):
    client, _ = api
    assert getattr(client, method)(path).status_code == 401, f"{method.upper()} {path} is open"


def test_the_token_lets_the_call_through(api):
    """With the token the request is authorised — and then fails on its own
    merits (a payload with no trigger), not on authentication."""
    client, security = api
    r = client.post("/api/orders", json={"symbol": "ACME"},
                    headers={security.TOKEN_HEADER: security.token()})
    assert r.status_code == 400, r.text
    assert client.post("/api/orders", json={"symbol": "ACME"},
                       headers={security.TOKEN_HEADER: "wrong"}).status_code == 401


def test_reading_stays_open_locally(api):
    client, _ = api
    assert client.get("/api/mh").status_code == 200
    assert client.get("/api/orders").status_code == 200


def test_the_served_page_carries_the_token_and_uses_it(api):
    client, security = api
    html = client.get("/").text
    assert f'const ATIP_TOKEN="{security.token()}"' in html
    assert "afetch('/api/orders'" in html and "X-ATIP-Token" in html
    assert "await fetch('/api/orders'," not in html, "a mutating call without the token"


def test_the_dashboard_binds_to_this_machine(api):
    client, security = api
    assert security.dashboard_host() == "127.0.0.1"
    security.CONFIG_PATH.write_text('{"dashboard_host": "0.0.0.0"}', encoding="utf-8")
    assert security.dashboard_host() == "0.0.0.0", "a deliberate override is honoured"


def test_the_token_is_issued_once_and_reused(api, tmp_path):
    client, security = api
    first = security.token()
    assert len(first) > 20 and security.token() == first
    assert (tmp_path / "dashboard_token.txt").read_text(encoding="utf-8").strip() == first
