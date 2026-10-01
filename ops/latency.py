"""
Latency telemetry (W34: EX-15) for the data and order paths.

    with timed("order.submit_adapter", order_id=...):   time a block
    record("feed.tick_to_flush", ms)                      record a measured interval

Samples go into an in-memory buffer (thread-safe, bounded to MAX_BUFFER per stage) and to
the W8 metrics histogram atip_latency_ms{stage=...}; nothing is written to SQLite on the hot
path. flush() -- every few minutes from the scheduler, and on demand from the API -- rolls
the buffer up per stage per minute (n, p50, p95, p99, max, mean) into latency_rollup.
The buffer is per process: the scheduler and the dashboard share one process under the
normal `python main.py`, so both see the same samples.

Stages instrumented in W34:
    data.live_quotes          data/dhan.fetch_live_quotes (REST round trip, all symbols)
    feed.tick_to_flush        data/dhan_ws: age of the newest tick when the feed flushes to the DB
    risk.evaluate             execution/pipeline: one intent through the risk engine
    order.decision_to_submit  intent created -> order submitted (queueing in ATIP itself)
    order.submit_adapter      order_manager.submit_order: the adapter call
    order.submit_to_fill      submitted -> first fill (from the event bus, EX-16)
    algo.tick                 one execution-algo scheduler tick (EX-11)
    events.dispatch           one event-bus drain (EX-16)

summary(conn, minutes) -> per stage over the window, from the rollup plus the unflushed buffer.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta

MAX_BUFFER = 5000
_lock = threading.Lock()
_buf = defaultdict(lambda: deque(maxlen=MAX_BUFFER))      # stage -> deque[(epoch_s, ms)]


def record(stage: str, ms: float):
    if ms is None or ms < 0:
        return
    with _lock:
        _buf[stage].append((time.time(), float(ms)))
    try:
        from ops import metrics
        metrics.observe("atip_latency_ms", float(ms), {"stage": stage})
    except Exception:
        pass


@contextmanager
def timed(stage: str, **_detail):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        record(stage, (time.perf_counter() - t0) * 1000.0)


def since_ms(t) -> float | None:
    """Milliseconds from a datetime / ISO string to now (for queueing latencies)."""
    if t is None:
        return None
    if not isinstance(t, datetime):
        try:
            t = datetime.fromisoformat(str(t)[:26].replace(" ", "T"))
        except ValueError:
            return None
    return (datetime.now() - t).total_seconds() * 1000.0


def _q(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return round(xs[i], 3)


def _stats(vals):
    return {"n": len(vals), "p50_ms": _q(vals, 0.5), "p95_ms": _q(vals, 0.95), "p99_ms": _q(vals, 0.99),
            "max_ms": round(max(vals), 3) if vals else None,
            "mean_ms": round(sum(vals) / len(vals), 3) if vals else None}


def _drain() -> dict:
    with _lock:
        out = {k: list(v) for k, v in _buf.items() if v}
        for v in _buf.values():
            v.clear()
    return out


def flush(conn=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        data = _drain()
        n = 0
        for stage, samples in data.items():
            by_min = defaultdict(list)
            for ts, ms in samples:
                by_min[datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:00")].append(ms)
            for minute, vals in by_min.items():
                old = conn.execute("SELECT n, mean_ms, max_ms FROM latency_rollup WHERE stage=? AND minute=?",
                                   (stage, minute)).fetchone()
                s = _stats(vals)
                if old and old[0]:            # a minute flushed twice: merge counts, keep the new percentiles
                    tot = old[0] + s["n"]
                    s["mean_ms"] = round((old[0] * (old[1] or 0) + s["n"] * s["mean_ms"]) / tot, 3)
                    s["max_ms"] = max(old[2] or 0, s["max_ms"])
                    s["n"] = tot
                conn.execute("INSERT OR REPLACE INTO latency_rollup (stage,minute,n,p50_ms,p95_ms,p99_ms,max_ms,mean_ms) "
                             "VALUES (?,?,?,?,?,?,?,?)", (stage, minute, s["n"], s["p50_ms"], s["p95_ms"],
                                                         s["p99_ms"], s["max_ms"], s["mean_ms"]))
                n += 1
        conn.commit()
        return {"status": "SUCCESS", "rows": n, "stages": len(data)}
    finally:
        if own:
            conn.close()


def summary(conn, minutes: int = 60 * 24) -> dict:
    """Per stage: the rollup rows in the window (n-weighted mean, max of p95/p99/max -- percentiles of
    different minutes cannot be merged exactly, so the worst minute is reported) + unflushed samples."""
    since = (datetime.now() - timedelta(minutes=int(minutes))).strftime("%Y-%m-%d %H:%M:00")
    out = {}
    for stage, n, mean, p50, p95, p99, mx in conn.execute(
            "SELECT stage, SUM(n), SUM(n*mean_ms)/SUM(n), MAX(p50_ms), MAX(p95_ms), MAX(p99_ms), MAX(max_ms) "
            "FROM latency_rollup WHERE minute>=? GROUP BY stage", (since,)):
        out[stage] = {"n": n, "mean_ms": round(mean or 0, 3), "worst_minute_p50_ms": p50,
                      "worst_minute_p95_ms": p95, "worst_minute_p99_ms": p99, "max_ms": mx}
    with _lock:
        live = {k: [ms for _, ms in v] for k, v in _buf.items() if v}
    for stage, vals in live.items():
        out.setdefault(stage, {})["unflushed"] = _stats(vals)
    return {"window_minutes": minutes, "stages": out}


def series(conn, stage: str, minutes: int = 60 * 24) -> list:
    since = (datetime.now() - timedelta(minutes=int(minutes))).strftime("%Y-%m-%d %H:%M:00")
    return [dict(r) for r in conn.execute("SELECT minute, n, p50_ms, p95_ms, p99_ms, max_ms FROM latency_rollup "
                                          "WHERE stage=? AND minute>=? ORDER BY minute", (stage, since))]


def run_scheduled() -> dict:
    return flush()
