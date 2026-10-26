# ATIP on the Mac — deployment plan

The plan for running ATIP from its new home, **`/Users/agtci/ATIP`**,
on the owner's Mac. Every step has a command in `setup_atip.sh`, the macOS setup
script at the project root (the counterpart of the Windows `setup_atip.ps1`).

| | |
|---|---|
| Project folder (the one holding `main.py`) | `/Users/agtci/ATIP` |
| Previous machine | Windows, `D:\Projects\ATIP` (its data, about 3 GB, came over on the USB drive as `/Volumes/MOSAM/atip_data`) |
| Earlier Mac folders, not used | `/Users/agtci/Documents/GIT_ATIP/ATIP` (a copy of the Windows git worktree, see step 2) and `/Users/agtci/Documents/Project_Documents/Projects/ATIP-dev` |
| Code | git clone of `sanjaymorankar-debug/ATIP-DEV`, branch `master` |
| Runtime data | `atip_data/` in the project folder: database, `config.json`, logs, `publish.json`, `secrets/`. Not in git |
| Secrets outside `atip_data/` | `.env` in the project folder (`ANTHROPIC_API_KEY`, optionally others). Not in git |
| Dashboard | http://localhost:8000 (127.0.0.1 only) |
| Auto-start | three LaunchAgents in `~/Library/LaunchAgents`, written for this folder by `deploy/launchd/install.sh` |

**Always work in the project folder.** `dashboard/`, `pipeline/`, `data/` and the rest are
Python packages inside it, not places to start ATIP from. The script finds the project
from its own location, so `../setup_atip.sh check` from `dashboard/` still acts on
`/Users/agtci/ATIP`.

## The short version

```bash
cd /Users/agtci/ATIP
./setup_atip.sh check                 # what is ready, what is missing (changes nothing)
./setup_atip.sh all --from /Volumes/MOSAM    # the drive holding the Windows atip_data/
#   copies atip_data/ (and .env) from there, builds .venv, installs packages,
#   upgrades the database, fetches first data if missing, installs the LaunchAgents
#   for THIS folder, and verifies. Re-running it skips what is already done.
./setup_atip.sh verify
```

Already copied `atip_data/` into the project by hand? Check the copy (step 5), then run
`./setup_atip.sh all` without `--from`. No old data at all? `all` without `--from` starts a
fresh database.

The steps below are the same plan, one piece at a time, with what to check after each.

---

## 1. Prerequisites

- [x] **Python 3.11+.** Installed: 3.15.2 at `/usr/local/bin/python3`, which `check` finds (it
  prefers Homebrew's 3.13, then any 3.11+). The `python3` that ships with macOS is 3.9 and too
  old. ATIP is tested on 3.12–3.14: if `install` fails building a package on 3.15,
  `brew install python@3.13`, `rm -rf .venv`, then
  `ATIP_PYTHON="$(brew --prefix)/bin/python3.13" ./setup_atip.sh install`.
- [ ] **git**, from the Xcode Command Line Tools (`xcode-select --install`, already there if Homebrew is).
- [ ] **Keys at hand:** Dhan client ID and access token (required), Dhan PIN and TOTP secret
  (automatic token renewal), Telegram bot token and chat ID, Anthropic API key, Alpha Vantage
  key. Only the Dhan pair is needed to start.

## 2. Get the code into the new folder

The first time:

```bash
cd /Users/agtci/Documents
git clone https://github.com/sanjaymorankar-debug/ATIP-DEV.git ATIP
```

**Clone; don't copy the code from Windows.** `D:\Projects\ATIP\ATIP-dev` was a git *worktree*:
its `.git` is a one-line file, `gitdir: D:/Projects/ATIP/.git/worktrees/ATIP-dev`, pointing at
history on the Windows drive. Copied to the Mac, every git command in it fails with
`fatal: not a git repository: …/D:/Projects/ATIP/.git/worktrees/ATIP-dev`. That is what
`/Users/agtci/Documents/GIT_ATIP/ATIP` is. Only `atip_data/` and `.env` come from Windows.

If the folder is already a clone: `cd /Users/agtci/ATIP && git pull`.

Check: `ls /Users/agtci/ATIP/main.py` exists, and `setup_atip.sh` is next to it.

## 3. Preflight

```bash
cd /Users/agtci/ATIP
./setup_atip.sh check
```

It reads and reports, and changes nothing: Python and `.venv`, packages, `config.json` and
`.env`, which keys are set (never their values), the database and how much price history it
holds, the clock, port 8000, other running copies of ATIP, and where each LaunchAgent points.
Every ❌ line names the command that fixes it. On a fresh clone, expect ❌ for `.venv`,
`config.json` and the database; the next steps clear them.

## 4. Stop the old copies

Two copies running at once each run the schedule, each send every Telegram alert and each
publish snapshots, against two different databases. Before the switch:

- **An earlier Mac folder.** `check` lists any ATIP running from another folder, and the script
  refuses to copy from a running one. To stop it: `launchctl bootout gui/$UID/com.atip.platform`,
  or Ctrl+C in the Terminal it runs in.
- **The Windows machine**, if it still runs: `.\setup_atip.ps1 -UnregisterStartup`, disable
  the "ATIP publish snapshot" and `ATIP_DhanTokenRefresh` tasks in Task Scheduler, and remove
  `atip_autostart.vbs` from the Startup folder (`Win+R` → `shell:startup`).

## 5. Install and bring the data over

The Windows `atip_data\` (about 3 GB) is on the USB drive as `/Volumes/MOSAM/atip_data`.

**Let the script copy and check it:**

```bash
cd /Users/agtci/ATIP
./setup_atip.sh migrate --from /Volumes/MOSAM      # the folder that CONTAINS atip_data/
./setup_atip.sh install
```

(`all --from /Volumes/MOSAM` runs both, then steps 6–9.)

**Or, if you already copied it into the project by hand**, check the copy against the drive
before using it. Compare first: opening the database can fold its `-wal` file in, and the
file then no longer matches the original byte for byte.

```bash
cd /Users/agtci/ATIP
cmp /Volumes/MOSAM/atip_data/atip.db atip_data/atip.db && echo "atip.db identical"
ls -l /Volumes/MOSAM/atip_data/atip.db-wal atip_data/atip.db-wal   # a -wal on the drive must be copied too
sqlite3 atip_data/atip.db "PRAGMA quick_check;"                    # must print: ok
./setup_atip.sh install
```

`migrate`:

- refuses while ATIP runs from the old folder, or from this one;
- checks there is room, then copies `atip_data/` (database with its `-wal` file,
  `config.json`, `publish.json`, `secrets/`, `raw/`, backups) and `.env`;
- never overwrites: if this folder already has its own `atip_data/`, it asks first and
  keeps the existing one as `atip_data.before-migrate-<date>`. A different `.env` here is kept;
- checks the copy: the database is byte-identical to the original and passes
  `PRAGMA quick_check`, and it prints how many daily prices and symbols came across;
- leaves the old folder untouched. It is the rollback (step 10);
- does not copy `.venv`: a virtualenv cannot move between folders.

`.env` from Windows (`D:\Projects\ATIP\.env`, if there is one) belongs next to `atip_data`
on the drive, so `migrate` picks it up; or copy it into the project by hand.

`install`:

- builds `.venv` from Python 3.11+, rebuilding a `.venv` made from Apple's 3.9;
- installs `requirements.txt`;
- creates `atip_data/config.json` from `config_template.json` and an empty `.env`, only when
  they don't exist yet;
- runs `main.py --init`, which only adds missing tables and columns and never drops anything,
  so it also upgrades a migrated database.

Check: `./setup_atip.sh check` shows ✅ for Python, `.venv`, packages and the database, with
the migrated price count.

## 6. Keys

```bash
open -e atip_data/config.json         # dhan_client_id, dhan_access_token (required)
open -e .env                          # ANTHROPIC_API_KEY=sk-ant-...
```

- **`dhan_pin` and `dhan_totp_secret`** in `config.json`: without them an *expired* Dhan token
  (the Mac was off for more than a day) can only be renewed by hand.
- **Anything you exported in `~/.zshrc`** works only from Terminal: the LaunchAgent never
  reads your shell profile. `check` flags each key that comes from the shell; move it to `.env`.
- **Optional, safer:** move the plaintext keys into the encrypted vault,
  `.venv/bin/python -m ops vault-migrate --apply`. The vault key lives in
  `atip_data/secrets/ATIP_ENCRYPTION_KEY`, so it moves with `atip_data/`.

Check: `./setup_atip.sh check`, under "Configuration and keys", shows ✅ for `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`.

First market data, only on a fresh database (a migrated one already has it):

```bash
./setup_atip.sh data                  # Dhan security list, then 600 days of history (5–10 min)
```

## 7. Mac settings

The new folder is inside `~/Documents`, which macOS protects:

- **Full Disk Access for `/bin/bash`.** System Settings → Privacy & Security → Full Disk Access
  → `+` → ⌘⇧G → `/bin/bash`. Without it the LaunchAgent cannot open the project and
  `atip_data/launchd.err` says `Operation not permitted`. Bash hands over to Python, so add
  the Python ATIP runs on as well: `.venv/bin/python -c "import os,sys;
  print(os.path.realpath(sys._base_executable))"` prints it (python.org's 3.15:
  `/Library/Frameworks/Python.framework/Versions/3.15/bin/python3.15`; add
  `/Library/Frameworks/Python.framework/Versions/3.15/Resources/Python.app` too, which is what
  that one starts). Then `./setup_atip.sh agents` and `./setup_atip.sh verify`. Moving the
  project out of `~/Documents` (e.g. to `~/ATIP`) avoids this altogether.
- **iCloud "Desktop & Documents Folders" off.** System Settings → your name → iCloud → iCloud
  Drive. With it on, iCloud syncs `atip.db` while ATIP writes to it, and a database copied
  mid-write can be corrupt. `check` reports ❌ when it looks on.
- **No sleep on trading days.** Nothing runs while the Mac sleeps. MacBook: System Settings →
  Battery → Options → "Prevent automatic sleeping on power adapter when the display is off",
  and keep it plugged in (closing the lid still sleeps it without an external display).
  Desktop: System Settings → Energy.
- **India time.** The LaunchAgents and `./setup_atip.sh start` run ATIP on IST themselves.
  The 06:30 token renewal is scheduled on the Mac's own clock, so keep the Mac on IST.

## 8. Auto-start

```bash
./setup_atip.sh agents
```

It refuses while another ATIP runs from a Terminal, and asks before taking over LaunchAgents
that still point at the old folder. Then it installs, for this folder:

| Agent | What |
|---|---|
| `com.atip.platform` | scheduler + dashboard; starts at login, restarts if it exits with an error |
| `com.atip.dhan-token-refresh` | renews the 24-hour Dhan token at 06:30 and at login |
| `com.atip.publish-snapshot` | snapshot upload every 5 minutes; installed only when `atip_data/publish.json` exists |

It then waits for the dashboard to answer. To run ATIP in a Terminal instead, without the
agents: `./setup_atip.sh start` (Ctrl+C stops it).

## 9. Verify

```bash
./setup_atip.sh verify
```

- ATIP holds the lock in **this** folder;
- every LaunchAgent points here;
- no second copy is running;
- `/health/live` and `/health/ready` answer;
- `launchd.err` has no permission errors;
- it shows today's ERROR lines and the last token-refresh and snapshot results.

Then open http://localhost:8000, and run `.venv/bin/python main.py --status` for the jobs run
today.

**The first trading day** after the switch, check that each job ran
(`.venv/bin/python main.py --status`, or `.venv/bin/python -m pipeline.health`):

| IST | Job | Where to look |
|---|---|---|
| 06:30 | Dhan token renewed | `tail atip_data/dhan_token_refresh.log` → "OK: new Dhan token saved" |
| 07:00 | pre-market run (global cues, news) | `--status` |
| 09:09 | NSE pre-open auction captured | `--status` |
| 09:15–15:30 | quotes and indexes every 15 minutes | dashboard KPI bar moves |
| 16:45 | post-market run: Bhavcopy, indicators, scores (catch-up at 18:30) | `--status`, dashboard scores |
| 19:15 | verified database backup | `.venv/bin/python -m ops backups` |
| every 5 min | snapshot published (if installed) | `tail atip_data/publish.log` |

## 10. Rollback

The Windows machine still has its own `atip_data\`, untouched, so going back means running
ATIP there again:

1. On the Mac: `./setup_atip.sh agents --uninstall` (stops ATIP and removes the auto-start).
2. Only to keep the days the Mac ran: with ATIP stopped on both, copy the Mac's
   `/Users/agtci/ATIP/atip_data` back over `D:\Projects\ATIP\atip_data` (USB drive),
   keeping the Windows one aside first. Without this, Windows carries on from the day of the copy.
3. On Windows: `.\setup_atip.ps1 -RegisterStartup`, and re-enable the snapshot and token tasks.

## 11. Retire the old folder

After a week of clean runs from `/Users/agtci/ATIP`:

- delete `/Users/agtci/Documents/GIT_ATIP` and `/Users/agtci/Documents/Project_Documents/Projects/ATIP-dev`;
- delete any `atip_data.before-migrate-*` folder in the project;
- keep one copy of the Windows `atip_data` (the USB drive or the Windows disk) as an archive,
  and delete the Windows scheduled tasks for good. ATIP's own verified daily backups are in
  `atip_data/backups/` from then on.

---

## Day-to-day

| What | Command (in `/Users/agtci/ATIP`) |
|---|---|
| Is everything fine? | `./setup_atip.sh verify` |
| Update the code | `.venv/bin/python -m ops backup`, then `git pull`, `./setup_atip.sh install`, `launchctl kickstart -k gui/$UID/com.atip.platform`, `./setup_atip.sh verify` |
| Restart | `launchctl kickstart -k gui/$UID/com.atip.platform` |
| Stop until next login | `./setup_atip.sh stop` |
| Remove auto-start | `./setup_atip.sh agents --uninstall` |
| Run in a Terminal | `./setup_atip.sh start` |
| Logs | `tail -f atip_data/atip.log` (ATIP), `atip_data/launchd.err` (start-up), `atip_data/publish.log`, `atip_data/dhan_token_refresh.log` |
| Today's jobs | `.venv/bin/python main.py --status` |
| Empty dashboard | `.venv/bin/python diagnose.py` |

`./setup_atip.sh help` lists every command. Running ATIP on the Mac in general (API keys,
commands, LaunchAgents, troubleshooting) is in the `README.md`.
