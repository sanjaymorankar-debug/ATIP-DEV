# Testing report: full code review, scores, backtest, design system (3 Nov 2026, prompt 10:54 PM IST)

Branch `claude/full-review-2026-11-03`. Every time below is IST (UTC + 5:30).

## 1. Where it ran

ATIP has no hosted Test or Prod server. The convention of the 26 Oct – 3 Nov reports is kept: local instances run from a
scratch folder, so `config.json` and the vault are not there and no credentials or broker reach them. The live Mac was
not touched until the deploy (§6).

| "Server" | What it really was | Started |
|---|---|---|
| **Before** | master `29b488e`, exported to `.ux_build/full-review-2026-11-03/baseline`, `ATIP_ENV=staging`, on a copy of the live `atip.db` taken at 23:13 | 23:14 |
| **Test** | this branch, `ATIP_ENV=test`, a fresh database (`main.py --init`), port 8802 | 23:46 |
| **Prod** | this branch, `ATIP_ENV=staging`, a SQLite backup of the live `atip.db` (23:13), port 8803 | 23:38 |

The market was closed (stop window 21:00–06:00), so live ticks were simulated in the page (§4). The first live session
on the new code is the next trading day, Wed 4 Nov 2026, 09:15.

## 2. Automated tests

| Run | When | Result |
|---|---|---|
| Untouched baseline (master `29b488e`) | 23:48–23:57 | 2,494 passed, 65 skipped, **5 failed** |
| This branch, before commit | 23:48–23:57 | 2,521 passed, 65 skipped, **5 failed**: the same 5 as the baseline |
| This branch, committed (`d632a53`) | 23:58–00:07 | **2,523 passed, 65 skipped, 3 failed** (sandbox sockets only) |

The 5 failures are the same on the untouched code:

| Test | Why |
|---|---|
| 3 × `test_w39_perf_budget` | the sandbox refuses local sockets (pass outside it: 14 / 14 on 3 Nov) |
| `test_mysql_backend::test_no_sql_uses_a_mysql_reserved_word_unquoted` | `scores/validation.py:166` (the 9:23 PM session) aliased `` `signal` AS signal ``. **Fixed here** (`` AS `signal` ``); the file passes |
| `test_ui_fixes_v2::test_order_paths_are_untouched` | the guard fails on an uncommitted tree; it allows this review's two order files and passes once committed |

New: `tests/test_full_review_2026_11_03.py`, **27 tests, all pass**. Three existing tests were adjusted for intended
behaviour, and nothing was weakened:
- `test_ux_console::test_preview_rupee_risk_reward_and_checklist` seeds a BUY signal (the ticket now buys only on one);
- `test_ui_fixes_v2::test_home_pipeline_score_vix_gift` slices after the KPI strip's "Market health";
- `test_order_paths_are_untouched` allows `orders/rules.py` and `orders/broker.py`.

### 2.1 Final run on the committed tree
Run 23:58:48 → 00:07:29 IST on commit `d632a53` (8 min 39 s): **2,523 passed, 65 skipped, 3 failed**, 0 errors. The 3 are
the `test_w39_perf_budget` socket tests the sandbox refuses; they fail the same way on the untouched baseline. The MySQL
quoting test and the order-path guard now pass.

### Test cases (new file)

| # | Case | Pass |
|---|---|---|
| 1 | `execute_rule` refuses a CANCELLED rule; nothing reaches the paper broker | ✅ |
| 2 | One pass: STOP and TARGET both true → exactly one SELL of 10; TARGET CANCELLED | ✅ |
| 3 | A failed STOP stays ACTIVE ("retrying"); FAILED once the shares are gone | ✅ |
| 4 | A failed entry still fails | ✅ |
| 5 | Kill switch on: the stop is HELD_HALTED, stays ACTIVE; off: it fires and the book is flat | ✅ |
| 6 | An expired confirmation cannot be confirmed (EXPIRED, no order) | ✅ |
| 7 | A LIVE buy with no price is refused | ✅ |
| 8 | A MARKET order with no live quote is valued at the last close (10 × ₹50 = ₹500) | ✅ |
| 9 | `POST /api/orders` with role STOP → 400 | ✅ |
| 10 | Board: 10 rows, ranks 1–10, counts 0 / 0 / 10 / 0, "10 of 10 sessions filled", detail line | ✅ |
| 11 | Board rank: equal ATIP → CRI ascending (SAFE 10 → MID 30 → RISKY 60) | ✅ |
| 12 | Auto-pick waits a day after a FAILED entry, with the reason | ✅ |
| 13 | `determine_signal`: MH 50 → BUY, MH 0 → not BUY; MRI None does not raise | ✅ |
| 14 | `last_price_date` (Sunday → Friday); scoring a day with no data = NO_DATA, 0 rows | ✅ |
| 15 | Technical score: unknown 200-DMA leaves Trend out | ✅ |
| 16 | Archive basis: raw 1,534.80 × 1,378,044 → 767.40 × 2,756,088; repair fixes the raw row only; second run [] | ✅ |
| 17 | FII/DII write keeps PCR 0.87 | ✅ |
| 18 | TOD alert with NULL TOD / ATIP / MH: "—", "no close yet", key `tod:2026-11-03` | ✅ |
| 19 | Calendar: 3 Nov 2026 None; 5 Dec 2026 "2027"; 26 Jan 2027 "every weekday …" | ✅ |
| 20 | Dark default in the head script, atip.js and the CSS note | ✅ |
| 21 | Feed label `updated` = "10:15:07 IST" | ✅ |
| 22 | Badges: CRI 20 strong / 80 weak, CRI↓, formula in the tooltip; VPI None "—"; 7 formulas; legend states CRI < 60 | ✅ |
| 23 | Every send path calls the dialog ("/", Command Center, ticket, baskets) | ✅ |
| 24 | UI smoke: Home KPI strip + 10 cards + stamp; Sessions 10 cards, badges, rank, Table view; `/`, `/command`, `/reports`, `/accounting`, `/settings` 200 | ✅ |
| 25 | Ticket preview: HOLD stock → blocking "Buy only on a BUY signal"; BUY stock → ok | ✅ |
| 26 | Tie-averaged ranks [3,1,1,2] → [3, 0.5, 0.5, 2]; NEUTRAL = 0 | ✅ |
| 27 | `max_hold_exit` with held_sessions None → False | ✅ |

The 9:23 PM session's 25 hand-calculated formula tests (VPI, MRI, RRI, ZPI, CRI, ACS, ATIP, RS vs Nifty, ten
sessions, CRI ascending) still pass.

## 3. Backtest metrics, before vs after

The full tables are in `docs/reports/2026-11-03-fullreview/BACKTEST_VALIDATION_REPORT.md`. Out of sample, last 30 %:

| Score | Verdict before → after | OOS 5d IC before → after | OOS 5d top-decile hit % / expectancy (after) |
|---|---|---|---|
| ATIP | BROKEN → BROKEN (improving) | −0.009 → +0.017 | 43.8 % / −0.45 % |
| VPI | BROKEN → BROKEN | +0.006 → +0.006 | 41.8 % / −0.69 % |
| MRI | INVERTED → WEAK | −0.031 → +0.018 | 43.1 % / −0.38 % |
| RRI | BROKEN → BROKEN | −0.003 → −0.003 | 42.1 % / −0.77 % |
| ZPI | BROKEN → WEAK | −0.012 → +0.020 | 42.8 % / −0.38 % |
| CRI↓ | WORKING → WORKING | −0.031 → −0.031 (1d −0.045, t −4.4) | 41.3 % / −0.50 % |
| ACS | BROKEN → WEAK | +0.006 → +0.020 | 42.9 % / −0.45 % |

Tonight's re-run on the 23:13 snapshot equals the committed after-run (0 differences). Variants tested and not adopted:
VPI with ATR flipped, VPI with ATR dropped, and cross-sectional percentile VPI / RRI (reasons in the score audit, §4).

## 4. UI tests (built-in browser, Chromium)

| Check | Test | Prod | Result |
|---|---|---|---|
| Every tab and API (26 routes: `/`, `/home`, `/sessions`, `/sessions/S1`, `/command`, `/watchlist`, `/accounting`, `/reports`, `/settings`, `/more`, `/market`, `/research`, `/stock/…`, `/trading`, `/execution-lab`, `/baskets`, `/global-asia`, `/ops`, `/m`, `/m?tab=live`, `/command/signals`, `/command/journal`, 4 APIs) | 200 (an unknown `/stock/ACME` 404, the same as the baseline) | 200 | ✅ |
| Console errors on Home, Sessions, Scores, Command, Baskets, S1 | none | none | ✅ |
| Dark by default; light via `?theme=light` / the button | ✅ | ✅ | ✅ |
| Home KPI strip (market health 58 Neutral, Nifty 23,810.85 ▲ +1.58 %, 10 of 10, ₹0.00 Flat, Updated 16:45:01 IST) | empty state "—" | ✅ | ✅ |
| Ten-session cards: chip, rank #1/499 …, badges, levels, P&L, wins; Table view | "0 of 10 … no starting amount" | 10 cards | ✅ |
| 390 px: no sideways scroll (scrollWidth 390 = viewport) on Home and Sessions | — | ✅ | ✅ |
| 1,440 px with the watchlist open: no overflow (1,425 px page) | — | ✅ (found 1,489 px first; fixed) | ✅ |
| Order dialog: symbol, quantity, price, value, stop −2.00 %, target +3.00 %, PAPER; Esc → no | — | ✅ | ✅ |
| Ticket on a HOLD stock: "✗ Buy only on a BUY signal", Save disabled | — | ✅ | ✅ |
| CMP flash: ₹1,524.00 → ₹1,530.50, class `ax-tick-up`, "▲ +17.80 (+1.18 %)", stamp "Updated 10:15:42 IST" (simulated tick) | — | ✅ | ✅ |
| WCAG AA: 32 token pairs, both themes (lowest 4.81:1) | — | — | ✅ |

Screenshots: `docs/testing/screenshots/full-review-2026-11-03/` (`before_*` 5, `after_*` 10).

## 5. Open issues

| # | Issue | Owner / next step |
|---|---|---|
| 1 | VPI's ATR reading is inverted vs returns; flipping it needs a BUY-gate re-set | Owner decision (evidence in the backtest report) |
| 2 | NSE 2027 holidays are not listed (the alarm fires from December) | Add NSE's circular in December |
| 3 | Evening jobs inside the 21:00 stop window run only on the 06:00 replay (P-M2) | Owner: move the slots (scheduler is protected) |
| 4 | The stale-quote guard uses the fetch time (`data/dhan.py`, protected) (O-M6) | Owner-approved Dhan change |
| 5 | Shared paper book: a flatten / W4 exit can sell a pool's shares (O-M1); no start-up leg rebuild (O-M8) | Next order-engine change |
| 6 | `scores/backtest.py` / `backtest_history.py` cross the 5-week bhavcopy hole; `trail_atr_mult` was calibrated through it (Q-H5) | Guard + re-calibrate |
| 7 | ACS HistoricalAccuracy rewards quiet stocks (Q-M2); ATIP–ACS ρ 0.87, ATIP–RRI 0.85 | ACS redesign |
| 8 | Mobile builds: no Android SDK / Xcode on this Mac | Build in Android Studio / Xcode |
| 9 | Safari/WebKit not run (Playwright's WebKit hangs on this Mac) | Unchanged since 3 Nov |
| 10 | S1's target is 4 %, S2–S10 3 % (an edit on 3 Nov) | Owner to confirm |
| 11 | Rank-fill now trades HOLD stocks in PAPER at the open (9:23 PM session's change, live from this deploy); the validation shows negative expectancy | Owner: `session_settings.autopick_rank_fill = OFF` to stop it |

## 6. Deployment on the Mac
Owner: "once this is done deploy code on mac". The market was closed and ATIP was in its 21:00–06:00 stop window
(read-only, no orders), and no other ATIP session was running.

| Step | IST (4 Nov 2026) | Result |
|---|---|---|
| Live folder check | 00:07 | `/Users/agtci/ATIP` on master `29b488e`, no tracked change; no other ATIP session running |
| Pre-release backup | 00:07:58 | `atip_data/backups/atip-pre-release-20261104-000758.db`, 501,866,496 bytes, sha256 `f9f7d4cc…7cbd6` |
| Merge into master | 00:08:11 | `0dbecdd` (branch `claude/full-review-2026-11-03`, not pushed) |
| Data repair | 00:08:29 | `python -m data.ema_gap --repair --apply`: TDPOWERSYS 165 rows; a second run finds nothing; 214 rows, no day-to-day jump > 25 % |
| Restart (`launchctl kickstart -k gui/$UID/com.atip.platform`) | 00:08:43 | pid 8697 → 36085; "Every recent session through 2026-11-03 is scored"; order monitor started; no error |
| Live check | 00:09 | `/home`, `/sessions`, `/`, `/command`, the board and shell APIs 200; dark; PAPER; 5 KPI tiles, 10 cards; board "10 of 10 sessions filled — 0 holding · 0 queued · 10 to buy at the next pick" |
| Defect found live | 00:09 | in the stop window the compact chip "→ Next pick · after the stop window" widened Home by 8 px (1,448 px in 1,440) |
| Fix, tests, merge | 00:11:10 | `e0f653e` → master `fac38d3`; UI test files 206 / 206 |
| Restart | 00:11:17 | pid 36453; no error; Home 1,425 px in 1,440, Sessions 390 px in 390; screenshot `live_mac_home_dark_1440_after_deploy.jpg` |

**Rollback:** to undo the code, `git revert -m 1 fac38d3 0dbecdd` and restart. To undo the data repair, stop ATIP and run
`python -m ops restore atip-pre-release-20261104-000758.db`; the repair touched only those 165 TDPOWERSYS rows.

**What the next session does on this code (Wed 4 Nov, 09:15 IST):** the ten PAPER sessions auto-pick at the open. On
3 Nov's scores there is no BUY signal, so all ten rank-fill HOLD stocks (PVRINOX, BALRAMCHIN, LODHA, …), as the
9:23 PM session built and this board now shows. To keep them empty instead: `session_settings.autopick_rank_fill = OFF`.
