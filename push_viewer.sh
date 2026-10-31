#!/usr/bin/env bash
# Sends a copy of the database to the view-only web server (ops/viewer_sync.py).
# Run every 10 minutes by deploy/launchd/com.atip.viewer-push.plist.
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
exec "$PY" -m ops.viewer_sync push "$@" >> atip_data/viewer_push.log 2>&1
