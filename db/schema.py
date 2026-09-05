"""ATIP — Database Schema"""
import sqlite3, logging
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path("atip_data/atip.db")
log = logging.getLogger(__name__)


# ── sqlite3 DATE/TIMESTAMP converters ──────────────────────────────────────
# get_connection() uses detect_types=PARSE_DECLTYPES, so sqlite3 converts any
# column declared DATE or TIMESTAMP on the way out. Python 3.12 deprecated the
# *default* converters for those two types, which made every such query emit
#   DeprecationWarning: The default date/timestamp converter is deprecated
# — thousands of lines per scoring run, drowning the actual output.
#
# Registering our own converters is the documented replacement. These
# deliberately reproduce the old behaviour (DATE -> datetime.date,
# TIMESTAMP -> datetime.datetime) so nothing downstream changes, and they fall
# back to the raw string rather than raising if a stored value isn't ISO —
# a malformed timestamp should not take down a query.
def _conv_date(raw):
    s = raw.decode() if isinstance(raw, bytes) else raw
    try:
        return datetime.fromisoformat(s).date() if len(s) > 10 else date.fromisoformat(s)
    except (ValueError, TypeError):
        return s

def _conv_timestamp(raw):
    s = raw.decode() if isinstance(raw, bytes) else raw
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return s

for _decl in ("date", "DATE"):
    sqlite3.register_converter(_decl, _conv_date)
for _decl in ("timestamp", "TIMESTAMP", "datetime", "DATETIME"):
    sqlite3.register_converter(_decl, _conv_timestamp)

def get_connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_index_levels_chg_columns(conn)
    _migrate_ai_scores_beta_column(conn)
    return conn

def _migrate_ai_scores_beta_column(conn):
    """Self-healing, non-destructive migration — adds ai_scores.beta_1y
    (1-year beta vs Nifty 50) for databases created before this column
    existed. Never drops or rewrites existing rows."""
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(ai_scores)").fetchall()}
    except sqlite3.OperationalError:
        return  # table doesn't exist yet — init_db() will create it with the column
    if "beta_1y" not in existing:
        try:
            conn.execute("ALTER TABLE ai_scores ADD COLUMN beta_1y REAL")
            conn.commit()
            log.info("  ✓ ai_scores migrated — added column: beta_1y")
        except sqlite3.OperationalError as e:
            log.warning(f"  ai_scores migration skipped beta_1y: {e}")

def _migrate_index_levels_chg_columns(conn):
    """Self-healing, non-destructive migration.

    markets.py computes a `<col>_chg` value for every entry in NSE_INDEXES
    (14 indexes), but index_levels originally only declared _chg columns
    for 4 of them. That mismatch is what threw:
        DB: table index_levels has no column named nifty_it_chg
    This adds the missing columns via ALTER TABLE ADD COLUMN only — it
    never drops or rewrites existing rows, and is a cheap no-op once the
    columns already exist.
    """
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(index_levels)").fetchall()}
    except sqlite3.OperationalError:
        return  # table doesn't exist yet (fresh install) — init_db() will create it with all columns
    needed = ["nifty_it_chg", "nifty_auto_chg", "nifty_fmcg_chg", "nifty_metal_chg",
              "nifty_realty_chg", "nifty_psubank_chg", "nifty_energy_chg", "nifty_pharma_chg",
              "india_vix_chg", "gift_nifty_chg"]
    added = []
    for col in needed:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE index_levels ADD COLUMN {col} REAL")
                added.append(col)
            except sqlite3.OperationalError as e:
                log.warning(f"  index_levels migration skipped {col}: {e}")
    if added:
        conn.commit()
        log.info(f"  ✓ index_levels migrated — added columns: {', '.join(added)}")

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS prices_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        open REAL, high REAL, low REAL, close REAL, adj_close REAL,
        volume INTEGER, delivery_qty INTEGER, delivery_pct REAL,
        series TEXT DEFAULT 'EQ', source TEXT DEFAULT 'bhavcopy',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices_daily(symbol,date)")
    c.execute("""CREATE TABLE IF NOT EXISTS technical_indicators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        rsi_14 REAL, stoch_k REAL, stoch_d REAL, williams_r REAL, cci_20 REAL,
        macd_line REAL, macd_signal REAL, macd_hist REAL, adx_14 REAL,
        ema_9 REAL, ema_21 REAL, ema_50 REAL, sma_200 REAL,
        atr_14 REAL, atr_pct REAL, bb_upper REAL, bb_lower REAL, bb_mid REAL, bb_width REAL,
        obv REAL, volume_sma20 REAL, volume_ratio REAL, rel_volume REAL,
        pivot REAL, r1 REAL, r2 REAL, s1 REAL, s2 REAL,
        fib_236 REAL, fib_382 REAL, fib_500 REAL, fib_618 REAL,
        golden_cross INTEGER DEFAULT 0, death_cross INTEGER DEFAULT 0,
        above_200dma INTEGER DEFAULT 0, gap_pct REAL, tech_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tech_symbol_date ON technical_indicators(symbol,date)")
    c.execute("""CREATE TABLE IF NOT EXISTS fundamental_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, quarter TEXT NOT NULL,
        report_date DATE, roe REAL, roce REAL, net_margin REAL, operating_margin REAL, roa REAL,
        eps_ttm REAL, eps_growth_yoy REAL, revenue_cr REAL, revenue_growth_yoy REAL,
        profit_cr REAL, profit_growth_yoy REAL, qoq_revenue_chg REAL, qoq_profit_chg REAL,
        debt_equity REAL, current_ratio REAL, interest_coverage REAL, fcf_cr REAL, cash_cr REAL,
        pe_ratio REAL, pb_ratio REAL, ev_ebitda REAL, peg_ratio REAL, dividend_yield REAL,
        book_value_ps REAL, promoter_hold REAL, promoter_pledge REAL, inst_hold REAL,
        fundamental_score REAL, source TEXT DEFAULT 'alpha_vantage',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,quarter))""")
    c.execute("""CREATE TABLE IF NOT EXISTS institutional_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        fii_net_cr REAL, dii_net_cr REAL, mf_net_cr REAL,
        promoter_buy INTEGER DEFAULT 0, promoter_sell INTEGER DEFAULT 0,
        delivery_pct REAL, inst_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fii_dii_market (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL UNIQUE,
        fii_buy_cr REAL, fii_sell_cr REAL, fii_net_cr REAL,
        dii_buy_cr REAL, dii_sell_cr REAL, dii_net_cr REAL,
        fii_5d_avg REAL, dii_5d_avg REAL, pcr REAL, mwpl_pct REAL, adv_decline REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS news_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TIMESTAMP NOT NULL,
        headline TEXT NOT NULL, source TEXT, url TEXT, category TEXT,
        symbols_mentioned TEXT, sentiment REAL, importance TEXT,
        confidence REAL, news_score REAL, ai_summary TEXT, processed INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_news_date ON news_articles(fetched_at)")
    c.execute("""CREATE TABLE IF NOT EXISTS ai_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        vpi REAL, spi REAL, rri REAL, mri REAL, cri REAL, msi REAL, zpi REAL, acs REAL,
        tech_score REAL, fund_score REAL, inst_score REAL, news_score REAL,
        atip_score REAL, atip_rank INTEGER, signal TEXT, confidence REAL,
        beta_1y REAL,
        tod_score REAL, is_tod INTEGER DEFAULT 0,
        mh_score REAL, regime TEXT,
        top_factor_1 TEXT, top_factor_2 TEXT, top_factor_3 TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scores_date_atip ON ai_scores(date,atip_score DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS market_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL UNIQUE,
        mh_score REAL, regime TEXT, nifty_trend REAL, banknifty REAL, breadth REAL,
        vix_score REAL, fii_score REAL, dii_score REAL, global_score REAL,
        sector_score REAL, adv_decline REAL, nifty_close REAL, vix_level REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS index_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, time TEXT NOT NULL,
        nifty50 REAL, nifty50_chg REAL, banknifty REAL, banknifty_chg REAL,
        midcap150 REAL, midcap150_chg REAL, smallcap250 REAL, smallcap250_chg REAL,
        nifty_it REAL, nifty_it_chg REAL, nifty_auto REAL, nifty_auto_chg REAL,
        nifty_fmcg REAL, nifty_fmcg_chg REAL, nifty_metal REAL, nifty_metal_chg REAL,
        nifty_realty REAL, nifty_realty_chg REAL, nifty_psubank REAL, nifty_psubank_chg REAL,
        nifty_energy REAL, nifty_energy_chg REAL, nifty_pharma REAL, nifty_pharma_chg REAL,
        india_vix REAL, india_vix_chg REAL, gift_nifty REAL, gift_nifty_chg REAL, overall_sentiment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_index_datetime ON index_levels(date,time)")
    c.execute("""CREATE TABLE IF NOT EXISTS global_markets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, time TEXT DEFAULT 'overnight',
        sp500 REAL, sp500_chg REAL, dow REAL, dow_chg REAL, nasdaq REAL, nasdaq_chg REAL,
        nikkei REAL, nikkei_chg REAL, hangseng REAL, hangseng_chg REAL,
        ftse100 REAL, ftse100_chg REAL, dax REAL, dax_chg REAL,
        crude_wti REAL, crude_wti_chg REAL, crude_brent REAL, crude_brent_chg REAL,
        gold REAL, gold_chg REAL, silver REAL, silver_chg REAL,
        usd_inr REAL, usd_inr_chg REAL, usd_index REAL, usd_index_chg REAL,
        us_10y REAL, us_10y_chg REAL,
        global_score REAL, us_score REAL, asia_score REAL, commodity_score REAL,
        global_sentiment TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(date,time))""")
    c.execute("""CREATE TABLE IF NOT EXISTS portfolio_holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, symbol TEXT NOT NULL,
        qty INTEGER, avg_price REAL, cmp REAL, current_val REAL, pnl REAL, pnl_pct REAL,
        atip_score REAL, vpi REAL, cri REAL, zpi REAL, signal TEXT, weight_pct REAL,
        sector TEXT, beta_1y REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(symbol,date))""")
    c.execute("""CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pred_date DATE NOT NULL, symbol TEXT NOT NULL,
        signal TEXT, atip_score REAL, vpi REAL, zpi REAL, mri REAL, cri REAL, acs REAL,
        entry_price REAL, stop_loss REAL, target_1 REAL, target_2 REAL,
        risk_reward REAL, position_size_pct REAL, confidence REAL,
        reasoning TEXT, regime TEXT, is_tod INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(pred_date,symbol))""")
    c.execute("""CREATE TABLE IF NOT EXISTS accuracy_tracker (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pred_date DATE NOT NULL, symbol TEXT NOT NULL,
        signal TEXT, entry_price REAL,
        price_5d REAL, return_5d REAL, correct_5d INTEGER,
        price_10d REAL, return_10d REAL, correct_10d INTEGER,
        price_20d REAL, return_20d REAL, correct_20d INTEGER,
        hit_target_1 INTEGER DEFAULT 0, hit_stop_loss INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(pred_date,symbol))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pipeline_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_date DATE, job_name TEXT NOT NULL,
        start_time TIMESTAMP, end_time TIMESTAMP, status TEXT,
        rows_processed INTEGER DEFAULT 0, error_msg TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS weight_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT, index_name TEXT NOT NULL,
        variable TEXT NOT NULL, weight REAL NOT NULL, description TEXT,
        regime TEXT DEFAULT 'ALL', active INTEGER DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(index_name,variable,regime))""")
    c.execute("""CREATE TABLE IF NOT EXISTS bulk_deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        net_value_cr REAL, deal_count INTEGER DEFAULT 0, source TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bulk_symbol_date ON bulk_deals(symbol,date)")
    c.execute("""CREATE TABLE IF NOT EXISTS live_quotes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT    NOT NULL,
        ltp        REAL,
        open       REAL,
        high       REAL,
        low        REAL,
        prev_close REAL,
        volume     INTEGER,
        chg_pct    REAL,
        timestamp  TEXT,
        source     TEXT DEFAULT 'dhan',
        UNIQUE(symbol, timestamp))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lq_symbol ON live_quotes(symbol, timestamp)")
    c.execute("""CREATE TABLE IF NOT EXISTS live_ticks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT,
        security_id TEXT,
        ltp         REAL,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        volume      INTEGER,
        timestamp   TEXT,
        received_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lt_symbol ON live_ticks(symbol, received_at)")
    conn.commit(); conn.close()
    log.info(f"✅ Database ready: {DB_PATH.resolve()}")
    return str(DB_PATH.resolve())

def seed_weights():
    weights = [
        ("VPI","V",0.18,"Volatility (ATR%)","ALL"),("VPI","TS",0.15,"Trend Strength (ADX)","ALL"),
        ("VPI","RS",0.12,"Relative Strength vs Nifty","ALL"),("VPI","LQ",0.10,"Liquidity","ALL"),
        ("VPI","VOL",0.10,"Volume Expansion","ALL"),("VPI","MR",0.10,"Mean Reversion","ALL"),
        ("VPI","FG",0.10,"Fundamental Growth","ALL"),("VPI","IS",0.08,"Institutional Strength","ALL"),
        ("VPI","NS",0.07,"News Sentiment","ALL"),
        ("SPI","ROE",0.20,"Return on Equity","ALL"),("SPI","ROCE",0.15,"Return on Capital","ALL"),
        ("SPI","EPS",0.15,"EPS Growth YoY","ALL"),("SPI","Revenue",0.10,"Revenue Growth","ALL"),
        ("SPI","FCF",0.10,"Free Cash Flow","ALL"),("SPI","Debt",0.10,"Debt/Equity inv","ALL"),
        ("SPI","PEG",0.10,"PEG Ratio inv","ALL"),("SPI","Quality",0.10,"Quality Composite","ALL"),
        ("RRI","Recovery",0.25,"Rebound from 52W low","ALL"),("RRI","Support",0.20,"Near support","ALL"),
        ("RRI","Volume",0.15,"Vol on up-days","ALL"),("RRI","RSIRecovery",0.15,"RSI crossed 30","ALL"),
        ("RRI","Institutional",0.15,"DII/MF buying","ALL"),("RRI","News",0.10,"Positive news","ALL"),
        ("MRI","MACD",0.25,"MACD cross zero","ALL"),("MRI","RSI",0.20,"RSI cross 40","ALL"),
        ("MRI","ADX",0.15,"ADX falling inv","ALL"),("MRI","Volume",0.15,"Vol surge","ALL"),
        ("MRI","EMA",0.15,"9-EMA cross 21","ALL"),("MRI","News",0.10,"Catalyst news","ALL"),
        ("CRI","Volatility",0.20,"High ATR near 52W high","ALL"),("CRI","Debt",0.20,"High D/E","ALL"),
        ("CRI","Distribution",0.15,"Vol on down-days","ALL"),("CRI","WeakTrend",0.15,"Below DMAs","ALL"),
        ("CRI","NegativeNews",0.15,"Negative news","ALL"),("CRI","MarketWeakness",0.15,"Sector+VIX","ALL"),
        ("MSI","News",0.30,"Aggregate news","ALL"),("MSI","FII",0.20,"FII 5-day trend","ALL"),
        ("MSI","DII",0.15,"DII activity","ALL"),("MSI","Sector",0.10,"% sectors green","ALL"),
        ("MSI","Options",0.10,"PCR inverted","ALL"),("MSI","Global",0.10,"Global sentiment","ALL"),
        ("MSI","VIX",0.05,"VIX inverted","ALL"),
        ("ZPI","Support",0.15,"Near key support","ALL"),("ZPI","Resistance",0.15,"Room to run","ALL"),
        ("ZPI","RSI",0.10,"RSI 30-50 zone","ALL"),("ZPI","ATR",0.10,"R:R >= 1:3","ALL"),
        ("ZPI","Volume",0.10,"Low vol pullback","ALL"),("ZPI","Trend",0.10,"Above 200-DMA","ALL"),
        ("ZPI","Institutional",0.10,"Accumulation 10d","ALL"),("ZPI","News",0.10,"No negative news","ALL"),
        ("ZPI","Sector",0.10,"Top-3 sector","ALL"),
        ("ACS","HistoricalAccuracy",0.25,"Past accuracy 90d","ALL"),
        ("ACS","Agreement",0.20,"% indexes agree","ALL"),("ACS","MarketRegime",0.20,"MH>60","ALL"),
        ("ACS","DataQuality",0.15,"Input completeness","ALL"),
        ("ACS","NewsConfidence",0.10,"AI news conf","ALL"),("ACS","Liquidity",0.10,"Turnover","ALL"),
        ("MH","NiftyTrend",0.20,"Nifty trend","ALL"),("MH","BankNifty",0.15,"BankNifty","ALL"),
        ("MH","Breadth",0.10,"% above 200DMA","ALL"),("MH","VIX",0.10,"VIX inv","ALL"),
        ("MH","FII",0.10,"FII net 5d","ALL"),("MH","DII",0.10,"DII net","ALL"),
        ("MH","Global",0.10,"Global score","ALL"),("MH","Sector",0.10,"Sector breadth","ALL"),
        ("MH","AdvanceDecline",0.05,"A/D ratio","ALL"),
        ("ATIP","VPI",0.20,"VPI","ALL"),("ATIP","SPI",0.15,"SPI","ALL"),
        ("ATIP","RRI",0.10,"RRI","ALL"),("ATIP","MRI",0.10,"MRI","ALL"),
        ("ATIP","MSI",0.10,"MSI","ALL"),("ATIP","ZPI",0.10,"ZPI","ALL"),
        ("ATIP","TS",0.10,"Tech Score","ALL"),("ATIP","FS",0.10,"Fund Score","ALL"),
        ("ATIP","INS",0.05,"Inst Score","ALL"),
        ("TOD","VPI",0.20,"VPI","ALL"),("TOD","ZPI",0.15,"ZPI","ALL"),
        ("TOD","MRI",0.15,"MRI","ALL"),("TOD","MSI",0.10,"MSI","ALL"),
        ("TOD","Volume",0.10,"Volume breakout","ALL"),("TOD","Breakout",0.10,"Price breakout","ALL"),
        ("TOD","Sector",0.10,"Sector strength","ALL"),("TOD","ACS",0.10,"ACS","ALL"),
        # INS -- Institutional Score. MutualFund/Insider from the original
        # doc formula (E17) are intentionally omitted: NSE does not publish
        # free per-stock mutual-fund-flow or insider-trade data, so there is
        # no real source to wire in for them (see compute_ins() docstring).
        # Weight is redistributed across the three sources that ARE real:
        # market-wide FII/DII 5-day net flow + promoter holding + bulk/block
        # deal net value for the stock.
        ("INS","FII",0.40,"Market FII 5-day net flow","ALL"),
        ("INS","DII",0.30,"Market DII 5-day net flow","ALL"),
        ("INS","Promoter",0.20,"Promoter shareholding %","ALL"),
        ("INS","BulkDeals",0.10,"Net bulk/block deal value (10d)","ALL"),
        # TS — Technical Score, exactly the architecture doc's formula. These
        # were hardcoded inside compute_tech_score() with only 8 of the 12
        # components and Trend at 0.15 instead of 0.08. VWAP is seeded for
        # completeness but never contributes yet (it needs intraday bars);
        # the score renormalises over whichever components are present.
        ("TS","RSI",0.10,"RSI zone","ALL"),("TS","MACD",0.10,"MACD histogram","ALL"),
        ("TS","ADX",0.10,"Trend strength","ALL"),("TS","ATR",0.08,"Volatility","ALL"),
        ("TS","EMA",0.08,"EMA alignment","ALL"),("TS","VWAP",0.08,"VWAP (needs intraday)","ALL"),
        ("TS","Bollinger",0.08,"Position in band","ALL"),("TS","Volume",0.08,"Volume ratio","ALL"),
        ("TS","Trend",0.08,"Above 200-DMA","ALL"),("TS","SR",0.08,"Support/Resistance crosses","ALL"),
        ("TS","Gap",0.07,"Gap analysis","ALL"),("TS","RelativeVolume",0.07,"Vs same-weekday avg","ALL"),
        # PHS — Portfolio Health Score, the doc's formula (section 11).
        ("PHS","Diversification",0.25,"Concentration across holdings","ALL"),
        ("PHS","Risk",0.20,"Portfolio beta + CRI exposure","ALL"),
        ("PHS","Drawdown",0.15,"Drawdown from peak","ALL"),
        ("PHS","Quality",0.15,"Mean ATIP score of holdings","ALL"),
        ("PHS","Allocation",0.15,"Position sizing vs regime","ALL"),
        ("PHS","Performance",0.10,"Unrealised P&L","ALL"),
    ]
    conn = get_connection()
    conn.executemany(
        "INSERT OR IGNORE INTO weight_config(index_name,variable,weight,description,regime) VALUES(?,?,?,?,?)",
        weights)
    conn.commit(); conn.close()
    log.info(f"✅ Seeded {len(weights)} weight configurations")

def log_job(job_name, status, rows=0, error=None, run_date=None):
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO pipeline_log(run_date,job_name,start_time,status,rows_processed,error_msg) VALUES(?,?,?,?,?,?)",
            (str(run_date or __import__('datetime').date.today()), job_name,
             datetime.now(), status, rows, str(error) if error else None))
        conn.commit(); conn.close()
    except Exception: pass

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(init_db()); seed_weights()
