"""
Verdict for a canary run (TEST_SPEC.md S6.4).

    python tests/canary/report.py <canary.jsonl>

Exit 0: no test called a broker API, opened a market feed, used the Zerodha SDK or
tried to leave this machine. Building a Dhan client object is listed but allowed (the
SDK's constructor sends nothing). Exit 1: at least one of those happened; every record
is printed with the test that made it, and order-capable calls are flagged first.
"""

import collections
import json
import sys
from pathlib import Path

ORDER_CALLS = {"place_order", "modify_order", "cancel_order", "place_slice_order", "place_forever",
               "modify_forever", "cancel_forever", "place_super_order", "modify_super_order",
               "cancel_super_order", "convert_position", "kill_switch", "place_gtt", "modify_gtt", "delete_gtt"}
ALLOWED = {"dhanhq.init"}


def main(path) -> int:
    p = Path(path)
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []
    by = collections.Counter((r["kind"], r["detail"], r["test"].split(" (")[0]) for r in rows)
    orders = [k for k in by if k[0] == "dhanhq.call" and k[1] in ORDER_CALLS]
    bad = [k for k in by if k[0] not in ALLOWED]
    print(f"canary records: {len(rows)} ({len(by)} distinct); order-capable calls: {len(orders)}; "
          f"other refused calls / connections: {len(bad) - len(orders)}")
    for k in orders:
        print(f"  ORDER   {k[0]} {k[1]}  <- {k[2]}  x{by[k]}")
    for k in sorted(set(bad) - set(orders)):
        print(f"  REFUSED {k[0]} {k[1]}  <- {k[2]}  x{by[k]}")
    for k in sorted(k for k in by if k[0] in ALLOWED):
        print(f"  allowed {k[0]} {k[1]}  <- {k[2]}  x{by[k]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "atip_canary.jsonl"))
