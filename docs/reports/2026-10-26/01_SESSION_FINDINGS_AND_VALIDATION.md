# ATIP — merge, restart and validation report (2026-10-26)

*Prepared 26 Oct 2026, 19:35–21:15 IST, on the owner's Mac, `/Users/agtci/ATIP`, branch `master`. Every figure below
comes from a command, the database or a log; where something could not be run, the report says so.*

## 1. Phase 0: the session that had to finish first

| Item | Evidence |
|---|---|
| Session | Cloud session "ATIP profit model and architecture" (`claude.ai/code/cshk_01UQ…`), branch `claude/youthful-archimedes-mubyk3`, PR #17 (draft) |
| State when Phase 1 started | Idle. Last push 88cd402 at 14:14 UTC; its own full suite 1,621 passed / 48 skipped / 0 failed; GitHub CI on 88cd402 green at 14:25 UTC (pytest + image); its last message: "CI passed on 88cd402 … Nothing needs changing" |
| Decisions it left for the owner | (1) rewrite 9 pushed commit messages to drop a model-name trailer (force-push; not done); (2) other project folders were not checked |
| Waiting | No ATIP file, branch or database was edited before 14:26 UTC; only read-only queries ran |

## 2. Merge and commit

Local `master` (was 3419370) was fast-forwarded to `origin/master` 37e2a3e (#18), then two merges were committed,
each listing every file it changed in its message:

| Commit | What | Files |
|---|---|---|
| f84e61d | Merge PR #17 (88cd402) | 23 files: README.md, docs/ENTERPRISE_ARCHITECTURE.md, docs/master/* (master file .md/.xlsx, 7 diagrams, simulations.json), enterprise/authz.py, pipeline/scheduler.py, strategy/aggressive.py, strategy/backtest_aggressive.py, strategy/live.py, tests (4 changed/added), tools/master_file.py. The fast-forward before it brought #18's 9 files (exit target study, ATR stop, risk sizing) |
| 912e654 | Merge `fix/mac-folder-move` (9aad349) | 13 files: the Mac fixes ATIP was already running with (certifi fallback in start_atip.sh and tools/dhan_token_refresh.sh, LaunchAgent via /bin/bash, paths for /Users/agtci/ATIP) |

| f6ceb25 | Merge PR #17 update (5ee26cb, pushed by the same session at 20:21 IST, after Phase 1 had started; CI green at 14:54 UTC) | 20 files: dashboard/security.py, dashboard/server.py, 13 dashboard route modules (page token), enterprise/authz.py, docs/ENTERPRISE_ARCHITECTURE.md, docs/master/ATIP_MASTER_FILE_2026-10-26.md/.xlsx, tests/test_enterprise_owner_book.py |

No conflicts in any of the three merges. The fixes made during validation are separate commits (§6). Nothing was
pushed: the merges and fixes are local on `master`, and PR #17 is still open on GitHub.

## 3. Restart

| Component | Result |
|---|---|
| Scheduler + dashboard | `launchctl kickstart -k gui/501/com.atip.platform` (20:05:06; 20:28:11 after the fixes; 20:43:38 after merging 5ee26cb). LaunchAgent run #4, PID 12407. Log: `LIVE_TRADING_ENABLED = FALSE (execution.mode is PAPER)`, `ATIP MASTER SCHEDULER STARTED`, `Aggressive exits: aggressive_manage every 2 min, 09:15-15:30 on trading days` (the merged session's new job) |
| Dashboard | `/health/live` LIVE, `/health/ready` READY, database READY |
| WebSocket feed | Supervised; "market closed, connecting at 09:00 on the next trading day". It cannot stream: Dhan's Data API plan is inactive (§5) — on 12 Oct it failed with "no close frame received or sent" |
| Order-rules monitor | Started |
| Evening jobs after restart | `participant_oi` 20:15 SUCCESS (20 rows) — the first participant-OI collection ever recorded |

## 4. Session Findings (what the previous session built, what is open, regressions)

**Built or fixed by the previous session (PR #17, now on master):**

1. ATIP master file (architecture, flows, terminology, 333-feature catalogue, 4%/75% analysis) + `tools/master_file.py` generator, tests.
2. MRI read the right way round at the +6% runner checkpoint (`strategy/aggressive.py`), with a test pinning the direction.
3. Aggressive exit manager scheduled every `manage_interval_minutes` (default 2) in market hours, only while a position is open, never selling through a broker other than the one the position was opened with (MODE_MISMATCH reported).
4. `OWNER_BOOK` guard: other tenants are refused the owner's orders, OMS positions, risk and mobile routes (latent; enterprise mode is off).

5. (5ee26cb, after the first merge) **R12 fixed**: the install's `X-ATIP-Token` now reaches only the owner's requests; another tenant gets a token derived from the install token and tied to its session, and the install token presented by another tenant is refused on write routes. Verified here after the restart: with enterprise mode off, the owner's pages (/, /trading, /signals, /execution-lab, /data-platform) still carry the install token (SHA-256 prefix match), so writes keep working.

**Still open from that session:** R10 (no hosted test server); rotate the install token before anyone enables `accept_legacy_token`; the per-session token is not browser-tested for a tenant; the force-push question (model name in 9 commit messages); PR #17 still a draft on GitHub.

**Regressions:** none found. Full suite after the merge: 1,621 passed, 45 skipped, 3 failed — the 3 are `tests/test_w39_perf_budget.py` loopback tests that this sandbox cannot run (`PermissionError: Operation not permitted` on local sockets); they pass in GitHub CI on 88cd402, and the merged Mac-fix files do not touch them.

**Found during validation (outside PR #17), with what was done:**

| # | Finding | Severity | Action |
|---|---|---|---|
| F1 | 9 trading sessions missing entirely from `prices_daily` (8, 12–16, 19, 21, 22 Oct): ATIP was off 13–23 Oct; the catch-up never recovers a session that has no bars | P0 data | Backfilled from NSE's Bhavcopy archive with ATIP's own recovery path (`run_postmarket(backfill=True)`), delivery filled, 23 and 26 Oct re-scored. Code fixed (ac58625) |
| F2 | Dhan Data API plan inactive (profile `dataPlan: Deactive`; quotes HTTP 401 code 806 "Data APIs not Subscribed"; history DH-902). Last Dhan bar 17 Sep; last live quote 5 Oct | P0 owner action | Cannot be fixed in code. Broker health, the exit manager and the phone now say so (63767d8) |
| F3 | A test run from the project folder overwrote the live `atip_data/dashboard_state.json` with fixture rows (at 20:23:33) | P1 test isolation | Restored from the database at 20:24:51 (80 s exposure). Fixed in `tests/conftest.py` (723928e) |
| F4 | Bulk/block deals: NSE's file holds only its latest session, and the downloader ignored the date — the catch-up stamped 26 Oct's deals onto 5 past sessions and overwrote genuine 13/23 Oct values | P1 data | Rows restored from the pre-backfill verified backup; none was in the scored universe. Code fixed (1ecda6b) |
| F5 | `pnl_daily` LIVE: 23 Oct equity NULL (sync failed, DH-901), so 26 Oct day P&L shows ₹5,94,472 on ₹5,77,996 equity | P2 | Tracked as GAP-2610-15 |
| F6 | The "morning brief" auto-recovery sent the brief at 18:49 | P2 | Tracked as GAP-2610-14 |
| F7 | An untracked `.ux_build/atip_src.tgz` (a source tarball) appeared in the project folder at 19:58; not created by this session, referenced by nothing in the repo | Info | Left untouched |

## 5. Validation and testing report

### 5.1 Test suite (pass / fail per test)

| Run | Code | Tests | Passed | Skipped | Failed | Time |
|---|---|---|---|---|---|---|
| Merged tree | 912e654 | 1,669 | 1,621 | 45 | 3 (sandbox-only) | 302 s |
| With the fixes | 723928e | 1,681 | 1,633 | 45 | 3 (sandbox-only) | 291 s |
| With the fixes + bulk deals + tracker | 98f816b | 1,683 | 1,635 | 45 | 3 (sandbox-only) | 296 s |
| After merging 5ee26cb (R12) — final | f6ceb25 | 1,688 | 1,640 | 45 | 3 (sandbox-only) | 305 s |

Per-test results (every test, outcome, seconds): `evidence/tests_2026-10-26_HEAD.csv` (JUnit at 723928e) and
`evidence/tests_f6ceb25.csv` (the final merged head).

New tests written for the merged code and the fixes (all pass):

| File | Tests | Covers |
|---|---|---|
| tests/test_scheduler_wiring.py | 1 | `start_scheduler()` registers PR #17's aggressive exit manager (`scheduled_pass`, every 2 min, through `run_job`) |
| tests/test_catchup_price_holes.py | 4 | holes found only between stored sessions; in-window holes re-scored oldest first; older holes get bars + delivery; fresh install does not fan out; complete history is a no-op |
| tests/test_dhan_data_plan.py | 7 | `data_plan_status()`; broker health names an inactive plan; exit manager alerts once a day when positions are unmanaged, not when managed; mobile app shows the Data feeds card; service worker v2 |
| tests/test_bulk_deals_dates.py | 2 | only the requested session's rows are stored; "NO RECORDS" is not a symbol; undated files read as before |

### 5.2 Dashboard tabs — Prod (the live instance, `http://localhost:8000`, fixed code)

All 26 pages return HTTP 200 with content and no traceback. Each page's GET `/api/` calls (194 in total) were called:
134 returned 200; the other 60 are expected non-200, none is a server error:

| Page | HTTP | APIs 200 / called | Non-200 explained |
|---|---|---|---|
| / (home) | 200 | 13/13 | — |
| /signals | 200 | 8/8 | — |
| /market | 200 | 5/5 | — |
| /market-pulse | 200 | 8/10 | 405 (POST-only: gift capture, order-book snapshot) |
| /trading | 200 | 17/19 | 405 (POST-only: broker-health check, paper match) |
| /strategies | 200 | 4/4 | — |
| /strategy-builder | 200 | 1/1 | — |
| /backtests | 200 | 2/2 | — |
| /execution-lab | 200 | 5/10 | 404 empty state (no event-driven run, no algo tick), 405 POST-only, 422 needs symbol/quantity |
| /data-platform | 200 | 8/15 | 405 POST-only (depth/chain snapshot, lake archive/verify, minute bars), 422 needs symbol |
| /research | 200 | 4/5 | 405 POST-only (history backfill) |
| /screener | 200 | 2/3 | 405 POST-only (ask) |
| /options-builder | 200 | 1/3 | 405 POST-only (analyse, build) |
| /quant | 200 | 10/11 | 422 needs key |
| /ml | 200 | 7/7 | — |
| /assistant | 200 | 2/3 | 405 POST-only (ask) |
| /baskets | 200 | 3/3 | — |
| /brokers | 200 | 4/5 | 405 POST-only (import from broker) |
| /compliance | 200 | 5/5 | — |
| /wealth | 200 | 23/33 | 404 empty state (no Investor DNA / allocation yet), 400 needs DNA, 405 POST-only |
| /m (mobile PWA) | 200 | 1/1 | — |
| /privacy | 200 | 0/0 | — |
| /login | 200 | 0/0 | — |
| /app, /account, /admin | 200 | 1/16, 0/7, 0/5 | 503 "enterprise features are disabled" (single-owner by decision) |

Also 200: `/health/live`, `/health/ready`, `/manifest.webmanifest`, `/sw.js`, `/icon.svg`, `/api/live-quotes`, `/api/mobile/summary`.
Raw: `evidence/smoke_prod_localhost8000.json`. The sweep was run three times — at 912e654, at 723928e and at f6ceb25
(after the R12 merge) — with the same result each time: 26/26 pages 200, 134 of 194 API calls 200, 0 unexpected 5xx.

### 5.3 Dashboard tabs — Test environment

There is no hosted test server (master file R10; `atip_data/publish.json` does not exist on this Mac, so even the
read-only snapshot to bkesari.com is not configured here). The closest honest test: the dashboard at the fixed code,
served in-process (FastAPI TestClient, no port) against a **copy** of the database: all 26 pages 200, no unexpected
5xx, all 7 extra endpoints 200. Raw: `evidence/smoke_test_env_inprocess.json`.

### 5.4 Live data, broker and pipeline smoke tests (no orders placed)

| Check | Result | Evidence |
|---|---|---|
| `/api/live-quotes` | PASS (HTTP 200) but empty `{}` | no Dhan data plan; outside market hours |
| Dhan quotes (`main.py --dhan-quote RELIANCE TCS INFY HDFCBANK`) | **FAIL** | `marketfeed/ltp` HTTP 401 `{"806": "Data APIs not Subscribed"}` |
| Dhan token | PASS | `/v2/profile` 200, tokenValidity 27/10/2026 18:47 (renewed 18:47 today by the LaunchAgent) |
| Funds check (`/v2/fundlimit`, read-only) | PASS | HTTP 200; available balance ₹0.00, sodLimit 0, withdrawable 0 |
| Bhavcopy ingestion | PASS | 22 Oct: 3,283 bars from NSE's archive; then 8 more sessions 3,290–3,316 bars each |
| Scoring pipeline | PASS | 11 sessions scored (8 Oct → 26 Oct), 498–499 symbols each; 26 Oct: 2 BUY, 229 HOLD, 268 WAIT |
| Broker health (fixed) | DOWN, cause named | "Dhan Data API plan is Deactive: quotes, WebSocket feeds, intraday bars and history need it" |

### 5.5 Data-quality checks

| Check | Before (19:50) | After backfill (20:23) |
|---|---|---|
| Trading sessions missing from `prices_daily` since 2025-03-03 | **9** (8, 12–16, 19, 21, 22 Oct) | **0** |
| Duplicate batches (a session's OHLCV identical to the previous one, > 5% of symbols, last 90 sessions) | none | none |
| Sawtooth batches (≥ 1% of symbols jumping > 8% and reversing > 8% next session) | 9 Oct flagged (35 of 3,204) — an artefact of the hole | none |
| Corrupted DATE values (malformed, future, non-trading day) in prices_daily / technical_indicators / ai_scores | 0 / 0 / 0 | 0 / 0 / 0 |
| Null EMA (9/21/50) for symbols with ≥ 60 bars, latest 5 dates | 0 | 0 |
| Null EMA-50 / SMA-200 / MACD overall | ~2,640 rows a day: symbols with < 50–200 bars (full-market Bhavcopy only since Sep 2026) — expected | same |

Raw: `evidence/data_quality_before_backfill.json`, `evidence/data_quality_after_backfill.json`.

## 6. Fix commits (delta patches)

| Commit | Fix | Files |
|---|---|---|
| ac58625 | Catch-up recovers holes in prices_daily | pipeline/scheduler.py, tests/test_catchup_price_holes.py |
| 63767d8 | Name the inactive Dhan Data API plan; alert when aggressive exits cannot run; mobile Data feeds card | data/dhan.py, execution/broker_health.py, strategy/live.py, dashboard/w38_page.py, tests/test_dhan_data_plan.py |
| 723928e | Tests never write the live dashboard state; exit-manager wiring test | tests/conftest.py, tests/test_scheduler_wiring.py |
| 1ecda6b | Bulk deals stored only for their own session | data/bhavcopy.py, tests/test_bulk_deals_dates.py |
| 98f816b | Tracker status 2026-10-26 | docs/ATIP_MASTER_TRACKER.csv, ATIP_MASTER_TRACKER.xlsx |

## 7. Mobile apps (Android and iOS)

ATIP's phone app is the installable PWA at `/m` (manifest + service worker); there is no native Android or iOS
project in the repository. Changes that affect it, made in 63767d8 and verified on a 375×812 phone viewport:
a **Data feeds** card (Dhan market data status with the reason, broker health, when news was last fetched and by which
classifier) and the service-worker cache bumped to `atip-shell-v2`, so installed copies on Android and iOS pick up the
new shell. PR #17's `OWNER_BOOK` guard also covers `/api/mobile/` (no change for the owner).
