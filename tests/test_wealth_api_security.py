"""W18 security gate for the wealth API: guard on every mutating route, RBAC mapping,
input validation, ownership, no path into trading."""

import ast
from pathlib import Path

import pytest

from tests._wealth_seed import ANSWERS, fresh


@pytest.fixture
def api(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from orders import rules
    fresh(temp_db)
    rules.init_orders_table()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def _wealth_routes():
    from dashboard import server
    out = []
    for r in server.app.routes:
        path = getattr(r, "path", "")
        if path.startswith("/api/wealth"):
            for m in getattr(r, "methods", ()) or ():
                out.append((m, path))
    return out


def test_every_mutating_wealth_route_needs_the_token(api):
    client, _ = api
    mutating = [(m, p) for m, p in _wealth_routes() if m in ("POST", "PUT", "DELETE")]
    assert len(mutating) >= 30
    for m, p in mutating:
        url = p.replace("{", "").replace("}", "")
        assert client.request(m, url, json={}).status_code == 401, f"{m} {p} is open"


def test_every_wealth_route_maps_to_a_wealth_permission():
    from enterprise.authz import permission_for
    for m, p in _wealth_routes():
        want = "wealth:read" if m == "GET" else "wealth:write"
        assert permission_for(m, p.replace("{", "").replace("}", "")) == want, (m, p)


def test_wealth_permissions_exist_and_reach_existing_roles(temp_db):
    from db.schema import get_connection
    from enterprise import rbac
    conn = get_connection()
    # an install seeded before W11: every role exists but the wealth permissions do not
    rbac.seed(conn)
    conn.execute("DELETE FROM enterprise_role_permission WHERE permission LIKE 'wealth:%'")
    conn.execute("DELETE FROM enterprise_permission WHERE permission LIKE 'wealth:%'")
    conn.commit()
    rbac.seed(conn)
    assert "wealth:read" in rbac.role_permissions(conn, "VIEWER")
    assert {"wealth:read", "wealth:write"} <= rbac.role_permissions(conn, "TRADER")


def test_validation_errors_are_400_not_500(api):
    client, security = api
    h = {security.TOKEN_HEADER: security.token()}
    bad = [("POST", "/api/wealth/dna", {"answers": {"age": "old"}}),
           ("POST", "/api/wealth/holdings", {"asset_class": "NPS", "instrument": "OTHER", "name": "x", "quantity": 1}),
           ("POST", "/api/wealth/holdings", {"asset_class": "CASH", "instrument": "BANK_ACCOUNT", "name": "x",
                                             "quantity": -5}),
           ("POST", "/api/wealth/goals", {"name": "x", "target_amount": "lots", "target_date": "2099-01-01"}),
           ("PUT", "/api/wealth/allocation/policy", {"bounds": {"EQUITY": [90, 10]}}),
           ("POST", "/api/wealth/rebalance/plan", {"mode": "yolo"}),
           ("POST", "/api/wealth/performance/ledger", {"kind": "SHORT", "trade_date": "2025-01-01"}),
           ("POST", "/api/wealth/performance/report", {"portfolio": "BANK"}),
           ("POST", "/api/wealth/advisor/ask", {"question": "x" * 600}),
           ("PUT", "/api/wealth/mode", {"mode": "ROOT"}),
           ("PUT", "/api/wealth/classifications", {"symbol": "A');alert(1);//", "asset_class": "GOLD"}),
           ("GET", "/api/wealth/suitability/A%27%29%3Balert%281%29", None)]
    for m, p, b in bad:
        r = client.request(m, p, json=b, headers=h) if b is not None else client.get(p)
        assert r.status_code == 400, (m, p, r.status_code, r.text[:200])


def test_unknown_ids_are_404(api):
    client, _ = api
    for p in ("/api/wealth/holdings/nope", "/api/wealth/goals/nope", "/api/wealth/rebalance/plans/nope",
              "/api/wealth/allocation/runs/nope", "/api/wealth/performance/reports/nope", "/api/wealth/advisor/nope",
              "/api/wealth/dna/nope"):
        assert client.get(p).status_code == 404, p


def test_happy_path_through_the_api(api):
    client, security = api
    h = {security.TOKEN_HEADER: security.token()}
    assert client.post("/api/wealth/dna", json={"answers": ANSWERS}, headers=h).status_code == 200
    assert client.post("/api/wealth/holdings", json={"asset_class": "CASH", "instrument": "BANK_ACCOUNT",
                                                     "name": "Savings", "quantity": 500000}, headers=h).status_code == 200
    assert client.post("/api/wealth/allocation/run", headers=h).status_code == 200
    assert client.get("/api/wealth/rebalance/check").json()["verdict"] in ("REBALANCE", "REVIEW", "NO_ACTION")
    assert client.get("/api/wealth/overview").status_code == 200
    assert client.get("/wealth").status_code == 200


WEALTH_DIR = Path(__file__).resolve().parents[1] / "wealth"
FORBIDDEN = {"orders.broker", "orders.rules", "execution.oms", "execution.pipeline", "execution.adapters",
             "orders.paper"}


def test_the_wealth_package_never_imports_a_trading_path():
    """Advisory only: no module under wealth/ may import order placement or execution.
    (orders.paper is allowed only for reading tables in tests, never from wealth/.)"""
    for f in WEALTH_DIR.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for n in names:
                assert not any(n == x or n.startswith(x + ".") for x in FORBIDDEN), f"{f.name} imports {n}"


def test_wealth_sql_is_parameterised():
    """No f-string SQL built from request values: the only f-strings in execute() calls
    interpolate placeholder lists or fixed column names."""
    import re
    for f in WEALTH_DIR.rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r'execute\(\s*f"([^"]*)"', src):
            body = m.group(1)
            for expr in re.findall(r"\{([^}]*)\}", body):
                assert expr in ("ph", "col", "','.join('?' * len(held)) or 'NULL'") or expr.startswith("','.join"), \
                    f"{f.name}: f-string SQL interpolates {expr!r}"
