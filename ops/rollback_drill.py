"""
Executed rollback drill (OPS-11), W31 -- on a scratch clone, never on production.

deploy/rollback_release.ps1 stops EVERY python process running main.py on the machine
(AtipProcesses matches by command line, and production runs as a bare `python main.py`),
so it cannot be rehearsed next to a live ATIP. This drill performs the same procedure on
an isolated copy and controls only the process it started:

    1  clone this repository to <temp>/atip-rollback-drill-<ts> (git clone --no-hardlinks)
       and check out FROM (the release being rolled back)
    2  database: the newest VERIFIED backup restored into the clone (ops.backup.restore --
       verified, never the live file); with no backup, an online copy of the live DB
    3  start the clone dashboard-only (`python main.py --dashboard --port <port>`,
       ATIP_DB_PATH = the clone's DB, ATIP_INSTANCE_NAME = drill so the single-instance
       mutex does not collide with production, ATIP_ENV = test). Dashboard-only on purpose:
       no scheduler jobs, no Dhan WebSocket next to production's
    4  postcheck (ops/release.postcheck) -- the "deployed" state must be healthy
    5  ROLLBACK, timed: stop the clone's process (by PID), `git reset --hard TO`, re-restore
       the database from the backup (the database-rollback step), start, postcheck again
    6  record ops_rollback_drill (status, measured RTO seconds, both postchecks) and
       ops/release history; stop the process; delete the clone (keep=True keeps it)

PASS needs both postchecks clean apart from /health/scheduler, which is expected to be
down in a dashboard-only drill (reported as EXPECTED), and LIVE_TRADING_ENABLED false.

    python -m ops rollback-drill --from HEAD --to ATIP-W24-RC1 [--port 8078] [--keep]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

PYTHON = sys.executable


def _git(*a, cwd=None):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _db_into(dest: Path) -> dict:
    from db.schema import DB_PATH, get_connection
    dest.parent.mkdir(parents=True, exist_ok=True)
    c = get_connection()
    try:
        r = c.execute("SELECT backup_id, path FROM ops_backup WHERE status='VERIFIED' AND pruned_at IS NULL ORDER BY "
                      "finished_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    bpath = None
    if r:
        for cand in (Path(r[1]), Path(DB_PATH).resolve().parent.parent / r[1]):   # paths are stored relative
            if cand.exists():                                                   # to the install folder
                bpath = cand
                break
    if bpath:
        from ops.backup import restore
        if dest.exists():
            dest.unlink()
        out = restore(str(bpath), str(dest))
        return {"source": f"backup {r[0]}", "verified": out["verified"]}
    src = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return {"source": "online copy of the live database (no VERIFIED backup)", "verified": None}


PROD_MUTEX = "ATIP_SingleInstance_D_Projects_ATIP"
DRILL_MUTEX = "ATIP_RollbackDrill"


def _isolate(clone: Path) -> bool:
    """Releases before W31 hard-code the production single-instance mutex in main.py, so the
    clone would refuse to start beside production (or, if production were down, would hold
    its name). In the SCRATCH CLONE only, rename it -- recorded in the drill details. The
    repository is never edited."""
    m = clone / "main.py"
    t = m.read_text(encoding="utf-8")
    if "ATIP_INSTANCE_NAME" in t or PROD_MUTEX not in t:
        return False
    m.write_text(t.replace(PROD_MUTEX, DRILL_MUTEX), encoding="utf-8")
    return True


def _start(clone: Path, port: int, db: Path):
    env = {**os.environ, "ATIP_DB_PATH": str(db), "ATIP_INSTANCE_NAME": "ATIP_RollbackDrill", "ATIP_ENV": "test"}
    log = open(clone / "drill_dashboard.log", "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen([PYTHON, "main.py", "--dashboard", "--port", str(port)], cwd=clone, env=env,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=flags)


def _stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def _postcheck(clone: Path, port: int, db: Path, wait=150) -> dict:
    env = {**os.environ, "ATIP_DB_PATH": str(db), "ATIP_ENV": "test"}
    r = subprocess.run([PYTHON, "-m", "ops", "release", "postcheck", "--port", str(port), "--wait", str(wait)],
                       cwd=clone, env=env, capture_output=True, text=True, timeout=wait + 120)
    try:
        checks = json.loads(r.stdout)
    except ValueError:
        checks = [{"check": "postcheck output", "status": "FAIL", "detail": (r.stdout or r.stderr)[-500:]}]
    for c in checks:
        if c["check"] == "/health/scheduler" and c["status"] == "FAIL":
            c["status"] = "EXPECTED"          # dashboard-only drill: no scheduler by design
    ok = not any(c["status"] == "FAIL" for c in checks)
    return {"ok": ok, "checks": checks}


def run(from_ref: str = "HEAD", to_ref: str | None = None, port: int = 8078, keep: bool = False) -> dict:
    from db.schema import get_connection
    repo = Path(_git("rev-parse", "--show-toplevel"))
    if not to_ref:
        tags = [t for t in _git("tag", "--list", "ATIP-*", "--sort=-creatordate", cwd=repo).splitlines() if t]
        to_ref = tags[0] if tags else "HEAD~1"
    did = "RB" + uuid.uuid4().hex[:12].upper()
    started = datetime.now()
    clone = Path(tempfile.gettempdir()) / f"atip-rollback-drill-{started:%Y%m%d-%H%M%S}"
    res = {"drill_id": did, "from": from_ref, "to": to_ref, "clone": str(clone), "status": "FAILED"}
    proc = None
    try:
        if Path(clone).resolve().is_relative_to(repo.resolve()):
            raise RuntimeError("the drill clone must be outside the repository")
        _git("clone", "--quiet", "--no-hardlinks", str(repo), str(clone))
        from_sha = _git("rev-parse", from_ref, cwd=repo)
        to_sha = _git("rev-parse", f"{to_ref}^{{commit}}", cwd=repo)
        _git("checkout", "--quiet", from_sha, cwd=clone)
        res["harness_patches"] = ["main.py mutex renamed (from)"] if _isolate(clone) else []
        res.update(from_sha=from_sha[:10], to_sha=to_sha[:10])
        db = clone / "atip_data" / "atip.db"
        res["database"] = _db_into(db)
        proc = _start(clone, port, db)
        res["deployed_check"] = _postcheck(clone, port, db)
        t0 = time.monotonic()                              # ---- rollback, timed ----
        _stop(proc)
        _git("reset", "--quiet", "--hard", to_sha, cwd=clone)
        if _isolate(clone):
            res["harness_patches"].append("main.py mutex renamed (to)")
        res["database_rollback"] = _db_into(db)
        proc = _start(clone, port, db)
        res["rolled_back_check"] = _postcheck(clone, port, db)
        res["rto_seconds"] = round(time.monotonic() - t0, 1)
        res["status"] = "PASSED" if res["deployed_check"]["ok"] and res["rolled_back_check"]["ok"] else "FAILED"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        _stop(proc)
        if not keep:
            shutil.rmtree(clone, ignore_errors=True)
    c = get_connection()
    try:
        c.execute("INSERT INTO ops_rollback_drill (drill_id,from_ref,to_ref,started_at,finished_at,rto_seconds,status,"
                  "details_json) VALUES (?,?,?,?,?,?,?,?)", (did, from_ref, to_ref, started, datetime.now(),
                                                             res.get("rto_seconds"), res["status"],
                                                             json.dumps(res, default=str)))
        c.commit()
    finally:
        c.close()
    try:
        from ops.release import record
        record(f"DRILL {from_ref}->{to_ref}", "ROLLED_BACK" if res["status"] == "PASSED" else "FAILED",
               f"rollback drill {did} on a scratch clone; RTO {res.get('rto_seconds')} s")
    except Exception:
        pass
    return res
