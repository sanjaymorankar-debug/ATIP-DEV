"""
W39 (SC-20) — stock screener (fundamental + technical): Screener.in / Dhan ScanX / Kite Screener style filters over
everything ATIP knows about a stock, one row per symbol.

    FIELDS        147 screenable fields: valuation (P/E, P/B, PEG, yields, market cap), profitability
                  (ROE, ROCE, margins), growth (YoY, QoQ), earnings surprise (research/earnings_surprise.py:
                  SUE and revenue SUE against the same quarter a year earlier -- ATIP's own history, not
                  consensus -- the EPS-trend revisions proxy, days since the result), balance sheet (debt/equity, interest cover,
                  cash, FCF), ownership (promoter, pledge, FPI, MF, promoter change), price (1-year /
                  3-year return, distance from 52-week high / low), ATIP (score, signal), the research
                  model (rating, upside, fair value, moat proxy, quality), a magic-formula rank
                  (Greenblatt, approximated with E/P and ROCE; financials excluded), the fundamental
                  scorecard (research/scorecard.py: checks passed of 30 and per axis), the DVM view
                  (research/dvm.py: durability, valuation, momentum 0-100 and the zone), the technical
                  snapshot (research/tech_signals.py: rating, weekly rating and daily / weekly agreement,
                  75-minute rating and daily / 75-minute agreement, RS rating, RSI, MACD, ADX, Supertrend,
                  patterns, chart patterns in place, VCP setup, signals and one scan_<key> 1/0 field per
                  scan) and order-book pressure
                  (data/order_pressure.py)
    query         a small, safe query language -- no eval:
                      roce_pct > 20 AND debt_equity < 0.5 AND (pe < 25 OR peg < 1)
                      industry IN ("Capital Goods", "Automobile and Auto Components")
                      scan_golden_cross = 1 AND roce_pct > 15       (technical + fundamental)
                      patterns CONTAINS "engulfing" AND tech_rating > 0
                      research_rating = "BUY" AND NOT atip_signal = "SELL"
                  AND binds tighter than OR; field names are case-insensitive and accept the aliases
                  in FIELDS (e.g. ROCE, PE, "debt to equity" written as debt_to_equity). A stock
                  missing a field never matches a condition on it (as Screener.in does).
    PRESETS       ready-made screens in three groups: fundamental (quality compounders, value, GARP,
                  dividend, debt-free, ...), technical (breakouts on volume, golden cross, Supertrend,
                  trend template, RS leaders, ...) and combined (quality stock breaking out, ...)
    saved screens research_screen: name, query, sort, columns, notify. run_saved_screens() (daily
                  20:50, after the research reports) re-runs them and alerts on NEW matches; the same
                  job stores the day's scorecards (fundamental_scorecard) for their track record.

Units: percentages are in % (roce_pct 22.5 means 22.5 %); money in rupees, *_cr in crore.
The snapshot is cached for 10 minutes per database so editing a query in the UI is instant.
CLI: python -m research.screener "roce_pct > 20 AND debt_equity < 0.5" [--sort roce_pct] [--limit 50]
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta

from research import valuation as V

log = logging.getLogger(__name__)
CACHE_SECONDS = 600
_CACHE: dict = {}


def _f(key, label, group, unit="", kind="num", aliases=(), desc=""):
    return key, {"key": key, "label": label, "group": group, "unit": unit, "kind": kind,
                 "aliases": list(aliases), "description": desc}


FIELDS = dict([
    _f("symbol", "Symbol", "Company", kind="text"),
    _f("industry", "Industry", "Company", kind="text", aliases=("sector",), desc="NSE Nifty 500 industry"),
    _f("price", "Price", "Valuation", "₹", aliases=("cmp", "close")),
    _f("market_cap_cr", "Market cap", "Valuation", "₹ cr", aliases=("mcap", "market_cap", "marketcap")),
    _f("pe", "P/E", "Valuation", "x", aliases=("pe_ratio", "price_to_earnings")),
    _f("pb", "P/B", "Valuation", "x", aliases=("pb_ratio", "price_to_book")),
    _f("peg", "PEG", "Valuation", "x", desc="P/E / EPS growth"),
    _f("earnings_yield_pct", "Earnings yield", "Valuation", "%", aliases=("earnings_yield",), desc="EPS / price"),
    _f("dividend_yield_pct", "Dividend yield", "Valuation", "%", aliases=("dividend_yield", "dy")),
    _f("fcf_yield_pct", "FCF yield", "Valuation", "%", aliases=("fcf_yield",), desc="free cash flow / market cap"),
    _f("roe_pct", "ROE", "Profitability", "%", aliases=("roe", "return_on_equity")),
    _f("roce_pct", "ROCE", "Profitability", "%", aliases=("roce", "return_on_capital")),
    _f("roa_pct", "ROA", "Profitability", "%", aliases=("roa",)),
    _f("net_margin_pct", "Net margin", "Profitability", "%", aliases=("net_margin", "npm")),
    _f("operating_margin_pct", "Operating margin", "Profitability", "%", aliases=("operating_margin", "opm")),
    _f("revenue_growth_pct", "Revenue growth YoY", "Growth", "%", aliases=("sales_growth", "revenue_growth")),
    _f("profit_growth_pct", "Profit growth YoY", "Growth", "%", aliases=("profit_growth",)),
    _f("eps_growth_pct", "EPS growth YoY", "Growth", "%", aliases=("eps_growth",)),
    _f("qoq_revenue_pct", "Revenue growth QoQ", "Growth", "%", aliases=("qoq_sales",)),
    _f("qoq_profit_pct", "Profit growth QoQ", "Growth", "%", aliases=("qoq_profit",)),
    _f("sue", "Earnings surprise (SUE)", "Earnings", "sd", aliases=("eps_surprise", "earnings_surprise"),
       desc="(EPS - EPS of the same quarter a year earlier) / the standard deviation of that change over the 8 "
            "quarters before; a surprise against ATIP's own history, not analyst consensus (research/earnings_surprise.py)"),
    _f("sue_revenue", "Revenue surprise (SUE)", "Earnings", "sd", aliases=("revenue_surprise", "sales_surprise"),
       desc="the same on quarterly revenue"),
    _f("eps_trend", "EPS growth trend", "Earnings", kind="text", aliases=("revisions_proxy", "earnings_trend"),
       desc="ACCELERATING / DECELERATING: trailing-4-quarter EPS growth against the filing before -- a proxy for "
            "estimate revisions, not analyst revisions"),
    _f("days_since_result", "Days since the latest result", "Earnings", "days",
       aliases=("days_since_results", "result_age"), desc="calendar days since the latest quarter's filing was broadcast"),
    _f("revenue_cr", "Revenue (quarter)", "Size", "₹ cr", aliases=("sales", "revenue")),
    _f("profit_cr", "Net profit (quarter)", "Size", "₹ cr", aliases=("profit", "net_profit")),
    _f("eps_ttm", "EPS (TTM)", "Size", "₹", aliases=("eps",)),
    _f("book_value_ps", "Book value / share", "Size", "₹", aliases=("book_value", "bvps")),
    _f("debt_equity", "Debt / equity", "Balance sheet", "x", aliases=("de", "debt_to_equity", "d_e")),
    _f("current_ratio", "Current ratio", "Balance sheet", "x"),
    _f("interest_coverage", "Interest cover", "Balance sheet", "x", aliases=("interest_cover", "icr")),
    _f("cash_cr", "Cash", "Balance sheet", "₹ cr", aliases=("cash",)),
    _f("fcf_cr", "Free cash flow (FY)", "Balance sheet", "₹ cr", aliases=("fcf", "free_cash_flow")),
    _f("promoter_pct", "Promoter holding", "Ownership", "%", aliases=("promoter", "promoter_holding")),
    _f("promoter_change_pts", "Promoter change (QoQ)", "Ownership", "pts", aliases=("promoter_change",)),
    _f("pledged_pct", "Promoter pledge", "Ownership", "%", aliases=("pledge", "pledged")),
    _f("fpi_pct", "FPI holding", "Ownership", "%", aliases=("fii", "fpi")),
    _f("mf_pct", "MF holding", "Ownership", "%", aliases=("mf",)),
    _f("return_1y_pct", "Return 1 year", "Price", "%", aliases=("return_1y",)),
    _f("cagr_3y_pct", "CAGR 3 years", "Price", "%", aliases=("return_3y", "cagr_3y")),
    _f("from_52w_high_pct", "From 52-week high", "Price", "%", aliases=("from_high",),
       desc="0 at the high, -20 means 20% below it"),
    _f("from_52w_low_pct", "From 52-week low", "Price", "%", aliases=("from_low",)),
    _f("tech_rating", "Technical rating", "Technical", "-1..1", aliases=("technical_rating",),
       desc="mean of moving-average and oscillator votes (research/technicals.py)"),
    _f("tech_rating_label", "Technical rating label", "Technical", kind="text",
       desc="STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL"),
    _f("tech_rating_w", "Weekly technical rating", "Technical", "-1..1", aliases=("weekly_rating",),
       desc="the same vote on completed weekly bars (35+ weeks of history needed)"),
    _f("tech_rating_w_label", "Weekly rating label", "Technical", kind="text", aliases=("weekly_label",),
       desc="STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL on weekly bars"),
    _f("mtf_alignment", "Daily + weekly agreement", "Technical", kind="text", aliases=("mtf", "timeframes"),
       desc="BULL: daily and weekly both BUY / STRONG_BUY; BEAR: both SELL / STRONG_SELL; else MIXED"),
    _f("rsi_14_w", "Weekly RSI (14)", "Technical", "", aliases=("weekly_rsi",)),
    _f("tech_rating_75", "75-minute technical rating", "Technical", "-1..1", aliases=("rating_75m", "tech_rating_75m"),
       desc="the same vote on completed 75-minute bars (the session in five from 09:15) built from the stored "
            "15-minute bars; 35+ bars (7 sessions) needed; as of the close"),
    _f("tech_rating_75_label", "75-minute rating label", "Technical", kind="text", aliases=("label_75m",),
       desc="STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL on 75-minute bars"),
    _f("mtf_alignment_75", "Daily + 75-minute agreement", "Technical", kind="text",
       aliases=("mtf_75", "mtf_75m", "intraday_daily"),
       desc="BULL: daily and 75-minute both BUY / STRONG_BUY; BEAR: both SELL / STRONG_SELL; else MIXED"),
    _f("rsi_14_75", "75-minute RSI (14)", "Technical", "", aliases=("rsi_75m",)),
    _f("supertrend_dir_75", "75-minute Supertrend direction", "Technical", "+1/-1", aliases=("supertrend_75m",)),
    _f("delivery_pct", "Delivery %", "Technical", "%", aliases=("deliv_pct", "delivery"),
       desc="NSE deliverable quantity as % of traded quantity (bhavcopy)"),
    _f("delivery_ratio", "Delivery % vs 20-day avg", "Technical", "x", aliases=("deliv_ratio",),
       desc="today's delivery % divided by its 20-day average"),
    _f("rs_line_at_high", "RS line at a 52-week high", "Technical", "1/0", aliases=("rs_line_high", "rs_new_high"),
       desc="price / Nifty (the RS line) above its prior 52-week high today"),
    _f("cap_bucket", "Size group", "Technical", kind="text", aliases=("size", "cap_group"),
       desc="LARGE (top 100 by market cap) / MID (101-250) / SMALL, AMFI's rule over ATIP's universe"),
    _f("rs_rating_cap", "RS rating in size group (1-99)", "Technical", "", aliases=("rs_cap", "rs_in_group"),
       desc="the RS rating ranked only against stocks of the same size group"),
    _f("chart_patterns", "Chart patterns in place", "Technical", kind="text", aliases=("chart_pattern",),
       desc="Darvas box / VCP setup / double bottom / ascending or descending triangle / head and shoulders or "
            "its inverse / rising or falling channel or wedge formed and not yet triggered, with their levels "
            "(research/patterns.py)"),
    _f("vcp_setup", "VCP setup", "Technical", "1/0", aliases=("vcp",),
       desc="a volatility contraction pattern below its pivot, volume drying up, in an up-trend"),
    _f("supertrend_dir_w", "Weekly Supertrend direction", "Technical", "+1/-1", aliases=("weekly_supertrend",)),
    _f("rs_rating", "RS rating (1-99)", "Technical", "", aliases=("rs_rank", "ibd_rs"),
       desc="IBD-style: weighted 3/6/9/12-month return, percentile across the universe; 99 = strongest"),
    _f("rsi_14", "RSI (14)", "Technical", "", aliases=("rsi",)),
    _f("macd_hist", "MACD histogram", "Technical", "", aliases=("macd",)),
    _f("adx_14", "ADX (14)", "Technical", "", aliases=("adx",)),
    _f("supertrend_dir", "Supertrend direction", "Technical", "+1/-1", aliases=("supertrend",)),
    _f("atr_pct", "ATR % of price", "Technical", "%", aliases=("atr",)),
    _f("above_200dma", "Above 200-DMA", "Technical", "1/0", aliases=("above_200",)),
    _f("pct_from_sma50", "From SMA 50", "Technical", "%", aliases=("from_sma50",)),
    _f("pct_from_sma200", "From SMA 200", "Technical", "%", aliases=("from_sma200",)),
    _f("bb_width_pct", "Bollinger width", "Technical", "%", aliases=("bb_width",)),
    _f("vol_ratio", "Volume vs 20-day avg", "Technical", "x", aliases=("volume_ratio", "rel_volume")),
    _f("rs_63_pct", "Relative strength vs Nifty (3m)", "Technical", "%", aliases=("rs", "relative_strength")),
    _f("return_1m_pct", "Return 1 month", "Price", "%", aliases=("return_1m",)),
    _f("return_3m_pct", "Return 3 months", "Price", "%", aliases=("return_3m",)),
    _f("patterns", "Candlestick patterns today", "Technical", kind="text", aliases=("pattern", "candles"),
       desc='e.g. patterns CONTAINS "engulfing"'),
    _f("signals", "Technical signals today", "Technical", kind="text",
       desc='e.g. signals CONTAINS "breakout"'),
    _f("bull_signals", "Bullish signals today", "Technical", "count"),
    _f("bear_signals", "Bearish signals today", "Technical", "count"),
    _f("book_imbalance", "Pending buy/sell imbalance (today)", "Order book", "-1..1", aliases=("order_imbalance",),
       desc="(total pending buy - sell) / (buy + sell), latest poll today (data/order_pressure.py)"),
    _f("book_pressure", "Order-book pressure", "Order book", kind="text",
       desc="STRONG_BUYERS / BUYERS / BALANCED / SELLERS / STRONG_SELLERS"),
    _f("book_persistent", "Persistent one-sided book", "Order book", kind="text", desc="BUYERS / SELLERS"),
    _f("atip_score", "ATIP score", "ATIP", "", aliases=("score",)),
    _f("atip_signal", "ATIP signal", "ATIP", kind="text", aliases=("signal",)),
    _f("research_rating", "Research rating", "Research", kind="text", aliases=("rating",)),
    _f("research_upside_pct", "Upside to target", "Research", "%", aliases=("upside",)),
    _f("fair_value", "Fair value", "Research", "₹"),
    _f("moat_proxy", "Moat proxy", "Research", kind="text", aliases=("moat",)),
    _f("quality_score", "Quality score", "Research", aliases=("quality",)),
    _f("magic_rank", "Magic formula rank", "Research", aliases=("magic_formula",),
       desc="Greenblatt: rank by earnings yield + rank by ROCE; 1 is best; financials excluded"),
    _f("checks_passed", "Scorecard checks passed (of 30)", "Scorecard", "0-30", aliases=("scorecard", "checks"),
       desc="5 axes x 6 pass / fail checks (research/scorecard.py); a check with no data never passes"),
    _f("value_checks", "Value checks (of 6)", "Scorecard", "0-6", aliases=("value_score",),
       desc="below fair value, 20 %+ below, P/E vs market and industry, PEG < 1, P/B vs industry"),
    _f("growth_checks", "Growth checks (of 6)", "Scorecard", "0-6", aliases=("growth_score",),
       desc="EPS growth vs a savings rate, the market and 20 %; revenue growth vs the market and 20 %; "
            "self-funded growth 10 %+"),
    _f("past_checks", "Past performance checks (of 6)", "Scorecard", "0-6", aliases=("past_score",),
       desc="EPS up over 3 years, accelerating, above the industry; net margin up; ROE 20 %+; positive FCF"),
    _f("health_checks", "Financial health checks (of 6)", "Scorecard", "0-6", aliases=("health_score",),
       desc="current ratio, net debt / equity, debt not rising, interest cover, FCF vs debt, promoter pledge "
            "(the debt checks are not scored for banks and NBFCs)"),
    _f("dividend_checks", "Dividend checks (of 6)", "Scorecard", "0-6", aliases=("dividend_score",),
       desc="yield vs payers' quartiles, paid 2 years running, growing, covered by earnings and by FCF"),
    _f("dvm_d", "DVM durability (0-100)", "DVM", "0-100", aliases=("durability", "dvm_durability"),
       desc="share of the scorecard's financial-health and past-performance checks passed, of those that could be "
            "made (research/dvm.py)"),
    _f("dvm_v", "DVM valuation (0-100)", "DVM", "0-100", aliases=("dvm_valuation", "valuation_score"),
       desc="60 % price vs the research fair value, 40 % P/E vs its industry's median (P/B for financials); "
            "high = cheap"),
    _f("dvm_m", "DVM momentum (0-100)", "DVM", "0-100", aliases=("dvm_momentum", "momentum_score"),
       desc="half the daily technical rating, half the RS rating"),
    _f("dvm_zone", "DVM zone", "DVM", kind="text", aliases=("dvm", "dvm_class"),
       desc="STRONG_PERFORMER / VALUE_TRAP / MOMENTUM_TRAP / EXPENSIVE_PERFORMER / VALUE_UNDER_RADAR / WEAK / "
            "MID_RANGE (55+ is high, below 35 low)"),
])

from research.technicals import SCANS as _SCANS                     # noqa: E402  (one 1/0 field per scan)
for _key, (_name, _dir, _rule, _desc) in _SCANS.items():
    _k, _m = _f(f"scan_{_key}", _name, "Technical scans", "1/0", desc=f"{_dir}: {_desc}")
    FIELDS[_k] = _m

_ALIAS = {}
for _k, _m in FIELDS.items():
    for _a in [_k, *_m["aliases"]]:
        _ALIAS[_a.lower()] = _k

DEFAULT_COLUMNS = ["symbol", "industry", "price", "market_cap_cr", "pe", "roce_pct", "roe_pct", "debt_equity",
                   "revenue_growth_pct", "eps_growth_pct", "research_rating", "research_upside_pct", "checks_passed",
                   "atip_score"]
TECH_COLUMNS = ["symbol", "industry", "price", "tech_rating_label", "tech_rating_w_label", "rs_rating", "rsi_14", "adx_14",
                "pct_from_sma200", "vol_ratio", "return_1m_pct", "signals"]
COMBINED_COLUMNS = ["symbol", "industry", "price", "pe", "roce_pct", "research_rating", "research_upside_pct",
                    "tech_rating_label", "rs_rating", "rsi_14", "signals"]
DVM_COLUMNS = ["symbol", "industry", "price", "dvm_d", "dvm_v", "dvm_m", "dvm_zone", "checks_passed",
               "research_upside_pct", "tech_rating_label", "rs_rating"]
_TECH_GROUPS = {"Technical", "Technical scans", "Order book"}
_NEUTRAL_GROUPS = {"Company", "Price"}


def default_columns(used: list) -> list:
    """Columns that suit the query: technical ones for a chart screen, a mix for a combined one, the three DVM
    scores with their inputs for a DVM screen."""
    groups = {FIELDS[f]["group"] for f in used if f in FIELDS}
    if "DVM" in groups:
        return DVM_COLUMNS
    tech, fund = bool(groups & _TECH_GROUPS), bool(groups - _TECH_GROUPS - _NEUTRAL_GROUPS)
    return COMBINED_COLUMNS if tech and fund else TECH_COLUMNS if tech else DEFAULT_COLUMNS

PRESETS = [
    {"key": "quality_compounders", "name": "Quality compounders",
     "description": "High and steady returns on capital, little debt, still growing",
     "query": "roce_pct > 20 AND roe_pct > 18 AND debt_equity < 0.5 AND revenue_growth_pct > 10",
     "sort": "roce_pct"},
    {"key": "value", "name": "Value picks", "description": "Cheap on earnings and book, profitable, not over-geared",
     "query": "pe < 15 AND pe > 0 AND pb < 2 AND roe_pct > 12 AND debt_equity < 1", "sort": "pe", "desc": False},
    {"key": "garp", "name": "Growth at a reasonable price", "description": "PEG under 1.2 with real growth",
     "query": "peg < 1.2 AND peg > 0 AND eps_growth_pct > 15 AND roe_pct > 15", "sort": "peg", "desc": False},
    {"key": "dividend", "name": "High dividend yield", "description": "Yield above 3% from a sound balance sheet",
     "query": "dividend_yield_pct > 3 AND debt_equity < 1 AND profit_growth_pct > 0", "sort": "dividend_yield_pct"},
    {"key": "debt_free", "name": "Debt-free and liquid", "description": "Almost no debt, comfortable current ratio",
     "query": "debt_equity < 0.1 AND current_ratio > 1.5 AND roe_pct > 10", "sort": "roe_pct"},
    {"key": "promoter_buying", "name": "Promoters adding, no pledge",
     "description": "Promoter stake up last quarter and nothing pledged",
     "query": "promoter_change_pts > 0 AND pledged_pct < 1", "sort": "promoter_change_pts"},
    {"key": "model_undervalued", "name": "Undervalued by ATIP's model",
     "description": "Research rating BUY or ADD with at least 15% upside",
     "query": 'research_rating IN ("BUY", "ADD") AND research_upside_pct > 15', "sort": "research_upside_pct"},
    {"key": "strong_near_high", "name": "Strong fundamentals near the 52-week high",
     "description": "Within 5% of the high, above the 200-DMA, ROE over 15%",
     "query": "from_52w_high_pct > -5 AND above_200dma = 1 AND roe_pct > 15", "sort": "from_52w_high_pct"},
    {"key": "turnaround", "name": "Turnaround", "description": "Profit jumped this quarter and is up on last year",
     "query": "qoq_profit_pct > 50 AND profit_growth_pct > 0 AND profit_cr > 0", "sort": "qoq_profit_pct"},
    {"key": "earnings_surprise", "name": "Positive earnings surprise",
     "description": "SUE of +2 or more on a result filed in the last 60 days: EPS beat the same quarter a year "
                    "earlier by 2+ standard deviations of its usual change (ATIP's own history; no consensus estimates)",
     "query": "sue >= 2 AND days_since_result <= 60", "sort": "sue"},
    {"key": "magic_formula", "name": "Magic formula (top 30)",
     "description": "Greenblatt's ranking (approximated with E/P and ROCE), market cap over ₹1,000 cr",
     "query": "magic_rank <= 30 AND market_cap_cr > 1000", "sort": "magic_rank", "desc": False},
    {"key": "oversold_quality", "name": "Oversold quality", "description": "RSI under 35 on a high-ROCE business",
     "query": "rsi_14 < 35 AND roce_pct > 18 AND debt_equity < 0.7", "sort": "rsi_14", "desc": False},
    {"key": "pledge_risk", "name": "Pledge risk", "description": "Promoter pledge above 20%: names to be careful with",
     "query": "pledged_pct > 20", "sort": "pledged_pct"},
    {"key": "sc_all_rounders", "name": "Scorecard all-rounders",
     "description": "20+ of the 30 scorecard checks passed, none of the five axes below 2",
     "query": ("checks_passed >= 20 AND value_checks >= 2 AND growth_checks >= 2 AND past_checks >= 2 AND "
               "health_checks >= 2 AND dividend_checks >= 2"), "sort": "checks_passed"},
    {"key": "sc_healthy_growers", "name": "Healthy and growing",
     "description": "5+ of 6 health checks and 4+ of 6 growth checks",
     "query": "health_checks >= 5 AND growth_checks >= 4", "sort": "growth_checks"},
    {"key": "sc_value_quality", "name": "Undervalued with a clean record",
     "description": "4+ value checks with 4+ past-performance and 4+ health checks",
     "query": "value_checks >= 4 AND past_checks >= 4 AND health_checks >= 4", "sort": "value_checks"},
    {"key": "sc_dividend", "name": "Dependable dividends",
     "description": "5+ of 6 dividend checks: a good yield, paid steadily, covered by earnings and cash",
     "query": "dividend_checks >= 5", "sort": "dividend_yield_pct"},
    # technical (Chartink / Finviz style; EOD, from research/technicals.py)
    {"key": "t_breakout_volume", "name": "52-week high breakout on volume", "group": "technical",
     "description": "Close above the prior 52-week high, volume over 1.5x average",
     "query": "scan_high_52w_breakout = 1", "sort": "vol_ratio"},
    {"key": "t_golden_cross", "name": "Golden cross", "group": "technical",
     "description": "SMA 50 crossed above SMA 200 today", "query": "scan_golden_cross = 1", "sort": "rs_63_pct"},
    {"key": "t_supertrend_buy", "name": "Supertrend buy", "group": "technical",
     "description": "Supertrend(10,3) flipped up today", "query": "scan_supertrend_buy = 1", "sort": "tech_rating"},
    {"key": "t_macd_bull", "name": "MACD bullish crossover above 200-DMA", "group": "technical",
     "description": "MACD crossed its signal line while price is above SMA 200",
     "query": "scan_macd_bull = 1 AND above_200dma = 1", "sort": "rs_63_pct"},
    {"key": "t_rsi_oversold", "name": "RSI oversold reversal", "group": "technical",
     "description": "RSI(14) crossed back up through 30", "query": "scan_rsi_oversold_turn = 1", "sort": "rsi_14",
     "desc": False},
    {"key": "t_trend_template", "name": "Minervini trend template", "group": "technical",
     "description": "Stage-2 up-trend: price > SMA50 > SMA150 > SMA200, near highs, RS positive",
     "query": "scan_trend_template = 1", "sort": "rs_63_pct"},
    {"key": "t_squeeze", "name": "Squeeze fired", "group": "technical",
     "description": "Bollinger bands expanded out of the Keltner channel with price above SMA 20",
     "query": "scan_bb_squeeze_fire = 1", "sort": "vol_ratio"},
    {"key": "t_bull_candles", "name": "Bullish candle at support", "group": "technical",
     "description": "Bullish engulfing / hammer / morning star / piercing line near the 50-DMA",
     "query": 'patterns CONTAINS "engulfing" OR patterns CONTAINS "hammer" OR patterns CONTAINS "morning star" '
              'OR patterns CONTAINS "piercing"', "sort": "tech_rating"},
    {"key": "t_strong_buy", "name": "Technical rating STRONG BUY", "group": "technical",
     "description": "Most moving-average and oscillator votes positive", "query": 'tech_rating_label = "STRONG_BUY"',
     "sort": "tech_rating"},
    {"key": "t_mtf_bull", "name": "Daily and weekly both bullish", "group": "technical",
     "description": "The technical rating is BUY or STRONG BUY on both daily and weekly bars",
     "query": 'mtf_alignment = "BULL"', "sort": "tech_rating_w"},
    {"key": "t_breakout_weekly", "name": "Breakout with the weekly trend", "group": "technical",
     "description": "A 20-day or 52-week breakout while the daily and weekly ratings both point up",
     "query": '(scan_donchian_20_breakout = 1 OR scan_high_52w_breakout = 1) AND mtf_alignment = "BULL"',
     "sort": "rs_rating"},
    {"key": "t_75m_daily_bull", "name": "75-minute and daily both bullish", "group": "technical",
     "description": "The technical rating is BUY or STRONG BUY on completed 75-minute bars and on daily bars, as of "
                    "the close",
     "query": 'mtf_alignment_75 = "BULL"', "sort": "tech_rating_75"},
    {"key": "t_pattern_breakouts", "name": "Chart pattern breakouts", "group": "technical",
     "description": ("Closed above a Darvas box, VCP pivot, double-bottom or inverse head-and-shoulders neckline, "
                     "ascending triangle, falling wedge or a channel's upper line today"),
     "query": ("scan_darvas_breakout = 1 OR scan_vcp_breakout = 1 OR scan_double_bottom_breakout = 1 OR "
               "scan_ascending_triangle_breakout = 1 OR scan_inverse_head_shoulders_breakout = 1 OR "
               "scan_falling_wedge_breakout = 1 OR scan_rising_channel_breakout = 1 OR "
               "scan_falling_channel_breakout = 1"), "sort": "rs_rating"},
    {"key": "t_pattern_breakdowns", "name": "Chart pattern breakdowns", "group": "technical",
     "description": ("Closed below a Darvas box, head-and-shoulders neckline, descending triangle, rising wedge or "
                     "a channel's lower line today"),
     "query": ("scan_darvas_breakdown = 1 OR scan_head_shoulders_breakdown = 1 OR "
               "scan_descending_triangle_breakdown = 1 OR scan_rising_wedge_breakdown = 1 OR "
               "scan_rising_channel_breakdown = 1 OR scan_falling_channel_breakdown = 1"), "sort": "rs_rating",
     "desc": False},
    {"key": "t_vcp_setups", "name": "VCP setups (not yet broken out)", "group": "technical",
     "description": "Volatility contraction below its pivot with volume drying up, strongest relative strength first",
     "query": "vcp_setup = 1", "sort": "rs_rating"},
    {"key": "t_rs_line_leads", "name": "RS line new high before price", "group": "technical",
     "description": "Relative strength vs the Nifty at a 52-week high while the price is not: early leadership (IBD)",
     "query": "scan_rs_line_leads = 1", "sort": "rs_rating"},
    {"key": "t_rs_in_size", "name": "RS leaders in their size group", "group": "technical",
     "description": "Top-decile relative strength among stocks of the same size (large / mid / small)",
     "query": "rs_rating_cap >= 90", "sort": "rs_rating_cap"},
    {"key": "t_delivery_spike", "name": "Delivery spike on an up day", "group": "technical",
     "description": "NSE delivery % at 1.5x+ its 20-day average on an up day (a heuristic; no peer-reviewed evidence)",
     "query": "scan_delivery_spike_up = 1", "sort": "delivery_ratio"},
    {"key": "t_rs_leaders", "name": "Relative-strength leaders", "group": "technical",
     "description": "RS rating 80+ (IBD-style) and above SMA 50", "query": "rs_rating >= 80 AND pct_from_sma50 > 0",
     "sort": "rs_rating"},
    {"key": "t_pocket_pivot", "name": "Pocket pivots", "group": "technical",
     "description": "Up day on volume above any down-day volume of the last 10 sessions, above SMA 50",
     "query": "scan_pocket_pivot = 1", "sort": "rs_rating"},
    {"key": "t_breakdown", "name": "Breakdowns (avoid / exit)", "group": "technical",
     "description": "New 52-week low on volume, death cross, or a Supertrend sell",
     "query": "scan_low_52w_breakdown = 1 OR scan_death_cross = 1 OR scan_supertrend_sell = 1", "sort": "rs_63_pct",
     "desc": False},
    {"key": "t_buyers_queuing", "name": "Buyers queuing, chart positive", "group": "technical",
     "description": "Persistent pending-buy pressure today with a positive technical rating (intraday context)",
     "query": 'book_persistent = "BUYERS" AND tech_rating > 0', "sort": "book_imbalance"},
    # technical + fundamental (the combination Trendlyne / Chartink + Screener users build by hand)
    {"key": "tf_quality_breakout", "name": "Quality stock breaking out", "group": "combined",
     "description": "ROCE over 15%, low debt, and a 20-day or 52-week breakout today",
     "query": "roce_pct > 15 AND debt_equity < 1 AND (scan_donchian_20_breakout = 1 OR scan_high_52w_breakout = 1)",
     "sort": "vol_ratio"},
    {"key": "tf_value_turning", "name": "Value stock turning up", "group": "combined",
     "description": "P/E under 20, ROE over 12%, and a MACD or Supertrend buy today",
     "query": "pe < 20 AND pe > 0 AND roe_pct > 12 AND (scan_macd_bull = 1 OR scan_supertrend_buy = 1)",
     "sort": "tech_rating"},
    {"key": "tf_model_buy_uptrend", "name": "Model BUY in an up-trend", "group": "combined",
     "description": "Research rating BUY / ADD, above the 200-DMA, technical rating positive",
     "query": 'research_rating IN ("BUY", "ADD") AND above_200dma = 1 AND tech_rating > 0.1',
     "sort": "research_upside_pct"},
    # DVM view (research/dvm.py): durability, valuation and momentum, 0-100 each
    {"key": "dvm_strong", "name": "DVM strong performers", "group": "combined",
     "description": "Durability, valuation and momentum all 55 or more: a sound business, not expensive, in favour",
     "query": 'dvm_zone = "STRONG_PERFORMER"', "sort": "dvm_m"},
    {"key": "dvm_value_radar", "name": "Sound and cheap, not yet in favour", "group": "combined",
     "description": "DVM durability and valuation 55+, momentum below 55: wait for the trend to turn",
     "query": 'dvm_zone = "VALUE_UNDER_RADAR"', "sort": "dvm_v"},
    {"key": "dvm_traps", "name": "DVM value and momentum traps", "group": "combined",
     "description": "Cheap or rising, but durability below 35: a weak business behind the price (names to be careful "
                    "with)",
     "query": 'dvm_zone IN ("VALUE_TRAP", "MOMENTUM_TRAP")', "sort": "dvm_d", "desc": False},
]
for _p in PRESETS:
    _p.setdefault("group", "fundamental")


class ScreenError(ValueError):
    pass


# ── query language ───────────────────────────────────────────────────────────

_TOKEN = re.compile(r"""\s*(?:
    (?P<num>-?\d+(?:\.\d+)?)%?
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<op>>=|<=|!=|==|=|>|<)
  | (?P<lp>\() | (?P<rp>\)) | (?P<comma>,)
  | (?P<word>[A-Za-z_][A-Za-z0-9_/]*)
)""", re.X)


def _tokens(text: str) -> list:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ScreenError(f"cannot read the query at: {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "num":
            out.append(("num", float(val)))
        elif kind == "str":
            out.append(("str", val[1:-1]))
        elif kind == "word" and val.upper() in ("AND", "OR", "NOT", "IN", "CONTAINS"):
            out.append((val.upper(), val.upper()))
        else:
            out.append((kind, val))
    return out


def field_key(name: str) -> str:
    k = _ALIAS.get(str(name).strip().lower())
    if not k:
        raise ScreenError(f"unknown field {name!r}; see /api/screener/fields")
    return k


class _Parser:
    def __init__(self, text):
        self.t = _tokens(text)
        self.i = 0
        if not self.t:
            raise ScreenError("the query is empty")

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, kind=None):
        tok = self.peek()
        if kind and tok[0] != kind:
            raise ScreenError(f"expected {kind} but found {tok[1]!r}" if tok[0] else f"expected {kind} at the end")
        self.i += 1
        return tok

    def parse(self):
        node = self.expr()
        if self.i != len(self.t):
            raise ScreenError(f"unexpected {self.peek()[1]!r}")
        return node

    def expr(self):
        node = self.term()
        while self.peek()[0] == "OR":
            self.take()
            node = ("or", node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.peek()[0] == "AND":
            self.take()
            node = ("and", node, self.factor())
        return node

    def factor(self):
        kind = self.peek()[0]
        if kind == "NOT":
            self.take()
            return ("not", self.factor())
        if kind == "lp":
            self.take()
            node = self.expr()
            self.take("rp")
            return node
        return self.comparison()

    def _value(self):
        kind, val = self.take()
        if kind not in ("num", "str", "word"):
            raise ScreenError(f"expected a value, found {val!r}")
        return val

    def comparison(self):
        _, name = self.take("word")
        key = field_key(name)
        kind, op = self.peek()
        if kind == "CONTAINS":
            self.take()
            val = self._value()
            if isinstance(val, float):
                raise ScreenError("CONTAINS needs text, e.g. patterns CONTAINS \"engulfing\"")
            return ("contains", key, val)
        if kind == "IN":
            self.take()
            self.take("lp")
            vals = [self._value()]
            while self.peek()[0] == "comma":
                self.take()
                vals.append(self._value())
            self.take("rp")
            return ("in", key, vals)
        self.take("op")
        val = self._value()
        if FIELDS[key]["kind"] == "num" and not isinstance(val, float):
            raise ScreenError(f"{key} is a number; {val!r} is not")
        return ("cmp", key, "=" if op == "==" else op, val)


def parse(text: str):
    return _Parser(text).parse()


def _cmp(a, op, b):
    if a is None:
        return False
    if isinstance(b, str):
        a, b = str(a).upper(), b.upper()
    try:
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "=": a == b, "!=": a != b}[op]
    except TypeError:
        return False


def matches(node, row: dict) -> bool:
    kind = node[0]
    if kind == "and":
        return matches(node[1], row) and matches(node[2], row)
    if kind == "or":
        return matches(node[1], row) or matches(node[2], row)
    if kind == "not":
        return not matches(node[1], row)
    if kind == "contains":
        v = row.get(node[1])
        return v is not None and node[2].upper() in str(v).upper()
    if kind == "in":
        v = row.get(node[1])
        return v is not None and any(_cmp(v, "=", x) for x in node[2])
    return _cmp(row.get(node[1]), node[2], node[3])


def fields_in(node) -> list:
    if node[0] in ("and", "or"):
        return fields_in(node[1]) + [f for f in fields_in(node[2]) if f not in fields_in(node[1])]
    if node[0] == "not":
        return fields_in(node[1])
    return [node[1]]


# ── the per-symbol snapshot ──────────────────────────────────────────────────

def _chunks(xs, n=500):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _closes_at(conn, symbols, d) -> dict:
    """Close on the last session at or before d (within 10 days), per symbol."""
    out = {}
    for part in _chunks(symbols):
        ins = ",".join("?" * len(part))
        cur = conn.execute(f"""SELECT p.symbol, p.close FROM prices_daily p JOIN (
                SELECT symbol, MAX(date) md FROM prices_daily WHERE symbol IN ({ins}) AND date<=? AND date>?
                  AND close>0 GROUP BY symbol) m ON p.symbol=m.symbol AND p.date=m.md""",
                           part + [str(d), str(d - timedelta(days=10))])
        out.update({r[0]: float(r[1]) for r in cur.fetchall() if r[1]})
    return out


def _latest_rows(conn, table, cols, symbols, as_of, date_col="date") -> dict:
    out = {}
    for part in _chunks(symbols):
        ins = ",".join("?" * len(part))
        try:
            cur = conn.execute(f"""SELECT t.symbol, {cols} FROM {table} t JOIN (
                    SELECT symbol, MAX({date_col}) md FROM {table} WHERE symbol IN ({ins}) AND {date_col}<=?
                    GROUP BY symbol) m ON t.symbol=m.symbol AND t.{date_col}=m.md""", part + [str(as_of)])
        except Exception:
            return {}
        names = [c[0] for c in cur.description]
        for r in cur.fetchall():
            out[r[0]] = dict(zip(names, r))
    return out


def _pct(x, nd=2):
    return round(x * 100, nd) if x is not None else None


def _margin_pct(v):
    """Margins are fractions in XBRL / Alpha Vantage rows; a value above 1.5 is already a percentage."""
    v = V._num(v)
    if v is None:
        return None
    return round(v if abs(v) > 1.5 else v * 100, 2)


def build_snapshot(conn, as_of=None, industry_map=None) -> list:
    """One row per symbol with fundamentals or a recent technical snapshot (and a price); every field in
    FIELDS, None where unknown."""
    from research.report import Universe
    as_of = (as_of if isinstance(as_of, date) else
             datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()) if as_of else date.today()
    syms = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM fundamental_data")}
    try:                     # stocks with only a technical snapshot belong in a technical screen too
        syms |= {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM technical_snapshot WHERE date>?",
                                            (str(as_of - timedelta(days=10)),))}
    except Exception:
        pass
    syms = sorted(syms)
    if not syms:
        return []
    uni = Universe(conn, as_of, industry_map, only=syms)
    syms = [s for s in syms if s in uni.prices]
    hi_lo = {}
    for part in _chunks(syms):
        ins = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT symbol, MAX(COALESCE(high, close)), MIN(COALESCE(low, close)) FROM prices_daily "
                              f"WHERE symbol IN ({ins}) AND date>? AND date<=? AND close>0 GROUP BY symbol",
                              part + [str(as_of - timedelta(days=365)), str(as_of)]):
            hi_lo[r[0]] = (r[1], r[2])
    c1 = _closes_at(conn, syms, as_of - timedelta(days=365))
    c3 = _closes_at(conn, syms, as_of - timedelta(days=round(3 * 365.25)))
    try:
        from research.tech_signals import latest_snapshot
        tsnap = latest_snapshot(conn, syms, as_of)
    except Exception:
        tsnap = {}
    tech = _latest_rows(conn, "technical_indicators", "t.rsi_14, t.above_200dma", syms, as_of)
    try:
        from data.order_pressure import latest as book_latest
        book = {r["symbol"]: r for r in book_latest(conn, day=as_of, limit=5000)}
    except Exception:
        book = {}
    sc = _latest_rows(conn, "ai_scores", "t.atip_score, t.`signal`", syms, as_of)
    rr = _latest_rows(conn, "research_report", "t.rating, t.upside_pct, t.fair_value, t.moat_proxy, t.quality_score",
                      syms, as_of, date_col="as_of")
    shp = {}
    for part in _chunks(syms):
        ins = ",".join("?" * len(part))
        try:
            for r in conn.execute(f"SELECT symbol, promoter_pct, pledged_pct, fpi_pct, mf_pct FROM shareholding_pattern "
                                  f"WHERE symbol IN ({ins}) AND as_of<=? ORDER BY symbol, as_of DESC",
                                  part + [str(as_of)]):
                shp.setdefault(r[0], []).append(r[1:])
        except Exception:
            break

    rows = []
    for s in syms:
        f, px = uni.fund.get(s) or {}, uni.prices[s]
        num = V._num
        eps, bv, shares = num(f.get("eps_ttm")), num(f.get("book_value_ps")), num(f.get("shares_out"))
        mcap = round(px * shares / 1e7, 2) if shares else None
        pe = round(px / eps, 2) if eps and eps > 0 else None
        eg = num(f.get("eps_growth_yoy"))
        fcf = num(f.get("fcf_cr"))
        hl = hi_lo.get(s)
        sh = shp.get(s) or []
        t, a, r = tech.get(s, {}), sc.get(s, {}), rr.get(s, {})
        row = {
            "symbol": s, "industry": uni.industry.get(s), "price": px, "market_cap_cr": mcap, "pe": pe,
            "pb": round(px / bv, 2) if bv and bv > 0 else None,
            "peg": round(pe / eg, 2) if pe and eg and eg > 0 else None,
            "earnings_yield_pct": round(eps / px * 100, 2) if eps is not None and px else None,
            "dividend_yield_pct": _pct(num(f.get("dividend_yield"))),
            "fcf_yield_pct": round(fcf / mcap * 100, 2) if fcf is not None and mcap else None,
            "roe_pct": _pct(num(f.get("roe"))), "roce_pct": _pct(num(f.get("roce"))),
            "roa_pct": _pct(num(f.get("roa"))),
            "net_margin_pct": _margin_pct(f.get("net_margin")),
            "operating_margin_pct": _margin_pct(f.get("operating_margin")),
            "revenue_growth_pct": num(f.get("revenue_growth_yoy")), "profit_growth_pct": num(f.get("profit_growth_yoy")),
            "eps_growth_pct": eg, "qoq_revenue_pct": num(f.get("qoq_revenue_chg")),
            "qoq_profit_pct": num(f.get("qoq_profit_chg")),
            "revenue_cr": num(f.get("revenue_cr")), "profit_cr": num(f.get("profit_cr")), "eps_ttm": eps,
            "book_value_ps": bv, "debt_equity": num(f.get("debt_equity")),
            "current_ratio": num(f.get("current_ratio")), "interest_coverage": num(f.get("interest_coverage")),
            "cash_cr": num(f.get("cash_cr")), "fcf_cr": fcf,
            "promoter_pct": num(sh[0][0]) if sh else num(f.get("promoter_hold")),
            "promoter_change_pts": (round(sh[0][0] - sh[1][0], 2) if len(sh) >= 2 and sh[0][0] is not None
                                    and sh[1][0] is not None else None),
            "pledged_pct": num(sh[0][1]) if sh else num(f.get("promoter_pledge")),
            "fpi_pct": num(sh[0][2]) if sh else None, "mf_pct": num(sh[0][3]) if sh else None,
            "return_1y_pct": round((px / c1[s] - 1) * 100, 2) if c1.get(s) else None,
            "cagr_3y_pct": round(((px / c3[s]) ** (1 / 3) - 1) * 100, 2) if c3.get(s) else None,
            "from_52w_high_pct": round((px / hl[0] - 1) * 100, 2) if hl and hl[0] else None,
            "from_52w_low_pct": round((px / hl[1] - 1) * 100, 2) if hl and hl[1] else None,
            "rsi_14": num(t.get("rsi_14")), "above_200dma": t.get("above_200dma"),
            "atip_score": num(a.get("atip_score")), "atip_signal": a.get("signal"),
            "research_rating": r.get("rating"), "research_upside_pct": num(r.get("upside_pct")),
            "fair_value": num(r.get("fair_value")), "moat_proxy": r.get("moat_proxy"),
            "quality_score": num(r.get("quality_score")), "magic_rank": None,
        }
        bk = book.get(s) or {}
        row["book_imbalance"], row["book_pressure"] = bk.get("total_imbalance"), bk.get("pressure")
        row["book_persistent"] = bk.get("persistent")
        ts = tsnap.get(s) or {}
        for k in ("tech_rating", "tech_rating_label", "rs_rating", "macd_hist", "adx_14", "supertrend_dir", "atr_pct",
                  "pct_from_sma50", "pct_from_sma200", "bb_width_pct", "vol_ratio", "rs_63_pct", "return_1m_pct",
                  "return_3m_pct", "patterns", "signals", "bull_signals", "bear_signals", "tech_rating_w",
                  "tech_rating_w_label", "rsi_14_w", "supertrend_dir_w", "mtf_alignment", "chart_patterns", "vcp_setup",
                  "rs_line_at_high", "cap_bucket", "rs_rating_cap", "delivery_pct", "delivery_ratio", "tech_rating_75",
                  "tech_rating_75_label", "rsi_14_75", "supertrend_dir_75", "mtf_alignment_75"):
            row[k] = ts.get(k)
        for k in ("rsi_14", "above_200dma"):
            if ts.get(k) is not None:
                row[k] = ts[k]
        for key in _SCANS:
            row[f"scan_{key}"] = ts.get(f"scan_{key}", 0) if ts else None
        rows.append(row)
    _magic_rank(rows)
    try:
        from research import earnings_surprise
        earnings_surprise.apply(conn, rows, as_of)
    except Exception as e:                      # like the scorecard: never takes the screener down
        log.warning(f"  Earnings surprise unavailable: {e}")
        for r in rows:
            for k in ("sue", "sue_revenue", "eps_trend", "days_since_result"):
                r.setdefault(k, None)
    from research import scorecard
    try:
        scorecard.apply(conn, rows, as_of, uni.fund)
    except Exception as e:                      # the scorecard must never take the screener down
        log.warning(f"  Scorecard unavailable: {e}")
        for r in rows:
            for k in scorecard.COUNT_FIELDS:
                r.setdefault(k, None)
    from research import dvm
    try:
        dvm.apply(rows)
    except Exception as e:                      # nor must the DVM view
        log.warning(f"  DVM unavailable: {e}")
        for r in rows:
            for k in dvm.FIELDS:
                r.setdefault(k, None)
    return rows


def _magic_rank(rows):
    """Greenblatt: rank by earnings yield (high first) + rank by ROCE (high first); lowest sum = 1."""
    pool = [r for r in rows if (r["earnings_yield_pct"] or 0) > 0 and (r["roce_pct"] or 0) > 0
            and not V.is_financial(r["industry"])]
    by_ey = {r["symbol"]: i for i, r in enumerate(sorted(pool, key=lambda r: -r["earnings_yield_pct"]))}
    by_rc = {r["symbol"]: i for i, r in enumerate(sorted(pool, key=lambda r: -r["roce_pct"]))}
    for i, r in enumerate(sorted(pool, key=lambda r: (by_ey[r["symbol"]] + by_rc[r["symbol"]], r["symbol"]))):
        r["magic_rank"] = i + 1


def snapshot(conn, use_cache=True, **kw) -> tuple:
    """(rows, built_at) -- cached per database for CACHE_SECONDS."""
    import db.schema as S
    key = (str(getattr(S, "DB_PATH", "")), str(kw.get("as_of") or date.today()))
    hit = _CACHE.get(key)
    if use_cache and hit and time.time() - hit[1] < CACHE_SECONDS:
        return hit[0], hit[2]
    rows = build_snapshot(conn, **kw)
    built = datetime.now().isoformat(timespec="seconds")
    _CACHE[key] = (rows, time.time(), built)
    return rows, built


def clear_cache():
    _CACHE.clear()


# ── running a screen ─────────────────────────────────────────────────────────

def _sort_key(field, desc):
    def k(r):
        v = r.get(field)
        if v is None:
            return (1, 0)                                   # missing values last either way
        if isinstance(v, str):
            return (0, v)
        return (0, -v if desc else v)
    return k


def run_screen(conn, query: str, sort: str | None = None, desc: bool = True, limit: int = 100,
               columns: list | None = None, use_cache: bool = True, rows: list | None = None) -> dict:
    node = parse(query)
    used = fields_in(node)
    sort = field_key(sort) if sort else (used[0] if used else "market_cap_cr")
    cols = [field_key(c) for c in (columns or default_columns(used))]
    for f in used + [sort]:
        if f not in cols:
            cols.append(f)
    built = None
    if rows is None:
        rows, built = snapshot(conn, use_cache=use_cache)
    hits = [r for r in rows if matches(node, r)]
    if FIELDS[sort]["kind"] == "text":
        hits.sort(key=lambda r: (r.get(sort) is None, str(r.get(sort) or "")), reverse=False)
        if desc:
            hits.reverse()
    else:
        hits.sort(key=_sort_key(sort, desc))
    limit = max(1, min(int(limit or 100), 2000))
    return {"query": query, "fields_used": used, "sort": sort, "desc": bool(desc), "count": len(hits),
            "universe": len(rows), "columns": cols, "snapshot_at": built,
            "rows": [{c: r.get(c) for c in cols} for r in hits[:limit]]}


def to_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = result["columns"]
    w.writerow([FIELDS[c]["label"] + (f" ({FIELDS[c]['unit']})" if FIELDS[c]["unit"] else "") for c in cols])
    for r in result["rows"]:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return buf.getvalue()


def catalog() -> dict:
    groups = {}
    for m in FIELDS.values():
        groups.setdefault(m["group"], []).append({k: m[k] for k in ("key", "label", "unit", "kind", "aliases",
                                                                      "description")})
    return {"groups": groups, "presets": PRESETS, "default_columns": DEFAULT_COLUMNS,
            "operators": [">", ">=", "<", "<=", "=", "!=", "IN", "CONTAINS"], "combine": ["AND", "OR", "NOT", "( )"]}


# ── saved screens ────────────────────────────────────────────────────────────

DDL = (
    """CREATE TABLE IF NOT EXISTS research_screen (
        screen_id TEXT PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL, sort TEXT, descending INTEGER DEFAULT 1,
        columns_json TEXT, notify INTEGER NOT NULL DEFAULT 0, last_run_at TIMESTAMP, last_count INTEGER,
        last_symbols_json TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""",
)


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def save_screen(conn, name: str, query: str, sort: str | None = None, desc: bool = True, columns=None,
                notify: bool = False, screen_id: str | None = None) -> dict:
    ensure_tables(conn)
    name = str(name or "").strip()
    if not name or len(name) > 80:
        raise ScreenError("a screen needs a name (up to 80 characters)")
    if len(query or "") > 2000:
        raise ScreenError("the query is too long")
    parse(query)                                             # refuse a screen that cannot run
    if sort:
        sort = field_key(sort)
    cols = [field_key(c) for c in columns] if columns else None
    now = datetime.now()
    if screen_id:
        if not conn.execute("SELECT 1 FROM research_screen WHERE screen_id=?", (screen_id,)).fetchone():
            raise LookupError(f"no saved screen {screen_id}")
        conn.execute("UPDATE research_screen SET name=?, query=?, sort=?, descending=?, columns_json=?, notify=?, "
                     "updated_at=? WHERE screen_id=?",
                     (name, query, sort, int(bool(desc)), json.dumps(cols) if cols else None, int(bool(notify)), now,
                      screen_id))
    else:
        screen_id = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO research_screen (screen_id, name, query, sort, descending, columns_json, notify, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (screen_id, name, query, sort, int(bool(desc)), json.dumps(cols) if cols else None,
                      int(bool(notify)), now, now))
    conn.commit()
    return get_screen(conn, screen_id)


def get_screen(conn, screen_id: str) -> dict:
    ensure_tables(conn)
    cur = conn.execute("SELECT * FROM research_screen WHERE screen_id=?", (screen_id,))
    r = cur.fetchone()
    if not r:
        raise LookupError(f"no saved screen {screen_id}")
    d = dict(zip([c[0] for c in cur.description], r))
    d["columns"] = json.loads(d.pop("columns_json") or "null")
    d["last_symbols"] = json.loads(d.pop("last_symbols_json") or "[]")
    return d


def list_screens(conn) -> list:
    ensure_tables(conn)
    ids = [r[0] for r in conn.execute("SELECT screen_id FROM research_screen ORDER BY name")]
    return [get_screen(conn, i) for i in ids]


def delete_screen(conn, screen_id: str) -> dict:
    get_screen(conn, screen_id)
    conn.execute("DELETE FROM research_screen WHERE screen_id=?", (screen_id,))
    conn.commit()
    return {"deleted": screen_id}


def run_saved(conn, screen_id: str, use_cache=True, rows=None, record=True) -> dict:
    s = get_screen(conn, screen_id)
    res = run_screen(conn, s["query"], s["sort"], bool(s["descending"]), limit=2000, columns=s["columns"],
                     use_cache=use_cache, rows=rows)
    now_syms = [r["symbol"] for r in res["rows"]]
    before = set(s["last_symbols"]) if s["last_run_at"] else None
    res["new"] = sorted(set(now_syms) - before) if before is not None else []
    res["dropped"] = sorted(before - set(now_syms)) if before is not None else []
    res["screen"] = {k: s[k] for k in ("screen_id", "name", "notify", "last_run_at")}
    if record:
        conn.execute("UPDATE research_screen SET last_run_at=?, last_count=?, last_symbols_json=? WHERE screen_id=?",
                     (datetime.now(), res["count"], json.dumps(now_syms), screen_id))
        conn.commit()
    return res


def run_saved_screens() -> dict:
    """Daily job: store the day's fundamental scorecards (research/scorecard.py, with each stock's DVM scores and
    zone from research/dvm.py), then re-run every saved screen; alert on new matches for screens with notify on."""
    from db.schema import get_connection
    conn = get_connection()
    ran = alerted = 0
    try:
        ensure_tables(conn)
        rows, _ = snapshot(conn, use_cache=False)
        from research import scorecard
        stored = scorecard.store(conn, rows, date.today())
        for s in list_screens(conn):
            res = run_saved(conn, s["screen_id"], rows=rows)
            ran += 1
            if s["notify"] and res["new"]:
                from alerts.telegram import notify
                names = ", ".join(res["new"][:15]) + (f" +{len(res['new']) - 15} more" if len(res["new"]) > 15 else "")
                notify(f"<b>Screener: {html.escape(s['name'])}</b>\n{len(res['new'])} new match(es): {names}\n"
                       f"{res['count']} in total.", category="screener", severity="info",
                       key=f"screen:{s['screen_id']}:{date.today()}")
                alerted += 1
    finally:
        conn.close()
    return {"status": "SUCCESS" if ran or stored else "SKIPPED", "rows": ran, "alerted": alerted, "scorecards": stored,
            "reason": None if ran else "no saved screens"}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m research.screener", description="W39 stock screener")
    ap.add_argument("query", nargs="?", help='e.g. "roce_pct > 20 AND debt_equity < 0.5"')
    ap.add_argument("--preset", choices=[p["key"] for p in PRESETS])
    ap.add_argument("--sort")
    ap.add_argument("--asc", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--csv", action="store_true")
    a = ap.parse_args(argv)
    p = next((x for x in PRESETS if x["key"] == a.preset), None)
    q = a.query or (p and p["query"])
    if not q:
        ap.error("give a query or --preset")
    from db.schema import get_connection
    conn = get_connection()
    try:
        res = run_screen(conn, q, a.sort or (p and p.get("sort")), not a.asc and (p or {}).get("desc", True),
                         a.limit, use_cache=False)
    finally:
        conn.close()
    print(to_csv(res) if a.csv else json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
