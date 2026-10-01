# W31: Production hardening handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. The tree compiles.

- **Scratch smoke test** (an isolated working folder with its own atip_data and a copy of the production DB) covered:
  - keygen → verified backup → encrypted off-site copy → restore drill from the off-site copy;
  - decrypt plus a tamper test;
  - vault migration;
  - key rotation, after which the vault and the old backup still decrypted.
- **Executed rollback drill:** a scratch clone, W30 → ATIP-W24-RC1. **PASSED, RTO 47.6 s**, with production (PID 115268) untouched.
- **API reference:** generated.

ChatGPT testing is pending.
**Branch:** `w31-production-hardening`, from `w30-advanced-quant`. Not merged, not deployed.

## Delivered

| ID | Feature | Where |
|---|---|---|
| OPS-06 | Off-site, encrypted backup + restore drill | `ops/backup.py`. See "Backups" below |
| SEC-04 | Encryption: credentials + key rotation | `ops/vault.py`, `ops/crypto.py`. See "Credentials" below |
| API-05 | Generated API reference | `python -m ops api-docs` → `docs/openapi.json` + `docs/API_REFERENCE.md`. 362 method + path pairs in 49 groups, with the **permission** each requires (the authz function itself), the token requirement, parameters and a summary |
| OPS-05 | Documentation | `docs/USER_GUIDE.md`: the daily schedule, every page, every W27–W31 switch, routine tasks, troubleshooting. DR and rollback docs updated |
| OPS-10 | Resilience on data sources | Circuit breakers on every Dhan market-data call, the NSE client, yfinance (plus retry) and each news feed. When a breaker is open, calls fail fast and callers handle it like any fetch error. Orders are never retried |
| OPS-11 | Rollback drill (executed) | `python -m ops rollback-drill`. See "Rollback drill" below. **Production deployment remains your decision** |

### Backups (OPS-06)

- **Encryption:** AES-256-GCM in 4 MiB authenticated chunks, which also catches reordering and truncation, plus a sha256 sidecar.
- **Off-site copy:** to `ops.backup_offsite_dir`, re-hashed at the destination, keeping 14. It is never copied in plaintext unless `ops.backup_offsite_plaintext` is true.
- **Restore drill:** weekly (Sunday 10:00), preferring the encrypted off-site copy. It checks integrity and row counts against those recorded at backup time, times the run and alerts on failure.

### Credentials (SEC-04)

- **Vault:** an encrypted credential vault (`vault-migrate` is a dry run by default). With `--apply` it verifies the copy and backs up config.json first.
- **Lookup:** `ops/secrets` and the legacy Dhan / Kite / Telegram readers read from the vault.
- **Key rotation:** `rotate-key` re-encrypts MFA secrets and the vault under a new key.
  - The old key stays readable as `.previous`, so old encrypted backups still restore.
  - It refuses when the key comes from env / `.env`, because that source would override the rotated key file.

### Rollback drill (OPS-11)

- **What it does:** clone → restore a verified backup → dashboard-only on port 8078 → postcheck → **timed rollback** (stop its own PID, reset to the tag, re-restore the DB, start, postcheck) → record.
- **Why not the PowerShell scripts:** they stop every `python main.py` on the machine, so they cannot be rehearsed next to production. The drill controls only its own process. For pre-W31 releases it renames the single-instance mutex **in the clone only**.

## Defects found and fixed

- **Inbound webhooks broken since W8.**
  - **Cause:** `dashboard/ops_routes.py` has `from __future__ import annotations`, so `request: Req` (a closure parameter) could not be resolved. FastAPI treated `request` as a JSON body field.
  - **Effect:** `POST /api/webhooks/{source}` (signed inbound webhooks) and `POST /api/ops/webhooks` never received the real request object.
  - **Fix:** they now use `fastapi.Request`. Verified: an unconfigured source now gets its proper 503.
  - **How it surfaced:** the same defect made OpenAPI generation fail.
- **First rollback drill FAILED**, and was recorded as such: the clone's main.py took the production single-instance mutex and refused to start. Fixed with `ATIP_INSTANCE_NAME` (W31+) and the in-clone rename for older releases. The second drill passed.

## Owner actions

1. `python -m ops keygen` (once). **Store a copy of `atip_data\secrets\ATIP_ENCRYPTION_KEY` outside the machine and outside the off-site folder.**
2. `python -m ops vault-migrate` (review), then `--apply`. Once ATIP has run a day on the vault, delete the `config.json.pre-vault-*` backup (it holds plaintext).
3. Set `ops.backup_offsite_dir`. Then `python -m ops offsite-copy` and `python -m ops restore-drill` once by hand.
4. Deploying W25–W33 to production is yours to authorize. Rehearse it first with `python -m ops rollback-drill --to ATIP-W24-RC1`.

## Not built / limits

- TLS: only with ENT-07 (exposure blocked by design).
- Per-route OpenAPI response schemas.
- Live outage behaviour of the breakers has not been observed.

## Files

- **New:** `ops/{vault,api_docs,rollback_drill}.py`, `docs/{USER_GUIDE,API_REFERENCE}.md`, `docs/openapi.json`
- **Changed:**
  - `ops/{backup,crypto,secrets,config,__main__}.py`, `main.py`;
  - `data/{dhan,nse_api,markets,news}.py`, `portfolio/zerodha.py`, `alerts/telegram.py`, `dashboard/ops_routes.py`;
  - `db/schema.py` (ops_restore_drill, ops_rollback_drill; ops_backup off-site columns), `pipeline/scheduler.py`;
  - `docs/{DISASTER_RECOVERY,ROLLBACK_PROCEDURE}.md`.
