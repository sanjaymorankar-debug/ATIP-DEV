# Production deployment (W9)

*Supersedes the W8 `DEPLOYMENT.md`, which was renamed to this file.*

## Topology (as it actually is)

**Production host:** the owner's Windows 11 machine.
- Repository `D:\Projects\ATIP` on branch **`master`**.
- Runs as **one process**, `python main.py`, which hosts the scheduler, the FastAPI dashboard on **127.0.0.1:8000** and the Dhan index feed.
- Launched by `start_atip.bat`. At logon it is started by `atip_autostart.vbs` (Startup folder) and/or the `ATIP_TaskScheduler.xml` task.
- A Windows mutex allows only one instance.

**Storage and configuration:**
- **Database:** SQLite (WAL) at `atip_data/atip.db`.
- **Configuration:** `atip_data/config.json` plus `.env`. Both are gitignored.
- **Secrets:** see PRODUCTION_CONFIGURATION.md.

**Development:** the worktree `D:\Projects\ATIP-dev`, on feature branches. Never develop in the live folder: the running process lazily imports modules from it.

**Not used:** Docker, Kubernetes, systemd, nginx and cloud hosting. No container is introduced; the existing architecture is kept (W9 brief, section 14). Internet exposure (ENT-07) is **BLOCKED**, and ATIP stays bound to 127.0.0.1.

**CI:** `.github/workflows/tests.yml` runs pytest on push to master, *on GitHub*. Nothing has been pushed since W1, so CI has **not** run on W2–W9.

## Release identification

- **Release = an annotated git tag on `master`.** This one is `ATIP-W9-RC1`.
- **Recovery points:**
  - one branch per wave, `backup/pre-<wave>-master`;
  - the tag `w5-final-prod-before-w6`;
  - dated database snapshots, `atip_data/atip.db.bak-before-<wave>-<ts>`.
- **Dependency versions:** `requirements.lock.txt` (exact pins); `requirements.txt` gives the minimums.
- **Manifest per deployment:** `atip_data/releases/<release>/manifest.json`. It records the commit, Python, dependencies, migrations, config fingerprint, the backup id and the trading-safety state, together with copies of `config.json` and `.env` (local only).
- **Deployment history:** `atip_data/releases/history.jsonl`.

## States

| State | Meaning |
|---|---|
| RELEASE READY | the tag exists on master, and the docs and release checks are done |
| DEPLOYMENT READY | preflight has no FAIL except the pre-deployment backup, which the deploy step itself takes |
| DEPLOYED | ATIP has been restarted onto the release commit; `history.jsonl` records DEPLOYED |
| POST-DEPLOYMENT VERIFIED | postcheck passes: health, logs, scheduler, broker mode, LIVE_TRADING_ENABLED = FALSE |
| PRODUCTION ACCEPTED | the owner accepts after independent (ChatGPT) testing. **Never set by development** |

**Deploying needs the owner's explicit authorization.** Without it, stop at DEPLOYMENT READY.

## Procedure (the script does exactly this)

Start with a dry run. It prints the plan and runs the read-only preflight; nothing changes:

```bash
powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC1
```

Then the authorized deployment:

```bash
powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC1 -Authorize
```

| # | Step | How |
|---|---|---|
| 1 | Verify release commit | tag → commit equals `master` HEAD |
| 2 | Verify clean git tree | preflight; the owner's untracked `*.xlsx` files are ignored |
| 3 | Verify backup | `python -m ops backup --kind pre-release` (VERIFIED: integrity_check + sha256), then preflight again |
| 4 | Verify configuration | preflight: `ops.config.validate` has no errors; secrets presence |
| 5 | Verify migrations | preflight: read-only `schema_migrations` vs code; database `quick_check` |
| 6 | Stop the application | stop the `python main.py` process. Autostart may relaunch it within about 40 s; the script waits and adopts that instance |
| 7 | Deploy the release | the code is already on disk (master = the tag); write the manifest and back up the configuration |
| 8 | Apply migrations | `python -m ops migrate` (also applied automatically by `ops.startup` at start) |
| 9 | Start services | `python main.py`, **never `--dashboard`**, which would drop the scheduler |
| 10 | Verify health | `python -m ops release postcheck --wait 180`: `/health/ready` READY, plus the components |
| 11 | Verify logs | postcheck scans `atip.log` since the start for ERROR / CRITICAL / Traceback |
| 12 | Verify scheduler | `/health/scheduler` heartbeat |
| 13 | Verify broker mode | `/health/broker`: execution mode PAPER, gate closed |
| 14 | Verify LIVE_TRADING_ENABLED = FALSE | postcheck (W4 gate, the W1 master switch, broker_env) |
| 15 | Record the release | `python -m ops release record <id> DEPLOYED` → `history.jsonl` |

**Manual equivalent** (run in `D:\Projects\ATIP`). This is steps 1–5; then stop and start ATIP as in the runbook:

```bash
git rev-list -n 1 ATIP-W9-RC1
git status
python -m ops backup --kind pre-release
python -m ops release preflight ATIP-W9-RC1
python -m ops release manifest ATIP-W9-RC1
```

After the start, verify and record:

```bash
python -m ops release postcheck --wait 180
python -m ops release record ATIP-W9-RC1 DEPLOYED
```

## Previous waves (history)

Up to W8, deploying meant fast-forwarding `master` and then restarting only when the owner asked. W9 keeps that model and adds the tag, manifest, preflight, postcheck and record.

## Future hosted deployment (not built; gated)

- TLS 1.3 reverse proxy (SECURITY_ARCHITECTURE.md).
- `environment: production` with the enterprise layer on, rate limiting, the encryption key, secrets in the environment, and an off-site backup.
- PostgreSQL (DBS-05).
- Split processes.

These are blocked by ENT-07 (owner decision) and the ENT-14 legal review.
