"""
Basket orders (W39, EX-18 -- Zerodha / Dhan parity): a named list of orders previewed and
placed together.

    save(conn, body, basket_id=None)   {name, note?, legs: [{symbol, side, quantity, order_type?,
                                       price?, product_type?}]}; at most MAX_LEGS legs
    get / list_baskets / archive
    preview(conn, basket_id)           every leg as a DRY RUN through orders.broker._place_order
                                       (the same halt / live-switch / security-id / risk checks as a
                                       single order), plus ONE funds check over the basket's total
                                       buy value net of its sells. Conservative: each BUY leg's own
                                       dry run also needs its value in cash, so a buy affordable only
                                       after the basket's sells is refused
    execute(conn, basket_id, confirm)  all-or-nothing gate: nothing is sent unless every leg's dry
                                       run is clean and the total is funded. Then SELL legs go first
                                       (they free cash), BUY legs after, each through _place_order
                                       with confirm. A leg refused at placement leaves the basket
                                       PARTIAL -- earlier fills are not undone (no broker can).

Where an order goes is still decided only by orders.environment.broker_env (PAPER by default)
and, for real money, confirm AND execution.live_trading_enabled -- a basket adds no path around
those. Every preview / placement is a row of order_basket_run; every leg is in order_log.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

MAX_LEGS = 20
SIDES = ("BUY", "SELL")
ORDER_TYPES = ("MARKET", "LIMIT")
PRODUCTS = ("CNC", "INTRADAY")
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]{0,29}$")
CLEAN = ("DRY_RUN_OK",)


def ensure_tables(conn):
    from db.schema_w39b import W39B_TABLES
    for t in ("order_basket", "order_basket_run"):
        for ddl in W39B_TABLES[t]:
            conn.execute(ddl)


def _legs(raw) -> list:
    if not isinstance(raw, list) or not raw:
        raise ValueError("legs must be a non-empty list")
    if len(raw) > MAX_LEGS:
        raise ValueError(f"a basket holds at most {MAX_LEGS} legs")
    out = []
    for i, leg in enumerate(raw, 1):
        if not isinstance(leg, dict):
            raise ValueError(f"leg {i}: must be an object")
        sym = str(leg.get("symbol") or "").strip().upper()
        if not SYMBOL_RE.match(sym):
            raise ValueError(f"leg {i}: invalid symbol {leg.get('symbol')!r}")
        side = str(leg.get("side") or "").upper()
        if side not in SIDES:
            raise ValueError(f"leg {i}: side must be BUY or SELL")
        try:
            qty = int(leg.get("quantity"))
        except (TypeError, ValueError):
            raise ValueError(f"leg {i}: quantity must be a whole number") from None
        if qty <= 0 or qty > 1_000_000:
            raise ValueError(f"leg {i}: quantity must be between 1 and 1,000,000")
        ot = str(leg.get("order_type") or "MARKET").upper()
        if ot not in ORDER_TYPES:
            raise ValueError(f"leg {i}: order_type must be MARKET or LIMIT")
        price = float(leg.get("price") or 0)
        if ot == "LIMIT" and price <= 0:
            raise ValueError(f"leg {i}: a LIMIT leg needs price > 0")
        prod = str(leg.get("product_type") or "CNC").upper()
        if prod not in PRODUCTS:
            raise ValueError(f"leg {i}: product_type must be CNC or INTRADAY")
        out.append({"symbol": sym, "side": side, "quantity": qty, "order_type": ot,
                    "price": price if ot == "LIMIT" else 0.0, "product_type": prod})
    return out


def save(conn, body: dict, basket_id: str | None = None) -> dict:
    ensure_tables(conn)
    name = str((body or {}).get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("name is required (at most 80 characters)")
    legs = _legs((body or {}).get("legs"))
    note = str(body.get("note") or "")[:300] or None
    now = datetime.now()
    if basket_id:
        if not conn.execute("SELECT 1 FROM order_basket WHERE basket_id=? AND archived=0", (basket_id,)).fetchone():
            raise LookupError("basket not found")
        conn.execute("UPDATE order_basket SET name=?, legs_json=?, note=?, updated_at=? WHERE basket_id=?",
                     (name, json.dumps(legs), note, now, basket_id))
    else:
        basket_id = f"bkt_{uuid.uuid4().hex[:12]}"
        conn.execute("INSERT INTO order_basket (basket_id,name,legs_json,note,archived,created_at,updated_at) VALUES "
                     "(?,?,?,?,0,?,?)", (basket_id, name, json.dumps(legs), note, now, now))
    conn.commit()
    return get(conn, basket_id)


def get(conn, basket_id: str) -> dict:
    ensure_tables(conn)
    r = conn.execute("SELECT basket_id, name, legs_json, note, archived, created_at, updated_at FROM order_basket "
                     "WHERE basket_id=?", (basket_id,)).fetchone()
    if not r:
        raise LookupError("basket not found")
    d = dict(r)
    d["legs"] = json.loads(d.pop("legs_json"))
    d["runs"] = [dict(x) for x in conn.execute(
        "SELECT run_id, at, env, confirm, status, buy_value, sell_value, available FROM order_basket_run "
        "WHERE basket_id=? ORDER BY at DESC LIMIT 10", (basket_id,))]
    return d


def list_baskets(conn) -> list:
    ensure_tables(conn)
    out = []
    for r in conn.execute("SELECT basket_id, name, legs_json, updated_at FROM order_basket WHERE archived=0 "
                          "ORDER BY updated_at DESC"):
        legs = json.loads(r["legs_json"])
        out.append({"basket_id": r["basket_id"], "name": r["name"], "legs": len(legs),
                    "symbols": sorted({x["symbol"] for x in legs}), "updated_at": r["updated_at"]})
    return out


def archive(conn, basket_id: str) -> dict:
    ensure_tables(conn)
    if not conn.execute("UPDATE order_basket SET archived=1, updated_at=? WHERE basket_id=? AND archived=0",
                        (datetime.now(), basket_id)).rowcount:
        raise LookupError("basket not found")
    conn.commit()
    return {"basket_id": basket_id, "archived": True}


def _ordered(legs):
    """SELL legs first (they free cash), then BUY legs, each group in the basket's order."""
    return [x for x in legs if x["side"] == "SELL"] + [x for x in legs if x["side"] == "BUY"]


def preview(conn, basket_id: str) -> dict:
    from orders import broker as B
    from orders.environment import broker_env
    b = get(conn, basket_id)
    legs, buy, sell = [], 0.0, 0.0
    for leg in _ordered(b["legs"]):
        r = B._place_order(leg["symbol"], leg["side"], leg["quantity"], leg["order_type"], leg["product_type"],
                           leg["price"], confirm=False, tag=f"basket:{basket_id}")
        est = float(r.get("estimated_value") or 0.0)
        if leg["side"] == "BUY":
            buy += est
        else:
            sell += est
        risk = r.get("risk") or {}
        why = None
        if r.get("status") not in CLEAN:
            why = r.get("reason") or r.get("message") or r.get("error") or r.get("status")
        elif r.get("halted"):
            why = f"trading is halted ({r.get('halt_reason')})"
        elif not risk.get("ok", True):
            why = f"risk: {risk.get('message')}"
        legs.append({**leg, "status": r.get("status"), "estimated_value": round(est, 2), "problem": why})
    available = B.available_balance()
    # a leg's own dry run checks funds for that leg alone; the basket needs its total covered
    need = max(0.0, buy - sell)
    funded = available >= need
    problems = [f"{x['side']} {x['quantity']} {x['symbol']}: {x['problem']}" for x in legs if x["problem"]]
    if not funded:
        problems.append(f"funds: Rs.{available:,.2f} available, Rs.{need:,.2f} needed (buys Rs.{buy:,.2f} - "
                        f"sells Rs.{sell:,.2f})")
    return {"basket_id": basket_id, "name": b["name"], "env": broker_env(), "legs": legs,
            "buy_value": round(buy, 2), "sell_value": round(sell, 2), "net_needed": round(need, 2),
            "available": round(available, 2), "ready": not problems, "problems": problems}


def execute(conn, basket_id: str, confirm: bool = False) -> dict:
    """Preview; with confirm and a clean preview, place every leg. Returns the run."""
    from orders import broker as B
    pv = preview(conn, basket_id)
    results, status = [], None
    if not confirm:
        status = "PREVIEW" if pv["ready"] else "PREVIEW_BLOCKED"
    elif not pv["ready"]:
        status = "BLOCKED"
    else:
        for leg in pv["legs"]:
            r = B._place_order(leg["symbol"], leg["side"], leg["quantity"], leg["order_type"],
                               leg["product_type"], leg["price"], confirm=True, tag=f"basket:{basket_id}")
            results.append({**{k: leg[k] for k in ("symbol", "side", "quantity", "order_type", "price")},
                            "status": r.get("status"), "order_id": r.get("order_id"),
                            "reason": r.get("reason") or r.get("message") or r.get("error")})
        ok = sum(1 for x in results if x["status"] == "PLACED")
        status = "PLACED" if ok == len(results) else ("FAILED" if ok == 0 else "PARTIAL")
    run_id = f"bkr_{uuid.uuid4().hex[:12]}"
    conn.execute("INSERT INTO order_basket_run (run_id,basket_id,at,env,confirm,status,buy_value,sell_value,available,"
                 "results_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (run_id, basket_id, datetime.now(), pv["env"], int(bool(confirm)), status, pv["buy_value"],
                  pv["sell_value"], pv["available"], json.dumps({"preview": pv, "placed": results})))
    conn.commit()
    return {"run_id": run_id, "status": status, "preview": pv, "placed": results}
