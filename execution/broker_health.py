"""
Broker connection health (BR-06) and the operational-risk gate it feeds (RK-17), W29.

check() runs these and stores one broker_health_check row:

    dhan_market_data   one index quote through Dhan's market-data API (read-only); latency
    dhan_token         Dhan's fund-limits call on the MARKET-DATA client (read-only) --
                       DH-901 / an auth failure means the access token has expired
    index_feed         the index WebSocket (dhan_ws) is running and ticking in the session
    stock_feed         the W27 stock feed (when enabled): mode WS / REST / IDLE
    quote_freshness    newest live_quotes row during the session (stale > stale_minutes)
    zerodha_token      Kite session present and not past its 06:00 IST expiry (when used)
    paper_broker       paper tables readable (the PAPER book of record)

Status per check: OK / DEGRADED / DOWN / SKIPPED (outside the session, or not configured).
overall = worst of the checks that apply; STALE when quotes are stale in the session.
An overall change to DOWN or STALE raises an alert (alert_log + Telegram), and a recovery
says so once.

RK-17: risk_engine asks gate() before every BUY. With execution.block_on_broker_health
(default true) a BUY is REJECTED while the latest check (no older than
broker_health_max_age_minutes) is DOWN or STALE. No recent check -> SKIP, never a block
on missing data. SELLs are never blocked by this gate (exits must stay possible).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as _time, timedelta

log = logging.getLogger("atip.execution")

ORDER = {"OK": 0, "SKIPPED": 0, "DEGRADED": 1, "STALE": 2, "DOWN": 3}
STALE_MINUTES = 20
SESSION = (_time(9, 20), _time(15, 30))


def _in_session(now):
    from utils.trading_calendar import is_trading_day
    return is_trading_day(now.date()) and SESSION[0] <= now.time() <= SESSION[1]


def _c(name, status, detail="", **extra):
    return {"check": name, "status": status, "detail": detail, **extra}


def _dhan_checks(network: bool) -> list:
    if not network:
        return [_c("dhan_market_data", "SKIPPED", "network checks off"), _c("dhan_token", "SKIPPED", "network off")]
    out = []
    try:
        from data.dhan import fetch_index_quotes, get_dhan_client
        dhan, _ = get_dhan_client()
    except Exception as e:
        return [_c("dhan_market_data", "DOWN", f"no Dhan client: {e}"), _c("dhan_token", "DOWN", str(e))]
    t0 = time.monotonic()
    try:
        df = fetch_index_quotes(["nifty50"], dhan)
        ms = round((time.monotonic() - t0) * 1000)
        out.append(_c("dhan_market_data", "OK" if not df.empty else "DOWN",
                      "NIFTY quote" if not df.empty else "empty quote response", latency_ms=ms))
    except Exception as e:
        out.append(_c("dhan_market_data", "DOWN", f"{type(e).__name__}: {e}"))
    try:
        resp = dhan.get_fund_limits()
        txt = json.dumps(resp, default=str)[:300]
        if resp and resp.get("status") != "failure":
            out.append(_c("dhan_token", "OK", "fund-limits call accepted"))
        elif "DH-901" in txt or "nvalid" in txt or "xpired" in txt:
            out.append(_c("dhan_token", "DOWN", "access token rejected (DH-901 / expired) -- renew it"))
        else:
            out.append(_c("dhan_token", "DEGRADED", f"fund-limits failed: {txt[:160]}"))
    except Exception as e:
        out.append(_c("dhan_token", "DEGRADED", f"{type(e).__name__}: {e}"))
    return out


def _feeds(now, in_session) -> list:
    out = []
    try:
        from data.dhan_ws import get_index_feed_manager
        m = get_index_feed_manager()
        if not in_session:
            out.append(_c("index_feed", "SKIPPED", "outside the session"))
        elif m is None:
            out.append(_c("index_feed", "DEGRADED", "index feed not running in this process"))
        else:
            n = len(m.snapshot())
            out.append(_c("index_feed", "OK" if n else "DEGRADED", f"{n} indexes ticking"))
    except Exception as e:
        out.append(_c("index_feed", "DEGRADED", str(e)))
    try:
        from data.stock_feed import feed_status
        fs = feed_status()
        if not fs.get("running"):
            out.append(_c("stock_feed", "SKIPPED", "stock feed not enabled"))
        else:
            st = "OK" if fs.get("mode") == "WS" else "DEGRADED" if fs.get("mode") == "REST" else "SKIPPED"
            out.append(_c("stock_feed", st, f"mode {fs.get('mode')}: {fs.get('detail')}"))
    except Exception as e:
        out.append(_c("stock_feed", "SKIPPED", str(e)))
    return out


def _freshness(conn, now, in_session):
    r = conn.execute("SELECT MAX(REPLACE(SUBSTR(timestamp,1,19),'T',' ')) FROM live_quotes").fetchone()
    last = r[0] if r else None
    if not in_session:
        return _c("quote_freshness", "SKIPPED", f"outside the session (last quote {last})")
    if not last:
        return _c("quote_freshness", "STALE", "no live quotes at all")
    age = (now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
    return _c("quote_freshness", "STALE" if age > STALE_MINUTES else "OK", f"newest quote {age:.0f} min old",
              age_minutes=round(age, 1))


def _zerodha():
    try:
        from portfolio.zerodha import token_status
        t = token_status()
    except Exception as e:
        return _c("zerodha_token", "SKIPPED", str(e))
    if t["state"] == "MISSING":
        return _c("zerodha_token", "SKIPPED", "Zerodha not connected")
    return _c("zerodha_token", "OK" if t["state"] == "VALID" else "DEGRADED", t["detail"])


def check(network: bool = True, now=None, notify: bool = True) -> dict:
    from db.schema import get_connection
    now = now or datetime.now()
    in_s = _in_session(now)
    conn = get_connection()
    try:
        checks = _dhan_checks(network) + _feeds(now, in_s) + [_freshness(conn, now, in_s), _zerodha()]
        try:
            conn.execute("SELECT COUNT(*) FROM paper_order").fetchone()
            checks.append(_c("paper_broker", "OK", "paper book readable"))
        except Exception as e:
            checks.append(_c("paper_broker", "DEGRADED", str(e)))
        overall = max((c["status"] for c in checks), key=lambda s: ORDER.get(s, 0))
        overall = "OK" if overall == "SKIPPED" else overall
        prev = conn.execute("SELECT overall FROM broker_health_check ORDER BY checked_at DESC LIMIT 1").fetchone()
        conn.execute("INSERT INTO broker_health_check (checked_at,overall,in_session,checks_json) VALUES (?,?,?,?)",
                     (now, overall, 1 if in_s else 0, json.dumps(checks, default=str)))
        conn.commit()
        if notify and prev and prev[0] != overall and (overall in ("DOWN", "STALE") or prev[0] in ("DOWN", "STALE")):
            bad = [f"{c['check']}: {c['status']} {c['detail']}" for c in checks if ORDER.get(c["status"], 0) >= 2]
            try:
                from alerts.telegram import notify as _notify
                _notify(f"{'🔴' if overall in ('DOWN', 'STALE') else '🟢'} <b>Broker health {prev[0]} → {overall}</b>"
                        + ("\n" + "\n".join(bad) if bad else ""), category="broker", severity=
                        "warning" if overall in ("DOWN", "STALE") else "info", key=f"broker-{overall}-{now:%Y%m%d%H}")
            except Exception as e:
                log.warning(f"  broker health alert: {e}")
        return {"status": "SUCCESS", "rows": len(checks), "overall": overall, "in_session": in_s, "checks": checks,
                "checked_at": now}
    finally:
        conn.close()


def latest(conn) -> dict | None:
    r = conn.execute("SELECT checked_at, overall, in_session, checks_json FROM broker_health_check ORDER BY "
                     "checked_at DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {"checked_at": r[0], "overall": r[1], "in_session": bool(r[2]), "checks": json.loads(r[3] or "[]")}


def history(conn, hours=24) -> list:
    since = datetime.now() - timedelta(hours=int(hours))
    return [{"checked_at": a, "overall": b} for a, b in conn.execute(
        "SELECT checked_at, overall FROM broker_health_check WHERE checked_at>=? ORDER BY checked_at", (since,))]


def gate(conn, side: str) -> tuple:
    """(status PASS / FAIL / SKIP, message) for the RK-17 risk check."""
    from execution.config import execution_settings
    s = execution_settings()
    if side != "BUY":
        return "SKIP", "exits are never blocked by broker health"
    if not s.get("block_on_broker_health", True):
        return "SKIP", "execution.block_on_broker_health is false"
    last = latest(conn)
    if not last:
        return "SKIP", "no broker health check yet"
    age = (datetime.now() - (last["checked_at"] if isinstance(last["checked_at"], datetime) else
                             datetime.fromisoformat(str(last["checked_at"])))).total_seconds() / 60
    if age > float(s.get("broker_health_max_age_minutes") or 30):
        return "SKIP", f"last broker health check is {age:.0f} min old"
    if last["overall"] in ("DOWN", "STALE"):
        bad = ", ".join(c["check"] for c in last["checks"] if c["status"] in ("DOWN", "STALE"))
        return "FAIL", f"broker health {last['overall']} ({bad}) as of {age:.0f} min ago"
    return "PASS", f"broker health {last['overall']} ({age:.0f} min ago)"


def run_scheduled() -> dict:
    return check(network=True)
