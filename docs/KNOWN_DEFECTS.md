# ATIP known defects

This is the register of open defects carried forward to a later
defect-resolution phase. W3 fixed none of them:
- The W3 brief allows a W1/W2 defect to be fixed only if it stops W3 from compiling, starting or operating. None did.
- **W3 made no W1/W2 compatibility fixes.**
- Defects are never removed or closed silently. A defect is closed only with the fix commit recorded against it.

Last updated: 2026-09-25 (W3).

## 1. Defects reported by ChatGPT (W1/W2 independent testing)

ChatGPT's independent testing of W1 and W2 found defects. Claude has not been given that list, so the defects are **not recorded individually here**.

When the list arrives, add each defect as a row below:
- Detected By = ChatGPT
- Status = OPEN

This keeps every defect for the defect-resolution phase in one register.

| Defect ID | Wave | Feature ID | Description | Severity | Detected By | Status | Impact | Workaround | Planned Fix Wave |
|---|---|---|---|---|---|---|---|---|---|
| CG-TBD | W1/W2 | TBD | ChatGPT's W1/W2 defect list (not yet provided to Claude) | TBD | ChatGPT | OPEN | TBD | — | Defect-resolution phase |

## 2. Defects observed during development (not fixed)

These came from runtime logs and code review while W1–W3 were being built. They are not ChatGPT's findings; they are recorded here so they are not lost.

| Defect ID | Wave | Feature ID | Description | Severity | Detected By | Status | Impact | Workaround | Planned Fix Wave |
|---|---|---|---|---|---|---|---|---|---|
| KD-001 | Baseline | NS-04 | The Anthropic API key in `.env` returns HTTP 401 | Medium | Claude (runtime log) | OPEN | AI commentary and news classification fall back to rule-based output | The owner renews the key in `.env`; Claude does not handle credentials | Defect-resolution phase |
| KD-002 | W1 | NS-01 | The Business Standard, Business Standard Companies and Financial Express RSS feeds return invalid XML; MoneyControl returns 0 items | Low | Claude (runtime log) | OPEN | Less news coverage | The other feeds still deliver news | Defect-resolution phase |
| KD-003 | W1 | NS-01 | News rows stored before the timezone fix are still in UTC; the repair tool has not been run | Low | Claude | OPEN | Older headlines show times off by 5h30 | Recent rows are correct | Defect-resolution phase |
| KD-004 | Baseline/W1 | MON-01 | The pre-market run dies around 07:00. News-first ordering and a catch-up job mitigate this, but the root cause is unknown | Medium | Claude (runtime log) | OPEN (mitigated) | Pre-market steps after the stall can be late | The news-first ordering and catch-up job | Defect-resolution phase |
| KD-005 | W1 | RK-07 / RK-08 | The daily loss and drawdown limits fail closed when there is no `pnl_daily` history | Low | Claude (code review) | OPEN (by design, to confirm) | BUY orders are refused until P&L history exists | Let the daily P&L job populate the history | Defect-resolution phase |
| KD-006 | W2 | BT-06 | The stitched walk-forward out-of-sample equity curve starts at the first test window's close, not its open | Low | Claude (code review) | OPEN | The first OOS session's return is missing from the stitched curve | Read the OOS metrics per window | Defect-resolution phase |

## 3. W3 limitations (known, by design for this wave)

These are scope boundaries, not defects. They are listed so that testers do not report them as W3 failures.

| ID | Feature ID | Limitation | Why | Planned wave |
|---|---|---|---|---|
| W3-L1 | — | In W3, position intents were never authorised or executed (`authorization_status = NOT_AUTHORIZED`). **Superseded by W4:** the risk engine now sets AUTHORIZED / REJECTED / BLOCKED / REVIEW_REQUIRED, and only AUTHORIZED intents can become PAPER orders | Execution belonged to W4 | W4 (done) |
| W3-L2 | — | An intent's `quantity` is indicative only:<br>• PAPER book: the W1 sizer applied to PAPER equity.<br>• LIVE book: `None`, because the broker is not asked. | Confirming sizing belongs to the W4 risk engine | W4 |
| W3-L3 | SE-09 | `max_hold_sessions` is applied in backtests but not to live or paper decisions | The books do not record holding dates | W4 |
| W3-L4 | SE-01 | ~~W2 backtests trade whole positions, so `ADD` / `REDUCE` decisions are not simulated~~ **Resolved W39 (PF-06):** the W2 engine simulates ADD / REDUCE (partial rows, averaged entries); only SHORT / COVER and changes to names not held are reported as not simulated. W39b: the event-driven engine (BT-17) trades them too, through its own order model (latency, participation cap, TWAP slices, impact, TTL); its BUY / SELL-only runs are byte-identical. **W40 (QR-05 / QR-06):** the W2 engine also simulates SHORT / COVER, as near-month stock futures with margin, daily mark-to-market, F&O costs and a roll (`backtest/futures.py`); the event-driven engine still reports them as not simulated | — | Done (W39) |
| W3-L5 | SE-07 | Combined (cross-strategy) decisions are computed on request and not stored | This wave builds the foundation only | W4 |
| W3-L6 | — | The brief's `strategy_event` table already exists as the aggressive-exit ledger, so the engine's event log is named `strategy_engine_event` | Avoids breaking `strategy/` | — |
