"""
Field encryption at rest: AES-256-GCM (the `cryptography` package, already installed).

Key: the secret ATIP_ENCRYPTION_KEY (base64 of 32 random bytes), resolved by
ops/secrets.py (env, .env or atip_data/secrets/ATIP_ENCRYPTION_KEY). Create one with
    python -m ops keygen          (writes atip_data/secrets/ATIP_ENCRYPTION_KEY, prints nothing)
Without a key, encrypt() raises -- ATIP never falls back to storing a protected
field in plaintext.

Ciphertext format: "enc:v1:<key id>:<base64(nonce 12 bytes | ciphertext+tag)>".
The key id (first 8 hex chars of SHA-256 of the key) lets a rotated key be
recognised; decrypt() of a value made with another key raises (re-encryption on
rotation is a documented procedure, not automatic).

Associated data binds a ciphertext to its column (e.g. "enterprise_user.mfa_secret"),
so a value copied into another field does not decrypt.

SENSITIVE FIELD CLASSIFICATION (docs/SECURITY_ARCHITECTURE.md): what is hashed,
encrypted, digest-only, or still plaintext (legacy config.json secrets).
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path


class CryptoUnavailable(RuntimeError):
    pass


def _key() -> bytes:
    from ops.secrets import get
    raw = get("ATIP_ENCRYPTION_KEY", log_access=False)
    if not raw:
        raise CryptoUnavailable("ATIP_ENCRYPTION_KEY is not configured (python -m ops keygen)")
    k = base64.b64decode(raw)
    if len(k) != 32:
        raise CryptoUnavailable("ATIP_ENCRYPTION_KEY must decode to 32 bytes (AES-256)")
    return k


def key_id(k: bytes | None = None) -> str:
    return hashlib.sha256(k or _key()).hexdigest()[:8]


def available() -> bool:
    try:
        _key()
        import importlib.util
        return importlib.util.find_spec("cryptography") is not None
    except Exception:
        return False


def encrypt(plaintext: str, context: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k = _key()
    nonce = os.urandom(12)
    ct = AESGCM(k).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return f"enc:v1:{key_id(k)}:{base64.b64encode(nonce + ct).decode()}"


def decrypt(token: str, context: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not token or not token.startswith("enc:v1:"):
        raise ValueError("not an ATIP ciphertext")
    _, _, kid, b = token.split(":", 3)
    k = _key()
    if kid != key_id(k):
        raise CryptoUnavailable(f"ciphertext was made with key {kid}; current key is {key_id(k)}")
    raw = base64.b64decode(b)
    return AESGCM(k).decrypt(raw[:12], raw[12:], context.encode("utf-8")).decode("utf-8")


def keygen(path: Path | None = None) -> str:
    """Create a new key file (refuses to overwrite). Returns the path, never the key."""
    p = path or Path("atip_data") / "secrets" / "ATIP_ENCRYPTION_KEY"
    if p.exists():
        raise FileExistsError(f"{p} exists; rotating a key needs re-encryption (see SECURITY_ARCHITECTURE.md)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(base64.b64encode(os.urandom(32)).decode(), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return str(p)
