"""
The bkesari ID (enterprise/hub_identity.py): when bkesari.com's hub signs a request for one of ATIP's owners,
ATIP signs that person in without its own form. Anything the hub did not sign -- a wrong secret, another
project, an old time, a changed field, a header sent twice, a role other than owner -- is ignored, and ATIP's
own sign-in works as before.
"""

import base64
import json
import time

import pytest

SECRET = "test-only-hub-proxy-secret-0123456789abcdef"
PASSWORD = "Correct-horse-9battery"


def headers(email, role="owner", project="atip", user="42", name="Sanjay", ts=None, secret=SECRET, **over):
    from enterprise.hub_identity import sign
    ts = str(int(time.time()) if ts is None else ts)
    n = base64.urlsafe_b64encode(name.encode("utf-8")).rstrip(b"=").decode()
    h = {"x-bkesari-project": project, "x-bkesari-user": user, "x-bkesari-email": email, "x-bkesari-role": role,
         "x-bkesari-name": n, "x-bkesari-ts": ts,
         "x-bkesari-sig": sign(secret, project, user, email, role, n, ts)}
    h.update(over)
    return h


class Plain(dict):
    """A dict standing in for request headers (no getlist)."""


def test_verify_accepts_only_what_the_hub_signed(monkeypatch):
    from enterprise import hub_identity as H
    monkeypatch.setenv("HUB_PROXY_SECRET", SECRET)
    ok = H.verify(Plain(headers("smorankar@builtin.bkesari.com", name="आशा पाटील")))
    assert ok == {"hub_user_id": "42", "email": "smorankar@builtin.bkesari.com", "role": "owner", "name": "आशा पाटील"}
    now = int(time.time())
    assert H.verify(Plain(headers("a@b.c", project="ladwani"))) is None, "signed for another project"
    assert H.verify(Plain(headers("a@b.c", ts=now - 61))) is None, "stale"
    assert H.verify(Plain(headers("a@b.c", ts=now + 61))) is None, "from the future"
    assert H.verify(Plain(headers("a@b.c", secret="another-secret-0123456789abcdef-0123456"))) is None
    for field, value in (("x-bkesari-role", "admin"), ("x-bkesari-email", "owner@example.com"),
                         ("x-bkesari-user", "1"), ("x-bkesari-name", "U29tZW9uZQ")):
        assert H.verify(Plain(headers("a@b.c", **{field: value}))) is None, field
    h = headers("a@b.c")
    del h["x-bkesari-sig"]
    assert H.verify(Plain(h)) is None, "no signature"
    monkeypatch.setenv("HUB_PROXY_SECRET", "")
    assert H.verify(Plain(headers("a@b.c"))) is None, "no secret: nobody is believed"


def test_a_header_sent_twice_is_not_the_hubs(monkeypatch):
    from starlette.datastructures import Headers
    from enterprise import hub_identity as H
    monkeypatch.setenv("HUB_PROXY_SECRET", SECRET)
    h = headers("a@b.c")
    raw = [(k.encode(), v.encode()) for k, v in h.items()] + [(b"x-bkesari-role", b"owner")]
    assert H.verify(Headers(raw=raw)) is None
    assert H.verify(Headers(raw=[(k.encode(), v.encode()) for k, v in h.items()])) is not None


@pytest.fixture
def app(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import security, server
    from db.schema import get_connection, init_db
    from enterprise import config as ecfg
    from enterprise import service, users

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enterprise": {"enabled": True}}), encoding="utf-8")
    for mod in (ecfg, security):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setenv("HUB_PROXY_SECRET", SECRET)
    init_db()
    conn = get_connection()
    service.ensure_seeded(conn)
    users.create_user(conn, "smorankar", PASSWORD, "default", ["SUPER_ADMIN"])
    users.create_user(conn, "taken", PASSWORD, "default", ["VIEWER"])
    conn.commit()
    yield {"client": TestClient(server.app, raise_server_exceptions=False), "conn": conn, "users": users}
    conn.close()


def me(app, h):
    return app["client"].get("/api/auth/me", headers=h)


def test_a_built_in_administrator_signed_in_at_the_hub_is_signed_in_here(app):
    r = me(app, headers("smorankar@builtin.bkesari.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["username"] == "smorankar" and body["principal"]["via"] == "hub"
    assert "SUPER_ADMIN" in body["principal"]["roles"] and body["principal"]["platform_admin"] is True
    # a page is served, not the sign-in redirect
    page = app["client"].get("/", headers=headers("smorankar@builtin.bkesari.com"), follow_redirects=False)
    assert page.status_code != 302 or "/login" not in page.headers.get("location", "")


def test_the_owners_own_address_gets_an_atip_administrator_once(app):
    h = headers("Owner.Person@Example.com")
    r = me(app, h)
    assert r.status_code == 200, r.text
    u = r.json()["user"]
    assert u["username"] == "owner.person" and u["email"] == "owner.person@example.com"
    again = me(app, headers("owner.person@example.com"))
    assert again.json()["user"]["user_id"] == u["user_id"], "the same account the second time"
    n = app["conn"].execute("SELECT COUNT(*) FROM enterprise_user WHERE username='owner.person'").fetchone()[0]
    assert n == 1
    a = app["conn"].execute("SELECT COUNT(*) FROM enterprise_audit WHERE action='auth.hub_provisioned'").fetchone()[0]
    assert a == 1


def test_someone_elses_username_is_never_taken_over(app):
    r = me(app, headers("taken@example.com"))
    assert r.status_code == 401, r.text
    row = app["conn"].execute("SELECT email FROM enterprise_user WHERE username='taken'").fetchone()
    assert row[0] in (None, ""), "the existing account is untouched"


def test_only_the_hubs_owner_role_signs_in(app):
    for role in ("admin", "member", ""):
        assert me(app, headers("smorankar@builtin.bkesari.com", role=role)).status_code == 401, role


def test_without_the_secret_the_headers_are_ignored_and_atip_s_own_sign_in_still_works(app, monkeypatch):
    monkeypatch.setenv("HUB_PROXY_SECRET", "")
    assert me(app, headers("smorankar@builtin.bkesari.com")).status_code == 401
    token = app["users"].login(app["conn"], "smorankar", PASSWORD)["token"]
    assert me(app, {"Authorization": f"Bearer {token}"}).json()["user"]["username"] == "smorankar"


def test_a_suspended_atip_account_is_refused_even_with_the_hubs_word(app):
    app["conn"].execute("UPDATE enterprise_user SET status='DISABLED' WHERE username='smorankar'")
    app["conn"].commit()
    assert me(app, headers("smorankar@builtin.bkesari.com")).status_code == 401
