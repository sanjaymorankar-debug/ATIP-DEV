"""
Encrypted credential vault (SEC-04 / the ENT-06 single-owner part), W31.

Broker and API credentials used to live in plaintext in atip_data/config.json (the
"legacy" source ops/secrets.py still reads). The vault stores them encrypted at rest:

    atip_data/secrets/vault.json   {"version": 1, "entries": {NAME: "enc:v1:<kid>:..."}}
    each value is AES-256-GCM (ops/crypto.py) with associated data "vault:<NAME>", so a
    value moved to another name does not decrypt.

ops/secrets.get(NAME) consults the vault after env / .env / the per-file secrets and
before the legacy config value; the legacy readers (data/dhan, portfolio/zerodha,
alerts/telegram) fall back to ops/secrets when their config.json value is blank -- so a
migrated credential keeps working everywhere.

    python -m ops vault-migrate             DRY RUN: which config.json secrets would move
    python -m ops vault-migrate --apply     move them: vault first (verified by decrypting),
                                            then blank them in config.json -- the original
                                            file is kept as config.json.pre-vault-<ts>
    python -m ops vault-set NAME            read the value from stdin (never argv / logs)
    python -m ops vault-list                names and key ids only

Needs ATIP_ENCRYPTION_KEY (python -m ops keygen). Values are never printed or returned
by any API. Rotating the key re-encrypts the vault (ops/crypto.rotate_key).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

VAULT = Path("atip_data") / "secrets" / "vault.json"


def _ctx(name):
    return f"vault:{name}"


def _load() -> dict:
    try:
        return json.loads(VAULT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "entries": {}}


def _save(d):
    VAULT.parent.mkdir(parents=True, exist_ok=True)
    tmp = VAULT.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, VAULT)
    try:
        os.chmod(VAULT, 0o600)
    except Exception:
        pass


def get(name: str):
    tok = _load()["entries"].get(name)
    if not tok:
        return None
    from ops.crypto import decrypt
    return decrypt(tok, _ctx(name))


def set_secret(name: str, value: str) -> dict:
    from ops.crypto import encrypt, decrypt, key_id
    if not value:
        raise ValueError("empty value")
    tok = encrypt(value, _ctx(name))
    if decrypt(tok, _ctx(name)) != value:
        raise RuntimeError("vault round-trip failed")
    d = _load()
    d["entries"][name] = tok
    d["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save(d)
    return {"name": name, "key_id": key_id()}


def delete(name: str) -> bool:
    d = _load()
    if d["entries"].pop(name, None) is None:
        return False
    _save(d)
    return True


def names() -> list:
    return [{"name": n, "key_id": t.split(":")[2] if t.count(":") >= 3 else None}
            for n, t in sorted(_load()["entries"].items())]


def migrate_from_config(apply: bool = False) -> dict:
    """Move CATALOG secrets that still sit in config.json into the vault."""
    from ops.secrets import CATALOG
    cfgp = Path("atip_data") / "config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    plan = []
    for name, meta in CATALOG.items():
        key = meta.get("legacy")
        v = cfg.get(key) if key else None
        if key and v not in (None, "") and not str(v).upper().startswith(("YOUR_", "XXX")):
            plan.append({"name": name, "config_key": key})
    if not apply:
        return {"dry_run": True, "would_move": plan}
    backup = cfgp.with_name(f"config.json.pre-vault-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(cfgp, backup)
    moved = []
    for p in plan:
        set_secret(p["name"], str(cfg[p["config_key"]]))
        if get(p["name"]) != str(cfg[p["config_key"]]):
            raise RuntimeError(f"vault verification failed for {p['name']}; config.json untouched")
        moved.append(p)
    for p in moved:
        cfg[p["config_key"]] = ""
    tmp = cfgp.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, cfgp)
    return {"dry_run": False, "moved": moved, "config_backup": str(backup),
            "note": "keep the backup until ATIP has run a full day on the vault, then delete it (it holds plaintext)"}
