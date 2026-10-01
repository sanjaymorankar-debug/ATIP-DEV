# W27: Data and scores handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. The tree compiles. Smoke runs were done on a scratch copy of the production database: NSE fundamentals / ownership / F&O ingestion for 6 symbols, a full re-score of 2026-10-01 with both W27 switches forced on, the deal study, and a 1-year global-history backfill. Every new endpoint was exercised through FastAPI TestClient. ChatGPT testing is pending.
**Branch:** `w27-data-scores`, from `tools-mysql-export` (`769383c`, which carries W25 + W26). Not merged, not deployed.
**Defaults:** ingestion runs on schedule, but no score changes until you flip a switch. The one exception is MSI's Options component, which now reads the real PCR (see SC-06).

## Owner decisions applied (2026-10-01)

- **Fundamentals source:** NSE filings, not Screener.in.
- **Pacing:** waves W27–W33 are built back-to-back, with one branch and one handoff per wave.

## Delivered

| ID | Feature | Where |
|---|---|---|
| DP-15 | Fundamentals from NSE | `data/nse_filings.py`, `data/nse_api.py`. See "Fundamentals" below |
| SC-13 | Fundamental Score | `scores/fundamental.compute_fs`: P/E (inv), P/B (inv), operating margin, profit QoQ, interest coverage, current ratio. Valued at the session close. Returns **None** below 3 inputs (the old 50.0 default is gone). New weight set `FS` |
| SC-02 | Stability Profit Index | `scores/fundamental.compute_spi` on the seeded `SPI` weights: ROE, ROCE, EPS YoY, revenue YoY, FCF conversion, D/E (inv), PEG (inv), Quality (share of profitable quarters + margin stability over up to 8 quarters). SPI and FS no longer share an input, which removes the 0.25 double weight |
| DP-16 | Institutional data | `data/institutional.py`: shareholding pattern (promoter / public; from the SHP XBRL, MF / FPI / insurance / DII / retail % and promoter pledge), SEBI PIT insider trades, SAST Reg 29. `features(conn, sym, as_of)` is point in time |
| SC-14 | Institutional Score | `scores/engine.compute_ins(…, own=)`: adds MutualFund (SHP MF % change QoQ), Insider (90-day net PIT value) and a promoter trend / pledge adjustment. New INS weights MutualFund 0.15, Insider 0.10 (seeded only when the switch is on) |
| SC-06 | MSI real PCR | `compute_msi` Options uses the session's NIFTY PCR (OI). **Before:** nothing wrote `fii_dii_market.pcr`, so it was a constant 1.0. **Now:** the component is left out on a session without PCR. Live without a switch: this is a defect fix, like earlier "measured or left out" fixes |
| DP-08 (partial) | F&O daily summary | `data/derivatives.py`: NSE F&O bhavcopy, one row per underlying in `fo_underlying_daily` (futures OI / change / volume, call / put OI and volume, PCR on OI and volume, near-expiry max pain). Runs post-market before scoring. IV / Greeks are not built |
| DP-12 | Global markets | US 3M / 5Y / 30Y yields (10Y existed); `global_market_history` (every series, appended each run; `backfill_global_history('5y')`); history API |
| DP-01 | Stock live feed | `data/stock_feed.py`. See "Live feed" below |
| AD-03 | Deal-signal validation | `quant/deal_signal.py`. See "Bulk / block deal finding" below. Weekly job and `/market` panel |
| DB-04 | Pre-open view | `/market` page (`dashboard/market_routes.py`, `market_page.py`): GIFT Nifty and implied gap vs Nifty's close, GIFT series, overnight global + yields with history chart, PCR / max pain, FII / DII; plus F&O table, per-symbol fundamentals + ownership lookup, deal study, feed status. "Market" link on the main dashboard |

## Fundamentals (DP-15)

- **Source:** NSE `integrated-filing-results` (Integrated Filing – Financials, March 2025 quarter on) lists each filing with an XBRL link. `--history` also reads the older results listing.
- **Per filing:** `fundamental_filing` keeps the facts used, by context (OneD quarter, OneI balance sheet, FourD year to date), plus the broadcast time.
- **Per quarter:** `fundamental_data` is derived locally.
  - Consolidated is preferred, standalone otherwise. Money is stored in ₹ crore; ratios as fractions.
  - A Q4 filing reporting only annual figures becomes FY minus the 3 earlier quarters.
- **Banks** are detected by Deposits on the balance sheet:
  - equity = Capital + Reserves;
  - operating margin = operating profit before provisions / total income;
  - D/E, interest coverage and ROCE are deliberately NULL.
- **Point in time:** `available_from` is the broadcast time. With the switch on, `scores/engine.get_fund(sym, conn, td)` reads only rows broadcast on or before `td`.
- **Incremental:** only new XBRLs are downloaded. `--reparse` re-downloads, which is needed after adding tags.
- **Schedule:** weekly (Saturday) over the whole tracked universe. The **first full run is about an hour** (around 12 filings × 500 symbols at a polite 0.35 s gap). Run it once by hand, off-hours:
  `python -m data.nse_filings` (optionally `--history`), then `python -m data.institutional`.

## Live feed (DP-01)

- **Feed:** supervised Dhan Quote WebSocket for at most 100 symbols. The symbols are LIVE holdings, open PAPER positions, active order-rule symbols and the top-25 ATIP, or `live_feed.symbols`.
- **Supervision:** same window, backoff and silent-socket recycling as the index feed.
- **Failover:** while the socket is parked or dead, quotes come from Dhan REST every `rest_seconds`.
- **Writes:** `live_quotes` every `flush_seconds` (60) and `live_feed_status`.
- **Off by default.** It cannot be exercised outside market hours, so it has only been compiled.

## Bulk / block deal finding (AD-03), needs your attention

The first event study covered 494 deals between 2026-09-07 and 10-01, scored by abnormal return vs NIFTY50 after the deal session:

| Horizon | BUY n | BUY mean | BUY t | SELL n | SELL mean | SELL t |
|---|---|---|---|---|---|---|
| 1d | 177 | +0.07% | 0.17 | 203 | **+0.96%** | **2.20** |
| 5d | 144 | −0.75% | −0.77 | 139 | **+4.47%** | **4.13** |
| 10d | 70 | +0.08% | 0.04 | 65 | **+5.34%** | **2.29** |

**Verdict: `CONTRADICTS_INS_DIRECTION`.**
- Stocks with net SELL deals went on to outperform. Net BUY deals showed nothing.
- INS's BulkDeals component scores net buying as bullish, which is the opposite of this result.
- **Caveats:** four weeks of history, mostly small and illiquid names. That is not enough to flip the sign.
- **Recommendation:** don't up-weight BulkDeals, and re-check the weekly study as history grows.

## Switches (config.json)

```json
"fundamentals":  {"score_enabled": false, "source": "nse"},
"institutional": {"score_enabled": false},
"live_feed":     {"stocks_enabled": false, "max_symbols": 100, "flush_seconds": 60, "rest_seconds": 60},
"fundamentals_enabled": true
```

- **fundamentals.score_enabled:** SPI and FS enter ATIP, and VPI's FG and CRI's Debt read the NSE fundamentals. While off, every scorer gets an empty fundamentals dict, exactly as before.
- **fundamentals_enabled:** gates only ingestion. Its default is now true; the old hold reason (double weighting, 50.0 default) is resolved.

**Scratch evidence.** This is the 2026-10-01 re-score with both switches on, for 6 ingested symbols. The figures are ATIP score before → after; SPI and FS come from the run with the switches on.

| Symbol | ATIP before → after | SPI | FS |
|---|---|---|---|
| INFY | 49.96 → 56.75 | 84.0 | 70.6 |
| TCS | 46.53 → 53.11 | 73.4 | 70.0 |
| HDFCBANK | 49.91 → 48.74 | 47.8 | — |

(These are from the scratch run before the bank fix. Re-run, HDFCBANK FS is 73.4.)

## Not built / limits

- DP-08 is the rest: IV / Greeks, option chain, intraday OI. MSI uses PCR only.
- **India 10-year G-sec yield:** no reliable free daily source via yfinance. Left out, not approximated.
- **Per-stock daily MF flows:** no free source. MF uses the quarterly SHP change.
- **News in MSI:** still rule-based. The AI path is W28 (NS-02/03/04).
- **Stored valuation fields:** `fundamental_data.pe_ratio` / `pb_ratio` on stored rows are at the latest close when the row was built (informational). Scoring recomputes at each session's close.
- **Bank asset-quality inputs** (GNPA / NNPA) are not used in FS / SPI yet.

## Files

- **New:** `data/nse_api.py`, `data/nse_filings.py`, `data/institutional.py`, `data/derivatives.py`, `data/stock_feed.py`, `scores/fundamental.py`, `quant/deal_signal.py`, `dashboard/market_routes.py`, `dashboard/market_page.py`
- **Changed:** `db/schema.py` (W27_TABLES: 7 tables; new columns on fundamental_data, institutional_data, global_markets), `db/purge.py`, `scores/engine.py`, `data/fundamentals.py`, `data/markets.py`, `pipeline/scheduler.py` (F&O post-market; weekly NSE fundamentals / institutional / deal study), `main.py` (stock feed start), `dashboard/server.py` (routes + Market link)
