"""ATIP — Zerodha Kite API Portfolio Sync + Portfolio Health Score"""
import os, json, logging, argparse
import pandas as pd
from datetime import date, datetime
from pathlib import Path
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)
CONFIG_PATH=Path("atip_data/config.json"); TOKEN_PATH=Path("atip_data/kite_token.json")
try: from kiteconnect import KiteConnect; HAS_KITE=True
except ImportError: HAS_KITE=False; log.warning("pip install kiteconnect")

def load_cfg():
    cfg={}
    if CONFIG_PATH.exists():
        try: cfg=json.loads(CONFIG_PATH.read_text())
        except: pass
    cfg.setdefault("kite_api_key",os.getenv("KITE_API_KEY",""))
    cfg.setdefault("kite_api_secret",os.getenv("KITE_API_SECRET",""))
    return cfg

def get_kite():
    if not HAS_KITE: raise RuntimeError("pip install kiteconnect")
    cfg=load_cfg()
    if not cfg["kite_api_key"]: raise RuntimeError("kite_api_key not set in atip_data/config.json")
    kite=KiteConnect(api_key=cfg["kite_api_key"])
    if TOKEN_PATH.exists():
        t=json.loads(TOKEN_PATH.read_text()); kite.set_access_token(t["access_token"])
    else: raise RuntimeError("No access token. Run: python main.py --login-zerodha")
    return kite

def login():
    if not HAS_KITE: print("pip install kiteconnect"); return
    cfg=load_cfg()
    if not cfg["kite_api_key"]: print("Set kite_api_key in atip_data/config.json"); return
    kite=KiteConnect(api_key=cfg["kite_api_key"])
    print(f"\n1. Open: {kite.login_url()}\n")
    url=input("2. Paste redirect URL: ").strip()
    rt=url.split("request_token=")[1].split("&")[0]
    data=kite.generate_session(rt,api_secret=cfg["kite_api_secret"])
    TOKEN_PATH.write_text(json.dumps({"access_token":data["access_token"],"at":str(datetime.now())}))
    print(f"✅ Logged in. Token saved.")

def run_portfolio_sync(trade_date=None):
    if trade_date is None: trade_date=date.today()
    if not HAS_KITE: log.error("kiteconnect not installed"); return {"status":"FAILED"}
    log.info(f"📈 Portfolio sync {trade_date}")
    conn=get_connection(); result={"status":"SUCCESS"}
    try:
        kite=get_kite(); holdings=kite.holdings()
        rows=[]
        for h in holdings:
            if h.get("quantity",0)==0: continue
            cmp=h.get("last_price",0); avg=h.get("average_price",0)
            rows.append({"symbol":h["tradingsymbol"],"qty":h["quantity"],"avg_price":avg,"cmp":cmp,
                         "current_val":h["quantity"]*cmp,"pnl":h.get("pnl",0),
                         "pnl_pct":round((cmp-avg)/avg*100,2) if avg else 0})
        if not rows: log.info("  No holdings"); return result
        df=pd.DataFrame(rows); total=df["current_val"].sum()
        df["weight_pct"]=(df["current_val"]/total*100).round(2) if total else 0
        count=0
        for _,row in df.iterrows():
            sc=conn.execute("SELECT atip_score,vpi,cri,zpi,signal FROM ai_scores WHERE symbol=? AND date=?",(row["symbol"],str(trade_date))).fetchone()
            conn.execute("""INSERT INTO portfolio_holdings (date,symbol,qty,avg_price,cmp,current_val,pnl,pnl_pct,weight_pct,atip_score,vpi,cri,zpi,signal)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET cmp=excluded.cmp,pnl=excluded.pnl,pnl_pct=excluded.pnl_pct""",
                (str(trade_date),row["symbol"],row["qty"],row["avg_price"],row["cmp"],row["current_val"],
                 row["pnl"],row["pnl_pct"],row["weight_pct"],
                 sc["atip_score"] if sc else None,sc["vpi"] if sc else None,
                 sc["cri"] if sc else None,sc["zpi"] if sc else None,
                 sc["signal"] if sc else "NO DATA"))
            count+=1
        conn.commit(); result["rows"]=count
        log.info(f"  ✓ {count} holdings synced  Total: ₹{total:,.0f}")
        log_job("portfolio_sync","SUCCESS",count,run_date=trade_date)
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; log.error(f"  ✗ {e}")
        log_job("portfolio_sync","FAILED",0,error=e,run_date=trade_date)
    finally: conn.close()
    return result

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--login",action="store_true"); ap.add_argument("--sync",action="store_true")
    args=ap.parse_args()
    if args.login: login()
    elif args.sync: print(run_portfolio_sync())
