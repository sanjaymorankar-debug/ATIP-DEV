"""
Task 2 / Task 3 (2026-10-26): the operating-status banner, /api/status, the manual override and the
"Global & Asia" tab.

    GET  /api/status                    STOPPED / RESUMING / RUNNING, why, next stop / resume, last data update,
                                        today's resume steps, recent transitions (pipeline/operating_schedule.py)
    POST /api/status/override  {action: stop|start|auto, reason}   token-guarded, logged in scheduler_status
    GET  /api/asia/markets              each Asia market's last value, % change, signal, session status, the Asia
                                        Sentiment Score, the Pakistan risk flag, and the global markets row
    GET  /api/asia/sources              source health (primary / fallback per index) + the holiday self-check
    POST /api/asia/refresh              fetch now (token-guarded; refused while ATIP is stopped)
    GET  /api/premarket-report          the latest pre-market report (pipeline/premarket_report.py)
    GET  /global-asia                   the Global & Asia page (desktop and phone)

status_banner_html() is the banner every page carries (/, /command, /m, /global-asia): it polls /api/status
every 60 s and says "ATIP stopped — resumes 06:00 IST" with the last-updated time while STOPPED. The Asia
signals are informational only -- nothing on this page or behind these routes places an order.
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
import math

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

GLOBAL_SERIES = (("sp500", "S&P 500"), ("dow", "Dow Jones"), ("nasdaq", "Nasdaq"), ("ftse100", "FTSE 100"),
                 ("dax", "DAX"), ("nikkei", "Nikkei (Yahoo)"), ("hangseng", "Hang Seng"), ("crude_brent", "Brent crude"),
                 ("crude_wti", "WTI crude"), ("gold", "Gold"), ("silver", "Silver"), ("usd_inr", "USD/INR"),
                 ("usd_index", "US Dollar Index"), ("us_10y", "US 10Y yield"))


def _finite(obj):
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def global_markets(conn) -> dict:
    try:
        r = conn.execute("SELECT * FROM global_markets ORDER BY date DESC, id DESC LIMIT 1").fetchone()
    except Exception:
        r = None
    r = dict(r) if r else {}
    rows = [{"key": k, "name": n, "last": r.get(k), "chg_pct": r.get(f"{k}_chg")} for k, n in GLOBAL_SERIES
            if r.get(k) is not None]
    return {"date": str(r.get("date") or "")[:10] or None, "run": r.get("time"), "score": r.get("global_score"),
            "sentiment": r.get("global_sentiment"), "rows": rows}


def asia_payload(conn) -> dict:
    from data import asia_markets
    out = asia_markets.snapshot(conn)
    out["global"] = global_markets(conn)
    return out


# ── the banner ─────────────────────────────────────────────────────────────
BANNER = r"""<div id="atipOpBanner" role="status" aria-live="polite" style="display:none;padding:7px 18px;font:600 12.5px/1.4 system-ui,-apple-system,'Segoe UI',sans-serif"></div>
<script>
(function(){
 var el=document.getElementById('atipOpBanner');
 var C={STOPPED:['#7c2d12','#ffedd5','⏸'],RESUMING:['#1e3a8a','#dbeafe','↻'],RUNNING:['#064e3b','#d1fae5','●']};
 function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
 function show(d){if(!d||!d.status)return;var c=C[d.status]||C.RUNNING;el.style.background=c[0];el.style.color=c[1];
  var t=c[2]+' '+esc(d.banner),lu=' · last updated '+esc(d.last_updated||'—');
  if(d.status==='STOPPED'){t+=' · read-only, no orders can be placed or triggered'+lu;}
  else if(d.status==='RESUMING'){var s=d.resume_steps||[];t+=' · step '+s.length+'/6'+(s.length?' ('+esc(s[s.length-1].name)+' '+esc(s[s.length-1].status)+')':'')+lu;}
  else{t+=' · stops '+esc(String(d.next_stop||'').slice(11))+' IST'+(d.live_feeds?'':' · no live feeds today (weekend/holiday)')+lu;}
  if(d.override){t+=' · manual '+esc(d.override.mode);}
  el.innerHTML=t;el.style.display='block';el.setAttribute('data-status',d.status);document.documentElement.setAttribute('data-atip-status',d.status);}
 async function poll(){try{var r=await fetch('/api/status',{cache:'no-store'});if(r.ok)show(await r.json());}catch(e){}}
 show(__INITIAL__);poll();setInterval(poll,60000);
})();
</script>"""


def status_banner_html() -> str:
    try:
        from pipeline.operating_schedule import status
        initial = status()
    except Exception:
        initial = None
    return BANNER.replace("__INITIAL__", json.dumps(initial).replace("</", "<\\/"))


# ── the Global & Asia renderer (shared by the / tab and the /global-asia page) ──
ASIA_JS = r"""<script>
(function(){
 var esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})};
 var n=function(v,d){return v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d==null?2:d})};
 var chg=function(v){if(v==null)return '<span class="ga-muted">—</span>';var up=v>=0;return '<span class="'+(up?'ga-up':'ga-dn')+'">'+(up?'▲ +':'▼ ')+n(v)+'%</span>'};
 var SIG={BULLISH:'ga-up',BEARISH:'ga-dn',NEUTRAL:'ga-muted'};
 var SES={OPEN:'Open',BREAK:'Lunch break',PRE_OPEN:'Pre-open',CLOSED:'Closed',HOLIDAY:'Holiday',WEEKEND:'Weekend'};
 function render(box,d){
  var s=d.sentiment||{},pk=s.pk_risk||{};
  var h='<div class="ga-cards"><div class="ga-card"><div class="ga-muted">Asia Sentiment Score</div><div class="ga-big">'+n(s.score,0)+'</div><div class="'+(SIG[s.signal]||'')+'">'+esc(s.signal||'no run yet')+'</div>'+
   '<div class="ga-muted">confidence '+n(s.confidence,0)+' · coverage '+(s.coverage==null?'—':Math.round(s.coverage*100)+'%')+' · '+esc(s.run_label||'')+' '+esc(String(s.computed_at||'').slice(0,16).replace('T',' '))+'</div></div>'+
   '<div class="ga-card"><div class="ga-muted">Pakistan geopolitical / risk-event flag (news)</div><div class="ga-big '+(s.pk_risk_level==='HIGH'?'ga-dn':s.pk_risk_level==='ELEVATED'?'ga-warn':'')+'">'+esc(s.pk_risk_level||'—')+'</div>'+
   '<div class="ga-muted">'+n(pk.count,0)+' risk headline(s) in '+n(pk.lookback_hours,0)+' h'+(pk.rule_classified?' · '+pk.rule_classified+' by keyword rules':'')+'</div>'+
   ((pk.headlines||[]).slice(0,3).map(function(x){return '<div class="ga-small">• '+esc(x.headline)+'</div>'}).join(''))+'</div></div>';
  h+='<div class="ga-scroll"><table class="ga-t"><thead><tr><th>Market</th><th>Index</th><th class="r">Last</th><th class="r">Change</th><th>Signal</th><th>Session (IST)</th><th>Data</th></tr></thead><tbody>'+
   (d.markets||[]).map(function(m){return '<tr><td>'+esc(m.market)+'</td><td><b>'+esc(m.name)+'</b></td><td class="r ga-num">'+n(m.last)+'</td><td class="r ga-num">'+chg(m.chg_pct)+'</td>'+
    '<td><span class="'+(SIG[m.signal]||'ga-muted')+'">'+esc(m.signal||'—')+'</span>'+(m.confidence!=null?' <span class="ga-muted">'+n(m.confidence,0)+'%</span>':'')+'</td>'+
    '<td><span class="ga-pill ga-'+esc(String(m.session_status||'').toLowerCase())+'">'+esc(SES[m.session_status]||m.session_status||'—')+'</span> <span class="ga-muted">'+esc((m.session_ist||[]).map(function(p){return p.join('–')}).join(', '))+'</span></td>'+
    '<td class="ga-muted">'+esc(m.date||'no data')+(m.fallback?' · <span class="ga-warn">fallback '+esc(m.source)+'</span>':'')+'</td></tr>'}).join('')+'</tbody></table></div>';
  var g=d.global||{};
  h+='<h3 class="ga-h">Global markets <span class="ga-muted">'+esc(g.date||'')+' '+esc(g.run||'')+' · global score '+n(g.score,0)+' '+esc(g.sentiment||'')+'</span></h3>'+
   '<div class="ga-scroll"><table class="ga-t"><thead><tr><th>Series</th><th class="r">Last</th><th class="r">Change</th></tr></thead><tbody>'+
   (g.rows||[]).map(function(r){return '<tr><td>'+esc(r.name)+'</td><td class="r ga-num">'+n(r.last)+'</td><td class="r ga-num">'+chg(r.chg_pct)+'</td></tr>'}).join('')+
   '</tbody></table></div><p class="ga-muted ga-small">Signals for these markets are informational only — ATIP places no orders on them. Updated '+esc(d.now_ist||'')+' IST.</p>';
  box.innerHTML=h;}
 window.atipLoadAsia=async function(id){var box=document.getElementById(id||'gaBox');if(!box)return;
  try{var r=await fetch('/api/asia/markets',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(box,await r.json());}
  catch(e){box.innerHTML='<div class="ga-muted">Global &amp; Asia data unavailable ('+esc(e.message)+')</div>';}};
})();
</script>"""

ASIA_CSS = """<style>
.ga-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-bottom:12px}
.ga-card{background:var(--ga-panel,#1e293b);border:1px solid var(--ga-line,#334155);border-radius:10px;padding:12px 14px}
.ga-big{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}.ga-small{font-size:11.5px;margin-top:3px}
.ga-muted{color:var(--ga-muted,#94a3b8)}.ga-up{color:var(--ga-up,#6ee7b7);font-weight:600}.ga-dn{color:var(--ga-dn,#fca5a5);font-weight:600}
.ga-warn{color:var(--ga-warn,#fde68a)}.ga-num{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.ga-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}.ga-t{width:100%;border-collapse:collapse;font-size:12.5px}
.ga-t th,.ga-t td{padding:7px 8px;border-bottom:1px solid var(--ga-line,#334155);text-align:left;white-space:nowrap}.ga-t th{color:var(--ga-muted,#94a3b8);font-weight:600}
.ga-t .r{text-align:right}.ga-h{font-size:14px;margin:16px 0 6px}
.ga-pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid var(--ga-line,#334155)}
.ga-open{background:#064e3b;color:#d1fae5;border-color:#065f46}.ga-break,.ga-pre_open{background:#1e3a8a;color:#dbeafe;border-color:#1e40af}
.ga-holiday,.ga-weekend{background:#3f3f46;color:#e4e4e7}
</style>"""


def asia_tab_html() -> str:
    """The container + loader for the main dashboard's "Global & Asia" tab."""
    return (ASIA_CSS + '<div id="asia" class="tc section"><div class="st">🌏 Global &amp; Asia '
            '<span style="font-size:11px;color:#64748b;font-weight:400">— Japan, South Korea, Taiwan, Pakistan · '
            'informational signals, no orders · <a href="/global-asia" style="color:#38bdf8">full page</a></span></div>'
            '<div id="gaBox" class="ga-muted">Loading…</div></div>' + ASIA_JS +
            "<script>setInterval(function(){var t=document.getElementById('asia');"
            "if(t&&t.classList.contains('active'))atipLoadAsia('gaBox')},300000);</script>")


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>ATIP Global &amp; Asia</title>
<style>
:root{--bg:#0B1220;--panel:#111A2C;--line:#22304A;--text:#E6EBF5;--muted:#9AA8C1;--accent:#38BDF8;
 --ga-panel:#111A2C;--ga-line:#22304A;--ga-muted:#9AA8C1;--ga-up:#6EE7B7;--ga-dn:#FCA5A5;--ga-warn:#FDE68A}
@media (prefers-color-scheme: light){:root{--bg:#F5F7FB;--panel:#FFFFFF;--line:#D5DDEA;--text:#0F172A;--muted:#4B5873;--accent:#0369A1;
 --ga-panel:#FFFFFF;--ga-line:#D5DDEA;--ga-muted:#4B5873;--ga-up:#047857;--ga-dn:#B91C1C;--ga-warn:#854D0E}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header b{font-size:15px}nav{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}.wrap{max-width:1200px;margin:0 auto;padding:14px 16px 40px}
</style>__CSS__</head><body>
<header><b>ATIP · Global &amp; Asia</b><nav aria-label="Main"><a href="/command">Command Center</a><a href="/">Signals &amp; scores</a><a href="/m">Mobile</a><a href="/api/asia/sources">Data sources</a></nav></header>
__BANNER__
<main class="wrap"><div id="gaBox" class="ga-muted">Loading…</div></main>
__JS__
<script>atipLoadAsia('gaBox');setInterval(function(){atipLoadAsia('gaBox')},300000);</script>
</body></html>"""


def render_page() -> str:
    return PAGE.replace("__CSS__", ASIA_CSS).replace("__BANNER__", status_banner_html()).replace("__JS__", ASIA_JS)


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(_finite(json_safe(fn(conn))))
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    @app.get("/api/status")
    async def api_status():
        from pipeline.operating_schedule import status
        return JSONResponse(await run_in_threadpool(status))

    @app.post("/api/status/override", dependencies=guard)
    async def api_status_override(request: Req):
        try:
            b = await request.json()
        except Exception:
            b = {}
        b = b if isinstance(b, dict) else {}
        from pipeline.operating_schedule import set_override

        def go():
            return set_override(b.get("action"), str(b.get("reason") or ""), by="dashboard")
        try:
            return JSONResponse(json.loads(json.dumps(await run_in_threadpool(go), default=str)))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/asia/markets")
    async def api_asia_markets():
        return await run_in_threadpool(call, asia_payload)

    @app.get("/api/asia/sources")
    async def api_asia_sources():
        from data import asia_markets

        def go(conn):
            return {"sources": asia_markets.source_health(conn), "calendar_check": asia_markets.calendar_check(conn)}
        return await run_in_threadpool(call, go)

    @app.post("/api/asia/refresh", dependencies=guard)
    async def api_asia_refresh():
        from data import asia_markets
        r = await run_in_threadpool(asia_markets.run, "manual")
        return JSONResponse(_finite(json.loads(json.dumps(r, default=str))))

    @app.get("/api/premarket-report")
    async def api_premarket_report():
        from pipeline.premarket_report import latest
        return await run_in_threadpool(call, lambda c: latest(c) or {"error": "no pre-market report yet"})

    @app.get("/global-asia", response_class=HTMLResponse)
    async def global_asia_page():
        return HTMLResponse(await run_in_threadpool(render_page))
