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

## The `wealth` section (W11–W20, all optional)

The wealth track is advisory. No setting here can place an order or change a trading setting.

| Key | Default | Effect |
|---|---|---|
| `enabled` | false | Runs the scheduled investor cycle after the post-market run (snapshot, ledger import, DNA / allocation / goals / rebalance, weekly performance report, wealth alerts). The API and `/wealth` work either way |
| `profile_validity_days` | 365 | An Investor DNA older than this is STALE |
| `default_inflation_pct` | 6.0 | Goals without their own inflation or a type default |
| `risk_free_pct` | 6.5 | Sharpe, Sortino, alpha |
| `benchmark` | nifty50 | Performance benchmark (any `prices_daily` symbol or nifty50 / niftybank / midcap150 / smallcap250) |
| `monte_carlo_paths` | 2000 | Goal success probability (100–20,000) |
| `rebalance_abs_band_pct` / `rebalance_rel_band_pct` | 5 / 25 | Drift bands |
| `rebalance_min_trade` | 5000 | Smallest rebalance leg (Rs) |
| `single_stock_cap_pct` | 10 | Concentration cap (% of assets) |
| `tactical_max_tilt_pct` | 10 | Largest tactical tilt per class (points) |
| `gold_domestic_premium_pct` | 9 | Added to international gold / silver spot |
| `cma` | built-in | Capital-market assumptions override: `returns`, `volatility`, `correlation` as `"A/B": rho` |
| `advisor_llm_enabled` | false | Claude narrates the advisor's evidence pack (sends it to Anthropic; key from the environment) |
| `advisor_llm_model` | claude-opus-5 | Narration model (must start with `claude-`) |
| `uat_owner` | null | **Beta only:** `uat:persona_<name>` makes the single-user UI act as a seeded UAT persona. Leave unset in production. `/health/wealth` reports DEGRADED while it is set |

## W9 master switch (all order paths)

From W9, **real-money orders on every path** need `execution.live_trading_enabled: true` **and** `environment: production` (`ops/trading_safety.live_trading_enabled()`).

| Path | Additional requirement | Status |
|---|---|---|
| W4 execution | `execution.mode: LIVE` | the Dhan adapter still refuses every call |
| W1 order rules and the aggressive strategy (`orders/broker._place_order`) | `broker_env: LIVE` + `confirm=True` | before W9 these did not consult `live_trading_enabled`; a refused order is logged as `BLOCKED_LIVE_DISABLED` and alerted |

## W9 configuration audit of the production instance (2026-09-26)

Values are never shown. Status is CONFIGURED / MISSING / INVALID / NOT REQUIRED.

| Variable (actual name in ATIP) | Where | Status |
|---|---|---|
| Database (`db.schema.DB_PATH`; `ATIP_DB_PATH` / `DATABASE_URL` overrides) | default `atip_data/atip.db` | CONFIGURED (SQLite; no URL needed) |
| `environment` / `ATIP_ENV` | config.json | NOT SET → development (see the note below) |
| Dashboard token (the local `SECRET_KEY` equivalent) | `atip_data/dashboard_token.txt` (gitignored) | CONFIGURED |
| `dhan_client_id`, `dhan_access_token` (broker keys) | config.json (legacy plaintext) | CONFIGURED (move to the secret store: W8-R4) |
| `kite_api_key`, `kite_api_secret` | config.json | CONFIGURED (optional) |
| `telegram_token`, `telegram_chat_id` | config.json | CONFIGURED |
| `ANTHROPIC_API_KEY` (news AI) | `.env` | CONFIGURED |
| `alpha_vantage_key` | config.json | CONFIGURED (optional) |
| `ATIP_ENCRYPTION_KEY` | not created | MISSING. Needed only for MFA / field encryption (owner: `python -m ops keygen`) |
| JWT secret | — | NOT REQUIRED (ATIP uses random opaque session tokens stored as digests, not JWTs) |
| Redis / queue | — | NOT REQUIRED (single process; `ops.shared_state_url` is not used in W9-RC2) |
| E-mail (SMTP) | — | NOT REQUIRED (no mail sender; W7-R5) |
| ML configuration (`ml` section) | defaults | CONFIGURED (defaults; `ml.enabled` false) |
| Quant configuration (`quant` section) | defaults | CONFIGURED (defaults; `quant.enabled` false) |
| Logging | `main.py` + `ops/logs.py` | CONFIGURED (atip.log 10 MB rotation, atip.jsonl 20 MB × 5, secret masking) |
| DEBUG | uvicorn `log_level="warning"`, FastAPI debug off, no reload | CONFIGURED (off) |
| CORS (`ops.cors_origins`) | default [] | CONFIGURED (same-origin only) |
| Session cookies | HttpOnly, SameSite=Strict; `Secure` when `ops.tls_enabled` (W9) | CONFIGURED |
| `execution.live_trading_enabled` | absent → default false | CONFIGURED = **FALSE** |
| `broker_env` | config.json | PAPER |

**Environment note.** The production instance runs single-user on 127.0.0.1 with the enterprise layer off. Its effective environment is therefore `development`.

Setting `environment: production` would make ATIP **refuse to start** until:
- the enterprise layer is enabled, with an admin bootstrapped;
- the encryption key exists;
- rate limiting is on.

This is by design (W8). Running as development has two effects:
- live trading is impossible, which is the required state;
- configuration errors are logged instead of refusing startup.

Recorded as W9-C1 (P2) in KNOWN_ISSUES.md.
