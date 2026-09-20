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
FUNDAMENTALS_ENABLED = False
FUNDAMENTALS_HOLD_REASON = ("fundamental_score is weighted twice (SPI 0.15 + FS 0.10) and "
                            "defaults to 50.0 when nothing parses; see FUNDAMENTALS_ENABLED")


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

def run_job(name, fn, *args, **kwargs):
    """Run a pipeline job with error handling, logging, and Telegram alert on failure."""
    start = datetime.now()
    log.info(f"▶  [{now_ist()}]  {name}")
    try:
        result  = fn(*args, **kwargs)
        elapsed = (datetime.now() - start).seconds
        rows    = result.get("rows", 0) if isinstance(result, dict) else 0
        status  = result.get("status", "SUCCESS") if isinstance(result, dict) else "SUCCESS"
        log.info(f"✅  [{now_ist()}]  {name}  ({rows} rows, {elapsed}s, {status})")
        return result
    except Exception as e:
        elapsed = (datetime.now() - start).seconds
        log.error(f"❌  [{now_ist()}]  FAILED: {name}  ({elapsed}s)\n{traceback.format_exc()}")
        try:
            from alerts.telegram import send_failure_alert
            send_failure_alert(name, str(e))
        except Exception:
            pass
        return {"status": "FAILED", "error": str(e)}


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

    # 7:15 AM — Global markets (S&P, Dow, Nasdaq, Nikkei, Gold, Crude, USD/INR)
    from data.markets import fetch_global_markets
    run_job("global_premarket", fetch_global_markets, td, "premarket")

    # 7:30 AM — Dhan live quotes snapshot (pre-market)
    _run_dhan_quotes(td, label="premarket")

    # 7:45 AM — News digest (last 14 hours)
    from data.news import run_news_pipeline
    run_job("news_premarket", run_news_pipeline, 14)

    # 8:00 AM — Portfolio sync (Dhan + Zerodha)
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


def run_midday_zpi_scan():
    """12:30 PM — ZPI buy-zone alert scan on live prices."""
    if not is_market_day(): return
    try:
        from alerts.telegram import check_zpi_alerts
        check_zpi_alerts(date.today())
    except Exception as e:
        log.warning(f"  ZPI scan: {e}")


def run_preclose_scan():
    """2:45 PM — power-hour momentum scan."""
    if not is_market_hours(): return
    log.info(f"  [{now_ist()}] Pre-close scan")
    _run_dhan_quotes(date.today(), "preclose")


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
    except Exception as e:
        log.warning(f"  Benchmark history: {e}")

    # ── Never score a day without that day's closing prices ──────────────
    # Scoring on older bars and labelling the result `td` is what silently broke
    # signals from 2026-09-10: indicators and scores for each day were really
    # the previous day's, and every signal was logged with a NULL entry price
    # (the join to prices_daily for td found nothing), which evaluate_outcomes
    # skips -- so no signal since could ever be tracked. It is also what scored
    # the Ganesh Chaturthi holiday as if it were a session. Skip instead, loudly;
    # run_postmarket_if_missing() retries at POSTMARKET_CATCHUP_TIME.
    ok, n_eod, n_uni = _eod_coverage(td)
    if not ok:
        log.warning(f"  ⏭  No EOD prices for {td} ({n_eod}/{n_uni} scored symbols have a bar) "
                    f"— NOT scoring. Either NSE hasn't published yet or it wasn't a "
                    f"trading session. Outcome tracking and the dashboard still update.")
        try:
            from scores.signal_log import evaluate_outcomes
            run_job("signal_outcomes", evaluate_outcomes)
        except Exception as e:
            log.warning(f"  Signal outcomes: {e}")
        run_job("dashboard_rebuild", _rebuild_dashboard, td)
        log.info("⏭  Post-market finished WITHOUT scoring (no EOD data)\n")
        return {"status": "SKIPPED_NO_EOD", "date": str(td), "bars": n_eod, "universe": n_uni}
    if n_uni:
        log.info(f"  ✓ EOD coverage for {td}: {n_eod}/{n_uni} scored symbols have today's bar")

    # 4:45 PM — Compute 35 technical indicators
    from data.technical import run_technical_pipeline
    run_job("technical_indicators", run_technical_pipeline, td)

    # 5:00 PM — Run all 9 AI scoring indexes
    # (this also writes today's predictions — entry/SL/targets/size — which is
    # what makes the accuracy tracker below able to measure anything at all)
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

    # 5:40 PM — Append today's signals to the immutable log, then re-evaluate
    # momentum outcomes for every open signal. Append-only: unlike ai_scores and
    # predictions, a re-run never overwrites what was previously said.
    try:
        from scores.signal_log import log_signals, evaluate_outcomes
        run_job("signal_log", log_signals, td)
        run_job("signal_outcomes", evaluate_outcomes)
    except Exception as e:
        log.warning(f"  Signal log: {e}")

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

    # Fundamental data refresh (top 100 ATIP stocks)
    if not fundamentals_enabled():
        log.info(f"  ⏸  Fundamentals refresh held off — {FUNDAMENTALS_HOLD_REASON}")
    else:
        try:
            from db.schema import get_connection
            from data.fundamentals import run_fundamentals_pipeline
            conn = get_connection()
            # The last session, not "yesterday": on a Saturday run yesterday is
            # a Friday only by luck, and after a Monday holiday it is a day with
            # no scores at all, which silently selected no symbols.
            prev = str(last_trading_day(date.today()))
            rows = conn.execute(
                "SELECT symbol FROM ai_scores WHERE date=? ORDER BY atip_score DESC LIMIT 100",
                (prev,)
            ).fetchall()
            conn.close()
            top_syms = [r["symbol"] for r in rows]
            if top_syms:
                run_job("fundamentals_weekly", run_fundamentals_pipeline, top_syms)
            else:
                log.warning(f"  Weekly fundamentals: no scored symbols for {prev} — skipped")
        except Exception as e:
            log.warning(f"  Weekly fundamentals: {e}")

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
            today_n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE date=? OR DATE(created_at)=?",
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
    schedule.every().day.at("14:45").do(run_preclose_scan)

    # ── Post-market ─────────────────────────────────────────────────────
    # After NSE publishes the Bhavcopy (~16:33), not before it — see
    # POSTMARKET_RUN_TIME. The catch-up re-runs only if the day has no scores.
    schedule.every().day.at(POSTMARKET_RUN_TIME).do(run_postmarket)
    schedule.every().day.at(POSTMARKET_CATCHUP_TIME).do(run_postmarket_if_missing)

    # ── Overnight ───────────────────────────────────────────────────────
    schedule.every().day.at("23:00").do(run_overnight)

    # ── Weekly ──────────────────────────────────────────────────────────
    schedule.every().saturday.at("08:00").do(run_weekly)

    # A missed post-market (machine off or asleep at run time, or started late)
    # is recovered here rather than silently skipped until tomorrow.
    try:
        run_postmarket_if_missing()
    except Exception as e:
        log.warning(f"  Post-market catch-up failed: {e}")

    log.info(f"  Waiting for next scheduled job... (Ctrl+C to stop)\n")

    while True:
        schedule.run_pending()
        nxt = schedule.next_run()
        if nxt:
            rem = nxt - datetime.now()
            h, r = divmod(int(rem.total_seconds()), 3600)
            m, s = divmod(r, 60)
            print(f"\r  ⏳ [{now_ist()} IST]  Next: {nxt.strftime('%H:%M')}  "
                  f"({h:02d}h {m:02d}m {s:02d}s)  "
                  f"Market: {'OPEN 🟢' if is_market_hours() else 'CLOSED ⚫'}",
                  end="", flush=True)
        time.sleep(15)


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
