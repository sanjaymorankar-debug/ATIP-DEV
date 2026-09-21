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

_MONTHS = {m: i for i, m in enumerate(
    ("JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"), 1)}

def _nse_date(s):
    """NSE's "18-Sep-2026" as a date (None if unparseable). Not strptime("%b"),
    which follows the OS locale."""
    m = re.fullmatch(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*", str(s or ""))
    if not m or m.group(2).upper() not in _MONTHS: return None
    try: return date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)))
    except ValueError: return None

def download_fii_dii(trade_date, session):
    cache = RAW_DIR / f"fii_dii_{trade_date.strftime('%Y%m%d')}.json"
    if cache.exists():
        import json; return json.loads(cache.read_text())
    try:
        r = session.get(FII_DII_URL, timeout=15, headers={**HEADERS,"Accept":"application/json"})
        r.raise_for_status(); data = r.json()
        # This endpoint only serves the LATEST published day, and each row says
        # which day that is ("date": "18-Sep-2026"). The figures used to be
        # stamped with whatever trade_date was asked for, so a backfill stored
        # today's flows under past dates, and a run before NSE published stored
        # the previous day's flows under today. Label them with NSE's own date.
        served = {_nse_date(row.get("date")) for row in data if row.get("date")}
        served.discard(None)
        if len(served) > 1:
            log.warning(f"  FII/DII: NSE response mixes dates {sorted(map(str, served))} -- not stored")
            return {}
        flows_date = served.pop() if served else trade_date
        if flows_date != trade_date:
            log.info(f"  FII/DII: NSE is serving {flows_date}'s flows, not {trade_date}'s "
                     f"-- storing them under {flows_date}")
            cache = RAW_DIR / f"fii_dii_{flows_date.strftime('%Y%m%d')}.json"
        result = {"date":str(flows_date),"fii_buy_cr":0,"fii_sell_cr":0,"dii_buy_cr":0,"dii_sell_cr":0}
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

def store_fii_dii(conn, trade_date, session=None) -> int:
    """
    Fetch the published FII/DII flows and store them under NSE's own date.

    Deliberately NOT inside the price-download path. A session's prices are
    stored at 16:45, but NSE publishes that session's flows later in the
    evening, so the 18:30 catch-up is the run that can actually get them --
    and by then the Bhavcopy step short-circuits as "already stored". While
    this lived behind that early return, no session's own flows were ever
    ingested: market_health carried the 2026-09-09 figures on 09-10, 09-14,
    09-16 and 09-17 alike, because get_fii() silently falls back to the newest
    earlier row.
    """
    try:
        fii = download_fii_dii(trade_date, session or get_nse_session())
    except Exception as e:
        log.warning(f"  FII/DII unavailable: {e}")
        return 0
    if not fii:
        return 0
    hist = pd.read_sql("SELECT fii_net_cr,dii_net_cr FROM fii_dii_market "
                       "ORDER BY date DESC LIMIT 5", conn)
    conn.execute("""INSERT OR REPLACE INTO fii_dii_market
        (date,fii_buy_cr,fii_sell_cr,fii_net_cr,dii_buy_cr,dii_sell_cr,dii_net_cr,fii_5d_avg,dii_5d_avg)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (fii.get("date"),fii.get("fii_buy_cr",0),fii.get("fii_sell_cr",0),fii.get("fii_net_cr",0),
         fii.get("dii_buy_cr",0),fii.get("dii_sell_cr",0),fii.get("dii_net_cr",0),
         hist["fii_net_cr"].mean() if not hist.empty else 0,
         hist["dii_net_cr"].mean() if not hist.empty else 0))
    conn.commit()
    return 1


INDEX_CLOSE_URL = "https://archives.nseindia.com/content/indices/ind_close_all_{date}.csv"
# index_levels column -> index name in NSE's daily index-close file. Checked on
# 2026-09-10, 09-17, 09-18 and 09-21 against the feed's own 15:44 row: all 13
# closes identical. gift_nifty is an NSE IX contract and is not in the file.
NSE_INDEX_NAMES = {
    "nifty50": "Nifty 50", "banknifty": "Nifty Bank", "midcap150": "Nifty Midcap 150",
    "smallcap250": "Nifty Smallcap 250", "nifty_it": "Nifty IT", "nifty_auto": "Nifty Auto",
    "nifty_fmcg": "Nifty FMCG", "nifty_metal": "Nifty Metal", "nifty_realty": "Nifty Realty",
    "nifty_psubank": "Nifty PSU Bank", "nifty_energy": "Nifty Energy",
    "nifty_pharma": "Nifty Pharma", "india_vix": "India VIX",
}
INDEX_CLOSE_SNAPSHOT_TIME = "15:30:00"

def parse_index_closes(content, trade_date) -> dict:
    """{index_levels column: (close, change %)} from NSE's daily index-close CSV.
    A row dated other than trade_date is ignored: after the one-day date shift
    in prices_daily, nothing is filed under a date it does not itself carry."""
    df = pd.read_csv(io.BytesIO(content))
    df["key"] = df["Index Name"].astype(str).str.strip().str.lower()
    df["day"] = pd.to_datetime(df["Index Date"], format="%d-%m-%Y", errors="coerce").dt.date
    out = {}
    for col, name in NSE_INDEX_NAMES.items():
        m = df[(df["key"] == name.lower()) & (df["day"] == trade_date)]
        if len(m) != 1:
            continue
        close = pd.to_numeric(m["Closing Index Value"].iloc[0], errors="coerce")
        pts = pd.to_numeric(m["Points Change"].iloc[0], errors="coerce")
        if pd.isna(close) or close <= 0:
            continue
        prev = close - pts if not pd.isna(pts) else None
        out[col] = (float(close), round(float(pts) / prev * 100, 3) if prev else None)
    return out

def download_index_closes(trade_date, session):
    """Parsed closes for one session, or None while NSE has not published the
    file (404 -- also the answer for a day with no session)."""
    r = session.get(INDEX_CLOSE_URL.format(date=trade_date.strftime("%d%m%Y")), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return parse_index_closes(r.content, trade_date)

def store_index_close_snapshot(conn, trade_date, closes) -> bool:
    """
    Store NSE's closing values as the session's index_levels row -- only when
    the live feed stored no row inside its market-hours window that day.

    compute_mh and compute_msi read the session's latest index_levels row. With
    none they fall back to neutral defaults; before the feed was confined to
    market hours they read the previous session's values stamped after
    midnight, which is how 2026-09-16, a +0.43% day, was scored BEAR from
    09-15's -1.2%. A session the feed did cover is never touched.
    """
    from data.dhan_ws import FEED_OPEN, FEED_CLOSE
    covered = conn.execute(
        "SELECT COUNT(*) FROM index_levels WHERE date=? AND time BETWEEN ? AND ?",
        (str(trade_date), FEED_OPEN.strftime("%H:%M:%S"), FEED_CLOSE.strftime("%H:%M:%S"))
    ).fetchone()[0]
    if covered or not closes:
        return False
    record = {"date": str(trade_date), "time": INDEX_CLOSE_SNAPSHOT_TIME}
    changes = []
    for col, (close, chg) in closes.items():
        record[col] = close
        record[f"{col}_chg"] = chg
        if chg is not None:
            changes.append(chg)
    if changes:  # the same breadth rule as data/markets.py's snapshot
        pos = sum(1 for c in changes if c > 0.3); neg = sum(1 for c in changes if c < -0.3)
        record["overall_sentiment"] = ("BULLISH" if pos >= len(changes) * 0.7 else
                                       "BEARISH" if neg >= len(changes) * 0.7 else "NEUTRAL")
    cols = list(record)
    conn.execute(f"INSERT OR IGNORE INTO index_levels ({','.join(cols)}) "
                 f"VALUES ({','.join('?' * len(cols))})", [record[c] for c in cols])
    return True

def sync_nse_index_closes(trade_date=None) -> dict:
    """
    The session's official NSE index closes, stored where ATIP needs them:
    the NIFTY50 benchmark row (beta_1y and relative strength), and a closing
    index_levels snapshot if the live feed missed the session.

    Dhan's index history cannot supply the session being scored (see
    data.dhan.sync_index_benchmark_history), so until this the benchmark always
    ended one session before the stocks it was compared with.
    """
    if trade_date is None:
        trade_date = postmarket_target_date()
    elif isinstance(trade_date, str):
        trade_date = date.fromisoformat(trade_date)
    if not is_trading_day(trade_date):
        return {"status": "SKIPPED", "rows": 0, "reason": f"{trade_date} is not a session"}
    try:
        closes = download_index_closes(trade_date, get_nse_session())
    except Exception as e:
        log.warning(f"  NSE index closes for {trade_date}: {e}")
        return {"status": "FAILED", "rows": 0, "error": str(e)}
    if closes is None:
        log.warning(f"  NSE index closes for {trade_date} not published yet — "
                    f"benchmark stays at the last stored session")
        return {"status": "SKIPPED", "rows": 0, "reason": "not published"}
    if "nifty50" not in closes:
        log.warning(f"  NSE index-close file for {trade_date} has no Nifty 50 row")
        return {"status": "FAILED", "rows": 0, "error": "no Nifty 50 row"}
    from data.dhan import _store_benchmark_rows, BETA_BENCHMARK_SYMBOL
    conn = get_connection()
    try:
        rows = _store_benchmark_rows(conn, BETA_BENCHMARK_SYMBOL, [trade_date],
                                     [closes["nifty50"][0]], source="nse_index")
        snapshot = store_index_close_snapshot(conn, trade_date, closes)
        conn.commit()
    finally:
        conn.close()
    log.info(f"  ✓ NSE index closes {trade_date}: Nifty 50 {closes['nifty50'][0]}"
             f"{' + index snapshot (feed missed the session)' if snapshot else ''}")
    return {"status": "SUCCESS", "rows": rows + int(snapshot),
            "benchmark": closes["nifty50"][0], "snapshot": snapshot}


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
            # Prices are done, flows may not be: NSE publishes them later in the
            # evening, so a later run of the same date must still try.
            result["fii_dii"] = store_fii_dii(conn, trade_date)
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
        conn.commit()
        result["fii_dii"] = store_fii_dii(conn, trade_date, session)
        result["rows"] = count
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
