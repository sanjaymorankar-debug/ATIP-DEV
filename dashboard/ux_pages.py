"""
The redesign's new pages (UXD / SES / ACC / RPT, 2 Nov 2026), rendered in the shell (dashboard/shell.py page()).

    /home              Is ATIP safe, running and making money today? -- three answers, then a customisable grid
    /sessions          the ten sessions as mini-funds: gross vs actual P&L, cycles, floors; the setup drawer
    /sessions/{sid}    one session: Manage (tiles, charts, every cycle, activity) / Accounting / Reports
    /accounting        ledger, session ledger, charges, tax, funds, reconciliation, transfers, trial balance
    /reports           generate any report, schedules (IST), archive, custom
    /settings          credentials, risk limits, tax rates, notifications, display, admin
    /more /markets /trade /lab    the section hubs (each page's job in one line); Strategy Lab's run -> compare -> promote
    /ops               health checks and the latest job runs (the page /compliance has linked to)

Every write goes through /api/v2/... (dashboard/ux_routes.py) with the page's token; the server refuses it while
ATIP is stopped (423) and on the view-only server (403), and the page says so in plain words.
"""

from __future__ import annotations

import json
import re
from datetime import date

from dashboard import ux_kit as K
from dashboard.shell import NAV, page

e = K.e
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _rows(conn, sql, a=()):
    try:
        return [dict(r) if hasattr(r, "keys") else r for r in conn.execute(sql, a).fetchall()]
    except Exception:
        return []


def _market_open() -> bool:
    try:
        from pipeline.scheduler import is_market_hours
        return bool(is_market_hours())
    except Exception:
        return False


def _json(obj) -> str:
    return json.dumps(obj, default=str).replace("</", "<\\/")


def _closed_banner() -> str:
    if _market_open():
        return ""
    return ('<div class="ax-banner info" role="status">Market closed: prices are the last close or last quote; nothing '
            'trades until 09:15 IST on the next trading day.</div>')


def _session_ids(conn) -> list:
    from orders import sessions as S
    return [(s["id"], s["name"], bool(s.get("start_capital"))) for s in S.list_sessions(conn)["sessions"]]


# ── Home ─────────────────────────────────────────────────────────────────────────────────────

def home_data(conn) -> dict:
    from accounting import cycles as C
    from dashboard.command_routes import _feed
    from dashboard.shell import state
    st = state(conn)
    mo = _market_open()
    ov = C.overview(conn)
    today = date.today().isoformat()
    t = C.totals([c for c in C.stored(conn) if str(c.get("closed_at") or "")[:10] == today])
    out = {"state": st, "market_open": mo, "feed": _feed(conn, mo), "sessions": ov, "today": t,
           "market": (_rows(conn, "SELECT date, mh_score, regime, nifty_close, vix_level FROM market_health "
                                  "ORDER BY date DESC LIMIT 1") or [{}])[0],
           "index": (_rows(conn, "SELECT date, time, nifty50, nifty50_chg, banknifty, banknifty_chg, india_vix, "
                                 "india_vix_chg FROM index_levels ORDER BY date DESC, time DESC LIMIT 1") or [{}])[0],
           "tod": (_rows(conn, "SELECT pred_date, symbol, `signal`, atip_score, entry_price, stop_loss, target_1, reasoning "
                               "FROM predictions WHERE is_tod = 1 ORDER BY pred_date DESC LIMIT 1") or [None])[0],
           "alerts": _rows(conn, "SELECT created_at, category, severity, title, message FROM alert_log "
                                 "ORDER BY id DESC LIMIT 6"),
           "pipeline": _rows(conn, "SELECT job_name, start_time, status, duration_s, error_msg FROM pipeline_log "
                                   "WHERE kind = 'run' OR kind IS NULL ORDER BY id DESC LIMIT 6"),
           "pending": (_rows(conn, "SELECT COUNT(*) AS n FROM order_rules WHERE status = 'PENDING_CONFIRMATION'")
                       or [{"n": 0}])[0]["n"],
           "scores_date": (_rows(conn, "SELECT MAX(date) AS d FROM ai_scores") or [{"d": None}])[0]["d"]}
    try:
        from ops.dhan_token import status as token_status
        out["dhan_token"] = token_status()
    except Exception:
        out["dhan_token"] = None
    return out


def _answer(q: str, a: str, sub: str, tone: str, sym: str) -> str:
    return (f'<section class="ax-answer" data-tone="{tone}" aria-label="{e(q)}"><div class="ax-q">{e(q)}</div>'
            f'<div class="ax-a"><span aria-hidden="true">{sym}</span>{a}</div><div class="ax-small ax-muted">{sub}</div></section>')


def home(conn, root="", app=False) -> str:
    try:
        d = home_data(conn)
    except Exception as ex:
        return page("Today", f'<div class="ax-banner bad" role="alert">The home page could not read ATIP\'s data: '
                             f'{e(ex)}. The other pages may still work.</div>', "/home", root=root, app=app)
    st, ov, t = d["state"], d["sessions"], d["today"]
    # 1. safe?
    problems = []
    if st.get("halted"):
        problems.append(f"kill switch ON ({e(st.get('halt_reason') or 'no reason given')})")
    if d["feed"].get("stale"):
        problems.append("live prices are stale")
    tok = d.get("dhan_token") or {}
    if tok.get("message") and tok.get("severity") in ("error", "bad", "critical", "warn"):
        problems.append("Dhan token needs attention")
    env = st.get("env", "PAPER")
    if env == "LIVE":
        problems.append("LIVE: real money")
    safe = _answer("Is ATIP safe?", "Yes" if not problems else ("Halted" if st.get("halted") else "Check"),
                   (f"{env} · feed {'live' if not d['feed'].get('stale') else 'stale'} · kill switch off"
                    if not problems else "; ".join(problems)),
                   "good" if not problems else "bad" if st.get("halted") or env == "LIVE" else "warn",
                   "✓" if not problems else "!")
    # 2. running?
    run_n = st.get("sessions_running", 0)
    if st.get("status") == "STOPPED":
        running = _answer("Is it running?", "Stopped", f"{e(st.get('banner'))} · read-only, no orders", "warn", "⏸")
    else:
        running = _answer("Is it running?", "Yes" if run_n else "Idle",
                          f"{run_n} of 10 sessions running · auto-send {'on' if st.get('auto_send') else 'off'} · "
                          f"data updated {e(str(st.get('last_updated') or '—')[:16])}", "good" if run_n else "warn",
                          "●" if run_n else "○")
    # 3. making money?
    tot = ov["total"]
    money = _answer("Making money today?", K.money(t["actual_pnl"], True) + ' <span class="ax-small ax-muted">actual</span>',
                    f"today: gross {K.money(t['gross_pnl'], True)} − charges {K.money(t['charges'])} − tax est. "
                    f"{K.money(t['tax_est'])} · {t['cycles']} cycle(s) closed · open {K.money(tot['unrealised'], True)}",
                    "good" if t["actual_pnl"] > 0 else "bad" if t["actual_pnl"] < 0 else "", "₹")
    banners = _closed_banner()
    if d["scores_date"]:
        try:
            age = (date.today() - date.fromisoformat(str(d["scores_date"])[:10])).days
            if age > 3:
                banners += (f'<div class="ax-banner warn" role="status">Stale data: the latest ATIP scores are from '
                            f'{e(d["scores_date"])} ({age} days ago).</div>')
        except ValueError:
            pass
    idx, mk = d["index"], d["market"]
    w_market = (f'<div class="ax-card" data-widget="market"><h2>Market</h2>' + K.kv([
        ("Nifty 50", (K.money(idx.get("nifty50")).replace("₹", "") if idx.get("nifty50") else "—") + " " + K.pct(idx.get("nifty50_chg"))),
        ("India VIX", (f'{idx["india_vix"]:.2f}' if idx.get("india_vix") else "—") + " " + K.pct(idx.get("india_vix_chg"))),
        ("Market health", f'{mk.get("mh_score") or "—"} {e(mk.get("regime") or "")}'),
        ("As of", e(f'{idx.get("date") or mk.get("date") or "—"} {idx.get("time") or ""} IST'))]) +
        '<a href="/market">Markets →</a></div>')
    tod = d["tod"]
    w_tod = ('<div class="ax-card" data-widget="tod"><h2>Trade of the Day</h2>' + (K.kv([
        ("Stock", f'<a href="/?stock={e(tod["symbol"])}">{e(tod["symbol"])}</a> {K.status(tod.get("signal") or "", "good" if tod.get("signal") == "BUY" else "")}'),
        ("ATIP score", e(f'{float(tod["atip_score"]):.0f}' if tod.get("atip_score") is not None else "—")),
        ("Entry · stop · target", f'{K.money(tod.get("entry_price"))} · {K.money(tod.get("stop_loss"))} · {K.money(tod.get("target_1"))}'),
        ("Date", e(tod["pred_date"]))]) if tod else '<div class="ax-empty">No Trade of the Day yet.</div>') + "</div>")
    srows = []
    for s in ov["sessions"]:
        ti = s["tiles"]
        if not ti:
            continue
        srows.append(f'<tr><td><a href="/sessions/{e(s["id"])}">{e(s["id"])}</a> {e(s["name"])}</td>'
                     f'<td class="n" data-v="{ti["gross_pnl"]}">{K.money(ti["gross_pnl"], True)}</td>'
                     f'<td class="n" data-v="{ti["actual_pnl"]}">{K.money(ti["actual_pnl"], True)}</td>'
                     f'<td>{K.spark(s["spark"], s["id"] + " actual equity")}</td></tr>')
    w_sessions = ('<div class="ax-card" data-widget="sessions"><h2>Sessions</h2>' + (
        f'<div class="ax-tablewrap" style="max-height:none"><table class="ax-table"><thead><tr><th>Session</th><th class="n">Gross P&amp;L</th>'
        f'<th class="n">Actual P&amp;L</th><th data-nosort>Trend</th></tr></thead><tbody>{"".join(srows)}</tbody></table></div>'
        if srows else '<div class="ax-empty">No session has money yet. <a href="/sessions">Set one up</a>.</div>') +
        f'<p class="ax-small ax-muted">All sessions: gross {K.money(tot["gross_pnl"], True)} · actual {K.money(tot["actual_pnl"], True)}'
        f' · {tot["waiting"]} waiting below their minimum</p></div>')
    sev = {"critical": "bad", "error": "bad", "warning": "warn", "warn": "warn"}
    w_alerts = ('<div class="ax-card" data-widget="alerts"><h2>Alerts</h2>' + ("".join(
        f'<div class="ax-small" style="padding:6px 0;border-bottom:1px solid var(--ax-border-soft)">{K.status(a["severity"], sev.get(a["severity"], "info"))} '
        f'<b>{e(a.get("title") or a["category"])}</b> <span class="ax-muted">{e(str(a["created_at"])[:16])}</span><br>{e(re.sub(r"<[^>]+>", "", str(a["message"]))[:160])}</div>'
        for a in d["alerts"]) or '<div class="ax-empty">No alerts.</div>') + "</div>")
    w_pipe = ('<div class="ax-card" data-widget="pipeline"><h2>Pipeline</h2>' + ("".join(
        f'<div class="ax-small" style="display:flex;gap:8px;padding:5px 0;border-bottom:1px solid var(--ax-border-soft)">'
        f'{K.status(p.get("status") or "?", "good" if str(p.get("status")).upper() in ("SUCCESS", "OK", "DONE") else "bad" if "FAIL" in str(p.get("status")).upper() else "")}'
        f'<span style="flex:1">{e(p["job_name"])}</span><span class="ax-muted">{e(str(p.get("start_time") or "")[5:16])}</span></div>'
        for p in d["pipeline"]) or '<div class="ax-empty">No job has run yet.</div>') + '<a href="/ops">Ops &amp; health →</a></div>')
    w_todo = ('<div class="ax-card" data-widget="todo"><h2>Needs you</h2>' + K.kv([
        ("Orders waiting for your tap", f'<a href="/command">{d["pending"]}</a>'),
        ("Sessions waiting (below minimum)", f'<a href="/sessions">{tot["waiting"]}</a>'),
        ("Reconciliation", '<a href="/accounting?tab=recon">check charges and cash</a>')]) + "</div>")
    widgets = [("market", "Market", w_market), ("tod", "Trade of the Day", w_tod), ("sessions", "Sessions", w_sessions),
               ("todo", "Needs you", w_todo), ("alerts", "Alerts", w_alerts), ("pipeline", "Pipeline", w_pipe)]
    custom = ('<details class="ax-help" id="ax-custom"><summary>Customise this page</summary><p class="ax-small ax-muted">'
              'Choose the widgets you want here. This is remembered in this browser only.</p><div class="ax-choice">' +
              "".join(f'<label><input type="checkbox" data-w="{k}" checked><span>{e(l)}</span></label>' for k, l, _ in widgets)
              + "</div></details>")
    body = (banners + f'<div class="ax-five">{safe}{running}{money}</div>' +
            '<div class="ax-grid-2" id="ax-widgets">' + "".join(w for _, _, w in widgets) + "</div>" + custom +
            """<script>(function(){var k='atip-home-hidden',h=[];try{h=JSON.parse(localStorage.getItem(k)||'[]')}catch(e){}
function apply(){document.querySelectorAll('[data-widget]').forEach(function(w){w.hidden=h.indexOf(w.getAttribute('data-widget'))>=0;});
document.querySelectorAll('#ax-custom [data-w]').forEach(function(c){c.checked=h.indexOf(c.getAttribute('data-w'))<0;});}
document.querySelectorAll('#ax-custom [data-w]').forEach(function(c){c.addEventListener('change',function(){var w=c.getAttribute('data-w');
h=h.filter(function(x){return x!==w});if(!c.checked)h.push(w);try{localStorage.setItem(k,JSON.stringify(h))}catch(e){}apply();});});apply();})();</script>""")
    return page("Today", body, "/home", job="Is ATIP safe, running and making money today? The three answers first; "
                "everything else below.", root=root, app=app,
                actions='<a class="ax-btn" href="/sessions">Sessions</a><a class="ax-btn primary" href="/command">Command Center</a>')


# ── Sessions ─────────────────────────────────────────────────────────────────────────────────

SETUP_DRAWER = """
<aside class="ax-drawer" id="ax-setup" aria-hidden="true" aria-labelledby="ax-setup-h">
<header><h2 id="ax-setup-h">Session setup</h2><button class="ax-iconbtn" type="button" data-ax-close aria-label="Close">✕</button></header>
<form id="ax-setup-form" novalidate class="ax-body">
 <div class="ax-banner warn" id="ax-open-note" hidden></div>
 <div class="ax-banner bad" id="ax-form-err" role="alert" hidden></div>
 <fieldset class="ax-group"><legend>Identity</legend>
  <div class="ax-field"><label for="f-name">Name</label><input class="ax-input" id="f-name" name="name" maxlength="80" required></div>
  <div class="ax-row"><div class="ax-field"><label for="f-strategy">Strategy</label><input class="ax-input" id="f-strategy" name="strategy" maxlength="80"></div>
  <div class="ax-field"><label for="f-universe">Universe</label><input class="ax-input" id="f-universe" name="universe" maxlength="80" placeholder="e.g. Nifty 500"></div></div>
 </fieldset>
 <fieldset class="ax-group"><legend>Mode and status</legend>
  <div class="ax-field"><span class="ax-label" id="l-mode">Mode</span><div class="ax-choice" role="radiogroup" aria-labelledby="l-mode">
   <label><input type="radio" name="mode" value="MANUAL"><span>Manual — you place every order</span></label>
   <label><input type="radio" name="mode" value="CONFIRM"><span>Confirm — ATIP asks first</span></label>
   <label><input type="radio" name="mode" value="AUTO"><span>Auto — sends within limits</span></label></div></div>
  <div class="ax-field" id="f-ack-wrap" hidden><label class="ax-check"><input type="checkbox" name="ack" id="f-ack"><span>I understand AUTO sends this session's entries without asking me (on PAPER, with auto-send on).</span></label><div class="ax-err" data-err="ack"></div></div>
  <div class="ax-field"><span class="ax-label" id="l-status">Status</span><div class="ax-choice" role="radiogroup" aria-labelledby="l-status">
   <label><input type="radio" name="status" value="RUNNING"><span>● Running</span></label>
   <label><input type="radio" name="status" value="PAUSED"><span>⏸ Paused</span></label>
   <label><input type="radio" name="status" value="OFF"><span>○ Off</span></label></div><div class="ax-err" data-err="mode"></div></div>
 </fieldset>
 <fieldset class="ax-group"><legend>Money</legend>
  <div class="ax-field"><label for="f-start">Starting amount (₹)</label><input class="ax-input" id="f-start" name="start_amount" inputmode="decimal" aria-describedby="h-start">
   <div class="ax-help" id="h-start">This session's own money. Changing it restarts the session's books from now.</div><div class="ax-err" data-err="start_amount"></div></div>
  <div class="ax-field"><label for="f-min">Minimum starting amount per cycle (₹)</label><input class="ax-input" id="f-min" name="min_buy_value" inputmode="decimal" aria-describedby="h-min">
   <div class="ax-help" id="h-min">A new cycle does not start with less cash than this (the floor).</div><div class="ax-err" data-err="min_buy_value"></div></div>
  <div class="ax-field"><label for="f-floor">How the minimum grows</label><select class="ax-select" id="f-floor" name="floor_rule">
   <option value="FIXED">Fixed — stays as entered</option><option value="RATCHET_PCT">Ratchet — a % of the highest cycle start so far</option>
   <option value="STEP">Step — rises by ₹ after every N profitable cycles</option></select><div class="ax-help" id="h-floor"></div></div>
  <div class="ax-row" id="f-floor-extra"><div class="ax-field"><label for="f-fval" id="l-fval">Value</label><input class="ax-input" id="f-fval" name="floor_value" inputmode="decimal"><div class="ax-err" data-err="floor_value"></div></div>
   <div class="ax-field" id="f-every-wrap"><label for="f-every">Every N profitable cycles</label><input class="ax-input" id="f-every" name="floor_step_every" inputmode="numeric"><div class="ax-err" data-err="floor_step_every"></div></div></div>
  <div class="ax-field"><span class="ax-label" id="l-basis">What the next cycle starts with</span><div class="ax-choice" role="radiogroup" aria-labelledby="l-basis">
   <label><input type="radio" name="compound_basis" value="CASH_AFTER_CHARGES"><span>Cash after charges (all of it)</span></label>
   <label><input type="radio" name="compound_basis" value="AFTER_TAX_RESERVE"><span>After setting aside the estimated tax</span></label></div></div>
 </fieldset>
 <fieldset class="ax-group"><legend>Exits</legend>
  <div class="ax-row"><div class="ax-field"><label for="f-tgt">Target %</label><input class="ax-input" id="f-tgt" name="target_pct" inputmode="decimal"><div class="ax-err" data-err="target_pct"></div></div>
  <div class="ax-field"><label for="f-stop">Stop %</label><input class="ax-input" id="f-stop" name="stop_pct" inputmode="decimal"><div class="ax-err" data-err="stop_pct"></div></div></div>
  <div class="ax-row"><div class="ax-field"><label for="f-trail">Trailing % (blank = off)</label><input class="ax-input" id="f-trail" name="trail_pct" inputmode="decimal"><div class="ax-err" data-err="trail_pct"></div></div>
  <div class="ax-field"><label for="f-hold">Hold days (blank = off)</label><input class="ax-input" id="f-hold" name="hold_days" inputmode="decimal"><div class="ax-err" data-err="hold_days"></div></div></div>
 </fieldset>
 <fieldset class="ax-group"><legend>Cycles</legend>
  <div class="ax-row"><div class="ax-field"><label for="f-max">Max cycles</label><input class="ax-input" id="f-max" name="max_occurrences" inputmode="numeric"><div class="ax-err" data-err="max_occurrences"></div></div>
  <div class="ax-field"><span class="ax-label">Auto-pick</span><label class="ax-check"><input type="checkbox" name="auto_pick" id="f-pick"><span>Pick each next stock from ATIP's BUY signals</span></label></div></div>
 </fieldset>
 <div class="ax-banner warn" id="ax-restart" hidden><label class="ax-check"><input type="checkbox" name="restart" id="f-restart"><span id="ax-restart-t"></span></label></div>
</form>
<footer><button class="ax-btn" type="button" data-ax-close>Cancel</button><button class="ax-btn primary" type="submit" form="ax-setup-form" id="ax-setup-save">Save session</button></footer>
</aside>"""

SETUP_JS = r"""<script>(function(){
var DATA=JSON.parse(document.getElementById('ax-sdata').getAttribute('data-json')),proposed={},cur=null,f=document.getElementById('ax-setup-form');
function v(n){var el=f.elements[n];if(!el)return null;if(el.length&&el[0]&&el[0].type==='radio'){for(var i=0;i<el.length;i++)if(el[i].checked)return el[i].value;return null;}return el.type==='checkbox'?el.checked:el.value;}
function set(n,x){var el=f.elements[n];if(!el)return;if(el.length&&el[0]&&el[0].type==='radio'){for(var i=0;i<el.length;i++)el[i].checked=el[i].value===x;return;}if(el.type==='checkbox')el.checked=!!x;else el.value=(x===null||x===undefined)?'':x;}
var HELP={FIXED:'The minimum stays as entered.',RATCHET_PCT:'Minimum = the larger of the amount above and this % of the highest amount any cycle has started with. It rises as the session compounds and never falls.',STEP:'The minimum rises by this many rupees after every N profitable cycles.'};
function floorUI(){var r=v('floor_rule');document.getElementById('h-floor').textContent=HELP[r]||'';document.getElementById('f-floor-extra').hidden=r==='FIXED';
 document.getElementById('l-fval').textContent=r==='RATCHET_PCT'?'Ratchet % of the highest cycle start':'Rise by (₹)';document.getElementById('f-every-wrap').hidden=r!=='STEP';}
function modeUI(){document.getElementById('f-ack-wrap').hidden=!(v('mode')==='AUTO'&&cur&&cur.mode!=='AUTO');}
function startUI(){var s=parseFloat(String(v('start_amount')).replace(/,/g,'')),old=cur&&cur.setup.start_capital,box=document.getElementById('ax-restart');
 var changed=!isNaN(s)&&(!old||Math.abs(s-old)>=0.01);box.hidden=!changed||!old;
 if(changed&&old)document.getElementById('ax-restart-t').textContent='Restart '+cur.id+"'s books from now with "+AX.inr(s)+' (it has '+AX.inr(cur.tiles?cur.tiles.cash:old)+' now). Its cycle count, floor history and P&L tiles start again; past cycles stay in the archive and the tax register.';}
function clearErr(){f.querySelectorAll('[data-err]').forEach(function(x){x.textContent='';});f.querySelectorAll('[aria-invalid]').forEach(function(x){x.removeAttribute('aria-invalid');});var b=document.getElementById('ax-form-err');b.hidden=true;}
window.axSetup=function(sid){cur=DATA[sid];if(!cur)return;clearErr();var s=cur.setup;document.getElementById('ax-setup-h').textContent=sid+' setup';
 set('name',cur.name);set('strategy',cur.strategy);set('universe',cur.universe);set('mode',cur.mode);set('status',cur.status);set('ack',false);
 set('start_amount',proposed[sid]!==undefined?proposed[sid]:s.start_capital);set('min_buy_value',s.min_buy_value);set('floor_rule',s.floor_rule||'FIXED');set('floor_value',s.floor_value);set('floor_step_every',s.floor_step_every);
 set('compound_basis',s.compound_basis||'CASH_AFTER_CHARGES');set('target_pct',s.target_pct);set('stop_pct',s.stop_pct);set('trail_pct',s.trail_pct);set('hold_days',s.hold_days);set('max_occurrences',s.max_occurrences);set('auto_pick',cur.auto_pick);set('restart',false);
 var note=document.getElementById('ax-open-note'),open=(cur.open||[]);note.hidden=!open.length;
 document.getElementById('f-start').disabled=!!open.length;
 if(open.length)note.textContent=sid+' holds an open trade ('+open.map(function(o){return o.symbol;}).join(', ')+'). Its starting amount can change only after that trade closes, because the trade is using the cash. Every other setting can change now.';
 floorUI();modeUI();startUI();AX.openDrawer('ax-setup');};
['floor_rule'].forEach(function(n){f.elements[n].addEventListener('change',floorUI);});
Array.prototype.forEach.call(f.elements.mode,function(r){r.addEventListener('change',modeUI);});f.elements.start_amount.addEventListener('input',startUI);
f.addEventListener('submit',function(ev){ev.preventDefault();clearErr();var body={};
 ['name','strategy','universe','mode','status','start_amount','min_buy_value','floor_rule','floor_value','floor_step_every','compound_basis','target_pct','stop_pct','trail_pct','hold_days','max_occurrences'].forEach(function(n){var x=v(n);if(x!==null)body[n]=typeof x==='string'?x.replace(/,/g,'').trim():x;});
 body.auto_pick=v('auto_pick');body.ack=v('ack');body.restart=v('restart');if(document.getElementById('f-start').disabled)delete body.start_amount;
 var btn=document.getElementById('ax-setup-save');btn.disabled=true;
 AX.api('/api/v2/sessions/'+cur.id+'/setup',{body:body}).then(function(r){AX.toast(cur.id+' saved. '+(r.changes&&r.changes.length?r.changes.join('; '):''));setTimeout(function(){location.reload();},700);})
 .catch(function(e){var errs=(e.data&&e.data.errors)||{},first=null;Object.keys(errs).forEach(function(k){var t=f.querySelector('[data-err="'+k+'"]'),el=f.elements[k];if(t)t.textContent=errs[k];if(el&&el.setAttribute)el.setAttribute('aria-invalid','true');if(!first&&el&&el.focus)first=el;});
  var b=document.getElementById('ax-form-err');b.textContent=e.message;b.hidden=false;if(errs.restart)document.getElementById('ax-restart').hidden=false;if(first)first.focus();else b.scrollIntoView();})
 .finally(function(){btn.disabled=false;});});
var sd=document.getElementById('ax-split');if(sd){sd.querySelector('form').addEventListener('submit',function(ev){ev.preventDefault();var ids=[].slice.call(sd.querySelectorAll('input[name=ids]:checked')).map(function(x){return x.value;});
 AX.api('/api/v2/sessions/split',{body:{total:sd.querySelector('input[name=total]').value.replace(/,/g,''),ids:ids}}).then(function(r){proposed=r.amounts;
  document.querySelectorAll('[data-proposed]').forEach(function(el){var a=proposed[el.getAttribute('data-proposed')];el.hidden=a===undefined;if(a!==undefined)el.textContent='Proposed '+AX.inr(a)+' — open Setup to review and save';});
  sd.close();AX.toast('Proposed amounts filled in. Nothing is saved until you save each session.');}).catch(function(e){sd.querySelector('.ax-err').textContent=e.message;});});}
var m=location.hash.match(/^#setup-(S\d+)$/);if(m)setTimeout(function(){window.axSetup(m[1]);},200);
})();</script>"""


def _scard(s: dict) -> str:
    ti = s["tiles"]
    head = (f'<div class="ax-stop"><a class="ax-sname" href="/sessions/{e(s["id"])}">{e(s["id"])} · {e(s["name"])}</a>'
            f'{K.mode_status(s["mode"], s["status"])}</div><div class="ax-small ax-muted">{e(s.get("strategy") or "")}'
            f'{" · auto-pick" if s.get("auto_pick") else ""}</div>')
    if not ti:
        return (f'<div class="ax-card ax-scard">{head}<div class="ax-empty">No starting amount yet.</div>'
                f'<span class="ax-proposed" data-proposed="{e(s["id"])}" hidden></span>'
                f'<button class="ax-btn primary" type="button" onclick="axSetup(\'{e(s["id"])}\')">Set up {e(s["id"])}</button></div>')
    opens = "".join(f'<div class="ax-small"><b>{e(o["symbol"])}</b> ×{o["qty"]:g} · {K.money(o["unrealised"], True)} open'
                    f'{K.progress(o["progress"])}</div>' for o in s["open"] or [])
    waiting = f'<div class="ax-banner warn ax-small" style="margin:0">{e(ti["waiting"])}</div>' if ti.get("waiting") else ""
    return (f'<div class="ax-card ax-scard{" ax-attn" if ti.get("waiting") else ""}">{head}'
            f'<div class="ax-gva"><div><div class="ax-lbl">Gross P&amp;L</div><div class="ax-val">{K.money(ti["gross_pnl"], True)}</div></div>'
            f'<div><div class="ax-lbl">Actual P&amp;L <span title="after charges and estimated tax">ⓘ</span></div>'
            f'<div class="ax-val">{K.money(ti["actual_pnl"], True)}</div></div></div>'
            f'<div class="ax-small ax-muted">Equity {K.money(ti["equity"])} · started {K.money(ti["start_capital"])} · '
            f'cycles {ti["cycles_done"]}/{ti["max_cycles"]} · floor {K.money(ti["floor"])}</div>'
            f'{K.spark(s["spark"], s["id"] + " actual equity")}{opens}{waiting}'
            f'<span class="ax-proposed" data-proposed="{e(s["id"])}" hidden></span>'
            f'<div class="ax-row"><button class="ax-btn sm" type="button" aria-label="Set up {e(s["id"])}" onclick="axSetup(\'{e(s["id"])}\')">Setup</button>'
            f'<a class="ax-btn sm" href="/sessions/{e(s["id"])}" aria-label="{e(s["id"])} cycles and P&amp;L">Cycles &amp; P&amp;L</a></div></div>')


def sessions(conn, root="", app=False) -> str:
    from accounting import cycles as C
    ov = C.overview(conn)
    t = ov["total"]
    tiles = '<div class="ax-tiles">' + "".join([
        K.tile("Starting amounts", K.money(t["start_capital"])), K.tile("Equity now", K.money(t["equity"])),
        K.tile("Gross P&L", K.money(t["gross_pnl"], True), "before any cost", True),
        K.tile("Charges", K.money(t["charges"])), K.tile("Tax (est.)", K.money(t["tax_est"]), "provisional"),
        K.tile("Actual P&L", K.money(t["actual_pnl"], True), "after charges and tax", True),
        K.tile("Cycles closed", f'<span class="ax-num">{t["cycles_done"]}</span>'),
        K.tile("Waiting", f'<span class="ax-num">{t["waiting"]}</span>', "below their minimum")]) + "</div>"
    data = {s["id"]: {k: s[k] for k in ("id", "name", "strategy", "universe", "mode", "status", "auto_pick", "setup",
                                         "open", "tiles")} for s in ov["sessions"]}
    series = [s for s in ov["sessions"] if s["tiles"]]
    mult = ""
    if series:
        figs = []
        for s in series:
            b_pts = [{"label": "", "a": v} for v in s["spark"] or []]
            figs.append(f'<figure><figcaption><span>{e(s["id"])} {e(s["name"])}</span>{K.money(s["tiles"]["actual_pnl"], True)}'
                        f'</figcaption>{K.spark(s["spark"], s["id"] + " actual equity, last 30 cycles") or "<div class=ax-small>one point so far</div>"}</figure>')
        mult = ('<h2 class="ax-h2">All sessions side by side</h2><p class="ax-small ax-muted">Each small chart is that '
                'session\'s actual equity (after charges and estimated tax), on its own scale.</p>'
                f'<div class="ax-mult">{"".join(figs)}</div>')
    split = ('<dialog class="ax-dialog" id="ax-split" aria-labelledby="ax-split-h"><form><div class="ax-dhead" id="ax-split-h">'
             'Split a total across sessions</div><div class="ax-dbody"><p class="ax-small ax-muted">This only fills in a '
             'proposed starting amount on each card. Nothing is saved until you open each session\'s setup and save it.</p>'
             '<div class="ax-field"><label for="sp-total">Total (₹)</label><input class="ax-input" id="sp-total" name="total" '
             'inputmode="decimal" placeholder="250000"></div><fieldset class="ax-group"><legend>Sessions</legend><div class="ax-choice">' +
             "".join(f'<label><input type="checkbox" name="ids" value="{e(s["id"])}" checked><span>{e(s["id"])}</span></label>'
                     for s in ov["sessions"]) +
             '</div></fieldset><div class="ax-err"></div></div><div class="ax-dfoot"><button class="ax-btn" type="button" '
             'data-ax-dclose>Cancel</button><button class="ax-btn primary" type="submit">Fill in amounts</button></div></form></dialog>')
    help_ = _worked_example()
    body = (_closed_banner() + tiles + '<h2 class="ax-h2">Sessions</h2><div class="ax-sessions">' +
            "".join(_scard(s) for s in ov["sessions"]) + "</div>" + mult + help_ + SETUP_DRAWER + split +
            f'<div id="ax-sdata" hidden data-json="{e(json.dumps(data, default=str))}"></div>' + SETUP_JS)
    return page("Sessions", body, "/sessions", root=root, app=app,
                job="Ten independent mini-funds. Set each one's money and rules, and see what it really earned: gross "
                    "P&L before any cost, actual P&L after charges and estimated tax.",
                actions='<button class="ax-btn" type="button" onclick="document.getElementById(\'ax-split\').showModal()">'
                        'Split a total…</button><a class="ax-btn" href="/reports">Session reports</a>',
                mar=[("Manage", "/sessions", True), ("Accounting", "/accounting?tab=session", False),
                     ("Reports", "/reports", False)])


def _worked_example() -> str:
    return ("""<details class="ax-help"><summary>How the numbers work (worked example)</summary>
<p>S1 starts with ₹25,000. Cycle 1 buys ₹25,000 of stock and sells it at +3 % for ₹25,750.</p>
<dl class="ax-kv"><dt>Gross P&amp;L (Total P&amp;L)</dt><dd>+₹750.00</dd>
<dt>Charges (delivery)</dt><dd>₹72.27 = STT ₹50.75 + exchange ₹1.51 + SEBI ₹0.05 + stamp ₹3.75 + GST ₹0.28 + DP ₹15.93</dd>
<dt>Net P&amp;L before tax</dt><dd>+₹677.73</dd><dt>Tax (est.) — STCG 20 %</dt><dd>₹135.55</dd>
<dt>Actual P&amp;L (net after charges &amp; tax)</dt><dd>+₹542.18</dd>
<dt>Cycle 2 starts with</dt><dd>₹25,677.73 (cash after charges) or ₹25,542.18 (after setting aside the tax)</dd></dl>
<p class="ax-small ax-muted">Tax is an estimate, not tax advice. Per cycle it is provisional; the FY view on Accounting › Tax
sets losses off against gains and applies the LTCG exemption once a year. Rates are on Settings › Tax.</p></details>""")


EVENT_LABEL = {"LINK": "Order linked", "EDIT": "Settings changed", "MODE": "Mode", "STATUS": "Status", "POOL": "Starting amount",
               "POOL_END": "Pool ended", "TRANSFER": "Transfer", "ADJUSTMENT": "Adjustment", "CREATED": "Created",
               "AUTO_SEND": "Auto-send"}


def event_text(x: dict) -> str:
    """A session_events row in words: its JSON detail as "key value" pairs, nested parts left out."""
    raw = x.get("detail") or ""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)[:220]
    if not isinstance(d, dict):
        return str(d)[:220]
    if x.get("event") == "LINK":
        p = d.get("pool_rules") or {}
        return f'rule {str(d.get("rule_id") or "")[:8]}' + (f' · {p.get("exit")}' if p.get("exit") else "")
    if x.get("event") in ("MODE", "STATUS") and "from" in d:
        return f'{d["from"]} → {d["to"]}' + (f' · {d["reason"]}' if d.get("reason") else "")
    parts = []
    for k, v in d.items():
        if v is None or isinstance(v, (dict, list)) or k in ("ack",):
            continue
        if isinstance(v, float) and k in ("start_capital", "min_buy_value", "amount", "floor_value"):
            v = K.inr(v)
        parts.append(f'{k.replace("_", " ")} {v}')
    return " · ".join(parts)[:220]


# ── Session detail ───────────────────────────────────────────────────────────────────────────

CYCLE_COLS = [("n", "#", "int"), ("symbol", "Symbol", "text"), ("opened", "Opened", "text"), ("closed", "Closed", "text"),
              ("holding_days", "Days", "int"), ("start_amount", "Start amount", "money"), ("buy_value", "Buy value", "money"),
              ("sell_value", "Sell value", "money"), ("gross_pnl", "Gross P&L", "pnl"), ("ch_brokerage", "Brokerage", "money"),
              ("ch_stt", "STT", "money"), ("ch_exchange", "Exchange", "money"), ("ch_sebi", "SEBI", "money"),
              ("ch_stamp", "Stamp", "money"), ("ch_gst", "GST", "money"), ("ch_dp", "DP", "money"), ("ch_other", "Other", "money"),
              ("charges", "Charges", "money"), ("net_pnl", "Net before tax", "pnl"), ("tax_est", "Tax (est.)", "money"),
              ("actual_pnl", "Actual P&L", "pnl"), ("end_amount", "End amount", "money"), ("gross_pct", "Gross %", "pct"),
              ("actual_pct", "Actual %", "pct"), ("exit_reason", "Exit", "text"), ("floor_at_start", "Floor", "money"),
              ("env", "Env", "text")]


def _cycle_drawer(r: dict) -> dict:
    return {"title": f"Cycle {r['n']} · {r['symbol']}", "rows": [
        ["Opened → closed", f"{str(r['opened_at'])[:16].replace('T', ' ')} → {str(r['closed_at'])[:16].replace('T', ' ')} IST"],
        ["Quantity", f"{r['qty']:g} shares at {K.inr(r['buy_price'])} → {K.inr(r['sell_price'])}"],
        ["Start amount", K.inr(r["start_amount"])], ["Buy value / sell value", f"{K.inr(r['buy_value'])} / {K.inr(r['sell_value'])}"],
        ["Gross P&L", K.inr(r["gross_pnl"], True)],
        *[[f"− {lab}", K.inr(r[f'ch_{k}'])] for k, lab in (("brokerage", "Brokerage"), ("stt", "STT"), ("exchange", "Exchange txn"),
                                                           ("sebi", "SEBI fee"), ("stamp", "Stamp duty"), ("gst", "GST"),
                                                           ("dp", "DP charge"), ("other", "Other (fill vs model)")) if r.get(f"ch_{k}")],
        ["Net P&L before tax", K.inr(r["net_pnl"], True)], [f"− Tax (est., {r['tax_head']})", K.inr(r["tax_est"])],
        ["Actual P&L", K.inr(r["actual_pnl"], True)], ["End amount", f"{K.inr(r['end_amount'])} ({r['compound_basis']})"],
        ["Cash left idle this cycle", K.inr(r["idle_cash"])], ["Floor at the start", K.inr(r["floor_at_start"])],
        ["Exit", r.get("exit_reason") or "—"], ["Env · FY", f"{r['env']} · {r['fy']}"]]}


def session_detail(conn, sid: str, tab: str = "manage", root="", app=False) -> str:
    from accounting import cycles as C
    from accounting import ledger as LG
    from enterprise import session_reports as R
    b = C.book(conn, sid)
    s = b["session"]
    path = f"/sessions/{sid}"
    mar = [("Manage", path, tab == "manage"), ("Accounting", path + "?tab=accounting", tab == "accounting"),
           ("Reports", path + "?tab=reports", tab == "reports")]
    title = f"{sid} · {s['name']}"
    actions = (f'<a class="ax-btn" href="/sessions#setup-{e(sid)}">Setup</a>'
               f'<button class="ax-btn primary" type="button" data-gen-book="{e(sid)}">Download cycle book (PDF)</button>')
    gen_js = """<script>document.querySelectorAll('[data-gen-book]').forEach(function(b){b.addEventListener('click',function(){b.disabled=true;
AX.api('/api/v2/reports/generate',{body:{kind:'session_cycle_book',params:{sessions:[b.getAttribute('data-gen-book')]},formats:['pdf']}})
.then(function(r){location.href=r.files[0].url;}).catch(function(e){AX.toast(e.message,true);}).finally(function(){b.disabled=false;});});});</script>"""
    if not b["pool"]:
        body = (f'<div class="ax-empty">{e(sid)} has no starting amount yet, so it has no cycles. '
                f'<a href="/sessions#setup-{e(sid)}">Give it one</a>.</div>')
        return page(title, body, path, root=root, app=app, mar=mar)
    ti = b["tiles"]
    rows = R._cycle_rows(b["cycles"])
    if tab == "accounting":
        led = LG.session_ledger(conn, sid)
        lrows = [{**r, "at": str(r["at"]).replace("T", " ")[:16]} for r in led["rows"]]
        reg = [{"type": C.CHARGE_LABEL[k], "amount": v} for k, v in b["totals"]["charges_by_type"].items() if v]
        body = ('<h2 class="ax-h2">Cash account</h2><p class="ax-small ax-muted">Every line that moved this session\'s '
                f'cash, from the double-entry ledger. Balance now {K.money(led["balance"])}; the engine\'s free cash is '
                f'{K.money(ti["cash"])} (an open buy is posted when its cycle closes).</p>' +
                K.table("t-sled", [("at", "When (IST)", "text"), ("kind", "Kind", "text"), ("memo", "What", "text"),
                                   ("debit", "In", "money"), ("credit", "Out", "money"), ("balance", "Balance", "money"),
                                   ("env", "Env", "text")], lrows, name=f"{sid}_cash", empty="No ledger line yet.") +
                '<h2 class="ax-h2">Charges by type</h2>' +
                K.table("t-sch", [("type", "Charge", "text"), ("amount", "Amount", "money")], reg, tools=False,
                        totals=[{"type": "Total", "amount": b["totals"]["charges"]}], empty="No charges yet.") +
                f'<p><a href="/accounting?tab=ledger&session={e(sid)}">Open in the general ledger →</a></p>')
        return page(title, body, path, "tab=accounting", root=root, app=app, mar=mar, actions=actions) + ""
    if tab == "reports":
        arch = [a for a in R.archive(conn, "session_cycle_book")][:30]
        body = ('<div class="ax-card"><h2>This session\'s cycle book</h2><p class="ax-small ax-muted">Every cycle with '
                'gross P&amp;L, each charge, estimated tax and actual P&amp;L.</p><div class="ax-row">' +
                "".join(f'<button class="ax-btn" type="button" data-fmt="{f}">{f.upper()}</button>' for f in ("pdf", "xlsx", "csv", "html")) +
                '</div></div><h2 class="ax-h2">Generated before</h2>' +
                K.table("t-arch", [("created_at", "Generated", "text"), ("period_label", "Period", "text"), ("format", "Format", "text"),
                                   ("link", "File", "html")],
                        [{**a, "link": f'<a href="/api/v2/reports/archive/{a["id"]}/download">Download</a>'} for a in arch],
                        empty="Nothing generated yet.") +
                '<p><a href="/reports?tab=schedules">Schedule a report →</a></p>' +
                f"""<script>document.querySelectorAll('[data-fmt]').forEach(function(b){{b.addEventListener('click',function(){{b.disabled=true;
AX.api('/api/v2/reports/generate',{{body:{{kind:'session_cycle_book',params:{{sessions:['{e(sid)}']}},formats:[b.getAttribute('data-fmt')]}}}})
.then(function(r){{location.href=r.files[0].url;}}).catch(function(e){{AX.toast(e.message,true);}}).finally(function(){{b.disabled=false;}});}});}});</script>""")
        return page(title, body, path, "tab=reports", root=root, app=app, mar=mar)
    # manage
    tl = '<div class="ax-tiles">' + "".join([
        K.tile("Starting amount", K.money(ti["start_capital"])), K.tile("Equity now", K.money(ti["equity"]), f'cash {K.money(ti["cash"])}'),
        K.tile("Gross P&L", K.money(ti["gross_pnl"], True), "before any cost", True),
        K.tile("Charges", K.money(ti["charges"])), K.tile("Tax (est.)", K.money(ti["tax_est"]), "provisional"),
        K.tile("Actual P&L", K.money(ti["actual_pnl"], True), "after charges and tax", True),
        K.tile("Return", f'{K.pct(ti["abs_return_gross_pct"])} / {K.pct(ti["abs_return_actual_pct"])}', "gross / actual"),
        K.tile("CAGR", f'{K.pct(ti["cagr_gross_pct"])} / {K.pct(ti["cagr_actual_pct"])}', "gross / actual (after 30 days)"),
        K.tile("Cycles", f'<span class="ax-num">{ti["cycles_done"]} / {ti["max_cycles"]}</span>'),
        K.tile("Win rate", K.pct(ti["win_rate_pct"], False)), K.tile("Average R", e(ti["avg_r"] if ti["avg_r"] is not None else "—")),
        K.tile("Max drawdown", K.pct(ti["max_drawdown_pct"]), "on actual equity"),
        K.tile("Best cycle", K.money((ti["best_cycle"] or {}).get("actual_pnl"), True), e((ti["best_cycle"] or {}).get("symbol") or "")),
        K.tile("Worst cycle", K.money((ti["worst_cycle"] or {}).get("actual_pnl"), True), e((ti["worst_cycle"] or {}).get("symbol") or "")),
        K.tile("Current floor", K.money(ti["floor"]), e(ti["floor_rule"].replace("_", " ").lower())),
        K.tile("Tax set aside", K.money(ti["tax_reserve"]), e(ti["compound_basis"].replace("_", " ").lower()))]) + "</div>"
    waiting = f'<div class="ax-banner warn" role="status">{e(ti["waiting"])}</div>' if ti.get("waiting") else ""
    opens = "".join(f'<div class="ax-card"><h3>Open cycle · {e(o["symbol"])}</h3>' + K.kv([
        ("Bought", f'{o["qty"]:g} at {K.money(o["buy_price"])} on {e(o["since"])}'), ("Now", f'{K.money(o["mark"])} · {K.money(o["unrealised"], True)}'),
        ("Stop", K.money(o["stop"]) + (" (trailing)" if o["trailing"] else "")), ("Sells at", K.money(o["sell_at"]))]) +
        K.progress(o["progress"]) + "</div>" for o in b["open"])
    eq = [{"label": f'#{p["n"]} {p["date"]}', "gross": p["gross"], "actual": p["actual"]} for p in b["charts"]["equity"]]
    charts = ('<div class="ax-grid-2"><div class="ax-card"><h2>Equity: gross vs actual</h2>' +
              K.line_chart(eq, [("gross", "Gross", "--ax-series-2"), ("actual", "Actual", "--ax-series-1")],
                           f"{sid} equity after each cycle, gross and actual") +
              '</div><div class="ax-card"><h2>From gross to actual</h2>' +
              K.waterfall(b["charts"]["waterfall"], f"{sid}: gross P&L less each charge and the tax, to actual P&L") +
              '</div></div>')
    totals = [{"n": None, "symbol": "Total", **{k: b["totals"].get(k) for k in ("buy_value", "sell_value", "gross_pnl", "charges",
                                                                              "net_pnl", "tax_est", "actual_pnl")},
               **{f"ch_{k}": v for k, v in b["totals"]["charges_by_type"].items()}}]
    table = ('<h2 class="ax-h2">Every cycle</h2><p class="ax-small ax-muted">Gross and actual P&amp;L are never added '
             'together. Click a row for the full breakdown.</p>' +
             K.table("t-cycles", CYCLE_COLS, rows, totals, name=f"{sid}_cycles", drawer=_cycle_drawer,
                     empty="No cycle has closed yet."))
    ev = "".join(f'<li><time>{e(str(x["at"]).replace("T", " ")[:16])}</time><span><b>{e(EVENT_LABEL.get(x["event"], x["event"].title()))}</b> '
                 f'<span class="ax-muted">{e(event_text(x))}</span></span></li>' for x in b["events"])
    timeline = ('<div class="ax-grid-2" style="margin-top:14px"><div class="ax-card"><h2>Compounding: what each cycle started '
                'with</h2>' + K.staircase(b["charts"]["staircase"], f"{sid}: each cycle's start amount and its floor") +
                f'</div><div class="ax-card"><h2>Activity</h2><ol class="ax-timeline">{ev or "<li>No change yet.</li>"}</ol></div></div>')
    body = (_closed_banner() + waiting + tl + (f'<div class="ax-grid" style="margin-top:14px">{opens}</div>' if opens else "") +
            '<div style="margin-top:14px"></div>' + charts + timeline + table + _worked_example() + gen_js)
    return page(title, body, path, root=root, app=app, mar=mar, actions=actions,
                job=f"{s.get('strategy') or ''} · {s['mode']} · {s['status']} · {b['pool']['exit_rule']}")


# ── Accounting ───────────────────────────────────────────────────────────────────────────────

ACC_TABS = [("ledger", "General ledger"), ("session", "Session ledger"), ("charges", "Charges register"), ("tax", "Tax register"),
            ("funds", "Funds"), ("recon", "Reconciliation"), ("transfers", "Transfers"), ("trial", "Trial balance")]


def _select(name, label, options, current, allow_all=True) -> str:
    opts = (['<option value="">All</option>'] if allow_all else []) + [
        f'<option value="{e(v)}"{" selected" if str(v) == str(current or "") else ""}>{e(l)}</option>' for v, l in options]
    return (f'<div class="ax-field"><label for="q-{e(name)}">{e(label)}</label><select class="ax-select" id="q-{e(name)}" '
            f'name="{e(name)}">{"".join(opts)}</select></div>')


def accounting(conn, q: dict, root="", app=False) -> str:
    from accounting import cycles as C
    from accounting import ledger as LG
    from accounting import reconcile as RC
    from accounting import tax as TX
    tab = q.get("tab") or "ledger"
    if tab not in dict(ACC_TABS):
        tab = "ledger"
    C.sync(conn)
    sids = _session_ids(conn)
    tabs = '<nav class="ax-tabs" aria-label="Accounting views">' + "".join(
        f'<a href="/accounting?tab={k}"{" aria-current=page" if k == tab else ""}>{e(l)}</a>' for k, l in ACC_TABS) + "</nav>"
    fys = LG.by_fy_options(conn)
    body = ""
    if tab == "ledger":
        r = LG.entries(conn, q.get("session"), None, q.get("kind"), q.get("env"), q.get("fy"))
        tb = LG.trial_balance(conn)
        filt = ('<form class="ax-row" method="get"><input type="hidden" name="tab" value="ledger">' +
                _select("session", "Session", [(i, f"{i} {n}") for i, n, _ in sids], q.get("session")) +
                _select("kind", "Kind", [(k, k.title().replace("_", " ")) for k in LG.KINDS], q.get("kind")) +
                _select("env", "Env", [("VIRTUAL", "Virtual (paper)"), ("ACTUAL", "Actual (broker)")], q.get("env")) +
                _select("fy", "Financial year", [(f, f) for f in fys], q.get("fy")) +
                '<div class="ax-field" style="justify-content:flex-end"><button class="ax-btn" type="submit">Apply</button></div></form>')
        ok = (f'<div class="ax-banner {"good" if tb["balanced"] else "bad"}" role="status">{"✓ Books balance" if tb["balanced"] else "✗ Books do not balance"}: '
              f'debits {K.inr(tb["debit"])} · credits {K.inr(tb["credit"])}.</div>')
        rows = [{**x, "at": str(x["at"]).replace("T", " ")[:16]} for x in r["rows"]]
        body = ok + filt + K.table("t-gl", [("at", "When (IST)", "text"), ("txn_id", "Transaction", "text"), ("kind", "Kind", "text"),
                                            ("account", "Account", "text"), ("debit", "Debit", "money"), ("credit", "Credit", "money"),
                                            ("session_id", "Session", "text"), ("symbol", "Symbol", "text"), ("memo", "Memo", "text"),
                                            ("env", "Env", "text"), ("fy", "FY", "text")], rows,
                                   totals=[{"at": "Shown", "debit": r["debit"], "credit": r["credit"]}], name="general_ledger",
                                   empty="No ledger line yet: lines appear when a session gets money and as its cycles close.")
    elif tab == "session":
        sid = q.get("session") or next((i for i, _, p in sids if p), "S1")
        led = LG.session_ledger(conn, sid)
        rows = [{**x, "at": str(x["at"]).replace("T", " ")[:16]} for x in led["rows"]]
        body = ('<form class="ax-row" method="get"><input type="hidden" name="tab" value="session">' +
                _select("session", "Session", [(i, f"{i} {n}") for i, n, _ in sids], sid, allow_all=False) +
                '<div class="ax-field" style="justify-content:flex-end"><button class="ax-btn" type="submit">Show</button></div></form>' +
                f'<p>Cash balance in the ledger: <b>{K.money(led["balance"])}</b> · <a href="/sessions/{e(sid)}">{e(sid)} cycles →</a></p>' +
                K.table("t-sl", [("at", "When (IST)", "text"), ("kind", "Kind", "text"), ("memo", "What", "text"), ("symbol", "Symbol", "text"),
                                 ("debit", "In", "money"), ("credit", "Out", "money"), ("balance", "Balance", "money"), ("env", "Env", "text")],
                        rows, name=f"{sid}_ledger", empty=f"{sid} has no ledger line yet."))
    elif tab == "charges":
        by = q.get("by") or "type"
        reg = LG.charges_register(conn, by if by in ("type", "session", "month") else "type", q.get("fy"), q.get("env"))
        cols = [(by, {"type": "Charge", "session": "Session", "month": "Month"}[reg["by"]], "text")] + \
            [(k, C.CHARGE_LABEL[k], "money") for k in (*C.CHARGE_TYPES, "other")] + [("total", "Total", "money")]
        if reg["by"] == "type":
            cols = [("type", "Charge", "text"), ("total", "Amount", "money")]
        body = ('<form class="ax-row" method="get"><input type="hidden" name="tab" value="charges">' +
                _select("by", "Group by", [("type", "Charge type"), ("session", "Session"), ("month", "Month")], reg["by"], False) +
                _select("fy", "Financial year", [(f, f) for f in fys], q.get("fy")) +
                _select("env", "Env", [("VIRTUAL", "Virtual (paper)"), ("ACTUAL", "Actual (broker)")], q.get("env")) +
                '<div class="ax-field" style="justify-content:flex-end"><button class="ax-btn" type="submit">Apply</button></div></form>' +
                K.table("t-ch", cols, reg["rows"], totals=[{cols[0][0]: "Total", "total": reg["total"]}], name="charges_register",
                        empty="No charges yet: they are recorded as cycles close.") +
                '<p class="ax-small ax-muted">ATIP\'s figures (the paper fill\'s own, itemised with its cost model). Check them '
                'against your broker\'s contract notes on the Reconciliation tab.</p>')
    elif tab == "tax":
        fy = q.get("fy") or TX.fy_of(date.today())
        tr = LG.tax_register(conn, fy)
        st = tr["statement"]
        heads = K.table("t-heads", [("label", "Head", "text"), ("gains", "Gains", "money"), ("losses", "Losses", "money"),
                                    ("net", "Net", "pnl"), ("after_set_off", "After set-off", "pnl"), ("taxable", "Taxable", "money"),
                                    ("rate_pct", "Rate", "rate"), ("tax", "Tax (est.)", "money")], st["heads"], tools=False)
        kvs = [(f'Set-off: {x["from"]} loss against {x["against"]}', f'{K.money(x["amount"])} <span class="ax-muted">{e(x["rule"])}</span>')
               for x in st["set_offs"]]
        kvs += [("LTCG exemption used", K.money(st["ltcg_exemption_used"])), ("Tax before surcharge and cess", K.money(st["tax_before_addons"])),
                (f'Surcharge {st["rates"]["surcharge_pct"]:g}% · cess {st["rates"]["cess_pct"]:g}%',
                 f'{K.money(st["surcharge"])} · {K.money(st["cess"])} ({"in" if st["addons_applied"] else "not in"} the liability)'),
                ("Estimated liability", f'<b>{K.money(st["liability"])}</b>'),
                ("Set aside per cycle (provisional)", K.money(st["provisional_set_aside"])),
                ("Provisional − liability", K.money(st["difference"], True))]
        kvs += [(f"Carried forward: {h} loss", K.money(v)) for h, v in st["carried_forward"].items()]
        cyc = [{**c, "opened": str(c["opened_at"])[:10], "closed": str(c["closed_at"])[:10]} for c in tr["cycles"]]
        body = (f'<div class="ax-banner warn" role="note">{e(TX.DISCLAIMER)} Rates effective {e(st["rates"]["effective_from"])}: '
                f'STCG {st["rates"]["stcg_pct"]:g}%, LTCG {st["rates"]["ltcg_pct"]:g}% above {K.inr(st["rates"]["ltcg_exemption"])}, '
                f'slab {st["rates"]["slab_pct"]:g}%. <a href="/settings#tax">Change rates</a></div>'
                '<form class="ax-row" method="get"><input type="hidden" name="tab" value="tax">' +
                _select("fy", "Financial year (Apr–Mar)", [(f, f) for f in tr["fys"]], fy, False) +
                '<div class="ax-field" style="justify-content:flex-end"><button class="ax-btn" type="submit">Show</button></div></form>' +
                heads + '<div class="ax-card" style="margin-top:12px"><h2>Set-offs and liability</h2>' + K.kv(kvs) + "</div>" +
                '<h2 class="ax-h2">Cycles of the year</h2>' +
                K.table("t-taxc", [("session_id", "Session", "text"), ("symbol", "Symbol", "text"), ("opened", "Bought", "text"),
                                   ("closed", "Sold", "text"), ("holding_days", "Days", "int"), ("net_pnl", "Net P&L", "pnl"),
                                   ("tax_head", "Head", "text"), ("tax_est", "Tax (est.)", "money"), ("env", "Env", "text")], cyc,
                        name=f"tax_{fy}", empty="No cycle closed in this financial year.") +
                f'<p><a class="ax-btn" href="/reports?kind=fy_tax_statement&fy={e(fy)}">FY tax statement for your CA →</a></p>')
    elif tab == "funds":
        f = LG.funds(conn)
        v, a = f["VIRTUAL"], f["ACTUAL"]
        body = ('<div class="ax-grid-2"><div class="ax-card"><h2>Virtual (paper) <span class="ax-st info">VIRTUAL</span></h2>' + K.kv([
            ("Paper account cash", K.money(v.get("cash"))), ("Invested at cost", K.money(v.get("invested_at_cost"))),
            ("Invested at market", K.money(v.get("invested_at_market"))), ("Cumulative deployed (all buys)", K.money(v.get("cumulative_deployed"))),
            ("Allocated to session pools", K.money(v.get("allocated_to_pools"))), ("Pools' cash now", K.money(v.get("pools_cash"))),
            ("Pools' equity now", K.money(v.get("pools_equity"))), ("Paper cash outside the pools", K.money(v.get("outside_pools_cash")))]) +
            '</div><div class="ax-card"><h2>Actual (broker) <span class="ax-st warn">ACTUAL</span></h2>' + K.kv([
                ("Holdings (latest Dhan sync)", f'{a.get("holdings") or 0} on {e(a.get("holdings_date") or "—")}'),
                ("Invested at cost", K.money(a.get("invested_at_cost"))), ("Invested at market", K.money(a.get("invested_at_market"))),
                ("Available cash", "read live on <a href='/brokers'>Brokers</a>")]) +
            f'<p class="ax-small ax-muted">{e(a.get("note"))}</p></div></div><p class="ax-small ax-muted">Virtual and actual '
            'money are never added together on any ATIP screen or report.</p>')
    elif tab == "recon":
        its = RC.items(conn)
        rows = []
        for i in its:
            act = (f'<button class="ax-btn sm" type="button" data-res="{i["id"]}" data-st="ACCEPTED">Accept</button> '
                   f'<button class="ax-btn sm" type="button" data-res="{i["id"]}" data-st="INVESTIGATE">Investigate</button>'
                   if i["status"] in ("OPEN", "INVESTIGATE") else "")
            tone = {"MATCHED": "good", "ACCEPTED": "good", "OPEN": "bad", "INVESTIGATE": "warn"}.get(i["status"], "")
            rows.append({**i, "st": K.status(i["status"], tone), "act": act})
        body = ('<div class="ax-grid-2"><div class="ax-card"><h2>Charges vs the broker\'s contract note</h2><p class="ax-small ax-muted">'
                'Upload the contract note as CSV (columns: trade date, symbol, buy/sell, quantity, and total charges or one '
                'column per charge). Each row is matched to an ATIP order on date, symbol and side.</p>'
                '<div class="ax-field"><label for="cn-file">Contract note (CSV)</label><input class="ax-input" type="file" id="cn-file" accept=".csv,text/csv"></div>'
                '<button class="ax-btn primary" type="button" id="cn-go">Reconcile charges</button><div class="ax-err" id="cn-err" role="alert"></div></div>'
                '<div class="ax-card"><h2>Session cash vs the books</h2><p class="ax-small ax-muted">Each session\'s cash as the '
                'trading engine sees it against its Cash account in the ledger, and the paper account against all sessions.</p>'
                '<button class="ax-btn primary" type="button" id="cash-go">Check cash now</button><div id="cash-out"></div></div></div>'
                '<h2 class="ax-h2">Differences and matches</h2>' +
                K.table("t-recon", [("created_at", "Checked", "text"), ("kind", "Kind", "text"), ("session_id", "Session", "text"),
                                    ("on_date", "Date", "text"), ("symbol", "Symbol", "text"), ("side", "Side", "text"),
                                    ("atip_value", "ATIP", "money"), ("broker_value", "Broker / ledger", "money"), ("diff", "Difference", "pnl"),
                                    ("detail", "Detail", "text"), ("st", "Status", "html"), ("note", "Note", "text"), ("act", "Action", "html")],
                        rows, name="reconciliation", empty="Nothing reconciled yet.") +
                """<script>(function(){var g=document.getElementById('cn-go');g.addEventListener('click',function(){var f=document.getElementById('cn-file').files[0],er=document.getElementById('cn-err');er.textContent='';
if(!f){er.textContent='Choose the contract-note CSV first.';return;}f.text().then(function(t){return AX.api('/api/v2/accounting/recon/import',{body:t});}).then(function(r){AX.toast(r.rows+' rows: '+r.matched+' matched, '+r.open+' with a difference, '+r.unmatched+' not found.');setTimeout(function(){location.reload();},900);}).catch(function(e){er.textContent=e.message;});});
document.getElementById('cash-go').addEventListener('click',function(){AX.api('/api/v2/accounting/recon/cash',{body:{}}).then(function(r){var bad=r.sessions.filter(function(s){return s.status!=='MATCHED';}).length;
document.getElementById('cash-out').innerHTML='<p class="ax-small">'+(bad?bad+' session(s) differ.':'All '+r.sessions.length+' sessions match.')+' Paper cash '+AX.inr(r.paper_cash)+'; sessions '+AX.inr(r.pools_cash)+'; outside the pools '+AX.inr(r.outside_pools_cash)+'.</p>';setTimeout(function(){location.reload();},1500);}).catch(function(e){AX.toast(e.message,true);});});
document.querySelectorAll('[data-res]').forEach(function(b){b.addEventListener('click',function(){var note=prompt(b.getAttribute('data-st')==='ACCEPTED'?'Why is the difference fine? (optional)':'What needs checking? (optional)')||'';
AX.api('/api/v2/accounting/recon/'+b.getAttribute('data-res'),{body:{status:b.getAttribute('data-st'),note:note}}).then(function(){location.reload();}).catch(function(e){AX.toast(e.message,true);});});});})();</script>""")
    elif tab == "transfers":
        from orders import sessions as S
        pools = [x for x in S.list_sessions(conn)["sessions"] if x.get("pool")]
        cash = {x["id"]: {"cash": x["pool"]["investable_cash"], "open": [h["symbol"] for h in x["pool"]["open"]]} for x in pools}
        opts = '<option value="">Unallocated money</option>' + "".join(
            f'<option value="{e(x["id"])}">{e(x["id"])} {e(x["name"])} — {K.inr(x["pool"]["investable_cash"])} free'
            f'{" (open trade)" if x["pool"]["open"] else ""}</option>' for x in pools)
        hist = _rows(conn, "SELECT at, from_sid, to_sid, amount, reason, env FROM session_transfers ORDER BY id DESC LIMIT 200")
        body = ('<div class="ax-grid-2"><form class="ax-card" id="tf"><h2>Move money</h2><p class="ax-small ax-muted">Allowed only '
                'when the session the money leaves has no open trade. No order changes and no books restart.</p>'
                f'<div class="ax-row"><div class="ax-field"><label for="tf-from">From</label><select class="ax-select" id="tf-from" name="from">{opts}</select></div>'
                f'<div class="ax-field"><label for="tf-to">To</label><select class="ax-select" id="tf-to" name="to">{opts}</select></div></div>'
                '<div class="ax-field"><label for="tf-amt">Amount (₹)</label><input class="ax-input" id="tf-amt" name="amount" inputmode="decimal"></div>'
                '<div class="ax-field"><label for="tf-why">Reason</label><input class="ax-input" id="tf-why" name="reason" maxlength="200" placeholder="e.g. top up S4 after a good month"></div>'
                '<p class="ax-small" id="tf-impact" aria-live="polite"></p><div class="ax-err" id="tf-err" role="alert"></div>'
                '<button class="ax-btn primary" type="submit">Move money…</button></form>'
                '<details class="ax-card"><summary><b>Manual adjustment</b> (books only)</summary><form id="adj"><p class="ax-small ax-muted">Corrects the ledger; '
                'the session\'s trading cash is not changed. Positive adds to the account, negative takes off.</p>'
                '<div class="ax-field"><label for="adj-acc">Account</label><input class="ax-input" id="adj-acc" name="account" placeholder="Cash:S1"></div>'
                '<div class="ax-field"><label for="adj-amt">Amount (₹, + or −)</label><input class="ax-input" id="adj-amt" name="amount" inputmode="decimal"></div>'
                '<div class="ax-field"><label for="adj-why">Reason</label><input class="ax-input" id="adj-why" name="reason" maxlength="200"></div>'
                '<button class="ax-btn" type="submit">Post adjustment</button></form></details></div>'
                '<h2 class="ax-h2">Transfers made</h2>' +
                K.table("t-tf", [("at", "When", "text"), ("from_sid", "From", "text"), ("to_sid", "To", "text"), ("amount", "Amount", "money"),
                                 ("reason", "Reason", "text"), ("env", "Env", "text")],
                        [{**h, "from_sid": h["from_sid"] or "unallocated", "to_sid": h["to_sid"] or "unallocated",
                          "at": str(h["at"]).replace("T", " ")[:16]} for h in hist], name="transfers", empty="No transfer yet.") +
                f'<div id="tf-cash" hidden data-json="{e(json.dumps(cash, default=str))}"></div>' +
                """<script>(function(){var C=JSON.parse(document.getElementById('tf-cash').getAttribute('data-json')),f=document.getElementById('tf');
function imp(){var a=parseFloat((f.amount.value||'').replace(/,/g,'')),fr=f.from.value,to=f.to.value,t=document.getElementById('tf-impact');if(!a||a<=0){t.textContent='';return;}
var s=[];if(fr)s.push(fr+': '+AX.inr(C[fr].cash)+' → '+AX.inr(C[fr].cash-a)+(C[fr].open.length?' — blocked: open trade in '+C[fr].open.join(', '):''));if(to)s.push(to+': '+AX.inr(C[to].cash)+' → '+AX.inr(C[to].cash+a));t.textContent=s.join(' · ');}
['amount','from','to'].forEach(function(n){f[n].addEventListener('input',imp);f[n].addEventListener('change',imp);});
f.addEventListener('submit',function(ev){ev.preventDefault();var er=document.getElementById('tf-err');er.textContent='';imp();
if(!confirm('Move '+AX.inr(f.amount.value.replace(/,/g,''))+' from '+(f.from.value||'unallocated')+' to '+(f.to.value||'unallocated')+'?\\n'+document.getElementById('tf-impact').textContent))return;
AX.api('/api/v2/accounting/transfer',{body:{from:f.from.value||null,to:f.to.value||null,amount:f.amount.value.replace(/,/g,''),reason:f.reason.value}}).then(function(){AX.toast('Moved.');setTimeout(function(){location.reload();},700);}).catch(function(e){er.textContent=e.message;});});
var a=document.getElementById('adj');a.addEventListener('submit',function(ev){ev.preventDefault();AX.api('/api/v2/accounting/adjust',{body:{account:a.account.value,amount:a.amount.value,reason:a.reason.value}}).then(function(){AX.toast('Adjustment posted.');setTimeout(function(){location.reload();},700);}).catch(function(e){AX.toast(e.message,true);});});})();</script>""")
    elif tab == "trial":
        env = q.get("env")
        tb = LG.trial_balance(conn, env or None, q.get("fy") or None)
        body = ('<form class="ax-row" method="get"><input type="hidden" name="tab" value="trial">' +
                _select("env", "Env", [("VIRTUAL", "Virtual (paper)"), ("ACTUAL", "Actual (broker)")], env) +
                _select("fy", "Financial year", [(f, f) for f in fys], q.get("fy")) +
                '<div class="ax-field" style="justify-content:flex-end"><button class="ax-btn" type="submit">Apply</button></div></form>' +
                f'<div class="ax-banner {"good" if tb["balanced"] else "bad"}">{"✓ Balanced" if tb["balanced"] else "✗ Not balanced"}</div>' +
                K.table("t-tb", [("account", "Account", "text"), ("debit", "Debits", "money"), ("credit", "Credits", "money"),
                                 ("balance", "Balance (Dr − Cr)", "pnl")], tb["rows"],
                        totals=[{"account": "Total", "debit": tb["debit"], "credit": tb["credit"]}], name="trial_balance"))
    return page("Accounting", tabs + body, "/accounting", f"tab={tab}", root=root, app=app,
                job="The money every session touched: a double-entry ledger, every charge, estimated tax, virtual and actual "
                    "funds kept apart, and reconciliation against the broker.",
                actions='<a class="ax-btn" href="/reports?kind=monthly_pnl">Monthly P&amp;L statement</a>')


# ── Reports ──────────────────────────────────────────────────────────────────────────────────

def reports(conn, q: dict, root="", app=False) -> str:
    from accounting import tax as TX
    from enterprise import session_reports as R
    tab = q.get("tab") or "generate"
    tabs = '<nav class="ax-tabs" aria-label="Reports">' + "".join(
        f'<a href="/reports?tab={k}"{" aria-current=page" if k == tab else ""}>{e(l)}</a>' for k, l in
        (("generate", "Generate"), ("schedules", "Schedules"), ("archive", "Archive"), ("custom", "Custom report"))) + "</nav>"
    sids = _session_ids(conn)
    fy = TX.fy_of(date.today())
    fmts = "".join(f'<label><input type="checkbox" name="formats" value="{f}"{" checked" if f in ("pdf", "xlsx") else ""}>'
                   f'<span>{f.upper()}</span></label>' for f in ("pdf", "xlsx", "csv", "html"))
    gen_js = """<script>document.querySelectorAll('form[data-gen]').forEach(function(f){f.addEventListener('submit',function(ev){ev.preventDefault();
var p={},fm=[].slice.call(f.querySelectorAll('input[name=formats]:checked')).map(function(x){return x.value;});
f.querySelectorAll('[data-p]').forEach(function(x){if(x.type==='checkbox'){if(x.checked)(p[x.getAttribute('data-p')]=p[x.getAttribute('data-p')]||[]).push(x.value);}else if(x.value)p[x.getAttribute('data-p')]=x.value;});
var out=f.querySelector('.ax-out'),b=f.querySelector('button[type=submit]');if(!fm.length){out.innerHTML='<span class="ax-err">Pick at least one format.</span>';return;}
b.disabled=true;out.innerHTML='<div class="ax-skel" style="height:18px;width:60%"></div>';
AX.api('/api/v2/reports/generate',{body:{kind:f.getAttribute('data-gen'),params:p,formats:fm}}).then(function(r){out.innerHTML='<div class="ax-small">'+AX.esc(r.period.label)+' · '+r.rows+' rows · '+r.files.map(function(x){return '<a href="'+x.url+'">'+x.format.toUpperCase()+'</a>';}).join(' · ')+'</div>';})
.catch(function(e){out.innerHTML='<span class="ax-err">'+AX.esc(e.message)+'</span>';}).finally(function(){b.disabled=false;});});});</script>"""
    body = ""
    if tab == "generate":
        pick = q.get("kind")
        cards = []
        for kind, k in R.KINDS.items():
            if kind == "custom":
                continue
            if kind == "daily_session_summary":
                inp = f'<div class="ax-field"><label for="p-{kind}">Day</label><input class="ax-input" id="p-{kind}" type="date" data-p="date" value="{date.today()}"></div>'
            elif kind == "weekly_performance":
                inp = f'<div class="ax-field"><label for="p-{kind}">Week ending</label><input class="ax-input" id="p-{kind}" type="date" data-p="date" value="{date.today()}"></div>'
            elif kind == "monthly_pnl":
                inp = f'<div class="ax-field"><label for="p-{kind}">Month</label><input class="ax-input" id="p-{kind}" type="month" data-p="month" value="{date.today():%Y-%m}"></div>'
            elif kind == "quarterly_review":
                qs = [f"{fy} Q{i}" for i in range(1, 5)]
                inp = f'<div class="ax-field"><label for="p-{kind}">Quarter</label><select class="ax-select" id="p-{kind}" data-p="quarter">' + "".join(
                    f'<option{" selected" if x == R.fy_quarter(date.today())[0] else ""}>{x}</option>' for x in qs) + "</select></div>"
            elif kind == "fy_tax_statement":
                y = int(fy.split()[1][:4])
                inp = f'<div class="ax-field"><label for="p-{kind}">Financial year</label><select class="ax-select" id="p-{kind}" data-p="fy">' + "".join(
                    f'<option{" selected" if f"FY {x}-{str(x + 1)[-2:]}" == (q.get("fy") or fy) else ""}>FY {x}-{str(x + 1)[-2:]}</option>'
                    for x in range(y, y - 4, -1)) + "</select></div>"
            else:
                inp = f'<div class="ax-field"><label for="p-{kind}">Session</label><select class="ax-select" id="p-{kind}" data-p="sessions">' + "".join(
                    f'<option value="{e(i)}">{e(i)} {e(n)}</option>' for i, n, p in sids if p) + "</select></div>"
            sched = k["schedule"]
            cards.append(f'<form class="ax-card" data-gen="{kind}" id="gen-{kind}"{" style=outline:2px solid var(--ax-brand)" if pick == kind else ""}>'
                         f'<h2>{e(k["label"])}</h2><p class="ax-small ax-muted">{e(k["about"])}</p>{inp}'
                         f'<div class="ax-field"><span class="ax-label">Formats</span><div class="ax-choice">{fmts}</div></div>'
                         f'<button class="ax-btn primary" type="submit">Generate</button>'
                         f'<p class="ax-small ax-muted">{e(R.describe({**sched, "freq": sched["freq"]}) if sched else "On demand")}</p>'
                         f'<div class="ax-out" aria-live="polite"></div></form>')
        body = f'<div class="ax-grid">{"".join(cards)}</div>{gen_js}'
    elif tab == "custom":
        secs = "".join(f'<label><input type="checkbox" data-p="sections" value="{k}"{" checked" if k in ("summary", "pnl", "charges") else ""}>'
                       f'<span>{e(l)}</span></label>' for k, l in R.CUSTOM_SECTIONS.items())
        ss = "".join(f'<label><input type="checkbox" data-p="sessions" value="{e(i)}"><span>{e(i)}</span></label>' for i, _, _ in sids)
        body = ('<form class="ax-card" data-gen="custom"><h2>Build a report</h2>'
                f'<div class="ax-field"><label for="c-title">Title</label><input class="ax-input" id="c-title" data-p="title" placeholder="My report"></div>'
                f'<div class="ax-field"><span class="ax-label">Sections</span><div class="ax-choice">{secs}</div></div>'
                f'<div class="ax-field"><span class="ax-label">Sessions (none ticked = all)</span><div class="ax-choice">{ss}</div></div>'
                f'<div class="ax-row"><div class="ax-field"><label for="c-from">From</label><input class="ax-input" type="date" id="c-from" data-p="from" value="{date.today().replace(day=1)}"></div>'
                f'<div class="ax-field"><label for="c-to">To</label><input class="ax-input" type="date" id="c-to" data-p="to" value="{date.today()}"></div></div>'
                f'<div class="ax-field"><span class="ax-label">Formats</span><div class="ax-choice">{fmts}</div></div>'
                '<button class="ax-btn primary" type="submit">Generate</button><div class="ax-out" aria-live="polite"></div></form>'
                '<p class="ax-small ax-muted">To run a custom report on a schedule, add a schedule of kind “Custom report” on the Schedules tab.</p>' + gen_js)
    elif tab == "schedules":
        rows = R.schedules(conn)
        data = {r["id"]: r for r in rows}
        trs = [{**r, "kind_l": R.KINDS[r["kind"]]["label"], "fmts": ", ".join(f.upper() for f in r["formats"]),
                "on": K.status("On", "good") if r["enabled"] else K.status("Off"),
                "last": (str(r["last_run_at"] or "—").replace("T", " ")[:16]) + (f' · ✗ {r["last_error"]}' if r.get("last_error") else ""),
                "edit": f'<button class="ax-btn sm" type="button" data-edit="{r["id"]}">Edit</button>'} for r in rows]
        kinds = "".join(f'<option value="{k}">{e(v["label"])}</option>' for k, v in R.KINDS.items())
        days = "".join(f'<option value="{i}">{d}</option>' for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
        body = ('<div class="ax-row" style="margin-bottom:10px"><button class="ax-btn primary" type="button" data-edit="new">New schedule</button>'
                '<button class="ax-btn" type="button" id="run-due">Run due schedules now</button></div>' +
                K.table("t-sched", [("name", "Name", "text"), ("kind_l", "Report", "text"), ("when", "When (IST)", "text"),
                                    ("fmts", "Formats", "text"), ("on", "Status", "html"), ("last", "Last run", "text"), ("edit", "", "html")],
                        trs, name="schedules", empty="No schedule.") +
                '<p class="ax-small ax-muted">Schedules are checked on the Mac every 15 minutes (06:00–20:45 IST) (a run missed while the Mac slept '
                'is caught up the same day), store every file in the Archive, and send a note to your inbox and channels.</p>' +
                f"""<aside class="ax-drawer" id="ax-sched" aria-hidden="true" aria-labelledby="sch-h"><header><h2 id="sch-h">Schedule</h2><button class="ax-iconbtn" type="button" data-ax-close aria-label="Close">✕</button></header>
<form class="ax-body" id="sch-f"><div class="ax-field"><label for="s-name">Name</label><input class="ax-input" id="s-name" name="name"></div>
<div class="ax-field"><label for="s-kind">Report</label><select class="ax-select" id="s-kind" name="kind">{kinds}</select></div>
<div class="ax-row"><div class="ax-field"><label for="s-freq">How often</label><select class="ax-select" id="s-freq" name="freq"><option value="daily">Every trading day</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option><option value="quarterly">Each FY quarter</option><option value="fy">Each financial year (1 Apr)</option><option value="cron">Custom (cron)</option></select></div>
<div class="ax-field"><label for="s-time">Time (IST)</label><input class="ax-input" id="s-time" name="time_ist" placeholder="08:00"></div></div>
<div class="ax-row"><div class="ax-field" id="w-wd"><label for="s-wd">Weekday</label><select class="ax-select" id="s-wd" name="weekday">{days}</select></div>
<div class="ax-field" id="w-day"><label for="s-day">Day of month (1–28)</label><input class="ax-input" id="s-day" name="day" inputmode="numeric"></div>
<div class="ax-field" id="w-cron"><label for="s-cron">Cron (min hour day month weekday)</label><input class="ax-input" id="s-cron" name="cron" placeholder="30 17 * * 1-5"></div></div>
<div class="ax-field"><span class="ax-label">Formats</span><div class="ax-choice">{"".join(f'<label><input type="checkbox" name="formats" value="{f}"><span>{f.upper()}</span></label>' for f in R.FORMATS)}</div></div>
<div class="ax-field"><label for="s-params">Options (JSON, e.g. {{"sessions": ["S1"]}})</label><input class="ax-input" id="s-params" name="params"></div>
<label class="ax-check"><input type="checkbox" name="enabled"><span>On</span></label><label class="ax-check"><input type="checkbox" name="deliver"><span>Send to my inbox and channels</span></label>
<div class="ax-err" id="sch-err" role="alert"></div></form>
<footer><button class="ax-btn danger" type="button" id="sch-del">Delete</button><span style="flex:1"></span><button class="ax-btn" type="button" data-ax-close>Cancel</button><button class="ax-btn primary" type="submit" form="sch-f">Save</button></footer></aside>
<div id="sch-data" hidden data-json="{e(json.dumps(data, default=str))}"></div>
<script>(function(){{var D=JSON.parse(document.getElementById('sch-data').getAttribute('data-json')),f=document.getElementById('sch-f'),cur=null;
function ui(){{var v=f.freq.value;document.getElementById('w-wd').hidden=v!=='weekly';document.getElementById('w-day').hidden=v!=='monthly';document.getElementById('w-cron').hidden=v!=='cron';}}
f.freq.addEventListener('change',ui);
document.querySelectorAll('[data-edit]').forEach(function(b){{b.addEventListener('click',function(){{var id=b.getAttribute('data-edit');cur=id==='new'?null:D[id];var r=cur||{{name:'',kind:'daily_session_summary',freq:'daily',time_ist:'17:30',formats:['pdf','xlsx'],enabled:1,deliver:1,params:{{}},weekday:0,day:1}};
f.name.value=r.name;f.kind.value=r.kind;f.freq.value=r.freq;f.time_ist.value=r.time_ist||'';f.weekday.value=r.weekday||0;f.day.value=r.day||'';f.cron.value=r.cron||'';f.params.value=JSON.stringify(r.params||{{}});f.enabled.checked=!!r.enabled;f.deliver.checked=!!r.deliver;
f.querySelectorAll('input[name=formats]').forEach(function(c){{c.checked=(r.formats||[]).indexOf(c.value)>=0;}});document.getElementById('sch-del').hidden=!cur;document.getElementById('sch-err').textContent='';ui();AX.openDrawer('ax-sched');}});}});
f.addEventListener('submit',function(ev){{ev.preventDefault();var p={{}};try{{p=JSON.parse(f.params.value||'{{}}');}}catch(e){{document.getElementById('sch-err').textContent='Options must be JSON, like {{"sessions": ["S1"]}}.';return;}}
var b={{name:f.name.value,kind:f.kind.value,freq:f.freq.value,time_ist:f.time_ist.value,weekday:f.weekday.value,day:f.day.value,cron:f.cron.value,params:p,enabled:f.enabled.checked,deliver:f.deliver.checked,formats:[].slice.call(f.querySelectorAll('input[name=formats]:checked')).map(function(c){{return c.value;}})}};
AX.api('/api/v2/reports/schedules'+(cur?'/'+cur.id:''),{{body:b}}).then(function(){{location.reload();}}).catch(function(e){{document.getElementById('sch-err').textContent=e.message;}});}});
document.getElementById('sch-del').addEventListener('click',function(){{if(!cur||!confirm('Delete the schedule “'+cur.name+'”? Reports already made stay in the archive.'))return;AX.api('/api/v2/reports/schedules/'+cur.id+'/delete',{{body:{{}}}}).then(function(){{location.reload();}}).catch(function(e){{AX.toast(e.message,true);}});}});
document.getElementById('run-due').addEventListener('click',function(){{AX.api('/api/v2/reports/run-due',{{body:{{}}}}).then(function(r){{AX.toast(r.skipped||((r.ran||[]).length+' schedule(s) ran.'));setTimeout(function(){{location.reload();}},900);}}).catch(function(e){{AX.toast(e.message,true);}});}});}})();</script>""")
    elif tab == "archive":
        rows = R.archive(conn, q.get("kind") or None)
        trs = [{**r, "kind_l": R.KINDS.get(r["kind"], {}).get("label", r["kind"]), "created": str(r["created_at"]).replace("T", " ")[:16],
                "kb": round((r["size"] or 0) / 1024, 1),
                "dl": f'<a class="ax-btn sm" href="/api/v2/reports/archive/{r["id"]}/download">Download {e(r["format"].upper())}</a>'} for r in rows]
        body = K.table("t-arch", [("created", "Generated (IST)", "text"), ("kind_l", "Report", "text"), ("period_label", "Period", "text"),
                                  ("format", "Format", "text"), ("env", "Env", "text"), ("engine", "Made with", "text"),
                                  ("kb", "Size (KB)", "int"), ("created_by", "By", "text"), ("dl", "", "html")], trs,
                       name="report_archive", empty="No report generated yet. Make one on the Generate tab.")
    return page("Reports", tabs + body, "/reports", f"tab={tab}" if tab != "generate" else "", root=root, app=app,
                job="Daily, weekly, monthly, quarterly and FY reports of the sessions' money, on schedule or now, in PDF, "
                    "Excel, CSV and HTML. Every figure says whether it is VIRTUAL (paper) or ACTUAL.")


# ── Settings, More, hubs, Ops ────────────────────────────────────────────────────────────────

def settings(conn, root="", app=False) -> str:
    from accounting import tax as TX
    from dashboard.shell import light_state
    st = light_state()
    try:
        from orders.risk import limits
        lim = limits()
    except Exception:
        lim = {}
    try:
        from ops.dhan_token import status as tok
        t = tok()
    except Exception:
        t = {}
    tx = TX.settings()
    num = lambda k, lab, help_="": (f'<div class="ax-field"><label for="tx-{k}">{e(lab)}</label><input class="ax-input" id="tx-{k}" name="{k}" '
                                    f'value="{e(tx[k])}" inputmode="decimal">{f"<div class=ax-help>{e(help_)}</div>" if help_ else ""}</div>')
    body = ('<div class="ax-grid-2">'
            '<section class="ax-card" id="credentials"><h2>Credentials and brokers</h2>' + K.kv([
                ("Broker environment", f'<span class="ax-pill ax-env" data-env="{e(st["env"])}">{e(st["env"])}</span>'),
                ("Dhan access token", e(t.get("message") or t.get("state") or "no problem reported")),
                ("Zerodha", "off until you turn it on")]) +
            '<p><a class="ax-btn" href="/brokers">Brokers</a> <a class="ax-btn" href="/account">Account &amp; sign-in</a></p>'
            '<p class="ax-small ax-muted">Secrets stay in the vault; ATIP never shows them on a page.</p></section>'
            '<section class="ax-card" id="risk"><h2>Risk limits</h2>' + K.kv([(k.replace("_", " "), e(v)) for k, v in lim.items()] or
                                                                            [("Limits", "none configured")]) +
            f'<p>Kill switch: {K.status("ON — " + str(st.get("halt_reason") or ""), "bad") if st.get("halted") else K.status("off", "good")}</p>'
            '<p class="ax-small ax-muted">Limits are set in atip_data/config.json ("risk"). The kill switch and resume are on '
            '<a href="/command">Command Center</a>.</p></section>'
            '<section class="ax-card" id="tax"><h2>Tax rates (estimates)</h2>'
            f'<div class="ax-banner warn">{e(TX.DISCLAIMER)} Verify these before relying on them.</div><form id="txf">' +
            f'<div class="ax-field"><label for="tx-eff">Effective from</label><input class="ax-input" id="tx-eff" name="effective_from" type="date" value="{e(tx["effective_from"])}"></div>'
            '<div class="ax-row">' + num("stcg_pct", "STCG % (≤ 12 months)") + num("ltcg_pct", "LTCG % (> 12 months)") + "</div>"
            '<div class="ax-row">' + num("ltcg_exemption", "LTCG exemption (₹ a year)") + num("slab_pct", "Your slab % (intraday, F&O)") + "</div>"
            '<div class="ax-row">' + num("surcharge_pct", "Surcharge %") + num("cess_pct", "Cess %") + "</div>"
            f'<label class="ax-check"><input type="checkbox" name="addons_enabled"{" checked" if tx["addons_enabled"] else ""}><span>Include surcharge and cess in the estimates</span></label>'
            f'<label class="ax-check"><input type="checkbox" name="stt_deductible"{" checked" if tx["stt_deductible"] else ""}><span>Tax net P&amp;L after STT too (off: STT is not deducted, as s.48 says)</span></label>'
            '<div class="ax-err" id="tx-err" role="alert"></div><button class="ax-btn primary" type="submit">Save tax rates</button></form></section>'
            '<section class="ax-card" id="notifications"><h2>Notifications</h2><p>Report schedules send a note to your inbox and '
            'channels (in-app, Telegram if you opted in). Choose per schedule on <a href="/reports?tab=schedules">Reports › Schedules</a>; '
            'channels and quiet hours are on <a href="/account#notifications">Account</a>.</p></section>'
            '<section class="ax-card" id="display"><h2>Display</h2><p>Theme: light, dark, or follow your device. The button '
            '◐ in the top bar switches it; it is remembered in this browser.</p><button class="ax-btn" type="button" '
            'onclick="AX.cycleTheme()">Switch theme</button></section>'
            '<section class="ax-card" id="admin"><h2>Administration</h2><div class="ax-hub">' +
            "".join(f'<a class="ax-card" href="{h}"><b>{e(l)}</b><span class="ax-small ax-muted">{e(j)}</span></a>'
                    for l, h, j in next(k for k in NAV if k[0] == "settings")[4][2:]) + "</div></section></div>" +
            """<script>(function(){var f=document.getElementById('txf');f.addEventListener('submit',function(ev){ev.preventDefault();var b={};
['effective_from','stcg_pct','ltcg_pct','ltcg_exemption','slab_pct','surcharge_pct','cess_pct'].forEach(function(n){b[n]=f[n].value;});b.addons_enabled=f.addons_enabled.checked;b.stt_deductible=f.stt_deductible.checked;
AX.api('/api/v2/tax/config',{body:b}).then(function(){AX.toast('Tax rates saved. New cycles use them; recompute old ones on Accounting.');}).catch(function(e){document.getElementById('tx-err').textContent=e.message;});});})();</script>""")
    return page("Settings", body, "/settings", root=root, app=app,
                job="Credentials, risk limits, tax rates, notifications and display, grouped in one place.")


def hub(key: str, root="", app=False) -> str:
    sec = next(s for s in NAV if s[0] == key)
    _, label, _, href, kids = sec
    cards = "".join(f'<a class="ax-card" href="{e(h)}"><b>{e(l)}</b><span class="ax-small ax-muted">{e(j)}</span></a>'
                    for l, h, j in kids if h != href or key == "trade")
    extra = ""
    if key == "lab":
        extra = ('<h2 class="ax-h2">Run → compare → promote</h2><div class="ax-flow">'
                 '<a class="ax-card" href="/backtests"><b>Run</b><span class="ax-small ax-muted">Backtest a strategy over your data, '
                 'with the delivery cost model.</span></a>'
                 '<a class="ax-card" href="/backtests#compare"><b>Compare</b><span class="ax-small ax-muted">Put runs side by side: '
                 'return, drawdown, win rate, after costs.</span></a>'
                 '<a class="ax-card" href="/strategies"><b>Promote</b><span class="ax-small ax-muted">Move the winner to paper, then link '
                 'it to a session on the Sessions page.</span></a></div><h2 class="ax-h2">Everything in the lab</h2>')
    jobs = {"markets": "Where the market is and why: India, Asia, global, news and data health.",
            "trade": "Act on signals, size and send trades, and watch what is open.",
            "lab": "Build, test and promote strategies before they trade."}
    return page(label, extra + f'<div class="ax-hub">{cards}</div>', href, root=root, app=app, job=jobs.get(key, ""))


def more(root="", app=False) -> str:
    secs = "".join(f'<section class="ax-card"><h2>{ico} {e(label)}</h2>' + "".join(
        f'<div style="padding:6px 0;border-bottom:1px solid var(--ax-border-soft)"><a href="{e(h)}">{e(l)}</a>'
        f'<div class="ax-small ax-muted">{e(j)}</div></div>' for l, h, j in kids) + "</section>"
                   for key, label, ico, _, kids in NAV if key not in ("home",))
    return page("More", f'<div class="ax-grid">{secs}</div>', "/more", root=root, app=app,
                job="Every part of ATIP. The phone's tabs cover Home, Trade, Sessions and Reports; everything else is here.")


def ops(conn, root="", app=False) -> str:
    runs = _rows(conn, "SELECT job_name, start_time, end_time, status, rows_processed, duration_s, error_msg FROM pipeline_log "
                       "ORDER BY id DESC LIMIT 40")
    body = ('<div class="ax-grid-2"><div class="ax-card"><h2>Liveness and readiness</h2><div id="ops-h"><div class="ax-skel" style="height:80px"></div></div></div>'
            '<div class="ax-card"><h2>Operating status</h2><div id="ops-s"><div class="ax-skel" style="height:80px"></div></div></div></div>'
            '<h2 class="ax-h2">Latest job runs</h2>' +
            K.table("t-runs", [("job_name", "Job", "text"), ("start_time", "Started", "text"), ("status", "Status", "text"),
                               ("rows_processed", "Rows", "int"), ("duration_s", "Seconds", "int"), ("error_msg", "Error", "text")],
                    runs, name="job_runs", empty="No job has run yet.") +
            """<script>(function(){function kv(o){return '<dl class="ax-kv">'+Object.keys(o).filter(function(k){return typeof o[k]!=='object'||o[k]===null;}).map(function(k){return '<dt>'+AX.esc(k.replace(/_/g,' '))+'</dt><dd>'+AX.esc(o[k])+'</dd>';}).join('')+'</dl>';}
Promise.all([fetch('/health').then(function(r){return r.json();}).catch(function(e){return {error:String(e)};}),fetch('/health/ready').then(function(r){return r.json();}).catch(function(e){return {error:String(e)};})])
.then(function(a){document.getElementById('ops-h').innerHTML='<h3 class="ax-h3">/health</h3>'+kv(a[0])+'<h3 class="ax-h3">/health/ready</h3>'+kv(a[1]);});
AX.api('/api/status').then(function(s){document.getElementById('ops-s').innerHTML=kv(s);}).catch(function(e){document.getElementById('ops-s').textContent=e.message;});})();</script>""")
    return page("Ops & health", body, "/ops", root=root, app=app,
                job="Whether ATIP's processes, data and jobs are healthy. The JSON behind it stays at /health.")
