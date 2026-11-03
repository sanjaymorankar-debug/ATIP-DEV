# ATIP full code review: every module, every session's work (3 Nov 2026, prompt 10:54 PM IST)

Branch `claude/full-review-2026-11-03`, cut from master `29b488e` (3 Nov 22:38 IST). Five read-only reviewers covered
the code in parallel (ingestion · orders and execution · pipeline, ops, db, alerts · dashboard · scores, quant, ml,
backtest, research). Every High and Medium finding below was checked against the code before it was fixed. The live
database was read read-only, and the fixes were tested on a copy. All times are IST.

**Risk:** High = can lose money, place a wrong order, or corrupt stored data. Medium = wrong numbers on screen or in
a score, a job that silently fails, or a latent High. Low = cosmetic, or a rare edge.
**Status:** ✅ fixed in this patch · ⏸ open (reason given) · ℹ checked, no defect.

## 0. The four specific checks the brief asked for

| Check | Result |
|---|---|
| **Hardcoded `date.today()`** | Most uses are harmless query bounds, or the scheduler passes the resolved trading date. Real defects fixed here: `scores/engine.py` scored `date.today()` from the CLI (a weekend run wrote news-only rows stamped with a closed day). It now defaults to the last trading day with EQ closes and refuses a day with no bars (`NO_DATA`). Open and Low: `research/screener.py:935`, `strategy_engine/option_overlay.py:489`, `scores/portfolio_health.py:62`, `w39_routes.py:196` cache key. |
| **NULL vs 0** | Fixed: `data/technical.py` stored "no 200-DMA" as `above_200dma = 0` ("below"), which cost every recent listing its Trend score. `determine_signal` read a market health of 0 as 50 and raised on `mri = None`. Post-market alerts crashed on a NULL MH / TOD score (5 times in Oct). The live-quote API returned NaN (one bad row froze every CMP). A LIVE MARKET buy with no quote was valued at ₹0. The confirm dialog now shows "—", never 0. The earlier session's engine fixes (SV-02a/b/c) were re-verified. |
| **Dual-batch sawtooth (~73 large caps)** | **Not reproduced in the scored universe.** Among the 504 Dhan-tracked symbols there are 0 series with ≥ 4 alternating ±5 % moves in the last 120 sessions. Across all 3,548 symbols there are 23, mostly SME/BE stocks with real volatility. Dhan-written closes equal NSE's official close on 99.6 % of rows; the rest are corporate-action-adjusted names. The one real basis mix was **TDPOWERSYS**: 165 raw pre-split archive rows inside its adjusted history (−51 % / +104 % / −49 % jumps). That is fixed (I-H1) and repaired. The `source` column misleads this check because it keeps the first writer (I-M6). |
| **Conflicting writes between sessions** | Pool cash is filtered per session (✓). **Fixed:** one trigger pass could sell a position twice (O-H1), and a failed stop was never retried (O-H2). **Open:** the paper book is one row per symbol, so an emergency flatten, a W4 exit or a basket sell can sell a pool's shares (O-M1). Auto-pick's `_busy` stops two sessions picking one symbol (✓). There are no locks; with O-H1 fixed, the remaining race is the non-atomic send then state write (O-M8). |

## 1. Ingestion (`data/`)

| Module | Purpose | In → out | Risk | Notes |
|---|---|---|---|---|
| bhavcopy.py | NSE CM EOD, delivery, FII/DII, index closes, bulk deals | NSE archives → prices_daily, fii_dii_market, index_levels, bulk_deals | Med | I-M3 ✅; I-M4 ⏸ |
| dhan.py | Daily / intraday bars, quotes, index history | Dhan API → prices_daily, intraday_bars, live_quotes | Med | `source` never updated (I-M6 ⏸); protected by the brief, unchanged |
| dhan_ws.py, stock_feed.py, ticks.py | WebSocket feeds (stock feed OFF) | WS → index_levels, live_quotes, live_feed_status | Low | a missing change is stored as 0 (Low) |
| index_constituents.py | Nifty 500 list, cache, fallback | NSE CSV → cache | Low | silent fallback to ~50 symbols (Low) |
| market_series.py, markets.py | Nifty 50 benchmark, GIFT, global markets | index_levels, prices_daily, yfinance | Low | `INSERT OR REPLACE` into index_levels (Low) |
| corporate_actions.py | Split / bonus basis reconciliation | NSE calendar → corporate_actions, prices_daily | Med | untracked names are never reconciled (I-M5 ⏸) |
| technical.py, technical_ext.py | Indicators, tech_score | prices_daily → technical_indicators | Med | I-M2 ✅; no look-ahead; `compute_beta` lacks a date bound (display only, Low) |
| ema_gap.py | Fills short histories from the NSE archive | NSE archive → prices_daily | **High** | I-H1 ✅ |
| history_backfill.py | 7-year Dhan walk-back | Dhan → prices_daily | Low | fails every day (DH-904/905); one alert each morning |
| quality.py, news*.py, institutional.py, fundamentals.py, depth*.py | Checks, news sentiment, ownership, legacy fundamentals, order book | … | Low | news is rule-classified only, so 60 % of articles score 0 sentiment |

| ID | Finding | Status |
|---|---|---|
| **I-H1** | `ema_gap.fill_session` inserted NSE's RAW archive price into history already put on the post-split basis. TDPOWERSYS got 165 rows at twice the price (−51 % / +104 % / −49 %), with an SMA-200 of 972 against a price of ~720. | ✅ Rows are put on the basis of every reconciled later event at insert (`_later`). One-off repair `python -m data.ema_gap --repair --apply` is exact (only rows inserted after `reconciled_at`) and idempotent (repaired rows become `bhavcopy_archive_adj`). Tested on a copy: 165 rows, a second run finds 0, and no jump > 25 % is left. Run on the live DB at deploy, after the backup. |
| **I-M2** | No 200-day average was stored as `above_200dma = 0` ("below"): 2,805 of 3,329 rows on 3 Nov, 9 of them tracked. | ✅ Stored NULL. `compute_tech_score` adds Trend only when it is known. |
| **I-M3** | The FII/DII write used `INSERT OR REPLACE`, which deleted the F&O PCR stored in the same row (every row since 23 Oct has `pcr` NULL). | ✅ Upsert of the six flow columns only. |
| I-M4 | The bhavcopy upsert overwrites close and volume and keeps OHL. It is latent (0 rows today), and changing it could stop the EOD file from correcting a provisional Dhan bar. | ⏸ Needs a decision on which source wins; recorded. |
| I-M5 | 233 pending corporate actions, all on untracked symbols, are never reconciled, so the screener sees unadjusted splits (JSWDULUX ×0.1 …). | ⏸ Not in the scored universe. A larger change to `corporate_actions.py:284`. |
| I-M6 | `prices_daily.source` records the first writer only, so source-based diagnostics are wrong. | ⏸ Depends on I-M4. |

## 2. Orders and execution (`orders/`, `execution/`, `brokers/`, `portfolio/`, `accounting/`)

| Module | Purpose | Risk | Notes |
|---|---|---|---|
| orders/rules.py | Trigger engine, OCO brackets, trailing stop, confirm / execute | **High** | O-H1, O-H2, O-M4 ✅ |
| orders/sessions.py | S1–S10 pools, `gate()`, ledger | Med | per-session cash ✓; the trailing high-water mark persists ✓ |
| orders/autopick.py | Picks the next stock for a free session | Med | O-M2, O-M3 ✅ |
| orders/session_board.py | The read-only ten-row board | Low | rank and counts added (Task 1) |
| orders/broker.py | The one path for every order: halt, LIVE switch, funds, risk | Med | O-M7 ✅ |
| orders/paper.py, risk.py, basket.py, sip.py, environment.py | Paper book, limits, baskets, SIPs, environment | Low–Med | one shared paper book (O-M1) |
| execution/* (W4 OMS) | Intents → risk → paper orders; the LIVE Dhan adapter refuses everything | Med (latent) | `auto_execute_paper` is off |
| portfolio/*, accounting/* | P&L, emergency flatten, cycles, ledger, tax | Low–Med | four P&L calculators that can disagree on charges (Low) |

| ID | Finding | Status |
|---|---|---|
| **O-H1** | `check_triggers` reads the ACTIVE rules once, and `execute_rule` never re-checked a rule's status. A STOP that fires cancels its TARGET (`settle_oco_group`), but the stale TARGET in the same pass was sent too: a second SELL of the whole position. In LIVE, exit legs send without a tap. | ✅ The loop re-reads each triggered rule. `execute_rule` sends only for an ACTIVE or PENDING rule (else `SKIPPED`). Tested. |
| **O-H2** | A failed exit leg (halt, no quote, risk check) went to FAILED for good, because only ACTIVE rules are scanned. The position was left with no stop, and its session waited forever. | ✅ A STOP / TARGET stays ACTIVE with `execution_error = "retrying: …"`, with one Telegram message. It is FAILED only when the shares are gone. While the kill switch is on, automatic sends are held (`HELD_HALTED`), with no order_log row every 30 s, and they fire after the halt lifts. Tested. |
| O-M1 | One paper position per symbol: an emergency flatten, a W4 exit or a basket sell can sell a pool's shares. The pool's stop then finds 0 shares. | ⏸ O-H2's "shares gone" rule now ends that stop cleanly. Attributing the paper book per session is a larger change. |
| **O-M2** | An entry that FAILED (risk limit, sizing) was re-picked every 30 s: about 750 rules and Telegram failures a day. | ✅ The session waits for the next trading day, and the board says why. Tested. |
| **O-M3** | Auto-pick compared raw cash with `min_buy_value`; `gate()` uses the floor rule's floor and investable cash. A STEP-floor session (S2) was picked every day and then held all day. | ✅ The same floor and free cash as `gate()`. |
| **O-M4** | `confirm_rule` did not check the 30-minute window outside market hours, so a 15:20 confirmation could be tapped at 20:00 and sent as a MARKET order. | ✅ Expires first, then refuses. Tested. |
| **O-M5** | `POST /api/orders` accepted `role: STOP` with no parent. Exit legs skip the LIVE tap. | ✅ The route refuses any non-ENTRY role (400). Bracket legs are built internally only. Tested. |
| O-M6 | The stale-quote guard never fires: quotes are stamped with the fetch time (`data/dhan.py:562`). | ⏸ The fix is in `data/dhan.py`, which the brief protects. Recorded for an owner-approved change. |
| **O-M7** | A MARKET BUY with no quote was valued at ₹0, so it passed the funds, `max_order_value` and exposure checks. | ✅ It is valued at the reference price (stored quote, else last close). A LIVE buy that still has no value is refused. PAPER is unchanged; the paper broker prices from its own feed. Tested. |
| O-M8 | Place → EXECUTED → build legs is not idempotent. A crash between the steps could double-buy, or leave an entry without legs, and nothing rebuilds legs at start-up. | ⏸ O-H1's status guard closes the in-process half. A start-up reconcile needs its own change and test plan. |

Checked and fine: −2 % stop from the actual fill; trailing 1 % from +3 % (activate = fill × 1.03, high-water mark
persisted); 15-day TARGET; PAPER-only auto-send (two guards); viewer refusal in `get_execution_client`, `PaperBroker` and the
POST middleware. Owner check: **S1 has `target_pct` 4.0 where S2–S10 have 3.0** (an edit on 3 Nov, not code).

## 3. Pipeline, ops, db, alerts (`pipeline/`, `ops/`, `db/`, `alerts/`, `utils/`, `main.py`, `enterprise/`)

| Module | Purpose | Risk | Notes |
|---|---|---|---|
| pipeline/scheduler.py | About 120 slots, `run_job`, catch-ups, the post-market chain | Med | P-M3, P-M5, P-M6 ✅; P-M2 ⏸ |
| pipeline/operating_schedule.py | The 21:00–06:00 IST stop, the resume, replay of deferred jobs | Med | P-M2 ⏸ |
| pipeline/health.py, recover.py, premarket_report.py, w39_jobs.py | Job health, recovery, the 8:30 report, SIP / total return | Low–Med | SKIPPED counts as a working run (Low) |
| utils/trading_calendar.py | NSE holidays and sessions | **High** | P-H1 ✅ (alarm) / ⏸ (the list itself) |
| ops/role.py, jobs.py, dhan_token.py, backup.py, vault.py, viewer_sync.py | Viewer guard, locks, token, backups | Low (Med on MySQL) | P-M8 ⏸ |
| db/schema.py, mysql.py, purge.py | Connections (WAL, 60 s busy), migrations, MySQL adapter | Low | no connection is shared across threads ✓ |
| alerts/telegram.py | notify / dedupe / viewer suppression | Med | P-M3, P-M4 ✅ |
| enterprise/session_reports.py, privacy.py, auth | Reports, retention, auth | Low | schedules after 20:45 never fire (Low) |

| ID | Finding | Status |
|---|---|---|
| **P-H1** | `NSE_HOLIDAYS` ends at 2026-12-25, so 26 Jan 2027 would be a "trading day": polling, paper matching, SIP orders, and "missed post-market" alarms on a closed market. | ✅ `calendar_gap()` and a critical alert at scheduler start ("no NSE holidays listed for 2027"), raised from December. ⏸ **The 2027 dates are not invented.** NSE publishes its circular in December, and the owner adds it. |
| P-M2 | Evening jobs (total_return 21:10, risk_model 21:45, history_backfill 22:20 …) fall inside the 21:00 stop and run only on the 06:00 replay. The replay is keyed to the wrong window on a late resume and is lost on a restart. | ⏸ Moving slots changes `pipeline/scheduler.py`'s schedule and exclusions, which the brief protects. Recorded with the reviewer's fix. |
| **P-M3** | Post-market alerts shared one try block, and a NULL MH / TOD score crashed the format (`unsupported format string … NoneType`, 5 times in Oct), losing every alert after it. `cmp or 0` planned a trade off ₹0. | ✅ Each alert runs on its own. None shows as "—". No trade plan without a close. Tested. |
| **P-M4** | VIX Spike / Broad Selloff had no dedupe key and were re-sent every 15 min (24 in one day on 28 Sep). | ✅ One key per kind per day. |
| **P-M5** | No whole-run lock on `run_postmarket`. The 16:45 slot, a catch-up and `POST /api/refresh` could overlap and run the unlocked tail (alerts, held signals) twice. | ✅ `job_lock("postmarket_pipeline")`; a second run returns SKIPPED. |
| **P-M6** | A wake-up fires every overdue `.at()` slot at once: about 22 FII/DII watches back to back (3 NSE calls each), and 34 health checks. | ✅ Both are debounced (60 s). |
| **P-M7** | Scoring held SQLite's write lock for the whole ~50 s loop. In a market-hours catch-up, the order monitor's stops waited for it, then hit "database is locked". | ✅ Rows are computed first and written in one short transaction, and the MH / formula rows are committed before the loop. A half-scored day is never visible. |
| P-M8 | On MySQL, `BEGIN IMMEDIATE` fails and the job lock fails open: no duplicate protection. | ⏸ MySQL is not enabled. |

## 4. Dashboard (`dashboard/` server and front end)

| Module | Purpose | Risk | Notes |
|---|---|---|---|
| server.py | "/" scores page, the core `/api/*`, order routes, `/api/live-quotes`, the order monitor | High → Low | D-H1, D-M1, D-M2, D-M3, D-M4 ✅ |
| command_page.py | Command Center | High → Low | D-H1 ✅ |
| workspace.py / workspace.js | Watchlist, the W2 order ticket, `/stock/…` | High → Low | D-H2 ✅, D-H1 (ticket) ✅ |
| command_more.py | Signal explorer, Trade Setup preview | Med → Low | D-M5 ✅ |
| w39_retail_page.py | Baskets, SIP | Med → Low | D-M7 ✅ |
| ux_pages.py, shell.py, atip.js/css, ux_kit.py, fmt.py, score_glossary.py | The redesign's pages, shell, design system | Med | Task 4 changes; D-M6 ⏸ |
| stock_view.py, execution_page.py, w39_page.py, w28_assets.py, global_asia.py | Stock panel, execution lab, research | Low | w28 crash-risk cells all red (Low) |

| ID | Finding | Status |
|---|---|---|
| **D-H1** | Three Confirm buttons sent the order in one click, with no dialog. The pending line showed "BUY 100 SBIN", where 100 is 100 % of the pool's cash, not shares. | ✅ `AX.confirmOrder` (one dialog: symbol, side, quantity, price, value, stop-loss, target, PAPER/LIVE, red for LIVE) before **every** send: "/" Confirm, the order-book Confirm, Command Center, the order ticket's Save. `/api/orders/pending` adds `est_qty` / `est_value`. Esc or Cancel is no. If the dialog cannot load, nothing is sent. |
| **D-H2** | "Buy only on a BUY signal" (owner, 3 Nov) was bypassed by the W2 ticket, the B key and the watchlist B button. | ✅ The ticket preview has a blocking check "Buy only on a BUY signal", so Save is disabled on HOLD / WAIT / SELL. Pool sessions are exempt by design (rank-fill). ⏸ The server-side lock stays the owner's call, as recorded on 3 Nov. |
| **D-M1** | News headlines went unescaped into "/", the page that carries the order token. | ✅ Escaped (after unescaping the feed's `&amp;`), and so are `execution_error` texts. |
| **D-M2** | The Global panel was capped at the scored date, so the day before's markets showed under a current-looking label. | ✅ The newest written row. |
| **D-M3** | The Portfolio tab needed holdings on the exact scored date. 29 Oct had none, so it said "No portfolio data". | ✅ The latest snapshot at or before that date. |
| **D-M4** | A NaN price made `/api/live-quotes` a 500 and froze every CMP cell while the label said Live. | ✅ Only finite prices are sent. |
| **D-M5** | The preview looked up the signal with `LIKE %SYM% LIMIT 5`, which missed "LT" (19 matches). | ✅ A page big enough for every match. |
| D-M6 | "/" and Home compute freshness with different cut-offs (16:00 vs 18:30). | ⏸ Cosmetic window (16:00–16:50); needs one shared helper. |
| **D-M7** | Basket placement used a native `confirm()` without legs or values; "Run due SIPs" had no confirmation. | ✅ The shell's dialog with every leg, the environment and the net amount; SIPs ask too. |

Checked and fine: viewer mode refuses every write; LIVE entries always need the tap; SQL is parameterised; CRI is
inverted correctly everywhere it is coloured; score dates use `MAX(date)`.

## 5. Scores, quant, ML, backtest, strategy, research, wealth, tools

`scores/engine.py`, `formulas.py` and `validation.py` are audited in [SCORE_AUDIT_REPORT.md](SCORE_AUDIT_REPORT.md).

| Module | Purpose | Risk | Notes |
|---|---|---|---|
| scores/accuracy.py, predictions.py, signal_log.py | Grading, trade plans, signal outcomes | Med | Q-M2, Q-M3, Q-M4 ⏸ |
| scores/backtest.py, strategy/backtest_history.py | CLI replays, trail calibration | Med-High | Q-H5 ⏸ |
| backtest/* (engine, walk-forward, CPCV, DSR/PBO) | Portfolio / event engines | Low | next-open fills, PIT guard, survivorship flagged ✓ |
| strategy/aggressive.py, live.py | Live exit management | Low | CRI inverted correctly ✓ |
| strategy_engine/* | 17 strategies, all DRAFT | High (latent) | Q-H1 ✅; Q-M5 ⏸ |
| quant/* | Factors, approval, events | High | Q-H2, Q-M9 ✅; Q-H4 ⏸ |
| ml/* (all tables empty) | Validation, selection, regime | High (latent) | Q-H3 ✅; Q-M8, Q-M10 ⏸ |
| research/*, wealth/*, altdata/*, tools/* | Exit study, reports, wealth, alt data, ops scripts | Low–Med | Q-M6, Q-M7 ⏸ |

| ID | Finding | Status |
|---|---|---|
| **Q-H1** | `max_hold_exit`: `held_sessions` is None, and `None >= mh` raised. Every decision run would fail once a strategy with a max hold went to PAPER. | ✅ Tested. |
| **Q-H2** | The factor approval gate applied the direction twice: the IC is measured on the already direction-adjusted score. A working "lower is better" factor failed and an inverted one passed. | ✅ The expected sign is always positive. |
| **Q-H3** | `ml/validation`: NEUTRAL labels were missing from ORDER, and rank ties were broken by row order, so a drifting score showed IC ≈ 0.26 against a true 0. The same tie flaw was in `quant/research`. | ✅ Tie-averaged ranks in both, plus NEUTRAL = 0. Tested. |
| Q-H4 | Disclosures filed after 15:30 are treated as known at that day's close, which inflates event and factor studies measured from close(d). Harmless for next-open fills. | ⏸ One knowledge-date helper across `quant/factors.py`, `events.py`, `deal_signal.py`. Research-only today. |
| Q-H5 | `scores/backtest.py` / `strategy/backtest_history.py` walk bars by position across the 5-week bhavcopy hole (30 Jul – 4 Sep), so a "5-day hold" spans 6 weeks. `trail_atr_mult = 4.0` was calibrated through it. | ⏸ A calendar-span guard in three places, then re-run the calibration. Recorded as the first follow-up. |
| Q-M2 | `accuracy.py` counts HOLD / WAIT as correct when \|ret\| < 2 %, so ACS's HistoricalAccuracy (weight 0.25) measures how quiet a stock is. | ⏸ It changes ACS for every stock; see the score audit. |
| Q-M3 / Q-M4 | Two hit-rate denominators disagree (72 % vs 81 % at +3 %); outcomes are measured from the signal day's close, not the next open. | ⏸ The dashboard's signal-log figures are optimistic; the validation harness here uses the next open. |
| Q-M5 | Each strategy sees the whole paper book, so its exits can sell another's positions (all DRAFT). | ⏸ |
| **Q-M9** | `quant/engine` and `composite` took percentiles of the raw values, so normalisation, sector-relative and neutralise had no effect. | ✅ |
| Q-M6, M7, M8, M10, M11 | Exit study drops crashes; open trades counted in win rates; in-sample regime predictions; feature selection inside the test windows; survivorship not printed everywhere. | ⏸ Research tooling, Low impact on trading. |

## 6. Dead code and duplicated logic (not removed: the brief asks for delta patches)

* About 7 money formatters (`fmt`, `ux_kit`, `server.money`, `AX.inr`, `AX.fmt.inr`, inline `inrf`, Command Center `inr`) and
  about 25 copies of `esc`. The new code uses `AX.inr` / `ux_kit.money` only.
* Four P&L calculators (pool ledger, `accounting/cycles`, `paper_report`, `paper_position.realized_pnl`). PNBHOUSING reads −₹520.96
  in the paper book and −₹589.74 in the pool, after charges.
* Four hit-rate harnesses with different entry and denominator rules. The validation harness's next-open rule is the reference.
* Dead: `PREV_CLOSE_SQL`, the old order modal (~300 lines, reachable only through "Advanced"), `environment.py if False:`,
  `accounting/cycles.whole_shares`, `db/mysql.Pool`, `dhan.py:1298-1300`, the legacy Claude news classifier,
  `prices_daily.adj_close` (NULL in every row).
