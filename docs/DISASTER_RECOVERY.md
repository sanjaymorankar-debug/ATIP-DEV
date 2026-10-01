# Disaster recovery (W8)

## Objectives

| | Target | Basis |
|---|---|---|
| **RPO** (data loss) | ≤ 24 h | daily verified backup at 19:15, plus pre-release backups |
| **RTO** (time to restore service) | ~10–15 min | stop, restore the file (BACKUP_RESTORE.md), start |

Market data is largely re-derivable. Bhavcopy and Dhan history can be re-fetched, and the post-market catch-up recovers missed sessions. The irreplaceable data is:
- strategy definitions and decisions;
- risk decisions, orders and fills (paper book);
- ML model registry and artifacts (`atip_data/ml`);
- enterprise users and audit;
- configuration.

## Scenarios

| Scenario | Detection | Recovery |
|---|---|---|
| ATIP process crashed / stopped | `/health/live` down; scheduler heartbeat rule (if another monitor runs); autostart relaunches at logon | Start with a bare `python main.py`. The single-instance guard prevents duplicates. Missed jobs are caught up at start |
| Database corruption | `integrity_check` failure in the nightly backup (backup rule, critical); SQLite errors in the log | Restore the newest VERIFIED backup (BACKUP_RESTORE.md); re-run catch-up |
| Bad release | errors after deploy; health DEGRADED/FAILED | ROLLBACK_PROCEDURE.md: code rollback via git, database via the pre-release backup |
| Disk full | `disk_space` rule (< 2 GB) | Free space; prune backups (`python -m ops prune`); old logs rotate automatically |
| Broker token expired (DH-901) | `/health/broker` DEGRADED, last portfolio sync FAILED, data jobs failing | Owner updates the Dhan token (never development), then restart |
| Machine loss / disk failure | — | **Not covered yet:** backups are on the same disk (W8-R2). Needs an off-machine copy of `atip_data/backups`, `atip_data/ml`, `atip_data/config.json` and `atip_data/secrets/` (owner decision) |
| Encryption key lost | MFA logins fail (fail closed) | Admin MFA reset for users (`/api/admin/users/{uid}/mfa-reset`); generate a new key. **Back up the key separately from the database** |
| Secret leaked | scan or log review | Owner rotates at the provider, updates the secret store, runs `python -m ops rotate <NAME>`, and restarts |

## DR drill (recommended monthly, not yet performed)

1. Run a restore:

   ```bash
   python -m ops restore <latest VERIFIED id>
   ```

2. Point a second process at the restored file and check it. Nothing is written to the live database:

   ```bash
   set ATIP_DB_PATH=atip_data\restore\<file>.db
   python -m ops status
   ```

3. Record the time taken; that is the measured RTO.

## W31: off-site copies and automated drills

**Encrypted off-site copy (OPS-06)**

- **Setup:** set `ops.backup_offsite_dir` (a second drive, NAS share or synced cloud folder). After each VERIFIED backup:
  - the database is encrypted (AES-256-GCM, 4 MiB authenticated chunks, the ATIP data key);
  - it is copied there with a `.sha256` sidecar, and re-hashed at the destination;
  - only `ops.backup_offsite_keep` (14) copies are kept.
- **No plaintext off-site:** without a data key nothing is copied unless `ops.backup_offsite_plaintext` is `true`.
- **Restoring from it:**

  ```bash
  python -m ops decrypt-backup <file.db.enc> --target atip_data\restore\from-offsite.db
  ```

  Then follow the restore steps above.
- **The key must survive the machine:** keep a copy of `atip_data\secrets\ATIP_ENCRYPTION_KEY` somewhere that is **not** the off-site folder. Without it the copies are unreadable.

**Restore drill (weekly, Sunday 10:00)**

- **What it does:** restores the newest backup, preferring its encrypted off-site copy, into a drill file. It checks integrity and compares key-table row counts with those recorded at backup time, times the run, then deletes the file.
- **Where results go:** `ops_restore_drill`. A failure alerts.
- **Run now:** `python -m ops restore-drill`.

**Rollback drill (OPS-11)**

- **Run:** `python -m ops rollback-drill --from HEAD --to <release tag>`.
- **What it does:** rehearses the documented rollback end to end on a **scratch clone** in the temp folder: dashboard-only on port 8078, the database restored from the newest backup, code reset to the tag, the database re-restored, postchecks before and after. It measures the RTO and records it in `ops_rollback_drill` and the release history.
- **Isolation:** it only stops the process it started. `deploy\rollback_release.ps1` cannot be rehearsed beside a live ATIP, because it stops *every* `python main.py` on the machine.
