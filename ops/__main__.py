"""
python -m ops <command>

    status                 health of every component (database, broker, data, scheduler, ml)
    validate               configuration + secrets + migration validation
    safety                 trading-safety report (LIVE_TRADING_ENABLED)
    migrate                apply pending versioned migrations
    backup                 verified online backup now (kind=manual)
    backups                list backups
    prune                  apply backup retention
    restore <id|path> [--target FILE]
                           verify a backup and copy it to a NEW file (never the live DB)
    verify <path>          integrity-check a database file
    keygen                 create atip_data/secrets/ATIP_ENCRYPTION_KEY (never printed; refuses to overwrite)
    secrets                secret presence / source / rotation (never values)
    rotate <NAME>          record that a secret was rotated (after you changed it)
    audit-verify           recompute the enterprise_audit hash chain
    monitor                evaluate the monitoring rules once (no notifications)
    scan                   secret / config scan of tracked files (+ pip-audit if installed)
    backup --kind pre-release   (W9) verified backup labelled for a deployment
    release preflight|manifest|postcheck|record ...   (W9) see ops/release.py
Exit code 1 when the command found a problem.
"""

from __future__ import annotations

import argparse
import json
import sys


def _p(x):
    print(json.dumps(x, indent=2, default=str))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "release":                       # W9 release engineering
        from ops.release import main as release_main
        return release_main(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m ops")
    ap.add_argument("cmd")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--target")
    ap.add_argument("--kind", default="manual", choices=["manual", "pre-release"])
    a = ap.parse_args(argv)
    from db.schema import get_connection
    if a.cmd == "status":
        from ops import health
        comps = {n: health.component(n) for n in health.COMPONENTS}
        _p({"status": health.worst(*(v["status"] for v in comps.values())), "components": comps})
        return 0 if all(v["status"] != "FAILED" for v in comps.values()) else 1
    if a.cmd == "validate":
        from ops.config import validate
        from ops.migrations import validate as mv
        from ops.secrets import validate as sv
        c = get_connection()
        try:
            f = validate() + sv() + mv(c)
        finally:
            c.close()
        _p(f)
        return 1 if any(x["level"] == "error" for x in f) else 0
    if a.cmd == "safety":
        from ops.trading_safety import report
        r = report()
        _p(r)
        return 1 if r["LIVE_TRADING_ENABLED"] else 0
    if a.cmd == "migrate":
        from ops.migrations import apply, status
        c = get_connection()
        try:
            res = apply(c)
            _p({"applied_now": res, "status": status(c)})
        finally:
            c.close()
        return 1 if any(r["status"] == "FAILED" for r in res) else 0
    if a.cmd == "backup":
        from ops.backup import backup
        r = backup(a.kind)
        _p(r)
        return 0 if r["status"] == "VERIFIED" else 1
    if a.cmd == "backups":
        from ops.backup import list_backups
        _p(list_backups())
        return 0
    if a.cmd == "prune":
        from ops.backup import prune
        _p({"pruned": prune()})
        return 0
    if a.cmd == "restore":
        from ops.backup import restore
        _p(restore(a.arg, a.target))
        return 0
    if a.cmd == "verify":
        from ops.backup import verify
        r = verify(a.arg)
        _p(r)
        return 0 if r["ok"] else 1
    if a.cmd == "keygen":
        from ops.crypto import keygen
        print(f"key written to {keygen()} (not printed). Back it up securely: encrypted fields are unreadable "
              f"without it.")
        return 0
    if a.cmd == "secrets":
        from ops.secrets import rotation_status
        c = get_connection()
        try:
            _p(rotation_status(c))
        finally:
            c.close()
        return 0
    if a.cmd == "rotate":
        from ops.secrets import mark_rotated
        c = get_connection()
        try:
            mark_rotated(c, a.arg, actor="cli")
        finally:
            c.close()
        print(f"{a.arg} marked rotated")
        return 0
    if a.cmd == "audit-verify":
        from enterprise.audit import verify_chain
        c = get_connection()
        try:
            r = verify_chain(c)
        finally:
            c.close()
        _p(r)
        return 0 if r["ok"] else 1
    if a.cmd == "monitor":
        from ops.monitor import evaluate
        c = get_connection()
        try:
            _p(evaluate(c, notify=False))
        finally:
            c.close()
        return 0
    if a.cmd == "scan":
        from ops.scan import run
        r = run()
        _p(r)
        return 0 if r["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
