"""
The /assistant page (W36: ML-16): chat with ATIP. Conversations on the left, the thread on the right,
each answer labelled with how it was produced (Claude + the tools it used, or the rule-based fallback)
and its cost. Reads and writes only through /api/assistant/*.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Assistant</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13.5px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}
.main{display:grid;grid-template-columns:240px 1fr;height:calc(100vh - 46px)}
.side{border-right:1px solid var(--line);overflow:auto;padding:8px}.side div{padding:7px 8px;border-radius:6px;cursor:pointer;color:var(--muted);font-size:12.5px}
.side div:hover,.side div.on{background:var(--panel);color:var(--text)}
.chat{display:flex;flex-direction:column;min-width:0}.log{flex:1;overflow:auto;padding:14px 18px}
.q{background:#1d4ed8;color:#fff;padding:8px 12px;border-radius:10px;margin:10px 0 4px auto;max-width:80%;width:fit-content;white-space:pre-wrap}
.a{background:var(--panel);border:1px solid var(--line);padding:10px 12px;border-radius:10px;margin:4px 0 10px;max-width:92%;white-space:pre-wrap;line-height:1.5}
.meta{color:var(--muted);font-size:11px;margin-top:6px}
.bar{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--line);background:var(--panel)}
textarea{flex:1;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px;font-size:13.5px;resize:none;height:54px}
button{background:#2563eb;color:#fff;border:none;border-radius:8px;padding:0 16px;cursor:pointer;font-size:13px}
.hint{color:var(--muted);font-size:12px;padding:6px 18px}.hint span{display:inline-block;border:1px solid var(--line);border-radius:12px;padding:2px 9px;margin:2px;cursor:pointer}
@media(max-width:700px){.main{grid-template-columns:1fr}.side{display:none}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Assistant (ML-16)</span> <span id="st" class="muted" style="margin-left:8px"></span></div>
<div><a href="#" onclick="newChat();return false">+ New chat</a><a href="/">← Dashboard</a></div></div>
<div class="main"><div class="side" id="side"></div>
<div class="chat"><div class="log" id="log"></div>
<div class="hint" id="hints"></div>
<div class="bar"><textarea id="q" placeholder="Ask about a stock, the market, your portfolio… (Enter to send, Shift+Enter for a new line)"></textarea><button id="send" onclick="send()">Ask</button></div></div></div>
<script>
const TOKEN=__TOKEN__;let CID=null;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const HINTS=['What is the market doing today?','Which stocks have the highest crash risk?','How is my portfolio doing?','Show the top 10 BUY signals','Why is TCS rated the way it is?'];
document.getElementById('hints').innerHTML=HINTS.map(h=>`<span onclick="ask('${h.replace(/'/g,"\\'")}')">${esc(h)}</span>`).join('');
function meta(m){if(m.mode==='claude')return `Claude (${esc(m.model||'')}) · tools: ${(m.tools||m.tools_used||[]).map(t=>esc(t.tool)).join(', ')||'none'} · $${Number(m.cost_usd||0).toFixed(4)}`;
  return 'rule-based fallback (AI assistant off or unavailable)'+(m.fallback_reason?` — ${esc(m.fallback_reason)}`:'')}
function add(q,a,m){const l=document.getElementById('log');l.insertAdjacentHTML('beforeend',`<div class="q">${esc(q)}</div><div class="a">${esc(a)}<div class="meta">${meta(m)}</div></div>`);l.scrollTop=l.scrollHeight}
async function status(){try{const s=await j('/api/assistant/status');document.getElementById('st').textContent=s.enabled?`AI on · ${s.model} · today $${s.spent_today_usd} of $${s.daily_cost_cap_usd}`:'AI off (assistant.enabled) — rule-based answers'}catch(e){}}
async function side(){const C=await j('/api/assistant/conversations');document.getElementById('side').innerHTML=C.map(c=>`<div class="${c.conversation_id===CID?'on':''}" onclick="open_('${c.conversation_id}')">${esc(c.title)}<br><span style="font-size:11px">${esc(String(c.updated_at).slice(0,16))} · ${c.messages}</span></div>`).join('')||'<div>No conversations yet</div>'}
async function open_(cid){CID=cid;document.getElementById('log').innerHTML='';const c=await j('/api/assistant/conversations/'+cid);c.messages.forEach(m=>add(m.question,m.answer,m));side()}
function newChat(){CID=null;document.getElementById('log').innerHTML='';side()}
async function ask(q){document.getElementById('q').value=q;send()}
async function send(){const t=document.getElementById('q'),q=t.value.trim();if(!q)return;t.value='';const b=document.getElementById('send');b.disabled=true;b.textContent='…';
  try{const r=await post('/api/assistant/ask',{question:q,conversation_id:CID});CID=r.conversation_id;add(q,r.answer,r);side();status()}
  catch(e){add(q,'Error: '+e.message,{mode:'error'})}finally{b.disabled=false;b.textContent='Ask'}}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
status();side();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
