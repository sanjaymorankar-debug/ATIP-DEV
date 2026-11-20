# ATIP session review — today, last week and the month (Fri 20 Nov 2026, IST)

Owner request (~22:50 IST 20 Nov): today's P&L per session, last week's total profit, how many strategies failed and
why, a one-month loss analysis with corrective actions, and the corrections made in the code.

**Data.** The VPS's own 19:15 IST pg_dump of 20 Nov (`atip-2026-11-20_1915.dump`, sha256-checked), copied read-only by
the VPS session at 22:55 IST and restored on a throwaway local PostgreSQL. Nothing on the VPS was written, no order was
placed, modified or cancelled, and the service was not restarted (pid 850092 since 01:46:11 IST). All money is **PAPER**
(`broker_env` PAPER; the Dhan account shows 0 orders, 0 trades and 0 positions today).
P&L uses ATIP's own session code (`orders/sessions.py` `_trades`, `ledger`) and NSE's official closes
(`prices_daily`, 20 Nov loaded). A position held overnight is marked close to close; a trade's charges are booked on the
day it closes. Check: the per-day figures of every closed trade add up to its booked P&L (0 off), and the month total
equals the paper book's equity change in `pnl_daily` (Rs 5,04,865.24 on 23 Oct → Rs 5,16,522.96 on 20 Nov =
+Rs 11,657.72) to the paisa.

---

## 1. Today, 20 Nov 2026

| Session | Arm | Today (official close) | ATIP's 16:00 RCA | What happened |
|---|---|---:|---:|---|
| S1 | Momentum | +₹87.36 | +₹72.80 | holds PNB (bought 18 Nov) |
| S2 | Buy zones (retest K2) | +₹603.45 | +₹591.30 | holds INDHOTEL (bought 13 Nov, **old rules**) |
| S3 | Breakouts (retest K1) | ₹0.00 | ₹0.00 | flat; VIJAYA resting buy at ₹1,457.10 placed 09:15:15 |
| S4 | Swing (retest K4) | +₹105.60 | +₹105.60 | holds CDSL (bought 19 Nov) |
| S5 | Control | +₹296.15 | +₹325.13 | **sold KOTAKBANK 13:48:54 on its trailing stop at ₹510.35: realised +₹1,014.15** (bought 13 Nov); MARUTI limit buy ₹13,656 queued 13:48:55 (LTP ₹13,951, not filled) |
| S6 | Recovery (ATR) | −₹29.60 | −₹48.40 | holds GRASIM (bought 16 Nov) |
| S7 | Wide-stop | +₹497.60 | +₹505.60 | holds ICICIBANK (bought 12 Nov, **old rules**) |
| S8 | Sector | +₹764.80 | +₹761.60 | holds PHOENIXLTD (bought 18 Nov) |
| S9 | Bracket 3/6 | +₹442.00 | +₹494.00 | holds DLF (bought 16 Nov) |
| S10 | Retest fast-gate | +₹34.62 | −₹10.38 | **bought FACT 15:03:38 at ₹755.25** (Kite resting buy) |
| **Total** | | **+₹2,801.98** | **+₹2,797.25** | 8 sessions up, 1 flat (S3), 1 down (S6); 1 sale, 1 buy |

Evidence: `evidence/session_pnl_by_day.csv` (row 2026-11-20); `post_market_rca` row 2026-11-20 built 16:00:07 IST
(`evidence/review_summary.json`); journal 13:48:54–55 and 15:03:38 IST; `paper_order` PAPERE9413DFCD8, PAPERC254B9B2D2.
ATIP's own figure differs by a few rupees because the 16:00 run marks at the last 1-minute candle, this review at NSE's
official close. `pnl_daily` (16:51 IST, live quotes) says +₹2,830.96.

**Today's order-monitor check (the owner's 09:15 request) — PASS.** `ops_heartbeat` order_monitor last beat 15:30:27 IST,
pid 850092, "ok"; no `order_monitor_failing` row or alert; the journal 09:10–15:30 IST has no RULE_ERROR, monitor error,
lock error or Traceback (the only ERROR lines today are the external KSE100 index source, 10:45 and 17:15 IST);
the KOTAKBANK trail moved 53 times from 09:16:47 and filled 13:48:54, and auto-pick ran in the next second (exits
before picks, LRCA-02); no "leftover" line (nothing left over from 19 Nov, LRCA-16). Strategy v2 is still **OFF**: the
morning's switch-on ended when the VPS session's question was interrupted (~09:24 IST); config.json has no
`strategy_v2` key (22:55 IST).

## 2. Last week (16–20 Nov) and the weeks before

| Session | 26–30 Oct | 2–6 Nov (+8 Nov Muhurat) | 9–13 Nov | **16–20 Nov** | Since start | Return on ₹45,000 |
|---|---:|---:|---:|---:|---:|---:|
| S1 | −589.74 | +642.02 | +906.94 | **+1,892.09** | +2,851.31 | +6.34 % |
| S2 | 0 | +11.83 | −86.39 | **+585.90** | +511.34 | +1.14 % |
| S3 | 0 | +1,610.72 | +1,241.56 | **−734.45** | +2,117.83 | +4.71 % |
| S4 | 0 | +593.50 | +350.37 | **+271.07** | +1,214.94 | +2.70 % |
| S5 | 0 | +910.16 | −1,093.27 | **+1,018.65** | +835.54 | +1.86 % |
| S6 | 0 | +1,438.36 | −1,344.12 | **+918.15** | +1,012.39 | +2.25 % |
| S7 | 0 | +320.58 | +384.78 | **+902.40** | +1,607.76 | +3.57 % |
| S8 | 0 | +894.11 | −1,298.96 | **+1,165.52** | +760.67 | +1.69 % |
| S9 | 0 | −130.56 | +238.17 | **+113.92** | +221.53 | +0.49 % |
| S10 | 0 | −141.97 | −441.04 | **+1,107.42** | +524.41 | +1.17 % |
| **Total** | **−589.74** | **+6,148.75** | **−1,141.96** | **+7,240.67** | **+11,657.72** | **+2.59 %** |

₹ including open positions at the close (`evidence/session_pnl_by_week.csv`; since-start = `orders.sessions.ledger`:
equity − capital, capital ₹45,000 after the 16 Nov top-up). Last week's profit: **+₹7,240.67** (realised +₹2,096.67 from
8 closed trades, 5 won and 3 lost; the rest is the rise of open positions). Days: 16 Nov +436.37, 17 Nov +2,306.05, 18 Nov −990.23,
19 Nov +2,686.50, 20 Nov +2,801.98.

## 3. How many strategies failed

| Measure | Failed | Detail |
|---|---|---|
| Sessions, last week (16–20 Nov) | **1 of 10** | S3 −₹734.45 — all from AUROPHARMA, bought 13 Nov under the **old** rules (2 % stop), stopped 19 Nov 09:21:41 at ₹1,779.30 (−₹670.10). S3's own arm (K1 retest) had not traded yet: its gate was closed on 19 Nov (breadth50 49.90 % on 18 Nov). |
| Sessions, week of 9–13 Nov | 5 of 10 | S2, S5, S6, S8, S10 — every session still ran the same old rules then. |
| Sessions, the month | **0 of 10** | All ten are above their capital (+0.49 % to +6.34 %). |
| New arms (since 16 Nov 00:52 IST) | **0 so far** | 6 entries, none closed, all 6 in profit: PNB +₹821.06, DLF +₹825.70, PHOENIXLTD +₹775.20, CDSL +₹419.79, GRASIM +₹271.61, FACT +₹34.62 (+₹3,147.98). Too early to judge: the owner's rule is 30 closed trades and 6 weeks per arm (first weekly A/B report Sun 22 Nov 18:00 IST; review date Sun 3 Jan 2027). |
| Trades, the month | **18 of 42 closed** | Every loss is a −2 % stop under the old rules (−₹10,871.32); every win a trailing stop (24, +₹17,768.54). Net +₹6,897.22, profit factor 1.63, win 57.1 %. |

The old rule set is the one that failed (rank-fill, stop −2 %, trail 1 % from +3 %, no regime gate). It still makes
money over the month because its winners (avg +₹740.36) outweigh its losers (avg −₹603.96), but it produced all 18
losses. It was replaced on 16 Nov by the ten arms (`orders/arms.py` ARMS); S5 keeps it on purpose as the control.

## 4. Why they lost — one month (30 Oct – 20 Nov)

Evidence: `evidence/trades.csv`, `evidence/stops.json`, `evidence/closed_with_gate.csv`; replay `evidence/entry_time_*`.

1. **The stop sat inside a normal day's move (proven).** All 18 stops were −2 %; the stock's average daily range
   (ATR14, day before entry) was larger than 2 % for all 18 (median 3.14 %; winners' median 3.65 %). 8 of the 18 traded
   back above the buy price within 5 sessions — stopped by noise (PVRINOX 5 Nov, LODHA 6 Nov, BHEL 5 Nov, M&M 16 Nov …).
   But 12 of 18 were still lower 10 sessions later (median −3.7 %): for those the stop saved money. So a wider stop
   alone is not the fix; the regime matters (next).
2. **Market regime decides whether any exit works (proven on the replay).** ATIP's replay (`research/session_ab_replay.py`,
   15-minute bars, slippage and charges) of 1,339 rank-fill entries per exit, 29 Sep – 19 Nov, split by the breadth50
   gate known before the buy day:

   | Exit | Gate closed: avg / win / stopped | Gate open: avg / win / stopped |
   |---|---|---|
   | S5 2 % stop, trail 1 % from +3 % (old rule) | −0.59 % / 36.5 % / 60.5 % | +0.16 % / 57.3 % / 29.0 % |
   | S7 4 % stop, trail 2 % from +4 % | −0.67 % / 46.7 % / 49.6 % | **+0.45 %** / 64.5 % / 10.9 % |
   | S1 ATR stop 1.5× (3–6 %) | −1.08 % / 49.6 % / 46.2 % | +0.44 % / 64.5 % / 7.7 % |
   | S9 bracket 3 % / 6 % | −0.37 % / 35.2 % / 61.0 % | +0.30 % / 57.3 % / 23.0 % |

   Every exit loses with the gate closed and makes money with it open; the old 2 % stop is the weakest when it is open.
   The sessions only started trading on 30 Oct, as breadth50 climbed from 18.6 % to 56 % — a recovery, which is why
   the old rules still made money live (34 trades bought with the gate closed: +₹4,800.55).
3. **Sector clustering (proven, live).** IT: 5 trades in 4 sessions within 4 days (TCS ×2, HCLTECH ×2, ZENSARTECH,
   6–13 Nov), **0 wins, −₹2,889.35 = 27 % of all losses**. Capital Goods: 2 of 2 lost (BHEL, ASHOKLEY), −₹1,310.51.
   Only S8 has an industry cap today.
4. **Charges.** ₹2,970.21 on 42 trades (₹70.72 each, about 0.28 % of a ₹25,000 buy) — 27 % of the gross losses
   (₹10,871.32) and 43 % of the closed trades' net (₹6,897.22); each −2 % stop costs about −2.4 % after charges.
5. **Late-day entries — hypothesis, NOT confirmed.** Live, buys at or after 10:00 IST won 4 of 15 (−₹4,311.15) against
   20 of 27 before 10:00 (+₹11,208.37). The replay does not confirm it (S5: −0.39 % before 10:00 vs −0.50 % after; in
   27 Oct – 19 Nov later entries did as well or better; with the gate open the gap is ≤0.15 %). No change made.
6. **Re-buying after a stop — not a cause.** All 4 re-entries after a stop won (PVRINOX ×3 +₹2,212.12, INDHOTEL
   +₹394.75). The re-entry cooldown stays off.
7. **No data error, no order-path fault** in the month's losses: every exit fired at its stop (−2.02 % to −2.61 %; the −2.61 %
   is BHEL on 6 Nov, sold at ₹408.20 under its ₹410.78 stop as it fell in the first minutes), the monitor ran clean today, and the LRCA fixes are live since 00:46–01:46 IST.

## 5. Corrective actions

**Already live (16–20 Nov).** Ten arms (`orders/arms.py`): stops wider than a day's range (S1 ATR 3–6 %, S7 4 %, S8 and
S9 3 %, retest arms 5 %), trails 2 % from +4 %, targets on S2/S3/S9/S10, the breadth50 gate on S1–S4 and S6–S9 (S10
breadth20, S5 none: control), risk-budget sizing (2.5 % of the session's cash at the stop), S8's industry cap of 2.
LRCA-01..20 (exits first, a failing rule no longer stops the others, leftovers cleared first, monitor heartbeat and
alerts). The first new-rule trades are all in profit.

**Owner decisions (switches; the code is there; nothing changed):**

| # | Action | Command / where | Evidence for | Against / risk |
|---|---|---|---|---|
| A1 | Switch on strategy v2 for the paper sessions | config.json `"strategy_v2": {"enabled": true}` via the VPS session | stale breadth/scores cannot open a gate; a resting Kite buy is held while its gate is closed (S4 CDSL filled 19 Nov 09:15 with breadth50 49.90 %) | fewer entries; CDSL is +₹419.79 today |
| A2 | Book-wide industry cap of 2 for all sessions | `python -m orders.entry_guards --set guard_sector_cap 2 --ack` | IT 0/5, −₹2,889 (27 % of losses) | would also have blocked S1's PNB on 18 Nov (now +₹821.06): Financial Services already held 3 |
| A3 | Do **not** re-point INDHOTEL (S2) and ICICIBANK (S7) to their arms' exits (changes the 19 Nov recommendation) | — | both are in profit (+₹688.76, +₹923.76) and both trails are active: ICICIBANK's stop is ₹1,628.40 (buy ₹1,580.39, high ₹1,644.90), INDHOTEL's ₹903.80 (buy ₹885.44, high ₹912.95), so each has locked a profit; the arms' 4–5 % stops would sit far below it | they keep S2 and S7 from trading their arms until they exit (SR-02 now shows this) |
| A4 | Keep S5 (control) and S10 (breadth20) as they are until the A/B review | — | the A/B needs a control and a fast-gate arm | S5 runs the rule that made every loss |
| A5 | Judge arms only after 30 closed trades and 6 weeks | `/ab`, `python -m portfolio.ab_report` | 6 trades so far | — |

## 6. Corrections made in the code (branch `claude/session-review-2026-11-20`; owner 23:15 IST: "commit, push and deploy now")

| ID | File | Change | Why |
|---|---|---|---|
| SR-01 | `portfolio/post_market_rca.py` `_headline` | When a losing trade carries more than one cause, the daily RCA headline now adds how much was lost in all and that the cause totals overlap | 16 Nov read "Signal quality (Rs −1,203), Stop-loss design (Rs −1,203), Position sizing (Rs −1,203)" for ₹1,203 lost in 2 trades: a trade is counted under every cause it shows, which read as ₹3,609 |
| SR-02 | `portfolio/ab_report.py` `arm_stats`, `build`; `dashboard/arms_routes.py` | The weekly A/B report names a position bought before the session's arm ("Held from before the arm"), and the verdict starts "blocked: holds …" | one position per session: S2 (INDHOTEL from 13 Nov) and S7 (ICICIBANK from 12 Nov) made no pick in the arms' first week; without this the first report (22 Nov) would show them as arms that never fire |

Run on the 20 Nov copy, SR-02 flags exactly S2 "INDHOTEL (bought 2026-11-13)" and S7 "ICICIBANK (bought 2026-11-12)";
the other eight show none. Neither change touches orders, prices, stops or sizing. Tests: `tests/test_session_review_2026_11_20.py` (3, pass).
Full suite 23:07–23:21 IST: 2,917 passed, 31 failed, 80 skipped; untouched master 52ad07c run alongside at the same
hour: 2,912 passed, the **same 31** failed, 82 skipped — no new failure (the 31 are auto-pick's 14:45 IST cut-off after
hours, a database lock between two after-hours writers in the same tests, and the sandbox's blocked local sockets).

No strategy rule was changed in code: the losses came from the rule set already replaced on 16 Nov, the replacement has
no closed trade yet, and the two remaining candidates (strategy v2, a book-wide sector cap) already exist as switches
for the owner (A1, A2). Changing live paper trading needs the owner's yes and goes through the VPS session.

## 7. Out of scope but seen

- The LIVE book in `pnl_daily` is the owner's own Dhan holdings (8: IEX, BPCL, ANMOL, IRFC, RTNPOWER, COALINDIA, NTPC,
  NHPC; cost ₹88,497.96, value ₹76,605.08, −₹11,892.88 on 20 Nov). ATIP placed no live order; its older LIVE `pnl_daily`
  rows still carry the PA-01 cash error (repair awaits the owner).
- `market_breadth` for 20 Nov is not in the 19:15 dump (built later in the evening).
- The evening indicator run logs "Indicators that failed this run (ta): adx×68" (×76, ×73, ×70 on 17–19 Nov). Checked:
  68 of 3,392 symbols in `technical_indicators` on 20 Nov have no ADX; 55 of them have no EQ-series daily rows and the
  rest 5–26 sessions of history (ADX-14 needs about 28). Expected, not a data fault.
- The 16:51 IST scoring logged "No actionable BUY/SELL signals for 2026-11-20" (journal 16:51:03 IST), as on 17 and
  18 Nov: rank-fill picks for the next session then come from HOLD stocks (`orders/autopick.py`).
