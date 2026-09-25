# W9: final release and production readiness (ATIP-W9-RC1)

**Deployment state:** RELEASE READY and DEPLOYMENT READY. **NOT DEPLOYED.**
**PRODUCTION DEPLOYMENT AUTHORIZATION REQUIRED.**

**Testing:**
- IMPLEMENTED and SMOKE VERIFIED (startup + health on a database copy).
- **NOT** functionally tested; independent ChatGPT testing is pending.

**Trading safety:** LIVE_TRADING_ENABLED = FALSE.

## 1. Scope

This W9 follows the revised brief ("final deployment, release and production readiness"). It adds **no trading functionality**. An earlier W9 prompt (enterprise SaaS) was started and then superseded. That work is parked, unmerged, on branch `w9-enterprise-saas` (`3aae821`); see KNOWN_ISSUES W9-P1.

## 2. What W9 changed

| Area | Change | Files |
|---|---|---|
| Trading safety | **Master switch for real-money orders on every path.** W1 order rules and the aggressive strategy (`orders/broker._place_order`, `broker_env` LIVE + confirm) now also need `execution.live_trading_enabled` true and `environment` production. Otherwise the order is refused (`BLOCKED_LIVE_DISABLED`, logged and alerted). The trading-safety report and the monitor's live_trading rule cover both paths | `orders/broker.py`, `ops/trading_safety.py`, `ops/monitor.py` |
| Security | Fix for the W7 workspace cross-tenant overwrite (W9-S1); `Secure` session/refresh cookies with TLS (W9-S2) | `enterprise/workspace.py`, `enterprise/config.py`, `dashboard/enterprise_routes.py` |
| Health | `/health/storage` (disk, backup dir, latest verified backup age) and `/health/market_data` (feed freshness in market hours, last broker data job, credential presence; no broker call). No queue exists, so there is no queue check | `ops/health.py`, `enterprise/authz.py` |
| Release tooling | `python -m ops release preflight | manifest | postcheck | record`; `python -m ops backup --kind pre-release` | `ops/release.py`, `ops/__main__.py` |
| Deployment | `deploy/deploy_release.ps1` and `deploy/rollback_release.ps1` for the existing Windows / `python main.py` model. Dry run by default; `-Authorize` to act | `deploy/` |
| Dependencies | `cryptography` declared; `requirements.lock.txt` (exact versions of the release environment) | `requirements*.txt` |
| Docs | PRODUCTION_DEPLOYMENT.md (was DEPLOYMENT.md), ROLLBACK_PROCEDURE.md (was ROLLBACK.md), PRODUCTION_CONFIGURATION.md (audit + master switch), RELEASE_CHECKLIST.md, WAVE_STATUS.md, KNOWN_ISSUES.md (P0–P3), this file | `docs/` |

There are no database schema changes, so no new migrations.

## 3. Inspection findings (2026-09-26, before changes)

**Git (`D:\Projects\ATIP`):**
- `master` at `a7d7187` (W8), clean apart from the owner's untracked `*.xlsx` files.
- Remote `origin` (GitHub `ATIP-DEV`) is 50 commits **behind** master; nothing has been pushed since W1.
- One tag, `w5-final-prod-before-w6`.
- Recovery branches `backup/pre-<wave>-master`.

**Runtime:**
- One Windows process, `python main.py` (PID 45796, started 00:42, W8 code), on 127.0.0.1:8000.
- SQLite WAL database of 69.6 MB; migrations 0001–0004 applied.
- Started by `start_atip.bat`, `atip_autostart.vbs` and `ATIP_TaskScheduler.xml`.
- No Docker, systemd, nginx or cloud.

**CI:** `.github/workflows/tests.yml` runs pytest on GitHub; it has not run since W1 because nothing was pushed.

**Configuration:**
- Environment `development` (unset).
- `execution` section absent, so PAPER / live off.
- `broker_env` PAPER.
- Enterprise off; ML and quant off.
- Credentials are in `config.json` (gitignored) and `.env`. No encryption key exists.

**Secrets in git:**
- None in tracked files.
- The history has one `dhan_access_token` match in the baseline commit `9676bcf`, which is a truncated placeholder (`eyJ0…`, contains "...") in README and `dhan_test.py`, not a token.

**Health (live, W8):**
- READY: database, broker, data, ml.
- DEGRADED: scheduler, because of the 2026-09-25 failures of `dhan_15min_bars` and `dhan_portfolio`.

**Trading paths:**
- W4 → Dhan adapter refuses every call.
- **W1 real orders were gated only by `broker_env` + confirm + kill switch, not by `LIVE_TRADING_ENABLED`.** Fixed in W9 (W9-T1).

**Backups:**
- Scheduled W8 backups have not run yet (first at 19:15); `atip_data/backups` does not exist.
- Dated manual snapshots exist for every wave.

## 4. Smoke verification (what was actually run)

**Build:**
- `python -m compileall` clean.
- pyflakes clean on the changed modules (plus one pre-existing warning in `orders/broker.py`).
- PowerShell parser: 0 errors on both scripts.

**Startup smoke:** the W9 code ran against a copy of the production database with a sanitized config, on port 8765.
- `ops.startup`: environment development, 0 pending migrations, trading safety FALSE.
- All health endpoints answered 200.
- `/api/v1/mh` answered.
- Security headers present.
- The server was then stopped. The live instance was not touched.

**Trading-safety report:** LIVE_TRADING_ENABLED false, master switch closed. An in-memory simulation of `live_trading_enabled: true` in development still returns "not allowed" (environment gate). No file was changed for this.

**Not run:**
- the pytest suite and CI;
- an authorized deployment;
- a rollback;
- a backup restore;
- any functional, API, UI, strategy, risk, ML or quant testing.

## 5. Readiness matrix

| Category | Status |
|---|---|
| Code complete | READY (W9 scope); W3–W8 IMPLEMENTED BUT NOT VERIFIED |
| Database ready | READY (no pending migrations; W9 adds none) |
| Configuration ready | PARTIAL (W9-C1 environment development; W8-R4 plaintext credentials) |
| Security ready | PARTIAL (static review done, P0 fixed; W8-R4 open; no penetration test performed) |
| Backup ready | PARTIAL (mechanism verified in W8; no scheduled backup yet; the deploy step takes a verified one) |
| Restore procedure ready | PARTIAL (documented and scripted; restore not tested) |
| Rollback ready | PARTIAL (scripted + `backup/pre-w9-master`; not executed) |
| Monitoring ready | READY |
| Health checks ready | READY |
| Deployment package ready | READY (scripts dry-run only) |
| Documentation ready | READY |
| Git release ready | READY (tag `ATIP-W9-RC1`; not pushed) |
| Production authorization | BLOCKED: **PRODUCTION DEPLOYMENT AUTHORIZATION REQUIRED** |
| Production deployment | NOT VERIFIED (not deployed) |
| Post-deployment verification | NOT VERIFIED |

## 6. Deploy (next operator, after authorization)

In `D:\Projects\ATIP`, dry run first:

```bash
powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC1
```

Then the authorized run:

```bash
powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC1 -Authorize
```

Rollback if needed:

```bash
powershell -ExecutionPolicy Bypass -File deploy\rollback_release.ps1 -ToRef backup/pre-w9-master -Authorize
```

## 7. ChatGPT QA handoff

1. **Trading safety.**
   - `broker_env: LIVE` with `execution.live_trading_enabled` false returns `BLOCKED_LIVE_DISABLED` for the W1 order rules and `strategy/live.py`, with an `order_log` row and an alert.
   - With true but environment development: still blocked.
   - W4 LIVE: the Dhan adapter refuses.
   - `python -m ops safety` reports every path.
2. **Health.**
   - `/health/storage` and `/health/market_data` report DEGRADED / READY / FAILED honestly: no backup, stale feed in market hours, missing credentials.
   - No secrets appear in any payload.
3. **Release tooling.**
   - `python -m ops release preflight ATIP-W9-RC1` FAILs a dirty tree, a wrong branch, a missing tag, no recent backup and live trading on.
   - `manifest` writes the manifest and config copies.
   - `postcheck` against a running instance.
   - `record` appends history.
4. **Deploy / rollback scripts.**
   - Dry runs change nothing.
   - Run `-Authorize` on a **copy** of the repository (not production), including the autostart relaunch adoption and the restore swap.
5. **W9-S1.** A user cannot overwrite another user's or tenant's watchlist, alert rule or report by passing its id (enterprise enabled).
6. **W9-S2.** Cookies carry Secure only when `ops.tls_enabled` is true.
7. **Regression.**
   - Run the existing pytest suite. `tests/test_risk_controls.py` covers the W1 broker path; check that no W1 test relied on `broker_env LIVE` + confirm reaching a mock.
   - Then the W3–W8 handoff scenarios (each handoff's test section).
