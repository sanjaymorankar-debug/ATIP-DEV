"""
The 9:00 AM Pre-Open Checklist on screen (owner prompt 22 Nov 2026, PO-CL-09/10): a card on /home (the web dashboard
and both mobile apps, which load /home), the full page /preopen, and its API. Every time shown is IST. Read-only:
nothing here can place or change an order (strategy/preopen_checklist.py is informational and paper-only).

    GET  /preopen                       the page: today's runs, the 20 / 60-day record, the last 30 days, thresholds
    GET  /api/preopen                   {"today", "runs_today", "record", "history", "config", "gift_latest", ...}
    GET  /api/preopen?compact=1         the mobile apps' 09:00 notification: {"trading_day", "ready", "notify": {...}}
    POST /api/preopen/run?label=0900    run a check now (X-ATIP-Token; refused on the view-only server)
"""

from dashboard import ux_kit as K       # no `from __future__ import annotations`: FastAPI must see `Req` itself

e = K.e
TONE = {1: "good", -1: "bad", 0: "", None: ""}
BIAS_TONE = {"Bullish": "good", "Bearish": "bad", "Neutral": ""}
OPEN_TONE = {"Gap-Up": "good", "Gap-Down": "bad", "Flat": "", "Unknown": ""}
RUN_NAME = {"preliminary": "08:15 preliminary", "0900": "09:00", "0909": "09:09 (after matching)"}


def _pct(v, n=2):
    return "—" if v is None else f"{v:+.{n}f}%"


def _vote_chip(v, up="Bullish", down="Bearish", flat="Flat") -> str:
    """A green / red / grey chip that says its meaning in words and a symbol, not by colour alone."""
    if v is None:
        return K.status("– No data", "")
    return K.status({1: f"▲ {up}", -1: f"▼ {down}", 0: f"● {flat}"}[v], TONE[v])


def _time(s) -> str:
    s = str(s or "")
    return s[11:16] if len(s) >= 16 else (s or "—")


def _status_note(r: dict, k: str) -> str:
    st = r.get(f"{k}_status")
    if st in (None, "LIVE"):
        return ""
    word = {"FALLBACK": "fallback", "STALE": "stale — not counted", "UNAVAILABLE": "unavailable"}.get(st, st.lower())
    return f' <span class="ax-small ax-muted">({e(word)})</span>'


def check_rows(r: dict) -> list:
    """[(label, chip html, detail html)] for the four checks of one run."""
    us = (f'Nasdaq {_pct(r.get("nasdaq_chg_pct"))} · Dow {_pct(r.get("dow_chg_pct"))} · '
          f'NQ {_pct(r.get("nq_chg_pct"))} · YM {_pct(r.get("ym_chg_pct"))}')
    us_sub = f'US close {e(r.get("us_close_date") or "—")}' + (
        f' · futures {e(_time(r.get("us_futures_as_of")))} IST' if r.get("us_futures_as_of") else "")
    regime = r.get("vix_regime") or "Unknown"
    vix_chip = K.status(f"{'⚡' if regime == 'Volatile' else '≈' if regime == 'Stable' else '–'} {regime}",
                        "warn" if regime == "Volatile" else "good" if regime == "Stable" else "")
    vix_txt = "—" if r.get("vix") is None else "%.2f" % r["vix"]
    vix = (f'{e(vix_txt)} vs {e(r.get("vix_threshold") or 15)} · '
           f'size ×{e(r.get("size_multiplier") or "—")}, stops ×{e(r.get("stop_multiplier") or "—")} (advice only)')
    ad_basis = r.get("ad_basis") or ""
    ad = ("—" if r.get("advances") is None else
          f'{r["advances"]:,} up / {r["declines"]:,} down (ratio {e(r.get("ad_ratio"))})')
    ad_tag = (K.status("PRIOR-DAY", "warn") if ad_basis == "prior-day" else
              f'<span class="ax-small ax-muted">{e(ad_basis)}</span>' if ad_basis else "")
    gift = ("—" if r.get("gift_price") is None else
            f'{r["gift_price"]:,.1f} · move {_pct(r.get("gift_move_pct"))} '
            f'<span class="ax-small ax-muted">({e(r.get("gift_move_basis") or "")})</span> · '
            f'raw vs Nifty close {_pct(r.get("gift_vs_close_pct"))}')
    return [
        ("US cues", _vote_chip(r.get("us_vote"), "Green", "Red", "Mixed") + _status_note(r, "us"),
         f'{us}<div class="ax-small ax-muted">{us_sub} · {e(r.get("us_source") or "")}</div>'),
        ("India VIX", vix_chip + _status_note(r, "vix"),
         f'{vix}<div class="ax-small ax-muted">{e(r.get("options_note") or "")} · {e(r.get("vix_source") or "")}</div>'),
        ("NSE A/D", _vote_chip(r.get("ad_vote")) + _status_note(r, "ad"),
         f'{ad} {ad_tag}<div class="ax-small ax-muted">{e(r.get("ad_source") or "")}'
         f'{" · " + e(_time(r.get("ad_as_of"))) + " IST" if len(str(r.get("ad_as_of") or "")) >= 16 else ""}</div>'),
        ("GIFT Nifty", _vote_chip(r.get("gift_vote"), "Gap-Up", "Gap-Down", "Flat") + _status_note(r, "gift"),
         f'{gift}<div class="ax-small ax-muted">{e(r.get("gift_source") or "")}'
         f'{" · " + e(_time(r.get("gift_as_of"))) + " IST" if r.get("gift_as_of") else ""}</div>'),
    ]


def _headline(r: dict) -> str:
    bias, regime, opn = r.get("bias") or "Neutral", r.get("vix_regime") or "Unknown", r.get("expected_open") or "Unknown"
    votes = " (%+d)" % int(r.get("vote_sum") or 0)
    return (f'<div class="ax-row" style="gap:8px;flex-wrap:wrap" data-ax-preopen-head>'
            f'<span>Bias {K.status(bias + votes, BIAS_TONE.get(bias, ""))}</span>'
            f'<span>Volatility {K.status(regime, "warn" if regime == "Volatile" else "good" if regime == "Stable" else "")}</span>'
            f'<span>Expected open {K.status(opn, OPEN_TONE.get(opn, ""))}</span></div>')


def _record_line(rec: dict) -> str:
    w = (rec or {}).get("last_20") or {}
    if not w.get("days"):
        return ('<p class="ax-small ax-muted">Track record: no scored day yet — each run is checked against the real '
                'open and close at 15:45 IST.</p>')

    def part(name, h):
        return f'{name} {h["hits"]}/{h["n"]}' + (f' ({h["hit_rate_pct"]:g}%)' if h["n"] else "")
    bits = [part("bias", w["bias"]), part("open call", w["gift_open_3way"]), part("US", w["us"]), part("A/D", w["ad"])]
    return (f'<p class="ax-small">Last {w["days"]} scored days: ' + " · ".join(e(b) for b in bits) +
            (' <span class="ax-muted">(fewer than 20 days: noise)</span>' if w["days"] < 20 else "") + "</p>")


def card_html(conn) -> str:
    """The /home card. Never raises: a problem becomes a line in the card."""
    from strategy import preopen_checklist as PC
    from utils.trading_calendar import is_trading_day
    try:
        today = PC.now_ist().date()
        r = PC.latest_run(conn, today) or PC.latest_run(conn)
        rec = PC.record(conn)
    except Exception as ex:
        return (f'<div class="ax-card" data-widget="preopen"><h2>Pre-Open Checklist</h2><div class="ax-empty">'
                f'Could not read the checklist: {e(ex)}</div></div>')
    if not r:
        msg = ("Runs at 08:15 (preliminary), 09:00 and 09:09 IST on trading days." if is_trading_day(today) else
               "Today is not an NSE trading day; the next run is 08:15 IST on the next one.")
        return (f'<div class="ax-card" data-widget="preopen"><h2>Pre-Open Checklist</h2><div class="ax-empty">'
                f'No run stored yet. {e(msg)}</div></div>')
    old = r["date"] != str(today)
    when = (f'{RUN_NAME.get(r["run_label"], r["run_label"])} · {_time(r.get("fetched_at_ist"))} IST' +
            (f' · {r["date"]}' if old else ""))
    # stacked rows, not a 3-column table: the apps show this card at phone width (375 px)
    rows = "".join(f'<li style="padding:8px 0;border-top:1px solid var(--ax-border);list-style:none" data-ax-check="{e(l)}">'
                   f'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>{e(l)}</b>{chip}</div>'
                   f'<div style="margin-top:4px;overflow-wrap:anywhere">{d}</div></li>' for l, chip, d in check_rows(r))
    cav = r.get("caveats") or []
    cav_html = (f'<details class="ax-more"><summary>{len(cav)} note(s) on the data</summary><ul class="ax-small">' +
                "".join(f"<li>{e(c)}</li>" for c in cav) + "</ul></details>") if cav else ""
    banner = (f'<div class="ax-banner warn" role="status">Last run is from {e(r["date"])} — no run today yet.</div>'
              if old else "")
    return (f'<div class="ax-card" data-widget="preopen"><div class="ax-cardhead"><h2>Pre-Open Checklist</h2>'
            f'<span class="ax-chip" title="When this run fetched its data (IST)">{e(when)}</span></div>{banner}'
            f'{_headline(r)}<ul class="ax-preopen" style="margin:10px 0 4px;padding:0" '
            f'aria-label="The four checks: signal, value, source and time (IST)">{rows}</ul>{_record_line(rec)}{cav_html}'
            f'<p class="ax-small ax-muted">Paper only · informational, not advice · VIX adjusts the advice, it does not '
            f'vote. <a href="/preopen">Details and track record →</a></p></div>')


def _rate(h) -> str:
    if not h or not h.get("n"):
        return "—"
    return f'{h["hits"]}/{h["n"]} ({h["hit_rate_pct"]:g}%)'


def page_html(conn, root: str = "", app: bool = False) -> str:
    from dashboard.shell import page
    from strategy import preopen_checklist as PC
    today = PC.now_ist().date()
    runs = PC.history(conn, 1)
    runs_today = sorted([r for r in runs if r["date"] == str(today)], key=lambda r: -PC.FINAL_ORDER.get(r["run_label"], 9))
    rec = PC.record(conn)
    hist = PC.history(conn, 30)
    cfg = PC.settings()
    body = f'<div class="ax-grid-2">{card_html(conn)}'
    if runs_today:
        head = "".join(f'<th scope="col">{e(RUN_NAME.get(r["run_label"], r["run_label"]))}<div class="ax-small ax-muted">'
                       f'{e(_time(r.get("fetched_at_ist")))} IST</div></th>' for r in runs_today)
        lines = [("Bias", lambda r: K.status("%s (%+d)" % (r.get("bias"), int(r.get("vote_sum") or 0)),
                                             BIAS_TONE.get(r.get("bias"), ""))),
                 ("Volatility", lambda r: e(r.get("vix_regime"))), ("Expected open", lambda r: e(r.get("expected_open"))),
                 ("US vote", lambda r: _vote_chip(r.get("us_vote"), "Green", "Red", "Mixed")),
                 ("A/D", lambda r: f'{_vote_chip(r.get("ad_vote"))} <span class="ax-small">{e(r.get("ad_basis") or "")}</span>'),
                 ("GIFT", lambda r: _vote_chip(r.get("gift_vote"), "Gap-Up", "Gap-Down", "Flat"))]
        body += ('<div class="ax-card"><h2>Today\'s runs</h2><div class="ax-tablewrap"><table class="ax-table"><thead><tr>'
                 f'<th scope="col"></th>{head}</tr></thead><tbody>' + "".join(
                     f'<tr><th scope="row">{e(n)}</th>' + "".join(f"<td>{fn(r)}</td>" for r in runs_today) + "</tr>"
                     for n, fn in lines) + "</tbody></table></div></div>")
    body += "</div>"
    wins = [("Last 20", rec.get("last_20") or {}), ("Last 60", rec.get("last_60") or {}), ("All", rec.get("all") or {})]
    rrows = [("Combined bias → day's close", "bias"), ("Expected open → actual open (3-way)", "gift_open_3way"),
             ("GIFT sign → gap sign", "gift_direction"), ("US cues → day's close", "us"),
             ("NSE A/D → day's close", "ad"), ("NSE A/D, pre-open days only", "ad_preopen_only")]
    body += ('<div class="ax-card"><h2>Track record</h2><p class="ax-small ax-muted">' + e(rec.get("note") or "") +
             ' Flat calls are not scored either. The backtest (docs/reports/2026-11-22-preopen-checklist) found the US '
             'cues and the bias call the gap and the close well, but not the move after 09:15.</p>'
             '<div class="ax-tablewrap"><table class="ax-table"><thead><tr><th scope="col">Check</th>' +
             "".join(f'<th class="n" scope="col">{e(n)} <span class="ax-small ax-muted">({w.get("days", 0)} days)</span></th>'
                     for n, w in wins) + "</tr></thead><tbody>" +
             "".join(f'<tr><th scope="row">{e(lbl)}</th>' + "".join(f'<td class="n">{e(_rate(w.get(k)))}</td>' for _, w in wins)
                     + "</tr>" for lbl, k in rrows))
    vx = (rec.get("last_60") or {}).get("vix") or {}
    body += (f'<tr><th scope="row">VIX regime: mean |day move|</th><td class="n" colspan="3">Volatile '
             f'{e(vx.get("volatile_mean_abs_day_pct") if vx.get("volatile_mean_abs_day_pct") is not None else "—")}% '
             f'({vx.get("volatile_days", 0)} days) · Stable {e(vx.get("stable_mean_abs_day_pct") if vx.get("stable_mean_abs_day_pct") is not None else "—")}% '
             f'({vx.get("stable_days", 0)} days), last 60</td></tr></tbody></table></div></div>')
    hrows = []
    for r in hist:
        hit = ""
        if r.get("day_direction") in ("Up", "Down") and r.get("bias") in ("Bullish", "Bearish"):
            ok = (r["bias"] == "Bullish") == (r["day_direction"] == "Up")
            hit = K.status("✓ right" if ok else "✗ wrong", "good" if ok else "bad")
        hrows.append(f'<tr><td>{e(r["date"])}</td><td>{e(RUN_NAME.get(r["run_label"], r["run_label"]))}</td>'
                     f'<td>{e(r.get("bias"))}</td><td>{e(r.get("vix_regime"))}</td><td>{e(r.get("expected_open"))}</td>'
                     f'<td>{e(r.get("actual_open_class") or "—")} {e(_pct(r.get("actual_gap_pct")) if r.get("actual_gap_pct") is not None else "")}</td>'
                     f'<td>{e(r.get("day_direction") or "—")} {e(_pct(r.get("day_chg_pct")) if r.get("day_chg_pct") is not None else "")}</td>'
                     f'<td>{hit}</td></tr>')
    body += ('<div class="ax-card"><h2>Last 30 days</h2><div class="ax-tablewrap"><table class="ax-table"><thead><tr>'
             '<th scope="col">Date</th><th scope="col">Run (IST)</th><th scope="col">Bias</th><th scope="col">Volatility</th>'
             '<th scope="col">Expected open</th><th scope="col">Actual open</th><th scope="col">Day (close vs prev)</th>'
             '<th scope="col">Bias right?</th></tr></thead><tbody>' +
             ("".join(hrows) or '<tr><td colspan="8"><div class="ax-empty">No runs stored yet.</div></td></tr>') +
             "</tbody></table></div></div>")
    body += ('<div class="ax-card"><h2>Thresholds</h2>' + K.kv([
        ("US leg flat band", f'±{cfg["us_band_pct"]:g}%'), ("India VIX: Volatile above", f'{cfg["vix_threshold"]:g}'),
        ("A/D ratio: +1 above / −1 below", f'{cfg["ad_ratio_bull"]:g} / {cfg["ad_ratio_bear"]:g}'),
        ("GIFT flat band", f'±{cfg["gift_band_pct"]:g}%'), ("GIFT vote basis", e(cfg["gift_vote_basis"])),
        ("Bias needs a vote sum of", f'±{cfg["bias_min_votes"]}'),
        ("Volatile-day advice", f'size ×{cfg["volatile_size_multiplier"]:g}, stops ×{cfg["volatile_stop_multiplier"]:g}')]) +
        '<p class="ax-small ax-muted">Change them in atip_data/config.json "preopen_checklist". Runs: 08:15 preliminary '
        '(inside the pre-market report), 09:00, 09:09 after the auction matches; scored at 15:45; GIFT reference at '
        '15:31. Trading days only.</p></div>')
    return page("Pre-open checklist", body, "/preopen", root=root, app=app,
                job="Before 09:15 IST: are US cues, NSE breadth and GIFT Nifty pointing the same way, how volatile is "
                    "it, and has this read been right?")


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

    @app.get("/preopen", response_class=HTMLResponse)
    async def preopen_page(request: Req):
        c = ctx(request)
        return HTMLResponse(await run_in_threadpool(with_conn, lambda conn: page_html(conn, **c)))

    @app.get("/api/preopen")
    async def preopen_api(compact: int = 0, days: int = 30):
        from strategy import preopen_checklist as PC
        from utils.trading_calendar import is_trading_day

        def build(conn):
            if compact:
                return PC.notify_payload(conn)
            today = PC.now_ist().date()
            runs = [r for r in PC.history(conn, 1) if r["date"] == str(today)]
            return {"date": str(today), "trading_day": is_trading_day(today),
                    "today": PC.latest_run(conn, today), "latest": PC.latest_run(conn), "runs_today": runs,
                    "record": PC.record(conn), "history": PC.history(conn, max(1, min(int(days), 365))),
                    "config": PC.settings(), "gift_latest": PC.latest_gift(conn),
                    "times": "IST", "paper_only": True, "informational_only": True}
        return JSONResponse(json_safe(await run_in_threadpool(with_conn, build)))

    @app.post("/api/preopen/run", dependencies=guard)
    async def preopen_run(label: str = "0900", force: int = 0):
        from ops.role import is_viewer
        if is_viewer():
            return JSONResponse({"error": "the view-only server shows the checklist; it does not run it"}, status_code=403)
        from strategy.preopen_checklist import RUN_LABELS, run
        if label not in RUN_LABELS:
            return JSONResponse({"error": f"label must be one of {list(RUN_LABELS)}"}, status_code=400)
        return JSONResponse(json_safe(await run_in_threadpool(lambda: run(label, force=bool(force)))))
