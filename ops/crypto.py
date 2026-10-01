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


def _previous_key():
    """W31: during / after a rotation the old key stays readable as ATIP_ENCRYPTION_KEY.previous."""
    p = Path("atip_data") / "secrets" / "ATIP_ENCRYPTION_KEY.previous"
    try:
        k = base64.b64decode(p.read_text(encoding="utf-8").strip())
        return k if len(k) == 32 else None
    except Exception:
        return None


def decrypt(token: str, context: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not token or not token.startswith("enc:v1:"):
        raise ValueError("not an ATIP ciphertext")
    _, _, kid, b = token.split(":", 3)
    k = _key()
    if kid != key_id(k):
        prev = _previous_key()
        if prev is not None and kid == key_id(prev):
            k = prev
        else:
            raise CryptoUnavailable(f"ciphertext was made with key {kid}; current key is {key_id(k)}")
    raw = base64.b64decode(b)
    return AESGCM(k).decrypt(raw[:12], raw[12:], context.encode("utf-8")).decode("utf-8")


def _enc_with(k: bytes, plaintext: str, context: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(k).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return f"enc:v1:{key_id(k)}:{base64.b64encode(nonce + ct).decode()}"


def rotate_key(conn, apply: bool = False) -> dict:
    """
    W31 (SEC-04): re-encrypt every protected value under a NEW key.

    Covered: enterprise_user.mfa_secret_enc / mfa_pending_enc (context
    "enterprise_user.mfa:<user_id>") and every vault entry ("vault:<NAME>").
    Steps (apply=True): decrypt all with the current key -> encrypt with the new key ->
    verify each new value decrypts -> one DB transaction + the vault file written ->
    the old key file is kept as ATIP_ENCRYPTION_KEY.previous (still readable) and the new
    key takes its place. Delete the .previous file only after a verified backup taken
    under the new key. Dry run (default) reports what would be re-encrypted.
    """
    from ops import vault as V
    from ops.secrets import _resolve
    src = _resolve("ATIP_ENCRYPTION_KEY")[1]
    if apply and src != "file":
        raise CryptoUnavailable(f"ATIP_ENCRYPTION_KEY comes from {src}; rotation rewrites the key FILE, which that "
                                f"source would override -- move the key to atip_data/secrets/ATIP_ENCRYPTION_KEY "
                                f"first (and remove it from {src})")
    k_old = _key()
    items = []
    for uid, s, p in conn.execute("SELECT user_id, mfa_secret_enc, mfa_pending_enc FROM enterprise_user WHERE "
                                  "mfa_secret_enc IS NOT NULL OR mfa_pending_enc IS NOT NULL").fetchall():
        for col, tok in (("mfa_secret_enc", s), ("mfa_pending_enc", p)):
            if tok:
                items.append(("db", uid, col, tok, f"enterprise_user.mfa:{uid}"))
    entries = V._load()["entries"]
    for n, tok in entries.items():
        items.append(("vault", n, None, tok, f"vault:{n}"))
    if not apply:
        return {"dry_run": True, "current_key_id": key_id(k_old), "values": len(items),
                "fields": sorted({f"{i[0]}:{i[2] or 'entry'}" for i in items})}
    k_new = os.urandom(32)
    plain = [(it, decrypt(it[3], it[4])) for it in items]            # fails before anything is written
    new = [(it, _enc_with(k_new, pt, it[4])) for it, pt in plain]
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    for (it, tok), (_, pt) in zip(new, plain):                         # verify with the new key
        raw = base64.b64decode(tok.split(":", 3)[3])
        if AESGCM(k_new).decrypt(raw[:12], raw[12:], it[4].encode()).decode() != pt:
            raise RuntimeError("re-encryption verification failed; nothing changed")
    keyp = Path("atip_data") / "secrets" / "ATIP_ENCRYPTION_KEY"
    prevp = keyp.with_name("ATIP_ENCRYPTION_KEY.previous")
    try:
        for it, tok in new:
            if it[0] == "db":
                conn.execute(f"UPDATE enterprise_user SET {it[2]}=? WHERE user_id=?", (tok, it[1]))
        vd = V._load()
        for it, tok in new:
            if it[0] == "vault":
                vd["entries"][it[1]] = tok
        keyp.parent.mkdir(parents=True, exist_ok=True)
        prevp.write_text(base64.b64encode(k_old).decode(), encoding="utf-8")
        V._save(vd)
        keyp.write_text(base64.b64encode(k_new).decode(), encoding="utf-8")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"dry_run": False, "old_key_id": key_id(k_old), "new_key_id": key_id(k_new), "re_encrypted": len(new),
            "note": "if ATIP_ENCRYPTION_KEY is also set in the environment or .env, update it there too -- the "
                    "environment value wins over the key file"}


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
