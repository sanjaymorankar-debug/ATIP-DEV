"""
The Mac's database, copied to a VIEWER (ops/role.py): the web server that shows ATIP without trading.

    Mac (TRADER)                                        web server (VIEWER)
    push(): a consistent copy of atip.db       rsync    viewer_incoming/atip.db, then manifest.json
            (sqlite backup API, sign-in       ───────▶  watcher thread (start_watcher, every 60 s):
            credentials emptied), sha256                 copy + sha256 check against the manifest, then
            in manifest.json                             the sqlite backup API into its own atip.db

Nothing travels the other way: the viewer holds no route, key or credential that reaches the Mac. rsync over
SSH sends only the pages that changed, so after the first push a copy is a few MB, not the whole database.

On the Mac, atip_data/viewer_push.json (git-ignored with the rest of atip_data/):
    {"target":   "atip@203.0.113.10:/home/atip/atip/viewer_incoming/",
     "ssh_key":  "~/.ssh/atip_viewer",     optional: the key the server accepts
     "ssh_port": 22}                       optional
deploy/launchd/com.atip.viewer-push.plist runs push_viewer.sh every 10 minutes. While ATIP is STOPPED
(21:00-06:00 IST) it pushes at most once an hour: nothing but heartbeats changes then.

On the viewer, ATIP_VIEWER_INCOMING names the folder rsync writes to (default viewer_incoming/ in the project;
/app/viewer_incoming in docker-compose.viewer.yml).

Usage (from the project root):
    python -m ops.viewer_sync push              # Mac: copy and send now
    python -m ops.viewer_sync push --dry-run    # Mac: make the copy, send nothing
    python -m ops.viewer_sync install           # viewer: install what has arrived now (the watcher does this)
    python -m ops.viewer_sync status            # either side: the last push / install
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import db.schema as schema

log = logging.getLogger("atip.viewer_sync")

PUSH_CONFIG = Path("atip_data") / "viewer_push.json"
DB_NAME = "atip.db"
MANIFEST = "manifest.json"
FORMAT = 1
STOPPED_PUSH_EVERY_S = 3600
# Sign-in credentials stay on the Mac. The viewer's own sign-in is the proxy's (docs/VIEWER_SERVER.md).
CREDENTIAL_TABLES = ("enterprise_user", "enterprise_session", "enterprise_password_reset", "enterprise_api_key",
                     "enterprise_refresh_token", "enterprise_vault_credential", "enterprise_email_token")


def _data_dir() -> Path:
    return Path(schema.DB_PATH).parent


def _state_path() -> Path:
    return _data_dir() / "viewer_sync.json"


def _state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(**changes) -> dict:
    st = {**_state(), **changes}
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
    return st


def _sqlite_only(what: str) -> None:
    if schema.pg_runtime_url() or schema.mysql_runtime_url():
        raise RuntimeError(f"{what} copies the SQLite database; this install runs on {schema.describe_target()}")


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@functools.lru_cache(maxsize=1)
def _release() -> str | None:
    """This checkout's commit, once per process (the dashboard's status poll asks every minute)."""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


# ── the Mac ────────────────────────────────────────────────────────────────
def snapshot(dest_dir: Path | None = None) -> dict:
    """A consistent, self-contained copy of the live database (one file, no -wal) with the sign-in
    credential tables emptied, and its manifest. The live database is only read."""
    _sqlite_only("The viewer push")
    src_path = Path(schema.DB_PATH)
    if not src_path.exists():
        raise FileNotFoundError(f"no database at {src_path}")
    dest_dir = Path(dest_dir or _data_dir() / "viewer_push")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / (DB_NAME + ".tmp")
    tmp.unlink(missing_ok=True)
    src, dst = _ro(src_path), sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("PRAGMA secure_delete=ON")       # emptied rows are zeroed in the file, not just unlinked
        emptied = {}
        for table in CREDENTIAL_TABLES:
            if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                emptied[table] = dst.execute(f'DELETE FROM "{table}"').rowcount
        dst.commit()
    finally:
        dst.close()
        src.close()
    db_file = dest_dir / DB_NAME
    os.replace(tmp, db_file)
    manifest = {"format": FORMAT, "generated_at": datetime.now().isoformat(timespec="seconds"),
                "sha256": _sha256(db_file), "size": db_file.stat().st_size, "release": _release(),
                "emptied": {t: n for t, n in emptied.items() if n}}
    (dest_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"db": db_file, "manifest": dest_dir / MANIFEST, **manifest}


def load_push_config(path: Path | None = None) -> dict:
    path = Path(path or PUSH_CONFIG)
    if not path.exists():
        raise SystemExit(f"Missing {path} — see the docstring at the top of ops/viewer_sync.py.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not str(cfg.get("target") or "").strip():
        raise SystemExit(f"No target in {path}: user@host:/path/to/viewer_incoming/")
    return cfg


def rsync_command(cfg: dict, source: Path) -> list:
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    if cfg.get("ssh_key"):
        ssh += ["-i", str(Path(str(cfg["ssh_key"])).expanduser())]
    if cfg.get("ssh_port"):
        ssh += ["-p", str(int(cfg["ssh_port"]))]
    target = str(cfg["target"]).strip()
    return ["rsync", "-az", "--timeout=120", "-e", " ".join(shlex.quote(a) for a in ssh), str(source), target]


def _send(cfg: dict, source: Path) -> None:
    r = subprocess.run(rsync_command(cfg, source), capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"rsync {source.name} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")


def _stopped() -> bool:
    try:
        from pipeline.operating_schedule import is_stopped
        return bool(is_stopped())
    except Exception:
        return False


def push(cfg: dict | None = None, dry_run: bool = False, force: bool = False) -> dict:
    """Copy the database and send it: the database first, then the manifest, so the viewer never installs a
    database whose manifest has not arrived (its sha256 would not match)."""
    last = _state().get("pushed") or {}
    if not force and not dry_run and last.get("at") and _stopped():
        age = (datetime.now() - datetime.fromisoformat(last["at"])).total_seconds()
        if age < STOPPED_PUSH_EVERY_S:
            return {"status": "SKIPPED", "why": f"ATIP is stopped; last push {int(age // 60)} min ago"}
    cfg = cfg if cfg is not None else (None if dry_run else load_push_config())
    started = time.monotonic()
    snap = snapshot()
    out = {"status": "BUILT" if dry_run else "PUSHED", "size": snap["size"], "sha256": snap["sha256"],
           "generated_at": snap["generated_at"], "emptied": snap["emptied"]}
    if not dry_run:
        _send(cfg, snap["db"])
        _send(cfg, snap["manifest"])
        _save_state(pushed={"at": datetime.now().isoformat(timespec="seconds"), "target": cfg["target"],
                            "sha256": snap["sha256"], "size": snap["size"], "generated_at": snap["generated_at"]})
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


# ── the viewer ─────────────────────────────────────────────────────────────
def incoming_dir() -> Path:
    return Path(os.environ.get("ATIP_VIEWER_INCOMING") or "viewer_incoming")


def _copy_hashed(src: Path, dst: Path) -> tuple:
    """Copy src to dst and hash what was copied, so the file checked is the file installed even if rsync
    replaces src meanwhile."""
    h, size = hashlib.sha256(), 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            h.update(chunk)
            fout.write(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def install_once(incoming: Path | None = None) -> dict:
    """Install what the Mac last sent, if it is new and complete. The sqlite backup API writes it into this
    server's database inside one transaction, so a page being served reads either the old copy or the new."""
    _sqlite_only("The viewer install")
    inc = Path(incoming or incoming_dir())
    man_path, db_in = inc / MANIFEST, inc / DB_NAME
    if not man_path.exists() or not db_in.exists():
        return {"status": "WAITING", "why": f"nothing from the Mac yet in {inc}"}
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    if (_state().get("installed") or {}).get("sha256") == manifest.get("sha256"):
        return {"status": "CURRENT", "generated_at": manifest.get("generated_at")}
    live = Path(schema.DB_PATH)
    live.parent.mkdir(parents=True, exist_ok=True)
    work = live.parent / "viewer_install.db"
    try:
        sha, size = _copy_hashed(db_in, work)
        if sha != manifest.get("sha256") or size != manifest.get("size"):
            # A push in flight: the new database has landed and its manifest follows it.
            return {"status": "MISMATCH", "why": "the database and its manifest differ; waiting for the manifest"}
        src, dst = _ro(work), sqlite3.connect(str(live), timeout=120)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    finally:
        work.unlink(missing_ok=True)
    # The Mac's copy may predate this server's release: let its additive migrations run on the next connection.
    schema._MIGRATED.discard(str(live.resolve()))
    installed = {"at": datetime.now().isoformat(timespec="seconds"), "sha256": manifest["sha256"],
                 "size": size, "generated_at": manifest.get("generated_at"), "release": manifest.get("release")}
    _save_state(installed=installed, last_error=None)
    return {"status": "INSTALLED", **installed}


def start_watcher(every_s: int = 60) -> threading.Thread:
    def loop():
        while True:
            try:
                r = install_once()
                if r["status"] == "INSTALLED":
                    log.info(f"  ✓ Data from the Mac installed (made {r['generated_at']}, {r['size'] / 1e6:.0f} MB)")
            except Exception as e:
                log.warning(f"  viewer sync: {e}")
                try:
                    _save_state(last_error={"at": datetime.now().isoformat(timespec="seconds"), "error": str(e)[:300]})
                except Exception:
                    pass
            time.sleep(every_s)

    t = threading.Thread(target=loop, name="atip-viewer-sync", daemon=True)
    t.start()
    log.info(f"  ✓ Viewer sync watching {incoming_dir().resolve()} every {every_s}s")
    return t


def status() -> dict:
    st = _state()
    inst = st.get("installed") or {}
    return {"installed_at": inst.get("at"), "data_from": inst.get("generated_at"), "mac_release": inst.get("release"),
            "release": _release(), "pushed": st.get("pushed"), "last_error": st.get("last_error"),
            "incoming": str(incoming_dir())}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("push", "install", "status"))
    ap.add_argument("--dry-run", action="store_true", help="push: make the copy, send nothing")
    ap.add_argument("--force", action="store_true", help="push: send even while ATIP is stopped")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    if args.action == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    try:
        out = push(dry_run=args.dry_run, force=args.force) if args.action == "push" else install_once()
    except Exception as e:
        log.error(f"  ✗ viewer {args.action}: {e}")
        return 1
    log.info(f"  {args.action}: {json.dumps(out, default=str)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
