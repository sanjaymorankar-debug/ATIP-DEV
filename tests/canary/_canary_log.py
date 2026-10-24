"""Recorder shared by the canary stand-ins (TEST_SPEC.md S6.4). Not used by ATIP itself.

Each record is one JSON line in $ATIP_CANARY_LOG: what was attempted and by which test
(pytest's PYTEST_CURRENT_TEST)."""
import json
import os
import time

LOG = os.environ.get("ATIP_CANARY_LOG", "atip_canary.jsonl")


def record(kind, detail):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, "detail": str(detail)[:300],
                                "test": os.environ.get("PYTEST_CURRENT_TEST", "")}) + "\n")
    except Exception:
        pass
