# W35 — Data platform: handoff

**Branch:** `w35-data-platform` (worktree `D:\Projects\ATIP-dev-w35`). It sits on top of `w34-execution-microstructure` → `w28b-news-weighting` → `w33-wealth-qa`.

**Scope:** the Yet-To-Start data features that no earlier wave covered:
- DP-04 tick data, DP-05 order-book depth, DP-08 derivatives data (completing the W27 partial)
- DP-14 macroeconomic data, DP-21 multi-asset data, DP-22 point-in-time data lake
- AD-01 alt-data framework, AD-02 alt-data sources

**Status:** developed, `compileall` clean. Not run, not tested, not merged, not deployed.

**Defaults:** everything that polls a feed or a third party during the session is **off** by default. That covers depth, option chains, tick capture and Wikipedia.

These jobs **run daily without being switched on**:

| Job | Time | Source |
|---|---|---|
| Macro | 07:15 | FRED / World Bank |
| Alt data | 19:45 | Internal sources only |
| Multi-asset | 23:30 | yfinance + AMFI |

## DP-22 — Point-in-time data lake (`data/lake.py`)

**Layout:** `atip_data/lake/<dataset>/date=YYYY-MM-DD/v<version>-<time>-<id>.<fmt>`.

**Format:** files are written as **Parquet when `pyarrow` is installed**. Otherwise they are `csv.gz` with the same content. Neither `pyarrow` nor `duckdb` is installed on this machine. `pip install pyarrow duckdb` (owner) switches new writes to Parquet and enables `query()` (DuckDB SQL over the lake). Older csv.gz parts stay readable.

**Manifest:** `lake_partition` has one row per file:
- version, path, format, rows, columns, sha256, bytes, source
- **knowledge_time**: when the data was known

Files are never overwritten. `write(replace=True)` starts a new version.

**Point-in-time reads:** `read(dataset, start, end, as_of)` returns only the parts known by `as_of`. Within each partition it takes the newest version known at that time.

**Other operations:**
- `archive_table(table, date_col, knowledge_col=...)` copies SQLite history into the lake.
- `verify()` re-hashes every file against the manifest.

## DP-04 — Tick data (`data/ticks.py`)

**Capture:** with `live_feed.capture_ticks` (which also needs the W27 stock feed, `live_feed.stocks_enabled`), every Dhan Quote packet is buffered in memory. The buffer is bounded and overflow is counted.

**Storage:** on the feed's flush timer, the buffer is written to lake `ticks` and counted in `tick_capture_status`. Nothing tick-level is written to SQLite.

**Minute bars:** at 15:50, `build_minute_bars` turns the day's ticks into **1-minute bars** in `intraday_bars` (`interval_min=1`, source `ticks`). Volume comes from cumulative-volume differences.

**Caveat:** Quote packets are Dhan's change snapshots, not the exchange trade tape. The docstring says so.

## DP-05 — Order book / depth (`data/depth.py`)

**Source:** Dhan `quote_data` (the same call live quotes use) returns 5 levels of depth.

**Polling:** with `depth.enabled`, it runs once a minute in session for up to 25 symbols. The default symbol set is the stock feed's.

**Stored in `order_book_snapshot`:**
- best bid / ask, mid
- spread in bps
- top-5 quantities
- imbalance (−1..+1)
- the 5 levels as JSON

Each snapshot is also written to lake `depth`.

**Daily features:** `features()` gives the time-weighted spread, median imbalance and depth percentiles. These are the measured inputs AF-07 lacked.

**Verify live:** the depth payload shape (`depth.buy` / `depth.sell` with `price`, `quantity`, `orders`) has not been confirmed on this machine.

## DP-08 — Derivatives data (`data/derivatives_store.py`)

**EOD contracts.** The W27 `run_fo_pipeline` already downloads the NSE F&O bhavcopy. It now also calls `store_contracts` on the same file:
- **Lake `fo_bhavcopy`:** every contract, at knowledge time 18:00 on that day.
- **`fo_contract_daily`:** futures of all expiries, plus options of the **nearest 2 expiries** (all strikes). Each row has OHLC, settle, OI, OI change, volume, value, underlying, lot size and a **contract IV** (W30's Black-Scholes and risk-free rate). IV is None outside no-arbitrage bounds. Retention is 90 days in SQLite; the full history stays in the lake.

**Intraday option chain:** NSE's option-chain API, every 15 minutes in session, with `derivatives.option_chain_enabled` (default symbols NIFTY and BANKNIFTY):
- **`option_chain_snapshot`:** every strike and expiry, with LTP, change, NSE IV, OI, OI change, volume, bid / ask and quantities, and the underlying.
- **Lake `option_chain`:** the same data.

**Performance:** contract IV is a per-row bisection. Expect tens of seconds per session post-market for the full F&O list.

**Unblocks AF-06:** "derivatives factors" was Blocked for want of a derivatives data source. It can now be built on `fo_contract_daily` / `option_chain_snapshot`.

## DP-14 — Macro data (`data/macro.py`)

**Series.** Eleven seeded series in `macro_series`, from **FRED CSV** (no key) and the **World Bank API**:

| Area | Series |
|---|---|
| India | CPI index, industrial production, call-money rate, 10-year yield, real GDP growth (annual), CPI inflation (annual) |
| FX and global | USD/INR, US 10-year yield, Fed funds rate, US CPI, Brent crude |

**Point in time:** `macro_observation.available_from` = end of the period + a conservative release lag. `point_in_time(series, as_of)` never shows a value before then.

**Revisions:** a changed value updates the observation and sets `revised=1`. Every fetch's full series is also kept in lake `macro_vintages`, so the as-first-published history exists from now on.

**Calendar:** `macro_calendar` holds owner entries (`POST /api/data/macro/calendar`) plus **rule-derived** expected India CPI dates (around the 12th). These are labelled `rule:day-12` and should be verified on MOSPI. No RBI MPC dates were invented; the owner should add them.

**Events:** new observations become `market_event` MACRO rows for QR-09. Direction is UP or DOWN versus the previous value. This is change, not surprise, because no consensus data exists.

## DP-21 — Multi-asset data (`data/multi_asset.py`)

**Mutual funds:** AMFI `NAVAll.txt` gives every scheme's daily NAV → `mf_nav`, with scheme code, ISINs, AMC and category. `history_mf(code, start)` fetches one scheme's history on demand.

**Other assets:** commodities (gold, silver, WTI, Brent, natural gas, copper), FX (USDINR, EURINR, GBPINR, JPYINR, DXY) and rates (US 3M / 5Y / 10Y) come from yfinance → `asset_price_daily`. The list can be extended via `multi_asset.add`.

**Not built:** **MCX and NSE-CDS exchange bhavcopies are not wired.** No free, documented endpoint was verified from this machine, so the yfinance benchmarks are labelled as what they are.

## AD-01 / AD-02 — Alternative data (`altdata/`)

**Framework** (`altdata/framework.py`):
- A `DataSource` class has `source_id`, `entity`, `metrics` and `fetch(conn, as_of, entities)`, and is registered with `@register`.
- Every `Observation` needs an `available_from`. Reads (`read`, `panel`, `zscore`) are point in time.
- `run_source` stores to `alt_observation`, writes lake `alt_<id>`, and records health in `alt_dataset`: rows, coverage, staleness and error.

**Sources** (`altdata/sources.py`):

| Source | Type | What it measures |
|---|---|---|
| `news_attention` | Internal, on | Daily article count, its 30-day z-score, and mean sentiment |
| `announcement_intensity` | Internal, on | Material NSE announcements per day and their mean tone (from W28b's table) |
| `wiki_pageviews` | External, **off** | Wikimedia daily views and their 60-day z-score. Article titles come from config `map` or are guessed from the company name; only titles the API answers for are kept. The required User-Agent is sent. |

These are **research inputs**. None feeds a score: a source becomes a signal only through IC research and the AF-08 approval gate.

## Files

**New:**
- `db/schema_w35.py` (12 tables)
- `data/lake.py`, `data/ticks.py`, `data/depth.py`, `data/derivatives_store.py`, `data/macro.py`, `data/multi_asset.py`
- `altdata/__init__.py`, `altdata/framework.py`, `altdata/sources.py`
- `dashboard/w35_routes.py`, `dashboard/w35_page.py` (`/data-platform`)

**Changed:**

| File | Change |
|---|---|
| `db/schema.py` | Applies the W35 tables |
| `data/stock_feed.py` | Tick capture hook; `capture_ticks` default |
| `data/derivatives.py` | Contract store after the summary |
| `pipeline/scheduler.py` | W35 jobs |
| `dashboard/server.py` | Registers the routes |
| `dashboard/market_page.py` | Link to Data platform |
| `enterprise/authz.py` | GET `/api/data` → `dashboard:read`; POST → `research:run` |
| `db/purge.py` | 90-day retention for snapshots and contracts |
| `config_template.json` | New sections |

## For QA

1. Run `init_db()` on a copy of the database. Expect the 12 tables.
2. **Lake:** write the same partition twice with `replace=True` at two knowledge times. `read(as_of=between)` should return version 1, and `read()` should return version 2. Run `verify()`, then corrupt a file and run it again: it should report a hash mismatch. Run `archive_table('prices_daily','date', start=...)`.
3. **F&O:** `python -m data.derivatives --date <session> --recompute`. Expect `fo_contract_daily` rows for the nearest 2 expiries, IVs that are plausible or None, and a lake `fo_bhavcopy` partition. Run `POST /api/data/fo/chain/snapshot {symbol: NIFTY}` and **verify NSE's field names**.
4. **Macro:** `python -m data.macro`. Expect 11 series (any failure is shown per series). Check that `available_from` is the period end plus the lag, that `point_in_time` hides later releases, and that `market_event` gets MACRO rows.
5. **Multi-asset:** `python -m data.multi_asset`. Expect a few thousand MF schemes and the yfinance assets. Run `--mf-history <code> 2025-01-01`.
6. **Ticks:** enable the stock feed and `capture_ticks` in session. Check lake `ticks` parts and `tick_capture_status`. At 15:50 (or via the API) 1-minute bars should be built, with volume that is monotone and non-negative.
7. **Depth:** set `depth.enabled` with 3 symbols. Check `order_book_snapshot` rows (bid < ask, spread > 0, imbalance between −1 and 1) and **verify the Dhan payload**.
8. **Alt data:** run `POST /api/data/alt/news_attention/run`. Check that coverage and z-scores are null with fewer than 10 days of history, and that `read(as_of=yesterday)` excludes today's rows. Run `wiki_pageviews` with a 3-symbol map.
9. Open `/data-platform`: every tab loads, and the macro and asset charts draw.

## Follow-ups

- **AF-06** (derivatives factors) is unblocked by DP-08.
- RBI MPC / GDP / IIP release dates are for the owner to add to `macro_calendar`.
- MCX / CDS exchange data needs a chosen source.
- `pip install pyarrow duckdb` turns on Parquet and SQL.
