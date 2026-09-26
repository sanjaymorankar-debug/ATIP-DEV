"""
Backup and restore (SQLite).

backup(kind="manual"|"scheduled"|"pre-release"):
  1. online copy with the SQLite backup API (consistent while ATIP keeps writing;
     WAL content included) to atip_data/backups/atip-<kind>-<YYYYmmdd-HHMMSS>.db
  2. VERIFICATION of the copy: PRAGMA integrity_check must return "ok", key tables
     must be readable (row counts recorded), sha256 of the file recorded
  3. ops_backup row: status VERIFIED only when every check passed, else FAILED (and
     the file is kept for inspection, named *.failed)
A backup is never reported as good unless step 2 ran and passed.

Retention (prune): keeps the newest ops.backup_keep_daily (7) VERIFIED backups plus
one per ISO week for ops.backup_keep_weekly (4) weeks. It deletes ONLY files this
module created (atip-*.db inside the backup directory, recorded in ops_backup) --
never the hand-made atip.db.bak-* snapshots in atip_data.

restore(backup_id or path, target): verifies the source, then copies it to a NEW
file (default atip_data/restore/atip-restored-<ts>.db). It never overwrites the live
database: switching is a documented manual step with ATIP stopped
(docs/BACKUP_RESTORE.md).

RPO: 24 h with the daily scheduled backup (plus the pre-release backups).
RTO: ~10 minutes (stop ATIP, swap the file, start ATIP) -- see docs/DISASTER_RECOVERY.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger("atip.ops.backup")
KEY_TABLES = ("prices_daily", "ai_scores", "market_health", "pipeline_log", "strategy", "oms_order", "risk_decision",
              "ml_model", "enterprise_user", "enterprise_audit",
              # W11-W20 wealth track: the owner's own records, counted when present
              "investor_profile_version", "wealth_holding", "wealth_goal", "perf_ledger")


def _dir() -> Path:
    from ops.config import ops
    d = Path(ops().get("backup_dir") or "atip_data/backups")
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: Path) -> dict:
    """integrity_check + key-table row counts + sha256. {"ok", "integrity", "tables", "sha256", "size_bytes"}."""
    path = Path(path)
    out = {"ok": False, "integrity": None, "tables": {}, "sha256": None, "size_bytes": None}
    if not path.exists():
        out["error"] = "file missing"
        return out
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in KEY_TABLES:
                if t in names:
                    out["tables"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        finally:
            c.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["sha256"] = sha256(path)
    out["size_bytes"] = path.stat().st_size
    out["ok"] = out["integrity"] == "ok" and "prices_daily" in out["tables"]
    if not out["ok"] and "error" not in out:
        out["error"] = "integrity_check failed" if out["integrity"] != "ok" else "prices_daily missing"
    return out


def backup(kind="manual") -> dict:
    from db.schema import DB_PATH, get_connection
    started = datetime.now()
    bid = f"atip-{kind}-{started:%Y%m%d-%H%M%S}"
    dest = _dir() / f"{bid}.db"
    c = get_connection()
    try:
        c.execute("INSERT INTO ops_backup (backup_id,kind,path,started_at,status) VALUES (?,?,?,?,?)",
                  (bid, kind, str(dest), started, "RUNNING"))
        c.commit()
    finally:
        c.close()
    res = {"backup_id": bid, "kind": kind, "path": str(dest), "status": "FAILED"}
    try:
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst, pages=4096, sleep=0.05)
        finally:
            dst.close()
            src.close()
        v = verify(dest)
        res.update({k: v.get(k) for k in ("integrity", "tables", "sha256", "size_bytes")})
        if v["ok"]:
            res["status"] = "VERIFIED"
        else:
            res["error"] = v.get("error")
            failed = dest.with_suffix(".db.failed")
            dest.replace(failed)
            res["path"] = str(failed)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    c = get_connection()
    try:
        c.execute("UPDATE ops_backup SET path=?, finished_at=?, size_bytes=?, sha256=?, integrity=?, tables_json=?, "
                  "status=?, error=? WHERE backup_id=?",
                  (res["path"], datetime.now(), res.get("size_bytes"), res.get("sha256"), res.get("integrity"),
                   json.dumps(res.get("tables") or {}), res["status"], res.get("error"), bid))
        c.commit()
    finally:
        c.close()
    (log.info if res["status"] == "VERIFIED" else log.error)(
        f"  backup {bid}: {res['status']}" + (f" ({res.get('error')})" if res.get("error") else ""))
    return res


def prune() -> list:
    """Retention. Returns the backup ids pruned."""
    from db.schema import get_connection
    from ops.config import ops
    cfg = ops()
    keep_daily, keep_weekly = int(cfg.get("backup_keep_daily") or 7), int(cfg.get("backup_keep_weekly") or 4)
    base = _dir().resolve()
    c = get_connection()
    try:
        rows = c.execute("SELECT backup_id, path, finished_at FROM ops_backup WHERE status='VERIFIED' AND pruned_at "
                         "IS NULL ORDER BY finished_at DESC").fetchall()
        keep = {r[0] for r in rows[:keep_daily]}
        weeks = {}
        for r in rows:
            wk = datetime.fromisoformat(str(r[2])[:19]).isocalendar()[:2]
            if wk not in weeks and len(weeks) < keep_weekly:
                weeks[wk] = r[0]
        keep |= set(weeks.values())
        pruned = []
        for bid, path, _ in rows:
            if bid in keep:
                continue
            p = Path(path).resolve()
            if p.parent != base or not p.name.startswith("atip-") or not p.name.endswith(".db"):
                continue                       # only files this module created
            p.unlink(missing_ok=True)
            c.execute("UPDATE ops_backup SET pruned_at=? WHERE backup_id=?", (datetime.now(), bid))
            pruned.append(bid)
        c.commit()
        return pruned
    finally:
        c.close()


def run_scheduled_backup() -> dict:
    """Scheduler job: verified backup, then retention (only after a VERIFIED backup)."""
    res = backup("scheduled")
    if res["status"] != "VERIFIED":
        return {"status": "FAILED", "error": res.get("error") or "backup verification failed", "rows": 0}
    pruned = prune()
    return {"status": "SUCCESS", "rows": 1, "backup_id": res["backup_id"], "pruned": pruned}


def list_backups(limit=50) -> list:
    from db.schema import get_connection
    c = get_connection()
    try:
        return [dict(r) for r in c.execute(
            "SELECT backup_id, kind, started_at, finished_at, size_bytes, sha256, integrity, status, error, pruned_at "
            "FROM ops_backup ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()]
    finally:
        c.close()


def restore(source: str, target: str | None = None) -> dict:
    """Verify `source` (a backup id or a path) and copy it to a NEW file. Never touches the live DB."""
    from db.schema import DB_PATH, get_connection
    path = Path(source)
    if not path.exists():
        c = get_connection()
        try:
            r = c.execute("SELECT path FROM ops_backup WHERE backup_id=?", (source,)).fetchone()
        finally:
            c.close()
        if not r:
            raise FileNotFoundError(f"no backup {source}")
        path = Path(r[0])
    v = verify(path)
    if not v["ok"]:
        raise RuntimeError(f"source failed verification: {v.get('error')}")
    tgt = Path(target) if target else Path("atip_data/restore") / f"atip-restored-{datetime.now():%Y%m%d-%H%M%S}.db"
    if tgt.resolve() == Path(DB_PATH).resolve():
        raise RuntimeError("restore never overwrites the live database; stop ATIP and swap files manually")
    if tgt.exists():
        raise FileExistsError(f"{tgt} exists")
    tgt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, tgt)
    v2 = verify(tgt)
    return {"restored_to": str(tgt), "source": str(path), "verified": v2["ok"], "sha256_match": v2["sha256"] == v["sha256"],
            "tables": v2["tables"]}
