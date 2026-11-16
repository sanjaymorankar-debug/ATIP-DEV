# ATIP — Testing report: capital review (16 Nov 2026, IST)

**What each server was** (ATIP has no hosted Test/Prod servers; same convention as the 26 Oct–16 Nov reports):

* **Test server** = local `uvicorn dashboard.server:app` on :8871, branch code (`PYTHONPATH` = worktree), `ATIP_ENV=test`, a fresh database (`main.py --init`), the owner's ten ₹25,000 pools created through the API, PAPER, no broker keys.
* **Prod server** = the same code on :8872 with `ATIP_ENV=staging` and a SQLite backup of the live `atip.db` taken 16:52:44 IST (after the 16:45 post-market run), the live `config.json` without secrets plus the `paper_capital_plan` block. The live Mac (pid 82833, master e41ccf3) was not restarted or written to.
* The market was closed (post-market); nothing was replayed through a live broker.

## 1. Full test suite

| Run | Code | IST | Passed | Failed | Skipped | Notes |
|---|---|---|---:|---:|---:|---|
| Baseline | master code (e41ccf3 = b717db7 + docs), untouched | 17:00:10 IST–17:11:07 IST | 2,766 | 31 | 65 | the same hour as the patched run |
| Patched | branch `claude/capital-plan-2026-11-16` | 17:19:14 IST–17:29:11 IST | 2,779 | 32 | 65 | +14 new tests; final |
| Patched, run 1 | same branch before the fix below | 17:08:30 IST–17:18:35 IST | 2,778 | 33 | 65 | 2 new failures, see below |

Run 1 had two failures the baseline did not: `test_mysql_backend.py::test_no_sql_uses_a_mysql_reserved_word_unquoted` (two new verification tools wrote `SELECT signal …`; `signal` is a MySQL reserved word — fixed 17:19 IST by quoting it, the test file then passed: 54 passed, 22 skipped) and `test_ui_fixes_v2.py::test_order_paths_are_untouched` (expected while uncommitted, below). The final run is after that fix.

**New failures vs the baseline: 1**: `tests/test_ui_fixes_v2.py::test_order_paths_are_untouched`.
Failures the patch removed: 0.

Every failure in both runs is pre-existing and environmental: 27 auto-pick / session tests read the real clock and wait after the 14:45 IST no-new-entry cut-off ("no new swing entry after 14:45 IST"; CAP-16), 1 feed-state test is time-of-day dependent, and 3 `test_w39_perf_budget` tests need local sockets the sandbox refuses. 
`tests/test_ui_fixes_v2.py::test_order_paths_are_untouched` requires nothing under `orders/` to be uncommitted; it fails while this branch is uncommitted and passes once committed (its allow-list now includes `orders/capital_plan.py`).

Failed in the patched run:

* `tests/test_autopick.py::test_a_free_session_queues_the_top_buy_signal`
* `tests/test_autopick.py::test_a_pick_still_waiting_next_day_is_cancelled_and_picked_again`
* `tests/test_autopick.py::test_dry_run_queues_nothing`
* `tests/test_autopick.py::test_hold_stocks_are_not_picked_while_the_fallback_is_off`
* `tests/test_autopick.py::test_old_scores_and_a_signal_that_already_ran_are_not_picked`
* `tests/test_autopick.py::test_paused_manual_or_no_signal_sessions_wait_and_say_why`
* `tests/test_autopick.py::test_the_cycle_rotates_into_the_next_signal_with_all_the_money`
* `tests/test_autopick.py::test_the_fallback_takes_hold_stocks_after_the_buy_signals_by_score`
* `tests/test_autopick.py::test_the_pnl_page_shows_what_auto_pick_would_do`
* `tests/test_autopick.py::test_two_picks_left_from_yesterday_are_both_cancelled_without_a_lock`
* `tests/test_autopick.py::test_two_sessions_never_pick_the_same_stock`
* `tests/test_autopick.py::test_without_the_auto_send_switch_a_pick_asks_first`
* `tests/test_dhan_realtime_feeds.py::test_chain_tick_states_off_idle_and_live`
* `tests/test_full_review_2026_11_03.py::test_board_gives_each_row_its_rank_and_says_how_many_are_held_queued_or_planned`
* `tests/test_full_review_2026_11_03.py::test_pages_render_the_kpi_strip_the_session_grid_and_the_stamp`
* `tests/test_kgf_gaps_2026_11_16.py::test_a_stop_leg_fires_while_the_gate_is_closed`
* `tests/test_kite_formula_fix_2026_11_16.py::test_a_pick_is_a_tick_rounded_limit_on_the_lower_of_ltp_and_signal_close`
* `tests/test_post_market_rca.py::test_auto_pick_passes_over_a_stock_a_guard_refuses`
* `tests/test_post_market_rca.py::test_build_run_and_latest_on_a_real_session_trade`
* `tests/test_post_market_rca.py::test_rca_page_and_api`
* `tests/test_rca13_monitor_lock.py::test_a_pass_locked_out_by_another_writer_queues_its_pick_on_the_retry`
* `tests/test_score_audit_2026_11_03.py::test_board_has_ten_rows_and_says_ten_of_ten`
* `tests/test_score_audit_2026_11_03.py::test_board_says_why_when_fewer_than_ten_are_eligible`
* `tests/test_score_audit_2026_11_03.py::test_buy_signals_come_first_then_rank_fill`
* `tests/test_score_audit_2026_11_03.py::test_ten_sessions_are_filled_when_ten_eligible_stocks_exist_and_no_buy_signal`
* `tests/test_ui_fixes_v2.py::test_order_paths_are_untouched`
* `tests/test_verify_fixes_2026_11_04.py::test_autopick_waits_when_the_days_buy_count_is_used`
* `tests/test_verify_fixes_2026_11_04.py::test_command_center_pending_rows_carry_shares_and_value`
* `tests/test_verify_fixes_2026_11_04.py::test_the_wait_reason_says_rank_fill_when_its_stocks_are_taken`
* `tests/test_w39_perf_budget.py::test_block_network_refuses_outside_hosts_and_keeps_loopback`
* `tests/test_w39_perf_budget.py::test_cli_exit_codes`
* `tests/test_w39_perf_budget.py::test_server_mode_over_real_http`

## 2. New tests (`tests/test_capital_plan_2026_11_16.py`, 14) and the session test files

| Test | Result | Covers |
|---|---|---|
| test_the_plan_tops_up_each_running_session_equally_and_keeps_the_buffer | PASS | CAP-04 plan math, guardrails |
| test_apply_moves_the_cash_without_counting_it_as_profit | PASS | CAP-04 apply, CAP-03 P&L, idempotent, event |
| test_the_next_buy_is_sized_from_the_topped_up_cash | PASS | CAP-04 sizing |
| test_a_transfer_in_never_takes_more_than_the_cash_no_session_owns | PASS | CAP-01 hard limit + buffer |
| test_no_session_above_max_session_pct | PASS | CAP-01 per-session cap |
| test_a_pool_restart_cannot_over_allocate_once_the_plan_is_set | PASS | CAP-01 start_pool |
| test_a_new_session_defaults_to_the_headroom_above_the_buffer | PASS | CAP-01 add_session |
| test_spare_cash_shows_an_over_allocation | PASS | CAP-01 over_allocated |
| test_live_has_no_plan_and_refuses_to_apply | PASS | PAPER only |
| test_occurrences_start_from_the_amount_after_a_transfer | PASS | CAP-03 occurrences |
| test_the_plan_api_and_the_sessions_dialog | PASS | API, token, dialog |
| test_a_lock_written_under_the_old_host_name_is_still_ours | PASS | CAP-02 |
| test_another_live_runner_still_holds_its_lock | PASS | CAP-02 no regression |
| test_no_price_for_an_open_stop_for_two_minutes_is_alerted_once | PASS | CAP-05 |

Session files run together (test_capital_plan, test_session_pools, test_spare_cash_session, test_session_cycles): **76 passed** at ≈17:04 IST.

## 3. Acceptance on the Test and Prod servers (HTTP, `tests/verification/capital_acceptance.py`)

| Test name | Test result | Prod result | IST time | Notes |
|---|---|---|---|---|
| setup: ten Rs 25,000 pools on the fresh DB | PASS | n/a (Prod has the live sessions) | 17:09:01 IST | HTTP 200 |
| CP-01 GET /api/v2/capital/plan | PASS | PASS | 17:09:02 IST | Test: pool Rs 500,000.00, spare Rs 250,000.00, buffer Rs 50,000.00, per session Rs 20,000 · Prod: pool Rs 509,598.94, spare Rs 254,865.24, buffer Rs 50,959.89, per session Rs 20,000 |
| CP-02 plan keeps >= 10 % buffer | PASS | PASS | 17:09:02 IST | Test: spare after 10.0 % of pool · Prod: spare after 10.77 % of pool |
| CP-03 /sessions shows 'Top up sessions…' dialog | PASS | PASS | 17:09:03 IST | HTTP 200 |
| CP-04 apply without the token is refused | PASS | PASS | 17:09:03 IST | HTTP 401 |
| CP-05 apply without ack is refused | PASS | PASS | 17:09:03 IST | HTTP 400: {'error': 'the capital plan moves paper cash into the sessions: confirm with ack |
| CP-06 apply moves the planned top-ups | PASS | PASS | 17:09:03 IST | HTTP 200: moved Rs 200,000.00 to 10 sessions |
| CP-07 session P&L unchanged by the top-up (CAP-03) | PASS | PASS | 17:09:03 IST | Test: 10 of 10 unchanged; S1 pnl 0.0 -> 0.0, capital 45000.0 · Prod: 10 of 10 unchanged; S1 pnl 902.87 -> 902.87, capital 45000.0 |
| CP-08 each session's capital rose by the top-up | PASS | PASS | 17:09:03 IST | S1 45,000, S2 45,000, S3 45,000 |
| CP-09 spare cash after = plan's spare after; no over-allocation | PASS | PASS | 17:09:03 IST | Test: spare Rs 50,000.00, over_allocated 0.0 · Prod: spare Rs 54,865.24, over_allocated 0.0 |
| CP-10 a transfer into the cash buffer is refused | PASS | PASS | 17:09:03 IST | Test: HTTP 400: {'error': 'This transfer: Rs 10,000.00 would leave less than the 10 % cash buffer (Rs 50,000.00 of t · Prod: HTTP 400: {'error': 'This transfer: Rs 10,000.00 would leave less than the 10 % cash buffer (Rs 50,959.89 |
| CP-11 a new session cannot take the buffer | PASS | PASS | 17:09:03 IST | Test: HTTP 400: {'error': 'there is no paper cash outside the sessions above the cash buffer'} · Prod: HTTP 400: {'error': 'min_buy_value Rs 12,000 is more than the Rs 3,905 capital'} |
| CP-12 a second apply moves nothing | PASS | PASS | 17:09:03 IST | HTTP 200: moved 0 |
| CP-13 accounting totals: net/actual P&L unchanged, capital up by the top-up | PASS | PASS | 17:09:03 IST | Test: net 0.0 -> 0.0; actual 0.0 -> 0.0; equity 250000.0 -> 450000.0 · Prod: net 4734.54 -> 4734.54; actual 1412.7 -> 1412.7; equity 254733.7 -> 454733.7 |
| CP-14 S1's activity shows the transfer | PASS | PASS | 17:09:03 IST | HTTP 200 |
| CP-15 GET /home after the top-up | PASS | PASS | 17:09:05 IST | HTTP 200 |
| CP-15 GET /sessions after the top-up | PASS | PASS | 17:09:05 IST | HTTP 200 |
| CP-15 GET /command/pnl after the top-up | PASS | PASS | 17:09:05 IST | HTTP 200 |
| CP-15 GET /accounting?tab=transfers after the top-up | PASS | PASS | 17:09:05 IST | HTTP 200 |
| CP-15 GET /api/v2/home after the top-up | PASS | PASS | 17:09:05 IST | HTTP 200 |
| CP-16 the top-up button is disabled once applied | PASS | PASS | 17:09:05 IST |  |

Test: 21/21 passed · Prod: 20/20 passed (second run on the final code, fresh databases).

## 4. Formula and signal validation (Prod data; the Test server has no market data)

| Check | Prod result | IST time | Notes |
|---|---|---|---|
| Composite scores recomputed from components (9 indices × 499) | PASS 4,491/4,491 | 16:55:34 | max diff 0.005 |
| Indicators recomputed from raw prices (10 stocks × 32 fields) | PASS 311/320; 9 OBV explained | 16:55:34 | OBV = `ta` convention, exact match |
| Coverage of the 16 Nov scores / indicators | PASS except SPI/FS 0 % (switch off) | 16:55:34 | CAP-08 |
| Dhan vs NSE (13 Nov full day, 16 Nov opens) | PASS O/H/L identical; close differs by definition | 16:55:34 | CAP-18 |
| Entry audit (4 picks) | PASS | ≈16:57 (query) | rules met; no trade without a signal |
| Exit audit (4 exits, 8 open) | 3 on time, 1 late (outage) | ≈16:57 (query) | CAP-05 |

## 5. UI evidence

`docs/testing/screenshots/capital-2026-11-16/`: the dialog (Test), the Sessions page after the top-up at 1440 px and 375 px (Prod; no horizontal scroll at 375 px), and the Test page after a top-up through the UI (unticked submit refused with "Tick the box to confirm.").

## 6. Mobile

Android `./gradlew assembleDebug --offline`: **built 17:10:53 IST**, `app-debug.apk` 5.6 MB, versionCode 11 / 2026.11.16.2. iOS: **not built** — no Xcode on the Mac (needs the owner's Apple ID; Xcode 16.2 for macOS 14.6).

## 7. Live deploy checks (the Mac, owner yes 19:19 IST)

| Check | Live result | IST time | Notes |
|---|---|---|---|
| Shared folder clean, no other ATIP session running | PASS | 19:19:12 | `git status` no tracked changes; session list idle |
| New tests + order-path guard on the committed branch | PASS 36/36 | ≈19:20 | `test_capital_plan_2026_11_16.py`, `test_order_paths_are_untouched`, `test_session_pools.py` |
| Pre-release backup | PASS | 19:21:07–19:21:15 | `atip-pre-release-20261116-192107.db`, 814 MB |
| Restart (launchd) | PASS | 19:21:39 → up 19:23:10 | pid 48592, master `e3ed12a`; leader lease `smoru:48592`; no error logged since |
| Plan (dry run) matches the Prod copy | PASS | 19:23:10 | ₹20,000 × 10, buffer ₹54,865.24 (10.77 %), allocated 49.99 % → 89.23 % |
| Top-up applied | PASS | 19:23:25 | session_transfers 1–10 (VIRTUAL), event CAPITAL_PLAN 239, `capital_plan:last` saved |
| Session capital / loss limit | PASS | 19:24 | each ₹45,000 / ₹2,700; paper cash unchanged ₹3,27,564.54; over_allocated ₹0 |
| P&L is not inflated (CAP-03 live) | PASS | 19:25 | ledger total capital ₹4,50,000, equity ₹4,54,733.70, P&L +₹4,733.70; no page shows ₹2,04,733 (/, /sessions, /accounting, /command/pnl, /reports, /api/v2/sessions) |
| Sessions page | PASS | 19:26 | "+₹2,00,000.00 moved in · capital ₹4,50,000.00"; screenshot `screenshots/capital-2026-11-16/live_sessions_after_topup_1923IST.jpg` |
