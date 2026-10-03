#!/usr/bin/env bash
# Install (or refresh) ATIP's macOS LaunchAgents.
#
# Replaces, on macOS, everything the Windows launchers did:
#   ATIP_TaskScheduler.xml + atip_autostart.vbs  -> com.atip.platform.plist
#   "ATIP publish snapshot" scheduled task       -> com.atip.publish-snapshot.plist
#
# The plists in this folder carry __ATIP_DIR__ placeholders; this script
# substitutes the project's real location (resolved from its own path, never
# hardcoded) and writes the result into ~/Library/LaunchAgents.
#
# Usage:
#   deploy/launchd/install.sh              # install both agents
#   deploy/launchd/install.sh platform     # just the scheduler/dashboard
#   deploy/launchd/install.sh snapshot     # just the 5-minute snapshot upload
#   deploy/launchd/install.sh --uninstall  # unload and remove both
set -euo pipefail

ATIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$ATIP_DIR/deploy/launchd"
DEST_DIR="$HOME/Library/LaunchAgents"

PLATFORM_LABEL="com.atip.platform"
SNAPSHOT_LABEL="com.atip.publish-snapshot"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "launchd is macOS-only. On Linux use systemd (see deploy/cloud/cloud-init.yaml)." >&2
    exit 1
fi

bootout() {   # unload an agent, tolerating "not loaded"
    local label="$1"
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
}

install_one() {
    local label="$1"
    local src="$SRC_DIR/$label.plist"
    local dest="$DEST_DIR/$label.plist"
    [[ -f "$src" ]] || { echo "missing $src" >&2; exit 1; }

    mkdir -p "$DEST_DIR" "$ATIP_DIR/atip_data"
    # Substitute the real project directory. | as the sed delimiter so a path
    # containing / needs no escaping.
    sed "s|__ATIP_DIR__|$ATIP_DIR|g" "$src" > "$dest"

    # plutil validates the XML before launchd sees it: a malformed plist
    # otherwise fails with an unhelpful "Load failed: 5: Input/output error".
    plutil -lint "$dest" >/dev/null

    bootout "$label"
    launchctl bootstrap "gui/$UID" "$dest"
    echo "  installed $label  ->  $dest"
}

uninstall_all() {
    for label in "$PLATFORM_LABEL" "$SNAPSHOT_LABEL"; do
        bootout "$label"
        rm -f "$DEST_DIR/$label.plist"
        echo "  removed $label"
    done
}

chmod +x "$ATIP_DIR/start_atip.sh" "$ATIP_DIR/publish_snapshot.sh" 2>/dev/null || true

case "${1:-all}" in
    --uninstall|uninstall) uninstall_all ;;
    platform)              install_one "$PLATFORM_LABEL" ;;
    snapshot)              install_one "$SNAPSHOT_LABEL" ;;
    all)                   install_one "$PLATFORM_LABEL"; install_one "$SNAPSHOT_LABEL" ;;
    *) echo "usage: $0 [all|platform|snapshot|--uninstall]" >&2; exit 2 ;;
esac

echo
echo "Project: $ATIP_DIR"
echo "Check:   launchctl list | grep com.atip"
echo "Logs:    tail -f $ATIP_DIR/atip_data/launchd.err"
echo "Stop:    launchctl bootout gui/$UID/$PLATFORM_LABEL"
echo
echo "Note: the snapshot agent needs atip_data/publish.json (targets + tokens)."
echo "      See publish_snapshot.py's docstring."
