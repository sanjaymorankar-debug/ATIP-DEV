#!/usr/bin/env bash
# Apply the UI fixes v2 delta (3 Nov 2026) to an ATIP folder -- idempotent and safe to run again.
#
#   ./apply_ui_fixes_v2.sh [ATIP folder]        (default: /Users/agtci/ATIP)
#   ./apply_ui_fixes_v2.sh --dry-run [folder]   show what would change, change nothing
#   ./apply_ui_fixes_v2.sh --force [folder]     also replace files that were changed locally (backed up first)
#
# Run it from the unzipped delta folder (it holds MANIFEST.tsv and files/). For each file in the manifest:
#   * already the new version (same sha256)       -> skipped
#   * still the version the delta was built on    -> backed up, then replaced
#   * changed locally since (neither)             -> NOT replaced unless --force; listed as a conflict, so the Mac's
#                                                    uncommitted work is never overwritten silently
# Backups go to <ATIP>/atip_data/backups/ui_fixes_v2_<YYYYMMDD_HHMM>/ with the same paths. Nothing under orders/ or
# execution/ is in the delta (the script refuses a manifest that has one). It does not restart ATIP.
set -euo pipefail

DRY=0; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) break ;;
  esac
done
TARGET="${1:-/Users/agtci/ATIP}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$HERE/MANIFEST.tsv"

[ -f "$MANIFEST" ] && [ -d "$HERE/files" ] || { echo "Run this from the unzipped delta folder (MANIFEST.tsv and files/ missing)."; exit 2; }
[ -f "$TARGET/main.py" ] && [ -f "$TARGET/dashboard/shell.py" ] || { echo "$TARGET does not look like an ATIP folder (main.py, dashboard/shell.py)."; exit 2; }
if cut -f1 "$MANIFEST" | grep -E '^(orders|execution)/' >/dev/null; then
  echo "Refusing: the manifest touches orders/ or execution/ (trading behaviour must not change)."; exit 3
fi

sha() { if command -v shasum >/dev/null; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
STAMP="$(TZ=Asia/Kolkata date +%Y%m%d_%H%M)"
BACKUP="$TARGET/atip_data/backups/ui_fixes_v2_$STAMP"
applied=0; skipped=0; conflicts=0; added=0

# MANIFEST.tsv: path <TAB> sha256 of the new file <TAB> sha256 of the base version (or "-" for a new file)
while IFS=$'\t' read -r path new base; do
  [ -z "$path" ] && continue
  case "$path" in \#*) continue ;; esac
  src="$HERE/files/$path"; dst="$TARGET/$path"
  [ -f "$src" ] || { echo "  missing in the delta: $path"; exit 4; }
  if [ -f "$dst" ]; then
    cur="$(sha "$dst")"
    if [ "$cur" = "$new" ]; then skipped=$((skipped+1)); continue; fi
    if [ "$cur" != "$base" ] && [ "$FORCE" -ne 1 ]; then
      echo "  CONFLICT (changed locally, kept): $path"; conflicts=$((conflicts+1)); continue
    fi
    if [ "$DRY" -eq 1 ]; then echo "  would replace: $path"; applied=$((applied+1)); continue; fi
    mkdir -p "$BACKUP/$(dirname "$path")"; cp -p "$dst" "$BACKUP/$path"
  else
    if [ "$DRY" -eq 1 ]; then echo "  would add: $path"; added=$((added+1)); continue; fi
    added=$((added+1))
  fi
  mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"
  [ "$(sha "$dst")" = "$new" ] || { echo "  checksum mismatch after copy: $path"; exit 5; }
  [ -f "$BACKUP/$path" ] && applied=$((applied+1))
  case "$path" in *.sh) chmod +x "$dst" ;; esac
done < "$MANIFEST"

echo
echo "UI fixes v2 delta -> $TARGET ($(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST'))"
echo "  replaced: $applied   added: $added   already current: $skipped   conflicts kept: $conflicts"
[ "$DRY" -eq 1 ] && echo "  (dry run: nothing was changed)"
[ -d "$BACKUP" ] && echo "  backups: $BACKUP"
if [ "$conflicts" -gt 0 ]; then
  echo "  Files changed locally were NOT replaced. Commit the Mac's work first, then merge the branch with git,"
  echo "  or re-run with --force (each replaced file is backed up)."
fi
echo "Next: restart ATIP, then: python -m pytest -q tests/test_ui_fixes_v2.py tests/test_tech_signals_catchup.py"
