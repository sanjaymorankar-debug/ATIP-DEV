"""
The Strategy Engine's output: StrategyDecision and PositionIntent.

StrategyDecision -- what a strategy concluded about one symbol on one date.

    decision   BUY | SELL | HOLD | WAIT | NO_TRADE        (the engine's vocabulary)
    action     the finer position action behind it:
               BUY    open a long (no position held)             -> decision BUY
               ADD    increase a held position                   -> decision BUY
               HOLD   keep a held position                       -> decision HOLD
               REDUCE partial exit                               -> decision SELL
               EXIT   close a held position                      -> decision SELL
               SELL   sell/avoid signal on a symbol NOT held     -> decision SELL
               NO_ACTION  nothing to do                          -> decision WAIT
               BLOCKED_BY_RISK  wanted BUY/ADD, a risk
                      requirement or the regime said no          -> decision NO_TRADE
    confidence 0..1 -- how strongly the strategy's rules held
    score      0..100 -- the standardised strategy score (confidence x 100;
               for multi-factor strategies this is the composite itself)
    regime     the Market Health regime on the decision date
    reasons    why, as a list of readable lines
    parameters the resolved parameters that produced it

The signal engine (scores/engine.py determine_signal) speaks the same
language: its BUY / SELL / HOLD / WAIT are these decisions; NO_TRADE is the
Strategy Engine's addition.

PositionIntent -- the hand-off object for a future risk/execution layer,
made only for decisions that would change a position (BUY, ADD, REDUCE,
EXIT). It is NOT an order: authorization_status is NOT_AUTHORIZED, always,
in W3. Turning an intent into an order is W4; nothing in W3 sends one.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

# decisions
BUY, SELL, HOLD, WAIT, NO_TRADE = "BUY", "SELL", "HOLD", "WAIT", "NO_TRADE"
DECISIONS = (BUY, SELL, HOLD, WAIT, NO_TRADE)
# actions
A_BUY, A_ADD, A_HOLD, A_REDUCE, A_EXIT, A_SELL, A_NONE, A_BLOCKED = (
    "BUY", "ADD", "HOLD", "REDUCE", "EXIT", "SELL", "NO_ACTION", "BLOCKED_BY_RISK")
ACTIONS = (A_BUY, A_ADD, A_HOLD, A_REDUCE, A_EXIT, A_SELL, A_NONE, A_BLOCKED)
ACTION_TO_DECISION = {A_BUY: BUY, A_ADD: BUY, A_HOLD: HOLD, A_REDUCE: SELL, A_EXIT: SELL, A_SELL: SELL,
                      A_NONE: WAIT, A_BLOCKED: NO_TRADE}
INTENT_ACTIONS = (A_BUY, A_ADD, A_REDUCE, A_EXIT)
SIGNAL_ENGINE_EQUIVALENT = {"BUY": BUY, "SELL": SELL, "HOLD": HOLD, "WAIT": WAIT}

RISK_REQUIREMENTS = ("STANDARD", "REDUCED", "STRICT")
NOT_AUTHORIZED = "NOT_AUTHORIZED"


@dataclass
class StrategyDecision:
    strategy_id: str
    strategy_version: str
    symbol: str
    as_of: date
    action: str
    confidence: float | None
    reasons: list
    regime: str | None = None
    parameters: dict = field(default_factory=dict)
    risk_requirement: str = "STANDARD"
    target_position_pct: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    max_hold_sessions: int | None = None
    features: dict = field(default_factory=dict)
    blocked_reason: str | None = None
    score: float | None = None
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:20])
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}")
        if self.risk_requirement not in RISK_REQUIREMENTS:
            raise ValueError(f"risk_requirement must be one of {RISK_REQUIREMENTS}")
        if self.confidence is not None:
            if not 0 <= self.confidence <= 1:
                raise ValueError("confidence must be within 0..1")
            if self.score is None:
                self.score = round(self.confidence * 100, 2)
        if isinstance(self.reasons, str):
            self.reasons = [self.reasons]

    @property
    def decision(self) -> str:
        return ACTION_TO_DECISION[self.action]

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def block(self, why: str):
        self.action = A_BLOCKED
        self.blocked_reason = why
        self.reasons = self.reasons + [f"blocked: {why}"]
        self.target_position_pct = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update({"decision": self.decision, "as_of": str(self.as_of), "timestamp": self.timestamp.isoformat()})
        return d


@dataclass
class PositionIntent:
    symbol: str
    side: str                           # BUY | SELL
    target_position_pct: float | None   # desired position as % of equity (0 = flat)
    quantity: int | None                # indicative size, or None -- sizing is confirmed by W4
    strategy_id: str
    strategy_version: str
    decision_id: str
    timestamp: datetime
    confidence: float | None
    reason: str
    action: str
    stop_price: float | None = None
    target_price: float | None = None
    max_hold_sessions: int | None = None
    risk_requirement: str = "STANDARD"
    authorization_status: str = NOT_AUTHORIZED
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:20])

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        self.authorization_status = NOT_AUTHORIZED     # never authorised in W3

    def as_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


def intent_for(dec: StrategyDecision, quantity: int | None = None) -> PositionIntent | None:
    """The PositionIntent for a decision that changes a position, else None."""
    if dec.action not in INTENT_ACTIONS:
        return None
    side = "BUY" if dec.action in (A_BUY, A_ADD) else "SELL"
    target = 0.0 if dec.action == A_EXIT else dec.target_position_pct
    return PositionIntent(symbol=dec.symbol, side=side, target_position_pct=target, quantity=quantity,
                          strategy_id=dec.strategy_id, strategy_version=dec.strategy_version,
                          decision_id=dec.decision_id, timestamp=dec.timestamp, confidence=dec.confidence,
                          reason=dec.reason[:1000], action=dec.action, stop_price=dec.stop_price,
                          target_price=dec.target_price, max_hold_sessions=dec.max_hold_sessions,
                          risk_requirement=dec.risk_requirement)


def risk_gate(dec: StrategyDecision, risk: dict, features: dict) -> StrategyDecision:
    """
    The strategy's OWN risk requirements, applied before a decision leaves the
    engine. Advisory: the W4 risk engine decides whether anything is executed.
    Only BUY / ADD can be blocked -- reducing risk is never blocked.

      risk.max_cri          block when the symbol's CRI is above it
      risk.blocked_regimes  block in these Market Health regimes
      kill switch           block while trading is halted (orders/risk.py, W1)
    """
    if dec.action not in (A_BUY, A_ADD):
        return dec
    why = []
    cri = features.get("cri")
    if risk.get("max_cri") is not None and cri is not None and cri > risk["max_cri"]:
        why.append(f"CRI {cri:.0f} > {risk['max_cri']}")
    regime = features.get("regime") or dec.regime
    if regime and regime in (risk.get("blocked_regimes") or []):
        why.append(f"regime {regime} is blocked for this strategy")
    try:
        from orders.risk import halted
        h, reason = halted()
        if h:
            why.append(f"trading halted: {reason}")
    except Exception:
        pass
    if why:
        dec.block("; ".join(why))
    return dec
