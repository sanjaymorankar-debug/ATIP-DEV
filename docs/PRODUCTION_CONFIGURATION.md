# Production configuration (W8)

## Where configuration comes from

| Source | Scope | Notes |
|---|---|---|
| `atip_data/config.json` | everything | the existing file; W1–W7 modules read it directly |
| `atip_data/config.<env>.json` | W8 view (`ops.config.load()`) | deep-merged overlay per environment |
| `ATIP__SECTION__KEY` env vars | W8 view | for example `ATIP__OPS__RATE_LIMIT_ENABLED=true`; values are parsed as JSON |
| `ATIP_ENV` | environment | overrides `environment` in config.json |
| `ATIP_DB_PATH` | database file | for a staging, test or restore-drill database |
| `ATIP_MIGRATE_EVERY_CONNECTION` | db | `1` restores migrations on every connection |
| secrets | see SECURITY_ARCHITECTURE.md | environment > `.env` > `atip_data/secrets/<NAME>` > legacy config.json |

W1–W7 modules still read `config.json` directly. Overlays and env overrides therefore change W8 behaviour and the environment only. Adoption by older modules is incremental.

## Environments

| Environment | Purpose | Validation errors | Live execution |
|---|---|---|---|
| development (default) | the owner's machine today | logged, ATIP starts | impossible (gate requires production) |
| test | automated tests | logged | impossible |
| staging | pre-release copy (`ATIP_DB_PATH` to a copy) | logged | impossible |
| production | hosted / real money (future) | **refuse to start** (exit 2) | only if also mode LIVE + live_trading_enabled, and the W4 Dhan adapter still refuses |

## The `ops` section

| Key | Default | Production recommendation |
|---|---|---|
| tls_enabled | false | true behind the TLS 1.3 proxy (enables HSTS) |
| hsts_seconds | 31536000 | keep |
| cors_origins | [] | explicit origins only; `*` is an error |
| csp | null | set once pages move off inline scripts |
| max_request_bytes | 2000000 | keep |
| rate_limit_enabled | false | **true** (warning if off) |
| rate_limit_per_minute | 300 | tune per client |
| require_idempotency_for_orders | false | true |
| idempotency_ttl_hours | 24 | keep |
| backup_enabled | true | **true** (error if off) |
| backup_time | "19:15" | after post-market |
| backup_keep_daily / weekly | 7 / 4 | keep, plus an off-site copy |
| backup_dir | atip_data/backups | a separate volume if available |
| monitor_enabled / monitor_minutes | true / 15 | keep |
| json_logs | true | keep (ship to a collector) |
| migrate_every_connection | false | false |
| features | {} | feature flags `{"name": true}` read via `ops.config.feature()` |

## Production checklist (validation enforces the items marked ✱)

- ✱ `enterprise.enabled: true`: sign-in required. Bootstrap first with `python -m enterprise bootstrap`.
- ✱ `enterprise.accept_legacy_token: false`.
- ✱ `ops.backup_enabled: true`.
- ✱ `ATIP_ENCRYPTION_KEY` present (`python -m ops keygen`).
- ✱ No wildcard CORS.
- ✱ A non-local `dashboard_host` requires `ops.tls_enabled` (only with ENT-07, currently **BLOCKED**).
- ✱ `broker_env: LIVE` is refused outside production.
- ✱ `execution.live_trading_enabled: true` is refused outside production.
- `ops.rate_limit_enabled: true` (warning).
- Secrets moved out of config.json (warnings list each one).
- An off-machine backup copy (W8-R2).
- MFA enrolled for every admin account.

## Trading safety settings (unchanged defaults)

```json
"execution": {"mode": "PAPER", "live_trading_enabled": false, "max_market_data_age_sessions": 2},
"broker_env": "PAPER"
```

**LIVE_TRADING_ENABLED = FALSE.** Do not change it without an explicit owner decision. Even when changed, live execution requires `environment: production`, and the W4 Dhan adapter refuses every call; live execution is not built.
