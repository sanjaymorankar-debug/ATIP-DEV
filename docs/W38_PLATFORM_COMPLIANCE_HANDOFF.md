# W38 — Platform & compliance: handoff

**Branch:** `w38-platform-compliance` (worktree `D:\Projects\ATIP-dev-w38`). It sits on `w37-brokers-multi-asset` → `w36-ml-strategy-tooling` → `master`, and also contains the RC2 auto-recovery hotfix (`f60658e`).

**Scope:**
- DBS-05 PostgreSQL
- OPS-04 cloud deployment
- SEC-05 compliance monitoring
- ENT-17 data privacy
- ENT-09 mobile
- ENT-14 regulatory pack (in progress: legal work)

**Status:** developed. 15 new tests and the full suite pass. Not merged and not deployed.

## DBS-05 — PostgreSQL (see docs/POSTGRESQL_MIGRATION.md)

**Pieces:**
- `db/postgres.py`: a translator for every SQLite construct ATIP uses, plus `PgConnection`.
- `db/dialect_scan.py`: an AST scan of all SQL. 99.7 % of 1,327 statements translate; the 4 that don't belong to SQLite-only tools.
- `tools/sqlite_to_postgres.py`: upgrades the copy to the release schema, then migrates schema, triggers and data, resets identities and verifies.

**Proven on a throwaway PostgreSQL 16 with a copy of the live database:**
- 213 tables and 497,317 rows, identical table by table.
- 198 GET routes with 0 server errors.
- Upserts, alerts, health, append-only triggers and catalog queries verified.

**Runtime switch:** opt-in only, needing all three of `ATIP_DATABASE_URL`, `database.backend=postgresql` and `database.allow_experimental=true`. A stray `DATABASE_URL` is ignored. SQLite remains the default.

**Bug found by this test:** a W36 regression that also hit SQLite. W36 had added the derivatives factors to `atip_factors@1`, so every `/api/quant/*` route returned 500 on an existing database. They are research-only again, and `ensure_builtin_set` now registers a new version instead of raising.

## OPS-04 — Containers / cloud (see docs/CONTAINER_DEPLOYMENT.md)

**Files:**
- `Dockerfile`: IST, non-root, volume, health check.
- `docker-compose.yml`: ports on 127.0.0.1 only; optional `postgres` profile.
- `deploy/atip.env.example`.
- `deploy/cloud/cloud-init.yaml`: SSH-only firewall, systemd unit.

**Other changes:**
- The `ATIP_DASHBOARD_HOST` env sets the bind inside a container.
- LF line endings are pinned for container files.

**Not built:** Docker isn't installed on the dev laptop. Public exposure stays with ENT-07.

## SEC-05 — Compliance monitoring (`ops/compliance.py`, `/compliance`)

**14 checks**, daily at 07:50 and on demand:
- audit-chain integrity
- live-trading gate
- algo order controls
- dashboard exposure
- admin MFA
- plaintext secrets
- encryption key
- secret rotation
- backup recency
- restore drill
- retention purge
- DSR SLA
- data inventory
- regulatory gates

Each run is stored with its evidence (`compliance_run` / `compliance_result`). An alert fires only when a check gets worse.

**First run on a copy of the live data:** 6 pass, 5 warn, 0 fail. The real findings for the owner:
- `risk_limits` not set
- 7 secrets in plaintext `config.json`
- no `ATIP_ENCRYPTION_KEY`
- no restore drill

## ENT-17 — Data privacy (`enterprise/privacy.py`)

- **Inventory:** every table is classified (explicit entries plus prefix rules); unclassified tables fail the check.
- **DSR SLA:** due date (`saas.dsr_sla_days`, 30), overdue flag, daily reminders inside `saas_daily`.
- **Privacy notice:** `/privacy` is generated from the inventory. It is public and stays **DRAFT** until `saas.privacy_policy_approved`.
- Inventory and DSR tabs are on `/compliance`.

## ENT-09 — Mobile (`/m`)

- An installable PWA with a read-only phone summary: market, indexes, global, buy/sell, trade of the day, P&L, pipeline, alerts.
- The service worker caches only the shell. The page keeps its last data for offline viewing (localStorage on the phone).
- **No token** is in the page.
- **Gaps:** no native store apps. A phone can reach `/m` only through a private tunnel (e.g. Tailscale) or the gated ENT-07 exposure.

## ENT-14 — Regulatory register (IN PROGRESS)

- `ops/regulatory.py` holds 10 items for a qualified professional to confirm, explained in `docs/ENT14_REGULATORY_PACK.md`:
  - SEBI RA/IA
  - retail algo framework
  - broker API terms
  - NSE data redistribution
  - DPDP
  - CERT-In
  - terms
  - payments/GST
  - live-risk sign-off
  - annual review
- Sign-off needs a reviewer and a reference, and is audited.
- The compliance check fails if multi-user or live trading is turned on with that gate's items open.

## Files

**New:**
- `db/postgres.py`, `db/dialect_scan.py`, `db/schema_w38.py`
- `ops/compliance.py`, `ops/regulatory.py`
- `dashboard/w38_routes.py`, `dashboard/w38_page.py`
- `Dockerfile`, `.dockerignore`, `docker-compose.yml`
- `deploy/atip.env.example`, `deploy/cloud/cloud-init.yaml`
- `docs/POSTGRESQL_MIGRATION.md`, `docs/CONTAINER_DEPLOYMENT.md`, `docs/ENT14_REGULATORY_PACK.md`
- `tests/test_w38_platform_compliance.py`

**Changed:**

| File | Change |
|---|---|
| `db/backend.py` | Delegates to postgres.py |
| `db/schema.py` | Runtime switch, W38 tables |
| `tools/sqlite_to_postgres.py` | Upgrade, triggers, identities |
| `quant/factors.py` | Bug fix |
| `enterprise/privacy.py` | Inventory, SLA, notice |
| `enterprise/w32.py` | DSR reminders |
| `enterprise/authz.py` | Public privacy / PWA routes; compliance rules |
| `dashboard/server.py` | Routes, nav links |
| `dashboard/security.py` | `ATIP_DASHBOARD_HOST` |
| `ops/config.py` | Experimental-PostgreSQL validation |
| `pipeline/scheduler.py` | 07:50 compliance job |
| `config_template.json` | `database` section, privacy notes |
| `.gitignore`, `.gitattributes` | Env/password files ignored; LF pinned |
| `docs/ATIP_MASTER_TRACKER.csv`, `docs/WAVE_STATUS.md` | W38 rows |

## For QA

1. `python -m pytest -q`: everything passes.
2. `python -m ops.compliance` prints 14 checks. Then on `/compliance`:
   - trying to sign off a register item without a reviewer is refused (400);
   - inventory: no unclassified tables;
   - the PostgreSQL tab shows 99.7 %.
3. `/privacy` shows the DRAFT banner and its sections.
4. `/m` on a phone-sized window loads and installs; offline it shows the last data.
5. **PostgreSQL** (a machine with PG 16 and `psycopg[binary]`): run the migration on a copy, then compare row counts. Optionally run the dashboard on it with the three switches set.
6. **Docker** (a machine with Docker): `docker compose up -d --build`. Check `/health/live` and that the port is reachable only on 127.0.0.1.
