# W14: Dynamic asset allocation engine (ATIP-AAL-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run. ChatGPT testing is pending.
**Branch:** `w14-asset-allocation` (on top of W13). Not merged, not deployed.

## Scope delivered (`wealth/allocation.py`, methodology AAL-1.0)

| Requirement | Implementation |
|---|---|
| Strategic allocation from Investor DNA | Model portfolios at band mid-points (risk 10/30/50/70/90), linearly interpolated for the exact risk score |
| Goals and horizon | Goal-weighted horizon of essential + important goals, else the DNA horizon. It caps the risk score: under 3 years 20, under 5 years 40, under 7 years 60 |
| Liquidity | `LOW_EMERGENCY_FUND` raises the cash floor to 10% |
| Tactical allocation from ATIP intelligence | 10 signals: regime / MH score, NIFTY 50 vs 200-session average, India VIX, breadth, median CRI, median ZPI, FII score, global score, gold trend (+ risk-off), USD/INR. Each carries its score, value, date and source |
| Tilt sizing | Per class: clip(Σ signal × weight) × max tilt (config 10 pts) × (0.5 + risk/200). An equity tilt is offset in bonds 60% / cash 40% |
| Constraints | Band bounds, the owner's policy (tighter bounds, excluded classes, tactical on/off, max tilt), then water-filling normalisation to 100% |
| Risk guard | CMA expected return / volatility / correlations. If the 1-in-20 bad year (μ − 1.645σ) exceeds the DNA maximum annual loss, growth assets move to bonds 1 point at a time |
| Equity split | Large / mid / small by band, regime tilt, Midcap-150 vs NIFTY-50 60-session relative trend |
| Sector views | Strongest / weakest three sectors by median ATIP score (informational) |
| Explainability and audit | Steps (strategic → liquidity → tactical → constraints → risk guard), tilt contributions, stored runs with inputs hash, profile id and methodology |
| Feeds goals | `expected_for_owner(horizon)`: the strategic allocation's return / volatility for a goal's horizon. W13 now uses it. It never reads goals, so there is no recursion |

Real estate and "other" count in net worth but are not allocated.

## Interfaces

- Tables: `wealth_allocation_policy`, `wealth_allocation_run`.
- API: `GET /api/wealth/allocation`, `POST /allocation/run`, `POST /allocation/preview`, `GET /allocation/runs[/{id}]`, `GET /allocation/signals`, `GET /allocation/cma`, `GET|PUT /allocation/policy`.
- Page: tab "Allocation".
- Config: `wealth.tactical_max_tilt_pct`, `wealth.cma` (override `returns`, `volatility`, and `correlation` as `"A/B": ρ`).

## Smoke run (scratch copy, 2026-09-25 data)

- All ten signals had data. Examples: regime NEUTRAL (0), NIFTY below its 200-session average (−0.76), VIX 12.2 (+0.15), median CRI supportive (+0.54), gold trend +0.56.
- Target for risk score 71.8: equity 56.7, international 11.3, bonds 16.6, gold 9.5, silver 1.8, cash 4.0.
- Expected 10.5%, volatility 11.6%, bad year −8.6%, within the 20% limit.

## Known limitations

- The CMA are ATIP defaults, not market-implied. Review them and set `wealth.cma`.
- The gold / USD-INR windows are as long as `global_markets` history, currently about two months.
- `fii_score` 0 on days when FII data is pending reads as maximum outflow. Its weight is small (0.05).
