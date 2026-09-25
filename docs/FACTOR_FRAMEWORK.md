# ATIP factor framework (W6)

## Registry

Each factor (`quant/factors.py`, table `quant_factor`, primary key `factor_id` + `version`) records:
- factor_id, name, category, description, inputs, formula, lookback, frequency (daily), version;
- normalization (its own spec), direction (+1 higher is better / −1 lower is better), data_dependency, status, content_hash, created_at.

**Versioning:** a changed definition under the same version is refused at sync. A formula change is a new version.

**Status:**

| Status | Meaning |
|---|---|
| ACTIVE | The inputs exist in ATIP |
| DATA_PENDING | A required input is not stored, so the calculator returns None. Nothing is estimated in its place |

| Category | Factors | Data |
|---|---|---|
| Momentum | mom_12_1, mom_6m, mom_3m, rel_mom_60 (relative), risk_adj_mom_125, sector_mom_60 (cross-sectional) | bars ✔ |
| Volatility | vol_20 (realised), vol_60 (historical), downside_vol_60, park_vol_20, vol_change_20_60, beta_250 | bars ✔ |
| Liquidity | adv_20, traded_value_20, amihud_20, delivery_pct_20 | bars ✔ |
| Liquidity | turnover | **pending**: shares outstanding |
| Liquidity | bid_ask_spread | **pending**: bid/ask |
| Size | market_cap, size_log (rank / buckets through normalization) | **pending**: shares outstanding |
| Value | earnings_yield, book_to_market, ebitda_ev_yield, dividend_yield, rel_earnings_yield (sector-relative) | fundamentals (**empty today**) |
| Value | fcf_yield, price_to_sales | **pending**: market cap |
| Quality | roe, roa, roce, net_margin, operating_margin, leverage, interest_coverage, cash_quality (FCF / profit) | fundamentals (**empty today**) |
| Growth | revenue_growth, eps_growth, profit_growth, fcf_growth, growth_consistency | fundamentals (**empty today**) |

- **Reuse:** technical inputs (returns, volatility, relative strength) come from the W3 feature code (`FactorContext.feat`), so each formula lives in one place.
- **Fundamentals:** fundamental rows count as known at `report_date + 45 days`. `fundamental_data` currently has 0 rows; the weekly fundamentals job (`data/fundamentals.py`: Alpha Vantage / Screener.in) fills it.

## Normalization (`quant/normalize.py`)

`apply(values, spec)` with `spec = {"method", "winsorize", "relative_to"}`:
- **method:** `percentile` (default), `zscore`, `rank`, `minmax`, `raw`
- **winsorize:** % per tail (default 1)
- **relative_to:**
  - `sector`: the method applied within each NSE industry; industries with fewer than 5 members fall back to the whole cross-section;
  - `market`: minus the cross-sectional median.

`neutralize(values, exposures)`: OLS residual on the exposure columns plus an intercept (beta, size, sector dummies…). Use it for factor or sector neutralization of a score.

## Scores stored per date (`quant_factor_score`)

| Column | Meaning |
|---|---|
| raw | Calculator output |
| norm | The factor's own normalization |
| pct | Percentile of raw, 0–100 |
| score | Direction-adjusted percentile (higher = better) — what strategies read as `qf_<factor>` |
| rank / sector_rank | 1 = best by score, across the universe / within the industry |
| universe_size | Symbols with a value that day |

## Composites (`quant/composite.py`, `quant_composite`)

`CompositeDef(name, version, components=[{factor_id, weight, direction?, include?}], min_coverage, normalization)`, content-hashed and immutable per version.

- **Composite value** = Σ w·s / Σ w over present, included components, where s is the factor score (a component `direction: -1` flips it).
- **Coverage rule:** a symbol with less than `min_coverage` of the total weight present gets no composite. There is no partial fallback.
- **Stored as:** `composite:<name>@<version>`, read by strategies as `qc_<name>`.

| Built-in | Components |
|---|---|
| vqm@1 | earnings_yield .15, book_to_market .10, roe .15, leverage .10, mom_12_1 .25, risk_adj_mom_125 .10, vol_60 .15 |
| mom_lowvol_liq@1 | mom_12_1 .30, risk_adj_mom_125 .15, vol_60 .20, downside_vol_60 .10, traded_value_20 .25 (price data only) |
| vqmg_lowvol@1 | earnings_yield .15, roe .15, cash_quality .10, mom_12_1 .20, revenue_growth .10, growth_consistency .10, vol_60 .20 |

With fundamentals empty, `vqm` and `vqmg_lowvol` produce no rows (coverage). `mom_lowvol_liq` works on price data.

## Ranking

- `engine.rankings(as_of, key, top|bottom, sector)` gives top-N / bottom-N stocks, within the market or within an industry.
- `engine.sector_rankings(as_of, key)` ranks industries by mean score.
- Factor, composite and ML-score rankings all use the same stored table.
- API: `/api/quant/rankings?key=…&top=20` and `&by=sector`.

## Using factors in strategies

| Kind | Example |
|---|---|
| multi_factor | Factors `qf_earnings_yield`, `qf_roe`, `qf_mom_12_1` … (library `vqm_multi_factor`) |
| quant_rank | Score term `qc_mom_lowvol_liq`, filter `qf_traded_value_20 >= 40` (library `mom_lowvol_liq`) |
| portfolio | Score feature `qc_mom_lowvol_liq`, inverse-vol weights, beta-neutral (library `market_neutral_mlq`) |
| rule | Any condition on `qf_*` / `qc_*` / `ev_*` |
