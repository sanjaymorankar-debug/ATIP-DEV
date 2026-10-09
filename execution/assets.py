"""
Asset classes ATIP can trade and the execution / risk path of each (W37: ENT-15).

    EQUITY / ETF   W4 OMS -> risk engine -> PaperBrokerAdapter (LIVE: refused); delivery (CNC)
    FUTURE         W30 short legs: risk engine sizes whole lots against futures margin -> FuturesPaperAdapter
    OPTION         W37 paper options book (execution/options_paper.py): long premium only, whole lots,
                   premium caps, expiry rules, intrinsic-value settlement. W40: option-overlay strategies'
                   multi-leg OPTION intents through the risk engine and OMS (instrument OPT)
    COMMODITY / CURRENCY / BOND
                   NOT tradable: ATIP holds their prices for research (W35 asset_price_daily) but has no
                   exchange connection or contract data for MCX / NSE-CDS / bonds -- listed so that
                   is explicit, not silently missing
"""

ASSET_CLASSES = {
    "EQUITY": {"tradable": "PAPER (LIVE refused)", "path": "OMS -> W4 risk engine -> paper broker",
               "rules": "W4 risk limits (position %, sector %, liquidity, volatility, correlation, VaR ...)"},
    "ETF": {"tradable": "PAPER (LIVE refused)", "path": "same as EQUITY (NSE cash segment)",
            "rules": "W4 risk limits"},
    "FUTURE": {"tradable": "PAPER, short legs of pair / market-neutral strategies (futures.enabled)",
               "path": "OMS (instrument FUT) -> FuturesPaperAdapter",
               "rules": "whole lots, margin_pct of notional, no new shorts in the expiry week, EOD-close pricing"},
    "OPTION": {"tradable": "PAPER, owner-entered and option-overlay strategies (options.enabled)",
               "path": "owner: execution/options_paper.place; strategies (W40): option_overlay OPTION intents -> "
                       "W4 risk engine (execution/option_intents.py) -> OMS (instrument OPT) -> OptionsPaperAdapter "
                       "-> options_paper.fill_strategy_order (all legs or none)",
               "rules": "owner: long premium only (no writing); strategies: spreads / condors / covered calls, naked "
                        "short calls only when the definition allows them; whole lots, premium per trade / total "
                        "caps, SPAN-like margin approximation and strategy max loss, no new positions near expiry, "
                        "traded contracts only, intrinsic settlement at expiry; LIVE refused"},
    "COMMODITY": {"tradable": "no", "path": "-", "rules": "prices for research only (W35 asset_price_daily)"},
    "CURRENCY": {"tradable": "no", "path": "-", "rules": "prices for research only (W35 asset_price_daily)"},
    "BOND": {"tradable": "no", "path": "-", "rules": "yields for research only (W35 macro / rates)"},
}


def catalogue() -> dict:
    from execution.options_paper import settings as opt
    try:
        from execution.futures_paper import settings as fut
        f = fut()
    except Exception:
        f = {}
    return {"asset_classes": ASSET_CLASSES, "options_settings": opt(), "futures_settings": f,
            "live": "LIVE execution is not built for any asset class"}
