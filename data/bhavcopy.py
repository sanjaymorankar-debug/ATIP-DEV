"""ATIP — NSE Bhavcopy Downloader (Phase 2A)"""
import io, re, time, zipfile, logging, argparse
import requests, pandas as pd
from datetime import date, datetime, timedelta
from pathlib import Path
from db.schema import get_connection, log_job
from utils.trading_calendar import is_trading_day, last_trading_day, postmarket_target_date

log = logging.getLogger(__name__)
RAW_DIR = Path("atip_data/raw"); RAW_DIR.mkdir(parents=True, exist_ok=True)
NSE_HOME     = "https://www.nseindia.com"
BHAV_CM_URL  = "https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
FII_DII_URL  = "https://www.nseindia.com/api/fiidiiTradeReact"
BULK_DEALS_URL  = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_DEALS_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"
HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0","Accept":"*/*","Referer":"https://www.nseindia.com/"}

def get_nse_session():
    s = requests.Session(); s.headers.update(HEADERS)
    try: s.get(NSE_HOME, timeout=12); time.sleep(1.2)
    except Exception as e: log.warning(f"NSE session: {e}")
    return s

def download_bhavcopy_cm(trade_date, session):
    ds    = trade_date.strftime("%Y%m%d")
    url   = BHAV_CM_URL.format(date=ds)
    cache = RAW_DIR / f"bhavcopy_cm_{ds}.csv"
    if cache.exists(): return pd.read_csv(str(cache))
    log.info(f"  Downloading CM Bhavcopy {trade_date}...")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30); r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                csv_name = [n for n in z.namelist() if n.endswith(".csv")][0]
                df = pd.read_csv(z.open(csv_name))
            df.to_csv(str(cache), index=False)
            log.info(f"  ✓ CM Bhavcopy: {len(df)} rows")
            return df
        except Exception as e:
            log.warning(f"  Attempt {attempt+1}/3: {e}"); time.sleep(5*(attempt+1))
    raise RuntimeError(f"Failed to download Bhavcopy for {trade_date}")

def parse_bhavcopy_cm(df, trade_date):
    col_map = {"TckrSymb":"symbol","OpnPric":"open","HghPric":"high","LwPric":"low",
               "ClsPric":"close","TtlTradgVol":"volume","SctySrs":"series",
               "DelivQty":"delivery_qty","DelivPer":"delivery_pct"}
    if "TckrSymb" in df.columns: df = df.rename(columns=col_map)
    elif "SYMBOL" in df.columns:
        df = df.rename(columns={"SYMBOL":"symbol","OPEN":"open","HIGH":"high","LOW":"low",
                                 "CLOSE":"close","TOTTRDQTY":"volume","SERIES":"series",
                                 "DELIV_QTY":"delivery_qty","DELIV_PER":"delivery_pct"})
    df = df[df.get("series", pd.Series(["EQ"]*len(df))).isin(["EQ","BE","SM"])].copy()
    df["date"] = trade_date; df["source"] = "bhavcopy"
    for col in ["open","high","low","close","volume","delivery_qty","delivery_pct"]:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors="coerce")
    # delivery_qty/delivery_pct are only present in NSE's combined bhavcopy
    # variant (sec_bhavdata_full / DelivQty+DelivPer columns) -- the plain
    # CM bhavcopy this pipeline downloads by default doesn't carry them, so
    # both columns are frequently absent. Add them as all-NaN rather than
    # skip, so downstream INSERTs always have a stable column set.
    if "delivery_qty" not in df.columns: df["delivery_qty"] = pd.NA
    if "delivery_pct" not in df.columns:
        df["delivery_pct"] = (df["delivery_qty"] / df["volume"] * 100).where(df["volume"] > 0)
    return df[["symbol","date","open","high","low","close","volume","delivery_qty","delivery_pct","series","source"]]


def download_bulk_block_deals(trade_date, session):
    """
    NSE's daily bulk & block deal CSVs -- large negotiated trades, usually
    institutional. This is a real per-stock institutional signal, feeding
    compute_ins()'s BulkDeals component (previously the ATIP Master's INS
    score was always None because nothing computed it at all).

    Best-effort only: NSE publishes each file for that trading day; older
    dates commonly 404 and empty/odd responses are common on holidays.
    Never raises -- returns {} per side that fails so the rest of the
    pipeline (and compute_ins()) keeps working with whatever loaded.
    """
    out = {}
    for label, url in (("bulk", BULK_DEALS_URL), ("block", BLOCK_DEALS_URL)):
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip() for c in df.columns]
            sym_col = next((c for c in df.columns if c.upper() == "SYMBOL"), None)
            qty_col = next((c for c in df.columns if "QTY" in c.upper() or "QUANTITY" in c.upper()), None)
            price_col = next((c for c in df.columns if "PRICE" in c.upper() or "RATE" in c.upper()), None)
            side_col = next((c for c in df.columns if "BUY" in c.upper() or "SELL" in c.upper()), None)
            if not (sym_col and qty_col and price_col):
                log.warning(f"  {label} deals: unexpected columns {list(df.columns)[:8]} — skipping")
                continue
            for _, row in df.iterrows():
                sym = str(row[sym_col]).strip().upper()
                if not sym: continue
                try:
                    qty = float(str(row[qty_col]).replace(",", ""))
                    price = float(str(row[price_col]).replace(",", ""))
                except (TypeError, ValueError):
                    continue
                value_cr = qty * price / 1e7
                side = str(row.get(side_col, "")).strip().upper() if side_col else ""
                signed = value_cr if side.startswith("B") else -value_cr if side.startswith("S") else 0.0
                d = out.setdefault(sym, {"net_value_cr": 0.0, "deals": 0, "source": label})
                d["net_value_cr"] += signed; d["deals"] += 1
        except Exception as e:
            log.warning(f"  {label} deals unavailable: {e}")
    if out:
        log.info(f"  ✓ Bulk/Block deals: {len(out)} symbols")
    return out


def run_bulk_deals_pipeline(trade_date=None):
    if trade_date is None:
        trade_date = postmarket_target_date()
    elif not is_trading_day(trade_date):
        trade_date = last_trading_day(trade_date)
    conn = get_connection()
    result = {"date": str(trade_date), "rows": 0, "status": "SUCCESS"}
    try:
        session = get_nse_session()
        deals = download_bulk_block_deals(trade_date, session)
        for sym, d in deals.items():
            conn.execute("""INSERT INTO bulk_deals(symbol,date,net_value_cr,deal_count,source)
                VALUES(?,?,?,?,?)
                ON CONFLICT(symbol,date) DO UPDATE SET
                net_value_cr=excluded.net_value_cr, deal_count=excluded.deal_count""",
                (sym, str(trade_date), round(d["net_value_cr"], 2), d["deals"], d["source"]))
        conn.commit(); result["rows"] = len(deals)
        log_job("bulk_deals", "SUCCESS", len(deals), run_date=trade_date)
    except Exception as e:
        conn.rollback(); result["status"] = "FAILED"; log.error(f"  ✗ {e}")
        log_job("bulk_deals", "FAILED", 0, error=e, run_date=trade_date)
    finally: conn.close()
    return result

def download_fii_dii(trade_date, session):
    cache = RAW_DIR / f"fii_dii_{trade_date.strftime('%Y%m%d')}.json"
    if cache.exists():
        import json; return json.loads(cache.read_text())
    try:
        r = session.get(FII_DII_URL, timeout=15, headers={**HEADERS,"Accept":"application/json"})
        r.raise_for_status(); data = r.json()
        result = {"date":str(trade_date),"fii_buy_cr":0,"fii_sell_cr":0,"dii_buy_cr":0,"dii_sell_cr":0}
        for row in data:
            cat = str(row.get("category","")).upper()
            buy = float(str(row.get("buyValue","0")).replace(",","") or 0)
            sell= float(str(row.get("sellValue","0")).replace(",","") or 0)
            if "FII" in cat or "FPI" in cat:
                result["fii_buy_cr"] += buy; result["fii_sell_cr"] += sell
            elif "DII" in cat:
                result["dii_buy_cr"] += buy; result["dii_sell_cr"] += sell
        result["fii_net_cr"] = result["fii_buy_cr"] - result["fii_sell_cr"]
        result["dii_net_cr"] = result["dii_buy_cr"] - result["dii_sell_cr"]
        import json; cache.write_text(json.dumps(result))
        log.info(f"  ✓ FII: ₹{result['fii_net_cr']:.0f}Cr  DII: ₹{result['dii_net_cr']:.0f}Cr")
        return result
    except Exception as e:
        log.warning(f"  FII/DII unavailable: {e}"); return {}

def _bhavcopy_already_stored(conn, trade_date) -> int:
    """Row count already in prices_daily for this date, sourced from bhavcopy."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM prices_daily WHERE date=? AND source='bhavcopy'",
        (str(trade_date),)
    ).fetchone()
    return row["c"] if row else 0

def run_bhavcopy_pipeline(trade_date=None):
    if trade_date is None:
        # No explicit date given -> resolve which trading day's EOD data we
        # should actually be after right now: before 4 PM IST (or on a
        # non-trading day) that's the previous trading day, since today's
        # Bhavcopy isn't published yet.
        trade_date = postmarket_target_date()
    elif not is_trading_day(trade_date):
        resolved = last_trading_day(trade_date)
        log.info(f"  {trade_date} is not an NSE trading day — using last working day's Bhavcopy ({resolved})")
        trade_date = resolved
    log.info(f"📥 Bhavcopy pipeline {trade_date}")
    conn = get_connection()
    result = {"date":str(trade_date),"rows":0,"status":"SUCCESS"}
    try:
        existing = _bhavcopy_already_stored(conn, trade_date)
        if existing > 0:
            log.info(f"  ✓ Bhavcopy for {trade_date} already in database ({existing} rows) — skipping download")
            result["rows"] = existing
            result["status"] = "SKIPPED"
            log_job("bhavcopy","SKIPPED",existing,run_date=trade_date)
            return result

        session = get_nse_session()
        df_raw    = download_bhavcopy_cm(trade_date, session)
        df_prices = parse_bhavcopy_cm(df_raw, trade_date)
        count = 0
        for _, row in df_prices.iterrows():
            dq = row.get("delivery_qty"); dp = row.get("delivery_pct")
            conn.execute("""INSERT INTO prices_daily(symbol,date,open,high,low,close,volume,delivery_qty,delivery_pct,series,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
                close=excluded.close,volume=excluded.volume,
                delivery_qty=COALESCE(excluded.delivery_qty,prices_daily.delivery_qty),
                delivery_pct=COALESCE(excluded.delivery_pct,prices_daily.delivery_pct)""",
                (row.get("symbol"),str(row.get("date")),row.get("open"),row.get("high"),
                 row.get("low"),row.get("close"),row.get("volume"),
                 None if pd.isna(dq) else dq, None if pd.isna(dp) else dp,
                 row.get("series","EQ"),row.get("source","bhavcopy")))
            count += 1
        fii = download_fii_dii(trade_date, session)
        if fii:
            hist = pd.read_sql("SELECT fii_net_cr,dii_net_cr FROM fii_dii_market ORDER BY date DESC LIMIT 5", conn)
            conn.execute("""INSERT OR REPLACE INTO fii_dii_market
                (date,fii_buy_cr,fii_sell_cr,fii_net_cr,dii_buy_cr,dii_sell_cr,dii_net_cr,fii_5d_avg,dii_5d_avg)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (fii.get("date"),fii.get("fii_buy_cr",0),fii.get("fii_sell_cr",0),fii.get("fii_net_cr",0),
                 fii.get("dii_buy_cr",0),fii.get("dii_sell_cr",0),fii.get("dii_net_cr",0),
                 hist["fii_net_cr"].mean() if not hist.empty else 0,
                 hist["dii_net_cr"].mean() if not hist.empty else 0))
        conn.commit(); result["rows"] = count
        log.info(f"  ✓ {count} prices stored")
        log_job("bhavcopy","SUCCESS",count,run_date=trade_date)
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; log.error(f"  ✗ {e}")
        log_job("bhavcopy","FAILED",0,error=e,run_date=trade_date)
    finally: conn.close()
    return result

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date"); ap.add_argument("--today",action="store_true")
    ap.add_argument("--bulk-deals",action="store_true",help="also/only fetch bulk & block deals")
    args = ap.parse_args()
    td = datetime.strptime(args.date,"%Y-%m-%d").date() if args.date else date.today()
    if args.bulk_deals:
        print(run_bulk_deals_pipeline(td)); raise SystemExit(0)
    print(run_bhavcopy_pipeline(td))
    print(run_bulk_deals_pipeline(td))
