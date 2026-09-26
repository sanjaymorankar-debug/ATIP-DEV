# W20: Production hardening and release candidate for the wealth track (ATIP-REL-001)

**Release:** `ATIP-W20-RC1` (tag on branch `w20-release`, local, not pushed).
**Deployment state:** RELEASE CANDIDATE. **NOT DEPLOYED. NOT MERGED TO `master`.**
**Why not merged:** `D:\Projects\ATIP` (production) has `master` checked out, and the running process imports modules on demand. Merging would put the new code into the live folder before QA and authorization.

**Gates still open:**
1. The W18 suite has to be run (ChatGPT).
2. The W19 UAT exit criteria have to be met.
3. The owner has to authorize the deploy.

**Trading safety:** unchanged. The wealth track has no import path into orders / execution (W18 AST test). `LIVE_TRADING_ENABLED` stays false.

## 1. What W20 adds

| Area | Change | Files |
|---|---|---|
| Database | Versioned migration **0005 `w20_wealth_append_only`**. It verifies every `WEALTH_TABLES` table exists and adds append-only triggers (no UPDATE; no DELETE except the `uat` tenant) on `investor_profile_version`, `perf_ledger`, `perf_ledger_void`, `perf_report_run`, `wealth_allocation_run`, `wealth_goal_event`. Checksummed like 0003 / 0004; the rollback note is recorded | `ops/migrations.py` |
| Health | `/health/wealth` (public probe, nothing sensitive). READY while the track is unused. DEGRADED when the scheduled cycle is on and stale / partial, migration 0005 is pending, benchmark prices are stale, or `uat_owner` is set. FAILED if a table is missing. Included in `/health` | `wealth/health.py`, `ops/health.py`, `enterprise/authz.py` |
| Monitoring | W8 monitor rule `wealth_cycle` (warning), only when `wealth.enabled` | `ops/monitor.py` |
| Backup | Backup verification counts `investor_profile_version`, `wealth_holding`, `wealth_goal`, `perf_ledger` (whole-database backups already include every table) | `ops/backup.py` |
| Operations CLI | `python -m wealth status \| cycle \| health \| uat-* \| feedback \| triage` | `wealth/__main__.py` |
| Configuration | The `wealth` section is documented (all optional, advisory) | `docs/PRODUCTION_CONFIGURATION.md` |
| Status docs | WAVE_STATUS (W11–W20 table), KNOWN_ISSUES (WLT-1…8), master tracker rows 267–277 | `docs/` |

**Verified on a scratch copy of production:**
- 0005 applied in 0.9 ms and validates without drift.
- UPDATE / DELETE on the audit tables are refused; the UAT persona reset still works (exempt tenant); ledger voids still work (insert-only).
- `/health/wealth` READY.

## 2. The whole release (W11–W20)

| Wave | ID | Delivered |
|---|---|---|
| W11 | INV-001 | Investor DNA |
| W12 | WLT-001 | Multi-asset wealth dashboard |
| W13 | GOL-001 | Goal planning |
| W14 | AAL-001 | Dynamic asset allocation |
| W15 | RBL-001 | Rebalancing |
| W15.5 | PERF-001 | Performance attribution |
| W16 | AIA-001 | Explainable advisor |
| W17 | INT-001 | Integrated intelligence |
| W18 | QA-001 | Test suite and security fixes |
| W19 | UAT-001 | UAT tooling |
| W20 | REL-001 | Hardening |

Each wave has a handoff in `docs/`.

**Schema:** 22 new tables, all additive. No existing table or column was altered. New permissions `wealth:read` / `wealth:write` are granted to the built-in roles.

**Existing behaviour changed** (all additive):
- a "Wealth" link in the dashboard's top bar;
- a post-market job `wealth_cycle` that is SKIPPED unless `wealth.enabled`;
- the new health component and monitor rule;
- `rbac.seed()` now grants newly introduced permissions to existing built-in roles.

## 3. Deploy (next operator, after the gates and authorization)

In `D:\Projects\ATIP`:

1. Take a verified backup: `python -m ops backup --kind pre-release`.
2. Fast-forward `master` to `ATIP-W20-RC1`. It sits on top of `ATIP-W9-RC2` = `24943f3`, which is master's current HEAD.
3. Run the W9 deployment script against the new release id:

```bash
powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W20-RC1
```

4. The dry run comes first. Then add `-Authorize`.
5. At startup, migration 0005 applies automatically (`ops.startup`).
6. Restart with bare `python main.py` (never `--dashboard`, which drops the scheduler).
7. Postcheck: `python -m ops release postcheck`, then `curl http://127.0.0.1:8000/health/wealth`.
8. Keep `wealth.enabled` false for the first day. Enable it after the owner has saved an Investor DNA.

## 4. Rollback

- **Preferred:** restore the pre-release verified backup (`docs/ROLLBACK_PROCEDURE.md`), then check out `ATIP-W9-RC2`.
- **Without a restore:** drop the twelve 0005 triggers listed in the migration's rollback note. The wealth tables can stay (nothing reads them without the code).

## 5. Readiness matrix

| Category | Status |
|---|---|
| Code complete (W11–W20 scope) | READY (IMPLEMENTED BUT NOT VERIFIED) |
| Automated tests | WRITTEN, NOT RUN (70 tests) |
| Security review | DONE (static). S1 / S2 fixed; S7 accepted opt-in; no penetration test |
| Database migration | READY (0005, verified on a copy) |
| Health / monitoring | READY |
| Backup | READY (whole-database; key tables extended) |
| UAT | TOOLING READY; NOT EXECUTED |
| Methodology review | PENDING (WLT-3) |
| Production authorization | BLOCKED: **PRODUCTION DEPLOYMENT AUTHORIZATION REQUIRED** |
| Production deployment | NOT DONE |
