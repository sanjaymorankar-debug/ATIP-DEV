# ATIP — Loss RCA fixes and strategy v2 — Testing report — 19 Nov 2026

Branch `claude/loss-rca-2026-11-19` on GitHub master 0c0c452 (worktree `.claude/worktrees/loss-rca-2026-11-19`;
committed as e5903e1 at 23:58:12 IST; live on the VPS since 00:46:11 IST 20 Nov, see "Deploy"). All times IST. Nothing was run against the VPS; both "servers" below are local copies of the VPS's
19 Nov 19:15 IST `pg_dump` (sha256 74bd1da7…d5f4).

## Environments

| | Test | Prod |
|---|---|---|
| Where | local, `http://127.0.0.1:8891` (launch config `lrca-test`) | local, `http://127.0.0.1:8892` (launch config `lrca-prod`) |
| Database | SQLite, ATIP's own schema (`main.py --init` + every module's ensure function), all 288 tables of the snapshot copied in | PostgreSQL 18 (Postgres.app, throwaway), `pg_restore` of the same snapshot (288 tables) |
| Config | `broker_env PAPER`, risk limits, **`strategy_v2` enabled** (`freshness_gate`, `retest_guard`; `nifty_gap_min_pct` null) | the VPS's own config.json (secrets were already empty; the `enterprise` sign-in block removed so the pages open without an account), **no `strategy_v2`** = as live |
| Process flags | `ATIP_OPERATING_SCHEDULE=off ATIP_ORDER_MONITOR=off ATIP_PREOPEN_GATE=off` (no scheduler, no monitor: a copy must never trade) | same |

## Results

| # | Test | Environment | Started – ended (IST) | Result |
|---|---|---|---|---|
| 1 | New regression tests `tests/test_loss_rca_2026_11_19.py` (28) | SQLite, worktree | 20:21 | **PASS** 28/28 |
| 2 | Live-PostgreSQL tests in the same file (lock timeout set; a blocked row-lock UPDATE ends "database is locked"; 55P03 mapping; `in_transaction`) | local PostgreSQL 18 | 19:30 | **PASS** 4/4 |
| 3 | The same new tests on untouched master c03fc29 (do they catch the bugs?) | baseline copy | 20:33 | 25 FAIL on old code, as intended (only the lock-reraise test passes there: old code raised too) |
| 4 | Full suite | SQLite, worktree | 20:20:18 – 20:31:41 | 3,018 tests: **2,904 passed, 34 failed, 80 skipped** |
| 4a | The 34 failures run on untouched master c03fc29 | baseline copy | 19:47:40 – 19:48:18 | the same 34 fail there → **no new failure**: 12 test_autopick + 4 score_audit + 3 verify_fixes + 3 post_market_rca + 2 full_review + kgf_gaps, kite_formula_fix, rca13 (auto-pick's 14:45 IST cut-off: these pass before 14:45 on any code, CAP note 16 Nov); 3 w39_perf_budget + 2 dhan_realtime_feeds (sandbox refuses local sockets / time of day); kite_retest_study::test_breadth_agrees_with_scores_breadth (fails identically on the baseline; cause not investigated); test_order_paths_are_untouched (passes once the branch is committed) |
| 4b | The clock-bound order tests with the clock pinned to 19 Nov 11:00 IST (a test-only conftest pins `datetime`): test_rca13_monitor_lock (incl. "a pass locked out by another writer queues its pick on the retry" — the pass this work reordered), test_autopick, score_audit, verify_fixes, kgf_gaps, full_review, post_market_rca, the KGF tick-rounding pick | fix and untouched master, SQLite | 20:40 | **identical on both: 101 passed, 3 failed** (cooldown and expired-confirmation fail only because the pinned clock stops time and pass in the unpinned run; the RCA-build test fails pinned and unpinned, identically on master) → the reorder (LRCA-02) breaks no auto-pick test in market hours |
| 5 | Pipeline run: technical indicators (499 tracked) + ATIP scoring for 19 Nov with the fix code | Test | 20:26:46 – 20:27:12 | **PASS**: technicals 499 rows 16.7 s, scoring 499 rows 8.4 s; vs the VPS's stored scores: 0 scores changed > 0.01, 0 signals changed (BUY 2, HOLD 464, WAIT 33) |
| 6 | Pipeline run (same) | Prod | 20:27:12 – 20:27:55 | **PASS**: technicals 499 rows 19.4 s, scoring 499 rows 22.6 s; 0 score / 0 signal changes |
| 7 | Breadth job for 19 Nov (the 21:20 / 06:00 job) | Test, Prod | 20:27 | breadth20 69.98 %, breadth50 55.83 % on both → every gate OPEN for 20 Nov |
| 8 | strategy v2 freshness gate, before step 7 (breadth of 18 Nov is older than the previous session for a 20 Nov decision) | Test | 20:26 | **PASS**: S1–S4, S6–S10 gates read "stored breadth is stale" — closed until the breadth job runs (Prod, v2 off: unchanged) |
| 9 | Paper-trade dry run: `orders.autopick.run(dry_run=True)` for 20 Nov 09:20 IST, every session | Test, Prod | 20:27 | **PASS** on both, same decisions: S3 → Kite retest KALYANKJIL resting buy ₹620.00 × 37 (gate open, 55.83 %); S1/S2/S4–S9 hold a position (PNB, INDHOTEL, CDSL, KOTAKBANK, GRASIM, ICICIBANK, PHOENIXLTD, DLF); S10 has a FACT buy queued. Nothing written: order_rules 169 → 169, paper_order 97 → 97 |
| 10 | Dashboard pages and APIs: `/`, `/sessions`, `/rca`, `/api/rca`, `/ab`, `/command`, `/algo-signals`, `/m`, `/api/v2/shell/state`, `/api/command/summary`, `/api/v2/engine` | Test 20:28:31, Prod 20:28:50 | — | **PASS** all 200, no error page; Sessions page shows combined P&L +₹8,826.76 (realised +₹5,883.07, open +₹2,943.69), gate OPEN for 20 Nov (screenshot `docs/reports/2026-11-19-loss-rca/evidence/prod_sessions_2026-11-19_2029IST.jpg`) |
| 10a | `/health` | Test, Prod | 20:29 | 503 "FAILED" on both — **expected on a copy**: no scheduler process (scheduler FAILED) and no Dhan token (broker / market data DEGRADED) |
| 11 | Prod with the VPS's sign-in on (`enterprise.enabled true`) | Prod | 20:28 | pages redirect to /login, APIs 401 — the live sign-in works; the block was then removed for checks 10/10a |
| 12 | Replay calibration (backtest fidelity) on the 41 real closed trades | `research.session_ab_replay calib` | 20:16:57 | 41/41 exit reasons, corr 0.971, simulated ₹4,717 vs actual ₹5,883 |

## Not covered (stated plainly)

- **Market-hours behaviour on the VPS**: the monitor's new order (exits first), the heartbeat and the 4-pass alert were
  tested by unit tests only; they first run live at 09:15 IST on the first trading day after a deploy.
- **No VPS deploy at test time**: the VPS ran c03fc29 (deployed 20:08:52 IST 19 Nov for the data-quality fix, unrelated).
  See "Deploy" below.

## Deploy (added 20 Nov 2026, IST)

| Step | When (IST) | Result |
|---|---|---|
| Commit e5903e1 on `claude/loss-rca-2026-11-19` | 23:58:12, 19 Nov | after the commit the order-path guard test passes; 29 passed, 3 skipped (need a throwaway PostgreSQL) |
| GitHub master fast-forwarded 0c0c452 → e5903e1 | 00:01:25, 20 Nov | owner: "Yes, deploy it" |
| Deploy by the VPS session (hand method, owner's yes there at about 00:40) | 00:41 – 00:50 | 13 server files sha256-checked against GitHub; the 11 replaced files matched c03fc29 (no drift); old files + `pg_dump` (139 MB) backed up 00:41; py_compile OK; 0 active ATIP queries |
| Stop / start | 00:46:11 | pid 781182 → 836452, NRestarts 0; scheduler leader held by the new pid |
| Health, read here | 00:51:05 | `/atip/health/live` 200 LIVE pid 836452 (started 00:46:13); `/atip/health/ready` 200 READY; `/atip/health` components all READY except scheduler DEGRADED from the `history_backfill` failure of 06:00:34 IST 19 Nov (the known DH-907 new-listing refusal, before this deploy) |
| Journal 00:46:10 – 00:50:36 (VPS session) | — | 0 tracebacks / errors; scheduler and order-rules monitor started |
| Strategy v2 on the VPS | — | `python -m orders.strategy_v2`: `"enabled": false`; config.json not edited (no `strategy_v2` block) |

Still not covered: the monitor's new behaviour (exits first, heartbeat, 4-pass alert) first runs live at 09:15 IST 20 Nov.
- **Mobile**: no native change in this work (the apps are shells over the server's screens); nothing to build.
  iOS still cannot be built on this Mac (no Xcode).

## Recheck, 20 Nov 2026 (IST) — owner: "recheck all code and fixes"

| # | Check | Where | When (IST) | Result |
|---|---|---|---|---|
| R1 | Every earlier fix commit (30, 3 Nov – 19 Nov) and every LRCA marker in GitHub master 514b439 | git | 00:57 | **PASS**: all present |
| R2 | The VPS's code files vs master 514b439 (sha256 of 477 code files) | VPS session, read-only | 00:59 | **PASS**: 477/477 identical, none missing; 13 extra `.py` are master's own docs/ analysis scripts |
| R3 | Post-market check tool (read-only) on the live PostgreSQL for 19 Nov, manifest 514b439 | VPS session | 01:03:46 – 01:03:54 | **PASS**: runtime code = master (0 differ/missing/extra); composites 499/499 on 18 and 19 Nov (max diff 0.005); 320 indicator fields 0 flags; breadth 18 Nov exact; P&L re-derived = stored; 8/8 open positions protected; DUMMY GTTs only. Flags explained: S4 CDSL size (tool ignores the 16 Nov top-up; app = 2.5 % of ~₹45,800 / 5 % stop = 16), P&L chain (tool on PostgreSQL; 0 on the same data in SQLite), `news_impact.article_id` counter behind (never used: the only insert supplies the id) |
| R4 | Same tool + independent pandas formula check on the Test copy | local | 01:00 – 01:01 | **PASS**: same composites; Dhan vs NSE 0 over 0.5 %; only OBV level differs (known CAP-12 convention, no score uses it) |
| R5 | Full suite on master 514b439 | worktree | 00:59:32 – 01:12:03 | 3,018: 2,932 passed, 6 failed, 80 skipped. 3 `w39_perf_budget` = sandbox sockets (14/14 pass outside); 3 stale tests fixed (R6) |
| R6 | Kite study + option-chain tests after the fixture fixes | worktree | 01:13 – 01:15 | **PASS** (63/63 in the two files; all Kite files pass) |
| R7 | Independent code review of c03fc29..e5903e1 | reviewer agent | 00:59 – 01:19 | 1 High (LRCA-16, reproduced on the live code), 3 Medium, 4 Low — see CODE_REVIEW.md addendum |
| R8 | Regression tests for LRCA-16..20 | worktree / export of 514b439 | 01:22 – 01:24 | 7/7 pass on the fix; 7/7 FAIL on 514b439 (= e5903e1) |
| R9 | Full suite on the fixes (6691471 before the date fix) | worktree | 01:23:12 – 01:34:47 | 3,025: 2,940 passed, 5 failed, 80 skipped — 3 `w39_perf_budget` (sandbox), the monitor-alert test (updated for LRCA-18 keys before the push), `test_new_dashboard_routes_add_no_date_today_to_server` (fixed by c9d168f); after c9d168f the affected files pass (253 related tests at 01:27, house test at 01:35) |
| R10 | Deploy 6691471 by the VPS session (owner's yes there ~01:33) | VPS | 01:35 – 01:40 | 5 files sha256-checked (old = e5903e1, no drift); old files + `pg_dump` 01:35:21; stop/start **01:36:10**, pid 836452 → 847061; health 200; journal 01:36–01:40 clean; strategy_v2 false; config.json unchanged |
| R11 | Deploy c9d168f (server.py only) so the VPS matches master (owner's yes there ~01:42) | VPS | 01:41 – 01:50 | old = 6691471, new = dd0d75ba…; old file + `pg_dump` 01:41; stop 01:46:10 / start **01:46:11**, pid 847061 → 850092; journal 01:46–01:50 clean; strategy_v2 false; config.json unchanged |
| R12 | Health read from here | public URL | 01:51:11 | `/atip/health/live` 200 LIVE pid 850092 started 01:46:12; `/health/ready` 200; scheduler DEGRADED only from the 19 Nov 06:00 `history_backfill` refusal (known); GitHub master = c9d168f = the VPS |
