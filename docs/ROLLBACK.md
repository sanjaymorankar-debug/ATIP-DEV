# Rollback (W8)

Rollback has two parts, **code** and **database**. Schema changes in ATIP are additive: new tables, columns, indexes and triggers, and nothing dropped. Older code therefore runs against a newer schema, and a code-only rollback is usually enough.

## Code rollback

1. Stop ATIP.
2. Find the target commit with `git -C D:\Projects\ATIP log --oneline --decorate -20`. Useful targets:
   - the `backup/pre-<wave>-master` branch;
   - the tag `w5-final-prod-before-w6`.
3. Move `master` back:

   ```bash
   git -C D:\Projects\ATIP reset --hard backup/pre-w8-master
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
