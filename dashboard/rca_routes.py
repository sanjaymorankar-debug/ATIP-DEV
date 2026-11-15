"""
/rca: the daily post-market RCA (owner prompt 6 Nov 2026). The page draws the stored report (portfolio/post_market_rca.py,
built at 16:00 IST on market days); the apps show it through the shell (More -> Reports -> Post-market RCA). Every time
on it is IST.

    GET  /rca?day=YYYY-MM-DD        the page (the newest report without a day)
    GET  /api/rca[?day=]            the stored report as JSON
    GET  /api/rca/guards            the entry guards (orders/entry_guards.py) and their state
    POST /api/rca/run               build and store today's (or ?day=) report now        trading Mac only
    POST /api/rca/guards            {"name", "value", "ack": true} turn a guard on / off  trading Mac only

The POSTs need the X-ATIP-Token header like every other; the view-only server refuses them (ops/role.py).
"""

import json

from dashboard import ux_kit as K

e = K.e

VERDICT_ORDER = {"BEST": 0, "PROMISING": 1, "SMALL": 2, "WEAK": 3, "MIXED": 4, "FEW": 5, "NOT": 6, "NO": 7, "REJECT": 8}
CAUSE_TONE = {"a": "info", "b": "warn", "c": "warn", "d": "bad", "e": "warn", "f": "bad", "g": "info", "h": "warn"}


def _price_chart(t: dict) -> str:
    """One stock's 5-minute path (IST) with its entry, stop, +target, the low after entry, the recovery and the exit.
    One y-axis in rupees; reference lines dashed; each marker labelled in text, not by colour alone."""
    pts = t.get("chart") or []
    if len(pts) < 2:
        return '<div class="ax-empty">No candles stored for this stock.</div>'
    w, h, pad, padr, top, bot = 500, 230, 62, 112, 14, 26         # 500 wide: the labels stay readable on a phone
    refs = [("Entry", t.get("entry"), "--ax-text-2"), ("Stop", t.get("stop"), "--ax-neg"),
            (f"Trail from", t.get("target_at"), "--ax-pos")]
    vals = [p[1] for p in pts] + [p[2] for p in pts] + [v for _, v, _ in refs if v]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    lo, hi = lo - span * .06, hi + span * .06
    n = len(pts) - 1
    X = lambda i: pad + i * (w - pad - padr) / n
    Y = lambda v: top + (hi - v) / (hi - lo) * (h - top - bot)
    idx = {p[0]: i for i, p in enumerate(pts)}
    def at(hhmm):
        if not hhmm:
            return None
        k = hhmm[:4] + ("0" if hhmm[4] < "5" else "5")          # the 5-minute candle that holds the minute
        return idx.get(k)
    grid = "".join(f'<line class="ax-gridline" x1="{pad}" x2="{w - padr}" y1="{Y(lo + (hi - lo) * j / 4):.1f}" '
                   f'y2="{Y(lo + (hi - lo) * j / 4):.1f}"/><text class="ax-axis" x="{pad - 6}" '
                   f'y="{Y(lo + (hi - lo) * j / 4) + 4:.1f}" text-anchor="end">₹{lo + (hi - lo) * j / 4:,.1f}</text>'
                   for j in range(5))
    band = "".join(f'<rect x="{X(i) - 3:.1f}" y="{Y(p[2]):.1f}" width="6" height="{max(1.0, Y(p[1]) - Y(p[2])):.1f}" '
                   f'rx="2" fill="var(--ax-border)" opacity=".55"/>' for i, p in enumerate(pts))
    line = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(p[3]):.1f}" for i, p in enumerate(pts))
    ref_svg, ends = "", []
    for name, v, var in refs:
        if not v:
            continue
        ref_svg += (f'<line x1="{pad}" x2="{w - padr}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var({var})" '
                    f'stroke-width="1.5" stroke-dasharray="5 4"/>')
        ends.append([Y(v), f"{name} ₹{v:,.2f}"])
    ends.sort()
    for j in range(1, len(ends)):
        if ends[j][0] - ends[j - 1][0] < 13:
            ends[j][0] = ends[j - 1][0] + 13
    ref_lab = "".join(f'<text class="ax-lab" x="{w - padr + 6}" y="{y + 4:.1f}">{e(s)}</text>' for y, s in ends)
    marks, placed = [], []
    for label, hhmm, price, var, below in (("Low", t.get("low_at"), t.get("low"), "--ax-neg", True),
                                           ("Exit", t.get("exit_at"), t.get("exit_price"), "--ax-text", False),
                                           ("Recovery", t.get("recovery_at"), None, "--ax-pos", False),
                                           ("Entry", (t.get("entry_at") or "")[11:16] if not t.get("carried") else None,
                                            t.get("entry"), "--ax-text-2", False)):
        i = at(hhmm)
        if i is None:
            continue
        py = price if price else pts[i][3]
        x, y = X(i), Y(py) + (16 if below else -9)
        for _ in range(4):                     # keep the labels apart: step away from one already placed
            if not any(abs(x - px) < 64 and abs(y - py_) < 12 for px, py_ in placed):
                break
            y += 13 if below else -13
        placed.append((x, y))
        anchor = "start" if x < pad + 40 else "end" if x > w - padr - 40 else "middle"
        marks.append(f'<g><title>{e(label)} {e(hhmm)} IST ₹{py:,.2f}</title><circle cx="{x:.1f}" cy="{Y(py):.1f}" r="5" '
                     f'fill="var({var})" stroke="var(--ax-surface)" stroke-width="2"/><text class="ax-axis" '
                     f'x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}">{e(label)} {e(hhmm)}</text></g>')
    hits = "".join(f'<rect x="{X(i) - (w - pad - padr) / n / 2:.1f}" y="{top}" width="{(w - pad - padr) / n:.1f}" '
                   f'height="{h - top - bot}" fill="transparent"><title>{e(p[0])} IST · close ₹{p[3]:,.2f} · '
                   f'low ₹{p[1]:,.2f} · high ₹{p[2]:,.2f}</title></rect>' for i, p in enumerate(pts))
    xl = "".join(f'<text class="ax-axis" x="{X(i):.1f}" y="{h - 7}" text-anchor="middle">{e(p[0])}</text>'
                 for i, p in enumerate(pts) if p[0] in ("09:15", "11:00", "13:00", "15:00"))
    title = f"{t['symbol']} on {t.get('day', '')}: 5-minute price path, IST"
    return (f'<div class="ax-chart"><svg viewBox="0 0 {w} {h}" role="img" aria-label="{e(title)}" style="width:100%;height:auto">'
            f'<title>{e(title)}</title>{grid}{band}<path d="{line}" fill="none" stroke="var(--ax-series-1)" '
            f'stroke-width="2" stroke-linejoin="round"/>{ref_svg}{ref_lab}{"".join(marks)}{hits}{xl}</svg></div>'
            f'<p class="ax-small ax-muted">Line: 5-minute closes; grey bars: each candle\'s low-high range; dashed: '
            f'entry, the 2 % stop and the +3 % trail start.</p>')


def _guard_row(g: dict, app: bool) -> str:
    state = K.status(f"ON {g['value']}", "good") if g["on"] else K.status("off")
    if g["on"]:                                # the apps show the same switch (parity); each one asks first
        btn = f'<button class="ax-btn sm" type="button" data-guard="{e(g["name"])}" data-on="0">Turn off</button>'
    else:
        btn = (f'<button class="ax-btn sm" type="button" data-guard="{e(g["name"])}" data-on="1" '
               f'data-value="{e(g["suggested"])}" data-label="{e(g["label"])}">Turn on…</button>')
    return (f'<tr><td>{e(g["label"])}<div class="ax-small ax-muted">{e(g["name"])}</div></td><td>{state}</td>'
            f'<td class="n">{e(g["suggested"])}</td><td>{btn}</td></tr>')


def page_html(conn, day: str | None = None, root: str = "", app: bool = False) -> str:
    from dashboard.shell import page
    from orders import entry_guards as G
    from portfolio import post_market_rca as R
    rep = R.latest(conn, day)
    days = R.days(conn)
    pick = ('<form class="ax-field" method="get" action="/rca" style="display:flex;gap:8px;align-items:end">'
            '<div><label for="rca-day">Day</label><select class="ax-select" id="rca-day" name="day">' + "".join(
                f'<option{" selected" if rep and d == rep["day"] else ""}>{e(d)}</option>' for d in days) +
            '</select></div><button class="ax-btn sm" type="submit">Show</button></form>') if days else ""
    actions = '<button class="ax-btn sm" type="button" id="rca-run" style="align-self:flex-end">Rebuild now</button>'
    if not rep:
        body = ('<div class="ax-card"><div class="ax-empty">No post-market RCA stored yet. It is built at 16:00 IST on '
                'market days; "Rebuild now" builds today\'s.</div></div>')
        return page("Post-market RCA", body + _js(), "/rca", root=root, app=app, actions=actions,
                    job="Why each losing trade lost and which rule would have helped (IST)")
    tot, mk = rep["totals"], rep["market"]
    losses = [t for t in rep["trades"] if t["losing"] and t["pnl_net"] is not None]
    kpi = lambda lbl, val, sub, tone="": (f'<div class="ax-kpi" data-tone="{tone}" role="group" aria-label="{e(lbl)}">'
                                          f'<div class="ax-lbl">{e(lbl)}</div><div class="ax-val">{val}</div>'
                                          f'<div class="ax-sub2">{sub}</div></div>')
    strip = ('<div class="ax-kpis" aria-label="The day at a glance">' +
             kpi("Realised net", K.money(tot["net"], True), f'{tot["closed"]} closed · {tot["wins"]} won · {tot["losses"]} lost',
                 "bad" if tot["net"] < 0 else "good") +
             kpi("Day P&L (marked)", K.money(tot["day_mtm"], True), "open positions at the close, today's charges") +
             kpi("Charges", K.money(tot["charges"]), "on the trades closed today") +
             kpi("Nifty 50", K.pct(mk.get("nifty_chg_pct")), f'low {mk.get("nifty_low") or 0:,.2f} at {e(mk.get("nifty_low_at"))} IST') +
             "</div>")
    head = (f'<div class="ax-card"><h2>Summary</h2><p>{e(rep["headline"])}</p><p class="ax-small ax-muted">'
            f'{e(rep["env_note"])} Candles: {e(rep["candles"])}. Built {e(rep["built_at"])}.</p></div>')
    sess = K.table("rca-sess", [("session", "Session", "text"), ("name", "Name", "text"), ("bought", "Bought", "int"),
                                ("closed", "Closed", "int"), ("wins", "Won", "int"), ("losses", "Lost", "int"),
                                ("win_rate_pct", "Win rate", "rate"), ("gross", "Gross", "pnl"), ("charges", "Charges", "money"),
                                ("net", "Net", "pnl"), ("net_pct", "Net %", "pct"), ("day_mtm", "Day P&L", "pnl"),
                                ("max_dd", "Max DD", "money"), ("max_dd_pct", "Max DD %", "rate"), ("max_dd_at", "DD at (IST)", "text")],
                   rep["sessions"], totals=[{"session": "All", **tot}], name=f"rca-sessions-{rep['day']}",
                   caption="Realised = trades sold today (whole trade, charges in). Day P&L = today's move of every position "
                           "(from the previous close, or the fill if bought today), today's charges. Max DD = the deepest fall "
                           "of the session's day P&L from its high.")
    path_rows = [dict(t, sym=f'{t["session"]} {t["symbol"]}', entry_when=(t["entry_at"] or "")[5:16],
                      sl_first=("Yes" if t.get("stopped_before_recovery") else "No" if t["exit_reason"] == "Stop-loss" else "—"),
                      exit_s=(f'{t["exit_reason"]} {t["exit_at"] or ""} ₹{t["exit_price"]:,.2f}' if t["exit_price"] else t["exit_reason"]),
                      sig=f'{t.get("signal") or "—"} {t.get("score") or ""} {t.get("source") or ""}'.strip(),
                      back=(f'{t["back_to_entry_at"]} (+{t["back_to_entry_min"]} min)' if t.get("back_to_entry_at") else "did not recover by 15:30"))
                 for t in rep["trades"]]
    path = K.table("rca-path", [("sym", "Session · stock", "text"), ("entry_when", "Entry (IST)", "text"), ("entry", "Entry ₹", "money"),
                                ("sig", "Signal", "text"), ("prev_close", "Prev close", "money"), ("day_open", "Open", "money"),
                                ("gap_pct", "Gap", "pct"), ("low", "Low after entry", "money"), ("low_at", "Low at", "text"),
                                ("low_dd_pct", "Drawdown", "pct"), ("high", "High after entry", "money"), ("high_at", "High at", "text"),
                                ("recovery_at", "Recovery start", "text"), ("back", "Back to entry", "text"),
                                ("close", "Close", "money"), ("exit_s", "Exit", "text"), ("sl_first", "Stopped before recovery", "text")],
                   path_rows, name=f"rca-path-{rep['day']}",
                   caption=f'Recovery start: {rep["rules_used"]["recovery"]}. Back to entry: {rep["rules_used"]["back_to_entry"]}. '
                           "A position bought on an earlier day is measured from today's open.")
    cards = []
    for t in rep["trades"]:
        if not t["losing"]:
            continue
        money = (f'Realised {K.money(t["pnl_net"], True)}' if t["pnl_net"] is not None
                 else f'Open, marked {K.money(t.get("mtm_close"), True)} at the close')
        causes = "".join(f'<li><span class="ax-st {CAUSE_TONE.get(c["code"], "")}">{e(c["code"])}) {e(c["cause"])}</span> '
                         f'{e(c["evidence"])}</li>' for c in t["causes"]) or "<li>No cause found by the rules.</li>"
        notes = "".join(f'<li class="ax-muted">{e(x)}</li>' for x in (t.get("market_note"), t.get("news_note")) if x)
        cards.append(f'<section class="ax-card"><h2>{e(t["session"])} · {e(t["symbol"])} <span class="ax-small ax-muted">'
                     f'{e(t.get("industry") or "")}</span></h2><p>{money}</p>{_price_chart(dict(t, day=rep["day"]))}'
                     f'<ul class="ax-small">{causes}{notes}</ul></section>')
    causes = K.table("rca-causes", [("cause", "Root cause", "text"), ("rupees", "₹ of losses", "pnl"), ("trades_s", "Trades", "text")],
                     [dict(c, trades_s=", ".join(c["trades"])) for c in rep["causes_ranked"]], name=f"rca-causes-{rep['day']}",
                     caption="₹ of the losing trades each cause was found in (a trade can carry several); execution counts only "
                             "the fill under the stop.", tools=False)
    wi = K.table("rca-whatif", [("label", "Alternative rule", "text"), ("pnl", "Day P&L with it", "pnl"), ("delta", "vs today", "pnl"),
                                ("study_avg_pct", "42-session avg / trade", "pct"), ("study_days", "Days better / worse", "text"),
                                ("verdict", "Verdict", "text")],
                 [dict(r, study_days=(f'{r["study_better"]} / {r["study_worse"]}' if r.get("study_better") is not None else "—"))
                  for r in sorted(rep["rules_ranked"], key=lambda r: (VERDICT_ORDER.get(str(r.get("verdict")).split(":")[0].split(" ")[0], 9),
                                                                       -(r.get("delta") or 0)))],
                 name=f"rca-whatif-{rep['day']}",
                 caption=f'Ranked by the study\'s verdict, then by today\'s ₹. Today: {rep["rules_used"]["what_if"]}. Study: '
                         f'{rep.get("study", {}).get("when", "")}, today\'s rules '
                         f'{rep.get("study", {}).get("base_avg_pct", 0):+.2f} % a trade. A rule that helped today and lost in the '
                         f'study is not a fix.')
    guards = "".join(_guard_row(g, app) for g in G.status(conn))
    gcard = (f'<section class="ax-card"><h2>Proposed guards (all off until you turn them on)</h2>'
             f'<div class="ax-tablewrap"><table class="ax-table"><thead><tr><th>Guard</th><th>State</th><th class="n">Suggested</th>'
             f'<th></th></tr></thead><tbody>{guards}</tbody></table></div><p class="ax-small ax-muted">Turning one on changes '
             f'what auto-pick buys from its next pass. Each asks first and is logged. Command line: '
             f'<code>python -m orders.entry_guards</code>.</p></section>')
    body = (strip + head + f'<section class="ax-card"><h2>Sessions</h2>{sess}</section>' +
            f'<section class="ax-card"><h2>Intraday path of every stock held</h2>{path}</section>' +
            f'<section class="ax-card"><h2>Root causes, ranked by ₹</h2>{causes}</section>' +
            f'<div class="ax-grid" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,560px),1fr))">'
            f'{"".join(cards)}</div>' +
            f'<section class="ax-card"><h2>What would have avoided or reduced the losses</h2>{wi}</section>' + gcard + _js())
    return page(f"Post-market RCA · {rep['day']}", body, "/rca", root=root, app=app, actions=pick + actions,
                job="Why each losing trade lost and which rule would have helped. All times IST.")


def _js() -> str:
    return """<script>(function(){var b=document.getElementById('rca-run');if(b)b.addEventListener('click',function(){
b.disabled=true;AX.api('/api/rca/run',{body:{}}).then(function(){location.reload();}).catch(function(x){alert(x.message);}).finally(function(){b.disabled=false;});});
document.querySelectorAll('[data-guard]').forEach(function(x){x.addEventListener('click',function(){var on=x.getAttribute('data-on')==='1',
n=x.getAttribute('data-guard'),v=x.getAttribute('data-value');var go=on?AX.ask({title:'Turn on this guard?',html:'<p>'+AX.esc(x.getAttribute('data-label'))+
' at <b>'+AX.esc(v)+'</b>.</p><p>From auto-pick\\'s next pass it changes which stocks the sessions buy (PAPER). It was back-tested over 42 sessions; one day is not evidence.</p>',ok:'Turn on',cancel:'Keep off',tone:'warn'}):Promise.resolve(true);
go.then(function(yes){if(!yes)return;AX.api('/api/rca/guards',{body:{name:n,value:on?v:'off',ack:on}}).then(function(){location.reload();}).catch(function(err){alert(err.message);});});});});})();</script>"""


def register(app, guard, Req, get_connection, json_safe):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import HTMLResponse, JSONResponse

    def ctx(request):
        ua = request.headers.get("user-agent", "")
        return {"root": request.scope.get("root_path") or "",
                "app": "ATIPApp/" in ua or request.query_params.get("app") == "1"}

    def with_conn(fn):
        conn = get_connection()
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.get("/rca", response_class=HTMLResponse)
    async def rca_page(request: Req, day: str | None = None):
        c = ctx(request)
        return HTMLResponse(await run_in_threadpool(with_conn, lambda conn: page_html(conn, day, **c)))

    @app.get("/api/rca")
    async def rca_api(day: str | None = None):
        from portfolio.post_market_rca import latest
        rep = await run_in_threadpool(with_conn, lambda conn: latest(conn, day))
        return JSONResponse(json_safe(rep) if rep else {"error": "no report stored for that day"}, status_code=200 if rep else 404)

    @app.get("/api/rca/guards")
    async def rca_guards():
        from orders.entry_guards import status
        return JSONResponse(json_safe({"guards": await run_in_threadpool(with_conn, status)}))

    @app.post("/api/rca/run", dependencies=guard)
    async def rca_run(day: str | None = None):
        from ops.role import is_viewer
        if is_viewer():
            return JSONResponse({"error": "the view-only server shows the Mac's reports; it does not build them"}, status_code=403)
        from portfolio.post_market_rca import run
        return JSONResponse(json_safe(await run_in_threadpool(lambda: run(day))))

    @app.post("/api/rca/guards", dependencies=guard)
    async def rca_set_guard(request: Req):
        from orders.entry_guards import set_guard
        try:
            b = await request.json()
        except Exception:
            b = {}
        try:
            out = await run_in_threadpool(with_conn, lambda conn: set_guard(
                conn, str(b.get("name") or ""), b.get("value"), ack=bool(b.get("ack")), reason="/rca page"))
        except PermissionError as ex:             # turning a guard on without ack
            return JSONResponse({"error": str(ex)}, status_code=409)
        except (ValueError, TypeError) as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        except RuntimeError as ex:                # ops/role.py ViewerRefused
            return JSONResponse({"error": str(ex)}, status_code=403)
        except Exception as ex:
            return JSONResponse({"error": str(ex)}, status_code=500)
        return JSONResponse(json_safe(out))
