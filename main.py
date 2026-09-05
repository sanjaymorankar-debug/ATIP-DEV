"""
ATIP — AI Trading Intelligence Platform  v0.2
================================================
Run ALL commands from the project root — the folder containing this file
(e.g. D:\Projects\ATIP\).

Layout: every package sits directly under the project root, named for what
it does — data/ db/ scores/ orders/ dashboard/ pipeline/ portfolio/
alerts/ utils/. There is deliberately no nested package folder: an earlier
layout put the package at ATIP/atip/, and extracting an update one level
off produced ATIP/atip/atip/ where Python silently kept importing the old
copy. Runtime data (database, logs, config) lives in atip_data/ and is the
only folder to preserve across updates.

Usage:
    python main.py --init               # First run: create database
    python main.py --run postmarket     # Run post-market pipeline now
    python main.py --run premarket      # Run pre-market pipeline now
    python main.py --run intraday       # Run intraday index refresh now
    python main.py --run weekly         # Run weekly accuracy audit now
    python main.py --dashboard          # Dashboard only → http://localhost:8000
    python main.py --scheduler          # Scheduler only (no web server)
    python main.py                      # Start scheduler + dashboard (default)
    python main.py --status             # Show today's pipeline status
    python main.py --login-zerodha      # Authenticate with Zerodha Kite API
    python main.py --test-telegram      # Send test Telegram alert
"""

import sys
import os
import logging
import argparse
import threading
from pathlib import Path

# ── Project root on sys.path so the top-level packages (data, db, scores,
#    orders, dashboard, ...) import cleanly however this was invoked ──────
ROOT = Path(__file__).resolve().parent          # the folder holding this file
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Data directory ────────────────────────────────────────────────────────
DATA_DIR = ROOT / "atip_data"
DATA_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
LOG_FILE = DATA_DIR / "atip.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def print_banner():
    print("""
╔══════════════════════════════════════════════════════╗
║   ATIP — AI Trading Intelligence Platform  v0.2     ║
║   "Turning Market Data into Daily Decisions."        ║
║   NSE / BSE  ·  Python  ·  Personal Use             ║
╚══════════════════════════════════════════════════════╝
""")


def check_dependencies():
    """Print status of all optional packages."""
    # Only pandas and requests are truly required to start
    truly_required = [
        ("pandas",   "pip install pandas"),
        ("requests", "pip install requests"),
    ]
    # Everything else is optional — warn but don't block
    optional_pkgs = [
        ("yfinance",    "pip install yfinance          (live NSE/global market data)"),
        ("pandas_ta",   "pip install pandas-ta         (technical indicators)"),
        ("schedule",    "pip install schedule          (daily scheduler)"),
        ("fastapi",     "pip install fastapi uvicorn   (web dashboard)"),
        ("uvicorn",     "pip install fastapi uvicorn   (web dashboard)"),
        ("anthropic",   "pip install anthropic         (news AI sentiment)"),
        ("feedparser",  "pip install feedparser        (news RSS feeds)"),
        ("bs4",         "pip install beautifulsoup4    (fundamentals scraper)"),
        ("kiteconnect", "pip install kiteconnect       (Zerodha portfolio)"),
    ]

    missing_required = []
    missing_optional = []

    for mod, install in truly_required:
        try: __import__(mod)
        except ImportError: missing_required.append(f"  ❌ {mod:<20s} →  {install}")

    for mod, install in optional_pkgs:
        try: __import__(mod)
        except ImportError: missing_optional.append(f"  ⚠  {mod:<20s} →  {install}")

    if missing_required:
        print("Missing REQUIRED packages — install these first:")
        for m in missing_required: print(m)
        print()
        print("Run:  pip install pandas requests")
        print("Or :  pip install -r requirements.txt")
        print()
        sys.exit(1)

    if missing_optional:
        print("Some optional packages are not installed (features will be limited):")
        for m in missing_optional: print(m)
        print()
        print("  → To install everything:  pip install -r requirements.txt")
        print("  → Or run install.bat to install all at once")
        print()


def main():
    print_banner()
    check_dependencies()

    ap = argparse.ArgumentParser(
        description="ATIP — AI Trading Intelligence Platform",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--init",            action="store_true", help="Initialise database (run once on first setup)")
    ap.add_argument("--dashboard",       action="store_true", help="Start dashboard server only → http://localhost:8000")
    ap.add_argument("--scheduler",       action="store_true", help="Start scheduler only (no web server)")
    ap.add_argument("--run",             metavar="JOB",       help="Run one job: premarket | postmarket | intraday | weekly | overnight")
    ap.add_argument("--status",          action="store_true", help="Show today's pipeline status and exit")
    ap.add_argument("--login-zerodha",   action="store_true", help="Authenticate with Zerodha Kite API (run once per day)")
    ap.add_argument("--test-telegram",   action="store_true", help="Send a test Telegram alert")
    ap.add_argument("--live-feed",        action="store_true", help="Start Dhan WebSocket live tick feed (market hours only)")
    ap.add_argument("--dhan-securities",  action="store_true", help="Download Dhan security master list (run once after setup)")
    ap.add_argument("--dhan-history",     action="store_true", help="Download 600 days of historical data from Dhan")
    ap.add_argument("--dhan-quote",       nargs="+", metavar="SYM", help="Get live Dhan quotes e.g. --dhan-quote RELIANCE TCS")
    ap.add_argument("--purge-db",         action="store_true", help="Purge old data past retention window (600d core tables / 90d operational tables)")
    ap.add_argument("--purge-dry-run",    action="store_true", help="With --purge-db: report what would be deleted, without deleting")
    ap.add_argument("--port",            type=int, default=8000, help="Dashboard port (default: 8000)")
    args = ap.parse_args()

    # ── INIT ──────────────────────────────────────────────────────────────
    if args.init:
        log.info("Initialising ATIP database...")
        from db.schema import init_db, seed_weights
        db_path = init_db()
        seed_weights()
        print(f"\n✅  Database created  : {db_path}")
        print(f"✅  Weights seeded    : 68 configurations")
        print(f"\nNext steps:")
        print(f"  1. Edit  atip_data\\config.json     ← add your API keys")
        print(f"  2. python main.py --run postmarket  ← first data run")
        print(f"  3. python main.py                   ← start full system")
        return

    # ── STATUS ────────────────────────────────────────────────────────────
    if args.status:
        from pipeline.scheduler import print_status
        print_status()
        return

    # ── ZERODHA LOGIN ─────────────────────────────────────────────────────
    if args.login_zerodha:
        from portfolio.zerodha import login
        login()
        return

    # ── DHAN SECURITIES ──────────────────────────────────────────────────────
    if args.dhan_securities:
        from data.dhan import download_security_list
        df = download_security_list()
        print(f"\n✅  {len(df)} securities → atip_data/raw/dhan/security_id_list.csv")
        return

    # ── DHAN HISTORY ──────────────────────────────────────────────────────────
    if args.dhan_history:
        log.info("Downloading 600 days of historical data from Dhan...")
        from data.dhan import run_historical_pipeline, sync_index_benchmark_history
        r = run_historical_pipeline(days=600)
        rb = sync_index_benchmark_history(days=600)  # Nifty 50 history for beta_1y calc
        print(f"\n{'✅' if r['status']=='SUCCESS' else '❌'}  {r}")
        print(f"{'✅' if rb['status']=='SUCCESS' else '❌'}  benchmark: {rb}")
        return

    # ── DATABASE PURGE ────────────────────────────────────────────────────────
    if args.purge_db:
        from db.purge import purge_old_data
        dry = args.purge_dry_run
        res = purge_old_data(dry_run=dry)
        print(f"\n{'DRY RUN — nothing deleted, drop --purge-dry-run to actually purge' if dry else 'PURGE COMPLETE'}")
        for table, n in res.items():
            print(f"  {table:<22} {n}")
        return

    # ── DHAN LIVE QUOTE ───────────────────────────────────────────────────────
    if args.dhan_quote:
        from data.dhan import fetch_live_quotes
        df = fetch_live_quotes(args.dhan_quote)
        if not df.empty:
            print(f"\n{'Symbol':<15} {'LTP':>10} {'Chg%':>8} {'High':>10} {'Low':>10} {'Volume':>12}")
            print("-"*68)
            for _, r in df.iterrows():
                print(f"{str(r.get('symbol','')):<15} ₹{r.get('ltp',0):>9,.2f} {r.get('chg_pct',0):>7,.2f}% ₹{r.get('high',0):>9,.2f} ₹{r.get('low',0):>9,.2f} {int(r.get('volume',0)):>12,}")
        else:
            print("\u274c  No data. Check Dhan credentials in atip_data/config.json")
        return

    # ── DHAN LIVE FEED ────────────────────────────────────────────────────────
    if args.live_feed:
        log.info("Starting Dhan WebSocket live tick feed...")
        log.info("  Symbols  : Nifty High Beta 50 (default)")
        log.info("  Feed type: QUOTE (LTP + OHLC + Volume)")
        log.info("  Stored in: atip_data/atip.db → live_ticks table")
        log.info("  Press Ctrl+C to stop\n")
        from data.dhan import DhanLiveFeed, _default_symbols, QUOTE
        feed = DhanLiveFeed(symbols=_default_symbols(), feed_type=QUOTE, store_to_db=True)
        try:
            feed.start()
        except KeyboardInterrupt:
            feed.stop()
            print("\n\u23f9  Live feed stopped")
        return

    # ── TELEGRAM TEST ─────────────────────────────────────────────────────
    if args.test_telegram:
        from alerts.telegram import send_telegram, fmt
        ok = send_telegram(fmt("✅", "ATIP Test Alert",
                               "Telegram is working!\nATIP is connected and monitoring markets."))
        print("✅ Telegram alert sent!" if ok else "❌ Telegram not configured — check atip_data/config.json")
        return

    # ── RUN ONE JOB ───────────────────────────────────────────────────────
    if args.run:
        job = args.run.lower().strip()
        from pipeline.scheduler import (
            run_premarket, run_postmarket,
            run_intraday_15min, run_weekly, run_overnight
        )
        jobs = {
            "premarket":  run_premarket,
            "postmarket": run_postmarket,
            "intraday":   run_intraday_15min,
            "weekly":     run_weekly,
            "overnight":  run_overnight,
        }
        fn = jobs.get(job)
        if fn:
            log.info(f"Running job: {job} (forced — bypassing weekend/market-hours gate)")
            fn(force=True) if job in ("premarket", "postmarket", "intraday") else fn()
        else:
            print(f"❌ Unknown job '{job}'")
            print(f"   Valid options: {', '.join(jobs.keys())}")
        return

    # ── DASHBOARD ONLY ────────────────────────────────────────────────────
    if args.dashboard:
        log.info(f"Starting dashboard → http://localhost:{args.port}")
        _start_index_feed()
        _start_dashboard(args.port)
        return

    # ── SCHEDULER ONLY ────────────────────────────────────────────────────
    if args.scheduler:
        from pipeline.scheduler import start_scheduler
        start_scheduler()
        return

    # ── DEFAULT: SCHEDULER + DASHBOARD ───────────────────────────────────
    log.info("Starting ATIP — Scheduler + Dashboard")
    log.info(f"  Dashboard : http://localhost:{args.port}")
    log.info(f"  Logs      : {LOG_FILE}")
    log.info(f"  Database  : {DATA_DIR / 'db'}\n")

    dash = threading.Thread(target=_start_dashboard, args=(args.port,), daemon=True)
    dash.start()
    _start_index_feed()

    from pipeline.scheduler import start_scheduler
    start_scheduler()


def _start_index_feed():
    """
    Start the persistent Dhan WebSocket feed for real-time NSE index updates
    (data/dhan_ws.py). Non-fatal if it fails — e.g. no Dhan credentials
    configured yet, or dhanhq not installed — the dashboard still works off
    whatever the 15-min REST poll (pipeline.scheduler) last wrote to
    index_levels; it just won't be real-time until this is fixed.
    """
    try:
        from data.dhan_ws import start_index_feed
        start_index_feed()
    except Exception as e:
        log.warning(f"  Index WebSocket feed did not start ({e}) — "
                    f"falling back to 15-min REST index polling only")


def _start_dashboard(port: int = 8000):
    try:
        import uvicorn
        from dashboard.server import app
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except ImportError:
        log.error("FastAPI/uvicorn not installed.  Run:  pip install fastapi uvicorn")
    except Exception as e:
        log.error(f"Dashboard failed: {e}")


if __name__ == "__main__":
    main()
