#!/usr/bin/env bash
# ============================================================================
#  ATIP - deploy a release to the production instance (macOS / Linux, W39b)
#
#  The macOS counterpart of deploy_release.ps1, with the same steps and checks.
#  Production is this repository's `master` checkout running `python main.py`
#  (scheduler + dashboard + index feed) on 127.0.0.1:8000. On macOS it is started
#  by the com.atip.platform LaunchAgent (deploy/launchd/install.sh).
#  A release is a tagged commit on master (e.g. ATIP-W39B).
#
#  DRY RUN BY DEFAULT: without --authorize, this only prints the plan and runs the
#  read-only preflight. Nothing is stopped, backed up, migrated or started.
#
#    git checkout master && git pull --ff-only && git fetch --tags
#    deploy/deploy_release.sh ATIP-W39B                # dry run
#    deploy/deploy_release.sh ATIP-W39B --authorize    # deploy
#
#  Options: --port N (default 8000), --python PATH (default .venv/bin/python, else python3).
#  It never enables live trading, never pushes and never edits configuration.
# ============================================================================
set -euo pipefail

RELEASE_ID=""
AUTHORIZE=0
PORT=8000
PY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --authorize) AUTHORIZE=1 ;;
        --port) PORT="$2"; shift ;;
        --python) PY="$2"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) RELEASE_ID="$1" ;;
    esac
    shift
done
[[ -n "$RELEASE_ID" ]] || { echo "usage: $0 <RELEASE_ID> [--authorize] [--port N] [--python PATH]" >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
if [[ -z "$PY" ]]; then
    if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"; else PY="python3"; fi
fi
LABEL="com.atip.platform"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

step() { printf '\n[%s] %s\n' "$1" "$2"; }
fail() { printf 'DEPLOYMENT STOPPED: %s\n' "$1" >&2; exit 1; }
# `python main.py` with no options = the production process (not --run, --init ...)
atip_pids() { pgrep -f '(^|/)python[0-9.]* main\.py$' || true; }
agent_loaded() { [[ "$(uname -s)" == "Darwin" ]] && launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; }

MODE=$([[ $AUTHORIZE == 1 ]] && echo AUTHORIZED || echo "DRY RUN")
echo "ATIP deployment of $RELEASE_ID from $REPO_DIR  (mode: $MODE, python: $PY)"

step 1 "Verify release commit"
TAG_COMMIT="$(git rev-list -n 1 "$RELEASE_ID" 2>/dev/null || true)"
[[ -n "$TAG_COMMIT" ]] || fail "tag $RELEASE_ID not found (git fetch --tags?)"
HEAD_COMMIT="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "  tag $RELEASE_ID -> $TAG_COMMIT ; HEAD $HEAD_COMMIT on $BRANCH"
[[ "$BRANCH" == "master" ]] || fail "production must be on master (is $BRANCH)"
[[ "$TAG_COMMIT" == "$HEAD_COMMIT" ]] || fail "master HEAD is not the release commit; git pull --ff-only first"

step 2-5 "Preflight (clean tree, configuration, secrets, database, migrations, trading safety, dependencies)"
"$PY" -m ops release preflight "$RELEASE_ID" || true
if [[ $AUTHORIZE != 1 ]]; then
    echo
    echo "DRY RUN complete. The preflight 'pre-deployment backup' check fails until step 3 runs."
    echo "With --authorize the script: 3 backs up, 6 stops ATIP, 7 records the manifest + config backup,"
    echo "8 applies migrations, 9 starts ATIP, 10-14 verifies, 15 records the deployment."
    exit 0
fi

step 3 "Pre-deployment database backup (verified)"
"$PY" -m ops backup --kind pre-release || fail "backup was not VERIFIED"
"$PY" -m ops release preflight "$RELEASE_ID" || fail "preflight has FAIL items (see above)"

step 6 "Stop the running ATIP"
WAS_AGENT=0
if agent_loaded; then
    # bootout, not kill: the agent's KeepAlive would restart the old process mid-migration
    launchctl bootout "gui/$UID/$LABEL" || true
    WAS_AGENT=1
    echo "  LaunchAgent $LABEL unloaded"
fi
for pid in $(atip_pids); do echo "  stopping PID $pid"; kill "$pid" 2>/dev/null || true; done
for _ in $(seq 1 30); do [[ -z "$(atip_pids)" ]] && break; sleep 1; done
[[ -z "$(atip_pids)" ]] || fail "ATIP is still running (PID $(atip_pids | tr '\n' ' '))"

step 7 "Record the release manifest and back up the configuration"
"$PY" -m ops release manifest "$RELEASE_ID" >/dev/null
echo "  atip_data/releases/$RELEASE_ID/manifest.json (+ config.json / .env copies, local only)"

step 8 "Apply database migrations"
"$PY" -m ops migrate || fail "a migration FAILED - restore with deploy/rollback_release.sh"

step 9 "Start ATIP (python main.py)"
if [[ $WAS_AGENT == 1 || -f "$PLIST" ]] && [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl bootstrap "gui/$UID" "$PLIST"
    echo "  LaunchAgent $LABEL loaded"
else
    nohup ./start_atip.sh >> atip_data/start_atip.out 2>&1 < /dev/null &
    echo "  started in the background (log: atip_data/start_atip.out)"
fi

step 10-14 "Verify health, logs, scheduler, broker mode and LIVE_TRADING_ENABLED = FALSE"
OK=1
"$PY" -m ops release postcheck --port "$PORT" --wait 180 || OK=0

step 15 "Record the deployment"
"$PY" -m ops release record "$RELEASE_ID" "$([[ $OK == 1 ]] && echo DEPLOYED || echo FAILED)" "deploy_release.sh"
[[ $OK == 1 ]] || fail "post-deployment checks failed - investigate, or roll back with deploy/rollback_release.sh"
echo
echo "DEPLOYED $RELEASE_ID ($HEAD_COMMIT). Post-deployment smoke checks passed; this is NOT functional testing."
