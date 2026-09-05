"""
ATIP — Manual Order Execution (Buy / Sell via Dhan)
=====================================================

⚠️  THIS MODULE CAN PLACE REAL ORDERS WITH REAL MONEY, if used with a live
    Dhan account and confirm=True. Read this whole docstring before using it.

WHY THIS IS SEPARATE FROM THE SCHEDULER (intentional design decision):
    This module is NOT imported or called anywhere in pipeline/scheduler.py,
    and it should stay that way unless you deliberately decide otherwise. Your
    own atip.log shows this process going silent for 2+ hours (machine
    sleep/network drop) and hitting an unhandled traceback after a catch-up
    burst. An unattended process with that failure profile auto-firing trades
    is a real way to lose money — e.g. a stale/duplicate signal firing twice,
    or a crash mid-order leaving state inconsistent. Wiring buy/sell into the
    scheduler would need a lot more hardening first (idempotency keys so a
    retry can't double-order, a kill switch, position reconciliation on
    startup, alerting on every fill). None of that exists yet — so for now
    this is a manual, explicitly-confirmed tool only: you call it yourself,
    for one order at a time.

SAFETY MODEL:
    1. Every BUY checks available funds via get_fund_limits() FIRST and
       refuses to place an order that would exceed your available balance.
    2. DRY RUN BY DEFAULT. Every function/CLI call only prints/logs what it
       WOULD do and returns without touching the order API, unless you pass
       confirm=True (Python) or --confirm (CLI). There is no way to place a
       real order by accident with default arguments.
    3. Every attempted order — dry-run or real, successful or blocked — is
       logged to the order_log table for audit.

VERIFY BEFORE FIRST LIVE USE:
    dhan.place_order()'s exact keyword arguments and dhan.get_fund_limits()'s
    exact response field names vary across dhanhq SDK versions and are NOT
    something that could be tested from here. Before ever passing --confirm:
      1. Run `python -m orders.broker --funds` and check the printed
         dict's keys actually contain a balance field — the code below tries
         a few known field-name variants, but confirm one of them matched.
      2. Run a dry-run buy/sell (no --confirm) and read the printed
         "estimated value" and funds-check line — make sure they look right
         for a symbol/quantity you know the numbers for.
      3. Only then try --confirm, ideally with quantity=1 on a cheap, liquid
         stock first.

Run standalone:
    python -m orders.broker --funds
    python -m orders.broker --buy RELIANCE --qty 1                # dry run
    python -m orders.broker --buy RELIANCE --qty 1 --confirm      # REAL
    python -m orders.broker --sell RELIANCE --qty 1 --confirm     # REAL
    python -m orders.broker --buy RELIANCE --qty 5 --order-type LIMIT --price 2500 --confirm
"""
import logging, argparse
from datetime import datetime
from db.schema import get_connection, log_job
from data.dhan import get_dhan_client, get_security_id, fetch_live_quotes

log = logging.getLogger(__name__)


def _ensure_order_log_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT,
            symbol           TEXT,
            transaction_type TEXT,
            quantity         INTEGER,
            order_type       TEXT,
            product_type     TEXT,
            price            REAL,
            estimated_value  REAL,
            available_funds  REAL,
            mode             TEXT,
            status           TEXT,
            dhan_order_id    TEXT,
            error            TEXT
        )
    """)


def _log_order(conn, symbol, ttype, qty, order_type, product_type, price,
               est_value, avail, mode, status, order_id=None, error=None):
    conn.execute("""
        INSERT INTO order_log
            (timestamp,symbol,transaction_type,quantity,order_type,product_type,
             price,estimated_value,available_funds,mode,status,dhan_order_id,error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, ttype, qty,
          order_type, product_type, price, est_value, avail, mode, status,
          order_id, error))
    conn.commit()


# ═════════════════════════════════════════════════════════════════════════
#  FUNDS
# ═════════════════════════════════════════════════════════════════════════

def get_fund_limits() -> dict:
    """Raw fund-limit payload from Dhan. Returns {} on any failure."""
    try:
        dhan, _ = get_dhan_client()
    except RuntimeError as e:
        log.error(str(e)); return {}
    try:
        resp = dhan.get_fund_limits()
        if not resp or resp.get("status") == "failure":
            log.error(f"  Fund limits failed: {resp}"); return {}
        return resp.get("data", {}) or {}
    except Exception as e:
        log.error(f"  Fund limits failed: {e}"); return {}


def available_balance() -> float:
    """
    Best-effort extraction of a usable balance figure. Dhan's fund-limit
    response field names have varied across SDK versions/docs
    (availabelBalance — yes, with that typo, in some versions —
    availableBalance, withdrawableBalance, sodLimit). Tries each in order
    and returns the first that parses as a number; 0.0 if none do.
    """
    data = get_fund_limits()
    for key in ("availabelBalance", "availableBalance", "withdrawableBalance", "sodLimit"):
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    if data:
        log.warning(f"  Could not find a known balance field in fund limits response: {list(data.keys())}")
    return 0.0


def check_funds(estimated_amount: float) -> dict:
    bal = available_balance()
    ok = bal >= estimated_amount
    msg = (f"OK — Rs.{bal:,.2f} available, Rs.{estimated_amount:,.2f} required" if ok else
           f"INSUFFICIENT — Rs.{bal:,.2f} available, Rs.{estimated_amount:,.2f} required "
           f"(short by Rs.{estimated_amount - bal:,.2f})")
    log.info(f"  Funds check: {msg}")
    return {"ok": ok, "available": bal, "required": estimated_amount, "message": msg}


# ═════════════════════════════════════════════════════════════════════════
#  ORDER PLACEMENT
# ═════════════════════════════════════════════════════════════════════════

def _estimate_order_value(symbol, quantity, order_type, price):
    """LTP-based estimate for MARKET orders; declared price for LIMIT orders."""
    if order_type == "LIMIT" and price:
        return float(price) * quantity
    try:
        dhan, _ = get_dhan_client()
        q = fetch_live_quotes([symbol], dhan)
        if not q.empty and q.iloc[0].get("ltp"):
            return float(q.iloc[0]["ltp"]) * quantity
    except Exception as e:
        log.warning(f"  Could not fetch LTP for value estimate: {e}")
    return float(price or 0) * quantity


def _place_order(symbol, transaction_type, quantity, order_type="MARKET",
                  product_type="CNC", price=0, confirm=False):
    """
    transaction_type : "BUY" or "SELL"
    order_type        : "MARKET" or "LIMIT"
    product_type      : "CNC" (delivery) or "INTRADAY"
    price              : required for LIMIT, ignored for MARKET
    confirm            : False (default) = dry run only. True = places a
                          real order — real money, real trade.
    """
    conn = get_connection(); _ensure_order_log_table(conn)
    mode = "REAL" if confirm else "DRY_RUN"

    sec = get_security_id(symbol)
    if not sec:
        msg = f"security_id not found for {symbol} — cannot place order (check security_id_list.csv is current)"
        log.error(f"  X {msg}")
        _log_order(conn, symbol, transaction_type, quantity, order_type, product_type,
                   price, None, None, mode, "FAILED", error=msg)
        conn.close()
        return {"status": "FAILED", "error": msg}

    est_value = _estimate_order_value(symbol, quantity, order_type, price)
    if transaction_type == "BUY":
        funds = check_funds(est_value)
    else:
        funds = {"ok": True, "available": available_balance(), "message": "SELL — funds check not required"}

    log.info(f"  {'LIVE ORDER' if confirm else 'DRY RUN'}: {transaction_type} {quantity} x {symbol} "
             f"({order_type}{f' @ Rs.{price}' if order_type == 'LIMIT' else ' @ market'}, {product_type}) "
             f"— est. value Rs.{est_value:,.2f}")

    if transaction_type == "BUY" and not funds["ok"]:
        log.error(f"  X Order blocked — {funds['message']}")
        _log_order(conn, symbol, transaction_type, quantity, order_type, product_type,
                   price, est_value, funds["available"], mode,
                   "BLOCKED_INSUFFICIENT_FUNDS", error=funds["message"])
        conn.close()
        return {"status": "BLOCKED_INSUFFICIENT_FUNDS", **funds}

    if not confirm:
        log.info("  Dry run only — pass confirm=True (Python) or --confirm (CLI) to place this for real.")
        _log_order(conn, symbol, transaction_type, quantity, order_type, product_type,
                   price, est_value, funds["available"], mode, "DRY_RUN_OK")
        conn.close()
        return {"status": "DRY_RUN_OK", "estimated_value": est_value, "funds": funds}

    try:
        dhan, _ = get_dhan_client()
        resp = dhan.place_order(
            security_id=sec["security_id"],
            exchange_segment=sec["exchange"],
            transaction_type=transaction_type,
            quantity=int(quantity),
            order_type=order_type,
            product_type=product_type,
            price=float(price) if order_type == "LIMIT" else 0,
        )
        if not resp or resp.get("status") == "failure":
            log.error(f"  X Order failed: {resp}")
            _log_order(conn, symbol, transaction_type, quantity, order_type, product_type,
                       price, est_value, funds["available"], mode, "FAILED", error=str(resp))
            conn.close()
            log_job(f"order_{transaction_type.lower()}", "FAILED", 0, error=str(resp))
            return {"status": "FAILED", "response": resp}

        data = resp.get("data") or {}
        order_id = data.get("orderId") or data.get("order_id")
        log.info(f"  Order placed — id {order_id}")
        _log_order(conn, symbol, transaction_type, quantity, order_type, product_type,
                   price, est_value, funds["available"], mode, "PLACED", order_id=order_id)
        conn.close()
        log_job(f"order_{transaction_type.lower()}", "SUCCESS", 1)
        return {"status": "PLACED", "order_id": order_id, "response": resp}

    except Exception as e:
        log.error(f"  X Order exception: {e}")
        _log_order(conn, symbol, transaction_type, quantity, order_type, product_type,
                   price, est_value, funds["available"], mode, "FAILED", error=str(e))
        conn.close()
        log_job(f"order_{transaction_type.lower()}", "FAILED", 0, error=e)
        return {"status": "FAILED", "error": str(e)}


def place_buy_order(symbol, quantity, order_type="MARKET", product_type="CNC", price=0, confirm=False):
    return _place_order(symbol, "BUY", quantity, order_type, product_type, price, confirm)


def place_sell_order(symbol, quantity, order_type="MARKET", product_type="CNC", price=0, confirm=False):
    return _place_order(symbol, "SELL", quantity, order_type, product_type, price, confirm)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP manual order execution — DRY RUN unless --confirm is passed")
    ap.add_argument("--funds", action="store_true", help="show available funds and exit")
    ap.add_argument("--buy", metavar="SYMBOL")
    ap.add_argument("--sell", metavar="SYMBOL")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--order-type", choices=["MARKET", "LIMIT"], default="MARKET")
    ap.add_argument("--product", choices=["CNC", "INTRADAY"], default="CNC")
    ap.add_argument("--price", type=float, default=0, help="required for --order-type LIMIT")
    ap.add_argument("--confirm", action="store_true", help="place the order for real — omit for a dry run")
    args = ap.parse_args()

    if args.funds:
        data = get_fund_limits()
        print(data if data else "Could not fetch fund limits — check Dhan credentials in atip_data/config.json.")
    elif args.buy:
        print(place_buy_order(args.buy, args.qty, args.order_type, args.product, args.price, confirm=args.confirm))
    elif args.sell:
        print(place_sell_order(args.sell, args.qty, args.order_type, args.product, args.price, confirm=args.confirm))
    else:
        ap.print_help()
