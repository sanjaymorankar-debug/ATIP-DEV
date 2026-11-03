**Date of prompt:** 3 Nov 2026 Time 09:23 PM > (work and testing 09:25 PM – 10:01 PM IST; every time in this report is IST)

# Ten sessions + score audit: testing report, 3 Nov 2026

Branch `fix/sessions-score-audit-2026-11-03` (not merged, not deployed to the live Mac). PAPER throughout. Nothing was
sent to Dhan. The live `atip_data/atip.db` was only read: by file copy at 09:26 PM and through the SQLite backup API,
read-only, at 09:52 PM.

## 1. Where it ran

| "Server" | What it really was | Database | When |
|---|---|---|---|
| **Test** | Local instance, `ATIP_ENV=test`, `ATIP_OPERATING_SCHEDULE=off`, port 8795 | fresh `main.py --init` database (empty) | 09:53–10:10 PM |
| **Prod** | Local instance, `ATIP_ENV=staging`, port 8796. **No hosted Prod server exists**; this is the same arrangement as the 2 and 3 Nov reports | snapshot of the live `atip.db` (09:52 PM), with 3 Nov **rescored by the new engine** (499 symbols, 09:52 PM) | 09:53–10:10 PM |
| **Backtest** | `scores/validation.py` | copy of the live `atip.db` (09:26 PM) | 09:30–09:55 PM |

## 2. Results at a glance

| Check | Result |
|---|---|
| Full pytest suite, before the change (baseline, 09:31 PM) | 11 failed. All of them are pre-existing and environmental: 6 `test_asia_markets` (stored Asia data is stale; no network in the sandbox), 1 `test_mysql_backend` reserved word, 1 `test_operating_schedule` lock, 3 `test_w39_perf_budget` (the sandbox refuses local sockets) |
| Full pytest suite, after (09:51 PM start) | **2,424 passed, 62 skipped, 10 failed, 0 errors** (09:51–09:59 PM). The 10 failures are the same environmental ones as the baseline (6 asia, 1 mysql, 3 perf-budget sockets). `test_operating_schedule` passed this time; it is timing-sensitive. **No new failure.** |
| New tests `tests/test_score_audit_2026_11_03.py` | **25 / 25 passed** |
| `tests/test_autopick.py` (existing, 13 tests) | **13 / 13 passed**, unchanged |
| Prod `/api/sessions/board` | 200; **"10 of 10 sessions filled (10 rank-filled (no BUY signal free))"**; 10 rows, each with symbol, rationale, score snapshot, entry < target, stop < entry, status, P&L, wins / closed |
| Test `/api/sessions/board` | 200; "0 of 10 sessions filled — S1…S10: no starting amount: set one up on /sessions" (an empty DB has no pools). It says why instead of showing nothing |
| `/sessions`, `/sessions?app=1`, `/`, `/home`, `/command`, `/api/sessions`, `/api/v2/sessions` on both | all 200 |
| `/sessions?app=1` at 375 × 812 (phone, app mode), Prod | board stacks into labelled cards; no horizontal scroll (`scrollWidth` 375) |
| CRI↓ on the score pages (Prod) | header `CRI↓` on the scores table, the signal history, the stock score history and the board; low CRI coloured strong |

## 3. Test cases

| # | Case | Where | Expected | Result |
|---|---|---|---|---|
| T1 | minmax / weighted_score by hand, including NaN | unit | 50 / 80 / 100; NaN → 50; renormalised 62.86 | PASS |
| T2 | VPI by hand | unit | 48.80 | PASS |
| T3 | MRI by hand (MACD reversal, ADX straight) | unit | 54.5; stronger ADX scores higher; rising MACD scores lower | PASS |
| T4 | MRI with NULL EMA / MACD | unit | 65.56 (left out, not 0) | PASS |
| T5 | RRI by hand | unit | 55.0 | PASS |
| T6 | ZPI by hand (calm ATR, straight ADX) | unit | 66.67 | PASS |
| T7 | ZPI for a listing without a 200-DMA | unit | 60.77 (Support and Trend left out) | PASS |
| T8 | CRI by hand; CRI without a 200-DMA | unit | 60.0; 55.38 | PASS |
| T9 | Relative strength vs Nifty 50 | unit | 66.67 | PASS |
| T10 | ATIP composite with SAFETY | unit | 58.75; lower CRI gives a higher ATIP | PASS |
| T11 | ACS by hand | unit | 61.67 | PASS |
| T12 | Composite weights sum to 1 (ATIP 1 before SAFETY) | unit | true | PASS |
| T13 | BUY needs CRI < 60; CRI 0 is the safest; CRI 80 with weak ATIP → SELL | unit | BUY / BUY / HOLD / SELL | PASS |
| T14 | CRI colour band | unit | CRI 20 strong, CRI 85 weak | PASS |
| T15 | **CRI ascending** in rank-fill among equal ATIP; CRI ≥ 75 excluded | DB | LOWRISK, MIDRISK, HIGHRISK | PASS |
| T16 | **Ten sessions** picked when 12 eligible stocks exist and there is no BUY signal | DB + paper broker | 10 distinct, best first, RANK-FILL | PASS |
| T17 | BUY signals first, then rank-fill | DB | ACME, BETA, GAMMA, then 7 fills = 10 | PASS |
| T18 | Board: 10 rows, "10 of 10", all fields, target > entry > stop, CRI note | DB | as stated | PASS |
| T19 | Board with 6 eligible | DB | "6 of 10 sessions filled — S7, … : reason" | PASS |
| T20 | Rank-fill switched off | DB | session waits | PASS |
| T21 | IC t-stat uses n/h | unit | 4.90 (h 1), 2.45 (h 4) | PASS |
| T22 | Forward return open(t+1) → close(t+h); a jump window is dropped | unit | 12/11 − 1; NaN | PASS |
| T23 | Spearman IC | unit | +1 / −1 | PASS |
| T24 | Existing auto-pick behaviour (BUY first, no duplicates, cancel stale, PAPER only, halted, HOLD cut-off) | DB | unchanged | PASS (13/13) |
| T25 | Prod: 3 Nov rescored with the new engine | Prod | 499 scored; signals HOLD 452 / WAIT 47 (old: HOLD 291 / WAIT 208); still 0 BUY (best ATIP 64.8 vs gate 65) | PASS (expected) |
| T26 | Prod board and page | Prod | 10 of 10 filled, rationale and scores present | PASS |
| T27 | Test board and page on an empty DB | Test | 0 of 10 with the reason | PASS |
| T28 | Phone width, app mode | Prod | no horizontal scroll; labelled cards | PASS (after one fix: the first layout pushed values off-screen; fixed and re-checked) |

## 4. Backtest metrics, before vs after (point-in-time, out of sample 3 Jun – 3 Nov 2026)

| Score | Verdict before → after | 5d IC before → after | 20d IC before → after | 20d decile spread % before → after | 5d top-decile hit % / expectancy % before → after |
|---|---|---|---|---|---|
| ATIP | BROKEN → BROKEN | −0.009 → **+0.017** | +0.008 → **+0.031** | −0.33 → **+0.58** | 41.3 / −0.87 → 43.8 / −0.45 |
| VPI | BROKEN → BROKEN | +0.006 → +0.006 | +0.032 → +0.032 | +0.40 → +0.40 | 41.8 / −0.69 (unchanged) |
| MRI | **INVERTED → WEAK** | −0.031 (t −2.1) → **+0.018** | −0.037 → **+0.032** | −1.01 → **+0.96** | 39.9 / −0.72 → 43.1 / −0.38 |
| RRI | BROKEN → BROKEN | −0.003 → −0.003 | +0.015 → +0.015 | −0.14 → −0.14 | 42.1 / −0.77 (unchanged) |
| ZPI | BROKEN → **WEAK** | −0.012 → **+0.020** | −0.019 → **+0.028** | −0.54 → **+0.33** | 40.7 / −0.64 → 42.8 / −0.38 |
| CRI↓ | WORKING → WORKING | −0.031 | −0.014 | +0.59 (riskiest decile outran the safest at 20d) | 41.3 / −0.50 |
| ACS | BROKEN → WEAK | +0.006 → +0.020 | +0.014 → +0.039 | −0.46 → +0.65 | 41.0 / −0.68 → 42.9 / −0.45 |

CRI's 1-day IC is −0.045 (t −4.4), which is the strongest relationship in the set. It fades by 20 days. Full tables
(in-sample, quintiles, SELL hit rates, turnover, correlation) are in
`docs/reports/2026-11-03/SCORE_VALIDATION_REPORT.md`.

## 5. Open issues

| # | Issue | Severity | Owner action |
|---|---|---|---|
| O1 | **No score makes money long-only after costs out of sample.** The 35 stored BUY signals won 36 % at 5 days (−1.28 % net). Rank-fill fills the sessions as asked, but on this evidence those trades are expected to lose. They are PAPER only | High | Decide whether to keep rank-fill on (`session_settings.autopick_rank_fill`) |
| O2 | BUY is still unreachable: 0 BUY on rescored 3 Nov (ATIP max 64.8 vs 65). The gates were **not** lowered, because the evidence does not support it | Medium | Re-calibrate only with the validation harness |
| O3 | 237 corporate actions pending; `adj_close` NULL everywhere | Medium | Run the corporate-action reconcile |
| O4 | ACS–ATIP ρ 0.87, ATIP–RRI 0.85 (redundant). ACS Agreement double-counts ATIP inputs | Low | Redesign ACS Agreement |
| O5 | Scores use fixed-range min-max, not per-date percentile ranks. That is safe from look-ahead but not cross-sectionally normalised | Low | Separate change (it moves every gate) |
| O6 | Android and iOS apps not built: no Android SDK or Xcode in this environment. They are thin web shells; the pages were tested at phone width | Medium | Build both and install |
| O7 | Not deployed to the live Mac (port 8000); the branch is not merged | — | Review, merge, restart the dashboard, rescore |
| O8 | The survivorship-biased universe (today's Nifty 500) in the backtest | Low | Needs point-in-time constituent history |
| O9 | Pre-existing suite failures (Asia data staleness, MySQL reserved word, perf-budget sockets) | Low | Unchanged by this patch |
