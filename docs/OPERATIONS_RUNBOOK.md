# Operations runbook (W8)

All commands run in `/Users/agtci/Documents/Project_Documents/Projects/ATIP`. The owner performs every credential action. Development never handles tokens.

## Daily checks (2 minutes)

1. Overall health; READY is expected:

   ```bash
   python -m ops status
   ```

   A DEGRADED component names its reason.
2. Check the alert panel on the dashboard (`alert_log`), or `GET /api/ops/alerts`.
3. Check `python -m ops backups`: yesterday's 19:15 backup should be VERIFIED.

## Start / stop / restart

- **Start:** run a bare `python main.py` (or `start_atip.bat`). Never use `--dashboard` for production; it drops the scheduler.
- **Stop:** close the console, or end the `python main.py` process.
- **Autostart:** autostart may relaunch ATIP about 40 s after a kill. The single-instance guard refuses a second instance, so check that only one is running.
- **Startup log lines to expect:**
  - `ATIP environment: development`
  - `TRADING SAFETY: LIVE_TRADING_ENABLED = FALSE (...)`
  - config and secrets warnings, if any.

## Common situations

| Symptom | Check | Action |
|---|---|---|
| `/health/broker` DEGRADED, last sync FAILED "DH-901 token expired" | log: DH-901 | **Owner** updates the Dhan access token in the secret store or config, then restarts. `python -m ops rotate DHAN_ACCESS_TOKEN` records it |
| `data_stale` alert | `/health/data` or `python -m ops status` | Check the `pipeline_log` failures for the source; catch-up runs at start and at 08:20 and 12:20; re-run with `python main.py --run postmarket` |
| `scheduler` alert (heartbeat old) | is the process alive? `/health/live` | Restart with a bare `python main.py` |
| Job SKIPPED "locked" | `GET /api/ops/jobs` locks | Another runner holds it. A lock owned by a dead process is taken over automatically, and any lock expires after 6 h |
| `backup` alert | `python -m ops backups` (error column) | Fix the cause (disk, lock), then run `python -m ops backup` |
| `disk_space` alert | disk usage | `python -m ops prune`; clear old `atip.db.bak-*` manually (owner) |
| `live_trading` alert | `python -m ops safety` | **Treat as an incident.** Set `execution.live_trading_enabled` to false and `execution.mode` to PAPER in config.json, then restart |
| Many 5xx (`error_rate`) | `atip.jsonl` entries with `error_code` INTERNAL | Look up the request id from the response in the log |
| Suspected audit tampering | `python -m ops audit-verify` | `first_break` names the first altered row; preserve the database copy |
| Suspected secret leak | `python -m ops scan`; log review | Owner rotates at the provider, then `python -m ops rotate <NAME>`, then restart |

## Kill switch (trading)

The W1 kill switch (`orders/risk.py`, dashboard) halts all trading. W4 checks it at risk evaluation and again at order validation. Use it first in any trading incident.

## Configuration changes

1. Edit `atip_data/config.json`. Keep a copy first.
2. Validate; errors must be fixed:

   ```bash
   python -m ops validate
   ```

3. Restart. The new fingerprint is recorded, and the changed key names are audited.

## Encryption key

- Create it once:

  ```bash
  python -m ops keygen
  ```

  It is written to `atip_data/secrets/ATIP_ENCRYPTION_KEY`. Back it up **separately** from database backups, because MFA secrets are unreadable without it.
- Never regenerate it while MFA users exist, unless you then reset their MFA.

## Useful commands

| Command | Purpose |
|---|---|
| `python -m ops status` | component health |
| `python -m ops validate` | configuration / secrets / migrations findings |
| `python -m ops safety` | trading-safety report |
| `python -m ops migrate` | apply pending migrations (also automatic at start) |
| `python -m ops backup` / `backups` / `prune` / `verify <f>` / `restore <id>` | backup operations |
| `python -m ops secrets` / `rotate <NAME>` | secret presence and rotation |
| `python -m ops monitor` | evaluate alert rules once (no notifications) |
| `python -m ops audit-verify` | audit hash chain |
| `python -m ops scan` | secret / config scan |
