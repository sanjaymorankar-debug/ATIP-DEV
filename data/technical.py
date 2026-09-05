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

_FAILED_INDICATORS = {}   # label -> count, for the end-of-run summary

def _step(symbol, label, fn):
    """
    Run one indicator in isolation.

    Every indicator used to live inside a SINGLE try/except, so the first
    failure aborted the rest of the block. Measured on this database: ADX threw
    for every symbol, which silently cost the 23 indicators computed after it —
    adx_14, all EMAs, SMA-200, ATR, Bollinger, OBV, volume_ratio, pivots, Fib
    levels, above_200dma, the crosses and gap_pct were NULL in 100% of 2,134
    rows, while rsi_14 and macd (computed BEFORE adx) were 100% populated.

    That one cascade is what starved most of the scoring engine: ZPI lost
    Support/Trend/ATR/Volume/Resistance and settled ~43 against a BUY gate of
    55, so the Decision Engine never emitted a single BUY. Per-indicator
    isolation means one broken library call costs one indicator, not the rest.
    """
    try:
        return fn()
    except Exception as e:
        _FAILED_INDICATORS[label] = _FAILED_INDICATORS.get(label, 0) + 1
        log.debug(f"  {symbol} {label}: {e}")
        return None

def _numeric(s):
    """
    Force a price/volume column to float64.

    Why this is load-bearing: prices_daily.adj_close is never populated, so
    sqlite hands pandas a column of all-NULL, which pandas 3.x types as
    `object`. `df["adj_close"].fillna(df["close"])` then returns an OBJECT
    dtype Series (the fill values are floats, but the dtype is not promoted).

    Downstream, the `ta` library's ADXIndicator does close.shift(1) and feeds
    the result to numpy's amax via _get_min_max(). On object dtype, shift()
    inserts None rather than NaN, and numpy compares float >= None and raises

        TypeError: '>=' not supported between instances of 'float' and 'NoneType'

    which is exactly the error every symbol hit. RSI/MACD/ATR/Bollinger/OBV
    survived it because they don't route through numpy's amax — which is why
    the symptom looked like "ADX is broken" rather than "the dtype is wrong".
    """
    return pd.to_numeric(s, errors="coerce")

def compute_indicators(symbol, df):
    if not HAS_TA or len(df)<20: return {}
    df=df.sort_values("date").copy()
    close=_numeric(df["adj_close"]).fillna(_numeric(df["close"]))
    high=_numeric(df["high"]); low=_numeric(df["low"])
    open_=_numeric(df["open"]); vol=_numeric(df["volume"]).fillna(0)
    r={}

    rsi=_step(symbol,"rsi",lambda: ta.rsi(close,length=14))
    if rsi is not None: r["rsi_14"]=safe_val(rsi.iloc[-1])

    macd_df=_step(symbol,"macd",lambda: ta.macd(close,fast=12,slow=26,signal=9))
    if macd_df is not None and not macd_df.empty:
        r["macd_line"]=safe_val(macd_df.iloc[-1,0]); r["macd_signal"]=safe_val(macd_df.iloc[-1,2]); r["macd_hist"]=safe_val(macd_df.iloc[-1,1])

    adx_df=_step(symbol,"adx",lambda: ta.adx(high,low,close,length=14))
    if adx_df is not None and not getattr(adx_df,"empty",True):
        r["adx_14"]=safe_val(adx_df.iloc[-1,0])

    e9=_step(symbol,"ema_9",lambda: ta.ema(close,length=9))
    if e9 is not None: r["ema_9"]=safe_val(e9.iloc[-1])
    e21=_step(symbol,"ema_21",lambda: ta.ema(close,length=21))
    if e21 is not None: r["ema_21"]=safe_val(e21.iloc[-1])
    if len(df)>=50:
        e50=_step(symbol,"ema_50",lambda: ta.ema(close,length=50))
        if e50 is not None: r["ema_50"]=safe_val(e50.iloc[-1])
    if len(df)>=200:
        s200=_step(symbol,"sma_200",lambda: ta.sma(close,length=200))
        if s200 is not None: r["sma_200"]=safe_val(s200.iloc[-1])

    atr=_step(symbol,"atr",lambda: ta.atr(high,low,close,length=14))
    if atr is not None:
        a=safe_val(atr.iloc[-1]); cmp_=safe_val(close.iloc[-1])
        r["atr_14"]=a; r["atr_pct"]=round(a/cmp_*100,3) if (a and cmp_) else None

    bb=_step(symbol,"bbands",lambda: ta.bbands(close,length=20,std=2))
    if bb is not None and not getattr(bb,"empty",True):
        r["bb_upper"]=safe_val(bb.iloc[-1,0]); r["bb_mid"]=safe_val(bb.iloc[-1,1]); r["bb_lower"]=safe_val(bb.iloc[-1,2])
        if all([r.get("bb_upper"),r.get("bb_lower"),r.get("bb_mid")]):
            r["bb_width"]=round((r["bb_upper"]-r["bb_lower"])/r["bb_mid"]*100,3)

    obv=_step(symbol,"obv",lambda: ta.obv(close,vol))
    if obv is not None: r["obv"]=safe_val(obv.iloc[-1])

    vsma=_step(symbol,"volume_sma",lambda: ta.sma(vol,length=20))
    if vsma is not None:
        vs=safe_val(vsma.iloc[-1]); r["volume_sma20"]=vs
        tv=safe_val(vol.iloc[-1]); r["volume_ratio"]=round(tv/vs,3) if (vs and vs>0) else None
        # rel_volume had a schema column that nothing ever wrote. Same-weekday
        # average is the comparison the doc specified; falls back to the plain
        # 20-day ratio when there isn't enough same-weekday history.
        try:
            dts=pd.to_datetime(df["date"]); dow=dts.iloc[-1].dayofweek
            same=vol[dts.dt.dayofweek==dow]
            if len(same)>=4:
                base=safe_val(same.iloc[:-1].tail(8).mean())
                if base and base>0: r["rel_volume"]=round(tv/base,3)
        except Exception as e:
            log.debug(f"  {symbol} rel_volume: {e}")

    def _pivots():
        ph=safe_val(high.iloc[-2]); pl=safe_val(low.iloc[-2]); pc=safe_val(close.iloc[-2])
        if not all([ph,pl,pc]): return None
        pv=(ph+pl+pc)/3
        return {"pivot":round(pv,2),"r1":round(2*pv-pl,2),"r2":round(pv+(ph-pl),2),
                "s1":round(2*pv-ph,2),"s2":round(pv-(ph-pl),2)}
    if len(df)>=2:
        piv=_step(symbol,"pivots",_pivots)
        if piv: r.update(piv)

    def _fib():
        hi52=safe_val(close.tail(252).max()); lo52=safe_val(close.tail(252).min())
        if not (hi52 and lo52 and hi52>lo52): return None
        rng=hi52-lo52
        return {"fib_236":round(hi52-0.236*rng,2),"fib_382":round(hi52-0.382*rng,2),
                "fib_500":round(hi52-0.500*rng,2),"fib_618":round(hi52-0.618*rng,2)}
    fib=_step(symbol,"fib",_fib)
    if fib: r.update(fib)

    cmp_=safe_val(close.iloc[-1]); sma200=r.get("sma_200")
    r["above_200dma"]=int(cmp_>sma200) if (cmp_ and sma200) else 0
    r["golden_cross"]=0; r["death_cross"]=0
    if len(df)>=210 and r.get("ema_50") and r.get("sma_200"):
        def _crosses():
            e50s=ta.ema(close,length=50).tail(10); s200s=ta.sma(close,length=200).tail(10)
            pa=(e50s.iloc[-6]>s200s.iloc[-6]); ca=(e50s.iloc[-1]>s200s.iloc[-1])
            return {"golden_cross":int(not pa and ca),"death_cross":int(pa and not ca)}
        cr=_step(symbol,"crosses",_crosses)
        if cr: r.update(cr)

    if len(df)>=2:
        def _gap():
            pc2=safe_val(close.iloc[-2]); to=safe_val(open_.iloc[-1])
            return round((to-pc2)/pc2*100,3) if (pc2 and to) else None
        g=_step(symbol,"gap",_gap)
        if g is not None: r["gap_pct"]=g

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
                # Refresh EVERY field on conflict. The old clause updated only
                # 4 of the 37 (tech_score, rsi_14, macd_hist, adx_14), so a
                # re-run could never repair the other 33 — once a row was
                # written with NULLs (e.g. by the ADX cascade bug above) those
                # NULLs were permanent for that symbol/date.
                updates=",".join(f"{f}=excluded.{f}" for f in fields)
                conn.execute(f"INSERT INTO technical_indicators (symbol,date,{','.join(fields)}) "
                             f"VALUES ({','.join(['?']*(len(fields)+2))}) "
                             f"ON CONFLICT(symbol,date) DO UPDATE SET {updates}",vals)
                count+=1
            except Exception as e: log.warning(f"  {sym}: {e}")
        conn.commit(); result["rows"]=count
        log.info(f"  ✓ Technical done: {count} stocks")
        # Which indicators failed, and how often. Previously a library call that
        # broke for every symbol produced no visible signal at all — it just
        # left columns NULL, and the scores quietly degraded around them.
        if _FAILED_INDICATORS:
            summary=", ".join(f"{k}×{v}" for k,v in sorted(_FAILED_INDICATORS.items(),
                                                            key=lambda kv:-kv[1]))
            log.warning(f"  ⚠ Indicators that failed this run ({TA_LIB}): {summary}")
            result["failed_indicators"]=dict(_FAILED_INDICATORS)
            _FAILED_INDICATORS.clear()
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
