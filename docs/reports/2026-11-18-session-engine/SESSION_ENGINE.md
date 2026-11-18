# ATIP session engine: independent ₹25,000 sessions (18 Nov 2026)

Owner prompt: 18 Nov 2026, 03:05 PM IST. Built 15:07–16:06 IST on branch `claude/session-engine-2026-11-18`
(worktree `.claude/worktrees/session-engine-2026-11-18`, from master `be077b2`). **PAPER only. Committed on the branch
at 18:19 IST (not merged to master, not pushed). Nothing is deployed to the VPS.**

## What already existed, and what this adds

ATIP has had independent cash-pool sessions since 29 Oct 2026 (`orders/sessions.py`, S1–S10). They already keep their
own money, compound it, net each leg's charges and stop after 500 trades. This change is a delta on top of that, in
one new module, `orders/session_engine.py`, plus small hooks:

| # | Spec item | Where it is now | New in this change |
|---|---|---|---|
| 1 | Split funds into ₹25K sessions; the remainder is the reserve | `session_engine.plan()` / `create()` | New. PAPER: the money no session owns, less the capital plan's 10 % cash buffer (the owner's 90 % rule), ÷ ₹25,000, rounded down. Each new session gets an ID (S11, S12, …), an IST start time and a ₹25,000 seed. |
| 2 | Isolated wallet; compounding; charges on every leg | `sessions.ledger()` (since 29 Oct) | `session_engine.stats()` shows seed, cash available, invested value, realised and unrealised P&L, and current balance. |
| 3 | No cap inside a session; funds checked against the session | `sessions.gate()`; `orders/broker.py` | Pool sessions are no longer held by the daily `max_trades` count. A pool's BUY is checked against that session's own free cash at the broker (`check_session_funds`), as well as the account's. |
| 4 | Legs, completed trades, wins, losses, win rate, average P&L | `stats()` | New: 1 BUY = 1 leg, 1 SELL = 1 leg. A completed trade is a BUY plus its SELL; an open position is not counted. |
| 5 | Auto-stop at 500; floor pause | `gate()` already held entries after `max_occurrences` (500) | New `check()`: once 500 trades are done and nothing is open, the session becomes COMPLETED, auto-pick is turned off and the final ledger is frozen. A session whose balance falls under 80 % of its capital is PAUSED with reason BELOW_FLOOR and an alert. It is never refilled from another session. |
| 6 | States CREATED → ACTIVE → (PAUSED) → COMPLETED, logged in IST | `trading_sessions.status` | New stored statuses CREATED and COMPLETED. RUNNING is shown as ACTIVE. Every change is a `session_events` row (event `STATE`) with its IST time and reason. |
| 7 | Data model | `trading_sessions` | New tables `session_trades` and `session_ledger`, new snapshot columns on `trading_sessions`, and `order_log.session_id` on every row. |
| 8 | Sessions tab | `/sessions` (since 2 Nov) | New: summary row (sessions, combined balance, combined P&L, reserve); each card shows its state, balance against seed, % growth, a meter of trades out of 500, and win rate. New drill-down `/sessions/<id>?tab=trades` with the trade list, ledger history and CSV export. |
| 9 | Safety | Unchanged | Creating sessions is PAPER only. LIVE orders stay confirmation-gated, and automatic LIVE sending still needs the SEBI Algo ID. The engine runs from the dashboard's order monitor, not `pipeline/scheduler.py`. |
| 10 | Tests | — | `tests/test_session_engine_2026_11_18.py`: 18 tests, including the 10 × 500 simulation. |

### How the owner's names map onto ATIP's

* **sessions** is the existing `trading_sessions` table (one row per session). The spec's columns are there:
  * `id` is the session ID;
  * `start_capital` is the seed;
  * the new snapshot columns are `current_balance`, `cash_available`, `completed_trades`, `legs`, `created_at_ist` and `completed_at_ist`.
* **ACTIVE** is stored as `RUNNING`, because the whole order engine (auto-pick, the gate, the kill switch) reads that word.
* **seed** is the ₹25,000 a session starts with. Money moved in later (the 16 Nov top-ups) is a TRANSFER line. Growth and the floor are measured on the session's **capital** (seed + transfers), as the 16 Nov capital review defined it. For S1–S10 today, that is 80 % of ₹45,000 = ₹36,000.
* **session_ledger** is each session's own cash book. Its line types are SEED, TRANSFER, BUY, CHARGES (one line per leg), SELL, and PNL. PNL is a memo line with the net result of a completed trade; it does not move the balance. The last `balance_after` equals the engine's cash; this is checked on every sync.
* **order_log.session_id** comes from the order's `ATIP-S<n>` tag. An order outside any session is `MANUAL`.
  * Older rows were backfilled from their paper order's tag.
  * Rows that no paper order identifies are `UNTAGGED`. These are refused or failed sends, which never reached the paper broker.

## Decisions taken (defaults; the owner can change them)

1. **The split keeps the 10 % cash buffer.** This morning the owner asked for 90 % of the money to be in use, and the 16 Nov capital plan keeps 10 % back.
   * With `session_engine.respect_buffer: false` in config.json, the split uses all the money no session owns.
2. **The floor is 80 % of capital** (`session_engine.floor_pct`, default 80). It can be set per session in `trading_sessions.balance_floor_pct`.
3. **The 500-trade cap stays per session** (`max_occurrences`, default 500). New sessions get `session_engine.max_completed_trades` (500).
4. **No auto-refill and no auto-resume.** A paused session stays paused until the owner starts it again (`activate`). Starting it is refused while its balance is still under the floor.
5. **New sessions start CREATED**, so they trade nothing until started. They copy S5's rules (the control arm), as "New paper session…" already does.

## What it would do on the live VPS today

Prod rehearsal: the last live data (Mac, 17 Nov 11:58 IST), loaded into PostgreSQL.

* **Nothing changes on deploy.** S1–S10 stay ACTIVE: each balance is above its ₹36,000 floor, and no session is near 500 trades.
* **No new session fits.**
  * The reserve is ₹54,865.24, but only ₹3,825.42 of it is above the ₹51,039.82 buffer.
  * The VPS at 09:16 IST today showed the same picture: ₹3,687 headroom.
* **More ₹25K sessions need a rebase.** Returning the 16 Nov top-ups (₹20,000 from each of S1–S10) would fit 8 new ₹25K sessions.
  * That reverses the owner's 16 Nov choice, so it is a tool (`python -m orders.session_engine rebase --ack`), not a default.
  * It moves money only from sessions with no open position. In the snapshot, 3 of 10 were idle.

## Operating it

```bash
python -m orders.session_engine plan                # what a split would make now (writes nothing)
python -m orders.session_engine create --n 2 --ack  # 2 new ₹25K sessions, CREATED (add --run to start them ACTIVE)
python -m orders.session_engine rebase              # dry run: what returning the top-ups would free
python -m orders.session_engine status              # every session's wallet, counts and state
```

Dashboard: Sessions › "Split reserve into ₹25K sessions…" (only shown when one fits), the cards, and each session's
"Trades & ledger" tab.

API: `/api/v2/engine`, `/api/v2/engine/plan`, `POST /api/v2/engine/create {n, ack, run}`,
`POST /api/v2/engine/<id>/activate {ack}`, and `/api/v2/engine/<id>/{trades,ledger}.csv`.

## SEBI

The 2025 retail algo framework requires an exchange-issued Algo ID before automated LIVE API orders. ATIP already
guards this in three ways:
* the auto-send switch can only be turned on in PAPER;
* outside PAPER, every entry asks for confirmation;
* a session cannot be set to AUTO under LIVE unless `sebi_algo_id` is configured.

This change keeps all three. Creating sessions is PAPER only, and nothing here sends a LIVE order automatically.
