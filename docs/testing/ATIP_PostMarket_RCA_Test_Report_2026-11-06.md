# ATIP testing report — post-market RCA, 6 Nov 2026

Owner prompt: 6 Nov 2026, 05:28 PM IST. Tested 17:30–18:20 IST on 6 Nov 2026. **Every time here is IST.**

Build under test: branch `claude/rca-2026-11-06` (from master `ea4f2a0`), worktree `.claude/worktrees/rca-2026-11-06`.
**Committed on the branch as `1966119` (6 Nov 2026, 21:44 IST); not merged, not live.** The Mac's running ATIP (launchd `com.atip.platform`, port 8000) was not
restarted or written to; the live `atip.db` was only read (one SQLite backup at 17:30:16 IST).

## 1. Environments

ATIP has no hosted Test or Prod server (tracker UXR-11). As in the 26 Oct – 5 Nov reports, "Test" and "Prod" are local
instances of the branch's code, each in its own scratch folder, so no config.json, vault or broker token reaches them.

| Name | URL | Database | Settings | Purpose |
|---|---|---|---|---|
| Test | http://127.0.0.1:8831 | fresh (`init_db`), `.ux_build/rca-2026-11-06/srv/test/atip.db` | `ATIP_ENV=test`, schedule off | empty-state, build with no trades, guard validation |
| Prod | http://127.0.0.1:8832 | SQLite backup of the live `atip.db` taken 17:30:16 IST, `.ux_build/rca-2026-11-06/srv/prod/atip.db` | `ATIP_ENV=staging`, schedule off | today's real trades, real Dhan 1-min candles, UI sweep |
| Viewer | http://127.0.0.1:8833 | copy of the Prod snapshot | `ATIP_ENV=staging`, `ATIP_ROLE=viewer` | the view-only server must refuse every change |

Launch entries: `atip-rca-test`, `atip-rca-prod`, `atip-rca-viewer` in `.claude/launch.json`. Machine: macOS (Darwin 23.6.0),
Python 3.15.2, the project's `.venv`. Browser checks: the desktop app's built-in Chromium at 1440×900 and 375×812
(mobile emulation, `?app=1` = the apps' user agent), light and dark.

Data used for today's report (Prod): 1-minute candles for the 13 stocks the sessions held and 13 NSE indices, fetched
from Dhan through `data/dhan.py` at 17:31–17:52 IST (read-only data calls; timestamps are IST, checked: first candle
09:15, last 15:29). The 42-session study fetched 5-minute candles for 156 stocks and the Nifty 50 at 17:40–17:46 IST.

## 2. Automated tests

| Run | Time (IST) | Result |
|---|---|---|
| `tests/test_post_market_rca.py` (new, 14 tests) | 17:36 | 14 passed |
| New + touched areas: test_post_market_rca, test_autopick, test_session_pools, test_ux_shell, test_ux_audit_fix_2026_11_03, test_dhan_realtime_feeds (final code) | 18:04:21–18:05:36 | **223 passed** |
| Full suite on the branch | 17:54:36–18:03:43 | **2,572 passed, 65 skipped, 4 failed** |
| `test_ui_fixes_v2::test_order_paths_are_untouched` in a committed scratch clone | 18:04 | 1 passed |
| After the commit (`1966119`): test_ui_fixes_v2, test_post_market_rca, test_autopick, test_session_pools, test_ux_shell | 21:46–21:47 | **173 passed**, test_order_paths_are_untouched included |

The 4 failures in the full run:

* `test_w39_perf_budget` × 3 (`test_block_network_refuses_outside_hosts_and_keeps_loopback`, `test_server_mode_over_real_http`,
  `test_cli_exit_codes`): the sandbox refuses local sockets. Same 3 on master (`ea4f2a0`: 2,559 passed / 65 skipped / 3 failed,
  5 Nov 22:43–22:52 IST).
* `test_ui_fixes_v2::test_order_paths_are_untouched`: it checks that the tree is clean under `orders/`; the work is not
  committed yet. Its allow-list now names `orders/entry_guards.py`; with the work committed (scratch clone) it passes.

The suite ran after the close (17:54–18:03 IST), so the market-hours order-monitor race does not apply.
2,572 = 2,559 + 14 new − 1 (the uncommitted-tree test).

New tests (`tests/test_post_market_rca.py`):

| ID | Test | Checks |
|---|---|---|
| U-01 | test_resample_aligns_five_minute_candles_on_0915 | 1-min → 5-min candles start 09:15 |
| U-02 | test_simulate_stop_gap_trail_breakeven_and_late_stop | gap-through fills at the open; 1 % trail from +3 %; break-even; stop armed from 09:45; a carried-in trail |
| U-03 | test_path_finds_the_low_the_recovery_and_a_stop_before_it | low / recovery (5-min rule) / back to entry; stopped at the low and before the recovery (LODHA-shaped day) |
| U-04 | test_classify_names_the_tight_stop_and_not_the_market | d) 0.53x ATR, b) RANK-FILL + NULL SPI, c) gap-down entry, h) beta 1.80; no a) when Nifty rose; no g) with positive news |
| U-05 | test_what_if_is_measured_like_the_day_mtm | actual = real fill net of charges; ATR stop rides it out; gap and score filters |
| I-01 | test_build_run_and_latest_on_a_real_session_trade | a real buy and stop through orders.rules → broker → paper; session summary; actual what-if = day MTM; stored / read back |
| I-02 | test_run_scheduled_skips_holidays_and_the_viewer | 16:00 job: holiday and viewer skip |
| I-03 | test_the_job_is_registered_at_1600_ist | `post_market_rca` registered at 16:00 |
| I-04 | test_rca_page_and_api | /rca empty state, then tables, chart, guards; /api/rca 404 → 200; apps' user agent gets the same page; NAV link |
| I-05 | test_guard_api_needs_the_token_and_an_ack | 401 without token, 409 without ack, 400 unknown guard, on / off |
| I-06 | test_every_guard_is_off_by_default_and_on_needs_an_ack | all OFF; ack needed; BUY not affected by the score guard; ORB in 15-min steps; GUARD event logged |
| I-07 | test_time_nifty_sector_and_orb_guards | not-before, Nifty weak, sector cap, ORB (no range / under / above the range high) |
| I-08 | test_auto_pick_passes_over_a_stock_a_guard_refuses | auto-pick waits with the guard's reason; picks again once off |
| I-09 | test_the_viewer_cannot_change_a_guard | ViewerRefused |

## 3. Test server (fresh DB, :8831)

| ID | Time (IST) | Case | Expected | Result |
|---|---|---|---|---|
| T1 | 17:59 | GET /rca with nothing stored | empty state, no error | PASS |
| T2 | 17:59 | GET /api/rca | 404 | PASS (404) |
| T3 | 17:59 | GET /api/rca/guards | 5 guards, all off | PASS |
| T4 | 17:59 | POST /api/rca/run (no credentials in the folder) | builds today from stored bars, 0 trades | PASS ("Realised net Rs 0.00 (0 closed …)") |
| T5 | 17:59 | GET /api/rca after T4 | day 2026-11-06, 10 sessions, source "stored 15-minute bars" | PASS |
| T6 | 17:59 | GET /rca after T4 | tables with empty rows | PASS |
| T7 | 17:59 | POST guard_min_score 150 | 400 out of range | PASS |
| T8 | 17:59 | POST guard_entry_not_before 25:00 | 400 | PASS |

## 4. Prod server (live-DB snapshot, :8832)

| ID | Time (IST) | Case | Expected | Result |
|---|---|---|---|---|
| P1 | 17:47–17:58 | /rca at 1440×900 dark | summary tiles, sessions, path table, causes, 6 loss charts, what-ifs, guards | PASS |
| P2 | 17:57 | GET /api/rca | 6 Nov; net −1,107.85; day MTM −763.12; 13 trades; 10 sessions | PASS |
| P3 | 17:57 | GET /api/rca/guards | 5, all off | PASS |
| P4 | 17:57 | POST guard on without ack / without token | 409 / 401 | PASS |
| P5 | 17:57 | guard_orb_minutes on with ack, then off | on (30), then off | PASS |
| P6 | 17:57 | POST /api/rca/run?day=2026-11-05 | builds 5 Nov from stored 15-min bars | PASS: realised net **₹1,346.12** = the 5 Nov profit audit's figure |
| P7 | 17:57 | GET /more | lists Post-market RCA (/rca) | PASS |
| P8 | 17:58 | "Turn on…" → dialog → "Keep off" | ATIP's own dialog; nothing turned on | PASS (0 guards on) |
| P9 | 17:57 | GET /api/rca?day=2026-01-01 | 404 | PASS |
| P10 | 17:53 | /rca?app=1 at 375 px, light and dark | no sideways scroll (scrollWidth 375 = clientWidth); charts legible | PASS |
| P11 | 17:50 | console | no errors | PASS |

Reconciliation on the Prod data: realised net −₹1,107.85 = the four sales in `session_cycles` (−716.87 −601.04 −578.70
+788.76); the what-if "actual" row (−₹763.12) equals the sessions' day P&L total to ₹0.01; charges per fill equal
`backtest/costs` (e.g. BHEL sale ₹40.89, LODHA buy ₹30.19).

## 5. View-only server (:8833)

| ID | Time (IST) | Case | Expected | Result |
|---|---|---|---|---|
| V1 | 17:59 | GET /rca | the Mac's stored report | PASS |
| V2 | 17:59 | GET /api/rca | 200 | PASS |
| V3 | 17:59 | POST /api/rca/run | refused | PASS (403, "view only") |
| V4 | 17:59 | POST /api/rca/guards (ack) | refused | PASS (403) |

## 6. Mobile apps (Android, iOS)

The apps are web shells (mobile/README.md): the RCA page reaches them through the server, under More → Reports →
Post-market RCA, with the same guard switches as the web (each asks first). Version 2026.11.06.1 (Android versionCode 9,
iOS CFBundleVersion 9). Checked: the page with the apps' user agent at 375 px (P10, I-04). **Not done here:** compiling
and installing the apps (no Android SDK / Gradle on this Mac; Xcode not run). Tracker RCA-08 stays IN PROGRESS.

## 7. Defects

Found and fixed during this testing (all on the branch):

| ID | Defect | Fix |
|---|---|---|
| D1 | Loss charts unreadable in the 4-column card grid | cards at least 560 px wide |
| D2 | Chart axis text unstyled (black on dark) outside `.ax-chart` | chart wrapped in `.ax-chart` |
| D3 | Exit / Low marker labels overlapped (LODHA) | labels step apart |
| D4 | POST /api/rca/* returned 422 (postponed annotations hid the Request type) | `from __future__ import annotations` removed from rca_routes |
| D5 | KeyError after the gap guard was dropped | gap check removed from check_entry |
| D6 | a DATE column came back as `date`, compared with `str` | dates normalised to text |
| D7 | the industry map tried to download from NSE inside tests / every call | read once a day; tests use a fixed map |
| D8 | carried positions replayed with the trigger-based stop, not fill × 0.98 | stop = fill × (1 − stop %); trail state from the monitor's own high-water |
| D9 | duplicated caption; what-ifs ranked by today's ₹ only | ranked by the 42-session verdict, then today's ₹ |

Open (found in the analysis, not fixed; tracker RCA-13 … RCA-17): the 09:16:09 database lock at the open; the 30-second
poll missing stop wicks; SPI / fund_score NULL for every stock; the global feed's S&P −4.36 %; TRAIL_MOVED log noise.

## 8. Pending

* Merge and restart (owner's go-ahead; committed on the branch 21:44 IST). Then the first live run is **Mon 9 Nov 2026 at 16:00 IST** (7 and 8 Nov
  are not market days; 10 Nov is a holiday in ATIP's calendar). Check `/rca` on the Mac after it.
* Guards stay OFF until the owner turns them on.
* Build and install the apps.

Evidence: `docs/testing/screenshots/rca-2026-11-06/` (7 screenshots), `.ux_build/rca-2026-11-06/out/` (suite output,
report JSON, 42-session study JSON).
