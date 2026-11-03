"""Super-admin seed and the pending-registration approval workflow (enterprise/approvals.py)."""

import pytest

from db.schema import get_connection
from enterprise import approvals, rbac, tenants, users
from enterprise import super_admin_seed as seed_mod
from enterprise.users import AuthError

PW = "Str0ng-passw0rd-1"


@pytest.fixture
def conn(temp_db):
    c = get_connection()
    rbac.seed(c)
    yield c
    c.close()


def _pending(conn, name="newbie", tenant="t-new"):
    if not tenants.get(conn, tenant):
        tenants.create(conn, tenant, tenant, actor="test")
    return users.create_user(conn, name, PW, tenant, ["SUPER_ADMIN"], email=f"{name}@example.test",
                             status="PENDING", actor="self-registration")


def test_parse_emails_dedupes_and_drops_junk():
    assert seed_mod.parse_emails(" A@x.com, a@X.com ,bad, b@y.org ") == ["a@x.com", "b@y.org"]
    assert seed_mod.parse_emails(None) == []


def test_seed_is_idempotent_and_makes_platform_admins(conn):
    emails = ["one@example.test", "two@example.test"]
    seed_mod.seed(conn, emails)
    seed_mod.seed(conn, emails)
    rows = conn.execute("SELECT status, must_change_password FROM enterprise_user").fetchall()
    assert len(rows) == 2 and all(r[0] == "ACTIVE" and r[1] == 1 for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM enterprise_user_role WHERE role='SUPER_ADMIN'").fetchone()[0] == 2


def test_seeded_password_is_not_guessable_or_stored_plain(conn):
    seed_mod.seed(conn, ["one@example.test"])
    h = conn.execute("SELECT password_hash FROM enterprise_user").fetchone()[0]
    assert "one" not in h and not h.startswith(PW)
    with pytest.raises(AuthError):
        users.login(conn, "one", "anything-Guess1!")


def test_seed_leaves_existing_user_password_and_disabled_state(conn):
    tenants.create(conn, "default", "Platform", actor="test")
    users.create_user(conn, "old", PW, "default", ["VIEWER"], email="old@example.test", status="DISABLED")
    before = conn.execute("SELECT password_hash FROM enterprise_user WHERE username='old'").fetchone()[0]
    seed_mod.seed(conn, ["old@example.test"])
    after = conn.execute("SELECT password_hash, status FROM enterprise_user WHERE username='old'").fetchone()
    assert after[0] == before and after[1] == "DISABLED"


def test_pending_user_cannot_login_until_approved(conn):
    u = _pending(conn)
    with pytest.raises(AuthError, match="PENDING"):
        users.login(conn, "newbie", PW)
    assert [p["user_id"] for p in approvals.pending(conn)] == [u["user_id"]]
    assert approvals.count(conn) == 1
    approvals.decide(conn, u["user_id"], "APPROVE", actor="boss")
    assert users.login(conn, "newbie", PW)["user"] if "user" in users.login(conn, "newbie", PW) else True
    assert approvals.count(conn) == 0


def test_rejected_user_is_refused_with_reason(conn):
    u = _pending(conn)
    approvals.decide(conn, u["user_id"], "REJECT", actor="boss", reason="Not a known member")
    with pytest.raises(AuthError, match="Not a known member"):
        users.login(conn, "newbie", PW)


def test_decision_is_logged_with_actor_and_ist(conn):
    u = _pending(conn)
    approvals.decide(conn, u["user_id"], "REJECT", actor="boss", reason="no")
    row = approvals.history(conn)[0]
    assert row["actor"] == "boss" and row["action"] == "REJECT" and row["user_id"] == u["user_id"]
    assert row["at_ist"].endswith("IST")
    assert conn.execute("SELECT COUNT(*) FROM enterprise_audit WHERE action='user.reject'").fetchone()[0] == 1


def test_cannot_decide_twice(conn):
    u = _pending(conn)
    approvals.decide(conn, u["user_id"], "APPROVE", actor="boss")
    with pytest.raises(ValueError, match="already"):
        approvals.decide(conn, u["user_id"], "REJECT", actor="boss")


def test_tenant_scoped_admin_cannot_decide_other_tenants_users(conn):
    u = _pending(conn, tenant="t-other")
    with pytest.raises(ValueError):
        approvals.decide(conn, u["user_id"], "APPROVE", actor="x", tenant_id="t-mine")
    assert approvals.count(conn, "t-mine") == 0 and approvals.count(conn, "t-other") == 1


def test_password_reset_does_not_self_approve_pending_user(conn):
    u = _pending(conn)
    token = users.issue_reset(conn, u["user_id"], actor="boss")["reset_token"]
    users.reset_password(conn, token, "An0ther-str0ng-pass")
    assert conn.execute("SELECT status FROM enterprise_user WHERE user_id=?", (u["user_id"],)).fetchone()[0] == "PENDING"


def test_admin_role_can_manage_users_but_is_not_super_admin(conn):
    perms = rbac.role_permissions(conn, "ADMIN")
    assert "admin:users" in perms and "admin:roles" not in perms and "admin:tenants" not in perms
