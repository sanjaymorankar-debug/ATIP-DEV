# W12: Multi-asset wealth dashboard (ATIP-WLT-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a smoke run on a scratch copy of the production database. ChatGPT testing is pending.
**Branch:** `w12-wealth-dashboard` (on top of W11). Not merged, not deployed.

## Scope delivered

| Requirement | Where |
|---|---|
| Normalized holdings model across sources: BROKER (synced account), MANUAL (entered), PAPER (simulated, never counted in net worth) | `wealth/holdings.py` `positions()` |
| Supported classes: Indian equity, international equity, bonds, gold, silver, cash, real estate, other; instruments (stock, ETF, bond, T-bill, SGB, physical / digital gold, silver, bank account, property) | `wealth/assets.py` |
| Scope exclusions enforced: MUTUAL_FUND, FD, NPS, INSURANCE refused with an explicit message | `assets.EXCLUDED_CLASSES`, `check_class()` |
| Valuation with source and date: ATIP mark (live quote or last close), gold / silver spot × USD/INR (+ domestic-premium assumption), foreign price × FX, manual, cash | `_value_manual()`, `market_price()`, `spot_inr_per_gram()` |
| ETF classification: explicit map of known NSE gold / silver / liquid / gilt / international ETFs, suffix rule for equity ETFs, per-owner overrides | `classify_symbol()`, `wealth_classification` |
| Wealth summary: net worth, gross assets, liabilities, invested cost, unrealized P&L; by asset class, by source, by instrument, equity by sector | `summary()` |
| Concentration (largest, top 5, HHI, effective holdings, single-stock cap breaches) and liquidity buckets | `summary()` |
| ATIP intelligence overlay on equity holdings (ATIP score, CRI, VPI, signal); holdings at risk (CRI ≥ 75 or SELL/EXIT/AVOID) | `_risk_overlay()` |
| Existing portfolio health (PHS) reused for the house book | `scores.portfolio_health.compute_phs` |
| Reconciliation: broker-reported vs ATIP-marked value; paper book shown separately | `broker_positions()` |
| Data quality: stale or missing prices (valued at cost and flagged, never zero), stale broker sync | `summary()["data_quality"]` |
| Liabilities (loans, cards) for net worth | `wealth_liability` |
| Daily net-worth snapshots and history | `record_snapshot()`, `snapshots()` |

## Interfaces

- Tables: `wealth_holding` (closing a holding sets status CLOSED; rows are never deleted), `wealth_liability`, `wealth_classification`, `wealth_snapshot`.
- API: `/api/wealth/assets`, `/summary` (`?positions=true`), `/positions`, `/holdings` CRUD (DELETE = close), `/liabilities` CRUD, `/classifications`, `/snapshots`. Writes need the token.
- Page: `/wealth`, tab "Wealth" (the default tab), under `dashboard/wealth_ui/wealth_tab.py`. Later tabs follow the same one-module-per-tab pattern.
- Config: `wealth.gold_domestic_premium_pct` (default 9, used for gold and silver spot), `wealth.single_stock_cap_pct` (default 10).

## Smoke run (scratch copy of production data)

- 8 broker holdings: broker-reported ₹78,492.50, ATIP-marked ₹78,492.50, difference 0.
- Manual gold, cash, ETF, US share (FX), property, and a home loan valued; the mutual-fund class was refused.
- PHS 46.36 read through; the snapshot was written.

## Known limitations

- The international price is manual: ATIP has no foreign-market price feed. USD/INR comes from `global_markets`.
- Gold and silver: international spot × FX × (1 + premium). This approximates, but is not, the domestic (IBJA / MCX) rate.
- The broker account keeps no transaction history. Actual-return analytics come in W15.5 from the ledger.
- The ETF map is hand-maintained. An unlisted non-equity ETF appears as equity until an override is saved.
