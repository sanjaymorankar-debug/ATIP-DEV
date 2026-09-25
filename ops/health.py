"""
Health checks.

    GET /health            overall: the worst of the component statuses (+ each component's status)
    GET /health/live       LIVE while the process answers (no dependencies checked)
    GET /health/ready      READY when the database answers and migrations are applied;
                           FAILED otherwise (HTTP 503)
    GET /health/database   latency, journal mode, file size, migrations
    GET /health/broker     execution mode, live gate (must be closed), broker credential
                           PRESENCE (never values), last portfolio sync, circuit breakers
    GET /health/data       per-source freshness (ops/data_health.py) + latest data-quality score
    GET /health/scheduler  heartbeat age, job locks, failed jobs (24 h)
    GET /health/ml         ML layer enabled?, active models, predictions today, drift alerts
    GET /health/storage    (W9) disk free, backup directory, latest verified backup age
    GET /health/market_data (W9) live quote / index feed freshness in market hours, last broker
                           data job, credential presence -- no broker call is made
    (no queue exists in ATIP, so there is no queue component)

Status values: LIVE, READY, DEGRADED, FAILED. HTTP 200 for LIVE / READY / DEGRADED,
503 for FAILED (so a load balancer / uptime probe can act on the code alone).
Nothing sensitive is returned: no paths outside atip_data, no tokens, no config
values beyond modes and switches. The endpoints are public (authz PUBLIC) because
they are what an uptime probe calls; ATIP itself is bound to 127.0.0.1.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

LIVE, READY, DEGRADED, FAILED = "LIVE", "READY", "DEGRADED", "FAILED"
_ORDER = {LIVE: 0, READY: 0, DEGRADED: 1, FAILED: 2}
STARTED = datetime.now()


def worst(*statuses) -> str:
    s = [x for x in statuses if x]
    return max(s, key=lambda x: _ORDER[x]) if s else READY


def _conn():
    from db.schema import get_connection
    return get_connection()


def live() -> dict:
    import os
    return {"status": LIVE, "pid": os.getpid(), "started_at": STARTED.isoformat(timespec="seconds"),
            "uptime_s": int((datetime.now() - STARTED).total_seconds())}


def database() -> dict:
    t = time.perf_counter()
    try:
        c = _conn()
    except Exception as e:
        return {"status": FAILED, "error": f"connect failed: {type(e).__name__}"}
    try:
        c.execute("SELECT 1").fetchone()
        latency = round((time.perf_counter() - t) * 1000, 1)
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        from db.schema import DB_PATH
        size = Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else None
        wal = Path(str(DB_PATH) + "-wal")
        from ops.migrations import status as mig_status
        mig = mig_status(c)
        st = READY
        if mig["pending"] or mig["failed"]:
            st = DEGRADED
        if latency > 2000:
            st = worst(st, DEGRADED)
        return {"status": st, "latency_ms": latency, "journal_mode": mode, "size_mb": round((size or 0) / 1e6, 1),
                "wal_mb": round(wal.stat().st_size / 1e6, 1) if wal.exists() else 0,
                "migrations": {"applied": mig["applied"], "pending": mig["pending"], "failed": mig["failed"]}}
    except Exception as e:
        return {"status": FAILED, "error": type(e).__name__}
    finally:
        c.close()


def ready() -> dict:
    db = database()
    st = READY if db["status"] in (READY, DEGRADED) and not (db.get("migrations") or {}).get("failed") else FAILED
    return {"status": st, "database": db["status"]}


def broker() -> dict:
    out, st = {}, READY
    try:
        from execution.config import execution_settings, live_gate
        s = execution_settings()
        allowed, reason = live_gate()
        out.update({"execution_mode": s["mode"], "live_trading_enabled": s["live_trading_enabled"],
                    "live_gate_open": allowed, "live_gate_reason": reason})
        if allowed:
            st = DEGRADED            # not an error in production, but it must be visible
    except Exception as e:
        out["execution"] = f"unavailable: {type(e).__name__}"
        st = DEGRADED
    try:
        from ops.secrets import status as sec_status
        out["credentials_present"] = {k: sec_status(k)["present"] for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN")}
        if not all(out["credentials_present"].values()):
            st = worst(st, DEGRADED)
    except Exception:
        pass
    c = None
    try:
        c = _conn()
        r = c.execute("SELECT date, source, status, SUBSTR(error,1,120) FROM portfolio_sync ORDER BY id DESC LIMIT 1"
                      ).fetchone()
        if r:
            out["last_portfolio_sync"] = {"date": str(r[0]), "source": r[1], "status": r[2],
                                          "error": ("DH-901 token expired" if r[3] and "DH-901" in r[3] else
                                                    ("error" if r[3] else None))}
            if r[2] != "SUCCESS":
                st = worst(st, DEGRADED)
    except Exception:
        pass
    finally:
        if c:
            c.close()
    from ops.resilience import breakers
    out["circuits"] = {k: b.state for k, b in breakers().items()}
    if any(v == "OPEN" for v in out["circuits"].values()):
        st = worst(st, DEGRADED)
    out["status"] = st
    return out


def data() -> dict:
    from ops.data_health import quality, sources
    c = _conn()
    try:
        src = sources(c)
        q = [r for r in quality(c, 1) if r.get("check_name") == "DQS"]
    finally:
        c.close()
    st = READY
    for s in src:
        if s["status"] == "STALE":
            st = worst(st, DEGRADED)
        if s["status"] == "MISSING" and s["source"] in ("prices_daily", "ai_scores"):
            st = FAILED
    return {"status": st, "sources": src, "data_quality_score": q[0] if q else None}


def scheduler() -> dict:
    from ops.jobs import heartbeat_age, locks
    c = _conn()
    try:
        age = heartbeat_age(c)
        failed = c.execute("SELECT job_name, MAX(start_time) FROM pipeline_log WHERE kind='run' AND status='FAILED' "
                           "AND start_time >= ? GROUP BY job_name", (datetime.now() - timedelta(hours=24),)).fetchall()
        lk = locks(c)
    finally:
        c.close()
    if age is None:
        st = DEGRADED                  # no heartbeat recorded yet (scheduler started before W8, or not running)
    elif age > 300:
        st = FAILED
    else:
        st = READY
    if failed:
        st = worst(st, DEGRADED)
    return {"status": st, "heartbeat_age_s": None if age is None else int(age),
            "failed_jobs_24h": [{"job": r[0], "last": str(r[1])[:19]} for r in failed],
            "locks": [{"job": x["job"], "since": str(x["acquired_at"])[:19]} for x in lk]}


def ml() -> dict:
    try:
        from ml.config import settings
        enabled = settings()["enabled"]
    except Exception:
        enabled = False
    c = _conn()
    try:
        def one(sql, *a):
            try:
                return c.execute(sql, a).fetchone()[0]
            except Exception:
                return None
        active = one("SELECT COUNT(*) FROM ml_model WHERE status='ACTIVE'")
        today = one("SELECT COUNT(*) FROM ml_prediction WHERE DATE(created_at)=?", str(date.today()))
        drift = one("SELECT COUNT(*) FROM ml_model_monitoring WHERE status IN ('ALERT','DRIFT') AND "
                    "DATE(created_at) >= DATE('now','-7 days')")
    finally:
        c.close()
    st = READY
    if enabled and not active:
        st = DEGRADED
    if drift:
        st = worst(st, DEGRADED)
    return {"status": st, "enabled": enabled, "active_models": active, "predictions_today": today,
            "drift_alerts_7d": drift}


def storage() -> dict:
    """W9: disk space on the database volume, database / WAL size, backup directory and
    the latest verified backup (no paths beyond atip_data are returned)."""
    import shutil
    from db.schema import DB_PATH
    free = shutil.disk_usage(str(Path(DB_PATH).resolve().parent)).free
    st = READY if free >= 5 * 1024 ** 3 else (DEGRADED if free >= 1024 ** 3 else FAILED)
    out = {"free_gb": round(free / 1024 ** 3, 1)}
    try:
        from ops.config import ops as _ops
        bdir = Path(_ops().get("backup_dir") or "atip_data/backups")
        out["backup_dir_exists"] = bdir.exists()
    except Exception:
        pass
    c = _conn()
    try:
        r = c.execute("SELECT MAX(finished_at) FROM ops_backup WHERE status='VERIFIED'").fetchone()
        last = r[0] if r else None
        out["last_verified_backup"] = str(last)[:19] if last else None
        if last:
            age_h = (datetime.now() - datetime.fromisoformat(str(last)[:19])).total_seconds() / 3600
            out["backup_age_hours"] = round(age_h, 1)
            if age_h > 26:
                st = worst(st, DEGRADED)
        else:
            st = worst(st, DEGRADED)                  # no verified backup yet: say so
        f = c.execute("SELECT status, error FROM ops_backup WHERE status IN ('VERIFIED','FAILED') ORDER BY "
                      "started_at DESC LIMIT 1").fetchone()
        if f and f[0] == "FAILED":
            out["latest_backup"] = "FAILED"
            st = worst(st, DEGRADED)
    finally:
        c.close()
    out["status"] = st
    return out


def market_data() -> dict:
    """W9: market-data connectivity as ATIP can observe it without calling the broker:
    the live quote / index feed freshness during market hours, the last successful broker
    data job, and Dhan credential presence (never values). Outside market hours the feed
    is expected to be idle and is reported as such."""
    from ops.data_health import _is_session
    now = datetime.now()
    in_session = _is_session(now.date()) and (9 * 60 + 15) <= now.hour * 60 + now.minute <= 15 * 60 + 30
    out = {"market_open": in_session}
    st = READY
    c = _conn()
    try:
        def age_min(sql):
            try:
                v = c.execute(sql).fetchone()[0]
            except Exception:
                return None
            if not v:
                return None
            t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:19].replace("T", " "))
            return round((now - t).total_seconds() / 60, 1)
        out["live_quotes_age_min"] = age_min("SELECT MAX(timestamp) FROM live_quotes")
        out["index_levels_age_min"] = age_min("SELECT MAX(date || ' ' || COALESCE(time,'00:00:00')) FROM index_levels")
        r = c.execute("SELECT job_name, MAX(start_time) FROM pipeline_log WHERE kind='run' AND status='SUCCESS' AND "
                      "job_name LIKE 'dhan%' GROUP BY job_name ORDER BY 2 DESC LIMIT 1").fetchone()
        out["last_successful_broker_data_job"] = {"job": r[0], "at": str(r[1])[:19]} if r else None
    finally:
        c.close()
    if in_session:
        q = out["live_quotes_age_min"]
        if q is None or q > 30:
            st = DEGRADED
            out["feed"] = "STALE during market hours"
        else:
            out["feed"] = "LIVE"
    else:
        out["feed"] = "IDLE (market closed)"
    try:
        from ops.secrets import status as sec_status
        out["credentials_present"] = sec_status("DHAN_ACCESS_TOKEN")["present"]
        if not out["credentials_present"]:
            st = worst(st, DEGRADED)
    except Exception:
        pass
    out["status"] = st
    return out


COMPONENTS = {"database": database, "broker": broker, "data": data, "scheduler": scheduler, "ml": ml,
              "storage": storage, "market_data": market_data}


def component(name) -> dict:
    try:
        return COMPONENTS[name]()
    except Exception as e:
        return {"status": FAILED, "error": type(e).__name__}


def overall() -> dict:
    comps = {n: component(n) for n in COMPONENTS}
    st = worst(*(v["status"] for v in comps.values()))
    return {"status": st, "checked_at": datetime.now().isoformat(timespec="seconds"),
            "components": {n: v["status"] for n, v in comps.items()}}


def http_status(payload) -> int:
    return 503 if payload.get("status") == FAILED else 200
