"""ATIP — Fundamental Data Fetcher (Alpha Vantage + Screener.in fallback)"""
import os, json, time, logging, argparse
import requests, pandas as pd
from datetime import datetime
from pathlib import Path
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)
CACHE_DIR=Path("atip_data/raw/fundamentals"); CACHE_DIR.mkdir(parents=True,exist_ok=True)
AV_BASE="https://www.alphavantage.co/query"

def get_av_key():
    cfg_path=Path("atip_data/config.json")
    if cfg_path.exists():
        try:
            cfg=json.loads(cfg_path.read_text())
            if cfg.get("alpha_vantage_key") and cfg["alpha_vantage_key"]!="YOUR_ALPHA_VANTAGE_KEY":
                return cfg["alpha_vantage_key"]
        except: pass
    return os.getenv("ALPHA_VANTAGE_KEY","")

def av_get(function, symbol):
    key=get_av_key()
    if not key: log.warning("  Alpha Vantage key not set"); return {}
    cache=CACHE_DIR/f"{symbol}_{function}.json"
    if cache.exists() and (datetime.now().timestamp()-cache.stat().st_mtime)<86400:
        return json.loads(cache.read_text())
    try:
        r=requests.get(AV_BASE,params={"function":function,"symbol":f"{symbol}.BSE","apikey":key},timeout=15)
        r.raise_for_status(); data=r.json()
        if "Note" in data or "Information" in data: time.sleep(62); r=requests.get(AV_BASE,params={"function":function,"symbol":f"{symbol}.BSE","apikey":key},timeout=15); data=r.json()
        cache.write_text(json.dumps(data)); return data
    except Exception as e: log.warning(f"  AV {function} {symbol}: {e}"); return {}

def safe_num(val):
    try: f=float(str(val).replace(",","").replace("%","")); return None if f!=f else round(f,4)
    except: return None

def parse_av(symbol):
    ov=av_get("OVERVIEW",symbol); ic=av_get("INCOME_STATEMENT",symbol)
    bl=av_get("BALANCE_SHEET",symbol); cf=av_get("CASH_FLOW",symbol)
    r={"symbol":symbol,"source":"alpha_vantage"}
    if ov:
        for k,v in [("pe_ratio","PERatio"),("pb_ratio","PriceToBookRatio"),("peg_ratio","PEGRatio"),
                    ("dividend_yield","DividendYield"),("roe","ReturnOnEquityTTM"),
                    ("operating_margin","OperatingMarginTTM"),("net_margin","ProfitMargin"),("eps_ttm","EPS")]:
            r[k]=safe_num(ov.get(v))
    if ic and ic.get("quarterlyReports"):
        q=ic["quarterlyReports"]
        if len(q)>=4:
            r0=q[0]; r4=q[3] if len(q)>3 else {}
            rv0=safe_num(r0.get("totalRevenue")); rv4=safe_num(r4.get("totalRevenue"))
            pf0=safe_num(r0.get("netIncome"));    pf4=safe_num(r4.get("netIncome"))
            ep0=safe_num(r0.get("reportedEPS"));  ep4=safe_num(r4.get("reportedEPS"))
            if rv0 and rv4 and rv4!=0: r["revenue_growth_yoy"]=round((rv0-rv4)/abs(rv4)*100,2)
            if pf0 and pf4 and pf4!=0: r["profit_growth_yoy"]=round((pf0-pf4)/abs(pf4)*100,2)
            if ep0 and ep4 and ep4!=0: r["eps_growth_yoy"]=round((ep0-ep4)/abs(ep4)*100,2)
            r["quarter"]=r0.get("fiscalDateEnding","")[:7]; r["report_date"]=r0.get("fiscalDateEnding")
    if bl and bl.get("quarterlyReports"):
        b=bl["quarterlyReports"][0]
        debt=safe_num(b.get("shortLongTermDebtTotal")) or (safe_num(b.get("longTermDebt")) or 0)
        eq=safe_num(b.get("totalShareholderEquity"))
        ca=safe_num(b.get("totalCurrentAssets")); cl=safe_num(b.get("totalCurrentLiabilities"))
        if debt is not None and eq and eq!=0: r["debt_equity"]=round(debt/eq,3)
        if ca and cl and cl!=0: r["current_ratio"]=round(ca/cl,3)
    if cf and cf.get("quarterlyReports"):
        c0=cf["quarterlyReports"][0]
        cfo=safe_num(c0.get("operatingCashflow")); cap=safe_num(c0.get("capitalExpenditures"))
        if cfo and cap: r["fcf_cr"]=round((cfo-abs(cap))/1e7,2)
    r["fundamental_score"]=compute_fs(r); return r

def fetch_screener(symbol):
    try:
        from bs4 import BeautifulSoup
        r=requests.get(f"https://www.screener.in/company/{symbol}/consolidated/",
                       headers={"User-Agent":"Mozilla/5.0"},timeout=15)
        if r.status_code!=200: return {}
        soup=BeautifulSoup(r.text,"html.parser"); result={"symbol":symbol,"source":"screener_in"}
        ratios={}
        for li in soup.select("#top-ratios li"):
            n=li.select_one(".name"); v=li.select_one(".value .nowrap") or li.select_one(".value")
            if n and v:
                val=re.sub(r"[^\d.\-]","",v.text.strip()) if __import__("re").search(r"\d",v.text) else None
                if val: ratios[n.text.strip()]=safe_num(val)
        result["pe_ratio"]=ratios.get("Stock P/E"); result["pb_ratio"]=ratios.get("Price to Book")
        result["roe"]=ratios.get("Return on equity"); result["debt_equity"]=ratios.get("Debt to equity")
        result["promoter_hold"]=ratios.get("Promoter holding"); result["dividend_yield"]=ratios.get("Dividend Yield")
        result["fundamental_score"]=compute_fs(result); log.info(f"  ✓ Screener: {symbol}"); return result
    except Exception as e: log.warning(f"  Screener {symbol}: {e}"); return {}

def compute_fs(f):
    def nm(v,lo,hi,inv=False):
        if v is None: return None
        s=max(0,min(100,(v-lo)/(hi-lo)*100)); return round(100-s if inv else s,2)
    scores={}
    if f.get("roe"): scores["ROE"]=nm(f["roe"],0,0.5)
    if f.get("eps_growth_yoy"): scores["EPS"]=nm(f["eps_growth_yoy"],-30,100)
    if f.get("revenue_growth_yoy"): scores["Rev"]=nm(f["revenue_growth_yoy"],-20,60)
    if f.get("debt_equity") is not None: scores["Debt"]=nm(f["debt_equity"],0,4,inv=True)
    if f.get("peg_ratio"): scores["PEG"]=nm(f["peg_ratio"],0,4,inv=True)
    if f.get("operating_margin"): scores["Margin"]=nm(f["operating_margin"],0,0.4)
    return round(sum(scores.values())/len(scores),2) if scores else 50.0

def run_fundamentals_pipeline(symbols=None):
    import re
    conn=get_connection(); result={"rows":0,"status":"SUCCESS"}
    if not symbols:
        rows=conn.execute("SELECT DISTINCT symbol FROM prices_daily").fetchall()
        symbols=[r["symbol"] for r in rows]
    log.info(f"📊 Fundamentals for {len(symbols)} symbols"); count=0
    for sym in symbols:
        try:
            data=parse_av(sym)
            if not data or len(data)<5: data=fetch_screener(sym)
            if not data: continue
            q=data.get("quarter") or datetime.now().strftime("Q%mFY%y")
            conn.execute("""INSERT INTO fundamental_data
                (symbol,quarter,report_date,roe,net_margin,operating_margin,eps_ttm,eps_growth_yoy,
                 revenue_growth_yoy,profit_growth_yoy,debt_equity,current_ratio,fcf_cr,
                 pe_ratio,pb_ratio,peg_ratio,dividend_yield,promoter_hold,fundamental_score,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,quarter) DO UPDATE SET
                roe=excluded.roe,eps_growth_yoy=excluded.eps_growth_yoy,
                revenue_growth_yoy=excluded.revenue_growth_yoy,debt_equity=excluded.debt_equity,
                pe_ratio=excluded.pe_ratio,fundamental_score=excluded.fundamental_score""",
                (sym,q,data.get("report_date"),data.get("roe"),data.get("net_margin"),
                 data.get("operating_margin"),data.get("eps_ttm"),data.get("eps_growth_yoy"),
                 data.get("revenue_growth_yoy"),data.get("profit_growth_yoy"),data.get("debt_equity"),
                 data.get("current_ratio"),data.get("fcf_cr"),data.get("pe_ratio"),data.get("pb_ratio"),
                 data.get("peg_ratio"),data.get("dividend_yield"),data.get("promoter_hold"),
                 data.get("fundamental_score"),data.get("source","alpha_vantage")))
            count+=1; time.sleep(0.5)
        except Exception as e: log.warning(f"  {sym}: {e}")
    conn.commit(); conn.close(); result["rows"]=count
    log.info(f"  ✓ Fundamentals stored: {count}"); log_job("fundamentals","SUCCESS",count)
    return result

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--symbols",nargs="+"); ap.add_argument("--all",action="store_true")
    args=ap.parse_args(); run_fundamentals_pipeline(args.symbols)
