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

W31 (OPS-06):
  ENCRYPTION   encrypt_file(): AES-256-GCM in 4 MiB chunks with the ATIP data key
               (ops/crypto.py); each chunk's associated data binds it to the file header and
               its index, so chunks cannot be reordered, dropped or spliced. A .sha256 sidecar
               covers the encrypted file. decrypt_file() reverses it.
  OFF-SITE     offsite_copy(backup): with ops.backup_offsite_dir set (a second drive, a NAS
               share, a synced cloud folder), the VERIFIED backup is encrypted and the .enc +
               sidecar copied there, then re-hashed at the destination. Without a data key it
               is NOT copied in plaintext unless ops.backup_offsite_plaintext is true.
               Off-site retention: ops.backup_offsite_keep (14) newest copies.
  DRILL        restore_drill(): the newest VERIFIED backup -- from its OFF-SITE encrypted copy
               when one exists (that proves the off-site copy restores) -- is decrypted /
               copied into a drill file, integrity-checked, its key-table counts compared with
               those recorded at backup time, timed (the measured RTO of the data step), then
               deleted. Recorded in ops_restore_drill; a failure alerts. Weekly (scheduler,
               Sunday 10:00).
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


# ── PostgreSQL runtime (17 Nov 2026) ───────────────────────────────────────────────────────────────
# When ATIP runs on PostgreSQL (db.schema.pg_runtime_url()), a backup is a pg_dump custom-format
# archive (atip-<kind>-<ts>.dump) instead of a copy of the SQLite file. verify() reads the archive back
# with pg_restore (it must list, and its key tables' data are counted row by row from the archive
# itself); the restore drill restores it into a scratch database, counts the key tables there and
# drops it. Nothing here writes to the live database except the ops_backup / ops_restore_drill rows.

def _pg_url():
    from db.schema import pg_runtime_url
    return pg_runtime_url()


def _pg_bin(name: str) -> str:
    """pg_dump / pg_restore / psql: ops.pg_bin_dir when set, else the newest /usr/lib/postgresql/N/bin, else PATH."""
    from ops.config import ops
    d = ops().get("pg_bin_dir")
    if d:
        return str(Path(d) / name)
    found = sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{name}"),
                   key=lambda p: int(p.parent.parent.name) if p.parent.parent.name.isdigit() else 0)
    return str(found[-1]) if found else (shutil.which(name) or name)


def _run(args, **kw):
    import subprocess
    r = subprocess.run(args, capture_output=True, text=kw.pop("text", True), **kw)
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"{Path(args[0]).name} failed: {err.strip()[:400]}")
    return r


def _dump_counts(path: Path) -> dict:
    """Row counts of KEY_TABLES read from the archive's own data (COPY blocks), not from the live database."""
    import subprocess
    toc = _run([_pg_bin("pg_restore"), "-l", str(path)]).stdout
    present = [t for t in KEY_TABLES if f" TABLE DATA public {t} " in toc]
    counts = {}
    for t in present:
        p = subprocess.Popen([_pg_bin("pg_restore"), "-a", "-t", t, "-f", "-", str(path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        n, inside = 0, False
        for line in p.stdout:
            if inside:
                if line == b"\\.\n":
                    inside = False
                else:
                    n += 1
            elif line.startswith(b"COPY "):
                inside = True
        if p.wait() != 0:
            raise RuntimeError(f"pg_restore could not read {t}: {p.stderr.read().decode('utf-8', 'replace')[:300]}")
        counts[t] = n
    return counts


def _verify_dump(path: Path) -> dict:
    out = {"ok": False, "integrity": None, "tables": {}, "sha256": None, "size_bytes": None}
    try:
        out["tables"] = _dump_counts(path)
        out["integrity"] = "ok"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["sha256"] = sha256(path)
    out["size_bytes"] = path.stat().st_size
    out["ok"] = "prices_daily" in out["tables"]
    if not out["ok"]:
        out["error"] = "prices_daily missing"
    return out


def _pg_db_url(url: str, dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    return make_conninfo(**{**conninfo_to_dict(url), "dbname": dbname})


def _pg_restore_into(path: Path, url: str, dbname: str) -> dict:
    """CREATE DATABASE dbname, pg_restore the archive into it, count KEY_TABLES. Leaves the database."""
    import psycopg
    if dbname == conninfo_dbname(url):
        raise RuntimeError("restore never overwrites the live database")
    with psycopg.connect(_pg_db_url(url, "postgres"), autocommit=True) as a:
        a.execute(f'CREATE DATABASE "{dbname}"')
    target = _pg_db_url(url, dbname)
    _run([_pg_bin("pg_restore"), "--no-owner", "--exit-on-error", "-d", target, str(path)])
    counts = {}
    with psycopg.connect(target) as c:
        names = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        for t in KEY_TABLES:
            if t in names:
                counts[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    return counts


def conninfo_dbname(url: str) -> str:
    from psycopg.conninfo import conninfo_to_dict
    return conninfo_to_dict(url).get("dbname") or ""


def _pg_drop(url: str, dbname: str):
    """Plain DROP DATABASE (it ends autovacuum workers itself); WITH (FORCE) would need the right to
    signal them, which an ordinary role lacks on PostgreSQL 18, and a freshly restored database
    always has one. Retried briefly in case a worker is mid-start."""
    import time
    import psycopg
    with psycopg.connect(_pg_db_url(url, "postgres"), autocommit=True) as a:
        for attempt in range(5):
            try:
                a.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
                return
            except psycopg.errors.ObjectInUse:
                if attempt == 4:
                    raise
                time.sleep(3)


def verify(path: Path) -> dict:
    """integrity_check + key-table row counts + sha256. {"ok", "integrity", "tables", "sha256", "size_bytes"}."""
    path = Path(path)
    out = {"ok": False, "integrity": None, "tables": {}, "sha256": None, "size_bytes": None}
    if not path.exists():
        out["error"] = "file missing"
        return out
    if path.suffix == ".dump":
        return _verify_dump(path)
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
    pg = _pg_url()
    dest = _dir() / f"{bid}.{'dump' if pg else 'db'}"
    c = get_connection()
    try:
        c.execute("INSERT INTO ops_backup (backup_id,kind,path,started_at,status) VALUES (?,?,?,?,?)",
                  (bid, kind, str(dest), started, "RUNNING"))
        c.commit()
    finally:
        c.close()
    res = {"backup_id": bid, "kind": kind, "path": str(dest), "status": "FAILED"}
    try:
        if pg:
            part = dest.with_name(dest.name + ".part")
            try:
                _run([_pg_bin("pg_dump"), "-Fc", "-Z", "6", "-f", str(part), "-d", pg])
                part.replace(dest)
            finally:
                part.unlink(missing_ok=True)
        else:
            src = sqlite3.connect(str(DB_PATH))
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst, pages=4096, sleep=0.05)
                # the copy inherits the live DB's WAL mode, and every later read-only open (verify, drill)
                # then leaves -wal / -shm files that prune / the drill never delete: make it one file
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
                src.close()
        v = verify(dest)
        res.update({k: v.get(k) for k in ("integrity", "tables", "sha256", "size_bytes")})
        if v["ok"]:
            res["status"] = "VERIFIED"
        else:
            res["error"] = v.get("error")
            failed = dest.with_suffix(dest.suffix + ".failed")
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
            if p.parent != base or not p.name.startswith("atip-") or not p.name.endswith((".db", ".dump")):
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
    try:                                           # W31 (OPS-06): encrypted off-site copy
        off = offsite_copy(res["backup_id"])
    except Exception as e:
        off = {"status": "FAILED", "error": str(e)}
        log.error(f"  off-site backup copy failed: {e}")
    return {"status": "SUCCESS", "rows": 1, "backup_id": res["backup_id"], "pruned": pruned, "offsite": off}


def list_backups(limit=50) -> list:
    from db.schema import get_connection
    c = get_connection()
    try:
        return [dict(r) for r in c.execute(
            "SELECT backup_id, kind, started_at, finished_at, size_bytes, sha256, integrity, status, error, pruned_at "
            "FROM ops_backup ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()]
    finally:
        c.close()


# ── W31 (OPS-06): encryption, off-site copy, restore drill ─────────────────────
MAGIC = b"ATIPBK1\0"
CHUNK = 4 * 1024 * 1024


def encrypt_file(src: Path, dst: Path) -> dict:
    import base64
    import os
    import struct
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from ops.crypto import _key, key_id
    k = _key()
    kid = key_id(k).encode()
    header = MAGIC + kid + struct.pack(">I", CHUNK)
    aes = AESGCM(k)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(header)
        i = 0
        while True:
            chunk = fi.read(CHUNK)
            last = len(chunk) < CHUNK
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, chunk, header + struct.pack(">I?", i, last))
            fo.write(nonce + struct.pack(">I", len(ct)) + ct)
            i += 1
            if last:
                break
    return {"path": str(dst), "chunks": i, "key_id": kid.decode(), "sha256": sha256(dst)}


def decrypt_file(src: Path, dst: Path) -> dict:
    import struct
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from ops.crypto import _key, _previous_key, key_id
    with open(src, "rb") as fi:
        header = fi.read(len(MAGIC) + 8 + 4)
        if not header.startswith(MAGIC):
            raise ValueError("not an ATIP encrypted backup")
        kid = header[len(MAGIC):len(MAGIC) + 8].decode()
        k = _key()
        if key_id(k) != kid:
            prev = _previous_key()
            if prev is None or key_id(prev) != kid:
                raise RuntimeError(f"backup was encrypted with key {kid}; that key is not available")
            k = prev
        aes = AESGCM(k)
        i, done = 0, False
        with open(dst, "wb") as fo:
            while True:
                nonce = fi.read(12)
                if not nonce:
                    break
                n = struct.unpack(">I", fi.read(4))[0]
                ct = fi.read(n)
                try:
                    pt = aes.decrypt(nonce, ct, header + struct.pack(">I?", i, False))
                except Exception:
                    pt = aes.decrypt(nonce, ct, header + struct.pack(">I?", i, True))
                    done = True
                fo.write(pt)
                i += 1
        if not done:
            raise RuntimeError("encrypted backup is truncated (final chunk missing)")
    return {"path": str(dst), "chunks": i}


def offsite_copy(backup_id: str) -> dict:
    from db.schema import get_connection
    from ops.config import ops
    from ops.crypto import available
    cfg = ops()
    dest_dir = cfg.get("backup_offsite_dir")
    c = get_connection()
    try:
        r = c.execute("SELECT path, status, sha256 FROM ops_backup WHERE backup_id=?", (backup_id,)).fetchone()
        if not r or r[1] != "VERIFIED":
            return {"status": "SKIPPED", "reason": "backup is not VERIFIED"}
        if not dest_dir:
            c.execute("UPDATE ops_backup SET offsite_status='NOT_CONFIGURED' WHERE backup_id=?", (backup_id,))
            c.commit()
            return {"status": "NOT_CONFIGURED", "reason": "ops.backup_offsite_dir is not set"}
        src = Path(r[0])
        od = Path(dest_dir)
        od.mkdir(parents=True, exist_ok=True)
        if available():
            tmp = src.with_suffix(src.suffix + ".enc")
            enc = encrypt_file(src, tmp)
            Path(str(tmp) + ".sha256").write_text(f"{enc['sha256']}  {tmp.name}\n", encoding="utf-8")
            shutil.copy2(tmp, od / tmp.name)
            shutil.copy2(str(tmp) + ".sha256", od / (tmp.name + ".sha256"))
            if sha256(od / tmp.name) != enc["sha256"]:
                raise IOError("off-site copy hash mismatch")
            tmp.unlink(missing_ok=True)
            Path(str(tmp) + ".sha256").unlink(missing_ok=True)
            out = {"status": "COPIED_ENCRYPTED", "path": str(od / tmp.name), "sha256": enc["sha256"]}
        elif cfg.get("backup_offsite_plaintext") is True:
            shutil.copy2(src, od / src.name)
            if sha256(od / src.name) != r[2]:
                raise IOError("off-site copy hash mismatch")
            out = {"status": "COPIED_PLAINTEXT", "path": str(od / src.name), "sha256": r[2]}
        else:
            out = {"status": "SKIPPED_NO_KEY", "reason": "no ATIP_ENCRYPTION_KEY and backup_offsite_plaintext is not "
                                                          "true: refusing to copy an unencrypted database off-site"}
        c.execute("UPDATE ops_backup SET offsite_path=?, offsite_status=?, encrypted_sha256=? WHERE backup_id=?",
                  (out.get("path"), out["status"], out.get("sha256") if out["status"] == "COPIED_ENCRYPTED" else None,
                   backup_id))
        c.commit()
        keep = int(cfg.get("backup_offsite_keep") or 14)
        mine = sorted(od.glob("atip-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        mine = [p for p in mine if p.suffix in (".db", ".dump", ".enc")]
        for old in mine[keep:]:
            old.unlink(missing_ok=True)
            Path(str(old) + ".sha256").unlink(missing_ok=True)
        return out
    finally:
        c.close()


def restore_drill(notify: bool = True) -> dict:
    import json as _json
    import time
    import uuid
    from db.schema import get_connection
    started = datetime.now()
    t0 = time.monotonic()
    did = "RD" + uuid.uuid4().hex[:12].upper()
    c = get_connection()
    try:
        r = c.execute("SELECT backup_id, path, tables_json, offsite_path, offsite_status FROM ops_backup WHERE "
                      "status='VERIFIED' AND pruned_at IS NULL ORDER BY finished_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    res = {"drill_id": did, "status": "FAILED"}
    is_dump = bool(r) and str(r[1]).endswith(".dump")
    drill = Path("atip_data/restore") / f"drill-{started:%Y%m%d-%H%M%S}.{'dump' if is_dump else 'db'}"
    pg, drill_db = _pg_url(), f"atip_drill_{did.lower()}"
    try:
        if not r:
            raise RuntimeError("no VERIFIED backup to drill")
        bid, path, tables_json, off, off_st = r
        if is_dump and not pg:
            raise RuntimeError("the newest backup is a PostgreSQL dump but ATIP is not running on PostgreSQL")
        drill.parent.mkdir(parents=True, exist_ok=True)
        if off and off_st == "COPIED_ENCRYPTED" and Path(off).exists():
            source = "offsite_encrypted"
            side = Path(str(off) + ".sha256")
            if side.exists() and side.read_text(encoding="utf-8").split()[0] != sha256(Path(off)):
                raise RuntimeError("off-site copy does not match its sidecar hash")
            decrypt_file(Path(off), drill)
        elif off and off_st == "COPIED_PLAINTEXT" and Path(off).exists():
            source = "offsite_plaintext"
            shutil.copy2(off, drill)
        else:
            source = "local"
            shutil.copy2(path, drill)
        if is_dump:                  # a real restore into a scratch database, then count there
            got = _pg_restore_into(drill, pg, drill_db)
            v = {"ok": "prices_daily" in got, "integrity": "ok", "tables": got,
                 "error": None if "prices_daily" in got else "prices_daily missing"}
        else:
            v = verify(drill)
        want = _json.loads(tables_json or "{}")
        diff = {t: (want.get(t), v["tables"].get(t)) for t in want if want.get(t) != v["tables"].get(t)}
        ok = v["ok"] and not diff
        res.update(status="PASSED" if ok else "FAILED", backup_id=bid, source=source, integrity=v.get("integrity"),
                   tables_checked=len(want), mismatches=diff, error=None if ok else (v.get("error") or "row counts "
                                                                                   "differ"))
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res.setdefault("source", None)
    finally:
        drill.unlink(missing_ok=True)
        if is_dump and pg:
            try:
                _pg_drop(pg, drill_db)
            except Exception as e:
                log.error(f"  restore drill: could not drop scratch database {drill_db}: {e}")
    res["seconds"] = round(time.monotonic() - t0, 2)
    c = get_connection()
    try:
        c.execute("INSERT INTO ops_restore_drill (drill_id,backup_id,source,started_at,finished_at,seconds,status,"
                  "details_json) VALUES (?,?,?,?,?,?,?,?)", (did, res.get("backup_id"), res.get("source"), started,
                                                             datetime.now(), res["seconds"], res["status"],
                                                             _json.dumps(res, default=str)))
        c.commit()
    finally:
        c.close()
    if res["status"] != "PASSED" and notify:
        try:
            from alerts.telegram import notify as _n
            _n(f"🛑 <b>Restore drill FAILED</b>: {res.get('error')}", category="ops", severity="critical",
               key=f"restore-drill-{started:%Y%m%d}")
        except Exception:
            pass
    # the job status last: res["status"] is the drill's PASSED / FAILED and must not override it
    return {**res, "status": "SUCCESS" if res["status"] == "PASSED" else "FAILED", "rows": 1}


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
    if path.suffix == ".dump":     # PostgreSQL: restore into a NEW database (never the live one)
        pg = _pg_url()
        if not pg:
            raise RuntimeError("a PostgreSQL dump restores only where ATIP runs on PostgreSQL")
        dbname = target or f"atip_restored_{datetime.now():%Y%m%d_%H%M%S}"
        got = _pg_restore_into(path, pg, dbname)
        return {"restored_to": f"database {dbname}", "source": str(path), "verified": got == v["tables"],
                "sha256_match": True, "tables": got}
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
