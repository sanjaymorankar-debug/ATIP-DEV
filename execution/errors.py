"""W4 error types. Every one is a ValueError, so API routes map them to 400."""


class ExecutionError(ValueError):
    pass


class InvalidIntentError(ExecutionError):
    """The intent is malformed, unknown, or not in a state that can be evaluated."""


class DuplicateDecisionError(ExecutionError):
    """The intent already has a risk decision (evaluation is once per intent)."""


class DuplicateOrderError(ExecutionError):
    """An order already exists for this intent / risk decision."""


class OrderStateError(ExecutionError):
    """An order state transition that ORDER_TRANSITIONS does not allow."""


class LiveTradingDisabled(ExecutionError):
    """A LIVE path was reached while live trading is not enabled (the default)."""


class BrokerError(ExecutionError):
    """The broker adapter failed or answered with an error."""


class RiskLimitError(ExecutionError):
    """An invalid risk-limit key or value."""
