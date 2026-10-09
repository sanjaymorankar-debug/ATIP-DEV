"""
ATIP is owner-only once hosted behind bkesari.com (dashboard/owner_gate.py).

USER, MODERATOR and ADMIN accounts, a second super-admin that is not the
owner, logged-out requests and an unreachable auth service must all get the
same 404 as a path that does not exist, on every page and API -- including
the routes that place orders -- and never anything that mentions ATIP.
"""

import re
from pathlib import Path

import pytest

from dashboard import owner_gate as og

# What the central service answers for each kind of visitor. Only the owner's
# answer carries "atip": true; for everyone else the key is simply absent.
ANSWERS = {
    "owner": {"active": True, "user": {"email": "owner@example.com"}, "roles": ["SUPER_ADMIN", "USER"], "mfa": True, "atip": True},
    "admin": {"active": True, "user": {"email": "admin@example.com"}, "roles": ["ADMIN", "USER"], "mfa": True},
    "moderator": {"active": True, "user": {"email": "mod@example.com"}, "roles": ["MODERATOR", "USER"], "moderator_of": ["LADWANI"]},
    "user": {"active": True, "user": {"email": "user@example.com"}, "roles": ["USER"], "features": ["LADWANI"]},
    "second-super": {"active": True, "user": {"email": "other@example.com"}, "roles": ["SUPER_ADMIN"], "mfa": True},
    "owner-no-mfa": {"active": True, "user": {"email": "owner@example.com"}, "roles": ["SUPER_ADMIN"], "mfa": False},
    "expired": {"active": False},
}
NON_OWNERS = ["admin", "moderator", "user", "second-super", "owner-no-mfa", "expired", None]


def fake_introspect(calls):
    def introspect(token):
        calls.append(token)
        if token == "down":
            raise OSError("connection refused")
        return ANSWERS.get(token, {"active": False})
    return introspect


# ── the decision ─────────────────────────────────────────────────────────

def test_only_an_atip_true_answer_is_the_owner():
    check = og.OwnerCheck(fake_introspect([]))
    assert check.is_owner("owner") is True
    for who in NON_OWNERS + ["down", "x" * 300]:
        assert check.is_owner(who) is False, who


def test_answers_are_cached_briefly():
    calls = []
    check = og.OwnerCheck(fake_introspect(calls), ttl=30)
    check.is_owner("owner"); check.is_owner("owner")
    assert calls == ["owner"]
    expired = og.OwnerCheck(fake_introspect(calls), ttl=0)
    expired.is_owner("user"); expired.is_owner("user")
    assert calls.count("user") == 2


def test_the_cookie_is_read_by_name():
    assert og.read_token("a=1; __Host-bk_sso=tok%2B1; bk_sso=other") == "tok+1"
    assert og.read_token("bk_sso=plain") == "plain"
    assert og.read_token("session=x") is None


def test_off_unless_configured(monkeypatch):
    from fastapi import FastAPI
    monkeypatch.delenv("BKESARI_SSO_INTROSPECT_URL", raising=False)
    app = FastAPI()
    assert og.install(app) is False
    assert app.user_middleware == []
    monkeypatch.setenv("BKESARI_SSO_INTROSPECT_URL", "https://dev.bkesari.com/auth/api/introspect")
    assert og.install(app, check=og.OwnerCheck(fake_introspect([]))) is True
    assert app.user_middleware[0].cls is og.OwnerGateMiddleware


def test_server_installs_the_gate_last_so_it_is_outermost():
    src = Path(__file__).resolve().parents[1].joinpath("dashboard", "server.py").read_text(encoding="utf-8")
    ops = src.index("_install_ops_http(app)")
    gate = src.index("_install_owner_gate(app)")
    assert gate > ops
    # Nothing after it adds another middleware that would wrap it.
    assert not re.search(r"add_middleware|@app\.middleware", src[gate:])


# ── in front of the real dashboard ───────────────────────────────────────

@pytest.fixture
def gated(api_app):
    from fastapi.testclient import TestClient
    calls = []
    app = og.OwnerGateMiddleware(api_app, check=og.OwnerCheck(fake_introspect(calls), ttl=0), exempt=["/health/live"])
    return TestClient(app), TestClient(api_app), calls


@pytest.fixture
def api_app(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from db.schema import init_db
    from dashboard import security, server
    from orders import rules
    init_db()
    rules.init_orders_table()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return server.app


# Pages, read APIs that reveal the portfolio, and the routes that can trade.
ROUTES = [
    ("GET", "/"), ("GET", "/app"), ("GET", "/login"), ("GET", "/admin"), ("GET", "/trading"),
    ("GET", "/api/scores"), ("GET", "/api/orders/broker-status"), ("GET", "/api/live-quotes"),
    ("POST", "/api/orders"), ("POST", "/api/orders/1/confirm"), ("DELETE", "/api/orders/1"),
    ("POST", "/api/risk/emergency-exit"), ("POST", "/api/refresh"), ("POST", "/api/oms/orders"),
    ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/health"), ("GET", "/manifest.webmanifest"),
]


def _call(client, method, path, token):
    cookies = {"__Host-bk_sso": token} if token else {}
    client.cookies.clear()
    for k, v in cookies.items():
        client.cookies.set(k, v)
    return client.request(method, path, json={} if method in ("POST", "PUT") else None)


@pytest.mark.parametrize("who", NON_OWNERS + ["down"])
def test_every_route_is_a_plain_404_for_anyone_but_the_owner(gated, who):
    client, _, _ = gated
    unknown = _call(client, "GET", "/no-such-route-anywhere", who)
    for method, path in ROUTES:
        r = _call(client, method, path, who)
        assert r.status_code == 404, (who, method, path, r.status_code)
        assert r.content == unknown.content == og.NOT_FOUND, (who, method, path)
        assert b"atip" not in r.content.lower()
        assert "www-authenticate" not in r.headers


def test_the_owner_reaches_the_dashboard(gated):
    client, raw, _ = gated
    r = _call(client, "GET", "/", "owner")
    assert r.status_code == 200
    assert "ATIP" in r.text
    # The owner is still subject to everything behind the gate: the order
    # routes keep requiring the dashboard token.
    r = _call(client, "POST", "/api/orders", "owner")
    assert r.status_code == 401


def test_the_liveness_probe_is_the_only_exemption_and_says_nothing(gated):
    client, _, calls = gated
    r = _call(client, "GET", "/health/live", None)
    assert r.status_code != 404
    assert calls == []
    assert _call(client, "POST", "/health/live", None).status_code == 404
