# CHANGES — Phase 3 fixes of the independent verification (4 Nov 2026, prompt 06:00 PM IST)

Branch `claude/verify-2026-11-04` (worktree `.claude/worktrees/verify-2026-11-04`), cut from master `369f6d6` (the code
the live Mac has run since 00:11 IST, = `fac38d3` plus tracker/report commits). This is a delta patch: every file is
changed in place. Unzip at the project root (`~/ATIP`). Defect IDs (VF-xx) refer to the defect log in
[VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) §3. **Committed** (`09eeeaa` code + tests, `ac7b5c8` apps, `3fc44af` docs)
and **merged into master** at 22:55 IST (`7887351`). **Live on the Mac since 23:45:47 IST** (master `f8002be`, pid 42595).

## Scores (V2)

| File | Change |
|---|---|
| `scores/formulas.py` | **VF-02** `store_components` replaces a stock's rows for the day (and only the captured indices of `__MARKET__`) instead of upserting over them. A re-score kept components it no longer used: 944 "Support" rows of stocks with no 200-day average on the live DB, so the score breakdown showed parts that did not count. |
| `scores/engine.py` | **VF-03** a stock with no indicator row for the day (under 20 sessions, e.g. ICICILIFE) is not scored, and a re-score removes its earlier row; it used to get MRI 50 from neutral news and a "safe" CRI 23 from stand-ins. `result["no_indicators"]` lists them. **VF-20** MSI's market news and `get_ns`'s 7-day fallback are bounded above by the day: a re-score of a past day read later news (the extended look-ahead test changed ATIP for 499 of 499 stocks on the old code, 0 of 498 now). |

## Sessions and orders (V1, V4)

| File | Change |
|---|---|
| `orders/autopick.py` | **VF-04** a free session waits ("today's 10 buys are used (risk limit max_orders_per_day)") instead of queuing a buy the risk check will refuse; on 4 Nov three sessions queued NEULANDLAB after their profitable exits and all three FAILED. The limit itself is unchanged. **VF-07** the wait reason names rank-fill when its stocks were looked at and all taken. |
| `orders/rules.py` | **VF-05** `with_estimates(rows)`: est_qty / est_value of pending rules, sized read-only the way `execute_rule` sizes them (moved from the `/api/orders/pending` route so both lists use it). |
| `dashboard/server.py` | `/api/orders/pending` uses `rules.with_estimates`. **VF-27** the scores table's headers sort from the keyboard (Enter / Space), carry `aria-sort`, and stay visible when scrolling (sticky). **VF-28** "Updated HH:MM:SS IST" in the page's own header (the top bar hides its stamp under 1,180 px, i.e. at 1,440 with the rail). |
| `dashboard/command_routes.py` | **VF-05** Command Center's pending list uses `rules.with_estimates`, so "Confirm & send" shows ≈ shares and the order value (it showed "all of the session's cash" and "—"). |

## Design system and pages (V4)

| File | Change |
|---|---|
| `dashboard/static/atip/atip.css` | **VF-22** new token `--ax-neg-ink` (text on a `--ax-neg` fill: #FFFFFF light, #2A0A0C dark); the unread badge and the error toast use it — white on #FCA5A5 was 1.89:1 on every dark page, now 9.6:1. **VF-24** tabular figures in every table cell; `td.ax-n` right-aligned. **VF-32** the remaining applied literals in component rules moved to tokens (`--ax-on-fill`, `--ax-live-ring`). |
| `dashboard/static/atip/atip.js` | **VF-23** table sort reads "₹1,538.57", "−₹253.34", "#118", "+3.5 %" as numbers and orders text naturally (S2 before S10). **VF-24** `AX.alignNumbers()` tags every cell that holds only a number `.ax-n`, again whenever a table is redrawn (one MutationObserver, throttled to a frame). |
| `dashboard/stock_view.py` | **VF-26** the stock panel (`role=dialog`, `aria-modal`) moves focus to its Close button, keeps Tab inside, closes on Esc and returns focus. |
| `dashboard/execution_page.py` | **VF-25** P&L and return cells and the "today" KPIs say their direction in the text (▲ +, ▼ −), not by colour alone. |
| `dashboard/w38_page.py` | **VF-25** `/m` day P&L the same. |
| `dashboard/shell.py` | **VF-29** the P&L strip (it scrolls sideways at 375 px) takes keyboard focus. |

## Mobile (mandatory item 1)

| File | Change |
|---|---|
| `mobile/android/app/build.gradle.kts`, `mobile/ios/project.yml`, `mobile/ios/ATIP/WebScreen.swift` | 2026.11.04.1 (Android versionCode 8, iOS build 8). The apps are web shells, so the fixes above reach them from the server; no native code changed. |
| `mobile/README.md` | What changed on the phone. |

## Tests

| File | Change |
|---|---|
| `tests/verification/recompute_scores.py` (new) | Independent recomputation of the 7 scores from the raw tables (does not import `scores.engine`). |
| `tests/verification/test_independent_recompute.py` (new, 2 tests) | **VF-06** the post-market pipeline vs the recomputation, all 7 scores within 0.01; SAFETY = 100 − CRI. Kills the SAFETY-sign mutant that survived every earlier test. |
| `tests/verification/v1_sessions_harness.py`, `v2_invariance.py` (new) | The V1 point-in-time allocator harness and the V2 look-ahead / normalisation / NULL-EMA / composite checks (scripts, run on copies). |
| `tests/test_verify_fixes_2026_11_04.py` (new, 9 tests) | VF-02 (×2), VF-03, VF-04, VF-05, VF-07, VF-20, VF-22, VF-32. All 9 fail on the unfixed code. |
| `tests/test_dhan_realtime_feeds.py` | **VF-11** the option-chain test's clock is pinned to 3 Nov (its fake expiries are 3/10/17 Nov); it failed from 4 Nov. |
| `tests/test_ui_fixes_v2.py` | Comment only: the order-path guard notes why `orders/autopick.py` and `orders/rules.py` change (both were already allowed). |

## Docs and tracker

`docs/reports/2026-11-04-verification/*` (this file, VERIFICATION_REPORT.md, evidence/), `docs/testing/ATIP_Verification_Test_Report_2026-11-04.md`,
`ATIP_MASTER_TRACKER.xlsx` + `docs/ATIP_MASTER_TRACKER.csv` (phase 21 + corrected rows; backups `*-before-verification.*`), `PATCH_NOTES.md`.

## Follow-up (5 Nov 2026): VF-34 and VF-35, found live after the deploy

Owner: "Fix both now" (5 Nov, ~13:05 IST). Branch `claude/fix-vf34-vf35-2026-11-05`, cut from master `0ab3de9` (the live
code). Both were pre-existing (the same with the `369f6d6` CSS/JS). **Committed** (`d266ed6` code + tests, `eba5afd` docs),
**merged into master** at 17:33 IST (`fde1951`), **live on the Mac since 21:11:00 IST** (pid 55822; testing report §8).

| File | Change |
|---|---|
| `pipeline/operating_schedule.py` | **VF-34** `status()` adds `banner_short`: "Stopped till 06:00", "Resuming" or "Running" — the run pill's words when the full sentence does not fit. An added key; `banner` is unchanged. |
| `dashboard/shell.py` | **VF-34** the run pill carries both texts (`.ax-runtext`, `.ax-runshort`) and the full sentence as its title; `light_state()` passes `banner_short`. |
| `dashboard/static/atip/atip.js` | **VF-34** `paint()` writes both texts; `AX.fitTop()` runs after every status poll and whenever the bar's width changes (ResizeObserver: window, watchlist). While a pill's words are cut or the bar overflows, it drops in turn the search's "Ctrl K" hint, the feed pill's long words, the run pill's long words, folds the four icons into "⋯" (which already carries the unread count), then hides the breadcrumb's page part (the page's heading names it). |
| `dashboard/static/atip/atip.css` | **VF-34** the search keeps its own width (it shrank under its label and spilled over the PAPER pill); the run and feed pills may shrink and end in "…" as the last resort; the `ax-fit-*` steps. **VF-35** a card's "P&L +₹1,144.40" and "Wins 1/1" each stay on one line; when both do not fit, "Wins" moves under the P&L. |
| `tests/test_fix_vf34_vf35_2026_11_05.py` (new, 5 tests) | The short words per state, the shell markup, the fit order in atip.js, the CSS rules, the card footer. All 5 fail on `0ab3de9`. |

The mobile apps hide the run pill (`html.ax-app`) and load the cards from the server: no app change, no new build.

## Rollout notes

* **No schema change, no data repair.** The stale `score_components` rows and ICICILIFE's provisional rows are replaced at
  the next scoring run of each day (or `python -m scores.engine --date YYYY-MM-DD`).
* Behaviour changes: a stock with fewer than ~20 sessions of history is not scored; free sessions wait once the day's 10 buys
  are used; every table's numbers are right-aligned.
* **Not changed (owner decisions):** the VPI / RRI model (VF-01), "momentum" = ATIP rank (VF-08), `max_orders_per_day` = 10 (VF-04 note),
  session names (VF-18).
