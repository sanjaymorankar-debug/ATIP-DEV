"""
Plain-English names for what the UI shows by its internal name (UI fixes v2, H6 / G3 / G4, 3 Nov 2026).

    job_label(name)      "broker_health" -> "Broker connection"; an unknown job reads as words ("Saved screens")
    GLOSSARY             the hover definitions of ATIP's own terms (confluence, market gate, 75m, ...): the pages
                         and atip.js's term tooltips read the same text
The raw name stays available in the element's title, so nothing is lost for the owner who knows the jobs.
"""

from __future__ import annotations

JOB_NAMES = {
    "accuracy_audit": "Signal accuracy audit", "accuracy_update": "Signal accuracy update",
    "ai_scoring_engine": "ATIP scores", "audit_export": "Audit export", "benchmark_history": "Benchmark history",
    "bhavcopy_eod": "NSE closing prices", "bhavcopy_gap_fill": "NSE closing prices (catch-up)",
    "broker_health": "Broker connection", "bulk_block_deals": "Bulk and block deals", "close_indexes": "Index closes",
    "compliance_checks": "Compliance checks", "corporate_actions": "Corporate actions", "daily_pnl": "Daily P&L",
    "dashboard_rebuild": "Dashboard rebuild", "dashboard_refresh": "Dashboard refresh", "data_quality": "Data quality checks",
    "db_purge": "Database clean-up", "deal_signal_study": "Deal signal study", "delivery": "Delivery volumes",
    "delivery_gap_fill": "Delivery volumes (catch-up)", "dhan_15min_bars": "15-minute bars",
    "dhan_historical_eod": "Dhan daily history", "dhan_portfolio": "Dhan holdings", "dhan_security_list": "Dhan security list",
    "dhan_security_refresh": "Dhan security list refresh", "earnings_surprise": "Earnings surprises",
    "enterprise_jobs": "Workspace jobs", "event_studies": "Event studies", "execution_algos": "Execution algorithms",
    "execution_cycle": "Strategy execution cycle", "execution_reconciliation": "Order reconciliation",
    "fii_dii_watch": "FII / DII flows", "fo_bhavcopy": "F&O closing prices", "fundamentals_weekly": "Fundamentals (weekly)",
    "futures_expiry_settlement": "Futures expiry settlement", "gap_evaluation": "Opening-gap check",
    "gift_preopen": "GIFT Nifty pre-open", "global_catchup": "Global markets (catch-up)",
    "global_intraday": "Global markets (intraday)", "global_overnight": "Global markets (overnight)",
    "global_premarket": "Global markets (pre-market)", "history_backfill": "Price history backfill",
    "impact_calibration": "Market-impact calibration", "institutional_weekly": "Institutional holdings (weekly)",
    "intraday_indexes": "Index levels (intraday)", "intraday_scans": "Intraday scans", "intraday_signals": "Intraday signals",
    "intraday_scan": "Intraday scan", "live_pnl_snapshot": "Live P&L snapshot", "market_series": "Market series",
    "minute_bars_rest": "1-minute bars", "ml_anomalies": "ML anomaly check", "ml_model_health": "ML model health",
    "ml_predictions": "ML predictions", "morning_digest": "Morning digest", "news_catchup": "News (catch-up)",
    "news_impact_evaluate": "News impact check", "news_midday": "News (midday)", "news_premarket": "News (pre-market)",
    "news_summary": "News summary", "news_weights": "News weights", "nifty_history": "Nifty history",
    "nse_index_closes": "NSE index closes", "ops_webhook_dispatch": "Webhook delivery", "option_chain": "Option chain",
    "options_expiry": "Options expiry", "options_strategy_marks": "Option strategy marks", "order_pressure": "Order-book pressure",
    "paper_matching": "Paper order matching", "paper_next_open_fills": "Paper fills at the open",
    "participant_oi": "Participant open interest", "portfolio_health": "Portfolio health", "portfolio_risk": "Portfolio risk",
    "premarket_report": "Pre-market report", "preopen_capture": "Pre-open capture", "preopen_evaluate": "Pre-open check",
    "quant_factors": "Quant factors", "research_data": "Research data", "research_reports": "Research reports",
    "saved_screens": "Saved screens", "signal_log": "Signal log", "signal_outcomes": "Signal outcomes",
    "strategy_decisions": "Strategy decisions", "strategy_health": "Strategy health",
    "technical_indicators": "Technical indicators", "technical_signals": "Technical signals",
    "ticks_minute_bars": "Tick capture to 1-minute bars", "wealth_cycle": "Wealth update", "zerodha_portfolio": "Zerodha holdings",
    "zerodha_token_check": "Zerodha sign-in check", "stock_feed": "Stock live feed", "index_feed": "Index live feed",
    "depth20": "20-level market depth", "news": "News", "global_markets": "Global markets", "bhavcopy": "NSE closing prices",
}


def job_label(name) -> str:
    n = str(name or "").strip()
    if n in JOB_NAMES:
        return JOB_NAMES[n]
    words = n.replace("_", " ").replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "Job"


GLOSSARY = {
    "confluence": "How many independent pieces of evidence agree with the signal, out of 6: technical rating, volume, "
                  "strength against the Nifty, the market gate, a candle pattern and the research rating.",
    "market gate": "ATIP's read of the whole market. While it is closed, a buy signal goes against the market and is "
                   "hidden by default.",
    "75m": "The same technical rating on the day's 75-minute bars: ✓ agrees with the daily rating, ✗ disagrees, "
           "~ one of the two is neutral.",
    "weekly": "The same technical rating on completed weekly bars: ✓ when it is on the signal's side.",
    "atr": "Average true range: how far the price usually moves in a day. Stops and targets are set in ATRs.",
    "atip score": "ATIP's 0-100 rating of a stock today, from its momentum, value, risk and news scores.",
    "eod": "End of day: computed from the closing prices, not live.",
}
