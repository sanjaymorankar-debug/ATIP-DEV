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

# python.org's Python ships without root certificates until "Install
# Certificates.command" is run, which fails every HTTPS call with
# CERTIFICATE_VERIFY_FAILED. Fall back to certifi's bundle when its own are missing.
if [[ -z "${SSL_CERT_FILE:-}" ]]; then
    _ca="$("$PY" -c 'import os, ssl; p = ssl.get_default_verify_paths().cafile; import certifi; print("" if p and os.path.exists(p) else certifi.where())' 2>/dev/null || true)"
    [[ -n "$_ca" ]] && export SSL_CERT_FILE="$_ca"
fi

mkdir -p atip_data
exec "$PY" tools/dhan_token_refresh.py "$@"
