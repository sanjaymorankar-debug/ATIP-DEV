"""
ATIP research & backtesting framework (W2).

One module per stage of the flow, so each can be used and checked on its own:

    data.py        historical bars, data/timestamp validation, point-in-time view (BT-15)
    strategy.py    the Strategy interface and Signal type -- no strategy logic here
    strategies.py  reference strategies built on that interface
    costs.py       transaction-cost model (BT-05)
    slippage.py    slippage model (BT-06)
    liquidity.py   liquidity constraint on fills (BT-07)
    engine.py      portfolio-level simulation: sizing, fills, accounting, equity (BT-08)
    metrics.py     performance and drawdown metrics, pure functions (BT-09, BT-11)
    periods.py     research / validation / test separation (BT-03)
    walkforward.py rolling walk-forward validation (BT-02)
    montecarlo.py  trade-shuffle and return-bootstrap analysis (BT-13)
    store.py       run records: config snapshot, trades, equity, metrics (BT-16)
    service.py     create / run / retrieve -- what the CLI and the API call

The older engines (scores/backtest.py, strategy/backtest_aggressive.py,
strategy/backtest_history.py) are left as they are; this framework does not
replace their reports.

    python -m backtest run --strategy dip --start 2025-06-01 --end 2026-09-23
"""
