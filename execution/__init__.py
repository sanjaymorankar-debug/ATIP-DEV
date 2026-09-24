"""
ATIP W4 -- risk engine and execution.

    StrategyDecision  (strategy_engine, W3)
        -> PositionIntent          strategy_position_intent, created NOT_AUTHORIZED
        -> Risk Engine             risk_engine.evaluate()  -> risk_decision
              APPROVED / REJECTED / BLOCKED / REVIEW_REQUIRED
        -> Order Manager           order_manager.create_order() -> oms_order (+ oms_order_event)
              explicit state machine (models.ORDER_TRANSITIONS)
        -> Execution Adapter       adapters.get_adapter(mode) -> BrokerAdapter
              PaperBrokerAdapter   orders/paper.py PaperBroker (simulated fills at live prices)
              DhanBrokerAdapter    placeholder: refuses every call in W4
        -> Fill                    oms_execution + oms_fill
        -> Position                paper_position (the paper broker's book) + per-strategy
                                   attribution from oms_fill (positions.py)

Safety gates (config.py):
    execution.mode                  PAPER (default) | LIVE
    execution.live_trading_enabled  false (default). LIVE needs mode LIVE AND this true
                                    AND an order-by-order manual review -- and even then
                                    the W4 Dhan adapter refuses: live execution is not built.
    execution.auto_execute_paper    false (default): the scheduled cycle evaluates risk only;
                                    orders are sent when the owner runs the cycle with
                                    execute=true or submits an order, or turns this on.
    The W1 kill switch (orders/risk.py halted()) blocks every new order.

Nothing in the strategy engine imports this package; the dependency runs one
way, strategy -> intent -> execution.
"""
