"""
Wealth-track health (W20): used by /health/wealth (ops/health.py COMPONENTS), the
W8 monitor rule `wealth_cycle`, and `python -m wealth health`.

READY      tables present and, when the scheduled cycle is on (wealth.enabled), the
           latest cycle for every profiled owner succeeded within WINDOW_DAYS
DEGRADED   the scheduled cycle is on and a cycle is missing / stale / PARTIAL, or the
           append-only migration (0005) is not applied, or market inputs are stale
FAILED     a wealth table is missing (the additive schema did not run)

The track is optional: with wealth.enabled false and the tables present it is READY,
so it never degrades ATIP's overall health when nobody uses it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from wealth import common as C
from wealth.config import settings

WINDOW_DAYS = 3


def _age_days(v):
    if not v:
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:19])
    return (datetime.now() - d).total_seconds() / 86400


def check(conn) -> dict:
    from db.schema import WEALTH_TABLES
    missing = [t for t in WEALTH_TABLES if not C.table_exists(conn, t)]
    enabled = settings()["enabled"]
    out = {"enabled": enabled, "missing_tables": missing}
    if missing:
        return {**out, "status": "FAILED", "reason": f"missing tables {missing}"}
    try:
        mig = conn.execute("SELECT status FROM schema_migrations WHERE version='0005'").fetchone()
        out["append_only_migration"] = mig[0] if mig else "PENDING"
    except Exception:
        out["append_only_migration"] = "UNKNOWN"
    owners = conn.execute("SELECT COUNT(*) FROM investor_profile").fetchone()[0]
    out["profiled_owners"] = owners
    problems = []
    if enabled and owners:
        rows = conn.execute("SELECT p.tenant_id, p.owner_id, (SELECT status FROM wealth_cycle_run c WHERE "
                            "c.tenant_id=p.tenant_id AND c.owner_id=p.owner_id ORDER BY created_at DESC LIMIT 1), "
                            "(SELECT MAX(created_at) FROM wealth_cycle_run c WHERE c.tenant_id=p.tenant_id AND "
                            "c.owner_id=p.owner_id) FROM investor_profile p").fetchall()
        for t, o, st, at in rows:
            if t == "uat":
                continue
            age = _age_days(at)
            if age is None or age > WINDOW_DAYS:
                problems.append(f"{t}:{o} no investor cycle in {WINDOW_DAYS} days")
            elif st != "SUCCESS":
                problems.append(f"{t}:{o} latest cycle {st}")
    if out["append_only_migration"] not in ("APPLIED", "UNKNOWN"):
        problems.append("migration 0005 (append-only wealth audit tables) not applied")
    r = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol='NIFTY50'").fetchone()
    last_px = str(r[0])[:10] if r and r[0] else None
    out["benchmark_prices_as_of"] = last_px
    if enabled and (not last_px or (date.today() - date.fromisoformat(last_px)).days > 5):
        problems.append("NIFTY50 prices older than 5 days (performance and allocation inputs stale)")
    out["advisor_narration"] = settings()["advisor_llm_enabled"]
    out["uat_owner_active"] = bool(settings().get("uat_owner"))
    if out["uat_owner_active"]:
        problems.append("wealth.uat_owner is set: the UI shows a UAT persona, not the owner's data")
    out["problems"] = problems
    out["status"] = "DEGRADED" if problems else "READY"
    return out


def component() -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        return check(conn)
    finally:
        conn.close()


def recent_cycle_problem(conn) -> tuple[bool, str]:
    """For the W8 monitor: (firing, message)."""
    if not settings()["enabled"]:
        return False, "wealth cycle disabled"
    d = check(conn)
    cyc = [p for p in d.get("problems", []) if "cycle" in p]
    return bool(cyc), "; ".join(cyc) or "wealth cycle healthy"
