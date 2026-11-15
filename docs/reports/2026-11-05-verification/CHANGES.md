# CHANGES — Phase 3 fixes of the re-verification (5 Nov 2026; prompt 4 Nov 06:00 PM IST, run 5 Nov from 01:05 PM IST)

Branch `claude/verify-2026-11-05` (worktree `.claude/worktrees/verify-2026-11-05`), cut from master `0ab3de9` (the code the live
Mac has run since 23:45 IST on 4 Nov). This is a delta patch: every file is changed in place. Unzip at the project root
(`~/ATIP`). Defect IDs (VF5-xx) refer to the defect log in [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) §3. Code commit
`8bbc735` (13:51 IST). **Not merged and not deployed** — see the rollout notes.

## Scores (V2, V3)

| File | Change |
|---|---|
| `scores/engine.py` | **VF5-01** (VF-01, owner decision): VPI's V component is `100 − min(ATR% × 12, 100)` (calm = high), as ZPI's ATR already is. Point in time to 4 Nov, VPI's out-of-sample rank IC goes −0.004 → +0.027 (t 2.8) at 1 day and rises at 5 and 20; ATIP's rises at 1, 5 and 20. `BUY_STRICT["vpi"]` 70 → **76.5**, `BUY_LOOSE["vpi"]` 60 → **66.5**, re-set on the in-sample period to keep its BUY rate (2.42 → 2.53 a day). Nothing else in the gates changes. |
| `scores/formulas.py` | VPI's formula note says V is calm = high. |
| `dashboard/score_glossary.py` | VPI's meaning ("energy to move without the noise: calm volatility …") and formula ("100 − ATR % ×12 (calm) .18 …"). The BUY legend reads the engine's gates, so it now says VPI ≥ 66.5 / 76.5. |

## Sessions, orders, pipeline (V0, V1)

| File | Change |
|---|---|
| `orders/rules.py` | **VF5-06** `settle_oco_group` settles siblings that are ACTIVE **or PENDING_CONFIRMATION**. A target waiting for its tap survived a filled stop, and Confirm then sent a second SELL of a flat position. |
| `orders/risk.py` | **VF5-02** (VF-04, owner decision): the documented example says `max_orders_per_day: 20`. The value itself is in `atip_data/config.json` (not in git). |
| `orders/autopick.py` | **VF5-08** `FILL_TARGET` removed: nothing read it (its mutant survived every test). |
| `pipeline/scheduler.py` | **VF5-04** the evening re-score (`settle_session_flows`) runs the F&O pipeline for the session first, so MSI's Options (NIFTY PCR) is in live scores; the 16:45 job only ever found the previous session's file. **VF5-05** `_evening_data` counts the session's flows only when `fii_net_cr` is stored, not for a PCR-only row. |
| `data/quality.py` | **VF5-05** the market-inputs check reads the flows, not just the row. |
| `dashboard/server.py` | **VF5-07** `ATIP_ORDER_MONITOR=off` keeps the order-rules monitor thread from starting (tests, QA copies of the DB). **VF5-03** (VF-08) the "/" tab says "Momentum-reversal (MRI)". |
| `dashboard/command_more.py` | **VF5-03** Command Center's factor list says "Momentum-reversal (MRI)". |

## Tests

| File | Change |
|---|---|
| `tests/test_verify_fixes_2026_11_05.py` (new, 8 tests) | VF5-01 (calm VPI, gates, legend, glossary), VF5-03, VF5-04 (F&O before the re-score), VF5-05, VF5-06. |
| `tests/conftest.py` | **VF5-07** every test runs with `ATIP_ORDER_MONITOR=off`. |
| `tests/test_viewer_role.py` | The trader test switches the monitor back on; new `test_order_monitor_off_starts_none`. |
| `tests/test_score_audit_2026_11_03.py`, `tests/test_full_review_2026_11_03.py` | VPI by hand with the calm V (48.80 → 60.22); BUY examples use a VPI over the new loose gate. |
| `tests/test_ui_fixes_v2.py` | `orders/risk.py` allowed in the order-path guard. |
| `tests/test_ux_merge_regression.py` | The renamed MRI tab. |
| `tests/verification/independent_7scores.py` (new) | Independent recomputation of the 7 scores and 8 indicators for every scored stock (`--spec 2026-11-03` for rows scored before VF5-01). |
| `tests/verification/vf01_variants.py` (new) | The VF-01 decision run: RRI plan from in-sample ICs, IS/OOS ICs of every variant, the adoption rule, the BUY-gate re-set, top-10 returns. |
| `tests/verification/v3_reweights_is_oos.py` (new) | The 3 Nov MRI / ZPI / SAFETY changes vs the formulas they replaced, IS and OOS. |
| `tests/verification/v2_invariance.py` | The look-ahead copy also corrupts PCR, per-stock FII/DII, index changes and news confidence after D. |
| `tests/verification/recompute_scores.py` | The calm V. |

## Mobile (mandatory item 1)

| File | Change |
|---|---|
| `mobile/android/app/build.gradle.kts`, `mobile/ios/project.yml`, `mobile/ios/ATIP/WebScreen.swift` | 2026.11.05.1 (Android versionCode 9, iOS build 9). Web shells: the fixes reach the apps from the server; no native code changed. |
| `mobile/README.md` | What changed on the phone. |

## Docs and tracker

`docs/reports/2026-11-05-verification/*` (this file, VERIFICATION_REPORT.md, evidence/), `docs/testing/ATIP_Verification_Test_Report_2026-11-05.md`,
`DESIGN.md`'s 16 before/after screenshots now in git (**VF5-09**), `ATIP_MASTER_TRACKER.xlsx` + `docs/ATIP_MASTER_TRACKER.csv`
(phase 22, 16 corrected rows; backups `*-before-verification.*` with today's date, kept out of git like the earlier ones), `PATCH_NOTES.md`.

## Rollout notes

* **Model change.** From the first scoring run on the new code, VPI ranks calm stocks higher and the VPI BUY gates are 66.5 / 76.5.
  ATIP rises about 1–2 points on average; on 4 Nov's data the one BUY (PVRINOX, VPI 65.2) becomes HOLD and 20 stocks move from
  WAIT to HOLD. Stored scores before the deploy keep the old VPI (no back-fill).
* **Config on the live Mac:** set `"risk_limits": {"max_orders_per_day": 20, …}` in `atip_data/config.json` (owner, VF-04).
* No schema change. `ATIP_ORDER_MONITOR` is unset on the live Mac (monitor on, as before).
* **Not changed:** RRI (VF5-10, failed the OOS rule), session names (VF-18), VF-34 / VF-35 (another session).
