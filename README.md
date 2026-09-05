# ATIP — AI Trading Intelligence Platform v0.2

> *"Turning Market Data into Daily Decisions."*
> Personal Use · NSE/BSE · Python 3.10+ · Windows

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

---

## Project Structure

```
D:\Projects\atip\                   ← RUN ALL COMMANDS FROM HERE
│
├── main.py                         ← SINGLE ENTRY POINT
├── requirements.txt
├── setup.py
├── install.bat                     ← Double-click to install all packages
├── start_atip.bat                  ← Double-click to start ATIP
├── setup_atip.ps1                  ← PowerShell: full setup + Windows startup
├── ATIP_TaskScheduler.xml          ← Windows Task Scheduler import file
├── config_template.json            ← Copy to atip_data\config.json
│
├── atip\
│   ├── db\schema.py                ← 19 SQLite tables + 68 AI weight configs
│   ├── data\
│   │   ├── dhan.py                 ← Dhan API: live quotes, history, WebSocket
│   │   ├── bhavcopy.py             ← NSE Bhavcopy official EOD
│   │   ├── technical.py            ← 35 technical indicators (RSI, MACD, ADX…)
│   │   ├── markets.py              ← NSE indexes + Global markets (yfinance)
│   │   ├── news.py                 ← RSS fetch + Claude API sentiment
│   │   └── fundamentals.py         ← Alpha Vantage + Screener.in
│   ├── scores\
│   │   ├── engine.py               ← VPI, MRI, RRI, CRI, ZPI, MSI, ACS, MH, ATIP
│   │   └── accuracy.py             ← 5/10/20-day prediction tracking
│   ├── pipeline\scheduler.py       ← Full daily schedule (all 3 windows)
│   ├── dashboard\server.py         ← FastAPI HTML dashboard
│   ├── alerts\telegram.py          ← 10 push alert types
│   └── portfolio\zerodha.py        ← Zerodha Kite fallback
│
└── atip_data\                      ← Auto-created on first run
    ├── db                     ← SQLite database (all data)
    ├── config.json                 ← YOUR API KEYS
    ├── atip.log                    ← Activity log
    └── raw\dhan\                   ← Dhan security list + cache
```

---

## Quick Setup (Windows)

### Option A — PowerShell (Recommended)
```powershell
cd D:\Projects\atip
# Right-click setup_atip.ps1 → Run with PowerShell as Administrator
.\setup_atip.ps1 -All
```
This installs everything, creates config, inits DB, and registers Windows startup.

### Option B — Manual
```powershell
cd D:\Projects\atip

# Step 1: Install packages
install.bat

# Step 2: Create config
mkdir atip_data
copy config_template.json atip_data\config.json
# Edit atip_data\config.json → add your API keys

# Step 3: Init DB + download Dhan security list
python main.py --init
python main.py --dhan-securities

# Step 4: First data run
python main.py --dhan-history

# Step 5: Start
python main.py
```

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

```powershell
# Setup
python main.py --init                    # Create database (first time)
python main.py --dhan-securities         # Download Dhan security ID list (first time)
python main.py --dhan-history            # Download 1-year historical prices

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

## Dhan API Integration Details

### Getting Dhan Credentials
1. Go to [web.dhanhq.com](https://web.dhanhq.com)
2. Login → **My Profile → Apps → Create App**
3. Copy **Client ID** and **Access Token**
4. Add to `atip_data\config.json`:
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

### Dhan Rate Limits
| API | Limit | ATIP Usage |
|-----|-------|-----------|
| Historical daily | 1 req/sec | 0.3s delay between stocks |
| Live quotes (REST) | 100 req/min | ~3 req per 15-min refresh |
| WebSocket | Unlimited ticks | 1 persistent connection |
| Security list | 1 req/day | Downloaded weekly |

---

## Windows Startup Automation

### Method 1 — PowerShell (Easiest)
```powershell
# Run as Administrator
.\setup_atip.ps1 -RegisterStartup
```
ATIP will auto-start 30 seconds after Windows login and also at 6:45 AM on weekdays.

### Method 2 — Task Scheduler (Manual)
1. Open **Task Scheduler** (search in Start menu)
2. **Action → Import Task...**
3. Browse to `D:\Projects\atip\ATIP_TaskScheduler.xml`
4. Click **OK**

### Method 3 — Startup Folder
1. Press `Win + R` → type `shell:startup` → Enter
2. Create shortcut to `D:\Projects\atip\start_atip.bat`

### Verify Auto-Start is Working
```powershell
schtasks /query /tn "ATIP Platform" /fo LIST
```

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

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'atip'` | Run from `D:\Projects\atip\` not from inside `atip\` |
| `Dhan credentials not set` | Add `dhan_client_id` + `dhan_access_token` to `atip_data\config.json` |
| `security_id not found` | Run `python main.py --dhan-securities` first |
| `pandas-ta` fails on Python 3.14 | Use `pip install pandas-ta --no-deps` then `pip install ta` |
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
