"""
ATIP — Master Scheduler
========================
Complete data refresh schedule with Dhan API integration.

DATA REFRESH FREQUENCY SUMMARY
================================
Source          | Data              | Frequency          | Time (IST)
----------------|-------------------|--------------------|------------------
Dhan API        | Live quotes       | Every 15 min       | 09:15 – 15:30
Dhan API        | Index LTP (local) | Every 15 min       | 09:15 – 15:30
Dhan API        | Global markets    | Every 15 min       | 09:15 – 15:30
Dhan API        | Intraday 1-min    | Every 15 min       | 09:15 – 15:30
Dhan WebSocket  | Tick streaming    | Real-time          | 09:15 – 15:30
Dhan API        | OHLC snapshot     | Every 30 min       | 09:15 – 15:30
Dhan API        | Historical daily  | Once daily         | 16:05 PM
Dhan API        | Portfolio sync    | Twice daily        | 08:00, 16:30
NSE Bhavcopy    | Official EOD data | Once daily         | 16:05 PM
Global Markets  | US/Asia/Europe    | Pre-market + 15min | 07:15, then 15min
Global Markets  | Overnight US      | Once nightly       | 23:00
India VIX       | Live VIX level    | Every 15 min       | 09:15 – 15:30
News RSS        | Market news       | Twice daily        | 07:45, 12:00
AI Scoring      | All 9 indexes     | Once daily         | 17:00 (after EOD)
Fundamentals    | ROE/EPS/D/E etc   | Weekly             | Saturday 08:00
Accuracy Audit  | Prediction track  | Weekly             | Saturday 09:00

NOTE ON FEED CADENCE (fix applied 2026-07-29):
  Global markets used to refresh on a separate 30-min job, out of step
  with the 15-min local index refresh. It now runs inside the SAME
  15-min job as the local index refresh, so both update together.

  A DEBOUNCE GUARD (see _debounce() below) was also added to every
  intraday job. If this process is paused/frozen (machine sleep, network
  drop, laptop lid closed) for a long stretch, the `schedule` library
  does NOT skip the runs it missed — it fires every overdue slot
  back-to-back the instant the loop resumes, which was hammering Dhan's
  API within the same second and tripping their rate limit (see
  atip.log, 2026-07-29 10:36:49-52). The debounce guard makes a job a
  no-op if it already ran within the last MIN_JOB_GAP_SECONDS, so a
  catch-up burst collapses to a single real run instead of firing
  4-6 times in a row.

Run:
    python main.py                          # start scheduler + dashboard
    python main.py --run postmarket         # run post-market pipeline now
    python main.py --run premarket          # run pre-market pipeline now
    python main.py --run intraday           # run one intraday refresh now
    python main.py --status                 # show pipeline status
"""

import time, logging, argparse, traceback
from datetime import date, datetime, timedelta
from utils.trading_calendar import is_trading_day, postmarket_target_date, last_trading_day

log = logging.getLogger(__name__)

try: import schedule; HAS_SCHEDULE = True
except ImportError: HAS_SCHEDULE = False; log.warning("pip install schedule")


# ── Helpers ───────────────────────────────────────────────────────────────

def now_ist(): return datetime.now().strftime("%H:%M:%S")

def is_market_day(): return is_trading_day(date.today())  # Mon–Fri, minus known NSE holidays

# When post-market runs. NSE's CM Bhavcopy -- the only source that delivers the
# SAME day's closing prices on that day -- carries Last-Modified 11:03 GMT
# (16:33 IST); Dhan's daily-history endpoint doesn't return a day's bar until the
# next day. This used to be 16:05: every Bhavcopy attempt 404'd, and from
# 2026-09-10 each day was scored on the previous day's closes, so every signal
# was logged with no entry price and could never be tracked.
POSTMARKET_RUN_TIME = "16:45"
# Safety net: re-runs post-market only if the day still has no scores (late
# NSE publication, or ATIP started after POSTMARKET_RUN_TIME).
POSTMARKET_CATCHUP_TIME = "18:30"
# NSE's full Bhavcopy (the file with delivery) comes out in the evening, after
# the post-market run.
EOD_LATE_RUN_TIME = "19:30"
# Two of a session's inputs are published by NSE only in the evening, after
# the 16:45 run: FII/DII cash-market flows (provisional; 2026-09-21's were out
# by 18:22, and at 16:45 NSE still serves the previous session's -- there is
# no intraday source) and delivery, in the full Bhavcopy (ZPI's delivery
# component). The closest to real time is to poll from the close until both
# appear and then score the session on its own data.
FII_DII_WATCH_START = "17:00"
FII_DII_WATCH_END = "21:30"
FII_DII_WATCH_EVERY = 10          # minutes
# Share of the scored universe that must have a bar for the target date before
# it may be scored. Below this, the "day" would really be the previous close.
EOD_COVERAGE_MIN = 0.5

# Fundamentals are held OFF pending a decision on how fundamental_score is
# weighted. scores/engine.py's atip_comp passes the SAME field twice -- as SPI
# (0.15) and as FS (0.10) -- so one number drives 25% of the master score that
# every BUY gate reads, and data/fundamentals.py's compute_fs returns a
# hardcoded 50.0 when no ratio parses. fundamental_data is empty today, so that
# 25% currently drops out and the score renormalises over the rest; switching
# the (now working) Screener fetch back on would move the BUY gate for reasons
# unrelated to price. Flip this, or set "fundamentals_enabled": true in
# atip_data/config.json, once the weighting is settled.
FUNDAMENTALS_ENABLED = True
# W27: the hold above is resolved -- SPI and FS are now two different formulas
# (scores/fundamental.py), nothing defaults to 50.0, and whether fundamentals
# reach the scores at all is a separate switch (fundamentals.score_enabled,
# off by default; scores/engine.w27_flags). This flag now only gates INGESTION
# from NSE filings (data/nse_filings.py), which is safe to run.
FUNDAMENTALS_HOLD_REASON = "fundamentals_enabled is false in atip_data/config.json"


def fundamentals_enabled() -> bool:
    """config.json wins if it says anything; otherwise the constant above."""
    try:
        import json
        from pathlib import Path
        cfg = json.loads(Path("atip_data/config.json").read_text(encoding="utf-8"))
        if "fundamentals_enabled" in cfg:
            return bool(cfg["fundamentals_enabled"])
    except Exception:
        pass
    return FUNDAMENTALS_ENABLED


def _eod_coverage(td):
    """
    (ok, symbols_with_a_bar_for_td, universe_size) for the target date.

    The universe is the TRACKED universe, so this measures "do we have today's
    close for the stocks we are supposed to score". Judging against the previous
    run's scored count instead lets the bar ratchet down: scoring only covers
    symbols that have a bar, so a half-covered day scores half the universe, and
    the next day then only has to beat half of THAT -- 502, 251, 126, 63 -- while
    each log line still reads like a full session. With nothing tracked and
    nothing scored before (first run) it can't judge, and allows scoring.
    """
    from db.schema import get_connection
    conn = get_connection()
    try:
        try:
            from data.dhan import get_tracked_symbols
            universe = set(get_tracked_symbols(conn))
        except Exception as e:
            log.warning(f"  Coverage: tracked universe unavailable ({e}) — "
                        f"falling back to the last scored set")
            universe = set()
        if not universe:
            prev = conn.execute("SELECT MAX(date) FROM ai_scores WHERE date < ?",
                                (str(td),)).fetchone()[0]
            if not prev:
                return True, None, None
            universe = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM ai_scores WHERE date=?", (str(prev),))}
        if not universe:
            return True, None, None
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM prices_daily WHERE date=?", (str(td),))}
        n_eod = len(universe & have)
        n_uni = len(universe)
        return (n_eod / n_uni >= EOD_COVERAGE_MIN), n_eod, n_uni
    finally:
        conn.close()


# Share of a session's bars (tracked universe) allowed to be byte-identical to
# the same symbol's previous stored bar. A real session essentially never
# repeats open/high/low/close AND volume to the unit: measured over 425 stored
# sessions, normal days sit at median 0.00%, p95 0.12%, max 0.40% (a couple of
# suspended scrips). The 18 stale-copy days -- the session after an NSE holiday,
# carrying the pre-holiday bar left behind by the pre-2026-09-08 UTC date shift
# -- sit at 99.6-99.8%. 5% is twelve times the worst normal day.
EOD_STALE_MAX = 0.05


def _eod_freshness(td):
    """
    (ok, identical, checked): are td's bars a real session, or a copy of the
    previous one? The coverage guard only asks whether a bar EXISTS, so a day of
    stale copies passes it at 100% and gets scored on yesterday's prices.
    """
    from db.schema import get_connection
    conn = get_connection()
    try:
        prev = conn.execute("SELECT MAX(date) FROM prices_daily WHERE date < ?",
                            (str(td),)).fetchone()[0]
        if not prev:
            return True, 0, 0
        try:
            from data.dhan import get_tracked_symbols
            universe = list(get_tracked_symbols(conn))
        except Exception:
            universe = []
        where, params = "", [str(prev), str(td)]
        if universe:
            where = f" AND p.symbol IN ({','.join('?' * len(universe))})"
            params += universe
        n, same = conn.execute(
            "SELECT COUNT(*), SUM(p.open=q.open AND p.high=q.high AND p.low=q.low "
            "AND p.close=q.close AND p.volume=q.volume) "
            "FROM prices_daily p JOIN prices_daily q ON q.symbol=p.symbol AND q.date=? "
            "WHERE p.date=?" + where, params).fetchone()
        n, same = n or 0, same or 0
        return (n == 0 or same / n <= EOD_STALE_MAX), same, n
    finally:
        conn.close()


CATCHUP_LOOKBACK_SESSIONS = 5


def _recent_sessions(n, upto):
    """The last n trading days up to and including `upto`, oldest first."""
    out, d = [], upto
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def run_postmarket_if_missing():
    """
    Score any of the last CATCHUP_LOOKBACK_SESSIONS trading days that still has
    no scores, oldest first.

    `schedule` never catches up a missed slot: a process started after the
    post-market time waits until TOMORROW. That lost 2026-09-15 outright (ATIP
    started at 16:59) and 2026-09-11 (machine asleep from 10:44 until the next
    morning). Recovering only the NEWEST session was not enough either -- both of
    those days have bars in prices_daily and no ai_scores at all, and once the
    calendar moved past them nothing would ever have revisited them: the 18:30
    catch-up resolves one date, and by the next evening that date is yesterday.

    An older session is only re-run when its bars are already stored, so a fresh
    install does not fan out five days of downloads; the newest session is run
    regardless, because "EOD not published yet" is exactly what it is for.
    Called at scheduler start and again at POSTMARKET_CATCHUP_TIME; a no-op when
    everything is scored, so it is safe to call as often as the process restarts.
    """
    now = datetime.now()
    td = postmarket_target_date()
    hh, mm = (int(x) for x in POSTMARKET_RUN_TIME.split(":"))
    due_later = (td == now.date() and (now.hour, now.minute) < (hh, mm))

    sessions = _recent_sessions(CATCHUP_LOOKBACK_SESSIONS, td)
    if due_later:
        log.info(f"  Post-market for {td} is due at {POSTMARKET_RUN_TIME} — "
                 f"checking earlier sessions only")
        sessions = [d for d in sessions if d != td]
    if not sessions:
        return

    from db.schema import get_connection
    conn = get_connection()
    try:
        scored = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT date FROM ai_scores WHERE date >= ?", (str(sessions[0]),))}
        has_bars = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT date FROM prices_daily WHERE date >= ?", (str(sessions[0]),))}
    finally:
        conn.close()

    release_held_signals()
    missing = [d for d in sessions
               if str(d) not in scored and (d == td or str(d) in has_bars)]
    if not missing:
        log.info(f"  ✓ Every recent session through {sessions[-1]} is scored — "
                 f"no catch-up needed")
        return

    log.warning(f"  ⚠ No scores for {', '.join(str(d) for d in missing)} — post-market was "
                f"missed (not running at {POSTMARKET_RUN_TIME}, or EOD data not published "
                f"at the time). Running now, oldest first.")
    for d in missing:
        try:
            run_postmarket(force=True, target_date=d, backfill=(d != td))
        except Exception as e:
            log.error(f"  Catch-up for {d} failed: {e}")
    if any(d != td for d in missing):
        # One rebuild, for the newest session, rather than one per recovered day.
        run_job("dashboard_rebuild", _rebuild_dashboard, td)


def is_market_hours():
    now = datetime.now()
    return (is_market_day() and
            now.replace(hour=9, minute=15) <= now <= now.replace(hour=15, minute=30))

# ── Debounce guard against `schedule` catch-up bursts ──────────────────────
# If this process is paused (machine sleep, network drop) for longer than a
# few job intervals, `schedule.run_pending()` fires every job whose slot has
# passed, back-to-back, the instant the loop wakes up — see the note at the
# top of this file. This makes each guarded job a no-op if it already ran
# within MIN_JOB_GAP_SECONDS, collapsing a catch-up burst to one real run.
MIN_JOB_GAP_SECONDS = 60
_last_run = {}

def _debounce(job_name: str) -> bool:
    """Returns True if `job_name` should run now, False if it ran too recently."""
    now = datetime.now()
    prev = _last_run.get(job_name)
    if prev and (now - prev).total_seconds() < MIN_JOB_GAP_SECONDS:
        log.info(f"  ⏩  {job_name}: skipped — ran {int((now-prev).total_seconds())}s ago "
                 f"(catch-up burst guard)")
        return False
    _last_run[job_name] = now
    return True

def _job_run_date(args, kwargs):
    """The session a job is for: the first date among its arguments, else today."""
    for v in (*args, *kwargs.values()):
        if isinstance(v, date):
            return v.date() if isinstance(v, datetime) else v
        if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
            try:
                return date.fromisoformat(v)
            except ValueError:
                pass
    return date.today()

def _alert_failure(name, error):
    try:
        from alerts.telegram import send_failure_alert
        send_failure_alert(name, str(error))
    except Exception:
        pass

def run_job(name, fn, *args, **kwargs):
    """
    Run a pipeline job with error handling and logging, record the run in
    pipeline_log (kind='run': start, end, duration, rows, status, error), and
    alert on failure.

    Status recorded:
      FAILED   it raised, or returned status FAILED -- the second used to log a
               green tick and alert nobody (dhan_historical, portfolio_sync)
      EMPTY    it reported SUCCESS having processed 0 rows (93 such SUCCESS
               rows were in pipeline_log by 2026-09-21)
      anything else the job returned (SUCCESS, SKIPPED, HELD, ...)
    rows is NULL when the job does not report a count.
    """
    from db.schema import log_job
    from ops import context as _ctx, metrics as _metrics
    from ops.jobs import job_lock
    start = datetime.now()
    run_date = _job_run_date(args, kwargs)
    # W8: one runner per job name at a time (a second ATIP process, a CLI run, or
    # an overlapping catch-up SKIPS instead of running the job twice)
    with job_lock(name) as held:
        if not held:
            log.warning(f"⏭  [{now_ist()}]  {name} is already running elsewhere — skipped")
            log_job(name, "SKIPPED", None, error="locked: another runner holds this job", run_date=run_date,
                    start_time=start, end_time=datetime.now(), kind="run")
            _metrics.inc("atip_job_runs_total", {"job": name, "status": "SKIPPED"})
            return {"status": "SKIPPED", "reason": "locked"}
        tok = _ctx.job_name.set(name)
        try:
            return _run_job_locked(name, fn, start, run_date, log_job, args, kwargs)
        finally:
            _ctx.job_name.reset(tok)
            _metrics.observe("atip_job_duration_seconds", (datetime.now() - start).total_seconds(), {"job": name})


def _run_job_locked(name, fn, start, run_date, log_job, args, kwargs):
    from ops import metrics as _metrics
    log.info(f"▶  [{now_ist()}]  {name}")
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        end = datetime.now()
        log.error(f"❌  [{now_ist()}]  FAILED: {name}  ({(end - start).seconds}s)\n{traceback.format_exc()}")
        log_job(name, "FAILED", None, error=f"{type(e).__name__}: {e}", run_date=run_date,
                start_time=start, end_time=end, kind="run")
        _metrics.inc("atip_job_runs_total", {"job": name, "status": "FAILED"})
        _alert_failure(name, e)
        return {"status": "FAILED", "error": str(e)}
    end = datetime.now()
    elapsed = (end - start).seconds
    rows = result.get("rows") if isinstance(result, dict) else None
    status = str(result.get("status") or "SUCCESS") if isinstance(result, dict) else "SUCCESS"
    error = (result.get("error") or result.get("reason")) if isinstance(result, dict) else None
    if status == "SUCCESS" and rows == 0:
        status = "EMPTY"
    log_job(name, status, rows, error=error, run_date=run_date, start_time=start, end_time=end, kind="run")
    _metrics.inc("atip_job_runs_total", {"job": name, "status": status})
    if status == "FAILED":
        log.error(f"❌  [{now_ist()}]  FAILED: {name}  ({elapsed}s) — {error or 'returned FAILED'}")
        _alert_failure(name, error or "returned FAILED")
    else:
        log.info(f"✅  [{now_ist()}]  {name}  ({rows if rows is not None else '?'} rows, {elapsed}s, {status})")
    return result


# ═════════════════════════════════════════════════════════════════════════
#  PRE-MARKET PIPELINE  (6:45 AM – 9:15 AM)
#  Refresh: Global markets, GIFT Nifty, News, Portfolio sync
# ═════════════════════════════════════════════════════════════════════════

def run_premarket(force=False):
    if not force and not is_market_day():
        log.info("⏩  Weekend — skipping pre-market"); return

    td = date.today()
    log.info(f"\n{'='*55}\n  PRE-MARKET PIPELINE — {td}\n{'='*55}")

    # 7:00 AM — Dhan security list (only if missing)
    from pathlib import Path
    if not Path("atip_data/raw/dhan/security_id_list.csv").exists():
        run_job("dhan_security_list",
                lambda: __import__("data.dhan", fromlist=["download_security_list"]).download_security_list())

    # News first (last 14 hours): it takes seconds, and from 2026-08-04 this
    # run kept ending right after the quotes step (the process was gone by
    # ~07:03 with no error logged), so the news step placed after it never ran.
    # run_morning_catchup() also fetches it if it is still missing.
    from data.news import run_news_pipeline
    run_job("news_premarket", run_news_pipeline, 14)
    _news_summary()
    try:                                             # W29 (BR-02): an expired Kite session alerts now,
        from portfolio.zerodha import check_token_and_alert   # not as a failed sync tonight
        run_job("zerodha_token_check", check_token_and_alert)
    except Exception as e:
        log.warning(f"  Zerodha token check: {e}")

    # Global markets (S&P, Dow, Nasdaq, Nikkei, Gold, Crude, USD/INR)
    from data.markets import fetch_global_markets
    run_job("global_premarket", fetch_global_markets, td, "premarket")

    # Dhan live quotes snapshot (pre-market)
    _run_dhan_quotes(td, label="premarket")

    # Portfolio sync (Dhan, else Zerodha when connected)
    _run_portfolio_sync(td)

    # 9:05 AM — Final pre-open index snapshot
    _run_dhan_quotes(td, label="pre-open")

    # Gap alert
    try:
        from alerts.telegram import check_gap_alert
        check_gap_alert(td)
    except Exception as e:
        log.warning(f"  Gap alert: {e}")

    log.info("✅  Pre-market pipeline complete\n")


def _run_dhan_quotes(td, label=""):
    """Fetch live quotes via Dhan API (with Bhavcopy fallback)."""
    try:
        from data.dhan import run_live_quote_refresh
        run_job(f"dhan_quotes_{label}", run_live_quote_refresh)
    except Exception as e:
        log.warning(f"  Dhan quotes ({label}): {e} — skipping")


def _ran_ok_today(conn, jobs, ok=("SUCCESS", "NO_NEW", "EMPTY")):
    """Did any of `jobs` record a run today with one of the `ok` statuses?"""
    q = (f"SELECT 1 FROM pipeline_log WHERE kind='run' AND job_name IN ({','.join('?' * len(jobs))}) "
         f"AND status IN ({','.join('?' * len(ok))}) AND start_time>=? LIMIT 1")
    return bool(conn.execute(q, (*jobs, *ok, datetime.combine(date.today(), datetime.min.time()))).fetchone())


def run_morning_catchup():
    """
    Run the pre-market steps a trading day still lacks: the news fetch and the
    portfolio sync. `schedule` never re-runs a missed slot, and the 07:00
    pre-market run has been ending early (see run_premarket), so news had not
    been fetched on schedule since 2026-08-03 and the LIVE book could stay a
    day old. Called at start-up and at 08:20 and 12:20; a no-op once both
    have worked today.
    """
    if not is_market_day():
        return
    now = datetime.now()
    if now.hour < 7:
        return
    from db.schema import get_connection
    conn = get_connection()
    try:
        need_news = not _ran_ok_today(conn, ("news_premarket", "news_midday", "news_catchup"))
        need_book = now.hour >= 8 and not conn.execute(
            "SELECT 1 FROM portfolio_sync WHERE status='SUCCESS' AND date=?", (str(date.today()),)).fetchone()
    finally:
        conn.close()
    if need_news:
        log.info("  ↻ Catch-up: no news fetched yet today")
        from data.news import run_news_pipeline
        run_job("news_catchup", run_news_pipeline, 18)
    if need_book:
        log.info("  ↻ Catch-up: portfolio not synced yet today")
        _run_portfolio_sync(date.today())


def _last_ok(conn, jobs, ok=("SUCCESS", "NO_NEW", "EMPTY", "PARTIAL"), before=None):
    """Start time of the newest successful run of any of `jobs` (at or before `before`), or None."""
    q = (f"SELECT MAX(start_time) FROM pipeline_log WHERE kind='run' AND job_name IN ({','.join('?' * len(jobs))}) "
         f"AND status IN ({','.join('?' * len(ok))}) AND start_time<=?")
    v = conn.execute(q, (*jobs, *ok, before or datetime.now())).fetchone()[0]
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:26].replace(" ", "T"))


def run_startup_market_catchup(now=None):
    """
    Bring the market indicators up to date when ATIP starts after their scheduled
    slots have passed (the PC was off or asleep). `schedule` never re-runs a missed
    slot: started at 10:30, the 07:00 global-markets / pre-open quotes waited until
    the next day -- pipeline_log shows global_premarket last ran on 2026-09-29, so
    every later morning ran without them -- and started after the close, the index
    panel kept the last intraday values until the next session.

    Each step runs only if its slot has passed today AND its job has not succeeded
    since that slot, so a restart in the middle of a normal day does nothing:

      07:00-15:30  global markets if none fetched since 07:00; before 09:15 also the
                   pre-open Dhan quotes, and 08:30-09:15 the morning brief if unsent
      09:15-15:30  the 15-minute refresh (quotes, indexes, VIX, GIFT Nifty, global)
                   when the last one is older than 15 min; 15-min bars when older
                   than 30 min
      after 15:30  one closing snapshot of indexes + quotes if none was taken after
                   the close (what the index panel shows overnight)
      17:00-       FII/DII settle for held signals (its own guard decides)
      19:30-       delivery (EOD late)
      23:00- / before 07:00  overnight global markets if not fetched since 23:00
      weekend      the Saturday weekly job if it did not run this week

    Called by start_scheduler() before the post-market catch-up (these are quick
    and the dashboard should show current values while scoring catches up).
    """
    now = now or datetime.now()
    td = now.date()

    def today(h, m):
        return datetime.combine(td, datetime.min.time()).replace(hour=h, minute=m)

    from db.schema import get_connection
    conn = get_connection()
    try:
        plan = startup_catchup_plan(conn, now)
    finally:
        conn.close()
    return _run_startup_plan(plan, now, td, today)


def startup_catchup_plan(conn, now) -> list:
    """The missed market-indicator steps at `now` (pure: reads pipeline_log only)."""
    td = now.date()

    def today(h, m):
        return datetime.combine(td, datetime.min.time()).replace(hour=h, minute=m)

    def last(*jobs):
        return _last_ok(conn, jobs, before=now)
    plan = []
    if is_trading_day(td):
        if today(7, 0) <= now < today(15, 30):
            g = last("global_premarket", "global_intraday", "global_catchup")
            if not g or g < today(7, 0):
                plan.append("global")
            q = last("dhan_quotes_premarket", "dhan_quotes_pre-open", "dhan_quotes_intraday",
                     "dhan_quotes_catchup")
            if now < today(9, 15) and (not q or q < today(7, 0)):
                plan.append("preopen_quotes")
            d = last("morning_digest")
            if today(8, 30) <= now < today(9, 15) and (not d or d < today(8, 30)):
                plan.append("digest")
        if today(9, 15) <= now <= today(15, 30):
            i = last("intraday_indexes")
            if not i or i < now - timedelta(minutes=15):
                plan.append("intraday_15")
            b = last("dhan_15min_bars")
            if now >= today(9, 45) and (not b or b < now - timedelta(minutes=30)):
                plan.append("intraday_30")
        if now >= today(15, 31):
            i = last("intraday_indexes", "close_indexes")
            if not i or i < today(15, 30):
                plan.append("close_snapshot")
        if now.time() >= datetime.strptime(FII_DII_WATCH_START, "%H:%M").time():
            plan.append("fii_dii")
        eh, em = (int(x) for x in EOD_LATE_RUN_TIME.split(":"))
        if now >= today(eh, em):
            dl = last("delivery")
            if not dl or dl < today(eh, em):
                plan.append("eod_late")
    o = last("global_overnight")
    if now >= today(23, 0):
        if not o or o < today(23, 0):
            plan.append("overnight")
    elif now < today(7, 0) and (not o or o < today(23, 0) - timedelta(days=1)):
        plan.append("overnight")
    if now.weekday() in (5, 6):
        sat = today(8, 0) - timedelta(days=now.weekday() - 5)
        w = last("accuracy_audit", "db_purge")
        if now >= sat and (not w or w < sat):
            plan.append("weekly")
    return plan


def _run_startup_plan(plan, now, td, today):
    if not plan:
        log.info("  ✓ Market indicators are current — no start-up catch-up needed")
        return {"status": "SKIPPED", "rows": 0, "ran": []}
    log.warning(f"  ↻ Start-up catch-up: missed market-indicator slots → {', '.join(plan)}")
    from data.markets import fetch_global_markets, fetch_intraday_indexes
    for step in plan:
        try:
            if step == "global":
                run_job("global_catchup", fetch_global_markets, td,
                        "premarket" if now < today(9, 15) else "intraday")
            elif step == "preopen_quotes":
                _run_dhan_quotes(td, label="catchup")
            elif step == "digest":
                run_morning_digest()
            elif step == "intraday_15":
                run_intraday_15min(force=True)
            elif step == "intraday_30":
                run_intraday_30min()
            elif step == "close_snapshot":
                run_job("close_indexes", fetch_intraday_indexes, td)
                _run_dhan_quotes(td, label="close_catchup")
            elif step == "fii_dii":
                run_fii_dii_watch()
            elif step == "eod_late":
                run_eod_late()
            elif step == "overnight":
                run_overnight()
            elif step == "weekly":
                run_weekly()
        except Exception as e:
            log.warning(f"  Start-up catch-up step {step}: {e}")
    if any(s in plan for s in ("global", "preopen_quotes", "close_snapshot", "overnight")):
        run_job("dashboard_refresh", _rebuild_dashboard, td)
    return {"status": "SUCCESS", "rows": len(plan), "ran": plan}


def _run_portfolio_sync(td):
    """Sync portfolio — tries Dhan first, then Zerodha."""
    try:
        from data.dhan import sync_dhan_portfolio
        result = run_job("dhan_portfolio", sync_dhan_portfolio, td)
        if result.get("status") == "SUCCESS":
            return
    except Exception:
        pass
    try:
        from portfolio.zerodha import run_portfolio_sync
        run_job("zerodha_portfolio", run_portfolio_sync, td)
    except Exception as e:
        log.warning(f"  Portfolio sync: {e}")


# ═════════════════════════════════════════════════════════════════════════
#  INTRADAY PIPELINE  (9:15 AM – 3:30 PM)
#
#  Every 15 min:  Dhan live quotes + NSE index levels + VIX + alert checks
#  Every 30 min:  Global markets refresh + intraday OHLC bars
#  12:00 PM:      Midday news refresh
#  12:30 PM:      ZPI buy-zone alert scan
# ═════════════════════════════════════════════════════════════════════════

def run_intraday_15min(force=False):
    """
    Runs every 15 min during market hours (9:15 AM – 3:30 PM IST).
    REFRESH: Live quotes (Dhan) + NSE indexes + Global markets + India VIX
    + alert checks. Global markets moved here (from the old 30-min job) so
    local and global data refresh together on the same cadence.
    """
    if not force and not is_market_hours(): return
    if not force and not _debounce("intraday_15min"): return

    td = date.today()

    # 1. Dhan live quotes for all tracked stocks (primary data source)
    _run_dhan_quotes(td, label="intraday")

    # 2. NSE index levels (Nifty, BankNifty, Midcap, Smallcap, VIX etc.)
    from data.markets import fetch_intraday_indexes
    run_job("intraday_indexes", fetch_intraday_indexes, td)

    # 3. Global markets (S&P, Dow, Nasdaq, Nikkei, Gold, Crude, USD/INR etc.)
    #    — now on the same 15-min cadence as the local index refresh above.
    from data.markets import fetch_global_markets
    run_job("global_intraday", fetch_global_markets, td, "intraday")

    # 4. Alert checks (VIX spike, broad selloff)
    try:
        from alerts.telegram import check_vix_alert, check_broad_selloff
        check_vix_alert(td)
        check_broad_selloff(td)
    except Exception as e:
        log.warning(f"  Intraday alerts: {e}")

    # 5. Refresh dashboard state so the "Updated:" timestamp on top reflects
    #    this cycle, not just the once-daily post-market rebuild.
    run_job("dashboard_refresh", _rebuild_dashboard, td)


def run_intraday_30min():
    """
    Runs every 30 min during market hours.
    REFRESH: Dhan OHLC bars (15-min candles for technical analysis).
    Global markets no longer run here — moved to run_intraday_15min above.
    """
    if not is_market_hours(): return
    if not _debounce("intraday_30min"): return

    # Dhan intraday 15-min OHLC bars (for technical analysis)
    try:
        from data.dhan import run_historical_pipeline
        run_job("dhan_15min_bars",
                run_historical_pipeline,
                None,       # use default symbols
                days=1,     # today only
                interval_min=15)
    except Exception as e:
        log.warning(f"  Dhan 15-min bars: {e}")


def run_midday_news():
    """12:00 PM — fresh news since pre-market digest."""
    if not is_market_day(): return
    from data.news import run_news_pipeline
    run_job("news_midday", run_news_pipeline, 5)
    _news_summary()
    _announcements("announcements_midday")          # W28b (NS-06)


def _announcements(job):
    """W28b (NS-06): NSE corporate announcements of the day; documents of tracked stocks to
    the news model when news.ai_enabled (same daily cost cap)."""
    try:
        from data.announcements import run_announcements
        run_job(job, run_announcements, 1)
    except Exception as e:
        log.warning(f"  Announcements: {e}")


def _news_summary():
    """W28 (NS-04): market brief over the last news.summary_hours of headlines --
    Claude Haiku 4.5 when news.ai_enabled and under the daily cap, else rule-based."""
    try:
        from data.news_ai import run_summary_job
        run_job("news_summary", run_summary_job)
    except Exception as e:
        log.warning(f"  News summary: {e}")


def run_intraday_scan_job():
    """W28 (SG-08): intraday scans on live quotes + today's closed 15-min bars against
    the previous session's scores (strategy/intraday_scan.py)."""
    if not is_market_hours(): return
    try:
        from strategy.intraday_scan import run_intraday_scans
        run_job("intraday_scans", run_intraday_scans)
    except Exception as e:
        log.warning(f"  Intraday scans: {e}")


def run_midday_zpi_scan():
    """12:30 PM — intraday scans (W28). This used to call check_zpi_alerts(today),
    which reads TODAY's ai_scores -- rows written only by the 16:05 post-market run
    -- so it could never find a candidate. The zpi_pullback scan replaces it."""
    if not is_market_day(): return
    run_intraday_scan_job()


def run_preclose_scan():
    """2:45 PM — power-hour momentum scan."""
    if not is_market_hours(): return
    log.info(f"  [{now_ist()}] Pre-close scan")
    _run_dhan_quotes(date.today(), "preclose")
    run_intraday_scan_job()                          # W28: scans on the fresh quotes


# ═════════════════════════════════════════════════════════════════════════
#  POST-MARKET PIPELINE  (3:35 PM – 6:30 PM)
#
#  3:35 PM:   Final live quotes snapshot + intraday 1-min bars
#  4:05 PM:   NSE Bhavcopy (official EOD) + FII/DII data
#  4:30 PM:   Dhan historical daily update
#  4:45 PM:   Technical indicators (35 indicators)
#  5:00 PM:   AI scoring engine (all 9 indexes)
#  5:30 PM:   Portfolio re-sync with fresh AI scores
#  5:45 PM:   Telegram alerts (CRI, FII, TOD)
#  6:00 PM:   Dashboard rebuild
# ═════════════════════════════════════════════════════════════════════════

def run_postmarket(force=False, target_date=None, backfill=False):
    """
    Score one trading session end to end.

    target_date scores a session other than the one the clock implies, and
    backfill=True marks it as recovering an OLDER session: the steps whose data
    is "as of now" rather than as of that session — the live quote snapshot, the
    portfolio re-sync, the alerts — are skipped, because running them would
    stamp today's prices and holdings onto a past date and raise alerts about a
    session that is already over.
    """
    if not force and not is_market_day():
        log.info("⏩  Weekend — skipping post-market"); return

    # Which trading day's EOD data are we actually after right now?
    # - trading day, on/after 4:00 PM IST -> today
    # - trading day, before 4:00 PM IST   -> previous trading day (today's isn't out yet)
    # - non-trading day (forced run)      -> previous trading day
    # ...unless a specific session is being scored (catch-up/backfill).
    td = target_date or postmarket_target_date()
    log.info(f"\n{'='*55}\n  POST-MARKET PIPELINE — target date {td}\n{'='*55}")

    # 3:35 PM — Final live snapshot from Dhan. "Live" means as of now, so a
    # backfill would stamp today's prices onto a session that is already over.
    if not backfill:
        _run_dhan_quotes(td, label="market_close")

    # 4:05 PM — NSE Bhavcopy (official EOD prices + delivery data)
    from data.bhavcopy import run_bhavcopy_pipeline, run_bulk_deals_pipeline
    run_job("bhavcopy_eod", run_bhavcopy_pipeline, td)

    # 4:10 PM — NSE bulk & block deals (feeds compute_ins()'s BulkDeals
    # component -- see scores/engine.py). Best-effort: NSE only
    # publishes each day's CSV for that trading day.
    run_job("bulk_block_deals", run_bulk_deals_pipeline, td)

    # W27 (DP-08 partial / SC-06): NSE F&O bhavcopy -> per-underlying OI / PCR /
    # max pain, and the session's NIFTY PCR that compute_msi's Options reads.
    try:
        from data.derivatives import run_fo_pipeline
        run_job("fo_bhavcopy", run_fo_pipeline, td, 5)
    except Exception as e:
        log.warning(f"  F&O bhavcopy: {e}")
    # W30 (QR-05): close paper futures shorts whose contract has expired, at the final close
    try:
        from execution.futures_paper import settle_expired
        run_job("futures_expiry_settlement", settle_expired, td)
    except Exception as e:
        log.warning(f"  Futures settlement: {e}")

    # Corporate actions -- NSE's calendar, then any split or bonus whose ex-date
    # has arrived is applied to the stored history. Before the Dhan re-sync,
    # which must find the rows before an ex-date still on the old basis.
    try:
        from data.corporate_actions import run_corporate_actions
        run_job("corporate_actions", run_corporate_actions, td)
    except Exception as e:
        log.warning(f"  Corporate actions: {e}")

    # 4:30 PM — Dhan historical daily data (fills any gaps + confirms EOD)
    try:
        from data.dhan import run_historical_pipeline
        run_job("dhan_historical_eod",
                run_historical_pipeline,
                None, days=5, interval_min=0, end_date=td)   # last 5 days up to the resolved trading day
    except Exception as e:
        log.warning(f"  Dhan historical: {e}")

    # 4:35 PM — Nifty 50 benchmark history (feeds beta_1y calc for every stock)
    try:
        from data.dhan import sync_index_benchmark_history
        run_job("benchmark_history", sync_index_benchmark_history, days=5, end_date=td)
        # The other daily index series (India VIX, Bank Nifty, Midcap 150,
        # Smallcap 250): what Market Health reads for a session index_levels
        # does not cover. NSE's file below supplies the session itself.
        from data.dhan import INDEX_SERIES_SYMBOLS
        for key in INDEX_SERIES_SYMBOLS:
            if key != "nifty50":
                run_job(f"{key}_history", sync_index_benchmark_history, days=5, end_date=td,
                        index_key=key)
    except Exception as e:
        log.warning(f"  Benchmark history: {e}")

    # The session's own official closes from NSE -- Dhan's index history above
    # ends the day before -- and, if the live feed did not record the close, a
    # closing index_levels snapshot for compute_mh. Must run before scoring.
    try:
        from data.bhavcopy import sync_nse_index_closes
        run_job("nse_index_closes", sync_nse_index_closes, td)
    except Exception as e:
        log.warning(f"  NSE index closes: {e}")

    # Data quality (DP-19): record what is wrong with the session's stored data
    # -- missing, invalid, stale, gapped, abnormal -- before anything scores it.
    # It reports and alerts; the coverage and freshness guards below still
    # decide whether the session may be scored.
    try:
        from data.quality import run_data_quality
        run_job("data_quality", run_data_quality, td)
    except Exception as e:
        log.warning(f"  Data quality: {e}")

    # ── Never score a day without that day's closing prices ──────────────
    # Scoring on older bars and labelling the result `td` is what silently broke
    # signals from 2026-09-10: indicators and scores for each day were really
    # the previous day's, and every signal was logged with a NULL entry price
    # (the join to prices_daily for td found nothing), which evaluate_outcomes
    # skips -- so no signal since could ever be tracked. It is also what scored
    # the Ganesh Chaturthi holiday as if it were a session. Skip instead, loudly;
    # run_postmarket_if_missing() retries at POSTMARKET_CATCHUP_TIME.
    def _finish_without_scoring(status, why, **info):
        log.warning(f"  ⏭  {why} — NOT scoring. Outcome tracking and the dashboard "
                    f"still update.")
        try:
            from scores.signal_log import evaluate_outcomes
            run_job("signal_outcomes", evaluate_outcomes)
        except Exception as e:
            log.warning(f"  Signal outcomes: {e}")
        run_job("dashboard_rebuild", _rebuild_dashboard, td)
        log.info(f"⏭  Post-market finished WITHOUT scoring ({status})\n")
        return {"status": status, "date": str(td), **info}

    ok, n_eod, n_uni = _eod_coverage(td)
    if not ok:
        return _finish_without_scoring(
            "SKIPPED_NO_EOD",
            f"No EOD prices for {td} ({n_eod}/{n_uni} scored symbols have a bar). Either NSE "
            f"hasn't published yet or it wasn't a trading session",
            bars=n_eod, universe=n_uni)
    if n_uni:
        log.info(f"  ✓ EOD coverage for {td}: {n_eod}/{n_uni} scored symbols have today's bar")

    # ...and a bar that exists is not necessarily today's. See EOD_STALE_MAX.
    fresh, n_same, n_chk = _eod_freshness(td)
    if not fresh:
        return _finish_without_scoring(
            "SKIPPED_STALE_EOD",
            f"EOD prices for {td} are a copy of the previous session: {n_same}/{n_chk} tracked "
            f"symbols carry a bar identical to their last one",
            identical=n_same, checked=n_chk)
    if n_chk:
        log.info(f"  ✓ EOD freshness for {td}: {n_same}/{n_chk} bars identical to the "
                 f"previous session")

    # 4:45 PM — Compute 35 technical indicators
    from data.technical import run_technical_pipeline
    run_job("technical_indicators", run_technical_pipeline, td)
    # W21: GIFT Nifty daily close + sector breadth (DP-11 / DP-13)
    try:
        from data.market_series import run_scheduled as run_market_series
        run_job("market_series", run_market_series, td)
    except Exception as e:
        log.warning(f"  Market series: {e}")

    # 5:00 PM — Run all 9 AI scoring indexes
    # (this also writes today's predictions — entry/SL/targets/size — which is
    # what makes the accuracy tracker below able to measure anything at all)
    # W28b (NS-05): source weights + per-stock weighted news scores, read by get_ns()
    try:
        from data.news_weighting import run_scheduled as run_news_weights
        run_job("news_weights", run_news_weights, td)
    except Exception as e:
        log.warning(f"  News weights: {e}")
    from scores.engine import run_scoring_pipeline
    run_job("ai_scoring_engine", run_scoring_pipeline, td)

    # 5:15 PM — Measure any predictions whose 5/10/20-day horizon has matured.
    # Runs daily rather than only in the Saturday weekly job: the 5-day horizon
    # matures every week and waiting for Saturday delays every outcome by up to
    # 6 days. It's cheap and self-backfilling — it only touches rows that have
    # matured and aren't measured yet.
    try:
        from scores.accuracy import update_accuracy
        run_job("accuracy_update", update_accuracy)
    except Exception as e:
        log.warning(f"  Accuracy update: {e}")

    # 5:30 PM — Re-sync portfolio with fresh AI scores (holdings are as of now)
    if not backfill:
        _run_portfolio_sync(td)

    # 5:35 PM — Portfolio Health Score (doc section 11). After the sync so it
    # scores today's holdings; it reads per-stock scores from ai_scores directly,
    # so a failed sync costs it inputs rather than the whole score.
    try:
        from scores.portfolio_health import store_phs
        run_job("portfolio_health", store_phs, td)
    except Exception as e:
        log.warning(f"  Portfolio health: {e}")

    # Daily P&L (PF-13): the session's equity, day P&L and drawdown for the
    # paper and live books -- what the daily-loss and drawdown limits
    # (orders/risk.py) measure against. After the sync, so it values today's book.
    if not backfill:
        try:
            from portfolio.pnl import record_daily_pnl
            run_job("daily_pnl", record_daily_pnl, td)
        except Exception as e:
            log.warning(f"  Daily P&L: {e}")
        # W25: VaR / concentration / correlation snapshot of each book, after its P&L
        try:
            from portfolio.risk import run_scheduled as run_portfolio_risk
            run_job("portfolio_risk", run_portfolio_risk, td)
        except Exception as e:
            log.warning(f"  Portfolio risk: {e}")

    # Strategy Engine (W3): decisions for every PAPER / READY / ACTIVE strategy
    # on this session's scores, then a health snapshot for every strategy.
    # Decisions are position intents (NOT_AUTHORIZED) -- nothing is ordered.
    if not backfill:
        # W5: ML predictions first, so strategies deciding next can read today's
        # ml_* features. SKIPPED unless config ml.enabled and a model is ACTIVE.
        try:
            from ml.predict import run_scheduled_predictions
            run_job("ml_predictions", run_scheduled_predictions, td)
            # W24: model drift / decay + factor decay (SKIPPED unless ml or quant is
            # enabled) and the anomaly scan (SKIPPED unless ml.anomalies_enabled)
            from ml.decay import run_scheduled as run_ml_health
            from ml.unsupervised import run_scheduled_anomalies
            run_job("ml_model_health", run_ml_health, td)
            run_job("ml_anomalies", run_scheduled_anomalies, td)
        except Exception as e:
            log.warning(f"  ML predictions: {e}")
        # W6: factor scores, composites, events, intraday microstructure -- before the
        # strategies that read qf_* / qc_* / ev_*. SKIPPED unless config quant.enabled.
        try:
            from quant.engine import run_scheduled as run_quant
            run_job("quant_factors", run_quant, td)
        except Exception as e:
            log.warning(f"  Quant factors: {e}")
        # W30: research data that strategies read (ms_* / ev_*), prepared whether or not
        # quant scoring is on -- bar microstructure (AF-07) and event sources (QR-09)
        try:
            from quant.w30 import run_postmarket as run_w30
            run_job("research_data", run_w30, td)
        except Exception as e:
            log.warning(f"  Research data: {e}")
        try:
            from strategy_engine.engine import run_scheduled_decisions
            from strategy_engine.health import run_health_all
            run_job("strategy_decisions", run_scheduled_decisions, td)
            run_job("strategy_health", run_health_all, td)
        except Exception as e:
            log.warning(f"  Strategy engine: {e}")
        # W4: new intents -> risk engine. Orders are sent only when
        # execution.auto_execute_paper is true (default false) and only to the
        # PAPER adapter; LIVE execution does not exist in W4.
        try:
            from execution.pipeline import run_execution_cycle
            run_job("execution_cycle", run_execution_cycle, td)
        except Exception as e:
            log.warning(f"  Execution cycle: {e}")
        # W29 (BR-05): OMS vs broker book; a break raises an alert
        try:
            from execution.reconcile import run_reconciliation
            run_job("execution_reconciliation", run_reconciliation, td)
        except Exception as e:
            log.warning(f"  Reconciliation: {e}")
        # W7: workspace alert rules + tenant usage metering. SKIPPED unless
        # config enterprise.enabled.
        try:
            from enterprise.service import run_scheduled as run_enterprise
            run_job("enterprise_jobs", run_enterprise, td)
        except Exception as e:
            log.warning(f"  Enterprise jobs: {e}")
        # W17: investor cycle (snapshot, ledger import, DNA / allocation / goals / rebalance,
        # weekly performance report, wealth alerts) for every owner with an Investor DNA.
        # Advisory only: nothing is ordered. SKIPPED unless config wealth.enabled.
        try:
            from wealth.integrated import run_scheduled as run_wealth
            run_job("wealth_cycle", run_wealth, td)
        except Exception as e:
            log.warning(f"  Wealth cycle: {e}")

    # 5:40 PM — Append today's signals to the immutable log, then re-evaluate
    # momentum outcomes for every open signal. Append-only: unlike ai_scores and
    # predictions, a re-run never overwrites what was previously said.
    # Held while NSE has not published this session's FII/DII and delivery:
    # these scores carry the previous session's flows and delivery, and
    # run_fii_dii_watch() re-scores the session and logs its signals once its
    # own appear. A backfilled session is logged now -- NSE's FII/DII API only
    # ever serves the latest day, so an older session's flows will not arrive.
    if not backfill and not all(_evening_data(td)):
        _hold_signals(td)
        try:
            from scores.signal_log import evaluate_outcomes
            run_job("signal_outcomes", evaluate_outcomes)
        except Exception as e:
            log.warning(f"  Signal outcomes: {e}")
    else:
        _log_session_signals(td)

    # 5:45 PM — Send Telegram alerts. Never for a backfill: an alert about a
    # session that closed days ago is noise, and the TOD pick is not actionable.
    if not backfill:
        try:
            from alerts.telegram import check_cri_alerts, check_fii_alert, send_tod_alert
            check_cri_alerts(td)
            check_fii_alert(td)
            send_tod_alert(td)
        except Exception as e:
            log.warning(f"  Post-market alerts: {e}")

    # 6:00 PM — Rebuild dashboard state. A backfill leaves this to its caller,
    # which rebuilds once for the newest session instead of once per old one.
    if not backfill:
        run_job("dashboard_rebuild", _rebuild_dashboard, td)

    log.info("✅  Post-market pipeline complete"
             + (f" (backfilled {td})" if backfill else "") + "\n")
    return {"status": "SCORED", "date": str(td)}


def _evening_data(td):
    """(FII/DII stored, delivery stored) for the session."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        flows = bool(conn.execute("SELECT 1 FROM fii_dii_market WHERE date=?", (str(td),)).fetchone())
        deliv = bool(conn.execute("SELECT 1 FROM prices_daily WHERE date=? AND delivery_pct IS NOT NULL "
                                  "LIMIT 1", (str(td),)).fetchone())
        return flows, deliv
    finally:
        conn.close()


def _signal_log_state(td):
    """'logged', 'held' or None, from this session's signal_log job records."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        states = {r[0] for r in conn.execute(
            "SELECT status FROM pipeline_log WHERE job_name='signal_log' AND run_date=?", (str(td),))}
    finally:
        conn.close()
    return "logged" if "SUCCESS" in states else "held" if "HELD" in states else None


def _hold_signals(td):
    from db.schema import log_job
    log.info(f"  ⏸  Signals for {td} held: NSE has not published the session's FII/DII and "
             f"delivery yet, so these scores carry the previous session's. Watching until "
             f"{FII_DII_WATCH_END}; the session is re-scored and logged when they appear.")
    log_job("signal_log", "HELD", 0, run_date=td)


def _log_session_signals(td):
    try:
        from scores.signal_log import log_signals, evaluate_outcomes
        run_job("signal_log", log_signals, td)
        run_job("signal_outcomes", evaluate_outcomes)
    except Exception as e:
        log.warning(f"  Signal log: {e}")


def settle_session_flows(td, final=False) -> dict:
    """
    For a session whose signals are held: fetch NSE's FII/DII and delivery;
    once the session's own are both stored, re-score it on them and log its
    signals. With final=True and one still missing, log the signals on the
    best scores available rather than never.
    """
    from db.schema import get_connection
    from data.bhavcopy import store_fii_dii, run_delivery_pipeline, sync_nse_index_closes
    flows, deliv = _evening_data(td)
    if not flows:
        conn = get_connection()
        try:
            store_fii_dii(conn, td)
        finally:
            conn.close()
    if not deliv:
        run_delivery_pipeline(td, lookback=1)
    # NSE publishes the index-close file in the evening too -- 2026-09-22's was
    # not out at 16:48, so that session never got a benchmark close. Fill it
    # (and any recent session still missing one) before re-scoring.
    sync_nse_index_closes(td, lookback=CATCHUP_LOOKBACK_SESSIONS)
    if _signal_log_state(td) != "held":
        return {"status": "SKIPPED", "rows": 0, "reason": "signals not held"}
    current = td == postmarket_target_date()
    flows, deliv = _evening_data(td)
    if (flows and deliv) or (final and (flows or deliv)):
        missing = [] if flows and deliv else ["FII/DII"] if not flows else ["delivery"]
        log.info(f"  ✓ NSE's evening data for {td} is in ({now_ist()}"
                 + (f"; still missing {missing[0]} at the deadline" if missing else "")
                 + ") — re-scoring the session on it")
        from scores.engine import run_scoring_pipeline
        run_job("ai_scoring_engine", run_scoring_pipeline, td)
        try:
            from scores.portfolio_health import store_phs
            run_job("portfolio_health", store_phs, td)
        except Exception as e:
            log.warning(f"  Portfolio health: {e}")
        _log_session_signals(td)
        if current:
            try:
                from alerts.telegram import check_fii_alert
                check_fii_alert(td)
            except Exception as e:
                log.warning(f"  FII alert: {e}")
            run_job("dashboard_rebuild", _rebuild_dashboard, td)
        return {"status": "SUCCESS" if not missing else "PARTIAL", "rows": 1, "rescored": True}
    if final:
        log.warning(f"  ⚠ NSE had published neither FII/DII nor delivery for {td} by "
                    f"{FII_DII_WATCH_END} — logging its signals as scored at 16:45")
        _log_session_signals(td)
        return {"status": "PARTIAL", "rows": 1, "rescored": False}
    return {"status": "SKIPPED", "rows": 0, "reason": "not published yet"}


def run_fii_dii_watch():
    """Every FII_DII_WATCH_EVERY minutes from FII_DII_WATCH_START to _END on a
    trading day, while the session's signals are held for its FII/DII and
    delivery."""
    if not is_market_day():
        return
    td = date.today()
    if _signal_log_state(td) != "held":
        return          # logged already, or post-market has not scored yet
    final = datetime.now().strftime("%H:%M") >= FII_DII_WATCH_END
    run_job("fii_dii_watch", settle_session_flows, td, final)


def release_held_signals(now=None):
    """A session still held after its watch window -- ATIP was not running at
    the time -- is settled now: re-scored if NSE still serves its flows,
    otherwise logged as it stands."""
    now = now or datetime.now()
    for d in _recent_sessions(CATCHUP_LOOKBACK_SESSIONS, postmarket_target_date()):
        closed = d < now.date() or now.strftime("%H:%M") >= FII_DII_WATCH_END
        if closed and _signal_log_state(d) == "held":
            run_job("fii_dii_watch", settle_session_flows, d, True)


def _rebuild_dashboard(td):
    try:
        from dashboard.server import generate_state
        generate_state(td)
        return {"status": "SUCCESS", "rows": 1}
    except Exception as e:
        log.warning(f"  Dashboard rebuild: {e}")
        return {"status": "PARTIAL"}


# ═════════════════════════════════════════════════════════════════════════
#  OVERNIGHT PIPELINE  (11:00 PM)
#  REFRESH: US market close data, commodities, FX
# ═════════════════════════════════════════════════════════════════════════

def run_morning_digest():
    """8:30 AM Morning Brief. Skips non-trading days — there is no 'what should
    I do today' on a Sunday."""
    if not is_market_day():
        log.info("⏩  Not a trading day — skipping morning brief"); return
    log.info(f"\n{'='*55}\n  MORNING BRIEF — {date.today()}\n{'='*55}")
    try:
        from alerts.telegram import send_morning_digest
        run_job("morning_digest", lambda: {"status": "SUCCESS", "rows": int(bool(send_morning_digest()))})
    except Exception as e:
        log.warning(f"  Morning brief: {e}")


def run_health_check():
    """Compare what ran with what should have (pipeline/health.py), RE-RUN what was
    missed or failed (pipeline/recover.py: current global markets, news, portfolio,
    unscored sessions, the brief), then alert only on what is still wrong -- once a
    day per problem, on the dashboard and Telegram."""
    try:
        from pipeline.health import format_problems
        from pipeline.recover import check_and_recover
        from alerts.telegram import send_job_health_alerts, send_recovery_notice
        out = check_and_recover()
        if out["ran"]:
            send_recovery_notice(out)
        problems = out["problems"]
        if problems:
            log.warning("  🩺 Job health:\n" + format_problems(problems))
            send_job_health_alerts(problems)
        return problems
    except Exception as e:
        log.warning(f"  Job health check: {e}")
        return []


def run_eod_late():
    """
    Data NSE publishes after the post-market run: delivery, from the full
    Bhavcopy. Nothing scores on it yet; it is stored so the history is complete.
    Looks back CATCHUP_LOOKBACK_SESSIONS sessions, so a missed evening (or a
    file NSE publishes late) is picked up by the next one.
    """
    from data.bhavcopy import run_delivery_pipeline
    run_job("delivery", run_delivery_pipeline, None, CATCHUP_LOOKBACK_SESSIONS)
    _announcements("announcements_eod")             # W28b (NS-06): results land in the evening


def run_overnight():
    log.info(f"\n{'='*55}\n  OVERNIGHT PIPELINE — {date.today()}\n{'='*55}")

    # US market close (after NYSE closes at ~1:30 AM IST, data by 11 PM)
    from data.markets import fetch_global_markets
    run_job("global_overnight", fetch_global_markets, date.today(), "overnight")

    log.info("✅  Overnight pipeline complete\n")


# ═════════════════════════════════════════════════════════════════════════
#  WEEKLY PIPELINE  (Saturday 8:00 AM)
#  REFRESH: Accuracy audit + fundamental data refresh
# ═════════════════════════════════════════════════════════════════════════

def run_weekly():
    log.info(f"\n{'='*55}\n  WEEKLY PIPELINE — {date.today()}\n{'='*55}")

    # Accuracy tracker — compare predictions vs actual outcomes
    try:
        from scores.accuracy import update_accuracy
        run_job("accuracy_audit", update_accuracy)
    except Exception as e:
        log.warning(f"  Accuracy audit: {e}")

    # W27: fundamentals (DP-15) and ownership (DP-16) from NSE filings, for the
    # whole tracked universe. Incremental -- only XBRLs not stored yet are fetched.
    # The first run downloads ~12 filings per symbol (about an hour); run it once
    # by hand off-hours:  python -m data.nse_filings
    if not fundamentals_enabled():
        log.info(f"  ⏸  Fundamentals refresh held off — {FUNDAMENTALS_HOLD_REASON}")
    else:
        try:
            from data.fundamentals import run_fundamentals_pipeline
            run_job("fundamentals_weekly", run_fundamentals_pipeline)
        except Exception as e:
            log.warning(f"  Weekly fundamentals: {e}")
    try:
        from data.institutional import run_institutional_pipeline
        run_job("institutional_weekly", run_institutional_pipeline)
    except Exception as e:
        log.warning(f"  Weekly institutional: {e}")
    try:
        from quant.deal_signal import run_scheduled as run_deal_signal
        run_job("deal_signal_study", run_deal_signal)
    except Exception as e:
        log.warning(f"  Deal-signal study: {e}")
    try:                                             # W30 (QR-09): weekly event studies
        from quant.w30 import run_weekly_studies
        run_job("event_studies", run_weekly_studies)
    except Exception as e:
        log.warning(f"  Event studies: {e}")

    # Refresh Dhan security list (in case of new listings/delistings)
    try:
        from data.dhan import download_security_list
        run_job("dhan_security_refresh", download_security_list)
    except Exception as e:
        log.warning(f"  Security list refresh: {e}")

    # Database retention purge — trims tables past their retention window
    # (600 days for prices/indicators/scores, 90 days for high-frequency
    # operational tables). See db/purge.py for the per-table policy.
    try:
        from db.purge import purge_old_data
        run_job("db_purge", purge_old_data, dry_run=False)
    except Exception as e:
        log.warning(f"  DB purge: {e}")

    log.info("✅  Weekly pipeline complete\n")


# ═════════════════════════════════════════════════════════════════════════
#  STATUS REPORT
# ═════════════════════════════════════════════════════════════════════════

def print_status():
    from db.schema import get_connection
    conn  = get_connection()
    today = str(date.today())

    print(f"\n{'='*65}")
    print(f"  ATIP Pipeline Status — {today}  ({now_ist()} IST)")
    print(f"{'='*65}")

    # Recent jobs
    rows = conn.execute(
        "SELECT job_name,status,rows_processed,start_time FROM pipeline_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    print("\n  Recent Jobs:")
    print(f"  {'Job':<32} {'Status':<10} {'Rows':>6}  {'Time'}")
    print(f"  {'-'*32} {'-'*10} {'-'*6}  {'-'*20}")
    for r in rows:
        icon = "✅" if r["status"] == "SUCCESS" else "❌" if r["status"] == "FAILED" else "⚠️"
        print(f"  {icon} {r['job_name']:<30} {r['status']:<10} {r['rows_processed']:>6}  {str(r['start_time'])[:19]}")

    # Table row counts
    print(f"\n  Database Row Counts:")
    tables = [
        ("prices_daily",          "EOD prices"),
        ("live_quotes",           "Live quotes"),
        ("live_ticks",            "WS ticks"),
        ("technical_indicators",  "Tech indicators"),
        ("fundamental_data",      "Fundamentals"),
        ("ai_scores",             "AI scores"),
        ("news_articles",         "News articles"),
        ("fii_dii_market",        "FII/DII data"),
        ("index_levels",          "Index levels"),
        ("global_markets",        "Global markets"),
        ("portfolio_holdings",    "Portfolio"),
        ("predictions",           "Predictions"),
        ("accuracy_tracker",      "Accuracy"),
    ]
    for t, label in tables:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            # created_at is SQLite's CURRENT_TIMESTAMP -- UTC -- so it is shifted
            # to IST before comparing with today's IST date (db/schema.py)
            today_n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE date=? OR "
                                   f"DATE(created_at,'+5 hours','+30 minutes')=?",
                                   (today, today)).fetchone()[0]
            icon = "✅" if n > 0 else "⚠️"
            print(f"  {icon} {label:<22} {n:>8,} total  {today_n:>6,} today")
        except Exception:
            print(f"  ⚠️  {label:<22} (table not yet created)")

    # Today's intelligence
    mh  = conn.execute("SELECT mh_score,regime FROM market_health WHERE date=?", (today,)).fetchone()
    tod = conn.execute("SELECT symbol,tod_score,atip_score FROM ai_scores WHERE date=? AND is_tod=1", (today,)).fetchone()
    lq  = conn.execute("SELECT COUNT(*) FROM live_quotes WHERE DATE(timestamp)=?", (today,)).fetchone()
    idx = conn.execute("SELECT nifty50_chg,india_vix,overall_sentiment FROM index_levels WHERE date=? ORDER BY time DESC LIMIT 1", (today,)).fetchone()

    print(f"\n  Today's Intelligence:")
    print(f"  Market Health  : {mh['mh_score']:.1f} ({mh['regime']})" if mh else "  Market Health  : not computed")
    print(f"  Trade of Day   : {tod['symbol']} — ATIP:{tod['atip_score']:.1f} TOD:{tod['tod_score']:.1f}" if tod else "  Trade of Day   : not computed")
    if idx:
        print(f"  Nifty          : {idx['nifty50_chg']:+.2f}%  VIX:{idx['india_vix']}  → {idx['overall_sentiment']}")
    print(f"  Live Quotes    : {lq[0] if lq else 0} stocks refreshed today")
    print(f"\n  Data Refresh Schedule:")
    print(f"  09:15–15:30    Dhan live quotes  every 15 min")
    print(f"  09:15–15:30    NSE indexes       every 15 min")
    print(f"  09:15–15:30    Global markets    every 15 min")
    print(f"  12:00 PM       News refresh      once")
    print(f"  16:05 PM       EOD pipeline      once (Bhavcopy + AI scores)")
    print(f"  23:00 PM       Overnight global  once")
    print(f"  Saturday       Weekly audit      once")
    print()
    conn.close()


# ═════════════════════════════════════════════════════════════════════════
#  MASTER SCHEDULER  (registers all jobs and runs forever)
# ═════════════════════════════════════════════════════════════════════════

def _schedule_ops_jobs():
    """W8 jobs (ops/): the monitor every ops.monitor_minutes, the verified backup
    daily at ops.backup_time, outbound webhook delivery every 5 minutes. Each is
    switchable in config.json "ops" (monitor_enabled, backup_enabled)."""
    from ops.config import OPS_DEFAULTS
    try:
        from ops.config import ops as _ops
        cfg = _ops()
    except Exception as e:
        log.warning(f"  ops config unreadable ({e}) — ops jobs use defaults")
        cfg = dict(OPS_DEFAULTS)
    if cfg.get("monitor_enabled", True):
        from ops.monitor import run_monitor
        schedule.every(max(1, int(cfg.get("monitor_minutes") or 15))).minutes.do(run_job, "ops_monitor", run_monitor)
    if cfg.get("backup_enabled", True):
        from ops.backup import run_scheduled_backup
        schedule.every().day.at(str(cfg.get("backup_time") or "19:15")).do(run_job, "ops_backup",
                                                                             run_scheduled_backup)
    schedule.every(5).minutes.do(_ops_webhook_tick)
    if cfg.get("restore_drill_enabled", True):               # W31 (OPS-06): weekly restore drill
        from ops.backup import restore_drill
        schedule.every().sunday.at(str(cfg.get("restore_drill_time") or "10:00")).do(run_job, "restore_drill",
                                                                                    restore_drill)
    _schedule_w29_jobs()


def _schedule_w29_jobs():
    """W29 execution jobs. Market-hours guards inside each tick keep pipeline_log quiet
    outside the session."""
    schedule.every(2).minutes.do(_w29_paper_match_tick)
    schedule.every(5).minutes.do(_w29_broker_health_tick)
    schedule.every(15).minutes.do(_w29_pnl_tick)
    schedule.every().day.at("08:45").do(run_job, "broker_health", _broker_health_now)
    schedule.every().day.at("19:30").do(_w29_audit_export)
    _schedule_w32_jobs()
    _schedule_w34_jobs()
    _schedule_w35_jobs()
    schedule.every().day.at("16:20").do(_w37_options_settle)           # W37 (ENT-15)
    schedule.every().day.at("08:10").do(_w37_vault_expiry)             # W37 (ENT-06)
    schedule.every().day.at("07:50").do(_w38_compliance)              # W38 (SEC-05)
    _schedule_w39_jobs()


def _schedule_w39_jobs():
    """W39: order-book pressure every 15 minutes in the session (data/order_pressure.py); the pre-open
    GIFT / global-cue estimate at 08:45 and 09:05 and its check against the open at 09:35, NSE
    participant OI at 20:15 and the Nifty history at 23:20 (research/market_pulse.py); the
    technical snapshot and signals (research/tech_signals.py), the earnings surprise and post-earnings-drift
    scan at 20:35 (research/earnings_surprise.py), equity research reports after the
    evening scoring (research/report.py), the day's fundamental scorecards and the saved
    screens after them (research/scorecard.py, research/screener.py), intraday scans on the stored 15-minute
    bars every 15 minutes in the session (research/intraday_signals.py), global snapshots at 15:31 and
    08:42 for the synchronised gap model (research/global_sync.py), and the nightly, budgeted 7-year
    price-history backfill (data/history_backfill.py); the NSE pre-open auction at 09:09, once order entry
    has closed, and its outcome after the post-market prices at 17:10 (data/preopen.py)."""
    schedule.every(15).minutes.do(_w39_order_pressure_tick)
    schedule.every().day.at("08:45").do(_w39_gift)
    schedule.every().day.at("09:05").do(_w39_gift)
    schedule.every().day.at("09:35").do(_w39_gap_eval)
    schedule.every().day.at("20:15").do(_w39_participant_oi)
    schedule.every().day.at("23:20").do(_w39_nifty_history)
    schedule.every().day.at("20:30").do(_w39_technical_signals)
    schedule.every().day.at("20:35").do(_w39_earnings_surprise)
    schedule.every().day.at("20:40").do(_w39_research_reports)
    schedule.every().day.at("20:50").do(_w39_saved_screens)
    schedule.every().day.at("22:20").do(_w39_history_backfill)
    schedule.every(15).minutes.do(_w39_intraday_tick)
    schedule.every().day.at("15:31").do(_w39_global_sync, "close")
    schedule.every().day.at("08:42").do(_w39_global_sync, "pre")
    schedule.every().day.at("09:09").do(_w39_preopen_capture)
    schedule.every().day.at("17:10").do(_w39_preopen_evaluate)


def _w39_global_sync(label):
    """Global futures, Asia and FX at India's close and before the open (research/global_sync.py)."""
    if not is_market_day():
        return
    try:
        from research.global_sync import run_capture
        run_job(f"global_sync_{label}", run_capture, label)
    except Exception as e:
        log.warning(f"  Global snapshot ({label}): {e}")


def _w39_intraday_tick():
    """Intraday scans on the stored 15-minute bars, 09:45-15:50 on trading days; evaluates at the close
    (research/intraday_signals.py)."""
    from datetime import time as dtime
    if not is_market_day():
        return
    now = datetime.now().time()
    if not (dtime(9, 45) <= now <= dtime(15, 50)):
        return
    try:
        from research.intraday_signals import run_job as intraday_job
        run_job("intraday_signals", intraday_job)
    except Exception as e:
        log.warning(f"  Intraday signals: {e}")


def _w39_preopen_capture():
    """The NSE pre-open auction's final picture: IEP and total buy / sell per stock (data/preopen.py). Order
    entry closes at a random second between 09:07 and 09:08; 09:09 reads the closed book."""
    if not is_market_day():
        return
    try:
        from data.preopen import capture, settings as po_settings
        if po_settings()["enabled"]:
            run_job("preopen_capture", capture)
    except Exception as e:
        log.warning(f"  Pre-open capture: {e}")


def _w39_preopen_evaluate():
    """The pre-open rows' outcome (open, first 15 minutes, close) once post-market has stored the day's prices."""
    if not is_market_day():
        return
    try:
        from data.preopen import evaluate
        run_job("preopen_evaluate", evaluate)
    except Exception as e:
        log.warning(f"  Pre-open evaluation: {e}")


def _w39_order_pressure_tick():
    """Pending buy / sell totals for the tracked universe (data/order_pressure.py), in market hours."""
    try:
        from data.order_pressure import settings as op_settings, snapshot
        if op_settings()["enabled"] and is_market_hours():
            run_job("order_pressure", snapshot)
    except Exception as e:
        log.warning(f"  Order pressure: {e}")


def _w39_gift():
    """Pre-open GIFT Nifty + global-model gap estimate (research/market_pulse.py)."""
    if not is_market_day():
        return
    try:
        from research.market_pulse import capture_gift
        run_job("gift_preopen", capture_gift)
    except Exception as e:
        log.warning(f"  GIFT pre-open: {e}")


def _w39_gap_eval():
    if not is_market_day():
        return
    try:
        from research.market_pulse import evaluate_gaps
        run_job("gap_evaluation", evaluate_gaps)
    except Exception as e:
        log.warning(f"  Gap evaluation: {e}")


def _w39_participant_oi():
    if not is_market_day():
        return
    try:
        from data.participant_oi import run as poi_run
        run_job("participant_oi", poi_run)
    except Exception as e:
        log.warning(f"  Participant OI: {e}")


def _w39_nifty_history():
    """Keep the long Nifty close series the global-cue model regresses on (5 years the first time)."""
    try:
        from db.schema import get_connection
        from research.market_pulse import nifty_history
        c = get_connection()
        try:
            n = c.execute("SELECT COUNT(*) FROM global_market_history WHERE series='nifty50'").fetchone()[0]
        finally:
            c.close()
        run_job("nifty_history", nifty_history, "5y" if n < 300 else "1mo")
    except Exception as e:
        log.warning(f"  Nifty history: {e}")


def _w39_technical_signals():
    """Technical snapshot + scan signals for the tracked universe, outcomes of open signals, top-signal alert.
    The snapshot's 75-minute rating is built here from the day's stored 15-minute bars: after the close all
    five 75-minute bars are complete and the agreement compares it with the same session's daily rating."""
    if not is_market_day():
        return
    try:
        from research.tech_signals import run_job as tech_job
        run_job("technical_signals", tech_job)
    except Exception as e:
        log.warning(f"  Technical signals: {e}")


def _w39_earnings_surprise():
    """SUE, revenue surprise and the EPS-trend proxy for every stored quarter, then the post-earnings-drift
    scan into the signal engine and its (held until proven) alert (research/earnings_surprise.py)."""
    if not is_market_day():
        return
    try:
        from research.earnings_surprise import run_job as earnings_job
        run_job("earnings_surprise", earnings_job)
    except Exception as e:
        log.warning(f"  Earnings surprise: {e}")


def _w39_saved_screens():
    """Store the day's fundamental scorecards (with the DVM scores and zone), then re-run the saved screens
    after the research reports; alert on new matches."""
    if not is_market_day():
        return
    try:
        from research.screener import run_saved_screens
        run_job("saved_screens", run_saved_screens)
    except Exception as e:
        log.warning(f"  Saved screens: {e}")


def _w39_research_reports():
    if not is_market_day():
        return
    try:
        from research.report import settings as rsettings, run_reports
        if rsettings()["reports_enabled"]:
            run_job("research_reports", run_reports)
    except Exception as e:
        log.warning(f"  Research reports: {e}")


def _w39_history_backfill():
    try:
        from data.history_backfill import settings as hsettings, run_backfill
        if hsettings()["backfill_enabled"]:
            run_job("history_backfill", run_backfill)
    except Exception as e:
        log.warning(f"  History backfill: {e}")


def _w38_compliance():
    """Daily compliance-monitoring run (ops/compliance.py); alerts only on a check that got worse."""
    try:
        from ops.compliance import run_job as compliance_job
        run_job("compliance_checks", compliance_job)
    except Exception as e:
        log.warning(f"  Compliance checks: {e}")


def _w37_options_settle():
    """Settle paper options that expired today (intrinsic value) -- the owner's and (W40) the option-overlay
    strategies' positions -- then mark the strategies' open positions at today's chain. No-op without
    open positions."""
    if not is_market_day():
        return
    try:
        from execution.options_paper import mark_strategy_positions, settings as opt_settings, settle_expired
        if opt_settings()["enabled"]:
            run_job("options_expiry", settle_expired)
            run_job("options_strategy_marks", mark_strategy_positions)
    except Exception as e:
        log.warning(f"  Options expiry: {e}")


def _w37_vault_expiry():
    """Tell the owner which stored broker credentials expire today (daily tokens)."""
    try:
        from db.schema import get_connection
        from enterprise.vault import expiring
        c = get_connection()
        try:
            soon = expiring(c, hours=16)
        finally:
            c.close()
        if soon:
            from alerts.telegram import notify
            lines = [f"{x['broker']} ({x['label']}) {'EXPIRED' if x['expired'] else 'expires ' + x['expires_at']}"
                     for x in soon[:10]]
            notify("<b>Broker credentials</b>\n" + "\n".join(lines), category="vault", severity="warning",
                   key="vault_expiry")
    except Exception as e:
        log.warning(f"  Vault expiry reminder: {e}")


def _schedule_w35_jobs():
    """W35 data platform. Depth and option-chain ticks are no-ops unless enabled in config.json."""
    schedule.every(1).minutes.do(_w35_depth_tick)
    schedule.every(15).minutes.do(_w35_chain_tick)
    schedule.every().day.at("15:50").do(_w35_ticks_eod)
    schedule.every().day.at("07:15").do(_w35_guard, "macro_data", "data.macro", "run_macro")
    schedule.every().day.at("19:45").do(_w35_guard, "alt_data", "altdata.framework", "run_enabled")
    schedule.every().day.at("23:30").do(_w35_guard, "multi_asset", "data.multi_asset", "run_multi_asset")


def _w35_guard(name, module, fn):
    try:
        import importlib
        run_job(name, getattr(importlib.import_module(module), fn))
    except Exception as e:
        log.warning(f"  {name}: {e}")


def _w35_depth_tick():
    try:
        from data.depth import settings as depth_settings
        if depth_settings()["enabled"] and is_market_hours():
            from data.depth import run_tick
            run_tick()                               # once a minute: not a pipeline_log row each time
    except Exception as e:
        log.warning(f"  Depth snapshot: {e}")


def _w35_chain_tick():
    try:
        from data.derivatives_store import settings as dsettings
        if dsettings()["option_chain_enabled"] and is_market_hours():
            from data.derivatives_store import run_chain_tick
            run_job("option_chain", run_chain_tick)
    except Exception as e:
        log.warning(f"  Option chain: {e}")


def _w35_ticks_eod():
    try:
        from data.ticks import enabled
        if enabled() and is_market_day():
            from data.ticks import run_eod
            run_job("ticks_minute_bars", run_eod)
    except Exception as e:
        log.warning(f"  Tick minute bars: {e}")


def _schedule_w34_jobs():
    """W34 execution microstructure: algo ticks + event delivery each minute in session (logged only
    when there is work), latency rollups every 5 minutes, impact re-calibration weekly."""
    schedule.every(1).minutes.do(_w34_algo_tick)
    schedule.every(5).minutes.do(_w34_latency_flush)
    schedule.every().saturday.at("09:00").do(_w34_impact_calibration)


def _w34_algo_tick():
    if not is_market_hours():
        return
    try:
        from db.schema import get_connection
        c = get_connection()
        try:
            busy = c.execute("SELECT (SELECT COUNT(*) FROM exec_algo_parent WHERE status IN ('WAITING','WORKING')) + "
                             "(SELECT COUNT(*) FROM oms_event_outbox WHERE dispatched_at IS NULL AND attempts<5)"
                             ).fetchone()[0]
        finally:
            c.close()
        if busy:
            from execution.algos import run_scheduled as run_algos
            run_job("execution_algos", run_algos)
    except Exception as e:
        log.warning(f"  Execution algos: {e}")


def _w34_latency_flush():
    try:
        from ops.latency import flush
        flush()                                  # not a run_job: it would add a pipeline_log row every 5 min
    except Exception as e:
        log.warning(f"  Latency flush: {e}")


def _w34_impact_calibration():
    try:
        from execution.impact import run_scheduled as run_impact
        run_job("impact_calibration", run_impact)
    except Exception as e:
        log.warning(f"  Impact calibration: {e}")


def _schedule_w32_jobs():
    """W32 SaaS jobs; each is a no-op (no pipeline_log row) while enterprise.enabled is false."""
    schedule.every(5).minutes.do(_w32_saas_tick)
    schedule.every(15).minutes.do(_w32_alerts_tick)
    schedule.every().day.at("06:30").do(_w32_guard, "saas_daily", "saas_daily")
    schedule.every().day.at("19:00").do(_w32_guard, "saas_digest", "saas_digest")


def _w32_on():
    try:
        from enterprise.config import enabled
        return enabled()
    except Exception:
        return False


def _w32_guard(name, fn):
    if _w32_on():
        from enterprise import w32
        run_job(name, getattr(w32, fn))


def _w32_saas_tick():
    """Queued deliveries whose quiet hours ended; logged only when some are queued."""
    if not _w32_on():
        return
    try:
        from db.schema import get_connection
        c = get_connection()
        try:
            n = c.execute("SELECT COUNT(*) FROM enterprise_notification_delivery WHERE status='QUEUED'").fetchone()[0]
        finally:
            c.close()
        if n:
            _w32_guard("saas_tick", "saas_tick")
    except Exception as e:
        log.warning(f"  SaaS tick: {e}")


def _w32_alerts_tick():
    if is_market_hours():
        _w32_guard("alerts_intraday", "alerts_intraday")


def _broker_health_now():
    from execution.broker_health import run_scheduled
    return run_scheduled()


def _w29_paper_match_tick():
    """EX-02 / W4-R4: fill resting paper LIMIT / SL / SL-M orders that have crossed. Logged only
    when something is pending."""
    if not is_market_hours():
        return
    try:
        from db.schema import get_connection
        c = get_connection()
        try:
            n = c.execute("SELECT COUNT(*) FROM paper_order WHERE status='PENDING'").fetchone()[0]
        finally:
            c.close()
        if n:
            from execution.paper_matching import run_matching
            run_job("paper_matching", run_matching)
    except Exception as e:
        log.warning(f"  Paper matching: {e}")


def _w29_broker_health_tick():
    if is_market_hours():
        run_job("broker_health", _broker_health_now)


def _w29_pnl_tick():
    if is_market_hours():
        try:
            from portfolio.live_pnl import snapshot
            run_job("live_pnl_snapshot", snapshot)
        except Exception as e:
            log.warning(f"  Live P&L snapshot: {e}")


def _w29_audit_export():
    try:
        from enterprise.audit_export import run_scheduled
        run_job("audit_export", run_scheduled)
    except Exception as e:
        log.warning(f"  Audit export: {e}")


def _ops_webhook_tick():
    """Run the delivery job only when a delivery is due (no pipeline_log row every 5 minutes)."""
    try:
        from ops.webhooks import dispatch_due, due_count
        if due_count():
            run_job("ops_webhook_dispatch", dispatch_due)
    except Exception as e:
        log.warning(f"  webhook dispatch tick: {e}")


def start_scheduler():
    if not HAS_SCHEDULE:
        log.error("schedule not installed. Run: pip install schedule"); return

    log.info(f"\n{'='*65}")
    log.info("  ATIP MASTER SCHEDULER STARTED")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    log.info(f"{'='*65}")
    log.info("""
  DATA REFRESH SCHEDULE:
  ──────────────────────────────────────────────────────────
  07:00 AM   Pre-market  : Global markets, GIFT Nifty, News
  07:30 AM   Pre-market  : Dhan live quotes (pre-open)
  08:00 AM   Pre-market  : Portfolio sync (Dhan/Zerodha)
  09:00 AM   Pre-market  : Final pre-open snapshot
  09:15 AM   Intraday ↺  : Dhan live quotes START
  09:15–     Every 15min : Dhan quotes + NSE indexes + Global markets + VIX
  09:15–     Every 30min : 15-min OHLC bars
  12:00 PM   Midday      : News refresh
  12:30 PM   Midday      : ZPI buy-zone alert scan
  14:45 PM   Pre-close   : Power-hour snapshot
  15:30 PM   Market close: Final intraday snapshot
  16:45 PM   Post-market : Bhavcopy -> Dhan history -> indicators -> AI
                           scoring -> signal log (one sequential run; skipped,
                           with a warning, if the day has no EOD prices)
  18:30 PM   Catch-up    : re-runs post-market only if today has no scores
  23:00 PM   Overnight   : US close + Commodities + FX
  Saturday   Weekly      : Accuracy audit + Fundamentals
  ──────────────────────────────────────────────────────────
""")

    # ── Pre-market ──────────────────────────────────────────────────────
    schedule.every().day.at("07:00").do(run_premarket)

    # ── 8:30 AM — the Morning Brief ────────────────────────────────────
    # The architecture doc opens by promising an answer to "What should I do
    # today?" every morning at 8:30 IST. Scores are written post-market the
    # evening before and pre-market data lands at 07:00, so the answer exists
    # by now — this is what actually delivers it, 45 minutes before the open.
    schedule.every().day.at("08:30").do(run_morning_digest)

    # ── Intraday — every 15 min (9:15 AM to 3:30 PM) ───────────────────
    for hh in range(9, 16):
        for mm in [0, 15, 30, 45]:
            if hh == 9  and mm < 15: continue   # skip before market open
            if hh == 15 and mm > 30: continue   # skip after market close
            schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(run_intraday_15min)

    # ── Intraday — every 30 min ─────────────────────────────────────────
    for hh in range(10, 16):
        for mm in [0, 30]:
            schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(run_intraday_30min)

    # ── Midday ──────────────────────────────────────────────────────────
    schedule.every().day.at("12:00").do(run_midday_news)
    schedule.every().day.at("12:30").do(run_midday_zpi_scan)
    try:                                             # W28: extra scan times (12:30 / 14:45 above)
        from strategy.intraday_scan import settings as _scan_settings
        for _t in _scan_settings().get("times") or []:
            if _t not in ("12:30", "14:45"):
                schedule.every().day.at(_t).do(run_intraday_scan_job)
    except Exception as e:
        log.warning(f"  intraday scan schedule: {e}")
    schedule.every().day.at("14:45").do(run_preclose_scan)

    # ── Post-market ─────────────────────────────────────────────────────
    # After NSE publishes the Bhavcopy (~16:33), not before it — see
    # POSTMARKET_RUN_TIME. The catch-up re-runs only if the day has no scores.
    schedule.every().day.at(POSTMARKET_RUN_TIME).do(run_postmarket)
    schedule.every().day.at(POSTMARKET_CATCHUP_TIME).do(run_postmarket_if_missing)
    schedule.every().day.at(EOD_LATE_RUN_TIME).do(run_eod_late)
    t = datetime.strptime(FII_DII_WATCH_START, "%H:%M")
    while t <= datetime.strptime(FII_DII_WATCH_END, "%H:%M"):
        schedule.every().day.at(t.strftime("%H:%M")).do(run_fii_dii_watch)
        t += timedelta(minutes=FII_DII_WATCH_EVERY)

    # ── Overnight ───────────────────────────────────────────────────────
    schedule.every().day.at("23:00").do(run_overnight)

    # ── Weekly ──────────────────────────────────────────────────────────
    schedule.every().saturday.at("08:00").do(run_weekly)

    # ── Job health — every 30 min, off the other jobs' minutes ─────────
    for hh in range(7, 24):
        for mm in (10, 40):
            schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(run_health_check)

    # ── W8 operations: monitoring, verified backup, webhook delivery ───
    _schedule_ops_jobs()

    # ── W39b: stock SIP (paper); W40: the nightly factor risk model (after post-market) ───
    try:
        from pipeline import w39_jobs
        for line in w39_jobs.schedule_jobs(schedule, run_job):
            log.info(f"  W39b job: {line}")
    except Exception as e:
        log.warning(f"  W39b jobs not scheduled: {e}")

    # ── W40: meta-labelling of the technical signals: scoring 20:45 (after the 20:30 signals),
    # training Saturday 09:30 -- both no-ops unless config meta_label.enabled (ml/meta_label.py) ───
    try:
        from ml.meta_label import schedule_jobs as _meta_label_jobs
        for line in _meta_label_jobs(schedule, run_job):
            log.info(f"  W40 job: {line}")
    except Exception as e:
        log.warning(f"  W40 meta-label jobs not scheduled: {e}")

    # ── Morning catch-up — news and portfolio if the pre-market missed them
    for t in ("08:20", "12:20"):
        schedule.every().day.at(t).do(run_morning_catchup)

    # Market indicators whose slots passed while the PC was off (global markets,
    # indexes / VIX / GIFT Nifty, quotes, FII/DII, delivery, overnight) -- first,
    # because they are quick and the dashboard should be current while scoring catches up.
    try:
        run_startup_market_catchup()
    except Exception as e:
        log.warning(f"  Market-indicator catch-up failed: {e}")

    # A missed post-market (machine off or asleep at run time, or started late)
    # is recovered here rather than silently skipped until tomorrow.
    try:
        run_postmarket_if_missing()
    except Exception as e:
        log.warning(f"  Post-market catch-up failed: {e}")
    # The FII/DII settle only acts once post-market has held the session's signals, so it
    # is retried after the post-market catch-up -- started after the 21:30 watch window, it
    # would otherwise never run for the day.
    try:
        if is_market_day() and datetime.now().strftime("%H:%M") >= FII_DII_WATCH_START:
            run_fii_dii_watch()
    except Exception as e:
        log.warning(f"  FII/DII catch-up failed: {e}")
    try:
        run_morning_catchup()
    except Exception as e:
        log.warning(f"  Morning catch-up failed: {e}", exc_info=True)
    # Anything still missed or failing (earlier sessions included) is re-run now
    # rather than reported as MISSED until tomorrow's slot.
    run_health_check()

    log.info(f"  Waiting for next scheduled job... (Ctrl+C to stop)\n")

    from ops.jobs import beat, leader_lease
    # W9: leader election -- with several scheduler processes only the lease holder runs jobs
    while not leader_lease():
        log.warning("  another scheduler holds the leader lease — standing by (checking every 30 s)")
        beat("scheduler_standby", detail="waiting for the leader lease")
        time.sleep(30)
    last_lease = time.monotonic()
    slept_from = None
    while True:
        if time.monotonic() - last_lease >= 60:
            if not leader_lease():
                log.warning("  scheduler leader lease lost — standing by")
                while not leader_lease():
                    time.sleep(30)
                log.info("  scheduler leader lease re-acquired")
            last_lease = time.monotonic()
        schedule.run_pending()
        if slept_from:
            # After run_pending has fired the overdue slots once: whatever is still
            # missing gets the same catch-up + recovery a restart would.
            _on_wake(slept_from)
            slept_from = None
        beat("scheduler", detail=f"{len(schedule.get_jobs())} jobs")   # W8: throttled to one write / 60 s
        nxt = schedule.next_run()
        if nxt:
            rem = nxt - datetime.now()
            h, r = divmod(int(rem.total_seconds()), 3600)
            m, s = divmod(r, 60)
            print(f"\r  ⏳ [{now_ist()} IST]  Next: {nxt.strftime('%H:%M')}  "
                  f"({h:02d}h {m:02d}m {s:02d}s)  "
                  f"Market: {'OPEN 🟢' if is_market_hours() else 'CLOSED ⚫'}",
                  end="", flush=True)
        before_sleep = datetime.now()
        time.sleep(15)
        if datetime.now() - before_sleep > WAKE_GAP:        # the PC slept through this tick
            slept_from = before_sleep


WAKE_GAP = timedelta(minutes=10)


def _on_wake(slept_from):
    """The PC slept from `slept_from` until now. `schedule` fires each overdue slot once
    on wake, but jobs whose own guards skip late runs (morning brief, pre-market) and
    anything that failed meanwhile stay missed -- 2026-10-01 had the PC asleep 01:33-09:03.
    Run the start-up market catch-up, the morning catch-up and recovery."""
    log.warning(f"\n  ⏰ Woke after {datetime.now() - slept_from} asleep (since {slept_from:%H:%M}) — catching up")
    for fn in (run_startup_market_catchup, run_morning_catchup, run_postmarket_if_missing):
        try:
            fn()
        except Exception as e:
            log.warning(f"  Wake catch-up {fn.__name__}: {e}", exc_info=True)
    run_health_check()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",   action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--job",    choices=["premarket","postmarket","intraday","weekly","overnight"])
    args = ap.parse_args()

    if args.status:  print_status()
    elif args.once:  run_postmarket()
    elif args.job == "premarket":  run_premarket()
    elif args.job == "postmarket": run_postmarket()
    elif args.job == "intraday":   run_intraday_15min()
    elif args.job == "weekly":     run_weekly()
    elif args.job == "overnight":  run_overnight()
    else: start_scheduler()
