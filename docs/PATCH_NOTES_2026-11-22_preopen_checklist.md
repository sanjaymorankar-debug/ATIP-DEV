# Patch notes: 9:00 AM Pre-Open Checklist (22–23 Nov 2026)

Owner prompt 22 Nov 2026, 11:21 PM IST. The Step 1 audit was shared first; the owner answered "Yes, build defaults".
Branch `claude/preopen-checklist-2026-11-22`, off GitHub master `0f8f539`. **PAPER only**: nothing here places, sizes
or confirms an order. All times are IST.

## What it does

At 09:00 IST on NSE trading days (again at 09:09, after the pre-open auction matches) ATIP runs four checks. Three of
them vote:

| Check | Votes? | Rule (defaults; all set in `atip_data/config.json` "preopen_checklist") |
|---|---|---|
| 1. US cues: Nasdaq + Dow previous close, NQ + YM futures | +1 / 0 / −1 | Each leg is green above +0.10 % and red below −0.10 %. The vote is the sign of green legs minus red legs. |
| 2. India VIX | **No**: a modifier | Above 15 is Volatile, otherwise Stable. Advice only on Volatile days: size ×0.5, stops ×1.5, avoid buying options, prefer defined-risk. |
| 3. NSE advances vs declines | +1 / 0 / −1 | NSE pre-open book, all securities (key=ALL): ratio above 1.0 is +1, below 1.0 is −1. Before NSE publishes the book, the previous day's A/D, labelled "prior-day". |
| 4. GIFT Nifty (NSE IX) | +1 / 0 / −1 | Basis-adjusted move (GIFT now vs GIFT at 15:30 IST yesterday): within ±0.15 % is Flat, else Gap-Up or Gap-Down. The raw "vs previous Nifty close" figure is always shown. |

- **Bias**: the vote sum. +1 or more is Bullish, −1 or less is Bearish, otherwise Neutral.
- **Volatility regime**: from VIX.
- **Expected open**: from GIFT.
- Every check stores its raw value, status (LIVE / FALLBACK / STALE / UNAVAILABLE), source and fetch time in IST. A
  failed fetch becomes a caveat, never an exception.

## Files

**New**

| File | Purpose |
|---|---|
| `strategy/preopen_checklist.py` | The four checks, vote aggregation, storage (`preopen_signals`), the 15:31 GIFT reference, the learning loop (`evaluate`, `record`), text for Telegram and the apps, CLI. |
| `dashboard/preopen_routes.py` | The `/home` card, the `/preopen` page, `GET /api/preopen` (`?compact=1` for the apps), and `POST /api/preopen/run` (token; refused on the view-only server). |
| `tools/preopen_backtest.py` | The backtest. Results are in `docs/reports/2026-11-22-preopen-checklist/BACKTEST.md`. |
| `tests/test_preopen_checklist.py` | 28 tests. |
| `mobile/android/.../PreOpenReceiver.kt`, `PreOpenRules.kt`, `res/drawable/ic_notify_preopen.xml` | Android 09:00 IST notification. |
| `mobile/ios/ATIP/PreOpenReminder.swift` | iOS 09:00 IST local reminder. |

**Changed**

| File | Change |
|---|---|
| `ops/migrations.py` | Migration **0007** `preopen_checklist_signals`: creates `preopen_signals` and its index. Additive. Rollback: drop the table. |
| `pipeline/scheduler.py` | Jobs: 09:00 and 09:09 runs, 15:31 GIFT reference, 15:45 scoring; the preliminary run inside the 08:15 report refresh. Trading days only. |
| `pipeline/premarket_report.py` | New `preopen_checklist` section (the 08:15 preliminary read). GIFT now comes from the checklist's source. |
| `alerts/telegram.py` | The 08:30 brief gets a checklist line. Its GIFT figure no longer reads Dhan's frozen value. |
| `research/market_pulse.py` | `capture_gift` (08:45 / 09:05) takes GIFT from the checklist's source and uses the 15:31 reference. It never mixes in Dhan's frozen quote. |
| `dashboard/ux_pages.py` | The Pre-Open Checklist card is first on `/home`. The Market card's GIFT line uses the live GIFT. |
| `dashboard/market_routes.py` | `/api/market/preopen` computes its implied gap from the live GIFT and adds `gift_live` and `gift_dhan_frozen`. |
| `dashboard/server.py`, `shell.py`, `labels.py` | Route registration, the Reports → Pre-open checklist menu entry, job labels. |
| `mobile/*` | Version 2026.11.22.1 (build 13): 09:00 IST notification. See `mobile/README.md`. |

## Fixes to existing behaviour (found in the audit)

- **Dhan's GIFT Nifty (`IDX_I` 5024) is frozen**: one value all day; 23,240 from 2 to 6 Nov while the Nifty moved from
  23,441 to 24,537. `market_cue`, the 08:30 brief ("GIFT +0.00 %"), the Home Market card and `/api/market/preopen` all
  read it. They now read the checklist's GIFT source (NSE `marketStatus`, then the configurable fallback adapter,
  which is OFF until a URL is set).

## Not changed, on purpose

- Market Health (VIX weight 0.10, A/D 0.05), `vol_regime` (13 / 20), the CRI steps (18 / 25), the Telegram VIX alert
  (25) and Market Pulse's RISK_ON/OFF context are untouched. The checklist feeds none of them.
- Autopick, the arms, stops, sizing and every order path are untouched (owner decision D2).

## Follow-up fix DEF-06 (23 Nov 2026): GIFT contract switch

The VPS's live NSE payload (08:46 IST 23 Nov) showed the 29-Dec-2026 GIFT contract already in use.

**The problem.** The 15:31 reference kept no contract. So the morning after NSE switches contracts, the
"basis-free" GIFT move would compare two different months' prices and read their gap (about 0.3–0.7 %) as a gap
in the market.

**The fix:**
- `strategy/preopen_checklist.py`
  - New table `preopen_gift_ref` (`GIFT_REF_DDL`, migration **0008**). It holds the date, price, contract (expiry),
    quote time, source and capture time.
  - `capture_gift_close()` writes it and still writes `global_snapshot`.
  - `gift_reference()` returns the contract.
  - `check_gift()` does not use a reference from another contract: it falls back to NSE's day change of the
    contract, with a caveat naming both contracts.
  - A reference with no contract on record is still used, with a caveat.
  - New `expiry_key()` reads NSE's spellings.
- `research/market_pulse.py`: the 08:45 GIFT estimate passes the quote's contract, and skips a reference from
  another contract.
- `ops/migrations.py`: 0008 `preopen_gift_reference`. It is additive; 0007's text and checksum are unchanged.
- Tests UT-29..35 are in `tests/test_preopen_checklist.py`.

## Deploy

- 86db41e went to GitHub master at 08:44:20 IST, 23 Nov. The VPS session deploys it with the owner's yes.
- The owner said on 23 Nov that paper mode makes market-hours deploys safe. Still avoid the job minutes and 09:14–09:35 IST.
- Migrations 0007 (and 0008 with the DEF-06 fix) run at start-up.
- 24 Nov 2026 is an NSE holiday, so the first live 09:00 run is **Wed 25 Nov 2026**.
- VPS read-only checks, 23 Nov:
  - `marketStatus` carries `giftnifty`.
  - Pre-open `key=ALL` answers with whole-market counts.
  - The `.in` redirect is Mac-only (TEST_REPORT §6a).
