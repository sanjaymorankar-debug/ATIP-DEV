"""
/algo-signals: the Kite high-volume breakout and retest scanner (strategy/breakout_retest.py, KGF-10, owner prompt
15 Nov 2026 item 10) as a dashboard tab ("Algo Signals", under Reports next to the post-market RCA). Times are IST.

    GET /algo-signals                       the page: summary (triggered breakouts, stocks scanned, live ticks), the
                                            "Triggered Breakouts" table, the selected row's bid and candles
    GET /api/scanner-data                   the last stored run (latest_scan), ranked, with each row's current price;
                                            a run is made and stored only when none is stored yet
    GET /api/scanner-data?refresh=1         run the scanner now and store it (needs X-ATIP-Token; refused on the
        [&as_of=YYYY-MM-DD]                 view-only server, which reads the Mac's runs from its copied database)
    GET /api/scanner-data?prices=1          the current prices only, for the page's 15 s poll

THE RANKING STAYS FIXED until the scanner runs again: the page and the order engine both read the stored run, and
the live price only moves the "Current Price" cell (and the selected row's "distance now"), never the order.

CURRENT PRICE, per stock: the newest WebSocket LTP today (live_quotes from the stock feed), else the newest OHLC
snapshot LTP today (the quote poll's live_quotes rows, or this dashboard's own 15 s quote fetch), else the last close.
One tick state is shared by every open tab: the tab that fetches writes it to localStorage and a BroadcastChannel
("atip-ticks"), and a tab that finds a fresh state there (or is hidden) uses it instead of asking the server again.

Viewer (ops/role.py): the page and the reads work; a refresh that would write the scan tables answers 403, and with
no stored run the viewer scans its copy in memory without storing it.
"""

import re
import threading
from datetime import date, datetime

from dashboard import ux_kit as K

e = K.e

PATH = "/algo-signals"
_run_lock = threading.Lock()          # two tabs opening the page before any run is stored make one run, not two
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _overlay_quote_cache(px: dict, syms: list) -> None:
    """A stock with no live_quotes row today may still have a price from this dashboard's own 15 s quote fetch
    (dashboard/server.py /api/live-quotes, an OHLC snapshot from Dhan): use it before falling back to the close."""
    try:
        from dashboard import server as S
        c = S._live_quotes_cache
        today = str(date.today())
        if c.get("date") != today or not c.get("data"):
            return
        for s in syms:
            q = c["data"].get(s) or {}
            if q.get("ltp") and px.get(s, {}).get("source") in (None, "close"):
                px[s] = {"ltp": float(q["ltp"]), "source": "snapshot", "at": f"{today} {c.get('ts') or ''}".strip()}
    except Exception:
        pass


def prices_payload(conn, syms: list) -> dict:
    from strategy.breakout_retest import live_prices
    px = live_prices(conn, syms)
    _overlay_quote_cache(px, syms)
    counts = {"ws": 0, "snapshot": 0, "close": 0}
    for p in px.values():
        counts[p["source"]] = counts.get(p["source"], 0) + 1
    try:
        feed = K.feed_label(conn)
    except Exception:
        feed = {"state": "none", "tone": "", "text": "Live feed state unavailable", "market_open": False}
    return {"prices": px, "price_sources": counts, "feed": feed,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def scanner_data(conn, refresh: bool = False, as_of: str | None = None, viewer: bool = False) -> dict:
    """The stored run (or a new one), each row with "ltp" {ltp, source, at}, plus the feed state and whether newer
    candles than the run's exist."""
    from strategy import breakout_retest as B
    scan = None if refresh else B.latest_scan(conn)
    if scan is None:
        with _run_lock:
            scan = None if refresh else B.latest_scan(conn)
            if scan is None:
                if viewer:
                    scan = {**B.scan_universe(conn, as_of), "run_id": None, "run_at": B._now_ist(), "stored": False,
                            "source": "viewer (not stored)"}
                else:
                    scan = B.run_scan(conn, as_of, source="refresh" if refresh else "api")
    syms = [r["symbol"] for r in scan["rows"]]
    live = prices_payload(conn, syms)
    for r in scan["rows"]:
        r["ltp"] = live["prices"].get(r["symbol"])
    try:
        newest = conn.execute("SELECT MAX(date) FROM prices_daily WHERE series='EQ'").fetchone()[0]
        newest = B._iso(newest)
    except Exception:
        newest = None
    return {**scan, "title": B.TITLE, "count": len(scan["rows"]), **live, "latest_candle": newest,
            "outdated": bool(newest and scan.get("as_of") and newest > scan["as_of"])}


# -- the page -------------------------------------------------------------------------------------------------------------

def _kpi(kid: str, label: str, value: str, sub: str = "", tone: str = "") -> str:
    return (f'<div class="ax-kpi" id="{kid}" data-tone="{tone}" role="group" aria-label="{e(label)}">'
            f'<div class="ax-lbl">{e(label)}</div><div class="ax-val" data-k="val">{value}</div>'
            f'<div class="ax-sub2" data-k="sub">{e(sub)}</div></div>')


_CSS = """<style>
.algo-table th { padding: 6px 8px; }
.algo-table td { padding: 5px 8px; vertical-align: middle; }
.algo-table tbody tr[data-sym] { cursor: pointer; }
.algo-table tbody tr[data-sym]:focus-visible { outline: 2px solid var(--ax-focus); outline-offset: -2px; }
.ax-tablewrap .algo-table tbody tr[aria-selected="true"] td { background: var(--ax-brand-soft); }
.ax-tablewrap .algo-table tbody tr[aria-selected="true"] td:first-child { box-shadow: inset 3px 0 0 var(--ax-brand); }
.algo-sym { font-weight: 700; font-size: 14px; color: var(--ax-text); letter-spacing: .02em; }
.algo-src { font-size: 10.5px; color: var(--ax-muted); margin-left: 4px; text-transform: uppercase; letter-spacing: .03em; }
.algo-rank { font-family: var(--ax-mono, ui-monospace, monospace); color: var(--ax-text-2); }
.algo-detail .ax-kv { margin: 0; }
.algo-spark { width: 100%; max-width: 520px; height: auto; display: block; margin: 8px 0 4px; }
.algo-spark .c { fill: none; stroke: var(--ax-series-1); stroke-width: 2; }
.algo-spark .m { stroke: var(--ax-text-2); stroke-width: 1.2; stroke-dasharray: 5 4; }
.algo-spark .b { stroke: var(--ax-pos); stroke-width: 1.2; stroke-dasharray: 2 3; }
.algo-spark .s { stroke: var(--ax-neg); stroke-width: 1.2; stroke-dasharray: 2 3; }
.algo-spark .k { fill: var(--ax-amber); }
.algo-spark text { fill: var(--ax-muted); font-size: 10px; }
</style>"""

COLUMNS = [("Rank", True), ("Symbol", False), ("Current Price", True), ("Breakout Date", False), ("Age", True),
           ("Breakout Vol", True), ("Retest Distance", True), ("Peak Gains", True), ("Retracement", True)]


def page_html(root: str = "", app: bool = False, viewer: bool = False) -> str:
    from dashboard.shell import page
    from strategy.breakout_retest import MAX_AGE, MID_TOL_PCT, PRICE_LOOKBACK, TITLE, VOL_LOOKBACK, VOL_MULT
    actions = "" if viewer else ('<button class="ax-btn sm" type="button" id="algo-run" '
                                 'title="Scan prices_daily again and replace the ranking">Run scanner</button>')
    dash = '<span class="ax-muted">—</span>'
    kpis = ('<div class="ax-kpis" aria-label="Scanner summary">' +
            _kpi("algo-k-count", "Triggered breakouts", dash, "ranked by age, then distance from the midpoint") +
            _kpi("algo-k-scanned", "Stocks scanned", dash, "the scored universe, EQ daily candles") +
            _kpi("algo-k-feed", "Live ticks", dash, "current price: WebSocket, else snapshot, else close") +
            _kpi("algo-k-run", "Scanner run", dash, "ranking fixed until the next run") + "</div>")
    head = "".join(f'<th scope="col" data-nosort class="{"n" if num else ""}">{e(l)}</th>' for l, num in COLUMNS)
    rule = (f"A green daily candle that closed above the previous {PRICE_LOOKBACK} candles' high on at least "
            f"{VOL_MULT}× their average volume ({VOL_LOOKBACK} candles), a later high above it, and the price now "
            f"within ±{MID_TOL_PCT:g} % of that candle's midpoint, at most {MAX_AGE} candles ago. One setup a stock, "
            f"the most recent. The order engine bids at the breakout candle's low (tick-rounded down), stop 5 % below; "
            f"never at the live price.")
    table = (f'<section class="ax-card" aria-labelledby="algo-h"><h2 id="algo-h">Triggered Breakouts</h2>'
             f'<div class="ax-tablewrap"><table class="ax-table algo-table" id="algo-table" aria-describedby="algo-rule">'
             f'<thead><tr>{head}</tr></thead><tbody id="algo-body"><tr><td colspan="{len(COLUMNS)}">'
             f'<div class="ax-empty" role="status">Loading scanner cache...</div></td></tr></tbody></table></div>'
             f'<p class="ax-small ax-muted" id="algo-rule">{e(rule)} Click a row for its bid, stop and candles.</p>'
             f'</section>')
    detail = ('<section class="ax-card algo-detail" id="algo-detail" hidden aria-live="polite">'
              '<h2 id="algo-d-h">Setup</h2><div id="algo-d-body"></div></section>')
    body = (_CSS + kpis + '<div class="ax-banner info" id="algo-note" role="status" hidden></div>'
            '<div class="ax-banner bad" id="algo-error" role="alert" hidden></div>' + table + detail + _js())
    return page(TITLE, body, PATH, root=root, app=app, actions=actions,
                job="Kite breakout-retest scan of ATIP's daily candles: what the breakout arm would bid on. IST.")


def _js() -> str:
    # atip.js (window.AX) is a deferred script: it runs after this inline one, before DOMContentLoaded
    return r"""<script>(function(){"use strict";function boot(){
var API='/api/scanner-data',KEY='atip-ticks',CH='atip-ticks',LIVE_MS=15000,IDLE_MS=300000;
var $=function(i){return document.getElementById(i);};
var body=$('algo-body'),errBox=$('algo-error'),note=$('algo-note'),det=$('algo-detail'),btn=$('algo-run');
var rows=[],sel=null,timer=null,marketOpen=false,chan=null,ncol=9;
var SRC={ws:'WebSocket',snapshot:'Snapshot',close:'Last close'},TAG={ws:'ws',snapshot:'snap',close:'close'};
function esc(s){return AX.esc(s);}
function pct(v){return (v===null||v===undefined||isNaN(v))?'—':AX.pct(v);}
function tone(v){return v>0?'ax-pos':v<0?'ax-neg':'';}
function msgRow(t,role){return '<tr><td colspan="'+ncol+'"><div class="ax-empty"'+(role?' role="'+role+'"':'')+'>'+esc(t)+'</div></td></tr>';}
function showError(m){errBox.hidden=!m;errBox.textContent=m?'Scanner data could not be loaded: '+m:'';}
function kpi(id,val,sub,t){var k=$(id);if(!k)return;k.querySelector('[data-k=val]').innerHTML=val;if(sub!==undefined)k.querySelector('[data-k=sub]').textContent=sub;k.setAttribute('data-tone',t||'');}
function cur(r){var p=r.ltp||{};return p.ltp!=null?p.ltp:r.currentPrice;}
function rowHtml(r){var p=r.ltp||{},s=p.source||'close';
return '<tr data-sym="'+esc(r.symbol)+'" tabindex="0" aria-selected="'+(sel===r.symbol)+'">'+
'<td class="n algo-rank">'+r.rank+'</td>'+
'<td><span class="algo-sym"'+(r.companyName?' title="'+esc(r.companyName)+'"':'')+'>'+esc(r.symbol)+'</span></td>'+
'<td class="n"><span class="ax-num" data-px="'+esc(r.symbol)+'">'+AX.inr(cur(r))+'</span><span class="algo-src" data-src="'+esc(r.symbol)+'" title="'+esc(SRC[s]||s)+'">'+esc(TAG[s]||s)+'</span></td>'+
'<td>'+esc(r.breakoutDate)+'</td><td class="n">'+r.daysSince+'</td>'+
'<td class="n">'+Number(r.volMultiple).toFixed(2)+'x</td>'+
'<td class="n">'+pct(r.distanceFromMidpoint)+'</td>'+
'<td class="n"><span class="'+tone(r.returnFromCloseToSubHigh)+'">'+pct(r.returnFromCloseToSubHigh)+'</span></td>'+
'<td class="n">'+pct(r.retracementHigh)+'</td></tr>';}
function spark(r){var c=r.candles||[];if(c.length<2)return '';var w=520,h=120,pl=6,pr=74,t=8,b=14,v=[];
c.forEach(function(x){v.push(x.low,x.high);});v.push(r.midpoint,r.buy_price,r.stop_price);
var lo=Math.min.apply(null,v),hi=Math.max.apply(null,v),sp=(hi-lo)||1;lo-=sp*.04;hi+=sp*.04;
var X=function(i){return pl+i*(w-pl-pr)/(c.length-1);},Y=function(y){return t+(hi-y)/(hi-lo)*(h-t-b);};
var d=c.map(function(x,i){return (i?'L':'M')+X(i).toFixed(1)+','+Y(x.close).toFixed(1);}).join(' ');
var ln=function(cls,y,lab){return '<line class="'+cls+'" x1="'+pl+'" x2="'+(w-pr)+'" y1="'+Y(y).toFixed(1)+'" y2="'+Y(y).toFixed(1)+'"/><text x="'+(w-pr+4)+'" y="'+(Y(y)+3).toFixed(1)+'">'+esc(lab)+'</text>';};
var bi=r.bIndex,bx=X(bi);
return '<svg class="algo-spark" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'+esc(r.symbol+': daily closes from '+c[0].date+' to '+c[c.length-1].date+', breakout '+r.breakoutDate)+'">'+
ln('m',r.midpoint,'Mid '+r.midpoint.toFixed(2))+ln('b',r.buy_price,'Buy '+r.buy_price.toFixed(2))+ln('s',r.stop_price,'Stop '+r.stop_price.toFixed(2))+
'<path class="c" d="'+d+'"/><circle class="k" cx="'+bx.toFixed(1)+'" cy="'+Y(c[bi].close).toFixed(1)+'" r="4"><title>Breakout '+esc(r.breakoutDate)+'</title></circle>'+
'<text x="'+pl+'" y="'+(h-2)+'">'+esc(c[0].date)+'</text><text x="'+(w-pr)+'" y="'+(h-2)+'" text-anchor="end">'+esc(c[c.length-1].date)+'</text></svg>';}
function flags(r){var n={nifty50:'Nifty 50',next50:'Nifty Next 50',midcap150:'Midcap 150',smallcap250:'Smallcap 250',nifty500:'Nifty 500'},o=[];
Object.keys(n).forEach(function(k){if(r[k]===true)o.push(n[k]);});return o.length?o.join(', '):'—';}
function detail(){var r=null;rows.forEach(function(x){if(x.symbol===sel)r=x;});if(!r){det.hidden=true;return;}
var p=r.ltp||{},now=cur(r),dn=r.midpoint?(now-r.midpoint)/r.midpoint*100:null;
var kv=function(k,v){return '<dt>'+esc(k)+'</dt><dd>'+v+'</dd>';};
$('algo-d-h').textContent='#'+r.rank+' '+r.symbol+(r.companyName?' · '+r.companyName:'');
$('algo-d-body').innerHTML=spark(r)+'<dl class="ax-kv">'+
kv('Buy at (breakout low, tick-rounded down)','<b class="ax-num">'+AX.inr(r.buy_price)+'</b>')+
kv('Stop (buy × 0.95, rounded down)','<span class="ax-num">'+AX.inr(r.stop_price)+'</span> · risk '+AX.inr(r.buy_price-r.stop_price)+' a share')+
kv('Tick',String(r.tick_size))+
kv('Breakout candle',esc(r.breakoutDate)+' · low '+AX.inr(r.breakoutLow)+' · high '+AX.inr(r.breakoutHigh)+' · close '+AX.inr(r.breakoutClose))+
kv('Midpoint',AX.inr(r.midpoint))+kv('Previous 20-candle high',AX.inr(r.prevHigh))+kv('Highest high after',AX.inr(r.maxPriceAfter))+
kv('Breakout volume',Number(r.volMultiple).toFixed(2)+'x the 20-candle average')+
kv('Current price',AX.inr(now)+' · '+esc(SRC[p.source]||'Last close')+(p.at?' · '+esc(p.at)+(String(p.at).length>10?' IST':''):''))+
kv('Distance now',pct(dn)+' (ranked on '+pct(r.distanceFromMidpoint)+' at the scan)')+
kv('Index',esc(flags(r)))+'</dl>';det.hidden=false;}
function paint(){if(!rows.length){body.innerHTML=msgRow('No breakout retest setups match the current parameters.');det.hidden=true;return;}
body.innerHTML=rows.map(rowHtml).join('');detail();}
function select(sym){sel=sel===sym?null:sym;Array.prototype.forEach.call(body.rows,function(tr){tr.setAttribute('aria-selected',String(tr.getAttribute('data-sym')===sel));});detail();}
body.addEventListener('click',function(ev){var tr=ev.target.closest('tr[data-sym]');if(tr)select(tr.getAttribute('data-sym'));});
body.addEventListener('keydown',function(ev){if(ev.key!=='Enter'&&ev.key!==' ')return;var tr=ev.target.closest('tr[data-sym]');if(tr){ev.preventDefault();select(tr.getAttribute('data-sym'));}});
function feedKpi(f,counts){if(!f)return;marketOpen=!!f.market_open;var c=counts||{};
kpi('algo-k-feed',esc(f.short||f.text||'—'),(f.text||'')+(rows.length?' · prices: '+(c.ws||0)+' WebSocket, '+(c.snapshot||0)+' snapshot, '+(c.close||0)+' last close':''),f.tone||'');}
function applyTicks(st){if(!st||!st.prices)return;var c={ws:0,snapshot:0,close:0};
rows.forEach(function(r){var p=st.prices[r.symbol];if(p&&p.ltp!=null){var old=cur(r);r.ltp=p;
var el=body.querySelector('[data-px="'+r.symbol+'"]'),sr=body.querySelector('[data-src="'+r.symbol+'"]');
if(el&&p.ltp!==old){el.textContent=AX.inr(p.ltp);if(AX.flash)AX.flash(el,p.ltp>old?1:-1);}if(sr){sr.textContent=TAG[p.source]||p.source;sr.title=SRC[p.source]||p.source;}}
var k=(r.ltp&&r.ltp.source)||'close';c[k]=(c[k]||0)+1;});
feedKpi(st.feed,c);if(sel)detail();}
function readShared(){try{var s=localStorage.getItem(KEY);return s?JSON.parse(s):null;}catch(x){return null;}}
function share(st){var old=readShared()||{},m={at:st.at,feed:st.feed,prices:Object.assign({},old.prices||{},st.prices||{})};
try{localStorage.setItem(KEY,JSON.stringify(m));}catch(x){}try{if(chan)chan.postMessage(m);}catch(x){}}
try{if('BroadcastChannel' in window){chan=new BroadcastChannel(CH);chan.onmessage=function(ev){applyTicks(ev&&ev.data);};}}catch(x){chan=null;}
window.addEventListener('storage',function(ev){if(ev.key===KEY&&ev.newValue){try{applyTicks(JSON.parse(ev.newValue));}catch(x){}}});
function every(){return marketOpen?LIVE_MS:IDLE_MS;}
function schedule(){clearTimeout(timer);timer=setTimeout(poll,every());}
function poll(){if(!rows.length){schedule();return;}var s=readShared(),fresh=s&&s.at&&Date.now()-s.at<every()-1000&&rows.every(function(r){return s.prices&&s.prices[r.symbol];});
if(fresh||(document.hidden&&s)){applyTicks(s);schedule();return;}
AX.api(API+'?prices=1').then(function(d){var st={at:Date.now(),feed:d.feed,prices:d.prices||{}};applyTicks(st);share(st);}).catch(function(){}).then(schedule);}
document.addEventListener('visibilitychange',function(){if(!document.hidden)poll();});
function render(d){showError('');rows=d.rows||[];if(sel&&!rows.some(function(r){return r.symbol===sel;}))sel=null;
kpi('algo-k-count',String(d.count!=null?d.count:rows.length),'ranked by age, then distance from the midpoint',rows.length?'good':'');
kpi('algo-k-scanned',String(d.scanned||0),'of '+(d.universe||0)+' scored · as of '+(d.as_of||'—'));
kpi('algo-k-run',esc(d.run_at?AX.istShort(d.run_at)+' IST':'—'),d.stored===false?'not stored (view-only copy)':'ranking fixed until the next run');
note.hidden=!d.outdated;note.textContent=d.outdated?'Newer candles ('+d.latest_candle+') are in the database than this run used ('+d.as_of+'). The ranking stays as it is until the scanner runs again.':'';
paint();feedKpi(d.feed,d.price_sources);share({at:Date.now(),feed:d.feed,prices:d.prices||{}});schedule();}
function load(refresh){showError('');if(refresh&&btn)btn.disabled=true;if(refresh)body.innerHTML=msgRow('Running the scanner...','status');
AX.api(API+(refresh?'?refresh=1':'')).then(render).catch(function(x){showError(x.message||String(x));if(!rows.length)body.innerHTML=msgRow('No scanner data: see the error above.');else paint();})
.then(function(){if(btn)btn.disabled=false;});}
if(btn)btn.addEventListener('click',function(){AX.ask({title:'Run the scanner now?',html:'<p>The ranking is replaced by a new scan of the newest daily candles. The breakout arm reads the new ranking from its next pass.</p>',ok:'Run scanner',cancel:'Keep this ranking'}).then(function(y){if(y)load(true);});});
load(false);}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();})();</script>"""


# -- routes -------------------------------------------------------------------------------------------------------------

def register(app, guard, Req, get_connection, json_safe):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import HTMLResponse, JSONResponse

    def with_conn(fn):
        conn = get_connection()
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.get(PATH, response_class=HTMLResponse)
    async def algo_signals_page(request: Req):
        from ops.role import is_viewer
        ua = request.headers.get("user-agent", "")
        return HTMLResponse(page_html(root=request.scope.get("root_path") or "",
                                      app="ATIPApp/" in ua or request.query_params.get("app") == "1",
                                      viewer=is_viewer()))

    @app.get("/api/scanner-data")
    async def scanner_data_api(request: Req, refresh: str | None = None, as_of: str | None = None,
                               prices: str | None = None):
        from ops.role import MESSAGE, is_viewer
        from dashboard.security import TOKEN_HEADER, guard_ok
        want = str(refresh or "").lower() in ("1", "true", "yes")
        if as_of and not _DAY.match(as_of):
            return JSONResponse({"error": "as_of must be YYYY-MM-DD"}, status_code=400)
        if str(prices or "").lower() in ("1", "true", "yes"):
            def only_prices(conn):
                from strategy.breakout_retest import latest_scan
                scan = latest_scan(conn) or {"rows": []}
                return prices_payload(conn, [r["symbol"] for r in scan["rows"]])
            return JSONResponse(json_safe(await run_in_threadpool(with_conn, only_prices)))
        viewer = is_viewer()
        if want:
            if viewer:
                return JSONResponse({"error": f"the scanner runs on the Mac: {MESSAGE}"}, status_code=403)
            if not guard_ok(request.headers.get(TOKEN_HEADER)):
                return JSONResponse({"error": f"{TOKEN_HEADER} missing or wrong: running the scanner stores a new "
                                              f"ranking"}, status_code=401)
        try:
            out = await run_in_threadpool(with_conn, lambda conn: scanner_data(conn, want, as_of, viewer))
        except Exception as ex:                  # shown above the table, never a blank page
            return JSONResponse({"error": f"{type(ex).__name__}: {ex}"}, status_code=500)
        return JSONResponse(json_safe(out))
