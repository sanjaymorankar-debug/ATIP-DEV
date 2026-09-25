"""
Startup sequence (called by main.py for the long-running modes, after the
single-instance lock):

  1. logging: secret-masking filter on every handler + JSON log atip_data/atip.jsonl
  2. environment (ATIP_ENV / config "environment" / development)
  3. configuration validation -- in PRODUCTION an error refuses startup
     (StartupRefused); elsewhere errors and warnings are logged and ATIP starts
  4. secrets validation (presence / legacy plaintext; values never logged)
  5. trading-safety report (LIVE_TRADING_ENABLED must read FALSE unless deliberately
     configured for production)
  6. database: additive migrations (db/schema.py) + versioned migrations
     (ops/migrations.py) applied and recorded; a failed migration is logged loudly
  7. configuration version recorded (ops_config_version) + audit of changed keys
Each step is independent: a failure in one is logged and the next still runs.
"""

from __future__ import annotations

import logging

log = logging.getLogger("atip.ops.startup")


class StartupRefused(RuntimeError):
    pass


def run() -> dict:
    out = {}
    from ops.config import ops, environment, validate
    try:
        from ops.logs import install
        out["logging"] = install(json_logs=bool(ops().get("json_logs", True)))
    except Exception as e:
        log.warning(f"  structured logging not installed: {e}")
    env = environment()
    out["environment"] = env
    log.info(f"  ATIP environment: {env}")

    findings = validate()
    for f in findings:
        (log.error if f["level"] == "error" else log.warning)(f"  config {f['level']}: {f['key']}: {f['message']}")
    errors = [f for f in findings if f["level"] == "error"]
    if errors and env == "production":
        raise StartupRefused("; ".join(f"{f['key']}: {f['message']}" for f in errors))
    out["config_findings"] = len(findings)

    try:
        from ops.secrets import validate as sv
        for f in sv():
            (log.error if f["level"] == "error" else log.warning)(f"  secrets {f['level']}: {f['message']}")
    except Exception as e:
        log.warning(f"  secrets validation failed: {e}")

    try:
        from ops.trading_safety import assert_safe
        out["trading_safety"] = assert_safe()
    except Exception as e:
        log.warning(f"  trading safety report failed: {e}")

    try:
        from db.schema import get_connection
        from ops.migrations import apply, validate as mv
        c = get_connection()
        try:
            res = apply(c)
            out["migrations"] = res
            for f in mv(c):
                (log.error if f["level"] == "error" else log.warning)(f"  migrations: {f['message']}")
            from ops.config import record_version
            out["config_version"] = record_version(c)
        finally:
            c.close()
    except Exception as e:
        log.error(f"  migrations / config version failed: {e}")
    return out
