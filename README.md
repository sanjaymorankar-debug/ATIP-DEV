# ATIP — AI Trading Intelligence Platform v0.2

> *"Turning Market Data into Daily Decisions."*
> Personal Use · NSE/BSE · Python 3.11+ · macOS (Windows still supported)

---

## Data Refresh Frequency (Complete Reference)

| Data | Source | Frequency | Time (IST) |
|------|--------|-----------|------------|
| Live stock quotes (LTP, OHLC, Vol) | **Dhan API** | Every 15 min | 09:15 – 15:30 |
| NSE index levels (Nifty, BankNifty etc.) | **Dhan + yfinance** | Every 15 min | 09:15 – 15:30 |
| India VIX | **Dhan + yfinance** | Every 15 min | 09:15 – 15:30 |
| Dhan WebSocket tick stream | **Dhan WS** | Real-time (every tick) | 09:15 – 15:30 |
| Intraday 15-min OHLC bars | **Dhan API** | Every 30 min | 09:15 – 15:30 |
| Global markets (S&P, Dow, Gold, Crude) | **yfinance** | Every 30 min | 09:15 – 15:30 |
| Pre-market GIFT Nifty + Global | **yfinance** | Once | 07:15 AM |
| NSE pre-open auction (IEP, total buy / sell per stock) | **NSE** (`data/preopen.py`) | Once, after order entry closes | 09:09 AM |
| Pre-open outcome (open, first 15 min, close) | **Computed** | Once | 17:10 PM |
| Market news (RSS + Claude AI) | **RSS + Anthropic** | Twice | 07:45 AM, 12:00 PM |
| Portfolio sync (Dhan or Zerodha) | **Dhan / Kite API** | Twice | 08:00 AM, 17:30 PM |
| NSE Bhavcopy (official EOD) | **NSE Archives** | Once | 16:05 PM |
| Dhan historical daily (EOD confirm) | **Dhan API** | Once | 16:30 PM |
| Technical indicators (35 indicators) | **Computed** | Once | 16:45 PM |
| AI scoring (9 indexes + ATIP rank) | **Computed** | Once | 17:00 PM |
| Dashboard rebuild | **Computed** | Once | 18:00 PM |
| Overnight US markets | **yfinance** | Once | 23:00 PM |
| Fundamental data (ROE, EPS, D/E etc.) | **Alpha Vantage** | Weekly | Saturday 08:00 |
| Accuracy audit (prediction vs actual) | **Computed** | Weekly | Saturday 09:00 |
| Dhan security master list | **Dhan API** | Weekly | Saturday 08:30 |
| Dhan access token renewal (RenewToken, else TOTP + PIN) | **Dhan auth** | Daily + at login (macOS LaunchAgent; Windows task 08:00) | 06:30 |
| 7-year daily history backfill (`data/history_backfill.py`, 40 symbols a night) | **Dhan API** | Nightly until complete | 22:20 |

---

## Project Structure

```
/Users/agtci/Documents/Project_Documents/Projects/ATIP-dev/    ← RUN ALL COMMANDS FROM HERE
│
├── main.py                         ← SINGLE ENTRY POINT
├── requirements.txt
├── setup.py
├── config_template.json            ← Copy to atip_data/config.json
│
│   macOS / Linux launchers
├── start_atip.sh                   ← Start ATIP
├── publish_snapshot.sh             ← Push the read-only dashboard to bkesari.com
├── deploy/launchd/                 ← LaunchAgents: auto-start, 5-min snapshot, Dhan token
│
│   Windows launchers (kept for the old machine)
├── install.bat                     ← Install all packages
├── start_atip.bat                  ← Start ATIP
├── setup_atip.ps1                  ← PowerShell: full setup + Windows startup
├── ATIP_TaskScheduler.xml          ← Windows Task Scheduler import file
│
│   Packages sit directly under the project root — there is deliberately NO
│   nested atip/ package (see the note in main.py's docstring).
├── db/schema.py                    ← the full schema (186 tables) + AI weight configs
├── db/postgres.py                  ← PostgreSQL backend (the server database)
├── db/sql/                         ← generated fresh-database scripts (PostgreSQL, SQLite)
├── data/
│   ├── dhan.py                     ← Dhan API: live quotes, history, WebSocket
│   ├── bhavcopy.py                 ← NSE Bhavcopy official EOD
│   ├── technical.py                ← 37 technical indicators (RSI, MACD, ADX…)
│   ├── markets.py                  ← NSE indexes + Global markets (yfinance)
│   ├── news.py                     ← RSS fetch + Claude API sentiment
│   └── fundamentals.py             ← Alpha Vantage + Screener.in
├── scores/
│   ├── engine.py                   ← VPI, MRI, RRI, CRI, ZPI, MSI, ACS, MH, ATIP
│   └── accuracy.py                 ← 5/10/20-day prediction tracking
├── pipeline/scheduler.py           ← Full daily schedule (all 3 windows)
├── dashboard/server.py             ← FastAPI HTML dashboard
├── alerts/telegram.py              ← 10 push alert types
├── portfolio/zerodha.py            ← Zerodha Kite fallback
├── strategy/ execution/ orders/    ← signals, routing, order lifecycle
├── ml/ quant/ wealth/ enterprise/  ← models, factors, planning, tenancy
├── brokers/ altdata/ backtest/     ← broker adapters, alt data, backtesting
├── ops/ tools/ docs/ tests/
│
└── atip_data/                      ← Auto-created on first run; NOT in git
    ├── atip.db                     ← SQLite database (all data)
    ├── config.json                 ← YOUR API KEYS
    ├── atip.log                    ← Activity log
    ├── publish.json                ← Snapshot upload targets + tokens
    └── raw/dhan/                   ← Dhan security list + cache
```

---

## Quick Setup (macOS)

The project lives at `/Users/agtci/Documents/Project_Documents/Projects/ATIP-dev`.
Any folder name works, because the scripts find the project from their own
location. Inside `~/Documents`, read the privacy and iCloud items under
[Mac checks](#mac-checks) first.

### Python 3.11 or newer — not the one macOS ships

The `python3` that comes with macOS is 3.9, which is too old: ATIP needs 3.11+.
Install a current Python with [Homebrew](https://brew.sh) and build the
virtualenv from it by its full path, as in Step 1 below. Plain `python3 -m venv`
picks up Apple's 3.9.

```bash
brew install python@3.13
```

### Option A — one-time setup
```bash
cd /Users/agtci/Documents/Project_Documents/Projects/ATIP-dev

# Step 1: a virtualenv from Homebrew's Python, and the packages
"$(brew --prefix)/bin/python3.13" -m venv .venv
.venv/bin/python --version           # must say 3.11 or newer
.venv/bin/pip install -r requirements.txt

# Step 2: config (or bring atip_data/ over from Windows instead, see below)
mkdir -p atip_data
cp config_template.json atip_data/config.json
open -e atip_data/config.json        # add your API keys

# Step 3: init DB + download the Dhan security list
.venv/bin/python main.py --init
.venv/bin/python main.py --dhan-securities

# Step 4: first data run (skip it if you brought the Windows database over)
.venv/bin/python main.py --dhan-history

# Step 5: start
./start_atip.sh
```

`start_atip.sh` picks `.venv/bin/python` automatically when that virtualenv
exists, so later runs are just `./start_atip.sh`. Keep `.venv`: the LaunchAgent
starts with launchd's minimal `PATH`, which has no Homebrew in it, so without
`.venv` it falls back to Apple's Python 3.9.

### Option B — Windows (the old machine)
```powershell
cd D:\Projects\ATIP
# Right-click setup_atip.ps1 → Run with PowerShell as Administrator
.\setup_atip.ps1 -All
```

### Moving over from the Windows machine

To keep your history, copy the Windows `atip_data/` across instead of starting
from `config_template.json`:

1. **Stop ATIP on Windows and switch off its auto-start first.** Otherwise both
   machines run the schedule, send Telegram alerts and publish snapshots.
   `.\setup_atip.ps1 -UnregisterStartup` removes the "ATIP Platform" task.
   Also disable the "ATIP publish snapshot" task in Task Scheduler, and if
   you used `atip_autostart.vbs`, remove its entry from the Startup folder
   (`Win+R` → `shell:startup`).
2. Copy `D:\Projects\ATIP\atip_data\` into `atip_data/` in the project folder on
   the Mac. That brings `atip.db`, `config.json`, `publish.json`, `raw/`, and
   `secrets/` if you have it. If `atip.db-wal` or `atip.db-shm` sit next to
   `atip.db`, copy them too, because they can hold the most recent writes.
   Bring the Windows `.env` as well if you had one.
3. Run `.venv/bin/python main.py --init` anyway. It only adds what is missing
   and never drops anything.

### Anthropic API key: use `.env`, not `~/.zshrc`

The LaunchAgent never reads your shell profile, so a key exported in
`~/.zshrc` only works when you start ATIP from Terminal. Put it in a `.env` file
in the project folder instead (git-ignored), with one line,
`ANTHROPIC_API_KEY=sk-ant-...`:

```bash
touch .env && open -e .env
```

### Mac checks

**Documents folder privacy.** macOS protects `~/Documents`, and a LaunchAgent
has no window to ask you for permission. If the agent will not stay up
(`launchctl list | grep com.atip` shows a non-zero status) and
`atip_data/launchd.err` says `Operation not permitted`, either:
- move the project out of Documents (e.g. to `~/ATIP-dev`) and re-run
  `deploy/launchd/install.sh`; or
- add `/bin/bash` under System Settings → Privacy & Security → Full Disk Access
  (⌘⇧G in the file picker lets you type the path). If it still fails, add
  Homebrew's Python there as well.

**iCloud Drive.** If "Desktop & Documents Folders" is on (System Settings →
your name → iCloud → iCloud Drive), iCloud syncs everything in `~/Documents`,
including `atip_data/atip.db`, while ATIP is writing to it. A SQLite file
copied mid-write can be corrupt. Turn that option off, or keep the project
outside `~/Documents`.

**Sleep.** Nothing runs while the Mac sleeps: no quotes, no post-market jobs,
no order monitoring. On trading days, keep it awake:
- MacBook: System Settings → Battery → Options → "Prevent automatic sleeping
  on power adapter when the display is off", and leave it plugged in. Closing
  the lid still sleeps it unless an external display is connected.
- Desktop Mac: the same setting is under System Settings → Energy.

**Time zone.** The schedule runs on the Mac's clock. The LaunchAgent sets
`TZ=Asia/Kolkata` for itself. A manual `./start_atip.sh` uses the Mac's own
time zone, so if the Mac is not on India time, start it with
`TZ=Asia/Kolkata ./start_atip.sh`.

---

## API Keys

| Key | Where to Get | Cost | Priority |
|-----|-------------|------|----------|
| **Dhan Client ID + Access Token** | [web.dhanhq.com](https://web.dhanhq.com) → My Profile → Apps | **FREE** | ⭐ Primary |
| Telegram Bot Token + Chat ID | @BotFather on Telegram | Free | Alerts |
| Anthropic API Key | [console.anthropic.com](https://console.anthropic.com) | Pay per use | News AI |
| Alpha Vantage Key | [alphavantage.co](https://www.alphavantage.co/support/#api-key) | Free (5/min) | Fundamentals |
| Zerodha Kite API | [developers.kite.trade](https://developers.kite.trade) | ₹2,000/yr | Portfolio (optional) |

**Minimum required to start:** Just Dhan credentials.

---

## All Commands

On the Mac, use `.venv/bin/python` wherever these say `python`, or run
`source .venv/bin/activate` once per Terminal window first.

```powershell
# Setup
python main.py --init                    # Create database (first time)
python main.py --dhan-securities         # Download Dhan security ID list (first time)
python main.py --dhan-history            # Download 1-year historical prices
python -m data.history_backfill run      # One budgeted pass of the 7-year history backfill
python -m data.history_backfill status   # How far back each symbol reaches

# Daily usage
python main.py                           # Start scheduler + dashboard (default)
python main.py --dashboard               # Dashboard only → http://localhost:8000
python main.py --scheduler               # Scheduler only (no web server)

# Live data
python main.py --live-feed               # Start Dhan WebSocket tick stream
python main.py --dhan-quote RELIANCE TCS # Get live quotes for specific stocks

# Manual pipeline runs
python main.py --run premarket           # Run 7:00 AM jobs now
python main.py --run postmarket          # Run 4:05 PM jobs now
python main.py --run intraday            # Run one 15-min refresh now
python main.py --run weekly              # Run Saturday jobs now

# Portfolio & alerts
python main.py --login-zerodha           # Login to Zerodha (if using Zerodha)
python main.py --test-telegram           # Send test Telegram alert

# Status & diagnostics
python main.py --status                  # Show full pipeline status

# Windows startup
.\setup_atip.ps1 -RegisterStartup        # Auto-start on Windows login
.\setup_atip.ps1 -UnregisterStartup      # Remove auto-start
.\setup_atip.ps1 -Status                 # Check installation status
```

---

## Database: SQLite now, PostgreSQL on a server

ATIP runs on SQLite (`atip_data/atip.db`, WAL) by default, and that is unchanged.
**PostgreSQL is the one server database target** — MySQL / MariaDB is no longer
supported (the MySQL backend and its tools were removed; a `mysql://` URL is refused).

- Move existing data: `python tools/sqlite_to_postgres.py --source <backup copy>` (dry
  run), then `--target postgresql://... --execute`.
- Start empty: `psql -d atip -v ON_ERROR_STOP=1 -f db/sql/atip_schema.postgresql.sql`
  (generated by `python tools/schema_sql.py`; see `db/sql/README.md`).
- Switch the runtime (opt-in): `ATIP_DATABASE_URL=postgresql://...` **and**
  `"database": {"backend": "postgresql", "allow_experimental": true}` in
  `atip_data/config.json`.

Details: `docs/POSTGRESQL_MIGRATION.md`; hosting on a VPS: `docs/HOSTED_DEPLOYMENT.md`.

---

## Dhan API Integration Details

### Getting Dhan Credentials
1. Go to [web.dhanhq.com](https://web.dhanhq.com)
2. Login → **My Profile → Apps → Create App**
3. Copy **Client ID** and **Access Token**
4. Add to `atip_data/config.json`:
```json
"dhan_client_id":    "1000000000",
"dhan_access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9..."
```

### What Dhan Provides
| Feature | Dhan API | yfinance |
|---------|----------|----------|
| NSE live quotes | ✅ Real-time | ❌ 15-min delay |
| WebSocket ticks | ✅ Yes | ❌ No |
| Intraday 1/5/15-min bars | ✅ Yes | ❌ No |
| Historical daily data | ✅ Yes | ✅ Yes |
| Your portfolio (holdings) | ✅ Yes | ❌ No |
| F&O option chain | ✅ Yes | ❌ No |
| Global markets | ❌ No | ✅ Yes (used for Dow/S&P/Gold etc.) |
| India VIX | ✅ Yes | ✅ Yes |

### First-Time Dhan Setup Sequence
```powershell
# 1. Download security master list (maps symbol → security_id)
python main.py --dhan-securities

# 2. Test live quotes
python main.py --dhan-quote RELIANCE TCS INFY

# 3. Download 1-year history (takes ~5-10 min for 50 stocks)
python main.py --dhan-history

# 4. Start live feed (during market hours only)
python main.py --live-feed
```

### Token renewal (TOTP)
Dhan access tokens last 24 hours. `tools/dhan_token_refresh.py` renews it: the macOS
LaunchAgent runs it daily at 06:30 and at login (see *LaunchAgents* below), the Windows
task `ATIP_DhanTokenRefresh` at 08:00. A valid token is renewed with `RenewToken`. An
expired one can only be regenerated with your trading PIN and TOTP secret, so add both once:
```json
"dhan_pin":         "<6-digit Dhan PIN>",
"dhan_totp_secret": "<base32 secret shown when you enable TOTP at web.dhan.co>"
```
It is better to move them into the encrypted vault with `python -m ops vault-migrate --apply`.
To run it by hand: `python tools/dhan_token_refresh.py` (or `--check`).

### Long history (7 years)
`data/history_backfill.py` walks each tracked symbol back from its earliest stored bar to
seven years ago, one 365-day window at a time, resumably (40 symbols a night at 22:20; the
Nifty 500 completes in about two weeks). The weekly purge keeps `prices_daily` for seven
years (the HISTORY tier in `db/purge.py`). Coverage: `GET /api/data/history/coverage` or
`python -m data.history_backfill status`. Config: `"history": {"years", "symbols_per_run", ...}`.

### Dhan Rate Limits
| API | Limit | ATIP Usage |
|-----|-------|-----------|
| Historical daily | 1 req/sec | 0.3s delay between stocks |
| Live quotes (REST) | 100 req/min | ~3 req per 15-min refresh |
| WebSocket | Unlimited ticks | 1 persistent connection |
| Security list | 1 req/day | Downloaded weekly |

---

## Startup Automation

### macOS — LaunchAgents (current machine)

```bash
deploy/launchd/install.sh            # scheduler/dashboard + 5-min snapshot upload + Dhan token refresh
deploy/launchd/install.sh platform   # just the scheduler/dashboard
deploy/launchd/install.sh snapshot   # just the snapshot upload
deploy/launchd/install.sh token      # just the daily Dhan token refresh
deploy/launchd/install.sh --uninstall
```

The installer resolves the project's real location from its own path and writes
the agents into `~/Library/LaunchAgents`, so nothing has a path baked in.
Install the snapshot agent only once `atip_data/publish.json` exists. With the
project in `~/Documents`, read [Mac checks](#mac-checks) before relying on
either agent.

| Agent | What it does |
|---|---|
| `com.atip.platform` | Starts ATIP at login and restarts it if it exits non-zero (`KeepAlive`), which is what the Windows Task Scheduler task provided and the Startup folder could not. |
| `com.atip.publish-snapshot` | Runs `publish_snapshot.sh` every 5 minutes. Needs `atip_data/publish.json`. |
| `com.atip.dhan-token-refresh` | Runs `tools/dhan_token_refresh.sh` daily at 06:30 and at login: renews the 24-hour Dhan token before the 07:00 pre-market run, and saves it where ATIP reads it (`config.json`, or the vault once migrated). Replaces the Windows `ATIP_DhanTokenRefresh` task. |

All set `TZ=Asia/Kolkata` so scheduled job times match the market session
regardless of the machine's locale. The token agent's 06:30 is the exception:
launchd reads a calendar time in the Mac's own time zone, so keep the Mac on IST.

The token agent renews a token that is still valid with no extra setup. To get
a new one after it has **expired** (the Mac was off for over a day), it logs in
with your Dhan PIN and TOTP. Add them once to `atip_data/config.json` (or as
`DHAN_PIN` / `DHAN_TOTP_SECRET` in ATIP's secret store):
```json
"dhan_pin":         "<your 6-digit Dhan PIN>",
"dhan_totp_secret": "<the base32 secret shown when you enable TOTP at web.dhan.co>"
```

**Verify:**
```bash
launchctl list | grep com.atip          # a 0 in the second column = last run exited cleanly
tail -f atip_data/launchd.err           # ATIP's own output
tail -f atip_data/publish.log           # snapshot uploads
tail atip_data/dhan_token_refresh.log   # "OK: new Dhan token saved" each morning
tools/dhan_token_refresh.sh --check     # is the current token valid?
```

**Stop:**
```bash
launchctl bootout gui/$UID/com.atip.platform
```

> ATIP refuses to start a second copy of itself: it takes an exclusive `flock` on
> `atip_data/atip_single_instance.lock`, so a manual `./start_atip.sh` beside the
> LaunchAgent exits with an error instead of running two schedulers against one
> database. The lock is released by the OS on exit, crash or kill, so there is no
> stale lock to clear.

### Windows — Task Scheduler (the old machine)

1. Open **Task Scheduler** → **Action → Import Task...**
2. Browse to `D:\Projects\ATIP\ATIP_TaskScheduler.xml` → **OK**

Or `.\setup_atip.ps1 -RegisterStartup` (as Administrator). Verify with
`schtasks /query /tn "ATIP Platform" /fo LIST`.

---

## Dashboard Panels

Open **http://localhost:8000** after starting ATIP.

| Panel | Data | Refresh |
|-------|------|---------|
| KPI Bar | Market Health, Nifty, Bank Nifty, VIX, Sentiment, S&P, Gold, USD/INR | 15 min |
| Index Sidebar | 12 NSE sector indexes | 15 min |
| Global Sidebar | S&P, Dow, Nasdaq, Nikkei, Crude, Gold, USD/INR | 30 min |
| Trade of Day | TOD stock with CMP, SL, Target 1, Target 2 | Daily 5:45 PM |
| ATIP Scores | All stocks ranked — sortable, filterable | Daily 5:00 PM |
| Portfolio | Holdings with ATIP overlay + signals | 8:00 AM, 5:30 PM |
| Top VPI | Best swing candidates | Daily 5:00 PM |
| Buy Zones | ZPI ≥ 75 stocks | Daily 5:00 PM |
| CRI Risk | Crash-risk stocks (avoid) | Daily 5:00 PM |
| News | AI-classified news with sentiment | 7:45 AM, 12:00 PM |

Click any stock to open its history panel. It offers 1M to 5Y and All ranges, daily, weekly or monthly
bars, SMA, EMA and Bollinger overlays, a log scale, and BUY / SELL signal markers.

Other pages:
- `/wealth`: the investor mode. A Simple / Detailed toggle sits in the header.
- `/baskets`: basket orders and paper stock SIP plans.
- `/trading`: orders and risk.
- `/data-platform`: includes the long-history coverage.

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'atip'` | Run from the project folder (the one holding `main.py`), not from inside a package subfolder |
| `SyntaxError` / `TypeError` at start-up on the Mac | Run `.venv/bin/python --version`. If it says 3.9, `.venv` was built from Apple's Python: `rm -rf .venv` and redo Step 1 with Homebrew's |
| LaunchAgent won't stay up; `launchd.err` says `Operation not permitted` | macOS privacy protection on `~/Documents`; see [Mac checks](#mac-checks) |
| Anthropic key works from Terminal but not at login | It's exported in `~/.zshrc`; move it to `.env` in the project folder |
| `Dhan credentials not set` | Add `dhan_client_id` + `dhan_access_token` to `atip_data/config.json` |
| Dhan token expired / `401` from Dhan | Run `tools/dhan_token_refresh.sh` and read `atip_data/dhan_token_refresh.log`; make sure `deploy/launchd/install.sh token` was run |
| `security_id not found` | Run `python main.py --dhan-securities` first |
| `pandas-ta` fails on Python 3.14 | Not needed — ATIP uses `ta` (`pip install ta`); see docs/DEV_SETUP.md |
| Dashboard empty | Run `python main.py --run postmarket` first to populate data |
| NSE Bhavcopy 403 error | NSE blocks cloud — must run on local machine (not VPS/cloud) |
| Live feed disconnects | Normal — Dhan WS reconnects automatically |
| Port 8000 in use | Use `python main.py --dashboard --port 8080` |

---

## Disclaimer

⚠️ **ATIP is for personal use only.** Not for commercial distribution.
All AI scores and signals are informational — **not financial advice**.
Past performance is not indicative of future results.
Not SEBI registered. **Consult a SEBI-registered advisor before investing.**
