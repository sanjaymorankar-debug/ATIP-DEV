"""
Broker connectors behind one interface (W37: BR-07; extends the W4 BR-03 BrokerAdapter).

Two halves per broker:

  BrokerConnector  READ-ONLY account access -- profile(), holdings(), positions(), funds(), trades()
                   -- normalised to the dataclasses below. This is what the multi-broker import (PF-12),
                   credential verification (ENT-06) and the consolidated holdings view use. A connector
                   has no method that can place, modify or cancel an order.
  LiveOrderAdapter the W4 BrokerAdapter for the broker's order API. build_payload(order) maps an ATIP OMS
                   order to the broker's request body (testable, previewable). submit / cancel / modify /
                   status REFUSE with LiveTradingDisabled: LIVE execution is not built for any broker in
                   ATIP (the Dhan adapter has refused since W4), and enabling it is an owner + legal decision
                   (ENT-14), not a code default.

Credentials come only from enterprise.vault.credentials_for() (vault entry, or the owner's existing
configuration for brokers ATIP already used). Connectors are constructed per call and keep nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

from execution.adapters import BrokerAdapter
from execution.errors import LiveTradingDisabled
from execution.models import BrokerResult


@dataclass
class Holding:
    symbol: str
    quantity: float
    avg_price: float | None
    ltp: float | None = None
    isin: str | None = None
    exchange: str | None = "NSE"
    t1_quantity: float = 0.0


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_price: float | None
    ltp: float | None = None
    product: str | None = None
    pnl: float | None = None
    exchange: str | None = "NSE"


@dataclass
class Trade:
    trade_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    ts: str | None = None
    order_id: str | None = None
    exchange: str | None = "NSE"
    product: str | None = None
    isin: str | None = None
    extra: dict = field(default_factory=dict)


def as_dicts(items) -> list:
    return [asdict(x) for x in items]


def f(v, default=None):
    try:
        x = float(v)
        return default if x != x else x
    except (TypeError, ValueError):
        return default


def clean_symbol(s: str) -> str:
    """'RELIANCE-EQ' / 'NSE:RELIANCE' / 'reliance' -> 'RELIANCE'."""
    s = (s or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for suf in ("-EQ", "-BE", "-BZ", "-SM", "-ST"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


class BrokerConnector(ABC):
    name = "base"
    required_fields: tuple = ()

    def __init__(self, fields: dict):
        missing = [k for k in self.required_fields if not fields.get(k)]
        if missing:
            raise ValueError(f"{self.name}: missing credential fields {missing}")
        self.fields = fields

    @abstractmethod
    def profile(self) -> dict: ...

    @abstractmethod
    def holdings(self) -> list: ...

    @abstractmethod
    def positions(self) -> list: ...

    @abstractmethod
    def funds(self) -> dict: ...

    @abstractmethod
    def trades(self) -> list:
        """Today's executed trades (the broker's trade book)."""


ORDER_TYPES = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "SL", "SL-M": "SL-M"}


class LiveOrderAdapter(BrokerAdapter):
    """Payload mapping only; every call to the broker is refused (see module docstring)."""
    name = "live_base"
    broker = "base"

    def build_payload(self, order: dict) -> dict:
        raise NotImplementedError

    def _refuse(self, what):
        raise LiveTradingDisabled(f"{self.broker}: LIVE {what} is not built in ATIP (payload preview only; enabling "
                                  f"live orders needs the owner's decision and the ENT-14 legal review)")

    def submit(self, order: dict) -> BrokerResult:
        self._refuse("order placement")

    def cancel(self, order: dict) -> BrokerResult:
        self._refuse("order cancellation")

    def status(self, order: dict) -> BrokerResult:
        self._refuse("order status")

    def modify(self, order: dict, changes: dict) -> BrokerResult:
        self._refuse("order modification")
