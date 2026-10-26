"""
Enterprise mode: the owner's broker account and paper book are not another tenant's to read or act on.

The routes in enterprise/authz.py OWNER_BOOK read or act on tables that have no tenant column (order rules,
baskets, SIPs, the paper book, the mobile summary) or on the owner's global risk limits and risk decisions. A
permission that a role holds in another tenant does not make those routes that tenant's. Enterprise mode is off
for the single owner, so these tests switch it on, as a second tenant would meet the app.

The install's dashboard token (X-ATIP-Token) is the owner's. Another tenant is served a token of its own, tied to
its session, and the write guard takes that for its session only: the install's token does not pass for it.
"""

import json
import re

import pytest

PASSWORD = "Correct-horse-9battery"
CANARY = ("ZZCANARY", "987654.321")         # seeded into every owner table: any of these in a response is a leak
REFUSED = "the paper book and broker account belong to the owner's tenant"
SKIP_GET = re.compile(r"(stream|ws|feed|backfill|export|download|run|start|stop)")   # long-running or side-effecting


@pytest.fixture
def app(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import security, server
    from db.schema import get_connection, init_db
    from enterprise import config as ecfg
    from enterprise import service, tenants, users
    from orders import basket, paper, sip
    from orders.rules import init_orders_table
    from strategy import positions

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enterprise": {"enabled": True}}), encoding="utf-8")
    for mod in (ecfg, security):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    init_db()
    conn = get_connection()
    service.ensure_seeded(conn)

    # the owner's book, in every table an owner route reads
    init_orders_table()
    now = "2026-10-26T10:00:00"
    conn.execute("INSERT INTO order_rules (id,symbol,side,trigger_type,trigger_value,reference_price,"
                 "resolved_trigger_price,quantity_type,quantity_value,created_at,updated_at) "
                 "VALUES ('rule_owner',?,'BUY','PRICE',100,100,100,'SHARES',5,?,?)", (CANARY[0], now, now))
    conn.execute("INSERT INTO portfolio_holdings (date,symbol,qty,avg_price,cmp,current_val) "
                 "VALUES ('2026-10-23',?,10,100,101,1010)", (CANARY[0],))
    conn.execute("INSERT INTO pnl_daily (date,env,equity,day_pnl,drawdown_pct) VALUES ('2026-10-23','PAPER',?,1,0)",
                 (float(CANARY[1]),))
    paper.ensure_tables(conn)
    conn.execute("INSERT INTO paper_account (id,balance,opened_at) VALUES (1,?,?)", (float(CANARY[1]), now))
    conn.execute("INSERT INTO paper_position (symbol,quantity,avg_price) VALUES (?,3,100)", (CANARY[0],))
    positions.open_position(CANARY[0], 100.0, 10, conn=conn)
    conn.commit()
    basket_id = basket.save(conn, {"name": "ZZCANARY basket",
                                   "legs": [{"symbol": CANARY[0], "side": "BUY", "quantity": 1}]})["basket_id"]
    plan_id = sip.create(conn, {"symbol": CANARY[0], "quantity": 1, "frequency": "MONTHLY",
                                "day": 5})["plan_id"]

    # another tenant with a viewer, a trader and a risk manager; and a viewer of the owner's tenant
    tenants.create(conn, "acme_fund", "Acme Fund")
    users.create_user(conn, "acme.viewer", PASSWORD, "acme_fund", ["VIEWER"])
    users.create_user(conn, "acme.trader", PASSWORD, "acme_fund", ["TRADER"])
    users.create_user(conn, "acme.risk", PASSWORD, "acme_fund", ["RISK_MANAGER"])
    users.create_user(conn, "acme.research", PASSWORD, "acme_fund", ["RESEARCHER"])
    users.create_user(conn, "owner.viewer", PASSWORD, "default", ["VIEWER"])
    users.create_user(conn, "owner.admin", PASSWORD, "default", ["SUPER_ADMIN"])
    conn.commit()

    def bearer(username):
        return {"Authorization": f"Bearer {users.login(conn, username, PASSWORD)['token']}"}

    acme_secret = users.login(conn, "acme.viewer", PASSWORD)["token"]
    research_secret = users.login(conn, "acme.research", PASSWORD)["token"]

    owner_paths = ["/api/orders", "/api/orders/pending", "/api/orders/broker-status", "/api/orders/baskets",
                   f"/api/orders/baskets/{basket_id}", "/api/orders/sip", f"/api/orders/sip/{plan_id}",
                   "/api/oms/positions", "/api/risk/limits", "/api/risk/portfolio", "/api/risk/emergency-exit",
                   "/api/mobile/summary"]
    yield {"client": TestClient(server.app, raise_server_exceptions=False), "app": server.app, "conn": conn,
           "acme": {"Authorization": f"Bearer {acme_secret}"}, "acme_trader": bearer("acme.trader"),
           "acme_risk": bearer("acme.risk"), "acme_research": {"Authorization": f"Bearer {research_secret}"},
           "owner": bearer("owner.viewer"), "owner_admin": bearer("owner.admin"), "anonymous": {},
           "token": security.token(), "acme_token": security.session_token(acme_secret),
           "research_token": security.session_token(research_secret), "owner_paths": owner_paths}
    conn.close()


def test_another_tenant_is_refused_every_owner_book_read(app):
    for path in app["owner_paths"]:
        r = app["client"].get(path, headers=app["acme"])
        assert r.status_code == 403 and r.json()["error"] == REFUSED, (path, r.status_code, r.text[:200])


def test_another_tenant_cannot_act_on_the_owner_book(app):
    cases = [("POST", "/api/orders", {"symbol": "ACME"}, app["acme_trader"]),
             ("PUT", "/api/risk/limits", {}, app["acme_risk"]),
             ("POST", "/api/risk/emergency-exit", {}, app["acme_risk"]),
             ("POST", "/api/risk/evaluate", {}, app["acme_risk"])]
    for method, path, body, h in cases:
        r = app["client"].request(method, path, headers=h, json=body)
        assert r.status_code == 403 and r.json()["error"] == REFUSED, (method, path, r.status_code, r.text[:200])


def test_another_tenant_is_sent_from_the_owner_dashboard_to_its_own_app(app):
    r = app["client"].get("/", headers=app["acme"], follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/app"


def test_the_owner_tenant_keeps_the_owner_book(app):
    c, h = app["client"], app["owner"]
    r = c.get("/api/orders", headers=h)
    assert r.status_code == 200 and CANARY[0] in r.text
    for path in app["owner_paths"]:
        assert c.get(path, headers=h).status_code not in (401, 403), path
    assert c.get("/", headers=h, follow_redirects=False).status_code == 200


def test_another_tenant_keeps_its_own_book(app):
    r = app["client"].get("/api/tenant/book", headers=app["acme"])
    assert r.status_code == 200 and r.json()["tenant_id"] == "acme_fund"


def test_each_refusal_is_audited(app):
    app["client"].get("/api/orders", headers=app["acme"])
    n = app["conn"].execute("SELECT COUNT(*) FROM enterprise_audit WHERE action='authz.denied' AND path='/api/orders' "
                            "AND tenant_id='acme_fund'").fetchone()[0]
    assert n == 1


def _static_get_paths(app):
    """Every GET route without a path parameter, but the long-running or side-effecting ones."""
    for route in app.routes:
        methods, path = getattr(route, "methods", None) or set(), getattr(route, "path", "")
        if "GET" in methods and "{" not in path and not SKIP_GET.search(path):
            yield path


def test_no_get_route_shows_the_owner_book_to_another_tenant(app):
    leaks, checked = [], 0
    for path in _static_get_paths(app["app"]):
        r = app["client"].get(path, headers=app["acme"])
        checked += 1
        if r.status_code < 300 and any(c in r.text for c in CANARY):
            leaks.append((path, r.status_code))
    assert checked > 100, checked
    assert leaks == [], f"another tenant reads the owner's book on {len(leaks)} routes: {leaks}"


@pytest.mark.parametrize("who", ["acme", "anonymous"])
def test_no_get_route_serves_the_install_token_to_another_tenant_or_a_stranger(app, who):
    leaks, checked = [], 0
    for path in _static_get_paths(app["app"]):
        r = app["client"].get(path, headers=app[who])
        checked += 1
        if r.status_code < 300 and app["token"] in r.text:
            leaks.append((path, r.status_code))
    assert checked > 100, checked
    assert leaks == [], f"{who} is served the install token on {len(leaks)} routes: {leaks}"


def test_the_owner_pages_still_carry_the_install_token(app):
    for path in ["/", "/research", "/backtests"]:
        r = app["client"].get(path, headers=app["owner"], follow_redirects=False)
        assert r.status_code == 200 and app["token"] in r.text, (path, r.status_code)


def test_another_tenant_pages_carry_its_own_session_token_and_not_the_install_token(app):
    r = app["client"].get("/backtests", headers=app["acme"])
    assert r.status_code == 200 and app["acme_token"] in r.text and app["token"] not in r.text, r.status_code


def test_the_write_guard_takes_a_tenants_own_session_token_and_nothing_else(app):
    # A researcher may run backtests, so the middleware lets this through; the guard still needs the session's own
    # token. The run does not exist, so an accepted request reaches a handler that starts nothing.
    path = "/api/backtests/no-such-run/pbo"
    own = {**app["acme_research"], "X-ATIP-Token": app["research_token"]}
    assert app["client"].post(path, headers=own).status_code == 404
    assert app["client"].post(path, headers=app["acme_research"]).status_code == 401
    assert app["client"].post(path, headers={**app["acme_research"], "X-ATIP-Token": app["token"]}).status_code == 401
    assert app["client"].post(path, headers={**app["owner_admin"], "X-ATIP-Token": app["token"]}).status_code == 404
