"""
Event-driven OMS (W34: EX-16): a transactional outbox + in-process dispatcher.

Until W34 the OMS was call-and-return: the pipeline created an order, submitted it, and
whoever needed to react to a fill (protective stops, P&L, analytics) polled the tables
afterwards. Now every order state change and every fill is also an EVENT:

    publish(conn, topic, key, payload)   inserts into oms_event_outbox WITHOUT committing, so it
                                         lands in the same transaction as the state change that
                                         caused it (order_manager.transition / _record_fill commit
                                         right after) -- no event without its change, no change
                                         without its event
    subscribe(topic, name)(fn)           register a handler; topic "*" receives everything
    dispatch(conn, limit)                deliver undelivered events in order to every handler;
                                         at-least-once: a handler that raises is retried on the
                                         next drain (up to MAX_ATTEMPTS), and oms_event_delivery
                                         records (event, handler) -> OK / FAILED so a handler that
                                         succeeded is never called twice for the same event
    replay(conn, topic, since)           re-deliver past events to a NEW handler (it has no
                                         delivery rows yet), e.g. to backfill an analytics view

Topics published in W34:
    order.state    {order_id, from, to, symbol, side, quantity, strategy_id, algo_parent_id, message}
    order.fill     {order_id, fill_qty, price, fees, symbol, side, strategy_id, algo_parent_id}
    algo.parent    {parent_id, status, filled_qty, total_qty}           (execution/algos.py)

Built-in handlers (execution/event_handlers.py): algo parent progress on child fills and
terminal child states (EX-11), submit->fill latency (EX-15), and a fill/reject audit line.

The dispatcher runs from the scheduler every minute in session (and after each algo tick);
dispatch() is also safe to call from anywhere. Handlers must be quick and must not publish
to the topic they consume.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("atip.execution.events")

MAX_ATTEMPTS = 5
_HANDLERS = {}            # topic -> [(name, fn)]


def subscribe(topic: str, name: str):
    def deco(fn):
        lst = _HANDLERS.setdefault(topic, [])
        if not any(n == name for n, _ in lst):
            lst.append((name, fn))
        return fn
    return deco


def handlers_for(topic: str) -> list:
    return list(_HANDLERS.get(topic, [])) + list(_HANDLERS.get("*", []))


def publish(conn, topic: str, key: str | None, payload: dict) -> str:
    """Insert the event; the CALLER commits (same transaction as the change it describes)."""
    eid = "EV" + uuid.uuid4().hex[:18].upper()
    try:
        conn.execute("INSERT INTO oms_event_outbox (event_id,topic,key,payload_json,created_at) VALUES (?,?,?,?,?)",
                     (eid, topic, key, json.dumps(payload, default=str), datetime.now()))
    except Exception as e:                    # the outbox must never break an order state change
        log.warning(f"  event {topic} {key} not recorded: {e}")
        return ""
    return eid


def _load_handlers():
    import execution.event_handlers  # noqa: F401  (registers the built-in handlers)


def dispatch(conn=None, limit: int = 500) -> dict:
    from ops.latency import timed
    from db.schema import get_connection
    _load_handlers()
    own = conn is None
    conn = conn or get_connection()
    delivered = failed = events = 0
    try:
        with timed("events.dispatch"):
            rows = conn.execute("SELECT seq, event_id, topic, key, payload_json, attempts FROM oms_event_outbox WHERE "
                                "dispatched_at IS NULL AND attempts<? ORDER BY seq LIMIT ?",
                                (MAX_ATTEMPTS, int(limit))).fetchall()
            for seq, eid, topic, key, pj, attempts in rows:
                events += 1
                payload = json.loads(pj or "{}")
                done = {r[0] for r in conn.execute("SELECT handler FROM oms_event_delivery WHERE event_id=? AND "
                                                   "status='OK'", (eid,))}
                all_ok, last_err = True, None
                for name, fn in handlers_for(topic):
                    if name in done:
                        continue
                    try:
                        fn(conn, {"event_id": eid, "topic": topic, "key": key, "payload": payload})
                        st, err = "OK", None
                        delivered += 1
                    except Exception as e:
                        st, err = "FAILED", f"{type(e).__name__}: {e}"[:500]
                        all_ok, last_err = False, err
                        failed += 1
                        log.warning(f"  event {topic} {key} handler {name}: {err}")
                    conn.execute("INSERT INTO oms_event_delivery (event_id,handler,status,attempts,error,at) VALUES "
                                 "(?,?,?,1,?,?) ON CONFLICT(event_id,handler) DO UPDATE SET status=excluded.status,"
                                 "attempts=attempts+1,error=excluded.error,at=excluded.at",
                                 (eid, name, st, err, datetime.now()))
                conn.execute("UPDATE oms_event_outbox SET attempts=attempts+1, last_error=?, dispatched_at=? WHERE seq=?",
                             (last_err, datetime.now() if all_ok else None, seq))
                conn.commit()
        return {"status": "SUCCESS", "rows": events, "delivered": delivered, "failed": failed}
    finally:
        if own:
            conn.close()


def replay(conn, handler_name: str, topic: str | None = None, since=None, limit: int = 5000) -> dict:
    """Deliver past events to one handler that has not seen them (no OK delivery row)."""
    _load_handlers()
    sql = "SELECT event_id, topic, key, payload_json FROM oms_event_outbox WHERE 1=1"
    args = []
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    if since:
        sql += " AND created_at>=?"
        args.append(str(since))
    n = 0
    for eid, tp, key, pj in conn.execute(sql + " ORDER BY seq LIMIT ?", args + [int(limit)]).fetchall():
        h = next((fn for name, fn in handlers_for(tp) if name == handler_name), None)
        if h is None:
            continue
        if conn.execute("SELECT 1 FROM oms_event_delivery WHERE event_id=? AND handler=? AND status='OK'",
                        (eid, handler_name)).fetchone():
            continue
        h(conn, {"event_id": eid, "topic": tp, "key": key, "payload": json.loads(pj or "{}")})
        conn.execute("INSERT OR REPLACE INTO oms_event_delivery (event_id,handler,status,attempts,error,at) VALUES "
                     "(?,?,'OK',1,NULL,?)", (eid, handler_name, datetime.now()))
        n += 1
    conn.commit()
    return {"replayed": n, "handler": handler_name}


def stats(conn, hours: int = 24) -> dict:
    since = datetime.now() - timedelta(hours=int(hours))
    by_topic = [dict(zip(("topic", "events", "pending", "failed_attempts"), r)) for r in conn.execute(
        "SELECT topic, COUNT(*), SUM(dispatched_at IS NULL), SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM oms_event_outbox WHERE created_at>=? GROUP BY topic", (since,))]
    stuck = [dict(zip(("event_id", "topic", "key", "attempts", "last_error"), r)) for r in conn.execute(
        "SELECT event_id, topic, key, attempts, last_error FROM oms_event_outbox WHERE dispatched_at IS NULL AND "
        "attempts>=? ORDER BY seq DESC LIMIT 50", (MAX_ATTEMPTS,))]
    _load_handlers()
    return {"hours": hours, "by_topic": by_topic, "dead_letters": stuck,
            "handlers": {t: [n for n, _ in hs] for t, hs in _HANDLERS.items()}}


def recent(conn, topic=None, key=None, limit=200) -> list:
    sql = "SELECT seq, event_id, topic, key, payload_json, created_at, dispatched_at, attempts, last_error FROM oms_event_outbox WHERE 1=1"
    args = []
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    if key:
        sql += " AND key=?"
        args.append(key)
    out = []
    for r in conn.execute(sql + " ORDER BY seq DESC LIMIT ?", args + [int(limit)]):
        d = dict(zip(("seq", "event_id", "topic", "key", "payload", "created_at", "dispatched_at", "attempts",
                      "last_error"), r))
        d["payload"] = json.loads(d["payload"] or "{}")
        out.append(d)
    return out


def run_scheduled() -> dict:
    return dispatch()
