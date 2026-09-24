# ATIP known defects

Open defects carried forward for a later defect-resolution phase. None of them
was fixed in W3: the W3 brief says W1/W2 defects are fixed only if they stop W3
from compiling, importing or running, and none did. **W3 made no W1/W2
compatibility fixes.**

Last updated: 2026-09-24 (W3 merge).

## 1. Defects reported by ChatGPT (W1/W2 independent testing)

ChatGPT's independent testing of W1 and W2 found defects, but the list itself
has not been given to Claude. They are **not recorded individually here**. Add
each one to the table below (Detected By = ChatGPT, Status = OPEN) when the list
arrives, so the defect-resolution phase works from one register.

| Defect ID | Description | Detected By | Wave | Severity | Current Status | Workaround | Planned Fix Wave |
|---|---|---|---|---|---|---|---|
| CG-TBD | ChatGPT's W1/W2 defect list (not yet provided to Claude) | ChatGPT | W1/W2 | TBD | OPEN | — | Defect-resolution phase |

## 2. Defects and limitations observed during development (not fixed)

These came from runtime logs and code review while W1–W3 were being built.
They are recorded so they are not lost. They are not ChatGPT's findings.

| Defect ID | Description | Detected By | Wave | Severity | Current Status | Workaround | Planned Fix Wave |
|---|---|---|---|---|---|---|---|
| KD-001 | The Anthropic API key in `.env` returns HTTP 401, so AI commentary calls fail | Claude (runtime log) | Baseline | Medium | OPEN | The owner renews the key and puts it in `.env` (Claude does not handle credentials) | Defect-resolution phase |
| KD-002 | The Business Standard, Business Standard Companies and Financial Express RSS feeds return invalid XML; MoneyControl returns 0 items | Claude (runtime log) | W1 | Low | OPEN | The other feeds still deliver news | Defect-resolution phase |
| KD-003 | Stored news rows written before the timezone fix are still in UTC; the repair tool has not been run | Claude | W1 | Low | OPEN | Recent rows are correct | Defect-resolution phase |
| KD-004 | The pre-market run dies around 07:00, so news can stall. This was mitigated (news runs first, plus a catch-up) but the root cause was not found | Claude (runtime log) | Baseline/W1 | Medium | OPEN (mitigated) | News-first ordering + catch-up job | Defect-resolution phase |
| KD-005 | Daily loss limits fail closed when there is no `pnl_daily` history: sizing/limits refuse until P&L rows exist | Claude (code review) | W1 | Low | OPEN (by design, to be confirmed) | Let the daily P&L job populate history | Defect-resolution phase |
| KD-006 | The W2 walk-forward stitched out-of-sample equity curve starts at the first test window's close, not its open | Claude (code review) | W2 | Low | OPEN | Read OOS metrics per window | Defect-resolution phase |

## 3. W3 limitations (known, by design for this wave)

These are scope boundaries, not defects. They are listed so testers do not
report them as W3 failures.

| ID | Limitation | Why | Planned wave |
|---|---|---|---|
| W3-L1 | Position intents are never authorised or executed (`authorization_status = NOT_AUTHORIZED`) | Execution belongs to W4 | W4 |
| W3-L2 | An intent's `quantity` is indicative: the W1 sizer on PAPER equity for the PAPER book; `None` for the LIVE book (the broker is not asked) | Sizing confirmation belongs to the W4 risk engine | W4 |
| W3-L3 | `max_hold_sessions` is not applied to live/paper decisions (holding dates are not known from the books); it is applied in backtests | Needs W4 position tracking | W4 |
| W3-L4 | W2 backtests trade whole positions, so `ADD` / `REDUCE` decisions are not simulated (each run's warnings say so) | W2 engine design; no W2 redesign in W3 | Later |
| W3-L5 | Combined (cross-strategy) decisions are computed on request from stored per-strategy decisions and are not stored | Foundation only | W4 |
| W3-L6 | The table named `strategy_event` in the brief already exists (the aggressive-exit position ledger), so the Strategy Engine's event log is `strategy_engine_event` | Avoids breaking `strategy/` | — |
