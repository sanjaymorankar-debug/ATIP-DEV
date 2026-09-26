"""
Investor performance attribution (W15.5, ATIP-PERF-001): the performance-trust
layer. ATIP never reports one ambiguous "return". It reports four, side by side,
each with its own method:

    MODEL        what ATIP's signals would have earned at the signal price under the
                 model exit rule, gross (signal_log; reproducible from the log alone)
    EXECUTABLE   the same signals as a real account could have traded them: next-session
                 open, slippage, NSE costs, and a liquidity cap
    ACTUAL       what the investor's own book earned: ledger quantities, prices, fees and
                 cash flows (average-cost P&L, TWR, XIRR)
    BENCHMARK    NIFTY 50 (or a chosen index) over the same dates, and the same cash flows
                 invested in it instead (PME)

    metrics.py   XIRR, TWR, CAGR, Sharpe, Sortino, drawdown, alpha / beta, trade stats
    data.py      trading calendar, price series on today's share basis, benchmarks
    ledger.py    the immutable transaction ledger (perf_ledger) and its imports
    engine.py    average-cost positions, realized / unrealized P&L, daily valuation,
                 actual returns, cost / position / portfolio attribution
    model.py     model and executable returns from the signal log
    report.py    the assembled, stored, exportable report with its audit trail
"""
