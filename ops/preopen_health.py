"""
The pre-open health gate (owner item 6, 16 Nov 2026): at 08:45 IST on a market day, before anything trades, check
that ATIP can trade today. If a check fails, auto-pick waits and every page says why; the token renewal is retried
and the owner is told on Telegram.

    dhan_token    Dhan's /v2/profile answers with ATIP's token (read-only). A refusal is DH-901 -- 29 Oct to
                  30 Oct the token was dead and nothing said so until the dashboard banner (ops/dhan_token.py).
                  On a failure the scheduled run starts tools/dhan_token_refresh.py (RenewToken, then PIN + TOTP;
                  its own daily cap on rejected codes holds: Dhan locks the login after 5) and checks again.
    quote_feed    a REST quote for RELIANCE comes back with a last price, and its last trade is from the previous
                  session or later -- a quote stuck on an older day is a stale feed
    scrip_master  today's instrument master is stored (data/instruments.py, 08:00); a missing or older one is
                  downloaded now (Dhan rebuilds the file around 08:20 IST)
    scores        the previous session has ai_scores rows: auto-pick ranks on them

    gate_status(conn, today=None) -> {"ok", "reason", "checked_at", "checks": [...], ...}
        What orders/autopick.py asks before a pass. ok is False with a plain-English reason while today's check
        is failing. Before 08:45 (and on a day that is not a session) ok is True and reason None: nothing has been
        checked and nothing blocks. From 08:45, with no run stored yet (ATIP started late, the Mac was asleep), the
        checks run now. A failing result is re-checked on demand at most every RECHECK_SECONDS, so the gate clears
        itself once the cause is fixed -- without a token renewal or an alert (those are the scheduled runs').
    run_scheduled(trigger)  the 08:45 job and its retries at RETRY_TIMES (5, 10, 10 minutes: backoff), each a
                            no-op once today's gate has passed
    banner(now=None)        what the red banner on every page shows (dashboard/global_asia.py, beside the Dhan-token
                            banner): stored results only, so a page poll never calls Dhan

Each run is stored in the table preopen_health (check_date, checked_at, ok, reason, checks JSON, trigger, renewed,
alerted). The view-only server never runs a check, renews or alerts (ops/role.py): it shows the Mac's stored rows.

Config (atip_data/config.json): "preopen_health": {"enabled": true, "time": "08:45"}
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

log = logging.getLogger("atip.preopen_health")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path("atip_data") / "config.json"      # relative to the working directory, like the other modules
GATE_TIME = dtime(8, 45)
RETRY_TIMES = ("08:50", "09:00", "09:10")
RECHECK_SECONDS = 120
BANNER_UNTIL = dtime(15, 30)
PROBE_SYMBOL = "RELIANCE"
PROFILE_URL = "https://api.dhan.co/v2/profile"
CHECKS = ("dhan_token", "quote_feed", "scrip_master", "scores")

FIX = {
    "dhan_token": [{"text": "Renew the Dhan token now, in the ATIP folder:", "cmd": "tools/dhan_token_refresh.sh"}],
    "quote_feed": [{"text": "Check Dhan's Data API plan and the quote feed on the broker-health panel.", "cmd": None}],
    "scrip_master": [{"text": "Download today's instrument master, in the ATIP folder:",
                      "cmd": ".venv/bin/python -m data.instruments --refresh"}],
    "scores": [{"text": "Score the last session (the post-market run), in the ATIP folder:",
                "cmd": ".venv/bin/python main.py --run postmarket"}],
}

_run_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now()


def settings() -> dict:
    """config "preopen_health"; ATIP_PREOPEN_GATE=off switches the gate off (tests/conftest.py does, for every test).
    Off: no checks, no renewal, no alert, and gate_status() is always open."""
    try:
        c = (json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("preopen_health") or {}
    except Exception:
        c = {}
    off = os.environ.get("ATIP_PREOPEN_GATE", "").strip().lower() in ("off", "0", "false", "no")
    return {"enabled": c.get("enabled", True) is not False and not off, "time": str(c.get("time") or "08:45")}


def _gate_time() -> dtime:
    try:
        return datetime.strptime(settings()["time"], "%H:%M").time()
    except ValueError:
        return GATE_TIME


def ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS preopen_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, check_date TEXT NOT NULL, checked_at TEXT NOT NULL, ok INTEGER NOT NULL,
        reason TEXT, checks TEXT, `trigger` TEXT, renewed INTEGER DEFAULT 0, alerted INTEGER DEFAULT 0)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_preopen_health_date ON preopen_health (check_date, id)")


def _prev_session(today: date) -> date:
    from utils.trading_calendar import last_trading_day
    return last_trading_day(today - timedelta(days=1))


# ── probes (read-only; tests replace them) ────────────────────────────────
def _probe_profile(timeout=10) -> dict:
    """Dhan's /v2/profile with ATIP's token: {"ok": True, "data_plan"} or {"ok": False, "code": "DH-901" | "MISSING"
    | "NETWORK" | "HTTP 4xx", "error"}. The token never appears in what is returned or logged."""
    from data.dhan import load_dhan_config
    cfg = load_dhan_config()
    cid, tok = str(cfg.get("dhan_client_id") or ""), str(cfg.get("dhan_access_token") or "")
    if not tok:
        return {"ok": False, "code": "MISSING", "error": "no Dhan access token saved"}
    import requests
    try:
        r = requests.get(PROFILE_URL, timeout=timeout,
                         headers={"access-token": tok, "client-id": cid, "Accept": "application/json"})
    except Exception as e:
        return {"ok": False, "code": "NETWORK", "error": type(e).__name__}
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if r.status_code == 200:
        return {"ok": True, "data_plan": body.get("dataPlan"), "token_validity": body.get("tokenValidity")}
    rem = body.get("remarks") if isinstance(body.get("remarks"), dict) else {}
    code = body.get("errorCode") or body.get("error_code") or rem.get("error_code") or f"HTTP {r.status_code}"
    msg = body.get("errorMessage") or body.get("error_message") or rem.get("error_message") or ""
    return {"ok": False, "code": str(code), "error": str(msg)[:200]}


def _probe_quote(symbol=PROBE_SYMBOL) -> dict:
    """A REST quote for one stock: {"ok", "ltp", "last_trade_time", "error"}. Read-only (Dhan's quote API)."""
    from data import dhan as D
    try:
        client, _ = D.get_dhan_client()
        sec = D.get_security_id(symbol)
        if not sec:
            return {"ok": False, "error": f"no Dhan security id for {symbol}"}
        resp = D._guarded(client.quote_data, {sec["exchange"]: [int(sec["security_id"])]})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    if not isinstance(resp, dict) or resp.get("status") == "failure":
        return {"ok": False, "error": D.describe_dhan_error(resp)[:200]}
    q = (D._unwrap_feed(resp).get(sec["exchange"]) or {}).get(str(sec["security_id"])) or {}
    try:
        ltp = float(q.get("last_price"))
    except (TypeError, ValueError):
        ltp = None
    if not ltp or ltp <= 0:
        return {"ok": False, "error": "Dhan's quote has no last price"}
    return {"ok": True, "ltp": ltp, "last_trade_time": q.get("last_trade_time")}


def _renew_token(timeout=180) -> dict:
    """tools/dhan_token_refresh.py in its own process (it chdirs and reads argv): {"ok", "detail"}."""
    from ops.role import is_viewer
    if is_viewer():
        return {"ok": False, "detail": "view-only copy: the Mac renews the Dhan token"}
    try:
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "dhan_token_refresh.py")], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=timeout)
        code = p.returncode
    except Exception as e:
        return {"ok": False, "detail": f"renewal did not run: {type(e).__name__}"}
    try:
        from ops.dhan_token import _log as refresh_log
        last = (refresh_log()[0] or {}).get("message")
    except Exception:
        last = None
    return {"ok": code == 0, "detail": (last or f"exit {code}")[:200]}


def _parse_trade_time(v):
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v))
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


# ── the four checks ───────────────────────────────────────────────────────
def check_token(renew=False) -> dict:
    p = _probe_profile()
    renewed = None
    if not p["ok"] and renew and p.get("code") != "NETWORK":
        renewed = _renew_token()
        log.warning(f"  Pre-open: Dhan token {p.get('code')} — renewal {'OK' if renewed['ok'] else 'failed'}: "
                    f"{renewed['detail']}")
        if renewed["ok"]:
            p = _probe_profile()
    out = {"name": "dhan_token", "ok": bool(p["ok"]), "code": p.get("code"), "renewed": renewed}
    if p["ok"]:
        out["reason"] = None
        out["detail"] = "Dhan accepted the token" + (" (renewed now)" if renewed and renewed["ok"] else "")
        if p.get("data_plan") and str(p["data_plan"]).strip().lower() != "active":
            out["detail"] += f"; Dhan's data plan is {p['data_plan']}"
        return out
    code = p.get("code")
    out["reason"] = {"DH-901": "Dhan refused the token (DH-901)", "MISSING": "no Dhan token saved",
                     "NETWORK": "Dhan could not be reached"}.get(code, f"Dhan token check failed ({code})")
    out["detail"] = (p.get("error") or "") + (f"; renewal: {renewed['detail']}" if renewed else "")
    return out


def check_quotes(today: date) -> dict:
    p = _probe_quote()
    out = {"name": "quote_feed", "ok": False}
    if not p.get("ok"):
        out.update(reason=f"no quote from Dhan ({p.get('error')})", detail=p.get("error"))
        return out
    prev = _prev_session(today)
    ltt = _parse_trade_time(p.get("last_trade_time"))
    if ltt is not None and ltt.date() < prev:
        out.update(reason=f"quote feed stale: Dhan's last {PROBE_SYMBOL} trade is {ltt:%d %b %H:%M}, the previous "
                          f"session was {prev:%d %b}", detail=f"ltp {p.get('ltp')}")
        return out
    out.update(ok=True, reason=None,
               detail=f"{PROBE_SYMBOL} {p.get('ltp')}" + (f", last trade {ltt:%d %b %H:%M}" if ltt else ""))
    return out


def check_scrip_master(conn, today: date, repair=False) -> dict:
    from data import instruments as I
    st = I.status(conn, today)
    tried = None
    if repair:
        try:
            tried = I.ensure_today(conn)
        except Exception as e:
            tried = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
        st = I.status(conn, today)
    out = {"name": "scrip_master", "ok": bool(st.get("fresh")), "as_of": st.get("as_of")}
    if out["ok"]:
        out.update(reason=None, detail=f"{st.get('rows')} rows as of {st.get('as_of')}"
                                       + (f" (Dhan published {st['published_at']})" if st.get("published_at") else ""))
    else:
        out.update(reason=f"scrip master not refreshed today (last {st.get('as_of') or 'never'})",
                   detail=(tried or {}).get("error") or (st.get("last_error") or {}).get("error"))
    return out


def check_scores(conn, today: date) -> dict:
    prev = _prev_session(today)
    try:
        n = conn.execute("SELECT COUNT(*) FROM ai_scores WHERE date=?", (str(prev),)).fetchone()[0] or 0
    except Exception:
        n = 0
    out = {"name": "scores", "ok": n > 0, "session": str(prev), "rows": n}
    out.update(reason=None if n else f"no scores for the previous session ({prev:%d %b})",
               detail=f"{n} scored symbols for {prev}")
    return out


# ── a run ─────────────────────────────────────────────────────────────────
def _row(r) -> dict:
    try:
        checks = json.loads(r["checks"] or "[]")
    except (TypeError, ValueError):
        checks = []
    return {"ok": bool(r["ok"]), "reason": r["reason"], "checked_at": r["checked_at"], "checks": checks,
            "check_date": r["check_date"], "trigger": r["trigger"], "renewed": bool(r["renewed"]),
            "alerted": bool(r["alerted"]), "id": r["id"]}


def latest(conn, day) -> dict | None:
    ensure_table(conn)
    cur = conn.execute("SELECT id, check_date, checked_at, ok, reason, checks, `trigger`, renewed, alerted "
                       "FROM preopen_health WHERE check_date=? ORDER BY id DESC LIMIT 1", (str(day),))
    r = cur.fetchone()
    if not r:
        return None
    return _row(dict(zip([d[0] for d in cur.description], r)))


def _alerted_today(conn, day) -> bool:
    return bool(conn.execute("SELECT 1 FROM preopen_health WHERE check_date=? AND alerted=1 LIMIT 1",
                             (str(day),)).fetchone())


def run_checks(conn, today: date = None, now: datetime = None, trigger="on_demand", renew=False, alert=False) -> dict:
    """Run the four checks, store the run, alert as asked. Returns the stored result (as gate_status does)."""
    now = now or _now()
    today = today or now.date()
    with _run_lock:
        ensure_table(conn)
        was_alerted = _alerted_today(conn, today)
        checks = [check_token(renew=renew)]
        if checks[0]["ok"]:
            checks.append(check_quotes(today))
        else:
            checks.append({"name": "quote_feed", "ok": False, "reason": "quotes not checked without a valid token",
                           "detail": None})
        checks.append(check_scrip_master(conn, today, repair=renew))
        checks.append(check_scores(conn, today))
        failed = [c for c in checks if not c["ok"]]
        ok = not failed
        # the quote line adds nothing when the token is the cause
        reasons = [c["reason"] for c in failed if not (c["name"] == "quote_feed" and not checks[0]["ok"])]
        reason = None if ok else "; ".join(reasons)
        renewed = bool(checks[0].get("renewed") and checks[0]["renewed"].get("ok"))
        sent = False
        # notify() writes on a connection of its own: never while the caller holds an open write (it would wait out
        # the busy timeout), so an on-demand pass inside a caller's transaction stays silent
        in_tx = bool(getattr(conn, "in_transaction", False))
        if alert and not ok and not was_alerted:
            sent = _alert_failed(today, now, reason, checks)
        elif ok and was_alerted and (alert or not in_tx):
            _alert_cleared(today, now, trigger)
        conn.execute("INSERT INTO preopen_health (check_date, checked_at, ok, reason, checks, `trigger`, renewed, alerted) "
                     "VALUES (?,?,?,?,?,?,?,?)", (str(today), now.isoformat(timespec="seconds"), int(ok), reason,
                                                  json.dumps(checks, default=str), trigger, int(renewed), int(sent)))
        if not in_tx:             # a caller's open transaction is not committed for it: the row rides with it
            conn.commit()
        (log.info if ok else log.warning)(f"  Pre-open gate {today} ({trigger}): "
                                          + ("OK" if ok else f"BLOCKED — {reason}"))
        return latest(conn, today)


def _next_check(now: datetime):
    later = [t for t in RETRY_TIMES if t > now.strftime("%H:%M")]
    return later[0] if later else None


def _alert_failed(today, now, reason, checks) -> bool:
    try:
        from alerts.telegram import fmt, notify
        lines = "\n".join(f"{'✅' if c['ok'] else '❌'} {c['name'].replace('_', ' ')}: "
                          f"{c.get('reason') or c.get('detail') or 'ok'}" for c in checks)
        nxt = _next_check(now)
        body = (f"{lines}\n\nAuto-pick waits until this passes. "
                + (f"Next automatic check at {nxt} IST" if nxt else "ATIP re-checks before each auto-pick")
                + " (the token renewal is retried there).")
        r = notify(fmt("⛔", "Pre-open check failed", body), category="job_health", severity="error",
                   key=f"preopen_gate:{today}")
        return bool(r.get("recorded") or r.get("duplicate"))
    except Exception as e:
        log.warning(f"  pre-open alert: {e}")
        return False


def _alert_cleared(today, now, trigger):
    try:
        from alerts.telegram import fmt, notify
        notify(fmt("✅", "Pre-open check passed", f"All pre-open checks pass as of {now:%H:%M} IST ({trigger}). "
                                                 "Auto-pick can run."),
               category="job_health", severity="info", key=f"preopen_gate_ok:{today}")
    except Exception as e:
        log.warning(f"  pre-open clear alert: {e}")


# ── the gate auto-pick asks ───────────────────────────────────────────────
def _open_result(state) -> dict:
    return {"ok": True, "reason": None, "checked_at": None, "checks": [], "state": state}


def gate_status(conn, today=None, now=None) -> dict:
    """{"ok": bool, "reason": str | None, "checked_at": str | None, "checks": [...], "state": ...} for today's
    auto-pick (see the module docstring). Never raises: a check that cannot run is a failed check."""
    from utils.trading_calendar import is_trading_day
    now = now or _now()
    today = today or now.date()
    if isinstance(today, str):
        today = date.fromisoformat(today[:10])
    if not settings()["enabled"]:
        return _open_result("DISABLED")
    if not is_trading_day(today):
        return _open_result("NOT_A_MARKET_DAY")
    if today == now.date() and now.time() < _gate_time():
        return _open_result("BEFORE_CHECK")
    try:
        row = latest(conn, today)
    except Exception as e:
        log.warning(f"  pre-open gate unreadable: {e}")
        row = None
    from ops.role import is_viewer
    live = today == now.date() and not is_viewer()
    if live:
        stale = row is not None and not row["ok"] and (
            now - datetime.fromisoformat(str(row["checked_at"]))).total_seconds() >= RECHECK_SECONDS
        if row is None or stale:
            try:
                row = run_checks(conn, today, now, trigger="on_demand", renew=False, alert=False)
            except Exception as e:
                log.warning(f"  pre-open gate check failed to run: {e}")
                return {"ok": False, "reason": f"pre-open check could not run ({type(e).__name__})",
                        "checked_at": now.isoformat(timespec="seconds"), "checks": [], "state": "ERROR"}
    if row is None:
        return _open_result("NOT_CHECKED")
    out = dict(row)
    out["state"] = "OK" if row["ok"] else "BLOCKED"
    return out


# ── scheduled runs ────────────────────────────────────────────────────────
def run_scheduled(trigger="scheduled") -> dict:
    """08:45 IST (trigger "scheduled") and the retries (trigger "retry", a no-op once today passed)."""
    from ops.role import is_viewer
    from utils.trading_calendar import is_trading_day
    from db.schema import get_connection
    if is_viewer():
        return {"status": "SKIPPED", "reason": "view-only copy: the Mac checks"}
    if not settings()["enabled"]:
        return {"status": "SKIPPED", "reason": "preopen_health.enabled is false"}
    now = _now()
    if not is_trading_day(now.date()):
        return {"status": "SKIPPED", "reason": "not a market day"}
    conn = get_connection()
    try:
        if trigger == "retry":
            row = latest(conn, now.date())
            if row is not None and row["ok"]:
                return {"status": "SKIPPED", "reason": "today's pre-open check already passed"}
        r = run_checks(conn, now.date(), now, trigger=trigger, renew=True, alert=True)
    finally:
        conn.close()
    return {"status": "SUCCESS" if r["ok"] else "BLOCKED", "rows": len(r["checks"]), "error": r["reason"]}


def schedule_jobs(schedule, run_job) -> list:
    """Registered from pipeline/w39_jobs.schedule_jobs."""
    st = settings()
    if not st["enabled"]:
        return []
    schedule.every().day.at(st["time"]).do(run_job, "preopen_health", run_scheduled)
    for t in RETRY_TIMES:
        schedule.every().day.at(t).do(run_job, "preopen_health", run_scheduled, "retry")
    return [f"preopen_health daily {st['time']} IST, retries {', '.join(RETRY_TIMES)} (market days; gates auto-pick)"]


# ── the banner ────────────────────────────────────────────────────────────
def banner(now=None) -> dict | None:
    """The red banner while today's gate is failing (08:45-15:30 on a market day); None otherwise. Stored rows only."""
    from utils.trading_calendar import is_trading_day
    from db.schema import get_connection
    now = now or _now()
    if not settings()["enabled"]:
        return None
    if not is_trading_day(now.date()) or not (_gate_time() <= now.time() < BANNER_UNTIL):
        return None
    conn = get_connection()
    try:
        row = latest(conn, now.date())
    finally:
        conn.close()
    if not row or row["ok"]:
        return None
    at = str(row["checked_at"])[11:16]
    nxt = _next_check(now)
    msg = (f"Auto-pick is waiting: the pre-open check at {at} failed — {row['reason']}. "
           + (f"Next automatic check at {nxt} IST; auto-pick also re-checks before each pass."
              if nxt else "Auto-pick re-checks before each pass."))
    fix = [f for c in row["checks"] if not c.get("ok") and c["name"] in FIX
           and not (c["name"] == "quote_feed" and any(x["name"] == "dhan_token" and not x["ok"] for x in row["checks"]))
           for f in FIX[c["name"]]]
    return {"state": "BLOCKED", "severity": "error", "message": msg, "fix": fix, "checked_at": row["checked_at"],
            "failed": [c["name"] for c in row["checks"] if not c.get("ok")]}
