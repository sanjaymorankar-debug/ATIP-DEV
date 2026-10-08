r"""
ATIP — publish a read-only dashboard snapshot to bkesari.com
============================================================
Renders the same dashboard page as http://localhost:8000 from the local
database and uploads it to the hosted site, which shows it behind a login.

Why a push and not exposing the dashboard: the local dashboard can create,
confirm and delete order rules. Nothing on the internet should be able to
reach those endpoints, so this machine only ever sends a finished HTML page
outward — the hosted copy has no path back here, and its server blocks the
page's scripts from making any network calls.

This is a separate short-lived process: it does not touch the running
scheduler/dashboard (see main.py), so it is safe to run on a timer.

Setup — create atip_data/publish.json (gitignored with the rest of atip_data/):
    {
      "targets": [
        {"url": "https://bkesari.com/atip/api/snapshot",      "token": "<ATIP_PUBLISH_TOKEN of bkesari.com>"},
        {"url": "https://test.bkesari.com/atip/api/snapshot", "token": "<ATIP_PUBLISH_TOKEN of test.bkesari.com>"}
      ]
    }

Usage (from the project root):
    python publish_snapshot.py            # publish once
    python publish_snapshot.py --dry-run  # build the page, report its size, upload nothing
"""

import sys
import json
import logging
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
os.chdir(ROOT)  # dashboard.server uses paths relative to the project root

import requests

log = logging.getLogger("publish_snapshot")
CONFIG_PATH = ROOT / "atip_data" / "publish.json"


def load_targets():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH} — see the docstring at the top of this file.")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    targets = [t for t in cfg.get("targets", []) if t.get("url") and t.get("token")]
    if not targets:
        raise SystemExit(f"No targets with both url and token in {CONFIG_PATH}")
    return targets


def build_snapshot():
    from dashboard.security import token
    from dashboard.server import build_html, generate_state, latest_scored_date
    state = generate_state(latest_scored_date())
    # The local page embeds the dashboard's write token (X-ATIP-Token, which authorises the
    # order-rule endpoints); a page sent to the internet must not carry it.
    html = build_html(state).replace(token(), "")
    return {
        "html": html,
        "generated_at": state.get("generated_at"),
        "trade_date": state.get("trade_date"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="build the page but do not upload")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

    snap = build_snapshot()
    size_kb = len(snap["html"].encode("utf-8")) / 1024
    log.info(f"Snapshot for trade date {snap['trade_date']} built ({size_kb:.0f} KB)")
    if args.dry_run:
        return 0

    failures = 0
    for t in load_targets():
        try:
            r = requests.post(t["url"], json=snap, timeout=60,
                              headers={"Authorization": f"Bearer {t['token']}"})
            if r.ok:
                log.info(f"  ✓ {t['url']}")
            else:
                failures += 1
                log.error(f"  ✗ {t['url']} → HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            failures += 1
            log.error(f"  ✗ {t['url']} → {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
