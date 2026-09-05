"""ATIP — Technical Indicators Engine (35 indicators via pandas-ta)"""
import logging, argparse
import numpy as np, pandas as pd
from datetime import date, datetime
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)
# Try pandas-ta first, fall back to 'ta' library (pure Python, no numba)
try:
    import pandas_ta as ta
    HAS_TA = True
    TA_LIB = "pandas_ta"
except (ImportError, Exception):
    try:
        import ta as ta_lib
        # Wrap ta library to match pandas-ta API
        class _TaWrapper:
            def rsi(self, close, length=14):
                return ta_lib.momentum.RSIIndicator(close, window=length).rsi()
            def ema(self, close, length=9):
                return close.ewm(span=length, adjust=False).mean()
            def sma(self, close, length=20):
                return close.rolling(window=length).mean()
            def macd(self, close, fast=12, slow=26, signal=9):
                import pandas as pd
                m = ta_lib.trend.MACD(close, window_fast=fast, window_slow=slow, window_sign=signal)
                return pd.DataFrame({"MACD":m.macd(),"Signal":m.macd_signal(),"Hist":m.macd_diff()})
            def adx(self, high, low, close, length=14):
                import pandas as pd
                a = ta_lib.trend.ADXIndicator(high, low, close, window=length)
                return pd.DataFrame({"ADX":a.adx()})
            def atr(self, high, low, close, length=14):
                return ta_lib.volatility.AverageTrueRange(high, low, close, window=length).average_true_range()
            def bbands(self, close, length=20, std=2):
                import pandas as pd
                b = ta_lib.volatility.BollingerBands(close, window=length, window_dev=std)
                return pd.DataFrame({"Upper":b.bollinger_hband(),"Mid":b.bollinger_mavg(),"Lower":b.bollinger_lband()})
            def obv(self, close, volume):
                return ta_lib.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
            def stoch(self, high, low, close, k=14, d=3):
                import pandas as pd
                s = ta_lib.momentum.StochasticOscillator(high, low, close, window=k, smooth_window=d)
                return pd.DataFrame({"K":s.stoch(),"D":s.stoch_signal()})
            def willr(self, high, low, close, length=14):
                return ta_lib.momentum.WilliamsRIndicator(high, low, close, lbp=length).williams_r()
            def cci(self, high, low, close, length=20):
                return ta_lib.trend.CCIIndicator(high, low, close, window=length).cci()
        ta = _TaWrapper()
        HAS_TA = True
        TA_LIB = "ta"
        log.info("  Using 'ta' library (pandas-ta fallback)")
    except ImportError:
        HAS_TA = False
        TA_LIB = "none"
        log.warning("No TA library found. Run: pip install ta   OR   pip install pandas-ta --no-deps")

def safe_val(val, default=None):
    if val is None: return default
    try:
        f = float(val)
        return default if (f!=f) else round(f,4)
    except: return default

def compute_beta(symbol, conn, benchmark_symbol="NIFTY50", lookback=252, min_points=60):
    """
    1-year beta vs Nifty 50: cov(stock daily returns, benchmark daily
    returns) / var(benchmark daily returns), using up to `lookback` trading
    days of overlapping history. Returns None (not 0 or 1) when there isn't
    enough overlapping data to compute a meaningful figure — a missing beta
    should read as "unknown", not "market-neutral" or "no data available".

    Requires data.dhan.sync_index_benchmark_history() to have already
    populated prices_daily for `benchmark_symbol` (default: a synthetic
    "NIFTY50" row set stored the same way as any tracked stock).
    """
    try:
        stock_rows = conn.execute(
            "SELECT date, close FROM prices_daily WHERE symbol=? ORDER BY date DESC LIMIT ?",
            (symbol, lookback + 1)
        ).fetchall()
        bench_rows = conn.execute(
            "SELECT date, close FROM prices_daily WHERE symbol=? ORDER BY date DESC LIMIT ?",
            (benchmark_symbol, lookback + 1)
        ).fetchall()
        if len(stock_rows) < min_points + 1 or len(bench_rows) < min_points + 1:
            return None

        s = pd.DataFrame(stock_rows, columns=["date", "close"]).set_index("date").sort_index()
        b = pd.DataFrame(bench_rows, columns=["date", "close"]).set_index("date").sort_index()
        merged = s.join(b, how="inner", lsuffix="_s", rsuffix="_b").dropna()
        if len(merged) < min_points + 1:
            return None

        s_ret = merged["close_s"].pct_change().dropna()
        b_ret = merged["close_b"].pct_change().dropna()
        if len(s_ret) < min_points or b_ret.var() == 0:
            return None

        beta = float(np.cov(s_ret, b_ret)[0, 1] / b_ret.var())
        return safe_val(beta)
    except Exception:
        return None

def compute_indicators(symbol, df):
    if not HAS_TA or len(df)<20: return {}
    df=df.sort_values("date").copy()
    close=df["adj_close"].fillna(df["close"]); high=df["high"]; low=df["low"]
    open_=df["open"]; vol=df["volume"].fillna(0)
    r={}
    try:
        rsi=ta.rsi(close,length=14); r["rsi_14"]=safe_val(rsi.iloc[-1])
        macd_df=ta.macd(close,fast=12,slow=26,signal=9)
        if macd_df is not None and not macd_df.empty:
            r["macd_line"]=safe_val(macd_df.iloc[-1,0]); r["macd_signal"]=safe_val(macd_df.iloc[-1,2]); r["macd_hist"]=safe_val(macd_df.iloc[-1,1])
        adx_df=ta.adx(high,low,close,length=14)
        if adx_df is not None and not adx_df.empty: r["adx_14"]=safe_val(adx_df.iloc[-1,0])
        r["ema_9"]=safe_val(ta.ema(close,length=9).iloc[-1])
        r["ema_21"]=safe_val(ta.ema(close,length=21).iloc[-1])
        if len(df)>=50:  r["ema_50"]=safe_val(ta.ema(close,length=50).iloc[-1])
        if len(df)>=200: r["sma_200"]=safe_val(ta.sma(close,length=200).iloc[-1])
        atr=ta.atr(high,low,close,length=14)
        if atr is not None:
            a=safe_val(atr.iloc[-1]); cmp=safe_val(close.iloc[-1])
            r["atr_14"]=a; r["atr_pct"]=round(a/cmp*100,3) if (a and cmp) else None
        bb=ta.bbands(close,length=20,std=2)
        if bb is not None and not bb.empty:
            r["bb_upper"]=safe_val(bb.iloc[-1,0]); r["bb_mid"]=safe_val(bb.iloc[-1,1]); r["bb_lower"]=safe_val(bb.iloc[-1,2])
            if all([r.get("bb_upper"),r.get("bb_lower"),r.get("bb_mid")]):
                r["bb_width"]=round((r["bb_upper"]-r["bb_lower"])/r["bb_mid"]*100,3)
        obv=ta.obv(close,vol)
        if obv is not None: r["obv"]=safe_val(obv.iloc[-1])
        vsma=ta.sma(vol,length=20)
        if vsma is not None:
            vs=safe_val(vsma.iloc[-1]); r["volume_sma20"]=vs
            tv=safe_val(vol.iloc[-1]); r["volume_ratio"]=round(tv/vs,3) if (vs and vs>0) else None
        if len(df)>=2:
            ph=safe_val(high.iloc[-2]); pl=safe_val(low.iloc[-2]); pc=safe_val(close.iloc[-2])
            if all([ph,pl,pc]):
                pv=(ph+pl+pc)/3; r["pivot"]=round(pv,2); r["r1"]=round(2*pv-pl,2); r["r2"]=round(pv+(ph-pl),2); r["s1"]=round(2*pv-ph,2); r["s2"]=round(pv-(ph-pl),2)
        hi52=safe_val(close.tail(252).max()); lo52=safe_val(close.tail(252).min())
        if hi52 and lo52 and hi52>lo52:
            rng=hi52-lo52; r["fib_236"]=round(hi52-0.236*rng,2); r["fib_382"]=round(hi52-0.382*rng,2)
            r["fib_500"]=round(hi52-0.500*rng,2); r["fib_618"]=round(hi52-0.618*rng,2)
        cmp=safe_val(close.iloc[-1]); sma200=r.get("sma_200")
        r["above_200dma"]=int(cmp>sma200) if (cmp and sma200) else 0
        r["golden_cross"]=0; r["death_cross"]=0
        if len(df)>=210 and r.get("ema_50") and r.get("sma_200"):
            e50s=ta.ema(close,length=50).tail(10); s200s=ta.sma(close,length=200).tail(10)
            pa=(e50s.iloc[-6]>s200s.iloc[-6]); ca=(e50s.iloc[-1]>s200s.iloc[-1])
            r["golden_cross"]=int(not pa and ca); r["death_cross"]=int(pa and not ca)
        if len(df)>=2:
            pc2=safe_val(close.iloc[-2]); to=safe_val(open_.iloc[-1])
            if pc2 and to: r["gap_pct"]=round((to-pc2)/pc2*100,3)
    except Exception as e:
        log.warning(f"  {symbol} indicators: {e}")
    return r

def compute_tech_score(ind):
    scores={}
    rsi=ind.get("rsi_14")
    if rsi is not None:
        if rsi<30: scores["RSI"]=70
        elif rsi<50: scores["RSI"]=60
        elif rsi<65: scores["RSI"]=80
        elif rsi<75: scores["RSI"]=55
        else: scores["RSI"]=30
    hist=ind.get("macd_hist")
    if hist is not None: scores["MACD"]=min(50+abs(hist)*10,100) if hist>0 else max(0,50-abs(hist)*10)
    adx=ind.get("adx_14")
    if adx is not None: scores["ADX"]=90 if adx>40 else 75 if adx>25 else 50 if adx>15 else 25
    atr_pct=ind.get("atr_pct")
    if atr_pct: scores["ATR"]=min(atr_pct*10,100)
    e9,e21,e50=ind.get("ema_9"),ind.get("ema_21"),ind.get("ema_50")
    if all([e9,e21,e50]):
        scores["EMA"]=85 if e9>e21>e50 else 65 if e9>e21 else 15 if e9<e21<e50 else 40
    scores["Trend"]=80 if ind.get("above_200dma") else 20
    if ind.get("golden_cross"): scores["SR"]=90
    elif ind.get("death_cross"): scores["SR"]=10
    else: scores["SR"]=50
    vr=ind.get("volume_ratio")
    if vr: scores["Vol"]=min(vr*40,100)
    if not scores: return 50.0
    weights={"RSI":0.12,"MACD":0.12,"ADX":0.10,"ATR":0.10,"EMA":0.12,"Trend":0.15,"SR":0.12,"Vol":0.12}
    tw=sum(weights[k] for k in scores); ts=sum(scores[k]*weights[k] for k in scores)
    return round(ts/tw,2) if tw>0 else 50.0

def run_technical_pipeline(trade_date=None, symbol=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection(); result={"date":str(trade_date),"rows":0,"status":"SUCCESS"}
    try:
        if symbol: symbols=[symbol]
        else:
            rows=conn.execute("SELECT DISTINCT symbol FROM prices_daily WHERE date<=? ORDER BY symbol",(str(trade_date),)).fetchall()
            symbols=[r["symbol"] for r in rows]
        log.info(f"⚙️  Technical indicators for {len(symbols)} stocks")
        count=0
        for sym in symbols:
            try:
                df=pd.read_sql("SELECT date,open,high,low,close,adj_close,volume FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 260",
                               conn,params=(sym,str(trade_date)))
                if df.empty or len(df)<14: continue
                df=df.sort_values("date"); ind=compute_indicators(sym,df)
                if not ind: continue
                ind["tech_score"]=compute_tech_score(ind)
                fields=["rsi_14","stoch_k","stoch_d","williams_r","cci_20","macd_line","macd_signal","macd_hist","adx_14",
                        "ema_9","ema_21","ema_50","sma_200","atr_14","atr_pct","bb_upper","bb_lower","bb_mid","bb_width",
                        "obv","volume_sma20","volume_ratio","rel_volume","pivot","r1","r2","s1","s2",
                        "fib_236","fib_382","fib_500","fib_618","golden_cross","death_cross","above_200dma","gap_pct","tech_score"]
                vals=[sym,str(trade_date)]+[ind.get(f) for f in fields]
                conn.execute(f"INSERT INTO technical_indicators (symbol,date,{','.join(fields)}) VALUES ({','.join(['?']*(len(fields)+2))}) ON CONFLICT(symbol,date) DO UPDATE SET tech_score=excluded.tech_score,rsi_14=excluded.rsi_14,macd_hist=excluded.macd_hist,adx_14=excluded.adx_14",vals)
                count+=1
            except Exception as e: log.warning(f"  {sym}: {e}")
        conn.commit(); result["rows"]=count
        log.info(f"  ✓ Technical done: {count} stocks")
        log_job("technical","SUCCESS",count,run_date=trade_date)
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; log.error(f"  ✗ {e}")
        log_job("technical","FAILED",0,error=e,run_date=trade_date)
    finally: conn.close()
    return result

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--date"); ap.add_argument("--symbol")
    args=ap.parse_args()
    td=datetime.strptime(args.date,"%Y-%m-%d").date() if args.date else date.today()
    run_technical_pipeline(td, args.symbol)
