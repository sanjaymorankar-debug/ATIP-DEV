"""
MFA foundation: TOTP (RFC 6238, SHA-1, 30 s, 6 digits) with the standard library.

    enroll(conn, user_id)          new secret, stored ENCRYPTED (ops/crypto.py, AES-256-GCM)
                                   as mfa_pending_enc; returns the base32 secret and an
                                   otpauth:// URI ONCE for the authenticator app
    confirm(conn, user_id, code)   a valid code moves pending -> mfa_secret_enc, mfa_enabled=1
    verify(conn, user_id, code)    used by users.login when mfa_enabled (window +-1 step;
                                   a code is accepted once -- replay within the process refused)
    disable(conn, user_id, code)   needs a valid current code
    admin_reset(conn, user_id)     admin: clears MFA (the user enrolls again); audited
Requires an encryption key (python -m ops keygen); without one enrollment is refused --
the TOTP secret is never stored in plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import threading
import time
from urllib.parse import quote

from enterprise import audit

STEP, DIGITS, WINDOW = 30, 6, 1
_USED = {}
_LOCK = threading.Lock()


def new_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def check(secret: str, code: str, now=None, user_key=None) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return False
    c0 = int((now or time.time()) // STEP)
    for c in range(c0 - WINDOW, c0 + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, c), code):
            if user_key is not None:
                with _LOCK:
                    if _USED.get(user_key, -1) >= c:
                        return False               # replay of an already-used code
                    _USED[user_key] = c
            return True
    return False


def _ctx(user_id):
    return f"enterprise_user.mfa:{user_id}"


def _secret(conn, user_id, col):
    from ops.crypto import decrypt
    r = conn.execute(f"SELECT {col} FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    return decrypt(r[0], _ctx(user_id)) if r and r[0] else None


def enabled(conn, user_id) -> bool:
    try:
        r = conn.execute("SELECT mfa_enabled FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    except Exception:
        return False
    return bool(r and r[0])


def enroll(conn, user_id, issuer="ATIP") -> dict:
    from ops.crypto import available, encrypt
    if not available():
        raise ValueError("MFA needs an encryption key: python -m ops keygen (then restart ATIP)")
    if enabled(conn, user_id):
        raise ValueError("MFA is already enabled; disable it first")
    r = conn.execute("SELECT username FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        raise ValueError(f"no user {user_id}")
    s = new_secret()
    conn.execute("UPDATE enterprise_user SET mfa_pending_enc=? WHERE user_id=?", (encrypt(s, _ctx(user_id)), user_id))
    audit.record(conn, "mfa.enroll_started", user_id=user_id, actor=user_id, commit=False)
    conn.commit()
    uri = f"otpauth://totp/{quote(issuer)}:{quote(r[0])}?secret={s}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"
    return {"secret": s, "otpauth_uri": uri, "note": "shown once; confirm with a code from the authenticator app"}


def confirm(conn, user_id, code) -> dict:
    s = _secret(conn, user_id, "mfa_pending_enc")
    if not s:
        raise ValueError("no pending MFA enrollment")
    if not check(s, code, user_key=user_id):
        raise ValueError("invalid code")
    conn.execute("UPDATE enterprise_user SET mfa_secret_enc=mfa_pending_enc, mfa_pending_enc=NULL, mfa_enabled=1 "
                 "WHERE user_id=?", (user_id,))
    audit.record(conn, "mfa.enabled", user_id=user_id, actor=user_id, commit=False)
    conn.commit()
    return {"mfa_enabled": True}


def verify(conn, user_id, code) -> bool:
    try:
        s = _secret(conn, user_id, "mfa_secret_enc")
    except Exception:
        return False                                   # key missing / wrong: fail closed
    return bool(s) and check(s, code, user_key=user_id)


def disable(conn, user_id, code) -> dict:
    if not verify(conn, user_id, code):
        raise ValueError("invalid code")
    conn.execute("UPDATE enterprise_user SET mfa_enabled=0, mfa_secret_enc=NULL, mfa_pending_enc=NULL WHERE user_id=?",
                 (user_id,))
    audit.record(conn, "mfa.disabled", user_id=user_id, actor=user_id, commit=False)
    conn.commit()
    return {"mfa_enabled": False}


def admin_reset(conn, user_id, actor) -> dict:
    conn.execute("UPDATE enterprise_user SET mfa_enabled=0, mfa_secret_enc=NULL, mfa_pending_enc=NULL WHERE user_id=?",
                 (user_id,))
    audit.record(conn, "mfa.admin_reset", user_id=user_id, actor=actor, commit=False)
    conn.commit()
    return {"mfa_enabled": False}
