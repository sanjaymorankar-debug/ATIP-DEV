# ATIP W1 Foundation: development handoff for independent validation

Developed 2026-09-24 in `D:\Projects\ATIP` (branch `master`, base `99e80d8`).
**Nothing in this wave has been tested, run against the live database, committed, pushed or deployed.**
The only checks were syntax compilation (`python -m py_compile`) of the changed files. The tests listed
below were written or updated but **not run** in this wave.

W1 scope: DP-19, DBS-06, PF-01, PF-02, PF-13, BR-04, RK-07, RK-08, RK-13, MON-01, MON-02, MON-03, NS-01, OPS-03.
Also in the same uncommitted tree, written earlier the same day before the W1 brief arrived and outside W1:
MON-06 (log rotation), DB-14 (morning brief health section), RK-15 (risk alerts). They are listed so they get validated too.

## 1. Changed functionality

| ID | Feature | What was implemented |
|---|---|---|
| MON-01 | Job logging | `run_job` records every scheduled run in `pipeline_log` (`kind='run'`): start, end, duration, rows, status, error. A job that *returns* FAILED is a failure (it used to log ✅ and alert nobody). SUCCESS with 0 rows is recorded as `EMPTY`. `log_job` no longer swallows its own errors. |
| MON-02 | Stalled-job detection | `pipeline/health.py`: expected behaviour per job (daily deadline / intraday interval / weekly) → MISSED, STALLED, FAILING (3 failures in a row), EMPTY. Nothing is judged before monitoring began. Runs every 30 min (07:10–23:40). |
| MON-03 | Failure alerting | Every alert is recorded in `alert_log` and shown on the dashboard, plus Telegram when configured. De-duplicated by key per day. The Telegram token is scrubbed from error text. `python -m alerts.telegram --test` explains setup when not configured. |
| NS-01 | News ingestion | News runs first in pre-market. Morning catch-up (start-up, 08:20, 12:20) fetches news if none today. De-dup against the last 3 days by URL or headline key. Per-feed status (`news_source_status`). 4 more RSS feeds (unverified URLs). A run that finds only already-stored stories reports `NO_NEW`. No sentiment/AI changes. |
| PF-01 / BR-04 | Portfolio ingestion / sync | Each sync attempt is recorded in `portfolio_sync`, with its reason on failure (DH-901 = Dhan token expired, stated as such). Same-day re-sync updates qty/avg cost and removes sold symbols. Zerodha reports SKIPPED (not FAILED) when not connected. Morning catch-up re-syncs if there's no successful sync today. Health expects a real SUCCESS. |
| PF-02 | Holdings & P&L | `portfolio/pnl.py`: one view of the PAPER book (paper_position + cash) and the LIVE book (latest successful sync). Mark-to-market at live quote / session close; unrealised, realised (paper), cash, equity. `GET /api/pnl`. |
| PF-13 | Daily P&L | `pnl_daily` (date, env): equity, day P&L, peak equity, drawdown %. Recorded post-market (`daily_pnl` job), or via `python -m portfolio.pnl --record`. |
| RK-07 | Daily loss limit | `risk_limits.max_daily_loss_value` (₹): blocks new BUYs when today's loss (now vs the last `pnl_daily` row) exceeds it. Set but unmeasurable also blocks. SELLs never blocked. |
| RK-08 | Drawdown limit | `risk_limits.max_drawdown_pct`: blocks new BUYs when equity is that far below its stored peak. Same rules as RK-07. |
| RK-13 | Risk-based sizing | `orders.risk.size_position()` (pure) and `size_for_account()` (capital from config or account equity). Order rules accept `quantity_type: "RISK"` (quantity_value = % of capital risked; needs a BUY with a stoploss below the trigger), sized at execution. Dashboard order form has the option. |
| DP-19 | Data quality layer | `data/quality.py`: 12 weighted checks + info check per session over the tracked universe → `data_quality` rows + DQS (0–100). Runs post-market before scoring; alerts when DQS < 90 or any ERROR. Reports only, does not gate scoring. `GET /api/data-quality`. |
| DBS-06 | Timestamp consistency | Convention documented in `db/schema.py`: IST local everywhere except `created_at DEFAULT CURRENT_TIMESTAMP` (UTC). News publication times now converted UTC → IST. `--status` compares UTC `created_at` correctly. `tools/repair_news_timezone.py` fixes stored news rows (dry run default; **not run**). |
| OPS-03 | Install / setup | `setup.py`, `install.bat`, `pip_install.bat`, `setup_atip.ps1`: `ta` instead of the uninstallable `pandas-ta`. `requirements-dev.txt` (pytest, httpx). CI installs httpx. `config_template.json` gains `risk_limits` / `position_sizing`. `docs/DEV_SETUP.md`. |

## 2. Files changed

| File | Change | Reason |
|---|---|---|
| pipeline/scheduler.py | run_job records runs; news first; `run_morning_catchup`; `run_health_check`; `data_quality` + `daily_pnl` jobs; status query UTC fix | MON-01/02, NS-01, BR-04, DP-19, PF-13, DBS-06 |
| pipeline/health.py | **new** | MON-02 |
| alerts/telegram.py | `notify()` records to alert_log, token scrub, health alerts, brief health section, `--test` setup help | MON-03, DB-14 |
| dashboard/server.py | Alerts/health panel; MH "—" when unknown; RISK quantity display/option; `/api/alerts`, `/api/pnl`, `/api/data-quality` | MON-03, RK-13, PF-02, DP-19 |
| db/schema.py | log_job start/end/kind; migrations for pipeline_log cols, alert_log, pnl_daily, data_quality, news_source_status, portfolio_sync, market_health.portfolio_health; time convention doc | all |
| data/news.py | UTC→IST; per-feed status; de-dup; 4 feeds; NO_NEW | NS-01, DBS-06 |
| data/quality.py | **new** | DP-19 |
| data/dhan.py | sync failure reasons (`describe_dhan_error`), `record_portfolio_sync`, qty/avg update, remove sold | PF-01, BR-04 |
| portfolio/zerodha.py | SKIPPED when not connected; sync recording; qty/avg update | BR-04 |
| portfolio/pnl.py | **new** | PF-02, PF-13 |
| orders/risk.py | loss limits, sizing functions, `risk_alert`, status output | RK-07/08/13, RK-15 |
| orders/rules.py | `quantity_type RISK` validation + execution sizing | RK-13 |
| orders/broker.py | risk alerts on halted / limit-refused orders | RK-15 |
| main.py | rotating log handler | MON-06 |
| tools/repair_news_timezone.py | **new** (not run) | DBS-06 |
| setup.py, install.bat, pip_install.bat, setup_atip.ps1, README.md, requirements-dev.txt (**new**), .github/workflows/tests.yml, config_template.json, docs/DEV_SETUP.md (**new**) | dependency and setup fixes | OPS-03 |
| tests/conftest.py | every test isolated DB + Telegram stub | test safety |
| tests/test_job_monitoring.py (**new**), tests/test_market_health.py | tests written for MON-01/02/03 (not run in W1) | validation |

## 3. Database changes (all additive, applied on connection)

| Object | Change |
|---|---|
| `pipeline_log` | + `kind` TEXT ('run' / 'step'), + `duration_s` REAL, + index (job_name, start_time) |
| `alert_log` | **new**: created_at, category, severity, title, message, dedupe_key, telegram_sent, telegram_error |
| `pnl_daily` | **new**: UNIQUE(date, env); n_positions, positions_value, cost, unrealised, realised_cum, cash, equity, day_pnl, peak_equity, drawdown_pct, recorded_at |
| `data_quality` | **new**: UNIQUE(date, check_name); severity, failed, checked, score, detail (JSON), run_at |
| `news_source_status` | **new**: source PK, url, last_attempt, last_ok, last_items, consecutive_failures, last_error |
| `portfolio_sync` | **new**: date, source, status, n_holdings, error, synced_at |
| `market_health` | + `portfolio_health` added to the standard migration (was only added by scores/portfolio_health.py) |
| `news_articles` | stored `fetched_at` values still UTC until `tools/repair_news_timezone.py --apply` is run |

## 4. API changes (new, read-only; existing routes unchanged)

| Route | Returns |
|---|---|
| `GET /api/alerts` | `{job_health: [...], alerts: [...last 72h]}` |
| `GET /api/pnl` | `{books: {PAPER, LIVE}, risk: {PAPER, LIVE}, daily: [pnl_daily], last_sync: [portfolio_sync]}` |
| `GET /api/data-quality` | `{date, checks: [...], dqs_history: [...]}` |
| `POST /api/orders` (existing) | now also accepts `quantity_type: "RISK"` (validated in orders/rules.py) |

## 5. Configuration (atip_data/config.json, all optional)

| Key | Default | Effect |
|---|---|---|
| `risk_limits.max_daily_loss_value` | unset | ₹; blocks BUYs past today's loss |
| `risk_limits.max_drawdown_pct` | unset | %; blocks BUYs below peak equity |
| `position_sizing.capital` | null = account equity | capital for risk sizing |
| `position_sizing.risk_per_trade_pct` | 1.0 | % of capital risked per trade |
| `position_sizing.max_position_pct` | 10.0 | cap per position, % of capital |
| `telegram_token`, `telegram_chat_id` | placeholders | Telegram delivery; alerts show on the dashboard regardless |

## 6. Expected behaviour

| Feature | Input | Expected output |
|---|---|---|
| MON-01 | `run_job("x", lambda: {"status":"FAILED","error":"e"})` | pipeline_log row kind=run, status FAILED, error_msg "e", end_time set; failure alert recorded |
| MON-01 | job returns `{"status":"SUCCESS","rows":0}` | status EMPTY |
| MON-02 | trading day 08:40, no `news_premarket`/`news_catchup` run today, monitoring began earlier | Problem "News (morning)" MISSED |
| MON-02 | 3 consecutive FAILED `dhan_portfolio` runs | "Portfolio sync" FAILING with the last error |
| MON-02 | `intraday_indexes` last run 09:15, now 11:00 on a trading day | STALLED; not reported after 15:30 |
| MON-03 | same alert key twice in a day | one alert_log row; second call `duplicate: true` |
| MON-03 | Telegram HTTP error with token in URL | stored/logged error contains `<token>`, never the token |
| NS-01 | the same story fetched by two runs | stored once |
| NS-01 | a feed URL that errors | its `news_source_status` row: `last_error` set, `consecutive_failures` incremented |
| PF-01 | Dhan returns DH-901 | `sync_dhan_portfolio` → FAILED with "DH-901 Dhan access token invalid or expired — regenerate…"; portfolio_sync FAILED row |
| PF-01 | Dhan returns no holdings | portfolio_sync SUCCESS n_holdings=0; LIVE book reads empty |
| PF-13 | paper cash 90,000 + position 10 × ₹1,100; previous row equity 100,000 | pnl_daily PAPER equity 101,000, day_pnl +1,000 |
| RK-07 | `max_daily_loss_value` 5000, day P&L −6000, BUY | BLOCKED_RISK_LIMIT, blocked_by max_daily_loss_value; SELL not blocked |
| RK-07 | limit set, no pnl_daily history | BUY blocked as unmeasurable |
| RK-08 | peak 100,000, equity 88,000, `max_drawdown_pct` 10 | BUY blocked (12% drawdown) |
| RK-13 | `size_position(100, 95, capital=100000)` | quantity 200 (₹1,000 risk / ₹5), but capped at 10% → 100 shares, capped_by max_position_pct |
| RK-13 | `size_position(100, 105, capital=100000)` | quantity 0, reason "stop … is not below entry" |
| RK-13 | rule quantity_type RISK on a SELL, or without a stoploss | create_rule raises ValueError |
| DP-19 | a tracked bar with close > high | ohlc_relationship failed ≥ 1, DQS < 100 |
| DP-19 | a bar identical to the previous session's | stale_bars counts it |
| DBS-06 | feed entry published 03:00 UTC | stored fetched_at 08:30 IST |

## 7. Test scenarios for ChatGPT

| Test ID | Feature | Scenario | Expected result |
|---|---|---|---|
| W1-T01 | MON-01 | Run `python -m pytest tests/test_job_monitoring.py` | all pass |
| W1-T02 | MON-01 | Run a job that raises; inspect pipeline_log | kind=run, FAILED, rows NULL, "ExceptionType: msg" |
| W1-T03 | MON-02 | Seed pipeline_log per §6; call `check_job_health(conn, now)` | Problems as §6 |
| W1-T04 | MON-02 | Fresh DB with no run rows | `check_job_health` returns [] |
| W1-T05 | MON-03 | No Telegram config; raise a job failure | alert_log row telegram_sent=0, telegram_error "not configured"; dashboard panel lists it |
| W1-T06 | MON-03 | GET /api/alerts | JSON with job_health and alerts |
| W1-T07 | NS-01 | `store_articles` twice with the same articles | second call stores 0 |
| W1-T08 | NS-01 | Feed parse raising an exception (mock feedparser) | news_source_status failure recorded, other feeds still stored |
| W1-T09 | NS-01 | `run_morning_catchup` on a trading day after 07:00 with no news run | runs `news_catchup`; second call does nothing |
| W1-T10 | BR-04 | Mock Dhan get_holdings failure DH-901 | FAILED + readable error + portfolio_sync row + alert |
| W1-T11 | PF-01 | Sync twice same day with qty change and one symbol sold | qty/avg updated; sold symbol removed for that date |
| W1-T12 | BR-04 | Zerodha with no token file | `run_portfolio_sync` → SKIPPED; health still FAILING/MISSED if Dhan failed |
| W1-T13 | PF-02 | Paper position + live quote today | `portfolio_summary` value uses live ltp; otherwise last close |
| W1-T14 | PF-13 | `record_daily_pnl` on two dates | second row day_pnl = equity difference; peak/drawdown correct; re-run replaces |
| W1-T15 | RK-07 | Limits configured, day loss beyond cap, BUY and SELL via `pretrade_check` | BUY blocked, SELL ok |
| W1-T16 | RK-07/08 | Limits configured, no pnl_daily rows | BUY blocked with "cannot be measured" |
| W1-T17 | RK-13 | `size_position` boundaries: no stop, stop ≥ entry, tiny capital, cap binding | per docstring |
| W1-T18 | RK-13 | Create RISK rule (BUY with stoploss) and execute with confirm=False | quantity from sizing; no order placed in dry run |
| W1-T19 | DP-19 | Seed invalid/OHLC/stale/gap/abnormal bars; `run_data_quality(td)` | each check counts them; DQS row; alert when DQS < 90 |
| W1-T20 | DP-19 | GET /api/data-quality | latest date's checks + history |
| W1-T21 | DBS-06 | `tools/repair_news_timezone.py` dry run then `--apply` on a copy DB; run again | first moves UTC rows +5:30; second moves 0 |
| W1-T22 | OPS-03 | Fresh venv: `pip install -r requirements-dev.txt`; `python main.py --init`; `python -m pytest` | installs without pandas-ta; DB created; suite runs |
| W1-T23 | Regression | Full `python -m pytest` | existing tests still pass (390+ before W1 edits) |
| W1-T24 | MON-06 | Log handler with small maxBytes; hold file open in another handle | rotation works; a refused rename keeps logging, retries later |

## 8. Known limitations

- **LIVE P&L is approximate.** The broker's holdings carry no realised P&L. LIVE equity needs Dhan's available funds (only asked for today). Deposits and withdrawals move equity as if they were P&L. When cash is unknown, day P&L falls back to the change in unrealised P&L.
- **Loss limits fail closed.** With a loss limit configured, BUYs are refused until a `pnl_daily` row exists before today. That's at least one post-market run after enabling.
- **Loss limits don't halt.** They block new BUYs only. The kill switch is still manual.
- **Pre-market deaths not fixed at the root.** The ~07:00 pre-market process death is mitigated (news first, catch-ups), not explained. The Windows task and event logs were deliberately not inspected.
- **Dhan token renewal is manual.** DH-901 is reported and alerted, not fixed automatically.
- **New RSS feed URLs are unverified.** `news_source_status` will show whether they answer.
- **Data quality reports but doesn't gate scoring.** The universe is today's constituent list, so survivorship bias (BT-15) remains.
- **Stored news times are still UTC.** They are fixed only when `tools/repair_news_timezone.py --apply` is run, after a backup.
- **Audit columns stay UTC.** `created_at` columns keep SQLite UTC by design, as documented.
- **Predictions ignore sizing overrides.** `scores/predictions.py` still uses its own constants for `position_size_pct`, with the same defaults as `SIZING_DEFAULTS`. Config overrides don't reach it.
- **The running instance is on older code.** It runs commit `99e80d8` until restarted. Nothing in W1 has been deployed.
