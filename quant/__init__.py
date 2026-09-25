"""
ATIP W6 -- advanced quant.

    market data (prices_daily, fundamental_data, corporate_actions, bulk_deals,
                 live_quotes, index series)
      -> factors         factors.py      registry + point-in-time calculators
      -> normalization   normalize.py    z-score / percentile / rank / winsorize /
                                         min-max / sector- / market-relative / neutralize
      -> cross-section   engine.py       per-date factor scores, composites, rankings
                                         -> quant_factor_score
      -> research        research.py     IC, quantile spreads, factor correlation;
                         experiments.py  experiment records that run through W2
      -> strategies      W3 kinds: multi_factor / quant_rank on qf_* / qc_* features,
                         new kinds "pairs" (stat-arb) and "portfolio" (construction)
      -> portfolio       portfolio.py    weighting, constraints, neutrality, exposures
      -> W3 StrategyDecision -> PositionIntent -> W4 risk engine -> OMS -> paper

    statarb.py        hedge ratio, spreads, correlation, Engle-Granger cointegration,
                      half-life, z-score
    volatility.py     close-to-close / Parkinson / Garman-Klass / downside / regime /
                      implied-vs-realised
    derivatives.py    Black-Scholes, greeks, implied volatility, IV rank, futures
                      basis / roll -- math ready; NO derivatives data exists yet
    events.py         market_event from corporate_actions + bulk_deals; typed interface
                      for earnings / dividends / macro / index changes (data pending)
    microstructure.py intraday features from live_quotes snapshots; tick / depth /
                      spread interface (data pending: live_ticks is empty)

Nothing in quant/ imports execution/ or orders/: quant output reaches trading
only through W3 strategies and the W4 risk engine.
"""
