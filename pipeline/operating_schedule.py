"""
Operating schedule (Task 2, 2026-10-26): ATIP is STOPPED from 21:00 to 06:00 IST every day.

States, recorded with every transition in scheduler_status and served by GET /api/status:

    STOPPED   21:00 -> 06:00 IST (or a manual stop). No scheduled job runs: data ingestion, news fetch,
              Claude sentiment classification, scoring, the Dhan WebSocket feeds and quote polling, and
              order-trigger monitoring all pause, and no order can be placed or triggered -- the window is
              a third way into orders.risk.halted(), which every order path already checks. The dashboard
              stays reachable, read-only, with a banner "ATIP stopped — resumes 06:00 IST".
    RESUMING  06:00 IST: the resume sequence runs, in order, each step logged (pipeline_log names
              resume_1_backfill ... resume_6_live; resume_runs.steps_json):
                1 backfill what the night missed: US/Europe closes, GIFT Nifty, USD/INR, crude, gold, US 10Y
                  (the overnight global fetch), macro and multi-asset series, global news, and every daily
                  job whose slot fell inside the window (deferred_jobs)
                2 collect market data and news: Asia (Japan/Korea opened at 05:30 IST -- this run captures
                  their opening move), Indian news, the Dhan security list if missing
                3 news sentiment (ATIP keyword rules when news.ai_enabled is false; with it true, a
                  fall-back to the keyword classifier alerts LOUDLY)
                4 analysis: any unscored session is scored (VPI, ZPI, MRI, RRI, CRI, MSI, ACS, Market Health,
                  technicals), the null-EMA history gap is filled for short-history small/mid-caps
                5 signals and the pre-market report, stored before 08:30 IST (refreshed again at 08:15)
                6 live feeds and trigger monitoring resume -- on NSE trading days only; on weekends and
                  holidays steps 1-5 still run and the live feeds stay off
    RUNNING   after the resume sequence, until 21:00.

Catch-up: if the Mac was off or asleep at 06:00, the scheduler's first tick after start/wake runs the resume
sequence at once. resume_runs (one row per IST date) plus ops.jobs.job_lock("operating_resume") make it run
once per day however many ticks, restarts or processes ask for it.

Manual override (POST /api/status/override, token-guarded, logged in scheduler_status as source 'manual'):
    stop    STOPPED now, until the next 06:00 resume (or "start" / "auto")
    start   RUNNING now, even inside the window, until the next 21:00 (runs today's resume if not done)
    auto    clear the override

Scheduling mechanism: ONE -- the in-process `schedule` loop in pipeline/scheduler.py, which this module gates
(see the note "OPERATING WINDOW" there for why it is not APScheduler or Windows Task Scheduler). Order-trigger
monitoring stays in the dashboard process (dashboard/server.py _order_monitor_loop), separate from the
pipeline scheduler, and reads is_stopped() itself.

Config, atip_data/config.json:
    "operating_schedule": {"enabled": true, "stop_at": "21:00", "resume_at": "06:00", "report_ready_by": "08:30"}
The environment variable ATIP_OPERATING_SCHEDULE=off disables it (the test suite does, so tests that place
paper orders are not refused when they happen to run after 21:00).

CLI:
    python -m pipeline.operating_schedule status
    python -m pipeline.operating_schedule resume          # run today's resume sequence now (once a day)
    python -m pipeline.operating_schedule stop|start|auto [--reason TEXT]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time as _time
import traceback
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
STOPPED, RUNNING, RESUMING = "STOPPED", "RUNNING", "RESUMING"
DEFAULTS = {"enabled": True, "stop_at": "21:00", "resume_at": "06:00", "report_ready_by": "08:30"}
RESUME_STALE = timedelta(hours=2)          # a RUNNING resume row older than this is a crashed run
LOCK = "operating_resume"
STEPS = ("backfill", "collect", "sentiment", "analysis", "report", "live")

DDL = [
    """CREATE TABLE IF NOT EXISTS scheduler_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TIMESTAMP NOT NULL, status TEXT NOT NULL, previous TEXT,
        source TEXT, reason TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS scheduler_override (
        id INTEGER PRIMARY KEY, mode TEXT, reason TEXT, set_by TEXT, set_at TIMESTAMP, expires_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS resume_runs (
        run_date TEXT PRIMARY KEY, started_at TIMESTAMP, finished_at TIMESTAMP, status TEXT, triggered_by TEXT,
        trading_day INTEGER, steps_json TEXT, report_ready_at TIMESTAMP, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS deferred_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, window_date TEXT NOT NULL, job TEXT NOT NULL, slot TEXT,
        deferred_at TIMESTAMP, replayed_at TIMESTAMP, UNIQUE (window_date, job))""",
]


# ── clock and config ───────────────────────────────────────────────────────
def now_ist() -> datetime:
    """Aware IST now. Tests replace this (monkeypatch) to walk the clock."""
    return datetime.now(IST)


def _ist(now: datetime | None) -> datetime:
    now = now or now_ist()
    return now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)


_cfg_cache = {"at": 0.0, "cfg": None}


def settings() -> dict:
    t = _time.monotonic()
    if _cfg_cache["cfg"] is not None and t - _cfg_cache["at"] < 10:
        return _cfg_cache["cfg"]
    cfg = dict(DEFAULTS)
    try:
        user = json.loads(Path("atip_data/config.json").read_text(encoding="utf-8")).get("operating_schedule") or {}
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except Exception:
        pass
    _cfg_cache.update(at=t, cfg=cfg)
    return cfg


def enabled() -> bool:
    if str(os.environ.get("ATIP_OPERATING_SCHEDULE", "")).strip().lower() in ("off", "0", "false", "disabled"):
        return False
    return bool(settings().get("enabled", True))


def _hm(s) -> dtime:
    h, m = (int(x) for x in str(s).split(":"))
    return dtime(h, m)


def in_stop_window(now: datetime | None = None, cfg: dict | None = None) -> bool:
    """21:00 <= t or t < 06:00 IST (the window crosses midnight)."""
    cfg = cfg or settings()
    t = _ist(now).time()
    stop, resume = _hm(cfg["stop_at"]), _hm(cfg["resume_at"])
    return (t >= stop or t < resume) if stop > resume else (stop <= t < resume)


def next_resume(now: datetime | None = None, cfg: dict | None = None) -> datetime:
    cfg = cfg or settings()
    now = _ist(now)
    r = datetime.combine(now.date(), _hm(cfg["resume_at"]), IST)
    return r if r > now else r + timedelta(days=1)


def next_stop(now: datetime | None = None, cfg: dict | None = None) -> datetime:
    cfg = cfg or settings()
    now = _ist(now)
    s = datetime.combine(now.date(), _hm(cfg["stop_at"]), IST)
    return s if s > now else s + timedelta(days=1)


def window_date(now: datetime | None = None, cfg: dict | None = None) -> str:
    """The IST date a stop window began on (a 02:00 moment belongs to the previous evening's window)."""
    cfg = cfg or settings()
    now = _ist(now)
    d = now.date()
    if now.time() < _hm(cfg["resume_at"]):
        d -= timedelta(days=1)
    return str(d)


def _naive(dt: datetime) -> datetime:
    return _ist(dt).replace(tzinfo=None)


def _conn():
    from db.schema import get_connection
    c = get_connection()
    ensure_tables(c)
    return c


def ensure_tables(conn):
    for ddl in DDL:
        conn.execute(ddl)


# ── override ───────────────────────────────────────────────────────────────
_ovr_cache = {"at": 0.0, "row": None}


def _override(conn=None, now: datetime | None = None) -> dict | None:
    """The active manual override, or None (an expired one is ignored)."""
    t = _time.monotonic()
    if conn is None and t - _ovr_cache["at"] < 5:
        row = _ovr_cache["row"]
    else:
        own = conn is None
        try:
            conn = conn or _conn()
            r = conn.execute("SELECT mode, reason, set_by, set_at, expires_at FROM scheduler_override WHERE id=1").fetchone()
            row = dict(r) if r else None
        except Exception:
            row = None
        finally:
            if own and conn is not None:
                conn.close()
        _ovr_cache.update(at=t, row=row)
    if not row or not row.get("mode"):
        return None
    exp = row.get("expires_at")
    if exp:
        try:
            exp = datetime.fromisoformat(str(exp)[:19])
        except ValueError:
            exp = None
    if exp and _naive(_ist(now)) >= exp:
        return None
    return row


def set_override(action: str, reason: str = "", by: str = "dashboard", now: datetime | None = None) -> dict:
    """stop / start / auto, logged in scheduler_status (source 'manual') and alert_log."""
    action = str(action or "").strip().lower()
    if action not in ("stop", "start", "auto"):
        raise ValueError("action must be stop, start or auto")
    now = _ist(now)
    reason = (reason or "").strip()[:200]
    conn = _conn()
    try:
        prev = current(conn)
        if action == "auto":
            conn.execute("DELETE FROM scheduler_override WHERE id=1")
            expires = None
        else:
            expires = next_resume(now) if action == "stop" else next_stop(now)
            conn.execute("INSERT INTO scheduler_override (id,mode,reason,set_by,set_at,expires_at) VALUES (1,?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, reason=excluded.reason, "
                         "set_by=excluded.set_by, set_at=excluded.set_at, expires_at=excluded.expires_at",
                         (action, reason, by, _naive(now), _naive(expires)))
        conn.commit()
        _ovr_cache.update(at=0.0, row=None)
        _log_event(conn, prev.get("status") or "-", f"OVERRIDE_{action.upper()}", "manual",
                   reason or f"manual {action} from {by}",
                   {"expires_at": str(_naive(expires)) if expires else None}, now)
        conn.commit()
    finally:
        conn.close()
    _notify(f"ATIP manual {action}", f"{by}: {reason or 'no reason given'}"
            + (f" — until {expires:%d %b %H:%M} IST" if expires else ""), "warning")
    out = tick(now, trigger="manual")
    return {"action": action, "expires_at": str(_naive(expires)) if expires else None, **out}


# ── status ─────────────────────────────────────────────────────────────────
def current(conn) -> dict:
    r = conn.execute("SELECT at, status, previous, source, reason, detail FROM scheduler_status "
                     "WHERE status IN ('STOPPED','RUNNING','RESUMING') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else {}


def _resume_row(conn, run_date: str) -> dict | None:
    r = conn.execute("SELECT * FROM resume_runs WHERE run_date=?", (run_date,)).fetchone()
    return dict(r) if r else None


def desired(now: datetime | None = None, conn=None) -> tuple[str, str]:
    """(state, why) the clock and the override ask for, ignoring whether today's resume has run."""
    now = _ist(now)
    if not enabled():
        return RUNNING, "operating schedule disabled"
    o = _override(conn, now)
    if o and o["mode"] == "stop":
        return STOPPED, f"manual stop: {o.get('reason') or 'no reason given'}"
    if o and o["mode"] == "start":
        return RUNNING, f"manual start: {o.get('reason') or 'no reason given'}"
    if in_stop_window(now):
        cfg = settings()
        return STOPPED, f"scheduled stop window {cfg['stop_at']}–{cfg['resume_at']} IST"
    return RUNNING, "inside operating hours"


def is_stopped(now: datetime | None = None) -> bool:
    """True while nothing may run: the stop window (unless manually started) or a manual stop. Cheap enough
    for every order (orders.risk.halted), every dashboard write and every feed-supervisor tick."""
    try:
        return enabled() and desired(now)[0] == STOPPED
    except Exception as e:                      # a broken status store must not stop trading by accident
        log.warning(f"  operating schedule check failed ({e}) — treating as running")
        return False


def stop_reason(now: datetime | None = None) -> str:
    now = _ist(now)
    st, why = desired(now)
    if st != STOPPED:
        return ""
    return f"ATIP stopped ({why}) — resumes {next_resume(now):%H:%M} IST"


def _log_event(conn, previous, status, source, reason, detail=None, now=None):
    conn.execute("INSERT INTO scheduler_status (at,status,previous,source,reason,detail) VALUES (?,?,?,?,?,?)",
                 (_naive(_ist(now)), status, previous, source, reason,
                  json.dumps(detail, default=str) if detail is not None else None))


def transition(conn, status: str, source: str, reason: str, detail=None, now=None) -> bool:
    """Record a state change (a no-op when already in that state). Returns True when it changed."""
    prev = current(conn).get("status")
    if prev == status:
        return False
    _log_event(conn, prev, status, source, reason, detail, now)
    conn.commit()
    log.warning(f"  ⏯  ATIP {prev or '—'} → {status} ({source}: {reason})")
    return True


def last_updated(conn) -> str | None:
    """When ATIP last stored anything: the newest successful pipeline run's end."""
    try:
        r = conn.execute("SELECT MAX(COALESCE(end_time,start_time)) FROM pipeline_log WHERE "
                         "status IN ('SUCCESS','PARTIAL','NO_NEW','EMPTY')").fetchone()
        return str(r[0])[:19] if r and r[0] else None
    except Exception:
        return None


def status(now: datetime | None = None) -> dict:
    """Everything /api/status, the banner and the mobile apps show."""
    now = _ist(now)
    cfg = settings()
    conn = _conn()
    try:
        cur = current(conn)
        st, why = desired(now, conn)
        today = _resume_row(conn, str(now.date()))
        ovr = _override(conn, now)
        if st == STOPPED:
            state = STOPPED
        elif today and today["status"] == "RUNNING":
            state = RESUMING
        elif not enabled() or (today and today["status"] in ("DONE", "PARTIAL")) or (ovr or {}).get("mode") == "start":
            state = RUNNING
        else:                                    # 06:00 has passed and the scheduler has not resumed yet
            state = RESUMING if cur.get("status") in (STOPPED, RESUMING, None) else cur["status"]
        events = [dict(r) for r in conn.execute("SELECT at, status, previous, source, reason FROM scheduler_status "
                                                "ORDER BY id DESC LIMIT 12").fetchall()]
        try:
            from utils.trading_calendar import is_trading_day
            trading = is_trading_day(now.date())
        except Exception:
            trading = now.weekday() < 5
        steps = json.loads(today["steps_json"] or "[]") if today else []
        out = {"status": state, "desired": st, "reason": why, "enabled": enabled(),
               "now_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
               "stop_at": cfg["stop_at"], "resume_at": cfg["resume_at"],
               "next_stop": next_stop(now).strftime("%Y-%m-%d %H:%M"),
               "next_resume": next_resume(now).strftime("%Y-%m-%d %H:%M"),
               "banner": (f"ATIP stopped — resumes {cfg['resume_at']} IST" if state == STOPPED else
                          "ATIP resuming — running the 06:00 sequence" if state == RESUMING else "ATIP running"),
               "read_only": state == STOPPED, "orders_blocked": state == STOPPED,
               "last_updated": last_updated(conn), "trading_day": trading,
               "live_feeds": bool(state == RUNNING and trading),
               "override": ({k: str(v)[:19] if v is not None else None for k, v in ovr.items()} if ovr else None),
               "resume_today": ({k: today.get(k) for k in ("run_date", "status", "triggered_by", "started_at",
                                                            "finished_at", "report_ready_at", "error")}
                                if today else None),
               "resume_steps": steps, "transitions": events}
        return json.loads(json.dumps(out, default=str))
    finally:
        conn.close()


# ── the clock tick (called by the scheduler loop every 15 s, at start-up and on wake) ──
def tick(now: datetime | None = None, trigger: str = "schedule") -> dict:
    """Move to the state the clock asks for. STOPPED: stop the live feeds. Leaving STOPPED (06:00, or a manual
    start) and on a start after 06:00 with no resume yet today: the resume sequence (once per day)."""
    now = _ist(now)
    if not enabled():
        return {"status": RUNNING, "acted": None}
    conn = _conn()
    try:
        st, why = desired(now, conn)
        if st == STOPPED:
            changed = transition(conn, STOPPED, "manual" if why.startswith("manual") else trigger, why,
                                 {"resumes_at": str(_naive(next_resume(now)))}, now)
            if changed:
                conn.close()
                conn = None
                _stop_live_feeds()
                _notify("ATIP stopped", f"{why}. Resumes {next_resume(now):%d %b %H:%M} IST. Dashboard is read-only; "
                                        f"no order can be placed or triggered.", "info", key="ops_stopped")
            return {"status": STOPPED, "acted": "stopped" if changed else None}
        today = _resume_row(conn, str(now.date()))
        if today and today["status"] in ("DONE", "PARTIAL"):
            transition(conn, RUNNING, trigger, "resume sequence already ran today", None, now)
            return {"status": RUNNING, "acted": None}
        if why.startswith("manual start") and in_stop_window(now):
            # a manual start at night runs nothing heavy; the next 06:00 does the full sequence
            transition(conn, RUNNING, "manual", why, None, now)
            return {"status": RUNNING, "acted": "started"}
    finally:
        if conn is not None:
            conn.close()
    res = ensure_resumed(now, trigger)
    return {"status": RUNNING if res.get("status") in ("DONE", "PARTIAL") else RESUMING,
            "acted": "resumed" if res.get("ran") else None, "resume": res}


# ── the resume sequence ────────────────────────────────────────────────────
def ensure_resumed(now: datetime | None = None, trigger: str = "schedule", steps: dict | None = None) -> dict:
    """Run today's resume sequence unless it already ran (or is running) -- the catch-up entry point.
    `steps` replaces step functions by name (tests)."""
    now = _ist(now)
    run_date = str(now.date())
    from ops.jobs import job_lock
    with job_lock(LOCK) as held:
        if not held:
            log.info("  ↻ Resume sequence is already running in another process — skipped")
            return {"status": "SKIPPED", "ran": False, "reason": "locked"}
        conn = _conn()
        try:
            row = _resume_row(conn, run_date)
            if row and row["status"] in ("DONE", "PARTIAL"):
                transition(conn, RUNNING, trigger, "resume sequence already ran today", None, now)
                return {"status": row["status"], "ran": False, "reason": "already ran today"}
            if row and row["status"] == "RUNNING":
                started = datetime.fromisoformat(str(row["started_at"])[:19])
                if _naive(now) - started < RESUME_STALE:
                    return {"status": "RUNNING", "ran": False, "reason": "in progress"}
                log.warning(f"  ↻ Resume run of {run_date} started {started:%H:%M} never finished — re-running")
            try:
                from utils.trading_calendar import is_trading_day
                trading = bool(is_trading_day(now.date()))
            except Exception:
                trading = now.weekday() < 5
            conn.execute("INSERT INTO resume_runs (run_date,started_at,status,triggered_by,trading_day,steps_json) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(run_date) DO UPDATE SET started_at=excluded.started_at, "
                         "status=excluded.status, triggered_by=excluded.triggered_by, trading_day=excluded.trading_day, "
                         "steps_json=excluded.steps_json, finished_at=NULL, error=NULL",
                         (run_date, _naive(now), "RUNNING", trigger, int(trading), "[]"))
            conn.commit()
            late = _naive(now) > _naive(datetime.combine(now.date(), _hm(settings()["resume_at"]), IST)) + timedelta(minutes=10)
            transition(conn, RESUMING, trigger,
                       ("catch-up: ATIP was not running at " + settings()["resume_at"]) if late else "scheduled resume",
                       {"run_date": run_date, "trading_day": trading}, now)
        finally:
            conn.close()
        return _run_sequence(now, run_date, trigger, trading, steps or {})


def _run_sequence(now, run_date, trigger, trading, overrides) -> dict:
    fns = {**STEP_FUNCTIONS, **overrides}
    done, worst = [], "DONE"
    ctx = {"now": now, "run_date": run_date, "trading_day": trading, "started": _naive(now)}
    for i, name in enumerate(STEPS, 1):
        rec = {"step": i, "name": name, "started": str(datetime.now(IST).replace(tzinfo=None))[:19]}
        try:
            from pipeline.scheduler import run_job
            out = run_job(f"resume_{i}_{name}", fns[name], ctx)
        except Exception as e:
            out = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
            log.error(f"  ✗ resume step {i} {name}: {traceback.format_exc()}")
        out = out if isinstance(out, dict) else {"status": "SUCCESS"}
        rec.update(status=str(out.get("status") or "SUCCESS"), finished=str(datetime.now(IST).replace(tzinfo=None))[:19],
                   detail={k: v for k, v in out.items() if k not in ("status",)})
        if rec["status"] in ("FAILED",):
            worst = "PARTIAL"
        elif rec["status"] in ("PARTIAL", "DEGRADED") and worst == "DONE":
            worst = "PARTIAL"
        done.append(rec)
        log.info(f"  ↻ resume step {i}/{len(STEPS)} {name}: {rec['status']}")
        c = _conn()
        try:
            c.execute("UPDATE resume_runs SET steps_json=? WHERE run_date=?", (json.dumps(done, default=str), run_date))
            if name == "report" and rec["status"] not in ("FAILED", "SKIPPED"):
                c.execute("UPDATE resume_runs SET report_ready_at=? WHERE run_date=?",
                          (datetime.now(IST).replace(tzinfo=None), run_date))
            c.commit()
        finally:
            c.close()
    c = _conn()
    try:
        c.execute("UPDATE resume_runs SET status=?, finished_at=? WHERE run_date=?",
                  (worst, datetime.now(IST).replace(tzinfo=None), run_date))
        c.commit()
        # a manual stop that arrived meanwhile wins; the next tick records it
        if desired(None, c)[0] == RUNNING:
            transition(c, RUNNING, trigger, f"resume sequence {worst.lower()}",
                       {"failed_steps": [d["name"] for d in done if d["status"] == "FAILED"]}, None)
    finally:
        c.close()
    if worst != "DONE":
        _notify("ATIP resumed with problems", "Resume steps not clean: " + ", ".join(
            f"{d['name']} {d['status']}" for d in done if d["status"] not in ("SUCCESS", "SKIPPED", "EMPTY", "NO_NEW")),
            "warning", key=f"resume_partial:{run_date}")
    return {"status": worst, "ran": True, "steps": done}


# ── steps ──────────────────────────────────────────────────────────────────
def _sub(results, name, fn, *a, **k):
    """Run one piece of a step through run_job and keep its status."""
    from pipeline.scheduler import run_job
    try:
        r = run_job(name, fn, *a, **k)
    except Exception as e:
        r = {"status": "FAILED", "error": str(e)}
    results[name] = (r or {}).get("status", "SUCCESS") if isinstance(r, dict) else "SUCCESS"
    return r


def _rollup(results: dict, **extra) -> dict:
    bad = [k for k, v in results.items() if v == "FAILED"]
    status = "FAILED" if results and len(bad) == len(results) else "PARTIAL" if bad else "SUCCESS"
    return {"status": status, "rows": len(results), "parts": results, **({"failed": bad} if bad else {}), **extra}


def step_backfill(ctx) -> dict:
    """1. What the night missed: the overnight global fetch (US/Europe closes, crude, gold, USD/INR, US 10Y,
    Nikkei/Hang Seng), GIFT Nifty, macro + multi-asset series, global news, then each daily job deferred by
    the stop window."""
    res = {}
    from data.markets import fetch_global_markets
    _sub(res, "global_overnight", fetch_global_markets, _ist(ctx["now"]).date(), "overnight")
    if ctx["trading_day"]:
        try:
            from research.market_pulse import capture_gift
            _sub(res, "gift_resume", capture_gift)
        except Exception as e:
            res["gift_resume"] = "FAILED"
            log.warning(f"  GIFT Nifty: {e}")
    for name, mod, fn in (("macro_data", "data.macro", "run_macro"), ("multi_asset", "data.multi_asset", "run_multi_asset")):
        try:
            _sub(res, name, getattr(__import__(mod, fromlist=[fn]), fn))
        except Exception as e:
            res[name] = "FAILED"
            log.warning(f"  {name}: {e}")
    try:
        from data.news import run_news_pipeline, GLOBAL_FEEDS
        _sub(res, "news_global", run_news_pipeline, 14, feeds=GLOBAL_FEEDS)
    except Exception as e:
        res["news_global"] = "FAILED"
        log.warning(f"  global news: {e}")
    replayed = replay_deferred(window_date(ctx["now"] - timedelta(minutes=1)))
    return _rollup(res, replayed=replayed)


def step_collect(ctx) -> dict:
    """2. Market data and news: Asia (Japan/Korea are already open), Indian news, Dhan's security list."""
    res = {}
    from data import asia_markets
    _sub(res, "asia_markets_open", asia_markets.run, "open")
    from data.news import run_news_pipeline
    _sub(res, "news_resume", run_news_pipeline, 14)
    if not Path("atip_data/raw/dhan/security_id_list.csv").exists():
        try:
            from data.dhan import download_security_list
            _sub(res, "dhan_security_list", download_security_list)
        except Exception as e:
            res["dhan_security_list"] = "FAILED"
            log.warning(f"  security list: {e}")
    return _rollup(res)


def _news_ai_on() -> bool:
    try:
        from data.news_ai import settings
        return bool(settings()["ai_enabled"])
    except Exception:
        return False


def classifier_check(conn, since) -> dict:
    """How the news fetched since `since` was classified, and the latest summary's classifier."""
    try:
        rows = conn.execute("SELECT classifier, COUNT(*) n FROM news_articles WHERE fetched_at>=? GROUP BY classifier",
                            (since,)).fetchall()
        by = {str(r[0] or "none"): int(r[1]) for r in rows}
    except Exception as e:
        return {"error": str(e), "by_classifier": {}, "fallback": False}
    try:
        s = conn.execute("SELECT classifier, fallback_reason, created_at FROM news_summary ORDER BY id DESC LIMIT 1").fetchone()
        s = dict(s) if s else {}
    except Exception:
        s = {}
    rule = by.get("rule", 0)
    if not _news_ai_on():                         # keyword rules by choice (news.ai_enabled false): not a fall-back
        return {"by_classifier": by, "rule_classified": rule, "ai_classified": by.get("claude", 0),
                "summary_classifier": s.get("classifier"), "summary_fallback_reason": None, "fallback": False,
                "engine": "rules"}
    fallback = bool(rule) or (s.get("classifier") == "rule" and str(s.get("created_at") or "") >= str(since))
    return {"by_classifier": by, "rule_classified": rule, "ai_classified": by.get("claude", 0),
            "summary_classifier": s.get("classifier"), "summary_fallback_reason": s.get("fallback_reason"),
            "fallback": fallback}


def step_sentiment(ctx) -> dict:
    """3. News sentiment (data/news_ai). With news.ai_enabled false ATIP's keyword classifier is the engine by
    choice and nothing is alerted. With it true, any headline left to the keyword classifier -- no API key, cost cap
    reached, API down -- is a LOUD alert, not a log line: the scores read it as AI sentiment."""
    res = {}
    from data.news_ai import run_summary_job
    _sub(res, "news_summary_resume", run_summary_job)
    try:                                          # NI-04: what the overnight news means for today's session
        from data.news_impact import run as news_impact_run
        _sub(res, "news_impact_resume", news_impact_run, "resume", fetch_feeds=False)
    except Exception as e:
        res["news_impact_resume"] = "FAILED"
        log.warning(f"  news impact: {e}")
    from db.schema import get_connection
    c = get_connection()
    try:
        chk = classifier_check(c, ctx["started"])
    finally:
        c.close()
    if chk.get("fallback"):
        msg = (f"⚠️ NEWS SENTIMENT FELL BACK TO THE KEYWORD CLASSIFIER — {chk.get('rule_classified', 0)} headline(s) "
               f"scored by keyword rules, {chk.get('ai_classified', 0)} by Claude"
               + (f"; summary: {chk['summary_fallback_reason']}" if chk.get("summary_fallback_reason") else "")
               + ". Check ANTHROPIC_API_KEY / the news.daily_cost_cap_usd in config.json.")
        log.error("  " + msg)
        _notify("NEWS SENTIMENT FALLBACK", msg, "critical", key=f"sentiment_fallback:{ctx['run_date']}")
        out = _rollup(res, **chk)
        out["status"] = "DEGRADED"
        return out
    return _rollup(res, **chk)


def step_analysis(ctx) -> dict:
    """4. Indexes: any session without scores is scored now (VPI, ZPI, MRI, RRI, CRI, MSI, ACS, Market Health
    with the Asia input, technicals); the null-EMA gap is filled for short-history small/mid-caps."""
    res = {}
    from pipeline.scheduler import run_postmarket_if_missing
    try:
        run_postmarket_if_missing()
        res["postmarket_catchup"] = "SUCCESS"
    except Exception as e:
        res["postmarket_catchup"] = "FAILED"
        log.warning(f"  scoring catch-up: {e}")
    try:
        from data.ema_gap import run as ema_gap_run
        _sub(res, "ema_gap_fill", ema_gap_run)
    except Exception as e:
        res["ema_gap_fill"] = "FAILED"
        log.warning(f"  EMA gap fill: {e}")
    return _rollup(res)


def step_report(ctx) -> dict:
    """5. Signals + the pre-market report, stored (premarket_reports, atip_data/reports/) before 08:30."""
    from pipeline.premarket_report import build_and_store
    r = build_and_store(ctx["now"], label="resume")
    ready_by = _hm(settings()["report_ready_by"])
    late = datetime.now(IST).time() > ready_by
    if late:
        _notify("Pre-market report late", f"Built at {datetime.now(IST):%H:%M} IST, after {ready_by:%H:%M}.",
                "warning", key=f"premarket_late:{ctx['run_date']}")
    return {"status": "SUCCESS" if r.get("ok") else "FAILED", "rows": 1 if r.get("ok") else 0,
            "path": r.get("path"), "late": late, "error": r.get("error")}


def step_live(ctx) -> dict:
    """6. Live feeds and trigger monitoring for market hours -- on NSE trading days only."""
    if not ctx["trading_day"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "weekend / NSE holiday — live feeds stay off"}
    started = _start_live_feeds()
    return {"status": "SUCCESS", "rows": len(started), "feeds": started,
            "note": "feeds connect inside their own market window; the order monitor resumes on its next tick"}


STEP_FUNCTIONS = {"backfill": step_backfill, "collect": step_collect, "sentiment": step_sentiment,
                  "analysis": step_analysis, "report": step_report, "live": step_live}


# ── deferred jobs (slots that fell inside the window) ──────────────────────
def is_daily_job(job) -> bool:
    return getattr(job, "unit", None) in ("days", "weeks") or getattr(job, "start_day", None) is not None


def job_name(job) -> str:
    f = getattr(job, "job_func", None)
    fn = getattr(f, "func", f)
    args = getattr(f, "args", ()) or ()
    base = getattr(fn, "__name__", repr(fn))
    # run_job(name, fn) wrappers: the job's own name is the first argument
    if base == "run_job" and args and isinstance(args[0], str):
        return args[0]
    return base + (f"({','.join(str(a) for a in args if isinstance(a, (str, int)))})" if args else "")


def gate_pending(sched, now: datetime | None = None) -> list:
    """Inside the stop window: every job that is due is skipped -- moved to its next slot without running --
    and a daily one is recorded in deferred_jobs for the 06:00 replay. Returns the skipped names."""
    now = _ist(now)
    skipped = []
    wd = window_date(now)
    conn = None
    for job in list(sched.jobs):
        if "operating" in getattr(job, "tags", set()) or not job.should_run:
            continue
        name = job_name(job)
        try:
            job._schedule_next_run()          # skip without running (schedule has no public "skip")
        except Exception as e:
            log.warning(f"  could not reschedule {name}: {e}")
            continue
        skipped.append(name)
        if is_daily_job(job):
            try:
                conn = conn or _conn()
                conn.execute("INSERT OR IGNORE INTO deferred_jobs (window_date, job, slot, deferred_at) VALUES (?,?,?,?)",
                             (wd, name, _naive(now).strftime("%H:%M"), _naive(now)))
                _DEFERRED_FUNCS[name] = job.job_func
            except Exception as e:
                log.warning(f"  deferred_jobs: {e}")
    if conn is not None:
        conn.commit()
        conn.close()
    if skipped:
        log.info(f"  ⏸  stop window — not run: {', '.join(sorted(set(skipped)))}")
    return skipped


_DEFERRED_FUNCS: dict = {}
NOT_REPLAYED = ("run_health_check", "run_morning_catchup", "run_morning_digest", "_ops_webhook_tick")
REPLAY_COVERED = ("run_overnight", "multi_asset", "macro_data")     # step 1 already ran these


def replay_deferred(wd: str) -> list:
    """Run once each daily job the window deferred (this process saw it; a job deferred by a process that
    has since exited is covered by the start-up catch-ups instead)."""
    try:
        conn = _conn()
    except Exception:
        return []
    try:
        rows = conn.execute("SELECT id, job FROM deferred_jobs WHERE window_date=? AND replayed_at IS NULL ORDER BY id",
                            (wd,)).fetchall()
    finally:
        conn.close()
    out = []
    for rid, name in [(r[0], r[1]) for r in rows]:
        fn = _DEFERRED_FUNCS.get(name)
        if fn is None or name.split("(")[0] in NOT_REPLAYED or any(k in name for k in REPLAY_COVERED):
            status = "NOT_REPLAYED"
        else:
            try:
                fn()
                status = "REPLAYED"
            except Exception as e:
                status = f"FAILED: {e}"
        out.append({"job": name, "result": status})
        c = _conn()
        try:
            c.execute("UPDATE deferred_jobs SET replayed_at=? WHERE id=?", (datetime.now(IST).replace(tzinfo=None), rid))
            c.commit()
        finally:
            c.close()
    return out


# ── live feeds ─────────────────────────────────────────────────────────────
def _stop_live_feeds() -> list:
    """The feeds also gate themselves (data/dhan_ws.feed_window_open reads is_stopped); this closes any socket
    that is open right now."""
    stopped = []
    for mod, fn in (("data.dhan_ws", "get_index_feed_manager"), ("data.stock_feed", "get_stock_feed")):
        try:
            m = getattr(__import__(mod, fromlist=[fn]), fn)()
            if m is not None and hasattr(m, "_disconnect"):
                m._disconnect("ATIP stop window")
                stopped.append(mod)
        except Exception as e:
            log.debug(f"  {mod}: {e}")
    return stopped


def _start_live_feeds() -> list:
    started = []
    for mod, get, start in (("data.dhan_ws", "get_index_feed_manager", "start_index_feed"),
                            ("data.stock_feed", "get_stock_feed", "start_stock_feed")):
        try:
            m = __import__(mod, fromlist=[get, start])
            if getattr(m, get)() is None:
                getattr(m, start)()
            started.append(mod)
        except Exception as e:
            log.info(f"  {mod} not started: {e}")
    return started


def _notify(title, body, severity="info", key=None):
    try:
        from alerts.telegram import notify, fmt
        icon = {"critical": "🚨", "warning": "⚠️", "error": "❌"}.get(severity, "⏯")
        notify(fmt(icon, title, body), category="ops", severity=severity, key=key)
    except Exception as e:
        log.debug(f"  notify: {e}")


# ── dashboard read-only mode ───────────────────────────────────────────────
READ_ONLY_ALLOWED = ("/api/status", "/api/risk/halt", "/api/auth", "/api/enterprise/auth", "/api/login",
                     "/api/logout", "/login", "/logout")


def install_readonly_guard(app):
    """While STOPPED every write to the dashboard is refused with 423 and the reason, except the override itself,
    the kill switch (halting is always allowed) and sign-in. GET pages and APIs keep working."""
    from starlette.responses import JSONResponse

    @app.middleware("http")
    async def _operating_readonly(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            path = request.scope.get("path") or ""
            root = request.scope.get("root_path") or ""
            if root and path.startswith(root):
                path = path[len(root):] or "/"
            if not path.startswith(READ_ONLY_ALLOWED) and is_stopped():
                return JSONResponse({"error": stop_reason() + "; the dashboard is read-only until then",
                                     "status": STOPPED, "read_only": True}, status_code=423)
        return await call_next(request)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP operating schedule")
    ap.add_argument("cmd", choices=["status", "resume", "stop", "start", "auto"])
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif a.cmd == "resume":
        print(json.dumps(ensure_resumed(trigger="manual"), indent=2, default=str))
    else:
        print(json.dumps(set_override(a.cmd, a.reason, by="cli"), indent=2, default=str))


if __name__ == "__main__":
    main()
