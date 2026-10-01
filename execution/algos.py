"""
Execution algorithms (W34: EX-11) -- PAPER only.

An APPROVED risk decision normally becomes one order (W4). When the algo policy selects it
(select(): execution.algo.enabled and the order is large -- by value or by the EX-12 impact
model's participation of ADV), it becomes an ALGO PARENT instead, worked in child orders over
the session:

    TWAP      target(t) = total x elapsed / duration, child every interval_min
    VWAP      target(t) = total x the stock's cumulative intraday volume share at t, from the
              mean 15-min slot volumes of the last PROFILE_SESSIONS sessions (intraday_bars);
              falls back to TWAP when fewer than MIN_PROFILE_SESSIONS sessions are stored
    POV       target(t) = rate x the market volume traded since start (today's stored 15-min
              bars), never above total
    ICEBERG   one resting LIMIT child of display_qty at a time (limit = params.limit_price or the
              reference price); the next slice goes in when the working one fills

Risk: the parent is the risk-approved quantity; children only ever sum to it. Each child is an
ordinary oms_order (MARKET, or LIMIT for ICEBERG) linked by algo_parent_id with a synthetic
intent / risk-decision id (`<id>:<slice>`), so the W4 audit chain, fills, slippage analytics and
reconciliation all see it. A child REJECTED / FAILED pauses the parent (PAUSED, reason recorded):
nothing is retried blindly.

Lifecycle: WAITING (before start_at) -> WORKING -> COMPLETED (filled) | EXPIRED (end_at passed
with quantity left and complete_at_end false) | PAUSED | CANCELLED (owner). With complete_at_end
true (default) whatever is left at end_at goes as one final MARKET child.

tick(conn, now) runs every minute in session (scheduler) and after event dispatch: it brings each
WORKING parent's submitted quantity up to its schedule target. Parent fill progress is recomputed
from its children on every tick (the EX-16 fill events only make it quicker), so a missed event
cannot leave a parent wrong.

Config (config.json "execution"."algo", all optional):
    {"enabled": false, "default_algo": "TWAP", "min_order_value": 500000, "min_participation": 0.02,
     "duration_min": 60, "interval_min": 5, "pov_rate": 0.1, "iceberg_display_pct": 10,
     "complete_at_end": true, "min_child_qty": 1}
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import date, datetime, time as _time, timedelta

log = logging.getLogger("atip.execution.algos")

ALGOS = ("TWAP", "VWAP", "POV", "ICEBERG")
WAITING, WORKING, COMPLETED, EXPIRED, PAUSED, CANCELLED = ("WAITING", "WORKING", "COMPLETED", "EXPIRED", "PAUSED",
                                                           "CANCELLED")
OPEN_STATES = (WAITING, WORKING, PAUSED)
SESSION_OPEN, SESSION_CLOSE = _time(9, 15), _time(15, 30)
LAST_ENTRY = _time(15, 20)
PROFILE_SESSIONS = 20
MIN_PROFILE_SESSIONS = 5
DEFAULTS = {"enabled": False, "default_algo": "TWAP", "min_order_value": 500000, "min_participation": 0.02,
            "duration_min": 60, "interval_min": 5, "pov_rate": 0.1, "iceberg_display_pct": 10,
            "complete_at_end": True, "min_child_qty": 1}


def settings() -> dict:
    from execution.config import execution_settings
    raw = execution_settings().get("algo") or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out["enabled"] = out.get("enabled") is True
    return out


def _session_bounds(d: date):
    return datetime.combine(d, SESSION_OPEN), datetime.combine(d, SESSION_CLOSE)


def next_session_start(now: datetime) -> datetime:
    from utils.trading_calendar import is_trading_day
    d = now.date()
    if is_trading_day(d) and now.time() < LAST_ENTRY:
        return max(now, datetime.combine(d, SESSION_OPEN))
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, SESSION_OPEN)


# ── policy ────────────────────────────────────────────────────────────────
def select(conn, risk_decision_id: str) -> dict | None:
    """{"algo", "params"} when this approved decision should be worked by an algo, else None."""
    s = settings()
    if not s["enabled"]:
        return None
    rd = conn.execute("SELECT * FROM risk_decision WHERE risk_decision_id=?", (risk_decision_id,)).fetchone()
    if not rd:
        return None
    rd = dict(rd)
    if rd.get("mode") == "LIVE" or rd.get("action") in ("SHORT", "COVER"):
        return None                                   # LIVE is not built; futures legs fill at EOD
    qty, px = int(rd["approved_quantity"] or 0), float(rd["reference_price"] or 0)
    if qty <= 1:
        return None
    from execution.impact import estimate
    est = estimate(conn, rd["symbol"], qty, rd["side"], px)
    big_value = qty * px >= float(s["min_order_value"])
    big_part = bool(est.get("ok") and (est.get("participation") or 0) >= float(s["min_participation"]))
    if not (big_value or big_part):
        return None
    algo = str(s["default_algo"]).upper()
    params = {"duration_min": int(s["duration_min"]), "interval_min": int(s["interval_min"]),
              "complete_at_end": bool(s["complete_at_end"]), "min_child_qty": int(s["min_child_qty"]),
              "selected_because": ("value" if big_value else "") + (" participation" if big_part else ""),
              "impact": {k: est.get(k) for k in ("participation", "total_bps", "cost_rs", "y_source")}}
    if algo == "POV":
        params["rate"] = float(s["pov_rate"])
    if algo == "ICEBERG":
        params["display_qty"] = max(1, int(qty * float(s["iceberg_display_pct"]) / 100))
    return {"algo": algo, "params": params}


# ── parent lifecycle ──────────────────────────────────────────────────────
def get_parent(conn, parent_id: str) -> dict:
    r = conn.execute("SELECT * FROM exec_algo_parent WHERE parent_id=?", (parent_id,)).fetchone()
    if not r:
        raise LookupError(f"no algo parent {parent_id}")
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    return d


def _set(conn, parent_id, publish_event=True, **fields):
    fields["updated_at"] = datetime.now()
    conn.execute(f"UPDATE exec_algo_parent SET {', '.join(f'{k}=?' for k in fields)} WHERE parent_id=?",
                 list(fields.values()) + [parent_id])
    if publish_event and "status" in fields:
        from execution.events import publish
        p = conn.execute("SELECT status, filled_qty, total_qty FROM exec_algo_parent WHERE parent_id=?",
                         (parent_id,)).fetchone()
        publish(conn, "algo.parent", parent_id, {"parent_id": parent_id, "status": p[0], "filled_qty": p[1],
                                                 "total_qty": p[2], "reason": fields.get("reason")})
    conn.commit()


def start_algo(conn, risk_decision_id: str, algo: str, params: dict | None = None, now=None) -> dict:
    algo = (algo or "").upper()
    if algo not in ALGOS:
        raise ValueError(f"algo must be one of {ALGOS}")
    rd = conn.execute("SELECT * FROM risk_decision WHERE risk_decision_id=?", (risk_decision_id,)).fetchone()
    if not rd:
        raise LookupError(f"no risk decision {risk_decision_id}")
    rd = dict(rd)
    if rd["risk_status"] != "APPROVED":
        raise ValueError(f"risk decision {risk_decision_id} is {rd['risk_status']}; only APPROVED decisions are worked")
    if rd.get("mode") == "LIVE":
        raise ValueError("execution algos are PAPER only")
    ex = conn.execute("SELECT order_id FROM oms_order WHERE risk_decision_id=? OR intent_id=?",
                      (risk_decision_id, rd["intent_id"])).fetchone()
    if ex or conn.execute("SELECT 1 FROM exec_algo_parent WHERE risk_decision_id=?", (risk_decision_id,)).fetchone():
        raise ValueError(f"risk decision {risk_decision_id} already has an order or an algo parent")
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (params or {}).items()})
    now = now or datetime.now()
    start = next_session_start(now)
    _, close = _session_bounds(start.date())
    end = min(start + timedelta(minutes=int(p.get("duration_min") or 60)), close - timedelta(minutes=5))
    if end <= start:
        end = close - timedelta(minutes=5)
    pid = "ALG" + uuid.uuid4().hex[:13].upper()
    from execution.impact import estimate
    est = estimate(conn, rd["symbol"], int(rd["approved_quantity"]), rd["side"], rd["reference_price"])
    conn.execute("INSERT INTO exec_algo_parent (parent_id,risk_decision_id,intent_id,decision_id,strategy_id,"
                 "strategy_version,symbol,side,total_qty,algo,params_json,start_at,end_at,status,reference_price,"
                 "impact_estimate_json,mode,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, risk_decision_id, rd["intent_id"], rd["decision_id"], rd["strategy_id"], rd["strategy_version"],
                  rd["symbol"], rd["side"], int(rd["approved_quantity"]), algo, json.dumps(p, default=str), start, end,
                  WAITING, rd["reference_price"], json.dumps({k: v for k, v in est.items() if k != "inputs"}),
                  "PAPER", now, now))
    conn.commit()
    log.info(f"  algo {pid}: {algo} {rd['side']} {rd['approved_quantity']} {rd['symbol']} {start:%d %b %H:%M}-{end:%H:%M}")
    return get_parent(conn, pid)


def children(conn, parent_id) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM oms_order WHERE algo_parent_id=? ORDER BY algo_slice",
                                          (parent_id,))]


def _progress(conn, p) -> tuple:
    """(filled, working, avg_price) from the children."""
    from execution.models import TERMINAL
    filled = working = 0
    notional = 0.0
    for c in children(conn, p["parent_id"]):
        f = int(c["filled_quantity"] or 0)
        filled += f
        notional += f * float(c["avg_fill_price"] or 0)
        if c["status"] not in TERMINAL:
            working += int(c["quantity"]) - f
    return filled, working, (round(notional / filled, 4) if filled else None)


# ── schedules ─────────────────────────────────────────────────────────────
def volume_profile(conn, symbol, as_of: date) -> list | None:
    """[(slot 'HH:MM', cumulative share)] from the last PROFILE_SESSIONS sessions' 15-min bars."""
    rows = conn.execute("SELECT substr(ts,1,10) d, substr(ts,12,5) slot, volume FROM intraday_bars WHERE symbol=? AND "
                        "interval_min=15 AND ts<? AND ts>=date(?, '-45 day')",
                        (symbol, f"{as_of} 00:00:00", str(as_of))).fetchall()
    days = sorted({r[0] for r in rows})[-PROFILE_SESSIONS:]
    if len(days) < MIN_PROFILE_SESSIONS:
        return None
    keep = set(days)
    by_slot = {}
    for d, slot, v in rows:
        if d in keep:
            by_slot[slot] = by_slot.get(slot, 0.0) + float(v or 0)
    tot = sum(by_slot.values())
    if not tot:
        return None
    cum, out = 0.0, []
    for slot in sorted(by_slot):
        cum += by_slot[slot] / tot
        out.append((slot, cum))
    return out


def _profile_share(profile, start: datetime, end: datetime, now: datetime) -> float:
    """Share of [start, end]'s expected volume traded by `now` (bar slots, linearly within a slot)."""
    def at(t: datetime) -> float:
        hm = t.strftime("%H:%M")
        prev_cum, prev_slot = 0.0, None
        for slot, cum in profile:
            if hm < slot:
                return prev_cum
            prev_cum, prev_slot = cum, slot
        return prev_cum
    a, b, x = at(start), at(end), at(min(now, end))
    return 1.0 if b <= a else max(0.0, min(1.0, (x - a) / (b - a)))


def market_volume_since(conn, symbol, start: datetime) -> float:
    r = conn.execute("SELECT SUM(volume) FROM intraday_bars WHERE symbol=? AND interval_min=15 AND ts>=? AND ts<?",
                     (symbol, start.strftime("%Y-%m-%d %H:%M:%S"),
                      (start.replace(hour=23, minute=59)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    return float(r[0] or 0)


def target_qty(conn, p: dict, now: datetime) -> int:
    total = int(p["total_qty"])
    start, end = _dt(p["start_at"]), _dt(p["end_at"])
    if now >= end:
        return total
    if now <= start:
        return 0
    algo, prm = p["algo"], p["params"]
    if algo == "POV":
        return min(total, int(float(prm.get("rate") or 0.1) * market_volume_since(conn, p["symbol"], start)))
    if algo == "ICEBERG":
        return total                                  # gated by display size, not by time
    frac = (now - start).total_seconds() / max(1.0, (end - start).total_seconds())
    if algo == "VWAP":
        prof = volume_profile(conn, p["symbol"], start.date())
        if prof:
            frac = _profile_share(prof, start, end, now)
    step = max(1, int(prm.get("interval_min") or 5))
    slots = max(1, math.ceil((end - start).total_seconds() / 60 / step))
    done_slots = math.floor((now - start).total_seconds() / 60 / step) + 1
    frac = min(frac, done_slots / slots) if algo == "TWAP" else frac
    return min(total, int(math.ceil(total * frac)))


def _dt(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:19].replace(" ", "T"))


# ── children ──────────────────────────────────────────────────────────────
def _child(conn, p: dict, qty: int, order_type="MARKET", limit_price=None, final=False) -> dict:
    from execution import order_manager as OM
    from execution.config import execution_settings
    n = int(p["child_count"] or 0) + 1
    oid = "OMS" + uuid.uuid4().hex[:14].upper()
    now = datetime.now()
    s = execution_settings()
    conn.execute("INSERT INTO oms_order (order_id,intent_id,risk_decision_id,decision_id,strategy_id,strategy_version,"
                 "symbol,side,quantity,order_type,limit_price,trigger_price,product_type,mode,adapter,status,"
                 "reference_price,instrument,algo_parent_id,algo_slice,created_at,updated_at) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, f"{p['intent_id']}:{n}", f"{p['risk_decision_id']}:{n}", p["decision_id"], p["strategy_id"],
                  p["strategy_version"], p["symbol"], p["side"], int(qty), order_type,
                  limit_price if order_type == "LIMIT" else None, None, s["product_type"], "PAPER", "paper", "CREATED",
                  p["reference_price"], "CASH", p["parent_id"], n, now, now))
    conn.execute("INSERT INTO oms_order_event (order_id,from_status,to_status,message,details_json,actor,at) "
                 "VALUES (?,?,?,?,?,?,?)", (oid, None, "CREATED", f"{p['algo']} slice {n} of {p['parent_id']}"
                                            + (" (final)" if final else ""),
                                            json.dumps({"algo_parent_id": p["parent_id"], "slice": n}), "algo", now))
    _set(conn, p["parent_id"], publish_event=False, child_count=n)
    return OM.submit_order(conn, oid)


def _halt_on_failed_child(conn, p) -> bool:
    """A REJECTED / FAILED child after the last resume pauses the parent."""
    after = int(p["params"].get("resumed_after_slice") or 0)
    bad = conn.execute("SELECT order_id, status, reason FROM oms_order WHERE algo_parent_id=? AND algo_slice>? AND "
                       "status IN ('REJECTED','FAILED') ORDER BY algo_slice DESC LIMIT 1",
                       (p["parent_id"], after)).fetchone()
    if bad:
        _set(conn, p["parent_id"], status=PAUSED, reason=f"child {bad[0]} {bad[1]}: {bad[2] or ''}"[:500])
        return True
    return False


def work_parent(conn, p: dict, now: datetime) -> dict:
    filled, working, avg = _progress(conn, p)
    if (filled, avg) != (p["filled_qty"], p["avg_price"]):
        _set(conn, p["parent_id"], publish_event=False, filled_qty=filled, avg_price=avg)
    total = int(p["total_qty"])
    if filled >= total:
        _set(conn, p["parent_id"], status=COMPLETED, reason="filled")
        return {"parent_id": p["parent_id"], "status": COMPLETED}
    if p["status"] == PAUSED or _halt_on_failed_child(conn, p):
        return {"parent_id": p["parent_id"], "status": PAUSED}
    start, end = _dt(p["start_at"]), _dt(p["end_at"])
    if now < start:
        return {"parent_id": p["parent_id"], "status": WAITING}
    if p["status"] == WAITING:
        _set(conn, p["parent_id"], status=WORKING)
    prm = p["params"]
    if now >= end:
        left = total - filled - working
        if left > 0 and prm.get("complete_at_end", True) and now.time() < SESSION_CLOSE:
            _child(conn, p, left, final=True)
            return {"parent_id": p["parent_id"], "status": WORKING, "final_child": left}
        if working == 0:
            _set(conn, p["parent_id"], status=EXPIRED if filled < total else COMPLETED,
                 reason=f"end reached with {total - filled} unfilled")
            return {"parent_id": p["parent_id"], "status": EXPIRED}
        return {"parent_id": p["parent_id"], "status": WORKING}
    if p["algo"] == "ICEBERG":
        if working == 0:
            show = min(int(prm.get("display_qty") or max(1, total // 10)), total - filled)
            lim = float(prm.get("limit_price") or p["reference_price"])
            _child(conn, p, show, "LIMIT", lim)
        return {"parent_id": p["parent_id"], "status": WORKING}
    want = target_qty(conn, p, now) - filled - working
    if want >= max(1, int(prm.get("min_child_qty") or 1)):
        _child(conn, p, want)
        return {"parent_id": p["parent_id"], "status": WORKING, "child_qty": want}
    return {"parent_id": p["parent_id"], "status": WORKING}


def tick(conn=None, now=None) -> dict:
    from ops.latency import timed
    from db.schema import get_connection
    from utils.trading_calendar import is_trading_day
    own = conn is None
    conn = conn or get_connection()
    now = now or datetime.now()
    try:
        if not is_trading_day(now.date()) or not (SESSION_OPEN <= now.time() <= SESSION_CLOSE):
            return {"status": "SKIPPED", "rows": 0, "reason": "outside the session"}
        out = []
        with timed("algo.tick"):
            for (pid,) in conn.execute("SELECT parent_id FROM exec_algo_parent WHERE status IN (?,?) ORDER BY start_at",
                                       (WAITING, WORKING)).fetchall():
                try:
                    out.append(work_parent(conn, get_parent(conn, pid), now))
                except Exception as e:
                    log.exception(f"  algo {pid}: tick failed")
                    _set(conn, pid, status=PAUSED, reason=f"tick error: {type(e).__name__}: {e}"[:500])
        return {"status": "SUCCESS", "rows": len(out), "parents": out}
    finally:
        if own:
            conn.close()


def cancel(conn, parent_id: str, reason="cancelled by owner") -> dict:
    from execution import order_manager as OM
    from execution.models import TERMINAL
    p = get_parent(conn, parent_id)
    if p["status"] not in OPEN_STATES:
        raise ValueError(f"algo {parent_id} is {p['status']}")
    for c in children(conn, parent_id):
        if c["status"] not in TERMINAL:
            try:
                OM.cancel_order(conn, c["order_id"], f"algo parent cancelled: {reason}")
            except Exception as e:
                log.warning(f"  cancel child {c['order_id']}: {e}")
    filled, _, avg = _progress(conn, p)
    _set(conn, parent_id, status=CANCELLED, reason=reason, filled_qty=filled, avg_price=avg)
    return get_parent(conn, parent_id)


def resume(conn, parent_id: str, reason="resumed by owner") -> dict:
    p = get_parent(conn, parent_id)
    if p["status"] != PAUSED:
        raise ValueError(f"algo {parent_id} is {p['status']}, not PAUSED")
    # failed slices up to now stay linked (and count as unfilled); only later failures pause it again
    prm = dict(p["params"], resumed_after_slice=int(p["child_count"] or 0))
    _set(conn, parent_id, status=WORKING, reason=reason, params_json=json.dumps(prm, default=str))
    return get_parent(conn, parent_id)


def report(conn, parent_id: str) -> dict:
    """Parent, children, and execution quality: average price vs arrival (reference) and vs the
    interval VWAP of the stored 15-min bars over the parent's window."""
    p = get_parent(conn, parent_id)
    kids = children(conn, parent_id)
    filled, working, avg = _progress(conn, p)
    start, end = _dt(p["start_at"]), _dt(p["end_at"])
    bars = conn.execute("SELECT close, volume FROM intraday_bars WHERE symbol=? AND interval_min=15 AND ts>=? AND ts<=?",
                        (p["symbol"], start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    vol = sum(float(v or 0) for _, v in bars)
    ivwap = (sum(float(c) * float(v or 0) for c, v in bars) / vol) if vol else None
    sign = 1 if p["side"] == "BUY" else -1
    q = {"arrival_price": p["reference_price"], "avg_price": avg, "interval_vwap": round(ivwap, 4) if ivwap else None,
         "vs_arrival_bps": round(sign * (avg - p["reference_price"]) / p["reference_price"] * 1e4, 2)
         if (avg and p["reference_price"]) else None,
         "vs_vwap_bps": round(sign * (avg - ivwap) / ivwap * 1e4, 2) if (avg and ivwap) else None,
         "estimated_bps": json.loads(p["impact_estimate_json"] or "{}").get("total_bps")}
    return {"parent": p, "filled_qty": filled, "working_qty": working, "children": kids, "quality": q}


def list_parents(conn, status=None, limit=100) -> list:
    sql = "SELECT parent_id FROM exec_algo_parent"
    args = []
    if status:
        sql += " WHERE status=?"
        args.append(status.upper())
    return [get_parent(conn, r[0]) for r in conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", args + [int(limit)])]


def run_scheduled() -> dict:
    """Scheduler: deliver pending OMS events, then work every parent."""
    from execution.events import dispatch
    dispatch()
    return tick()
