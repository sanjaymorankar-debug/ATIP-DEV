# Testing report: independent re-verification + Phase 3 fixes (5 Nov 2026; prompt 4 Nov 06:00 PM IST, run 5 Nov from 01:05 PM IST)

Every time is IST (UTC + 5:30). Verification report: `docs/reports/2026-11-05-verification/VERIFICATION_REPORT.md`.

## 1. Where it ran

ATIP has no hosted Test or Prod server; the convention of the 26 Oct – 4 Nov reports is kept: local instances from a scratch
folder with a **credential-free** `config.json` (PAPER, the live risk limits, no Dhan / Telegram / broker keys), started with
`ATIP_OPERATING_SCHEDULE=off`, and — after the fixes — `ATIP_ORDER_MONITOR=off`, so a copy never auto-picks or fires
triggers on its own.

| "Server" | What it really was | Code | Started |
|---|---|---|---|
| **Live Mac** (read only) | the running ATIP (`atip_data/atip.log` live; quotes every 30 s); `GET /api/sessions/board` → 200 at 13:17:50 | master `0ab3de9` | — |
| **Test** | `ATIP_ENV=test`, fresh DB (`main.py --init`, 13:29), port 8832 | `0ab3de9`, then the fixed branch `8bbc735` | 13:29, restarted 13:52 |
| **Prod** | `ATIP_ENV=staging`, consistent SQLite backup of the live `atip.db` taken 13:15:37 (534,003,712 B), port 8833; `max_orders_per_day` 10, then 20 (owner) | same; 4 Nov re-scored on the fixed code at 13:52:20–13:52:30 | 13:29, restarted 13:52 |

The market was open during the pass (13:05 onwards). Live ticks did not reach the copies (no keys), so the feed shows stale
— which is how the stale-feed warning was seen live.

## 2. Automated tests

| Run | When (IST) | Result |
|---|---|---|
| Verified build (master `0ab3de9`), worktree, sandbox | 13:14:11 → 13:24:39 (10 min 28 s) | **4 failed + 1 error**: 3 × `test_w39_perf_budget` (sandbox refuses local sockets, as on 3–4 Nov), `test_the_wait_reason_says_rank_fill_when_its_stocks_are_taken` and the `/settings` structural-audit error — both pass when run alone |
| Same build, second run | 13:25 → 13:37:37 | **a different auto-pick test failed** (`test_autopick_waits_when_the_days_buy_count_is_used`: "a buy of ACME is queued") + 2 `test_w39_mcp` errors + the 3 socket tests. Root cause VF5-07: the order monitor a TestClient starts runs auto-pick every 30 s in market hours into the current test's DB |
| Fixed branch `8bbc735`, worktree, sandbox, **market hours** | 13:52:03 → 14:01:38 (9 min 33 s) | **2,543 passed, 65 skipped, 3 failed**: only the 3 socket tests (14/14 `test_w39_perf_budget` pass outside the sandbox at 14:01:44). The auto-pick tests that failed on master pass; `test_order_paths_are_untouched` passes on the committed tree |

New tests: `tests/test_verify_fixes_2026_11_05.py` (8), `test_order_monitor_off_starts_none`. Mutation on a scratch copy
(restored, `diff -rq` clean): VPI V reverted → 3 tests fail; CRI ×12 → 2; board TARGET 9 → 4; OCO pending sibling → 1;
`FILL_TARGET` 9 → none (never read; removed).

## 3. Checks repeated on both servers

| Check | Test (fresh DB) | Prod (live copy) | Result |
|---|---|---|---|
| V1 sessions API, before and after the fixes | `/api/sessions` 10, `/api/sessions/board` 10 rows, "0 of 10 … no starting amount: set one up on /sessions" | 10 + 10 rows, "10 of 10 sessions filled (10 rank-filled (no BUY signal free))", 10 distinct | PASS |
| V1 cards | 10 cards (waiting) | 10 cards: symbol, #rank/499, rationale, 7 badges, entry/target/stop, status, P&L, wins | PASS |
| V2 spot checks | — | 4 Nov re-scored on the fixed code: 498/498 within 0.01 of the independent recompute (new spec), signals equal; ICICILIFE dropped (VF-03) | PASS |
| VF5-01 effect | — | 4 Nov: BUY 1 → 0 (PVRINOX VPI 65.21 < 66.5), WAIT 53 → 33; BUY legend "VPI ≥ 66.5 … VPI ≥ 76.5" | PASS |
| V4 sweep (21 pages × 2 themes × 2 widths) | 84 runs: 200, 0 JS errors, 0 axe, 0 sideways scroll | 84 runs: same | PASS (before and after) |
| V4 keyboard | /home 43/43, /sessions 57/57, "/" 69/69 | 143/143, 77/77, 122/122 | PASS |
| V4 order dialog, Cancel | — | "/" Buy PVRINOX 6 sh: symbol, qty, ₹1,577.30, ₹9,463.80, stop ₹1,545.00 (−2.05 %); Not now → `order_log` 46 → 46, `order_rules` 70 → 70 | PASS |
| V5 suite | — | — | see §2 |

## 4. Deployed version on each

| Where | Version | Matches the verified build? |
|---|---|---|
| Test (:8832) | branch `claude/verify-2026-11-05` @ `8bbc735` (PYTHONPATH = the worktree) | yes |
| Prod (:8833) | same | yes |
| Live Mac (:8000) | master `0ab3de9` | **no — the fixes are not deployed** (owner approval: it restarts the trading app; see the report §5) |

## 5. Deploy steps (for the owner's go-ahead)

1. After 15:30 IST: merge `claude/verify-2026-11-05` into master in `/Users/agtci/ATIP`.
2. In `atip_data/config.json` set `risk_limits.max_orders_per_day` to `20`.
3. Restart ATIP the usual way (launchd `com.atip.platform`); check `GET /home` → 200 and the log line "Order-rules monitor started".
4. Before 16:49 IST if tonight's scores should use the calm VPI; after the 16:49 run, check `ai_scores` for today has no ICICILIFE
   row and the BUY legend says 66.5.
5. Rollback: `git revert` the merge, restore `max_orders_per_day` 10, restart.
