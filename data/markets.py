"""ATIP — Global Markets & Intraday Index Fetcher"""
import logging, argparse
import yfinance as yf
from datetime import date, datetime
from db.schema import get_connection, log_job
from data.dhan import fetch_index_quotes, NSE_INDEX_SECURITY_IDS

log = logging.getLogger(__name__)

# NSE indexes now come from Dhan (data/dhan.py: fetch_index_quotes),
# not Yahoo Finance. Yahoo's ^CNXxxx sector-index ticker family (midcap150,
# smallcap250, nifty_auto, nifty_fmcg, nifty_metal, nifty_realty,
# nifty_psubank, nifty_energy) is retired/dead — confirmed by all 7 coming
# back empty/0.00% simultaneously on a day Nifty/BankNifty moved +0.7%+.
# Dhan's security master has working security_ids for all of these
# (SEM_EXCH_INSTRUMENT_TYPE == "INDEX", SEM_EXM_EXCH_ID == "NSE"), so the
# whole set — including the ones Yahoo could never serve — is fetched
# through Dhan's quote_data() REST endpoint instead. See
# NSE_INDEX_SECURITY_IDS in data/dhan.py for the id map.
NSE_INDEXES = dict(NSE_INDEX_SECURITY_IDS)  # column name -> Dhan security_id

GLOBAL_TICKERS = {
    "sp500":"^GSPC","dow":"^DJI","nasdaq":"^IXIC","nikkei":"^N225","hangseng":"^HSI",
    "ftse100":"^FTSE","dax":"^GDAXI","crude_wti":"CL=F","crude_brent":"BZ=F",
    "gold":"GC=F","silver":"SI=F","usd_inr":"INR=X","usd_index":"DX-Y.NYB","us_10y":"^TNX",
}

def safe_float(val):
    try: f = float(val); return None if f!=f else round(f,4)
    except: return None

def fetch_intraday_indexes(trade_date=None):
    """
    Snapshot the live index quotes into index_levels.

    The row pairs a date with `now`'s clock time and `now`'s prices, so the date
    must be today. Passing a past date writes current prices under a historical
    stamp and silently corrupts that day's index history — which is exactly how
    a pre-open snapshot ended up filed as 2026-09-07 08:43:16 with every change
    at 0.00%, and then surfaced on the dashboard as the previous session's close.
    """
    today = date.today()
    if trade_date is None:
        trade_date = today
    elif trade_date != today:
        log.warning(f"  Index refresh asked for {trade_date}, but these are LIVE prices "
                    f"as of {today} — recording under {today} instead. Backfilling a past "
                    f"session's indexes needs historical data, not a live snapshot.")
        trade_date = today
    now_time = datetime.now().strftime("%H:%M:%S")  # matches dhan_ws.py's format so
                                                       # ORDER BY time DESC sorts correctly
                                                       # across rows from both writers
    log.info(f"📊 Index refresh {now_time}")
    try:
        df = fetch_index_quotes(list(NSE_INDEXES.keys()))
    except Exception as e:
        log.error(f"  Index fetch failed: {e}"); return {}
    if df.empty:
        log.error("  Index fetch returned no data"); return {}
    record = {"date":str(trade_date),"time":now_time}
    changes = []
    quotes = {row["index"]: row for _, row in df.iterrows()}
    for col in NSE_INDEXES:
        q = quotes.get(col)
        if q is None: continue
        try:
            latest = safe_float(q.get("ltp")); chg = q.get("chg_pct")
            if latest is None: continue
            chg = round(float(chg), 3) if chg is not None else 0
            if abs(chg) > 20:  # sanity guard — no NSE broad/sector index moves >20% in a day
                log.warning(f"  {col}: implausible {chg:+.2f}% change — likely a bad/stale quote, skipping")
                continue
            record[col] = latest; record[f"{col}_chg"] = chg; changes.append(chg)
        except: pass
    if changes:
        pos = sum(1 for c in changes if c>0.3); neg = sum(1 for c in changes if c<-0.3)
        record["overall_sentiment"] = ("BULLISH" if pos>=len(changes)*0.7 else
                                       "BEARISH" if neg>=len(changes)*0.7 else "NEUTRAL")
    conn = get_connection()
    try:
        cols = [k for k in record if k not in ("date","time")]
        conn.execute(f"INSERT INTO index_levels (date,time,{','.join(cols)}) VALUES (?,?,{','.join(['?']*len(cols))})",
                     [record["date"],record["time"]]+[record.get(c) for c in cols])
        conn.commit()
        log.info(f"  ✓ Nifty:{record.get('nifty50_chg',0):+.2f}% VIX:{record.get('india_vix','?')} → {record.get('overall_sentiment')}")
        log_job("intraday_indexes","SUCCESS",len(cols),run_date=trade_date)
    except Exception as e:
        conn.rollback(); log.error(f"  DB: {e}")
    finally: conn.close()
    return record

def compute_global_score(changes):
    def avg(tks, scale=2.0):
        vals=[v for v in tks if v is not None]
        if not vals: return 50.0
        return max(0,min(100,50+(sum(vals)/len(vals)/scale)*50))
    us=avg([changes.get("sp500"),changes.get("dow"),changes.get("nasdaq")],1.5)
    asia=avg([changes.get("nikkei"),changes.get("hangseng")],1.5)
    cmd=avg([changes.get("gold"),-(changes.get("crude_wti") or 0)],2.0)
    g=us*0.40+asia*0.25+cmd*0.35
    return round(g,2),round(us,2),round(asia,2),round(cmd,2),("BULLISH" if g>=65 else "BEARISH" if g<=40 else "NEUTRAL")

def fetch_global_markets(trade_date=None, mode="premarket"):
    if trade_date is None: trade_date = date.today()
    log.info(f"🌍 Global markets ({mode})")
    tickers = list(GLOBAL_TICKERS.values())
    try: data = yf.download(tickers, period="3d", interval="1d", group_by="ticker", progress=False, auto_adjust=True)
    except Exception as e: log.error(f"  Failed: {e}"); return {}
    record={"date":str(trade_date),"time":mode}; changes={}
    for col, ticker in GLOBAL_TICKERS.items():
        try:
            closes=(data[ticker]["Close"] if len(tickers)>1 else data["Close"]).dropna()
            if len(closes)<2: continue
            latest=safe_float(closes.iloc[-1]); prev=safe_float(closes.iloc[-2])
            chg=round((latest-prev)/prev*100,3) if (latest and prev) else 0
            record[col]=latest; record[f"{col}_chg"]=chg; changes[col]=chg
        except: pass
    gs,us,asia,cmd,sent = compute_global_score(changes)
    record.update({"global_score":gs,"us_score":us,"asia_score":asia,"commodity_score":cmd,"global_sentiment":sent})
    conn=get_connection()
    try:
        keys=[k for k in record]
        conn.execute(f"INSERT OR REPLACE INTO global_markets ({','.join(keys)}) VALUES ({','.join(['?']*len(keys))})",
                     [record[k] for k in keys])
        conn.commit()
        log.info(f"  ✓ Global:{sent} S&P:{changes.get('sp500',0):+.2f}% Gold:{changes.get('gold',0):+.2f}%")
        log_job("global_markets","SUCCESS",len(keys),run_date=trade_date)
    except Exception as e:
        conn.rollback(); log.error(f"  DB: {e}")
    finally: conn.close()
    return record

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--mode",default="intraday")
    args=ap.parse_args()
    if args.mode=="intraday": fetch_intraday_indexes()
    else: fetch_global_markets(mode=args.mode)
