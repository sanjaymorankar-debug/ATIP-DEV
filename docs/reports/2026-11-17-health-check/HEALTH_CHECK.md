# ATIP health check — Tue 17 Nov 2026, before the session (PAPER)

Checked 03:46–04:16 IST on the Mac. Live ATIP: pid 48592 (launchd `com.atip.platform`, up since 19:21:39 IST 16 Nov),
code = master `6ed65da` (same as GitHub). All times IST. Nothing on the live system was changed by this check: every
database read was read-only, and the Dhan calls were the read-only profile and RELIANCE quote probes the 08:45 gate
itself makes (not stored).

**Verdict: READY for today's session.** No blocker on the live system. The one fault found was in the test suite, not
the app: since the capital plan block went into the live `atip_data/config.json` last night, 101 tests read it and
failed. Fixed in `tests/conftest.py` (one isolating line, test-only, uncommitted; section 5). The watch items below are
known and none of them stops ATIP from trading today in PAPER.

## Addendum 2, 18 Nov 00:20 IST: the token recovered on its own; the Mac copy was retired the same evening

* **Token:** the 08:00 automatic retry on 17 Nov worked (`OK: new Dhan token saved via generateAccessToken`, valid to
  18 Nov 08:00), and the 08:45 pre-open gate passed. "Invalid TOTP" at 06:30 has happened before (6, 7, 11, 12 Oct)
  and cleared on a later try, so the guess below that TOTP was reset on the account was wrong. No owner action was
  needed; the owner's `vault-set` did not store anything (vault unchanged since 08:00).
* **The Mac stopped trading at 11:58 IST 17 Nov.** Dhan quotes came back empty from 11:55:31; the exit-monitor alert
  (CAP-05) went to Telegram at 11:57:32; the log ends at 11:58:43 with no shutdown line. At 20:59 IST both LaunchAgents
  (`com.atip.platform`, `com.atip.dhan-token-refresh`) were moved to `~/Library/LaunchAgents-retired-atip/` by the
  session "Migrate bkesari.com projects to VPS", which moved the live ATIP to a VPS on PostgreSQL (commits `a439e41`
  20:14 IST and `6861e3b` 21:52 IST, "verified on the live VPS"). The Mac's database has no 17 Nov close, scores or
  post-market. ATIP on the Mac was deliberately not restarted: two copies would both trade and renew the Dhan token.
* **Not checked:** the VPS itself (no access from this session). Its readiness for 18 Nov needs a check there.

## Addendum 07:05 IST: the 06:30 Dhan token renewal failed — BLOCKER until the owner renews it

The token expired at 06:30 as expected, and the renewal LaunchAgent ran at 06:30:05, but Dhan refused it:
`RenewToken failed: HTTP 400 Invalid Token`, then `generateAccessToken failed: HTTP 200 Invalid TOTP` for both codes
(06:30:15 and 06:30:42; 2 of the tool's 4 a day used). At 07:00 `dhan_portfolio` FAILED and the pre-market quote jobs
were PARTIAL. Not the cause: the Mac's clock (within 0.01 s of time.apple.com and time.google.com) or the config
(`config.json` differs from the 16 Nov backup only by the capital plan block; the Dhan PIN and TOTP secret live in the
secret store, `atip_data/secrets/vault.json`, unchanged since the successful renewal at 06:30 16 Nov). So the same
secret that worked yesterday is refused today: most likely TOTP was reset or re-enabled on the Dhan account.

Next automatic tries: 08:00 (2 codes; the tool's daily cap of 4 stays under Dhan's 5-wrong-codes lock) and the 08:45
gate. Owner fix (ATIP's own banner): generate a token at web.dhan.co and store it with
`.venv/bin/python -m ops vault-set DHAN_ACCESS_TOKEN` (ATIP reads the vault on each call; no restart); if TOTP was
re-enabled, store the new secret with `.venv/bin/python -m ops vault-set DHAN_TOTP_SECRET` too, so tomorrow's 06:30
renewal works. Until then the 08:45 gate blocks auto-pick and the 8 open positions' stops are not watched.

## 1. Checks

| Area | Check | Result | Status |
|---|---|---|---|
| Process | launchd service, pid, uptime | running, pid 48592, 8 h 25 m, 11 launches total | OK |
| Process | `python -m ops status` (8 components) | database, broker, data, scheduler, ml, storage, market_data, wealth all READY; scheduler heartbeat 17 s | OK |
| Process | log since the 19:21 restart | 0 errors, 2 warnings (start-up catch-up; the 21:00–06:00 stop window) | OK |
| Safety | `python -m ops safety` | execution PAPER, live gate closed, master switch off, `w1_real_orders_possible` false, Dhan adapter refuses | OK |
| Safety | config | `broker_env` PAPER; risk limits ₹1,00,000/order, 50 orders/day, ₹10,000 daily loss, 10 % drawdown | OK |
| Config | `python -m ops validate` (config, secrets, migrations) | no findings | OK |
| Broker | Dhan token | valid, expires 06:30 today; renewal LaunchAgent runs 06:30 / 08:00 / 09:00 (renewed at 06:30 on 14, 15 and 16 Nov); the 08:45 gate retries a renewal | OK |
| Broker | Dhan `/v2/profile` probe | "Dhan accepted the token" | OK |
| Broker | Dhan quote probe | RELIANCE ₹1,344.80, last trade 16 Nov 15:58 | OK |
| Calendar | 17 Nov a market day | yes; previous session 16 Nov; next holidays 24 Nov (plus weekends 21–22, 28–29 Nov) | OK |
| Schedule | 06:00 resume | on time on all 16 days 1–16 Nov | OK |
| Schedule | jobs since 16 Nov 15:30 | 0 FAILED. EMPTY/SKIPPED runs match earlier sessions (delivery, corporate actions, bulk deals, F&O settlement: no data; ML/quant/wealth: switched off) | OK |
| Schedule | job locks | `scheduler_leader` held by pid 48592. The 16 Nov `dhan_15min_bars` row is left over from the outage: its pid (52907) is gone and it expired 16:01 IST, so the first 15-minute bar run today takes it over (`ops/jobs.acquire`) | OK |
| Data | daily prices 16 Nov | 3,376 rows (stocks + 13 index series) | OK |
| Data | AI scores 16 Nov | 499 stocks; re-scored 18:20 IST after NSE's index closes (17:40) and FII/DII (18:20) were in | OK |
| Data | technical indicators 16 Nov | 3,375 rows; technical signals 20:30 (499) | OK |
| Data | 15-minute bars 16 Nov | 499 stocks, 12,258 bars, 09:15–15:15 (refilled 20:44–20:52 IST) | OK |
| Data | FII/DII, market health | 16 Nov stored | OK |
| Data | Bhavcopy history | 19:50 run SUCCESS (384,636 rows) | OK |
| Data | breadth | stored to 13 Nov; the 07:30 `breadth_and_scan` run adds 16 Nov before the open (as on 16 Nov) | OK |
| Data | data quality 16 Nov | DQS 89.85 "ERROR": index_series 13/13 is a timing artifact (the check ran 16:48:19, NSE's index closes arrived 17:40 and are stored now, all 13); non_session_dates = the 8 Nov Muhurat bar (CAP-13) | OK (known) |
| Sessions | S1–S10 | all RUNNING, AUTO; capital ₹45,000 each; loss limit ₹2,700; P&L +₹4,733.70 in total | OK |
| Sessions | paper cash | balance ₹3,27,564.54, sessions' cash ₹2,72,699.30, buffer ₹54,865.24, over-allocated ₹0 | OK |
| Orders | open positions | 8, each with one ACTIVE stop and one ACTIVE target (table 3) | OK |
| Orders | resting buys | S8 KALYANKJIL limit ₹700.45 and S4 CDSL limit ₹1,408.10 (DUMMY GTT, valid to 1 Dec): neither price was reached after they were placed (16 Nov low after 10:26 was ₹701.50 and ₹1,419.00) | OK |
| Orders | stuck orders | paper orders all TRADED (91); OMS none open | OK |
| Alerts | Telegram | last sent 21:00:01 IST 16 Nov ("ATIP stopped"); no open ops alert | OK |
| Dashboard | pages | `/`, `/sessions`, `/accounting`, `/command/pnl`, `/rca`, `/ab`, `/lab` all HTTP 200 in under 0.2 s; no error text on the pages; `/health` READY | OK |
| Database | `PRAGMA quick_check`, foreign keys | ok; 0 foreign-key errors; 822 MB, WAL 0 MB | OK |
| Database | backups | 3 verified tonight (latest 20:49:40, integrity ok); 36.4 GB free | OK |
| Network | Dhan, Telegram, NSE, Yahoo | reachable | OK |
| Network | FRED (macro series) | not answering (as on earlier days: `macro_data` PARTIAL); scores do not depend on it | Watch |
| Mac | power | AC, battery 100 %; system sleep after 1 min idle is held off while the display is on and the Claude app is open | Watch |
| Code | working tree | no changes to tracked files; master = GitHub `6ed65da` | OK |
| Code | live process vs master | the only code changed since the restart is `tools/backfill_intraday_bars.py` (a command-line tool, not loaded by the app) | OK |
| Code | dependencies | `pip check`: no broken requirements | OK |
| Code | full test suite | first run 90 failed + 11 errors: the tests read the live config's new capital plan block (section 5); after the one-line fixture fix 2,809 passed, 4 failed (environmental, section 5), 65 skipped | FIXED |

## 2. Today, on its own

| IST | What runs |
|---|---|
| 06:00 | Resume sequence (backfill, global, news, analysis, report), then the catch-up of the 21:10 / 21:45 jobs (`total_return`, `risk_model`) |
| 06:30 | Dhan token renewal (the token runs out at 06:30); tries again 08:00 and 09:00 if needed |
| 07:00–07:40 | Pre-market news, global markets, Dhan pre-open quotes; 07:30 breadth for 16 Nov; 07:40 news impact |
| 08:00 | Instrument master (scrip master) |
| 08:15 / 08:30 | Pre-market report / morning digest |
| 08:45 | Pre-open gate: token, quote feed, scrip master, scores (retries 08:50, 09:00, 09:10; auto-pick waits while it fails, and Telegram is told) |
| 09:15 | Market open: order monitor every 30 s, auto-pick, quotes every 15 min, 15-minute bars every 30 min |

## 3. Open positions going into 17 Nov

| Session | Stock | Qty | Bought ₹ | Close 16 Nov ₹ | Stop ₹ (vs close) | Target ₹ (vs close) |
|---|---|---|---|---|---|---|
| S1 | PVRINOX | 15 | 1,649.62 | 1,639.40 | 1,616.63 (−1.4 %) | 1,715.60 (+4.6 %) |
| S2 | INDHOTEL | 27 | 885.44 | 886.00 | 867.73 (−2.1 %) | 912.00 (+2.9 %) |
| S3 | AUROPHARMA | 15 | 1,818.91 | 1,794.00 | 1,782.53 (−0.6 %) | 1,873.48 (+4.4 %) |
| S5 | KOTAKBANK | 50 | 488.64 | 494.20 | 478.87 (−3.1 %) | 503.30 (+1.8 %) |
| S6 | GRASIM | 4 | 3,032.20 | 3,084.40 | 2,885.30 (−6.5 %) | 3,179.10 (+3.1 %) |
| S7 | ICICIBANK | 16 | 1,580.39 | 1,585.00 | 1,548.78 (−2.3 %) | 1,627.80 (+2.7 %) |
| S9 | DLF | 26 | 752.35 | 754.75 | 729.75 (−3.3 %) | 797.50 (+5.7 %) |
| S10 | PNB | 170 | 143.98 | 144.59 | 141.10 (−2.4 %) | 148.30 (+2.6 %) |

S4 (CDSL) and S8 (KALYANKJIL) hold no stock: each waits on its resting buy. AUROPHARMA's stop is 0.6 % under
yesterday's close, so a weak open can sell it in the first minutes.

## 4. Watch items (none blocks today)

1. **Dhan outage cover (CAP-05, open).** If Dhan's quote API stops answering again (16 Nov 09:47–10:16), open stops
   are not watched until it returns; since 19:21 IST 16 Nov an error alert fires after 2 minutes without prices.
   The fallback price source still needs a market-hours test.
2. **Mac sleep.** System sleep is set to 1 minute; it is held off only while the display is on or the Claude app
   keeps the Mac awake. 06:00 resumed on time on every day this month, but a Mac asleep at 06:30 or 08:45 would delay
   the token renewal and the pre-open gate until it wakes. Keep the Mac awake and on power through 15:30.
3. **Token timing.** The token expires at 06:30, the same minute the renewal runs. It has worked every day; if it
   fails, 08:00, 09:00 and the 08:45 gate try again and Telegram says so.
4. **FRED** macro series time out (external). Macro tiles may show older values; nothing trades on them.
5. **Data-quality alert at 16:48 is premature** on most days (index closes arrive ~17:40). The 18:20 re-score uses
   them, so scores are right; the 16:48 DQS alert is noise. Related to CAP-07.
6. **Housekeeping.** Two verification servers from 4 Nov still run on 127.0.0.1:8812 and :8813 (own DB copies,
   schedule off, no outside connections), and 12 old git worktrees remain under `.claude/worktrees/`. Neither touches
   the live app.

## 5. Test suite: 101 failures from the live config, fixed in the fixture

| Run | Code | IST | Passed | Failed | Errors | Skipped |
|---|---|---|---:|---:|---:|---:|
| 1 | master `6ed65da`, main folder | 03:51:16–04:01:34 | 2,712 | 90 | 11 | 65 |
| Re-run of the 101 | + `tests/conftest.py` fix | about 04:04 | 97 | 4 | 0 | – |
| 2 (final) | + `tests/conftest.py` fix, main folder | 04:05:10–04:16:12 | 2,809 | 4 | 0 | 65 |

**Cause.** `orders/capital_plan.py` reads `atip_data/config.json` relative to the working directory. Since 19:21 IST
16 Nov the live file has the `paper_capital_plan` block, so every test run from the ATIP folder had the guard on:
its 10 % cap of a test fixture's ₹1,00,000 paper pool is ₹10,000, and the fixtures' ₹25,000 sessions were refused
(`ValueError: session S1's starting amount: Rs 25,000.00 is more than 10 % of the paper pool`), which then showed up as
`KeyError: 'S1'` in auto-pick, cycle, pool, trailing-exit, RCA and P&L-audit tests. Yesterday's runs were on the
branch worktree, whose config had no block, so they did not see it.

**The live app is not affected.** The guard is only reached from the owner's actions (`start_pool`, `setup_pools`,
`add_session`, `ledger.transfer`: dashboard and CLI). Auto-pick and the cycle rotation do not call it, and each
session's ₹45,000 is under the live cap (10 % of the ≈ ₹5 L pool).

**Fix (test-only, delta).** `tests/conftest.py` `_isolated` now points `orders.capital_plan.CONFIG_PATH` at a missing
file for every test, as it already does for the stop window, order monitor and pre-open gate;
`tests/test_capital_plan_2026_11_16.py` still writes its own plan file and passes.

**The 4 that still fail are environmental, not code.** From a clean checkout of the same commit three of them pass:
* `test_dhan_realtime_feeds::test_chain_tick_states_off_idle_and_live`: time-of-day (also in the 16 Nov reports).
* `test_mysql_backend::test_no_sql_uses_a_mysql_reserved_word_unquoted`: scans the folder, and the hits are all in
  untracked scratch copies under `.ux_build/` and `deliverables/` — no tracked file.
* `test_post_market_rca::test_time_nifty_sector_and_orb_guards` and
  `test_regressions::test_history_sync_stores_each_index_under_its_own_symbol`: read local files in the live
  `atip_data/` (Dhan security list / index data) that a clean checkout does not have.

## 6. How this was checked

`python -m ops status | validate | safety`; read-only SQLite (`mode=ro`) queries of `pipeline_log`, `ops_job_lock`,
`data_quality`, `prices_daily`, `ai_scores`, `intraday_bars`, `trading_sessions`, `session_transfers`,
`session_events`, `order_rules`, `paper_order`, `oms_order`, `algo_dummy_gtt`, `alert_log`, `ops_alert`;
`orders.sessions.list_sessions/ledger`; `ops.dhan_token.status()`; `ops.preopen_health.check_token/check_quotes/
check_scores/check_scrip_master` called directly (not `run_checks`, so no row was stored); HTTP GETs of the dashboard;
`PRAGMA quick_check`; `pip check`; the full pytest suite on master. Raw output: `.ux_build/health-2026-11-17/`.
