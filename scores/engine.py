"""ATIP — AI Scoring Engine: VPI, MRI, RRI, CRI, ZPI, MSI, MH, ACS, ATIP Master"""
import logging, argparse
import numpy as np, pandas as pd
from datetime import date, datetime
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

def load_weights(conn, index_name):
    rows=conn.execute("SELECT variable,weight FROM weight_config WHERE index_name=? AND active=1",(index_name,)).fetchall()
    return {r["variable"]:r["weight"] for r in rows}

def minmax(val, mn, mx, invert=False):
    if val is None or mn==mx: return 50.0
    s=max(0.0,min(100.0,(val-mn)/(mx-mn)*100))
    return round(100.0-s if invert else s,2)

def weighted_score(components, weights):
    total_w=total_s=0.0
    for k,w in weights.items():
        v=components.get(k)
        if v is not None: total_s+=v*w; total_w+=w
    return round(total_s/total_w,2) if total_w>0 else 50.0

def get_tech(sym,td,conn):
    r=conn.execute("SELECT * FROM technical_indicators WHERE symbol=? AND date=?",(sym,str(td))).fetchone()
    return dict(r) if r else {}

def get_prices(sym,td,conn,n=260):
    return pd.read_sql("SELECT * FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?"
                       ,conn,params=(sym,str(td),n))

def get_fund(sym,conn):
    r=conn.execute("SELECT * FROM fundamental_data WHERE symbol=? ORDER BY report_date DESC LIMIT 1",(sym,)).fetchone()
    return dict(r) if r else {}

def get_inst(sym,td,conn):
    return pd.read_sql("SELECT * FROM institutional_data WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 30",conn,params=(sym,str(td)))

def get_ns(sym,td,conn):
    from datetime import timedelta
    since=str((datetime.strptime(str(td),"%Y-%m-%d")-timedelta(days=7)).date())
    r=conn.execute("SELECT AVG(news_score) as ns FROM news_articles WHERE symbols_mentioned LIKE ? AND fetched_at>=?",(f'%\"{sym}\"%',since)).fetchone()
    return float(r["ns"]) if r and r["ns"] else 50.0

def get_fii(td,conn):
    r=conn.execute("SELECT * FROM fii_dii_market WHERE date<=? ORDER BY date DESC LIMIT 1",(str(td),)).fetchone()
    return dict(r) if r else {}

def get_idx(td,conn):
    r=conn.execute("SELECT * FROM index_levels WHERE date=? ORDER BY time DESC LIMIT 1",(str(td),)).fetchone()
    return dict(r) if r else {}

def get_vix(td,conn):
    """
    India VIX for session td, or None when ATIP has no VIX for that session.

    The session's own latest index_levels value (the live feed, or NSE's close
    snapshot) and otherwise the INDIAVIX daily close in prices_daily (NSE's
    index file / Dhan history). Never another session's value, and never a
    stand-in: compute_mh/compute_msi/compute_cri used to read `or 15`, so a
    session with no index row was scored as if VIX were a calm 15. A missing
    VIX now leaves its component out, and weighted_score renormalises.
    """
    r=conn.execute("SELECT india_vix FROM index_levels WHERE date=? AND india_vix>0 "
                   "ORDER BY time DESC LIMIT 1",(str(td),)).fetchone()
    if r: return float(r[0])
    from data.dhan import VIX_SYMBOL
    r=conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date=? AND close>0",
                   (VIX_SYMBOL,str(td))).fetchone()
    return float(r[0]) if r else None

def get_global(td,conn):
    r=conn.execute("SELECT * FROM global_markets WHERE date<=? ORDER BY date DESC LIMIT 1",(str(td),)).fetchone()
    return dict(r) if r else {}

def get_bulk(sym,td,conn,days=10):
    """Trailing net bulk/block-deal value (₹Cr) for a stock — sparse data,
    so summed over a trailing window rather than a single day. Feeds
    compute_ins()'s BulkDeals component. Source: data/bhavcopy.py
    :download_bulk_block_deals() / run_bulk_deals_pipeline()."""
    from datetime import timedelta
    since=str((datetime.strptime(str(td),"%Y-%m-%d")-timedelta(days=days)).date())
    r=conn.execute("SELECT SUM(net_value_cr) as v FROM bulk_deals WHERE symbol=? AND date>=? AND date<=?",
                   (sym,since,str(td))).fetchone()
    return {"net_value_cr":float(r["v"])} if r and r["v"] is not None else {}

def get_news_confidence(sym,td,conn):
    """
    Average AI news-classification confidence for this stock over the 7 days
    to td, scaled 0-100; else the market-wide figure for td and the day
    before; else None, and ACS's NewsConfidence is left out.

    Only articles classifier='claude' count. The rule-based fallback writes a
    fixed 0.5 (data/news.py), and with the Anthropic key never set every one
    of the 1,249 stored articles carried it -- so NewsConfidence was a
    constant 50 wearing the name of a measurement, as was the old last-resort
    return value. Nothing after td is read: the windows had no upper bound, so
    re-scoring a past session read news published after it.
    """
    from datetime import timedelta
    day=datetime.strptime(str(td),"%Y-%m-%d")
    until=str((day+timedelta(days=1)).date())
    since=str((day-timedelta(days=7)).date())
    r=conn.execute("SELECT AVG(confidence) as c FROM news_articles WHERE classifier='claude' "
                   "AND symbols_mentioned LIKE ? AND fetched_at>=? AND fetched_at<?",
                   (f'%\"{sym}\"%',since,until)).fetchone()
    if r and r["c"] is not None: return round(float(r["c"])*100,2)
    since24=str((day-timedelta(days=1)).date())
    r2=conn.execute("SELECT AVG(confidence) as c FROM news_articles WHERE classifier='claude' "
                    "AND fetched_at>=? AND fetched_at<?",(since24,until)).fetchone()
    if r2 and r2["c"] is not None: return round(float(r2["c"])*100,2)
    return None

# ACS HistoricalAccuracy: the stock's 5-session hit rate over the last 90 days
# (weight_config: "Past accuracy 90d"), from predictions whose outcome was
# already known on the day being scored.
ACS_ACCURACY_WINDOW_DAYS = 90
ACS_ACCURACY_HORIZON = 5        # accuracy_tracker.correct_5d
ACS_ACCURACY_MIN_OUTCOMES = 5

def sessions_back(td, n):
    """The session n NSE sessions before td (td itself need not be one)."""
    from datetime import timedelta
    from utils.trading_calendar import is_trading_day
    d=datetime.strptime(str(td),"%Y-%m-%d").date(); count=0
    while count<n:
        d-=timedelta(days=1)
        if is_trading_day(d): count+=1
    return d

def get_historical_accuracy(sym,td,conn):
    """
    % of the stock's predictions that were right 5 sessions later, over the 90
    days to td, counting only predictions made at least 5 sessions before td --
    the ones whose outcome td's close had already decided. None with fewer
    than ACS_ACCURACY_MIN_OUTCOMES.

    The query this replaces had no date at all: it averaged every outcome ever
    recorded for the symbol, so a re-score of a past session used outcomes
    that session could not have known, and 90 days was never applied. It also
    returned 50.0 for no data -- and for a stock that was never right, 0.0
    being falsy.
    """
    from datetime import timedelta
    known_by=sessions_back(td,ACS_ACCURACY_HORIZON)
    since=datetime.strptime(str(td),"%Y-%m-%d").date()-timedelta(days=ACS_ACCURACY_WINDOW_DAYS)
    r=conn.execute("SELECT AVG(correct_5d)*100 AS acc, COUNT(correct_5d) AS n FROM accuracy_tracker "
                   "WHERE symbol=? AND pred_date>=? AND pred_date<=? AND correct_5d IS NOT NULL",
                   (sym,str(since),str(known_by))).fetchone()
    if not r or r["n"]<ACS_ACCURACY_MIN_OUTCOMES: return None
    return round(float(r["acc"]),2)

def get_index_change(td,conn,key,idx=None):
    """
    % change of index `key` (nifty50, banknifty, midcap150, smallcap250) on
    session td: the session's index_levels value (live feed or NSE's close),
    else its daily series in prices_daily (data.dhan.INDEX_SERIES_SYMBOLS),
    close over the previous session's close. None when neither has it --
    compute_mh used to read `or 0`, scoring a missing index as a flat day.
    """
    if idx is None: idx=get_idx(td,conn)
    v=idx.get(f"{key}_chg")
    if v is not None: return float(v)
    from data.dhan import INDEX_SERIES_SYMBOLS
    from utils.trading_calendar import last_trading_day
    from datetime import timedelta
    sym=INDEX_SERIES_SYMBOLS.get(key)
    if not sym: return None
    rows=conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 "
                      "ORDER BY date DESC LIMIT 2",(sym,str(td))).fetchall()
    if len(rows)<2 or str(rows[0][0])!=str(td): return None
    prev=last_trading_day(datetime.strptime(str(td),"%Y-%m-%d").date()-timedelta(days=1))
    if str(rows[1][0])!=str(prev): return None      # a gap in the series is not a one-day change
    return round((rows[0][1]/rows[1][1]-1)*100,3)

# ── Market breadth (DP-17) ─────────────────────────────────────────────────
# Computed from prices_daily over the tracked universe (Nifty 500 + holdings).
# Before this nothing ever wrote a breadth figure: compute_mh read
# fii_dii_market.adv_decline, NULL in every row, as 1.0, and scored BOTH its
# Breadth (0.10) and AdvanceDecline (0.05) components as minmax(1.0, 0.3, 3) =
# 25.93 every session -- 15% of Market Health pinned to a bearish constant.
# The universe is today's constituent list, so older sessions carry its
# survivorship bias (BT-15).
BREADTH_MIN_COVERAGE = 0.80   # share of the universe a figure must be measured over
BREADTH_SMA = 200
BREADTH_52W = 252             # sessions in a 52-week range, the session included

def breadth_series(conn,start,end,universe=None):
    """
    {date string: breadth dict} for every session from start to end:
      advances / declines   closes above / below the previous session's close
      pct_advancing         advances / (advances + declines) x 100
      ad_ratio              advances / declines
      pct_above_200dma      % of stocks with 200 sessions of history closing
                            above their 200-session simple average
      new_highs / new_lows  stocks whose high (low) beat every high (low) of
                            the previous 251 sessions
      universe              stocks with a bar on the session and the one before
    A figure is None when fewer than BREADTH_MIN_COVERAGE of the universe
    could be measured for it; a session missing entirely has no entry.
    """
    if universe is None:
        from data.dhan import get_tracked_symbols
        universe=get_tracked_symbols(conn)
    universe=sorted(set(universe))
    if not universe: return {}
    from datetime import timedelta
    lo=datetime.strptime(str(start),"%Y-%m-%d").date()-timedelta(days=int(BREADTH_52W*1.6)+10)
    df=pd.read_sql(f"SELECT symbol,date,high,low,close FROM prices_daily WHERE date>=? AND date<=? "
                   f"AND close>0 AND symbol IN ({','.join('?'*len(universe))})",
                   conn,params=(str(lo),str(end),*universe))
    if df.empty: return {}
    df["date"]=df["date"].astype(str).str[:10]
    close=df.pivot(index="date",columns="symbol",values="close").sort_index()
    high=df.pivot(index="date",columns="symbol",values="high").reindex_like(close)
    low=df.pivot(index="date",columns="symbol",values="low").reindex_like(close)
    chg=close-close.shift(1)
    adv=(chg>0).sum(axis=1); dec=(chg<0).sum(axis=1); measured=chg.notna().sum(axis=1)
    sma=close.rolling(BREADTH_SMA,min_periods=BREADTH_SMA).mean()
    has_sma=sma.notna()&close.notna()
    above=((close>sma)&has_sma).sum(axis=1); n_sma=has_sma.sum(axis=1)
    lookback=BREADTH_52W-1
    prior_hi=high.shift(1).rolling(lookback,min_periods=lookback-10).max()
    prior_lo=low.shift(1).rolling(lookback,min_periods=lookback-10).min()
    n52=(prior_hi.notna()&high.notna()).sum(axis=1)
    nh=((high>prior_hi)&prior_hi.notna()).sum(axis=1); nl=((low<prior_lo)&prior_lo.notna()).sum(axis=1)
    need=BREADTH_MIN_COVERAGE*len(universe)
    out={}
    for d in close.index:
        if d<str(start) or d>str(end): continue
        if measured[d]<need:
            out[d]={"universe":int(measured[d])}; continue      # too few bars to say anything
        a,b=int(adv[d]),int(dec[d])
        out[d]={"advances":a,"declines":b,"universe":int(measured[d]),
                "pct_advancing":round(a/(a+b)*100,2) if a+b else None,
                "ad_ratio":round(a/b,3) if b else None,
                "pct_above_200dma":round(above[d]/n_sma[d]*100,2) if n_sma[d]>=need else None,
                "new_highs":int(nh[d]) if n52[d]>=need else None,
                "new_lows":int(nl[d]) if n52[d]>=need else None}
    return out

def compute_breadth(td,conn,universe=None):
    """breadth_series() for the one session td, or {} when it has no bars."""
    return breadth_series(conn,td,td,universe).get(str(td),{})

def compute_liquidity(prices):
    """20-day average traded turnover (₹, min-max scaled 0-100 the same way
    VPI's old single-day LQ was: turnover of ₹100Cr/day -> 100). Shared by
    VPI's LQ component and ACS's Liquidity component so both read one real,
    derived figure instead of ACS's old hardcoded 60.0."""
    if prices is None or prices.empty: return None
    window=prices.iloc[:min(len(prices),20)]
    turnover=(window["close"]*window["volume"]).mean()
    if turnover!=turnover: return None  # NaN
    return round(min(float(turnover)/1e9*100,100),2)

def compute_relative_strength(prices,bench_prices):
    """20-day return of the stock minus the 20-day return of the Nifty 50
    benchmark (synthetic 'NIFTY50' rows in prices_daily, populated by
    data.dhan.sync_index_benchmark_history() -- the same series
    compute_beta() already uses). Replaces VPI's RS component, which had a
    seeded weight but was never actually computed.

    The two series are matched on date, not row position. Positionally, a
    benchmark one session behind the stock -- which it always was at 16:45,
    Dhan's index history ending the day before -- compared the stock's 20
    sessions to the index's 20 sessions one day earlier."""
    if prices is None or prices.empty or len(prices)<21: return None
    if bench_prices is None or bench_prices.empty or len(bench_prices)<21: return None
    m=prices[["date","close"]].merge(bench_prices[["date","close"]],on="date",suffixes=("","_b"))
    m=m.sort_values("date",ascending=False)
    if len(m)<21: return None
    s0,s20=m["close"].iloc[0],m["close"].iloc[20]
    b0,b20=m["close_b"].iloc[0],m["close_b"].iloc[20]
    if not s20 or not b20: return None
    stock_ret=(s0/s20-1)*100; bench_ret=(b0/b20-1)*100
    return minmax(stock_ret-bench_ret,-15,15)

def compute_breakout(prices,tech):
    """Real price/volume breakout detection off the prior 20 sessions'
    closing high and the volume_ratio already computed in
    technical_indicators -- replaces Trade-of-the-Day's hardcoded
    Breakout=60 stub. No new data source needed."""
    if prices is None or prices.empty or len(prices)<21: return 50.0
    hi20=prices["close"].iloc[1:21].max(); cmp=prices["close"].iloc[0]
    if not hi20 or hi20<=0: return 50.0
    pct_above=(cmp-hi20)/hi20*100
    vr=tech.get("volume_ratio") or 1.0
    base=min(70+pct_above*10,100) if pct_above>0 else max(0.0,50+pct_above*5)
    vol_boost=min(max(vr,0.3)/1.2,1.4)
    return round(min(base*vol_boost,100),2)

def compute_sector_ranks(conn,trade_date,symbols):
    """
    Real sector-relative-performance rank per stock, replacing the
    hardcoded sector_rank=5 fed into compute_zpi()/Trade-of-Day for every
    stock. Uses the Nifty 500 constituent list's Industry column (already
    downloaded for the tracked universe -- see
    data.index_constituents.get_symbol_industry_map()) and each
    stock's 1-day price change from prices_daily: groups stocks by
    industry, ranks industries by that day's average % change, and maps
    each stock to its industry's rank. No new external data source.

    Returns (rank_map, n_industries). rank_map maps symbol -> rank
    (1 = best-performing industry that day); symbols whose industry is
    unknown or has no rank are simply absent -- callers should default
    them to a neutral score.
    """
    if not symbols: return {},0
    try:
        from data.index_constituents import get_symbol_industry_map
        ind_map=get_symbol_industry_map()
    except Exception as e:
        log.warning(f"  sector ranks: industry map unavailable ({e})"); ind_map={}
    if not ind_map: return {},0
    placeholders=",".join("?"*len(symbols))
    rows=conn.execute(
        f"SELECT symbol,date,close FROM prices_daily WHERE symbol IN ({placeholders}) AND date<=? ORDER BY symbol,date DESC",
        (*symbols,str(trade_date))).fetchall()
    if not rows: return {},0
    df=pd.DataFrame(rows,columns=["symbol","date","close"])
    chg={}
    for sym,g in df.groupby("symbol"):
        if len(g)>=2:
            g=g.sort_values("date",ascending=False)
            c0,c1=g["close"].iloc[0],g["close"].iloc[1]
            if c1: chg[sym]=(c0/c1-1)*100
    industry_chgs={}
    for sym,pct in chg.items():
        ind=ind_map.get(sym)
        if ind: industry_chgs.setdefault(ind,[]).append(pct)
    industry_avg={ind:sum(v)/len(v) for ind,v in industry_chgs.items() if v}
    if not industry_avg: return {},0
    ranked=sorted(industry_avg.items(),key=lambda kv:kv[1],reverse=True)
    industry_rank={ind:i+1 for i,(ind,_) in enumerate(ranked)}
    n=len(ranked)
    rank_map={sym:industry_rank[ind_map[sym]] for sym in symbols if ind_map.get(sym) in industry_rank}
    return rank_map,n

def sector_score(rank,n):
    """Rank -> 0-100 score by percentile-of-industries-ranked-today, so it
    stays meaningful regardless of how many industries are actually
    represented (unlike a fixed '<=3 / <=6' cutoff assuming ~12 sectors)."""
    if rank is None or not n: return 50.0
    pct=rank/n
    return 90.0 if pct<=0.25 else 60.0 if pct<=0.5 else 35.0

def compute_ins(fii,fund,bulk,weights):
    """
    Institutional Score -- was always None in run_scoring_pipeline() before
    this fix, so it silently dropped out of the ATIP Master Score despite
    having a seeded weight_config entry.

    Built from three real, already-fetched-or-newly-added sources:
      FII/DII  -- market-wide 5-day net flow (fii_dii_market, via
                  data.bhavcopy.download_fii_dii)
      Promoter -- promoter shareholding % (fundamental_data, via Alpha
                  Vantage / Screener.in)
      BulkDeals-- net bulk/block deal value for this stock, trailing 10
                  days (bulk_deals, via the new
                  data.bhavcopy.download_bulk_block_deals())

    MutualFund and Insider from the original doc formula (E17) are
    intentionally omitted rather than faked with a constant -- NSE does
    not publish free per-stock mutual-fund-flow or insider-trade data, so
    there's no real source to wire in for them yet. Their weight is
    redistributed across the three components above (see seed_weights()).
    Returns None (not a fabricated number) if none of the three inputs
    are available for this stock.
    """
    c={}
    fii_5d=fii.get("fii_5d_avg"); dii_5d=fii.get("dii_5d_avg")
    if fii_5d is not None: c["FII"]=minmax(fii_5d,-2000,2000)
    if dii_5d is not None: c["DII"]=minmax(dii_5d,-500,1500)
    ph=fund.get("promoter_hold")
    if ph is not None: c["Promoter"]=minmax(ph,0,75)
    bulk_net=bulk.get("net_value_cr") if bulk else None
    if bulk_net is not None: c["BulkDeals"]=minmax(bulk_net,-50,50)
    if not c: return None
    return round(weighted_score(c,weights),2)

def compute_vpi(tech,prices,fund,inst,ns,weights,rs=None,liquidity=None):
    c={}
    if tech.get("atr_pct"): c["V"]=min(tech["atr_pct"]*12,100)
    if tech.get("adx_14"):  c["TS"]=minmax(tech["adx_14"],0,60)
    if rs is not None: c["RS"]=rs
    lq=liquidity if liquidity is not None else compute_liquidity(prices)
    if lq is not None: c["LQ"]=lq
    vr=tech.get("volume_ratio")
    if vr: c["VOL"]=min(vr/3*100,100)
    rsi=tech.get("rsi_14")
    if rsi is not None: c["MR"]=(80+(50-rsi) if 30<=rsi<=50 else 70 if rsi<30 else max(0,60-(rsi-50)*2))
    eps_g=fund.get("eps_growth_yoy")
    if eps_g: c["FG"]=minmax(eps_g,-50,100)
    if not inst.empty:
        net=(inst.get("fii_net_cr",pd.Series([0])).sum()+(inst.get("dii_net_cr",pd.Series([0])).sum()))
        c["IS"]=minmax(float(net),-500,500)
    c["NS"]=ns
    return round(weighted_score(c,weights),2)

def compute_mri(tech,ns,weights):
    c={}
    # Normalised by price -- an absolute rupee histogram is not comparable
    # across a universe priced from Rs 7 to Rs 134,860. See
    # data.technical.MACD_HIST_FULL_SCALE_PCT.
    from data.technical import macd_component
    macd=macd_component(tech.get("macd_hist_pct"))
    if macd is not None: c["MACD"]=macd
    rsi=tech.get("rsi_14")
    if rsi is not None: c["RSI"]=(85 if 40<=rsi<=55 else 60 if rsi<40 else 20 if rsi>70 else 50)
    adx=tech.get("adx_14")
    if adx: c["ADX"]=minmax(adx,0,60,invert=True)
    vr=tech.get("volume_ratio")
    if vr: c["Volume"]=min(vr/2*100,100)
    e9,e21=tech.get("ema_9"),tech.get("ema_21")
    if e9 and e21: c["EMA"]=75 if e9>e21 else 30
    c["News"]=ns
    return round(weighted_score(c,weights),2)

def compute_rri(tech,prices,inst,ns,weights):
    c={}
    if not prices.empty:
        cmp=prices["close"].iloc[0]; lo52=prices["close"].min()
        if lo52>0: c["Recovery"]=min((cmp-lo52)/lo52*200,100)
    rsi=tech.get("rsi_14")
    if rsi is not None: c["RSIRecovery"]=min(max(0,(rsi-30)*2),100)
    vr=tech.get("volume_ratio")
    if vr: c["Volume"]=min(vr/2*100,100)
    if not inst.empty:
        dii=(inst["dii_net_cr"].sum() if "dii_net_cr" in inst.columns else 0)
        c["Institutional"]=minmax(float(dii),-200,500)
    c["Support"]=80 if tech.get("above_200dma") else 30
    c["News"]=ns
    return round(weighted_score(c,weights),2)

def compute_cri(tech,fund,ns,mh,weights):
    c={}
    atr_pct=tech.get("atr_pct")
    if atr_pct: c["Volatility"]=min(atr_pct*15,100)
    de=fund.get("debt_equity")
    if de is not None: c["Debt"]=minmax(de,0,5)
    vr=tech.get("volume_ratio")
    c["Distribution"]=min((vr or 1)*30,100) if (vr and vr>1.5) else 20
    w=0
    if not tech.get("above_200dma",1): w+=40
    if tech.get("death_cross",0): w+=40
    c["WeakTrend"]=min(w,100)
    c["NegativeNews"]=max(0,100-ns)
    mh_s=mh.get("mh_score",50) or 50; vix=mh.get("vix_level")   # None: no VIX for the session
    mw=0
    if mh_s<40: mw+=50
    if vix is not None and vix>18: mw+=30
    if vix is not None and vix>25: mw+=20
    c["MarketWeakness"]=min(mw,100)
    return round(weighted_score(c,weights),2)

# ZPI's "Institutional -- Accumulation 10d" component. It used to read
# institutional_data, which nothing has ever populated (there is no free
# per-stock FII/DII source), so the component was always absent and its 0.10
# weight renormalised away. Delivery is the per-stock accumulation evidence
# NSE does publish.
DELIVERY_SHORT, DELIVERY_BASE = 10, 60
DELIVERY_FULL_SCALE = 0.24

def compute_delivery_accumulation(prices):
    """
    The stock's average delivery % over its last 10 sessions relative to its
    own average over the last 60: above 1 is more delivery-based buying than
    usual for that stock. 50 at parity, 0 and 100 at -/+24%.

    Measured 2026-09-22 on 409 sessions of delivery for the tracked universe:
    rank IC against the next 5 sessions' return +0.020 (t +2.7, non-overlapping
    windows; +0.011 and +0.029 in each half of the history; +0.018 net of
    10-day momentum). The directional alternative -- delivered shares on up
    days minus down days -- had IC -0.016 and was rejected: it mostly
    re-measures the last 10 days' price move, which tends to reverse. Full
    scale 0.24 is the 95th percentile of |ratio - 1|. None (component absent)
    without 8 of the last 10 and 40 of the last 60 sessions' delivery.

    The session's own delivery is published in the evening, so the 16:45
    scores use the window ending the session before (IC +0.012, t +1.5; +0.018
    on dates offset by two) and the evening re-score (pipeline/scheduler.py,
    settle_session_flows) includes it -- the figure above.
    """
    if prices is None or prices.empty or "delivery_pct" not in prices.columns:
        return None
    p = pd.to_numeric(prices["delivery_pct"], errors="coerce")   # newest first
    short, base = p.iloc[:DELIVERY_SHORT], p.iloc[:DELIVERY_BASE]
    if short.notna().sum() < 8 or base.notna().sum() < 40:
        return None
    b = base.mean()
    if not b or b <= 0:
        return None
    return minmax(short.mean() / b, 1 - DELIVERY_FULL_SCALE, 1 + DELIVERY_FULL_SCALE)

def compute_zpi(tech,inst,ns,sector_val,weights,accumulation=None):
    c={}
    c["Support"]=80 if tech.get("above_200dma") else 30
    adx=tech.get("adx_14")
    if adx: c["Resistance"]=max(0,100-adx*2)
    rsi=tech.get("rsi_14")
    if rsi is not None: c["RSI"]=(90 if 30<=rsi<=50 else 70 if rsi<30 else 60 if rsi<=60 else 20)
    atr_pct=tech.get("atr_pct")
    if atr_pct: c["ATR"]=min(atr_pct*12,100)
    vr=tech.get("volume_ratio")
    if vr: c["Volume"]=(80 if vr<0.7 else 65 if vr<1.0 else 45 if vr<1.5 else 20)
    c["Trend"]=85 if tech.get("above_200dma") else 15
    if accumulation is not None:
        c["Institutional"]=accumulation   # delivery-based -- see compute_delivery_accumulation()
    c["News"]=ns
    c["Sector"]=sector_val  # real per-day industry-relative-performance score — see compute_sector_ranks()/sector_score()
    return round(weighted_score(c,weights),2)

# Below this share of the MH weight present, no MH score or regime is stored:
# a "market health" from one or two inputs is not the index the weights define.
MH_MIN_COVERAGE = 0.50
GLOBAL_MAX_AGE_DAYS = 4       # a global_markets snapshot older than this is not "today's"

def get_session_fii(td,conn):
    """fii_dii_market for td, or for the session before it -- NSE publishes the
    session's flows in the evening, so the 16:45 score uses the previous
    session's and the evening re-score replaces them (pipeline/scheduler.py).
    Anything older is not this session's and returns {}; get_fii() would carry
    the latest row forward indefinitely."""
    from utils.trading_calendar import last_trading_day
    from datetime import timedelta
    d=datetime.strptime(str(td),"%Y-%m-%d").date()
    prev=last_trading_day(d-timedelta(days=1))
    r=conn.execute("SELECT * FROM fii_dii_market WHERE date IN (?,?) ORDER BY date DESC LIMIT 1",
                   (str(d),str(prev))).fetchone()
    return dict(r) if r else {}

def get_recent_global(td,conn):
    """The latest global_markets snapshot at most GLOBAL_MAX_AGE_DAYS before td, or {}."""
    from datetime import timedelta
    since=datetime.strptime(str(td),"%Y-%m-%d").date()-timedelta(days=GLOBAL_MAX_AGE_DAYS)
    r=conn.execute("SELECT * FROM global_markets WHERE date<=? AND date>=? ORDER BY date DESC, time DESC LIMIT 1",
                   (str(td),str(since))).fetchone()
    return dict(r) if r else {}

def mh_regime(mh_score):
    if mh_score is None: return None
    return ("STRONG_BULL" if mh_score>=80 else "BULL" if mh_score>=60 else "NEUTRAL" if mh_score>=40
            else "BEAR" if mh_score>=20 else "HIGH_RISK")

MH_STORED_COLUMNS = ("mh_score","regime","nifty_trend","banknifty","breadth","vix_score","fii_score",
                     "dii_score","global_score","sector_score","adv_decline","nifty_close","vix_level",
                     "advances","declines","pct_advancing","new_highs","new_lows","breadth_universe",
                     "mh_coverage","mh_inputs","backfilled")

def compute_mh(td,conn,weights,breadth=None,backfilled=False,quiet=False):
    """
    Market Health for session td, stored in market_health and returned.

    Every input is the session's own figure or is left out -- weighted_score
    renormalises over what is present, and mh_coverage/mh_inputs record what
    that was. The inputs used to default silently: a missing index change
    scored as a flat day (`or 0`), missing FII/DII as zero flows, a missing
    global snapshot as 50, VIX as 15, and breadth as the constant 25.93.

      NiftyTrend, BankNifty, Sector  index_levels, else the daily index series
      VIX                            get_vix()
      FII, DII                       the session's flows, or the previous session's
      Global                         a snapshot at most 4 days old
      Breadth                        % of the universe above its 200-DMA
      AdvanceDecline                 advances / (advances + declines) x 100 --
                                     50 on a balanced day; the old A/D-ratio
                                     scale put a balanced day at 25.93

    breadth: a precomputed breadth_series() entry (the backfill passes one per
    session); None computes it here.
    """
    idx=get_idx(td,conn); fii=get_session_fii(td,conn); glb=get_recent_global(td,conn); c={}
    nifty=get_index_change(td,conn,"nifty50",idx)
    if nifty is not None: c["NiftyTrend"]=minmax(nifty,-3,3)
    bank=get_index_change(td,conn,"banknifty",idx)
    if bank is not None: c["BankNifty"]=minmax(bank,-4,4)
    vix=get_vix(td,conn)
    if vix is not None: c["VIX"]=minmax(vix,8,35,invert=True)
    elif not quiet: log.warning(f"  India VIX missing for {td} — MH scored without its VIX component")
    if fii.get("fii_net_cr") is not None: c["FII"]=minmax(fii["fii_net_cr"],-3000,3000)
    if fii.get("dii_net_cr") is not None: c["DII"]=minmax(fii["dii_net_cr"],-1000,2000)
    if glb.get("global_score") is not None: c["Global"]=glb["global_score"]
    mid=get_index_change(td,conn,"midcap150",idx); small=get_index_change(td,conn,"smallcap250",idx)
    if mid is not None and small is not None: c["Sector"]=minmax((mid+small)/2,-4,4)
    br=compute_breadth(td,conn) if breadth is None else breadth
    if br.get("pct_above_200dma") is not None: c["Breadth"]=br["pct_above_200dma"]
    if br.get("pct_advancing") is not None: c["AdvanceDecline"]=br["pct_advancing"]
    total=sum(weights.values()) or 1
    coverage=round(sum(w for k,w in weights.items() if c.get(k) is not None)/total,3)
    mh_score=weighted_score(c,weights) if coverage>=MH_MIN_COVERAGE else None
    regime=mh_regime(mh_score)
    nclose=conn.execute("SELECT close FROM prices_daily WHERE symbol='NIFTY50' AND date=?",(str(td),)).fetchone()
    row={"mh_score":mh_score,"regime":regime,"nifty_trend":c.get("NiftyTrend"),"banknifty":c.get("BankNifty"),
         "breadth":br.get("pct_above_200dma"),"vix_score":c.get("VIX"),"fii_score":c.get("FII"),
         "dii_score":c.get("DII"),"global_score":c.get("Global"),"sector_score":c.get("Sector"),
         "adv_decline":br.get("ad_ratio"),"nifty_close":nclose[0] if nclose else idx.get("nifty50"),
         "vix_level":vix,"advances":br.get("advances"),"declines":br.get("declines"),
         "pct_advancing":br.get("pct_advancing"),"new_highs":br.get("new_highs"),"new_lows":br.get("new_lows"),
         "breadth_universe":br.get("universe"),"mh_coverage":coverage,
         "mh_inputs":",".join(sorted(k for k in weights if c.get(k) is not None)),"backfilled":int(backfilled)}
    # An upsert, not INSERT OR REPLACE: REPLACE deletes the row first, which
    # threw away the portfolio_health that scores/portfolio_health.py stores in it.
    conn.execute(f"INSERT INTO market_health (date,{','.join(MH_STORED_COLUMNS)}) "
                 f"VALUES (?,{','.join('?'*len(MH_STORED_COLUMNS))}) ON CONFLICT(date) DO UPDATE SET "
                 + ",".join(f"{k}=excluded.{k}" for k in MH_STORED_COLUMNS),
                 (str(td),*[row[k] for k in MH_STORED_COLUMNS]))
    if mh_score is None:
        log.warning(f"  MH for {td}: only {coverage:.0%} of its weight has data — no score stored")
    elif not quiet:
        log.info(f"  ✓ MH: {mh_score:.1f} ({regime}) — {coverage:.0%} of inputs present"
                 + (f"; breadth {br['pct_above_200dma']:.0f}% above 200-DMA, A/D {br.get('advances')}/{br.get('declines')}"
                    if br.get("pct_above_200dma") is not None else ""))
    return {"mh_score":mh_score,"regime":regime,"vix_level":vix,"coverage":coverage}

def backfill_market_health(start=None,end=None,overwrite=False):
    """
    Market Health for every past session (a date with a NIFTY50 benchmark
    close) that has none, marked backfilled=1. Sessions already scored live
    are left alone unless overwrite=True: their ai_scores rows were produced
    from the stored MH and regime, and rewriting one without the other would
    make the two disagree. FII/DII history exists only from 2026-07 and
    global snapshots only from 2026-07-24, so older sessions are scored on
    the inputs that have history -- mh_coverage records the share.
    """
    conn=get_connection()
    try:
        weights=load_weights(conn,"MH")
        sessions=[str(r[0]) for r in conn.execute(
            "SELECT date FROM prices_daily WHERE symbol='NIFTY50' AND date>=? AND date<=? ORDER BY date",
            (str(start or "0000-01-01"),str(end or "9999-12-31")))]
        have={str(r[0]) for r in conn.execute("SELECT date FROM market_health WHERE mh_score IS NOT NULL")}
        todo=[d for d in sessions if overwrite or d not in have]
        if not todo:
            return {"status":"SKIPPED","rows":0,"reason":"every session already has a market-health row"}
        br=breadth_series(conn,todo[0],todo[-1])
        scored=0; regimes={}
        for d in todo:
            mh=compute_mh(d,conn,weights,breadth=br.get(d,{}),backfilled=d not in have,quiet=True)
            if mh["mh_score"] is not None:
                scored+=1; regimes[mh["regime"]]=regimes.get(mh["regime"],0)+1
        conn.commit()
        log.info(f"  ✓ Market health backfilled for {scored}/{len(todo)} sessions {todo[0]} → {todo[-1]}: {regimes}")
        return {"status":"SUCCESS","rows":scored,"sessions":len(todo),"regimes":regimes}
    finally:
        conn.close()

def compute_msi(td,conn,weights):
    fii=get_fii(td,conn); idx=get_idx(td,conn); glb=get_global(td,conn); c={}
    from datetime import timedelta
    since=str((datetime.strptime(str(td),"%Y-%m-%d")-timedelta(hours=24)).date())
    r=conn.execute("SELECT AVG(news_score) as ns FROM news_articles WHERE fetched_at>=?",(since,)).fetchone()
    c["News"]=float(r["ns"]) if r and r["ns"] else 50.0
    c["FII"]=minmax(fii.get("fii_5d_avg",0) or 0,-2000,2000)
    c["DII"]=minmax(fii.get("dii_5d_avg",0) or 0,-500,1500)
    chgs=[idx.get(k,0) or 0 for k in ["nifty50_chg","banknifty_chg","midcap150_chg","smallcap250_chg"]]
    c["Sector"]=sum(1 for x in chgs if x>0)/len(chgs)*100 if chgs else 50
    c["Options"]=minmax(fii.get("pcr",1.0) or 1.0,0.5,1.8,invert=True)
    c["Global"]=glb.get("global_score",50) or 50
    vix=get_vix(td,conn)
    if vix is not None: c["VIX"]=minmax(vix,8,35,invert=True)
    return round(weighted_score(c,weights),2)

def compute_acs(sym,td,all_scores,mh,conn,weights,liquidity=None,news_conf=None):
    """
    AI Confidence Score. Every component is measured or left out -- the
    stand-ins it used to take (HistoricalAccuracy 50, MarketRegime 50,
    NewsConfidence 50, Liquidity 60) are gone, and weighted_score renormalises
    over what is present. See get_historical_accuracy() / get_news_confidence().
    """
    c={}
    acc=get_historical_accuracy(sym,td,conn)
    if acc is not None: c["HistoricalAccuracy"]=acc
    idx_s={k:v for k,v in all_scores.items() if k in ("vpi","mri","rri","zpi","msi") and v}
    if idx_s: c["Agreement"]=sum(1 for v in idx_s.values() if v>60)/len(idx_s)*100
    if mh.get("mh_score") is not None: c["MarketRegime"]=minmax(mh["mh_score"],0,100)
    avail=sum(1 for v in all_scores.values() if v is not None)
    c["DataQuality"]=avail/max(len(all_scores),1)*100
    nc=news_conf if news_conf is not None else get_news_confidence(sym,td,conn)
    if nc is not None: c["NewsConfidence"]=nc
    if liquidity is not None: c["Liquidity"]=liquidity
    return round(weighted_score(c,weights),2)

# ── Decision Engine thresholds ─────────────────────────────────────────────
# Pulled out of determine_signal() so the gates are reviewable and tunable in
# one place instead of being magic numbers buried in a boolean cascade.
#
# ⚠ MEASURED 2026-09 — THE BUY BRANCH IS CURRENTLY UNREACHABLE.
#   Across the 2,127 scored rows in this database the ZPI distribution was
#   mean 43.1, max 50.96. Both BUY gates require zpi >= 55 (strict: >= 65),
#   so neither can ever be satisfied: the signal history is 1,598 HOLD +
#   529 WAIT and not one BUY or SELL, ever. ZPI reads low partly because its
#   Institutional component is always absent (institutional_data is never
#   populated by any code path) and its Sector component was a hardcoded
#   constant until recently.
#
#   Two things must be true before re-calibrating these is meaningful:
#     1. The missing inputs are actually populated. SPI and FS were NULL in
#        100% of rows (fundamental_data was empty) and INS was never computed,
#        which systematically depresses atip_score — max observed 71.1 against
#        a BUY gate of 75/65.
#     2. The new calibration is validated with scores/backtest.py in
#        `signal` mode over a real span of scored history.
#
#   Do NOT just lower these until a BUY appears. That manufactures signals
#   without any evidence they are profitable — and the measured base rates for
#   fixed 3%/6% targets on this universe are already negative after costs.
BUY_STRICT = {"atip": 75, "vpi": 70, "zpi": 65, "acs": 60, "mh": 50}
BUY_LOOSE  = {"atip": 65, "vpi": 60, "zpi": 55, "mh": 40}
SELL_CRI_DANGER = 75      # above this, exit/avoid regardless of other scores
SELL_ATIP_FLOOR = 30      # below this, treat as a sell candidate
HOLD_ATIP_FLOOR = 50      # below this (and not a sell), sit out as WAIT

def determine_signal(scores,mh):
    vpi=scores.get("vpi",0) or 0; zpi=scores.get("zpi",0) or 0
    cri=scores.get("cri",100) or 100; acs=scores.get("acs",0) or 0
    atip=scores.get("atip_score",0) or 0; mh_s=mh.get("mh_score",50) or 50
    regime=mh.get("regime","NEUTRAL")
    if cri>SELL_CRI_DANGER: return "SELL" if atip<40 else "HOLD"
    if regime=="HIGH_RISK": return "WAIT"
    if regime=="BEAR" and atip<70: return "WAIT"
    b=BUY_STRICT
    if atip>=b["atip"] and vpi>=b["vpi"] and zpi>=b["zpi"] and acs>=b["acs"] and mh_s>=b["mh"]: return "BUY"
    b=BUY_LOOSE
    if atip>=b["atip"] and vpi>=b["vpi"] and zpi>=b["zpi"] and mh_s>=b["mh"]: return "BUY"
    if atip<SELL_ATIP_FLOOR or (cri>60 and scores.get("mri",50)<40): return "SELL"
    return "HOLD" if atip>=HOLD_ATIP_FLOOR else "WAIT"

def run_scoring_pipeline(trade_date=None):
    if trade_date is None: trade_date=date.today()
    log.info(f"🧠 AI Scoring pipeline {trade_date}")
    conn=get_connection(); result={"date":str(trade_date),"rows":0,"status":"SUCCESS"}
    try:
        w_vpi=load_weights(conn,"VPI"); w_mri=load_weights(conn,"MRI")
        w_rri=load_weights(conn,"RRI"); w_cri=load_weights(conn,"CRI")
        w_zpi=load_weights(conn,"ZPI"); w_acs=load_weights(conn,"ACS")
        w_atip=load_weights(conn,"ATIP"); w_tod=load_weights(conn,"TOD")
        w_mh=load_weights(conn,"MH"); w_msi=load_weights(conn,"MSI")
        w_ins=load_weights(conn,"INS")
        mh=compute_mh(trade_date,conn,w_mh)
        msi=compute_msi(trade_date,conn,w_msi)
        fii=get_fii(trade_date,conn)  # market-wide FII/DII 5-day avg — shared input to compute_ins() for every symbol
        # Score the curated tracked universe (Nifty High Beta 50 + portfolio
        # holdings), NOT "every symbol that happens to be in prices_daily
        # for this date" — that table also holds the full NSE Bhavcopy
        # market dump (~3,000 EQ/BE/SM tickers), and scoring all of them
        # produces thousands of junk rows for stocks that were never part
        # of the tracked strategy and have no technical/fundamental data.
        from data.dhan import get_tracked_symbols, BETA_BENCHMARK_SYMBOL
        from data.technical import compute_beta
        tracked = set(get_tracked_symbols(conn))
        syms_rows=conn.execute("SELECT DISTINCT symbol FROM prices_daily WHERE date=?",(str(trade_date),)).fetchall()
        available={r["symbol"] for r in syms_rows}
        symbols=sorted(tracked & available) if (tracked & available) else sorted(tracked)
        if not symbols:
            syms_rows=conn.execute("SELECT DISTINCT symbol FROM technical_indicators WHERE date=?",(str(trade_date),)).fetchall()
            symbols=[r["symbol"] for r in syms_rows]
        log.info(f"  Scoring {len(symbols)} symbols...")
        # Computed once for the whole run, not per symbol — both are shared
        # inputs (the Nifty 50 benchmark series, and the industry-relative
        # sector ranking for this trade date).
        bench_prices=get_prices(BETA_BENCHMARK_SYMBOL,trade_date,conn,n=30)
        sector_rank_map,n_sectors=compute_sector_ranks(conn,trade_date,symbols)
        count=0; all_scores_list=[]
        for sym in symbols:
            try:
                tech=get_tech(sym,trade_date,conn); prices=get_prices(sym,trade_date,conn)
                fund=get_fund(sym,conn); inst=get_inst(sym,trade_date,conn)
                ns=get_ns(sym,trade_date,conn)
                rs=compute_relative_strength(prices,bench_prices)
                liquidity=compute_liquidity(prices)
                news_conf=get_news_confidence(sym,trade_date,conn)
                sector_val=sector_score(sector_rank_map.get(sym),n_sectors)
                breakout=compute_breakout(prices,tech)
                bulk=get_bulk(sym,trade_date,conn)
                vpi=compute_vpi(tech,prices,fund,inst,ns,w_vpi,rs=rs,liquidity=liquidity)
                mri=compute_mri(tech,ns,w_mri)
                rri=compute_rri(tech,prices,inst,ns,w_rri)
                cri=compute_cri(tech,fund,ns,mh,w_cri)
                zpi=compute_zpi(tech,inst,ns,sector_val,w_zpi,
                                accumulation=compute_delivery_accumulation(prices))
                spi=fund.get("fundamental_score"); ts=tech.get("tech_score")
                ins=compute_ins(fii,fund,bulk,w_ins)
                all_s={"vpi":vpi,"spi":spi,"rri":rri,"mri":mri,"cri":cri,"msi":msi,"zpi":zpi,"tech_score":ts}
                acs=compute_acs(sym,trade_date,all_s,mh,conn,w_acs,liquidity=liquidity,news_conf=news_conf)
                all_s["acs"]=acs
                atip_comp={"VPI":vpi,"SPI":spi,"RRI":rri,"MRI":mri,"MSI":msi,"ZPI":zpi,"TS":ts,"FS":fund.get("fundamental_score"),"INS":ins}
                atip_score=round(weighted_score({k:v for k,v in atip_comp.items() if v is not None},w_atip),2)
                all_s["atip_score"]=atip_score
                tod_comp={"VPI":vpi,"ZPI":zpi,"MRI":mri,"MSI":msi,"Volume":min((tech.get("volume_ratio",1) or 1)*40,100),"Breakout":breakout,"Sector":sector_val,"ACS":acs}
                tod_score=round(weighted_score(tod_comp,w_tod),2)
                signal=determine_signal(all_s,mh)
                # Display-only reference figure -- 1-year beta vs Nifty 50.
                # Not fed into VPI/CRI/ZPI/ATIP or any other weighted score;
                # purely so the dashboard can show each stock's actual beta.
                beta_1y=compute_beta(sym,conn)
                factors=[f for f in [f"VPI:{vpi:.0f}",f"ZPI:{zpi:.0f}",f"MRI:{mri:.0f}",f"CRI:{cri:.0f}"] if f]
                conn.execute("""INSERT INTO ai_scores (symbol,date,vpi,spi,rri,mri,cri,msi,zpi,acs,tech_score,fund_score,inst_score,news_score,atip_score,tod_score,signal,confidence,beta_1y,mh_score,regime,top_factor_1,top_factor_2,top_factor_3)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,date) DO UPDATE SET vpi=excluded.vpi,spi=excluded.spi,mri=excluded.mri,rri=excluded.rri,cri=excluded.cri,msi=excluded.msi,zpi=excluded.zpi,acs=excluded.acs,tech_score=excluded.tech_score,fund_score=excluded.fund_score,inst_score=excluded.inst_score,news_score=excluded.news_score,atip_score=excluded.atip_score,tod_score=excluded.tod_score,signal=excluded.signal,confidence=excluded.confidence,beta_1y=excluded.beta_1y,mh_score=excluded.mh_score,regime=excluded.regime,top_factor_1=excluded.top_factor_1,top_factor_2=excluded.top_factor_2,top_factor_3=excluded.top_factor_3""",
                    # Every column the run just recomputed is refreshed. The
                    # list used to stop at beta_1y, so a re-score left spi,
                    # confidence, the four component scores, the regime and the
                    # factors frozen at the FIRST run's values: re-scoring
                    # 2026-09-18 on the real closes left 499 of 502 rows with a
                    # confidence that no longer matched their acs, and ai_scores
                    # saying NEUTRAL/59.9 for that date while market_health --
                    # rewritten by the same run -- said BULL/60.01.
                    (sym,str(trade_date),vpi,spi,rri,mri,cri,msi,zpi,acs,ts,fund.get("fundamental_score"),ins,ns,atip_score,tod_score,signal,acs,beta_1y,mh.get("mh_score"),mh.get("regime"),
                     factors[0] if len(factors)>0 else None,factors[1] if len(factors)>1 else None,factors[2] if len(factors)>2 else None))
                all_scores_list.append({"symbol":sym,"atip_score":atip_score,"cri":cri,"acs":acs,"zpi":zpi,"tod_score":tod_score})
                count+=1
            except Exception as e: log.warning(f"  {sym}: {e}")
        if all_scores_list:
            df_s=pd.DataFrame(all_scores_list).sort_values("atip_score",ascending=False)
            for i,(_,row) in enumerate(df_s.iterrows()):
                conn.execute("UPDATE ai_scores SET atip_rank=? WHERE symbol=? AND date=?",(i+1,row["symbol"],str(trade_date)))
            tod_cand=df_s[(df_s["cri"]<30)&(df_s["acs"]>=60)&(df_s["zpi"]>=60)]
            if not tod_cand.empty:
                ts=tod_cand.sort_values("tod_score",ascending=False).iloc[0]["symbol"]
                # Clear first: is_tod was only ever set, never reset, so a
                # re-score left the previous pick flagged too and 2026-09-18
                # ended up with two Trades of the Day.
                conn.execute("UPDATE ai_scores SET is_tod=0 WHERE date=? AND is_tod=1",(str(trade_date),))
                conn.execute("UPDATE ai_scores SET is_tod=1 WHERE symbol=? AND date=?",(ts,str(trade_date)))
                log.info(f"  🎯 TOD: {ts}")
        conn.commit(); result["rows"]=count
        log.info(f"  ✓ Scored {count} stocks")
        # Signal mix + why-no-BUY diagnostic. A run that produces zero
        # actionable signals is the failure mode that went unnoticed for
        # months here (see the threshold block above) — so say so loudly,
        # and show how far the best candidate actually fell short.
        try:
            mix=conn.execute("SELECT signal, COUNT(*) n FROM ai_scores WHERE date=? GROUP BY signal",
                             (str(trade_date),)).fetchall()
            log.info("  signals: "+", ".join(f"{r['signal']}={r['n']}" for r in mix))
            result["signals"]={r["signal"]:r["n"] for r in mix}
            if not any(r["signal"] in ("BUY","SELL") for r in mix):
                best=conn.execute("""SELECT MAX(atip_score) a, MAX(vpi) v, MAX(zpi) z, MAX(acs) c
                                     FROM ai_scores WHERE date=?""",(str(trade_date),)).fetchone()
                log.warning(
                    f"  ⚠ No actionable BUY/SELL signals for {trade_date}. "
                    f"Best available: ATIP={best['a']} (gate {BUY_LOOSE['atip']}), "
                    f"VPI={best['v']} (gate {BUY_LOOSE['vpi']}), "
                    f"ZPI={best['z']} (gate {BUY_LOOSE['zpi']}), ACS={best['c']}. "
                    f"If ZPI never reaches its gate, the BUY branch is unreachable — "
                    f"check fundamentals/institutional inputs before re-calibrating.")
        except Exception as e:
            log.debug(f"  signal mix diagnostic skipped: {e}")
        log_job("ai_scoring","SUCCESS",count,run_date=trade_date)
        # Turn today's scores into measurable predictions (entry/SL/targets/
        # size). Without this the predictions table stays empty and the
        # accuracy tracker can never produce a single outcome — which is
        # exactly the state this system was in before.
        try:
            from scores.predictions import write_predictions
            pred=write_predictions(trade_date,conn=conn)
            result["predictions"]=pred.get("rows",0)
        except Exception as e:
            log.warning(f"  predictions write skipped: {e}")
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; log.error(f"  ✗ {e}")
        log_job("ai_scoring","FAILED",0,error=e,run_date=trade_date)
    finally: conn.close()
    return result

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--date")
    ap.add_argument("--backfill-mh",action="store_true",
                    help="store market health for past sessions that have none (live rows untouched)")
    args=ap.parse_args()
    if args.backfill_mh:
        print(backfill_market_health())
    else:
        td=datetime.strptime(args.date,"%Y-%m-%d").date() if args.date else date.today()
        run_scoring_pipeline(td)
