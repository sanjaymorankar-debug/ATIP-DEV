#!/usr/bin/env bash
# ATIP — AI Trading Intelligence Platform (macOS / Linux)
#
# The macOS counterpart of start_atip.bat. Like that script it resolves the
# project from its OWN location rather than a hardcoded path, so it keeps
# working wherever the project is moved to — which is the whole point after the
# move off D:\Projects\ATIP to
# /Users/agtci/Documents/Project_Documents/Projects/ATIP.
#
# Install as a login item / auto-restarting service with:
#   deploy/launchd/install.sh
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo
echo "  ================================================"
echo "    ATIP - AI Trading Intelligence Platform"
echo "  ================================================"
echo
echo "  Project    : $(pwd)"
echo "  Dashboard  : http://localhost:8000"
echo "  Press Ctrl+C to stop ATIP (this also stops the order monitor)."
echo

# Prefer the project virtualenv if one exists, then python3, then python.
if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "  [ERROR] No python found on PATH. Install Python 3.11+ (brew install python@3.12)," >&2
    echo "          or create a virtualenv at .venv, then re-run." >&2
    exit 1
fi
echo "  Python     : $($PY --version 2>&1) ($PY)"
echo

# Open the dashboard once the server has had time to bind. `open` is macOS;
# xdg-open on Linux. Backgrounded so it never blocks startup.
if command -v open >/dev/null 2>&1; then
    ( sleep 10 && open http://localhost:8000 >/dev/null 2>&1 || true ) &
elif command -v xdg-open >/dev/null 2>&1; then
    ( sleep 10 && xdg-open http://localhost:8000 >/dev/null 2>&1 || true ) &
fi

exec "$PY" main.py "$@"
