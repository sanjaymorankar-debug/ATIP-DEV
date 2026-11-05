# ATIP independent verification of the 3 Nov 2026 work (prompt 4 Nov 2026, 06:00 PM IST)

**Date of report:** 4 Nov 2026, evening. All times are IST (UTC + 5:30).
**Role:** QA lead and quant reviewer. Nothing in CHANGES.md or the earlier reports was taken as true until it was reproduced.
**Build verified:** master `369f6d6` = the code the live Mac has run since 00:11:17 IST (pid 36453, started 00:11:17; its
folder's code equals `fac38d3`). **Phase 3 fixes:** branch `claude/verify-2026-11-04`, merged into master at 22:55 IST
(`7887351`); live on the Mac since 23:45:47 IST (master `f8002be`, pid 42595). **Follow-up 5 Nov:** VF-34 and VF-35 (found
live after the deploy) fixed on branch `claude/fix-vf34-vf35-2026-11-05`, merged into master at 17:33 IST (`fde1951`), live on
the Mac since 21:11:00 IST 5 Nov (pid 55822) (testing report §7, §8).
**Servers.** ATIP has no hosted servers (see the testing report §1): **Test** = this code on a fresh DB (`main.py --init`,
:8812); **Prod** = this code on a SQLite backup of the live `atip.db` taken at 18:03:26 IST (:8813). The live Mac (:8000)
was only read (one `GET /home` → 200, and the process list).
**Skills:** `share-market` loaded. `frontend-design` does not exist on this machine; the UI checks follow DESIGN.md, WCAG 2.2 AA
and axe-core 4.13.

## Verdict per task

| Task | Verdict | One line |
|---|---|---|
| Phase 1 deliverables | **PASS** (1 Low) | All 8 deliverables exist; 59/59 changed files are in the zip and equal `fac38d3`; `scores/validation.py` changed but not listed |
| V0 code-review fixes | **PASS** after fixes | Every High/Medium fix found at file:line; protected behaviour intact; re-grep found VF-20 (fixed) and Lows |
| V1 ten sessions | **PASS** (owner note) | 10 picks / 10 distinct / SQL 10 / API 10 / 10 cards on 4 dates; ranking = independent recomputation. Ranked by ATIP, not price momentum (VF-08) |
| V2 score formulas | **PASS** after fixes | 42/42 scores within 0.01; look-ahead, normalisation, NULL-EMA, composite pass; fixed VF-02, VF-03, VF-20 |
| V3 backtest | **FAIL — owner decision** | Reproduced exactly (1,557 values, 0 differences); method sound; but ATIP, VPI, RRI remain **BROKEN** (VF-01) |
| V4 UI/UX | **PASS** after fixes (open Lows) | 336 page runs; fixed dark contrast, table sort/alignment, colour-only P&L, modal focus, Command Center dialog value; hard-coded colours in legacy pages open |
| V5 tests | **PASS** after fixes | Suite green except 3 sandbox-socket tests; dated test fixed; SAFETY-sign gap closed; mutants killed |
| V6 mandatory | **PARTIAL** | Apps bumped and their web views verified; native builds impossible here (VF-16); tracker corrected; Test + Prod done |

## 1. Phase 1 — deliverable check

| ID | Check | Result | Evidence |
|---|---|---|---|
| P1-1 | Delta zip | PASS | `deliverables/2026-11-03/ATIP_full_review_delta_2026-11-03.zip` 2,368,592 B, 119 entries (59 files, `_orig/` copies, screenshots) |
| P1-2 | CHANGES.md | PASS | `docs/reports/2026-11-03-fullreview/CHANGES.md` 8,058 B |
| P1-3 | Code review report | PASS | `CODE_REVIEW_REPORT.md` 21,789 B (6 High, 24 Medium) |
| P1-4 | Score audit report | PASS | `SCORE_AUDIT_REPORT.md` 9,975 B |
| P1-5 | Backtest report | PASS | `BACKTEST_VALIDATION_REPORT.md` 17,620 B + JSON evidence |
| P1-6 | DESIGN.md + before/after screenshots | PASS | `DESIGN.md` 9,141 B; `docs/testing/screenshots/full-review-2026-11-03/` 5 before, 10 after, 1 live |
| P1-7 | Test + Prod report | PASS | `docs/testing/ATIP_Full_Review_Test_Report_2026-11-03.md` |
| P1-8 | Tracker | PASS | `ATIP_MASTER_TRACKER.xlsx` phase 20 rows + sheet "Status 2026-11-03 full review" |
| P1-9 | Every file listed in CHANGES.md changed | PASS | 43 listed paths, 0 missing from `git diff --name-status 29b488e fac38d3` (59 files) |
| P1-10 | Every changed file listed | **FAIL (Low, VF-09)** | `scores/validation.py` (the MySQL-quoting fix, `` AS `signal` ``) changed but not in CHANGES.md (only in the test report) |
| P1-11 | Zip = merged code | PASS | 57/59 files byte-equal to `fac38d3`; the 2 that differ are the test report and tracker CSV, updated by later doc commits `a24d1a3`..`369f6d6` |

## 2. Phase 2 — checks

Server column: **T** = Test, **P** = Prod, **L** = live DB copy outside a server, **C** = code.

### V0 — code-review fixes and protected behaviour

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V0-1 | O-H1 stale OCO sibling | PASS | `orders/rules.py:938-951` execute_rule refuses a non-ACTIVE/PENDING rule (`SKIPPED`); test `test_one_pass_sells_once_when_the_stop_cancels_its_target` | C |
| V0-2 | O-H2 failed exit retries; halt holds | PASS | `orders/rules.py:1027` "retrying:", `:867` HELD_HALTED; 2 tests | C |
| V0-3 | O-M2/M3/M4/M5/M7 | PASS | `autopick.py:225-240`; `rules.py:676-691`; `server.py` role guard; `broker.py:166-175`; tests in `test_full_review_2026_11_03.py` | C |
| V0-4 | I-H1, I-M2, I-M3 | PASS | `ema_gap.py:94-125`; `technical.py:287`; `bhavcopy.py:219-230`; TDPOWERSYS max one-day move 104.1 % → 16.0 % after the repair (`evidence/v3/v3_independent.txt`) | L |
| V0-5 | P-H1, P-M3..M7 | PASS | `trading_calendar.py:71`, `scheduler.py:882` lock, `:366` debounce, `:2054` alarm; `engine.py` single write | C |
| V0-6 | D-H1/H2, D-M1..M7 | PASS | `server.py:1517-1518`, `command_page.py:269-270`, `workspace.js` ticket, `command_more.py:236-238`, `server.py:2251` isfinite | C |
| V0-7 | Q-H1/H2/H3, Q-M9 | PASS | `kinds.py:203`, `approval.py:59`, `ml/validation.py:56-81`, `quant/research.py:31` | C |
| V0-8 | Re-grep `date.today()` in routes/pipeline | PASS with Lows | 69 uses in dashboard/pipeline/orders/scores; real issues: allocator replay (`autopick.py:133-142` MAX(date), `:233`) **VF-10**; `technical.run_technical_pipeline(None)` defaults to today (scheduler passes the day) | C |
| V0-9 | Re-grep `.get(x, 0)` on possibly-None | PASS with Lows | `determine_signal` (`engine.py:841-844`) maps None to 0 — conservative (blocks a BUY); `compute_cri` `mh_score or 50`, `compute_msi` FII/DII and index changes `or 0` **VF-12**; "/" CRI cell `or 0` | C |
| V0-10 | Re-grep hard-coded symbol lists | PASS | only `data/dhan.py:861` (a docstring example) outside tests | C |
| V0-11 | Re-grep duplicated scoring logic | PASS (noted) | one engine (`scores/engine.py`); `scores/validation.py` re-implements indicators by design; review §6 lists the money/P&L duplicates | C |
| V0-12 | Unbounded time windows (found while re-scoring) | **FAIL → fixed (VF-20)** | `compute_msi` news `fetched_at >= since` with no upper bound; re-scoring 3 Nov on 4 Nov moved MSI 62.92 → 58.95 (with the late PCR) | L |
| V0-13 | /api/live-quotes polls every 15 s, CMP cells update | PASS | `server.py:1600` `setInterval(pollLiveQuotes,15000)`; test `test_live_cmp_feed_still_polls_every_15s_and_updates_cells_in_place`; 583 `td.cmpcell[data-cmp]` on "/"; `AX.flash` adds `ax-tick-up/down` | C, P |
| V0-14 | BUY/SELL stays EOD-computed | PASS | signals only from `run_scoring_pipeline` (16:49, held until the evening data, re-score 19:00 on 3 Nov per `atip.log`) | L |
| V0-15 | Orders confirmation-gated | PASS | live dialog on "/", Command Center and the ticket; Cancel / Esc sent nothing (`order_log` 34 → 34, `order_rules` 47 → 47) | P |
| V0-16 | scheduler.py exclusions intact | PASS | the full-review diff of `pipeline/scheduler.py` changes no `.every()/.at()` slot (5 hunks: lock, debounce ×2, alarm, try blocks); `operating_schedule.py` unchanged | C |
| V0-17 | Dhan envelope unwrapping | PASS | `data/dhan.py` unchanged since `29b488e`; envelope tests in `test_regressions.py`, `test_dhan_realtime_feeds.py` pass | C |

### V1 — ten top-momentum sessions

Harness `tests/verification/v1_sessions_harness.py`: copies the DB, cuts it back to day D (scores, prices, rules after D
deleted), restarts every pool empty, runs the real `orders.autopick.run()` for the next session, then reads SQL and the board,
and re-ranks independently with pandas (no `orders` import).

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V1-1 | Allocator, last trading date (4 Nov → 5 Nov) | PASS | 10 PICKED, 10 distinct: PVRINOX (BUY), LAURUSLABS, KOTAKBANK, NEULANDLAB, ADANIENT, REDINGTON, TITAN, BALRAMCHIN, GAIL, BHEL | L |
| V1-2 | SQL: 10 session ENTRY rules, no duplicate symbol | PASS | `sql_entry_rules 10, sql_distinct_symbols 10` (`evidence/v1/v1_2026-11-04.json`) | L |
| V1-3 | API returns 10 | PASS | `/api/sessions/board` 10 rows and `/api/sessions` 10 on both servers; Prod "7 of 10 sessions filled — S2, S6, S10: today's buy of NEULANDLAB failed (max_orders_per_day …)" (the live state after today's trading) | T, P |
| V1-4 | UI: 10 cards with symbol, rank, rationale, scores, entry/target/stop, status, P&L, trades | PASS | `/sessions` on Prod: 10 `.ax-tcard`; each filled card has `#k/499`, the rationale, 7 badges, entry/target/stop, P&L, wins; waiting cards give the reason | P |
| V1-5 | Shortfall (test DB only) | PASS (wording fixed, VF-07) | turnover floor ₹2,000 Cr: "3 of 10 sessions filled (1 on BUY signals, 2 rank-filled) — S4…S10: no free …"; rank-fill off: "1 of 10 …" | L |
| V1-6 | Ranking = independent recomputation | PASS | `order_matches_independent: true` on all 4 dates (first run's 2 mismatches were my harness keeping later rules / an EQ-only BUY price; fixed in the harness, re-run) | L |
| V1-7 | Three historical dates give 10 | PASS | 29 Oct, 23 Oct, 19 Oct: 10/10, distinct 10, SQL 10, board "10 of 10" | L |
| V1-8 | "Momentum" ranking | **Owner note (VF-08)** | the sessions rank by the ATIP composite (rank-fill), not price momentum; overlap with the top 10 by 20-day return: 1, 4, 2, 2 of 10 | L |
| V1-9 | Live behaviour today | **FAIL → fixed (VF-04)** | 09:15 IST: 10 buys, 10 distinct. Four sessions sold at a profit (trailing stop) and re-picked; NEULANDLAB FAILED three times at 10:39, 13:10, 15:01 ("max_orders_per_day: 11 would exceed the configured 10"). Auto-pick now waits ("today's 10 buys are used") — shown on the 4 Nov state with the old code picking NEULANDLAB/ADANIENT/REDINGTON and the new code waiting | L, P |

### V2 — score formulas (ATIP, VPI, MRI, RRI, ZPI, CRI↓, ACS)

Script `tests/verification/recompute_scores.py` (does not import `scores.engine` or `data.technical`): indicators from
`prices_daily`, every component from the spec, weights from `weight_config`, compared with `score_components` and `ai_scores`.
Symbols: RELIANCE, HDFCBANK (large), LAURUSLABS (mid), REDINGTON (small), VEDPOWER (new listing, no 200-DMA),
ICICILIFE (10 sessions, **NULL EMA / no indicator row**).

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V2-1 | 7 scores × 6 symbols within 0.01 (settled day 3 Nov) | PASS 42/42 | `evidence/v2/recompute_2026-11-03.json` | L |
| V2-2 | Same on 4 Nov | PASS after explanation | MRI/RRI/CRI/ACS exact; VPI, ZPI, ATIP differ because the stored 4 Nov rows are the provisional 16:49 run: the NIFTY50 bar was written at 17:20 (VPI RS 65.59 vs 58.12) and delivery later (ZPI 12.4 vs 13.93); `atip.log` "Signals for 2026-11-04 held … Watching until 21:30". Not a formula error | L |
| V2-3 | After the fixes, re-score 3 Nov | PASS 35/35 + ICICILIFE not scored | `recompute_2026-11-03_fixed.json`: all components match, 0 stale rows | L |
| V2-4 | Spec table = code | PASS (doc Low VF-13) | `compute_vpi` l.473, `compute_mri` l.492, `compute_rri` l.518, `compute_cri` l.535, `compute_zpi` l.596, `compute_acs` l.789, composite l.978, `determine_signal` l.840; weights in `weight_config` match; ZPI's weight order in the table is ambiguous (Resistance and Support .15, Trend .10) | C |
| V2-5 | CRI↓ ascending wherever it ranks | PASS | `autopick.py:117` and `session_board.py:69` `cri ASC`; `ORDER BY cri DESC` only in risk lists ("riskiest first": `scores/lists.py:36`, `w38_page.py:134`, `telegram.py:181/397`, `premarket_report.py:77`, `server.py` top-25 CRI > 60) | C |
| V2-6 | CRI↓ low = good colour (web + apps) | PASS | badge "● CRI↓ 25" strong on every Prod card; test `test_score_badges_read_cri_low_as_strong_and_carry_the_formula`; the apps load the same pages (no native colour code) | P |
| V2-7 | CRI↓ direction inside composites | PASS | SAFETY = 100 − CRI stored for every symbol; mutant flipping it now killed by 2 tests (VF-06) | L, C |
| V2-8 | Look-ahead: garbage after D, scores for D unchanged | PASS after VF-20 | D = 29 Oct; prices ×0.3–3, indicators, news scores, market health, FII/DII, accuracy outcomes, bulk deals, VIX, global, **news articles** after D corrupted: 0/498 scores or signals changed (old code with news corrupted: **499/499 ATIP changed**); indicators for 8 symbols identical | L |
| V2-9 | NaN: NULL EMA | PASS after VF-03 | RELIANCE with ema_9/21 NULL: EMA left out, MRI 61.41 = weighted mean of the rest (0 would give 52.2). ICICILIFE (no indicators) was scored from stand-ins (MRI 50, CRI 23.33, ATIP 54.77) — now not scored | L |
| V2-10 | Normalisation per date | PASS | rows after D and prices older than D − 450 days deleted: 0/498 changed; every component is fixed-range | L |
| V2-11 | Composite weights and scale | PASS | ATIP weights 1.20 stored, renormalised (0.95 present on average); ACS 1.00; 27,395 components on 29 Oct all in [0, 100]; MH weights sum 1.05 (renormalised, VF-13) | L |

### V3 — backtest validity

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V3-1 | Re-run from scratch matches IC, decile spread, hit rate | PASS | `python -m scores.validation` on the same 23:13 3 Nov snapshot (18:04:06–18:04:53): **1,557 values, 0 differences** vs `validation_recheck.json`. Independent IC code (not `scores.validation`): stored-CRI OOS 1-day IC −0.0834, t −2.12 — the report's figure exactly | L |
| V3-2 | OOS split chronological (last 30 %), no overlap | PASS (Low VF-17) | `split_dates` sorts and cuts at 70 %: IS 2025-06-03 → 2026-06-02, OOS 2026-06-03 → 2026-11-03; no embargo for the overlapping 5–20-day windows (nothing is fitted in-sample except the variant test) | C |
| V3-3 | Forward returns start at the next day | PASS | `validation.py:178` `close.shift(-h) / open.shift(-1) − 1` = open(t+1) → close(t+h); test `test_forward_return_is_next_open_to_close_and_masks_jumps` | C |
| V3-4 | Adjusted prices; sawtooth large caps clean | PASS | 0 of 499 scored symbols have ≥ 2 jump-and-revert pairs (> 5 %, net < 1 %) in 120 sessions (10 of 2,929 EQ, none scored); one-day moves > 35 % in the scored universe since 3 Jun: 4 (masked by JUMP_LIMIT) | L |
| V3-5 | Reweights improve out of sample, not only in-sample | PASS | ATIP (SAFETY), MRI, ZPI, ACS: IC up in-sample **and** out-of-sample at 1, 5 and 20 days (`evidence/v3/v3_is_vs_oos.txt`); VPI unchanged; RRI/CRI moved < 0.001 (NULL handling only) | L |
| V3-6 | Final verdicts; none BROKEN or INVERTED | **FAIL — owner decision (VF-01)** | PIT out of sample: **CRI↓ WORKING**; MRI, ZPI, ACS **WEAK**; **ATIP, VPI, RRI BROKEN**; none INVERTED. Same verdicts on today's snapshot (354 sessions). Stored view (44–45 sessions) flips between runs (ATIP WEAK → BROKEN, ACS WORKING → WEAK): too short to judge | L |

### V4 — UI/UX

Automated sweep (Playwright + axe-core 4.13, Chromium): 21 pages × light/dark × 1,440/375 × Test/Prod = 168 runs, run
before and after the fixes; manual checks in the in-app browser.

| ID | Check | Before fixes | After fixes | Evidence |
|---|---|---|---|---|
| V4-1 | DESIGN.md five stages with problems and decisions | PASS | — | Empathise (4 jobs), Define P1–P5, Ideate (options + pick + why), Prototype, Test (4 walks) |
| V4-2 | Tokens once; no hard-coded colours | **FAIL (VF-32)** | partial | 1,138 literals in 31 files (917 in legacy `.py` pages, mapped onto tokens by the legacy bridge); atip.css component rules now use tokens only |
| V4-3 | Contrast WCAG AA | **FAIL (VF-22)**: unread badge 1.89:1 on all 42 dark Prod runs | PASS: 0 axe violations on all 168 runs (badge now #2A0A0C on #FCA5A5, 9.6:1) | axe `color-contrast` |
| V4-4 | Colour-blind: arrow/label on every up/down/BUY/SELL | **FAIL (VF-25)**: 25 cells on /trading, /m day P&L | PASS: P&L cells and KPIs carry ▲ +/▼ −; residual hits are labelled values ("Stop Loss ₹1,494.88"), words ("INCOMPLETE") or a risk flag (concentration ratio), not up/down indicators | heuristic scan |
| V4-5 | Tabular numerals, right-aligned | **FAIL (VF-24)**: e.g. "/" 6,115 cells left/start | PASS: 0 numeric cells left-aligned or proportional on both servers (ranges, dates, fractions stay text) | computed style |
| V4-6 | KPI strip | PASS | — | 5 tiles on /home |
| V4-7 | 10-session card grid | PASS | — | 10 `.ax-tcard` on /sessions and /home |
| V4-8 | Sortable/filterable tables, sticky headers | **FAIL (VF-23, VF-27)**: Table view sorted as text; "/" headers mouse-only | PASS | numeric + natural sort; keyboard + `aria-sort` + sticky on "/" (filter `#srch` was already there) |
| V4-9 | Sparklines | PASS | — | `svg.ax-spark` 5 on /home, 10 on /sessions |
| V4-10 | Score tooltips | PASS (Low VF-30) | — | 49 badges, all titles carry the formula; hover-only |
| V4-11 | Detail drawer | PASS (modal fixed, VF-26) | PASS | stock panel takes focus to Close, keeps Tab inside, Esc closes; row drawer on /sessions/S1 |
| V4-12 | CMP change flash | PASS | — | `AX.flash` → `ax-tick-up/down`, removed after 1.2 s (forced; market closed) |
| V4-13 | "Last updated HH:MM:SS IST" | **FAIL at 1,440 on "/" (VF-28)** | PASS | "/" header "Updated 16:45:09 IST" |
| V4-14 | Stale-feed warning | PASS (Low VF-31) | — | forced stale → `data-state=stale`, "⚠", warn colour |
| V4-15 | Skeleton loaders | PASS | — | /command 9, /trading 5, /market 5, /research 14 in the raw HTML; none left visible |
| V4-16 | Order dialog: symbol, qty, price, value, stop; Cancel sends nothing | **FAIL on Command Center (VF-05)**: "Order value —" | PASS | "/" ≈6 sh ₹25,627.50 stop −2 %; ticket 10 sh ₹15,773 stop −1.98 %; Command Center now ≈6 sh ₹25,627.50; Cancel/Esc: `order_log` 34 → 34 |
| V4-17 | Light/dark on every tab | PASS | PASS | `data-theme` matched 168/168; body rgb(246,247,249) vs rgb(11,18,32) |
| V4-18 | No sideways scroll at 375 | PASS | PASS | scrollWidth 375 on 84/84 runs per server |
| V4-19 | Keyboard reaches all controls | PASS (Test: scrollable strip VF-29) | PASS | Tab ×200 on /home, /sessions, "/": 103/103, 74/74, 111/111 (Prod); visible focus on every stop; no trap |
| V4-20 | Screenshots, every tab, both themes, both widths | PASS | PASS | `docs/testing/screenshots/verify-2026-11-04/web/` (after fixes) and `web/before-fixes/` |

### V5 — tests

| ID | Check | Result | Evidence |
|---|---|---|---|
| V5-1 | Full suite on the verified build (master) | **FAIL → fixed** | 18:03:42–18:13:03: 2,522 passed, 65 skipped, **4 failed** — 3 × `test_w39_perf_budget` (the sandbox refuses local sockets; same as 3 Nov) and **`test_dhan_chain_rows_for_the_nearest_expiries`** (hard-coded 3 Nov expiry; fails from 4 Nov, VF-11) |
| V5-2 | Full suite after the fixes | PASS: 18:58:35–19:07:37 IST (9 min 0 s): 2,533 passed, 65 skipped, 4 failed — the 3 `test_w39_perf_budget` socket tests (14/14 pass outside the sandbox, 19:07:50) and `test_order_paths_are_untouched`, which asserts a clean git tree under `orders/` and passes once the branch is committed; no other failure. On master after the merge (`7887351`, 22:55:10–23:04:11 IST): 2,534 passed, 65 skipped, 3 failed — only the socket tests (14/14 outside the sandbox, 23:04:48) | `evidence/v5/` |
| V5-3 | Hand-calculated formula tests per score | PASS | `test_score_audit_2026_11_03.py`: VPI, MRI, RRI, ZPI, CRI, ACS, ATIP, RS by hand |
| V5-4 | 10-session regression, CRI↓ ordering, UI smoke, dialog flow | PASS | `test_ten_sessions_are_filled…`, `test_rank_fill_orders_cri_ascending…`, `test_board_rank_puts_lower_cri_first…`, `test_pages_render_the_kpi_strip…`, `test_every_order_send_path_asks_first…` (code-level) + the live dialog checks |
| V5-5 | Mutation: break a formula | PASS | CRI ×15 → ×12: killed (1 test) |
| V5-6 | Mutation: break a session limit | PASS | board TARGET 9: 4 fail; S10 removed: 8 fail; auto-pick serves 9: 4 fail. `MAX_SESSIONS` 9: 0 fail — the constant is never read (VF-14) |
| V5-7 | Mutation: composite direction | **FAIL → fixed (VF-06)** | SAFETY = CRI survived all 221 tests that touch scoring; now killed by `tests/verification/test_independent_recompute.py` |

### V6 — mandatory items

| ID | Check | Result | Evidence |
|---|---|---|---|
| V6-1 | Apps: score views, labels, CRI↓, 10 sessions, tokens = web | PASS by construction | Android `WebView` / iOS `WKWebView` shells load `/home`, `/watchlist`, `/command`, `/sessions`, `/more`, `/m?tab=live` (no native score code); app-view screenshots at 390 × 844 and 412 × 915, both themes: `screenshots/verify-2026-11-04/mobile/` |
| V6-2 | Build both apps | **BLOCKED (VF-16)** | no Xcode (Command Line Tools only), no Android SDK, no Java runtime, no gradle/xcodegen; `swiftc -parse` fails in the CLT ("redefinition of module SwiftBridging"), as on 3 Nov |
| V6-3 | Apps updated with Phase 3 | DONE | 2026.11.04.1 (Android 8 / iOS 8), README |
| V6-4 | Tracker statuses = verification | FAIL → corrected (VF-19) | FR-TEST suite numbers, FR-SV-ATIP/VPI/RRI "Validated" read as working; phase 21 rows added |
| V6-5 | Test and Prod: V1, V2 spot, V4, V5 | DONE | testing report |

## 3. Defect log

Severity: High = wrong order / money / a score that misleads every user; Medium = wrong numbers on screen or a check that
fails; Low = cosmetic or rare. **Status after Phase 3.**

| ID | Task | Sev | Evidence | Root cause | Fix | Re-verified |
|---|---|---|---|---|---|---|
| VF-01 | V3 | High | ATIP, VPI, RRI BROKEN OOS | VPI's V (ATR) reads the wrong way for 1–5-day returns; RRI has no consistent component | **Owner decision** (VPI V flip + BUY gate re-set, RRI redesign); not fixed — a model change | — |
| VF-02 | V2 | Med | 944 stale "Support" rows | `store_components` upserted, never deleted | replace the day's rows | 0 stale; components 35/35 |
| VF-03 | V2 | Med | ICICILIFE CRI 23 from stand-ins | scoring ran with an empty indicator row | not scored; old row removed | 498 rows, ICICILIFE absent |
| VF-04 | V1 | Med | 3 FAILED NEULANDLAB buys on 4 Nov | auto-pick ignored the day's buy count | wait with a reason | live state: S2/S6/S10 wait |
| VF-05 | V4 | Med | Command Center "Order value —" | its pending list was not sized | `rules.with_estimates` | ≈6 sh, ₹25,627.50 |
| VF-06 | V5 | Med | SAFETY-sign mutant survived | no end-to-end score test | 2 tests vs the recompute | mutant killed |
| VF-07 | V1 | Low | "no free BUY signal" when rank-fill was exhausted | wording | names rank-fill | harness text |
| VF-08 | V1 | Info | 1–4 of 10 overlap with price momentum | "momentum" implemented as the ATIP rank | **Owner decision** | — |
| VF-09 | P1 | Low | `scores/validation.py` missing from CHANGES.md | doc | recorded here | — |
| VF-10 | V0 | Low | allocator replay reads MAX(date) / `date.today()` | live-only design | open | — |
| VF-11 | V5 | Med | dated test fails from 4 Nov | hard-coded expiry | clock pinned | 45/45 |
| VF-12 | V0 | Low | missing MH / FII / index change read as 50 / 0 | `or` defaults | open | — |
| VF-13 | V2 | Low | MH weights 1.05; ZPI weight order in the spec | doc / config | open | — |
| VF-14 | V5 | Low | `MAX_SESSIONS` unused | dead constant | open | — |
| VF-15 | V1 | Low | BUY candidates include BE series | two filters | open | — |
| VF-16 | V6 | Blocker | native builds impossible | no SDKs on this Mac | build in Xcode / Android Studio | — |
| VF-17 | V3 | Low | no IS/OOS embargo | harness design | open | — |
| VF-18 | V1 | Low | names "S5 Options", "S10 Intraday" | historical labels | **Owner** | — |
| VF-19 | V6 | Med | tracker overstated | statuses not re-checked | corrected | tracker |
| VF-20 | V2 | Med | re-score read later news: 499/499 ATIP moved | MSI news window had no upper bound | bounded | 0/498 |
| VF-21 | V2 | Low | `data/news_weighting.py:80,193-195` unbounded | same pattern | open | — |
| VF-22 | V4 | High (a11y) | badge 1.89:1 on every dark page | white text on `--ax-neg` | `--ax-neg-ink` token | 0 axe violations |
| VF-23 | V4 | Med | Table view sorted "#118" before "#2" | text sort without `data-v` | numeric + natural sort | P&L −₹253.34 → +₹1,144.40; #1…#118; S1…S10 |
| VF-24 | V4 | Med | numbers left-aligned on older tables | legacy markup | `AX.alignNumbers` + CSS | 0 failing cells |
| VF-25 | V4 | Med | colour-only P&L on /trading, /m | sign dropped for positives | ▲ + / ▼ − in text | /trading 23 → 0 P&L cells; /m 1 → 0 |
| VF-26 | V4 | Med | modal did not take focus | no focus handling | focus in / trap / return | manual check |
| VF-27 | V4 | Low | "/" headers mouse-only | legacy sort | keyboard + `aria-sort` + sticky | manual check |
| VF-28 | V4 | Low | no seconds stamp on "/" at 1,440 | container query | stamp in the header | "Updated 16:45:09 IST" |
| VF-29 | V4 | Low | scrollable strip not focusable (Test, 375) | missing tabindex | `tabindex="0"` | axe 0 on Test |
| VF-30 | V4 | Low | formula tooltip hover-only | badges not focusable | open | — |
| VF-31 | V4 | Low | "Updated —" shown as fresh | state default | open | — |
| VF-32 | V4 | Med | 1,138 colour literals | legacy pages | atip.css tokenised; legacy pages open | — |
| VF-33 | Research | Med | 44/448 fair values outside 0.2–5× price (BAJAJFINSV ₹2.31 crore vs ₹1,718) | valuation inputs' units | open (not used by scores or orders) | — |
| VF-34 | Deploy | Low | live, stop window only (21:00–06:00): header 1,466–1,490 px in 1,440 on `/command`, `/command/signals`, `/trading`, `/sessions` — the "⏸ ATIP stopped — resumes 06:00 IST" pill (243 px) + the feed pill (237 px) push the alert icons out. Same with the old atip.css/js (pre-existing); Test/Prod ran with the schedule off, so they never showed it. Reproduced 5 Nov on a copy with a manual stop: with the watchlist open (bar 904 px at 1,440) 20 of 22 pages pushed the icons out, and the search shrank under its label and spilled over the PAPER pill | the pills had one wording and the search could shrink; the only fallbacks were fixed widths, so a longer sentence or feed text spilled | **fixed 5 Nov** (`claude/fix-vf34-vf35-2026-11-05`): short words (`banner_short`), `AX.fitTop` gives way in order (Ctrl K hint → feed → run → icons into ⋯ → breadcrumb page part), search keeps its width, "…" last | yes, 5 Nov: 22 pages × 14 widths (360–1,920) × watchlist closed/open, stopped: before 80/528 loads with the icons or Kill switch up to 191 px off-screen (all 22 pages, 1,440 and 1,600 with the watchlist open), search spilling in 233; Test 0/528 and Prod 0/528; **live Mac after the 21:10 restart 0/528** (real stop window) |
| VF-35 | Deploy | Low | live: a session card with a 4-digit P&L wraps "P&L +" / "₹1,144.40" at 1,440 (S3). Same with the old atip.css/js (pre-existing) | the card allowed a break anywhere (`overflow-wrap: anywhere`) and the footer did not wrap between its two items | **fixed 5 Nov**: each footer item stays on one line; "Wins" moves under the P&L | yes, 5 Nov: before, a split figure in 8/528 loads (Home, 414–1,920 px: "P&L +₹1,144.40", "P&L +₹104.09" …); Test 0/528, Prod 0/528; **live 0/528** (S3 "P&L +₹1,534.74", S5 "P&L +₹1,231.37" whole) |

## 4. What needs the owner

1. **VF-01** — accept the three BROKEN verdicts, or approve the VPI V flip with a BUY-gate re-set (evidence: 3 Nov
   `BACKTEST_VALIDATION_REPORT.md` §3) and an RRI redesign.
2. **VF-08** — is "top-momentum" meant as the ATIP rank (today) or a price-momentum rank?
3. **VF-04** — `max_orders_per_day` = 10 equals the number of sessions, so no session can buy again on the day it sells.
   Raise it if same-day re-entry is wanted (the fix only stops the doomed orders).
4. ~~Deploy the Phase 3 fixes~~ — done: merged 22:55 IST (`7887351`), live since 23:45:47 IST (testing report §6).
5. **VF-16** — build the apps where Xcode and the Android SDK exist.
6. ~~VF-34 / VF-35~~ — done: merged 17:33 IST 5 Nov (`fde1951`), live since 21:11:00 IST (testing report §8).

⚠️ *This analysis is for informational purposes only and does not constitute financial advice. Past performance is not
indicative of future results.*
