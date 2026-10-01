"""
Broker-independent execution interface.

    BrokerAdapter            submit / cancel / status -> BrokerResult
      PaperBrokerAdapter     orders/paper.py PaperBroker: simulated fills priced
                             from the live Dhan quote (or the decision close, when
                             execution.paper_fill_price is "reference"). No code
                             path here reaches Dhan's trading API.
      DhanBrokerAdapter      placeholder for LIVE. Every call raises
                             LiveTradingDisabled in W4: live execution is not
                             built, whatever the configuration says.

The strategy engine never imports this module; only the order manager does.
Every paper order is also written to order_log (mode PAPER, status PLACED), so
the W1 limit max_orders_per_day keeps counting it.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from execution.config import LIVE, PAPER, execution_settings, live_gate
from execution.errors import BrokerError, LiveTradingDisabled
from execution.models import BrokerResult

log = logging.getLogger("atip.execution")


class BrokerAdapter(ABC):
    name = "abstract"
    mode = None

    @abstractmethod
    def submit(self, order: dict) -> BrokerResult: ...

    @abstractmethod
    def cancel(self, order: dict) -> BrokerResult: ...

    @abstractmethod
    def status(self, order: dict) -> BrokerResult: ...

    def modify(self, order: dict, changes: dict) -> BrokerResult:      # W29 (EX-08)
        raise BrokerError(f"{self.name} adapter does not support modify")


class PaperBrokerAdapter(BrokerAdapter):
    name, mode = "paper", PAPER

    def __init__(self, conn):
        self.conn = conn
        self.fill_price = execution_settings()["paper_fill_price"]

    def _broker(self):
        from orders.paper import PaperBroker
        quotes = None
        if self.fill_price == "live":
            try:
                from data.dhan import get_dhan_client
                quotes, _ = get_dhan_client()        # market data only
            except Exception as e:
                log.warning(f"  paper adapter: no Dhan market-data client ({e})")
        return PaperBroker(quote_source=quotes, conn=self.conn)

    def submit(self, order: dict) -> BrokerResult:
        b = self._broker()
        ref = order.get("reference_price") if self.fill_price == "reference" else None
        try:
            resp = b.place_order(security_id=None, exchange_segment="NSE_EQ", transaction_type=order["side"],
                                 quantity=int(order["quantity"]), order_type=order["order_type"],
                                 product_type=order.get("product_type") or "CNC",
                                 price=order.get("limit_price") or 0, symbol=order["symbol"],
                                 tag=order["order_id"], reference_price=ref,
                                 trigger_price=order.get("trigger_price"))
        except Exception as e:
            raise BrokerError(f"paper broker raised: {e}") from e
        data = (resp or {}).get("data") or {}
        oid = data.get("orderId")
        st = data.get("orderStatus")
        self._order_log(order, "PLACED" if resp.get("status") == "success" and st != "REJECTED" else "FAILED",
                        oid, None if resp.get("status") == "success" else str(resp.get("remarks")))
        if resp.get("status") != "success" or st == "REJECTED":
            rem = resp.get("remarks")
            msg = rem.get("error_message") if isinstance(rem, dict) else rem
            return BrokerResult("REJECTED", oid, message=str(msg or data.get("reason") or resp), raw=resp)
        fees = self._fees(oid)
        src = "decision close" if ref else "live LTP"
        if st == "TRADED":
            return BrokerResult("FILLED", oid, int(data.get("filledQty") or order["quantity"]),
                                data.get("averageTradedPrice"), fees, src, raw=resp)
        if st == "PARTIALLY_FILLED":
            return BrokerResult("PARTIALLY_FILLED", oid, int(data.get("filledQty") or 0),
                                data.get("averageTradedPrice"), fees, src, raw=resp)
        return BrokerResult("ACCEPTED", oid, message=f"paper order {st}", raw=resp)

    def _fees(self, broker_order_id):
        try:
            r = self.conn.execute("SELECT brokerage FROM paper_order WHERE order_id=?", (broker_order_id,)).fetchone()
            return float(r[0] or 0) if r else 0.0
        except Exception:
            return 0.0

    def _order_log(self, order, status, broker_order_id, error):
        try:
            from orders.broker import _ensure_order_log_table, _log_order
            _ensure_order_log_table(self.conn)
            _log_order(self.conn, order["symbol"], order["side"], int(order["quantity"]), order["order_type"],
                       order.get("product_type") or "CNC", order.get("limit_price") or 0,
                       (order.get("reference_price") or 0) * int(order["quantity"]), None, PAPER, status,
                       order_id=broker_order_id, error=error)
        except Exception as e:
            log.warning(f"  order_log write failed for {order['order_id']}: {e}")

    def cancel(self, order: dict) -> BrokerResult:
        b = self._broker()
        resp = b.cancel_order(order["broker_order_id"])
        ok = (resp or {}).get("status") == "success"
        return BrokerResult("CANCELLED" if ok else "ERROR", order["broker_order_id"],
                            message=(resp or {}).get("remarks"), raw=resp)

    def modify(self, order: dict, changes: dict) -> BrokerResult:
        b = self._broker()
        resp = b.modify_order(order["broker_order_id"], order_type=changes.get("order_type"),
                              quantity=changes.get("quantity"), price=changes.get("limit_price"),
                              trigger_price=changes.get("trigger_price"))
        if (resp or {}).get("status") != "success":
            rem = (resp or {}).get("remarks")
            return BrokerResult("REJECTED", order["broker_order_id"],
                                message=str(rem.get("error_message") if isinstance(rem, dict) else rem), raw=resp)
        return self.status(order)

    def status(self, order: dict) -> BrokerResult:
        b = self._broker()
        resp = b.get_order_by_id(order["broker_order_id"])
        data = (resp or {}).get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        st = data.get("orderStatus") or data.get("status")
        mapping = {"TRADED": "FILLED", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "PENDING": "ACCEPTED",
                   "CANCELLED": "CANCELLED", "REJECTED": "REJECTED"}
        return BrokerResult(mapping.get(st, "ACCEPTED"), order["broker_order_id"],
                            int(data.get("filledQty") or data.get("filled_qty") or 0),
                            data.get("averageTradedPrice") or data.get("fill_price"), raw=resp)


class FuturesPaperAdapter(BrokerAdapter):
    """W30 (QR-05 / QR-06): short legs on the paper stock-futures book
    (execution/futures_paper.py). Fills at the latest end-of-day futures close."""
    name, mode = "paper_fut", PAPER

    def __init__(self, conn):
        self.conn = conn

    def submit(self, order: dict) -> BrokerResult:
        from execution.futures_paper import fill
        try:
            r = fill(self.conn, order)
        except Exception as e:
            raise BrokerError(f"paper futures book raised: {e}") from e
        if r["status"] != "FILLED":
            return BrokerResult("REJECTED", None, message=r.get("message"), raw=r)
        return BrokerResult("FILLED", "FUT-" + order["order_id"], int(r["filled_qty"]), r["price"], r["fees"],
                            "EOD futures close", message=r.get("message"), raw=r)

    def cancel(self, order: dict) -> BrokerResult:
        return BrokerResult("ERROR", order.get("broker_order_id"), message="futures paper orders fill at once")

    def status(self, order: dict) -> BrokerResult:
        return BrokerResult("FILLED", order.get("broker_order_id"), int(order.get("filled_quantity") or 0),
                            order.get("avg_fill_price"), order.get("fees") or 0, "EOD futures close")


class DhanBrokerAdapter(BrokerAdapter):
    """LIVE placeholder. Refuses every call in W4 -- no order can reach Dhan
    through the W4 execution layer."""
    name, mode = "dhan", LIVE

    def _refuse(self, what):
        ok, why = live_gate()
        raise LiveTradingDisabled(f"{what}: live execution is not implemented in W4 "
                                  f"(live gate: {'open' if ok else 'closed'} — {why})")

    def submit(self, order):
        self._refuse("submit")

    def cancel(self, order):
        self._refuse("cancel")

    def status(self, order):
        self._refuse("status")

    def modify(self, order, changes):
        self._refuse("modify")


def get_adapter(conn, mode: str, instrument: str | None = None) -> BrokerAdapter:
    if mode == PAPER and (instrument or "CASH") == "FUT":
        return FuturesPaperAdapter(conn)
    if mode == PAPER:
        return PaperBrokerAdapter(conn)
    if mode == LIVE:
        return DhanBrokerAdapter()
    raise BrokerError(f"unknown execution mode {mode!r}")
