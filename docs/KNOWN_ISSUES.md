# ATIP known issues and deferred work (development TODO)

This is the running list of items deliberately deferred during W3/W4 development.

- **Defect detail** (wave, feature, impact) is kept in `docs/KNOWN_DEFECTS.md`. Both files use the same IDs.
- **Testing:** nothing here has been functionally tested. ChatGPT performs the independent testing.
- **What was fixed during W3/W4:** no earlier defect was fixed, because none met the fix-now criteria (blocks startup, blocks W3/W4 structurally, risks data corruption, or could allow unintended live trading).

Last updated: 2026-09-25.

## Deferred defects

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| CG-TBD | ChatGPT's W1/W2 defect list. It has not been provided to Claude and must be added here when received | W1/W2 | TBD | OPEN | Defect-resolution phase |
| KD-001 | The Anthropic API key in `.env` returns 401 | AI commentary / news classification | Medium | OPEN | Defect-resolution phase |
| KD-002 | Business Standard, Business Standard Companies and Financial Express RSS feeds return invalid XML; MoneyControl returns 0 items | data/news.py | Low | OPEN | Defect-resolution phase |
| KD-003 | News rows stored before the timezone fix are still in UTC | news_articles | Low | OPEN | Defect-resolution phase |
| KD-004 | The pre-market run stalls around 07:00. It is mitigated, but the root cause is unknown | pipeline/scheduler.py | Medium | OPEN (mitigated) | Defect-resolution phase |
| KD-005 | Loss limits fail closed without `pnl_daily` history. This applies to the W1 limits **and** the W4 `daily_loss_limit_pct` / `portfolio_drawdown_limit_pct` checks | orders/risk.py, execution/risk_engine.py | Low | OPEN (by design, to confirm) | Defect-resolution phase |
| KD-006 | The W2 stitched OOS equity curve starts at the first test window's close | backtest/ | Low | OPEN | Defect-resolution phase |

## W3/W4 items not built (remaining work)

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| W4-R1 | Live execution: `DhanBrokerAdapter` refuses every call. There is no live order path in W4 | execution/adapters.py | — (by design) | NOT BUILT | Later wave, only with explicit authorisation |
| W4-R2 | Order **modify** is not implemented; cancel is | execution/order_manager.py | Low | NOT BUILT | W4 follow-up |
| W4-R3 | Stop-loss and stop-limit order types are not implemented. The paper broker supports MARKET and LIMIT only; intents carry stop and target prices, but no bracket is placed | execution/ | Medium | NOT BUILT | W4 follow-up (reuse orders/rules.py brackets) |
| W4-R4 | Resting paper LIMIT orders are never filled later, because the paper broker has no matching loop. Such an order stays ACKNOWLEDGED until cancelled | orders/paper.py | Low | OPEN | W4 follow-up |
| W4-R5 | These risk controls are not built: volatility limits (RK-09), liquidity limits (RK-10), correlation risk (RK-11), VaR/ES (RK-12) and emergency exit (RK-16) | execution/risk_engine.py | Medium | NOT BUILT | W4 follow-up / W6 |
| W4-R6 | Broker reconciliation (OMS vs broker book) is not built. The paper broker is the book of record | execution/ | Medium | NOT BUILT | W4 follow-up |
| W4-R7 | Per-strategy attribution comes only from W4 fills. Positions opened outside W4 (order rules, aggressive exit, manual paper orders) belong to no strategy, but they do count in portfolio, sector and position limits | execution/positions.py | Low | OPEN (by design) | — |
| W4-R8 | The sector map is read from the cached Nifty 500 list. When the cache is missing, sector exposure cannot be measured and every BUY is REJECTED (fail closed) | execution/positions.py | Low | OPEN | Defect-resolution phase if it occurs |
| W4-R9 | The post-market cycle runs after the close. With `paper_fill_price` "live", an auto-executed order fills at the Dhan LTP at that time (the day's last price), not the next open | execution/pipeline.py | Low | OPEN (by design) | Scheduling decision for the owner |
| W3-L1..L6 | W3 limitations (see KNOWN_DEFECTS.md section 3). L1 (intents never authorised) is now superseded by the W4 risk engine | strategy_engine/ | — | See KNOWN_DEFECTS.md | — |

## Deferred testing items (for ChatGPT)

All W3 and W4 functionality. The test scenarios are listed in `W3_STRATEGY_ENGINE_HANDOFF.md` section 9 and `W4_RISK_EXECUTION_HANDOFF.md` section 11.
