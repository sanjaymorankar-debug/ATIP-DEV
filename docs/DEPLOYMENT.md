# Deployment (W8)

## Topology

**Development:**
- Worktree `D:\Projects\ATIP-dev`, on a feature branch.
- Never develop in the live directory: the running process lazily imports modules from it.

**Production:**
- `D:\Projects\ATIP` on branch `master`. It runs `python main.py` (scheduler, dashboard and index feed in one process) on 127.0.0.1:8000.
- Started by `start_atip.bat`, `atip_autostart.vbs` or the Task Scheduler template.

**Environments:** `ATIP_ENV` / `environment` = development / test / staging / production. Production is currently configured as **development** (the default). See PRODUCTION_CONFIGURATION.md before switching.

## Release procedure

1. **Develop** on a branch in the worktree. Commit.
2. **Build checks** (not QA):
   - `python -m py_compile` on the changed files, and `python -m pyflakes` on the changed modules.
   - Import `dashboard.server`; routes register and the middleware stack builds.
   - Run migrations against a **copy** of the production database (`ATIP_DB_PATH=<copy>`, then `python -m ops migrate`).
   - `python -m ops scan`: no secret findings.
   - `python -m ops safety` on the production settings: `LIVE_TRADING_ENABLED` false.
3. **Independent testing** by ChatGPT against the branch or MAIN, according to the wave handoff's section 9.
4. **Pre-release backup** of the production database: `python -m ops backup` or a dated `atip.db.bak-before-<wave>-<ts>` with an integrity check. Also create the branch `backup/pre-<wave>-master`.
5. **Merge:**

   ```bash
   git -C D:\Projects\ATIP merge --ff-only <branch>
   ```

   Fast-forward only, into `master`.
6. **Restart** (owner-approved):
   - Stop the running `python main.py`.
   - Start with a bare `python main.py`, **not** `--dashboard`, which would drop the scheduler.
   - Autostart may relaunch ATIP about 40 s after it is killed; the single-instance guard refuses the duplicate.
7. **Verify** after the restart:
   - the log shows `ATIP environment: …` and `TRADING SAFETY: LIVE_TRADING_ENABLED = FALSE`;
   - `migration … applied` appears once;
   - `python -m ops status` and `/health` are READY, or DEGRADED with understood reasons;
   - `/health/scheduler` heartbeat is under 60 s after the first minute.
8. **Record:** in the release notes and `ATIP_MASTER_TRACKER.csv`, record the backup id, the merge commit and the restart time.

## Hosted deployment (future: Phase 9, not built)

- A reverse proxy with TLS 1.3 (SECURITY_ARCHITECTURE.md).
- `environment: production` with the enterprise layer on, rate limiting on, the encryption key, secrets in the environment, and an off-site backup.
- PostgreSQL (DBS-05).
- Split API, scheduler and feed processes (PRODUCTION_ARCHITECTURE.md).
- Gated by ENT-07 (**BLOCKED**) and the ENT-14 legal review.
