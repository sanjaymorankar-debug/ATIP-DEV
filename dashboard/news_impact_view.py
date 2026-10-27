"""
NI-05 (2026-10-27): the News Impact Estimate on the dashboard (data/news_impact.py).

    GET  /api/news-impact               the latest estimate (score, bias, sectors, drivers, brief), the last 40
                                        assessed headlines, and the tracking record
    GET  /api/news-impact/accuracy      the tracking record over ?days= (default 90)
    POST /api/news-impact/refresh       fetch the feeds and re-estimate now (token-guarded; refused while ATIP is
                                        stopped, like every write)

NI_JS renders the panel wherever a page has a container for it: the / "News" and "Global & Asia" tabs, the
/global-asia page and the /m Global & Asia screen (which the Android / iOS apps load). Informational only.
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

NI_CSS = """<style>
.ni-head{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;margin:4px 0 10px}
.ni-pos{color:var(--ga-up,#6ee7b7);font-weight:700}.ni-neg{color:var(--ga-dn,#fca5a5);font-weight:700}
.ni-neu{color:var(--ga-muted,#94a3b8);font-weight:700}.ni-wrap{white-space:normal!important;min-width:240px}
.ni-ul{margin:6px 0 0 18px;padding:0}.ni-ul li{margin:3px 0}
</style>"""

NI_JS = r"""<script>
(function(){
 var esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})};
 var n=function(v,d){return v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d==null?1:d})};
 var BC={POSITIVE:'ni-pos',NEGATIVE:'ni-neg',NEUTRAL:'ni-neu'};
 var DIR={UP:'<span class="ni-pos">▲ up</span>',DOWN:'<span class="ni-neg">▼ down</span>',NONE:'<span class="ni-neu">—</span>'};
 var lab=function(s){return esc(String(s||'').replace(/_/g,' ').toLowerCase())};
 function render(box,d){
  var L=d.latest,A=d.accuracy||{},h='';
  if(!L){box.innerHTML='<div class="ga-muted">No news impact estimate yet — it runs at 06:00 (resume), 07:40, 12:20, 15:50 and 20:15 IST.</div>';return;}
  h+='<div class="ga-cards"><div class="ga-card"><div class="ga-muted">News impact on Indian equities · session '+esc(L.for_session)+'</div>'+
   '<div class="ga-big '+(BC[L.bias]||'')+'">'+n(L.score,0)+' <span style="font-size:15px">'+esc(L.bias)+' · '+lab(L.strength)+'</span></div>'+
   '<div class="ga-muted">'+n(L.n_items,0)+' market-relevant headline(s), Indian + world feeds, last '+n(d.window_hours||18,0)+' h · '+
   'ATIP keyword analysis'+' · '+esc(L.run_label)+' '+esc(String(L.generated_at||'').slice(0,16).replace('T',' '))+'</div></div>';
  var b=L.brief||{};
  h+='<div class="ga-card"><div class="ga-muted">Brief</div><div>'+esc(b.summary||'—')+'</div>'+
   ((b.bullets||[]).length?'<ul class="ni-ul">'+b.bullets.map(function(x){return '<li>'+esc(x.text)+'</li>'}).join('')+'</ul>':'')+
   ((b.watch||[]).length?'<div class="ga-small ga-muted">Watch: '+b.watch.map(esc).join(' · ')+'</div>':'')+'</div>';
  var ac=A.hit_rate_pct==null?'—':n(A.hit_rate_pct,0)+'%',bl=A.baselines||{};
  h+='<div class="ga-card"><div class="ga-muted">Track record (pre-open call vs Nifty 50 close, '+n(A.days,0)+' days)</div><div class="ga-big">'+ac+'</div>'+
   '<div class="ga-muted">'+n(A.sessions,0)+' session(s)'+(Object.keys(bl).length?' · naive baselines: '+Object.keys(bl).map(function(k){return lab(k)+' '+(bl[k]==null?'—':n(bl[k],0)+'%')}).join(' · '):' · scored after each session closes')+'</div>'+
   '<div class="ga-small">'+esc(A.note||'')+'</div></div></div>';
  var S=L.sectors||[];
  if(S.length){h+='<h3 class="ga-h">Sectors</h3><div class="ga-scroll"><table class="ga-t"><thead><tr><th>Sector</th><th class="r">Score</th><th>Bias</th><th class="r">Headlines</th></tr></thead><tbody>'+
   S.map(function(s){return '<tr><td>'+esc(s.name)+'</td><td class="r ga-num">'+n(s.score,0)+'</td><td class="'+(BC[s.bias]||'')+'">'+esc(s.bias)+' <span class="ga-muted">'+lab(s.strength)+'</span></td><td class="r ga-num">'+n(s.headlines,0)+'</td></tr>'}).join('')+'</tbody></table></div>';}
  var D=L.drivers||[];
  h+='<h3 class="ga-h">What is driving it</h3>'+(D.length?'<div class="ga-scroll"><table class="ga-t"><thead><tr><th>Nifty</th><th>Size</th><th>Channel</th><th>Headline</th><th>Why</th><th>Sectors</th></tr></thead><tbody>'+
   D.map(function(x){return '<tr><td>'+(DIR[x.direction]||'—')+'</td><td>'+lab(x.magnitude)+'</td><td>'+lab(x.transmission)+'</td><td class="ni-wrap">'+esc(x.headline)+' <span class="ga-muted">'+esc(x.source||'')+'</span></td>'+
    '<td class="ni-wrap ga-muted">'+esc(x.rationale)+'</td><td>'+(x.sectors||[]).map(function(s){return esc(s.sector)+' '+(s.direction==='UP'?'▲':'▼')}).join(', ')+'</td></tr>'}).join('')+'</tbody></table></div>':'<div class="ga-muted">No market-moving headline in the window.</div>');
  h+='<p class="ga-muted ga-small">An estimate of direction and size only — no price levels or forecasts. Informational: it feeds no ATIP score and places no orders. Updated '+esc(d.now_ist||'')+' IST.</p>';
  box.innerHTML=h;}
 window.atipLoadNewsImpact=async function(id){var box=document.getElementById(id||'niBox');if(!box)return;
  try{var r=await fetch('/api/news-impact',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(box,await r.json());}
  catch(e){box.innerHTML='<div class="ga-muted">News impact unavailable ('+esc(e.message)+')</div>';}};
})();
</script>"""


def panel_html(box_id: str = "niBox", title: bool = True) -> str:
    """A container + its loader; pages that already carry NI_CSS / NI_JS pass them once themselves."""
    head = ('<div class="st">📰&nbsp; News impact on the Indian market <span style="font-size:11px;color:#64748b;'
            'font-weight:400">— Indian + world news, estimated direction and size · informational, no orders</span>'
            '</div>') if title else ""
    return f'{head}<div id="{box_id}" class="ga-muted">Loading…</div>'


def payload(conn) -> dict:
    from data import news_impact
    out = news_impact.snapshot(conn)
    out["window_hours"] = news_impact.settings()["window_hours"]
    return out


def register(app, guard, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json.loads(json.dumps(json_safe(fn(conn)), default=str)))
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    @app.get("/api/news-impact")
    async def api_news_impact():
        return await run_in_threadpool(call, payload)

    @app.get("/api/news-impact/accuracy")
    async def api_news_impact_accuracy(days: int = 90):
        from data.news_impact import accuracy
        return await run_in_threadpool(call, lambda c: accuracy(c, max(1, min(int(days), 600))))

    @app.post("/api/news-impact/refresh", dependencies=guard)
    async def api_news_impact_refresh():
        from data.news_impact import run
        r = await run_in_threadpool(run, "manual")
        return JSONResponse(json.loads(json.dumps(r, default=str)))
