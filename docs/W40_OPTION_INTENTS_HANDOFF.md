# W40 — Strategies that emit option intents (ENT-15): handoff

**Scope:** ENT-15 "strategies do not emit option intents yet". A new strategy kind, `option_overlay`, emits multi-leg OPTION intents. The W4 risk engine sizes and checks them in PAPER. The OMS routes each approved intent to the paper options book as one all-or-nothing order. Positions are marked daily, exited by the definition's rules and settled at expiry. A dry run shows today's legs and risk check.

**Safety:**
- PAPER only. LIVE is refused at four places:
  - the risk engine BLOCKS a LIVE book or LIVE mode ("LIVE options are not built");
  - `order_manager.create_order` refuses LIVE;
  - `adapters.get_adapter` raises `LiveTradingDisabled` for a LIVE OPT order;
  - there is no LIVE options adapter at all.
- `options.enabled` still ships `false`.
- Naked short calls are refused unless the definition sets `allow_naked_short_calls: true`.
- Option strategies trade only in the owner's book. Tenants have no options book.

## The path (mirrors the W30 futures path)

```
option_overlay evaluator ─ OPTION_OPEN / OPTION_CLOSE decision (legs in features.option_plan)
  → PositionIntent (side BUY to open, SELL to close; NOT_AUTHORIZED)
  → risk_engine.evaluate → execution/option_intents.evaluate_option  (sizes in lots, refuses)
  → order_manager.create_order: ONE order, instrument OPT, adapter paper_opt, legs_json
  → OptionsPaperAdapter → options_paper.fill_strategy_order        (all legs or none)
  → paper_option_strategy_position / _leg / _trade; marks in _mark; settle_expired at expiry
```

## Definition (`kind: "option_overlay"`; full reference in `strategy_engine/option_overlay.py`)

```json
{"strategy_id": "nifty_monthly_condor", "name": "NIFTY monthly condor", "version": "1.0.0",
 "kind": "option_overlay",
 "underlyings": {"source": "symbols", "symbols": ["NIFTY50"]},
 "template": "iron_condor",
 "strikes": {"method": "delta", "near": 0.20, "wing_strikes": 2},
 "expiry": {"rule": "monthly", "min_days": 20},
 "lots": 1,
 "exits": {"profit_take_pct": 50, "stop_loss_pct": 100, "days_before_expiry": 2},
 "option_risk": {"max_loss_pct": 2.0},
 "allow_naked_short_calls": false,
 "position": {"max_positions": 3, "max_new_per_day": 1}}
```

**Underlyings** — the `source` decides which symbols get an overlay:

| Source | Fields | Meaning |
|---|---|---|
| `symbols` | `symbols` | A fixed list of ATIP symbols. `NIFTY50` trades the NIFTY chain. |
| `held` | optional `symbols` filter | Shares held in the paper cash book. |
| `rule` | `entry`, optional `symbols` | A scan: a condition tree over features, e.g. `score_signal == "BUY"`. |
| `strategy` | `strategy_id`, `version` | A signal source: that strategy version's BUY decisions on the day. |

**Templates** come from `quant/options_strategy.py`:
- covered_call
- protective_put
- bull_call_spread / bear_put_spread
- bull_put_spread / bear_call_spread
- iron_condor / iron_butterfly
- long_call / long_put
- short_put
- long and short straddle / strangle
- short_call (naked: needs `allow_naked_short_calls: true`)

Each template's own `build()` gives every leg a role. On each option type, the leg nearest the money is NEAR and the other is FAR. covered_call and protective_put replace the template's future leg with shares held in the paper cash book.

**Strikes** are chosen from the stored chain (`data/derivatives_store.chain_on`). Only traded contracts of the chosen expiry count.
- `delta`: the strike whose |Black-Scholes delta| is closest to the target. The IV is the stored IV, else implied from the price, else 18%.
- `pct_otm`: the strike closest to spot × (1 ± pct/100).
- `wing_strikes`: the far leg sits n listed strikes beyond the near leg.
- Ties go to the further-out-of-the-money strike.

**Expiry:** the first listed expiry at least `min_days` away.
- With `monthly`, only a monthly expiry qualifies: the last listed expiry of its calendar month that also falls in the month's final 8 days.
- With `nearest`, any expiry qualifies.

**Validation is strict.** Each sub-object refuses unknown keys. Every number is range-checked, and `{"param": ...}` defaults are checked too. `far` must be further out than `near`. A far value is refused on a template with no far leg. Position keys other than `max_positions` / `max_new_per_day` are refused.

## Risk engine (`execution/option_intents.py`)

Checks (each recorded):
- instrument (PAPER and owner only);
- `options_book` (enabled);
- `option_definition`;
- `option_position` (one open position per strategy and underlying);
- `option_chain` (no older than `max_chain_age_sessions` = 2);
- `option_expiry` (`no_new_days_to_expiry`);
- `option_legs` (every leg priced by the fill rule and traded);
- `naked_short_call`;
- `option_risk`.

Lots are then capped:
- `covered_shares`;
- `max_loss_per_strategy`: open risk plus the new risk must stay within min(`option_risk.max_loss_pct`% of paper equity, `max_loss_rupees`);
- `option_premium_trade` / `option_premium_total`: the existing `max_premium_*_pct` limits, applied to the debit;
- `option_margin_total`: `max_margin_total_pct` = 30% of cash;
- the W4 limit `max_capital_allocation_pct`.

After that the risk engine applies `max_daily_trades`, `daily_loss_limit_pct`, `portfolio_drawdown_limit_pct` and manual review. `approved_quantity` is the number of structure lots.

**Margin approximation** (`options_paper.structure_metrics`, not SPAN):

| Position | Margin |
|---|---|
| Long options | 0 (premium paid in full) |
| Defined risk (every short leg matched by a long leg of the same type and expiry) | max(0, max loss − debit). A credit spread blocks its max loss. |
| Share-covered short call | 0 (the shares are the collateral) |
| Each naked short leg | `naked_margin_pct` (15)% × spot × quantity |

**Risk amount** (counts toward the strategy's max loss):

| Position | Risk amount |
|---|---|
| Bounded payoff | The max loss at expiry |
| Covered call | 0 (the premium is already in hand) |
| Allowed naked call | The expiry loss after a `naked_stress_move_pct` (15)% rally |

## Paper book (`execution/options_paper.py`)

**Fill rule:** every leg fills at the chain mid moved against the order by `slippage_bps` (50): BUY at mid × 1.005, SELL at mid × 0.995, never below 0.05.
- The mid is the bid/ask mid from a same-day snapshot no older than `max_quote_age_minutes`. Otherwise it is the latest EOD close / settle.
- One unpriced or untraded leg, a stale chain, or too little cash rejects the whole order. Nothing is written.

**Cash:**
- Open: + net premium, − fees, − margin.
- Close: ± the closing value, − fees, + margin.
- `realized_pnl` carries every fee, so after a round trip the cash moves by exactly the realised P&L.
- Fees are the owner book's: brokerage per leg, plus STT on SELL premium.

**Mark:** `mark_strategy_positions` runs post-market from the 16:20 `_w37_options_settle` job. It values every open position at the session's mid, without slippage, writes one row per position per day, and reports the exit rule that would fire.

**Exits:** the strategy's next decision run turns a fired rule into an OPTION_CLOSE intent.
- Profit-take: unrealised P&L ≥ pct% of max profit. When max profit is unlimited, the base is the premium paid.
- Stop: unrealised P&L ≤ −pct% of max loss. When max loss is unlimited, the base is the credit received.
- `days_before_expiry`.
- COVER_GONE: a covered call whose shares were sold is closed.

**Expiry:** `settle_expired` also settles strategy positions. Each leg closes at its intrinsic value from the underlying's close on the expiry day. It does not settle on the day before's close.

**No `oms_fill` rows for OPT orders.** The legs are the fills, recorded in `paper_option_strategy_trade`. One fill row would read as a cash position in the underlying. Reconciliation, algos, protective stops and cash-book P&L exclude OPT orders.

## Pages and API

- `/strategies` performance table: a new "Derivatives (paper)" column shows the FUTURES and OPTIONS books.
- `/strategies` detail of an option overlay: lists its option positions (legs, risk, margin, marks, exit) and has a "Dry run today" button.
- `GET /api/strategies/{id}/options`: positions with legs and marks, and the book summary.
- `GET /api/strategies/{id}/options/dry-run?as_of=`: the decisions, legs, payoff and the full W4 risk check.
  - Evaluated with `store=False` on an in-memory intent (`risk_engine.evaluate(..., intent=...)`). Nothing is stored or placed.
  - When a W4 gate stops the check first (a DRAFT strategy, the kill switch), the option checks also run on their own (`option_checks`).
- `GET /api/execution/options/book`: now also returns `strategy_positions`.

## Backtest / replay

A W2 backtest of an option overlay is refused, with the replay named in the message. W2 simulates cash and futures, not option legs.

`option_overlay.replay(conn, strategy_id, start, end)` replays the overlay on the daily option prices `fo_contract_daily` already stores. It uses the same selection, fill rule, exits and intrinsic settlement. It does not model:
- the W4 caps;
- the shares behind covered calls and protective puts;
- sources other than `symbols`.

Only the nearest 2 expiries are stored per session, so a monthly expiry far out is often missing. Those days are counted in `skipped`.

## Tables

All are in `db/schema_w39b.py`, with the DDL in `options_paper.STRATEGY_TABLES`:
- `paper_option_strategy_position`
- `paper_option_strategy_leg`
- `paper_option_strategy_trade`
- `paper_option_strategy_mark`
- the new column `oms_order.legs_json`

The tables are classified as OWNER in `enterprise/scoping.py` and as financial in `enterprise/privacy.py`.

## Limitations

**Margin and pricing:**
- Margin is an approximation, not SPAN plus exposure.
- Fills are at the EOD close ± slippage unless an intraday snapshot is fresh. Thin strikes' EOD closes can be stale.
- IVs come from the stored chain, else are implied, else 18%.

**Structures:**
- One open position per strategy and underlying, with no rolling or adjustments.
- No calendar or ratio structures.
- All legs share one expiry.

**Exits:**
- Exits are checked once a day at the decision run. Nothing runs intraday.
- With the shipped `execution.auto_execute_paper: false`, nothing is sent automatically. That is the same as cash strategies.
- Expiry settlement is automatic once `options.enabled` is true.

**Other:**
- Combined regime decisions (`selection.py`) see an overlay's OPTION_OPEN as a BUY vote for the underlying.
- Index underlyings have no tradeable bars, so scans and signal-strategy sources cannot select them. Listed and held sources can.
