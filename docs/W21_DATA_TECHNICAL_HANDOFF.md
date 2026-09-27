# W21: Data and technical analysis handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run on real symbols. ChatGPT testing is pending.
**Branch:** `w21-data-technical` (on top of `w20-release` / `ATIP-W20-RC1`). Not merged, not deployed.

## Tracker items

| ID | Feature | Delivered |
|---|---|---|
| DP-03 | Intraday bars | The 30-minute Dhan 15-min bar job now **stores** bars in `intraday_bars` (symbol, interval, timestamp, OHLCV). Before, it fetched them and threw them away. Short retention (90 days) in `db/purge.py` |
| TA-03 | VWAP | `vwap_20d` (20-session volume-weighted typical price), `vwap_session` (from stored intraday bars), `vwap_dev_pct`. **Technical Score unchanged by default.** The VWAP component (weight 0.08, dormant until now) joins only when `config technical.vwap_in_score = true`, because turning it on changes every score and signal |
| TA-04 | Volume profile | POC / VAH / VAL over 20 sessions (24 bins, 70% value area). Intraday bars when stored, else daily bars |
| TA-06 | Support / resistance | 5-bar fractal pivots over 120 sessions, clustered within ±0.75% of the cluster mean. The nearest level tested ≥ 2 times on each side, with touches and distance %. Was UNCLEAR |
| TA-09 | Beta | `beta_60`, `beta_downside`, `beta_long` (needs ≥ 500 sessions) with `beta_long_sessions`. **A true 3-year beta needs ~750 sessions; prices_daily keeps 600 calendar days (`db/purge.py DEFAULT_LONG_DAYS`), about 410 sessions. That is a retention decision for the owner** |
| SC-16 | Beta Risk Index | `bri` 0–100: 50% beta level, 30% downside beta, 20% instability of 60-session betas |
| TA-10 | Multi-timeframe | Weekly RSI, weekly trend (10-week EMA), monthly trend (10-month SMA), `mtf_alignment` −3..+3 |
| TA-11 | Extended catalogue | Supertrend, Ichimoku, Keltner, Donchian, MFI, CMF, ROC, Aroon, Parabolic SAR. **Plus Stochastic %K/%D, Williams %R and CCI**: their `technical_indicators` columns existed but were never computed |
| TA-12 | Full-history panel | `python -m data.technical --backfill 426` computes every stored session (tracked universe, resumable). **Not run.** It is heavy: run it off-hours, on a copy first |
| DP-11 | GIFT Nifty history | The session's last GIFT Nifty value from `index_levels` is stored daily as `GIFTNIFTY` in `prices_daily`. The dedicated pre-open panel (DB-04) is not built |
| DP-13 | Sector data | Eight sector indices (IT, Pharma, Auto, FMCG, Metal, Realty, PSU Bank, Energy) added to the daily Dhan index-series sync (`INDEX_SERIES_SYMBOLS`); backfill with `python -m data.market_series --backfill-sectors`. New `sector_breadth` table: stocks per NSE industry, % advancing, average return, % above 50 / 200-session averages |

## Strategy features (W3 catalogue)

These are registered as point-in-time features, identical in live decisions and backtests:
`supertrend_dir`, `mtf_alignment`, `sr_support_dist_pct`, `sr_resistance_dist_pct`, `mfi_14`, `cmf_20`, `aroon_up`, `aroon_down`, `vwap_dev_pct`, `beta_60`, `beta_downside`, `bri`.

## Integration

- New module `data/technical_ext.py` (pandas / numpy only, no new dependency). It is called from `run_technical_pipeline`, which reads 520 bars (the classic indicators still use their 260).
- Rows go to the new table `technical_ext`.
- New module `data/market_series.py`, with a post-market job `market_series` after `technical_indicators`.
- Tables (additive): `intraday_bars`, `technical_ext`, `sector_breadth` (`db/schema.py W21_TABLES`).

## Smoke run (scratch copy, 2026-09-25)

- **Speed:** 6 symbols in 0.54 s, about 0.1 s per symbol. The whole ~3,400-symbol post-market run gains a few minutes.
- **RELIANCE:**
  - Indicators: VWAP-20 1,269.58 (close −3.4%), POC 1,238.57, β60 1.23, BRI 43.4, MTF −3, Supertrend down.
  - Oscillators: Stochastic %K 13.6, CCI −112.
  - Technical Score 47.95, unchanged path.
- **Sector breadth:** 20 sectors from 499 constituents. GIFT Nifty daily close captured.
- **S/R clustering:** the first version chained pivots into a band with 29 "touches". It was fixed to mean-anchored clusters and confirmed levels.

## Owner decisions raised

1. **`technical.vwap_in_score`:** turning it on changes Technical, ATIP scores and signals. Default off.
2. **prices_daily retention:** needs ≥ 1,100 days for a 3-year beta. This grows the database, roughly 1.8×.
3. **TA-12 backfill:** when and where to run it.
