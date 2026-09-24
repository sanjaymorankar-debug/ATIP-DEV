# ATIP W4 Risk Engine & Execution: development handoff

**Independent functional testing: PENDING — ChatGPT.**
**Testing performed by Claude: none.** Only build/integration checks were run (section 10).
**Live trading: DISABLED.** No live order path exists in W4.

- **Built:** 2026-09-25 in `D:\Projects\ATIP-dev`, branch `w4-risk-execution`, on top of W3 (`d563978`).
- **Merged:** into `master` in `D:\Projects\ATIP` (section 12).

## 1. Architecture

```
Market data -> Features -> Regime -> Strategy evaluation            (W3 strategy_engine/)
    -> StrategyDecision        strategy_decision
    -> PositionIntent          strategy_position_intent   created NOT_AUTHORIZED
    -> Risk Engine             execution/risk_engine.py -> risk_decision
         APPROVED / REJECTED / BLOCKED / REVIEW_REQUIRED
         intent -> AUTHORIZED / REJECTED / BLOCKED / REVIEW_REQUIRED
    -> Order Manager           execution/order_manager.py -> oms_order (+ oms_order_event)
    -> Execution Adapter       execution/adapters.py
         PaperBrokerAdapter -> orders/paper.py PaperBroker (existing, unchanged)
         DhanBrokerAdapter  -> refuses every call (W4 placeholder)
    -> Execution / Fill        oms_execution, oms_fill
    -> Position                paper_position (paper broker book) + per-strategy attribution
```

- **One-way dependency:** the strategy engine never imports `execution/`.
- **Only APPROVED decisions become orders:** a REJECTED, BLOCKED or not-yet-reviewed decision cannot become an order, because `create_order` refuses anything that is not APPROVED.
- **Unchanged existing modules:** the paper broker, `orders/broker.py`, `orders/rules.py`, `order_log`, the kill switch and the aggressive-exit module (`strategy/`).

## 2. Implemented components

| Component | File | What it does |
|---|---|---|
| Configuration & safety gates | execution/config.py | `execution` section: mode PAPER, live_trading_enabled false, auto_execute_paper false, require_manual_review false. `w4_risk_limits` holds 14 limits with safe defaults. Overrides live in the `risk_limit` table with history; the live switches cannot be set via the API |
| Data model | execution/models.py | `RiskDecision`, `RiskCheck`, `BrokerResult`, risk statuses, the order state machine (`ORDER_TRANSITIONS`) |
| Risk engine | execution/risk_engine.py | `evaluate(intent_id)`: gates → validity → sizing and caps → pass/fail limits → W1 `pretrade_check` → review. `approve_review()`, `get_decision()` |
| Positions & exposure | execution/positions.py | PAPER book (W1 `portfolio/pnl.py`), sector map, per-strategy positions and P&L from fills, exposure summary |
| Execution adapters | execution/adapters.py | `BrokerAdapter` ABC, `PaperBrokerAdapter`, `DhanBrokerAdapter` (refuses), `get_adapter(mode)` |
| Order manager | execution/order_manager.py | `create_order`, `validate_order`, `submit_order`, `cancel_order`, `refresh_order`, `transition()` (enforced + logged), `order_detail` |
| Execution cycle | execution/pipeline.py | `run_execution_cycle(execute=None)`: pending intents (SELLs first) → risk → optionally PAPER orders |
| Audit trail | execution/audit.py | `trail(intent_id / order_id)`: run → decision (features, regime, reasons, codes) → intent → risk checks → order, events, executions → fills → position |
| CLI | execution/__main__.py | `python -m execution status / limits / evaluate / run [--execute] / order / cancel / audit / exposure` |
| API | dashboard/execution_routes.py | Section 6 |
| UI | dashboard/execution_page.py | `/trading`: status, decisions, intents, limits, risk decisions, exposure, orders, fills, positions |
| Scheduling | pipeline/scheduler.py | Post-market `execution_cycle` job after `strategy_decisions`. With the defaults it evaluates risk and **places nothing** |

## 3. Risk flow

Check order (every check is stored with PASS / WARN / FAIL / SKIP, value and limit):

| Stage | Check | Result on failure |
|---|---|---|
| Gate | Kill switch (W1 `halted()`) | BLOCKED |
| Gate | Strategy enabled: status PAPER / READY / ACTIVE | BLOCKED |
| Gate | Intent is for the strategy's current version | REJECTED (superseded) |
| Gate | Live gate: a LIVE-book intent, or mode LIVE, needs mode LIVE + `live_trading_enabled` | BLOCKED |
| Gate | Book is in `execute_books` | BLOCKED |
| Validity | Side, symbol, quantity | REJECTED |
| Validity | Fresh: `max_intent_age_days` | REJECTED |
| Validity | Market data: the reference price (the decision close, else the latest close) | REJECTED |
| SELL / EXIT / REDUCE | A paper position is held. EXIT sells everything; REDUCE sells the intent quantity, or half | REJECTED if nothing is held. Never capped by exposure limits (reducing risk) |
| BUY sizing | Intent quantity, else W1 `size_position(per_trade_loss_pct, stop, equity)`; a missing stop defaults to `default_stop_pct` | REJECTED when 0 |
| BUY caps (quantity reduced; the binding cap is a WARN) | `max_order_quantity`, `max_order_value_pct`, `max_position_pct`, `max_portfolio_exposure_pct`, `max_sector_exposure_pct`, `max_strategy_exposure_pct`, `max_capital_allocation_pct` (cash), `per_trade_loss_pct` | REJECTED if nothing fits |
| BUY pass/fail | `max_open_positions`, `max_daily_trades`, `daily_loss_limit_pct`, `portfolio_drawdown_limit_pct`, `strategy_drawdown_limit_pct` | REJECTED. A limit that can't be measured fails (fail closed) |
| BUY W1 | `orders/risk.py pretrade_check` (config.json `risk_limits`) | REJECTED |
| Review | `require_manual_review`, or anything LIVE | REVIEW_REQUIRED. An owner approves via `POST /api/risk/decisions/{id}/approve` |

- **One verdict per intent:** each intent is evaluated once; a second evaluation raises DuplicateDecisionError.
- **Audit protection:** a W3 decision re-run no longer replaces intents that the risk engine has already acted on.

## 4. Execution flow & order state machine

```
CREATED -> VALIDATED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
CREATED -> REJECTED | CANCELLED | FAILED       VALIDATED -> CANCELLED | FAILED
SUBMITTED -> REJECTED | FAILED | FILLED | PARTIALLY_FILLED
ACKNOWLEDGED / PARTIALLY_FILLED -> CANCEL_PENDING -> CANCELLED (or FILLED / PARTIALLY_FILLED)
Terminal: FILLED, CANCELLED, REJECTED, FAILED
```

- **Enforced transitions:** `check_transition()` raises `OrderStateError` for anything else. Every change is an `oms_order_event` row.
- **Validation** repeats the kill-switch and live-gate checks immediately before submission.
- **Paper submission:**
  - `PaperBroker.place_order(symbol, tag=order_id)` fills at the live Dhan LTP, or at the decision close when `paper_fill_price` is "reference".
  - The call and response are recorded in `oms_execution`, fills in `oms_fill` (with strategy and price source), and a PLACED row goes to `order_log` so the W1 `max_orders_per_day` limit counts it.
  - The paper broker updates `paper_position` / `paper_account` exactly as before.
- **Failure handling:**
  - A broker exception or LiveTradingDisabled → FAILED, with the error recorded and logged.
  - A broker rejection → REJECTED with the broker's message.
  - Nothing is retried silently.
- **Duplicates:** there is at most one order per intent and per risk decision (UNIQUE constraints + `DuplicateOrderError`).

## 5. Database changes

All additive, applied by `get_connection()`. No existing table was altered except the two W3 tables below, which gained columns only.

| Table | Change | Purpose |
|---|---|---|
| risk_limit | new | Risk-limit overrides (key, value, note, who, when) |
| risk_limit_history | new | Every limit change (old → new) |
| risk_decision | new | One per evaluated intent: statuses, quantities, reference price, equity, every check, a limits snapshot, engine version, reviewer |
| oms_order | new; UNIQUE intent_id, UNIQUE risk_decision_id | Orders with state, fills and reason |
| oms_order_event | new | Every order state transition |
| oms_execution | new | Every adapter call (submit / cancel / status): request, response, status, error |
| oms_fill | new | Fills with strategy, version, price, fees, price source, mode |
| strategy_feature | new (W3) | Features each strategy version needs |
| strategy_decision | + reason_codes_json, signal_source (W3) | Explainability |
| strategy_position_intent | + entry_reference, book, risk_decision_id, authorized_at | W4 hand-off fields |

The brief's candidate tables `orders`, `executions` and `fills` are named `oms_*`: `order_rules`, `order_log` and `paper_order` already exist and are kept.

## 6. APIs

The existing `/api/orders*` routes (the W1-era order rules) are unchanged.

| Method | Path | Purpose |
|---|---|---|
| GET | /api/strategy-decisions | Decisions across strategies (`as_of`, `strategy_id`, `decision`, `include_wait`) |
| GET | /api/position-intents | Intents with authorization status |
| GET | /api/risk/limits | Effective limits with source + change history |
| PUT (token) | /api/risk/limits | `{key: number / null / "default", "_note"?}` |
| GET | /api/risk/decisions, /api/risk/decisions/{id} | Risk decisions with checks |
| POST (token) | /api/risk/decisions/{id}/approve | REVIEW_REQUIRED → APPROVED |
| POST (token) | /api/risk/evaluate | Evaluate pending intents (risk only) |
| GET | /api/risk/exposure | Equity, positions, sector and strategy exposure, limits |
| GET | /api/oms/orders, /api/oms/orders/{id} | Orders; the detail includes events, executions and fills |
| POST (token) | /api/oms/orders | `{risk_decision_id, submit?}`: create (and submit) a PAPER order |
| POST (token) | /api/oms/orders/{id}/submit, /cancel, /refresh | Order actions |
| GET | /api/oms/executions, /api/oms/fills, /api/oms/positions | Executions, fills, positions (book + per strategy) |
| GET | /api/execution/status | Mode, live gate, kill switch, pending intents, state machine |
| POST (token) | /api/execution/run | `{execute?, as_of?}`: one cycle |
| GET | /api/audit/intent/{id}, /api/audit/order/{id} | Full audit trail |
| GET | /trading | UI page |

## 7. Configuration

`atip_data/config.json` (documented in `config_template.json`, section 11). Absent keys take these defaults:

```json
"execution": {"mode": "PAPER", "live_trading_enabled": false, "auto_execute_paper": false,
              "require_manual_review": false, "execute_books": ["PAPER"], "order_type": "MARKET",
              "product_type": "CNC", "max_intent_age_days": 5, "paper_fill_price": "live"},
"w4_risk_limits": {"max_position_pct": 10, "max_portfolio_exposure_pct": 100, "max_sector_exposure_pct": 30,
                   "max_strategy_exposure_pct": 25, "max_capital_allocation_pct": 95, "max_open_positions": 20,
                   "max_order_quantity": 5000, "max_order_value_pct": 10, "max_daily_trades": 20,
                   "per_trade_loss_pct": 1, "default_stop_pct": 5, "daily_loss_limit_pct": 3,
                   "portfolio_drawdown_limit_pct": 15, "strategy_drawdown_limit_pct": 10}
```

- **Precedence:** a DB override (`PUT /api/risk/limits`) beats config.json, which beats the code default. `null` disables a limit.
- **Live gate:** it opens only for a real JSON `true`. Strings and numbers are ignored.
- **Live config was not changed:** the owner's `atip_data/config.json` was not modified, so the defaults apply.

## 8. Strategy lifecycle and trading

| Lifecycle state | Intents | Risk engine result |
|---|---|---|
| DRAFT … APPROVED, PAUSED, DISABLED, RETIRED, ARCHIVED | Not generated by the scheduler | BLOCKED if one exists (e.g. a manual decision run) |
| PAPER, READY | Against the PAPER book | Evaluated; APPROVED intents may become PAPER orders |
| ACTIVE | Against the LIVE book | BLOCKED: live trading disabled |

All 9 library strategies are in DRAFT, so nothing trades until the owner moves one to PAPER.

## 9. Remaining work

See `docs/KNOWN_ISSUES.md` (W4-R1..R9):
- live adapter
- order modify
- stop and stop-limit order types / brackets
- resting paper LIMIT matching
- volatility, liquidity, correlation and VaR limits
- emergency exit
- broker reconciliation

## 10. Build/integration checks performed (not testing)

| Check | Result |
|---|---|
| `py_compile` of `execution/`, the dashboard W4 files, `strategy_engine/`, `db/schema.py`, `pipeline/scheduler.py`, `dashboard/server.py` | 0 errors |
| Import of every `execution` module and the dashboard W4 modules | 0 errors |
| pyflakes on `execution/`, the dashboard W4 files and `strategy_engine/` | No undefined names; one unused import in `strategy_engine/definition.py` (pre-existing) |
| `get_connection()` migrations on a **copy** of the live DB | All 8 new tables created; new columns added; existing rows kept (paper_order 7, paper_position 2, order_rules 10, order_log 15, strategy_position 1, strategy_event 3) |
| Library sync on the copy | 9 unchanged; 35 strategy_feature rows back-filled |
| App startup (`dashboard.server` import) and route registration | 23 W4 routes; no duplicate method+path; the existing `/api/orders*` routes are still registered |
| Settings with no config sections | mode PAPER, live gate closed ("execution.mode is PAPER") |

No functional, API, UI, risk, execution or backtest testing was performed.

## 11. Deferred testing items (for ChatGPT)

| ID | Area | Scenario | Expected |
|---|---|---|---|
| W4-T01 | Gate | Kill switch on (flag file), evaluate a BUY intent | BLOCKED |
| W4-T02 | Gate | Intent of a PAUSED strategy | BLOCKED |
| W4-T03 | Gate | Intent from an ACTIVE strategy (LIVE book) | BLOCKED, live trading disabled |
| W4-T04 | Validity | Intent older than max_intent_age_days | REJECTED, stale |
| W4-T05 | Sizing | BUY with no quantity | Sized by size_position; ≤ caps |
| W4-T06 | Caps | max_order_quantity 10, request 50 | APPROVED 10, WARN on that check |
| W4-T07 | Caps | Sector already at its limit | REJECTED, max_sector_exposure_pct |
| W4-T08 | Limits | max_open_positions reached, new symbol | REJECTED |
| W4-T09 | Limits | daily_loss_limit_pct set, no pnl_daily history | REJECTED (fail closed) |
| W4-T10 | W1 | W1 risk_limits.max_order_value below the order value | REJECTED, W1 limit |
| W4-T11 | SELL | EXIT with a paper position held | APPROVED, quantity = held |
| W4-T12 | SELL | EXIT with nothing held | REJECTED |
| W4-T13 | Review | require_manual_review true | REVIEW_REQUIRED; approve → APPROVED |
| W4-T14 | Duplicate | Evaluate the same intent twice | Second raises DuplicateDecisionError |
| W4-T15 | Order | create_order on a REJECTED decision | Refused |
| W4-T16 | Order | Second create_order for the same intent | DuplicateOrderError |
| W4-T17 | State | cancel a FILLED order | OrderStateError / terminal |
| W4-T18 | Paper | Submit an approved BUY (Dhan quote available) | FILLED; oms_fill, paper_position and order_log rows |
| W4-T19 | Paper | No live quote | REJECTED with the broker message; no fill |
| W4-T20 | Live | mode LIVE + live_trading_enabled true, submit | FAILED: LiveTradingDisabled (adapter refuses) |
| W4-T21 | Cycle | Defaults, post-market | Risk decisions only; no orders |
| W4-T22 | Cycle | `/api/execution/run {"execute": true}` | APPROVED → PAPER orders |
| W4-T23 | Audit | /api/audit/intent/{id} after a fill | Full chain run → position |
| W4-T24 | Limits API | PUT an unknown key / negative / pct > 100 | 400; nothing stored |
| W4-T25 | W3 guard | Re-run decisions for a date with evaluated intents | Refused; intents kept |
| W4-T26 | Regression | Order rules, aggressive exit, paper CLI, backtests | Unchanged |

## 12. Git

Recorded in the final report and in `git log`:
- **Branch:** `w4-risk-execution`
- **Commits:** "feat: complete wave 3 strategy engine", "feat: implement wave 4 risk and execution", "docs: update wave 3 and wave 4 handoff"
- **Merge:** fast-forward into `master`

**Backups before the merge:**
- `atip_data/atip.db.bak-before-w4-<timestamp>`
- branch `backup/pre-w4-master`
