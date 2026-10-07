"""W39 Zerodha / Dhan parity: basket orders (EX-18), stock SIP (EX-20), progressive disclosure (UX-01)."""

from datetime import date, timedelta

import pytest


def _db():
    from db.schema import get_connection, init_db
    init_db()
    return get_connection()


@pytest.fixture
def fake_broker(monkeypatch):
    """orders.broker._place_order / available_balance replaced: records calls, never reaches a broker."""
    from orders import broker as B
    calls, state = [], {"cash": 100000.0, "refuse": set(), "fail_on_place": set()}

    def place(symbol, ttype, qty, order_type="MARKET", product_type="CNC", price=0, confirm=False, tag=None,
              reference_price=None):
        calls.append((symbol, ttype, qty, confirm, tag))
        est = (price or 100.0) * qty
        if symbol in state["refuse"]:
            return {"status": "BLOCKED_RISK_LIMIT", "message": "symbol exposure limit"}
        if not confirm:
            return {"status": "DRY_RUN_OK", "estimated_value": est, "halted": False, "risk": {"ok": True}}
        if symbol in state["fail_on_place"]:
            return {"status": "FAILED", "error": "broker said no"}
        return {"status": "PLACED", "order_id": f"P-{len(calls)}"}
    monkeypatch.setattr(B, "_place_order", place)
    monkeypatch.setattr(B, "available_balance", lambda: state["cash"])
    return calls, state


LEGS = [{"symbol": "ACME", "side": "BUY", "quantity": 10}, {"symbol": "OLD", "side": "SELL", "quantity": 5},
        {"symbol": "XYZ", "side": "BUY", "quantity": 2, "order_type": "LIMIT", "price": 250}]


# ── EX-18 baskets ──────────────────────────────────────────────────────

def test_basket_validation(temp_db):
    from orders import basket as BK
    conn = _db()
    for bad, msg in (({"name": "x", "legs": []}, "non-empty"), ({"name": "", "legs": LEGS}, "name"),
                     ({"name": "x", "legs": [{"symbol": "a b", "side": "BUY", "quantity": 1}]}, "symbol"),
                     ({"name": "x", "legs": [{"symbol": "A", "side": "HOLD", "quantity": 1}]}, "side"),
                     ({"name": "x", "legs": [{"symbol": "A", "side": "BUY", "quantity": 0}]}, "quantity"),
                     ({"name": "x", "legs": [{"symbol": "A", "side": "BUY", "quantity": 1, "order_type": "LIMIT"}]},
                      "price"),
                     ({"name": "x", "legs": [{"symbol": "A", "side": "BUY", "quantity": 1}] * 21}, "at most")):
        with pytest.raises(ValueError, match=msg):
            BK.save(conn, bad)


def test_preview_dry_runs_every_leg_sells_first_and_checks_total_funds(temp_db, fake_broker):
    from orders import basket as BK
    calls, state = fake_broker
    conn = _db()
    b = BK.save(conn, {"name": "rebalance", "legs": LEGS})
    pv = BK.preview(conn, b["basket_id"])
    assert [c[0] for c in calls] == ["OLD", "ACME", "XYZ"] and not any(c[3] for c in calls)   # dry runs only
    assert pv["buy_value"] == pytest.approx(1000 + 500) and pv["sell_value"] == pytest.approx(500)
    assert pv["net_needed"] == pytest.approx(1000) and pv["ready"]
    state["cash"] = 900
    assert not BK.preview(conn, b["basket_id"])["ready"]


def test_execute_is_all_or_nothing_and_records_runs(temp_db, fake_broker):
    from orders import basket as BK
    calls, state = fake_broker
    conn = _db()
    bid = BK.save(conn, {"name": "b", "legs": LEGS})["basket_id"]
    assert BK.execute(conn, bid)["status"] == "PREVIEW" and not any(c[3] for c in calls)
    state["refuse"].add("XYZ")
    r = BK.execute(conn, bid, confirm=True)
    assert r["status"] == "BLOCKED" and r["placed"] == [] and not any(c[3] for c in calls)
    state["refuse"].clear()
    calls.clear()
    r = BK.execute(conn, bid, confirm=True)
    assert r["status"] == "PLACED" and [x["symbol"] for x in r["placed"]] == ["OLD", "ACME", "XYZ"]
    assert all(c[4] == f"basket:{bid}" for c in calls)
    state["fail_on_place"].add("XYZ")
    assert BK.execute(conn, bid, confirm=True)["status"] == "PARTIAL"
    assert [x["status"] for x in BK.get(conn, bid)["runs"][:4]] == ["PARTIAL", "PLACED", "BLOCKED", "PREVIEW"]


# ── EX-20 SIP ──────────────────────────────────────────────────────────

def test_next_due_rules():
    from orders.sip import next_due
    assert next_due("MONTHLY", 5, date(2026, 10, 3)) == date(2026, 10, 5)
    assert next_due("MONTHLY", 5, date(2026, 10, 6)) == date(2026, 11, 5)
    assert next_due("MONTHLY", 28, date(2026, 12, 29)) == date(2027, 1, 28)
    assert next_due("WEEKLY", 0, date(2026, 10, 7)) == date(2026, 10, 12)        # Wed -> next Mon
    assert next_due("WEEKLY", 2, date(2026, 10, 7)) == date(2026, 10, 7)


def test_sip_validation(temp_db):
    from orders import sip as SIP
    conn = _db()
    for bad, msg in (({"symbol": "ACME"}, "exactly one"), ({"symbol": "ACME", "amount": 5000, "quantity": 2}, "exactly one"),
                     ({"symbol": "ACME", "amount": 10}, "amount"), ({"symbol": "ACME", "quantity": 1, "day": 31}, "1-28"),
                     ({"symbol": "ACME", "quantity": 1, "frequency": "WEEKLY", "day": 6}, "0 \\(Mon\\)"),
                     ({"symbol": "ACME", "quantity": 1, "frequency": "DAILY"}, "frequency")):
        with pytest.raises(ValueError, match=msg):
            SIP.create(conn, bad)


def test_sip_runs_once_per_due_date_by_amount_in_paper(temp_db, monkeypatch):
    from orders import sip as SIP
    conn = _db()
    d0 = date.today() - timedelta(days=1)
    while d0.weekday() > 4:                     # the last weekday before today: next week's is still ahead
        d0 -= timedelta(days=1)
    conn.execute("INSERT INTO prices_daily (symbol,date,close) VALUES ('ACME',?,240)", (str(d0),))
    conn.commit()
    p = SIP.create(conn, {"symbol": "ACME", "amount": 5000, "frequency": "WEEKLY", "day": d0.weekday(),
                          "start_date": str(d0)})
    conn.execute("UPDATE sip_plan SET next_due=? WHERE plan_id=?", (str(d0), p["plan_id"]))
    conn.commit()
    placed = []
    monkeypatch.setattr("orders.environment.broker_env", lambda: "PAPER")
    r = SIP.run_due(place=lambda s, q, tag: placed.append((s, q, tag)) or {"status": "PLACED", "order_id": "X1"})
    assert r["status"] == "SUCCESS" and placed == [("ACME", 20, f"sip:{p['plan_id']}:{d0}")]     # floor(5000/240)
    assert SIP.run_due(place=lambda *a: pytest.fail("ran twice"))["status"] in ("NO_NEW", "SUCCESS")
    plan = SIP.get(conn, p["plan_id"])
    assert date.fromisoformat(str(plan["next_due"])[:10]) > d0 and plan["executions"][0]["status"] == "PLACED"


def test_sip_never_sends_outside_paper_and_retries_then_misses(temp_db, monkeypatch):
    from orders import sip as SIP
    conn = _db()
    conn.execute("INSERT INTO prices_daily (symbol,date,close) VALUES ('ACME',?,100)", (str(date.today()),))
    conn.commit()
    p = SIP.create(conn, {"symbol": "ACME", "quantity": 3, "frequency": "MONTHLY", "day": 1, "start_date": "2026-01-01"})
    monkeypatch.setattr("orders.environment.broker_env", lambda: "LIVE")
    r = SIP.run_due(place=lambda *a: pytest.fail("a LIVE SIP must not be sent"))
    assert r["results"][0]["status"] == "SKIPPED_NOT_PAPER"
    p2 = SIP.create(conn, {"symbol": "ACME", "quantity": 3, "frequency": "MONTHLY", "day": 1, "start_date": "2026-01-01"})
    SIP.set_status(conn, p["plan_id"], "ENDED")
    monkeypatch.setattr("orders.environment.broker_env", lambda: "PAPER")
    fail = lambda s, q, tag: {"status": "FAILED", "error": "no quote"}                       # noqa: E731
    assert [SIP.run_due(place=fail)["results"][0]["status"] for _ in range(3)] == ["FAILED", "FAILED", "MISSED"]
    assert SIP.get(conn, p2["plan_id"])["executions"][0]["attempts"] == 3
    with pytest.raises(ValueError, match="cannot be restarted"):
        SIP.set_status(conn, p["plan_id"], "ACTIVE")


def test_routes_for_baskets_and_sip(tmp_path, monkeypatch, temp_db, fake_broker):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    _db().close()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    assert c.post("/api/orders/baskets", json={"name": "x", "legs": LEGS}).status_code == 401
    bid = c.post("/api/orders/baskets", json={"name": "x", "legs": LEGS}, headers=h).json()["basket_id"]
    assert c.get("/api/orders/baskets").json()[0]["basket_id"] == bid
    assert c.post(f"/api/orders/baskets/{bid}/preview", headers=h).json()["ready"] is True
    assert c.post(f"/api/orders/baskets/{bid}/execute", headers=h, json={}).json()["status"] == "PREVIEW"
    assert c.get("/api/orders/baskets/nope").status_code == 404
    pid = c.post("/api/orders/sip", headers=h, json={"symbol": "ACME", "quantity": 1}).json()["plan_id"]
    assert c.post(f"/api/orders/sip/{pid}/status", headers=h, json={"status": "PAUSED"}).json()["status"] == "PAUSED"
    assert c.get("/baskets").status_code == 200
    from enterprise.authz import permission_for
    assert permission_for("POST", f"/api/orders/baskets/{bid}/execute") == "orders:manage"
    assert permission_for("GET", "/api/orders/sip") == "execution:read"


# ── UX-01 progressive disclosure ──────────────────────────────────────

def test_wealth_page_has_a_simple_view_that_hides_detail():
    from dashboard import wealth_page
    h = wealth_page.render("tok")
    assert "setView('simple')" in h and "body.simple .adv{display:none" in h
    assert h.count('class="adv"') >= 3 and "setView(_v)" in h
