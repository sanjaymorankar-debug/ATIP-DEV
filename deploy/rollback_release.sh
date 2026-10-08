#!/usr/bin/env bash
# ============================================================================
#  ATIP - roll the production instance back to a previous release (macOS / Linux, W39b)
#
#  The macOS counterpart of rollback_release.ps1.
#
#  Application rollback: master is reset to <TO_REF> (a release tag or a
#  backup/pre-<wave>-master branch). Commits after it stay reachable on their
#  tag / branch. Untracked files (the owner's spreadsheets, atip_data/) are not
#  touched by the reset.
#
#  --restore-backup <backup_id>: ATIP schema changes are additive, so older code
#  runs on the newer schema and a code rollback is normally enough. Restore the
#  database only when the release damaged data. The verified backup is restored
#  to a NEW file; the live file is kept as atip.db.pre-rollback-<ts>. Data written
#  after that backup is lost.
#
#  --restore-config-from <release id>: copies atip_data/releases/<id>/config.json.backup
#  back to atip_data/config.json (the current file is kept as config.json.pre-rollback-<ts>).
#
#  DRY RUN BY DEFAULT; --authorize performs it.
#    deploy/rollback_release.sh backup/pre-w39b-master
#    deploy/rollback_release.sh backup/pre-w39b-master --authorize
# ============================================================================
set -euo pipefail

TO_REF=""
RESTORE_BACKUP=""
RESTORE_CONFIG_FROM=""
AUTHORIZE=0
PORT=8000
PY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --authorize) AUTHORIZE=1 ;;
        --restore-backup) RESTORE_BACKUP="$2"; shift ;;
        --restore-config-from) RESTORE_CONFIG_FROM="$2"; shift ;;
        --port) PORT="$2"; shift ;;
        --python) PY="$2"; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) TO_REF="$1" ;;
    esac
    shift
done
[[ -n "$TO_REF" ]] || { echo "usage: $0 <TO_REF> [--authorize] [--restore-backup ID] [--restore-config-from RELEASE]" >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
if [[ -z "$PY" ]]; then
    if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"; else PY="python3"; fi
fi
LABEL="com.atip.platform"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

step() { printf '\n[%s] %s\n' "$1" "$2"; }
fail() { printf 'ROLLBACK STOPPED: %s\n' "$1" >&2; exit 1; }
atip_pids() { pgrep -f '(^|/)python[0-9.]* main\.py$' || true; }
agent_loaded() { [[ "$(uname -s)" == "Darwin" ]] && launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; }

TARGET="$(git rev-parse --verify "$TO_REF^{commit}" 2>/dev/null || true)"
[[ -n "$TARGET" ]] || fail "$TO_REF is not a commit / tag / branch"
CURRENT="$(git rev-parse HEAD)"
echo "Rollback master $CURRENT -> $TO_REF ($TARGET)  (mode: $([[ $AUTHORIZE == 1 ]] && echo AUTHORIZED || echo 'DRY RUN'))"
echo "  database restore : ${RESTORE_BACKUP:-no (code-only rollback)}"
echo "  config restore   : ${RESTORE_CONFIG_FROM:+from release }${RESTORE_CONFIG_FROM:-no}"
if [[ $AUTHORIZE != 1 ]]; then echo; echo "DRY RUN: nothing changed. Re-run with --authorize."; exit 0; fi

step 1 "Safety backup of the current database"
"$PY" -m ops backup --kind pre-release || fail "could not take a VERIFIED safety backup"

step 2 "Stop ATIP"
WAS_AGENT=0
if agent_loaded; then launchctl bootout "gui/$UID/$LABEL" || true; WAS_AGENT=1; fi
for pid in $(atip_pids); do echo "  stopping PID $pid"; kill "$pid" 2>/dev/null || true; done
for _ in $(seq 1 30); do [[ -z "$(atip_pids)" ]] && break; sleep 1; done
[[ -z "$(atip_pids)" ]] || fail "ATIP is still running (PID $(atip_pids | tr '\n' ' '))"

step 3 "Application rollback (git)"
git checkout master
git reset --hard "$TARGET"
echo "  master now at $(git rev-parse --short HEAD)"

TS="$(date +%Y%m%d-%H%M%S)"
if [[ -n "$RESTORE_BACKUP" ]]; then
    step 4 "Database rollback from $RESTORE_BACKUP"
    mkdir -p atip_data/restore
    RESTORED="atip_data/restore/rollback-$TS.db"
    "$PY" -m ops restore "$RESTORE_BACKUP" --target "$RESTORED" || fail "restore verification failed; live database untouched"
    mv atip_data/atip.db "atip_data/atip.db.pre-rollback-$TS"
    for s in -wal -shm; do [[ -f "atip_data/atip.db$s" ]] && mv "atip_data/atip.db$s" "atip_data/atip.db.pre-rollback-$TS$s"; done
    cp "$RESTORED" atip_data/atip.db
    echo "  restored; previous file kept as atip_data/atip.db.pre-rollback-$TS"
fi
if [[ -n "$RESTORE_CONFIG_FROM" ]]; then
    step 5 "Configuration rollback"
    SRC="atip_data/releases/$RESTORE_CONFIG_FROM/config.json.backup"
    [[ -f "$SRC" ]] || fail "$SRC not found"
    cp atip_data/config.json "atip_data/config.json.pre-rollback-$TS"
    cp "$SRC" atip_data/config.json
fi

step 6 "Start ATIP (python main.py)"
if [[ $WAS_AGENT == 1 || -f "$PLIST" ]] && [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl bootstrap "gui/$UID" "$PLIST"
else
    nohup ./start_atip.sh >> atip_data/start_atip.out 2>&1 < /dev/null &
fi

step 7 "Verify health and trading safety"
OK=1
"$PY" -m ops release postcheck --port "$PORT" --wait 180 || OK=0
"$PY" -m ops release record "$TO_REF" "$([[ $OK == 1 ]] && echo ROLLED_BACK || echo FAILED)" "rollback_release.sh from $CURRENT"
[[ $OK == 1 ]] || fail "post-rollback checks failed - see output above"
echo
echo "ROLLED BACK to $TO_REF. Smoke checks passed (not functional testing)."
