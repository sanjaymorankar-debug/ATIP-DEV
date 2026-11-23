# 9:00 AM Pre-Open Checklist: Step 1 audit and comparison

Prepared 22 Nov 2026, 23:29 IST (Sunday). Read-only audit. Nothing was built, changed, restarted or deployed.

**Code audited:** GitHub master `0f8f539`, the code the VPS runs (`43ca948` plus docs-only commits).
The main folder `/Users/agtci/ATIP` is still on `be077b2`, 13 commits behind, so it was not used.

**Data checked:** the Mac's `atip_data/atip.db`, opened read-only. It stops at 17 Nov 2026 11:58 IST, when the Mac
was retired. Live data is on the VPS (PostgreSQL), and this Mac cannot reach it.

**Skill audited:** `share-market/SKILL.md` (Module A). It is installed in the Claude desktop app's plugin folder,
outside the ATIP repo.

## 1. Comparison table

| # | Check (spec) | Already in ATIP? | Where (file / function / table) | What is missing or wrong | Action |
|---|---|---|---|---|---|
| 1a | US cues: Nasdaq + Dow previous close | **Yes** | `data/markets.py` `fetch_global_markets()` → `global_markets.nasdaq_chg`, `dow_chg` (Yahoo `^IXIC`, `^DJI`), run by the pre-market job (`pipeline/scheduler.py:511`); history in `global_market_history` | Feeds a 0–100 `global_score` (US 40 % / Asia 25 % / commodities 35 %). It is never turned into a +1/−1 vote. Only about 28 days of Nasdaq/Dow history are stored on the Mac. | **Reuse** the stored closes. **Build** the vote. |
| 1b | US cues: live NQ / YM futures at 09:00 | **Partial** | `research/global_sync.py` captures `NQ=F` and `ES=F` at 08:42 and 15:31 (`global_snapshot`) | No `YM=F` (Dow futures). Nothing is captured at 09:00. | **Extend** with a new adapter: one Yahoo call for NQ=F + YM=F at 09:00. Leave global_sync's model inputs unchanged. |
| 1c | Related models already in ATIP | Yes | `research/market_pulse.py` `global_cue_model()` (ridge, 9 factors, walk-forward record); `global_sync.model()` (synchronised moves) | These are richer than a vote, and they do not conflict. | **Show** them next to the vote. Do not replace them. |
| 2 | India VIX > 15 → Volatile, ≤ 15 → Stable; modifier only | **Partial** | VIX is fetched from Dhan `IDX_I` id 21 into `index_levels.india_vix`, and its daily close into `prices_daily` `INDIAVIX` (422 days). `scores/engine.py` `get_vix()`. Market Health VIX component: `minmax(8..35)` inverted, weight 0.10. `strategy_engine/regime.py` `vol_regime`: LOW < 13, HIGH ≥ 20. CRI `MarketWeakness`: +30 above 18, +20 more above 25. Telegram VIX alert: "panic" above 25. | Nothing uses a 15 threshold. Nothing maps VIX to a size or stop-width recommendation, or to an "avoid option buying" flag. | **Reuse** the fetch. **Build** the regime field and the advisory size/stop/options flags (see conflict C1). |
| 3a | NSE A/D from the pre-open session (09:00–09:08) | **Yes, but late and narrow** | `data/preopen.py` `capture()` at 09:09 → `preopen_snapshot`; `summary()` gives advances and declines by IEP change. `research/market_pulse.pulse()` shows it with no vote. | It reads only the `NIFTY` and `FO` keys (about 200 stocks), not the whole market. There is no 09:00 read, because NSE serves the previous day's page until about 09:00 and `capture()` correctly refuses it as STALE. | **Reuse** it. **Extend** it: whole-market counts from NSE's `key=ALL` payload (top-level advances and declines), with the vote taken from the post-matching read (see C3). |
| 3b | Prior-day A/D fallback, labelled "prior-day" | **Yes, but a different universe** | `market_health.advances` / `declines` (ATIP's ~500-stock universe, since Mar 2025). The NSE Bhavcopy archive (`data/bhavcopy_history.py`, lake `nse_cm_bhavcopy`) covers 7 years of the whole NSE market. | Neither is labelled or used as a pre-open vote. | **Reuse** the Bhavcopy for whole-market prior-day A/D, falling back to `market_health`. **Build** the label. |
| 4 | GIFT Nifty vs previous Nifty close, ±0.15 % neutral band | **Exists but broken** | `data/dhan.py` index id `gift_nifty` = `5024` (`IDX_I`) → `index_levels.gift_nifty`; `research/market_pulse.capture_gift()` at 08:45 and 09:05 → `market_cue` (beta-fitted expected gap, event band); `evaluate_gaps()` at 09:35; `gap_record()` hit rate; `/api/market/preopen` (implied gap vs last close); 08:30 Telegram brief line "GIFT ±x %" | **Dhan's GIFT value is frozen.** On 4, 13, 16 and 17 Nov it never changed all day: 1,516 / 1,621 / 1,541 / 716 rows, one value each. It stuck at 23,240 from 2 to 6 Nov while the Nifty moved from 23,441 to 24,537. `gift_nifty_chg` is 0.00 on every row from 11 to 17 Nov, so the 08:30 brief's GIFT line is meaningless. `market_cue` holds bogus moves (+5.34 % on 6 Nov, +2.09 % on 9 Nov), and only 12 of 26 mornings have any GIFT value. | **Build** a new GIFT source adapter (see D1). **Fix** the existing `market_cue` and brief so they stop reading the frozen value. |
| 5 | Vote sum → Bias; Regime; Expected Open | No | `pulse()` has a different composite (RISK_ON / NEUTRAL / RISK_OFF from 6 weighted votes) | There is no 3-vote checklist and no stored daily row. | **Build** `strategy/preopen_checklist.py` (ATIP has no `atip/strategies/` package, and `strategy/` is the nearest home) and table `preopen_signals`. |
| 6 | Scheduler 09:00 + refresh, weekdays, skip holidays | Partial | `schedule` jobs at 08:42, 08:45, 09:05, 09:09, 09:35; `is_market_day()` / `utils/trading_calendar.is_trading_day()` | There is no 09:00 job. The NSE holiday list ends 25 Dec 2026 (already flagged). | **Build** a 09:00 run and refresh. **Reuse** the holiday skip. |
| 7 | Dashboard card + `/api/preopen` | No | The `/market` page and `/api/market/preopen` (a different endpoint: GIFT + PCR + FII); the Market Pulse page | — | **Build** the card on the home dashboard and on the mobile `/m` screens, and a new `GET /api/preopen`. Keep `/api/market/preopen` unchanged. |
| 8 | 08:30 report + skill Module A | Partial | `pipeline/premarket_report.py` (built at 06:00 and 08:15); `alerts/telegram.send_morning_digest()` at 08:30; skill Module A (web search, its own scoring) | Neither has the checklist. | **Extend** the report with a "preliminary checklist" section (US closes, futures at 08:15, VIX prior close, prior-day A/D, GIFT). **Deliver** a SKILL.md patch (see C5). |
| 9 | Learning loop (EOD gap + close direction, 20/60-day hit rate) | Partial | `evaluate_gaps()` (actual open = first `index_levels` reading 09:15–09:30), `gap_record()`; `preopen.record()` | Nothing scores each of the 4 checks and the combined bias over rolling 20/60 days. | **Build** it. **Reuse** the same "actual open" definition. |
| 10 | Backtest ≥ 1 year | Partial data | VIX daily since Mar 2025; Bhavcopy A/D for 7 years; US closes via Yahoo history | **`prices_daily` NIFTY50 has open = high = low = close on all 422 rows** (close only), so ATIP cannot measure a historical gap today. GIFT has no usable history (see row 4). Pre-open A/D has only 15 mornings. | **Build** a backtest on a Nifty OHLC series (NSE's daily index file or Dhan `IDX_I` history). GIFT and pre-open A/D are forward-test only, and the report will say so. |
| 11 | Mobile apps (Android + iOS) card + 09:00 notification | Partial | Thin native shells around the server's `/m` screens; native banner from `/api/v2/shell/state`; v2026.11.18.1 | Neither app has any notification code. iOS still cannot be built on this Mac (no Xcode; needs the owner's Apple ID). | **Build** the card server-side (both apps show it with no store release) and the native 09:00 notification (see D4). |
| 12 | Paper only, no live orders | Yes | The checklist is informational. It is never linked to `orders/`, autopick or arms. | — | Nothing to do. A test will assert that the module imports nothing from `orders/`. |

## 2. Conflicts with existing logic (none silently overridden)

**C1. VIX thresholds disagree in five places.** The spec uses a binary 15. ATIP already uses `vol_regime` 13/20,
the CRI step at 18 and 25, the Telegram panic level at 25, and the Market Health inverted 8–35 scale. The skill's
regime table uses < 15 and > 20.
*Proposal:* add a new field, "Pre-open volatility regime: Volatile/Stable (VIX vs 15, configurable)". Keep
`vol_regime`, MH and CRI exactly as they are. The size multiplier, stop-width hint and "prefer defined-risk" flag are
**advisory text only**. Wiring VIX into autopick, the arms' stops or sizing would change live paper-trading rules (the
owner's −2 % stop, trail 1 % from +3 %, and the S1–S10 arms) and needs a separate yes.

**C2. GIFT vs "previous Nifty close" compares a future with spot.** GIFT Nifty is a near-month future and normally
trades at a carry premium to the spot close, which pushes a raw comparison toward "Gap-Up". ATIP's existing design
removes this basis: it compares GIFT now with GIFT at 15:30 IST yesterday. The 12 stored mornings are too few and too
corrupted (row 4) to settle which works better.
*Proposal:* store and show both numbers. Base the vote on the basis-adjusted move (GIFT now − GIFT at yesterday's
15:30), and show the spec's raw "GIFT vs Nifty prev close" alongside. The learning loop then scores both.

**C3. The pre-open book closes at a random second between 09:07 and 09:08.** A refresh at 09:08:00 can read an
unfinished book. That is why the existing capture runs at 09:09.
*Proposal:* refresh at 09:09, chained right after the existing `preopen_capture`. If you want exactly 09:08, it
can be done, labelled "may be pre-match".

**C4. Two "bias" labels on screen.** Market Pulse already shows RISK_ON / NEUTRAL / RISK_OFF.
*Proposal:* label the new one "Pre-Open Checklist bias". It does not feed Market Pulse or Market Health (no
double-counting of GIFT or US moves). The learning loop shows both records side by side.

**C5. The skill's Market Health differs from ATIP's.** The skill's own 0–100 MHS gives VIX 15 points, A/D 15 and
GIFT 10. ATIP's MH weights VIX 0.10 and A/D 0.05, and has no GIFT input. The skill also fetches by web search.
*Proposal:* Module A reads ATIP's `/api/preopen` and ATIP's MH instead of recomputing them. The skill file sits in
the desktop app's plugin folder: I will deliver a SKILL.md patch in the zip for you to apply or republish. Editing it
in place would be lost on the next plugin update.

**C6. `/api/market/preopen` already exists** and means something else (GIFT, PCR, FII). The new endpoint is
`/api/preopen`, as the spec asks, and the old one stays unchanged.

## 3. Side findings (existing defects, outside the 4 checks)

- **F1.** Dhan's `gift_nifty` (`IDX_I` 5024) is frozen, so `market_cue`, `gap_record` and the 08:30 brief's GIFT line
  have no real GIFT data. The fix is part of row 4.
- **F2.** `prices_daily` NIFTY50 and INDIAVIX are close-only (open = high = low = close). Anything reading their
  "open" is wrong. The backtest will use a real OHLC source.
- **F3.** `https://www.nseindia.com/api/...` now answers **301 → `www.nseindia.in`**. NSE's own Akamai edge sent
  this at 23:28 IST 22 Nov, setting cookies for `.nseindia.com`. ATIP's NSE fetchers (pre-open, filings, F&O)
  follow redirects, but the cookie domain differs, so they may be failing quietly on the VPS. This should be checked
  on the VPS. I did not follow the redirect to the new host from here.
- **F4.** The NSE holiday list ends 25 Dec 2026 (already known). 24 Nov 2026 (Tue, Guru Nanak Jayanti) is a
  holiday, so after a Monday-evening deploy the first live 09:00 run would be **Wed 25 Nov**, and 24 Nov tests the
  holiday skip.

## 4. Decisions needed before building

- **D1. GIFT source.**
  - (a) **Recommended:** NSE's `marketStatus` API, which on NSE's site carries a `giftnifty` block. It goes through
    ATIP's existing NSE client, and it must be verified first, given F3.
  - (b) The NSE IX website, as a fallback adapter.
  - (c) investing.com, isolated behind the fallback adapter, only if (a) and (b) fail. The spec discourages this.
- **D2. VIX:** advisory only (**recommended**), or also apply a size multiplier in the paper autopick (changes trading
  rules).
- **D3. US vote rule:** sum of the signs of Nasdaq close, Dow close, NQ at 09:00 and YM at 09:00, each with a
  ±0.10 % neutral band. Sum > 0 → +1, < 0 → −1, 0 → 0. All bands are configurable.
- **D4. 09:00 notification.**
  - Android: a scheduled local notification that fetches `/api/preopen` and shows bias, regime and open.
  - iOS: a local 09:00 notification. Without a push service (APNs needs your Apple Developer account) it can only say
    "Pre-Open Checklist ready", and it is built but unverifiable until Xcode and your Apple ID are set up.
  - Optionally also a 09:00 Telegram line, through ATIP's existing alert channel.
- **D5. Test and Prod servers.**
  - Test = a local instance on the Mac (scratch DB, fake broker and feeds, a replayed 09:00).
  - Prod = the live VPS. Deploying there needs your yes and goes through the VPS session (this Mac has no VPS access),
    outside 05:45–15:35 IST.

Defaults if you just say "build": D1(a) with (b) fallback, D2 advisory, D3 as written, D4 Android data notification +
iOS generic + Telegram line, D5 as written. Bias = Bullish when the vote sum is ≥ +1, Bearish when ≤ −1, else Neutral
(configurable).
