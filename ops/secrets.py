"""
Secret management.

get(name) resolves a secret from, in order:
    1. the process environment (ATIP_* / provider names below)
    2. the .env file (python-dotenv, already a dependency) -- loaded once
    3. atip_data/secrets/<name> file (one value per file; the directory is git-ignored
       because atip_data/ is)
    4. W31: the encrypted vault atip_data/secrets/vault.json (ops/vault.py)
    5. LEGACY: the key's historical location in atip_data/config.json (Dhan, Telegram,
       Anthropic ...). Supported so nothing breaks; flagged "legacy" by validate().
Every access is recorded in ops_secret_access with the NAME, source, caller and
time -- never the value. mask() and the logging filter (logs.py) keep values
out of logs; nothing in ops returns a secret value through the API.

CATALOG: the secrets ATIP knows about, where each legacy value lives, and its
rotation period (days). rotation_status() compares ops_secret_meta.rotated_at
against it (mark_rotated() after the owner rotates one). Rotation itself is done
by the owner at the provider (Dhan tokens expire daily and are renewed by hand).
"""

from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

CATALOG = {
    "DHAN_CLIENT_ID": {"legacy": "dhan_client_id", "rotation_days": None, "desc": "Dhan client id"},
    "DHAN_ACCESS_TOKEN": {"legacy": "dhan_access_token", "rotation_days": 1, "desc": "Dhan API token (24 h)"},
    # W38: what tools/dhan_token_refresh.py logs in with -- together a full account login, so vaulted too
    "DHAN_PIN": {"legacy": "dhan_pin", "rotation_days": None, "desc": "Dhan trading PIN (token auto-renewal)"},
    "DHAN_TOTP_SECRET": {"legacy": "dhan_totp_secret", "rotation_days": None, "desc": "Dhan TOTP seed (token auto-renewal)"},
    "TELEGRAM_TOKEN": {"legacy": "telegram_token", "rotation_days": 365, "desc": "Telegram bot token"},
    "TELEGRAM_CHAT_ID": {"legacy": "telegram_chat_id", "rotation_days": None, "desc": "Telegram chat id"},
    "ANTHROPIC_API_KEY": {"legacy": None, "rotation_days": 180, "desc": "Anthropic API key (.env)"},
    "ALPHA_VANTAGE_KEY": {"legacy": "alpha_vantage_key", "rotation_days": 365, "desc": "Alpha Vantage key"},
    "KITE_API_KEY": {"legacy": "kite_api_key", "rotation_days": 365, "desc": "Zerodha Kite key"},
    "KITE_API_SECRET": {"legacy": "kite_api_secret", "rotation_days": 365, "desc": "Zerodha Kite secret"},
    "ATIP_ENCRYPTION_KEY": {"legacy": None, "rotation_days": 365, "desc": "AES-256 data key (base64, 32 bytes)"},
    "WEBHOOK_SECRET_DEFAULT": {"legacy": None, "rotation_days": 180, "desc": "inbound webhook HMAC secret"},
}
SECRETS_DIR = Path("atip_data") / "secrets"
_ENV_LOADED = {"done": False}


def _dotenv():
    if not _ENV_LOADED["done"]:
        try:
            from dotenv import dotenv_values
            _ENV_LOADED["values"] = {k: v for k, v in dotenv_values(".env").items() if v}
        except Exception:
            _ENV_LOADED["values"] = {}
        _ENV_LOADED["done"] = True
    return _ENV_LOADED["values"]


def _legacy(name):
    key = (CATALOG.get(name) or {}).get("legacy")
    if not key:
        return None
    from ops.config import load
    v = load().get(key)
    return str(v) if v not in (None, "") else None


def _resolve(name):
    if os.environ.get(name):
        return os.environ[name], "env"
    if _dotenv().get(name):
        return _dotenv()[name], "dotenv"
    f = SECRETS_DIR / name
    try:
        if f.exists():
            v = f.read_text(encoding="utf-8").strip()
            if v:
                return v, "file"
    except Exception:
        pass
    try:                                            # W31 (SEC-04): the encrypted vault
        from ops.vault import get as _vget
        v = _vget(name)
        if v:
            return v, "vault"
    except Exception:
        pass
    v = _legacy(name)
    return (v, "legacy_config") if v else (None, None)


def get(name: str, log_access: bool = True) -> str | None:
    value, source = _resolve(name)
    if log_access:
        _log_access(name, source, bool(value))
    return value


def _log_access(name, source, found):
    try:
        caller = next((f"{Path(fr.filename).name}:{fr.lineno}" for fr in inspect.stack()[2:5]
                       if "ops" + os.sep + "secrets.py" not in fr.filename), "?")
        from db.schema import get_connection
        c = get_connection()
        try:
            c.execute("INSERT INTO ops_secret_access (name,source,found,caller,at) VALUES (?,?,?,?,?)",
                      (name, source, int(found), caller, datetime.now()))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


def status(name: str) -> dict:
    """Presence / source / format -- never the value."""
    value, source = _resolve(name)
    fmt_ok = None
    if value and name == "ATIP_ENCRYPTION_KEY":
        import base64
        try:
            fmt_ok = len(base64.b64decode(value)) == 32
        except Exception:
            fmt_ok = False
    return {"name": name, "present": bool(value), "source": source, "format_ok": fmt_ok,
            "legacy": source == "legacy_config", "length": len(value) if value else 0}


def validate() -> list:
    out = []
    for name in CATALOG:
        s = status(name)
        if s["legacy"]:
            out.append({"level": "warning", "key": name,
                        "message": f"{name} is read from config.json (plaintext); move it to the environment / "
                                   f".env / atip_data/secrets/{name}"})
        if s["format_ok"] is False:
            out.append({"level": "error", "key": name, "message": f"{name} has the wrong format"})
    return out


def mark_rotated(conn, name, actor="owner"):
    if name not in CATALOG:
        raise ValueError(f"unknown secret {name}")
    conn.execute("INSERT INTO ops_secret_meta (name,rotated_at,rotated_by) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE "
                 "SET rotated_at=excluded.rotated_at, rotated_by=excluded.rotated_by", (name, datetime.now(), actor))
    try:
        from enterprise.audit import record
        record(conn, "secret.rotated", actor=actor, resource=name, commit=False)
    except Exception:
        pass
    conn.commit()


def rotation_status(conn) -> list:
    out = []
    for name, meta in CATALOG.items():
        r = conn.execute("SELECT rotated_at FROM ops_secret_meta WHERE name=?", (name,)).fetchone()
        days = meta["rotation_days"]
        due = None
        if days and r and r[0]:
            due = str(datetime.fromisoformat(str(r[0])[:19]) + timedelta(days=days))
        out.append({"name": name, "present": status(name)["present"], "rotation_days": days,
                    "last_rotated": str(r[0]) if r else None, "due": due,
                    "overdue": bool(due and due < str(datetime.now()))})
    return out


def known_values() -> list:
    """Secret values currently configured (for the log-masking filter only)."""
    vals = []
    for name in CATALOG:
        v, _ = _resolve(name)
        if v and len(v) >= 6:
            vals.append(v)
    try:
        from ops.config import is_secret_key, load

        def walk(d):
            for k, v in (d or {}).items():
                if isinstance(v, dict):
                    walk(v)
                elif is_secret_key(k) and isinstance(v, str) and len(v) >= 6:
                    vals.append(v)
        walk(load())
    except Exception:
        pass
    return sorted(set(vals), key=len, reverse=True)


_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BOT = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
_ANTH = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")
_ATIP = re.compile(r"\b(ats_|atr_|atf_|atk_[0-9a-f]{8}_)[A-Za-z0-9_-]{20,}")


def mask(text: str, values=None) -> str:
    if not text:
        return text
    for v in values or ():
        if v in text:
            text = text.replace(v, "****")
    for rx in (_JWT, _BOT, _ANTH, _ATIP):
        text = rx.sub("****", text)
    return text
