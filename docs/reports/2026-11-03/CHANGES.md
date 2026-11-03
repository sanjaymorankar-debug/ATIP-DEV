# CHANGES — ten sessions + score audit (3 Nov 2026, prompt 09:23 PM IST)

Branch `fix/sessions-score-audit-2026-11-03`. This is a delta patch. Unzip it at the project root (`~/ATIP`); every path
below is relative to that root. No file was rewritten whole.

## Task 1 — ten momentum sessions

**Where the count dropped.** The path config → `orders/sessions.py` (`MAX_SESSIONS = 10`, S1–S10 seeded) →
`orders/autopick.py` → `order_rules` / `session_rules` → `/api/sessions` → `/sessions` and the apps keeps all ten all
the way through. There is no `head(5)`, `LIMIT`, `[:n]` or front-end slice on the sessions. The count dropped at the
**pick**. `autopick.candidates()` took only the latest scored day's **BUY** signals, and on 3 Nov 2026 the scoring
produced **0 BUY of 499**: the BUY branch is all but unreachable, with 35 BUY in 22,011 stored rows. The HOLD fallback
is off by default, so all ten sessions waited, and nothing on screen said why. The dry run also showed only "trading is
halted" during the 21:00–06:00 IST stop window, which hid the plan.

| File | Change |
|---|---|
| `orders/autopick.py` | **Rank-fill**: after the BUY signals (and the owner's HOLD cut-off, if set), the next-ranked eligible stocks fill the free sessions. Ranking is ATIP desc, then **CRI asc**. Eligible means a HOLD signal, CRI < 75, price ≥ ₹20 and 20-day turnover ≥ ₹2 Cr. It is on by default and switched off with `session_settings.autopick_rank_fill = OFF`. Every pick carries its source (BUY / HOLD-CUT / RANK-FILL) in the event and the rule note. The dry run now shows the would-be picks while halted, flagged `held`. Live picking still stops when halted or not PAPER |
| `orders/session_board.py` (new) | `board()`: always ten rows. Each row has symbol, entry rationale, score snapshot (ATIP/VPI/MRI/RRI/ZPI/CRI↓/ACS + signal), entry / target / stop, status (HOLDING / QUEUED / AWAITING CONFIRMATION / NEXT PICK / WAITING + reason), P&L, booked, unrealised and successful trades (wins / closed). The board also carries the line "N of 10 sessions filled (k on BUY signals, m rank-filled) — reason". CLI: `python -m orders.session_board` |
| `dashboard/command_routes.py` | `GET /api/sessions/board` |
| `dashboard/ux_pages.py` | `/sessions`: "The ten sessions" board with its status line above the session cards. Under 700 px each row stacks as a labelled card (no sideways scroll) |

## Task 2 — score audit fixes (`scores/engine.py`)

| ID | Change |
|---|---|
| SV-02a | `above_200()`: no 200-day average (under 200 sessions) means **unknown**, not "below". Support, Trend and WeakTrend drop out of RRI, ZPI and CRI instead of scoring weak / +40 risk |
| SV-02b | `minmax(NaN)` is neutral; it used to return 100 |
| SV-02c | `determine_signal`: a CRI of 0 is the safest, not "missing = 100" |
| SV-03a/b | MRI: MACD read as **reversal**; ADX read straight (it was inverted). The old MRI ranked backwards out of sample |
| SV-03c/d | ZPI: ATR inverted (calm = buy zone); "Resistance" reads ADX straight |
| SV-04a | ATIP composite: new component **SAFETY = 100 − CRI** at weight 0.2 (`ATIP_SAFETY_WEIGHT`). It is seeded with INSERT OR IGNORE by `ensure_atip_safety_weight()` on the next scoring run, so an owner's edit is kept |
| SV-04b | BUY needs **CRI < 60** (`BUY_MAX_CRI`) |
| `scores/formulas.py` | Formula registry notes for MRI, ZPI, CRI and ATIP updated. The weights hash changes with SAFETY, so new signals are stamped with the new formula |

CRI↓ labels: `dashboard/server.py` (signal-history header), `dashboard/stock_view.py` (stock score history) and
`dashboard/w28_assets.py` (crash-risk list: "CRI↓ (riskiest first)").

## Task 3 — validation

| File | Change |
|---|---|
| `scores/validation.py` (new) | Point-in-time recompute through the engine's own functions. Forward returns open(t+1) → close(t+h), with corporate-action jumps masked. Per-date Spearman IC with an overlap-corrected t. Decile and quintile spreads, hit rates, turnover and stability, the correlation matrix, verdicts and the markdown report. `python -m scores.validation --db <copy> --tag after` |
| `docs/reports/2026-11-03/validation_before.json`, `validation_after.json` | Raw results |
| `docs/reports/2026-11-03/SCORE_VALIDATION_REPORT.md` | Backtest / validation report |
| `docs/reports/2026-11-03/SCORE_AUDIT_REPORT.md` | Spec table, findings, CRI↓ audit, verdicts |

## Task 4 — tests

| File | Change |
|---|---|
| `tests/test_score_audit_2026_11_03.py` (new, 25 tests) | Hand-calculated values for minmax, weighted_score, VPI, MRI, RRI, ZPI, CRI, ACS, RS vs Nifty and the ATIP composite. NaN, NULL EMA and no-200-DMA handling. The CRI-cap BUY gate. CRI colour band. **CRI ascending in rank-fill**. **Ten sessions picked when ≥ 10 eligible**. BUY first, then fill. The board's 10 rows and "10 of 10". The "6 of 10 … — reason" case. Rank-fill off. The IC t-stat, forward-return timing and the Spearman check |

## Mandatory items

| File | Change |
|---|---|
| `mobile/android/app/build.gradle.kts` | versionCode 6, versionName 2026.11.03.2 |
| `mobile/ios/project.yml`, `mobile/ios/ATIP/WebScreen.swift` | 2026.11.03.2 (build 6) |
| `mobile/README.md` | What changed on the phone (ten-session board, corrected scores, CRI↓) |
| `ATIP_MASTER_TRACKER.xlsx`, `docs/ATIP_MASTER_TRACKER.csv` | New rows for every item (phase 18). Backup: `ATIP_MASTER_TRACKER.backup-2026-11-03-before-scoreaudit.xlsx` |
| `docs/testing/ATIP_Sessions_Score_Audit_Test_Report_2026-11-03.md` | Testing report (Test + Prod) |

## Rollout notes

* Nothing changes the database schema. The only data write is one `weight_config` row (ATIP / SAFETY / 0.2), seeded by
  the next scoring run.
* The stored `ai_scores` keep their old values until each date is rescored (`python -m scores.engine --date YYYY-MM-DD`).
* Rank-fill trades HOLD stocks. The validation shows that no score's top decile made money after costs out of sample.
  Keep the sessions in PAPER, which is the only mode auto-pick runs in. Switch rank-fill off with
  `session_settings.autopick_rank_fill = OFF` if you prefer empty sessions to rank-filled ones.
