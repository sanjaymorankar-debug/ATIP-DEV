"""
ATIP Diagnostic Tool
====================
Run this FIRST to find out exactly why the dashboard is empty.
Usage: python diagnose.py

It checks every layer: DB, data, scores, dashboard.
"""
import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

print("""
╔══════════════════════════════════════════════════════╗
║   ATIP Diagnostic Tool                               ║
║   Checking every layer — will tell you exactly       ║
║   what to run to get stocks showing in dashboard     ║
╚══════════════════════════════════════════════════════╝
""")

issues   = []
actions  = []
ok_count = 0

# ── 1. Check database exists ─────────────────────────────────────────────
print("[ 1 ] Checking database...")
db_path = ROOT / "atip_data" / "atip.db"
if not db_path.exists():
    print("  ❌  Database not found")
    issues.append("Database missing")
    actions.append("python main.py --init")
else:
    size = db_path.stat().st_size
    print(f"  ✅  Database found ({size/1024:.1f} KB)")
    ok_count += 1

# ── 2. Check config ───────────────────────────────────────────────────────
print("\n[ 2 ] Checking config.json...")
cfg_path = ROOT / "atip_data" / "config.json"
if not cfg_path.exists():
    print("  ❌  config.json not found")
    issues.append("config.json missing")
    actions.append("copy config_template.json atip_data\\config.json  then add your Dhan credentials")
else:
    import json
    try:
        cfg = json.loads(cfg_path.read_text())
        dhan_ok = (cfg.get("dhan_client_id","") not in ("","YOUR_DHAN_CLIENT_ID"))
        tg_ok   = (cfg.get("telegram_token","") not in ("","YOUR_TELEGRAM_BOT_TOKEN"))
        print(f"  {'✅' if dhan_ok else '❌'}  Dhan credentials  {'SET' if dhan_ok else 'NOT SET — add dhan_client_id + dhan_access_token'}")
        print(f"  {'✅' if tg_ok   else '⚠️ '}  Telegram          {'SET' if tg_ok else 'optional — not set'}")
        if not dhan_ok:
            issues.append("Dhan credentials missing")
            actions.append("Edit atip_data\\config.json → add dhan_client_id and dhan_access_token")
        ok_count += 1
    except Exception as e:
        print(f"  ❌  config.json parse error: {e}")

# ── 3. Check tables & row counts ─────────────────────────────────────────
print("\n[ 3 ] Checking database tables and row counts...")
if db_path.exists():
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    tables = [
        ("prices_daily",         "EOD price data",          "python main.py --run postmarket  OR  python main.py --dhan-history"),
        ("technical_indicators", "Technical indicators",     "python main.py --run postmarket"),
        ("ai_scores",            "AI scores (ATIP ranking)", "python main.py --run postmarket"),
        ("index_levels",         "NSE index levels",         "python main.py --run intraday"),
        ("global_markets",       "Global markets data",      "python main.py --run premarket"),
        ("news_articles",        "News articles",            "python main.py --run premarket"),
        ("market_health",        "Market Health score",      "python main.py --run postmarket"),
        ("live_quotes",          "Live quotes",              "python main.py --run intraday"),
        ("portfolio_holdings",   "Portfolio data",           "python main.py --login-zerodha  OR  configure Dhan"),
        ("fii_dii_market",       "FII/DII data",             "python main.py --run postmarket"),
    ]

    critical_empty = []
    for table, label, fix in tables:
        try:
            n     = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            today = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE date=DATE('now') OR DATE(created_at)=DATE('now')").fetchone()[0]
            icon  = "✅" if n > 0 else "❌"
            print(f"  {icon}  {label:<26} {n:>7,} rows total  {today:>6} today")
            if n == 0:
                critical_empty.append((label, fix))
        except Exception:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {'✅' if n>0 else '❌'}  {label:<26} {n:>7,} rows")
                if n == 0: critical_empty.append((label, fix))
            except Exception:
                print(f"  ⚠️   {label:<26} table not created yet")
                critical_empty.append((label, fix))

    conn.close()

    if critical_empty:
        for label, fix in critical_empty:
            issues.append(f"{label} is empty")
            if fix not in actions:
                actions.append(fix)
    else:
        ok_count += 1
        print("\n  ✅  All tables have data")

# ── 4. Check if ai_scores has TODAY's data ────────────────────────────────
print("\n[ 4 ] Checking if dashboard has TODAY's scores...")
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    from datetime import date
    today = str(date.today())
    n_today = conn.execute("SELECT COUNT(*) FROM ai_scores WHERE date=?", (today,)).fetchone()[0]
    n_total = conn.execute("SELECT COUNT(*) FROM ai_scores").fetchone()[0]
    n_any   = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
    conn.close()

    print(f"  Total AI scores in DB : {n_total:,}")
    print(f"  Latest score date     : {n_any or 'none'}")
    print(f"  Scores for TODAY ({today}): {n_today}")

    if n_total == 0:
        print("  ❌  No AI scores at all — need to run full pipeline")
        issues.append("No AI scores computed yet")
        actions.append("python main.py --run postmarket")
    elif n_today == 0:
        print(f"  ⚠️   No scores for today — dashboard shows last date: {n_any}")
        print(f"       Dashboard will show {n_any} data (this is normal on weekends/holidays)")
        issues.append(f"No scores for today — last available: {n_any}")
        actions.append("python main.py --run postmarket  (will use latest available data)")
    else:
        ok_count += 1
        print(f"  ✅  {n_today} stocks scored today")

# ── 5. Check prices_daily has data ───────────────────────────────────────
print("\n[ 5 ] Checking price data...")
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    n_prices = conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0]
    n_syms   = conn.execute("SELECT COUNT(DISTINCT symbol) FROM prices_daily").fetchone()[0]
    latest   = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()[0]

    if n_prices == 0:
        print("  ❌  No price data in DB")
        print("      → You need to download data first")
        issues.append("No price data downloaded")
        if "python main.py --dhan-history" not in actions:
            actions.append("python main.py --dhan-history")
    else:
        print(f"  ✅  {n_prices:,} price records  |  {n_syms} symbols  |  Latest: {latest}")
        ok_count += 1
    conn.close()

# ── 6. Check Dhan security list ───────────────────────────────────────────
print("\n[ 6 ] Checking Dhan security list...")
sec_path = ROOT / "atip_data" / "raw" / "dhan" / "security_id_list.csv"
if not sec_path.exists():
    print("  ❌  Dhan security list not downloaded")
    issues.append("Dhan security list missing")
    if "python main.py --dhan-securities" not in actions:
        actions.append("python main.py --dhan-securities")
else:
    try:
        import pandas as pd
        df = pd.read_csv(str(sec_path))
        print(f"  ✅  {len(df):,} securities in Dhan master list")
        ok_count += 1
    except Exception as e:
        print(f"  ⚠️   Could not read security list: {e}")

# ── 7. Check packages ──────────────────────────────────────────────────────
print("\n[ 7 ] Checking required packages...")
pkg_checks = [
    ("pandas",    True),
    ("yfinance",  True),
    ("dhanhq",    True),
    ("fastapi",   True),
    ("uvicorn",   True),
    ("schedule",  False),
    ("anthropic", False),
    ("feedparser",False),
    ("pandas_ta", False),
]
missing_required = []
for pkg, required in pkg_checks:
    try:
        __import__(pkg)
        print(f"  ✅  {pkg}")
    except ImportError:
        print(f"  {'❌' if required else '⚠️ '}  {pkg} {'(REQUIRED)' if required else '(optional)'}")
        if required:
            missing_required.append(pkg)

if missing_required:
    issues.append(f"Missing packages: {missing_required}")
    actions.append(f"pip install {' '.join(missing_required)}")

# ── SUMMARY ───────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  DIAGNOSIS COMPLETE")
print(f"{'='*60}")

if not issues:
    print("""
  ✅  Everything looks good!
  
  If dashboard still shows empty stocks:
  → Open http://localhost:8000 in your browser
  → Wait 30 seconds and refresh
  → Check atip_data/atip.log for errors
""")
else:
    print(f"\n  Found {len(issues)} issue(s):\n")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. ❌  {issue}")

    print(f"\n  {'='*56}")
    print(f"  RUN THESE COMMANDS IN ORDER TO FIX:")
    print(f"  {'='*56}\n")
    seen = []
    step = 1
    for action in actions:
        if action not in seen:
            print(f"  Step {step}: {action}")
            seen.append(action)
            step += 1
    print()

# ── QUICK FIX SCRIPT ──────────────────────────────────────────────────────
print("="*60)
print("  FASTEST WAY TO GET STOCKS IN DASHBOARD:")
print("="*60)
print("""
  Option A — Use yfinance (works without Dhan, downloads fast):
  
    python quickstart.py
  
  Option B — Use Dhan API (real-time, needs credentials):
  
    python main.py --dhan-securities
    python main.py --dhan-history
    python main.py --run postmarket
    python main.py --dashboard

  Either way, once data is loaded:
    python main.py --dashboard
    Open: http://localhost:8000
""")
