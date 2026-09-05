# ATIP Consolidated Patch — September 2026

Supersedes the incremental patches v1–v5. Everything below is already included;
apply this one package only.

**Scope:** 12 files modified, 2 files added, +1,933 lines. No file outside
`atip/` is touched, so your `atip_data/` (database, logs, `config.json`) and
`main.py` are left alone.

---

## New files

| File | Purpose |
|---|---|
| `scores/predictions.py` | Writes each day's scores into the `predictions` table as a tradeable plan (entry / stop / target_1 / target_2 / R:R / position size). Nothing wrote this table before, which is why the accuracy tracker could never produce a row. |
| `scores/backtest.py` | Replays `prices_daily` to measure a fixed-percentage-target strategy. Modes: `dip` ("buy at its low"), `vol50`, `signal` (ATIP's own scores), `all` (no-selection benchmark). Models costs and exposes an entry-timing switch. |

---

## 1. The big one — 23 technical indicators were silently dead

`compute_indicators()` had every indicator inside a **single** `try/except`.
`ta.adx()` threw for every symbol, so everything computed after it was skipped.

Measured on your database (2,134 rows):

```
rsi_14      2134/2134   macd_*  2134/2134
adx_14         0/2134   <== execution stopped here
ema_*, sma_200, atr_*, bb_*, obv, volume_ratio,
pivot, r1/r2/s1/s2, fib_*, above_200dma,
golden/death_cross, gap_pct  ... ALL 0/2134
```

**This is why the Decision Engine never emitted a single BUY** (1,598 HOLD +
529 WAIT, zero BUY/SELL ever). ZPI sat at ~43 against a gate of 55 purely
because Support / Trend / ATR / Volume / Resistance were reading NULLs — the
formula itself is fine: run with real inputs, a typical stock scores 58.9 and
best case 88.7.

Fixed:
- each indicator now runs in isolation (`_step()`), so one broken library call
  costs one indicator, not the other 23;
- `ON CONFLICT` now refreshes **all 37** columns, not 4 — previously a row
  written with NULLs could never be repaired by a re-run;
- failures are summarised per run (`⚠ Indicators that failed: adx×502`) instead
  of vanishing silently;
- `rel_volume` (schema column nothing populated) is now computed.

**After deploying, re-run technicals to repair the existing NULL rows:**
`python -m data.technical --date <last trading day>`

---

## 2. Hardcoded score components replaced with real data

| Component | Was | Now |
|---|---|---|
| VPI `RS` | weight seeded, never computed | 20-day return vs the Nifty 50 benchmark series |
| ZPI / TOD `Sector` | constant `5` / `60` for every stock | real daily industry-relative rank from the Nifty 500 `Industry` column |
| TOD `Breakout` | constant `60` | 20-day-high breach + volume confirmation |
| ACS `NewsConfidence` | constant `50.0` | mean article confidence, 7-day, recency-weighted |
| ACS `Liquidity` | constant `60.0` | 20-day average turnover (shared with VPI's `LQ`) |
| ATIP `INS` | always `None` — silently dropped | new `compute_ins()` from FII/DII + promoter holding + bulk/block deals |

`MutualFund` and `Insider` from the doc's INS formula are **deliberately
omitted, not faked** — NSE publishes no free per-stock source for them. Their
weight is redistributed across the three real inputs.

New: NSE bulk & block deal fetcher (`download_bulk_block_deals`) + `bulk_deals`
table, wired into the post-market pipeline.

---

## 3. Orders — working stoploss, brackets, trailing

**The stoploss was non-functional in both directions.** Direction was inferred
from side, so:
- on a BUY rule the stop sat below the buy trigger and `price <= trigger`
  matched first — the stop branch was unreachable;
- on a SELL rule the stop was below the trigger but the branch tested
  `price >= …`, so a *falling* price triggered nothing. A crash exited nothing.

Fixed with an explicit `trigger_direction` (`ABOVE`/`BELOW`), which makes all
four cases expressible — including "sell if it falls", i.e. an actual stop.

Added:
- **Bracket orders** — an entry that fills auto-creates its exit legs, measured
  from the **actual fill price**, not the trigger;
- **Staged exit** — e.g. 50% at +3%, remainder at +6%, configurable split;
- **Trailing stop** — keeps a fixed distance below the high-water mark, only
  ever moves in your favour, optional ₹ step (like Dhan's trailing jump);
- **Correct partial-fill reconciliation** — a filled target *shrinks* the stop's
  quantity and leaves the other target alone. (An earlier version cancelled the
  whole group, which would have left the remaining shares unprotected.)
- **Telegram on trigger** — pending / executed / failed / expired, plus a loud
  `POSITION UNPROTECTED` if an entry fills but its legs can't be created.
- Confirmation window **120s → 30 min**, with a re-check on confirm that refuses
  if price moved >1% against you since the trigger (overridable).
- Auto-execution refuses to act on a **quote older than 180s** — aimed at the
  2+ hour silent-process failure your `orders.py` documents.

### ⚠ Trailing is computed by ATIP, not by Dhan

It only trails while the dashboard process is alive. For an overnight position
the stop should live at the broker. `_place_order()` currently passes no bracket
params, and `requirements.txt` pins only `dhanhq>=1.4.0` (v1 and v2 differ), so
broker-side placement is **scaffolded but not implemented**. Run:

```
python -m orders.rules --probe-broker
```

It reports what your installed SDK actually exposes. Note Dhan's Bracket/Cover
products are **intraday-only** — a multi-day CNC hold needs a GTT/Forever order.

---

## 4. Measurement — you can now know your hit rate

- `predictions` is written every scoring run (also fills `risk_reward` and
  `position_size_pct`, both previously always NULL).
- `accuracy.py` rewritten. Five real bugs fixed, the worst: `hit_target_1` only
  compared the **close on one single day**, so a stock that touched +3% and fell
  back was recorded as "never hit target" — exactly backwards for a
  take-profit-at-3% strategy. It now scans the whole holding window on intraday
  high/low. Also self-backfilling (a missed run no longer leaves a permanent
  hole), holiday-aware, and no longer interpolates a symbol into SQL.
- Accuracy now runs **daily** post-market, not Saturday-only.
- `python -m scores.predictions --backfill` recovers plans from existing scores.

---

## 5. Dashboard

- **Buy/Sell buttons on every screen** (ATIP Scores, Portfolio, Top VPI, Buy
  Zones, CRI Risk, and the Trade-of-the-Day card). Previously only the main
  scores table had one, and the Portfolio tab had a phantom `Action` column
  header with no button — the Signal value was rendering under it.
- Bracket panel: Target 1 + % of qty, Target 2, stoploss, trailing, auto-exit.
- Missing-CMP handling: falls back to the live-quote cell, then locks the form
  to an absolute price rather than storing a rule against a ₹0 reference.
- Real validation and success feedback on save.

---

## 6. Other fixes

- **News symbol attribution**: was a hardcoded 19-symbol list (11% of articles
  linked, 15 symbols ever matched). Now derives aliases for the whole universe
  from the Dhan security master, with the curated list as overrides and generic
  words (BANK, POWER, STEEL…) filtered out.
- **Telegram placeholder detection**: `YOUR_TELEGRAM_BOT_TOKEN` is non-empty, so
  the old check passed it to the API and got a 404 — every alert this system ever
  attempted failed looking like a network fault. Now reported as *not configured*.
- `bulk_deals` added to the 600-day purge tier.
- Decision Engine thresholds extracted into documented constants, plus a warning
  when a scoring run produces zero actionable signals.

---

## Deploy

```bash
# 1. extract over your existing install (touches only atip/)
python -m zipfile -e ATIP_consolidated_patch.zip D:\Projects\

# 2. additive schema migration — safe on your existing database
python main.py --init

# 3. repair the NULL indicator rows the old ON CONFLICT could not fix
python -m data.technical --date 2026-07-30

# 4. recover measurable history from scores you already have
python -m scores.predictions --backfill

# 5. restart the dashboard (it hosts the order monitor)
```

Then reproduce the strategy measurements:
```bash
python -m scores.backtest --mode dip --sweep
python -m orders.rules --probe-broker
```

---

## Still needs YOUR action — these are config, not code

| Setting | Where | Without it |
|---|---|---|
| `telegram_token`, `telegram_chat_id` | `atip_data/config.json` | no alert ever reaches you |
| `alpha_vantage_key` | `atip_data/config.json` | `fundamental_data` stays empty → SPI/FS NULL → 25% of ATIP weight dead |
| `ANTHROPIC_API_KEY` | **environment variable** | news stays on the keyword fallback, never Claude |
| `kite_api_key/secret` | `atip_data/config.json` | Zerodha portfolio fallback keeps failing (Dhan is primary, so optional) |

---

## Known limitations (not fixed)

- **9 of 18 `config.json` keys are read by nothing** — `schedule_*_time`,
  `intraday_refresh_mins`, `dashboard_port`, `max_stocks_to_score`,
  `min_data_days`, `primary/eod/global_data_source`. Editing them does nothing;
  the values are hardcoded in `scheduler.py` / `server.py`.
- **`institutional_data` is never written by any code path** — VPI `IS`,
  RRI/ZPI `Institutional` remain absent.
- **`pcr` and `adv_decline` are never written** — so MSI `Options` is a constant
  61.54 and MH `Breadth`+`AdvanceDecline` a constant 25.93 (0.10 and 0.15 of
  those indexes).
- **`adj_close` is never populated** — every indicator runs on unadjusted close,
  a correctness risk for recent splits/bonuses.
- **NSE holiday list** has only 4 fixed-date holidays; festival dates need
  adding manually each year.
- Dead code still present: `atip/orders/*.py`, root `./orders/`,
  `static/orders_panel.*` — superseded by `orders/rules.py`, safe to delete.
- `bhavcopy` was failing 11× vs 2 successes and `portfolio_sync` 9× in your job
  log; those root causes are not addressed here.

## Verification notes — what I did and didn't test

Tested end-to-end against your database: bracket creation, OCO/partial-fill
reconciliation, trailing ratchet, trigger direction (all four cases), the
accuracy tracker (synthetic matured prediction, all three horizons), prediction
backfill (1,563 rows), dashboard rendering, and the backtester (~18,700 trades
per configuration).

**Not tested:** the technical-indicator fix could not be run here (no pandas in
my environment) — it compiles and the restructure is mechanical, but its first
real run is the proof. No real Dhan order was placed; the order path was tested
with a stubbed broker. Dry-run a single share before sizing up.
