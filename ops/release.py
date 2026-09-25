"""
Release engineering (W9): preflight, manifest + configuration backup, deployment record,
post-deployment check. Used by deploy/deploy_release.ps1 and runnable by hand:

    python -m ops release preflight <RELEASE_ID>    checks; exit 1 on any FAIL
    python -m ops release manifest  <RELEASE_ID>    atip_data/releases/<id>/manifest.json and
                                                    a copy of atip_data/config.json + .env (local
                                                    only: atip_data/ is gitignored)
    python -m ops release postcheck [--port 8000]   health / scheduler / trading-safety after a start
    python -m ops release record <RELEASE_ID> <DEPLOYED|ROLLED_BACK|FAILED> [note]
                                                    appends atip_data/releases/history.jsonl

Preflight never writes to the production database: it reads it through a read-only
SQLite connection. Nothing here restarts ATIP, pushes, or changes configuration.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

RELEASES = Path("atip_data") / "releases"
IGNORED_UNTRACKED = (".xlsx",)


def _git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def _ro_db():
    from db.schema import DB_PATH
    return sqlite3.connect(f"file:{Path(DB_PATH).resolve().as_posix()}?mode=ro", uri=True)


def _lock_versions() -> dict:
    from importlib import metadata
    out = {}
    p = Path("requirements.lock.txt")
    if not p.exists():
        return {"_error": "requirements.lock.txt missing"}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, want = line.split("==", 1)
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        out[name] = {"locked": want, "installed": have, "match": have == want}
    return out


def preflight(release_id: str) -> list:
    checks = []

    def add(name, status, detail=""):
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. git
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        head = _git("rev-parse", "--short", "HEAD")
        dirty = [line for line in _git("status", "--porcelain").splitlines()
                 if not (line.startswith("??") and line.strip().strip('"').endswith(IGNORED_UNTRACKED))]
        add("git branch", "PASS" if branch == "master" else "FAIL", f"{branch} @ {head}")
        add("git working tree", "PASS" if not dirty else "FAIL",
            "clean (untracked owner spreadsheets ignored)" if not dirty else f"{len(dirty)} change(s): {dirty[:5]}")
        try:
            tag_commit = _git("rev-list", "-n", "1", release_id)[:len(head)]
            add("release tag", "PASS" if tag_commit == head else "FAIL",
                f"{release_id} -> {tag_commit}" + ("" if tag_commit == head else f" but HEAD is {head}"))
        except subprocess.CalledProcessError:
            add("release tag", "FAIL", f"tag {release_id} not found")
    except Exception as e:
        add("git", "FAIL", f"git unavailable: {e}")
    # 2. configuration + secrets
    try:
        from ops.config import environment, validate
        f = validate()
        errs = [x for x in f if x["level"] == "error"]
        add("configuration", "FAIL" if errs else "PASS",
            f"environment {environment()}; {len(errs)} error(s), {len(f) - len(errs)} warning(s)"
            + (f": {[e['key'] for e in errs]}" if errs else ""))
    except Exception as e:
        add("configuration", "FAIL", f"unreadable: {e}")
    try:
        from ops.secrets import CATALOG, status, validate as sv
        present = sum(1 for n in CATALOG if status(n)["present"])
        legacy = [x for x in sv() if "config.json" in x["message"]]
        add("secrets", "WARN" if legacy else "PASS",
            f"{present}/{len(CATALOG)} catalogued secrets present; {len(legacy)} still in config.json plaintext")
    except Exception as e:
        add("secrets", "FAIL", f"unreadable: {e}")
    # 3. database (read-only) + migrations
    try:
        from ops.migrations import MIGRATIONS
        c = _ro_db()
        try:
            ic = c.execute("PRAGMA quick_check").fetchone()[0]
            applied = {r[0] for r in c.execute("SELECT version FROM schema_migrations WHERE status='APPLIED'")}
            pending = [v for v, *_ in MIGRATIONS if v not in applied]
            add("database integrity (quick_check)", "PASS" if ic == "ok" else "FAIL", ic)
            add("migrations", "PASS", f"applied {sorted(applied)}; pending {pending} (applied at start)"
                if pending else f"all {len(applied)} applied")
            b = c.execute("SELECT backup_id, finished_at, kind FROM ops_backup WHERE status='VERIFIED' ORDER BY "
                          "finished_at DESC LIMIT 1").fetchone()
        finally:
            c.close()
        if b and datetime.now() - datetime.fromisoformat(str(b[1])[:19]) < timedelta(hours=2):
            add("pre-deployment backup", "PASS", f"{b[0]} ({b[2]}) at {str(b[1])[:19]}")
        else:
            add("pre-deployment backup", "FAIL", "no VERIFIED backup in the last 2 h -- run: python -m ops backup "
                                                  "--kind pre-release")
    except Exception as e:
        add("database", "FAIL", f"{type(e).__name__}: {e}")
    # 4. trading safety
    try:
        from ops.trading_safety import report
        r = report()
        add("LIVE_TRADING_ENABLED", "PASS" if not r["LIVE_TRADING_ENABLED"] else "FAIL",
            f"{r['LIVE_TRADING_ENABLED']} (mode {r.get('execution_mode')}, broker_env {r.get('w1_broker_env')}, "
            f"{r['master_switch']['reason']})")
    except Exception as e:
        add("LIVE_TRADING_ENABLED", "FAIL", f"cannot be determined -- fail closed ({e})")
    # 5. dependencies
    lv = _lock_versions()
    if "_error" in lv:
        add("dependency lock", "FAIL", lv["_error"])
    else:
        bad = {k: v for k, v in lv.items() if not v["match"]}
        add("dependency lock", "PASS" if not bad else "WARN",
            f"{len(lv)} pinned; mismatches: {bad}" if bad else f"{len(lv)} pinned packages match the environment")
    return checks


def manifest(release_id: str) -> dict:
    from ops.config import environment, fingerprint
    from ops.migrations import MIGRATIONS
    from ops.trading_safety import report
    d = RELEASES / release_id
    d.mkdir(parents=True, exist_ok=True)
    cfg_src = Path("atip_data") / "config.json"
    saved = []
    for src, name in ((cfg_src, "config.json"), (Path(".env"), ".env")):
        if src.exists():
            shutil.copy2(src, d / f"{name}.backup")
            saved.append(str(d / f"{name}.backup"))
    c = _ro_db()
    try:
        b = c.execute("SELECT backup_id, finished_at, sha256 FROM ops_backup WHERE status='VERIFIED' ORDER BY "
                      "finished_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    m = {"release_id": release_id, "created_at": datetime.now().isoformat(timespec="seconds"),
         "commit": _git("rev-parse", "HEAD"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
         "python": sys.version.split()[0], "environment": environment(), "config_fingerprint": fingerprint(),
         "migrations_in_code": [v for v, *_ in MIGRATIONS],
         "dependencies": {k: v["installed"] for k, v in _lock_versions().items() if k != "_error"},
         "database_backup": {"backup_id": b[0], "finished_at": str(b[1])[:19], "sha256": b[2]} if b else None,
         "configuration_backup": saved,
         "trading_safety": {k: v for k, v in report().items() if k in ("LIVE_TRADING_ENABLED", "execution_mode",
                                                                       "w1_broker_env", "live_gate_open")}}
    (d / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    return m


def postcheck(port=8000, wait_s=0) -> list:
    import time
    import urllib.request
    checks = []

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
            return r.status, json.loads(r.read().decode())

    deadline = time.time() + wait_s
    while True:
        try:
            code, body = get("/health/ready")
            break
        except Exception as e:
            if time.time() >= deadline:
                return [{"check": "ATIP answers", "status": "FAIL", "detail": str(e)}]
            time.sleep(5)
    checks.append({"check": "/health/ready", "status": "PASS" if body.get("status") == "READY" else "FAIL",
                   "detail": body})
    for comp in ("database", "scheduler", "broker", "storage", "market_data"):
        try:
            code, body = get(f"/health/{comp}")
            st = body.get("status")
            checks.append({"check": f"/health/{comp}", "status": "PASS" if st == "READY" else
                           ("WARN" if st == "DEGRADED" else "FAIL"), "detail": {k: v for k, v in body.items()
                                                                                 if k != "sources"}})
        except Exception as e:
            checks.append({"check": f"/health/{comp}", "status": "FAIL", "detail": str(e)})
    try:
        code, b = get("/health/broker")
        live = bool(b.get("live_trading_enabled")) or bool(b.get("live_gate_open"))
        from ops.trading_safety import report
        live = live or report()["LIVE_TRADING_ENABLED"]
        checks.append({"check": "LIVE_TRADING_ENABLED", "status": "FAIL" if live else "PASS",
                       "detail": f"{live}; execution mode {b.get('execution_mode')}"})
    except Exception as e:
        checks.append({"check": "LIVE_TRADING_ENABLED", "status": "FAIL", "detail": str(e)})
    log = Path("atip_data") / "atip.log"
    if log.exists():
        tail = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
        startup = [i for i, l in enumerate(tail) if "Starting ATIP" in l or "ATIP environment:" in l]
        seg = tail[startup[-1]:] if startup else tail
        bad = [l for l in seg if " ERROR " in l or "CRITICAL" in l or "Traceback" in l]
        checks.append({"check": "log since start", "status": "PASS" if not bad else "WARN",
                       "detail": f"{len(bad)} ERROR/CRITICAL line(s)" + (f": {bad[:3]}" if bad else "")})
    return checks


def record(release_id, result, note="") -> dict:
    RELEASES.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now().isoformat(timespec="seconds"), "release_id": release_id, "result": result,
           "commit": _git("rev-parse", "HEAD"), "note": note}
    with open(RELEASES / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ops release")
    ap.add_argument("action", choices=["preflight", "manifest", "postcheck", "record"])
    ap.add_argument("release_id", nargs="?")
    ap.add_argument("result", nargs="?")
    ap.add_argument("note", nargs="?", default="")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--wait", type=int, default=0)
    a = ap.parse_args(argv)
    if a.action == "preflight":
        c = preflight(a.release_id or "")
        print(json.dumps(c, indent=2, default=str))
        return 1 if any(x["status"] == "FAIL" for x in c) else 0
    if a.action == "manifest":
        print(json.dumps(manifest(a.release_id), indent=2, default=str))
        return 0
    if a.action == "postcheck":
        c = postcheck(a.port, a.wait)
        print(json.dumps(c, indent=2, default=str))
        return 1 if any(x["status"] == "FAIL" for x in c) else 0
    if a.action == "record":
        if a.result not in ("DEPLOYED", "ROLLED_BACK", "FAILED"):
            print("result must be DEPLOYED, ROLLED_BACK or FAILED")
            return 2
        print(json.dumps(record(a.release_id, a.result, a.note)))
        return 0
    return 2
