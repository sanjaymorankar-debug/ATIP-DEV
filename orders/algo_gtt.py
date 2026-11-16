"""
Dummy broker-side orders for the Kite breakout-retest entry (KGF-11, KGF-12, KGF-16; 16 Nov 2026). SIMULATION ONLY.

The Kite formula rests a buy at the breakout candle's low (a GTT single trigger on Kite, a Forever Order SINGLE on Dhan)
and puts the stop and target (a two-leg GTT / an OCO Forever Order) on only after the buy executes. ATIP writes each of
those as a local record whose id starts with DUMMY-, in the persistent table algo_dummy_gtt. NOTHING HERE PLACES,
MODIFIES OR CANCELS AN ORDER AT ANY BROKER: the only broker calls are the read-only ones in ReadOnlyBroker (orders,
positions, holdings, forever orders, GTTs), used to refuse a duplicate. `confirm_live` is kept for API compatibility with
the Kite spec; it never enables placement.

    buy_price   breakout-candle low, tick-rounded DOWN (never the LTP)
    stoploss    buy_price x 0.95, tick-rounded DOWN (risk per share = buy - stop; no ATR)
    valid       10 trading sessions (VALID_SESSIONS), the day it is made counting when made before the close;
                cancelled earlier when the setup is superseded or a close falls under the stop before a fill
    fill        in PAPER, when the LTP is at or under the trigger, at min(LTP, trigger): the session's entry rule
                (orders/rules.py, a LIMIT at the buy price) does it through the paper broker; a record made from the API
                without a session fills on the record only
    legs        planned_after_buy until the buy executes; then a DUMMY- OCO record mirrors the rule's STOP / TARGET legs

Duplicate prevention is atomic and survives a restart: one SQLite BEGIN IMMEDIATE transaction re-reads every state check
and inserts, and a partial UNIQUE index allows one ACTIVE dummy BUY per symbol. A new entry is refused when the paper
book holds the symbol, a session holds or has queued it, an active DUMMY- exists for it, or (read-only, when Dhan
credentials are present) the real account has a non-cancelled, non-rejected CNC BUY on NSE today, a net position, a
holding, or an active forever order / GTT for it.

Statuses (Kite spec): ready, success, failed, blocked, already_active, planned_after_buy, waiting_for_ltp, not_available,
not_placed, checking.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import date, datetime

log = logging.getLogger("atip.algo_gtt")

PREFIX = "DUMMY-"
STOP_PCT = 5.0
VALID_SESSIONS = 10
ACTIVE, TRIGGERED, CANCELLED, EXPIRED, CLOSED = "ACTIVE", "TRIGGERED", "CANCELLED", "EXPIRED", "CLOSED"
KIND_BUY, KIND_OCO = "BUY", "OCO"
STATUSES = ("ready", "success", "failed", "blocked", "already_active", "planned_after_buy", "waiting_for_ltp",
            "not_available", "not_placed", "checking")
NO_ORDER = "No order sent to the broker."
MSG = {
    "created": f"Dummy buy GTT created. {NO_ORDER} Stoploss planned after buy execution.",
    "already_active": f"An active dummy buy GTT already exists for this symbol. {NO_ORDER}",
    "confirm": f"confirm_live=true is required. Nothing was created. {NO_ORDER}",
    "position": f"Blocked: a position is already open for this symbol. {NO_ORDER}",
    "holding": f"Blocked: the account already holds this symbol. {NO_ORDER}",
    "day_order": f"Blocked: a CNC buy order for this symbol is already open today. {NO_ORDER}",
    "broker_gtt": f"Blocked: an active GTT / forever order already exists for this symbol. {NO_ORDER}",
    "session": f"Blocked: a session already holds or has queued this symbol. {NO_ORDER}",
    "no_ltp": f"Waiting for a live price before the dummy GTT can be checked. {NO_ORDER}",
    "no_setup": "No breakout retest setup for this symbol.",
    "bad_price": f"Buy price and stoploss must be positive and the stoploss below the buy price. {NO_ORDER}",
    "unreadable": f"Broker state could not be read, so a duplicate cannot be ruled out. {NO_ORDER}",
    "ready": f"Ready: no position, holding, open order or active GTT for this symbol. {NO_ORDER}",
}
# Read-only broker methods: anything else raises (the safety test checks no order-changing method is reachable).
READ_METHODS = {"get_order_list", "get_positions", "get_holdings", "get_forever", "get_super_order_list",
                "get_conditional_orders", "orders", "positions", "holdings", "get_gtts", "gtts"}


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS algo_dummy_gtt (
            id               TEXT PRIMARY KEY,
            kind             TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            exchange         TEXT NOT NULL DEFAULT 'NSE',
            product          TEXT NOT NULL DEFAULT 'CNC',
            order_type       TEXT NOT NULL DEFAULT 'LIMIT',
            transaction_type TEXT NOT NULL,
            quantity         INTEGER,
            trigger_price    REAL,
            limit_price      REAL,
            stop_trigger     REAL,
            target_trigger   REAL,
            tick_size        REAL,
            status           TEXT NOT NULL,
            session_id       TEXT,
            rule_id          TEXT,
            parent_id        TEXT,
            setup            TEXT,
            valid_until      TEXT,
            last_price       REAL,
            fill_price       REAL,
            filled_at        TEXT,
            message          TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_algo_dummy_gtt_active_buy ON algo_dummy_gtt(symbol) "
                 "WHERE kind = 'BUY' AND status = 'ACTIVE'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_algo_dummy_gtt_rule ON algo_dummy_gtt(rule_id)")
    conn.commit()


# -- read-only broker state ---------------------------------------------------------------------------------------------

class ReadOnlyBroker:
    """A broker client seen through READ_METHODS only. Every other attribute raises, so no code path holding this can
    place, modify or cancel anything -- confirm_live or not."""

    def __init__(self, client, name: str = "dhan"):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "name", name)

    def __getattr__(self, item):
        if item not in READ_METHODS:
            raise PermissionError(f"{item} is not a read-only broker call: algo_gtt never changes broker orders")
        return getattr(object.__getattribute__(self, "_client"), item)

    def __setattr__(self, k, v):
        raise PermissionError("ReadOnlyBroker is immutable")


def default_broker():
    """The real Dhan account, read-only, when credentials are present and this is the trading Mac; else None."""
    try:
        from ops.role import is_viewer
        if is_viewer():
            return None
        from data import dhan as D
        if not D.HAS_DHAN:
            return None
        cfg = D.load_dhan_config()
        if not cfg.get("dhan_client_id") or not cfg.get("dhan_access_token"):
            return None
        return ReadOnlyBroker(D.get_dhan_client()[0], "dhan")
    except Exception as e:
        log.debug(f"  no read-only broker for the dummy-GTT checks: {e}")
        return None


def _rows(resp) -> list:
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    d = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(d, dict) and "data" in d:
        d = d["data"]
    return d if isinstance(d, list) else []


def _sym(o) -> str:
    return str(o.get("tradingSymbol") or o.get("tradingsymbol") or o.get("symbol") or "").upper().replace("-EQ", "")


def broker_state(broker, symbol: str, today: str | None = None) -> dict:
    """{"readable", "day_buy_order_id", "day_buy_order_status", "position_quantity", "holding_quantity",
    "active_gtt_id", "errors"} for the real account. broker None -> readable False with no blockers (no credentials)."""
    out = {"readable": broker is not None, "day_buy_order_id": None, "day_buy_order_status": None,
           "position_quantity": 0, "holding_quantity": 0, "active_gtt_id": None, "errors": []}
    if broker is None:
        return out
    sym, today = symbol.upper(), today or date.today().isoformat()
    calls = (("get_order_list", "orders"), ("get_positions", "positions"), ("get_holdings", "holdings"),
             ("get_forever", "gtts"))
    got = {}
    for dhan_name, kite_name in calls:
        try:
            try:
                fn = getattr(broker, dhan_name)          # Dhan's name, else Kite's (orders / positions / holdings / gtts)
            except AttributeError:
                fn = getattr(broker, kite_name)
            got[kite_name] = _rows(fn())
        except PermissionError:
            raise
        except Exception as e:
            out["errors"].append(f"{dhan_name}: {e}")
            got[kite_name] = []
    for o in got["orders"]:
        st = str(o.get("orderStatus") or o.get("status") or "").upper()
        if (_sym(o) == sym and str(o.get("transactionType") or o.get("transaction_type") or "").upper() == "BUY"
                and str(o.get("productType") or o.get("product") or "").upper() in ("CNC", "")
                and str(o.get("exchangeSegment") or o.get("exchange") or "NSE").upper().startswith("NSE")
                and st not in ("CANCELLED", "REJECTED", "EXPIRED")
                and str(o.get("createTime") or o.get("order_timestamp") or today)[:10] == today):
            out["day_buy_order_id"], out["day_buy_order_status"] = str(o.get("orderId") or o.get("order_id")), st
            break
    for p in got["positions"]:
        if _sym(p) == sym:
            out["position_quantity"] += float(p.get("netQty") or p.get("quantity") or 0)
    for h in got["holdings"]:
        if _sym(h) == sym:
            out["holding_quantity"] += float(h.get("totalQty") or h.get("quantity") or h.get("availableQty") or 0)
    for g in got["gtts"]:
        st = str(g.get("orderStatus") or g.get("status") or "").upper()
        if _sym(g) == sym and st in ("PENDING", "ACTIVE", "TRANSIT", "CONFIRM", ""):
            out["active_gtt_id"] = str(g.get("orderId") or g.get("id") or "")
            break
    return out


# -- prices -----------------------------------------------------------------------------------------------------------

def last_price(conn, symbol: str) -> tuple:
    """(price, source): the WebSocket / REST LTP stored in live_quotes (orders/risk.py's freshness), else the last close."""
    try:
        from orders.risk import reference_price
        return reference_price(conn, symbol)
    except Exception:
        return None, None


def _ticks(symbol, buy, stop, conn):
    from orders.ticks import D, to_tick, tick_size, mul
    tick = tick_size(symbol, buy, conn)
    b = to_tick(buy, "BUY", "ENTRY", tick=tick)
    s = to_tick(stop if stop else mul(b, (100 - STOP_PCT) / 100), "SELL", "STOP", tick=tick)
    return b, s, float(D(tick))


# -- state and creation -----------------------------------------------------------------------------------------------

def _active(conn, symbol: str) -> dict | None:
    r = conn.execute("SELECT * FROM algo_dummy_gtt WHERE symbol = ? AND kind = 'BUY' AND status = 'ACTIVE'",
                     (symbol,)).fetchone()
    return _row(r) if r else None


def _active_oco(conn, symbol: str) -> dict | None:
    r = conn.execute("SELECT * FROM algo_dummy_gtt WHERE symbol = ? AND kind = 'OCO' AND status = 'ACTIVE' "
                     "ORDER BY created_at DESC LIMIT 1", (symbol,)).fetchone()
    return _row(r) if r else None


def _paper_qty(conn, symbol: str) -> float:
    try:
        r = conn.execute("SELECT COALESCE(SUM(quantity), 0) AS q FROM paper_position WHERE symbol = ?", (symbol,)).fetchone()
        return float(_row(r)["q"] or 0)
    except Exception:
        return 0.0


def _session_busy(conn, symbol: str, own_rule: str | None = None) -> bool:
    try:
        from orders.autopick import QUEUED
        rows = conn.execute("SELECT r.id, r.status, r.role FROM order_rules r JOIN session_rules s ON s.rule_id = r.id "
                            "WHERE r.symbol = ?", (symbol,)).fetchall()
        if any((_row(r)["role"] or "ENTRY") == "ENTRY" and _row(r)["status"] in QUEUED and _row(r)["id"] != own_rule
               for r in rows):
            return True
        from orders import sessions
        for s in sessions.list_sessions(conn)["sessions"]:
            if any(h["symbol"] == symbol for h in ((s.get("pool") or {}).get("open") or [])):
                return True
    except Exception as e:
        log.debug(f"  session check for {symbol}: {e}")
    return False


def _blank(symbol, quantity=None, buy=None, stop=None, tick=None, ltp=None) -> dict:
    return {"symbol": symbol, "quantity": quantity, "buy_price": buy, "stoploss": stop, "tick_size": tick,
            "last_price": ltp, "account_state": "checking", "position_quantity": 0, "active_buy_trigger_id": None,
            "active_stoploss_trigger_id": None, "day_buy_order_id": None, "day_buy_order_status": None,
            "buy_status": "checking", "buy_trigger_id": None, "stoploss_status": "not_placed",
            "stoploss_trigger_id": None, "message": ""}


def _checks(conn, out: dict, broker, own_rule: str | None = None, check_session: bool = True) -> str | None:
    """Fill out's account fields; return the MSG key of the first blocker, or None. Runs inside the caller's transaction."""
    sym = out["symbol"]
    act, oco = _active(conn, sym), _active_oco(conn, sym)
    out["active_buy_trigger_id"] = act["id"] if act else None
    out["active_stoploss_trigger_id"] = oco["id"] if oco else None
    pq = _paper_qty(conn, sym)
    bs = broker_state(broker, sym)
    out["position_quantity"] = pq + bs["position_quantity"]
    out["day_buy_order_id"], out["day_buy_order_status"] = bs["day_buy_order_id"], bs["day_buy_order_status"]
    if act:
        out["account_state"] = "dummy_active"
        return "already_active"
    if pq > 0 or bs["position_quantity"] > 0:
        out["account_state"] = "position"
        return "position"
    if bs["holding_quantity"] > 0:
        out["account_state"] = "holding"
        return "holding"
    if bs["day_buy_order_id"]:
        out["account_state"] = "open_order"
        return "day_order"
    if bs["active_gtt_id"]:
        out["account_state"] = "active_gtt"
        out["active_buy_trigger_id"] = bs["active_gtt_id"]
        return "broker_gtt"
    if check_session and _session_busy(conn, sym, own_rule):
        out["account_state"] = "session_busy"
        return "session"
    if broker is not None and bs["errors"] and len(bs["errors"]) >= 4:
        out["account_state"] = "unknown"
        return "unreadable"
    out["account_state"] = "flat"
    return None


def gtt_state(conn, symbol: str, quantity=None, buy_price=None, stoploss=None, broker="default") -> dict:
    """POST /api/algo-orders/gtt-state: what a dummy GTT for symbol would be and whether it may be created. Read-only."""
    ensure_table(conn)
    sym = str(symbol or "").upper().strip()
    if broker == "default":
        broker = default_broker()
    ltp, _src = last_price(conn, sym)
    buy, stop = buy_price, stoploss
    act = _active(conn, sym)
    if not buy and act:                     # the active dummy's own prices
        buy, stop = act["trigger_price"], act["stop_trigger"]
    if not buy:
        setup = setup_for(conn, sym)
        if setup:
            buy, stop = setup.get("buy_price") or setup.get("breakoutLow"), setup.get("stop_price")
    out = _blank(sym, quantity, None, None, None, ltp)
    if not buy:
        out.update(buy_status="not_available", message=MSG["no_setup"])
        return out
    b, s, tick = _ticks(sym, float(buy), float(stop) if stop else None, conn)
    out.update(buy_price=b, stoploss=s, tick_size=tick)
    blocker = _checks(conn, out, broker)
    if blocker == "already_active":
        out.update(buy_status="already_active", buy_trigger_id=out["active_buy_trigger_id"],
                   stoploss_status="planned_after_buy", message=MSG["already_active"])
    elif blocker:
        out.update(buy_status="blocked", message=MSG[blocker])
    elif not ltp:
        out.update(buy_status="waiting_for_ltp", message=MSG["no_ltp"])
    else:
        out.update(buy_status="ready", stoploss_status="planned_after_buy", message=MSG["ready"])
    return out


def setup_for(conn, symbol: str) -> dict | None:
    """The symbol's row in the latest stored breakout-retest scan (strategy/breakout_retest.py), if any."""
    try:
        from strategy.breakout_retest import latest_scan
        for r in (latest_scan(conn) or {}).get("rows") or []:
            if str(r.get("symbol")).upper() == symbol.upper():
                return r
    except Exception as e:
        log.debug(f"  breakout-retest scan unavailable: {e}")
    return None


def _valid_until(start: date, sessions: int = VALID_SESSIONS, now: datetime | None = None) -> str:
    """The last of `sessions` trading sessions the dummy rests for. The day it is made counts as the first when that is a
    trading day and the market has not closed (`now` before 15:30 IST); otherwise the next trading day is the first.
    (Until 16 Nov 2026 the day it was made was not counted, so a dummy made in market hours rested 11 sessions.)"""
    from utils.trading_calendar import is_trading_day
    from datetime import time, timedelta
    d = start
    n = 1 if is_trading_day(d) and (now is None or now.time() < time(15, 30)) else 0
    while n < sessions:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return d.isoformat()


def place_dummy_buy(conn, symbol: str, quantity, buy_price, stoploss=None, *, confirm_live=None, session_id=None,
                    rule_id=None, setup=None, broker="default", last_px=None, valid_sessions: int = VALID_SESSIONS,
                    check_session: bool = True) -> dict:
    """POST /api/algo-orders/live-gtt: a DUMMY- resting buy, after every state check, atomically. Never places anything.
    confirm_live must be exactly True (the Kite spec's guard); it changes nothing else."""
    from ops.role import refuse
    refuse("Dummy GTT")
    ensure_table(conn)
    sym = str(symbol or "").upper().strip()
    if confirm_live is not True:
        out = _blank(sym, quantity)
        out.update(buy_status="blocked", account_state="unknown", message=MSG["confirm"])
        return out
    if broker == "default":
        broker = default_broker()
    ltp = last_px if last_px is not None else last_price(conn, sym)[0]
    out = _blank(sym, quantity, None, None, None, ltp)
    try:
        q = int(quantity or 0)
        b, s, tick = _ticks(sym, float(buy_price), float(stoploss) if stoploss else None, conn)
    except (TypeError, ValueError):
        out.update(buy_status="failed", message=MSG["bad_price"])
        return out
    out.update(quantity=q, buy_price=b, stoploss=s, tick_size=tick)
    if q < 1 or b <= 0 or s <= 0 or s >= b:
        out.update(buy_status="failed", message=MSG["bad_price"])
        return out
    if not ltp:
        out.update(buy_status="waiting_for_ltp", message=MSG["no_ltp"])
        return out
    # The broker's read-only state is read before the write lock (network), then every local check runs again inside
    # BEGIN IMMEDIATE, where no other writer can slip a second record in between the check and the insert.
    pre = broker_state(broker, sym)
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        out.update(buy_status="checking", message=f"Busy, try again ({e}). {NO_ORDER}")
        return out
    try:
        # broker None here: its state was read just above (`pre`), so the write lock is never held over a network call
        blocker = _checks(conn, out, None, own_rule=rule_id, check_session=check_session)
        if not blocker:
            if pre["position_quantity"] > 0:
                blocker, out["account_state"] = "position", "position"
            elif pre["holding_quantity"] > 0:
                blocker, out["account_state"] = "holding", "holding"
            elif pre["day_buy_order_id"]:
                blocker, out["account_state"] = "day_order", "open_order"
                out["day_buy_order_id"], out["day_buy_order_status"] = pre["day_buy_order_id"], pre["day_buy_order_status"]
            elif pre["active_gtt_id"]:
                blocker, out["account_state"] = "broker_gtt", "active_gtt"
            elif broker is not None and len(pre["errors"]) >= 4:
                blocker, out["account_state"] = "unreadable", "unknown"
        if blocker:
            conn.rollback()
            if blocker == "already_active":
                out.update(buy_status="already_active", buy_trigger_id=out["active_buy_trigger_id"],
                           stoploss_status="planned_after_buy", message=MSG["already_active"])
            else:
                out.update(buy_status="blocked", message=MSG[blocker])
            return out
        gid = PREFIX + uuid.uuid4().hex[:16].upper()
        now = _now()
        conn.execute(
            "INSERT INTO algo_dummy_gtt (id, kind, symbol, transaction_type, quantity, trigger_price, limit_price, "
            "stop_trigger, tick_size, status, session_id, rule_id, setup, valid_until, last_price, message, created_at, "
            "updated_at) VALUES (?, 'BUY', ?, 'BUY', ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, sym, q, b, b, s, tick, session_id, rule_id, json.dumps(setup, default=str) if setup else None,
             _valid_until(date.today(), valid_sessions, datetime.now()), ltp, MSG["created"], now, now))
        conn.commit()
    except sqlite3.IntegrityError:
        # The partial unique index: another request created the active dummy between our read and our insert.
        conn.rollback()
        act = _active(conn, sym)
        out.update(buy_status="already_active", buy_trigger_id=act["id"] if act else None,
                   active_buy_trigger_id=act["id"] if act else None, account_state="dummy_active",
                   stoploss_status="planned_after_buy", message=MSG["already_active"])
        return out
    except Exception:
        conn.rollback()
        raise
    out.update(buy_status="success", buy_trigger_id=gid, active_buy_trigger_id=gid, account_state="flat",
               stoploss_status="planned_after_buy", message=MSG["created"])
    log.info(f"  algo: {gid} dummy buy {q} {sym} at {b} (stop {s} after the buy) -- {NO_ORDER}")
    return out


def link_rule(conn, gid: str, rule_id: str) -> None:
    conn.execute("UPDATE algo_dummy_gtt SET rule_id = ?, updated_at = ? WHERE id = ?", (rule_id, _now(), gid))
    conn.commit()


def cancel(conn, gid: str, why: str, status: str = CANCELLED, cancel_rule: bool = True) -> dict | None:
    """Close a dummy (and its session's queued entry rule): a record change only."""
    r = conn.execute("SELECT * FROM algo_dummy_gtt WHERE id = ?", (gid,)).fetchone()
    if not r:
        return None
    r = _row(r)
    conn.execute("UPDATE algo_dummy_gtt SET status = ?, message = ?, updated_at = ? WHERE id = ? AND status = 'ACTIVE'",
                 (status, f"{why}. {NO_ORDER}", _now(), gid))
    conn.commit()
    if cancel_rule and r.get("rule_id"):
        try:
            from orders import rules
            cur = rules.get_rule(r["rule_id"])
            if cur and cur.get("status") in ("ACTIVE", "PENDING_CONFIRMATION"):
                rules.cancel_rule(r["rule_id"])
        except Exception as e:
            log.warning(f"  algo: could not cancel rule {r['rule_id']} of {gid}: {e}")
    return r


# -- the monitor's pass -------------------------------------------------------------------------------------------------

def sync(conn, prices: dict | None = None) -> list:
    """Mirror each dummy onto what its session's rule did: an executed buy -> TRIGGERED and a DUMMY- OCO with the
    rule's STOP / TARGET legs; a cancelled / failed / expired rule -> CANCELLED; an OCO whose legs are done -> CLOSED.
    A dummy without a rule (made from the API) fills on the record when prices[symbol] <= trigger."""
    ensure_table(conn)
    from orders import rules
    out, now = [], _now()
    for d in [_row(r) for r in conn.execute("SELECT * FROM algo_dummy_gtt WHERE status = 'ACTIVE'").fetchall()]:
        if d["kind"] == KIND_BUY:
            if d.get("rule_id"):
                r = rules.get_rule(d["rule_id"])
                st = (r or {}).get("status")
                if st == "EXECUTED":
                    conn.execute("UPDATE algo_dummy_gtt SET status = 'TRIGGERED', fill_price = ?, filled_at = ?, "
                                 "quantity = COALESCE(?, quantity), message = ?, updated_at = ? WHERE id = ?",
                                 (r.get("execution_price"), r.get("updated_at") or now, r.get("execution_quantity"),
                                  f"Buy executed in PAPER at {r.get('execution_price')}. {NO_ORDER}", now, d["id"]))
                    out.append({"id": d["id"], "event": "TRIGGERED", "symbol": d["symbol"]})
                    oco = _write_oco(conn, d, r)
                    if oco:
                        out.append({"id": oco, "event": "OCO_PLANNED", "symbol": d["symbol"]})
                elif st in ("CANCELLED", "FAILED", "EXPIRED") or r is None:
                    conn.execute("UPDATE algo_dummy_gtt SET status = 'CANCELLED', message = ?, updated_at = ? WHERE id = ?",
                                 (f"The session's entry rule is {st or 'gone'}. {NO_ORDER}", now, d["id"]))
                    out.append({"id": d["id"], "event": "CANCELLED", "symbol": d["symbol"]})
            elif prices and prices.get(d["symbol"]) and float(prices[d["symbol"]]) <= float(d["trigger_price"]):
                fill = min(float(prices[d["symbol"]]), float(d["trigger_price"]))
                conn.execute("UPDATE algo_dummy_gtt SET status = 'TRIGGERED', fill_price = ?, filled_at = ?, message = ?, "
                             "updated_at = ? WHERE id = ?",
                             (fill, now, f"Dummy filled on the record at {fill} (no session, no paper order). {NO_ORDER}",
                              now, d["id"]))
                _write_oco(conn, {**d, "fill_price": fill}, None)
                out.append({"id": d["id"], "event": "TRIGGERED", "symbol": d["symbol"]})
        elif d["kind"] == KIND_OCO and d.get("rule_id"):
            legs = [_row(x) for x in conn.execute("SELECT * FROM order_rules WHERE parent_rule_id = ?", (d["rule_id"],))]
            live = [l for l in legs if l["status"] in ("ACTIVE", "PENDING_CONFIRMATION")]
            stop = next((l for l in live if l.get("role") == "STOP"), None)
            if legs and not live:
                done = next((l for l in legs if l["status"] == "EXECUTED"), None)
                conn.execute("UPDATE algo_dummy_gtt SET status = 'CLOSED', fill_price = ?, filled_at = ?, message = ?, "
                             "updated_at = ? WHERE id = ?",
                             ((done or {}).get("execution_price"), (done or {}).get("updated_at"),
                              f"{(done or {}).get('role') or 'Exit'} leg done. {NO_ORDER}", now, d["id"]))
                out.append({"id": d["id"], "event": "CLOSED", "symbol": d["symbol"]})
            elif stop and float(stop["resolved_trigger_price"]) != float(d.get("stop_trigger") or 0):
                conn.execute("UPDATE algo_dummy_gtt SET stop_trigger = ?, updated_at = ? WHERE id = ?",
                             (stop["resolved_trigger_price"], now, d["id"]))       # a trailing move, mirrored
    conn.commit()
    return out


def _write_oco(conn, d: dict, rule: dict | None) -> str | None:
    """The DUMMY- two-leg record after the buy: the rule's legs, or for a record-only dummy its planned stop."""
    if _active_oco(conn, d["symbol"]):
        return None
    stop = target = None
    if rule:
        for l in conn.execute("SELECT * FROM order_rules WHERE parent_rule_id = ?", (rule["id"],)).fetchall():
            l = _row(l)
            if l.get("role") == "STOP":
                stop = l["resolved_trigger_price"]
            elif l.get("role") == "TARGET":
                target = l["resolved_trigger_price"]
    stop = stop or d.get("stop_trigger")
    gid, now = PREFIX + uuid.uuid4().hex[:16].upper(), _now()
    conn.execute("INSERT INTO algo_dummy_gtt (id, kind, symbol, transaction_type, quantity, stop_trigger, target_trigger, "
                 "tick_size, status, session_id, rule_id, parent_id, message, created_at, updated_at) "
                 "VALUES (?, 'OCO', ?, 'SELL', ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)",
                 (gid, d["symbol"], (rule or {}).get("execution_quantity") or d.get("quantity"), stop, target,
                  d.get("tick_size"), d.get("session_id"), (rule or {}).get("id"), d["id"],
                  f"Dummy stop{' + target' if target else ''} after the buy (OCO). {NO_ORDER}", now, now))
    conn.commit()
    return gid


def maintain(conn, today: date | None = None) -> list:
    """End-of-validity and invalidation of resting dummy buys: past valid_until -> EXPIRED; the setup superseded by a
    newer breakout of the same stock in the latest scan, or a close under the stop before a fill -> CANCELLED. Each
    takes its session's entry rule with it."""
    ensure_table(conn)
    today = today or date.today()
    out = []
    try:
        from strategy.breakout_retest import latest_scan
        scan = {str(r.get("symbol")).upper(): r for r in ((latest_scan(conn) or {}).get("rows") or [])}
    except Exception:
        scan = None
    for d in [_row(r) for r in conn.execute("SELECT * FROM algo_dummy_gtt WHERE kind = 'BUY' AND status = 'ACTIVE'")]:
        if d.get("valid_until") and today.isoformat() > d["valid_until"]:
            cancel(conn, d["id"], f"Valid for {VALID_SESSIONS} trading days, until {d['valid_until']}", EXPIRED)
            out.append({"id": d["id"], "event": "EXPIRED", "symbol": d["symbol"]})
            continue
        try:
            from execution.positions import latest_close
            close, on = latest_close(conn, d["symbol"])
        except Exception:
            close, on = None, None
        if close and d.get("stop_trigger") and float(close) < float(d["stop_trigger"]) and \
                str(on or "")[:10] >= str(d["created_at"])[:10]:
            cancel(conn, d["id"], f"Close {close} on {on} is under the stop {d['stop_trigger']} before a fill")
            out.append({"id": d["id"], "event": "CANCELLED_STOP", "symbol": d["symbol"]})
            continue
        try:
            mine = json.loads(d.get("setup") or "{}").get("breakoutDate")
        except (TypeError, ValueError):
            mine = None
        new = (scan or {}).get(d["symbol"])
        if scan is not None and mine and new and str(new.get("breakoutDate")) > str(mine):
            cancel(conn, d["id"], f"Superseded by the {new.get('breakoutDate')} breakout")
            out.append({"id": d["id"], "event": "SUPERSEDED", "symbol": d["symbol"]})
    return out


def record_prices(conn) -> dict:
    """Fresh LTPs for the active dummies that have no session rule (they fill on the record), else {}."""
    ensure_table(conn)
    syms = [_row(r)["symbol"] for r in conn.execute(
        "SELECT symbol FROM algo_dummy_gtt WHERE kind = 'BUY' AND status = 'ACTIVE' AND rule_id IS NULL").fetchall()]
    if not syms:
        return {}
    from orders.autopick import fresh_ltp
    return {k: v[0] for k, v in fresh_ltp(conn, syms).items()}


def listing(conn, limit: int = 100, active_only: bool = False) -> list:
    ensure_table(conn)
    q = "SELECT * FROM algo_dummy_gtt" + (" WHERE status = 'ACTIVE'" if active_only else "") + \
        " ORDER BY created_at DESC LIMIT ?"
    return [_row(r) for r in conn.execute(q, (int(limit),)).fetchall()]
