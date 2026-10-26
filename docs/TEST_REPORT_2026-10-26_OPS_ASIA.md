# Testing report — UI/UX merge, operating schedule, Asia signals, mobile (26–27 Oct 2026)

Branch `claude/ops-asia-2026-10-26` on top of master `ccc20b3` (UXR-01..03). Run on the owner's Mac (macOS 14,
Python 3.15.2, `.venv`), 26 Oct 22:51 → 27 Oct 00:40 IST — i.e. **inside the new 21:00–06:00 stop window**, which
is why the live servers below report STOPPED.

## 1. Environments — read this first

| Environment | What it actually was | Result |
|---|---|---|
| **Test server** | Local instance, `ATIP_ENV=test`, port 8765, a scratch database seeded with a live Asia fetch | Run |
| **Prod server** | Local instance, `ATIP_ENV=production`, port 8766, a **read-only snapshot copy of the live `atip.db`** (373 MB, 26 Oct 23:47), real network for Asia/news/NSE | Run |
| Hosted Test / Prod servers | **Do not exist** (tracker GAP-2610-16 / UXR-11 BLOCKED: only the owner's Mac runs ATIP) | Not run |
| Live ATIP on the Mac | Not touched: no restart, no writes to `atip_data/` | Not run |

The live dashboard and scheduler must be restarted by the owner to pick up this change (§9).

## 2. Existing test suite

| Run | Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| Baseline (master + UXR, before this work) | 1,739 | 1,689 | 3 | 47 |
| After this work, first run | 1,842 | 1,790 | 5 | 47 |
| **After fixes (final)** | **1,843** | **1,793** | **3** | **47** |
| The 3 sandbox failures re-run outside the sandbox | 14 (whole file) | 14 | 0 | 0 |

The 3 failures present in every run are `tests/test_w39_perf_budget.py` (`test_block_network_refuses_outside_hosts_and_keeps_loopback`,
`test_server_mode_over_real_http`, `test_cli_exit_codes`): the session sandbox refuses local port binding
(`PermissionError: [Errno 1] Operation not permitted` on `bind`). They fail identically on the untouched baseline;
re-run outside the sandbox the whole file passes (14/14), so **no test fails because of code**. The 2 extra failures of the first run were caused by this work and fixed (D3, D4).
Skips: playwright not installed (browser tests), live PostgreSQL/MySQL, node.

## 3. New tests — 104, all pass

| File | Tests | Covers |
|---|---|---|
| `tests/test_operating_schedule.py` | 39 | window boundaries (20:59/21:00/05:59/06:00), IST regardless of caller tz, config hours, STOPPED recorded once + one alert, `run_job` refusal, kill switch closed, **no order placed in the window** (broker path → `BLOCKED_HALTED`, nothing sent), manual stop blocks orders in market hours, order-monitor gate, feeds hold no socket, due jobs skipped not run + daily ones replayed once at 06:00, scheduler loop runs nothing while stopped, **06:00 six steps in order and logged**, STOPPED→RESUMING→RUNNING, once-a-day, **missed-06:00 catch-up on startup**, held lock, crashed run re-run after 2 h, failed step does not stop the rest, **weekend and NSE holiday: steps 1–5 run, live feeds skipped**, manual stop/start/auto + expiry, bad action refused, **loud sentiment-fallback alert**, dashboard read-only (423) with banner on 4 pages, override needs token but works while stopped, writes work in hours, pre-market report stored |
| `tests/test_asia_markets.py` | 46 | 13 session-status cases (JPX lunch, KRX/JPX/PSX holidays, PSX Friday prayer break, weekends), Japan/Korea open 05:30 IST, hours/holidays overridable from `config.json`, no hours hardcoded, **fallback used + failed primary flagged**, stale/empty/flat data counts as failure, all-sources-down reported, verify probes every source, Naver/TWSE(ROC years)/Investing parsers, bullish/bearish/**neutral** signals, volatility spike, too little history, sentiment weighting, Pakistan factor, configurable weights, renormalisation, **5 Pakistan-risk news cases**, end-to-end run (6 indices stored with session status, signals, sentiment, **no order created**), 600-day purge, data-inventory classification, **MH takes the Asia input**, MH weight 0 switches it off, 08:30 brief Asia line, **tab / page / mobile screen / sources API**, calendar check |
| `tests/test_ema_gap.py` | 4 | short history filled + EMA-50 appears, existing rows never overwritten, sessions not retried, failed download recorded, shared hole repaired before one symbol's listing gap (D2) |
| `tests/test_ux_merge_regression.py` | 15 | all 11 existing tabs in order + Global & Asia, Trade-of-Day + KPIs, Buy/Sell target/stoploss/trailing modal fields, **live CMP poll every 15 s with `cmpcell` in-place updates**, `/api/live-quotes` shape in market hours, viewport + font + banner on `/`, `/command`, `/m`, `/global-asia`, Command Center links, mobile 3 screens + PWA v3, **no `date.today()` in merged dashboard route files**, none added to `server.py` |

## 4. UI/UX merge regression checks (prototype: "ATIP Trading Console Redesign" canvas)

| Check | Test | Prod | How |
|---|---|---|---|
| `/`, `/command`, `/m`, `/global-asia` return 200 with banner + phone viewport | Pass | Pass | HTTP |
| ATIP Scores, Portfolio, VPI, Buy Zones (ZPI), RRI, MRI, CRI, FII/DII, News, Signal History, Orders tabs, in order | Pass | Pass | HTTP + unit |
| Trade-of-Day box; Buy/Sell target, stoploss, trailing config modal | Pass | Pass | HTTP + unit |
| Live CMP: `/api/live-quotes` polled every 15 s, cells updated in place (585 / 1,169 `cmpcell`s rendered on Prod) | Pass | Pass | HTTP + unit (market-hours shape mocked; it is night) |
| Desktop layout (1024 px) `/global-asia` | — | Pass | in-app browser screenshot |
| Phone 375 px: `/m` Global & Asia screen and `/command` — no page-level horizontal scroll, tables scroll in their card, 44 px tabs | — | Pass | in-app browser |
| Phone 375 px: `/` | — | **Known (pre-existing)** | renders 1,068 px wide; `/` was never phone-responsive (phones use `/m`) — D8 |
| Colours/fonts/spacing: shared system-ui stack; banner colours identical on web, Android, iOS; ▲/▼ + sign on every change | Pass | Pass | review + unit |
| No hardcoded `date.today()` reintroduced in dashboard routes | Pass | Pass | unit (the UXR halt route had one; replaced with an IST clock) |
| Prototype screens not yet built (Signal Explorer, Trade Setup, Live Monitor, Journal: UXR-06..09) | n/a | n/a | out of scope, unchanged in tracker |

## 5. Scheduler

| Scenario | Result | Evidence |
|---|---|---|
| Stop at 21:00 | Pass | unit (boundaries, one transition row, alert once); **live**: both servers report `STOPPED` "scheduled stop window 21:00–06:00 IST" after 21:00 |
| Resume at 06:00, 6 steps in order, logged | Pass | unit; **real run** on the Prod snapshot (§7) |
| Missed-resume catch-up | Pass | unit (startup at 10:40 → resume, reason "catch-up…"); real run took the catch-up path |
| No duplicate runs | Pass | unit (second call, startup tick, held lock, crashed run) |
| Weekend / NSE holiday | Pass | unit (step 6 SKIPPED, `live_feeds` false, steps 1–5 run) |
| No orders during the stop window | Pass | unit (broker path refused, nothing sent; manual stop too); **live**: `POST /api/orders` → 423 on Test and Prod |
| Manual override (start/stop/auto), logged | Pass | unit; real run (OVERRIDE_START → RUNNING → RESUMING → RUNNING → OVERRIDE_AUTO → STOPPED) |

## 6. Asia markets

Live source verification, 26 Oct 23:5x IST (`python -m data.asia_markets verify`):

| Index | Primary | Fallback(s) | Data fetch | Signal (26 Oct) | Dashboard |
|---|---|---|---|---|---|
| Nikkei 225 | Naver ✅ | Yahoo ✅ | Pass (62,189.65 −0.80%) | BEARISH | Pass |
| TOPIX | Naver ✅ | Yahoo ❌ flagged | Pass (3,972.98 +0.18%) | BEARISH | Pass |
| KOSPI | Naver ✅ | Yahoo ✅ | Pass (5,469.47 −1.46%) | BEARISH | Pass |
| KOSDAQ | Naver ✅ | Yahoo ✅ | Pass (727.21 −3.26%) | BEARISH | Pass |
| TAIEX | TWSE ✅ | Yahoo ✅, Naver ✅ | Pass (46,041.43 −1.81%, 22 Oct) | BEARISH | Pass |
| KSE-100 | Investing ✅ | PSX DPS ❌ HTTP 462, Yahoo ❌ flagged | Pass (163,038.47 +0.41%) | BEARISH | Pass |

Fallback behaviour: Pass (unit: primary down → fallback used, primary flagged). Stooq (all indices) returned a
JavaScript bot wall and is not configured. Asia Sentiment Score 24.1 BEARISH, coverage 100%, Pakistan risk NONE.
Session status at 06:00 IST: Tokyo/Seoul OPEN, Karachi PRE_OPEN (unit). Calendar self-check found the TWSE closure
of 23 Oct (no bar from three sources); added to the calendar.

## 7. Real end-to-end resume (Prod-profile snapshot, 26 Oct 23:53–23:58, manual start)

| Step | Status | Detail |
|---|---|---|
| 1 backfill | PARTIAL | overnight global SUCCESS; GIFT Nifty FAILED (snapshot copy has no Dhan credentials — expected); macro PARTIAL (FRED timeouts through the proxy); multi-asset 126 rows; global news 0 (see D6) |
| 2 collect | SUCCESS | Asia 1,554 rows, all six primaries; Indian news 0 (D6) |
| 3 sentiment | DEGRADED | **critical "NEWS SENTIMENT FALLBACK" alert raised** — `news.ai_enabled` is false (D7) |
| 4 analysis | SUCCESS | scoring catch-up (nothing missing); EMA gap fill (D2 found here) |
| 5 report | SUCCESS | `premarket_2026-10-26.json` written; flagged "late" because it ran at 23:58 |
| 6 live | SUCCESS | feed managers started (they connect only inside 09:00–15:45 on trading days) |

## 8. Defects found and fixes

| # | Defect | Found by | Fix | Status |
|---|---|---|---|---|
| D1 | Asia trend had no dead band: a flat, choppy index scored a full +1 trend and read BULLISH | unit test | 0.2% dead band on close/EMA-20, EMA-20/EMA-50, slope; regression test | Fixed |
| D2 | EMA gap fill spent its 25 downloads on recent sessions one newly listed symbol (HEGAM) lacked — 48 rows, EMA-50 still null for 26 scored names | real run on live-data snapshot | rank sessions by symbols repaired; re-run added 425 rows, **null EMA-50 26 → 9**, AETHER 35 → 60 bars; regression test | Fixed (9 left need more nightly runs / recent listings) |
| D3 | MySQL reserved words: new columns `trigger`, `rows` and a test string | review + suite | renamed `triggered_by`, `n_rows`, test param | Fixed |
| D4 | `test_dhan_data_plan` pinned PWA cache `atip-shell-v2` | suite | cache bumped to v3 on purpose (phones must fetch the new `/m`); pin updated | Fixed |
| D5 | One transient proxy error left KSE-100 empty on the first Test seed | Test server smoke (16/17) | one retry per source on connection errors; Test re-seeded → 17/17 | Fixed |
| D6 | News feeds: `CERTIFICATE_VERIFY_FAILED` in the standalone resume harness | real run | none needed: `start_atip.sh` sets the certifi bundle for the real launcher; harness bypassed it | Environmental |
| D7 | Live `config.json` has no `news.ai_enabled` → keyword sentiment; the 06:00 run will alert every day | real run + config read | owner action: set `news.ai_enabled: true` and `ANTHROPIC_API_KEY` | **Open — owner** |
| D8 | `/` is desktop-only at 375 px | browser | pre-existing; `/m` and the apps are the phone UI | Open (not a regression) |
| D9 | TOPIX and KSE-100 have one working source from India | live verify | flagged in `/api/asia/sources`; alternatives probed (Stooq, PSX, Google) failed | Open |
| D10 | 2026 Asia holiday lists unverified (PSX lunar dates) | review | `holidays_verified:false` + data-driven calendar check | Open |

## 9. Not tested / to do on the Mac

- Restart ATIP (`start_atip.sh` / launchd) to load the change; watch `/api/status` at 21:00 and 06:00 the next day.
- Hosted Test/Prod servers (do not exist). Android/iOS builds: no Android SDK/JDK/Xcode on the Mac; sources only
  (`swiftc -parse` passes; Swift type-check impossible — toolchain/SDK mismatch).
- Browser tests with playwright (not installed); layout checked by hand in the in-app browser instead.
