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
    now=datetime.now()
    TOKEN_PATH.write_text(json.dumps({"access_token":data["access_token"],"at":now.isoformat(timespec="seconds"),
                                      "expires_at":_next_expiry(now).isoformat(timespec="seconds")}))
    print(f"✅ Logged in. Token saved.")

# ── W29 (BR-02): Kite session lifecycle ─────────────────────────────────────
# A Kite access token is valid for the trading day and expires at 06:00 IST the next
# morning (Zerodha's documented daily flush). Logging in needs the user's own Zerodha
# login in a browser -- ATIP never handles the password / TOTP. The flow:
#   1. login_url()                    the owner opens it and logs in at Zerodha
#   2. Zerodha redirects to the app's redirect URL (set it in the Kite developer console to
#      http://127.0.0.1:8000/zerodha/callback) with ?request_token=...
#   3. complete_login(request_token)  exchanges it (needs kite_api_secret) and stores the
#      token with its expiry in atip_data/kite_token.json
# token_status() says VALID / EXPIRED / MISSING; the pre-market job alerts on EXPIRED so
# the morning holdings sync does not fail silently. ORDER placement through Kite is NOT
# built (owner decision required; see docs/W29 handoff).

def _next_expiry(at: datetime) -> datetime:
    from datetime import timedelta, time as _t
    six = datetime.combine(at.date(), _t(6, 0))
    return six if at < six else six + timedelta(days=1)


def token_status() -> dict:
    if not TOKEN_PATH.exists():
        return {"state": "MISSING", "detail": "no Kite session (log in via /api/zerodha/login-url)",
                "configured": bool(load_cfg().get("kite_api_key"))}
    try:
        t = json.loads(TOKEN_PATH.read_text())
        at = datetime.fromisoformat(str(t.get("at"))[:19]) if t.get("at") else None
    except Exception as e:
        return {"state": "EXPIRED", "detail": f"token file unreadable: {e}", "configured": True}
    exp = datetime.fromisoformat(t["expires_at"]) if t.get("expires_at") else (_next_expiry(at) if at else None)
    if exp is None:
        return {"state": "EXPIRED", "detail": "token has no login time", "configured": True}
    valid = datetime.now() < exp
    return {"state": "VALID" if valid else "EXPIRED", "logged_in_at": str(at)[:19] if at else None,
            "expires_at": str(exp)[:19], "configured": True,
            "detail": f"valid until {exp:%Y-%m-%d %H:%M}" if valid else f"expired at {exp:%Y-%m-%d %H:%M} -- log in again"}


def login_url() -> str:
    if not HAS_KITE:
        raise RuntimeError("pip install kiteconnect")
    cfg = load_cfg()
    if not cfg.get("kite_api_key"):
        raise RuntimeError("kite_api_key not set in atip_data/config.json")
    return KiteConnect(api_key=cfg["kite_api_key"]).login_url()


def complete_login(request_token: str) -> dict:
    if not HAS_KITE:
        raise RuntimeError("pip install kiteconnect")
    import re as _re
    if not request_token or not _re.fullmatch(r"[A-Za-z0-9]{8,64}", request_token):
        raise ValueError("invalid request_token")
    cfg = load_cfg()
    if not cfg.get("kite_api_key") or not cfg.get("kite_api_secret"):
        raise RuntimeError("kite_api_key and kite_api_secret must be set in atip_data/config.json")
    kite = KiteConnect(api_key=cfg["kite_api_key"])
    data = kite.generate_session(request_token, api_secret=cfg["kite_api_secret"])
    now = datetime.now()
    TOKEN_PATH.write_text(json.dumps({"access_token": data["access_token"], "at": now.isoformat(timespec="seconds"),
                                      "expires_at": _next_expiry(now).isoformat(timespec="seconds"),
                                      "user_id": data.get("user_id")}))
    log.info("  ✓ Zerodha session stored")
    return token_status()


def check_token_and_alert() -> dict:
    """Pre-market: a configured Kite session that has expired raises one alert a day."""
    st = token_status()
    if st["state"] == "EXPIRED":
        try:
            from alerts.telegram import notify
            notify("🔑 <b>Zerodha session expired</b> — log in again before the holdings sync "
                   "(dashboard /account or /api/zerodha/login-url)", category="broker", severity="warning",
                   key=f"kite-expired-{date.today()}")
        except Exception as e:
            log.warning(f"  Zerodha expiry alert: {e}")
    return {"status": "SUCCESS", "rows": 0, **st}


def zerodha_connected() -> bool:
    """A Kite session exists (python main.py --login-zerodha has been run)."""
    return HAS_KITE and TOKEN_PATH.exists() and token_status()["state"] == "VALID"

def run_portfolio_sync(trade_date=None):
    if trade_date is None: trade_date=date.today()
    # Not set up is not a failure: without a Kite login this fallback used to
    # log FAILED ("No access token") on every evening Dhan's sync did not work.
    if not zerodha_connected():
        return {"status":"SKIPPED","rows":0,
                "reason":"Zerodha not connected (kiteconnect missing or no login: python main.py --login-zerodha)"}
    from data.dhan import record_portfolio_sync
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
        if not rows:
            log.info("  No holdings"); record_portfolio_sync(trade_date,"zerodha","SUCCESS",n_holdings=0)
            result["rows"]=0; return result
        df=pd.DataFrame(rows); total=df["current_val"].sum()
        df["weight_pct"]=(df["current_val"]/total*100).round(2) if total else 0
        count=0
        for _,row in df.iterrows():
            sc=conn.execute("SELECT atip_score,vpi,cri,zpi,signal FROM ai_scores WHERE symbol=? AND date=?",(row["symbol"],str(trade_date))).fetchone()
            conn.execute("""INSERT INTO portfolio_holdings (date,symbol,qty,avg_price,cmp,current_val,pnl,pnl_pct,weight_pct,atip_score,vpi,cri,zpi,signal)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET qty=excluded.qty,avg_price=excluded.avg_price,
                cmp=excluded.cmp,current_val=excluded.current_val,pnl=excluded.pnl,pnl_pct=excluded.pnl_pct,weight_pct=excluded.weight_pct""",
                (str(trade_date),row["symbol"],row["qty"],row["avg_price"],row["cmp"],row["current_val"],
                 row["pnl"],row["pnl_pct"],row["weight_pct"],
                 sc["atip_score"] if sc else None,sc["vpi"] if sc else None,
                 sc["cri"] if sc else None,sc["zpi"] if sc else None,
                 sc["signal"] if sc else "NO DATA"))
            count+=1
        held=list(df["symbol"])
        conn.execute(f"DELETE FROM portfolio_holdings WHERE date=? AND symbol NOT IN ({','.join('?'*len(held))})",
                     (str(trade_date),*held))                       # sold since an earlier sync today
        conn.commit(); result["rows"]=count
        record_portfolio_sync(trade_date,"zerodha","SUCCESS",n_holdings=count)
        log.info(f"  ✓ {count} holdings synced  Total: ₹{total:,.0f}")
        log_job("portfolio_sync","SUCCESS",count,run_date=trade_date)
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; result["error"]=str(e); log.error(f"  ✗ {e}")
        record_portfolio_sync(trade_date,"zerodha","FAILED",error=str(e))
        log_job("portfolio_sync","FAILED",0,error=e,run_date=trade_date)
    finally: conn.close()
    return result

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--login",action="store_true"); ap.add_argument("--sync",action="store_true")
    args=ap.parse_args()
    if args.login: login()
    elif args.sync: print(run_portfolio_sync())
