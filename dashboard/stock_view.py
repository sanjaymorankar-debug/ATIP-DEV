"""
Stock detail view + table filtering for the main dashboard (W26).

    GET /api/stock/{symbol}/history?sessions=400
        prices (OHLCV), ATIP score history, signal log with outcomes, latest
        technicals (W21 ext included), key stats (52-week range, returns, average
        volume), the LIVE holding and ATIP's orders / order rules for the symbol,
        and chart_marks (chart_marks() below): the stored technical signals and
        candle patterns (technical_signal / technical_snapshot, research/tech_signals.py)
        and the chart patterns in place now with their lines (research/patterns.py).
        Read-only; authz falls under the default dashboard:read rule.

    ASSETS (CSS + HTML + JS, injected before </body> by dashboard/server.py)
        * clicking any row / card that carries data-sym opens the stock panel:
          price chart (line or candles, 1M / 3M / 6M / 1Y / All, volume, BUY / SELL
          signal markers, crosshair), ATIP score chart, and tabs for scores,
          signals, technicals, position & orders; Buy / Sell open the existing
          order modal. Esc or the backdrop closes it; the page auto-refresh is
          held while it is open.
        * the Patterns toggle (on by default, remembered for the session) draws on
          the price chart: a diamond on the day of a chart-pattern breakout, a dot
          on the day of any other technical signal (below the bar for BULL, above for
          BEAR), a small dot at the bar for a bullish / bearish candle pattern, and
          the lines of each pattern in place (box top / bottom, neckline,
          resistance / support, channel or wedge lines) dashed from where the pattern
          starts to the last bar. The crosshair line names them. Plain SVG, as before.
        * ATIP Scores table: search matches symbol / company only, the signal
          filter matches the signal value exactly (the old filter matched the
          row text, so every row matched "sell" / "buy" through its order
          buttons), a Show 50 / 100 / 250 / All limit and a row count. Signal
          History gets the same exact filters; every other tab table gets a
          filter box. Sort arrows on headers; sort + filters survive the
          5-minute auto-refresh (sessionStorage, per tab).

Kept out of the server.py f-string on purpose: no doubled braces to get wrong.
"""

# No `from __future__ import annotations` (FastAPI must see the real types).
import re

from fastapi.responses import JSONResponse

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]{0,29}$")


def _rows(conn, sql, args=()):
    try:
        cur = conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _one(conn, sql, args=()):
    r = _rows(conn, sql, args)
    return r[0] if r else None


def stock_history(conn, symbol: str, sessions: int = 400) -> dict:
    sym = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValueError("invalid symbol")
    sessions = max(20, min(int(sessions), 2500))
    px = _rows(conn, "SELECT date, open, high, low, close, volume, delivery_pct FROM prices_daily "
                     "WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT ?", (sym, sessions))[::-1]
    if not px:
        raise LookupError(f"no price history for {sym}")
    for p in px:
        p["date"] = str(p["date"])[:10]
    closes = [float(p["close"]) for p in px]
    last = closes[-1]

    def ret(n):
        return round((last / closes[-1 - n] - 1) * 100, 2) if len(closes) > n and closes[-1 - n] else None
    yr = px[-250:]
    vols = [float(p["volume"] or 0) for p in px[-20:]]
    stats = {"last_close": last, "last_date": px[-1]["date"],
             "prev_close": closes[-2] if len(closes) > 1 else None,
             "change_pct": round((last / closes[-2] - 1) * 100, 2) if len(closes) > 1 and closes[-2] else None,
             "high_52w": max(float(p["high"] or p["close"]) for p in yr),
             "low_52w": min(float(p["low"] or p["close"]) for p in yr),
             "ret_1w": ret(5), "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126), "ret_1y": ret(250),
             "avg_volume_20d": round(sum(vols) / len(vols)) if vols else None, "sessions": len(px)}
    scores = _rows(conn, "SELECT date, atip_score, atip_rank, vpi, mri, rri, zpi, cri, acs, spi, signal, confidence, "
                         "beta_1y, regime, top_factor_1, top_factor_2 FROM ai_scores WHERE symbol=? ORDER BY date",
                   (sym,))
    for s in scores:
        s["date"] = str(s["date"])[:10]
    sigs = _rows(conn, "SELECT id, signal_date, signal, entry_price, atip_score, zpi, cri, model_version "
                       "FROM signal_log WHERE symbol=? AND duplicate_of IS NULL ORDER BY signal_date DESC LIMIT 200",
                 (sym,))
    for s in sigs:
        s["signal_date"] = str(s["signal_date"])[:10]
        outs = _rows(conn, "SELECT threshold_pct, hit, hit_date, max_favourable_pct, max_adverse_pct, sessions_tracked "
                           "FROM signal_outcome WHERE signal_id=? ORDER BY threshold_pct", (s["id"],))
        hits = [o for o in outs if o.get("hit")]
        s["first_hit"] = (min(hits, key=lambda o: str(o["hit_date"])) if hits else None)
        s["best_pct"] = next((o["max_favourable_pct"] for o in outs if o.get("max_favourable_pct") is not None), None)
        s["worst_pct"] = next((o["max_adverse_pct"] for o in outs if o.get("max_adverse_pct") is not None), None)
        s["sessions_tracked"] = max((o.get("sessions_tracked") or 0 for o in outs), default=None)
    tech = _one(conn, "SELECT * FROM technical_indicators WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)) or {}
    ext = _one(conn, "SELECT * FROM technical_ext WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)) or {}
    for d in (tech, ext):
        d.pop("id", None)
        d.pop("created_at", None)
    holding = _one(conn, "SELECT date, qty, avg_price, cmp, current_val, pnl, pnl_pct, weight_pct FROM portfolio_holdings "
                         "WHERE symbol=? AND date=(SELECT MAX(date) FROM portfolio_holdings) AND qty>0", (sym,))
    paper = _one(conn, "SELECT quantity, avg_price, realized_pnl FROM paper_position WHERE symbol=? AND quantity>0", (sym,))
    rules = _rows(conn, "SELECT id, side, trigger_type, trigger_value, resolved_trigger_price, quantity_type, "
                        "quantity_value, status, created_at, triggered_at FROM order_rules WHERE symbol=? "
                        "ORDER BY created_at DESC LIMIT 20", (sym,))
    orders = _rows(conn, "SELECT timestamp, transaction_type, quantity, order_type, price, mode, status, error "
                         "FROM order_log WHERE symbol=? ORDER BY timestamp DESC LIMIT 20", (sym,))
    oms = _rows(conn, "SELECT created_at, side, quantity, filled_quantity, avg_fill_price, status, strategy_id "
                      "FROM oms_order WHERE symbol=? ORDER BY created_at DESC LIMIT 20", (sym,))
    name = None
    try:
        from data.companies import load_company_names
        name = load_company_names().get(sym)
    except Exception:
        pass
    return {"symbol": sym, "name": name, "stats": stats, "prices": px, "scores": scores, "signals": sigs,
            "technicals": tech, "technical_ext": ext, "holding": holding, "paper_position": paper,
            "order_rules": rules, "orders": orders, "oms_orders": oms, "chart_marks": chart_marks(conn, sym, px)}


MARK_LIMIT = 2000           # technical signals sent to the chart (newest first), enough for years of one stock
PATTERN_BARS = 400          # bars the live pattern read uses (the 20:30 job reads ~600 calendar days)


def chart_marks(conn, sym: str, px: list) -> dict:
    """What the price chart draws from research/tech_signals.py's tables and research/patterns.py, read-only:

        signals    technical_signal rows in the chart's date range: date, scan, name, direction, whether
                   the scan is a chart-pattern breakout (research/technicals.py PATTERN_SCANS), its status
                   and, for a pattern, the reason (which names the pattern's levels)
        candles    candle patterns from technical_snapshot.patterns, one per name per date, with their side
                   (technicals.CANDLE_SIDES); in_place: technical_snapshot.chart_patterns, the chart
                   patterns the 20:30 run listed that day
        patterns   the patterns in place on the stored bars right now (patterns.active on the last
                   PATTERN_BARS bars, the same rules the 20:30 run applies), each with its lines (box top /
                   bottom, neckline, resistance or support, channel or wedge lines) from where it starts
                   to the last bar, and the scan the last bar fired, if any
    """
    out = {"signals": [], "candles": [], "in_place": [], "patterns": []}
    if not px:
        return out
    first = px[0]["date"]
    try:
        from research.technicals import CANDLE_SIDES, PATTERN_SCANS
    except Exception:                                   # no pandas / numpy: the chart simply has no marks
        return out
    rows = _rows(conn, "SELECT date, scan, name, direction, reason, status, r_multiple FROM technical_signal "
                       "WHERE symbol=? AND date>=? ORDER BY date DESC LIMIT ?", (sym, first, MARK_LIMIT))
    for r in rows[::-1]:
        pat = r["scan"] in PATTERN_SCANS
        out["signals"].append({"date": str(r["date"])[:10], "scan": r["scan"], "name": r["name"] or r["scan"],
                               "direction": r["direction"], "pattern": pat, "status": r["status"],
                               "r_multiple": r["r_multiple"], "reason": r["reason"] if pat else None})
    for r in _rows(conn, "SELECT date, patterns, chart_patterns FROM technical_snapshot WHERE symbol=? AND date>=? "
                         "AND (patterns IS NOT NULL OR chart_patterns IS NOT NULL) ORDER BY date", (sym, first)):
        d = str(r["date"])[:10]
        for nm in dict.fromkeys(x.strip() for x in str(r["patterns"] or "").split(",") if x.strip()):
            out["candles"].append({"date": d, "name": nm, "side": CANDLE_SIDES.get(nm, "NEUTRAL")})
        if r["chart_patterns"]:
            out["in_place"].append({"date": d, "text": r["chart_patterns"]})
    out["patterns"] = _active_patterns(px[-PATTERN_BARS:])
    return out


def _active_patterns(px: list) -> list:
    if len(px) < 60:
        return []
    try:
        import pandas as pd
        from research import patterns as P
        from research import technicals as T
        df = pd.DataFrame(px, columns=["date", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("date"))
        df = df.apply(pd.to_numeric, errors="coerce")
        for c in ("open", "high", "low"):
            df[c] = df[c].fillna(df["close"])
        df["volume"] = df["volume"].fillna(0)
        return P.active(T.indicators(df))
    except Exception:
        return []


def register(app, get_connection, json_safe):
    @app.get("/api/stock/{symbol}/history")
    async def api_stock_history(symbol: str, sessions: int = 400):
        from starlette.concurrency import run_in_threadpool

        def go():
            conn = get_connection()
            try:
                return JSONResponse(json_safe(stock_history(conn, symbol, sessions)))
            except LookupError as e:
                return JSONResponse({"error": str(e)}, status_code=404)
            except (ValueError, TypeError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            finally:
                conn.close()
        return await run_in_threadpool(go)


ASSETS = r"""
<style>
th[data-sorted="asc"]::after{content:" \25B2";color:#38bdf8;font-size:9px}
th[data-sorted="desc"]::after{content:" \25BC";color:#38bdf8;font-size:9px}
tr[data-sym]{cursor:pointer}.tod-card[data-sym]{cursor:pointer}
.tfilter{display:flex;gap:6px;align-items:center;margin:0 0 8px;flex-wrap:wrap}.tfilter .cnt{font-size:11px;color:#64748b}
#sv-back{position:fixed;inset:0;background:rgba(2,6,23,.72);display:none;z-index:900;overflow:auto;padding:24px 12px}
#sv-back.on{display:block}
#sv{max-width:1040px;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px 16px;color:#e2e8f0}
#sv .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
#sv .sym{font-size:20px;font-weight:700}#sv .nm{font-size:12px;color:#94a3b8}
#sv .px{font-size:22px;font-weight:700}#sv .up{color:#059669}#sv .dn{color:#dc2626}#sv .mut{color:#64748b}
#sv .btns button{border:none;border-radius:6px;padding:6px 14px;margin-left:4px;cursor:pointer;font-weight:600;color:#fff}
#sv .b-buy{background:#059669}#sv .b-sell{background:#dc2626}#sv .b-x{background:#334155}
#sv .bar{display:flex;gap:4px;flex-wrap:wrap;margin:10px 0 6px;align-items:center}
#sv .bar button{background:#0f172a;border:1px solid #334155;color:#94a3b8;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:11.5px}
#sv .bar button.on{background:#2563eb;color:#fff;border-color:#2563eb}
#sv .sp{flex:1}
#sv svg{width:100%;display:block;background:#0f172a;border-radius:8px}
#sv .tip{font-size:11px;color:#cbd5e1;min-height:16px;margin:4px 2px}
#sv .kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;margin:10px 0}
#sv .kv div{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:6px 9px}
#sv .kv span{display:block;font-size:10px;color:#64748b;text-transform:uppercase}
#sv .kv b{font-size:13px}
#sv .tabs2{display:flex;gap:4px;margin:12px 0 8px;flex-wrap:wrap}
#sv .tabs2 div{padding:4px 11px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid #334155;color:#94a3b8}
#sv .tabs2 div.on{background:#2563eb;color:#fff;border-color:#2563eb}
#sv .pane{display:none;max-height:340px;overflow:auto}#sv .pane.on{display:block}
#sv table{font-size:11.5px}
</style>
<div id="sv-back" onclick="if(event.target===this)svClose()"><div id="sv" role="dialog" aria-modal="true"></div></div>
<script>
(function(){
var S={data:null,range:'1Y',kind:'line',sym:null,pat:st().svpat!==false};
function st(){try{return JSON.parse(sessionStorage.getItem('atip.dash')||'{}')}catch(e){return {}}}
function save(k,v){try{var o=st();o[k]=v;sessionStorage.setItem('atip.dash',JSON.stringify(o))}catch(e){}}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function n(v,d){if(v==null||v==='')return '—';return Number(v).toLocaleString('en-IN',{maximumFractionDigits:d==null?2:d})}
function pc(v){if(v==null)return '<span class="mut">—</span>';return '<span class="'+(v>=0?'up':'dn')+'">'+(v>=0?'+':'')+Number(v).toFixed(2)+'%</span>'}

/* ---------- ATIP Scores / Signal History / other tables: filters ---------- */
function visibleLimit(){var e=document.getElementById('slim');return e?parseInt(e.value,10)||0:0}
window.ft=function(){
  var q=((document.getElementById('srch')||{}).value||'').trim().toLowerCase();
  var s=((document.getElementById('sf')||{}).value||'');
  var lim=visibleLimit(), shown=0, match=0, rows=document.querySelectorAll('#st tbody tr');
  for(var i=0;i<rows.length;i++){var r=rows[i];
    var ok=(!s||r.dataset.signal===s)&&(!q||(r.dataset.search||r.innerText.toLowerCase()).indexOf(q)>=0);
    if(ok){match++;}
    var show=ok&&(!lim||shown<lim); if(show)shown++;
    r.style.display=show?'':'none';}
  var c=document.getElementById('scnt'); if(c)c.textContent='Showing '+shown+' of '+match+(match!==rows.length?' matching ('+rows.length+' total)':'');
  save('srch',q);save('sf',s);save('slim',lim);
};
window.hft=function(){
  var q=((document.getElementById('hsrch')||{}).value||'').trim().toLowerCase();
  var s=((document.getElementById('hsf')||{}).value||'');
  var rows=document.querySelectorAll('#ht tbody tr'), m=0;
  for(var i=0;i<rows.length;i++){var r=rows[i];
    var ok=(!s||r.dataset.signal===s)&&(!q||(r.dataset.sym||'').toLowerCase().indexOf(q)>=0);
    if(ok)m++; r.style.display=ok?'':'none';}
  var c=document.getElementById('hcnt'); if(c)c.textContent=m+' of '+rows.length;
  save('hsrch',q);save('hsf',s);
};
function rowText(r){var t='';for(var i=0;i<r.cells.length;i++){if(!r.cells[i].classList.contains('acts'))t+=' '+r.cells[i].innerText}return t.toLowerCase()}
function addFilters(){
  document.querySelectorAll('.tc table').forEach(function(tb){
    if(tb.id==='st'||tb.id==='ht'||tb.dataset.filt)return;
    var body=tb.tBodies[0]; if(!body||body.rows.length<4)return;
    tb.dataset.filt='1';
    var w=document.createElement('div');w.className='tfilter';
    var inp=document.createElement('input');inp.placeholder='Filter this table…';
    var cnt=document.createElement('span');cnt.className='cnt';
    w.appendChild(inp);w.appendChild(cnt);tb.parentNode.insertBefore(w,tb);
    var key='f_'+(tb.closest('.tc')||{}).id+'_'+Array.prototype.indexOf.call(tb.parentNode.querySelectorAll('table'),tb);
    function run(){var q=inp.value.trim().toLowerCase(),m=0;
      for(var i=0;i<body.rows.length;i++){var r=body.rows[i],ok=!q||rowText(r).indexOf(q)>=0;if(ok)m++;r.style.display=ok?'':'none'}
      cnt.textContent=q?(m+' of '+body.rows.length):'';save(key,inp.value);}
    inp.addEventListener('input',run);
    var saved=st()[key]; if(saved){inp.value=saved;run();}
  });
}
/* keep the Show-limit and filters applied after a sort; remember the sort */
if(typeof srt==='function'){
  var _srt=srt;
  window.srt=function(tid,col){_srt(tid,col);
    var th=document.querySelectorAll('#'+tid+' thead th')[col];
    var o=st().sort||{};o[tid]={col:col,asc:th&&th.dataset.sorted==='asc'};save('sort',o);
    if(tid==='st')ft(); if(tid==='ht')hft();};
}
function restore(){
  var o=st();
  if(o.srch!=null&&document.getElementById('srch'))document.getElementById('srch').value=o.srch;
  if(o.sf!=null&&document.getElementById('sf'))document.getElementById('sf').value=o.sf;
  if(o.slim!=null&&document.getElementById('slim'))document.getElementById('slim').value=String(o.slim);
  if(o.hsrch!=null&&document.getElementById('hsrch'))document.getElementById('hsrch').value=o.hsrch;
  if(o.hsf!=null&&document.getElementById('hsf'))document.getElementById('hsf').value=o.hsf;
  var so=o.sort||{};
  Object.keys(so).forEach(function(tid){if(!document.getElementById(tid)||typeof ss==='undefined')return;
    ss[tid+so[tid].col]=!so[tid].asc; window.srt(tid,so[tid].col);});
  ft();hft();
}

/* ---------- stock panel ---------- */
window.openStock=function(sym){
  S.sym=sym;S.data=null;
  var b=document.getElementById('sv-back'),p=document.getElementById('sv');
  p.innerHTML='<div class="hd"><div><div class="sym">'+esc(sym)+'</div><div class="nm">loading history…</div></div><div class="btns"><button class="b-x" onclick="svClose()">✕</button></div></div>';
  b.classList.add('on');document.body.style.overflow='hidden';
  fetch('/api/stock/'+encodeURIComponent(sym)+'/history?sessions=2500').then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||r.status);return j})})
   .then(function(d){if(S.sym!==sym)return;S.data=d;render()})
   .catch(function(e){p.querySelector('.nm').textContent='No history: '+e.message});
};
window.svClose=function(){document.getElementById('sv-back').classList.remove('on');document.body.style.overflow='';S.sym=null};
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&S.sym)svClose()});
setInterval(function(){if(S.sym&&typeof _secsLeft!=='undefined'&&_secsLeft<60)_secsLeft=60},1000);
document.addEventListener('click',function(e){
  var t=e.target; if(!t.closest)return;
  if(t.closest('#sv-back')||t.closest('button,a,input,select,textarea,label,.acts,.modal'))return;
  var el=t.closest('[data-sym]'); if(!el)return;
  var sym=el.dataset.sym; if(!sym||sym==='—'||sym==='None')return;
  openStock(sym);
});

function cut(arr,key){var m={'1M':21,'3M':63,'6M':126,'1Y':250}[S.range];return m?arr.slice(-m):arr}
function render(){
  var d=S.data,s=d.stats,p=document.getElementById('sv');
  var live=(typeof liveCmpFor==='function')?liveCmpFor(d.symbol):null;
  var cmp=(live&&live>0)?live:s.last_close, chg=s.prev_close?((cmp-s.prev_close)/s.prev_close*100):null;
  var h='<div class="hd"><div><div class="sym">'+esc(d.symbol)+'</div><div class="nm">'+esc(d.name||'')+'</div></div>'+
   '<div><div class="px">₹'+n(cmp)+' '+pc(chg)+'</div><div class="nm">'+(live&&live>0?'live':'close '+esc(s.last_date))+'</div></div>'+
   '<div class="btns"><button class="b-buy" onclick="svOrder(\'BUY\')">Buy</button><button class="b-sell" onclick="svOrder(\'SELL\')">Sell</button><button class="b-x" onclick="svClose()">✕</button></div></div>';
  h+='<div class="kv">'+[['52W high',n(s.high_52w)],['52W low',n(s.low_52w)],['1W',pc(s.ret_1w)],['1M',pc(s.ret_1m)],['3M',pc(s.ret_3m)],['6M',pc(s.ret_6m)],['1Y',pc(s.ret_1y)],['Avg vol 20D',n(s.avg_volume_20d,0)]]
    .map(function(x){return '<div><span>'+x[0]+'</span><b>'+x[1]+'</b></div>'}).join('')+'</div>';
  h+='<div class="bar">'+['1M','3M','6M','1Y','All'].map(function(r){return '<button class="'+(S.range===r?'on':'')+'" onclick="svRange(\''+r+'\')">'+r+'</button>'}).join('')+
     '<span class="sp"></span><button class="'+(S.kind==='line'?'on':'')+'" onclick="svKind(\'line\')">Line</button><button class="'+(S.kind==='candle'?'on':'')+'" onclick="svKind(\'candle\')">Candles</button>'+
     '<button id="svpatbtn" class="'+(S.pat?'on':'')+'" onclick="svPat()" title="Chart patterns, technical signals and candle patterns">Patterns</button></div>';
  h+='<div id="svc"></div><div class="tip" id="svtip">Hover the chart for prices. ▲ BUY / ▼ SELL signals from the signal log.</div><div class="tip" id="svpl"></div>';
  var tabs=[['sc','Score history ('+d.scores.length+')'],['sg','Signals ('+d.signals.length+')'],['te','Technicals'],['po','Position & orders']];
  h+='<div class="tabs2">'+tabs.map(function(t,i){return '<div class="'+(i===0?'on':'')+'" onclick="svTab(\''+t[0]+'\',this)">'+t[1]+'</div>'}).join('')+'</div>';
  h+='<div class="pane on" id="sv-sc">'+scoresTable(d)+'</div><div class="pane" id="sv-sg">'+signalsTable(d)+'</div><div class="pane" id="sv-te">'+techTable(d)+'</div><div class="pane" id="sv-po">'+posTable(d)+'</div>';
  p.innerHTML=h; chart();
}
window.svRange=function(r){S.range=r;render()};
window.svKind=function(k){S.kind=k;render()};
window.svPat=function(){S.pat=!S.pat;save('svpat',S.pat);render()};
window.svTab=function(id,el){document.querySelectorAll('#sv .pane').forEach(function(x){x.classList.remove('on')});document.querySelectorAll('#sv .tabs2 div').forEach(function(x){x.classList.remove('on')});document.getElementById('sv-'+id).classList.add('on');el.classList.add('on')};
window.svOrder=function(side){var d=S.data;if(typeof openOrderModal!=='function'){alert('Order entry is not available on this page');return}
  var live=(typeof liveCmpFor==='function')?liveCmpFor(d.symbol):null;svClose();openOrderModal(d.symbol,(live&&live>0)?live:d.stats.last_close,side)};

function chart(){
  var d=S.data,P=cut(d.prices),W=1000,H=300,HV=60,HS=110,L=8,R=62,T=10,G=18;
  if(!P.length){document.getElementById('svc').innerHTML='';return}
  var hi=-1e18,lo=1e18,vmax=0;P.forEach(function(p){hi=Math.max(hi,+p.high||+p.close);lo=Math.min(lo,+p.low||+p.close);vmax=Math.max(vmax,+p.volume||0)});
  /* the lines of the patterns in place, cut to the visible range (they always run to the last bar) */
  var M=d.chart_marks||{},off=d.prices.length-P.length,gi={},segs=[],col={BULL:'#22c55e',BEAR:'#ef4444',BOTH:'#f59e0b'};
  if(S.pat){d.prices.forEach(function(p,i){gi[p.date]=i});
    (M.patterns||[]).forEach(function(pt){(pt.lines||[]).forEach(function(ln){var g0=gi[ln.from],g1=gi[ln.to];if(g0==null||g1==null||g1<off)return;
      var at=function(g){return g1>g0?ln.from_value+(ln.to_value-ln.from_value)*(g-g0)/(g1-g0):ln.to_value},s0=Math.max(g0,off);
      segs.push({pt:pt,ln:ln,i0:s0-off,v0:at(s0),i1:g1-off,v1:ln.to_value});hi=Math.max(hi,at(s0),ln.to_value);lo=Math.min(lo,at(s0),ln.to_value)})})}
  var pad=(hi-lo)*0.06||1;hi+=pad;lo-=pad;
  var cw=(W-L-R)/P.length,x=function(i){return L+cw*(i+0.5)},y=function(v){return T+(hi-v)/(hi-lo)*(H-T-HV-6)};
  var o='<svg viewBox="0 0 '+W+' '+(H+G+HS)+'" id="svsvg">';
  for(var k=0;k<=4;k++){var gv=lo+(hi-lo)*k/4,gy=y(gv);o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+gy+'" y2="'+gy+'" stroke="#1e293b"/><text x="'+(W-R+4)+'" y="'+(gy+4)+'" fill="#64748b" font-size="11">'+n(gv)+'</text>'}
  P.forEach(function(p,i){var vh=vmax?(+p.volume||0)/vmax*HV:0,up=i===0||+p.close>=+P[i-1].close;
    o+='<rect x="'+(x(i)-cw*0.4)+'" y="'+(H-vh)+'" width="'+Math.max(cw*0.8,0.6)+'" height="'+vh+'" fill="'+(up?'#05966955':'#dc262655')+'"/>'});
  if(S.kind==='candle'){P.forEach(function(p,i){var op=+p.open||+p.close,cl=+p.close,up=cl>=op,c=up?'#059669':'#dc2626';
      o+='<line x1="'+x(i)+'" x2="'+x(i)+'" y1="'+y(+p.high||cl)+'" y2="'+y(+p.low||cl)+'" stroke="'+c+'"/>'+
         '<rect x="'+(x(i)-Math.max(cw*0.35,0.5))+'" y="'+y(Math.max(op,cl))+'" width="'+Math.max(cw*0.7,1)+'" height="'+Math.max(Math.abs(y(op)-y(cl)),1)+'" fill="'+c+'"/>'})}
  else{var up=+P[P.length-1].close>=+P[0].close,col=up?'#059669':'#dc2626',path=P.map(function(p,i){return(i?'L':'M')+x(i).toFixed(1)+' '+y(+p.close).toFixed(1)}).join(' ');
    o+='<path d="'+path+' L'+x(P.length-1)+' '+(H-HV-6)+' L'+x(0)+' '+(H-HV-6)+' Z" fill="'+col+'18"/><path d="'+path+'" fill="none" stroke="'+col+'" stroke-width="1.8"/>'}
  var idx={};P.forEach(function(p,i){idx[p.date]=i});
  d.signals.forEach(function(sg){var i=idx[sg.signal_date];if(i==null)return;var p=P[i],buy=sg.signal==='BUY';
    var yy=buy?y(+p.low||+p.close)+14:y(+p.high||+p.close)-6;
    o+='<text x="'+x(i)+'" y="'+yy+'" text-anchor="middle" font-size="13" fill="'+(buy?'#22c55e':'#ef4444')+'">'+(buy?'▲':'▼')+'</text>'});
  /* technical signals (◆ chart-pattern breakout, ● any other scan), candle patterns (•) and pattern lines */
  var sigBy={},cdlBy={},inBy={};
  (M.signals||[]).forEach(function(z){(sigBy[z.date]=sigBy[z.date]||[]).push(z)});
  (M.candles||[]).forEach(function(z){(cdlBy[z.date]=cdlBy[z.date]||[]).push(z)});
  (M.in_place||[]).forEach(function(z){inBy[z.date]=z.text});
  if(S.pat){var cl=function(v){return Math.max(T+4,Math.min(H-HV-4,v))};o+='<g id="svmarks">';
    segs.forEach(function(g){var c=col[g.pt.side]||col.BOTH,y0=y(g.v0),y1=y(g.v1),t=g.pt.name+': '+g.ln.label.toLowerCase()+' '+n(g.v1);
      o+='<line class="svm-line" x1="'+x(g.i0)+'" y1="'+y0+'" x2="'+x(g.i1)+'" y2="'+y1+'" stroke="'+c+'" stroke-width="1.4" stroke-dasharray="6 4"><title>'+esc(t)+'</title></line>'+
         '<text x="'+(x(g.i1)-4)+'" y="'+(y1-4)+'" text-anchor="end" font-size="10.5" fill="'+c+'">'+esc(g.ln.label+' '+n(g.v1))+'</text>'});
    P.forEach(function(p,i){var hy=y(+p.high||+p.close),ly=y(+p.low||+p.close);
      (cdlBy[p.date]||[]).forEach(function(z){if(z.side!=='BULL'&&z.side!=='BEAR')return;
        o+='<circle class="svm-cdl" cx="'+x(i)+'" cy="'+cl(z.side==='BULL'?ly+5:hy-5)+'" r="2.2" fill="'+col[z.side]+'"/>'});
      var ss=sigBy[p.date];if(!ss)return;
      ['BULL','BEAR'].forEach(function(dir){var g=ss.filter(function(z){return z.direction===dir});if(!g.length)return;var b=dir==='BULL';
        if(g.some(function(z){return z.pattern}))o+='<text class="svm-pat" x="'+x(i)+'" y="'+cl(b?ly+27:hy-18)+'" text-anchor="middle" font-size="13" fill="'+col[dir]+'">◆</text>';
        else o+='<circle class="svm-sig" cx="'+x(i)+'" cy="'+cl(b?ly+22:hy-22)+'" r="3" fill="'+col[dir]+'" fill-opacity=".85"/>'})});
    o+='</g>'}
  var sm={};d.scores.forEach(function(s){sm[s.date]=s});
  var sy=function(v){return H+G+(100-v)/100*(HS-8)+4};
  o+='<text x="'+L+'" y="'+(H+G-4)+'" fill="#64748b" font-size="11">ATIP score</text>';
  [45,70].forEach(function(b){o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+sy(b)+'" y2="'+sy(b)+'" stroke="#334155" stroke-dasharray="3 4"/><text x="'+(W-R+4)+'" y="'+(sy(b)+4)+'" fill="#64748b" font-size="11">'+b+'</text>'});
  var seg='',started=false;P.forEach(function(p,i){var s=sm[p.date];if(!s||s.atip_score==null){started=false;return}seg+=(started?'L':'M')+x(i).toFixed(1)+' '+sy(+s.atip_score).toFixed(1)+' ';started=true});
  o+=seg?'<path d="'+seg+'" fill="none" stroke="#38bdf8" stroke-width="1.8"/>':'<text x="'+(W/2)+'" y="'+(H+G+HS/2)+'" fill="#475569" font-size="12" text-anchor="middle">no ATIP scores in this range</text>';
  P.forEach(function(p,i){var s=sm[p.date];if(s&&s.atip_score!=null)o+='<circle cx="'+x(i)+'" cy="'+sy(+s.atip_score)+'" r="1.8" fill="#38bdf8"/>'});
  o+='<line id="svx" x1="0" x2="0" y1="'+T+'" y2="'+(H+G+HS)+'" stroke="#94a3b8" stroke-dasharray="2 3" visibility="hidden"/>';
  o+='<rect x="'+L+'" y="0" width="'+(W-L-R)+'" height="'+(H+G+HS)+'" fill="transparent" id="svhit"/></svg>';
  document.getElementById('svc').innerHTML=o;
  var svg=document.getElementById('svsvg'),hit=document.getElementById('svhit'),vx=document.getElementById('svx'),tip=document.getElementById('svtip');
  hit.addEventListener('mousemove',function(ev){var r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)/r.width*W,i=Math.max(0,Math.min(P.length-1,Math.floor((px-L)/cw)));
    var p=P[i],s=sm[p.date],sg=d.signals.filter(function(z){return z.signal_date===p.date})[0];
    var ts=S.pat?(sigBy[p.date]||[]):[],cd=S.pat?(cdlBy[p.date]||[]):[],ip=S.pat?inBy[p.date]:null;
    vx.setAttribute('x1',x(i));vx.setAttribute('x2',x(i));vx.setAttribute('visibility','visible');
    tip.innerHTML='<b>'+esc(p.date)+'</b> &nbsp;O '+n(p.open)+' H '+n(p.high)+' L '+n(p.low)+' C <b>'+n(p.close)+'</b> &nbsp;Vol '+n(p.volume,0)+
      (s?' &nbsp;| ATIP <b>'+n(s.atip_score,0)+'</b> '+esc(s.signal||''):'')+(sg?' &nbsp;| <b style="color:'+(sg.signal==='BUY'?'#22c55e':'#ef4444')+'">'+esc(sg.signal)+' signal</b> @ '+n(sg.entry_price):'')+
      (ts.length?' &nbsp;| '+ts.map(function(z){return '<b style="color:'+(col[z.direction]||col.BOTH)+'">'+(z.pattern?'◆ ':'')+esc(z.name)+'</b>'+(z.reason?' <span class="mut">'+esc(z.reason)+'</span>':'')}).join(', '):'')+
      (cd.length?' &nbsp;| candles: '+cd.map(function(z){return '<span style="color:'+(col[z.side]||'#94a3b8')+'">'+esc(z.name)+'</span>'}).join(', '):'')+
      (ip?' &nbsp;| in place: <span class="mut">'+esc(ip)+'</span>':'')});
  hit.addEventListener('mouseleave',function(){vx.setAttribute('visibility','hidden')});
  var pl=document.getElementById('svpl'),pts=M.patterns||[];
  if(pl)pl.innerHTML=!S.pat?'<span class="mut">Chart patterns, technical signals and candle patterns hidden (Patterns).</span>':
    (pts.length?'In place on the last bar: '+pts.map(function(pt){return '<b style="color:'+(col[pt.side]||col.BOTH)+'">'+esc(pt.name)+'</b> '+
      (pt.lines||[]).map(function(l){return esc(l.label.toLowerCase())+' '+n(l.to_value)}).join(' / ')+
      (pt.triggered?' <b>· '+(/breakdown$/.test(pt.triggered)?'broke down':'broke out')+' today</b>':'')}).join(' &nbsp;·&nbsp; ')
      :'<span class="mut">No chart pattern in place on the last bar.</span>')+
    ' <span class="mut">&nbsp;◆ chart-pattern breakout · ● other technical signal · • candle pattern (the 20:30 signal run)</span>';
}
function tbl(h,rows,empty){return '<table><thead><tr>'+h.map(function(x){return '<th>'+x+'</th>'}).join('')+'</tr></thead><tbody>'+(rows.join('')||'<tr><td colspan="'+h.length+'" class="mut" style="text-align:center;padding:14px">'+empty+'</td></tr>')+'</tbody></table>'}
function scoresTable(d){return tbl(['Date','ATIP','Rank','VPI','MRI','RRI','ZPI','CRI','ACS','Signal','Regime','Top factor'],d.scores.slice().reverse().map(function(s){
  return '<tr><td>'+esc(s.date)+'</td><td><b>'+n(s.atip_score,0)+'</b></td><td>'+n(s.atip_rank,0)+'</td><td>'+n(s.vpi,0)+'</td><td>'+n(s.mri,0)+'</td><td>'+n(s.rri,0)+'</td><td>'+n(s.zpi,0)+'</td><td>'+n(s.cri,0)+'</td><td>'+n(s.acs,0)+'</td><td>'+esc(s.signal||'')+'</td><td class="mut">'+esc(s.regime||'')+'</td><td class="mut">'+esc(s.top_factor_1||'')+'</td></tr>'}),'No ATIP scores stored for this stock.')}
function signalsTable(d){return tbl(['Issued','Signal','Entry','ATIP','First target hit','Best','Worst','Sessions','Model'],d.signals.map(function(s){var f=s.first_hit;
  return '<tr><td>'+esc(s.signal_date)+'</td><td style="font-weight:600;color:'+(s.signal==='BUY'?'#059669':'#dc2626')+'">'+esc(s.signal)+'</td><td>₹'+n(s.entry_price)+'</td><td>'+n(s.atip_score,0)+'</td><td>'+(f?esc(String(f.hit_date).slice(0,10))+' @'+f.threshold_pct+'%':'<span class="mut">—</span>')+'</td><td>'+pc(s.best_pct)+'</td><td>'+pc(s.worst_pct)+'</td><td>'+n(s.sessions_tracked,0)+'</td><td class="mut">'+esc(s.model_version||'')+'</td></tr>'}),'No BUY / SELL signals logged for this stock.')}
function techTable(d){var t=d.technicals||{},e=d.technical_ext||{};
  var rows=[['As of',t.date||e.date],['RSI 14',t.rsi_14],['MACD hist',t.macd_hist],['ADX 14',t.adx_14],['EMA 21',t.ema_21],['EMA 50',t.ema_50],['SMA 200',t.sma_200],['Above 200 DMA',t.above_200dma==null?null:(t.above_200dma?'yes':'no')],['ATR %',t.atr_pct],['Bollinger upper / lower',t.bb_upper!=null?n(t.bb_upper)+' / '+n(t.bb_lower):null],['Pivot / R1 / S1',t.pivot!=null?n(t.pivot)+' / '+n(t.r1)+' / '+n(t.s1):null],['Volume ratio',t.volume_ratio],
    ['Support (touches)',e.sr_support!=null?n(e.sr_support)+' ('+n(e.sr_support_touches,0)+')':null],['Resistance (touches)',e.sr_resistance!=null?n(e.sr_resistance)+' ('+n(e.sr_resistance_touches,0)+')':null],['VWAP 20D',e.vwap_20d],['Supertrend',e.supertrend!=null?n(e.supertrend)+' ('+esc(e.supertrend_dir)+')':null],['Weekly / monthly trend',e.weekly_trend!=null?esc(e.weekly_trend)+' / '+esc(e.monthly_trend):null],['MTF alignment',e.mtf_alignment],['Beta 60D',e.beta_60],['MFI 14',e.mfi_14]]
    .filter(function(r){return r[1]!=null&&r[1]!==''});
  return tbl(['Indicator','Value'],rows.map(function(r){return '<tr><td>'+r[0]+'</td><td><b>'+(typeof r[1]==='number'?n(r[1]):esc(r[1]))+'</b></td></tr>'}),'No technical indicators stored for this stock.')}
function posTable(d){var h=d.holding,pp=d.paper_position,o='';
  o+='<div class="kv">'+(h?[['LIVE qty',n(h.qty,0)],['Avg price',n(h.avg_price)],['Value',n(h.current_val)],['P&L',n(h.pnl)+' ('+pc(h.pnl_pct)+')'],['Weight',h.weight_pct==null?'—':n(h.weight_pct)+'%'],['Synced',esc(String(h.date).slice(0,10))]]:[['LIVE holding','none']]).map(function(x){return '<div><span>'+x[0]+'</span><b>'+x[1]+'</b></div>'}).join('')+
     (pp?'<div><span>PAPER qty</span><b>'+n(pp.quantity,0)+' @ '+n(pp.avg_price)+'</b></div>':'')+'</div>';
  o+='<div class="nm" style="margin:6px 0 4px">Order rules</div>'+tbl(['Created','Side','Trigger','Price','Qty','Status'],d.order_rules.map(function(r){return '<tr><td>'+esc(String(r.created_at||'').slice(0,16))+'</td><td>'+esc(r.side)+'</td><td>'+esc(r.trigger_type)+' '+n(r.trigger_value)+'</td><td>'+n(r.resolved_trigger_price)+'</td><td>'+esc(r.quantity_type)+' '+n(r.quantity_value)+'</td><td>'+esc(r.status)+'</td></tr>'}),'No order rules.');
  o+='<div class="nm" style="margin:10px 0 4px">Orders placed</div>'+tbl(['When','Side','Qty','Type','Price','Mode','Status'],d.orders.map(function(r){return '<tr><td>'+esc(String(r.timestamp||'').slice(0,16))+'</td><td>'+esc(r.transaction_type)+'</td><td>'+n(r.quantity,0)+'</td><td>'+esc(r.order_type)+'</td><td>'+n(r.price)+'</td><td>'+esc(r.mode)+'</td><td>'+esc(r.status)+(r.error?' <span class="mut">'+esc(r.error)+'</span>':'')+'</td></tr>'}).concat(d.oms_orders.map(function(r){return '<tr><td>'+esc(String(r.created_at||'').slice(0,16))+'</td><td>'+esc(r.side)+'</td><td>'+n(r.quantity,0)+'</td><td>strategy '+esc(r.strategy_id)+'</td><td>'+n(r.avg_fill_price)+'</td><td>W4</td><td>'+esc(r.status)+'</td></tr>'})),'No orders for this stock.');
  return o}

addFilters();restore();
})();
</script>
"""
