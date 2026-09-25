# Release checklist (reusable; filled in for ATIP-W9-RC1)

Tick each item only with evidence. "Verified" means checked by the named command, not functionally tested.

## A. Release candidate
- [x] Every wave commit is in `master` (`git merge-base --is-ancestor`, WAVE_STATUS.md).
- [x] Release branch `w9-release`, fast-forwarded into master.
- [x] Annotated tag `ATIP-W9-RC1` on the master HEAD.
- [x] Recovery branch `backup/pre-w9-master` (= W8 `a7d7187`).
- [x] Dependency lock `requirements.lock.txt` matches the installed environment (preflight).
- [ ] Pushed to `origin`. **Owner decision; not done** (master is 50+ commits ahead of origin).

## B. Code and build
- [x] `python -m compileall` clean; pyflakes clean on the W9-changed modules.
- [x] The dashboard app imports; routes register; the middleware stack builds.
- [x] Smoke start of the W9 code on a **copy** of the production database (port 8765). All health endpoints answer; startup applied no migrations (all current); security headers and `API-Version` present.
- [ ] CI (`tests.yml`) green. **Not run** (nothing pushed).
- [ ] Independent functional QA (ChatGPT). **Pending.**

## C. Configuration and secrets
- [x] Audit of the production config (PRODUCTION_CONFIGURATION.md, names and status only).
- [x] No secrets in tracked files (`python -m ops scan`) or in git history (placeholder strings only).
- [x] `atip_data/`, `.env` and `atip_data/config.json` are gitignored.
- [x] DEBUG off; CORS same-origin; cookies HttpOnly + SameSite=Strict (+ Secure with TLS).
- [ ] Legacy plaintext credentials moved to the secret store. **Owner action** (W8-R4).
- [ ] Encryption key created. **Owner action**; needed only for MFA.

## D. Database
- [x] Migrations 0001–0004 recorded as APPLIED in production; W9 adds **no** schema change.
- [x] All migrations additive; rollback notes in `schema_migrations` and ROLLBACK_PROCEDURE.md.
- [x] Pre-merge snapshot `atip_data/atip.db.bak-before-w9-<ts>` with `integrity_check` ok (W9 report).
- [ ] VERIFIED pre-deployment backup (`python -m ops backup --kind pre-release`). This is taken by the deploy step.
- [ ] Restore tested. **Not performed** (the DR drill is documented and has not been run).

## E. Trading safety
- [x] `LIVE_TRADING_ENABLED = FALSE` (`python -m ops safety`).
- [x] W4 gate closed; the W1 `broker_env` is PAPER.
- [x] The master switch now covers W1 real orders (W9-T1).
- [x] No path Signal → Strategy → PositionIntent → Risk → Order → live broker without authorization:
  - W4: the Dhan adapter refuses every call.
  - W1: `broker_env` LIVE + confirm + master switch (live_trading_enabled + production).

## F. Operations
- [x] Health: `/health`, `/live`, `/ready`, `/database`, `/broker`, `/data`, `/scheduler`, `/ml`, `/storage`, `/market_data`.
- [x] Monitoring rules (12) and alert channel (W8).
- [x] Deploy and rollback scripts (dry-run default; syntax-checked; **not executed**).
- [x] Docs: W9_FINAL_RELEASE, PRODUCTION_DEPLOYMENT, ROLLBACK_PROCEDURE, PRODUCTION_CONFIGURATION, RELEASE_CHECKLIST, WAVE_STATUS, KNOWN_ISSUES.

## G. Deployment (needs explicit owner authorization)
- [ ] `deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC1 -Authorize`
- [ ] Postcheck passed; `history.jsonl` records DEPLOYED.
- [ ] Owner acceptance after independent QA.
