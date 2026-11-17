"""
The bkesari ID (bkesari-platform decision D-039): sign in once at bkesari.com.

When ATIP is served under bkesari.com/atip, the hub's access gate stands in front of it: it signs the
person in (Google, an email code or the built-in administrators), lets only ATIP's owners through, removes
any X-Bkesari-* header the browser sent and tells ATIP who the person is in signed headers. This module
checks those headers exactly as LadWani and Education do (bkesari-platform docs/design/bkesari-id.md §3):

    X-Bkesari-User / -Email / -Role / -Name (base64url of UTF-8) / -Project / -Ts / -Sig
    Sig = base64url(HMAC-SHA256(HUB_PROXY_SECRET,
          "bkesari-id-v1\\n<project>\\n<user>\\n<email>\\n<role>\\n<name as sent>\\n<ts>"))

and accepts them only when HUB_PROXY_SECRET is set, the project is "atip", the time is less than 60 s
away, every header was sent once, and the hub's role is "owner". Anything else is ignored, and ATIP's
own sign-in (sessions, API keys) works as before.

The person becomes an ATIP user of the owner's tenant:
  - a built-in administrator (<name>@builtin.bkesari.com): the ATIP user with that username;
  - anyone else: the ATIP user with that email; on the first visit of an owner who has none, one is made
    (SUPER_ADMIN of the default tenant, a random password nobody knows, audited). A username that is
    already someone else's is never taken over: the person is refused instead.
The user must still be ACTIVE and hold roles in the default tenant (authz.resolve checks that).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time

VERSION = "bkesari-id-v1"
PROJECT = "atip"
MAX_SKEW = 60
OWNER_ROLE = "owner"
BUILTIN_DOMAIN = "@builtin.bkesari.com"
HEADERS = ("x-bkesari-project", "x-bkesari-user", "x-bkesari-email", "x-bkesari-role", "x-bkesari-name",
           "x-bkesari-ts", "x-bkesari-sig")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")


def secret() -> str:
    return (os.environ.get("HUB_PROXY_SECRET") or "").strip()


def sign(key: str, project: str, user: str, email: str, role: str, name: str, ts) -> str:
    """The hub's signature (also what the tests sign with)."""
    msg = "\n".join([VERSION, project, user, email, role, name, str(ts)])
    mac = hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def _one(headers, name):
    """A header's single value; None when it is missing or was sent more than once (not the hub's)."""
    if hasattr(headers, "getlist"):
        vals = headers.getlist(name)
    else:
        v = headers.get(name)
        vals = [] if v is None else [v]
    return vals[0] if len(vals) == 1 else None


def verify(headers, now: float | None = None) -> dict | None:
    """The hub's identity from the request headers, or None."""
    key = secret()
    if not key:
        return None
    v = {h: _one(headers, h) for h in HEADERS}
    if any(x is None for x in v.values()):
        return None
    project, user, email, role = v["x-bkesari-project"], v["x-bkesari-user"], v["x-bkesari-email"], v["x-bkesari-role"]
    name, ts, sig = v["x-bkesari-name"], v["x-bkesari-ts"], v["x-bkesari-sig"]
    if project != PROJECT or not re.fullmatch(r"\d{1,15}", user) or not re.fullmatch(r"\d{9,11}", ts) or not email:
        return None
    if not _B64URL.match(name) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", sig):
        return None
    if abs((time.time() if now is None else now) - int(ts)) >= MAX_SKEW:
        return None
    if not hmac.compare_digest(sign(key, project, user, email, role, name, ts), sig):
        return None
    try:
        display = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return {"hub_user_id": user, "email": email.strip().lower(), "role": role, "name": display}


def _user_id(conn, idn: dict, tenant_id: str) -> str | None:
    from enterprise import audit, users
    email = idn["email"]
    if email.endswith(BUILTIN_DOMAIN):
        username = email[: -len(BUILTIN_DOMAIN)]
        r = conn.execute("SELECT user_id FROM enterprise_user WHERE username=?", (username,)).fetchone()
        if r:
            return r[0]
    else:
        r = conn.execute("SELECT user_id FROM enterprise_user WHERE lower(email)=?", (email,)).fetchone()
        if r:
            return r[0]
        username = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower())
        if not re.match(r"^[a-z]", username):
            username = "u" + username
        username = username[:40]
    # The owner's first visit with no ATIP account yet: one is made for them. Never someone else's username.
    if not re.match(r"^[a-z][a-z0-9._-]{2,39}$", username) or \
            conn.execute("SELECT 1 FROM enterprise_user WHERE username=?", (username,)).fetchone():
        audit.record(conn, "auth.hub_refused", actor=username[:40], details={"reason": "username taken or invalid"})
        conn.commit()
        return None
    u = users.create_user(conn, username, secrets.token_urlsafe(32), tenant_id, ["SUPER_ADMIN"],
                          email=None if email.endswith(BUILTIN_DOMAIN) else email,
                          display_name=idn["name"] or None, actor="bkesari-id")
    audit.record(conn, "auth.hub_provisioned", user_id=u["user_id"], actor=username,
                 details={"hub_user_id": idn["hub_user_id"]})
    conn.commit()
    return u["user_id"]


def principal(conn, headers, settings: dict) -> dict | None:
    """The ATIP principal for a request the hub signed for one of ATIP's owners, or None."""
    idn = verify(headers)
    if not idn or idn["role"] != OWNER_ROLE:
        return None
    tenant_id = settings["default_tenant"]
    uid = _user_id(conn, idn, tenant_id)
    if not uid:
        return None
    return {"user_id": uid, "tenant_id": tenant_id, "via": "hub"}
