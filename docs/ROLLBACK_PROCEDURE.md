# Rollback procedure (W9)

*The W8 `ROLLBACK.md`, renamed and extended for the W9 release process.*

## Scripted rollback (W9)

Dry run first; it changes nothing:

```bash
powershell -ExecutionPolicy Bypass -File deploy
ollback_release.ps1 -ToRef backup/pre-w9-master
```

Code-only rollback (the normal case):

```bash
powershell -ExecutionPolicy Bypass -File deploy
ollback_release.ps1 -ToRef backup/pre-w9-master -Authorize
```

To also restore the database and/or the configuration:

```bash
powershell -ExecutionPolicy Bypass -File deploy
ollback_release.ps1 -ToRef <tag|branch> -RestoreBackup <backup_id> -RestoreConfigFrom <release_id> -Authorize
```

The script:
1. takes a VERIFIED safety backup of the current database;
2. stops ATIP;
3. `git reset --hard` master to the target. Later commits stay on their branch or tag, and untracked files are not touched;
4. optionally restores the database: a verified copy goes to a new file, the live file is kept as `atip.db.pre-rollback-<ts>`, and the copy is swapped in;
5. optionally restores the configuration from `atip_data/releases/<id>/config.json.backup`;
6. starts `python main.py`;
7. runs the postcheck (health, logs, scheduler, LIVE_TRADING_ENABLED = FALSE);
8. records `ROLLED_BACK` in `atip_data/releases/history.jsonl`.

**Limitations:**
- The database rollback loses everything written after the chosen backup.
- It is a manual decision (`-RestoreBackup`) and is never automatic.
- Rollback targets for W9: `backup/pre-w9-master` (= W8 `a7d7187`) and `ATIP-W9-RC2` (the release itself).
- The scripts have been syntax-checked only. **A rollback has not been executed or tested.**


Rollback has two parts, **code** and **database**. Schema changes in ATIP are additive: new tables, columns, indexes and triggers, and nothing dropped. Older code therefore runs against a newer schema, and a code-only rollback is usually enough.

## Code rollback

1. Stop ATIP.
2. Find the target commit with `git -C /Users/agtci/Documents/Project_Documents/Projects/ATIP log --oneline --decorate -20`. Useful targets:
   - the `backup/pre-<wave>-master` branch;
   - the tag `w5-final-prod-before-w6`.
3. Move `master` back:

   ```bash
   git -C /Users/agtci/Documents/Project_Documents/Projects/ATIP reset --hard backup/pre-w8-master
   ```

   This is destructive for commits after that point. They stay reachable on their feature branch, for example `w8-production-hardening`.
4. Start with a bare `python main.py`.

## Database rollback

Use this only when the new release wrote data that must be undone, or damaged the database.

- Restore the pre-release backup (BACKUP_RESTORE.md, restore procedure). Data written since then is lost.
- Or undo individual W8 objects. This is safe, because W8 data is operational only:

| Migration | Rollback |
|---|---|
| 0001 baseline | none (validation only) |
| 0002 W8 tables | `DROP TABLE` the `ops_*` tables, `enterprise_refresh_token`, `schema_migrations` (the W7 code ignores them; leaving them is harmless) |
| 0003 indexes | `DROP INDEX idx_prices_date, idx_lq_timestamp, idx_pipeline_log_status, idx_ent_audit_at` |
| 0004 audit triggers | `DROP TRIGGER trg_ent_audit_no_update, trg_ent_audit_no_delete, trg_oms_event_no_update, trg_oms_event_no_delete` |
| W8 columns | `enterprise_user.mfa_*` and `enterprise_audit.prev_hash/row_hash` can stay: W7 code does not read them |

The rollback note for each migration is also stored in `schema_migrations.rollback_note`.

## Configuration rollback

`ops_config_version` records the fingerprint and the changed keys at each start. Restore `atip_data/config.json` from the owner's copy, or use the `config.json.bak-*` files.

## W8-specific switches (no rollback needed)

Each of these reverts a W8 behaviour through configuration. A restart is required.

| To revert | Set |
|---|---|
| Structured JSON log | `ops.json_logs: false` |
| Scheduled backup | `ops.backup_enabled: false` (not in production) |
| Monitor | `ops.monitor_enabled: false` |
| Stale-data BUY check | `execution.max_market_data_age_sessions: null` |
| Migrate-once | `ATIP_MIGRATE_EVERY_CONNECTION=1` restores migrations on every connection |

The ops middleware and health routes have no switch. They are removed by the code rollback.

## Rehearsing a rollback (W31)

> ⚠ `deploy\rollback_release.ps1` and `deploy\deploy_release.ps1` stop **every** python process whose command line is `main.py`. Production runs as a bare `python main.py`, so the scripts cannot tell it apart from a test copy. **Never run them "to try" while the live ATIP is up.**

To rehearse, use the drill:

```bash
python -m ops rollback-drill --from HEAD --to <release tag> [--port 8078] [--keep]
```

What the drill does:

- runs a scratch clone of the repository;
- uses a verified backup as the database;
- runs the dashboard only on its own port (no scheduler, no broker feed beside production);
- stops nothing but its own process.

When the target release predates W31, the clone's single-instance mutex is renamed **in the clone only**. The result, with the measured RTO, is in `ops_rollback_drill` and `atip_data\releases\history.jsonl`.
