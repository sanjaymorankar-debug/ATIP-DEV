# Backup and restore (W8)

## Automatic backups

- **When:** the scheduler job `ops_backup` runs daily at `ops.backup_time` (19:15). That is after the post-market pipeline (about 16:35) and the late EOD run.
- **How:**
  - The SQLite online backup API (`sqlite3.Connection.backup`) produces a consistent copy while ATIP keeps running. It includes WAL content.
  - Output: `atip_data/backups/atip-scheduled-YYYYmmdd-HHMMSS.db`.
- **Verification (every backup):**
  - `PRAGMA integrity_check` must return `ok`;
  - key tables must be readable, with row counts recorded;
  - the sha256 of the file is recorded.
- **Result:**
  - The `ops_backup` row gets status **VERIFIED** only when every check passed.
  - Otherwise it gets **FAILED** with the error, and the file is renamed `*.db.failed` for inspection.
  - A backup is never reported valid without verification.
- **Retention:**
  - After a VERIFIED backup, keep the newest 7 VERIFIED backups plus one per ISO week for 4 weeks.
  - Only `atip-*.db` files inside the backup directory that this module created are deleted.
  - The hand-made `atip_data/atip.db.bak-*` snapshots are never touched.
- **Monitoring:** the `backup` rule alerts when the latest backup FAILED or none has been VERIFIED for 26 h. It also appears on `/api/ops/metrics` as `atip_backup_age_hours`.

## Manual commands

Run all of these from `D:\Projects\ATIP`.

Take a verified backup now:

```bash
python -m ops backup
```

List backups with status, sha256 and integrity:

```bash
python -m ops backups
```

Check any database file:

```bash
python -m ops verify atip_data/backups/<file>.db
```

Apply retention:

```bash
python -m ops prune
```

Restore to a NEW file:

```bash
python -m ops restore <backup_id|path> [--target FILE]
```

## Pre-release backup

Take one before every merge that changes the schema. This procedure has been used since W1:

1. Take a verified backup:

   ```bash
   python -m ops backup
   ```

   Or use the dated manual snapshot `atip_data/atip.db.bak-before-<wave>-<ts>` made with the SQLite backup API and checked with `PRAGMA integrity_check`.
2. Record the backup id or file name in the release notes (PRODUCTION_DEPLOYMENT.md).

## Restore procedure

The live database is never overwritten by a tool.

1. Choose the backup:

   ```bash
   python -m ops backups
   ```

   Prefer the newest VERIFIED one before the incident.
2. Restore it to a new file. This verifies the source, copies it, and verifies the copy (the sha256 must match):

   ```bash
   python -m ops restore <backup_id>
   ```

   The result is written to `atip_data/restore/atip-restored-<ts>.db`.
3. Stop ATIP: close the ATIP console window, or end the `python main.py` process.
4. Keep the current database for forensics. Rename `atip_data/atip.db` to `atip.db.pre-restore-<ts>`, and move `atip.db-wal` and `atip.db-shm` aside with it.
5. Copy the restored file to `atip_data/atip.db`.
6. Start ATIP with a bare `python main.py` (not `--dashboard`).
7. Verify:
   - `python -m ops status`: database READY, migrations applied;
   - `/health/data` freshness;
   - the dashboard loads.
8. Data written after the backup is lost (RPO) unless it is re-derived. Market data can be re-fetched by the catch-up jobs: `run_postmarket_if_missing` runs at start.
