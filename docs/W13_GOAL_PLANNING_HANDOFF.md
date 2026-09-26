# W13: Goal planning engine (ATIP-GOL-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run. ChatGPT testing is pending.
**Branch:** `w13-goal-planning` (on top of W12). Not merged, not deployed.

## Scope delivered

| Requirement | Where (`wealth/goals.py`) |
|---|---|
| Goals: type (retirement, child education, house, vehicle, wedding, travel, emergency fund, wealth creation, custom), priority (essential / important / aspirational), target in today's rupees, target date | `_clean()`, `create()` |
| Target corpus with inflation (goal-type defaults: education 10%, house / wedding 7%, vehicle 5%, travel 6%, else config 6%) | `evaluate()` |
| Retirement corpus from monthly expense, years in retirement and post-retirement return (inflation-growing annuity) | `retirement_corpus()` |
| Emergency fund from months × Investor-DNA monthly expenses, assumed held in liquid assets | `_target_today()`, `_assumptions()` |
| Funding: an amount already saved plus linked W12 positions (share of each); over-linking warning | `_funding()`, `link_warnings()` |
| Contributions with an annual step-up; projected corpus; shortfall / surplus | `project()` |
| Required monthly contribution; required CAGR (bisection); lump sum today that closes the gap | `required_monthly()`, `solve_return()` |
| Success probability: seeded, reproducible Monte Carlo (numpy), with P10 / P50 / P90 | `monte_carlo()` |
| Scenarios: pessimistic −3%, base, optimistic +2%, inflation +2%, contributions stop; what-if simulation without saving | `evaluate()`, `simulate()` |
| Glide path: growth-asset share by years left, capped by the DNA band | `evaluate()` |
| Return assumption: goal value, else the W14 allocation's expected return (once W14 exists), else the DNA band default capped by horizon (under 3 years: conservative; under 5: moderately conservative) | `_assumptions()` |
| Status ON_TRACK / AT_RISK / OFF_TRACK from success probability; ACHIEVED when already funded | `evaluate()` |
| Risk requirement feed-back to Investor DNA: the future-target-weighted required return of essential + important goals | `required_return_for_owner()`; DNA `POST /api/wealth/dna/refresh` |
| History: change events; stored daily projections | `wealth_goal_event`, `wealth_goal_projection` |

The math was checked against closed forms: the level-SIP future value equals `P((1+r)^n−1)/r`, the solved return reproduces the input rate, and the required SIP reproduces the input SIP.

## Interfaces

- Tables: `wealth_goal`, `wealth_goal_event`, `wealth_goal_projection`.
- API: `/api/wealth/goals` (list / create), `/goals/overview`, `/goals/{id}` (GET / PUT / DELETE→ABANDONED), `/goals/{id}/status | simulate | projection | projections | events`, `POST /api/wealth/dna/refresh`.
- Page: tab "Goals" (`dashboard/wealth_ui/goals_tab.py`); "Refresh from goals" button on the DNA tab.

## Notes for QA

- The deterministic projection uses the average (expected) return. The Monte Carlo median is lower because of volatility drag, so a goal can show a positive gap while it is OFF_TRACK. The evaluation then carries a `note` that explains this.
- Monte Carlo defaults to 2,000 paths (`wealth.monte_carlo_paths`, 100–20,000). The seed comes from the goal id and its inputs.
- Tax is excluded (brief), so all figures are pre-tax.
