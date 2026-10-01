"""
A simulated Dhan broker for real-time testing.

Why this exists
---------------
Dhan publishes no sandbox. A host at `sandbox.dhan.co` does serve the `/v2/*`
routes — it answers with Dhan's own `DH-906 Invalid Token` JSON rather than a
Tomcat 404, unlike `/fundlimit` or `/api/v2/*` — so it is a real API deployment
with its own token namespace. It is undocumented, a production token is rejected
there, and `api-test.dhan.co` is a dead backend (502). Getting a token for it
means asking Dhan.

So the only way to exercise the order path today, in real time, without risking
real money is to simulate the broker while keeping the market real. That is what
this does:

  * **Prices are real.** Fills are priced from live Dhan quotes, fetched through
    the read-only market-data API. Nothing here is a made-up price path.
  * **Execution is simulated.** No order ever reaches Dhan's trading API. There
    is no code path in this module that can.
  * **The interface is identical.** Method names, arguments and response shapes
    match the dhanhq client, including Dhan's own `availabelBalance` typo, so
    swapping this in exercises the *real* calling code rather than a parallel
    branch that only exists in tests.

`broker_env` in atip_data/config.json chooses: PAPER (this), SANDBOX (a real
Dhan client pointed at sandbox.dhan.co, once you have a token for it), or LIVE.
Anything missing or unrecognised resolves to PAPER — see orders/environment.py.

Deliberate pessimism
--------------------
Slippage is always adverse, partial fills are possible, and a MARKET order is
filled at the live LTP moved against you. A simulator that fills better than
reality teaches the wrong lesson and hides exactly the failures worth catching.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from datetime import datetime
from pathlib import Path

from db.schema import get_connection

log = logging.getLogger("atip.paper")

CONFIG_PATH = Path("atip_data") / "config.json"

DEFAULTS = {
    "paper_opening_balance": 500000.0,
    "paper_slippage_bps": 5.0,      # 0.05% — always against the order
    "paper_brokerage_pct": 0.05,    # per leg
    "paper_partial_fill_odds": 0.0, # 0 = always fill in full; raise to test partials
    "paper_reject_odds": 0.0,       # raise to exercise rejection handling
}


def settings(overrides: dict = None) -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    except Exception:
        pass
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if k in DEFAULTS})
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════════════════

def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_order (
            order_id       TEXT PRIMARY KEY,
            created_at     TEXT,
            symbol         TEXT,
            security_id    TEXT,
            exchange       TEXT,
            transaction_type TEXT,
            quantity       INTEGER,
            filled_qty     INTEGER DEFAULT 0,
            order_type     TEXT,
            product_type   TEXT,
            limit_price    REAL,
            fill_price     REAL,
            status         TEXT,
            reason         TEXT,
            brokerage      REAL DEFAULT 0,
            tag            TEXT
        )
    """)
    # Additive migration, matching the pattern used everywhere else here.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_order)").fetchall()}
        if "tag" not in cols:
            conn.execute("ALTER TABLE paper_order ADD COLUMN tag TEXT")
        # W29 (EX-02 / EX-08): stop orders and modifications
        for col, typ in (("trigger_price", "REAL"), ("triggered_at", "TEXT"), ("updated_at", "TEXT"),
                         ("modifications", "INTEGER DEFAULT 0")):
            if col not in cols:
                conn.execute(f"ALTER TABLE paper_order ADD COLUMN {col} {typ}")
    except Exception as e:
        log.warning(f"  paper_order migration skipped: {e}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_position (
            symbol       TEXT PRIMARY KEY,
            quantity     INTEGER DEFAULT 0,
            avg_price    REAL DEFAULT 0,
            realized_pnl REAL DEFAULT 0,
            updated_at   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_account (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            balance  REAL NOT NULL,
            opened_at TEXT
        )
    """)
    conn.commit()


def _now():
    return datetime.now().isoformat()


class PaperBroker:
    """
    Drop-in stand-in for the dhanhq client, for order flow only.

    Market data is NOT simulated — `quote_source` is the real client, so quotes,
    OHLC and historical data all still come from Dhan. Only execution is fake.
    """

    # Declares that place_order() accepts a `symbol` kwarg. dhanhq's does not,
    # so callers test this flag rather than passing it blindly and breaking LIVE.
    accepts_symbol = True

    def __init__(self, quote_source=None, conn=None, overrides: dict = None):
        self.cfg = settings(overrides)
        self.quote_source = quote_source
        self._own_conn = conn is None
        self.conn = conn or get_connection()
        ensure_tables(self.conn)
        row = self.conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
        if not row:
            self.conn.execute("INSERT INTO paper_account (id, balance, opened_at) VALUES (1,?,?)",
                              (float(self.cfg["paper_opening_balance"]), _now()))
            self.conn.commit()

    # ── plumbing ──────────────────────────────────────────────────────────
    def close(self):
        if self._own_conn:
            self.conn.close()

    @property
    def balance(self) -> float:
        return float(self.conn.execute(
            "SELECT balance FROM paper_account WHERE id=1").fetchone()[0])

    def _adjust_balance(self, delta: float):
        self.conn.execute("UPDATE paper_account SET balance=balance+? WHERE id=1", (delta,))

    # Dhan's Quote API allows ONE request per second. A single order already
    # costs two quote calls (the value estimate, then the fill), so two orders
    # in a row are throttled and the second is refused for "no live quote" —
    # observed on the very first end-to-end run. A short cache plus one paced
    # retry fixes it without pretending a stale price is fresh.
    QUOTE_TTL_SECONDS = 2.0
    _quote_cache: dict = {}

    def _ltp(self, symbol: str) -> float | None:
        """Live last-traded price from the REAL market-data API."""
        now = time.monotonic()
        hit = self._quote_cache.get(symbol)
        if hit and now - hit[0] < self.QUOTE_TTL_SECONDS:
            return hit[1]

        from data.dhan import fetch_live_quotes
        for attempt in (1, 2):
            try:
                q = fetch_live_quotes([symbol], self.quote_source)
                if q is not None and not q.empty and q.iloc[0].get("ltp"):
                    ltp = float(q.iloc[0]["ltp"])
                    self._quote_cache[symbol] = (time.monotonic(), ltp)
                    return ltp
            except Exception as e:
                log.warning(f"  paper: live quote for {symbol} failed: {e}")
            if attempt == 1:
                time.sleep(1.1)      # clear the 1/sec quote limit, then retry once
        return None

    def _slipped(self, price: float, transaction_type: str) -> float:
        """Slippage always hurts: a buy fills higher, a sell fills lower."""
        bps = float(self.cfg["paper_slippage_bps"]) / 10000.0
        return round(price * (1 + bps) if transaction_type == "BUY" else price * (1 - bps), 2)

    # ── dhanhq-compatible surface ─────────────────────────────────────────
    def get_fund_limits(self) -> dict:
        bal = self.balance
        # Mirrors the live payload exactly, Dhan's "availabelBalance" spelling
        # included — available_balance() reads that key, and "fixing" it here
        # would make the simulator disagree with production.
        return {"status": "success", "remarks": "", "data": {
            "dhanClientId": "PAPER", "availabelBalance": round(bal, 2),
            "sodLimit": round(bal, 2), "collateralAmount": 0.0,
            "receiveableAmount": 0.0, "utilizedAmount": 0.0,
            "blockedPayoutAmount": 0.0, "withdrawableBalance": round(bal, 2)}}

    ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M", "STOP_LOSS", "STOP_LOSS_MARKET")

    @staticmethod
    def _norm_type(order_type):
        t = str(order_type or "MARKET").upper()
        return {"STOP_LOSS": "SL", "STOP_LOSS_MARKET": "SL-M", "SLM": "SL-M"}.get(t, t)

    @staticmethod
    def _triggered(side, ltp, trigger) -> bool:
        """A stop BUY triggers at or above its trigger, a stop SELL at or below."""
        return ltp >= trigger if side == "BUY" else ltp <= trigger

    def place_order(self, security_id=None, exchange_segment=None,
                    transaction_type=None, quantity=None, order_type="MARKET",
                    product_type="CNC", price=0, symbol=None, tag=None,
                    reference_price=None, trigger_price=None, **kw) -> dict:
        """
        Simulate an order against the live price.

        Returns the dhanhq response shape, so the caller cannot tell the
        difference — which is the point: the real code path gets exercised.
        """
        qty = int(quantity or 0)
        sym = symbol or self._symbol_for(security_id) or str(security_id)
        oid = f"PAPER{uuid.uuid4().hex[:10].upper()}"
        order_type = self._norm_type(order_type)
        if order_type not in ("MARKET", "LIMIT", "SL", "SL-M"):
            return self._reject(oid, sym, security_id, exchange_segment, transaction_type, qty, order_type,
                                product_type, price, f"unsupported order type {order_type}", tag)
        if order_type in ("SL", "SL-M") and qty > 0:
            # W29 (EX-02): stop orders rest until the trigger is touched; SL then becomes a
            # LIMIT at `price`, SL-M a MARKET. Validated like Dhan: a SL BUY's limit must be
            # at or above its trigger, a SL SELL's at or below.
            trig = float(trigger_price or 0)
            if trig <= 0:
                return self._reject(oid, sym, security_id, exchange_segment, transaction_type, qty, order_type,
                                    product_type, price, "a stop order needs trigger_price > 0", tag)
            if order_type == "SL":
                lim = float(price or 0)
                if lim <= 0 or (transaction_type == "BUY" and lim < trig) or \
                        (transaction_type == "SELL" and lim > trig):
                    return self._reject(oid, sym, security_id, exchange_segment, transaction_type, qty, order_type,
                                        product_type, price, "SL limit price must be on the far side of the "
                                        "trigger (BUY: limit >= trigger, SELL: limit <= trigger)", tag)
            ltp_now = float(reference_price) if reference_price else self._ltp(sym)
            if ltp_now is None or not self._triggered(transaction_type, ltp_now, trig):
                self._write_order(oid, sym, security_id, exchange_segment, transaction_type, qty, 0, order_type,
                                  product_type, float(price or 0), None, "PENDING", "awaiting trigger", 0.0, tag)
                self.conn.execute("UPDATE paper_order SET trigger_price=? WHERE order_id=?", (trig, oid))
                self.conn.commit()
                return {"status": "success", "remarks": "", "data": {"orderId": oid, "orderStatus": "PENDING"}}
            # already through the trigger: SL-M fills now, SL is a LIMIT from here
            order_type = "LIMIT" if order_type == "SL" else "MARKET"

        if qty <= 0:
            return self._reject(oid, sym, security_id, exchange_segment, transaction_type,
                                qty, order_type, product_type, price,
                                "quantity must be > 0", tag)

        if random.random() < float(self.cfg["paper_reject_odds"]):
            return self._reject(oid, sym, security_id, exchange_segment, transaction_type,
                                qty, order_type, product_type, price,
                                "simulated broker rejection", tag)

        # `reference_price` is the price the CALLER acted on. Without it the
        # broker fetches its own quote, so a decision made at one price fills at
        # another — and the two can disagree badly. Seen end to end: a trailing
        # stop that triggered at 1398.22 filled at 1289.85, turning a +12% runner
        # into a booked loss. In live use the two prices are the same tick; the
        # gap only appears under replay or a fast market, which is precisely when
        # a silent mismatch is most damaging.
        ltp = float(reference_price) if reference_price else self._ltp(sym)
        if ltp is None:
            # No live price means no honest fill. Refusing is correct: inventing
            # one would make the simulator disagree with the market at exactly
            # the moment the market is unavailable.
            return self._reject(oid, sym, security_id, exchange_segment, transaction_type,
                                qty, order_type, product_type, price,
                                "no live quote available — cannot price the fill", tag)

        if order_type == "LIMIT":
            lim = float(price or 0)
            crossed = (ltp <= lim) if transaction_type == "BUY" else (ltp >= lim)
            if not crossed:
                self._write_order(oid, sym, security_id, exchange_segment, transaction_type,
                                  qty, 0, order_type, product_type, lim, None,
                                  "PENDING", "limit not crossed", 0.0, tag)
                return {"status": "success", "remarks": "",
                        "data": {"orderId": oid, "orderStatus": "PENDING"}}
            fill = self._slipped(min(lim, ltp) if transaction_type == "BUY"
                                 else max(lim, ltp), transaction_type)
            # W29: a LIMIT never fills worse than its limit -- slippage used to push a BUY
            # limited at 3000 (market 2999) to 3000.50
            fill = min(fill, lim) if transaction_type == "BUY" else max(fill, lim)
        else:
            fill = self._slipped(ltp, transaction_type)

        filled = qty
        if random.random() < float(self.cfg["paper_partial_fill_odds"]) and qty > 1:
            filled = max(1, qty // 2)

        value = fill * filled
        brokerage = round(value * float(self.cfg["paper_brokerage_pct"]) / 100.0, 2)

        if transaction_type == "BUY":
            if self.balance < value + brokerage:
                return self._reject(oid, sym, security_id, exchange_segment, transaction_type,
                                    qty, order_type, product_type, price,
                                    f"insufficient paper funds: have {self.balance:,.2f}, "
                                    f"need {value + brokerage:,.2f}", tag)
            self._adjust_balance(-(value + brokerage))
        else:
            held = self._position_qty(sym)
            if held < filled:
                return self._reject(oid, sym, security_id, exchange_segment, transaction_type,
                                    qty, order_type, product_type, price,
                                    f"cannot sell {filled}, paper position holds {held}", tag)
            self._adjust_balance(value - brokerage)

        self._apply_position(sym, transaction_type, filled, fill)
        status = "TRADED" if filled == qty else "PARTIALLY_FILLED"
        self._write_order(oid, sym, security_id, exchange_segment, transaction_type,
                          qty, filled, order_type, product_type,
                          float(price or 0), fill, status, None, brokerage, tag)
        self.conn.commit()
        log.info(f"  [PAPER] {transaction_type} {filled}/{qty} {sym} @ {fill} "
                 f"(ltp {ltp}, brokerage {brokerage}) -> {oid}")
        return {"status": "success", "remarks": "",
                "data": {"orderId": oid, "orderStatus": status,
                         "averageTradedPrice": fill, "filledQty": filled}}

    def _reject(self, oid, sym, sec, exch, ttype, qty, otype, ptype, price, reason,
                tag=None) -> dict:
        self._write_order(oid, sym, sec, exch, ttype, qty, 0, otype, ptype,
                          float(price or 0), None, "REJECTED", reason, 0.0, tag)
        self.conn.commit()
        log.warning(f"  [PAPER] REJECTED {ttype} {qty} {sym}: {reason}")
        return {"status": "failure",
                "remarks": {"error_code": "PAPER-REJECT", "error_message": reason},
                "data": {"orderId": oid, "orderStatus": "REJECTED"}}

    def cancel_order(self, order_id: str) -> dict:
        cur = self.conn.execute(
            "UPDATE paper_order SET status='CANCELLED' WHERE order_id=? AND status='PENDING'",
            (order_id,))
        self.conn.commit()
        if cur.rowcount:
            return {"status": "success", "data": {"orderId": order_id, "orderStatus": "CANCELLED"}}
        return {"status": "failure",
                "remarks": {"error_message": "no pending paper order with that id"},
                "data": ""}

    def modify_order(self, order_id, order_type=None, leg_name=None, quantity=None, price=None,
                     trigger_price=None, disclosed_quantity=None, validity=None, **kw) -> dict:
        """W29 (EX-08): change a PENDING order's quantity / price / trigger (dhanhq-shaped).
        A modified order is re-checked against the live price at once, like a broker would."""
        r = self.conn.execute("SELECT * FROM paper_order WHERE order_id=?", (order_id,)).fetchone()
        if not r or r["status"] != "PENDING":
            return {"status": "failure", "remarks": {"error_message": "only a PENDING paper order can be modified"},
                    "data": ""}
        r = dict(r)
        otype = self._norm_type(order_type or r["order_type"])
        qty = int(quantity if quantity is not None else r["quantity"])
        lim = float(price) if price is not None else r["limit_price"]
        trig = float(trigger_price) if trigger_price is not None else r.get("trigger_price")
        if qty <= 0:
            return {"status": "failure", "remarks": {"error_message": "quantity must be > 0"}, "data": ""}
        if otype not in ("LIMIT", "SL", "SL-M"):
            return {"status": "failure", "remarks": {"error_message": f"cannot modify to {otype}"}, "data": ""}
        if otype in ("SL", "SL-M") and not trig:
            return {"status": "failure", "remarks": {"error_message": "a stop order needs trigger_price"}, "data": ""}
        if otype in ("LIMIT", "SL") and not lim:
            return {"status": "failure", "remarks": {"error_message": "a limit price is required"}, "data": ""}
        if otype == "SL" and ((r["transaction_type"] == "BUY" and lim < trig) or
                              (r["transaction_type"] == "SELL" and lim > trig)):
            return {"status": "failure", "remarks": {"error_message": "SL limit on the wrong side of the trigger"},
                    "data": ""}
        self.conn.execute("UPDATE paper_order SET order_type=?, quantity=?, limit_price=?, trigger_price=?, "
                          "updated_at=?, modifications=COALESCE(modifications,0)+1 WHERE order_id=?",
                          (otype, qty, lim, trig, _now(), order_id))
        self.conn.commit()
        self.match_pending(order_ids=[order_id])
        st = self.get_order_by_id(order_id)["data"]["status"]
        return {"status": "success", "data": {"orderId": order_id, "orderStatus": st}}

    def match_pending(self, prices: dict | None = None, order_ids=None) -> list:
        """
        W29 (fixes W4-R4): re-check every PENDING order against the current price and fill
        what has crossed -- a resting LIMIT used to stay ACKNOWLEDGED forever. `prices`
        ({symbol: ltp}) lets the caller supply fresh quotes (the scheduler passes the latest
        live_quotes); a symbol without one is priced from the Dhan quote API.
        Returns [{order_id, symbol, status, fill_price}] for orders that changed.
        """
        sql = "SELECT * FROM paper_order WHERE status='PENDING'"
        args = []
        if order_ids:
            sql += f" AND order_id IN ({','.join('?' * len(order_ids))})"
            args = list(order_ids)
        changed = []
        for r in [dict(x) for x in self.conn.execute(sql, args).fetchall()]:
            ltp = (prices or {}).get(r["symbol"])
            if ltp is None:
                ltp = self._ltp(r["symbol"])
            if not ltp:
                continue
            side, otype = r["transaction_type"], r["order_type"]
            if otype in ("SL", "SL-M"):
                if not self._triggered(side, ltp, float(r["trigger_price"] or 0)):
                    continue
                otype = "LIMIT" if otype == "SL" else "MARKET"
                self.conn.execute("UPDATE paper_order SET triggered_at=?, order_type=?, reason=? WHERE order_id=?",
                                  (_now(), otype, f"{r['order_type']} triggered at {ltp}", r["order_id"]))
            if otype == "LIMIT":
                lim = float(r["limit_price"] or 0)
                if not ((ltp <= lim) if side == "BUY" else (ltp >= lim)):
                    continue
                fill = self._slipped(min(lim, ltp) if side == "BUY" else max(lim, ltp), side)
                fill = min(fill, lim) if side == "BUY" else max(fill, lim)     # never worse than the limit
            else:
                fill = self._slipped(ltp, side)
            qty = int(r["quantity"]) - int(r["filled_qty"] or 0)
            value = fill * qty
            brokerage = round(value * float(self.cfg["paper_brokerage_pct"]) / 100.0, 2)
            why = None
            if side == "BUY" and self.balance < value + brokerage:
                why = f"insufficient paper funds at fill: need {value + brokerage:,.2f}"
            elif side == "SELL" and self._position_qty(r["symbol"]) < qty:
                why = "position no longer holds the quantity"
            if why:
                self.conn.execute("UPDATE paper_order SET status='REJECTED', reason=?, updated_at=? WHERE order_id=?",
                                  (why, _now(), r["order_id"]))
                changed.append({"order_id": r["order_id"], "symbol": r["symbol"], "status": "REJECTED"})
                continue
            self._adjust_balance(-(value + brokerage) if side == "BUY" else (value - brokerage))
            self._apply_position(r["symbol"], side, qty, fill)
            self.conn.execute("UPDATE paper_order SET status='TRADED', filled_qty=quantity, fill_price=?, "
                              "brokerage=COALESCE(brokerage,0)+?, updated_at=? WHERE order_id=?",
                              (fill, brokerage, _now(), r["order_id"]))
            changed.append({"order_id": r["order_id"], "symbol": r["symbol"], "status": "TRADED",
                            "fill_price": fill})
            log.info(f"  [PAPER] resting {side} {qty} {r['symbol']} filled @ {fill} (ltp {ltp}) -> {r['order_id']}")
        self.conn.commit()
        return changed

    def get_order_list(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM paper_order ORDER BY created_at DESC").fetchall()
        return {"status": "success", "data": [dict(r) for r in rows]}

    def get_order_by_id(self, order_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM paper_order WHERE order_id=?",
                              (order_id,)).fetchone()
        return {"status": "success", "data": dict(r)} if r else {
            "status": "failure", "remarks": {"error_message": "unknown order id"}, "data": ""}

    def get_positions(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM paper_position WHERE quantity != 0").fetchall()
        return {"status": "success", "data": [
            {"tradingSymbol": r["symbol"], "netQty": r["quantity"],
             "buyAvg": r["avg_price"], "realizedProfit": r["realized_pnl"]}
            for r in rows]}

    def get_holdings(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM paper_position WHERE quantity > 0").fetchall()
        return {"status": "success", "data": [
            {"tradingSymbol": r["symbol"], "totalQty": r["quantity"],
             "availableQty": r["quantity"], "avgCostPrice": r["avg_price"],
             "exchange": "ALL"} for r in rows]}

    # ── position maths ────────────────────────────────────────────────────
    def _position_qty(self, symbol: str) -> int:
        r = self.conn.execute("SELECT quantity FROM paper_position WHERE symbol=?",
                              (symbol,)).fetchone()
        return int(r[0]) if r else 0

    def _apply_position(self, symbol, transaction_type, qty, price):
        r = self.conn.execute("SELECT quantity, avg_price, realized_pnl "
                              "FROM paper_position WHERE symbol=?", (symbol,)).fetchone()
        held, avg, realized = (int(r[0]), float(r[1]), float(r[2])) if r else (0, 0.0, 0.0)
        if transaction_type == "BUY":
            new_qty = held + qty
            avg = round((held * avg + qty * price) / new_qty, 4) if new_qty else 0.0
            held = new_qty
        else:
            realized = round(realized + (price - avg) * qty, 2)
            held -= qty
            if held == 0:
                avg = 0.0
        self.conn.execute(
            "INSERT INTO paper_position (symbol, quantity, avg_price, realized_pnl, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity, "
            "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, "
            "updated_at=excluded.updated_at",
            (symbol, held, avg, realized, _now()))

    def _write_order(self, oid, sym, sec, exch, ttype, qty, filled, otype, ptype,
                     limit_price, fill_price, status, reason, brokerage, tag=None):
        self.conn.execute("""
            INSERT INTO paper_order (order_id, created_at, symbol, security_id, exchange,
                transaction_type, quantity, filled_qty, order_type, product_type,
                limit_price, fill_price, status, reason, brokerage, tag)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (oid, _now(), sym, str(sec) if sec else None, exch, ttype, qty, filled,
              otype, ptype, limit_price, fill_price, status, reason, brokerage, tag))

    def _symbol_for(self, security_id):
        if not security_id:
            return None
        try:
            from data.dhan import load_security_map
            for sym, sec in load_security_map().items():
                if str(sec["security_id"]) == str(security_id):
                    return sym
        except Exception:
            pass
        return None

    # ── housekeeping ──────────────────────────────────────────────────────
    def reset(self, balance: float = None):
        """Wipe simulated state. Only ever touches paper_* tables."""
        for t in ("paper_order", "paper_position"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.execute("UPDATE paper_account SET balance=?, opened_at=? WHERE id=1",
                          (float(balance if balance is not None
                                 else self.cfg["paper_opening_balance"]), _now()))
        self.conn.commit()

    def summary(self) -> dict:
        orders = self.conn.execute(
            "SELECT status, COUNT(*) n FROM paper_order GROUP BY status").fetchall()
        pos = self.conn.execute(
            "SELECT symbol, quantity, avg_price, realized_pnl FROM paper_position "
            "WHERE quantity != 0 OR realized_pnl != 0").fetchall()
        return {"balance": round(self.balance, 2),
                "orders": {r[0]: r[1] for r in orders},
                "positions": [dict(r) for r in pos],
                "realized_pnl": round(sum(r["realized_pnl"] for r in pos), 2)}


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def _cli():
    import argparse
    import json as _json

    p = argparse.ArgumentParser(
        prog="python -m orders.paper",
        description="Inspect and drive the simulated broker. Fills use LIVE Dhan "
                    "prices; no order ever reaches Dhan's trading API.")
    p.add_argument("--summary", action="store_true", help="balance, orders, positions")
    p.add_argument("--orders", action="store_true", help="list simulated orders")
    p.add_argument("--reset", action="store_true", help="wipe simulated state")
    p.add_argument("--balance", type=float, help="opening balance to reset to")
    p.add_argument("--buy", metavar="SYMBOL")
    p.add_argument("--sell", metavar="SYMBOL")
    p.add_argument("--qty", type=int, default=1)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from orders.environment import describe, broker_env, PAPER

    env = broker_env()
    print(f"  environment: {describe()}")
    if env != PAPER and (args.buy or args.sell):
        # This CLI is the paper tool. Placing through it while configured for
        # LIVE would be a genuinely dangerous surprise.
        print(f"  REFUSING to trade from the paper CLI while broker_env={env}.")
        print("  Use orders/broker.py directly if you really mean to trade there.")
        return

    b = PaperBroker()
    try:
        if args.reset:
            b.reset(args.balance)
            print(f"  reset — balance {b.balance:,.2f}")
        if args.buy or args.sell:
            side = "BUY" if args.buy else "SELL"
            from orders.risk import halted
            is_halted, why = halted()
            if is_halted:
                print(f"trading is halted ({why}) — not placing"); raise SystemExit(1)
            r = b.place_order(transaction_type=side, quantity=args.qty,
                              symbol=(args.buy or args.sell).upper())
            print(f"  {side}: {_json.dumps(r.get('data') or r, indent=2)}")
        if args.orders:
            for o in b.get_order_list()["data"]:
                print(f"  {o['created_at'][:19]}  {o['status']:<10} {o['transaction_type']:<4} "
                      f"{o['filled_qty']}/{o['quantity']:<5} {o['symbol']:<12} "
                      f"@ {o['fill_price'] or '-'}  {o['reason'] or ''}")
        if args.summary or not any((args.reset, args.buy, args.sell, args.orders)):
            print(_json.dumps(b.summary(), indent=2))
    finally:
        b.close()


if __name__ == "__main__":
    _cli()
