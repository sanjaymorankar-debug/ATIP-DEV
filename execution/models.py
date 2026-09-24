"""
W4 data model: the risk decision and the order state machine.

RISK STATUS
    APPROVED         every check passed (the quantity may be reduced to fit limits)
    REJECTED         a limit or validity check failed
    BLOCKED          a safety gate: kill switch, live trading disabled, strategy
                     not enabled (not in PAPER / READY / ACTIVE)
    REVIEW_REQUIRED  passed, but a person must approve it (require_manual_review,
                     or any LIVE intent)

The intent's authorization_status follows the verdict:
    APPROVED -> AUTHORIZED, REJECTED -> REJECTED, BLOCKED -> BLOCKED,
    REVIEW_REQUIRED -> REVIEW_REQUIRED (-> AUTHORIZED when approved by the owner)

ORDER STATES (every change is a row in oms_order_event)

    CREATED -> VALIDATED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
       |           |           |             |                |
       |           |           |             +-> CANCEL_PENDING -> CANCELLED
       +-----------+-----------+-> REJECTED / FAILED / CANCELLED (see the table)

FILLED, CANCELLED, REJECTED and FAILED are terminal.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from execution.errors import OrderStateError

APPROVED, REJECTED, BLOCKED, REVIEW_REQUIRED = "APPROVED", "REJECTED", "BLOCKED", "REVIEW_REQUIRED"
RISK_STATUSES = (APPROVED, REJECTED, BLOCKED, REVIEW_REQUIRED)
INTENT_STATUS_FOR = {APPROVED: "AUTHORIZED", REJECTED: "REJECTED", BLOCKED: "BLOCKED",
                     REVIEW_REQUIRED: "REVIEW_REQUIRED"}
ENGINE_VERSION = "w4-1.0"

# check results
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

CREATED, VALIDATED, SUBMITTED, ACKNOWLEDGED = "CREATED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED"
PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED = "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED"
O_REJECTED, FAILED = "REJECTED", "FAILED"
ORDER_STATES = (CREATED, VALIDATED, SUBMITTED, ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING,
                CANCELLED, O_REJECTED, FAILED)
TERMINAL = {FILLED, CANCELLED, O_REJECTED, FAILED}
ORDER_TRANSITIONS = {
    CREATED:          {VALIDATED, O_REJECTED, CANCELLED, FAILED},
    VALIDATED:        {SUBMITTED, CANCELLED, FAILED},
    SUBMITTED:        {ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, O_REJECTED, FAILED},
    ACKNOWLEDGED:     {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, O_REJECTED, FAILED},
    PARTIALLY_FILLED: {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, FAILED},
    CANCEL_PENDING:   {CANCELLED, PARTIALLY_FILLED, FILLED, FAILED},
    FILLED: set(), CANCELLED: set(), O_REJECTED: set(), FAILED: set(),
}


def check_transition(frm: str, to: str):
    if to not in ORDER_TRANSITIONS.get(frm, set()):
        raise OrderStateError(f"order cannot move {frm} -> {to}; allowed from {frm}: "
                              f"{sorted(ORDER_TRANSITIONS.get(frm, set())) or 'none (terminal)'}")


@dataclass
class RiskCheck:
    check: str
    status: str                  # PASS / WARN / FAIL / SKIP
    message: str
    value: float | None = None
    limit: float | None = None


@dataclass
class RiskDecision:
    intent_id: str
    symbol: str
    side: str
    risk_status: str
    requested_quantity: int | None
    approved_quantity: int
    rejection_reason: str | None = None
    risk_checks: list = field(default_factory=list)
    decision_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    action: str | None = None
    book: str | None = None
    mode: str | None = None
    reference_price: float | None = None
    est_value: float | None = None
    equity: float | None = None
    limits: dict = field(default_factory=dict)
    engine_version: str = ENGINE_VERSION
    risk_decision_id: str = field(default_factory=lambda: "RD" + uuid.uuid4().hex[:18].upper())
    timestamp: datetime = field(default_factory=datetime.now)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class BrokerResult:
    """What an adapter reports back for one call."""
    status: str                  # ACCEPTED | FILLED | PARTIALLY_FILLED | REJECTED | ERROR | CANCELLED
    broker_order_id: str | None = None
    filled_quantity: int = 0
    avg_price: float | None = None
    fees: float = 0.0
    price_source: str | None = None
    message: str | None = None
    raw: dict | None = None
