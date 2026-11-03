# CHANGES — full code review, scores, backtest, design system (3 Nov 2026, prompt 10:54 PM IST)

Branch `claude/full-review-2026-11-03`, from master `29b488e`. This is a delta patch: every file was changed in place and
none was rewritten whole. Unzip at the project root (`~/ATIP`); all paths are relative to it. Finding IDs (O-, D-, P-,
I-, Q-) refer to [CODE_REVIEW_REPORT.md](CODE_REVIEW_REPORT.md).

## Order engine (Task 0)

| File | Change |
|---|---|
| `orders/rules.py` | **O-H1:** `check_triggers` re-reads each triggered rule before acting, and `execute_rule` sends only for an ACTIVE or PENDING rule (`SKIPPED` otherwise), so a stale OCO sibling is never sold twice. **O-H2:** a failed STOP / TARGET stays ACTIVE (`retrying: …`, one Telegram message) and becomes FAILED only when the shares are gone. While the kill switch is on, automatic sends are held (`HELD_HALTED`). **O-M4:** `confirm_rule` expires stale confirmations first. |
| `orders/broker.py` | **O-M7:** a MARKET order with no live quote is valued at the reference price (stored quote, else last close). A LIVE buy that still has no value is refused. |
| `orders/autopick.py` | **O-M3:** the same floor and free cash as `sessions.gate()`. **O-M2:** a session whose entry FAILED today waits for the next trading day, and the board says why. |
| `orders/session_board.py` | **Task 1:** each row's ATIP rank on the latest scored day (`rank`, `rank_of`; ATIP desc, CRI asc); `counts` (holding / queued / planned / waiting) and the `detail` line. |
| `dashboard/server.py` | **D-H1:** `confirmRule` opens `AX.confirmOrder` before sending; the pending line shows shares (≈ for a pool buy) and the stop; `/api/orders/pending` adds `est_qty` / `est_value`. **O-M5:** `POST /api/orders` refuses a non-ENTRY role. **D-M1:** news headlines and order errors escaped. **D-M2:** Global panel = the newest row. **D-M3:** Portfolio = the latest holdings snapshot. **D-M4:** live quotes NaN-safe. Live CMP cells flash on change and refresh the freshness stamp. |
| `dashboard/command_page.py` | **D-H1:** Command Center's "Confirm & send" opens the dialog first and disables the button while it sends. |
| `dashboard/command_more.py` | **D-H2:** the ticket preview has a blocking "Buy only on a BUY signal" check (pool sessions exempt). **D-M5:** an exact signal lookup. |
| `dashboard/w39_retail_page.py` | **D-M7:** basket placement and "Run due SIPs" use the shell's dialog, listing every leg, value and the environment. |
| `dashboard/static/atip/workspace.js` | **D-H1:** the order ticket's Save shows the dialog (symbol, quantity, price, value, stop, target) first. |

## Data, pipeline, alerts, scores engine (Task 0 / Task 2)

| File | Change |
|---|---|
| `data/ema_gap.py` | **I-H1:** archive rows are put on the basis of every reconciled later split / bonus at insert (source `bhavcopy_archive_adj` when adjusted). `repair_unadjusted()` + `python -m data.ema_gap --repair [--apply]`: exact and idempotent (TDPOWERSYS: 165 rows). |
| `data/technical.py` | **I-M2:** no 200-DMA is stored NULL, not 0; the technical score's Trend only when known. |
| `data/bhavcopy.py` | **I-M3:** the FII/DII write upserts the six flow columns, so the PCR in the same row is kept. |
| `scores/engine.py` | `last_price_date()`: scoring defaults to the last trading day, and a day with no bars or indicators is `NO_DATA`. `determine_signal`: MH 0 is not 50, and MRI None no longer raises. **P-M7:** rows are computed first and written in one short transaction. |
| `pipeline/scheduler.py` | **P-M5:** `run_postmarket` holds `job_lock("postmarket_pipeline")`. **P-M3:** each post-market alert runs on its own. **P-M6:** FII/DII watch and health check debounced. **P-H1:** holiday-calendar alarm at start. |
| `alerts/telegram.py` | **P-M4:** one dedupe key per alert kind per day. **P-M3:** NULL TOD / MH scores show "—"; no trade plan without a close. |
| `utils/trading_calendar.py` | **P-H1:** `calendar_gap()` (the list is missing this year, or next year in December). |
| `strategy_engine/kinds.py` | **Q-H1:** `max_hold_exit` with an unknown hold is not an exit. |
| `quant/approval.py` | **Q-H2:** the direction is not applied twice. |
| `quant/engine.py`, `quant/composite.py` | **Q-M9:** percentiles of the normalised values. |
| `quant/research.py`, `ml/validation.py` | **Q-H3:** tie-averaged ranks; NEUTRAL = 0. |

## Design system (Task 4)

| File | Change |
|---|---|
| `dashboard/static/atip/atip.css` | Dark by default (comment); `.ax-updated` stamp (amber ⚠ when stale; hidden in a narrow top bar); `.ax-tick-up/down` flash (off under reduced motion); `.ax-kpis` strip; `.ax-score` badges; `.ax-sgrid` / `.ax-tcard` session cards; spacing scale. |
| `dashboard/static/atip/atip.js` | Theme: dark unless light / auto is chosen. `AX.confirmOrder` (the one order dialog). `AX.flash`, `AX.istNow`, `AX.stamp`. The feed pill's title and the stamp follow `feed.updated`. |
| `dashboard/shell.py` | The head script sets dark before first paint; the top bar's "Updated HH:MM:SS IST" stamp. |
| `dashboard/ux_kit.py` | `feed_label()` returns `updated` (HH:MM:SS IST). |
| `dashboard/ux_pages.py` | Home: the KPI strip (market health, Nifty 50, sessions filled, today's P&L, freshness) and a compact ten-session grid. Sessions: the card grid (status chip, rank, why, badges, levels, P&L, wins) with the table under "Table view". |
| `dashboard/score_glossary.py` | `FORMULAS` (each score in one line), `badge()` / `badges()`; MRI / ATIP / ZPI wording corrected; the BUY rule states CRI < 60. |
| `DESIGN.md` (new) | The five design-thinking stages, tokens, patterns, test walk. |

## Mobile, tests, reports

| File | Change |
|---|---|
| `mobile/android/app/build.gradle.kts`, `MainActivity.kt` | 2026.11.03.3 (versionCode 7); `MODE_NIGHT_YES` (dark by default). |
| `mobile/ios/project.yml`, `ATIP/ATIPApp.swift`, `ATIP/WebScreen.swift` | 2026.11.03.3 (build 7); `.preferredColorScheme(.dark)`. |
| `mobile/README.md` | What changed on the phone. |
| `tests/test_full_review_2026_11_03.py` (new, 27 tests) | Order engine (H1, H2, halt, expiry, LIVE no-price, route refusal), board rank / counts / CRI ascending, failed-entry wait, MH 0, the scoring default day and NO_DATA, 200-DMA NULL, archive basis + repair, PCR kept, TOD alert NULLs, the calendar alarm, the dark default, the feed stamp, badges, every order path asks, page smoke for every tab, the ticket's Buy-on-BUY check, tie ranks, max-hold. |
| `tests/test_ux_console.py` | The preview test seeds a BUY signal (the ticket now buys only on one). |
| `tests/test_ui_fixes_v2.py` | The GIFT slice starts after the KPI strip's "Market health"; the order-path guard allows `orders/rules.py` and `orders/broker.py` (this review). |
| `docs/reports/2026-11-03-fullreview/*` | CODE_REVIEW_REPORT.md, SCORE_AUDIT_REPORT.md, BACKTEST_VALIDATION_REPORT.md, this file, JSON evidence, analysis scripts. |
| `docs/testing/ATIP_Full_Review_Test_Report_2026-11-03.md` | Testing report (Test + Prod). |
| `ATIP_MASTER_TRACKER.xlsx`, `docs/ATIP_MASTER_TRACKER.csv` | Phase 20 rows + "Status 2026-11-03 full review" sheet (backup `ATIP_MASTER_TRACKER.backup-2026-11-03-before-fullreview.xlsx`). |
| `PATCH_NOTES.md` | 3 Nov night entry. |

## Rollout notes

* **No schema change.** One data repair: `python -m data.ema_gap --repair --apply` (TDPOWERSYS, 165 rows), run after the
  pre-release backup. TDPOWERSYS's indicators correct themselves from the next EOD run.
* **Behaviour changes to know about:**
  - every Confirm now asks once more (the dialog);
  - the order ticket refuses a Buy on a stock without a BUY signal (pool sessions are not affected);
  - a session whose entry failed waits until the next trading day;
  - Telegram VIX / selloff / CRI / TOD alerts come once a day;
  - the dashboard and apps open dark.
* **Not changed (owner decisions):**
  - the VPI ATR flip and BUY-gate re-set;
  - a server-side Buy lock;
  - moving the evening jobs out of the 21:00 stop window;
  - the 2027 NSE holidays (NSE publishes them in December).
