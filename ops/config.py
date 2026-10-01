"""
Environment, layered configuration, validation, feature flags, config versioning.

ENVIRONMENT
    ATIP_ENV (environment variable) > config.json "environment" > "development".
    One of development / test / staging / production.

LAYERED VIEW (ops.config.load())
    atip_data/config.json                       base (the existing file)
      <- atip_data/config.<environment>.json    optional per-environment overlay (deep merge)
      <- ATIP__<SECTION>__<KEY> env variables   e.g. ATIP__OPS__RATE_LIMIT_PER_MINUTE=120
    Values from env vars are parsed as JSON when possible (true / 12 / ["a"]).
    W8 modules read this view. W1-W7 modules still read config.json directly
    (documented in PRODUCTION_CONFIGURATION.md; adoption is incremental) -- so an
    overlay can only change W8 behaviour until a module is migrated.

ENVIRONMENT-SPECIFIC DATABASE
    ATIP_DB_PATH overrides the SQLite file (db/schema.py). Unset = atip_data/atip.db.

"ops" section (all optional, safe defaults):
    tls_enabled false, hsts_seconds 31536000, cors_origins [], csp null,
    max_request_bytes 2_000_000, rate_limit_enabled false, rate_limit_per_minute 300,
    require_idempotency_for_orders false, idempotency_ttl_hours 24,
    backup_enabled true, backup_time "19:15", backup_keep_daily 7, backup_keep_weekly 4,
    backup_dir "atip_data/backups", monitor_enabled true, monitor_minutes 15,
    json_logs true, migrate_every_connection false,
    features {}  -- feature flags: {"name": true|false}

validate(cfg) -> [{"level": "error"|"warning", "key", "message"}]; production turns
errors into a refusal to start (startup.py); other environments log them.

Versioning: record_version(conn) stores a SHA-256 of the effective configuration with
NON-secret values and masked secret values (ops_config_version) and audits which keys
changed since the last start -- never a secret value.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"
ENVIRONMENTS = ("development", "test", "staging", "production")
OPS_DEFAULTS = {
    "tls_enabled": False, "hsts_seconds": 31536000, "cors_origins": [], "csp": None,
    "max_request_bytes": 2_000_000, "rate_limit_enabled": False, "rate_limit_per_minute": 300,
    "require_idempotency_for_orders": False, "idempotency_ttl_hours": 24,
    "backup_enabled": True, "backup_time": "19:15", "backup_keep_daily": 7, "backup_keep_weekly": 4,
    "backup_dir": "atip_data/backups", "monitor_enabled": True, "monitor_minutes": 15, "json_logs": True,
    "migrate_every_connection": False, "features": {},
    # W31 (OPS-06): encrypted off-site backup copies + weekly restore drill
    "backup_offsite_dir": None, "backup_offsite_plaintext": False, "backup_offsite_keep": 14,
    "restore_drill_enabled": True, "restore_drill_time": "10:00",
    # W9: shared state for multi-process deployments ("memory" | "redis://..."), scheduler leader lease
    "shared_state_url": "memory", "scheduler_leader_lease_s": 180,
}
SECRET_WORDS = ("token", "secret", "password", "passwd", "api_key", "apikey", "private", "pin", "key_id",
                "client_secret", "access_key")


def environment() -> str:
    env = (os.environ.get("ATIP_ENV") or _read(CONFIG_PATH).get("environment") or "development").strip().lower()
    return env if env in ENVIRONMENTS else "development"


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge(a, b):
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _env_overrides() -> dict:
    out = {}
    for k, v in os.environ.items():
        if not k.startswith("ATIP__"):
            continue
        parts = [p.lower() for p in k[6:].split("__") if p]
        try:
            val = json.loads(v)
        except Exception:
            val = v
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if parts:
            cur[parts[-1]] = val
    return out


def load() -> dict:
    env = environment()
    cfg = _merge(_read(CONFIG_PATH), _read(CONFIG_PATH.with_name(f"config.{env}.json")))
    cfg = _merge(cfg, _env_overrides())
    cfg["environment"] = env
    return cfg


def ops() -> dict:
    return _merge(OPS_DEFAULTS, load().get("ops") or {})


def feature(name: str, default: bool = False) -> bool:
    v = (ops().get("features") or {}).get(name)
    return default if v is None else v is True


def is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(w in k for w in SECRET_WORDS)


def masked(cfg, _path=""):
    """A copy with every secret-looking key's value replaced by its presence marker."""
    if isinstance(cfg, dict):
        return {k: ("<set>" if v not in (None, "") else "<empty>") if is_secret_key(k) and not isinstance(v, (dict, list))
                else masked(v, f"{_path}.{k}") for k, v in cfg.items()}
    if isinstance(cfg, list):
        return [masked(v, _path) for v in cfg]
    return cfg


def validate(cfg: dict | None = None) -> list:
    cfg = cfg or load()
    env = cfg.get("environment", "development")
    o = _merge(OPS_DEFAULTS, cfg.get("ops") or {})
    ex = cfg.get("execution") or {}
    ent = cfg.get("enterprise") or {}
    out = []

    def add(level, key, msg):
        out.append({"level": level, "key": key, "message": msg})

    # trading safety (any environment)
    if ex.get("live_trading_enabled") is True and env != "production":
        add("error", "execution.live_trading_enabled", f"live trading is enabled in {env}; only production may")
    if str(cfg.get("broker_env", "PAPER")).upper() == "LIVE" and env != "production":
        add("error", "broker_env", f"broker_env LIVE in {env}; W1 order rules would reach the real broker")
    if ex.get("live_trading_enabled") is True:
        add("warning", "execution.live_trading_enabled", "live gate opened by config; W4's Dhan adapter still refuses")
    host = str(cfg.get("dashboard_host", "127.0.0.1"))
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not o.get("tls_enabled"):
        add("error" if env == "production" else "warning", "dashboard_host",
            f"dashboard bound to {host} without TLS in front (ops.tls_enabled false)")
    # types
    for k, t in (("max_request_bytes", int), ("rate_limit_per_minute", int), ("backup_keep_daily", int),
                 ("backup_keep_weekly", int), ("monitor_minutes", int), ("idempotency_ttl_hours", int)):
        if not isinstance(o.get(k), t) or isinstance(o.get(k), bool) or o.get(k) < 0:
            add("error", f"ops.{k}", f"must be a non-negative integer (got {o.get(k)!r})")
    if not isinstance(o.get("cors_origins"), list):
        add("error", "ops.cors_origins", "must be a list of origins")
    elif "*" in o["cors_origins"]:
        add("error" if env == "production" else "warning", "ops.cors_origins", "wildcard CORS origin")
    # production requirements
    if env == "production":
        if ent.get("enabled") is not True:
            add("error", "enterprise.enabled", "production requires sign-in (enterprise.enabled true)")
        if ent.get("accept_legacy_token") is True:
            add("error", "enterprise.accept_legacy_token", "the shared dashboard token must not act as admin")
        if not o.get("backup_enabled"):
            add("error", "ops.backup_enabled", "production requires scheduled backups")
        if not o.get("rate_limit_enabled"):
            add("warning", "ops.rate_limit_enabled", "rate limiting is off")
        from ops.secrets import status as secret_status
        if secret_status("ATIP_ENCRYPTION_KEY")["present"] is False:
            add("error", "ATIP_ENCRYPTION_KEY", "production requires an encryption key (python -m ops keygen)")
    # required for the data platform
    # W9 SaaS: nothing may reach real people / money outside production
    saas = cfg.get("saas") or {}
    email_mode = str(((saas.get("notifications") or {}).get("email") or {}).get("mode") or "sandbox").lower()
    tg_mode = str(((saas.get("notifications") or {}).get("telegram") or {}).get("mode") or "sandbox").lower()
    pay = str((saas.get("payments") or {}).get("provider") or "sandbox").lower()
    if email_mode not in ("sandbox", "smtp"):
        add("error", "saas.notifications.email.mode", "must be sandbox or smtp")
    elif email_mode == "smtp" and env != "production":
        add("error", "saas.notifications.email.mode", f"real e-mail (smtp) in {env}; only production may send")
    if tg_mode not in ("sandbox", "live"):
        add("error", "saas.notifications.telegram.mode", "must be sandbox or live")
    elif tg_mode == "live" and env != "production":
        add("error", "saas.notifications.telegram.mode", f"live per-user Telegram in {env}; only production may send")
    if pay not in ("sandbox", "noop", "razorpay", "stripe"):
        add("error", "saas.payments.provider", "must be sandbox, noop, razorpay or stripe")
    elif pay not in ("sandbox", "noop"):
        add("error" if env != "production" else "warning", "saas.payments.provider",
            f"{pay} is not enabled in W9 (the adapter refuses); real charges need an owner decision")
    import importlib.util as _ilu
    if str(o.get("shared_state_url") or "memory").startswith("redis://") and not _ilu.find_spec("redis"):
        add("error", "ops.shared_state_url", "redis:// configured but the redis package is not installed")
    try:
        from db.backend import backend as _db_backend
        if _db_backend() == "postgresql":
            add("error", "DATABASE_URL", "the ATIP runtime runs on SQLite in W9; PostgreSQL is for "
                                         "tools/sqlite_to_postgres.py only (docs/POSTGRESQL_MIGRATION.md)")
    except ValueError as e:
        add("error", "DATABASE_URL", str(e))
    from ops.secrets import status as _secret_status
    if not cfg.get("dhan_client_id") and not _secret_status("DHAN_CLIENT_ID")["present"]:
        add("warning", "dhan_client_id", "Dhan client id missing: market data jobs will fail")
    return out


def fingerprint(cfg: dict | None = None) -> str:
    return hashlib.sha256(json.dumps(masked(cfg or load()), sort_keys=True, default=str).encode()).hexdigest()


def _flat(d, p=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{p}.{k}" if p else k
        if isinstance(v, dict):
            out.update(_flat(v, key))
        else:
            out[key] = v
    return out


def record_version(conn) -> dict:
    """Store the effective config fingerprint; audit changed key NAMES (and non-secret values)."""
    cfg = load()
    m = masked(cfg)
    h = fingerprint(cfg)
    prev = conn.execute("SELECT fingerprint, config_json FROM ops_config_version ORDER BY id DESC LIMIT 1").fetchone()
    if prev and prev[0] == h:
        return {"fingerprint": h, "changed": False}
    old = _flat(json.loads(prev[1])) if prev else {}
    new = _flat(m)
    changes = {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
               "changed": sorted(k for k in set(new) & set(old) if new[k] != old[k])}
    conn.execute("INSERT INTO ops_config_version (fingerprint,environment,config_json,changes_json,recorded_at) "
                 "VALUES (?,?,?,?,?)", (h, cfg["environment"], json.dumps(m, default=str), json.dumps(changes),
                                        datetime.now()))
    try:
        from enterprise.audit import record
        record(conn, "config.change", actor="startup", resource="config", details=changes, commit=False)
    except Exception:
        pass
    conn.commit()
    return {"fingerprint": h, "changed": True, "changes": changes}
