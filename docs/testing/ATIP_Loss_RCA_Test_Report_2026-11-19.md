# ATIP — Loss RCA fixes and strategy v2 — Testing report — 19 Nov 2026

Branch `claude/loss-rca-2026-11-19` on GitHub master 0c0c452 (worktree `.claude/worktrees/loss-rca-2026-11-19`;
uncommitted). All times IST. Nothing was run against the VPS; both "servers" below are local copies of the VPS's
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
- **No VPS deploy**: the VPS still runs c03fc29 (deployed 20:08:52 IST tonight for the data-quality fix, unrelated).
- **Mobile**: no native change in this work (the apps are shells over the server's screens); nothing to build.
  iOS still cannot be built on this Mac (no Xcode).
