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
| Bad release | errors after deploy; health DEGRADED/FAILED | ROLLBACK.md: code rollback via git, database via the pre-release backup |
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
