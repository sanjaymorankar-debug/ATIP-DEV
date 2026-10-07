# W39: history, research and options handoff

**Branch:** `ccr-643d84fc-yig8ts` (PR #4, which also carries the Dhan token-refresh fix).

**Why this wave:**
- The owner's notes (`ATIP-.txt`): compare with Zerodha and Dhan, show stock history, store 7 years of data.
- `docs/ATIP_GAP_ANALYSIS_2026-10.md`: how ATIP compares with Dhan, Zerodha and institutional research.

**Status:** developed. 58 new test cases and the full suite pass. The three pages (/research, /screener, /options-builder) were rendered in a headless browser on seeded data, the options page also at a 390 px phone width. Not merged and not deployed. Nothing in this wave places an order.

## DP-11: 7 years of daily history

**Bug fixed:**
- **Before:** the weekly `db_purge` (`pipeline/scheduler.py`, `purge_old_data(dry_run=False)`) deleted `prices_daily`, `ai_scores`, `predictions`, `accuracy_tracker`, ownership and deal rows older than 600 days, without archiving them. ATIP's history starts on 2025-01-27, so the oldest prices were about to go.
- **Now:** `db/purge.py` has a **HISTORY** tier, 2557 days (7 years) by default, for those tables. Derived tables (indicators, components) keep 600 days. `retention_settings()` reads `config.json` `"retention": {"history_days", "long_days", "short_days"}`, where `history_days` 0 means keep forever.

**Backfill (`data/history_backfill.py`):**
- Walks each tracked symbol back from its earliest stored bar to today minus 7 years, one 365-day Dhan window at a time. Dhan serves daily candles from listing.
- Resumable: the earliest stored bar is the cursor, and progress is kept in table `prices_daily_backfill`.
- Listing-aware: an empty window, or bars starting more than 10 days into the window, marks the symbol `exhausted`.
- Refusals and exceptions are errors, never the listing date. After 3 consecutive failures the symbol is parked; `status` shows the reason and `run --symbols` retries it.
- Bars go through `data.dhan.store_daily_bars`, extracted from `run_historical_pipeline` so both use the same corporate-action basis.
- `fetch_historical_daily` now tags exceptions as `exception:<Type>` errors instead of returning an empty frame. So `run_historical_pipeline` counts a breaker-open or network failure as refused, not as "no bars", and a night where every fetch raised now reports FAILED instead of a clean run.

**Schedule:** nightly at 22:20, 40 symbols per run (`config.json` `"history": {"backfill_enabled", "years", "symbols_per_run", "chunk_days", "pause_seconds"}`). The Nifty 500 completes in about 2 weeks.

**CLI:**
- `python -m data.history_backfill status`
- `python -m data.history_backfill run [--symbols ...] [--max N]`

**API:**
- `GET /api/data/history/coverage`
- `POST /api/data/history/backfill`

**Needs:** the Dhan Data API subscription. Without it Dhan refuses with DH-902, and the symbols are parked after 3 runs.

## RS-01..RS-10: equity research (`research/`)

### `research/valuation.py` (pure, no database)

**Cost of equity:** CAPM, 7 % risk-free + beta × 5.5 % equity risk premium, beta clipped to 0.6–2.0.

**DCF:**
- Two-stage, 10 years, growth fading to 6 % terminal.
- Equity cash flow = E × (1 − g/ROE).
- Collapses exactly to Gordon growth when growth is flat (tested).
- Starting growth is the average of EPS, revenue and profit growth, capped at 25 % and at ROE.

**Scenarios:** bear / base / bull, weighted 25 / 50 / 25, plus a cost-of-equity × terminal-growth sensitivity grid.

**Financials** (NSE industry contains financial / bank / NBFC / insurance): justified P/B = (ROE − g)/(ke − g) instead of the DCF.

**Peer and own-history multiples:**
- Peers: industry-median P/E (P/B for financials), trimmed, 3+ peers.
- Own history: the stock's own P/E and P/B **at each quarter end**. The stored `pe_ratio` uses one current price for every quarter, so it is not used.

**Fair value and target:**
- Fair value = weighted blend: DCF or justified P/B 0.5, peers 0.3, own history 0.2, renormalised over the methods present.
- 12-month target = fair value × (1 + ke − dividend yield).

**Rating:**
- BUY when upside ≥ the hurdle for its uncertainty: LOW 10 %, MEDIUM 15 %, HIGH 20 %, VERY HIGH 30 %.
- ADD ≥ 5 %, REDUCE ≥ −5 %, SELL below that.
- NOT_RATED when no method applies.
- Uncertainty comes from the spread between methods and between scenarios, and from data completeness.

**Units:** `normalise_fundamentals` reconciles the three sources. XBRL stores growth in %, ROE as a fraction; Screener stores both in %; Alpha Vantage stores both as fractions.

**`quality_profile`:** a **moat proxy** (WIDE / NARROW / NONE) from ROCE level and stability plus leverage, explicitly labelled a proxy.

**Config:** every assumption can be overridden in `config.json` `"research": {"valuation": {...}}`.

### `research/report.py`

- **`Universe`:** latest fundamentals and prices for all symbols, read once per batch, plus the Nifty 500 industry map.
- **`gather`:**
  - Price statistics: 52-week range, 1-year return, 3/5/7-year CAGR from the new history, volatility, drawdown, 200-DMA.
  - Fundamentals and their history, peers, ATIP scores and signal.
  - Shareholding trend, insider trades over 90 days.
  - Upcoming events: `market_event`, corporate actions, board-meeting intimations.
  - Whether the operator holds the stock.
- **Thesis / risks / catalysts:** from explicit rules; every bullet names its number. No generative AI.
- **Disclosures** (modelled on the SEBI RA rules):
  - Automated model, no analyst review, no generative AI.
  - **Not a SEBI-registered RA; own research only.**
  - Rating definitions, horizon and method with assumptions.
  - Holding conflict.
  - Past hit rates are unverified.
- **`research_report` table:** one row per symbol per day. A row is a **call** when it is the first report, the rating changes, or the target moves more than 5 %.
- **`evaluate_targets`:** marks each call HIT (target touched within 365 days; upside calls use the high, downside calls the low) or MISSED at the horizon. Prices are adjusted with `corporate_actions.entry_factor`, so a split does not fake a miss.
- **`hit_rate`:** success rate and average return by rating.
- **Schedule:** daily at 20:40 on market days, after the evening scoring (`research.reports_enabled`, default on; DB only, no network).
- **CLI:** `python -m research report SYM | run | evaluate | hit-rate | ratings`.

**API:**
- `GET /api/research/equity[?rating]`
- `GET /api/research/equity/{sym}[?fresh=1]`
- `POST /api/research/equity/{sym}/refresh`
- `POST /api/research/equity/run`
- `GET /api/research/equity/{sym}/history`
- `GET /api/research/hit-rate`

**Page:** `/research`.

## SC-20: fundamental screener (`research/screener.py`)

**Snapshot:** one row per stock that has fundamentals and a price, about 50 fields:
- Valuation: price, market cap (price × shares / 10⁷), P/E, P/B, PEG, earnings / dividend / FCF yield.
- Profitability: ROE, ROCE, ROA, net and operating margin.
- Growth: revenue, profit and EPS, YoY and QoQ.
- Size and balance sheet: revenue, profit, EPS, book value, debt/equity, current ratio, interest cover, cash, FCF.
- Ownership: promoter %, its change over the latest quarter, pledge, FPI, MF.
- Price: 1-year return, 3-year CAGR (from the 7-year history), distance from the 52-week high / low.
- Technical: RSI(14), above the 200-DMA.
- ATIP: score and signal.
- Research model: rating, upside, fair value, moat proxy, quality score.
- **Magic-formula rank:** Greenblatt's earnings-yield rank + ROCE rank, approximated with E/P; financials excluded.

**Units:** percentages in %, `_cr` fields in ₹ crore.

**Cache:** the snapshot is cached for 10 minutes per database.

**Query language:**
- A small recursive-descent parser. No `eval` and no SQL from the query.
- `AND` binds tighter than `OR`; `NOT`, parentheses and `IN (...)` are supported.
- Text matching is case-insensitive.
- Field names and aliases (`ROCE`, `PE`, `mcap`, `debt_to_equity`, ...) are case-insensitive.
- A stock missing a field never matches a condition on it.
- Bad queries return a 400 that names the problem.

**12 presets:** quality compounders, value, GARP, dividend, debt-free, promoters adding, undervalued by ATIP's model, strong near the 52-week high, turnaround, magic formula top 30, oversold quality, pledge risk.

**Saved screens:**
- Table `research_screen`, class OWNER.
- Name, query, sort, columns, notify flag, and the last matches. Each run reports **new** and **dropped** stocks.
- Job `saved_screens` runs at 20:50 on market days, after the research reports. It alerts (alert log + Telegram when configured) on new matches for screens with notify on.

**CLI:** `python -m research.screener "roce_pct > 20 AND debt_equity < 0.5" [--sort ...] [--csv]` or `--preset quality_compounders`.

**API:**
- `GET /api/screener/fields`
- `GET /api/screener/run`
- `GET /api/screener/run.csv`
- `GET` / `POST /api/screener/saved`
- `GET /api/screener/saved/{id}/run`
- `POST /api/screener/saved/{id}/delete`

Saving and deleting require `workspace:write` and the token; running is a read.

**Page:** `/screener`: presets, query box, condition builder, sortable results linking to `/research?symbol=...`, CSV, saved screens.

## OP-01..OP-05: options strategy builder (`quant/options_strategy.py`)

**Legs:** CE / PE / FUT, buy or sell, strike, expiry, lots, premium, IV.

**16 templates:**
- Long / short call and put
- Bull / bear call and put spreads
- Long / short straddle and strangle
- Iron condor and iron butterfly
- Call butterfly
- Covered call, protective put
- Call ratio spread

Strikes snap to the ATM on the symbol's strike grid (NIFTY 50, BANKNIFTY 100, otherwise from the stored chain or about 1 % of spot).

**`analyse`:**
- Payoff at the first expiry, and on a chosen earlier date (Black-Scholes for legs still alive).
- Breakevens.
- Max profit and max loss, with **unlimited** flagged from the slope beyond the strikes.
- Net premium (credit / debit), net Greeks (Δ units, Γ, Θ ₹/day, ν ₹/vol point).
- Probability of profit: lognormal at the legs' average IV.
- Reward : risk.
- A missing IV is implied from the premium.
- The chart range is ±4 standard deviations of the move to expiry (3–30 %).

**Pricing:** `chain_quotes` / `price_legs` fill premiums from the latest `option_chain_snapshot` (bid/ask mid, else LTP), then the F&O bhavcopy (settle / close). A leg with no stored quote is priced with Black-Scholes at 18 % IV, and its source says so.

**Not computed:** margin (SPAN + exposure). The page says to use the broker's calculator.

**API:**
- `GET /api/options/templates`
- `GET /api/options/chain/{sym}`
- `POST /api/options/build`
- `POST /api/options/analyse` (`research:run`; analysis only)

**Page:** `/options-builder`. The payoff chart uses validated categorical colours with a dashed target-date line, plus a legend, crosshair tooltip and table view.

## Wiring

- Tables: `db/schema_w39.py` (`prices_daily_backfill`, `research_report`, `research_screen`), applied by `db/schema.py`. Scoping: GLOBAL, except `research_screen`, which is OWNER. Privacy inventory: classified by the existing rules.
- Routes: `dashboard/w39_routes.py`, registered in `server.py`. Authz: two new rules, `POST /api/options/(build|analyse)` → `research:run` and `POST /api/screener/` → `workspace:write`; the rest is covered by the existing `/api/research/` and `/api/data/` rules.
- `docs/API_REFERENCE.md` and `docs/openapi.json` regenerated. They were also stale from W34–W38: 402 → 511 routes.
- `db/sql/*.sql` are pinned snapshots (master @ 6896bea) and were not regenerated. The new tables are created on first start like any additive migration.

## Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_w39_history.py` | 8 | retention tiers and config; backfill walk-back, listing stop, resume; refusal vs listing; parking; exception tagging; schedule |
| `tests/test_w39_research.py` | 20 | DCF = Gordon invariant; scenarios; sensitivity monotonicity; growth caps; justified P/B; unit normalisation; rating hurdles; peers; financials; blend; quality; end-to-end report on a seeded DB; calls and hit rate; split-adjusted outcome; batch run; single-report read limited to the stock and its peers; API and token; authz; table classification |
| `tests/test_w39_screener.py` | 19 | query precedence / NOT / IN / aliases; missing values; malicious and malformed queries refused; presets valid; snapshot fields on a seeded DB; magic rank; filter / sort / columns / CSV; cache; saved screens with new matches and an alert; API and token; permissions and classification |
| `tests/test_w39_options.py` | 11 | payoff and breakevens; unlimited flags; condor risk = width − credit; every template; POP sanity; time value; implied IV; validation; chain pricing; API |

## Known limits and next steps

**Valuation inputs:**
- Consensus estimates and revisions are not used; there is no licensed feed.
- XBRL parses key lines only, not full statements. No 3-statement model, SOTP or insurer P/EV.
- With few stored quarters, own-history multiples are skipped and uncertainty rises. Run the XBRL history backfill (W27-1) to fill them.

**Moat proxy:** quantitative only, not an analyst judgement.

**Hit rates:**
- Exist only from the day reports start.
- Measured against ATIP's own prices and not independently verified.
- SEBI's PaRRVA rules apply before any past performance is shown to others.

**Options builder:** analysis only. One-click execution would need the live OMS (gap analysis §4, item 8), which needs the owner's explicit go-ahead.

**Next on the roadmap:**
- MF analytics / SIP
- Tax P&L
- Chart overlays / patterns
- ATIP MCP server
- Earnings-surprise signal
