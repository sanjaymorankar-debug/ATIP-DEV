"""
ATIP Strategy Engine (W3).

A strategy is DATA -- a versioned JSON definition -- run by a small set of
generic kinds, so a new strategy, parameter set or rule change never needs an
engine change:

    Market data -> features (indicators, ATIP scores, regime) -> strategy rules
      -> decision (BUY / EXIT / HOLD / ... ) -> PositionIntent
      -> [W4 risk engine -> execution]      (not built: intents are never orders)
      -> W2 backtest engine                 (through adapter.DefinitionStrategy)

    params.py     typed parameter specs and validation            (SE-02)
    definition.py the strategy definition schema, validation, hash (SE-01)
    features.py   point-in-time feature catalogue                 (inputs)
    rules.py      condition language: all / any / not / min-of    (SE-03)
    regime.py     regime providers: Market Health + VIX           (SE-08)
    kinds.py      rule, multi_factor, quant_rank, composite, python (SE-03/04/06/07/08)
    option_overlay.py  (W40, ENT-15) option_overlay: multi-leg OPTION intents on the paper options book,
                  plus its dry run and a replay on stored daily option prices
    decisions.py  decision states, PositionIntent, advisory risk gate
    registry.py   strategies, immutable versions, library sync    (SE-01)
    lifecycle.py  states, allowed transitions, audit trail        (SE-09)
    engine.py     generate and store decisions for a date         (signal-engine integration)
    adapter.py    a stored version as a W2 backtest Strategy      (backtest integration)
    health.py     strategy health snapshots                       (SE-11)
    library/      the definitions ATIP ships with

The existing `strategy/` package (the aggressive exit policy and its live
position ledger) is separate and unchanged.
"""
