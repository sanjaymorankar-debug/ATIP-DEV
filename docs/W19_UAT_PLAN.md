# W19: Beta / user acceptance (ATIP-UAT-001) plan and handoff

**Status:** UAT TOOLING IMPLEMENTED (not verified). **UAT NOT EXECUTED:** acceptance is by real users / ChatGPT, not development.
**Branch:** `w19-uat` (on top of W18). Not merged, not deployed.

## 1. What development delivered for UAT

| Item | Where |
|---|---|
| Four realistic personas in a separate `uat` tenant (never the house book or the owner's data): `conservative_retiree`, `young_accumulator`, `family_planner`, `active_trader` | `wealth/uat.py` |
| Seed / reset / list | `python -m wealth uat-seed --persona all`, `uat-reset`, `uat-list` |
| Persona view on a single-user install: config `"wealth": {"uat_owner": "uat:persona_young_accumulator"}`. Ignored with enterprise on; only the `uat` tenant is accepted. The page shows an amber "UAT persona" banner | `wealth/uat.uat_owner_from_config`, `dashboard/wealth_routes.owner()` |
| In-page feedback on every tab (category BUG / UX / DATA / METHODOLOGY / IDEA, severity P0–P3, tab and width captured) | `POST /api/wealth/feedback`, "Feedback" button |
| Advisor answer rating (helpful / not helpful + note) | W16 |
| Triage | `python -m wealth feedback [--status NEW]`, `python -m wealth triage --id … --status TRIAGED\|FIXED\|WONT_FIX\|DUPLICATE` |

**Smoke (scratch copy):** all four personas seeded and ran the full investor cycle without a failed step. Their profiles came out as designed:

| Persona | Band | Flags |
|---|---|---|
| conservative_retiree | CONSERVATIVE | REQUIREMENT_EXCEEDS_PROFILE |
| young_accumulator | AGGRESSIVE | none |
| family_planner | BALANCED | REQUIREMENT_EXCEEDS_PROFILE |
| active_trader | MODERATELY_AGGRESSIVE | TOLERANCE_EXCEEDS_CAPACITY and REQUIREMENT_EXCEEDS_PROFILE |

## 2. Controlled beta

1. Run it on a **copy**, not the live `D:\Projects\ATIP`. Use the dev worktree with its own `atip_data` (a copy of the production database), on a spare port.
2. Seed the personas; give each tester one persona via `wealth.uat_owner`.
3. Keep `wealth.enabled` false unless the scheduled cycle itself is under test.
4. Keep `wealth.advisor_llm_enabled` false unless narration is under test. Narration sends the persona's evidence pack to Anthropic.

## 3. Journeys and acceptance criteria

| # | Journey (persona) | Steps | Accept when |
|---|---|---|---|
| J1 | First run (a new owner with no data) | Open /wealth → Overview | "Next step: answer the Investor DNA questionnaire"; no errors on any tab |
| J2 | Investor DNA (any) | Answer, Preview, Save, change an answer, Save | Band, scores and every explanation row are understandable; version 2 in history; flags make sense to the tester |
| J3 | Wealth (family_planner) | Review net worth, add a holding of each supported class, try a mutual fund, add a liability, override a classification | Values match the tester's expectation; MF / FD / NPS / insurance refused with the scope message; stale prices visibly flagged |
| J4 | Goals (young_accumulator) | Open each goal, run what-ifs (SIP, date, target), store a projection | Numbers agree with an independent calculator (tolerance 0.5%); scenarios and the Monte Carlo note are clear |
| J5 | Allocation (conservative_retiree) | Preview; set a bound; exclude silver; toggle tactical | The target respects bounds and the risk guard; every tilt traces to a signal with date and source |
| J6 | Rebalance (family_planner) | Check; build to_band, to_target, cash_flow plans; accept one; dismiss one | Legs, costs and the tax-neutral note are clear; cash_flow never sells; decisions cannot be reversed |
| J7 | Performance (active_trader, and the house PAPER book) | Build a report; download CSV; add a trade; void it | The four returns are side by side with definitions; fees reconcile; the CSV matches the screen; a void is visible and excluded |
| J8 | Advisor (each persona) | Ask all suggested questions and three free questions; rate them | Every statement shows evidence; no suggestion conflicts with the profile; "cannot place orders" present |
| J9 | Integration (any) | Overview; switch Investor ↔ Trader mode; run the cycle | Mode persists; trader pages unchanged; cycle SUCCESS; alerts in the dashboard's alert panel (when `wealth.enabled`) |
| J10 | Regression (trader) | Use `/`, `/strategies`, `/trading`, `/backtests`, `/ml`, `/quant` as before | No behaviour change (the wealth track adds routes only) |
| J11 | Mobile | Repeat J1, J2 and J8 on a phone-width browser | No horizontal page scroll; forms usable |

## 4. Defect triage

| Severity | Meaning | Rule |
|---|---|---|
| P0 | Unsafe (could cause a trade, expose another tenant's data, lose data) or blocks a journey | Fix before release; stop the beta |
| P1 | Wrong number or misleading advice | Fix before release |
| P2 | Works but confusing / slow | Fix or accept with a note in KNOWN_ISSUES |
| P3 | Cosmetic / idea | Backlog |

- Feedback moves NEW → TRIAGED → FIXED / WONT_FIX / DUPLICATE.
- Every fix is noted in `docs/KNOWN_DEFECTS.md` with the feedback id.

## 5. Exit criteria (gate to W20 release)

1. All journeys J1–J11 accepted by at least one tester.
2. Zero open P0 / P1.
3. Every P2 fixed or accepted in KNOWN_ISSUES.
4. The W18 suite passes.
5. A methodology review (DNA-1.0, AAL-1.0, GOAL-1.0, RBL-1.0, PERF-1.0) is recorded, ideally with a SEBI-registered adviser.
