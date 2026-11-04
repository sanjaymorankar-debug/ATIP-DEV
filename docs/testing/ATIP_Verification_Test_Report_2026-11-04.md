# Testing report: independent verification + Phase 3 fixes (4 Nov 2026, prompt 06:00 PM IST)

Every time is IST (UTC + 5:30). Verification report: `docs/reports/2026-11-04-verification/VERIFICATION_REPORT.md`.

## 1. Where it ran

ATIP has no hosted Test or Prod server; the convention of the 26 Oct – 3 Nov reports is kept: local instances from a scratch
folder (no `config.json` keys, no vault, no broker credentials), started with `ATIP_OPERATING_SCHEDULE=off`.

| "Server" | What it really was | Code | Started |
|---|---|---|---|
| **Live Mac** (read only) | `com.atip.platform`, pid 36453, started 00:11:17 IST 4 Nov; `GET /home` → 200 at 18:04 | master `369f6d6` (= `fac38d3` code) | — |
| **Test** | `ATIP_ENV=test`, fresh DB (`main.py --init`, 18:07), port 8812 | verified build `369f6d6`, then the Phase 3 tree | 18:08, restarted 18:49 |
| **Prod** | `ATIP_ENV=staging`, SQLite backup of the live `atip.db` taken 18:03:26 (522,969,088 B), port 8813; from 18:49 a fresh copy plus `atip_data/config.json` with only `risk_limits.max_orders_per_day = 10` (the live value, as the 4 Nov order errors show) | same | 18:08, restarted 18:49 |

The market closed at 15:30; the snapshot holds the whole 4 Nov session (10 auto-pick buys at 09:15, 4 trailing-stop wins,
3 failed re-entries). Live ticks were not available, so the CMP flash and the stale stamp were forced in the page.

## 2. Automated tests

| Run | When (IST) | Result |
|---|---|---|
| Verified build (master `369f6d6`), worktree, sandbox | 18:03:42 → 18:13:03 (9 min 19 s) | 2,522 passed, 65 skipped, **4 failed**: 3 × `test_w39_perf_budget` (sandbox refuses local sockets; same on 3 Nov) + `test_dhan_chain_rows_for_the_nearest_expiries` (dated test, VF-11) |
| Phase 3 tree, first pass | 18:41:39 → 18:51:12 (9 min 32 s) | 2,531 passed, 65 skipped, 4 failed: the 3 socket tests + `test_order_paths_are_untouched` (it fails on any uncommitted tree under `orders/`; passes once committed) |
| Phase 3 tree, final | 18:58:35 → 19:07:37 (9 min 0 s) | **2,533 passed, 65 skipped, 4 failed**: the 3 socket tests (14/14 `test_w39_perf_budget` pass outside the sandbox at 19:07:50) + `test_order_paths_are_untouched` (uncommitted tree; passes once committed) |
| Master after the merge (`7887351`; same files as the branch tip `3fc44af`) | 22:55:10 → 23:04:11 (9 min 1 s) | **2,534 passed, 65 skipped, 3 failed**: only the 3 socket tests (14/14 `test_w39_perf_budget` pass outside the sandbox at 23:04:48); `test_order_paths_are_untouched` passes on the committed tree |

New tests (all pass on the Phase 3 tree; the 9 fix tests and the 2 recompute tests fail on the unfixed code):
`tests/test_verify_fixes_2026_11_04.py` (9), `tests/verification/test_independent_recompute.py` (2). Changed:
`tests/test_dhan_realtime_feeds.py` (clock pinned).

Mutation (scratch export of `369f6d6`): CRI formula killed; board limit, seeded sessions and auto-pick limit killed;
SAFETY sign survived the old suite and is killed by the new tests; `MAX_SESSIONS` is never read (equivalent mutant).
Detail: `docs/reports/2026-11-04-verification/evidence/v5/mutation.txt`.

## 3. Checks repeated on both servers

| Check | Test (fresh DB) | Prod (live snapshot) | Result |
|---|---|---|---|
| V1 sessions API | `/api/sessions/board` 10 rows, "0 of 10 … no starting amount" | 10 rows, "7 of 10 sessions filled — S2, S6, S10: today's buy of NEULANDLAB failed …" (today's real state) | PASS |
| V1 cards | 10 cards, waiting | 10 cards: symbol, #rank/499, rationale, 7 badges, entry/target/stop, P&L, wins | PASS |
| V1 allocator (harness, DB copies) | — | 10/10 distinct on 4 Nov, 29 Oct, 23 Oct, 19 Oct, before and after the fixes; shortfall "3 of 10 … — reason" | PASS |
| V2 spot checks | — | 42/42 scores within 0.01 (3 Nov), 35/35 after the fixes; look-ahead 0/498 | PASS |
| VF-04 on today's state | — | old code would queue NEULANDLAB / ADANIENT / REDINGTON (all refused at the cap); new code: "today's 10 buys are used" | PASS |
| VF-05 Command Center dialog | — | `/api/command/summary` pending RADICO: est_qty 6, est_value 25,627.50 | PASS |
| Order safety | — | "/" Confirm, Command Center, ticket: dialog with symbol, qty, price, value, stop; "Not now" and Esc: `order_log` 34 → 34, `order_rules` 47 → 47 | PASS |
| V4 sweep (168 runs per server) | 168/168 HTTP 200, 0 JS errors, 0 sideways scroll at 375, axe 0 (was 10 × scrollable-region), tabular 0 fails | 168/168 HTTP 200, 0 JS errors, 0 sideways scroll, axe 0 (was 42 × colour-contrast), tabular 0 fails (was 7,560), colour-only P&L 0 (was 25) | PASS (final run 18:58:38–19:05:06; first run 18:28–18:40 before the fixes) |
| App views (390 × 844 iOS UA, 412 × 915 Android UA, both themes) | — | 24/24: HTTP 200, theme applied, shell nav hidden (the native tab bar replaces it), no sideways scroll, 10 cards on Home and Sessions, CRI↓ on Sessions | PASS (web views); native builds BLOCKED |
| V5 suite | — | — | see §2 |

## 4. Deployed version

| Place | Version | Matches the verified build? |
|---|---|---|
| Live Mac | `369f6d6` (code = `fac38d3`), running since 00:11:17 IST | yes — it **is** the verified build; the Phase 3 fixes are **not** on it |
| Test / Prod | the Phase 3 working tree on `369f6d6` | yes (the tree the final suite ran on) |
| master | `7887351` (merge of `claude/verify-2026-11-04`, 22:55 IST) | yes — the same files as the tested tree; suite re-run on it (§2) |

The live folder `/Users/agtci/ATIP` is parked on branch `claude/live-running-2026-11-04` at `369f6d6` (the running commit),
so the merge did not put `atip.css` / `atip.js` live ahead of the Python code. At the restart: switch the folder to
master, take a pre-release backup, then kickstart.

**Not done:** restarting the live Mac, native app builds (no SDKs). Rollback is not needed: nothing on the live Mac changed.

## 5. Open issues

See the verification report §3 (VF-01, VF-08 and VF-18 are owner decisions; VF-16 needs Xcode / the Android SDK;
VF-10, 12, 13, 14, 15, 17, 21, 30, 31, 32, 33 are open with their reasons).
