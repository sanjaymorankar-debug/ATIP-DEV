# ATIP — Loss RCA, data errors, code review and strategy update — Thursday 19 Nov 2026

Owner prompt: 19 Nov 2026 06:48 PM IST. Work done 18:55–21:00 IST on a copy of the live data; nothing on the VPS was
changed by this work, no order was placed, modified or cancelled, and the broker was read with GET calls only.
**Every time in this report is IST (UTC + 5:30).** PostgreSQL `created_at` columns on the VPS are stored in UTC and are
converted here (e.g. a Bhavcopy row stamped 11:15:02 UTC = 16:45:02 IST).

**Where the data came from.** Live ATIP has run on the VPS (systemd `bkesari-atip.service`, PostgreSQL) since 17 Nov
11:59 IST; the Mac copy is retired. The VPS session collected, read-only, at 20:10 IST: the 19:15 IST `pg_dump`
(138,563,819 bytes, sha256 74bd1da7…d5f4 on both ends), the service journal and ATIP log files from 17 Nov 11:55 to
19 Nov 20:10, the config without secrets, the Dhan order list / trade book / positions / holdings / funds, and 72,733
one-minute Dhan candles (the 20 symbols ATIP ordered since 12 Nov and 13 NSE indices). The dump was restored into a
throwaway local PostgreSQL. Note: the VPS restarted at 20:08:52 IST for the owner-approved c03fc29 (data-quality) deploy,
unrelated to this work. Tools and raw outputs: `.ux_build/loss-rca-2026-11-19/`; evidence in `evidence/` beside this file.

---

## 1. Executive summary

**Today was not a losing day for the book — it had one losing trade.** The paper book's realised P&L was **−₹670.10**
(one stop-out), but the eight positions it held all rose with the market (Nifty 50 +1.14 %), so the day **marked to
market was +₹2,718.86** (`pnl_daily` PAPER, 16:50 IST; ATIP's own 16:00 RCA, marked at its last candle, says
+₹2,863.95). Paper equity closed at **₹5,13,692.00, a new high** (drawdown 0.0 %). Since the ten sessions started
(29 Oct) the sessions are **+₹8,826.76** (realised +₹5,883.07 on 41 closed trades, open +₹2,943.69). The Dhan account's
own 8 holdings (not traded by ATIP; Dhan order book and trade book were empty today) were +₹969.54 on the day.

The one loss: **S3 AUROPHARMA**, 15 shares bought 13 Nov 11:59 at ₹1,818.91, sold by its stop at 09:21:41 at ₹1,779.30,
**net −₹670.10** after ₹75.95 charges. Top three reasons, in order of weight:

1. **The stop was too tight for the stock's normal move (configuration).** The trade was bought on 13 Nov, before the
   16 Nov session arms, so it kept the old rule: stop 2 % under the entry (₹1,782.53) — 0.83× its 14-day ATR (2.42 %).
   The stock was already −1.24 % at the previous close, so the stop sat only **0.77 % under yesterday's close**, and an
   ordinary opening dip (−0.82 % by 09:21) hit it. The stock bottomed at 09:28 (₹1,778.00) and closed ₹1,800.00
   (−1.04 % from entry). S3's own rule since 16 Nov (stop 5 %, target 10 %) would not have fired: −₹283.65 gross marked at
   the close instead of −₹594.15 realised. Across the history the same pattern repeats: all 18 stop-outs since 30 Oct were
   on this 2 % rule; 11 of them closed the same day above the price they were sold at and 7 were back above their entry
   within three sessions.
2. **An opening dip in the sector and the market (market).** Between 09:15 and 09:21 Nifty 50 fell −0.31 % and Nifty
   Pharma −0.75 %; both recovered (Nifty closed +1.14 %, Nifty Pharma +0.47 % on its first print). No negative news
   (sentiment 0.00; the only filing was a shareholders' meeting notice).
3. **A weak entry signal (signal quality).** The pick was a rank-fill HOLD (no BUY that day) scoring 62.83, under the
   63 "borderline" line; in four sessions it never reached the +3 % trail (best +1.66 %).

**Not causes today:** bad data (no data error touched AUROPHARMA or any traded stock), execution (the stop was seen on
the 09:21:39 poll and sold at 09:21:41, 0.18 % under the trigger — normal), or a code bug (the stop fired as configured).

**The bigger finding (Parts 3, 4, 6).** The old rules (2 % stop, trail 1 % from +3 %, no market filter) are structurally
negative over a full market cycle: on ATIP's own signals since 28 Jul they lose **−1.16 % a trade** (330 trades, profit
factor 0.49, worst drawdown −₹1,07,660 on ten ₹25K slots). They made money from 4 Nov only because the sessions started
at the bottom of a rebound (breadth50 rose from 10 % in late October to 60 % on 12 Nov). The 16 Nov arms (a breadth
gate and wider stops on 9 of 10 sessions) are the right direction and are confirmed by a 7-year study (Kite retest K1
with the breadth50 gate: +1.05 % a trade over the last 12 months vs −1.07 % without it). What remains: three open
positions still carry the old 2 % stop, the Kite-retest resting buys ignore the gate when they fill (S4 CDSL filled
today at 09:15:29 with S4's gate closed), and the code-review defects in section 6.

---

## 2. Today's data errors (Part 1)

No data error fed a signal or a trade today. Checks on the restored 19:15 IST snapshot (`evidence/part1_dq_2026-11-19.json`)
and the ATIP log (`evidence/part1_logs_2026-11-19.json`).

### 2.1 Errors and warnings in today's logs, by type (atip.jsonl / atip.log, 19 Nov, IST)

| Level | Count | First – last | Type | Root cause | Fed a signal/trade? |
|---|---|---|---|---|---|
| ERROR | 1 | 06:00:36 | `history_backfill` FAILED — 7/7 symbols refused | Dhan DH-907 "incorrect parameters" for 7 new listings (TENNIND, TMCV, URBANCO, VAML, VEDPOWER, VISL, VOGL) — their security ids / history not in Dhan yet (hypothesis) | No (7 scored stocks lack SMA200; none picked) |
| WARNING | 7 | 06:00:34 – 06:00:36 | Dhan API error DH-907 per symbol | as above | No |
| ERROR ×18 / WARNING ×9 | 54 | 06:01:12 – 17:16:11 | KSE100 (Pakistan) index: investing 403, psx_dps 403, yfinance no data | external sources refuse the VPS | No (Asia is 5 % of Market Health; job PARTIAL) |
| WARNING | 3 | 16:48:16 – 17:10:13 | NSE index closes not published yet for 19 Nov | NSE's evening file lands ~17:00; checked at 16:48 | No — but it made the stored data_quality row ERROR (DQS 89.86, 16:48:17) — the known false alarm, **fixed by c03fc29, live since 20:08:52 IST tonight**; the VPS session reports re-running 17–19 Nov at 20:12 IST with DQS 100 / OK (after this snapshot) |
| WARNING | 1 | 16:49:50 | ADX failed for 70 symbols | all outside the 499 scored (scored ADX NULL = 0) | No |
| WARNING | 3 | 20:08:56 | start-up config warnings (rate limiting off; experimental PostgreSQL) | the c03fc29 restart | No |

The order path logged no error or warning all day (09:00–15:35): the order monitor polled every ~31 s, the 09:15:29 CDSL
buy and the 09:21:41 AUROPHARMA sale are the only orders.

### 2.2 Data-quality checks

| Check | Result (19 Nov) | Verdict |
|---|---|---|
| prices_daily rows | 3,342 (3,328 Bhavcopy at 16:45:02 IST; 13 NSE index closes 17:20:14 IST; 1 from index_levels); EQ 2,776 | OK |
| Missing scored symbols | 0 of 499 | OK |
| Duplicates | impossible (`UNIQUE(symbol,date)`); one created_at batch per source | OK |
| Zero / NULL / inconsistent OHLC | 0 / 0 / 0 | OK |
| Corrupted DATE values | all 69,909 rows written since 17 Nov are `YYYY-MM-DD` | OK |
| Weekend / future dates | only 8 Nov (539 rows: the Muhurat session, kept on purpose) | OK |
| Stale symbols | 0 tracked symbols without a 19 Nov bar | OK |
| **Dual-batch "sawtooth"** | Mechanism still present (DS-01: Dhan's 16:45 job rewrites D-1..D-5 over NSE's rows), **but no effect**: stored OHLC = NSE's official file for all 499 scored stocks on 12, 13 and 16 Nov (0 of 1,497 differ by > 0.05 %). The ±8 % reversal scan finds 11 names since 1 Nov, none in the scored universe | OK (latent, Low) |
| technical_indicators | 3,383 rows (every Bhavcopy symbol — DS-06; 93 s, no delay). Among the 499 scored: EMA9/21/50, RSI, ATR, ADX, MACD NULL = 0; SMA200 NULL = 7 (CLEANMAX, CMPDI, VOGL, FRACTAL, VAML, VEDPOWER, VISL — new listings, < 200 bars) | OK |
| ai_scores freshness | 499 scored 16:50:35, re-scored 18:10:52 after FII/DII; every symbol's latest = 19 Nov; signals BUY 2 (PNB, MARUTI), HOLD 464, WAIT 33 | OK |
| NULL factors | SPI and fund_score NULL for all 499 (switched off, CAP-08): 25 % of the ATIP weight (SPI 0.15 + FS 0.10) is absent and renormalised | Known gap — see section 5 |
| Sentiment | 447 articles, all `classifier='rule'` because `news.ai_enabled=false` in config (labelled, not a silent fallback); NULL sentiment 0; news_symbol_score 398 rows, 0 NULL | OK (AI news is off by choice) |
| Live quotes | 1,500 rows, **50 symbols only** (the REST list is 50 hard-coded names, DS-08), 07:00–16:45; NULL LTP/prev/open/chg 0; chg_pct recomputed: 0 mismatches; first quote after 09:15: prev_close = stored 18 Nov close for 50/50 (no wrong fallback, the double-wrapped envelope is fixed). After 15:30 Dhan's prev_close becomes today's close (after-hours chg ≈ 0, DS-09) | OK (stops use their own quote call) |
| Bhavcopy flood | 3,328 symbols stored, scoring held to 499 (no flood into signals); technicals still run for all (DS-06) | Partly |
| Post-market target date | bhavcopy_eod run_date 2026-11-19 at 16:45:02 | OK |
| date.today() | VPS clock is Asia/Kolkata (checked 18 Nov); code assumes local = IST (SCH-6, latent) | OK today |
| Nifty 50 / VIX benchmark | stored 17:20 IST; beta_1y NULL 0; index_levels 1,640 rows 09:00–15:44; the last row's Nifty change was real every day 12–19 Nov (DS-02's 0 % fallback did not trigger) | OK |
| Breadth gate input | 18 Nov breadth50 49.90 (stored 06:00:25 IST) → gates of S1–S4, S6–S9 closed on 19 Nov (correct). 19 Nov breadth50 = 55.83 → open on 20 Nov | OK |

---

## 3. Today's trade-by-trade analysis (Part 2)

All PAPER (simulated fills at live Dhan prices; `order_log.mode = PAPER` for both; Dhan order book and trade book: 0).

| Session | Symbol | Source | Entry (IST, ₹) | Exit (IST, ₹) | Qty | Stop / target | P&L | Cause |
|---|---|---|---|---|---|---|---|---|
| S3 | AUROPHARMA | rank-fill HOLD 62.83 (signal 12 Nov) | 13 Nov 11:59:20 @ 1,818.91 | **19 Nov 09:21:41 @ 1,779.30 (Stop)** | 15 | stop 1,782.53 (−2 %, legacy) / 15-day +3 % sale 1,873.48 from 28 Nov; trail 1 % from +3 % | **−₹594.15 gross, −₹670.10 net** | (c) config + (e) sector dip + (b) weak signal |
| S4 | CDSL | Kite retest K4 (breakout 12 Nov; resting buy placed 16 Nov 10:27) | 19 Nov 09:15:29 @ 1,402.10 (limit 1,408.10; LTP 1,401.30) | open | 16 | stop 1,331.90 (fill × 0.95), trail 3 % from +5 % (1,472.20), 40 days | +₹334.99 marked at the close | entry with S4's gate **closed** (LRCA-08) |

Held all day (marked at the close, ATIP RCA): PNB S1 +₹1,119.30, INDHOTEL S2 +₹535.95, PHOENIXLTD S8 +₹520.00,
KOTAKBANK S5 +₹350.00, GRASIM S6 +₹151.20, ICICIBANK S7 +₹84.80, DLF S9 +₹66.30. None came near its stop
(PHOENIXLTD's low was −2.2 % from entry vs a 3 % stop).

**AUROPHARMA minute by minute (Dhan 1-minute candles):** open 1,796.90 (prev close 1,796.30); 09:16 low 1,786.70; 09:20
low 1,782.80; **09:21 low 1,781.50 → stop 1,782.53 touched**; monitor poll 09:21:39–40, LTP 1,780.22, sold 09:21:41 at
1,779.30 (−0.18 % vs trigger, 41 s after the first touch); day low 1,778.00 at 09:28; back above 1,786 from 09:30,
1,800 by 11:15, close 1,800.00. Same minutes: Nifty 25,191.80 (09:15 close) → 25,150.55 (09:21 low) → 25,104.80 (09:30,
day low) → 25,514.30 close; Nifty Pharma 28,301 → 28,088.60 (09:21 low) → 28,371.80.
**Did the direction hold / what fired first?** The signal's direction failed over its 4-day life (never +3 %); on the
day the old stop fired first; neither the old target (1,873.48, live only from 28 Nov) nor S3's arm levels (stop
≈1,727.96 / target 2,000.80) were reached.

**CDSL:** opened 1,412.50 (−0.29 % gap), traded 1,400.70 in the first minute, filling the resting 1,408.10 buy at 1,402.10;
day high 1,428.00 (12:47), close 1,424.70. No stop or target reached. The fill is a rule defect, not a loss: the resting
buy was placed on 16 Nov while S4's breadth gate was open and fired today with the gate closed (breadth50 49.9 %);
S3's own auto-pick at 09:22:12 logged "regime gate closed: breadth50 49.9 %".

---

## 4. Historical loss patterns (Part 3)

All 41 closed paper trades, 30 Oct – 19 Nov (session_cycles; `evidence/trades_enriched.csv`, `part3_summary.json`).
Small samples: patterns with n < 15 are hypotheses, not findings.

| Measure | Value |
|---|---|
| Trades / win rate | 41 / 56.1 % |
| Average win / average loss | +₹728 / −₹604 (net of charges) |
| Profit factor | 1.54 |
| Net realised | +₹5,883 |
| Max realised drawdown | −₹2,905 |
| Longest losing streak | 4 |
| Exits | 23 trailing exits, all winners (+₹16,754); 18 stops, all losers (−₹10,871) |
| Losing days (equity) | 30 Oct −₹590, 6 Nov −₹771, 11 Nov −₹903, 13 Nov −₹1,526, 18 Nov −₹961 |

**Patterns (n in brackets):**
- **Stops:** all 18 stop-outs were on the 2 % rule; 11 closed the same day above their sale price, 7 recovered above the
  entry within 3 sessions (`stop_exits_followup.csv`). The stop is inside one day's normal range (section 5).
- **Entry time:** 09:15–09:20 entries won 77.8 % (18, +₹8,754); entries after 09:45 won 33 % (18, −₹3,700) — these are
  re-picks made when a session freed up mid-day. Hypothesis (the 6 Nov 42-session study found the opposite for
  "no entry before 09:45"), so no rule is proposed from it.
- **Gap on the entry day:** stocks opening more than +1 % up won 22 % (9, −₹3,341).
- **Market:** VIX > 17 at entry won 43.8 % (16, −₹1,027); FII net sellers the day before won 25 % (8, −₹2,346); Nifty
  flat (0 to +0.5 %) on the entry day won 22 % (9). Weekday: Mondays 28.6 % (7), Thursdays 22.2 % (9), Wednesdays
  92.9 % (14, mostly 4 Nov's rally) — calendar effects of a 3-week sample, not used.
- **Signal type:** 39 rank-fill HOLD picks (59 % won, PF 1.73); 2 BUY-signal trades (both stopped).
- **Market cap:** small caps 66.7 % (15), mid 57.1 % (7), large 50 % (18). **Sector:** no industry with n ≥ 5.
- **Holding period:** 1–2 days 61.5 % (13), 3–5 days 55.6 % (18), same day 50 % (8).
- **Expiry days:** no closed trade was entered on a Tuesday (weekly expiry) — cannot be measured yet.

**Losses vs data incidents and releases (by date):** 6 Nov −₹771 = the 09:16 database-lock day (BHEL sold at −2.61 %
on a −2 % stop, ₹152 slippage; fixed RCA-13, live 16 Nov). 13 Nov −₹1,526 = four 2 %-stops (TCS, ASHOKLEY, TITAN,
HCLTECH) on a −0.61 % Nifty day, no incident. 18 Nov −₹961 = MTM only (no realised loss), the day after the VPS move and
the morning the PostgreSQL id counters stopped auto-pick 09:15–09:31 (S8's PHOENIXLTD entry followed at 09:31:40). 11 Nov and 30 Oct: plain
stops. **No loss is explained by a data incident.** The releases of 16 Nov (arms, gate) and 18 Nov (session engine) did
not produce a loss: no trade opened under the 16 Nov arms has hit its stop yet.

**Recent vs longer history (Part 3.4):** the sessions have only 15 trading days, so "last 10 vs last 30 sessions" is
measured on ATIP's signals instead (replay of every rank-fill pick since 28 Jul, `portfolio_daily_E0.json`): the old
rules lost −1.35 % a trade Jul–Sep and −1.03 % Oct–Nov, but **+0.49 % a trade from 3 Nov** (55 trades, close to the real
+₹5,883). It is the **regime**, not the strategy, that changed: breadth50 was under 50 % from early September to 9 Nov
(as low as 9.75 % on 23 Oct) and above 50 % on 11–17 and 19 Nov.

---

## 5. Configuration vs the current market (Part 4)

### 5.1 Active configuration (VPS, 19 Nov; secrets removed)

- **Account:** PAPER; universe Nifty 500 (499 scored); risk limits max order ₹1,00,000, 50 orders/day (buys only), 50
  open positions, daily loss ₹10,000, drawdown 10 %; paper capital plan buffer 10 %, session loss limit 6 %.
- **Sessions (S1–S10, all RUNNING AUTO, auto-send ON, ₹45,000 capital each, daily loss limit ₹2,700, PAPER):**

| Session | Entry | Gate | Stop | Exit | Since |
|---|---|---|---|---|---|
| S1 Momentum | rank-fill | breadth50 ≥ 50 | 1.5×ATR14, 3–6 % | trail 2 % from +4 %; +4 % sale after 15 d | 16 Nov |
| S2 Buy zones | Kite retest K2 | breadth50 | 5 % | target 5 %, 20 d | 16 Nov |
| S3 Breakouts | Kite retest K1 | breadth50 | 5 % | target 10 %, 20 d | 16 Nov |
| S4 Swing | Kite retest K4 | breadth50 | 5 % | trail 3 % from +5 %, 40 d | 16 Nov |
| S5 Control | rank-fill | none | 2 % | trail 1 % from +3 %, 15 d | the 30 Oct rules |
| S6 Recovery | rank-fill | breadth50 | 2×ATR, 2–8 % | trail 1×ATR from +2×ATR | 16 Nov |
| S7 Wide-stop | rank-fill | breadth50 | 4 % | trail 2 % from +4 % | 16 Nov |
| S8 Sector | rank-fill, ≤ 2 per industry | breadth50 | 3 % | trail 2 % from +4 % | 16 Nov |
| S9 Bracket | rank-fill | breadth50 | 3 % | target 6 % | 16 Nov |
| S10 Fast gate | Kite retest K1 | breadth20 ≥ 50 | 5 % | target 10 %, 20 d | 16 Nov |

  Sizing: 2.5 % of the session's cash lost at the stop (risk budget), minimum buy ₹10,000; rank-fill buys a LIMIT at
  min(LTP, signal close) × 1.005, skips a pick > 3 % above or > 1.5 % under its signal close, no entry after 14:45.
- **Scoring:** ATIP = VPI 0.20, SPI 0.15, RRI 0.10, MRI 0.10, MSI 0.10, ZPI 0.10, TS 0.10, FS 0.10, INS 0.05, SAFETY 0.20;
  VPI (9 factors) = V 0.18, TS 0.15, RS 0.12, LQ 0.10, VOL 0.10, MR 0.10, FG 0.10, IS 0.08, NS 0.07; Market Health =
  NiftyTrend 0.20, BankNifty 0.15, Breadth/VIX/FII/DII/Global/Sector 0.10 each, A/D 0.05, Asia 0.05.
  BUY: ATIP ≥ 75 & VPI ≥ 76.5 & ZPI ≥ 65 & ACS ≥ 60 & MH ≥ 50 (or 65 / 66.5 / 55 / MH 40), CRI < 60; HOLD ≥ 50.
- **Scheduler (IST):** Bhavcopy + Dhan EOD 16:45, technicals/scoring ~16:48–16:50, FII/DII re-score 18:10, breadth
  21:20 + 07:30 (the 21:20 slot falls in the 21:00 stop window and runs at the 06:00 resume — SCH-7), pre-open gate
  08:45, order monitor every 30 s 09:15–15:30, post-market RCA 16:00.
- **Options:** no options trading in the sessions (CNC equity only).

### 5.2 Gaps with the data

| Parameter | Setting | Market now | Gap |
|---|---|---|---|
| **2 % stop** (S5, and the 3 open legacy legs: INDHOTEL S2, ICICIBANK S7, KOTAKBANK S5) | 2 % | median ATR14 of the 526 scored stocks 3.23 % (P10 2.33 %); **97.7 % of stocks move more than 2 % on an average day**; 31 % of stock-days in the last 30 days dipped > 2 % below their open | **Does not fit.** 7-year Kite study: ATIP's 2 %/3 % exit loses in every regime (−0.42 % a trade) |
| 3 % stop (S8, S9) | 3 % | 59.9 % of stocks have ATR > 3 %; 15.6 % of stock-days dip > 3 % | Tight for half the universe |
| 4–5 % / 1.5×ATR (S1, S2–S4, S7, S10) | 4–6 % | 4.2–21.7 % of stocks have ATR above it | Fits |
| Volatility vs when set | 2 % set 26 Oct | universe ATR 3.0 % in July → 3.4–3.5 % late Oct/early Nov → 3.3 % now; India VIX ~12 (Jul–Sep) → 15–17.5 (Oct–Nov), 16.11 today | The stop was already inside one ATR when it was set |
| breadth20 gate (S10) | breadth20 ≥ 50 | Kite K1 last 12 months: breadth20 gate +0.29 %/trade vs breadth50 gate +1.05 % | Weaker filter |
| No gate on S5 | — | old rules lose −1.16 %/trade over the full window | Kept on purpose as the A/B control |
| Beta vs sizing | sizing ignores beta | open picks' beta 0.43–1.57 (DLF 1.57) | Not shown to matter in this sample; no change |
| SPI / FS | weights 0.15 / 0.10 | NULL for all 499 | A quarter of the ATIP design is unused (renormalised) |
| Quote list | 50 hard-coded symbols | sessions hold 9 names, only some in the 50 | Stops fetch their own quotes; paper pending orders rely on the list (DS-08) |

---

## 6. Code review findings (Part 5)

The full table, with file:line, scenario, P&L impact and fix for every item, is `CODE_REVIEW.md` beside this report.
Summary:

| Severity | Fixed on the branch (tests) | Behind the strategy-v2 switch | Recommended / owner decision |
|---|---|---|---|
| High | LRCA-01 one failing rule stopped every stop check; LRCA-02 exits waited behind auto-pick; LRCA-03 monitor failures unseen; LRCA-04 PostgreSQL lock waits never end; LRCA-13 unknown index change stored as 0 % | LRCA-08 Kite retest: buy refused when LTP ≤ stop; gate re-checked at fire time | LRCA-12 manual STOP also freezes exits (owner's design) |
| Medium | LRCA-05 NaN LTP skipped a stop; LRCA-10 PG `in_transaction`; LRCA-11 locked job skips counted healthy; LRCA-15 `/api/refresh` re-scored yesterday mid-session | LRCA-06 stale breadth closes the gate; LRCA-07 signals older than one session not traded; LRCA-14 no BUY while market health is unknown | DS-03, DS-07, DS-08, OP-05, OP-07, SCH-7, SCH-8, SCH-9 |
| Low | — | LRCA-09 Nifty opening-gap filter (off by default) | DS-01 (measured: no effect), DS-06, OP-08, OP-10, SCH-6, SCH-12, DS-09..15 |

Look-ahead bias: none found (every indicator and score read is bounded `date <= td`; entries use the next session's
prices). Timezones: correct today only because the VPS runs `TZ=Asia/Kolkata`; a start-up assertion is recommended.

---

## 7. Recommended strategy and backtest comparison (Part 6)

### 7.1 Method and limits

- Engine: `research/session_ab_replay.py` (ATIP's own replay: 15-minute bars where captured, else daily; a bar opening
  through the stop fills at the open; stop exits pay 15 bps, other fills 5 bps; NSE delivery charges). **Calibration
  on the 41 real trades:** 41/41 exit reasons match, correlation 0.971, simulated ₹4,717 vs actual ₹5,883
  (conservative).
- Candidates: what rank-fill would have bought every day (BUY, then HOLD by score; ten a day), bought at the next
  session at ≤ close × 1.005, ₹25,000 a trade. Window: signals 28 Jul – 18 Nov 2026 (51 scored days; ATIP has no
  scores before 27 Jul, so no longer history exists for the rank-fill strategy). Portfolio = sessions as slots holding
  one position at a time.
- **Out-of-sample:** split at 1 Oct (A = Jul–Sep, B = Oct–Nov) and a walk-forward (exit chosen on A, applied unchanged
  to B). The gated strategies trade little (the gate was closed most of Sep–early Nov), so their samples are small
  (20–22 portfolio trades) — read the direction, not the decimals. Nothing was tuned on 19 Nov.
- Kite retest arms: `research/kite_retest_study.py` on 7 years of daily bars (2019–2026, 5,207 setups).

### 7.2 Side by side (portfolio walk, ₹25,000 a trade, net of charges)

| | CURRENT-A: rules to 15 Nov (2 % stop, no gate, 10 slots) | CURRENT-B: live arms since 16 Nov (S1, S5–S9) | **PROPOSED: gate + 4 % stop, trail 2 % from +4 % (6 rank-fill slots)** | PROPOSED-ATR: gate + 1.5×ATR 3–6 % (S1 rule) |
|---|---|---|---|---|
| Trades | 330 | 47 | 22 | 20 |
| Win rate | 30.0 % | 42.6 % | **63.6 %** | 65.0 % |
| Avg win / avg loss | ₹938 / −₹814 | ₹862 / −₹940 | ₹971 / −₹1,182 | ₹836 / −₹1,516 |
| Avg net a trade | −1.16 % | −0.67 % | **+0.97 %** | +0.30 % |
| Profit factor | 0.49 | 0.68 | **1.44** | 1.02 |
| Net P&L | −₹95,135 | −₹8,133 | **+₹4,130** | +₹251 |
| Max drawdown | −₹1,07,660 | −₹14,753 | **−₹5,551** | −₹7,306 |
| A (Jul–Sep) avg/trade | −1.35 % (144) | −1.93 % (23) | +0.96 % (12) | −0.22 % (10) |
| B (Oct–Nov, out of sample) | −1.03 % (190) | +0.54 % (24) | +0.97 % (10) | +0.82 % (10) |
| Live window only (signals 3–18 Nov, 15-min bars) | +0.49 % (55, +₹5,645) | — | +1.08 % (10, +₹1,481) | +1.06 % (10, +₹1,379) |

Walk-forward: the best gated exit on A was S6's (2×ATR); applied unchanged to B it made +0.93 % a trade (7 trades,
PF 1.25). Every gated variant is positive out of sample; every ungated one is negative over the full window.
Population view (every candidate, no slot limit, `analyse_bt.py`): gate on/off moves S7's exit from −0.95 % to
+0.74 % a trade (A +0.98 %, B +0.52 %); the VIX filter and "score ≥ 63" did not hold in both halves (rejected); the
Nifty opening-gap filter helped Jul–Sep and hurt Oct–Nov (left OFF).

Kite retest (7 years): K1 (S3/S10 exits) all trades +0.63 %/trade; **with breadth50 ≥ 50: +0.95 % (last 12 months
+1.05 %, PF 1.40); with breadth50 < 50: −0.19 % (last 12 months −1.07 %)**; breadth20 gate last 12 months +0.29 %.
K5 (ATIP's 2 % stop / 3 % target) loses in every regime.

**Honest trade-off:** the gate gives up the start of a rebound (3–10 Nov, when the old rules made most of today's
profit) in exchange for avoiding the long losing stretches (Aug–Oct). Over the full window that is −₹95K vs +₹4K.

### 7.3 Recommended strategy — one page

**Keep (already live since 16 Nov):** breadth50 ≥ 50 % regime gate on every non-control session; stops of at least 3 %
or 1.5×ATR14 (S1, S6, S7: 4 %/ATR), trailing 2 % from +4 %; Kite retest K1/K4 exits; 2.5 % risk budget per trade;
CNC delivery only; PAPER.

**Change (each needs your yes; all are off on Prod):**
1. **Move the three legacy legs to their session's rule** — INDHOTEL (S2: stop 5 %), ICICIBANK (S7: stop 4 %, trail
   2 % from +4 %); leave S5's KOTAKBANK on the control rule. Today's loss is exactly this case.
2. **Kite retest guard (strategy v2 `retest_guard`):** no resting buy when the LTP is already at or under its stop;
   a resting buy fires only while its session's gate is open (CDSL today). *The fire-time gate itself is not
   separately back-tested — the 7-year study gates on the setup day.*
3. **Data-freshness gate (strategy v2 `freshness_gate`):** close the gate when the stored breadth is older than the
   previous session; trade only the previous session's scores; no BUY when market health is unknown.
4. **S10 gate:** breadth20 → breadth50 (+1.05 % vs +0.29 % a trade over the last 12 months).
5. **S5 control:** keep it as the A/B control until the planned review (30 trades per arm or 6 weeks, earliest 3 Jan
   2027) — or retire it now: its rule loses −0.9 to −1.2 % a trade over the full window. Your call.
6. **No-trade conditions:** gate closed; stale breadth or scores; market health unknown; LTP under the planned stop
   (retest); after 14:45 (already). **Max trades:** one open position per session (already); **max loss:** ₹2,700 per
   session per day and ₹10,000 for the account (already).
7. **Not adopted** (no out-of-sample support): VIX filter, minimum score 63, Nifty opening-gap filter (available as
   `nifty_gap_min_pct`, off), entry-time filters, ATR-capped universe.

**Expectation, stated plainly:** with the gate the rank-fill sessions trade only on gate-open days — 10 of the 51
signal days since 27 Jul (27 Jul–3 Aug and 11–17 Nov), none in between — and averaged +0.5 to +1.0 % a trade net in
the samples above. That is a
small edge on small samples; it should be judged after the planned 30 trades per arm, not on any single day.

---

## 8. Changes applied vs awaiting your approval

**Applied on branch `claude/loss-rca-2026-11-19` (worktree `.claude/worktrees/loss-rca-2026-11-19`, on GitHub master
0c0c452). Not committed, not pushed, not on the VPS.**

| ID | Change | Files | Live effect once deployed |
|---|---|---|---|
| LRCA-01 | Per-rule isolation in `check_triggers`; exit legs before entries | orders/rules.py | Only when a rule errors |
| LRCA-02 | Exits checked before auto-pick each pass | dashboard/server.py | A queued pick fires ≤ 30 s later |
| LRCA-03 | `order_monitor` heartbeat + alert after 4 failing passes | dashboard/server.py | New alert |
| LRCA-04 | PostgreSQL `lock_timeout` 60 s = SQLite busy timeout | db/postgres.py | Only on a lock wait |
| LRCA-05 | NaN/None LTP is not a price | orders/rules.py | None normally |
| LRCA-10 | `PgConnection.in_transaction` | db/postgres.py | Pre-open gate no longer commits auto-pick's transaction |
| LRCA-11 | Locked SKIPPED runs are not healthy | pipeline/health.py | New STALLED/FAILING alerts on stuck locks |
| LRCA-13 | Unknown index change stored NULL; NSE close snapshot not blocked by such rows | data/dhan_ws.py, data/bhavcopy.py | Market Health falls back to daily closes when the feed has no previous close |
| LRCA-15 | `/api/refresh` refused 09:00–16:00 unless `force=true` | dashboard/server.py | Manual refresh blocked in market hours |
| strategy v2 | `orders/strategy_v2.py` + hooks (LRCA-06/07/08/09/14) | orders/*, scores/breadth.py, scores/engine.py | **None while `strategy_v2` is absent from config.json (Prod/VPS)**; ON only on the Test copy |

**Awaiting your approval (changes real trading behaviour or the live system):**
1. Deploy the branch to the VPS (via the VPS session, outside 09:15–15:30 / 16:00 / 16:45–16:50 / 19:15 / 19:50 /
   06:00–09:00 IST).
2. Turn on `strategy_v2` on the VPS (`freshness_gate`, `retest_guard`; `nifty_gap_min_pct` stays off).
3. Re-point the legacy legs INDHOTEL (S2) and ICICIBANK (S7) to their arm's stop/trail.
4. S10 gate breadth20 → breadth50; S5 keep or retire.
5. LRCA-12: let a manual STOP hold entries only (exits keep firing) — a change to the kill switch's design.
6. Implement the recommended items DS-03, DS-07, DS-08, OP-05, OP-07, SCH-6/7/8/9.
7. Commit and push the branch.

---

## 9. Testing report (Test + Prod)

Full report: `docs/testing/ATIP_Loss_RCA_Test_Report_2026-11-19.md`. Summary:

- **Test server** = local, port 8891, SQLite copy of the 19 Nov 19:15 IST snapshot (all 288 tables), `strategy_v2`
  **ON**. **Prod server** = local, port 8892, PostgreSQL restore of the same snapshot, the VPS's own config (secrets
  and the sign-in block removed), `strategy_v2` **OFF** (absent). Neither is the VPS; nothing there was touched.
- Regression tests: `tests/test_loss_rca_2026_11_19.py` 31 tests (28 on SQLite + 3 live-PostgreSQL), all pass.
- Full suite 20:20:18–20:31:41 IST: 3,018 tests — 2,904 passed, 34 failed, 80 skipped. All 34 also fail on untouched
  master (c03fc29) run at the same hour (auto-pick's 14:45 IST clock, sandboxed sockets, live-data files, and the
  order-path guard, which passes once the branch is committed): no new failure.
- Pipeline run (technicals 499 + scoring 499 for 19 Nov) on both: SUCCESS; scores identical to the VPS's (0 changes
  > 0.01; 0 signal changes).
- Paper-trade dry run for 20 Nov 09:20 IST on both: S3 would rest a Kite buy of KALYANKJIL at ₹620.00 (37 shares);
  the other nine sessions hold a position or a queued buy; nothing written (169 rules / 97 paper orders before and after).
- Dashboard: `/`, `/sessions`, `/rca`, `/api/rca`, `/ab`, `/command`, `/algo-signals`, `/m`, `/api/v2/shell/state`,
  `/api/command/summary`, `/api/v2/engine` all 200 on both; `/health` 503 on both (no scheduler and no Dhan token on a
  copy — expected).
- Mobile apps: ATIP has Android and iOS apps (thin shells over the server's screens, v2026.11.18.1). **No change in this
  work affects them natively**; the new monitor alert reaches them through the server's alert panel and Telegram. No
  rebuild needed; iOS still cannot be built on this Mac (no Xcode).
