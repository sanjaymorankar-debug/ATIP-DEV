"""
Users, memberships, sessions, passwords.

enterprise_user: user_id, username (unique, lowercase), email, display_name,
password_hash (PBKDF2, see security.py), status, failed_logins, locked_until,
must_change_password, preferences_json, created / updated / last_login, created_by.

Status: PENDING (self-registered, awaiting activation) -> ACTIVE <-> DISABLED;
ACTIVE -> LOCKED automatically after max_failed_logins (until lockout_minutes pass
or an admin re-activates).

Membership + roles: enterprise_user_role (tenant_id, user_id, role). A user may
belong to several tenants; a session is bound to ONE tenant.

Sessions: enterprise_session stores only the SHA-256 digest of the token, with
tenant, expiry, IP and user agent; logout / password change / disable revoke them.

Password reset: an admin issues a one-time reset token (digest stored, 30-minute
expiry) and hands it to the user out of band; ATIP sends no e-mail.

W8 refresh tokens: login also returns a refresh token (atf_..., digest stored in
enterprise_refresh_token, enterprise.refresh_days = 14). refresh() exchanges it ONCE
for a new session + a new refresh token of the same family (rotation). Presenting an
already-used refresh token is treated as theft: the whole family and every session of
the user are revoked. Logout / password change / disable revoke refresh tokens too.

W8 MFA: when enterprise_user.mfa_enabled, login requires a TOTP code (`otp`);
enterprise/mfa.py. A missing code -> AuthError "mfa_required"; a wrong one counts as
a failed login (lockout applies).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta

from enterprise import audit
from enterprise import security as S
from enterprise.config import settings

STATUSES = ("PENDING", "ACTIVE", "LOCKED", "DISABLED")


class AuthError(ValueError):
    pass


def _now():
    return datetime.now()


def _pw_check(pw):
    probs = S.password_problems(pw, int(settings()["password_min_length"]))
    if probs:
        raise ValueError("password needs " + ", ".join(probs))


def create_user(conn, username, password, tenant_id, roles, email=None, display_name=None, status="ACTIVE",
                actor="system", must_change_password=False) -> dict:
    from enterprise import tenants
    username = (username or "").strip().lower()
    if not re.match(r"^[a-z][a-z0-9._-]{2,39}$", username):
        raise ValueError("username: 3-40 chars, lowercase letters, digits, . _ -")
    if conn.execute("SELECT 1 FROM enterprise_user WHERE username=?", (username,)).fetchone():
        raise ValueError(f"username {username} is taken")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    _pw_check(password)
    if not tenants.get(conn, tenant_id):
        raise ValueError(f"no tenant {tenant_id}")
    tenants.check_limit(conn, tenant_id, "users")
    _roles_ok(conn, roles)
    uid = "U" + uuid.uuid4().hex[:12].upper()
    now = _now()
    conn.execute("INSERT INTO enterprise_user (user_id,username,email,display_name,password_hash,status,failed_logins,"
                 "must_change_password,preferences_json,created_at,updated_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uid, username, email, display_name or username, S.hash_password(password), status, 0,
                  int(bool(must_change_password)), "{}", now, now, actor))
    conn.executemany("INSERT INTO enterprise_user_role (tenant_id,user_id,role,granted_by,granted_at) VALUES (?,?,?,?,?)",
                     [(tenant_id, uid, r, actor, now) for r in roles])
    audit.record(conn, "user.create", tenant_id=tenant_id, user_id=uid, actor=actor, resource=username,
                 details={"roles": roles, "status": status}, commit=False)
    conn.commit()
    return get(conn, uid)


def _roles_ok(conn, roles):
    known = {r[0] for r in conn.execute("SELECT role FROM enterprise_role")}
    if not roles or set(roles) - known:
        raise ValueError(f"roles must be a non-empty subset of {sorted(known)}")


def get(conn, user_id) -> dict | None:
    r = conn.execute("SELECT * FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d.pop("password_hash", None)
    d.pop("mfa_secret_enc", None)
    d.pop("mfa_pending_enc", None)
    d["preferences"] = json.loads(d.pop("preferences_json") or "{}")
    d["memberships"] = memberships(conn, user_id)
    return d


def by_username(conn, username):
    r = conn.execute("SELECT user_id FROM enterprise_user WHERE username=?", ((username or "").lower(),)).fetchone()
    return get(conn, r[0]) if r else None


def memberships(conn, user_id) -> dict:
    out = {}
    for t, role in conn.execute("SELECT tenant_id, role FROM enterprise_user_role WHERE user_id=? ORDER BY tenant_id, role",
                                (user_id,)):
        out.setdefault(t, []).append(role)
    return out


def list_users(conn, tenant_id=None) -> list:
    if tenant_id:
        ids = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM enterprise_user_role WHERE tenant_id=?",
                                          (tenant_id,))]
    else:
        ids = [r[0] for r in conn.execute("SELECT user_id FROM enterprise_user ORDER BY username")]
    return [get(conn, i) for i in ids]


def set_roles(conn, user_id, tenant_id, roles, actor="system") -> dict:
    from enterprise import tenants
    _roles_ok(conn, roles)
    if not get(conn, user_id):
        raise ValueError(f"no user {user_id}")
    new_member = not conn.execute("SELECT 1 FROM enterprise_user_role WHERE tenant_id=? AND user_id=?",
                                  (tenant_id, user_id)).fetchone()
    if new_member:
        tenants.check_limit(conn, tenant_id, "users")
    conn.execute("DELETE FROM enterprise_user_role WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
    conn.executemany("INSERT INTO enterprise_user_role (tenant_id,user_id,role,granted_by,granted_at) VALUES (?,?,?,?,?)",
                     [(tenant_id, user_id, r, actor, _now()) for r in roles])
    audit.record(conn, "user.roles", tenant_id=tenant_id, user_id=user_id, actor=actor, details={"roles": roles},
                 commit=False)
    conn.commit()
    return get(conn, user_id)


def set_status(conn, user_id, status, actor="system") -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute("UPDATE enterprise_user SET status=?, failed_logins=0, locked_until=NULL, updated_at=? WHERE user_id=?",
                 (status, _now(), user_id))
    if status != "ACTIVE":
        revoke_sessions(conn, user_id)
    audit.record(conn, "user.status", user_id=user_id, actor=actor, details={"status": status}, commit=False)
    conn.commit()
    return get(conn, user_id)


def update_profile(conn, user_id, email=None, display_name=None, preferences=None) -> dict:
    u = get(conn, user_id)
    if not u:
        raise ValueError(f"no user {user_id}")
    prefs = {**u["preferences"], **(preferences or {})}
    conn.execute("UPDATE enterprise_user SET email=?, display_name=?, preferences_json=?, updated_at=? WHERE user_id=?",
                 (email if email is not None else u["email"], display_name or u["display_name"], json.dumps(prefs),
                  _now(), user_id))
    audit.record(conn, "user.profile", user_id=user_id, actor=user_id, commit=False)
    conn.commit()
    return get(conn, user_id)


# -- authentication -------------------------------------------------------------------

def login(conn, username, password, tenant_id=None, ip=None, user_agent=None, otp=None) -> dict:
    s = settings()
    row = conn.execute("SELECT * FROM enterprise_user WHERE username=?", ((username or "").lower(),)).fetchone()
    generic = AuthError("invalid username or password")
    if not row:
        S.verify_password(password or "", S.hash_password("x"))      # similar time either way
        audit.record(conn, "auth.login_failed", actor=(username or "")[:40], ip=ip, details={"reason": "unknown user"})
        raise generic
    u = dict(row)
    if u["status"] == "LOCKED" and u["locked_until"] and str(u["locked_until"]) < str(_now()):
        conn.execute("UPDATE enterprise_user SET status='ACTIVE', failed_logins=0, locked_until=NULL WHERE user_id=?",
                     (u["user_id"],))
        u["status"] = "ACTIVE"
    if u["status"] != "ACTIVE":
        audit.record(conn, "auth.login_refused", user_id=u["user_id"], ip=ip, details={"status": u["status"]})
        raise AuthError(f"account is {u['status']}")
    if not S.verify_password(password or "", u["password_hash"]):
        fails = (u["failed_logins"] or 0) + 1
        lock = fails >= int(s["max_failed_logins"])
        conn.execute("UPDATE enterprise_user SET failed_logins=?, status=?, locked_until=? WHERE user_id=?",
                     (fails, "LOCKED" if lock else "ACTIVE",
                      _now() + timedelta(minutes=int(s["lockout_minutes"])) if lock else None, u["user_id"]))
        audit.record(conn, "auth.login_failed", user_id=u["user_id"], ip=ip, details={"failed_logins": fails,
                                                                                       "locked": lock})
        raise generic
    from enterprise import mfa
    if u.get("mfa_enabled"):
        if not otp:
            audit.record(conn, "auth.mfa_required", user_id=u["user_id"], ip=ip)
            raise AuthError("mfa_required")
        if not mfa.verify(conn, u["user_id"], otp):
            fails = (u["failed_logins"] or 0) + 1
            lock = fails >= int(s["max_failed_logins"])
            conn.execute("UPDATE enterprise_user SET failed_logins=?, status=?, locked_until=? WHERE user_id=?",
                         (fails, "LOCKED" if lock else "ACTIVE",
                          _now() + timedelta(minutes=int(s["lockout_minutes"])) if lock else None, u["user_id"]))
            audit.record(conn, "auth.mfa_failed", user_id=u["user_id"], ip=ip, details={"failed_logins": fails,
                                                                                         "locked": lock})
            raise AuthError("invalid MFA code")
    mem = memberships(conn, u["user_id"])
    if not mem:
        raise AuthError("user belongs to no tenant")
    tid = tenant_id or (s["default_tenant"] if s["default_tenant"] in mem else sorted(mem)[0])
    if tid not in mem:
        raise AuthError(f"user is not a member of tenant {tid}")
    t = conn.execute("SELECT status FROM enterprise_tenant WHERE tenant_id=?", (tid,)).fetchone()
    if not t or t[0] in ("DISABLED", "ARCHIVED"):
        raise AuthError(f"tenant {tid} is {t[0] if t else 'missing'}")
    token, exp = _new_session(conn, u["user_id"], tid, ip, user_agent)
    refresh_token, rexp = _new_refresh(conn, u["user_id"], tid, uuid.uuid4().hex, ip)
    conn.execute("UPDATE enterprise_user SET failed_logins=0, last_login_at=? WHERE user_id=?", (_now(), u["user_id"]))
    audit.record(conn, "auth.login", tenant_id=tid, user_id=u["user_id"], actor=u["username"], ip=ip,
                 details={"mfa": bool(u.get("mfa_enabled"))}, commit=False)
    conn.commit()
    try:
        from enterprise.privacy import has_consent
        consent_required = not has_consent(conn, u["user_id"], "terms")
    except Exception:
        consent_required = False
    return {"token": token, "expires_at": exp.isoformat(), "tenant_id": tid, "user_id": u["user_id"],
            "must_change_password": bool(u["must_change_password"]), "refresh_token": refresh_token,
            "refresh_expires_at": rexp.isoformat(), "consent_required": consent_required,
            "tenants": sorted(mem)}


def _new_session(conn, user_id, tid, ip, user_agent):
    token = S.new_secret("ats_")
    exp = _now() + timedelta(hours=float(settings()["session_hours"]))
    conn.execute("INSERT INTO enterprise_session (token_hash,user_id,tenant_id,created_at,expires_at,last_seen_at,ip,"
                 "user_agent) VALUES (?,?,?,?,?,?,?,?)", (S.digest(token), user_id, tid, _now(), exp, _now(), ip,
                                                          (user_agent or "")[:200]))
    return token, exp


def _new_refresh(conn, user_id, tid, family, ip):
    token = S.new_secret("atf_")
    exp = _now() + timedelta(days=float(settings().get("refresh_days", 14)))
    conn.execute("INSERT INTO enterprise_refresh_token (token_hash,family_id,user_id,tenant_id,created_at,expires_at,ip)"
                 " VALUES (?,?,?,?,?,?,?)", (S.digest(token), family, user_id, tid, _now(), exp, ip))
    return token, exp


def refresh(conn, refresh_token, ip=None, user_agent=None) -> dict:
    """Rotate: one use per refresh token; reuse revokes the family and the user's sessions."""
    if not refresh_token:
        raise AuthError("refresh token required")
    r = conn.execute("SELECT * FROM enterprise_refresh_token WHERE token_hash=?", (S.digest(refresh_token),)).fetchone()
    if not r:
        raise AuthError("invalid refresh token")
    r = dict(r)
    if r["used_at"] or r["revoked_at"]:
        conn.execute("UPDATE enterprise_refresh_token SET revoked_at=COALESCE(revoked_at, ?) WHERE family_id=?",
                     (_now(), r["family_id"]))
        revoke_sessions(conn, r["user_id"])
        audit.record(conn, "auth.refresh_reuse", tenant_id=r["tenant_id"], user_id=r["user_id"], ip=ip,
                     details={"family": r["family_id"]}, commit=False)
        conn.commit()
        raise AuthError("refresh token reuse detected; all sessions revoked -- sign in again")
    if str(r["expires_at"]) < str(_now()):
        raise AuthError("refresh token expired")
    u = conn.execute("SELECT status FROM enterprise_user WHERE user_id=?", (r["user_id"],)).fetchone()
    if not u or u[0] != "ACTIVE":
        raise AuthError("account is not active")
    if r["tenant_id"] not in memberships(conn, r["user_id"]):
        raise AuthError("membership removed")
    token, exp = _new_session(conn, r["user_id"], r["tenant_id"], ip, user_agent)
    new_rt, rexp = _new_refresh(conn, r["user_id"], r["tenant_id"], r["family_id"], ip)
    conn.execute("UPDATE enterprise_refresh_token SET used_at=?, replaced_by=? WHERE token_hash=?",
                 (_now(), S.digest(new_rt), r["token_hash"]))
    audit.record(conn, "auth.refresh", tenant_id=r["tenant_id"], user_id=r["user_id"], ip=ip, commit=False)
    conn.commit()
    return {"token": token, "expires_at": exp.isoformat(), "tenant_id": r["tenant_id"], "user_id": r["user_id"],
            "refresh_token": new_rt, "refresh_expires_at": rexp.isoformat()}


def session_principal(conn, token) -> dict | None:
    r = conn.execute("SELECT * FROM enterprise_session WHERE token_hash=? AND revoked_at IS NULL",
                     (S.digest(token),)).fetchone()
    if not r or str(r["expires_at"]) < str(_now()):
        return None
    conn.execute("UPDATE enterprise_session SET last_seen_at=? WHERE token_hash=?", (_now(), r["token_hash"]))
    conn.commit()
    return {"user_id": r["user_id"], "tenant_id": r["tenant_id"], "via": "session"}


def list_sessions(conn, user_id, current_token=None) -> list:
    """W9 multi-device login: the user's live sessions (id = first 16 hex chars of the digest)."""
    cur = S.digest(current_token) if current_token else None
    out = []
    for r in conn.execute("SELECT token_hash, tenant_id, created_at, expires_at, last_seen_at, ip, user_agent FROM "
                          "enterprise_session WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY "
                          "last_seen_at DESC", (user_id, _now())):
        out.append({"session_id": r[0][:16], "tenant_id": r[1], "created_at": r[2], "expires_at": r[3],
                    "last_seen_at": r[4], "ip": r[5], "user_agent": r[6], "current": r[0] == cur})
    return out


def revoke_session(conn, user_id, session_id, actor=None) -> dict:
    if not re.match(r"^[0-9a-f]{16}$", session_id or ""):
        raise ValueError("bad session id")
    n = conn.execute("UPDATE enterprise_session SET revoked_at=? WHERE user_id=? AND token_hash LIKE ? AND "
                     "revoked_at IS NULL", (_now(), user_id, session_id + "%")).rowcount
    if not n:
        raise ValueError(f"no active session {session_id}")
    audit.record(conn, "auth.session_revoked", user_id=user_id, actor=actor or user_id, resource=session_id,
                 commit=False)
    conn.commit()
    return {"revoked": session_id}


def switch_tenant(conn, token, tenant_id, ip=None, user_agent=None) -> dict:
    """W9 tenant switcher: a new session bound to another of the user's tenants; the old
    session is revoked."""
    p = session_principal(conn, token)
    if not p:
        raise AuthError("not signed in")
    mem = memberships(conn, p["user_id"])
    if tenant_id not in mem:
        raise AuthError(f"not a member of tenant {tenant_id}")
    t = conn.execute("SELECT status FROM enterprise_tenant WHERE tenant_id=?", (tenant_id,)).fetchone()
    if not t or t[0] in ("DISABLED", "ARCHIVED"):
        raise AuthError(f"tenant {tenant_id} is {t[0] if t else 'missing'}")
    new, exp = _new_session(conn, p["user_id"], tenant_id, ip, user_agent)
    rt, rexp = _new_refresh(conn, p["user_id"], tenant_id, uuid.uuid4().hex, ip)
    conn.execute("UPDATE enterprise_session SET revoked_at=? WHERE token_hash=?", (_now(), S.digest(token)))
    audit.record(conn, "auth.switch_tenant", tenant_id=tenant_id, user_id=p["user_id"], ip=ip,
                 details={"from": p["tenant_id"]}, commit=False)
    conn.commit()
    return {"token": new, "expires_at": exp.isoformat(), "tenant_id": tenant_id, "user_id": p["user_id"],
            "refresh_token": rt, "refresh_expires_at": rexp.isoformat()}


def logout(conn, token) -> None:
    conn.execute("UPDATE enterprise_session SET revoked_at=? WHERE token_hash=?", (_now(), S.digest(token)))
    conn.commit()


def revoke_sessions(conn, user_id) -> None:
    conn.execute("UPDATE enterprise_session SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_now(), user_id))
    try:
        conn.execute("UPDATE enterprise_refresh_token SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                     (_now(), user_id))
    except Exception:
        pass


def change_password(conn, user_id, old, new) -> None:
    r = conn.execute("SELECT password_hash FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    if not r or not S.verify_password(old or "", r[0]):
        raise AuthError("current password is wrong")
    _pw_check(new)
    conn.execute("UPDATE enterprise_user SET password_hash=?, must_change_password=0, updated_at=? WHERE user_id=?",
                 (S.hash_password(new), _now(), user_id))
    revoke_sessions(conn, user_id)
    audit.record(conn, "user.password_change", user_id=user_id, actor=user_id, commit=False)
    conn.commit()


def issue_reset(conn, user_id, actor) -> dict:
    if not get(conn, user_id):
        raise ValueError(f"no user {user_id}")
    token = S.new_secret("atr_")
    exp = _now() + timedelta(minutes=30)
    conn.execute("INSERT INTO enterprise_password_reset (token_hash,user_id,expires_at,created_by,created_at) "
                 "VALUES (?,?,?,?,?)", (S.digest(token), user_id, exp, actor, _now()))
    audit.record(conn, "user.reset_issued", user_id=user_id, actor=actor, commit=False)
    conn.commit()
    return {"reset_token": token, "expires_at": exp.isoformat(), "note": "give this to the user; shown once"}


def reset_password(conn, reset_token, new) -> None:
    r = conn.execute("SELECT user_id, expires_at, used_at FROM enterprise_password_reset WHERE token_hash=?",
                     (S.digest(reset_token),)).fetchone()
    if not r or r[2] or str(r[1]) < str(_now()):
        raise AuthError("reset token invalid or expired")
    _pw_check(new)
    conn.execute("UPDATE enterprise_user SET password_hash=?, status='ACTIVE', failed_logins=0, locked_until=NULL, "
                 "must_change_password=0, updated_at=? WHERE user_id=?", (S.hash_password(new), _now(), r[0]))
    conn.execute("UPDATE enterprise_password_reset SET used_at=? WHERE token_hash=?", (_now(), S.digest(reset_token)))
    revoke_sessions(conn, r[0])
    audit.record(conn, "user.password_reset", user_id=r[0], actor=r[0], commit=False)
    conn.commit()
