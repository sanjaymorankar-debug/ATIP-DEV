"""
Metrics: an in-process registry (no new dependency) with Prometheus text exposition.

Recorded as they happen:
    atip_http_requests_total{method,route,status}        (HTTP middleware)
    atip_http_request_duration_seconds{method,route}     histogram
    atip_job_runs_total{job,status}                      (pipeline run_job)
    atip_job_duration_seconds{job}                       histogram
    atip_circuit_state{dependency}                       0 closed / 1 half-open / 2 open
    atip_webhook_deliveries_total{status}
Collected from the database at scrape time (collect()):
    atip_db_latency_seconds, atip_data_age_days{source}, atip_risk_decisions_today{status},
    atip_orders_today{status}, atip_ml_predictions_today, atip_backup_age_hours,
    atip_scheduler_heartbeat_age_seconds, atip_alerts_firing, atip_quant_scores_latest_rows
Exposed at GET /api/ops/metrics (system:operate when the enterprise layer is on).
Per-process: a restart resets counters (the database-backed series do not).
"""

from __future__ import annotations

import threading
import time

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)
_lock = threading.Lock()
_counters, _hist, _gauges = {}, {}, {}


def _k(name, labels):
    return name, tuple(sorted((labels or {}).items()))


def inc(name, labels=None, value=1.0):
    with _lock:
        k = _k(name, labels)
        _counters[k] = _counters.get(k, 0.0) + value


def observe(name, value, labels=None):
    with _lock:
        k = _k(name, labels)
        h = _hist.setdefault(k, {"buckets": [0] * len(BUCKETS), "sum": 0.0, "count": 0})
        for i, b in enumerate(BUCKETS):
            if value <= b:
                h["buckets"][i] += 1
        h["sum"] += value
        h["count"] += 1


def gauge(name, value, labels=None):
    with _lock:
        _gauges[_k(name, labels)] = value


class timer:
    def __init__(self, name, labels=None):
        self.name, self.labels = name, labels

    def __enter__(self):
        self.t = time.perf_counter()
        return self

    def __exit__(self, *a):
        observe(self.name, time.perf_counter() - self.t, self.labels)


def _lbl(labels, extra=None):
    items = list(labels) + list((extra or {}).items())
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in items) + "}"


def collect(conn):
    """Database-backed gauges, refreshed at scrape time. Each probe is independent."""
    from datetime import date, datetime

    def q(sql, *a):
        try:
            return conn.execute(sql, a).fetchall()
        except Exception:
            return []
    t = time.perf_counter()
    q("SELECT 1")
    gauge("atip_db_latency_seconds", time.perf_counter() - t)
    today = str(date.today())
    try:
        from ops.data_health import sources
        for s in sources(conn):
            if s["age_days"] is not None:
                gauge("atip_data_age_days", s["age_days"], {"source": s["source"]})
    except Exception:
        pass
    for st, n in q("SELECT risk_status, COUNT(*) FROM risk_decision WHERE DATE(created_at)=? GROUP BY 1", today):
        gauge("atip_risk_decisions_today", n, {"status": st})
    for st, n in q("SELECT status, COUNT(*) FROM oms_order WHERE DATE(created_at)=? GROUP BY 1", today):
        gauge("atip_orders_today", n, {"status": st})
    r = q("SELECT COUNT(*) FROM ml_prediction WHERE DATE(created_at)=?", today)
    if r:
        gauge("atip_ml_predictions_today", r[0][0])
    r = q("SELECT MAX(finished_at) FROM ops_backup WHERE status='VERIFIED'")
    if r and r[0][0]:
        gauge("atip_backup_age_hours", (datetime.now() - datetime.fromisoformat(str(r[0][0])[:19])).total_seconds() / 3600)
    r = q("SELECT MAX(beat_at) FROM ops_heartbeat WHERE component='scheduler'")
    if r and r[0][0]:
        gauge("atip_scheduler_heartbeat_age_seconds",
              (datetime.now() - datetime.fromisoformat(str(r[0][0])[:19])).total_seconds())
    r = q("SELECT COUNT(*) FROM ops_alert WHERE status='FIRING'")
    if r:
        gauge("atip_alerts_firing", r[0][0])
    try:
        from ops.resilience import breakers
        for name, b in breakers().items():
            gauge("atip_circuit_state", {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}[b.state], {"dependency": name})
    except Exception:
        pass


def exposition() -> str:
    lines = []
    with _lock:
        for (name, labels), v in sorted(_counters.items()):
            lines.append(f"{name}{_lbl(labels)} {v}")
        for (name, labels), v in sorted(_gauges.items()):
            lines.append(f"{name}{_lbl(labels)} {v}")
        for (name, labels), h in sorted(_hist.items()):
            for b, c in zip(BUCKETS, h["buckets"]):
                lines.append(f"{name}_bucket{_lbl(labels, {'le': b})} {c}")
            lines.append(f"{name}_bucket{_lbl(labels, {'le': '+Inf'})} {h['count']}")
            lines.append(f"{name}_sum{_lbl(labels)} {h['sum']}")
            lines.append(f"{name}_count{_lbl(labels)} {h['count']}")
    return "\n".join(lines) + "\n"
