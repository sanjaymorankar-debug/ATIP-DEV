import sys, json, pandas as pd, numpy as np, psycopg
pd.set_option('display.width',250); pd.set_option('display.max_rows',200)
pg = psycopg.connect("postgresql://postgres@/atip_vps?host=/private/tmp/claude-507/pgs56&port=55433", options="-c default_transaction_read_only=on")
T = pd.read_csv('trades.csv'); T['at'] = T['at'].astype(str)
C = T[T.closed].copy()
syms = sorted(set(T.symbol))
px = pd.read_sql("select symbol, date::text as date, open, high, low, close from prices_daily where date >= '2026-09-01' and (series='EQ' or series is null) and symbol = any(%s)", pg, params=(syms,))
px = px.drop_duplicates(['symbol','date'], keep='last').sort_values(['symbol','date'])
pc = px.groupby('symbol').close.shift(1)
px['tr'] = np.maximum(px.high-px.low, np.maximum((px.high-pc).abs(), (px.low-pc).abs()))
px['atr14p'] = px.groupby('symbol').tr.transform(lambda s: s.rolling(14).mean())/px.close*100
out = {}
w, l = C[C.pnl>0], C[C.pnl<=0]
out['legacy_closed'] = dict(n=len(C), wins=len(w), losses=len(l), net=round(C.pnl.sum(),2), gross_win=round(w.pnl.sum(),2), gross_loss=round(l.pnl.sum(),2),
    avg_win=round(w.pnl.mean(),2), avg_loss=round(l.pnl.mean(),2), pf=round(w.pnl.sum()/-l.pnl.sum(),2), charges=round(C.charges.sum(),2),
    exit_reasons=C.exit_reason.value_counts().to_dict(), loss_reasons=l.exit_reason.value_counts().to_dict())
rows = []
for r in l.itertuples():
    p = px[px.symbol==r.symbol].set_index('date')
    a = p.loc[:r.date].iloc[-2] if len(p.loc[:r.date])>1 else None   # day before entry
    atr = float(a['atr14p']) if a is not None and a['atr14p']==a['atr14p'] else None
    after = p.loc[p.index > r.exit_date].head(5)
    after10 = p.loc[p.index > r.exit_date].head(10)
    rec5 = bool((after.high >= r.entry).any()) if len(after) else None
    rows.append(dict(session=r.session, symbol=r.symbol, entry_at=r.at[:16], exit=r.exit_date, entry=r.entry, exit_px=r.exit_price, pnl=r.pnl,
        loss_pct=round((r.exit_price/r.entry-1)*100,2), atr14_pct_at_entry=round(atr,2) if atr else None,
        back_above_entry_in_5d=rec5, max_high_5d_pct=round((after.high.max()/r.entry-1)*100,2) if len(after) else None,
        close_5d_pct=round((after.close.iloc[-1]/r.entry-1)*100,2) if len(after) else None,
        close_10d_pct=round((after10.close.iloc[-1]/r.entry-1)*100,2) if len(after10) else None,
        days_after=len(after)))
S = pd.DataFrame(rows)
print(S.to_string(index=False))
out['stop_outs'] = S.to_dict('records')
out['stop_noise'] = dict(n=len(S), back_above_entry_within_5d=int(S.back_above_entry_in_5d.sum()), median_atr14_pct=round(S.atr14_pct_at_entry.median(),2),
    stop_inside_atr=int((S.atr14_pct_at_entry > 2).sum()), median_close_5d_pct=round(S.close_5d_pct.median(),2), median_close_10d_pct=round(S.close_10d_pct.median(),2),
    would_be_positive_10d=int((S.close_10d_pct>0).sum()), n10=int(S.close_10d_pct.notna().sum()))
# winners: ATR at entry too
wr=[]
for r in w.itertuples():
    p = px[px.symbol==r.symbol].set_index('date'); a = p.loc[:r.date]
    wr.append(a['atr14p'].iloc[-2] if len(a)>1 else np.nan)
out['winners_median_atr14_pct'] = round(float(np.nanmedian(wr)),2)
# entry time
C['hm'] = C['at'].str[11:16]
C['bucket'] = pd.cut(C['hm'].str[:2].astype(int)*60+C['hm'].str[3:5].astype(int), [0, 9*60+20, 10*60, 12*60, 24*60], labels=['09:15-09:20','09:20-10:00','10:00-12:00','12:00+'])
out['by_entry_time'] = C.groupby('bucket', observed=True).pnl.agg(['count','sum','mean', lambda s:(s>0).mean()*100]).round(1).reset_index().astype(str).to_dict('records')
print(C.groupby('bucket', observed=True).pnl.agg(['count','sum','mean', lambda s:(s>0).mean()*100]).round(1))
# re-entry: same session & symbol bought again within 3 trading days after a stop-loss
C2 = T.sort_values('at')
re = []
for r in C2.itertuples():
    prev = C2[(C2.session==r.session)&(C2.symbol==r.symbol)&(C2['at']<r.at)&(C2.exit_reason=='stop-loss')]
    if len(prev):
        re.append(dict(session=r.session, symbol=r.symbol, at=r.at[:16], prev_stop_exit=prev.exit_date.iloc[-1], pnl=r.pnl, closed=r.closed))
out['reentry_after_stop_same_session'] = re
# cross-session overlap: same symbol bought by 2+ sessions within the same day
T['d']=T['date']
ov = T.groupby(['symbol','d']).session.nunique(); ov = ov[ov>1]
out['same_symbol_same_day_multi_session'] = ov.reset_index().to_dict('records')
same=[]
for (sym,d) in ov.index:
    g = T[(T.symbol==sym)&(T.d==d)]
    same.append(dict(symbol=sym, day=d, sessions=list(g.session), pnl=list(g.pnl), reasons=list(g.exit_reason)))
out['overlap_detail']=same
# symbol shared by sessions over the month
sh = T.groupby('symbol').agg(n=('session','size'), sessions=('session', lambda s: sorted(set(s))), pnl=('pnl','sum')).sort_values('n', ascending=False)
out['symbol_concentration'] = sh[sh.n>1].reset_index().to_dict('records')
print(sh[sh.n>1])
json.dump(out, open('stops.json','w'), indent=1, default=str)
print(json.dumps({k: out[k] for k in ('legacy_closed','stop_noise','winners_median_atr14_pct')}, indent=1, default=str))
print('reentry', out['reentry_after_stop_same_session'])
print('overlap', out['overlap_detail'])
