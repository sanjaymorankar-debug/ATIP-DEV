# How to run and roll back: UX audit fixes, 3 Nov 2026 (times IST)

## Run it

1. On the Mac, check nothing else is half-done in the folder: `cd ~/ATIP && git status`. Another session's uncommitted work goes
   live with a restart.
2. Take a backup: `.venv/bin/python -m ops backup --kind pre-release`.
3. Either stay on this working tree (branch `claude/ux-audit-fix-2026-11-03`), or unzip
   `deliverables/2026-11-03/ATIP_ux_audit_fix_delta_2026-11-03.zip` over `~/ATIP` (paths are relative to the project root;
   skip the `_orig/` folder).
4. Restart the dashboard: `launchctl kickstart -k gui/$UID/com.atip.platform`. Open http://localhost:8000.
5. Phone apps: no reinstall needed for the web changes. The native banner change ships in app version 2026.11.03.1 (build 5)
   once the apps are built (see `mobile/README.md`; both builds were blocked on this Mac by missing toolchains).

Nothing to migrate: no database, config or scheduler change. Everything stays PAPER unless you switch LIVE yourself.

## Check it (2 minutes)

- Top bar: a feed pill reads "Market closed · last tick HH:MM IST" outside market hours; the bell opens the latest five alerts.
- ATIP Scores: one status strip, five KPI tiles, the table's first row visible without scrolling, "What do the scores mean?".
- Open Sell on any row: switches for "Exit legs fire without asking me" and "Require confirmation…", "Check broker", and the
  money at risk / charges / PAPER panel under the preview.
- Switch theme (moon icon): every page, the older ones included, follows light and dark.

## Roll back

1. Copy the files under the zip's `_orig/` folder back over the same paths in `~/ATIP` (they are the files as they were at
   08:18 IST, before this change; files listed as new in `docs/CHANGELOG_2026-11-03_ux_audit_fix.md` can be deleted).
   Or, on the branch: `git checkout -- <file>` does NOT work for this change alone, because the same files also hold the
   uncommitted C1–C4 fixes; use `_orig/`.
2. Restart the dashboard as above.
