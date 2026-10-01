"""
The broker connectors (W37: BR-07). Read-only account access + payload mapping; see brokers/base.py.

    dhan     dhanhq SDK (already used by ATIP): holdings, positions, fund limits, trade book
    zerodha  kiteconnect SDK (installed): profile, holdings, positions, margins, trades
    upstox   REST v2 (https://api.upstox.com/v2), bearer access token; no SDK needed
    angel    Angel One SmartAPI REST (https://apiconnect.angelone.in): login by client code + PIN + TOTP
             (generated here from the stored TOTP secret) -> JWT for the session's calls

Response fields are read defensively (brokers rename fields between API versions); anything that does
not parse is skipped rather than guessed. Every network call has a timeout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

from brokers.base import (ORDER_TYPES, BrokerConnector, Holding, LiveOrderAdapter, Position, Trade, clean_symbol, f)

TIMEOUT = 20


def _side(v) -> str:
    s = str(v or "").upper()
    return "BUY" if s.startswith("B") else "SELL" if s.startswith("S") else s


# ── Dhan ──────────────────────────────────────────────────────────────────
class DhanConnector(BrokerConnector):
    name = "dhan"
    required_fields = ("client_id", "access_token")

    def _client(self):
        from dhanhq import DhanContext
        from data.dhan import DhanHQ
        return DhanHQ(DhanContext(self.fields["client_id"], self.fields["access_token"]))

    @staticmethod
    def _data(resp):
        if isinstance(resp, dict):
            if resp.get("status") == "failure":
                raise RuntimeError(f"Dhan: {str(resp.get('remarks') or resp)[:200]}")
            d = resp.get("data")
            return d if d is not None else []
        return resp or []

    def profile(self):
        fl = self._data(self._client().get_fund_limits())
        return {"client_id": self.fields["client_id"], "name": None,
                "available": f((fl or {}).get("availabelBalance") or (fl or {}).get("availableBalance"))}

    def holdings(self):
        out = []
        for h in self._data(self._client().get_holdings()) or []:
            q = f(h.get("totalQty") or h.get("availableQty"), 0)
            if q:
                out.append(Holding(clean_symbol(h.get("tradingSymbol")), q, f(h.get("avgCostPrice")),
                                   f(h.get("lastTradedPrice")), h.get("isin"), h.get("exchange") or "NSE",
                                   f(h.get("t1Qty"), 0)))
        return out

    def positions(self):
        return [Position(clean_symbol(p.get("tradingSymbol")), f(p.get("netQty"), 0), f(p.get("costPrice")),
                         None, p.get("productType"), f(p.get("realizedProfit"), 0) + f(p.get("unrealizedProfit"), 0))
                for p in self._data(self._client().get_positions()) or [] if f(p.get("netQty"), 0)]

    def funds(self):
        return self._data(self._client().get_fund_limits()) or {}

    def trades(self):
        return [Trade(str(t.get("exchangeTradeId") or t.get("orderId")), clean_symbol(t.get("tradingSymbol")),
                      _side(t.get("transactionType")), f(t.get("tradedQuantity"), 0), f(t.get("tradedPrice"), 0),
                      str(t.get("exchangeTime") or t.get("createTime") or ""), str(t.get("orderId")),
                      t.get("exchangeSegment"), t.get("productType"))
                for t in self._data(self._client().get_trade_book()) or []]


# ── Zerodha (Kite Connect) ────────────────────────────────────────────────
class ZerodhaConnector(BrokerConnector):
    name = "zerodha"
    required_fields = ("api_key", "access_token")

    def _kite(self):
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=self.fields["api_key"], timeout=TIMEOUT)
        k.set_access_token(self.fields["access_token"])
        return k

    def profile(self):
        p = self._kite().profile()
        return {"client_id": p.get("user_id"), "name": p.get("user_name"), "broker": "zerodha",
                "exchanges": p.get("exchanges")}

    def holdings(self):
        return [Holding(clean_symbol(h.get("tradingsymbol")), f(h.get("quantity"), 0) + f(h.get("t1_quantity"), 0),
                        f(h.get("average_price")), f(h.get("last_price")), h.get("isin"), h.get("exchange"),
                        f(h.get("t1_quantity"), 0))
                for h in self._kite().holdings() or [] if f(h.get("quantity"), 0) + f(h.get("t1_quantity"), 0)]

    def positions(self):
        return [Position(clean_symbol(p.get("tradingsymbol")), f(p.get("quantity"), 0), f(p.get("average_price")),
                         f(p.get("last_price")), p.get("product"), f(p.get("pnl")), p.get("exchange"))
                for p in (self._kite().positions() or {}).get("net", []) if f(p.get("quantity"), 0)]

    def funds(self):
        return self._kite().margins() or {}

    def trades(self):
        return [Trade(str(t.get("trade_id")), clean_symbol(t.get("tradingsymbol")), _side(t.get("transaction_type")),
                      f(t.get("quantity"), 0), f(t.get("average_price"), 0),
                      str(t.get("fill_timestamp") or t.get("exchange_timestamp") or ""), str(t.get("order_id")),
                      t.get("exchange"), t.get("product"))
                for t in self._kite().trades() or []]


# ── Upstox (REST v2) ──────────────────────────────────────────────────────
class UpstoxConnector(BrokerConnector):
    name = "upstox"
    required_fields = ("access_token",)
    BASE = "https://api.upstox.com/v2"

    def _get(self, path):
        import requests
        r = requests.get(self.BASE + path, timeout=TIMEOUT, headers={
            "Authorization": f"Bearer {self.fields['access_token']}", "Accept": "application/json"})
        js = r.json() if r.content else {}
        if r.status_code != 200 or js.get("status") == "error":
            raise RuntimeError(f"Upstox {path}: HTTP {r.status_code} {str(js.get('errors') or js)[:200]}")
        return js.get("data")

    def profile(self):
        p = self._get("/user/profile") or {}
        return {"client_id": p.get("user_id"), "name": p.get("user_name"), "broker": "upstox"}

    def holdings(self):
        return [Holding(clean_symbol(h.get("tradingsymbol") or h.get("trading_symbol")), f(h.get("quantity"), 0),
                        f(h.get("average_price")), f(h.get("last_price")), h.get("isin"), h.get("exchange"),
                        f(h.get("t1_quantity"), 0))
                for h in self._get("/portfolio/long-term-holdings") or [] if f(h.get("quantity"), 0)]

    def positions(self):
        return [Position(clean_symbol(p.get("tradingsymbol") or p.get("trading_symbol")), f(p.get("quantity"), 0),
                         f(p.get("average_price")), f(p.get("last_price")), p.get("product"), f(p.get("pnl")),
                         p.get("exchange"))
                for p in self._get("/portfolio/short-term-positions") or [] if f(p.get("quantity"), 0)]

    def funds(self):
        return self._get("/user/get-funds-and-margin") or {}

    def trades(self):
        return [Trade(str(t.get("trade_id")), clean_symbol(t.get("tradingsymbol") or t.get("trading_symbol")),
                      _side(t.get("transaction_type")), f(t.get("quantity"), 0), f(t.get("average_price"), 0),
                      str(t.get("exchange_timestamp") or ""), str(t.get("order_id")), t.get("exchange"),
                      t.get("product"), extra={"instrument_token": t.get("instrument_token")})
                for t in self._get("/order/trades/get-trades-for-day") or []]


# ── Angel One (SmartAPI REST) ─────────────────────────────────────────────
def totp(secret: str, step=30, digits=6) -> str:
    s = secret.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    h = hmac.new(key, struct.pack(">Q", int(time.time()) // step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


class AngelConnector(BrokerConnector):
    name = "angel"
    required_fields = ("client_id", "api_key", "pin", "totp_secret")
    BASE = "https://apiconnect.angelone.in"

    def _headers(self, jwt=None):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER",
             "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.fields["api_key"]}
        if jwt:
            h["Authorization"] = f"Bearer {jwt}"
        return h

    def _jwt(self):
        if getattr(self, "_token", None):
            return self._token
        import requests
        r = requests.post(self.BASE + "/rest/auth/angelbroking/user/v1/loginByPassword", timeout=TIMEOUT,
                          headers=self._headers(), json={"clientcode": self.fields["client_id"],
                                                         "password": self.fields["pin"],
                                                         "totp": totp(self.fields["totp_secret"])})
        js = r.json() if r.content else {}
        tok = ((js or {}).get("data") or {}).get("jwtToken")
        if not tok:
            raise RuntimeError(f"Angel login failed: {str(js.get('message') or js)[:200]}")
        self._token = tok
        return tok

    def _call(self, method, path):
        import requests
        r = requests.request(method, self.BASE + path, timeout=TIMEOUT, headers=self._headers(self._jwt()))
        js = r.json() if r.content else {}
        if r.status_code != 200 or js.get("status") is False:
            raise RuntimeError(f"Angel {path}: HTTP {r.status_code} {str(js.get('message') or js)[:200]}")
        return js.get("data")

    def profile(self):
        p = self._call("GET", "/rest/secure/angelbroking/user/v1/getProfile") or {}
        return {"client_id": p.get("clientcode"), "name": p.get("name"), "broker": "angel"}

    def holdings(self):
        d = self._call("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding") or []
        return [Holding(clean_symbol(h.get("tradingsymbol")), f(h.get("quantity"), 0), f(h.get("averageprice")),
                        f(h.get("ltp")), h.get("isin"), h.get("exchange"), f(h.get("t1quantity"), 0))
                for h in d if f(h.get("quantity"), 0)]

    def positions(self):
        d = self._call("GET", "/rest/secure/angelbroking/order/v1/getPosition") or []
        return [Position(clean_symbol(p.get("tradingsymbol")), f(p.get("netqty"), 0), f(p.get("netprice")),
                         f(p.get("ltp")), p.get("producttype"), f(p.get("pnl")), p.get("exchange"))
                for p in d if f(p.get("netqty"), 0)]

    def funds(self):
        return self._call("GET", "/rest/secure/angelbroking/user/v1/getRMS") or {}

    def trades(self):
        d = self._call("GET", "/rest/secure/angelbroking/order/v1/getTradeBook") or []
        return [Trade(str(t.get("fillid") or t.get("orderid")), clean_symbol(t.get("tradingsymbol")),
                      _side(t.get("transactiontype")), f(t.get("fillsize"), 0), f(t.get("fillprice"), 0),
                      str(t.get("filltime") or ""), str(t.get("orderid")), t.get("exchange"), t.get("producttype"))
                for t in d]


# ── order payload mappers (LIVE refused; see base.LiveOrderAdapter) ───────
class DhanOrderAdapter(LiveOrderAdapter):
    name, broker = "live_dhan", "dhan"

    def build_payload(self, o):
        return {"transaction_type": o["side"], "exchange_segment": "NSE_EQ", "product_type": o.get("product_type") or
                "CNC", "order_type": {"SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"}.get(o["order_type"], o["order_type"]),
                "validity": "DAY", "security_id": "<from the Dhan security master for " + o["symbol"] + ">",
                "quantity": int(o["quantity"]), "price": o.get("limit_price") or 0,
                "trigger_price": o.get("trigger_price") or 0, "tag": o["order_id"][:25]}


class ZerodhaOrderAdapter(LiveOrderAdapter):
    name, broker = "live_zerodha", "zerodha"

    def build_payload(self, o):
        return {"variety": "regular", "exchange": "NSE", "tradingsymbol": o["symbol"], "transaction_type": o["side"],
                "quantity": int(o["quantity"]), "product": o.get("product_type") or "CNC",
                "order_type": ORDER_TYPES[o["order_type"]], "price": o.get("limit_price"),
                "trigger_price": o.get("trigger_price"), "validity": "DAY", "tag": o["order_id"][:20]}


class UpstoxOrderAdapter(LiveOrderAdapter):
    name, broker = "live_upstox", "upstox"

    def build_payload(self, o):
        return {"quantity": int(o["quantity"]), "product": "D" if (o.get("product_type") or "CNC") == "CNC" else "I",
                "validity": "DAY", "price": o.get("limit_price") or 0, "tag": o["order_id"][:20],
                "instrument_token": f"NSE_EQ|<ISIN of {o['symbol']}>", "order_type": ORDER_TYPES[o["order_type"]],
                "transaction_type": o["side"], "disclosed_quantity": 0, "trigger_price": o.get("trigger_price") or 0,
                "is_amo": False}


class AngelOrderAdapter(LiveOrderAdapter):
    name, broker = "live_angel", "angel"

    def build_payload(self, o):
        return {"variety": "STOPLOSS" if o["order_type"] in ("SL", "SL-M") else "NORMAL", "tradingsymbol": f"{o['symbol']}-EQ",
                "symboltoken": f"<Angel token for {o['symbol']}>", "transactiontype": o["side"], "exchange": "NSE",
                "ordertype": {"SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}.get(o["order_type"], o["order_type"]),
                "producttype": "DELIVERY" if (o.get("product_type") or "CNC") == "CNC" else "INTRADAY",
                "duration": "DAY", "price": str(o.get("limit_price") or 0), "triggerprice": str(o.get("trigger_price") or 0),
                "quantity": str(int(o["quantity"])), "ordertag": o["order_id"][:20]}
