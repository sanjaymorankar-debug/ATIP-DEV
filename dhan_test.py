"""
ATIP — Dhan Connection Tester + Data Loader
=============================================
Run FIRST to test Dhan and load data into dashboard.

  python dhan_test.py          # full: test + load data + build dashboard
  python dhan_test.py --test   # connection test only
  python dhan_test.py --quote  # live quotes only
  python dhan_test.py --load   # load 1-year history
"""
import sys,os,json,time,logging
from pathlib import Path
from datetime import date,datetime,timedelta

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s",datefmt="%H:%M:%S")
log=logging.getLogger(__name__)

# Verified Dhan security IDs for NSE EQ stocks
SECURITIES={
    "RELIANCE":1333,"TCS":11536,"INFY":1594,"HDFCBANK":1330,
    "ICICIBANK":4963,"SBIN":3045,"BHARTIARTL":10604,"WIPRO":3787,
    "TATAMOTORS":3456,"TATASTEEL":3499,"JSWSTEEL":11723,"HINDALCO":1348,
    "VEDL":3063,"ADANIENT":25,"ADANIGREEN":6718,"ADANIPOWER":27,
    "ADANIPORTS":15083,"BEL":383,"HAL":2303,"BHEL":438,
    "SAIL":2963,"PFC":14299,"RECLTD":532,"IRFC":13285,
    "SJVN":20220,"NHPC":13771,"SUZLON":3350,"NMDC":2270,
    "COALINDIA":20374,"NTPC":2303,"POWERGRID":14977,"ONGC":2475,
    "DLF":14732,"ZOMATO":21176,"BAJFINANCE":317,"KOTAKBANK":1922,
    "AXISBANK":5900,"INDUSINDBK":5258,"YESBANK":19239,"PNB":10666,
    "CANBK":15141,"BANKBARODA":1152,"SUNPHARMA":3351,"DRREDDY":881,
    "CIPLA":694,"DIVISLAB":10243,"TITAN":3506,"ASIANPAINT":236,
    "NESTLEIND":17963,"ITC":1660,"MARUTI":10999,"LT":11483,
    "TECHM":13538,"HCLTECH":1363,"BAJAJFINSV":16675,"EICHERMOT":910,
    "M&M":2031,"GRASIM":1232,"ULTRACEMCO":11532,"BRITANNIA":547,
    "APOLLOHOSP":157,"TATACONSUM":3432,"BPCL":526,"IREDA":26335,
    "RVNL":21238,"HUDCO":13554,"INOXWIND":27913,"BSE":543,
    "PAYTM":21048,"NYKAA":21145,"JIOFIN":25925,"LODHA":19253,
    "GODREJPROP":17928,"PRESTIGE":26099,"HINDUNILVR":1394,"BAJAJ-AUTO":16669,
    "HEROMOTOCO":1384,"TATAPOWER":3426,"NATIONALUM":15083,"HINDCOPPER":3048,
}

def load_config():
    p=ROOT/"atip_data"/"config.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}

def get_dhan():
    cfg=load_config()
    cid  =cfg.get("dhan_client_id","")   or os.getenv("DHAN_CLIENT_ID","")
    token=cfg.get("dhan_access_token","") or os.getenv("DHAN_ACCESS_TOKEN","")
    if not cid or cid=="YOUR_DHAN_CLIENT_ID":
        print("""
❌  Dhan credentials not found in atip_data\\config.json

Add these lines:
  "dhan_client_id":    "1234567890",
  "dhan_access_token": "eyJ0eXAi..."

Get them: https://web.dhanhq.com → My Profile → Apps → Create App
"""); sys.exit(1)
    from dhanhq import dhanhq,DhanContext
    ctx=DhanContext(cid,token)
    return dhanhq(ctx),cid

def test_connection(dhan,cid):
    print("\n[TEST 1] Dhan API connection...")
    try:
        r=dhan.get_fund_limits()
        if r and r.get("status")=="success":
            d=r.get("data",{})
            bal=d.get("availabelBalance",d.get("availableBalance","?"))
            print(f"  ✅  Connected! Client: {cid}  Balance: ₹{bal}")
            return True
        print(f"  ❌  {r}"); return False
    except Exception as e:
        print(f"  ❌  {e}"); return False

def test_quote(dhan):
    print("\n[TEST 2] Live quote for RELIANCE (sec_id=1333)...")
    try:
        r=dhan.ohlc_data({"NSE_EQ":[1333]})
        print(f"  Response: {json.dumps(r,indent=2)[:300]}")
        if r and r.get("status")=="success":
            data=r.get("data",{})
            nse=data.get("NSE_EQ",{})
            q=list(nse.values())[0] if nse else {}
            print(f"  ✅  LTP=₹{q.get('last_price','?')}  High=₹{q.get('high','?')}  Low=₹{q.get('low','?')}")
            return True
        return False
    except Exception as e:
        print(f"  ❌  {e}"); return False

def test_history(dhan):
    print("\n[TEST 3] Historical daily data for TCS (last 10 days)...")
    try:
        end=date.today(); start=end-timedelta(days=15)
        r=dhan.historical_daily_data(
            security_id="11536",exchange_segment="NSE_EQ",
            instrument_type="EQUITY",
            from_date=start.strftime("%Y-%m-%d"),
            to_date=end.strftime("%Y-%m-%d"))
        print(f"  Response keys: {list(r.keys()) if r else 'None'}")
        if r and r.get("status")=="success":
            d=r.get("data",{}); closes=d.get("close",[]); ts=d.get("timestamp",[])
            print(f"  ✅  {len(closes)} daily bars")
            for i in range(min(3,len(closes))):
                dt=datetime.fromtimestamp(ts[i]).strftime("%Y-%m-%d") if ts else "?"
                print(f"     {dt}: ₹{closes[i]:.2f}")
            return True,r
        print(f"  ❌  {r}"); return False,r
    except Exception as e:
        print(f"  ❌  {e}"); return False,{}

def fetch_quotes(dhan):
    print(f"\n[QUOTES] Fetching live quotes for {len(SECURITIES)} stocks...")
    syms=list(SECURITIES.keys()); ids=list(SECURITIES.values())
    all_q=[]
    for i in range(0,len(ids),100):
        b_ids=ids[i:i+100]; b_syms=syms[i:i+100]
        id2sym={str(s):n for s,n in zip(b_ids,b_syms)}
        try:
            r=dhan.ohlc_data({"NSE_EQ":b_ids})
            if r and r.get("status")=="success":
                nse=r.get("data",{}).get("NSE_EQ",{})
                for sid,q in nse.items():
                    sym=id2sym.get(sid,sid)
                    ltp=q.get("last_price",0); pc=q.get("prev_close",ltp) or ltp
                    all_q.append({"symbol":sym,"ltp":ltp,"open":q.get("open"),
                                  "high":q.get("high"),"low":q.get("low"),
                                  "prev_close":pc,"volume":q.get("volume"),
                                  "chg_pct":round((ltp-pc)/pc*100,2) if pc else 0})
        except Exception as e: log.warning(f"Batch {i}: {e}")
        time.sleep(0.3)

    if not all_q:
        print("  ❌  No quotes returned"); return []

    # Store in DB
    try:
        from db.schema import get_connection
        conn=get_connection()
        conn.execute("""CREATE TABLE IF NOT EXISTS live_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,ltp REAL,
            open REAL,high REAL,low REAL,prev_close REAL,volume INTEGER,
            chg_pct REAL,timestamp TEXT,source TEXT DEFAULT 'dhan',
            UNIQUE(symbol,timestamp))""")
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for q in all_q:
            conn.execute("INSERT OR REPLACE INTO live_quotes (symbol,ltp,open,high,low,prev_close,volume,chg_pct,timestamp) VALUES(?,?,?,?,?,?,?,?,?)",
                (q["symbol"],q["ltp"],q["open"],q["high"],q["low"],q["prev_close"],q["volume"],q["chg_pct"],ts))
        conn.commit(); conn.close()
        print(f"  ✅  Stored {len(all_q)} live quotes in DB")
    except Exception as e:
        log.warning(f"  DB store: {e}")

    print(f"\n  {'Symbol':<15} {'LTP':>9} {'Chg%':>7} {'High':>9} {'Low':>9}")
    print(f"  {'-'*55}")
    for q in sorted(all_q,key=lambda x:abs(x.get('chg_pct',0)),reverse=True)[:15]:
        c=q.get('chg_pct',0); s="▲" if c>=0 else "▼"
        print(f"  {q['symbol']:<15} ₹{q.get('ltp',0):>8,.2f} {s}{abs(c):>5.2f}% ₹{q.get('high',0):>8,.2f} ₹{q.get('low',0):>8,.2f}")
    return all_q

def load_history(dhan,days=365):
    print(f"\n[HISTORY] Downloading {days}-day history for {len(SECURITIES)} stocks...")
    from db.schema import init_db,get_connection
    init_db(); conn=get_connection()
    end=date.today(); start=end-timedelta(days=days)
    total=0; ok=0; fail=[]
    print(f"  [✓=loaded  .=empty  x=error  — every dot is 1 stock]\n  ",end="",flush=True)
    for i,(sym,sec_id) in enumerate(SECURITIES.items()):
        try:
            r=dhan.historical_daily_data(
                security_id=str(sec_id),exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=start.strftime("%Y-%m-%d"),
                to_date=end.strftime("%Y-%m-%d"))
            if r and r.get("status")=="success":
                d=r.get("data",{})
                closes=d.get("close",[]); opens=d.get("open",[])
                highs=d.get("high",[]); lows=d.get("low",[])
                vols=d.get("volume",[]); ts=d.get("timestamp",[])
                if closes:
                    for j in range(len(closes)):
                        dt=datetime.fromtimestamp(ts[j]).strftime("%Y-%m-%d") if ts else str(end)
                        conn.execute("""INSERT INTO prices_daily(symbol,date,open,high,low,close,volume,source)
                            VALUES(?,?,?,?,?,?,?,'dhan') ON CONFLICT(symbol,date) DO UPDATE SET
                            open=excluded.open,high=excluded.high,low=excluded.low,
                            close=excluded.close,volume=excluded.volume""",
                            (sym,dt,opens[j] if opens else None,highs[j] if highs else None,
                             lows[j] if lows else None,closes[j],int(vols[j]) if vols else None))
                    conn.commit(); total+=len(closes); ok+=1
                    print("✓",end="",flush=True)
                else:
                    print(".",end="",flush=True)
            else:
                fail.append(sym); print("x",end="",flush=True)
        except Exception as e:
            fail.append(sym); log.debug(f"{sym}:{e}"); print("E",end="",flush=True)
        if (i+1)%20==0: print(f" [{i+1}/{len(SECURITIES)}]",end="",flush=True)
        time.sleep(0.35)
    conn.close()
    print(f"\n\n  ✅  {ok} stocks  |  {total:,} rows  |  {len(fail)} failed")
    if fail: print(f"  Failed: {fail}")
    return total

def build_dashboard():
    print("\n[DASHBOARD] Building dashboard...")
    # Technical indicators
    try:
        from data.technical import run_technical_pipeline
        r=run_technical_pipeline(date.today())
        print(f"  ✅  Technical: {r.get('rows',0)} stocks")
    except Exception as e: print(f"  ⚠️  Technical: {e}")
    # AI scores
    try:
        from scores.engine import run_scoring_pipeline
        r=run_scoring_pipeline(date.today())
        print(f"  ✅  AI Scores: {r.get('rows',0)} stocks scored")
    except Exception as e: print(f"  ⚠️  AI Scores: {e}")
    # Dashboard state
    try:
        from dashboard.server import generate_state
        generate_state(date.today())
        print("  ✅  Dashboard state saved")
    except Exception as e: print(f"  ⚠️  Dashboard state: {e}")
    print("""
╔══════════════════════════════════════════════════════╗
║  ✅  DONE! Open dashboard:                           ║
║                                                      ║
║     python main.py --dashboard                       ║
║     http://localhost:8000                            ║
╚══════════════════════════════════════════════════════╝
""")

if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--test",  action="store_true")
    ap.add_argument("--quote", action="store_true")
    ap.add_argument("--load",  action="store_true")
    ap.add_argument("--days",  type=int,default=365)
    args=ap.parse_args()

    dhan,cid=get_dhan()
    if not test_connection(dhan,cid): sys.exit(1)
    test_quote(dhan)
    ok,_=test_history(dhan)

    if args.test:
        print("\n✅  Connection OK! Dhan API is working.\n")
    elif args.quote:
        fetch_quotes(dhan)
    elif args.load:
        load_history(dhan,args.days)
        build_dashboard()
    else:
        # Default: full pipeline — loads data + builds dashboard
        print("\n[LOADING] Running full data load (5-10 min)...")
        rows=load_history(dhan,args.days)
        if rows>0:
            fetch_quotes(dhan)
            build_dashboard()
        else:
            print("\n⚠️  No historical data loaded — trying live quotes only...")
            fetch_quotes(dhan)
            build_dashboard()
