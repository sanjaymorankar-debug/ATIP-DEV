# Testing report — Dhan real-time market data (31 Oct 2026)

Branch `claude/dhan-pacing-2026-10-31` on top of `8092f74`, working tree (not committed). Run on the owner's Mac
(macOS 14, Python 3.15, `.venv`), Saturday 31 Oct 2026 14:50–16:00 IST — **the market was closed**, so no feed could
be tested against a live Dhan session. Feature description: [DHAN_REALTIME_FEEDS_2026-10.md](DHAN_REALTIME_FEEDS_2026-10.md).

## 1. Environments — read this first

| Environment | What it actually was | Result |
|---|---|---|
| **Unit / integration** | `pytest`, isolated databases, fake Dhan SDK / depth socket / chain clients, no network | Run |
| **Test server** | Local instance, `ATIP_ENV=test`, port 8765, a scratch database (`main.py --init`), **feeds ON for 5 stocks** (RELIANCE, HDFCBANK, INFY, TCS, ICICIBANK), no credentials. A **full session replayed** into it (Fri 30 Oct, 09:00→15:50) through the real feed code with fake Dhan endpoints | Run |
| **Prod server** | Local instance, `ATIP_ENV=staging` (production refuses to start without ATIP sign-in and an encryption key — the documented convention for a server copy), port 8768, a **snapshot copy of the live `atip.db`** (446 MB, 31 Oct 15:21, SQLite backup API), **feeds ON**, no credentials | Smoke run |
| Live Dhan session | Needs a trading day: next is **Mon 2 Nov 2026** | **Not run** |
| Hosted Test / Prod servers | Do not exist (UXR-11); the view-only web server never runs feeds | Not run |
| Live ATIP on the Mac | Not restarted; `atip_data/config.json` not changed (all feeds stay OFF) | Not touched |

Data API plan: checked read-only through `data.dhan.data_plan_status()` — **Active, valid to 28 Oct 2027**.

## 2. Existing test suite (regression)

| Run | Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| Baseline: clean `8092f74` in a separate worktree, before this work | 2,027 | 1,969 | 3 | 55 |
| After this work, first run | 2,070 | 2,011 | 4 | 55 |
| **After fixes (final)** | **2,073** | **2,015** | **3** | **55** |

The 3 failures in every run, baseline included, are `tests/test_w39_perf_budget.py`
(`test_block_network_refuses_outside_hosts_and_keeps_loopback`, `test_server_mode_over_real_http`, `test_cli_exit_codes`):
the session sandbox refuses local port binding (`PermissionError: [Errno 1] Operation not permitted`), as in the 26 Oct
report — **no test fails because of code**. The extra failure of the first run was caused by this work and fixed (D9).
The final count also includes one test from a separate uncommitted change in the working tree
(`tests/test_signal_pipeline.py`, the +4% momentum threshold), which passes. Skips: playwright, live PostgreSQL / MySQL,
node. Regression scope covered: pipeline (scheduler, operating schedule, health), dashboard (UX merge regression, API
contract, UI smoke), orders and paper trading (paper broker, risk engine, broker-health gate), mobile web app.

## 3. New tests — `tests/test_dhan_realtime_feeds.py`, 45 tests, all pass

| Area | Tests | Covers |
|---|---|---|
| Index WebSocket | 6 | the 15:45 close is INFO and not counted; a mid-session drop is a WARNING, the reconnect counted and shown; a refused first connect is still an ERROR and a burst still parks; ticks counted, state row written; a stopped supervisor writes nothing more; token / client id masked |
| Stock WebSocket | 8 | ≤100 symbols on one connection (250 asked → 100, logged); no connection outside the window; REST failover backs off on refusals (60→120→…→300 s) and resets on success; parked socket → REST polls; ATIP's own close not counted; flush writes changed quotes only + status row; OFF by default → OFF rows; switched on → IDLE from the start |
| 1-minute bars | 3 | OHLCV + volume from cumulative volume, partial first minute skipped; REST fallback fills only missing minutes (keeps the WebSocket's bar, drops another day's); fallback waits while the WebSocket streams |
| Tick capture | 1 | lake `ticks` + `tick_capture_status` older than 90 days purged (dry run first), recent kept |
| 20-level depth | 7 | session closes when the window closes (final flush + status); a quiet socket still sees it; 805 parks 30 min, 806 until the next day; ≤50 stocks, one subscription; token masked in status; OFF by default; waits before reconnecting when Dhan closes it |
| Option chain | 5 | Dhan payload → rows (LTP, change, IV, OI, OI change, bid/ask, underlying) for the nearest 2 expiries, expiry list once a day; DH-904 → NSE fallback; due every `chain_minutes`; OFF / IDLE / LIVE states; own 3-second pacing family |
| Health (MON-07, BR-01) | 7 | OFF reads OFF and is no problem; IDLE / WS / STALE; a viewer goes by the Mac's rows; broker health shows OFF, a stale feed is DEGRADED and the **BUY gate stays PASS**; OFF alone leaves overall OK; one alert per change + one on recovery; `/health/market_data` lists all six |
| Dashboard + phone | 4 | `/api/live-feeds` (trader and viewer); Live tab, `/m` Live screen, `/market`; Android and iOS Live tab |
| Constraints | 3 | no feed module can reach order code; every feed OFF in the template and code defaults; status keeps last-data time across a restart |
| Replayed session | 1 | 09:59→10:04 + 15:45 through the stock feed on a driven clock: connect, quotes, 1-minute bars (partial 09:59 skipped), close at the window end, ATIP's own close not counted |

## 4. Test server — a replayed full session (Fri 30 Oct, 5 stocks, every feed ON)

Harness: real `IndexFeedManager`, `StockFeedManager`, `Depth20Feed`, `run_chain_tick`, `ticks.run_eod`; fake Dhan SDK,
depth frames, option-chain client, REST quotes and intraday bars; simulated clock 09:00:00 → 15:50:00, one step a second.
Scripted incidents: Dhan drops the index socket at 13:29:56 and the SDK reconnects at 13:29:58 (the 30 Oct incident);
the stock socket goes silent 11:00–11:10; the window closes at 15:45.

| Feed | Result | Pass |
|---|---|---|
| Index WebSocket | 1,621 `index_levels` rows 09:00:00–15:45:00, no gap at 13:29–13:30 (rows 13:29:30, :45, 13:30:00 …); `1 reconnect(s) today` in the status row; closed at 15:45 — logged INFO "closed by ATIP", no ERROR | ✅ |
| Stock WebSocket | 1,830 `live_quotes` rows from the WebSocket; silent 5 min → parked → **REST failover polled 5 times (11:05–11:09)** → reconnected 11:10; closed at 15:45 | ✅ |
| 1-minute bars | 376 bars per stock 09:15–15:30 (1,880): 1,830 rebuilt from ticks at 15:50, the **50 outage minutes filled by the REST fallback** | ✅ |
| Tick capture | 10,950 ticks in 366 lake parts, 0 dropped; `tick_capture_status` row for 30 Oct | ✅ |
| 20-level depth | 7,505 `depth20_snapshot` rows for 5 stocks, 7,500 with OFI (the first per stock has no interval), DWI computed | ✅ |
| Option chain | 26 snapshots, 2,288 rows (NIFTY + BANKNIFTY, 2 expiries, from the Dhan source) | ✅ |
| Connections | 3 opened (index, stock, stock reconnect), **0 left open** after 15:45 | ✅ |

Then the Test server was started on that database (feeds ON, Saturday): all six feeds **IDLE** with their last-data
times, no connection attempted ("connecting at 09:00 on the next trading day"), no server errors. Live tab, `/m?tab=live`
at 375 px (no horizontal scroll, 44 px tabs), `/market`, `/api/market/feed-status`, `/health/market_data` checked in
the in-app browser.

## 5. Prod server — smoke (snapshot of the live database, feeds ON)

| Check | Result | Pass |
|---|---|---|
| Starts; feeds supervised, none connects outside the window | "Index WebSocket feed supervised — market closed", stock and depth supervised; no Dhan call | ✅ |
| `/api/live-feeds` | 6 feeds, all IDLE, no problem flagged | ✅ (after D3) |
| `/health/market_data` (MON-07) | `feeds` block, all IDLE, `feed_problems` empty | ✅ |
| `/`, Live tab between Orders and Global & Asia; existing 11 tabs in order | as before + Live | ✅ |
| `/m?tab=live`, `/market`, `/api/market/feed-status` (6 feeds), `/api/mobile/summary` (`live_feeds`) | 200, content present | ✅ |
| `/api/live-quotes` outside hours | 200, cached (no Dhan call) | ✅ |
| Server errors | none | ✅ |

## 6. Defects found and their status

| # | Defect | Found by | Fix | Status |
|---|---|---|---|---|
| D1 | 30 Oct 15:45 "Index WebSocket error: sent 1000 (OK)": ATIP's own close logged as an ERROR and counted toward an error burst (index and stock feed) | log review | callbacks from a detached SDK instance are INFO and not counted | Fixed |
| D2 | 30 Oct 13:29 drop logged as an ERROR with nothing saying it recovered | log review | WARNING + "reconnected after Ns" + count in the status row (cause is Dhan's side) | Fixed (wording) |
| D3 | 20-level depth held its socket past 15:45: the read loop waited for the next frame and never checked the window | code review | window checked every 30 s; a quiet socket wakes every 5 s | Fixed |
| D4 | 20-level depth stopped for good on any Dhan refusal (805–809), needing a restart | code review | parks 30 min (805/807) or until the next day (806/808/809) | Fixed |
| D5 | 20-level depth subscribed >50 stocks on one connection (Dhan allows 50) | code review | capped at 50 | Fixed |
| D6 | Option chain ignored `chain_minutes` (fixed 15-min schedule) and came from NSE, not Dhan | code review | checked every minute, due by `chain_minutes`; Dhan source with NSE fallback | Fixed |
| D7 | Tick lake had no retention | code review | 90-day SHORT tier (`data/lake.purge`) | Fixed |
| D8 | A feed thread left sleeping by `stop()` ran once more after its test and **wrote one `live_feed_status` row into the real `atip_data/atip.db`** (31 Oct 15:07:43, `indices` IDLE) | live DB check | threads exit after `stop()`; `tests/conftest.py` points `DB_PATH` at a throwaway file outside tests; the stray row was deleted (the table is back to 0 rows) | Fixed |
| D9 | New Live tab appended after Global & Asia broke the 26 Oct tab-order contract | regression suite | Live sits before Global & Asia | Fixed |
| D10 | Prod smoke: switched-on feeds read NOT STARTED until their first flush (option chain: all weekend) | Prod server | feeds write IDLE when they start; the chain writes IDLE outside the window | Fixed |
| D11 | A restart wiped each feed's last-data time and instrument count from its status row | Test server | the status write keeps them when the new process has none | Fixed |
| D12 | Depth reconnected at once, in a loop, if Dhan closed the socket normally in the session | code review | waits 5 s (doubling) before reconnecting | Fixed |

No open defects from this work. Not verified (needs a session): see §8.

## 7. Logs per feed (replayed session; secrets masked)

Wall-clock stamps are the replay's; the session time is in brackets.

```
data.dhan_ws     ✓ Index WebSocket feed started — 14 indexes subscribed                         [09:00:00]
data.dhan_ws     Index WebSocket connected
data.stock_feed  ✓ Stock WebSocket feed started — 5 symbols
data.stock_feed  Stock WebSocket closed by ATIP (sent 1000 (OK); no close frame received)        [11:05:00]
data.stock_feed  Stock WebSocket disconnected — connected but silent
data.stock_feed  ⚠ Stock feed parked (parked 5 min: connected but silent) — REST failover
data.stock_feed  ✓ Stock WebSocket feed started — 5 symbols                                       [11:10:00]
data.dhan_ws     Index WebSocket dropped by the server (no close frame received or sent) — the SDK is reconnecting   [13:29:56]
data.dhan_ws     Index WebSocket reconnected after 2s (reconnect 1 today)                         [13:29:58]
data.dhan_ws     Index WebSocket closed by ATIP (sent 1000 (OK); no close frame received)         [15:45:30]
data.dhan_ws     Index WebSocket feed disconnected — market closed
data.stock_feed  Stock WebSocket closed by ATIP (sent 1000 (OK); no close frame received)
data.stock_feed  Stock WebSocket disconnected — market closed
replay           15:50 run_eod: {"status": "SUCCESS", "rows": 1880, "rest_fallback": {"rows": 50, "symbols": 5, "failed": 0}}
```

`live_feed_status` after the session:

| feed | mode | instruments | count | last data | detail |
|---|---|---|---|---|---|
| indices | IDLE | 14 | 22,694 ticks | 15:45:00 | outside 09:00-15:45 on a trading day; 1 reconnect(s) today |
| stocks | IDLE | 5 | 10,950 ticks | 15:30:00 | 5 symbols subscribed |
| ticks | LIVE→IDLE | 5 | 10,950 | 15:30:00 | 30 ticks to the lake this flush |
| minute_bars | IDLE | 5 | 1,820 bars | 15:30:00 | one-minute bars from the WebSocket; REST fallback every 15 min |
| depth20 | IDLE | 5 | 7,505 snapshots | 15:30:00 | window closed: disconnected |
| option_chain | LIVE→IDLE | 2 | 88 rows (last tick) | 15:30:00 | NIFTY 44 rows (dhan); BANKNIFTY 44 rows (dhan) |

(`ticks` and `option_chain` keep their last in-session mode in the row; the health reader shows them IDLE once the
window has closed.)

## 8. Not verified, and why

- **Against Dhan itself**: the WebSocket URLs and packet layouts come from dhanhq 2.2.0 (checked), the 20-level frame
  layout from dhanhq `fulldepth.py`, the option-chain payload from dhanhq `_option_chain.py` and Dhan's v2 response
  shape (both nestings handled). None of it has met a live Dhan session in this work — **next session Mon 2 Nov**.
- A Telegram feed alert in a real session (unit-tested only).
- Android build: no Android SDK / JDK on the Mac (one tab entry and one string added). iOS: `swiftc -parse` passes; no
  Xcode build.

## 9. Recommended live test window (owner)

Mon 2 Nov, 09:00–15:50: switch on the block in [DHAN_REALTIME_FEEDS_2026-10.md](DHAN_REALTIME_FEEDS_2026-10.md) for the
same 5 stocks, restart ATIP before 09:00, watch the Live tab; after 15:50 check `live_feed_status`, 375 one-minute bars
per stock, `depth20_snapshot`, 26 option-chain snapshots and `tick_capture_status`. Then decide the production symbol
list and whether to leave each feed on.
