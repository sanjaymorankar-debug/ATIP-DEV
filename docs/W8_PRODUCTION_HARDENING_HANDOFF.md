# W8 — Production Hardening: development handoff

**Status:** developed 2026-09-25 and merged to MAIN. **Functional QA: NOT PERFORMED.**
**Independent testing (ChatGPT): PENDING.** Nothing in this document has been tested.
Only build and integration checks were run (section 8).

**Trading safety:** `LIVE_TRADING_ENABLED = FALSE`. W8 adds no way to open the live gate. It adds a
condition: the environment must be `production`. It also adds two checks: stale market data blocks a
BUY, and LIVE orders are checked against the market session.

## 1. What W8 adds

A new package, `ops/`, holds the operations layer. Existing modules get small hooks into it.

| Area | Module | Summary |
|---|---|---|
| Configuration | `ops/config.py` | Environments (development / test / staging / production), `config.<env>.json` overlay, `ATIP__SECTION__KEY` environment overrides, validation, feature flags, fingerprint + `ops_config_version` + audit of changed keys |
| Secrets | `ops/secrets.py` | Resolution order: environment, `.env`, `atip_data/secrets/<NAME>`, then legacy `config.json`. Presence and format validation. Access log records names only. Rotation dates. Masking |
| Encryption | `ops/crypto.py` | AES-256-GCM field encryption with a context-bound AAD. Key in `ATIP_ENCRYPTION_KEY` (`python -m ops keygen`). Never falls back to plaintext |
| Logging | `ops/logs.py` | A secret-masking filter on every handler. JSON log `atip_data/atip.jsonl` with request, correlation, tenant, user and job ids |
| Metrics | `ops/metrics.py` | In-process counters, gauges and histograms plus database gauges, in Prometheus text format at `/api/ops/metrics` |
| Errors | `ops/errors.py` | `AtipError` hierarchy with codes and an envelope `{"error": {code, message, request_id, retryable}}`. No stack traces reach clients |
| Resilience | `ops/resilience.py` | Timeout, retry with jittered backoff, circuit breakers. Order submit and cancel are marked `@non_idempotent`, so `retry()` refuses them |
| Idempotency | `ops/idempotency.py` | `Idempotency-Key` on mutating API calls. A 2xx response is replayed; the same key with a different body gets 409. Can be required on order routes |
| HTTP layer | `ops/http.py` | Outermost ASGI middleware: request ids, the `/api/v1` alias, 2 MB limit, JSON validation, optional rate limit, security headers, v1 pagination, metrics. Audits mutating calls even when the enterprise layer is off |
| Health | `ops/health.py` | `/health`, `/health/live`, `/health/ready`, and `/health/{database, broker, data, scheduler, ml}`. Returns LIVE / READY / DEGRADED / FAILED |
| Migrations | `ops/migrations.py` | `schema_migrations`: versioned, checksummed, with rollback notes. 0001 baseline, 0002 W8 tables, 0003 indexes, 0004 append-only audit triggers |
| Backup / DR | `ops/backup.py` | Daily online backup at 19:15, verified by `integrity_check`, row counts and sha256. Retention applies only to its own files. Restore goes to a new file |
| Scheduler | `ops/jobs.py` | A lock per job name (a duplicate run is SKIPPED), a heartbeat every 60 s, a job overview |
| Data health | `ops/data_health.py` | Per-source freshness in NSE sessions, plus the latest data-quality score |
| Monitoring | `ops/monitor.py` | 12 rules evaluated every 15 min, with FIRING/RESOLVED state and a 6 h re-notify. Alerts go through the W1 alert channel |
| Webhooks | `ops/webhooks.py` | Inbound: HMAC signature, 300 s replay window, event-id dedupe. Outbound: signed, retried with backoff, DEAD letter, per-host breaker |
| Trading safety | `ops/trading_safety.py` | Report and startup log of every live-trading control |
| Scan | `ops/scan.py` | Secret patterns in tracked files, forbidden tracked files, config findings, pip-audit if installed |
| Startup | `ops/startup.py` | Runs from `main.py`: logging, validation (production refuses to start on errors), secrets, trading safety, migrations, config version |
| CLI | `ops/__main__.py` | `python -m ops status / validate / safety / migrate / backup / backups / prune / restore / verify / keygen / secrets / rotate / audit-verify / monitor / scan` |

**Security additions in `enterprise/`:**

- `enterprise/mfa.py`: TOTP MFA. Covers enroll, confirm, disable, admin reset, and the login `otp` field.
- Refresh tokens: login returns one. `POST /api/auth/refresh` rotates it, and reusing an old token revokes the whole family and every session.
- Audit hash chain: `prev_hash` / `row_hash`, checked by `verify_chain`, plus append-only triggers.

## 2. Changes to existing modules

**`db/schema.py`**
- `ATIP_DB_PATH` override.
- The additive migrations now run once per process per database file (W7-R8). They used to run on every `get_connection()`. `ATIP_MIGRATE_EVERY_CONNECTION=1` or `ops.migrate_every_connection` restores the old behaviour.
- `init_db()` forces a re-run.
- 13 `W8_TABLES` and 5 added columns.

**`pipeline/scheduler.py`**
- `run_job` takes the job lock (SKIPPED when another runner holds it), sets the job context id, and records job metrics.
- The loop writes the heartbeat.
- New jobs: `ops_monitor` (every 15 min), `ops_backup` (19:15), and `ops_webhook_dispatch` (every 5 min, only when a delivery is due).

**`main.py`**
- `ops.startup.run()` runs after the single-instance lock, in long-running modes only.
- A production configuration error exits with code 2. Any other failure is logged and ATIP starts as before.

**`dashboard/server.py`**
- Registers `dashboard/ops_routes.py`.
- Installs the ops middleware (outermost) and the error handlers.

**`dashboard/execution_page.py`**
- Every mutating call sends an `Idempotency-Key`.
- Order create and cancel use keys tied to the decision or order, so a double click cannot act twice.

**`dashboard/enterprise_routes.py`**, **`enterprise_page.py`**
- New routes: `/api/auth/refresh`, `/api/account/mfa*`, `/api/admin/users/{uid}/mfa-reset`.
- The refresh cookie is scoped to the refresh route.
- The login page has an MFA code field.

**`enterprise/authz.py`**
- Health probes, signed webhooks and `/api/auth/refresh` are public.
- `/api/ops/*` requires `system:operate`.

**`execution/config.py`**
- The live gate also requires `environment == production`.
- New setting: `max_market_data_age_sessions` (default 2).

**`execution/risk_engine.py`**
- New check `market_data_fresh`: a BUY is REJECTED when the symbol's last daily bar is more than 2 sessions old.

**`execution/order_manager.py`**
- LIVE orders are validated against the NSE session.
- `submit_order` and `cancel_order` are `@non_idempotent`.

**`config_template.json`**
- New section 15: `environment` and `ops`.
- New setting `enterprise.refresh_days`.

## 3. Defaults (nothing changes behaviour for the owner until configured)

| Setting | Default |
|---|---|
| environment | development |
| rate limit | off |
| CORS | none (same origin) |
| CSP | none (existing pages use inline scripts) |
| HSTS | only when `tls_enabled` |
| Idempotency-Key required on orders | no; `/trading` sends one anyway |
| Backups | on, 19:15, 7 daily + 4 weekly |
| Monitor | on, every 15 min |
| JSON logs | on |
| Enterprise layer | still off (W7 default) |

## 4. Database

**New tables:**
- `schema_migrations`
- `ops_config_version`, `ops_secret_access`, `ops_secret_meta`
- `ops_backup`, `ops_job_lock`, `ops_heartbeat`, `ops_alert`, `ops_idempotency`
- `ops_webhook_event`, `ops_webhook_endpoint`, `ops_webhook_delivery`
- `enterprise_refresh_token`

**New columns:**
- `enterprise_user`: `mfa_enabled`, `mfa_secret_enc`, `mfa_pending_enc`
- `enterprise_audit`: `prev_hash`, `row_hash`

**New indexes:**
- `idx_prices_date`
- `idx_lq_timestamp`
- `idx_pipeline_log_status`
- `idx_ent_audit_at`

**New triggers:** append-only on `enterprise_audit` and `oms_order_event`.

Everything is additive and nothing is dropped. Rollback notes are in `schema_migrations` and `docs/ROLLBACK_PROCEDURE.md`.

## 5. Owner actions (not done by development — credentials are the owner's)

1. Restart ATIP with a bare `python main.py` (not `--dashboard`) to load W8.
2. Optional: run `python -m ops keygen` to create the encryption key, and back it up. This is needed for MFA.
3. Optional: move secrets out of `config.json` into the environment, `.env` or `atip_data/secrets/`. Startup warns about each one still read from `config.json`.
4. Production later: set `environment: production`, enable the enterprise layer, enable rate limiting, and set up an off-machine backup copy. See `PRODUCTION_CONFIGURATION.md`.

## 6. Security notes

- ATIP remains bound to 127.0.0.1. Exposure (ENT-07) is still **BLOCKED**, and W8 does not change the binding.
- Secret values are never logged, stored in W8 tables, returned by an API, or printed by the CLI.
- Health endpoints return statuses and non-sensitive facts only.

## 7. Dependencies

- No new packages. `cryptography` and `python-dotenv` were already installed.
- `pip-audit` is not installed and not required.

## 8. Build / integration checks performed (not functional testing)

- `py_compile` of every changed file, and pyflakes clean on the changed modules.
- On a **copy** of the production database, with a sanitized config:
  - all 13 W8 tables and 5 columns are created;
  - migrations 0001–0004 applied with no drift;
  - the 4 indexes and 4 triggers are present;
  - the config version is recorded;
  - the audit chain verifies.
- The dashboard app imports with 181 routes, including the 18 ops/health/webhook routes and the 6 new auth routes. The middleware stack builds.
- Trading-safety report on the production execution settings: mode PAPER, gate closed, W1 `broker_env` PAPER, **LIVE_TRADING_ENABLED = FALSE**.
- Secret scan of tracked files: 0 findings. The single README hit is a truncated placeholder.

## 9. Test scenarios for independent testing (ChatGPT)

1. **Startup:** development starts with warnings only. `ATIP_ENV=production` with enterprise off refuses to start (exit 2). Migrations are recorded once; a second start applies nothing.
2. **Health:** each endpoint returns the right status. `/health/ready` returns 503 when the database is unavailable. No secret appears in any payload.
3. **Middleware:**
   - a request id is echoed back, and a supplied one is kept;
   - `/api/v1/scores` equals `/api/scores`;
   - a body over 2 MB returns 413, a non-JSON body 415, and malformed JSON 400;
   - security headers are present;
   - with `rate_limit_enabled`, requests get 429 and `Retry-After`.
4. **Idempotency:**
   - the same key and body returns the stored response with `Idempotent-Replay`;
   - the same key with a different body returns 409;
   - a failed request can be retried;
   - with `require_idempotency_for_orders`, a missing key returns 400.
5. **Order safety:**
   - a BUY on a symbol with stale bars is REJECTED (`market_data_fresh`);
   - a LIVE order outside session hours is rejected;
   - `retry()` refuses `submit_order`;
   - the live gate stays closed outside production.
6. **Scheduler:**
   - a second runner of the same job is SKIPPED;
   - a stale lock (dead pid) is taken over;
   - the heartbeat is updated;
   - `/health/scheduler` returns FAILED after 5 minutes without a beat.
7. **Backup:**
   - `python -m ops backup` produces a VERIFIED backup with sha256 and table counts;
   - a corrupted copy is FAILED;
   - prune keeps 7 daily + 4 weekly and never deletes `atip.db.bak-*`;
   - restore writes a new file and refuses the live path.
8. **Monitoring:** rules fire and resolve, with no repeat notification within 6 h. The live-trading rule fires if the gate is opened in a test config.
9. **Webhooks:**
   - a valid signature is accepted;
   - a bad signature returns 401;
   - an old timestamp returns 401;
   - a duplicate event id is accepted once;
   - no secret configured returns 503;
   - outbound delivery retries, then goes DEAD, and can be requeued.
10. **Enterprise (enabled):**
    - MFA: enroll, confirm, a login without `otp` returns `mfa_required`, a wrong code counts toward lockout, disable, admin reset.
    - Refresh: rotates tokens; reuse revokes everything.
    - Audit: `verify_chain` is ok, and a manual UPDATE of `enterprise_audit` is refused by the trigger.
11. **Secrets / logs:**
    - masking replaces configured secret values and token shapes in `atip.log` and `atip.jsonl`;
    - `ops_secret_access` holds names only;
    - `python -m ops secrets` never prints values.
12. **Config:**
    - an `ATIP__OPS__RATE_LIMIT_ENABLED=true` override and the `config.staging.json` overlay both appear in `/api/ops/config`;
    - a change produces a new `ops_config_version` row plus an audit row;
    - W1–W7 modules still read `atip_data/config.json` directly (overrides apply to the W8 `ops` view and the environment only; see `PRODUCTION_CONFIGURATION.md`).
