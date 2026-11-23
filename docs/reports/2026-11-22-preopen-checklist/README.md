# 9:00 AM Pre-Open Checklist: report (22–23 Nov 2026, IST)

Owner prompt 22 Nov 2026, 11:21 PM IST. The Step 1 audit was shared at 23:29 IST; the owner answered "Yes, build
defaults". Branch `claude/preopen-checklist-2026-11-22`, off GitHub master `0f8f539`. **PAPER only**. All times IST.

| Deliverable | Where |
|---|---|
| Comparison table (Step 1) | [01_AUDIT_COMPARISON.md](01_AUDIT_COMPARISON.md) |
| Delta patch | `preopen_checklist_delta_2026-11-23.zip`, next to this folder in `deliverables/` |
| Migration for the new table | [migration/](migration/) (SQLite + PostgreSQL); applied automatically as version **0007** |
| Backtest + accuracy summary | [BACKTEST.md](BACKTEST.md), [backtest/](backtest/) |
| Testing report | [TEST_REPORT.md](TEST_REPORT.md), [screenshots/](screenshots/), [evidence/](evidence/) |
| Updated product tracker | `ATIP_MASTER_TRACKER.xlsx` sheet "Pre-open checklist 2026-11-22"; `docs/ATIP_MASTER_TRACKER.csv` phase 30 |
| Share-market skill (Module A) | [skill/share-market-SKILL.md.patch](skill/share-market-SKILL.md.patch) and the patched [skill/SKILL.md](skill/SKILL.md) |
| Patch notes | `docs/PATCH_NOTES_2026-11-22_preopen_checklist.md` |

## What was built (defaults D1–D5)

- **Four checks at 09:00 and 09:09 IST** on trading days, plus a preliminary read at 08:15 inside the pre-market
  report, which also feeds the 08:30 brief.
  - US cues: Nasdaq + Dow close, NQ + YM futures.
  - India VIX: a modifier with advice only (D2).
  - NSE A/D: pre-open, else prior-day, labelled.
  - GIFT Nifty: NSE source, basis-adjusted move, raw move shown (C2).
- **Output stored** in the new table `preopen_signals`: bias, volatility regime and expected open, plus each check's
  raw value, status, source and IST time.
- **Thresholds** are all in `atip_data/config.json` "preopen_checklist".
- **Screens**:
  - Home: a "Pre-Open Checklist" card, which both mobile apps show because they load `/home`.
  - Reports → Pre-open checklist (`/preopen`): today's runs, the 20 / 60-day track record, the last 30 days.
  - `GET /api/preopen` (`?compact=1` for the apps).
- **Learning loop**: at 15:45 IST each run is scored on the real open (first 09:15–09:30 reading) and the close, and
  the dashboard shows rolling 20 / 60-day hit rates.
- **Notifications**: the 09:00 IST notification on Android shows the live read. iOS gets a generic local reminder
  (D4). A 09:00 Telegram line goes out, plus a 09:09 one only if the read changed.
- **Fixed in passing (F1)**: ATIP's GIFT figures came from Dhan's frozen quote (`market_cue`, the 08:30 brief, the
  Home Market card, `/api/market/preopen`). They now come from the checklist's source.

## What the backtest says (611 sessions, Jun 2024 – 20 Nov 2026)

- **US cues** called the opening gap 72.5 % of the time (base rate 56.7 %) and the close vs the previous close 64.5 %
  (base 50.4 %). Both edges are clear, and they held in 2024, 2025 and 2026.
- The **bias** did nearly as well (69.3 % on the gap, 62.8 % on the close).
- **After the open**, the bias was right only 47 %: the information is in the gap already. The checklist is a
  context read, not an entry signal.
- **Prior-day A/D adds nothing.** VIX above 15 days moved about 1.7 times as much.
- **GIFT cannot be backtested** from ATIP's data; it is forward-tested from the first live run.

Details are in [BACKTEST.md](BACKTEST.md).

## Status

- **Built and tested:**
  - unit tests;
  - the full suite against untouched master at the same hour;
  - a Test server (fresh SQLite, live Yahoo + a labelled synthetic morning);
  - a Prod snapshot (the VPS dump of 20 Nov 19:15 IST on PostgreSQL, three real mornings replayed and scored).

  See [TEST_REPORT.md](TEST_REPORT.md).
- **Deployed:**
  - **86db41e has been LIVE on the VPS since 09:36:13 IST, Mon 23 Nov 2026.** Owner's yes; one restart; migration 0007
    applied; health 200; +4 scheduler jobs (TEST_REPORT §6b).
  - **The DEF-06 fix (ade78e9) has been LIVE since 10:41:14 IST** (second restart, owner's yes; migration 0008 applied).
  - The first live 09:00 run is **Wed 25 Nov 2026** (24 Nov is an NSE holiday).
- **Checked on the VPS before the deploy:**
  - NSE `marketStatus` carries `giftnifty`.
  - The pre-open `key=ALL` gives whole-market counts.
  - The `.in` redirect is Mac-only (§6a).
- **Not verified:**
  - The iOS build: no Xcode on the Mac; it needs the owner's Apple ID.
  - A real phone.
  - The signed-in pages on the VPS.
