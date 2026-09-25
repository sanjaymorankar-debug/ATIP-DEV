"""
Scheduler hardening: job locks, heartbeats, job status.

Job locks (ops_job_lock): run_job() in pipeline/scheduler.py acquires a lock named
after the job before running it. A second runner of the same job -- a second ATIP
process, a CLI invocation, a manual catch-up overlapping the scheduled run -- sees
the lock and SKIPS (pipeline_log status SKIPPED, reason "locked"). A lock expires
after ttl seconds (default 6 h) or when its owner process is gone, so a crash never
blocks a job forever. Owner = "<host>:<pid>:<thread>".

Heartbeat (ops_heartbeat): the scheduler loop beats every 60 s ("scheduler");
/health/scheduler and the monitor treat > 5 minutes without a beat as FAILED.
"""

from __future__ import annotations

import os
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

DEFAULT_TTL = 6 * 3600
_last_beat = {}


def owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259                                          # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire(conn, job, ttl=DEFAULT_TTL) -> bool:
    now = datetime.now()
    me = owner()
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT owner, pid, expires_at FROM ops_job_lock WHERE job=?", (job,)).fetchone()
        if r:
            exp = r[2] if isinstance(r[2], datetime) else datetime.fromisoformat(str(r[2])[:26])
            if r[0] != me and exp > now and _pid_alive(r[1]):   # held by a live runner (any process / thread)
                conn.execute("ROLLBACK")
                return False
        conn.execute("INSERT INTO ops_job_lock (job,owner,pid,acquired_at,heartbeat_at,expires_at) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(job) DO UPDATE SET owner=excluded.owner, pid=excluded.pid, "
                     "acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, "
                     "expires_at=excluded.expires_at", (job, me, os.getpid(), now, now, now + timedelta(seconds=ttl)))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release(conn, job):
    conn.execute("DELETE FROM ops_job_lock WHERE job=? AND owner=?", (job, owner()))
    conn.commit()


@contextmanager
def job_lock(job, ttl=DEFAULT_TTL):
    """Yields True when this runner holds the lock, False when another runner does.
    Lock storage failing (database unavailable) yields True: the lock is protection
    against duplicates, not a gate that can stop the pipeline."""
    from db.schema import get_connection
    held = True
    try:
        c = get_connection()
        try:
            held = acquire(c, job, ttl)
        finally:
            c.close()
    except Exception:
        held, failed = True, True
    else:
        failed = False
    try:
        yield held
    finally:
        if held and not failed:
            try:
                c = get_connection()
                try:
                    release(c, job)
                finally:
                    c.close()
            except Exception:
                pass


def locks(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT job, owner, pid, acquired_at, expires_at FROM ops_job_lock "
                                          "ORDER BY acquired_at").fetchall()]


def beat(component="scheduler", detail=None, every=60):
    now = datetime.now()
    if component in _last_beat and (now - _last_beat[component]).total_seconds() < every:
        return
    _last_beat[component] = now
    try:
        from db.schema import get_connection
        c = get_connection()
        try:
            c.execute("INSERT INTO ops_heartbeat (component,beat_at,pid,detail) VALUES (?,?,?,?) ON CONFLICT(component) "
                      "DO UPDATE SET beat_at=excluded.beat_at, pid=excluded.pid, detail=excluded.detail",
                      (component, now, os.getpid(), detail))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


def heartbeat_age(conn, component="scheduler") -> float | None:
    try:
        r = conn.execute("SELECT beat_at FROM ops_heartbeat WHERE component=?", (component,)).fetchone()
    except Exception:
        return None
    if not r or not r[0]:
        return None
    t = r[0] if isinstance(r[0], datetime) else datetime.fromisoformat(str(r[0])[:26])
    return (datetime.now() - t).total_seconds()


def scheduled() -> list:
    """The in-process schedule (next runs); empty when this process runs no scheduler."""
    try:
        import schedule
    except ImportError:
        return []
    out = []
    for j in schedule.get_jobs():
        fn = getattr(j.job_func, "args", None)
        name = fn[0] if fn and isinstance(fn[0], str) else getattr(j.job_func, "__name__", str(j.job_func))[:60]
        out.append({"job": name, "next_run": str(j.next_run)[:19] if j.next_run else None,
                    "interval": f"{j.interval} {j.unit}", "at": str(j.at_time) if j.at_time else None})
    return sorted(out, key=lambda x: x["next_run"] or "")


def recent_runs(conn, hours=24) -> list:
    try:
        return [dict(r) for r in conn.execute(
            "SELECT job_name, status, start_time, end_time, duration_s, SUBSTR(error_msg,1,200) AS error FROM "
            "pipeline_log WHERE kind='run' AND start_time >= ? ORDER BY start_time DESC LIMIT 200",
            (datetime.now() - timedelta(hours=hours),)).fetchall()]
    except Exception:
        return []
