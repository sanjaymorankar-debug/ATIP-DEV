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
# UI fixes v2 (3 Nov 2026): every money figure says its period (H1), the three answers share one card pattern in
# one row from 900 px (H2, H3), alerts are unescaped once, clamped, de-duplicated and grouped (H4), idle sessions
# fold into one row (H5), jobs have names and one row each (H6), scores are whole numbers (H7), India VIX colours
# the other way round (H9). Numbers go through dashboard/fmt.py.

ALERT_STOP = {"ATIP", "VWAP", "IST", "NSE", "BSE", "BUY", "SELL", "HOLD", "WAIT", "LONG", "SHORT", "LIVE", "PAPER", "ATR",
              "RSI", "MACD", "EMA", "SMA", "OK", "FII", "DII", "ETF", "GIFT", "NIFTY", "SENSEX", "FAILED", "SUCCESS", "DHAN",
              "API", "PNL", "EOD", "LTP", "CMP", "UTC", "AM", "PM", "SL", "TP", "OHLC", "BANKNIFTY", "VIX", "SEBI", "STT",
              "GST", "HTML", "URL", "ID", "N"}


def _ist(ts, today: str | None = None) -> str:
    """'13:50 IST' today, '2 Nov 13:50 IST' another day, '' when missing."""
    s = str(ts or "").replace("T", " ")
    if len(s) < 16:
        return e(s[:10]) if s else ""
    d, t = s[:10], s[11:16]
    if d == (today or date.today().isoformat()):
        return f"{t} IST"
    try:
        dd = date.fromisoformat(d)
        return f"{dd.day} {dd.strftime('%b')} {t} IST"
    except ValueError:
        return f"{d} {t} IST"


def _symbols_known(conn) -> set:
    return {r["symbol"] for r in _rows(conn, "SELECT DISTINCT symbol FROM ai_scores WHERE date = "
                                             "(SELECT MAX(date) FROM ai_scores)")}


def clean_alert(title, message) -> str:
    """H4: the alert's text once: tags dropped, HTML entities decoded ONCE (the page escapes it once on the way out),
    whitespace collapsed, and the title not repeated at the start of the body."""
    import html as _html
    t = re.sub(r"<[^>]+>", " ", str(message or ""))
    for _ in range(2):                                   # a message stored escaped twice (&amp;#x27;) still reads right
        u = _html.unescape(t)
        if u == t:
            break
        t = u
    t = re.sub(r"\s+", " ", t).strip()
    ti = re.sub(r"\s+", " ", _html.unescape(str(title or ""))).strip()
    if ti and t.lower().startswith(ti.lower()):
        t = t[len(ti):].lstrip(" :—-–·|\n").strip()
        t = t[:1].upper() + t[1:]
    return _job_words(t)


def _job_words(text: str) -> str:
    """A job's internal name inside a sentence reads as its plain name ("Job failed: technical_signals" ->
    "Job failed: Technical signals")."""
    from dashboard.labels import JOB_NAMES
    return re.sub(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", lambda m: JOB_NAMES.get(m.group(0), m.group(0)), text or "")


def alert_symbols(text: str, known: set) -> list:
    out = []
    for tok in re.findall(r"\b[A-Z][A-Z0-9&-]{2,19}\b", text or ""):
        if tok in out or tok in ALERT_STOP:
            continue
        if tok in known or (len(tok) >= 4 and re.search(r"[A-Z]{3}", tok)):
            out.append(tok)
    return out[:8]


def group_alerts(rows: list, known: set, limit: int = 6) -> list:
    """Alerts of the same kind (category + title) as one group, newest first: {"title", "severity", "count",
    "latest", "items": [{"text", "at"}], "symbols"}."""
    groups, order = {}, []
    sev_rank = {"critical": 3, "error": 3, "warning": 2, "warn": 2, "info": 1}
    for a in rows:
        title = _job_words(clean_alert("", a.get("title"))) or str(a.get("category") or "Alert").replace("_", " ").title()
        k = (str(a.get("category") or ""), title)
        if k not in groups:
            groups[k] = {"title": title, "category": a.get("category"), "severity": a.get("severity") or "info",
                         "count": 0, "latest": a.get("created_at"), "items": [], "symbols": []}
            order.append(k)
        g = groups[k]
        text = clean_alert(title, a.get("message"))
        g["count"] += 1
        g["items"].append({"text": text, "at": a.get("created_at")})
        if sev_rank.get(str(a.get("severity")), 0) > sev_rank.get(str(g["severity"]), 0):
            g["severity"] = a.get("severity")
        for s in alert_symbols(text, known):
            if s not in g["symbols"]:
                g["symbols"].append(s)
    return [groups[k] for k in order][:limit]


def latest_jobs(rows: list, limit: int = 6) -> list:
    """H6: one row per job with its latest run (rows newest first)."""
    from dashboard.labels import job_label
    seen, out = set(), []
    for p in rows:
        n = p.get("job_name")
        if not n or n in seen:
            continue
        seen.add(n)
        out.append({**p, "label": job_label(n)})
        if len(out) >= limit:
            break
    return out


def freshness(conn, now=None) -> list:
    """G8: each EOD dataset's latest date against the trading day it should have reached by now ("stale" when
    behind): ATIP scores after post-market (16:45 IST), technical signals after their 20:30 run."""
    from datetime import datetime, time as dtime, timedelta
    try:
        from utils.trading_calendar import is_trading_day
    except Exception:
        is_trading_day = lambda d: d.weekday() < 5            # noqa: E731
    now = now or datetime.now()

    def expected(run_at: dtime):
        d = now.date() if now.time() >= run_at else now.date() - timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        return d
    out = []
    for key, label, sql, run_at in (
            ("scores", "ATIP scores", "SELECT MAX(date) AS d FROM ai_scores", dtime(18, 30)),
            ("technical", "Technical signals", "SELECT MAX(date) AS d FROM technical_signal", dtime(21, 0)),
            ("market", "Market health", "SELECT MAX(date) AS d FROM market_health", dtime(18, 30))):
        r = _rows(conn, sql)
        last = str((r[0] if r else {}).get("d") or "")[:10] or None
        exp = expected(run_at)
        stale = bool(last) and last < exp.isoformat()
        out.append({"key": key, "label": label, "date": last, "expected": exp.isoformat(), "stale": stale,
                    "missing": not last})
    return out


def freshness_chips(items: list) -> str:
    def nice(d):
        try:
            x = date.fromisoformat(d)
            return f"{x.day} {x.strftime('%b')}"
        except (TypeError, ValueError):
            return "—"
    return '<div class="ax-fresh" aria-label="How fresh the data is">' + "".join(
        f'<span class="ax-chip {"warn" if i["stale"] or i["missing"] else ""}" title="'
        f'{e(i["label"])}: latest {e(i["date"] or "none")}, expected {e(i["expected"])}">'
        f'{e(i["label"])} · as of {e(nice(i["date"]))}{" · stale" if i["stale"] else " · none yet" if i["missing"] else ""}</span>'
        for i in items) + "</div>"


def home_data(conn) -> dict:
    from accounting import cycles as C
    from accounting.periods import periods
    from dashboard.command_routes import _feed
    from dashboard.shell import state
    st = state(conn)
    mo = _market_open()
    ov = C.overview(conn)
    today = date.today().isoformat()
    t = C.totals([c for c in C.stored(conn) if str(c.get("closed_at") or "")[:10] == today])
    known = _symbols_known(conn)
    out = {"state": st, "market_open": mo, "feed": _feed(conn, mo), "sessions": ov, "today": t,
           "periods": periods(conn, ov),
           "market": (_rows(conn, "SELECT date, mh_score, regime, nifty_close, vix_level FROM market_health "
                                  "ORDER BY date DESC LIMIT 1") or [{}])[0],
           "index": (_rows(conn, "SELECT date, time, nifty50, nifty50_chg, banknifty, banknifty_chg, india_vix, "
                                 "india_vix_chg, gift_nifty FROM index_levels ORDER BY date DESC, time DESC LIMIT 1")
                     or [{}])[0],
           "gift_last": (_rows(conn, "SELECT date, time, gift_nifty FROM index_levels WHERE gift_nifty > 0 "
                                     "ORDER BY date DESC, time DESC LIMIT 1") or [None])[0],
           "tod": (_rows(conn, "SELECT pred_date, symbol, `signal`, atip_score, entry_price, stop_loss, target_1, reasoning "
                               "FROM predictions WHERE is_tod = 1 ORDER BY pred_date DESC LIMIT 1") or [None])[0],
           "alerts": _rows(conn, "SELECT created_at, category, severity, title, message FROM alert_log "
                                 "ORDER BY id DESC LIMIT 6"),
           "alert_groups": group_alerts(_rows(conn, "SELECT created_at, category, severity, title, message FROM alert_log "
                                                    "ORDER BY id DESC LIMIT 40"), known),
           "pipeline": _rows(conn, "SELECT job_name, start_time, status, duration_s, error_msg FROM pipeline_log "
                                   "WHERE kind = 'run' OR kind IS NULL ORDER BY id DESC LIMIT 6"),
           "jobs": latest_jobs(_rows(conn, "SELECT job_name, start_time, status, duration_s, error_msg FROM pipeline_log "
                                           "WHERE kind = 'run' OR kind IS NULL ORDER BY id DESC LIMIT 80")),
           "freshness": freshness(conn),
           "pending": (_rows(conn, "SELECT COUNT(*) AS n FROM order_rules WHERE status = 'PENDING_CONFIRMATION'")
                       or [{"n": 0}])[0]["n"],
           "scores_date": (_rows(conn, "SELECT MAX(date) AS d FROM ai_scores") or [{"d": None}])[0]["d"]}
    try:
        from ops.dhan_token import status as token_status
        out["dhan_token"] = token_status()
    except Exception:
        out["dhan_token"] = None
    try:                                     # owner item 14 (16 Nov 2026): FY STCG tax after set-off, not per cycle
        from accounting.ledger import fy_tax_estimate
        out["fy_tax"] = fy_tax_estimate(conn)
    except Exception:
        out["fy_tax"] = None
    return out


def _fy_tax_line(ft: dict | None) -> str:
    """Owner item 14 (16 Nov 2026): the FY STCG estimate after set-off beside the per-cycle reserve, with its
    assumption and the CA line. '' when there is nothing to show."""
    from accounting import tax as TX
    from dashboard import fmt as F
    est = (ft or {}).get("by_env", {}).get((ft or {}).get("main")) if ft else None
    if not est or not est.get("cycles"):
        return ""
    env = "paper" if est.get("env") == "VIRTUAL" else "broker"
    return (f'<p class="ax-small" data-ax-fytax><b>{e(est["fy"])} {e(TX.FY_LABEL.lower())}</b> ({env}): '
            f'{e(F.fmt_inr(est["estimate"]))} on net STCG {F.inr_html(est["net_stcg"], True)} '
            f'({est["cycles"]} cycles, losses set off, STT added back, {est["effective_pct"]:g}%) · '
            f'{e(TX.RESERVE_LABEL.lower())}: {e(F.fmt_inr(est["per_cycle_reserve"]))} · '
            f'<span class="ax-muted" title="{e(est["assumption"])}">{e(TX.FY_NOTE)}</span> '
            f'<a href="/accounting?tab=tax">Tax →</a></p>')


def _fy_estimate_card(ft: dict | None) -> str:
    """Accounting › Tax (owner item 14, 16 Nov 2026): the FY STCG estimate after set-off per env, beside the per-cycle
    reserve, with the assumption and the CA line."""
    from accounting import tax as TX
    if not ft:
        return ""
    out = []
    for env, est in (ft.get("by_env") or {}).items():
        out.append(f'<div class="ax-card" style="margin-top:12px" data-ax-fytax="{e(env)}"><h2>{e(est["fy"])} · '
                   f'{e(TX.FY_LABEL)} · {e("paper" if env == "VIRTUAL" else "broker")}</h2>' + K.kv([
                       ("STCG cycles (winning)", f'{est["cycles"]} ({est["winning_cycles"]})'),
                       ("Net P&L after charges", K.money(est["net_pnl"], True)),
                       ("+ STT added back (not deductible)", K.money(est["stt_added_back"])),
                       ("Gains − losses set off", f'{K.money(est["gains"])} − {K.money(est["losses"])}'),
                       ("Net STCG (taxable when positive)", K.money(est["net_stcg"], True)),
                       (f'Tax {est["rate_pct"]:g}% + cess {est["cess_pct"]:g}% ({est["effective_pct"]:g}%)',
                        f'{K.money(est["tax"])} + {K.money(est["cess"])}' +
                        (f' + surcharge {K.money(est["surcharge"])}' if est["surcharge"] else "")),
                       ("FY estimate", f'<b>{K.money(est["estimate"])}</b>'),
                       (TX.RESERVE_LABEL, K.money(est["per_cycle_reserve"])),
                       ("Reserve − FY estimate", K.money(est["reserve_over_estimate"], True))]) +
                   f'<p class="ax-small ax-muted">{e(est["assumption"])}</p>'
                   f'<p class="ax-small"><b>{e(TX.FY_NOTE)}</b></p></div>')
    return "".join(out)


def _answer(q: str, word: str, value: str, sub: str, tone: str, sym: str) -> str:
    """One of the three answers: icon + word + (value) + one line, with the coloured left border of its tone
    (good / flat / warn / bad). The same pattern for all three cards (H3)."""
    return (f'<section class="ax-answer" data-tone="{tone}" aria-label="{e(q)}"><div class="ax-q">{e(q)}</div>'
            f'<div class="ax-a"><span class="ax-sym" aria-hidden="true">{sym}</span><span class="ax-word">{e(word)}</span>'
            f'{value}</div><div class="ax-small ax-muted">{sub}</div></section>')


def _pnl_word(v) -> tuple:
    from dashboard import fmt as F
    t = F.tone(v)
    return {"up": ("Up", "good", "▲"), "down": ("Down", "bad", "▼")}.get(t, ("Flat", "flat", "—"))


def _session_rows(ov: dict, per: dict) -> str:
    """H5: active sessions first; sessions with money but no trade yet fold into one expandable row."""
    from dashboard import fmt as F
    active, idle = [], []
    for s in ov["sessions"]:
        ti = s.get("tiles")
        if not ti:
            continue
        p = per.get(s["id"]) or {}
        has = bool(ti.get("cycles_done") or s.get("open") or abs(float(ti.get("actual_pnl") or 0)) > 0.004
                   or abs(float(ti.get("unrealised") or 0)) > 0.004)
        (active if has else idle).append((s, p))

    def cells(s, p):
        out = []
        for key in ("today", "since_start"):
            b = p.get(key) or {}
            hide = ' hidden' if key != "today" else ""
            out.append(f'<td class="n ax-p-{key}"{hide} data-v="{b.get("realised", 0)}">{F.inr_html(b.get("realised"), True)}</td>'
                       f'<td class="n ax-p-{key}"{hide} data-v="{b.get("open", 0)}">{F.inr_html(b.get("open"), True)}</td>')
        sp = K.spark(s.get("spark") or [], s["id"] + " actual equity since start")
        trend = sp or ('<span class="ax-muted" title="No closed cycle yet: the trend starts with the first sale">—</span>')
        return "".join(out) + f"<td>{trend}</td>"
    rows = [f'<tr><th scope="row"><a href="/sessions/{e(s["id"])}">{e(s["id"])}</a> <span class="ax-small">{e(s["name"])}</span></th>'
            f'{cells(s, p)}</tr>' for s, p in active]
    if idle:
        ids = [s["id"] for s, _ in idle]
        nums = [int(i[1:]) for i in ids if i[1:].isdigit()]
        label = (f"S{nums[0]}–S{nums[-1]}" if len(nums) == len(ids) and len(ids) > 2 and nums == list(range(nums[0], nums[-1] + 1))
                 else ", ".join(ids))
        rows.append(f'<tr class="ax-idle-sum"><td colspan="4"><button class="ax-linkbtn" type="button" data-ax-idle aria-expanded="false">'
                    f'{e(label)} · no trades yet ({len(ids)})</button></td></tr>')
        rows += [f'<tr class="ax-idle-row" hidden><th scope="row"><a href="/sessions/{e(s["id"])}">{e(s["id"])}</a> '
                 f'<span class="ax-small">{e(s["name"])}</span></th>{cells(s, p)}</tr>' for s, p in idle]
    return "".join(rows)


def _home_kpis(conn, d: dict, idx: dict, mk: dict, td: dict) -> tuple[str, str]:
    """The KPI strip (3 Nov 2026 brief: market health, Nifty 50, sessions filled, today's P&L, freshness) and the compact
    ten-session grid under the three answers. Every tone has its word or arrow too, never colour alone."""
    from dashboard import fmt as F
    mh = F.num(mk.get("mh_score"))
    mh_tone = "" if mh is None else "good" if mh >= 60 else "bad" if mh < 40 else "warn"
    n50, n50c = F.num(idx.get("nifty50")), F.num(idx.get("nifty50_chg"))
    n_tone = "" if n50c is None else F.tone(n50c).replace("up", "good").replace("down", "bad").replace("flat", "")
    try:
        from orders.session_board import board
        b = board(conn)
    except Exception as ex:                                   # the strip still draws
        b = {"filled": None, "target": 10, "message": f"board unavailable: {ex}", "rows": [], "detail": ""}
    c = b.get("counts") or {}
    # green only when every session really holds or has queued its stock; ten planned picks are information, not done
    f_tone = ("" if b.get("filled") is None else "warn" if b["filled"] < b["target"]
              else "good" if (c.get("holding", 0) + c.get("queued", 0)) >= b["target"] else "info")
    word, tone, sym = _pnl_word(td["total"])
    fd = d["feed"]
    upd = fd.get("updated")
    kpi = lambda lbl, val, sub, t: (f'<div class="ax-kpi" data-tone="{t}" role="group" aria-label="{e(lbl)}"><div class="ax-lbl">{e(lbl)}</div>'
                                   f'<div class="ax-val">{val}</div><div class="ax-sub2">{sub}</div></div>')
    strip = ('<div class="ax-kpis" aria-label="Today at a glance">' +
             kpi("Market health", F.score_html(mk.get("mh_score"), "No market-health score yet"),
                 e((mk.get("regime") or "—").replace("_", " ").title()), mh_tone) +
             kpi("Nifty 50", f'<span class="ax-num">{e(F.fmt_num(idx.get("nifty50")))}</span>' if n50 else F.missing_html("No Nifty 50 level yet"),
                 F.pct_html(idx.get("nifty50_chg")), n_tone) +
             kpi("Sessions filled", f'<span class="ax-num">{b["filled"] if b.get("filled") is not None else "—"} of {b["target"]}</span>',
                 e(b.get("detail") or b.get("message") or ""), f_tone) +
             kpi("Today's P&L", f'<span class="ax-amt">{F.inr_html(td["total"], True)}</span>',
                 f'{sym} {e(word)} · realised {F.inr_html(td["realised"], True)}', tone if tone != "flat" else "") +
             kpi("Data freshness", f'<span data-ax-updated data-state="{"stale" if fd.get("stale") else "fresh"}">'
                                   f'{e("Updated " + upd) if upd else "Updated —"}</span>',
                 e(fd.get("text") or ""), "bad" if fd.get("stale") else "info") + "</div>")
    grid = ""
    if b.get("rows"):
        grid = (f'<div class="ax-card" data-widget="ten"><div class="ax-cardhead"><h2>{_n_sessions(b)}</h2>'
                f'<a class="ax-small" href="/sessions">All details →</a></div>'
                f'<p class="ax-small ax-muted" role="status">{e(b["message"])}</p>{_board_cards(b, compact=True)}</div>')
    return strip, grid


def home(conn, root="", app=False) -> str:
    from dashboard import fmt as F
    try:
        d = home_data(conn)
    except Exception as ex:
        return page("Today", f'<div class="ax-banner bad" role="alert">The home page could not read ATIP\'s data: '
                             f'{e(ex)}. The other pages may still work.</div>', "/home", root=root, app=app)
    st, ov, pr = d["state"], d["sessions"], d["periods"]
    today_iso = date.today().isoformat()
    # 1. safe?
    problems = []
    if st.get("halted"):
        problems.append(f"kill switch ON ({e(st.get('halt_reason') or 'no reason given')})")
    fd = d["feed"]
    if fd.get("stale"):
        problems.append(e(fd.get("text") or "live prices are stale"))
    tok = d.get("dhan_token") or {}
    if tok.get("message") and tok.get("severity") in ("error", "bad", "critical", "warn"):
        problems.append("Dhan token needs attention")
    env = st.get("env", "PAPER")
    if env == "LIVE":
        problems.append("LIVE: real money")
    feed_html = f'<span data-ax-feed data-state="{e(fd.get("state"))}">{e(fd.get("text"))}</span>'
    safe = _answer("Is ATIP safe?", "Yes" if not problems else ("Halted" if st.get("halted") else "Check"), "",
                   (f"{e(env)} · {feed_html} · kill switch off" if not problems else
                    "; ".join([p for p in problems if p != e(fd.get("text"))] + [feed_html])),
                   "good" if not problems else "bad" if st.get("halted") or env == "LIVE" else "warn",
                   "✓" if not problems else "!")
    # 2. running?
    run_n = st.get("sessions_running", 0)
    if st.get("status") == "STOPPED":
        running = _answer("Is it running?", "Stopped", "", f"{e(st.get('banner'))} · read-only, no orders", "warn", "⏸")
    else:
        running = _answer("Is it running?", "Yes" if run_n else "Idle", "",
                          f"{run_n} of {st.get('sessions_total') or 10} sessions running · auto-send {'on' if st.get('auto_send') else 'off'} · "
                          f"data {_ist(st.get('last_updated'), today_iso) or '—'}", "good" if run_n else "warn",
                          "●" if run_n else "○")
    # 3. making money today? (H1: today = realised today + open now; every figure says its period)
    td, ss = pr["today"], pr["since_start"]
    word, tone, sym = _pnl_word(td["total"])
    money = _answer("Making money today?", word,
                    f'<span class="ax-amt">{F.inr_html(td["total"], True)}</span>',
                    f'<b>Today</b>: realised {F.inr_html(td["realised"], True)} + open {F.inr_html(td["open"], True)} · '
                    f'<b>Since start</b> {F.inr_html(ss["total"], True)}', tone, sym)
    banners = _closed_banner()
    fr = d["freshness"]
    if any(i["stale"] for i in fr if i["key"] == "scores") and d["scores_date"]:
        try:
            age = (date.today() - date.fromisoformat(str(d["scores_date"])[:10])).days
            banners += (f'<div class="ax-banner warn" role="status">Stale data: the latest ATIP scores are from '
                        f'{e(d["scores_date"])} ({age} days ago).</div>')
        except ValueError:
            pass
    idx, mk = d["index"], d["market"]
    # PO-CL-08 (22 Nov 2026): GIFT from the pre-open checklist's source. index_levels.gift_nifty is Dhan's IDX_I 5024
    # quote, which is frozen (one value all day), so it is no longer shown as the live GIFT Nifty.
    try:
        from strategy.preopen_checklist import latest_gift
        gl = latest_gift(conn)
    except Exception:
        gl = None
    if gl and F.num(gl.get("price")):
        mv = gl.get("move_pct")
        gift = (f'<span class="ax-num">{e(F.fmt_num(gl["price"]))}</span>'
                + (f' {F.pct_html(mv)}' if mv is not None else "")
                + f'<span class="ax-small ax-muted"> ({e(_ist(gl.get("as_of"), today_iso) or "—")}'
                + (", stale" if gl.get("status") == "STALE" else "") + ")</span>")
    else:
        gift = F.missing_html("No live GIFT Nifty quote yet (Dhan's GIFT feed is frozen; the pre-open checklist "
                              "reads NSE from 08:15 IST)")
    w_market = (f'<div class="ax-card" data-widget="market"><h2>Market <span class="ax-chip" title="When these figures were read">'
                f'as of {e(_ist(str(idx.get("date") or mk.get("date") or "") + " " + str(idx.get("time") or "00:00"), today_iso) or "—")}</span></h2>' + K.kv([
                    ("Nifty 50", f'<span class="ax-num">{e(F.fmt_num(idx.get("nifty50")))}</span> {F.pct_html(idx.get("nifty50_chg"))}'),
                    ("India VIX", f'<span class="ax-num">{e(F.fmt_num(idx.get("india_vix")))}</span> '
                                  f'{F.pct_html(idx.get("india_vix_chg"), inverse=True)}'
                                  '<span class="ax-small ax-muted"> (rising = more fear)</span>'),
                    ("GIFT Nifty", gift),
                    ("Market health", f'{F.score_html(mk.get("mh_score"), "No market-health score yet")} {e(mk.get("regime") or "")}')]) +
                '<a href="/market">Markets →</a></div>')
    tod = d["tod"]
    # owner decision 3 Nov 2026: a Trade of the Day only on a BUY signal
    no_tod = (f'<div class="ax-empty">No BUY signal for today{" (" + e(tod["pred_date"]) + ")" if tod else ""}. '
              'ATIP names a Trade of the Day only from a BUY signal.</div>')
    w_tod = ('<div class="ax-card" data-widget="tod"><h2>Trade of the Day</h2>' + (K.kv([
        ("Stock", f'<a class="ax-symchip" href="/stock/{e(tod["symbol"])}">{e(tod["symbol"])}</a> '
                  f'{K.status(tod.get("signal") or "", "good" if tod.get("signal") == "BUY" else "")}'),
        ("ATIP score", F.score_html(tod.get("atip_score"))),
        ("Entry · stop · target", f'<span class="ax-num">{e(F.fmt_price(tod.get("entry_price")))} · {e(F.fmt_price(tod.get("stop_loss")))}'
                                  f' · {e(F.fmt_price(tod.get("target_1")))}</span>'),
        ("Signal from", e(f'the close of {tod["pred_date"]} (EOD)'))]) +
        f'<div class="ax-row" style="margin-top:8px"><button class="ax-btn sm primary" type="button" data-ax-trade="BUY" '
        f'data-sym="{e(tod["symbol"])}" data-entry="{e(tod.get("entry_price") or "")}" data-stop="{e(tod.get("stop_loss") or "")}" '
        f'data-target="{e(tod.get("target_1") or "")}">Buy {e(tod["symbol"])}</button>'
        f'<a class="ax-btn sm" href="/stock/{e(tod["symbol"])}">Chart</a></div>'
        if tod and tod.get("signal") == "BUY" else no_tod) + "</div>")
    per = pr["sessions"]
    srows = _session_rows(ov, per)
    toggle = ('<div class="ax-seg" role="group" aria-label="Period"><button type="button" data-ax-period="today" aria-pressed="true">'
              'Today</button><button type="button" data-ax-period="since_start" aria-pressed="false">Since start</button></div>')
    th = ('<thead><tr><th scope="col">Session</th>'
          '<th class="n ax-p-today" scope="col">Today realised</th><th class="n ax-p-today" scope="col">Open now</th>'
          '<th class="n ax-p-since_start" scope="col" hidden>Since start realised</th><th class="n ax-p-since_start" scope="col" hidden>Open now</th>'
          '<th data-nosort scope="col">Trend</th></tr></thead>')
    foot = (f'<p class="ax-small ax-muted ax-p-today">All sessions · <b>Today</b>: realised {F.inr_html(td["realised"], True)} '
            f'(gross {F.inr_html(td["gross"], True)} − charges {e(F.fmt_inr(td["charges"]))} − tax reserve (per-cycle, cash set aside) {e(F.fmt_inr(td["tax_est"]))}) '
            f'· open {F.inr_html(td["open"], True)} · {td["cycles"]} cycle(s) closed today</p>'
            f'<p class="ax-small ax-muted ax-p-since_start" hidden>All sessions · <b>Since start</b>: realised {F.inr_html(ss["realised"], True)} '
            f'(gross {F.inr_html(ss["gross"], True)}) · open {F.inr_html(ss["open"], True)} · {ss["cycles"]} cycle(s) · '
            f'{ov["total"]["waiting"]} waiting below their minimum</p>')
    w_sessions = (f'<div class="ax-card" data-widget="sessions" data-period="today"><div class="ax-cardhead"><h2>Sessions</h2>{toggle}</div>' + (
        f'<div class="ax-tablewrap" style="max-height:none"><table class="ax-table ax-home-sessions">{th}<tbody>{srows}</tbody></table></div>'
        if srows else '<div class="ax-empty">No session has money yet. <a href="/sessions">Give one a starting amount</a>.</div>') +
        foot + _fy_tax_line(d.get("fy_tax")) +
        '<p class="ax-small ax-muted">Realised = after charges and the per-cycle tax reserve (cash set aside, not the '
        'tax due — the FY estimate above sets losses off against gains). <a href="/accounting?tab=pnl">Reconcile in Accounting →</a></p></div>')
    sev = {"critical": "bad", "error": "bad", "warning": "warn", "warn": "warn"}
    sev_word = {"critical": "Critical", "error": "Error", "warning": "Warning", "warn": "Warning", "info": "Info"}

    def chips(syms):
        return "".join(f'<a class="ax-symchip" href="/stock/{e(s)}" aria-label="Open {e(s)}">{e(s)}</a>' for s in syms)

    def agroup(g):
        first, rest = g["items"][0], g["items"][1:]
        more = ("<details class='ax-more'><summary>" + str(len(rest)) + " earlier</summary>" + "".join(
            f'<div class="ax-clamp" data-ax-clamp>{e(i["text"])} <span class="ax-muted">{e(_ist(i["at"], today_iso))}</span></div>'
            for i in rest) + "</details>") if rest else ""
        count = f' <span class="ax-muted">· {g["count"]}</span>' if g["count"] > 1 else ""
        syms = f'<div class="ax-chips">{chips(g["symbols"])}</div>' if g["symbols"] else ""
        return (f'<div class="ax-alert"><div class="ax-alert-h">{K.status(sev_word.get(str(g["severity"]), "Info"), sev.get(str(g["severity"]), "info"))} '
                f'<b>{e(g["title"])}</b>{count}'
                f'<span class="ax-spacer"></span><span class="ax-small ax-muted">{e(_ist(g["latest"], today_iso))}</span></div>'
                f'<div class="ax-clamp" data-ax-clamp>{e(first["text"])}</div>'
                f'{syms}{more}</div>')
    w_alerts = ('<div class="ax-card" data-widget="alerts"><h2>Alerts</h2>' + ("".join(agroup(g) for g in d["alert_groups"])
                or '<div class="ax-empty">No alerts. ATIP raises one when a job fails, a signal fires or a limit is near.</div>') + "</div>")
    stw = {"SUCCESS": ("Done", "good"), "OK": ("Done", "good"), "DONE": ("Done", "good"), "FAILED": ("Failed", "bad"),
           "SKIPPED": ("Skipped", ""), "EMPTY": ("Nothing new", "warn"), "RUNNING": ("Running", "info"), "HELD": ("Held", "warn")}
    w_pipe = ('<div class="ax-card" data-widget="pipeline"><h2>Pipeline</h2>' + ("".join(
        f'<div class="ax-small ax-jobrow" title="{e(p["job_name"])}{(" — " + e(p["error_msg"])) if p.get("error_msg") else ""}">'
        f'{K.status(*stw.get(str(p.get("status")).upper(), (str(p.get("status") or "?").title(), "")))}'
        f'<span style="flex:1">{e(p["label"])}</span><span class="ax-muted ax-num">{e(_ist(p.get("start_time"), today_iso))}</span></div>'
        for p in d["jobs"]) or '<div class="ax-empty">No job has run yet: the first runs at 06:00 IST.</div>') +
        freshness_chips(fr) + '<a href="/ops">Ops &amp; health →</a></div>')
    w_todo = ('<div class="ax-card" data-widget="todo"><h2>Needs you</h2>' + K.kv([
        ("Orders waiting for your tap", f'<a href="/command">{d["pending"]}</a>'),
        ("Sessions waiting (below minimum)", f'<a href="/sessions">{ov["total"]["waiting"]}</a>'),
        ("Reconciliation", '<a href="/accounting?tab=recon">check charges and cash</a>')]) + "</div>")
    try:                                         # 22 Nov 2026 (PO-CL-09): the 9:00 AM Pre-Open Checklist card
        from dashboard.preopen_routes import card_html as _preopen_card
        w_preopen = _preopen_card(conn)
    except Exception as ex:
        w_preopen = (f'<div class="ax-card" data-widget="preopen"><h2>Pre-Open Checklist</h2><div class="ax-empty">'
                     f'Unavailable: {e(ex)}</div></div>')
    widgets = [("preopen", "Pre-Open Checklist", w_preopen),
               ("market", "Market", w_market), ("tod", "Trade of the Day", w_tod), ("sessions", "Sessions", w_sessions),
               ("todo", "Needs you", w_todo), ("alerts", "Alerts", w_alerts), ("pipeline", "Pipeline", w_pipe)]
    custom = ('<details class="ax-help" id="ax-custom"><summary>Customise this page</summary><p class="ax-small ax-muted">'
              'Choose the widgets you want here. This is remembered in this browser only.</p><div class="ax-choice">' +
              "".join(f'<label><input type="checkbox" data-w="{k}" checked><span>{e(l)}</span></label>' for k, l, _ in widgets)
              + "</div></details>")
    figures = {"today": td, "since_start": ss, "month": pr["month"]}
    kpis, grid = _home_kpis(conn, d, idx, mk, td)
    try:                                                              # KGF-09 (16 Nov 2026): the regime gate, per arm
        from dashboard.arms_routes import gate_banner
        banners += gate_banner(conn)
    except Exception:
        pass
    body = (banners + kpis + f'<div class="ax-five ax-answers" data-ax-figures="{e(_json(figures))}">{safe}{running}{money}</div>' +
            grid + '<div class="ax-grid-2" id="ax-widgets">' + "".join(w for _, _, w in widgets) + "</div>" + custom +
            """<script>(function(){var k='atip-home-hidden',h=[];try{h=JSON.parse(localStorage.getItem(k)||'[]')}catch(e){}
function apply(){document.querySelectorAll('[data-widget]').forEach(function(w){w.hidden=h.indexOf(w.getAttribute('data-widget'))>=0;});
document.querySelectorAll('#ax-custom [data-w]').forEach(function(c){c.checked=h.indexOf(c.getAttribute('data-w'))<0;});}
document.querySelectorAll('#ax-custom [data-w]').forEach(function(c){c.addEventListener('change',function(){var w=c.getAttribute('data-w');
h=h.filter(function(x){return x!==w});if(!c.checked)h.push(w);try{localStorage.setItem(k,JSON.stringify(h))}catch(e){}apply();});});apply();
var card=document.querySelector('[data-widget=sessions]');function period(p){if(!card)return;card.setAttribute('data-period',p);
card.querySelectorAll('[data-ax-period]').forEach(function(b){b.setAttribute('aria-pressed',b.getAttribute('data-ax-period')===p?'true':'false');});
card.querySelectorAll('.ax-p-today,.ax-p-since_start').forEach(function(el){el.hidden=!el.classList.contains('ax-p-'+p);});
try{localStorage.setItem('atip-home-period',p)}catch(e){}}
if(card){card.querySelectorAll('[data-ax-period]').forEach(function(b){b.addEventListener('click',function(){period(b.getAttribute('data-ax-period'));});});
var pp=null;try{pp=localStorage.getItem('atip-home-period')}catch(e){}if(pp==='since_start')period(pp);
var ib=card.querySelector('[data-ax-idle]');if(ib)ib.addEventListener('click',function(){var open=ib.getAttribute('aria-expanded')!=='true';
ib.setAttribute('aria-expanded',open?'true':'false');card.querySelectorAll('.ax-idle-row').forEach(function(r){r.hidden=!open;});});}})();</script>""")
    return page("Today", body, "/home", job="Is ATIP safe, running and making money today? The three answers first; "
                "everything else below.", root=root, app=app,
                actions='<a class="ax-btn" href="/sessions">Sessions</a><a class="ax-btn primary" href="/command">Command Center</a>')


# ── Sessions ─────────────────────────────────────────────────────────────────────────────────

SETUP_DRAWER = """
<aside class="ax-drawer" id="ax-setup" aria-hidden="true" inert aria-labelledby="ax-setup-h">
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
   <label><input type="radio" name="compound_basis" value="AFTER_TAX_RESERVE"><span>After setting aside the per-cycle tax reserve</span></label></div></div>
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


def _engine_meter(done: int, cap: int) -> str:
    """Completed trades out of the session's maximum (500), as a meter."""
    pct = min(100.0, done / cap * 100) if cap else 0.0
    return (f'<div class="ax-progress" role="meter" aria-valuemin="0" aria-valuemax="{cap}" aria-valuenow="{done}" '
            f'aria-label="{done} of {cap} completed trades"><span style="width:{pct:.1f}%"></span></div>'
            f'<div class="ax-progress-l"><span>0</span><span>{done} of {cap} trades</span><span>{cap}</span></div>')


def _engine_line(g: dict | None) -> str:
    """The session engine's part of a card (18 Nov 2026): state, balance against the seed, growth, the 500-trade
    meter, win rate and legs."""
    if not g:
        return ""
    tone = {"ACTIVE": "good", "CREATED": "info", "COMPLETED": "info"}.get(g["state"], "warn")
    wr = f'{g["win_rate_pct"]:.1f}%' if g.get("win_rate_pct") is not None else "—"
    seed = (f'seed {K.money(g["seed_capital"])}' + (f' + {K.money(g["transfers"])} moved in' if g.get("transfers") else ""))
    return (f'<div class="ax-engine" data-state="{e(g["state"])}">'
            f'<div class="ax-row" style="justify-content:space-between;gap:8px"><span>{K.status(g["state"], tone)}</span>'
            f'<span class="ax-small">Balance <b>{K.money(g["current_balance"])}</b> · {seed} · '
            f'<b>{K.pct(g["growth_pct"])}</b></span></div>' + _engine_meter(g["completed_trades"], g["max_completed_trades"]) +
            f'<div class="ax-small ax-muted">Win rate {wr} · {g["wins"]} won, {g["losses"]} lost · {g["legs"]} legs · '
            f'pauses under {K.money(g["floor"])}</div></div>')


def _engine_panel(conn) -> tuple[str, dict]:
    """The session engine's summary row (18 Nov 2026) and its per-session figures for the cards."""
    try:
        from orders import session_engine as SE
        ov = SE.overview(conn)
    except Exception as ex:                                  # the page must still draw
        return f'<div class="ax-banner warn ax-small">Session engine unavailable: {e(ex)}</div>', {}
    sm = ov["summary"]
    states = " · ".join(f"{n} {k.lower()}" for k, n in sorted(sm["by_state"].items()))
    fits = sm.get("fits_new_sessions") or 0
    tiles = '<div class="ax-tiles">' + "".join([
        K.tile("Sessions", f'<span class="ax-num">{sm["sessions"]}</span>', e(states)),
        K.tile("Combined balance", K.money(sm["combined_balance"]), f'capital {K.money(sm["capital"])}'),
        K.tile("Combined P&L", K.money(sm["combined_pnl"], True),
               f'realised {K.money(sm["realised_pnl"], True)} · open {K.money(sm["unrealised_pnl"], True)}', True),
        K.tile("Reserve", K.money(sm["reserve"]),
               (f'buffer {K.money(sm["buffer"])} · ' if sm.get("buffer") else "") +
               (f'room for {fits} more ₹25K session{"s" if fits != 1 else ""}' if fits else "below one ₹25K seed"))]) + "</div>"
    btn = ('<button class="ax-btn" type="button" onclick="document.getElementById(\'ax-engine-new\').showModal()">'
           'Split reserve into ₹25K sessions…</button>') if fits else ""
    dlg = ('<dialog class="ax-dialog" id="ax-engine-new" aria-labelledby="ax-engine-new-h"><form id="ax-engine-form">'
           '<div class="ax-dhead" id="ax-engine-new-h">New ₹25,000 sessions from the reserve</div><div class="ax-dbody">'
           f'<p class="ax-small">The reserve fits <b>{fits}</b> session(s) of ₹25,000 (PAPER). Each gets its own wallet and '
           'starts CREATED: it trades nothing until you start it.</p>'
           f'<div class="ax-field"><label for="en-n">How many</label><input class="ax-input" id="en-n" name="n" '
           f'inputmode="numeric" value="{fits}"></div>'
           '<label class="ax-check"><input type="checkbox" name="ack"> <span>I understand this moves paper cash from the '
           'reserve into the new sessions</span></label><div class="ax-err" id="en-err"></div></div><div class="ax-dfoot">'
           '<button class="ax-btn" type="button" data-ax-dclose>Cancel</button><button class="ax-btn primary" type="submit">'
           'Create sessions</button></div></form></dialog>'
           """<script>(function(){var f=document.getElementById('ax-engine-form');if(!f)return;f.addEventListener('submit',function(ev){
ev.preventDefault();var er=document.getElementById('en-err');er.textContent='';if(!f.ack.checked){er.textContent='Tick the box first.';return;}
AX.api('/api/v2/engine/create',{body:{n:parseInt(f.n.value,10),ack:true}}).then(function(r){AX.toast('Created '+r.created.join(', ')+'.');
setTimeout(function(){location.reload();},900);}).catch(function(e){er.textContent=e.message;});});})();</script>""") if fits else ""
    head = ('<h2 class="ax-h2">₹25K session engine</h2><p class="ax-small ax-muted">Each session trades only with its own '
            'balance; profits stay in it. A session stops after 500 completed trades, and pauses (with an alert) if its '
            f'balance falls under {ov["settings"]["floor_pct"]:g}% of its capital. Times are IST.</p>')
    return head + tiles + (f'<div class="ax-row">{btn}</div>' if btn else "") + dlg, {g["session_id"]: g for g in ov["sessions"]}


def _scard(s: dict, g: dict | None = None) -> str:
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
            f'<div><div class="ax-lbl">Actual P&amp;L <span title="after charges and the per-cycle tax reserve (cash set aside, not the tax due)">ⓘ</span></div>'
            f'<div class="ax-val">{K.money(ti["actual_pnl"], True)}</div></div></div>'
            f'<div class="ax-small ax-muted">Equity {K.money(ti["equity"])} · started {K.money(ti["start_capital"])} · '
            f'cycles {ti["cycles_done"]}/{ti["max_cycles"]} · floor {K.money(ti["floor"])}</div>'
            f'{_engine_line(g)}{K.spark(s["spark"], s["id"] + " actual equity")}{opens}{waiting}'
            f'<span class="ax-proposed" data-proposed="{e(s["id"])}" hidden></span>'
            f'<div class="ax-row"><button class="ax-btn sm" type="button" aria-label="Set up {e(s["id"])}" onclick="axSetup(\'{e(s["id"])}\')">Setup</button>'
            f'<a class="ax-btn sm" href="/sessions/{e(s["id"])}" aria-label="{e(s["id"])} cycles and P&amp;L">Cycles &amp; P&amp;L</a></div></div>')


_BOARD_STATUS = {"HOLDING": ("holding", "good", "● Holding"), "QUEUED": ("queued", "info", "◷ Queued"),
                 "AWAITING CONFIRMATION": ("confirm", "warn", "! Needs your tap"), "WAITING": ("waiting", "", "○ Waiting")}


def _board_status(st: str) -> tuple:
    """(data-status, chip tone, chip text with its glyph): colour is never the only cue."""
    if str(st).startswith("NEXT PICK"):
        return "next", "info", "→ Next pick" + (" · after the stop window" if "after" in str(st) else "")
    return _BOARD_STATUS.get(st, ("waiting", "", st))


def _n_sessions(b: dict) -> str:
    """The board's heading: "The ten sessions", or "The 11 sessions" once one is made from spare paper cash (16 Nov)."""
    n = b.get("target") or len(b.get("rows") or [])
    return "The ten sessions" if n == 10 else f"The {n} sessions"


def _board_cards(b: dict, compact: bool = False) -> str:
    """The ten sessions as a card grid (3 Nov 2026 brief): status chip, rank, why, score badges, entry / target / stop,
    P&L and wins. compact = the Home page's version: symbol, chip, rank and P&L only."""
    from dashboard import score_glossary as G
    cards = []
    for r in b["rows"]:
        ds, tone, chip = _board_status(r["status"])
        rank = (f'<span class="ax-rank" title="ATIP rank on {e(b.get("rank_day") or "the latest scored day")}, '
                f'of {r["rank_of"]} scored (ATIP high first, CRI↓ low first among equals)">#{r["rank"]}'
                f'<span class="ax-muted">/{r["rank_of"]}</span></span>') if r.get("rank") else ""
        sym = (f'<a class="ax-symchip" href="/stock/{e(r["symbol"])}">{e(r["symbol"])}</a>' if r.get("symbol")
               else '<span class="ax-muted">no stock</span>')
        src = f'<span class="ax-small ax-muted">{e(r["source"])}</span>' if r.get("source") and r.get("symbol") else ""
        pnl = K.money(r["pnl"], True) if r.get("pnl") is not None else "—"
        wins = f'{r.get("successful_trades") or 0}/{r.get("closed_trades") or 0}'
        # the compact card keeps the chip to its first words ("→ Next pick"); the rest is the chip's tooltip
        short = chip.split(" · ")[0] if compact else chip
        head = (f'<div class="ax-tc-h"><a class="ax-tc-id" href="/sessions/{e(r["session"])}">{e(r["session"])}'
                f'{"" if compact else " " + e(r.get("name") or "")}</a>'
                f'<span title="{e(chip)}">{K.status(short, tone)}</span></div>')
        if compact:
            cards.append(f'<li class="ax-tcard" data-status="{ds}">{head}<div class="ax-tc-sym">{sym}{rank}</div>'
                         f'<div class="ax-tc-foot"><span>P&amp;L <b>{pnl}</b></span><span title="winning closed trades / closed trades">'
                         f'Wins <b>{wins}</b></span></div></li>')
            continue
        lv = "".join(f'<div><dt>{k}</dt><dd>{K.money(r[f]) if r.get(f) else "—"}</dd></div>'
                     for k, f in (("Entry", "entry"), ("Target", "target"), ("Stop", "stoploss")))
        why = e(r.get("rationale") or r.get("reason") or "")
        cards.append(f'<li class="ax-tcard" data-status="{ds}">{head}<div class="ax-tc-sym">{sym}{rank}{src}</div>'
                     f'<div class="ax-tc-why">{why}</div>{G.badges(r.get("score_snapshot")) if r.get("symbol") else ""}'
                     f'<dl class="ax-tc-lv">{lv}</dl><div class="ax-tc-foot"><span>P&amp;L <b>{pnl}</b></span>'
                     f'<span title="winning closed trades / closed trades">Wins <b>{wins}</b></span></div></li>')
    return f'<ul class="ax-sgrid{" compact" if compact else ""}" aria-label="{_n_sessions(b)}">{"".join(cards)}</ul>'


def _board_html(conn) -> str:
    """SES-09: the ten sessions -- always ten -- under a "N of 10 sessions filled" line: a card grid, with the same rows
    as a sortable table one click away."""
    try:
        from orders.session_board import board
        b = board(conn)
    except Exception as ex:                                  # the page must still draw
        return f'<div class="ax-banner warn ax-small">Ten-session board unavailable: {e(ex)}</div>'
    cls = "ok" if b["complete"] else "warn"
    head = ("Session", "Symbol", "Rank", "Why this stock", "Scores (CRI↓ lower is better)", "Entry", "Target", "Stop", "Status", "P&L", "Wins")
    trs = []
    for r in b["rows"]:
        sn = r.get("score_snapshot") or {}
        sc = (f'ATIP {sn["atip"]:.0f} · VPI {sn["vpi"]:.0f} · MRI {sn["mri"]:.0f} · RRI {sn["rri"]:.0f} · ZPI {sn["zpi"]:.0f} · '
              f'CRI↓ {sn["cri"]:.0f} · ACS {sn["acs"]:.0f} · {e(sn["signal"])}') if sn and None not in (
                  sn["atip"], sn["vpi"], sn["mri"], sn["rri"], sn["zpi"], sn["cri"], sn["acs"]) else "—"
        trs.append("<tr>" + "".join(f'<td data-label="{e(h)}">{c}</td>' for h, c in zip(head, (
            f'<span><a href="/sessions/{e(r["session"])}">{e(r["session"])}</a> {e(r["name"])}</span>',
            f'<b>{e(r["symbol"])}</b>' if r.get("symbol") else "—", f'#{r["rank"]}' if r.get("rank") else "—",
            e(r.get("rationale") or ""), sc,
            K.money(r["entry"]) if r.get("entry") else "—", K.money(r["target"]) if r.get("target") else "—",
            K.money(r["stoploss"]) if r.get("stoploss") else "—", e(r["status"]),
            K.money(r["pnl"], True) if r.get("pnl") is not None else "—",
            f'{r.get("successful_trades") or 0}/{r.get("closed_trades") or 0}'))) + "</tr>")
    return (f'<h2 class="ax-h2">{_n_sessions(b)}</h2><div class="ax-banner {cls} ax-small" role="status">{e(b["message"])}</div>'
            f'<p class="ax-boarddetail">{e(b.get("detail") or "")}'
            f'{" · ranks from " + e(b["rank_day"]) if b.get("rank_day") else ""}</p>' + _board_cards(b) +
            '<details class="ax-help"><summary>Table view</summary>'
            '<style>@media (max-width:700px){.ax-boardwrap{max-height:none!important;overflow:visible!important}'
            '.ax-board{min-width:0!important;width:100%!important}.ax-board thead{display:none}'
            '.ax-board tbody,.ax-board tr{display:block;width:100%}.ax-board tr{border-bottom:1px solid var(--ax-line,#ddd);'
            'padding:8px 0}.ax-board td{display:flex;justify-content:space-between;gap:12px;border:0;padding:2px 0;'
            'white-space:normal!important;text-align:right;min-width:0;overflow-wrap:anywhere}.ax-board td::before{'
            'content:attr(data-label);font-weight:600;text-align:left;flex:0 0 36%}}</style>'
            f'<div class="ax-tablewrap ax-boardwrap"><table class="ax-table ax-board"><thead><tr>' + "".join(f"<th>{e(h)}</th>" for h in head) +
            '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></div></details>'
            '<p class="ax-small ax-muted">Wins = closed trades that made money / closed trades. Rank = ATIP rank on the latest '
            'scored day. CRI↓ is crash risk: lower is better. Hover a score for its formula.</p>')


def sessions(conn, root="", app=False) -> str:
    from accounting import cycles as C
    ov = C.overview(conn)
    t = ov["total"]
    tiles = '<div class="ax-tiles">' + "".join([
        K.tile("Starting amounts", K.money(t["start_capital"]),
               f'{"+" if t.get("transfers", 0) > 0 else "−"}{K.money(abs(t["transfers"]))} moved in · capital {K.money(t["capital"])}'
               if t.get("transfers") else ""), K.tile("Equity now", K.money(t["equity"])),
        K.tile("Gross P&L", K.money(t["gross_pnl"], True), "before any cost", True),
        K.tile("Charges", K.money(t["charges"])), K.tile("Tax reserve (per-cycle)", K.money(t["tax_est"]), "cash set aside, not tax due"),
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
                'session\'s actual equity (after charges and the per-cycle tax reserve), on its own scale.</p>'
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
    from dashboard.arms_routes import gate_banner                    # KGF-09 (16 Nov 2026): the regime gate, per arm
    new_btn, new_dlg = _new_session(conn, ov["sessions"])
    cap_btn, cap_dlg = _capital_plan(conn)
    new_btn, new_dlg = new_btn + cap_btn, new_dlg + cap_dlg
    eng_html, eng = _engine_panel(conn)                              # 18 Nov 2026: the Rs 25K session engine
    body = (_closed_banner() + gate_banner(conn) + eng_html + tiles + _board_html(conn) + '<h2 class="ax-h2">Sessions</h2><div class="ax-sessions">' +
            "".join(_scard(s, eng.get(s["id"])) for s in ov["sessions"]) + "</div>" + mult + help_ + SETUP_DRAWER + split + new_dlg +
            f'<div id="ax-sdata" hidden data-json="{e(json.dumps(data, default=str))}"></div>' + SETUP_JS)
    return page("Sessions", body, "/sessions", root=root, app=app,
                job="Independent mini-funds. Set each one's money and rules, and see what it really earned: gross "
                    "P&L before any cost, actual P&L after charges and the per-cycle tax reserve.",
                actions='<button class="ax-btn" type="button" onclick="document.getElementById(\'ax-split\').showModal()">'
                        'Split a total…</button>' + new_btn + '<a class="ax-btn" href="/reports">Session reports</a>',
                mar=[("Manage", "/sessions", True), ("Accounting", "/accounting?tab=session", False),
                     ("Reports", "/reports", False)])


def _new_session(conn, sessions: list) -> tuple:
    """The "New paper session" button and dialog (owner 16 Nov 2026): a session S11.. out of the PAPER cash outside the
    sessions (orders/sessions.py add_session). Nothing on a LIVE book."""
    from orders import sessions as S
    try:
        if not S._paper():
            return "", ""
        spare = S.spare_paper_cash(conn)["spare"]
    except Exception:
        return "", ""
    if spare < 1:
        return ('<button class="ax-btn" type="button" disabled title="No paper cash is outside the sessions">'
                'New paper session…</button>'), ""
    btn = ('<button class="ax-btn" type="button" onclick="document.getElementById(\'ax-new\').showModal()">'
           'New paper session…</button>')
    opts = "".join(f'<option value="{e(s["id"])}"{" selected" if s["id"] == S.NEW_SESSION_LIKE else ""}>'
                   f'{e(s["id"])} {e(s["name"])}</option>' for s in sessions)
    dlg = ('<dialog class="ax-dialog" id="ax-new" aria-labelledby="ax-new-h"><form novalidate><div class="ax-dhead" '
           'id="ax-new-h">New paper session</div><div class="ax-dbody">'
           f'<p class="ax-small ax-muted">{K.money(spare)} of paper cash is outside the sessions. A new session gets its own '
           'pool out of it and compounds on its own. PAPER only; the other sessions are not touched.</p>'
           '<div class="ax-field"><label for="ns-name">Name</label><input class="ax-input" id="ns-name" name="name" '
           'placeholder="Control (spare cash)" maxlength="80"></div>'
           '<div class="ax-field"><label for="ns-cap">Starting amount (₹)</label><input class="ax-input" id="ns-cap" '
           f'name="capital" inputmode="decimal" value="{int(spare)}" aria-describedby="ns-cap-h">'
           f'<div class="ax-small ax-muted" id="ns-cap-h">At most {K.money(spare)}.</div></div>'
           '<div class="ax-field"><label for="ns-like">Copy the rules of</label><select class="ax-input" id="ns-like" '
           f'name="like">{opts}<option value="none">None: the pool defaults (target 3 %, stop 2 %)</option></select></div>'
           '<label class="ax-check"><input type="checkbox" name="run"><span>Start it now: running in AUTO with auto-pick '
           'on. I understand AUTO sends its paper entries without asking me.</span></label>'
           '<div class="ax-err" role="alert"></div></div><div class="ax-dfoot"><button class="ax-btn" type="button" '
           'data-ax-dclose>Cancel</button><button class="ax-btn primary" type="submit">Create session</button></div>'
           '</form></dialog>'
           """<script>(function(){var d=document.getElementById('ax-new');if(!d)return;var f=d.querySelector('form');
f.addEventListener('submit',function(ev){ev.preventDefault();var run=f.elements.run.checked,b=f.querySelector('button[type=submit]');b.disabled=true;
 AX.api('/api/v2/sessions/new',{body:{name:f.elements.name.value,capital:f.elements.capital.value.replace(/,/g,''),like:f.elements.like.value,run:run,ack:run}})
 .then(function(r){d.close();AX.toast(r.id+' created with '+AX.inr(r.start_capital)+(run?', running in AUTO.':'. It is paused: open its setup to start it.'));
  setTimeout(function(){location.hash='setup-'+r.id;location.reload();},900);})
 .catch(function(x){d.querySelector('.ax-err').textContent=x.message;}).finally(function(){b.disabled=false;});});})();</script>""")
    return btn, dlg


def _capital_plan(conn) -> tuple:
    """CAP-04 (16 Nov 2026): the "Top up sessions" button and dialog -- the paper capital plan (orders/capital_plan.py):
    the same top-up to every running session out of the cash no session owns, keeping the cash buffer. PAPER only."""
    from orders import capital_plan as CP
    try:
        p = CP.plan(conn)
    except Exception as x:
        import logging
        logging.getLogger("atip.ux").warning(f"  capital plan unavailable: {x}")
        return "", ""
    if not p.get("paper"):
        return "", ""
    if not p["total_top_up"]:
        return ('<button class="ax-btn" type="button" disabled title="No paper cash above the cash buffer is outside the '
                'sessions">Top up sessions…</button>'), ""
    b, a = p["before"], p["after"]
    rows = "".join(f'<tr><td>{e(r["session"])}</td><td class="num">{K.money(r["capital_before"])}</td>'
                   f'<td class="num">+{K.money(r["top_up"])}</td><td class="num">{K.money(r["capital_after"])}</td>'
                   f'<td>{e(r["next_buy_when"])}</td></tr>' for r in p["rows"] if r["top_up"])
    btn = ('<button class="ax-btn" type="button" onclick="document.getElementById(\'ax-cap\').showModal()">'
           'Top up sessions…</button>')
    dlg = ('<dialog class="ax-dialog" id="ax-cap" aria-labelledby="ax-cap-h"><form novalidate><div class="ax-dhead" '
           'id="ax-cap-h">Top up the sessions (paper)</div><div class="ax-dbody">'
           f'<p class="ax-small ax-muted">Paper pool {K.money(p["pool"]["value"])}. {K.money(p["spare_before"])} belongs to no '
           f'session; {K.money(p["buffer"])} ({p["config"]["cash_buffer_pct"]:g} %) stays as a cash buffer. Each running session '
           f'gets the same {K.money(p["per_session"])} by a transfer: no books restart, no open trade changes, the next buy is '
           f'sized from the larger cash. Allocated {b["allocated_pct"]:g} % → {a["allocated_pct"]:g} % of the pool.</p>'
           '<table class="ax-table"><thead><tr><th>Session</th><th class="num">Capital</th><th class="num">Top-up</th>'
           f'<th class="num">After</th><th>Next buy</th></tr></thead><tbody>{rows}</tbody></table>'
           '<label class="ax-check"><input type="checkbox" name="ack" required><span>Move '
           f'{K.money(p["total_top_up"])} of paper cash into these sessions.</span></label>'
           '<div class="ax-err" role="alert"></div></div><div class="ax-dfoot"><button class="ax-btn" type="button" '
           'data-ax-dclose>Cancel</button><button class="ax-btn primary" type="submit">Top up</button></div></form></dialog>'
           """<script>(function(){var d=document.getElementById('ax-cap');if(!d)return;var f=d.querySelector('form');
f.addEventListener('submit',function(ev){ev.preventDefault();if(!f.elements.ack.checked){d.querySelector('.ax-err').textContent='Tick the box to confirm.';return;}
 var b=f.querySelector('button[type=submit]');b.disabled=true;
 AX.api('/api/v2/capital/apply',{body:{ack:true}}).then(function(r){d.close();AX.toast('Moved '+AX.inr(r.moved)+' into '+r.sessions.length+' session(s).');
  setTimeout(function(){location.reload();},900);}).catch(function(x){d.querySelector('.ax-err').textContent=x.message;}).finally(function(){b.disabled=false;});});})();</script>""")
    return btn, dlg


def _worked_example() -> str:
    return ("""<details class="ax-help"><summary>How the numbers work (worked example)</summary>
<p>S1 starts with ₹25,000. Cycle 1 buys ₹25,000 of stock and sells it at +3 % for ₹25,750.</p>
<dl class="ax-kv"><dt>Gross P&amp;L (Total P&amp;L)</dt><dd>+₹750.00</dd>
<dt>Charges (delivery)</dt><dd>₹72.27 = STT ₹50.75 + exchange ₹1.51 + SEBI ₹0.05 + stamp ₹3.75 + GST ₹0.28 + DP ₹15.93</dd>
<dt>Net P&amp;L before tax</dt><dd>+₹677.73</dd><dt>Tax reserve (per-cycle, cash set aside) — STCG 20 %</dt><dd>₹135.55</dd>
<dt>Actual P&amp;L (net after charges &amp; tax)</dt><dd>+₹542.18</dd>
<dt>Cycle 2 starts with</dt><dd>₹25,677.73 (cash after charges) or ₹25,542.18 (after setting aside the tax)</dd></dl>
<p class="ax-small ax-muted">Tax is an estimate, not tax advice. Per cycle it is only a reserve (cash set aside, not the tax due);
the FY estimate on Accounting › Tax sets losses off against gains and applies the LTCG exemption once a year. Rates are on Settings › Tax.</p></details>""")


EVENT_LABEL = {"LINK": "Order linked", "EDIT": "Settings changed", "MODE": "Mode", "STATUS": "Status", "POOL": "Starting amount",
               "POOL_END": "Pool ended", "TRANSFER": "Transfer", "ADJUSTMENT": "Adjustment", "CREATED": "Created",
               "AUTO_SEND": "Auto-send", "CAPITAL_PLAN": "Capital plan top-up"}


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
              ("charges", "Charges", "money"), ("net_pnl", "Net before tax", "pnl"), ("tax_est", "Tax reserve (per-cycle)", "money"),
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
        ["Net P&L before tax", K.inr(r["net_pnl"], True)], [f"− Tax reserve (per-cycle, cash set aside, {r['tax_head']})", K.inr(r["tax_est"])],
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
    mar = [("Manage", path, tab == "manage"), ("Trades & ledger", path + "?tab=trades", tab == "trades"),
           ("Accounting", path + "?tab=accounting", tab == "accounting"), ("Reports", path + "?tab=reports", tab == "reports")]
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
    if tab == "trades":                                              # 18 Nov 2026: the session engine's drill-down
        return page(title, _engine_drill(conn, sid), path, "tab=trades", root=root, app=app, mar=mar, actions=actions)
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
                'gross P&amp;L, each charge, the per-cycle tax reserve and actual P&amp;L.</p><div class="ax-row">' +
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
        K.tile("Starting amount", K.money(ti["start_capital"]),
               f'{"+" if ti.get("transfers", 0) > 0 else "−"}{K.money(abs(ti["transfers"]))} moved in · capital {K.money(ti["capital"])}'
               if ti.get("transfers") else ""),
        K.tile("Equity now", K.money(ti["equity"]), f'cash {K.money(ti["cash"])}'),
        K.tile("Gross P&L", K.money(ti["gross_pnl"], True), "before any cost", True),
        K.tile("Charges", K.money(ti["charges"])), K.tile("Tax reserve (per-cycle)", K.money(ti["tax_est"]), "cash set aside, not tax due"),
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
                f'</div><div class="ax-card"><h2>Activity</h2><ol class="ax-timeline" tabindex="0" aria-label="Activity, newest first">{ev or "<li>No change yet.</li>"}</ol></div></div>')
    body = (_closed_banner() + waiting + tl + (f'<div class="ax-grid" style="margin-top:14px">{opens}</div>' if opens else "") +
            '<div style="margin-top:14px"></div>' + charts + timeline + table + _worked_example() + gen_js)
    return page(title, body, path, root=root, app=app, mar=mar, actions=actions,
                job=f"{s.get('strategy') or ''} · {s['mode']} · {s['status']} · {b['pool']['exit_rule']}")


def _engine_drill(conn, sid: str) -> str:
    """One session's wallet, its trade list and its ledger history, each with a CSV export (times IST)."""
    from orders import session_engine as SE
    SE.sync(conn, sid)
    g = SE.stats(conn, sid)
    if not g:
        return f'<div class="ax-empty">{e(sid)} has no seed capital yet.</div>'
    wr = f'{g["win_rate_pct"]:.1f}%' if g.get("win_rate_pct") is not None else "—"
    tl = '<div class="ax-tiles">' + "".join([
        K.tile("State", e(g["state"]), f'since {e(str(g.get("created_at_ist") or "")[:16].replace("T", " "))} IST'),
        K.tile("Current balance", K.money(g["current_balance"]), f'{K.pct(g["growth_pct"])} on {K.money(g["capital"])}'),
        K.tile("Cash available", K.money(g["cash_available"]), f'invested {K.money(g["invested_value"])}'),
        K.tile("Realised P&L", K.money(g["realised_pnl"], True), f'charges {K.money(g["charges"])}', True),
        K.tile("Unrealised P&L", K.money(g["unrealised_pnl"], True), "", True),
        K.tile("Completed trades", f'<span class="ax-num">{g["completed_trades"]} / {g["max_completed_trades"]}</span>',
               f'{g["legs"]} legs · {g["open_trades"]} open'),
        K.tile("Win rate", wr, f'{g["wins"]} won · {g["losses"]} lost'),
        K.tile("Average P&L / trade", K.money(g["avg_pnl_per_trade"], True) if g.get("avg_pnl_per_trade") is not None else "—"),
        K.tile("Pause floor", K.money(g["floor"]), f'{g["floor_pct"]:g}% of capital: under it the session pauses')]) + "</div>"
    trs = [{**t, "opened": str(t.get("opened_at_ist") or "").replace("T", " ")[:16],
            "closed": str(t.get("closed_at_ist") or "").replace("T", " ")[:16]} for t in SE.trades(conn, sid)]
    led = [{**r, "when": str(r.get("ts_ist") or "").replace("T", " ")[:16]} for r in SE.ledger_rows(conn, sid)]
    csv = lambda kind: (f'<a class="ax-btn sm" href="/api/v2/engine/{e(sid)}/{kind}.csv" download '
                        f'title="Every column, times in IST">Full CSV (IST)</a>')
    return (_closed_banner() + _engine_meter(g["completed_trades"], g["max_completed_trades"]) + tl +
            f'<div class="ax-row" style="justify-content:space-between"><h2 class="ax-h2">Trades</h2>{csv("trades")}</div>' +
            K.table("t-etrades", [("opened", "Opened (IST)", "text"), ("symbol", "Symbol", "text"), ("qty", "Qty", "int"),
                                  ("buy_price", "Buy", "money"), ("sell_price", "Sell", "money"), ("charges", "Charges", "money"),
                                  ("net_pnl", "Net P&L", "pnl"), ("status", "Status", "text"), ("closed", "Closed (IST)", "text")],
                    list(reversed(trs)), name=f"{sid}_trades", empty="No trade yet.") +
            f'<div class="ax-row" style="justify-content:space-between"><h2 class="ax-h2">Ledger history</h2>{csv("ledger")}</div>'
            '<p class="ax-small ax-muted">The session\'s own cash book: its seed, money moved in or out, every BUY and SELL '
            'leg and the charges on each. A PNL line notes a completed trade\'s net result and does not move the balance.</p>' +
            K.table("t-eledger", [("seq", "#", "int"), ("when", "When (IST)", "text"), ("type", "Type", "text"),
                                  ("memo", "What", "text"), ("amount", "Amount", "pnl"), ("balance_after", "Balance", "money")],
                    list(reversed(led)), name=f"{sid}_ledger", empty="No ledger line yet."))


# ── Accounting ───────────────────────────────────────────────────────────────────────────────

ACC_TABS = [("pnl", "P&L by period"), ("ledger", "General ledger"), ("session", "Session ledger"), ("charges", "Charges register"), ("tax", "Tax register"),
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
    if tab == "pnl":                      # UI fixes v2 (H1): the figures Home shows, and the ledger's own read of them
        from accounting.periods import reconcile
        from dashboard import fmt as F
        rc = reconcile(conn)
        ok = (f'<div class="ax-banner {"good" if rc["ok"] else "bad"}" role="status">'
              f'{"✓ Home and the general ledger agree" if rc["ok"] else "✗ Home and the general ledger disagree"} '
              f'for Today, This month and Since start (realised, after charges and the per-cycle tax reserve).</div>')
        rows = "".join(
            f'<tr data-period="{e(r["key"])}"><th scope="row">{e(r["period"])}</th>'
            f'<td class="n" data-v="{r["gross"]}">{F.inr_html(r["gross"], True)}</td>'
            f'<td class="n" data-v="{r["charges"]}">{e(F.fmt_inr(r["charges"]))}</td>'
            f'<td class="n" data-v="{r["tax_est"]}">{e(F.fmt_inr(r["tax_est"]))}</td>'
            f'<td class="n" data-v="{r["realised"]}" data-k="realised">{F.inr_html(r["realised"], True)}</td>'
            f'<td class="n" data-v="{r["open"]}" data-k="open">{F.inr_html(r["open"], True)}</td>'
            f'<td class="n" data-v="{r["total"]}" data-k="total">{F.inr_html(r["total"], True)}</td>'
            f'<td class="n" data-v="{r["ledger"]}">{F.inr_html(r["ledger"], True)} {"✓" if r["match"] else "✗"}</td>'
            f'<td class="n">{r["cycles"]}</td></tr>' for r in rc["rows"])
        body = (ok + '<div class="ax-tablewrap"><table class="ax-table" id="t-pnl"><thead><tr><th scope="col">Period</th>'
                '<th class="n" scope="col">Gross</th><th class="n" scope="col">Charges</th><th class="n" scope="col">Tax reserve (per-cycle)</th>'
                '<th class="n" scope="col">Realised</th><th class="n" scope="col">Open now</th><th class="n" scope="col">Total</th>'
                '<th class="n" scope="col">Ledger realised</th><th class="n" scope="col">Cycles</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>'
                '<p class="ax-small ax-muted">Realised = cycles closed in the period, after charges and the per-cycle tax reserve (cash set aside, not the tax due; the FY estimate is on the Tax tab). '
                'Open now = the trades still open, at their current price; it is the same in every period. '
                'Today is the calendar day in IST; Since start is each session\'s current books.</p>')
    elif tab == "ledger":
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
                                    ("rate_pct", "Rate", "rate"), ("tax", "Tax (FY est.)", "money")], st["heads"], tools=False)
        kvs = [(f'Set-off: {x["from"]} loss against {x["against"]}', f'{K.money(x["amount"])} <span class="ax-muted">{e(x["rule"])}</span>')
               for x in st["set_offs"]]
        kvs += [("LTCG exemption used", K.money(st["ltcg_exemption_used"]))]
        if st["other_income"] is not None:
            kvs += [("Other income (from Settings)", K.money(st["other_income"])),
                    ("Unused nil slab set against STCG / LTCG", K.money(st["basic_exemption_used"]))]
        kvs += [("Tax before surcharge and cess", K.money(st["tax_before_addons"])),
                (f'Surcharge {st["rates"]["surcharge_pct"]:g}% · cess {st["rates"]["cess_pct"]:g}%',
                 f'{K.money(st["surcharge"])} · {K.money(st["cess"])} ({"in" if st["addons_applied"] else "not in"} the liability)'),
                ("Estimated liability", f'<b>{K.money(st["liability"])}</b>'),
                (TX.RESERVE_LABEL, K.money(st["provisional_set_aside"])),
                ("Reserve − liability", K.money(st["difference"], True))]
        kvs += [(f"Carried forward: {h} loss", K.money(v)) for h, v in st["carried_forward"].items()]
        cyc = [{**c, "opened": str(c["opened_at"])[:10], "closed": str(c["closed_at"])[:10]} for c in tr["cycles"]]
        body = (f'<div class="ax-banner warn" role="note">{e(TX.DISCLAIMER)} Rates effective {e(st["rates"]["effective_from"])}: '
                f'STCG {st["rates"]["stcg_pct"]:g}%, LTCG {st["rates"]["ltcg_pct"]:g}% above {K.inr(st["rates"]["ltcg_exemption"])}, '
                f'intraday and F&amp;O {"from " if st["rates"]["other_income"] is not None else ""}{TX.rate("BUSINESS", st["rates"]):g}% ({e(st["regime_label"])}); STT '
                f'{"deducted" if st["rates"]["stt_deductible"] else "not deducted"} from capital gains. '
                '<a href="/settings#tax">Change rates</a></div>'
                '<form class="ax-row" method="get"><input type="hidden" name="tab" value="tax">' +
                _select("fy", "Financial year (Apr–Mar)", [(f, f) for f in tr["fys"]], fy, False) +
                '<div class="ax-field" style="justify-content:flex-end"><button class="ax-btn" type="submit">Show</button></div></form>' +
                _fy_estimate_card(tr.get("estimate")) +
                heads + '<div class="ax-card" style="margin-top:12px"><h2>Set-offs and liability</h2>' + K.kv(kvs) + "</div>" +
                '<h2 class="ax-h2">Cycles of the year</h2>' +
                K.table("t-taxc", [("session_id", "Session", "text"), ("symbol", "Symbol", "text"), ("opened", "Bought", "text"),
                                   ("closed", "Sold", "text"), ("holding_days", "Days", "int"), ("net_pnl", "Net P&L", "pnl"),
                                   ("tax_head", "Head", "text"), ("tax_est", "Tax reserve (per-cycle)", "money"), ("env", "Env", "text")], cyc,
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
                job="The money every session touched: a double-entry ledger, every charge, the tax reserve and FY tax estimate, virtual and actual "
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
    shown = lambda v: "" if v is None else (int(v) if float(v).is_integer() else v)
    num = lambda k, lab, help_="": (f'<div class="ax-field"><label for="tx-{k}">{e(lab)}</label><input class="ax-input" id="tx-{k}" name="{k}" '
                                    f'value="{e(shown(tx[k]))}" inputmode="decimal">{f"<div class=ax-help>{e(help_)}</div>" if help_ else ""}</div>')
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
            '<p class="ax-small ax-muted">These limits are part of ATIP\'s configuration on the Mac; the operator changes them. '
            'The kill switch and resume are on <a href="/command">Command Center</a>. '
            '<details class="ax-tech"><summary>ⓘ Technical details</summary><code>atip_data/config.json → "risk"</code></details></p></section>'
            '<section class="ax-card" id="tax"><h2>Tax rates (estimates)</h2>'
            f'<div class="ax-banner warn">{e(TX.DISCLAIMER)} Defaults follow the Income-tax Act, 2025 and Budget 2026 '
            '(checked 3 Nov 2026).</div><form id="txf">' +
            f'<div class="ax-field"><label for="tx-eff">Effective from</label><input class="ax-input" id="tx-eff" name="effective_from" type="date" value="{e(tx["effective_from"])}"></div>'
            '<div class="ax-row">' + _select("regime", "Tax regime", [(k, v) for k, v in TX.REGIME_LABEL.items()], tx["regime"], False)
            .replace('id="q-regime"', 'id="tx-regime"').replace('for="q-regime"', 'for="tx-regime"') +
            num("other_income", "Your other taxable income this year (₹, optional)",
                "Salary, interest and the like, after deductions. Blank: intraday and F&O use the slab % below.") + "</div>"
            '<div class="ax-row">' + num("stcg_pct", "STCG % (≤ 12 months, s.196)") + num("ltcg_pct", "LTCG % (> 12 months, s.198)") + "</div>"
            '<div class="ax-row">' + num("ltcg_exemption", "LTCG exemption (₹ a year)") +
            num("slab_pct", "Slab % for intraday, F&O (when other income is blank)") + "</div>"
            '<div class="ax-row">' + num("surcharge_pct", "Surcharge %", "0 below ₹50 lakh of total income; capped at 15 on capital gains") +
            num("cess_pct", "Cess %") + "</div>"
            f'<label class="ax-check"><input type="checkbox" name="addons_enabled"{" checked" if tx["addons_enabled"] else ""}><span>Add surcharge and cess to the estimates (the law always adds the 4% cess)</span></label>'
            f'<label class="ax-check"><input type="checkbox" name="stt_deductible"{" checked" if tx["stt_deductible"] else ""}><span>Deduct STT from capital gains too (the law does not: s.72(3), was s.48; intraday and F&amp;O always deduct it)</span></label>'
            f'<p class="ax-small">Intraday and F&amp;O are taxed at {"from " if tx["other_income"] is not None else ""}{TX.rate("BUSINESS", tx):g}% '
            f'{"(your slab on the next rupee above your other income; each cycle is estimated at the slab its profit reaches)" if tx["other_income"] is not None else "(the slab % above)"}; '
            'delivery trades at the flat STCG / LTCG rates, whatever your slab.</p>'
            '<details><summary>Slabs, tax year 2026-27 (individual, resident, below 60)</summary>' + _slab_table() + '</details>'
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
['effective_from','regime','other_income','stcg_pct','ltcg_pct','ltcg_exemption','slab_pct','surcharge_pct','cess_pct'].forEach(function(n){b[n]=f[n].value;});b.addons_enabled=f.addons_enabled.checked;b.stt_deductible=f.stt_deductible.checked;
AX.api('/api/v2/tax/config',{body:b}).then(function(r){AX.toast('Tax rates saved'+(r&&r.retaxed?'; '+r.retaxed+' closed cycles re-estimated.':'.'));setTimeout(function(){location.reload();},900);}).catch(function(e){document.getElementById('tx-err').textContent=e.message;});});})();</script>""")
    return page("Settings", body, "/settings", root=root, app=app,
                job="Credentials, risk limits, tax rates, notifications and display, grouped in one place.")


def _slab_table() -> str:
    from accounting import tax as TX
    rows = []
    for regime, slabs in TX.SLABS.items():
        lower = 0
        for upper, pct in slabs:
            band = f"above {K.inr(lower)[:-3]}" if upper is None else f"{K.inr(lower)[:-3]} – {K.inr(upper)[:-3]}"
            rows.append(f'<tr><td>{e(TX.REGIME_LABEL[regime])}</td><td>{band}</td><td class="n">{pct:g}%</td></tr>')
            lower = upper or lower
    return ('<div class="ax-tablewrap"><table class="ax-table"><thead><tr><th scope="col">Regime</th><th scope="col">Income</th>'
            '<th scope="col" class="n">Rate</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
            '<p class="ax-small ax-muted">Surcharge 10% above ₹50 lakh, 15% above ₹1 crore, 25% above ₹2 crore (never above '
            '15% on capital gains); 4% cess on tax plus surcharge. The s.156 rebate (was s.87A) is not counted.</p>')


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
    from dashboard.shell import icon
    secs = "".join(f'<section class="ax-card"><h2 class="ax-ih">{icon(ico)} {e(label)}</h2>' + "".join(
        f'<div style="padding:6px 0;border-bottom:1px solid var(--ax-border-soft)"><a href="{e(h)}">{e(l)}</a>'
        f'<div class="ax-small ax-muted">{e(j)}</div></div>' for l, h, j in kids) + "</section>"
                   for key, label, ico, _, kids in NAV if key not in ("home",))
    return page("More", f'<div class="ax-grid">{secs}</div>', "/more", root=root, app=app,
                job="Every part of ATIP, section by section, Reports and Live feeds included. The phone's tabs cover Home, "
                    "Watchlist, Trade and Sessions; everything else is here.")


def ops(conn, root="", app=False) -> str:
    runs = _rows(conn, "SELECT job_name, start_time, end_time, status, rows_processed, duration_s, error_msg FROM pipeline_log "
                       "ORDER BY id DESC LIMIT 40")
    body = ('<div class="ax-grid-2"><div class="ax-card"><h2>Liveness and readiness</h2><div id="ops-h"><div class="ax-skel" style="height:80px"></div></div></div>'
            '<div class="ax-card"><h2>Operating status</h2><div id="ops-s"><div class="ax-skel" style="height:80px"></div></div></div></div>'
            '<h2 class="ax-h2">Latest job runs</h2>' +
            K.table("t-runs", [("job_name", "Job", "text"), ("start_time", "Started", "text"), ("status", "Status", "text"),
                               ("rows_processed", "Rows", "int"), ("duration_s", "Seconds", "int"), ("error_msg", "Error", "text")],
                    runs, name="job_runs", empty="No job has run yet.") +
            """<script>document.addEventListener('DOMContentLoaded',function(){function kv(o){return '<dl class="ax-kv">'+Object.keys(o).filter(function(k){return typeof o[k]!=='object'||o[k]===null;}).map(function(k){return '<dt>'+AX.esc(k.replace(/_/g,' '))+'</dt><dd>'+AX.esc(o[k])+'</dd>';}).join('')+'</dl>';}
Promise.all([fetch('/health').then(function(r){return r.json();}).catch(function(e){return {error:String(e)};}),fetch('/health/ready').then(function(r){return r.json();}).catch(function(e){return {error:String(e)};})])
.then(function(a){document.getElementById('ops-h').innerHTML='<h3 class="ax-h3">/health</h3>'+kv(a[0])+'<h3 class="ax-h3">/health/ready</h3>'+kv(a[1]);});
AX.api('/api/status').then(function(s){document.getElementById('ops-s').innerHTML=kv(s);}).catch(function(e){document.getElementById('ops-s').textContent=e.message;});});</script>""")      # after the deferred atip.js (AX)
    return page("Ops & health", body, "/ops", root=root, app=app,
                job="Whether ATIP's processes, data and jobs are healthy. The JSON behind it stays at /health.")
