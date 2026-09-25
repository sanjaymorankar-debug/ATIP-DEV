"""
Credential primitives (standard library only).

    hash_password / verify_password   PBKDF2-HMAC-SHA256, 390,000 iterations, 16-byte
                                      random salt; stored as
                                      "pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>";
                                      verified in constant time
    password_problems(pw, min_len)    policy: length, a letter and a digit, not all
                                      one character
    new_secret(prefix)                opaque random token (sessions, API keys, reset tokens)
    digest(token)                     SHA-256 hex -- only digests are stored, never the
                                      token itself
No password or token is ever logged or returned after creation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ITERATIONS = 390_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, iters, salt, h = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(h))
    except Exception:
        return False


def password_problems(pw: str, min_len: int = 12) -> list:
    out = []
    if not isinstance(pw, str) or len(pw) < min_len:
        out.append(f"at least {min_len} characters")
    if not any(c.isalpha() for c in pw or ""):
        out.append("a letter")
    if not any(c.isdigit() for c in pw or ""):
        out.append("a digit")
    if pw and len(set(pw)) < 4:
        out.append("more than a few distinct characters")
    return out


def new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
