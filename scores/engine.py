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
    """Average news-classification confidence (0-1 in news_articles.confidence,
    set by Claude's classify_with_claude() or the rule-based fallback) for
    this stock over the trailing 7 days, scaled to 0-100. Falls back to the
    market-wide 24h average when the stock has no coverage, then to a
    neutral 50.0 only if there is no news data at all. Replaces ACS's old
    hardcoded NewsConfidence=50.0."""
    from datetime import timedelta
    since=str((datetime.strptime(str(td),"%Y-%m-%d")-timedelta(days=7)).date())
    r=conn.execute("SELECT AVG(confidence) as c FROM news_articles WHERE symbols_mentioned LIKE ? AND fetched_at>=?",
                   (f'%\"{sym}\"%',since)).fetchone()
    if r and r["c"] is not None: return round(float(r["c"])*100,2)
    since24=str((datetime.strptime(str(td),"%Y-%m-%d")-timedelta(hours=24)).date())
    r2=conn.execute("SELECT AVG(confidence) as c FROM news_articles WHERE fetched_at>=?",(since24,)).fetchone()
    if r2 and r2["c"] is not None: return round(float(r2["c"])*100,2)
    return 50.0

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
    seeded weight but was never actually computed."""
    if prices is None or prices.empty or len(prices)<21: return None
    if bench_prices is None or bench_prices.empty or len(bench_prices)<21: return None
    s0,s20=prices["close"].iloc[0],prices["close"].iloc[20]
    b0,b20=bench_prices["close"].iloc[0],bench_prices["close"].iloc[20]
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
    hist=tech.get("macd_hist")
    if hist is not None: c["MACD"]=min(50+abs(hist)*10,100) if hist>0 else max(0,50-abs(hist)*10)
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
    mh_s=mh.get("mh_score",50) or 50; vix=mh.get("vix_level",15) or 15
    mw=0
    if mh_s<40: mw+=50
    if vix>18: mw+=30
    if vix>25: mw+=20
    c["MarketWeakness"]=min(mw,100)
    return round(weighted_score(c,weights),2)

def compute_zpi(tech,inst,ns,sector_val,weights):
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
    if not inst.empty:
        net=sum([inst.get("fii_net_cr",pd.Series([0])).sum(),inst.get("dii_net_cr",pd.Series([0])).sum()])
        c["Institutional"]=minmax(float(net),-200,400)
    c["News"]=ns
    c["Sector"]=sector_val  # real per-day industry-relative-performance score — see compute_sector_ranks()/sector_score()
    return round(weighted_score(c,weights),2)

def compute_mh(td,conn,weights):
    fii=get_fii(td,conn); idx=get_idx(td,conn); glb=get_global(td,conn); c={}
    c["NiftyTrend"]=minmax(idx.get("nifty50_chg",0) or 0,-3,3)
    c["BankNifty"]=minmax(idx.get("banknifty_chg",0) or 0,-4,4)
    vix=idx.get("india_vix",15) or 15
    c["VIX"]=minmax(vix,8,35,invert=True)
    c["FII"]=minmax(fii.get("fii_net_cr",0) or 0,-3000,3000)
    c["DII"]=minmax(fii.get("dii_net_cr",0) or 0,-1000,2000)
    c["Global"]=glb.get("global_score",50) or 50
    mid=(idx.get("midcap150_chg",0) or 0)+(idx.get("smallcap250_chg",0) or 0)
    c["Sector"]=minmax(mid/2,-4,4)
    ad=fii.get("adv_decline",1) or 1
    c["Breadth"]=minmax(ad,0.3,3); c["AdvanceDecline"]=c["Breadth"]
    mh_score=weighted_score(c,weights)
    regime=("STRONG_BULL" if mh_score>=80 else "BULL" if mh_score>=60 else "NEUTRAL" if mh_score>=40 else "BEAR" if mh_score>=20 else "HIGH_RISK")
    conn.execute("INSERT OR REPLACE INTO market_health (date,mh_score,regime,nifty_trend,banknifty,vix_score,fii_score,dii_score,global_score,sector_score,vix_level) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (str(td),mh_score,regime,c.get("NiftyTrend"),c.get("BankNifty"),c.get("VIX"),c.get("FII"),c.get("DII"),c.get("Global"),c.get("Sector"),vix))
    log.info(f"  ✓ MH: {mh_score:.1f} ({regime})")
    return {"mh_score":mh_score,"regime":regime,"vix_level":vix}

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
    vix=idx.get("india_vix",15) or 15; c["VIX"]=minmax(vix,8,35,invert=True)
    return round(weighted_score(c,weights),2)

def compute_acs(sym,td,all_scores,mh,conn,weights,liquidity=None,news_conf=None):
    c={}
    r=conn.execute("SELECT AVG(correct_5d)*100 as acc FROM accuracy_tracker WHERE symbol=?",(sym,)).fetchone()
    c["HistoricalAccuracy"]=float(r["acc"]) if r and r["acc"] else 50.0
    idx_s={k:v for k,v in all_scores.items() if k in ("vpi","mri","rri","zpi","msi") and v}
    if idx_s: c["Agreement"]=sum(1 for v in idx_s.values() if v>60)/len(idx_s)*100
    c["MarketRegime"]=minmax(mh.get("mh_score",50) or 50,0,100)
    avail=sum(1 for v in all_scores.values() if v is not None)
    c["DataQuality"]=avail/max(len(all_scores),1)*100
    # Real, per-stock figures when available (see get_news_confidence() /
    # compute_liquidity()) — the old hardcoded 50.0/60.0 constants now only
    # apply as a last-resort fallback when there's genuinely no data yet.
    c["NewsConfidence"]=news_conf if news_conf is not None else get_news_confidence(sym,td,conn)
    c["Liquidity"]=liquidity if liquidity is not None else 60.0
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
                zpi=compute_zpi(tech,inst,ns,sector_val,w_zpi)
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
                    ON CONFLICT(symbol,date) DO UPDATE SET vpi=excluded.vpi,mri=excluded.mri,rri=excluded.rri,cri=excluded.cri,msi=excluded.msi,zpi=excluded.zpi,acs=excluded.acs,inst_score=excluded.inst_score,atip_score=excluded.atip_score,signal=excluded.signal,tod_score=excluded.tod_score,beta_1y=excluded.beta_1y""",
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
    args=ap.parse_args()
    td=datetime.strptime(args.date,"%Y-%m-%d").date() if args.date else date.today()
    run_scoring_pipeline(td)
