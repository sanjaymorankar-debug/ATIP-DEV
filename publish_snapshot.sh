#!/usr/bin/env bash
# Publishes a read-only dashboard snapshot to the bkesari.com tiers.
# The macOS counterpart of publish_snapshot.bat. Run every 5 minutes by
# deploy/launchd/com.atip.publish-snapshot.plist.
#
# Resolves the project from its own location, so moving the project does not
# break the schedule.
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

mkdir -p atip_data
exec "$PY" publish_snapshot.py "$@" >> atip_data/publish.log 2>&1
