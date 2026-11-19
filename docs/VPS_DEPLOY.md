# Deploying ATIP to the VPS: one command

Live ATIP runs on the bkesari VPS (`bkesari-atip.service`, `/var/www/bkesari/atip`, user `bkesari`).
`deploy/vps_deploy.py` puts a GitHub master commit there and restarts ATIP. If the new code doesn't come up, it
rolls back. It replaces the hand-run runbook (`deliverables/2026-11-18/vps_deploy_4d793ea/VPS_DEPLOY_STEPS.md`),
doing the same steps in the same order.

It runs where the VPS ssh works (the Mac), never on the server. Every command that changes something is a
**dry run unless `--authorize`** is given.

## Who does what

| Step | Where | How |
|---|---|---|
| Change and test code | an ATIP session on the Mac | describe the fix |
| Push to GitHub master | same session | "commit and push" |
| Deploy and restart | the VPS session (the owner's own yes there) | `deploy --authorize --wait` |
| Undo the last deploy | the VPS session | `rollback --authorize --wait` |
| Restart only | the VPS session | `restart --authorize --wait` |
| What runs, is it healthy | anywhere with the ssh | `status` (add `--verify` to check every file) |

## Commands

```bash
T=deploy/vps_deploy.py             # or: git -C /Users/agtci/ATIP show origin/master:deploy/vps_deploy.py > "$TMPDIR/vps_deploy.py"
S='ssh <the VPS ssh command>'      # or export ATIP_VPS_SSH

python3 $T windows                                   # the restart rules and the next safe minute (no ssh)
python3 $T --ssh "$S" status                         # deployed commit, health, running jobs, GitHub master
python3 $T --ssh "$S" adopt c03fc29                  # FIRST USE: check the server's files against a commit ...
python3 $T --ssh "$S" adopt c03fc29 --authorize      # ... and record it as deployed (nothing restarts)
python3 $T --ssh "$S" deploy                         # dry run of GitHub master: commits, files, checks
python3 $T --ssh "$S" deploy --authorize --wait      # do it at the next safe moment
python3 $T --ssh "$S" rollback --authorize --wait    # back to the commit before
python3 $T --ssh "$S" restart --authorize --wait     # restart without a code change
```

Run it from a checkout of the repository, or pass `--repo /Users/agtci/ATIP`. The remote user needs
passwordless `sudo` for `systemctl`, `journalctl`, `install`, and to act as `bkesari`, the same rights the
runbook's commands needed.

## What a deploy does

1. Fetches GitHub, then checks two things:
   - the target is on `origin/master`;
   - the server's copies of the files it will touch are exactly the deployed commit's. If they differ (drift), nothing changes.
2. Refuses some commits:
   - one that touches `atip_data/`, `.venv/` or the secrets files;
   - one that changes `requirements*.txt` or `setup.py`, unless the packages were installed by hand and `--packages-installed` is passed.
3. Waits for a safe moment (below).
4. While ATIP keeps running:
   - copies the new files to a staging folder on the server;
   - checks them against GitHub's sha256;
   - compiles them with the server's Python.
5. Backs up the files it will replace, and the database (`bkesari-atip-pgdump.service`). If the database backup fails, it stops there.
6. Waits again, up to 10 minutes, if the backup ran past the safe moment.
7. Stops ATIP, installs the files, checks them again, records the commit in `DEPLOYED.json`, and starts ATIP. ATIP is down for about 30 seconds.
8. Checks the new ATIP:
   - a new process;
   - `/health/live` and `/health/ready` answer;
   - no restart in the next 20 seconds.

   Tracebacks in the journal are reported, but don't fail the deploy, since a job can fail for its own reasons.
9. If step 8 fails, or anything fails while ATIP is stopped (including Ctrl-C), it puts the old files back and starts ATIP again. It reports **ROLLED BACK** (exit 4), or exit 5 if even that isn't healthy.

## When a restart may happen (IST, every day)

* **Never 05:45–15:35:** the 06:00 resume, the Dhan token renewals (06:30/08:00/09:00), pre-market and market hours.
* **Never around the heavy evening jobs:**
  - 15:45–16:15 (RCA);
  - 16:17–16:25;
  - 16:42–17:20 (post-market and data quality);
  - 18:27–18:40;
  - 19:12–19:35 (database backup);
  - 19:42–20:05 (Bhavcopy archive).
* **Only in a quiet minute:** ATIP jobs fire on 5-minute marks, so a stop/start begins only in the 1st or 2nd minute after one.
* **Never while a job holds its lock** (`/health/scheduler` on the server), whatever the time.

`tests/test_vps_deploy.py` reads the scheduler and fails if a job is ever scheduled in a moment these rules
allow. `--outside-window "reason"` skips the time rules for an urgent fix, but not the lock check; the reason goes
in the history.

## On the server

`/var/www/bkesari/atip-deploys/` (owned by `bkesari`):
* `DEPLOYED.json`: the running commit, the one before it, and when.
* `history.log`: one line per deploy, rollback, restart and adopt.
* `backups/`: the replaced files of each deploy, plus the list of files it created.

Exit codes:
* 0: done, or a dry run.
* 2: refused.
* 3: not a safe moment.
* 4: rolled back.
* 5: look now.

## Standing rules it keeps

* It never edits `config.json`, the env file, the database contents or orders.
* `broker_env` stays PAPER.
* It never runs the Dhan token refresh.
* It never restarts the Mac copy.
