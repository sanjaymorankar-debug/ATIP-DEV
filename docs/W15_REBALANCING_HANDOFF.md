# W15: Portfolio rebalancing engine (ATIP-RBL-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run. ChatGPT testing is pending.
**Branch:** `w15-rebalancing` (on top of W14). Not merged, not deployed.

## Scope delivered (`wealth/rebalance.py`, methodology RBL-1.0)

| Requirement | Implementation |
|---|---|
| Target allocation | `allocation.current_target()`: the latest W14 run if under 7 days old, else computed now |
| Drift | Per investable class: current vs target in points and relative. Band = the tighter of ±5 pts and ±25% of target (config) |
| Triggers | THRESHOLD (out of band), CALENDAR (nothing accepted in 365 days), RISK (volatility above 1.25× target, or bad year beyond the DNA limit), CONCENTRATION (single-stock cap), SCORES (ATIP CRI ≥ 75 / SELL). Verdict REBALANCE / REVIEW / NO_ACTION |
| Risk reduction | Sell order within an overweight class: cap breaches, then ATIP-flagged holdings, then lowest ATIP score, then largest |
| Modes | `to_band` (to band edges, least turnover, the default), `to_target`, `cash_flow` (invest new cash in underweight classes, no selling) |
| Instrument suggestions | Listed holdings get whole-share quantities; manual assets get "reduce by Rs X". Buys prefer an existing holding with an ATIP BUY signal, else an illustrative index ETF per class |
| Transaction-cost awareness | Backtest cost model (NSE delivery; ETFs with ETF STT) plus 5 bps slippage per listed leg; legs under `rebalance_min_trade` (Rs 5,000) are dropped; turnover, cost and cost % reported |
| Post-plan view | Projected weights, remaining max drift, CMA stats after |
| Decisions | PROPOSED → ACCEPTED / DISMISSED / EXECUTED_MANUALLY, with a note; the calendar trigger reads the last accepted plan |
| Tax-neutral | Explicitly: gains and holding periods are not considered. The note is on every check and plan |

**Advisory only:** no order, intent or order rule is created. The plan states this.

## Interfaces

- Table: `wealth_rebalance_plan` (the full result JSON is stored).
- API: `GET /api/wealth/rebalance/check`, `POST /rebalance/plan`, `GET /rebalance/plans[/{id}]`, `POST /rebalance/plans/{id}/decision`.
- Page: tab "Rebalance".

## Smoke run (scratch copy)

- A deliberately gold / cash-heavy test book: verdict REBALANCE (THRESHOLD, CALENDAR).
- `to_band`: 5 legs, costs Rs 577 (0.034% of turnover), max drift after 7.6 pts.
- `to_target`: 6 legs, max drift after 0.03 pts.
- `cash_flow` with Rs 2 lakh: 3 buys, no sells.
- A second decision on an accepted plan was refused.

## Known limitations

- The ETF STT rate (0.001% on sell) is an approximation.
- Manual assets (physical gold, property, cash) are "reduce by amount" with no cost model.
- The buy vehicles are illustrative index ETFs, not recommendations of a specific product.
