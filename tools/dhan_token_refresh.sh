#!/usr/bin/env bash
# Renews the Dhan access token (tools/dhan_token_refresh.py) and saves it where
# ATIP reads it. The macOS counterpart of the Windows scheduled task
# "ATIP_DhanTokenRefresh". Run daily at 06:30 and at login by
# deploy/launchd/com.atip.dhan-token-refresh.plist.
#
# Resolves the project from its own location, so moving the project does not
# break the schedule. The Python script appends to atip_data/dhan_token_refresh.log
# itself.
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

mkdir -p atip_data
exec "$PY" tools/dhan_token_refresh.py "$@"
