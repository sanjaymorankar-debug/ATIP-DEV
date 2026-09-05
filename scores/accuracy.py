"""ATIP — Accuracy Tracker: prediction vs actual 5/10/20-day outcomes"""
import logging, argparse
import pandas as pd
from datetime import date, datetime, timedelta
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

def _trading_days_back(from_date, n):
    d=from_date; count=0
    while count<n: d-=timedelta(days=1); count+=1 if d.weekday()<5 else 0
    return d

def update_accuracy(target_date=None):
    if target_date is None: target_date=date.today()
    conn=get_connection(); updated=0
    try:
        for days_back in [5,10,20]:
            pred_date=str(_trading_days_back(target_date,days_back))
            preds=conn.execute("SELECT symbol,signal,entry_price,stop_loss,target_1 FROM predictions WHERE pred_date=? AND entry_price IS NOT NULL",(pred_date,)).fetchall()
            for p in preds:
                price_row=conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",(p["symbol"],str(target_date))).fetchone()
                if not price_row or not p["entry_price"]: continue
                ap=price_row["close"]; ret=round((ap-p["entry_price"])/p["entry_price"]*100,3)
                correct=int((ret>0 and p["signal"]=="BUY")or(ret<0 and p["signal"]=="SELL")or(abs(ret)<2 and p["signal"] in("HOLD","WAIT")))
                ht=int(bool(p["target_1"] and ap>=p["target_1"])); hsl=int(bool(p["stop_loss"] and ap<=p["stop_loss"]))
                conn.execute(f"""INSERT INTO accuracy_tracker(pred_date,symbol,signal,entry_price,price_{days_back}d,return_{days_back}d,correct_{days_back}d,hit_target_1,hit_stop_loss)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(pred_date,symbol) DO UPDATE SET price_{days_back}d=excluded.price_{days_back}d,return_{days_back}d=excluded.return_{days_back}d,correct_{days_back}d=excluded.correct_{days_back}d,hit_target_1=excluded.hit_target_1,hit_stop_loss=excluded.hit_stop_loss""",
                    (pred_date,p["symbol"],p["signal"],p["entry_price"],ap,ret,correct,ht,hsl))
                updated+=1
        conn.commit(); log.info(f"  ✓ Accuracy: {updated} outcomes updated"); log_job("accuracy","SUCCESS",updated)
    except Exception as e: conn.rollback(); log.error(f"  ✗ {e}"); log_job("accuracy","FAILED",0,error=e)
    finally: conn.close()
    return updated

def generate_report(symbol=None, days_back=90):
    conn=get_connection(); since=str(date.today()-timedelta(days=days_back))
    where=f"AND p.symbol='{symbol}'" if symbol else ""
    rows=conn.execute(f"""SELECT a.symbol,a.signal,a.return_5d,a.correct_5d,a.return_10d,a.correct_10d,a.return_20d,a.correct_20d,a.hit_target_1,a.hit_stop_loss
        FROM accuracy_tracker a JOIN predictions p ON a.pred_date=p.pred_date AND a.symbol=p.symbol WHERE a.pred_date>=? {where}""",(since,)).fetchall()
    conn.close()
    if not rows: return {"error":"No accuracy data yet."}
    df=pd.DataFrame([dict(r) for r in rows])
    rep={"total":len(df),"period_days":days_back,"symbol":symbol or "ALL"}
    for h in [5,10,20]:
        cc=f"correct_{h}d"; rc=f"return_{h}d"
        if cc in df.columns:
            v=df[df[cc].notna()]
            if len(v)>0:
                rep[f"accuracy_{h}d"]=round(v[cc].mean()*100,1); rep[f"avg_return_{h}d"]=round(v[rc].mean(),2)
                rep[f"win_rate_{h}d"]=round((v[rc]>0).mean()*100,1)
    return rep

def print_report(symbol=None):
    rep=generate_report(symbol)
    print(f"\n{'='*50}\n  ATIP Accuracy — {rep.get('symbol')} ({rep.get('period_days')}d)\n  {rep.get('total',0)} predictions\n{'='*50}")
    for h in [5,10,20]:
        acc=rep.get(f"accuracy_{h}d"); avg=rep.get(f"avg_return_{h}d"); wr=rep.get(f"win_rate_{h}d")
        if acc: print(f"\n  {h:2d}d: {'✅' if acc>=55 else '⚠️' if acc>=45 else '❌'} Accuracy:{acc:.1f}%  Avg:{avg:+.2f}%  WinRate:{wr:.1f}%")
    print()

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser()
    ap.add_argument("--update",action="store_true"); ap.add_argument("--report",action="store_true"); ap.add_argument("--symbol")
    args=ap.parse_args()
    if args.update: print(f"Updated {update_accuracy()} records")
    else: print_report(args.symbol)
