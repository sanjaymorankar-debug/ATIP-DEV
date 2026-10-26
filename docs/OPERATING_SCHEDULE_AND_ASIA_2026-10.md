# Operating schedule (21:00–06:00 IST stop) and Asia market signals — 2026-10-26

Code: `pipeline/operating_schedule.py`, `pipeline/premarket_report.py`, `data/asia_markets.py`,
`data/asia_exchanges.json`, `data/ema_gap.py`, `dashboard/global_asia.py`. Tests: `tests/test_operating_schedule.py`,
`tests/test_asia_markets.py`, `tests/test_ema_gap.py`, `tests/test_ux_merge_regression.py`.

## 1. States

| State | When | What happens |
|---|---|---|
| STOPPED | 21:00 → 06:00 IST, or a manual stop | No scheduled job runs (`schedule` slots are skipped unrun; daily ones are recorded in `deferred_jobs`); `run_job` refuses; the Dhan WebSocket / stock / depth feeds hold no socket; the dashboard order monitor does not call `check_triggers`; `orders.risk.halted()` returns the stop reason, so **every order path refuses** (broker, paper, OMS, risk engine). Dashboard GET pages work; writes get HTTP 423. Banner: "ATIP stopped — resumes 06:00 IST" + last-updated time. |
| RESUMING | from 06:00 (or first tick after a missed 06:00) | The six-step resume sequence, logged as `resume_1_backfill` … `resume_6_live` in `pipeline_log` and step by step in `resume_runs.steps_json`. |
| RUNNING | after the sequence, until 21:00 | Normal day schedule. |

Every transition is a row in `scheduler_status` (status, previous, source = schedule / startup / wake / manual, reason).
`GET /api/status` serves the current state, why, next stop/resume, last data update, today's resume steps, the
override and the last 12 transitions.

## 2. The 06:00 resume sequence

1. **Backfill** — overnight global fetch (S&P, Dow, Nasdaq, FTSE, DAX, Nikkei, Hang Seng, Brent/WTI, gold, silver,
   USD/INR, DXY, US 3M/5Y/10Y/30Y), GIFT Nifty (trading days), macro (FRED/World Bank), multi-asset, global + Asia +
   Pakistan news (`data/news.py GLOBAL_FEEDS`), then each daily job the window deferred (once each).
2. **Collect** — Asia markets (`asia_markets.run("open")`: Tokyo and Seoul opened at 05:30 IST, so their opening gap is
   captured), Indian news, Dhan security list if missing.
3. **Sentiment** — Claude news summary; if any headline since the resume started was classified by the keyword
   fallback, the step is DEGRADED and a **critical** alert ("NEWS SENTIMENT FALLBACK") goes to the dashboard alert
   panel and Telegram.
4. **Analysis** — `run_postmarket_if_missing` (scores any unscored session: VPI, ZPI, MRI, RRI, CRI, MSI, ACS, Market
   Health incl. the Asia input, technicals) and the **EMA gap fill** (§5).
5. **Report** — `premarket_reports` + `atip_data/reports/premarket_<date>.json`; refreshed at 08:15 so the 08:30
   morning brief and the 09:15 open see current inputs. A report built after 08:30 raises an alert.
6. **Live** — trading days only: WebSocket feeds restarted if stopped; the order monitor resumes on its next pass.
   Weekends / NSE holidays: steps 1–5 run, step 6 is SKIPPED ("weekend / NSE holiday — live feeds stay off").

**Catch-up and no duplicates.** The scheduler loop calls `operating_schedule.tick()` every 15 s, at start-up and on
wake from sleep. If 06:00 passed with no `resume_runs` row for today, the sequence runs immediately. A
`resume_runs` row per IST date plus `ops.jobs.job_lock("operating_resume")` make it run once per day across ticks,
restarts and processes; a run that crashed (still RUNNING after 2 h) is re-run.

## 3. Manual override

`POST /api/status/override {"action": "stop"|"start"|"auto", "reason": "..."}` (dashboard token required; owner-only
in enterprise mode) or `python -m pipeline.operating_schedule stop|start|auto --reason ...`.
`stop` lasts until the next 06:00; `start` lasts until the next 21:00; `auto` clears. Each is logged in
`scheduler_status` (source `manual`) and the alert log.

## 4. One scheduling mechanism — why not APScheduler or Windows Task Scheduler

The in-process `schedule` loop (`pipeline/scheduler.py`) stays the only job scheduler; the operating schedule gates it.
APScheduler would mean re-registering ~120 jobs and re-implementing the debounce guard, W8 job locks, W9 leader lease
and wake detection for no gain (its misfire handling has the same "late after sleep" problem). Windows Task Scheduler
does not exist on the Mac ATIP now runs on, and an OS scheduler next to the loop would be the two-mechanism,
duplicate-run setup this rules out. launchd only starts the process. Order-trigger monitoring stays in the dashboard
process, separate from the pipeline scheduler, and checks the window itself.

`.at()` slot times are the Mac's local clock (assumed IST, as before); the window is computed in `Asia/Kolkata`
explicitly.

Config (`atip_data/config.json`): `"operating_schedule": {"enabled": true, "stop_at": "21:00", "resume_at": "06:00",
"report_ready_by": "08:30"}`. `ATIP_OPERATING_SCHEDULE=off` disables it (the test suite sets this).

**Moved by the window:** jobs that used to run after 21:00 — FII/DII watch slots 21:00–21:30, history backfill
22:20, overnight global 23:00, Nifty history 23:20, multi-asset 23:30, health checks 21:10–23:40 — now run in the
06:00 replay instead. FII/DII data published after 21:00 is therefore settled at 06:00, not the same evening.

## 5. Null-EMA gap (small/mid-caps)

On 2026-10-26 `ema_50`/`sma_200` were NULL for 2,772 of 3,279 symbols (26 in the scored universe: AETHER, KSB,
KIRLOSBROS, SANSERA, CUPID, …). Cause: their price history starts ~33 sessions back (CM Bhavcopy archive), and
EMA-50 needs 50 bars; Dhan's history backfill fails with DH-902 (Data API plan inactive). `data/ema_gap.py` fills
missing past sessions from NSE's Bhavcopy archive — one file per session repairs every short symbol — inserting only
rows that do not exist, newest first, 25 sessions per run, then recomputes their indicators. Config `"ema_gap"`.

## 6. Asia markets

| Index | Exchange (hours, local) | Primary | Fallback(s) | Verified 2026-10-26 |
|---|---|---|---|---|
| Nikkei 225 | JPX 09:00–11:30, 12:30–15:30 JST (05:30–12:00 IST) | Naver world `.N225` | Yahoo `^N225` | both OK |
| TOPIX | JPX | Naver world `.TOPX` | Yahoo `^TOPX` | Yahoo **FLAGGED** (no data) |
| KOSPI | KRX 09:00–15:30 KST (05:30–12:00 IST) | Naver `KOSPI` | Yahoo `^KS11` | both OK |
| KOSDAQ | KRX | Naver `KOSDAQ` | Yahoo `^KQ11` | both OK |
| TAIEX | TWSE 09:00–13:30 CST (06:30–11:00 IST) | TWSE `MI_5MINS_HIST` | Yahoo `^TWII`, Naver `.TWII` | all OK |
| KSE-100 | PSX Mon–Thu 09:30–15:30, Fri 09:15–12:00 & 14:30–16:30 PKT (10:00–16:00 IST) | Investing.com `49677` | PSX DPS, Yahoo `^KSE` | PSX **FLAGGED** (HTTP 462), Yahoo **FLAGGED** |

Stooq was probed for every index and is not configured (JavaScript bot wall). Hours and holiday calendars live in
`data/asia_exchanges.json` (overridable from `config.json "asia_markets"`); holiday lists are best-known 2026
calendars, marked `holidays_verified: false`, and `GET /api/asia/sources` returns a data-driven calendar check
(listed holiday with a bar / weekday with no bar). It found the TWSE closure of 2026-10-23, now listed.

Runs: 06:00 (resume step 2), 10:45 (Taiwan + Pakistan sessions), 17:15 (after every close incl. PSX). Tables:
`global_indices_daily`, `asia_signals`, `asia_sentiment`, `asia_source_health` (600-day retention for the first three).

**Per-market signal**: trend (close vs EMA-20, EMA-20 vs EMA-50, EMA-20 slope; 0.2% dead band), momentum (RSI-14,
5-session return), volatility (20-day realised vs its longer median), gap (open vs previous close) → score −100…100,
BULLISH ≥ 20 / BEARISH ≤ −20 / NEUTRAL, confidence 0–100 (agreement, history completeness, strength).

**Asia Sentiment Score** (0–100, 50 neutral): weights N225 .25, TOPIX .10, KOSPI .20, KOSDAQ .05, TAIEX .25,
KSE-100 .05 and **PK_GEO_RISK .10** — the Pakistan geopolitical/risk-event flag from classified news (NONE 50 /
ELEVATED 30 / HIGH 0), a separate factor, not price. Feeds Market Health via `weight_config ("MH","Asia")`
(`asia_markets.mh_weight`, default 0.05; 0 switches it off), the pre-market report and the 08:30 brief.
**Informational only — no order execution.**
