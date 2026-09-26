# W17: Integrated intelligence (ATIP-INT-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED (not functionally tested). The build check and a scratch-copy smoke run include:
- an HTTP smoke of the whole wealth API: 47/47 expected status codes;
- a browser render of every tab: no console errors, and no horizontal scroll at a 375 px phone width.

ChatGPT end-to-end testing is pending.
**Branch:** `w17-integrated-intelligence` (on top of W16). Not merged, not deployed.

## What ties together (`wealth/integrated.py`)

| Piece | What it does |
|---|---|
| Investor cycle | snapshot (W12) → ledger import (W15.5) → DNA re-score when the goals' risk requirement moved > 5 pts (W11↔W13) → allocation run (W14) → goal projections (W13) → rebalance check (W15) → weekly performance report (W15.5) → alerts. Each step is isolated (one failure does not stop the rest) and recorded in `wealth_cycle_run` |
| Schedule | `pipeline/scheduler.py` post-market, after the W7 enterprise jobs: `run_job("wealth_cycle", wealth.integrated.run_scheduled)`. **SKIPPED unless `wealth.enabled` is true** (default false). Runs for every owner with an Investor DNA |
| Alerts | Through the existing `alerts.telegram.notify` (alert_log + Telegram when configured), de-duplicated per day: rebalance suggested, goal newly OFF_TRACK, DNA stale, ATIP-flagged holdings, stale prices |
| Overview | `/api/wealth/overview`: DNA, net worth, goals, allocation verdict, latest performance, the advisor briefing and today's signals with suitability. This is the new first tab, "Overview" |
| Investor / Trader mode | A preference in `wealth_preference`, defaulting to the DNA's `default_view`. Trader mode points to the trading pages. It changes no trading setting, and the existing `/` dashboard is unchanged |
| Trader → investor bridge | `signal_suitability(symbol)` checks risk profile, horizon, concentration (room under the cap) and the equity allocation drift, giving SUITABLE / CAUTION / NOT_SUITABLE. It is applied to today's BUY signals and to SELL signals on held names |
| Status | `/api/wealth/status`: freshness of every chain component and of the market inputs, state READY / DEGRADED / INCOMPLETE, and the next step for the user |

## Interfaces

- Tables: `wealth_cycle_run`, `wealth_preference`.
- API: `/api/wealth/overview`, `/mode` (GET / PUT), `/status`, `/signals`, `/suitability/{symbol}`, `POST /cycle`, `/cycles`.
- UI: Overview tab, mode switch, "Run investor cycle now". Every tab shows a loading indicator; wide tables scroll inside their own box.

## Smoke results (scratch copy)

- **Cycle:** 1.7 s, all 8 steps OK; the refresh rule held (requirement unchanged, no new DNA version).
- **Overview:** 0.3 s.
- **Another tenant's owner:** INCOMPLETE, next step "answer the Investor DNA questionnaire", allocation NO_PROFILE.
- **HTTP smoke:** token guard 401, validation 400, 404 on another owner's id, MUTUAL_FUND / FD refused, CSV export, `/api/v1/wealth/*` alias, trader `/` and `/api/scores` still 200.

## Also fixed in this wave

- The performance tab opens the latest stored report.
- PME is shown as undefined (not negative) when withdrawals exceeded what the benchmark would have grown to.
- A long audit JSON no longer widens the page.
