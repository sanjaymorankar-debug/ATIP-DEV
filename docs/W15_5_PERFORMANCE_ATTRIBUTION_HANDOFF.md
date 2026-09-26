# W15.5: Investor performance attribution (ATIP-PERF-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run on the real paper book, the real broker-sync history and a synthetic signal-linked book. ChatGPT testing is pending.
**Branch:** `w15-5-performance-attribution` (on top of W15). Not merged, not deployed.

## The rule this wave enforces

ATIP never shows one ambiguous "return". Every report shows four, with their definitions, over the same sessions:

| Return | Definition | Source |
|---|---|---|
| MODEL | ATIP BUY signals at the signal price, exit after 20 sessions or on a SELL signal, gross, equal weight | `signal_log` + `prices_daily` |
| EXECUTABLE | The same signals entered at the next session's open, ± slippage (10 bps), NSE delivery costs, skipped above 5% of 20-session average traded value | + open / volume + `backtest/costs.py` |
| ACTUAL | The investor's ledger: quantities, prices, fees, cash flows. TWR (inflows at the start of a session, outflows at its end), XIRR, average-cost realized / unrealized P&L | `perf_ledger` |
| BENCHMARK | NIFTY 50 (or a chosen index) over the same sessions, plus PME: the investor's own cash flows invested in the benchmark | `prices_daily` |

## Requirement map (PERF-001-xx)

| ID | Requirement | Where |
|---|---|---|
| 01 | Performance data model: immutable ledger with source, source_ref, price quality, reference price, strategy; voids in a side table | `wealth/perf/ledger.py`, `perf_ledger`, `perf_ledger_void` |
| 02 | Model return, reproducible from the signal log | `perf/model.py` `build()` |
| 03 | Executable return (next open, slippage, costs, liquidity) | `perf/model.py` |
| 04 | Actual investor return (ledger quantities, prices, flows; TWR + XIRR; average cost) | `perf/engine.py` `run_actual()` |
| 05 | Benchmark return, with period and method displayed; PME | `perf/report.py`, `engine.py` |
| 06 | Cost attribution: fees by kind, slippage vs reference price, exact reconciliation to the ledger | `engine.py` `costs` |
| 07 | Position attribution: entry timing + averaging + exit timing + costs = actual − model P&L (identity checked in the smoke run); sizing vs model notional | `model.py` `link_signals()` |
| 08 | CAGR / annualized (withheld under a year), XIRR, alpha, beta, Sharpe, Sortino, max drawdown, tracking error, information ratio, win rate, profit factor, holding period | `perf/metrics.py` |
| 09 | Signal attribution: signal → entry (delay) → add-ons → exits → outcome and momentum hits; signal-driven vs discretionary P&L | `model.py`, `report.py` |
| 10 | Portfolio attribution by symbol / sector / asset class; sums exactly to the total | `engine.py` `contribution` |
| 11 | Dashboard: the four returns side by side, never a single figure | `/wealth` tab "Performance" |
| 12 | Audit and explainability: methodology PERF-1.0, code commit, assumptions, ledger / signal hashes, price as-of, sources per section; stored report JSON | `report.py` `audit`, `perf_report_run` |
| 13 | Export: CSV (labelled sections) and JSON of the stored report, so it matches the dashboard | `report.export()`, `/reports/{id}/export` |
| 14 | Edge cases: corporate actions (trades carried onto today's share basis with `entry_factor`), partial fills (`filled_qty`), averaging, dividends and fees as flows, missing prices (carried forward, counted), holidays (NIFTY session calendar), oversells (refused on entry, capped + flagged on import), intraday round trips, voids | `data.py`, `engine.py`, `ledger.py` |

## Ledger sources

- **PAPER:** `paper_order` TRADED rows.
- **OMS:** W4 `oms_fill` with the order's reference price.
- **LIVE:** inferred from successive broker syncs.
  - The first sync is an OPENING position: cost basis = broker average cost, return measured from that day's close.
  - A quantity increase is a BUY at the exact price implied by the average-cost identity.
  - A decrease is a SELL at that session's close, marked APPROXIMATE.
- **MANUAL:** owner-entered.

Imports are idempotent (unique source_ref). Correct an inferred row by voiding it and entering the contract-note trade.

## Smoke run (scratch copy, 2026-07-01 → 2026-09-25)

**PAPER:**
- Model +0.17%, executable −1.20%, actual −1.12% (TWR), NIFTY 50 −3.04%.
- Realized +₹4,865. The +5.3% intraday RELIANCE round trip is captured on its own day.
- Fees ₹163.61 reconcile to the ledger; contributions sum to the total.

**LIVE:**
- 8 openings on 2026-07-25 and one implied buy (ANMOL, 2026-09-17).
- Actual −3.97% TWR vs NIFTY −3.04%; XIRR −21.7% vs PME −19.8%. Both are flagged "under 90 days: not meaningful annualised".

**Signal model:** 40 trades, win rate 47.5%, profit factor 0.90. Executable: 39 trades (1 not executable: no next bar), profit factor 0.75.

**Synthetic linked trade (DIVISLAB):**
- Attribution: entry timing −660, averaging +500, exit timing −7,441.5, costs −33, total −7,634.5.
- This equals actual − model on the same quantity exactly.

## API and UI

- API: `/api/wealth/performance/portfolios | benchmarks | sync | ledger (GET/POST) | ledger/{id}/void | report | reports | reports/{id} | reports/{id}/export`.
- Page: tab "Performance", with ledger entry and void, saved reports and CSV / JSON links.

## Known limitations

- The live broker account has no fills in ATIP. LIVE sells are approximate until the contract-note trade is entered.
- The model return is gross and equal-weight by design. Per-trade P&L assumes a notional of Rs 1,00,000.
- No dividend history source exists, so dividends are manual entries.
- The "positions sleeve" does not model cash. Idle cash neither drags on nor inflates returns.
