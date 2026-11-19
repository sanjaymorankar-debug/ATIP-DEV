# Part 5 — Code review findings (19 Nov 2026, git c03fc29 = GitHub master; the VPS runs e468d90)

Scope: order path (orders/*, the order monitor in dashboard/server.py), data and scoring (data/dhan*.py, data/bhavcopy*.py,
data/technical.py, scores/*), scheduler/ops/PostgreSQL adapter (pipeline/*, ops/*, db/postgres.py). Read by three
independent reviewers, then each item below re-checked against the code before it was listed. "Fixed" = a delta patch on
branch `claude/loss-rca-2026-11-19` with a regression test in `tests/test_loss_rca_2026_11_19.py`; commit e5903e1, live on the VPS since 00:46:11 IST
20 Nov (strategy v2 OFF).
"Flag" = behind `strategy_v2` in config.json (OFF unless turned on; ON on Test only).

Verified NOT broken (so no one re-checks them): tick rounding direction (orders/ticks.py), the trailing stop only moves up,
stop/target legs are measured from the fill price (not the signal close), a gap-through stop fills at the polled LTP (not
at the stop price), SELL direction, OCO double-fire inside one pass (get_rule re-check), NULL EMA/RSI storage (NaN →
NULL, no row under 20 bars), "latest" ai_scores (global MAX(date) then a.date = ?), WebSocket news/sentiment overwrite
(INSERT OR IGNORE; rule news carries classifier='rule'; ACS reads 'claude' only), Bhavcopy target date and coverage
guards, Nifty 50 via Dhan IDX_I. No look-ahead found in indicators or scores: every read is bounded `date <= td`.

| ID | Sev | Where | What goes wrong | P&L impact | Status |
|---|---|---|---|---|---|
| LRCA-01 | High | orders/rules.py:893-985 (check_triggers loop) | One rule that raises (sizing, funds check, ledger, DB error) aborted the whole pass. Rules are newest-first, so a new failing entry stood in front of every older STOP leg, every 30 s. | Stops never fire → losses run past −2 %/−5 % | **Fixed**: per-rule try/except (RULE_ERROR event, rollback, next rule); exit legs tested before entries; a DB lock is still raised after the other rules ran (RCA-13 retry kept) |
| LRCA-02 | High | dashboard/server.py:2296-2302 | Exits were checked only after auto-pick, in the same thread with no time budget; a Kite-retest pick makes 4 broker reads + a quote per setup, so at 09:15 the first stop check waited behind all picks (minutes with 60 s Dhan timeouts). | Late stops at the open (gap-down days) | **Fixed**: check_triggers runs before auto-pick; a queued pick fires on the next pass (≤30 s) |
| LRCA-03 | High | dashboard/server.py:2306, 2343 | Every monitor failure was a log line only: no heartbeat, no pipeline_log row, no alert. | Positions unprotected all day, unseen | **Fixed**: `order_monitor` heartbeat each clean pass; one error alert after 4 failing passes (~2 min), one on recovery |
| LRCA-04 | High | db/postgres.py:720 | PostgreSQL waits on a row lock forever (SQLite gave up after 60 s with "database is locked"); one connection left in a transaction could hang the single monitor thread. | No exits for the rest of the session | **Fixed**: `SET lock_timeout = 60 s`; SQLSTATE 55P03 worded "database is locked" (live-PG test proves a blocked UPDATE now ends) |
| LRCA-05 | Medium | orders/rules.py:872 | A missing LTP becomes NaN in pandas; NaN is truthy and `NaN <= stop` is False, so that stop was silently never tested and the blind-feed alert counted it as priced. | A stop skipped while the feed has holes | **Fixed**: only finite, positive LTPs are prices |
| LRCA-06 | Medium | scores/breadth.py:114,132 | The regime gate used the newest stored breadth at any age; a stopped 21:20/07:30 job froze it open (or shut). | Entries in a weak market / none in a strong one | **Flag** (freshness_gate): stale breadth closes the gate |
| LRCA-07 | Medium | orders/autopick.py:59,151 | Scores up to 5 calendar days old were traded; a stalled pipeline meant several sessions bought on one day's signals. | Stale-signal entries | **Flag** (freshness_gate): only the previous session's scores |
| LRCA-08 | High | orders/algo_gtt.py:396-399; orders/sessions.py:975 | A Kite-retest resting buy was placed even when the LTP was already under its planned stop (fills at once into a broken setup), and fired days later without re-checking the breadth gate. | S2/S3/S4/S10 entries into broken setups / closed regime | **Flag** (retest_guard): refused at placement when LTP ≤ stop; held at fire time while the gate is closed |
| LRCA-09 | — | orders/autopick.py (new) | No market filter on the open (Part 6). | Entries into gap-down opens | **Flag** (nifty_gap_min_pct, default −0.5 %) |
| LRCA-10 | Medium | db/postgres.py; ops/preopen_health.py:312 | PgConnection had no `in_transaction`, so the pre-open gate committed its caller's (auto-pick's) open transaction part-way. | Partial writes in auto-pick on PG | **Fixed**: `in_transaction` property |
| LRCA-11 | Medium | pipeline/health.py:126 | A run SKIPPED because another runner holds the lock counted as healthy (16 Nov CAP-02: every quote/index run 10:30-15:30 was "locked" and nothing alerted). | Stale quotes fed exits, auto-pick, the gate | **Fixed**: locked skips are not working runs (STALLED/FAILING fire) |
| LRCA-12 | High | dashboard/server.py:2338; orders/risk.py:145 | A manual STOP in market hours pauses the whole monitor and sets the kill switch the broker checks on every order, exits included: open positions have no stop until resume. | Unprotected positions after a STOP | **Awaiting owner decision** (Task 2 design: "nothing is triggered while stopped"); proposal: a STOP holds entries only |
| LRCA-13 | High | data/dhan_ws.py:494; data/bhavcopy.py:301 | The index feed stored an unknown change as 0.00 % (after a reconnect), read as a flat day by Market Health / the BEAR→WAIT rule / MSI Sector; its rows also kept NSE's official close snapshot out. | BUYs allowed on down days | **Fixed**: NULL, not 0; only rows with a change cover the close |
| LRCA-14 | Medium | scores/engine.py:857 | An unknown market health (coverage < 50 %) read as a neutral 50 and passed both BUY gates. | BUYs on blind days | **Flag** (freshness_gate): no BUY while MH is unknown |
| LRCA-15 | Medium | dashboard/server.py:1729 | `POST /api/refresh` during market hours re-ran post-market for the previous session: today's intraday prices stamped on yesterday, ai_scores re-scored under auto-pick, paper next-open fills re-run. | Corrupted signals mid-session | **Fixed**: refused 09:00-16:00 IST unless `?force=true` |
| DS-01 | Low (latent; was High) | data/dhan.py:1161-1168; data/bhavcopy.py:537; pipeline/scheduler.py:956 | Two price sources overwrite each other every evening: NSE's close is written at 16:45, then Dhan rewrites D-1..D-5 OHLC (rows keep source='bhavcopy'); the next evening D flips to Dhan's value. The old "sawtooth" in a new form. | **Measured: none** — stored OHLC equals NSE's official file for all 499 scored stocks on 12, 13 and 16 Nov (0 of 1,497 differ > 0.05 %; evidence/ds01_nse_vs_stored.json) | **Recommended** (latent): make NSE canonical (Dhan never overwrites a bhavcopy row; the NSE upsert sets OHLC + source) — changes scores, needs owner yes |
| DS-03 | Medium | scores/engine.py:769-788 | MSI reads FII/global of any age and turns a missing index row into 0 (bearish). | ~10.5 % of ATIP shifts for the whole universe | Recommended |
| DS-06 | Low (measured) | data/technical.py:446 | Technicals run for every symbol in prices_daily (~3,000 Bhavcopy symbols) instead of the tracked universe. | Measured: 3,383 symbols in 93 s on 19 Nov (16:48:17-16:49:50 IST): wasted work, no delay | Recommended |
| DS-07 | Medium | scores/engine.py:1021 | A symbol whose evening re-score fails keeps its earlier BUY row, which auto-pick then buys. | Stale BUY | Recommended |
| DS-08 | Medium (hyp.) | data/dhan.py:1334 | The REST quote refresh covers 50 hard-coded stocks; positions outside it rely on the stock feed. | Paper LIMIT/SL never match | Recommended (check VPS config) |
| OP-05 | Medium | orders/paper.py:457 → rules.py:1203 | Confirm path: a paper LIMIT that rests PENDING is booked as EXECUTED with brackets and ledger. | Phantom P&L (non-AUTO sessions only) | Recommended |
| OP-07 | Medium | orders/rules.py:1068,1108 | Stops tested on the polled LTP every ≥30 s: intrabar wicks missed (optimistic), fast falls filled at LTP − 15 bps (pessimistic); the 15:15 time exit pays stop slippage. | Paper P&L not exchange-realistic | Recommended |
| OP-08 | Low | data/dhan.py:602 | The quote "timestamp" is ATIP's fetch time, not Dhan's last-trade time, so the 180 s stale-quote guard never trips. | Untraded LTP treated as fresh | Recommended (store LTT beside it; do NOT block exits on it) |
| SCH-6/OP-11 | Low (latent) | pipeline/scheduler.py:356; all `.at()` | Market hours, cut-offs and every schedule use the OS local clock; correct only because the VPS runs TZ=Asia/Kolkata (checked 18 Nov). | Wrong-hour trading if TZ changes | Recommended: refuse to start unless the clock is IST |
| SCH-7 | Medium | scheduler.py:1334; w39_jobs.py | The 21:00 stop window cuts the FII/DII watch (21:00-21:30 slots) and the c03fc29 21:30 DQ deadline; deferred jobs live only in memory (a night restart loses them). | Held signals settle at 06:00 | Recommended |
| SCH-8 | Medium | ops/jobs.py:111,158 | Job locks and the leader lease fail OPEN on any error (double scoring/execution possible). | Duplicate runs | Recommended: fail closed for writers |
| SCH-9 | Medium (hyp.) | dashboard/server.py:2360 | The order monitor ignores the leader lease; a second checkout on the same DATABASE_URL would double auto-pick and exits. | Duplicate orders | Recommended |
| SCH-12 | Low | db/postgres.py translate() | `ORDER BY … DESC` puts NULLs first on PostgreSQL (last on SQLite); 2-arg GROUP_CONCAT invalid; MAX(a,b) → GREATEST ignores NULL. Rank-fill already excludes NULL scores (autopick.py:126). | None seen | Recommended |
| OP-10 | Low (latent) | orders/rules.py:447 | A partially filled stop cancels the target and leaves the rest unprotected (paper never partial-fills; LIVE can). | LIVE only | Recommended before LIVE |
| DS-09/10/11/12/14/15 | Low | see reviewers' notes | chg_pct 0.0 fallback; pivots from D-1; EOD coverage 50 %; 50-stock fallback universe; VWAP bars; signal_log measured from the signal close. | Small | Recommended |

## Addendum 20 Nov 2026 (IST) — independent review of the deployed fixes (e5903e1)

Owner, about 00:58 IST: "recheck all code and fixes … not any defect exist". A separate reviewer was given only the diff
c03fc29..e5903e1 and its intent, not these conclusions. Every item was re-checked here; LRCA-16 was reproduced on the
live code. All are fixed on the branch after 514b439, each with a test in `tests/test_review_fixes_2026_11_20.py` that
fails on e5903e1. **Live on the VPS** since 01:36:10 IST 20 Nov (6691471, owner's yes); the VPS = GitHub master
c9d168f since 01:46:11 IST (pid 850092).

| ID | Sev | Where | What goes wrong | Live effect (flag OFF) | Fix |
|---|---|---|---|---|---|
| LRCA-16 | High | dashboard/server.py `_order_monitor_pass` | LRCA-02 put `check_triggers` before auto-pick, whose `cancel_stale` is the only cleanup of an auto-pick LIMIT left ACTIVE from an earlier day: on a gap-down open the leftover fills before it is cancelled (probe: e5903e1 EXECUTED 97.0 × 257, c03fc29 CANCELLED). The same holds for a Kite dummy whose close broke its stop (`algo_gtt.maintain`, after the check in both orders). | Yes, but only if a leftover pick exists: none in the 19:15 IST snapshot (only S10 FACT, a Kite retest resting by design, close 775.40 above its buy 755.25 and stop 717.45) | `cancel_stale` + `algo_gtt.maintain` run first in each pass (DB-only; they run again later and find nothing) |
| LRCA-17 | Medium | orders/entry_guards.py:152 | Nifty-weak guard formats `n['chg']` with `:+.2f`; with LRCA-13 the change can be NULL → TypeError out of `check_entry` → every auto-pick pass fails | No (guards OFF) | NULL-safe message |
| LRCA-18 | Medium | dashboard/server.py `_monitor_health` | `notify` drops a repeated key for the rest of the day, so a second outage the same day was silent; the state was never reset at a new day | Monitoring only | Key per outage start; reset at a new day |
| LRCA-19 | Medium/Low | orders/rules.py `_watch_blind` | A NaN LTP counted as priced, so the 120 s exit-blind alert never started (LRCA-05 fixed only the trigger path) | Alert only | `_usable_price` |
| LRCA-20 | Low | data/markets.py:85; pipeline/scheduler.py:1570 | The 15-min REST index snapshot still stored an unknown change as 0 % (and counted it in the sentiment); `main.py --status` crashed on NULL | Small | NULL; prints n/a |
| — | Low (not fixed) | scores/engine.py:97 `get_idx` | Reads the day's latest index row (15:44 feed row) instead of NSE's 15:30 close snapshot: MSI "Sector" can score 0 where official closes give 100. Same before the change. | Score change → needs owner yes | Recommended |
| — | Low (not fixed) | dashboard/server.py pass order | An exit that pushes a session under the session-engine floor can let auto-pick queue one pick before the next pass pauses it; `sessions.gate` holds it at fire time | None seen | Recommended |

Checked and fine by the reviewer: all six strategy_v2 hooks short-circuit with the flag OFF; per-rule rollback cannot
undo an earlier rule's work; the lock re-raise; no OCO double-fire with exits first; `in_transaction`; 55P03; no job
waits > 60 s on a PostgreSQL lock; LRCA-11's "locked" prefix; every other reader of NULL index changes; Kite tick
rounding, stop/target from fill, breadth gate and time exits.

Also found while re-running the suite (tests only, no code change): `test_kite_retest_study::test_breadth_agrees_with_scores_breadth`
built `prices_daily` without the `id` column the real table has (the breadth query orders by it since a439e41, 17 Nov);
with it the test passes, i.e. the Kite research breadth equals the live formula. Two option-chain tests in
`test_dhan_realtime_feeds.py` used fake expiries ending 17 Nov without pinning the date (failing since 18 Nov); pinned
to 3 Nov like their sister test (VF-11).
