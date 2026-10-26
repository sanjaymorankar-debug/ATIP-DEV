# ATIP review, 26 October 2026: deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | Merged and committed code; ATIP restarted and running | `master` at the merge commits f84e61d (PR #17 @ 88cd402), 912e654 (Mac fixes), f6ceb25 (PR #17 update 5ee26cb), plus the fix commits; LaunchAgent run #4 |
| 2 | Validation and testing report (pass/fail per test, per tab; Test and Prod) + Session Findings | [01_SESSION_FINDINGS_AND_VALIDATION.md](01_SESSION_FINDINGS_AND_VALIDATION.md), `evidence/tests_*.csv`, `evidence/smoke_*.json` |
| 3 | Pre/post-market and news intelligence audit (status table) | [02_INTELLIGENCE_AUDIT.md](02_INTELLIGENCE_AUDIT.md), `evidence/counterfactual_2026-10-26.json` |
| 4 | Gap analysis, prioritised, with effort and files | [03_GAP_ANALYSIS.md](03_GAP_ANALYSIS.md), `evidence/signal_quality.json` |
| 5 | Trade and profit feasibility to Dec 2027 | [04_FEASIBILITY_TO_DEC_2027.md](04_FEASIBILITY_TO_DEC_2027.md), `evidence/phase4_backtests.json`, `evidence/dhan_margins_2026-10-26.json` |
| 6 | Delta patches (zip) | `deliverables/2026-10-26/ATIP_delta_patches_2026-10-26.zip` in the project folder (not in git) |
| — | Product tracker, every feature's status | `docs/ATIP_MASTER_TRACKER.csv` (columns "Status 2026-10-26", "Evidence 2026-10-26"; 350 rows: DONE 122, IN PROGRESS 182, BLOCKED 32, GAP 14) and sheet "Status 2026-10-26" in `ATIP_MASTER_TRACKER.xlsx` |
| — | Mobile apps (Android / iOS PWA) | `dashboard/w38_page.py`: Data feeds card, service worker `atip-shell-v2` (01 §7) |

Owner actions that block the most: subscribe to Dhan's Data APIs; keep the Mac awake from 07:00 on trading days;
decide hosting for a test server (R10); fund the account before any live stage (₹0 available today).
