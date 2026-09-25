"""
Monitoring rules and alert state (scheduler job ops_monitor, every ops.monitor_minutes).

Each rule returns (firing: bool, severity, message). State lives in ops_alert (one
row per rule): FIRING / RESOLVED, first/last seen, count. A rule notifies when it
STARTS firing and again at most every 6 hours while it keeps firing, and once when it
resolves -- no alert storms. Delivery is the W1 channel: alerts.telegram.notify(
category="ops") -> alert_log (dashboard) + Telegram when a bot is configured. No
other external service is contacted.

Rules:
  database          SELECT 1 fails or takes > 2 s
  data_stale        a market-data source is STALE / MISSING (ops/data_health.py)
  scheduler         no scheduler heartbeat for > 5 minutes (only once one was recorded)
  job_failures      pipeline.health.check_job_health problems (overdue / failed jobs)
  error_rate        >= 5% of API requests 5xx in this process (min 50 requests)
  auth_failures     >= 20 denied / failed sign-ins in the last hour (enterprise_audit)
  failed_orders     REJECTED / FAILED W4 orders today
  risk_failures     the execution_cycle job (intents -> risk engine -> orders) FAILED today
  ml_failures       failed ML training runs or ml_predictions job runs today
  disk_space        < 2 GB free on the database volume
  backup            no VERIFIED backup in 26 h, or the latest backup FAILED
  live_trading      the live gate is open (critical)
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime, timedelta

log = logging.getLogger("atip.ops.monitor")
RENOTIFY = timedelta(hours=6)


def _q(conn, sql, *a):
    try:
        return conn.execute(sql, a).fetchall()
    except Exception:
        return None


def r_database(conn):
    import time
    t = time.perf_counter()
    ok = _q(conn, "SELECT 1") is not None
    ms = (time.perf_counter() - t) * 1000
    return (not ok or ms > 2000), "critical", f"database {'unavailable' if not ok else f'slow ({ms:.0f} ms)'}"


def r_data_stale(conn):
    from ops.data_health import sources
    bad = [f"{s['source']} {s['status']} ({s['sessions_behind']} sessions)" for s in sources(conn)
           if s["status"] != "OK"]
    return bool(bad), "warning", "stale market data: " + ", ".join(bad)


def r_scheduler(conn):
    from ops.jobs import heartbeat_age
    age = heartbeat_age(conn)
    return (age is not None and age > 300), "critical", f"scheduler heartbeat {int(age or 0)} s old"


def r_job_failures(conn):
    from pipeline.health import check_job_health
    probs = check_job_health(conn)
    msgs = [f"{p.label} {p.kind}"[:120] for p in probs]
    return bool(probs), "warning", f"{len(probs)} job problem(s): " + "; ".join(msgs[:5])


def r_error_rate(conn):
    from ops import metrics
    total = err = 0
    with metrics._lock:
        for (name, labels), v in metrics._counters.items():
            if name == "atip_http_requests_total":
                total += v
                if str(dict(labels).get("status", "")).startswith("5"):
                    err += v
    rate = err / total if total else 0
    return (total >= 50 and rate >= 0.05), "warning", f"API 5xx rate {rate:.1%} ({int(err)}/{int(total)})"


def r_auth_failures(conn):
    r = _q(conn, "SELECT COUNT(*) FROM enterprise_audit WHERE at >= ? AND (action LIKE 'auth.%fail%' OR "
                 "action LIKE '%denied%' OR status_code IN (401,403))", datetime.now() - timedelta(hours=1))
    n = r[0][0] if r else 0
    return n >= 20, "warning", f"{n} failed / denied authentication events in the last hour"


def r_failed_orders(conn):
    r = _q(conn, "SELECT COUNT(*) FROM oms_order WHERE status IN ('REJECTED','FAILED') AND DATE(created_at)=?",
           str(date.today()))
    n = r[0][0] if r else 0
    return n > 0, "warning", f"{n} rejected / failed order(s) today"


def r_risk_failures(conn):
    """The execution cycle (strategy intents -> risk engine -> orders) failed today."""
    r = _q(conn, "SELECT COUNT(*) FROM pipeline_log WHERE kind='run' AND job_name='execution_cycle' AND "
                 "status='FAILED' AND run_date=?", str(date.today()))
    n = r[0][0] if r else 0
    return n > 0, "warning", f"execution / risk cycle failed {n} time(s) today"


def r_ml_failures(conn):
    t = _q(conn, "SELECT COUNT(*) FROM ml_training_run WHERE status='FAILED' AND DATE(started_at)=?",
           str(date.today()))
    p = _q(conn, "SELECT COUNT(*) FROM pipeline_log WHERE kind='run' AND job_name='ml_predictions' AND "
                 "status='FAILED' AND run_date=?", str(date.today()))
    n = (t[0][0] if t else 0) + (p[0][0] if p else 0)
    return n > 0, "warning", f"{n} failed ML training / prediction run(s) today"


def r_disk_space(conn):
    from db.schema import DB_PATH
    free = shutil.disk_usage(str(DB_PATH.resolve().parent)).free
    return free < 2 * 1024 ** 3, "critical", f"{free / 1024 ** 3:.1f} GB free on the database volume"


def r_backup(conn):
    from ops.config import ops
    if not ops().get("backup_enabled", True):
        return False, "info", "backups disabled"
    last = _q(conn, "SELECT status, finished_at, error FROM ops_backup WHERE status IN ('VERIFIED','FAILED') "
                    "ORDER BY started_at DESC LIMIT 1")
    ok = _q(conn, "SELECT MAX(finished_at) FROM ops_backup WHERE status='VERIFIED'")
    if last and last[0][0] == "FAILED":
        return True, "critical", f"latest backup FAILED: {last[0][2]}"
    if ok and ok[0][0]:
        age = datetime.now() - datetime.fromisoformat(str(ok[0][0])[:19])
        return age > timedelta(hours=26), "warning", f"latest verified backup is {age.total_seconds() / 3600:.0f} h old"
    first = _q(conn, "SELECT MIN(beat_at) FROM ops_heartbeat")
    since = datetime.fromisoformat(str(first[0][0])[:19]) if first and first[0][0] else datetime.now()
    return datetime.now() - since > timedelta(hours=26), "warning", "no verified backup yet"


def r_live_trading(conn):
    from execution.config import live_gate
    allowed, reason = live_gate()
    return allowed, "critical", f"LIVE TRADING GATE OPEN: {reason}"


RULES = {"database": r_database, "data_stale": r_data_stale, "scheduler": r_scheduler,
         "job_failures": r_job_failures, "error_rate": r_error_rate, "auth_failures": r_auth_failures,
         "failed_orders": r_failed_orders, "risk_failures": r_risk_failures, "ml_failures": r_ml_failures,
         "disk_space": r_disk_space, "backup": r_backup, "live_trading": r_live_trading}


def _notify(rule, severity, message):
    try:
        from alerts.telegram import notify
        blk = datetime.now().hour // 6            # alert_log dedupes a key per day; our own state throttles to 6 h
        notify(f"[ATIP ops] {message}", category="ops", severity=severity, key=f"ops:{rule}:{severity}:{blk}")
    except Exception as e:
        log.warning(f"  ops alert {rule} not delivered: {e}")


def evaluate(conn, notify=True) -> list:
    now = datetime.now()
    out = []
    for name, rule in RULES.items():
        try:
            firing, sev, msg = rule(conn)
        except Exception as e:
            firing, sev, msg = False, "info", f"rule error: {type(e).__name__}"
        prev = conn.execute("SELECT status, notified_at FROM ops_alert WHERE rule=?", (name,)).fetchone()
        if firing:
            send = not prev or prev[0] != "FIRING" or not prev[1] or \
                now - datetime.fromisoformat(str(prev[1])[:19]) > RENOTIFY
            conn.execute("INSERT INTO ops_alert (rule,status,severity,message,first_at,last_at,count,notified_at) "
                         "VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(rule) DO UPDATE SET status='FIRING', "
                         "severity=excluded.severity, message=excluded.message, last_at=excluded.last_at, "
                         "first_at=CASE WHEN ops_alert.status='FIRING' THEN ops_alert.first_at ELSE excluded.first_at END,"
                         " count=ops_alert.count+1, resolved_at=NULL, "
                         "notified_at=COALESCE(excluded.notified_at, ops_alert.notified_at)",
                         (name, "FIRING", sev, msg, now, now, now if (send and notify) else None))
            if send and notify:
                _notify(name, sev, msg)
        elif prev and prev[0] == "FIRING":
            conn.execute("UPDATE ops_alert SET status='RESOLVED', resolved_at=?, last_at=? WHERE rule=?",
                         (now, now, name))
            if notify:
                _notify(name, "info", f"resolved: {name}")
        out.append({"rule": name, "firing": firing, "severity": sev, "message": msg})
    conn.commit()
    return out


def run_monitor() -> dict:
    from db.schema import get_connection
    c = get_connection()
    try:
        res = evaluate(c)
    finally:
        c.close()
    firing = [r["rule"] for r in res if r["firing"]]
    return {"status": "SUCCESS", "rows": len(res), "firing": firing}


def alerts(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM ops_alert ORDER BY status, last_at DESC").fetchall()]
