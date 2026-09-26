"""
Wealth-track command line (W17-W20).

    python -m wealth status                        chain status for the owner
    python -m wealth cycle                         run the investor cycle now (owner)
    python -m wealth uat-list                      UAT personas and whether they are seeded
    python -m wealth uat-seed --persona NAME|all   (re)build persona(s) in the 'uat' tenant
    python -m wealth uat-reset --persona NAME|all  remove persona data (uat tenant only)
    python -m wealth feedback [--status NEW]       beta feedback for triage (all testers)
    python -m wealth triage --id FB --status TRIAGED|FIXED|WONT_FIX|DUPLICATE [--note ...]
    python -m wealth health                        the wealth health check (W20)

The owner is the single-user house owner ('default', 'owner'). Nothing here places an
order or touches the broker or paper books.
"""

from __future__ import annotations

import argparse
import json
import sys

from db.schema import get_connection
from wealth import common as C


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wealth")
    ap.add_argument("command", choices=["status", "cycle", "uat-list", "uat-seed", "uat-reset", "feedback", "triage",
                                        "health"])
    ap.add_argument("--persona")
    ap.add_argument("--status")
    ap.add_argument("--id")
    ap.add_argument("--note")
    a = ap.parse_args(argv)
    conn = get_connection()
    try:
        from wealth import uat
        if a.command == "status":
            from wealth.integrated import status
            out = status(conn, C.DEFAULT_OWNER)
        elif a.command == "cycle":
            from wealth.integrated import investor_cycle
            out = investor_cycle(conn, C.DEFAULT_OWNER)
        elif a.command == "uat-list":
            out = uat.personas(conn)
        elif a.command in ("uat-seed", "uat-reset"):
            if not a.persona:
                ap.error("--persona is required")
            names = list(uat.PERSONAS) if a.persona == "all" else [a.persona]
            fn = uat.seed if a.command == "uat-seed" else uat.reset
            out = [fn(conn, n) for n in names]
        elif a.command == "feedback":
            out = uat.list_feedback(conn, None, a.status)
        elif a.command == "triage":
            if not a.id or not a.status:
                ap.error("--id and --status are required")
            out = uat.triage(conn, a.id, a.status, a.note, actor="cli")
        else:
            from wealth.health import check
            out = check(conn)
    except (ValueError, LookupError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
