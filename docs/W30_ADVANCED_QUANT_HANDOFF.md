# W30: Advanced quant handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. The tree compiles. A scratch run on a copy of the production DB covered:
- the value / size factors (AF-05);
- 25 sessions of F&O bhavcopy recomputed with IV, and the dv_* / ms_* / ev_* features;
- bar microstructure for 502 symbols;
- the event sources and studies;
- a complete paper **futures SHORT → COVER** round trip through the risk engine, the OMS and the ledger.

ChatGPT testing is pending.
**Branch:** `w30-advanced-quant`, from `w29-execution`. Not merged, not deployed.
**Defaults:** `futures.enabled` is false. No shorts are possible until you switch it on, and even then only for strategies whose definition says `"short_via_futures": true`. The new data jobs (microstructure, events) only prepare data.

## Delivered

| ID | Feature | Where |
|---|---|---|
| AF-05 | Value / quality composites | `quant/factors.py`. See "Value factors" below |
| QR-10 | Volatility strategies | See "Volatility" below |
| QR-05 | Stat-arb, shortable leg | See "Futures short leg" below |
| QR-06 | Cross-sectional / market-neutral | See "Futures short leg" below |
| QR-09 | Event-driven research | See "Event research" below |
| AF-07 | Microstructure factors | See "Microstructure" below |

### Value factors (AF-05)

- **New versions (v2):** `earnings_yield`, `book_to_market`, `fcf_yield`, `price_to_sales`, `rel_earnings_yield`, `market_cap`, `size_log` and `turnover`.
- **Point in time:** each is computed **at the factor date's close** from W27's NSE fundamentals (TTM EPS, book value per share, filed shares, FY FCF, TTM revenue). The old factors read P/E values frozen at ingestion and were permanently None.
- **Knowledge date:** `fundamentals_as_of` uses the filing's broadcast time.
- **Composites:** the vqm / vqmg composites now have inputs.
- **Versioning:** versions are bumped, as the registry requires for a changed definition.

### Volatility (QR-10)

- **Implied volatility:** computed from the F&O bhavcopy option closes (Black-Scholes, NSE options are European). Fields: ATM call / put IV, 95% put minus 105% call skew, on the first expiry at least 4 days out. Stored in `fo_underlying_daily` along with the lot size.
- **Strategy features (`dv_*`):** iv_atm, iv_rank / iv_pct (252 sessions), VRP (IV − RV20), skew, PCR, annualised basis, futures OI change and the F&O flag.
- **Library strategies (DRAFT):** `vol_compression_breakout` and `vrp_trend_filter`.
- **Limits:** option-selling strategies are not executable from the cash book.

### Futures short leg (QR-05 / QR-06)

- **What it is:** a **paper stock-futures book** (`execution/futures_paper.py`).
  - Positions are the near month, in whole lots.
  - Margin is `futures.margin_pct` of notional, blocked from paper cash.
  - Fills are at the end-of-day futures close plus slippage.
  - Positions are settled at expiry.
- **New actions:** SHORT and COVER.
- **Pairs kind:** with `short_via_futures` it emits SHORT on the short leg and BUY on the long leg (it used to emit an unexecutable SELL and hold back the long leg).
- **Portfolio kind:** negative weights become SHORT intents.
- **Risk engine:** sizes in whole lots (< 1 lot → REJECTED, never rounded up), checks margin vs cash and applies the broker-health gate.
- **OMS:** routes to the futures adapter. LIVE futures are refused at every layer.
- **Isolation:** reconciliation, strategy P&L and live P&L exclude FUT fills from the cash book and show a FUTURES book.

### Event research (QR-09)

- **New event sources** in `market_event`:
  - EARNINGS: NSE results filings, known the next day for an evening filing; value = profit YoY.
  - DIVIDEND: NSE corporate actions.
  - INSIDER_BUY / INSIDER_SELL: PIT disclosures by promoters, directors and KMP.
  - SAST.
- **New features:** `ev_days_since_earnings / dividend / insider_buy`, `ev_earnings_growth` and `ev_insider_net_90d`.
- **Generic event study** (`quant/event_study.py`):
  - CAR vs NIFTY50 per horizon and direction, plus pre-event drift.
  - Significance uses 1 / 99% winsorized means.
  - **Guards:** day 0 within 4 days of the event, and no window across a price-data gap.
  - Runs weekly.

### Microstructure (AF-07)

- **Features** from the stored 15-min bars:
  - bar RV;
  - **Roll** and **Corwin-Schultz** spread estimates;
  - VWAP deviation, close location and last-hour volume share;
  - order imbalance from the stock feed's buy / sell quantities (now stored).
- **Strategy features:** `ms_*`, including `ms_spread_20`.
- These are estimates from trade prices. Quoted spread and depth still need DP-04 / DP-05.

## Findings worth knowing

- **Dividend study.**
  - **Bug fixed:** the first result was inflated by a bug: symbols with no price data around their event took a "day 0" months later. That is now guarded.
  - **Result:** 5-day winsorized CAR is +0.60% (t = 4.2), but the median is −0.07% and the hit rate 49%.
  - **Reading:** a right-skewed distribution, not a tradeable edge.
- **Bulk / block deals (W27):** the contrarian SELL-side result is unchanged under the new gap guard.
- **Futures lots:** one stock-futures lot is about ₹5–10 lakh notional. With the default paper capital, every SHORT is (correctly) REJECTED as "one lot exceeds the position target". Futures shorts need paper capital in the crores.

## Defects found and fixed in W30

- **IV rank above 100:** today's IV was outside its own reference range. NIFTY50's rank was missing because of the NSE alias.
- **COVER never matched:** a date vs string expiry comparison meant the held contract never matched.
- **Futures realised P&L:** the opening fee was left out; it now equals the cash effect.
- **Event-study day 0 / data gaps:** as above (also applied to the W27 deal study).

## Owner actions / next steps

1. `python -m data.derivatives --backfill 250 --recompute` (about 15 min) so IV rank has a year of history.
2. After the W27 fundamentals backfill, run a factor IC study on the v2 value factors.
3. To try futures shorts: `"futures": {"enabled": true}`, paper capital in the crores, and `"short_via_futures": true` on a pairs / portfolio strategy.

## Not built

- Backtesting of futures shorts.
- Automatic rolls.
- Live futures.
- Tick / depth microstructure.
- MACRO / INDEX_CHANGE events.

## Files

- **New:** `execution/futures_paper.py`, `quant/{derivatives_features,event_sources,event_study,w30}.py`, `strategy_engine/library/70_vol_compression_breakout.json`, `71_vrp_trend_filter.json`
- **Changed:**
  - `quant/{factors,microstructure,events,strategy_features,deal_signal}.py`, `data/{derivatives,stock_feed}.py`;
  - `strategy_engine/{decisions,engine,kinds,definition,features,performance}.py`;
  - `execution/{risk_engine,order_manager,adapters,reconcile}.py`, `portfolio/live_pnl.py`;
  - `db/schema.py` (W30_TABLES: paper_futures_position, paper_futures_trade, event_study; columns on fo_underlying_daily, live_quotes and oms_order);
  - `pipeline/scheduler.py`.
