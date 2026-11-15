# ATIP independent re-verification (prompt 4 Nov 2026, 06:00 PM IST — run again 5 Nov 2026 from 01:05 PM IST)

**Date of report:** 5 Nov 2026, afternoon. All times are IST (UTC + 5:30).
**Role:** QA lead and quant reviewer. Nothing in the 3 Nov or 4 Nov CHANGES.md / reports was taken as true until reproduced;
the 4 Nov pass's own fixes and scripts were treated as "the previous run" and checked too.
**Scope (owner, 13:07 IST):** a fresh full pass on today's build, with three owner decisions to apply: **VF-01** fix VPI/RRI
only if out of sample proves it, re-gate BUY; **VF-08** keep the ATIP rank (relabel "momentum"); **VF-04** raise
`max_orders_per_day` to 20.
**Build verified:** master `0ab3de9` in `/Users/agtci/ATIP` (the folder the live Mac runs; `/Users/agtci/Documents/ATIP` is an
older 26 Oct copy). **Phase 3 fixes:** branch `claude/verify-2026-11-05` (worktree `.claude/worktrees/verify-2026-11-05`),
code commit `8bbc735` (13:51 IST). **Not merged, not deployed** — see §5.
**Servers.** ATIP has no hosted servers. **Test** = the build on a fresh DB (`main.py --init`, :8832). **Prod** = the build on a
consistent SQLite backup of the live `atip.db` taken at 13:15 IST (:8833). Both run PAPER only, with a credential-free
`config.json`. The live Mac (:8000) was only read (one `GET /api/sessions/board` at 13:17:50 IST).
**Another session** was fixing VF-34 / VF-35 (branch `claude/fix-vf34-vf35-2026-11-05`, files `dashboard/shell.py`,
`atip.js`, `pipeline/operating_schedule.py`) at the same time. This pass left those two defects and those files alone.
**Skills:** `share-market` loaded. `frontend-design` does not exist on this machine; the UI checks follow DESIGN.md, WCAG 2.2 AA
and axe-core 4.13.

## Verdict per task

| Task | Verdict | One line |
|---|---|---|
| Phase 1 deliverables | **PASS** (1 Low fixed) | All 3 Nov + 4 Nov deliverables present; CHANGES.md files = git diff (only VF-09 again); 4 Nov zip = merged code 24/24; DESIGN.md screenshots were not in git (VF5-09, fixed) |
| V0 code-review fixes | **PASS** (2 partial, Lows) | 24 High/Medium fixes found at file:line; Q-H1, P-H1 partial; protected behaviour intact; new VF5-05, VF5-06 (fixed) |
| V1 ten sessions | **PASS** | 10 / 10 distinct / SQL 10 / board 10 on 4 dates; harness + my SQL-only ranking agree; shortfall says why; API 10 on both servers; live board 10 of 10 |
| V2 score formulas | **PASS** (1 Low fixed) | 498 stocks × 7 scores within 0.01, signals equal; indicators from OHLCV equal; look-ahead, normalisation, NULL-EMA, composite pass; PCR never in live MSI (VF5-04, fixed) |
| V3 backtest | **PASS for the method; RRI still BROKEN (owner)** | Reproduced exactly; VPI flip passed the pre-set OOS rule and is applied (VPI WORKING, ATIP WEAK); RRI redesign failed the rule (VF5-10) |
| V4 UI/UX | **PASS except hard-coded colours** | 168 runs before and after: 0 axe violations, 0 JS errors, 0 sideways scroll, keyboard 100 %; order dialog complete, Cancel sends nothing; 822 colour literals remain (VF5-16) |
| V5 tests | **PASS after fixes** | Suite was flaky in market hours (VF5-07, fixed); mutants killed; a dead constant let one survive (VF5-08, removed) |
| V6 mandatory | **PARTIAL** | Apps 2026.11.05.1 (build 9) web views checked; native builds still impossible here (VF-16); tracker phase 22; Test + Prod done |

## 1. Phase 1 — deliverable check

| ID | Check | Result | Evidence |
|---|---|---|---|
| P1-1 | 3 Nov delta zip | PASS | `deliverables/2026-11-03/ATIP_full_review_delta_2026-11-03.zip` 2,368,592 B |
| P1-2 | 3 Nov CHANGES.md, code review, score audit, backtest report | PASS | 8,058 / 21,789 / 9,975 / 17,620 B in `docs/reports/2026-11-03-fullreview/` |
| P1-3 | DESIGN.md with before/after screenshots | PASS → **Low fixed (VF5-09)** | `DESIGN.md` 9,141 B, five stages; its 16 screenshots existed only as untracked files on the Mac (0 in git), so a clone showed broken links — now committed (1.0 MB) |
| P1-4 | Test + Prod reports | PASS | `docs/testing/ATIP_Full_Review_Test_Report_2026-11-03.md` 11,768 B; `ATIP_Verification_Test_Report_2026-11-04.md` 8,779 B |
| P1-5 | Tracker | PASS | `ATIP_MASTER_TRACKER.xlsx` 177,993 B (phases 20, 21); `docs/ATIP_MASTER_TRACKER.csv` 558 rows |
| P1-6 | 4 Nov deliverables (zip, CHANGES, report, recompute script) | PASS | zip 32,144,266 B (377 entries); CHANGES 6,467 B; report 26,937 B; `tests/verification/recompute_scores.py` 19,363 B |
| P1-7 | 3 Nov: every listed file changed | PASS | 42 listed, 0 missing from `git diff 29b488e fac38d3` (`evidence/p1/changes_vs_diff.txt`) |
| P1-8 | 3 Nov: every changed file listed | FAIL (Low, VF-09, known) | `scores/validation.py` only; reports/analysis files are the deliverables themselves |
| P1-9 | 4 Nov: listed = changed | PASS | 27 listed, 0 missing; unlisted = evidence/docs and `tests/verification/__init__.py` (empty) |
| P1-10 | 4 Nov zip = merged code | PASS | 24/24 code files byte-equal to `7887351`; tracker and PATCH_NOTES differ (later doc commits) |

## 2. Phase 2 — checks

Server column: **T** Test, **P** Prod, **L** live DB copy (13:15 IST) outside a server, **C** code, **M** the live Mac (read only).

### V0 — code-review fixes and protected behaviour

A read-only reviewer re-located every fix from the code (not from the 4 Nov report); I re-checked the new findings myself.

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V0-1 | Orders O-H1, O-H2, O-M2/M3/M4/M5/M7 | PASS | `orders/rules.py:826-828`, `:965-968` (stale OCO sibling `SKIPPED`); `:1040-1044` retry, `:880-884` HELD_HALTED; `autopick.py:225-239`; `rules.py:707-717`; `server.py:2055-2062`; `broker.py:164-176, 236-243` | C |
| V0-2 | Ingestion I-H1, I-M2, I-M3 | PASS | `ema_gap.py:109-157`; `technical.py:287`; `bhavcopy.py:219-226` | C |
| V0-3 | Pipeline P-H1 | **PARTIAL (Low, VF5-13)** | `trading_calendar.py:71-82` alarm, but only raised in `start_scheduler()`; NSE 2027 holidays not in the calendar yet (NSE publishes in December) | C |
| V0-4 | Pipeline P-M3..M7 | PASS | `scheduler.py:1193-1199`, `telegram.py:146-161`, `scheduler.py:877-886` lock, `:1314/1372` debounce, `engine.py:939-943` one write | C |
| V0-5 | Dashboard D-H1, D-H2, D-M1..M5, D-M7 | PASS | `server.py:1522-1523`, `command_page.py:269-270`, `workspace.js:217-222`, `command_more.py:231-240`, `server.py:2236-2259`, `w39_retail_routes.py:94` | C |
| V0-6 | Quant Q-H1 | **PARTIAL (Low, VF5-12)** | no crash (`kinds.py:201-204`), but `strategy_engine/engine.py:61` always passes `held_sessions: None`, so a max-hold exit never fires. All 17 strategies are DRAFT, so nothing live is affected | C, L |
| V0-7 | Quant Q-H2, Q-H3, Q-M9 | PASS | `approval.py:84-94`; `ml/validation.py:56-77`; `quant/engine.py:145-146` | C |
| V0-8 | Re-grep `date.today()` | PASS with Lows | live "today" uses only; VF-10 (allocator replay) still open | C |
| V0-9 | Re-grep `or 0` / `or 50` on NULL | PASS with Lows | VF-12 still open (`engine.py:551, 772-775`) | C |
| V0-10 | Hard-coded symbol lists | PASS | `data/news.py:44-48` alias map (TATAMOTORS, ZOMATO renamed; Low); `dhan.py:861` docstring | C |
| V0-11 | Duplicated scoring | PASS | `scores/validation.py:143-152` calls the engine's own `compute_*` | C |
| V0-12 | OCO settle (found this pass) | **FAIL → fixed (VF5-06)** | `settle_oco_group` read only ACTIVE siblings (`rules.py:409-411`): a TARGET already PENDING_CONFIRMATION survived a filled STOP, and Confirm sent a second SELL | C |
| V0-13 | Evening flows check (found this pass) | **Low → fixed (VF5-05)** | `_evening_data` counted any `fii_dii_market` row as flows; `data/derivatives.py:175-178` can write a PCR-only row. Latent: on 3–4 Nov the PCR arrived next day as an UPDATE (rows created 19:00 / 18:50 IST) | C, L |
| V0-14 | Watch end after the stop (reported, checked) | Low (VF5-11) | `FII_DII_WATCH_END` 21:30 vs the 21:00 stop: the last 3 slots never run ("stop window — not run: … run_fii_dii_watch" 21:00/21:10/21:20 on 3 Nov), but `release_held_signals()` settles a held session at the 06:00 resume (`scheduler.py:265-329`), so nothing is lost — the reviewer's "never logged" is wrong | C, L |
| V0-15 | /api/live-quotes every 15 s; CMP cells update | PASS | `server.py:1605` `setInterval(pollLiveQuotes,15000)`, cells rewritten at `:1593-1597`; Prod: 583 `td.cmpcell[data-cmp]`, `AX.flash` gives `ax-tick-up`/`down` | C, P |
| V0-16 | BUY/SELL EOD only | PASS | only writer `engine.py` `_SQL_SCORE`, called from the post-market (16:49) and evening re-score; live-quote code never writes `ai_scores` | C, L |
| V0-17 | Orders confirmation-gated | PASS | §V4-16 | P |
| V0-18 | scheduler.py exclusions | PASS | no `.every()/.at()` slot changed; this pass adds one call inside `settle_session_flows` | C |
| V0-19 | Dhan envelope | PASS | `dhan.py:281-296` `_unwrap_feed`; unchanged; tests `test_regressions.py:52-77` pass | C |

### V1 — ten sessions (owner: ranked by the ATIP composite, VF-08)

Harness `tests/verification/v1_sessions_harness.py` (reviewed; it needed the Dhan security list in its work folder, which I
copied — it otherwise tries to download it) on the 13:15 IST live copy, plus my own SQL-only ranking
(`evidence/v1/my_rank.py`, no pandas, no `orders/` import).

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V1-1 | Allocator for the last scored day (4 Nov → 5 Nov) | PASS | 10 PICKED, 10 distinct: PVRINOX (BUY), LAURUSLABS, ADANIENT, KOTAKBANK, NEULANDLAB, TITAN, REDINGTON, BALRAMCHIN, GAIL, LODHA | L |
| V1-2 | SQL: 10 ENTRY rules, no duplicate | PASS | `sql_entry_rules 10, sql_distinct_symbols 10` (`evidence/v1/v1_2026-11-04.json`) | L |
| V1-3 | API returns 10 | PASS | `/api/sessions` 10 and `/api/sessions/board` 10 rows on both servers, before and after the fixes; Prod "10 of 10 sessions filled"; Test (no capital) "0 of 10 … no starting amount: set one up on /sessions" | T, P |
| V1-4 | UI: 10 cards with symbol, rank, rationale, scores, entry/target/stop, status, P&L, trades | PASS | 40 board rows on 4 dates, 0 missing fields; `/sessions` 10 `.ax-tcard`; 7 badges each; live board rows carry P&L and closed/successful trades | L, P, M |
| V1-5 | Shortfall (copies only) | PASS | turnover floor ₹2,000 Cr: "3 of 10 sessions filled (1 on BUY signals, 2 rank-filled) — S4…S10: no free BUY signal or rank-fill stock (…)"; rank-fill off: "4 of 10 … (4 on BUY signals)" | L |
| V1-6 | Ranking = independent recomputation | PASS | harness: `order_matches_independent: true` on 4 dates; SQL-only: equal on 30 Oct, 27 Oct, 14 Oct; on 4 Nov mine had BOSCHLTD 9th — its close ₹50,925 is above a session's ₹25,000, so the allocator rightly skips it | L |
| V1-7 | Three more historical dates | PASS | 30 Oct, 27 Oct (4 BUY), 14 Oct: 10/10, distinct 10, SQL 10, board "10 of 10" | L |
| V1-8 | Live today | PASS | `GET /api/sessions/board` 13:17:50 IST: "10 of 10 sessions filled", 10 distinct (AUROPHARMA, ADANIENT, NEULANDLAB, LAURUSLABS, BALRAMCHIN, TITAN, GAIL, KOTAKBANK, LODHA, BHEL); 7 buys placed by 12:06, under the cap | M, L |

### V2 — score formulas

Script `tests/verification/independent_7scores.py` (new; imports nothing from `scores/`, `data/`, `orders/`, `utils/` and not
the 4 Nov script). Layer A recomputes RSI-14, EMA-9/21, SMA-200, ATR-14 %, ADX-14, volume ratio and MACD-histogram % from
`prices_daily` OHLCV; layer B recomputes VPI, MRI, RRI, ZPI, CRI, ACS, ATIP (and MSI, INS, the signal) from the indicators and
raw tables. **All 498 scored stocks**, not five; the five-plus picked symbols are inside.

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V2-1 | 7 scores × 498 stocks within 0.01, 4 Nov (latest settled day) | PASS 498/498 | max diff 0.01 on every score; signal equal 498/498 (`evidence/v2/all_2026-11-04.json`) | L |
| V2-2 | Chosen symbols | PASS | TCS, ICICIBANK (large), COFORGE (mid), BALRAMCHIN (small), VEDPOWER (no 200-DMA), HEGAM (no 200-DMA, **no MACD**): every score equal (`five_2026-11-04_oldspec.json`). No scored stock has a NULL EMA in the live data, so NULL EMA is tested synthetically (V2-9) | L |
| V2-3 | 3 Nov | PASS with one explained difference | 497 stocks exact except TDPOWERSYS RRI (stored before the 4 Nov 00:11 split repair changed its 52-week low); every stock's ACS/ATIP 0.4 off because the stored MSI lacked the PCR that arrived next day (→ V2-12) | L |
| V2-4 | Indicators from raw OHLCV | PASS | 498/498 on 4 Nov; on 3 Nov 2 stocks (TDPOWERSYS, CONCORDBIO) differ — their prices were repaired after the 3 Nov indicators were stored | L |
| V2-5 | Spec table = code | PASS | `compute_vpi` l.475, `compute_mri` l.494, `compute_rri` l.520, `compute_cri` l.537, `compute_zpi` l.598, `compute_acs` l.794, composite l.989, `determine_signal` l.845 (master `0ab3de9`) | C |
| V2-6 | CRI↓ ascending wherever it ranks | PASS | `autopick.py:117`, `session_board.py:69` `cri ASC`; every `cri DESC` is a "riskiest first" list (`scores/lists.py:36`, `w38_page.py:134`, `telegram.py:181/397`, `premarket_report.py:77`, `ml/chat.py:154-155`) | C |
| V2-7 | CRI↓ low = good colour, web + apps | PASS | `score_glossary.band` inverts CRI; all three CRI pills on "/" pass `inv=True` (`server.py:579, 584, 865`); badge "CRI↓"; apps load the same pages | C, P |
| V2-8 | Look-ahead | PASS | D = 27 Oct (not 4 Nov's 29 Oct). Corruption extended this pass to PCR, per-stock FII/DII, index changes and news confidence: 0/498 scores or signals changed; indicators of 8 symbols identical (`invariance_2026-10-27.json`) | L |
| V2-9 | NULL EMA | PASS | TCS with ema_9/21 NULL: EMA left out, MRI 37.33 = weighted mean of the rest (31.73 if EMA were 0) | L |
| V2-10 | Normalisation per date | PASS | rows after D and prices older than D − 450 days removed: 0/498 changed | L |
| V2-11 | Composite weights / scale | PASS | ATIP 1.20 stored, renormalised (0.95 present on average); ACS 1.00; 27,260 components in [0, 100]; MH 1.05 (VF-13, open) | L |
| V2-12 | MSI's Options (found this pass) | **FAIL → fixed (VF5-04)** | the 16:45 F&O job stores the *previous* session's file (11-03 row created 11-04 16:45:15), so the 16:49 and 18:50 scores never had the day's PCR — while every later re-score and the validation do | L, C |
| V2-13 | After the fixes: re-score 4 Nov on Prod, recompute (new spec) | PASS 498/498 | max diff 0.01; ICICILIFE (no indicators) dropped by VF-03 (`all_2026-11-04_newspec.json`) | P |

### V3 — backtest validity and VF-01

| ID | Check | Result | Evidence | Srv |
|---|---|---|---|---|
| V3-1 | Re-run from scratch = reported IC / spread / hit | PASS | `python -m scores.validation` on the 3 Nov 23:13 snapshot (sha1 454e15e…, same file both reports used), 13:23:50–13:24:44: **1,491 values, 0 differences** vs `validation_recheck.json` | L |
| V3-2 | Split chronological, last 30 %, no overlap | PASS (Low VF-17) | `split_dates` sorts and cuts at 70 %; today: IS 2025-06-03 → 2026-06-02 (247), OOS 2026-06-03 → 2026-11-04 (107) | C |
| V3-3 | Forward returns from the next day | PASS | `validation.py:178` `close.shift(-h) / open.shift(-1) − 1` | C |
| V3-4 | Adjusted prices; sawtooth clean | PASS | 3 of 499 scored stocks have ≥ 2 jump-and-revert pairs (> 5 %) in 120 sessions — MTARTECH, OLAELEC, PVRINOX — all with volume spikes and consistent OHLC (real moves, not a split); one > 35 % day since 3 Jun: POLICYBZR −36 % on 24 Sep at 34× volume, a real crash the 35 % mask also drops (VF5-14) | L |
| V3-5 | 3 Nov reweights improve out of sample | PASS | old vs now, IS / OOS IC: MRI 1d −0.002→+0.005 / −0.015→+0.007, 20d −0.016→+0.021 / −0.037→+0.033; ZPI 1d +0.005→+0.021 / −0.006→+0.017; ATIP (SAFETY) 1d −0.001→+0.019 / −0.006→+0.008, 20d +0.025→+0.037 / +0.029→+0.030 — better in both periods at 1, 5 and 20 days (`reweights_is_oos.txt`) | L |
| V3-6 | VF-01 (owner): VPI flip and RRI redesign, adopted only by the rule set before the run | **VPI adopted; RRI not** | rule: own OOS IC higher at 1, 5, 20; IS 5d not lower; ATIP OOS not lower. **VPI flip**: VPI OOS −0.0035 / +0.0062 / +0.0324 → **+0.0274 (t 2.8)** / +0.0215 / +0.0330; IS 5d −0.001 → +0.021; ATIP OOS +0.0083 / +0.0172 / +0.0304 → +0.0200 / +0.0244 / +0.0330 → **adopt**. **RRI** (plan from IS t-stats only: keep Support, drop Recovery, RSIRecovery, Volume): OOS 20d +0.0136 → +0.0054 and ATIP 20d lower → **not adopted** (`vf01_live.txt`) | L |
| V3-7 | BUY gate re-set (owner) | Done, in-sample | VPI gates re-set to keep the IS BUY rate (2.42 → 2.53 a day): 76.5 / 66.5. OOS: 3.6 BUY rows a day (was 6.9), 5-day net per BUY −0.30 % (was −0.05 %; n 382 vs 736, difference not significant, t ≈ 0.8). Top-10 by ATIP, 5-day net OOS −0.14 % (was −0.38 %), IS −0.17 % (was +0.04 %) | L |
| V3-8 | Final verdicts (PIT, OOS) | **CRI↓ WORKING; VPI WORKING (was BROKEN); ATIP, MRI, ZPI, ACS WEAK; RRI BROKEN; none INVERTED** | only RRI remains BROKEN (VF5-10, owner) | L |

On the live data the change moves 4 Nov's one BUY (PVRINOX, VPI 61.6 → 65.2, under the new 66.5 gate) to HOLD, 20 stocks
from WAIT to HOLD (ATIP rises about 1–2 points as calm stocks gain VPI), and reorders the top ten by ATIP (KOTAKBANK 4 → 2,
TITAN 6 → 3, NEULANDLAB 5 → 10).

### V4 — UI/UX

Playwright + axe-core 4.13 (Chromium): 21 pages × light/dark × 1,440/375 × Test/Prod = 168 runs, before the fixes
(13:31–13:41 IST) and after them (13:53–14:04 IST, servers restarted on `8bbc735`, Prod re-scored); keyboard walk (Tab × 200) and feature checks; a manual order-dialog check in the in-app
browser.

| ID | Check | Result | Evidence |
|---|---|---|---|
| V4-1 | DESIGN.md five stages | PASS | Empathise, Define (P1–P5), Ideate, Prototype, Test |
| V4-2 | Tokens once; no hard-coded colours | **FAIL (VF5-16, open)** | 369 token definitions; **822 literal colours** outside them in 28 files (server.py 341, atip.css 151, stock_view.py 85, w39_page.py 56 …) |
| V4-3 | Contrast WCAG AA | PASS | 0 axe violations on 168/168 runs (both servers, both themes, both widths) |
| V4-4 | Colour-blind: arrow / label / icon | PASS | every flagged item is a labelled value or a word ("Stop Loss ₹1,494.88", "1 need attention", "INCOMPLETE"); P&L carries ▲ + / ▼ − |
| V4-5 | Tabular numerals, right-aligned | PASS | 0 visible numeric cells failing; text fractions/ranges ("1/3", "₹0 – ₹4,00,000", "72% (54/75)") stay left by design |
| V4-6..15 | KPI strip, 10-card grid, sortable/sticky tables, sparklines, tooltips, drawer, CMP flash, stamp, stale warning, skeletons | PASS | /home 5 KPI tiles; 10 `.ax-tcard`; Table view 11 sortable sticky headers; 7 sparklines on /home (Prod); score badges carry the formula; stock panel opens on a row; `ax-tick-up/down`; "Updated 13:15:08 IST"; stale pill "Feed stale · last tick 21 min ago"; skeletons in raw HTML, none left visible |
| V4-16 | Order dialog; Cancel sends nothing | PASS | Prod "/" Buy PVRINOX, 6 sh: dialog "Save this buy rule? ▲ BUY PVRINOX PAPER · Quantity 6 shares · Trigger price ₹1,577.30 · Order value ₹9,463.80 · Stop-loss ₹1,545.00 (−2.05%)", focus on "Not now"; Not now → `order_log` 46 → 46, `order_rules` 70 → 70. The ticket refused 10 sh (₹15,773 > `max_order_value` ₹10,000) before any dialog (`screenshots/verify-2026-11-05/order-safety/`) |
| V4-17 | Light/dark on every tab | PASS | `data-theme` applied 168/168 |
| V4-18 | No sideways scroll at 375 | PASS | 84/84 runs per server |
| V4-19 | Keyboard reaches all controls | PASS | before / after the fixes: Prod /home 143/143 → 140/140, /sessions 77/77, "/" 122/122 → 119/119 (the re-scored copy has fewer rows); Test 43/43, 57/57, 69/69; focus always visible; no trap |
| V4-20 | Screenshots | PASS | `docs/testing/screenshots/verify-2026-11-05/web/` (after fixes), `web/before-fixes/` (176), `mobile/`, `order-safety/` |

### V5 — tests

| ID | Check | Result | Evidence |
|---|---|---|---|
| V5-1 | Full suite on master `0ab3de9` | **FAIL → fixed (VF5-07)** | run 1 13:14:11–13:24:39 IST: 4 failed + 1 error — `test_the_wait_reason_says_rank_fill…` (passes alone), 3 × `test_w39_perf_budget` (sandbox refuses local sockets), `/settings` audit error (passes alone). Run 2 13:25–13:37: a *different* auto-pick test failed (`test_autopick_waits_when_the_days_buy_count_is_used`: "a buy of ACME is queued") + 2 MCP errors. Cause: a dashboard TestClient starts the order monitor, which in market hours auto-picks every 30 s into whichever test's DB is current ("order_rules monitor error: no such table" in the log). The 4 Nov runs were after 18:00, so it never showed |
| V5-2 | Full suite after the fixes | **PASS** | branch `8bbc735`, **in market hours** 13:52:03–14:01:38 IST (9 min 33 s): **2,543 passed, 65 skipped, 3 failed** — only the 3 `test_w39_perf_budget` socket tests, which pass 14/14 outside the sandbox (14:01:44). The two auto-pick tests that failed on master pass; `test_order_paths_are_untouched` passes on the committed tree (`evidence/v5/pytest_fixed_run1.txt`) |
| V5-3 | Hand-calculated formula tests per score | PASS | `test_score_audit_2026_11_03.py` (VPI updated to the calm V: 60.22), MRI, RRI, ZPI, CRI, ACS, ATIP, RS |
| V5-4 | 10-session regression, CRI↓ order, UI smoke, dialog flow | PASS | `test_board_has_ten_rows_and_says_ten_of_ten`, `test_rank_fill_orders_cri_ascending…`, `test_pages_render_the_kpi_strip…`, `test_every_order_send_path_asks_first…` |
| V5-5 | Mutation (scratch copy, restored, `diff -rq` clean) | PASS after VF5-08 | VPI V back to ATR×12: 3 tests fail; CRI ×15→×12: 2; board TARGET 9: 4; OCO pending sibling ignored: 1 (new test); `autopick.FILL_TARGET` 9: **survived** — nothing read it (removed, VF5-08). Sessions' real limit is the 10 configured sessions + the board's TARGET |

### V6 — mandatory items

| ID | Check | Result | Evidence |
|---|---|---|---|
| V6-1 | Apps: scores, labels, CRI↓, 10 sessions, tokens = web | PASS by construction | Android `WebView` / iOS `WKWebView` shells load `/home`, `/watchlist`, `/command`, `/sessions`, `/m?tab=live`, `/more`; app-view screenshots 390 × 844 and 412 × 915, both themes, user agent `ATIPApp/2026.11.05.1` |
| V6-2 | Build both apps | **BLOCKED (VF-16)** | `xcodebuild`: "requires Xcode, but active developer directory … is a command line tools instance"; no Android SDK; "Unable to locate a Java Runtime"; no gradle, no xcodegen |
| V6-3 | Apps updated with Phase 3 | DONE | 2026.11.05.1 (Android versionCode 9 / iOS build 9); no native change — the fixes reach the apps from the server |
| V6-4 | Tracker | DONE | phase 22 (8 task rows, 16 defect rows) + 16 corrected rows, sheet "Verification 2026-11-05" |
| V6-5 | Test and Prod | DONE | `docs/testing/ATIP_Verification_Test_Report_2026-11-05.md` |

## 3. Defect log (this pass)

Severity: High = wrong order / money / a score that misleads every user; Medium = wrong numbers on screen or a check that
fails; Low = cosmetic or rare.

| ID | Task | Sev | Evidence | Root cause | Fix | Re-verified |
|---|---|---|---|---|---|---|
| VF5-01 | V3 (VF-01) | High | VPI BROKEN OOS | V read ATR % ×12 as good; high-ATR stocks return less | V = 100 − ATR% ×12; VPI gates 76.5 / 66.5 | VPI WORKING, ATIP WEAK; 498/498 recompute; tests |
| VF5-02 | V1 (VF-04) | Med | 3 FAILED re-entries on 4 Nov | `max_orders_per_day` 10 = number of sessions | owner: 20; documented in `orders/risk.py`; the value is in the live `config.json` | Prod copy runs 20; **live: at deploy** |
| VF5-03 | V1 (VF-08) | Low | "Momentum (MRI)" on "/" and Command Center | MRI is a reversal index | "Momentum-reversal (MRI)"; tracker says ATIP-ranked | tests |
| VF5-04 | V2 | Low | MSI never had Options live | F&O job at 16:45 gets the previous session | evening re-score fetches the session's file first | test; live: next evening re-score |
| VF5-05 | V0 | Low | PCR-only row = "flows in" | row existence checked | `fii_net_cr IS NOT NULL` (scheduler + quality) | test |
| VF5-06 | V0 | Med | 2nd SELL possible after a stop fill | only ACTIVE siblings settled | ACTIVE + PENDING_CONFIRMATION | test; mutant killed |
| VF5-07 | V5 | Med | 2 different failures in 2 runs | TestClient's monitor thread in market hours | `ATIP_ORDER_MONITOR=off` (conftest, QA servers) | suite in market hours |
| VF5-08 | V5 | Low | mutant survived | `FILL_TARGET` never read | removed | — |
| VF5-09 | P1 | Low | DESIGN.md links broken in a clone | screenshots untracked | committed | — |
| VF5-10 | V3 | High (owner) | RRI BROKEN | no RRI component has an OOS edge | **not fixed** — the IS redesign failed the pre-set rule | owner |
| VF5-11 | V0 | Low | 21:00–21:30 watch slots never run | watch end after the stop | open (06:00 release covers it) | — |
| VF5-12 | V0 | Low | max-hold exits never fire | `held_sessions: None` | open (strategies DRAFT) | — |
| VF5-13 | V0 | Low | 2027 holidays missing | NSE not yet published | open | — |
| VF5-14 | V3 | Low | POLICYBZR −36 % crash masked | 35 % jump mask is not corporate-action-aware | open | — |
| VF5-15 | V2 | Info | TDPOWERSYS 3 Nov RRI, ICICILIFE 4 Nov row | stored before the repair / fix | re-score drops ICICILIFE | Prod |
| VF5-16 | V4 | Med | 822 colour literals | legacy pages | open — separate task | — |
| VF5-17 | V6 | Low | tracker XLSX has 463 rows, the CSV 582 | 119 older rows (phases 8, 10, 11, 19, R) were only ever in the CSV | open (phases 20–22 identical in both) | — |

Still open from 4 Nov (unchanged by this pass): VF-10, VF-12, VF-13, VF-14, VF-15, VF-17, VF-18 (owner), VF-21, VF-30,
VF-31, VF-33; VF-34 / VF-35 in the other session; VF-16 (tools).

## 4. Owner decisions applied

| Decision (5 Nov, 13:07 IST) | What this pass did |
|---|---|
| VF-01: fix if out of sample proves it; re-gate BUY | VPI flip passed the rule → applied, gates re-set in-sample. RRI redesign failed → not applied (VF5-10) |
| VF-08: keep the ATIP rank | No ranking change; MRI relabelled "Momentum-reversal"; tracker rows say "10 ATIP-ranked sessions" |
| VF-04: raise to 20 | Documented; Prod copy set to 20; the live `atip_data/config.json` change is part of the deploy (§5) |

## 5. What needs the owner

1. **Merge and deploy** `claude/verify-2026-11-05` to the live Mac, with `risk_limits.max_orders_per_day: 20` in the live
   `config.json`. Best after 15:30 IST and before the 16:49 scoring, so tonight's scores use the calm VPI; otherwise
   tomorrow. Not done by this pass: it restarts the trading app.
2. **VF5-10 RRI**: no component has an out-of-sample edge; drop RRI from ATIP, or redesign it with new inputs.
3. **VF5-16**: tokenise the 822 legacy colour literals (a page-by-page task of its own).
4. **VF-16**: build the apps where Xcode and the Android SDK exist.
5. VF-18 session names ("S1 Momentum" etc.) still describe pools that all pick by the ATIP rank.

⚠️ *This analysis is for informational purposes only and does not constitute financial advice. Past performance is not
indicative of future results.*
