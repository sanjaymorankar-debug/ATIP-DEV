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
from utils.trading_calendar import is_trading_day, postmarket_target_date

log = logging.getLogger(__name__)

try: import schedule; HAS_SCHEDULE = True
except ImportError: HAS_SCHEDULE = False; log.warning("pip install schedule")


# ── Helpers ───────────────────────────────────────────────────────────────

def now_ist(): return datetime.now().strftime("%H:%M:%S")

def is_market_day(): return is_trading_day(date.today())  # Mon–Fri, minus known NSE holidays

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

def run_postmarket(force=False):
    if not force and not is_market_day():
        log.info("⏩  Weekend — skipping post-market"); return

    # Which trading day's EOD data are we actually after right now?
    # - trading day, on/after 4:00 PM IST -> today
    # - trading day, before 4:00 PM IST   -> previous trading day (today's isn't out yet)
    # - non-trading day (forced run)      -> previous trading day
    td = postmarket_target_date()
    log.info(f"\n{'='*55}\n  POST-MARKET PIPELINE — target date {td}\n{'='*55}")

    # 3:35 PM — Final live snapshot from Dhan
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

    # 5:30 PM — Re-sync portfolio with fresh AI scores
    _run_portfolio_sync(td)

    # 5:45 PM — Send Telegram alerts
    try:
        from alerts.telegram import check_cri_alerts, check_fii_alert, send_tod_alert
        check_cri_alerts(td)
        check_fii_alert(td)
        send_tod_alert(td)
    except Exception as e:
        log.warning(f"  Post-market alerts: {e}")

    # 6:00 PM — Rebuild dashboard state
    run_job("dashboard_rebuild", _rebuild_dashboard, td)

    log.info("✅  Post-market pipeline complete\n")


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
    try:
        from db.schema import get_connection
        from data.fundamentals import run_fundamentals_pipeline
        conn = get_connection()
        yesterday = str(date.today() - timedelta(days=1))
        rows = conn.execute(
            "SELECT symbol FROM ai_scores WHERE date=? ORDER BY atip_score DESC LIMIT 100",
            (yesterday,)
        ).fetchall()
        conn.close()
        top_syms = [r["symbol"] for r in rows]
        if top_syms:
            run_job("fundamentals_weekly", run_fundamentals_pipeline, top_syms)
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
  16:05 PM   Post-market : NSE Bhavcopy + Dhan historical
  16:45 PM   Post-market : Technical indicators (35 indicators)
  17:00 PM   Post-market : AI scoring (9 indexes + ATIP rank)
  17:30 PM   Post-market : Portfolio re-sync with AI scores
  18:00 PM   Post-market : Dashboard rebuild
  23:00 PM   Overnight   : US close + Commodities + FX
  Saturday   Weekly      : Accuracy audit + Fundamentals
  ──────────────────────────────────────────────────────────
""")

    # ── Pre-market ──────────────────────────────────────────────────────
    schedule.every().day.at("07:00").do(run_premarket)

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
    schedule.every().day.at("16:05").do(run_postmarket)

    # ── Overnight ───────────────────────────────────────────────────────
    schedule.every().day.at("23:00").do(run_overnight)

    # ── Weekly ──────────────────────────────────────────────────────────
    schedule.every().saturday.at("08:00").do(run_weekly)

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
