"""
The ATIP shell (UXD-02, 2 Nov 2026): one persistent frame on every page -- a left rail of the ten sections on desktop,
a five-tab bar on phones, and a top bar with the Ctrl/⌘K search, the environment badge (PAPER / SANDBOX / LIVE),
the run state in /api/status's own words, the kill switch, notifications, theme and profile.

    NAV                 the information architecture: ten sections, every existing page folded into one of them
    install(app)        mounts /static and the ShellMiddleware: every older HTML page (GET, text/html) is served
                        inside the shell -- the stylesheet in its <head>, the frame after <body>, the script before
                        </body> -- with its own content untouched. Old URLs keep working; nothing redirects away.
                        A page that renders the shell itself (<meta name="atip-shell" content="native">), the
                        sign-in page, ?shell=0 and anything that is not HTML pass through as they are.
    page(...)           a new page (Home, Sessions, Accounting, Reports, Settings, the hubs) rendered in the shell
    state(conn)         GET /api/v2/shell/state: what the badge, the run state and the kill switch show

The middleware never writes the dashboard token into a page: an older page carries its own (TOKEN / ATIP_TOKEN),
and a new page gets page_token() inside its own request, so enterprise mode keeps each tenant's token its own.
Inside the phone apps (user agent "ATIPApp/", or ?app=1) the rail and the tab bar hide: the apps have native tabs.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("atip.shell")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# (key, label, icon, landing href, [(label, href, one-line job)])
NAV = [
    ("home", "Home", "⌂", "/home", [
        ("Today", "/home", "Is ATIP safe, running and making money today?")]),
    ("markets", "Markets", "◔", "/markets", [
        ("Markets overview", "/markets", "Every market screen in one place"),
        ("Market & FII/DII", "/market", "Index levels, breadth, FII / DII flows"),
        ("Market pulse", "/market-pulse", "Sector heat, movers and breadth right now"),
        ("Global & Asia", "/global-asia", "Asian sessions, global cues and the Asia sentiment score"),
        ("News & impact", "/?tab=news", "Headlines scored for their impact on your stocks"),
        ("Live feeds", "/m?tab=live", "Dhan real-time feed health"),
        ("Data platform", "/data-platform", "Data sources, freshness and quality")]),
    ("research", "Research", "⌕", "/", [
        ("ATIP Scores", "/", "Every stock's ATIP score with VPI, ZPI, RRI, MRI and CRI tabs"),
        ("Research", "/research", "One stock in depth"),
        ("Screener", "/screener", "Filter the universe by score, factor and price"),
        ("Signals", "/signals", "Signal explorer: today's and past signals"),
        ("Signal history", "/?tab=hist", "How past signals played out"),
        ("Options builder", "/options-builder", "Build and price option strategies")]),
    ("trade", "Trade", "⇅", "/command", [
        ("Command Center", "/command", "Is it safe, what needs me, what is running"),
        ("Trade signals", "/command/signals", "Today's EOD signals with live prices"),
        ("Trade setup", "/command/setup", "Size a trade with its ₹ risk before sending"),
        ("Live monitor", "/command/monitor", "Open trades, stops and each session's cycle progress"),
        ("Journal", "/command/journal", "Per-session trades, win rate and R"),
        ("Paper P&L", "/command/pnl", "The paper account's P&L by day, week and month"),
        ("Orders & execution", "/trading", "Order book, fills and execution quality"),
        ("Baskets & SIP", "/baskets", "Baskets and stock SIPs (paper)"),
        ("Execution lab", "/execution-lab", "Slippage, TCA and order-type experiments")]),
    ("sessions", "Sessions", "▦", "/sessions", [
        ("All sessions", "/sessions", "The ten sessions as mini-funds: gross vs actual P&L, cycles, floors")]),
    ("accounting", "Accounting", "₹", "/accounting", [
        ("General ledger", "/accounting?tab=ledger", "Every rupee every session touched, double-entry"),
        ("Session ledger", "/accounting?tab=session", "One session's cash, line by line"),
        ("Charges register", "/accounting?tab=charges", "Charges by type, session and month"),
        ("Tax register", "/accounting?tab=tax", "Gains by tax head and the FY estimate"),
        ("Funds", "/accounting?tab=funds", "Virtual (paper) and actual (broker) money, apart"),
        ("Reconciliation", "/accounting?tab=recon", "ATIP's charges and cash against the broker's"),
        ("Transfers", "/accounting?tab=transfers", "Move money between sessions")]),
    ("reports", "Reports", "▤", "/reports", [
        ("Generate", "/reports", "Make any report now in HTML, CSV, Excel or PDF"),
        ("Schedules", "/reports?tab=schedules", "When each report runs, in IST"),
        ("Archive", "/reports?tab=archive", "Every report generated, to download again"),
        ("Custom report", "/reports?tab=custom", "Pick sections, sessions and dates")]),
    ("lab", "Strategy Lab", "⚗", "/lab", [
        ("Run → compare → promote", "/lab", "Backtest, compare, then promote a strategy"),
        ("Strategies", "/strategies", "Your strategies and their status"),
        ("Strategy builder", "/strategy-builder", "Build a rule-based strategy"),
        ("Backtests", "/backtests", "Run and compare backtests"),
        ("Quant", "/quant", "Factor research and experiments"),
        ("ML", "/ml", "Models, versions and their metrics")]),
    ("wealth", "Wealth", "◈", "/wealth", [
        ("Wealth", "/wealth", "Goals, allocation, rebalancing and the advisor")]),
    ("settings", "Settings & Admin", "⚙", "/settings", [
        ("Settings", "/settings", "Credentials, risk limits, tax rates and notifications"),
        ("Account", "/account", "Profile, sign-in and notifications"),
        ("Brokers", "/brokers", "Dhan and other broker connections"),
        ("Admin", "/admin", "Users, tenants and roles"),
        ("Admin console", "/admin/console", "Platform administration"),
        ("Compliance", "/compliance", "SEBI, audit trail and the algo-ID guard"),
        ("Privacy", "/privacy", "Data retention and your data"),
        ("Ops & health", "/ops", "Health checks, jobs and backups"),
        ("Assistant", "/assistant", "Ask ATIP"),
        ("Workspace", "/app", "The enterprise workspace"),
        ("Mobile view", "/m", "The phone-sized summary")]),
]
TABBAR = [("Home", "⌂", "/home", "home"), ("Trade", "⇅", "/command", "trade"), ("Sessions", "▦", "/sessions", "sessions"),
          ("Reports", "▤", "/reports", "reports"), ("More", "⋯", "/more", "more")]
SKIP_PREFIXES = ("/api/", "/static/", "/health", "/manifest.webmanifest", "/sw.js", "/icon.svg", "/login",
                 "/zerodha/", "/docs", "/redoc", "/openapi.json")
NATIVE_MARK = '<meta name="atip-shell" content="native">'


def _version() -> str:
    h = hashlib.sha1()
    for f in ("atip.css", "atip.js"):
        try:
            h.update((STATIC_DIR / "atip" / f).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


VERSION = _version()


def section_of(path: str, query: str = "") -> tuple[str, str]:
    """(section key, page label) for a path: an exact child match (query included) first, then the child with the
    same path and no query, then the section a path prefix belongs to."""
    full = path + (("?" + query) if query else "")
    for exact in (True, False):
        for key, label, _, _, kids in NAV:
            for kl, href, _ in kids:
                if (exact and href == full) or (not exact and "?" not in href and href == path):
                    return key, kl
    for prefix, key in (("/sessions/", "sessions"), ("/command", "trade"), ("/accounting", "accounting"),
                        ("/reports", "reports"), ("/wealth", "wealth"), ("/admin", "settings"), ("/more", "more"),
                        ("/trade", "trade"), ("/scores", "research")):
        if path.startswith(prefix):
            return key, ""
    return "", ""


def nav_index() -> list:
    """Everything the search palette can jump to without asking the server."""
    out = []
    for key, label, _, _, kids in NAV:
        for kl, href, job in kids:
            out.append({"label": kl, "section": label, "href": href, "kind": "page", "keywords": job})
    return out


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def chrome(path: str, query: str, st: dict, root: str = "") -> tuple[str, str]:
    """(markup after <body>, markup before </body>) of the shell for one page."""
    sec, page_label = section_of(path, query)
    rail = []
    for key, label, ico, href, kids in NAV:
        on = key == sec
        sub = "".join(f'<a href="{_e(root + h)}"{" aria-current=page" if on and kl == page_label else ""}>{_e(kl)}</a>'
                      for kl, h, _ in kids) if len(kids) > 1 else ""
        rail.append(f'<div class="ax-sec{" on" if on else ""}"><a href="{_e(root + href)}"'
                    f'{" aria-current=true" if on else ""}><span class="ax-ico" aria-hidden="true">{ico}</span>'
                    f'{_e(label)}</a>{f"<div class=ax-sub>{sub}</div>" if sub else ""}</div>')
    sec_label = next((l for k, l, *_ in NAV if k == sec), "More" if sec == "more" else "")
    crumbs = (f'<b>{_e(sec_label)}</b>' + (f" › {_e(page_label)}" if page_label and page_label != sec_label else "")) \
        if sec_label else ""
    env = st.get("env") or "PAPER"
    status = st.get("status") or "RUNNING"
    sym = {"RUNNING": "●", "STOPPED": "⏸", "RESUMING": "↻"}.get(status, "●")
    viewer = st.get("role") == "VIEWER"
    kill = ('<button class="ax-killbtn" data-ax-kill type="button" disabled>View only</button>' if viewer else
            f'<button class="ax-killbtn" data-ax-kill type="button" data-halted="{1 if st.get("halted") else 0}" '
            f'aria-haspopup="dialog">{"Halted" if st.get("halted") else "Kill switch"}</button>')
    top = f"""<a class="ax-skip" href="#ax-main">Skip to content</a>
<header class="ax-top" role="banner">
 <button class="ax-iconbtn ax-menu" type="button" aria-label="Open the menu" aria-expanded="false" aria-controls="ax-rail">☰</button>
 <span class="ax-crumbs">{crumbs}</span><span class="ax-spacer"></span>
 <button class="ax-search" type="button" data-ax-search aria-label="Search pages, symbols and commands (Ctrl K)">⌕ <span class="ax-searchlabel">Search symbols, pages, commands</span> <kbd>Ctrl K</kbd></button>
 <span class="ax-pill ax-env ax-env-top" data-ax-env data-env="{_e(env)}" role="status">{_e(env)}</span>
 <span class="ax-pill ax-run" data-ax-run data-status="{_e(status)}" role="status" aria-live="polite">{sym} <span class="ax-runtext">{_e(st.get("banner") or "ATIP running")}</span></span>
 {kill}
 <a class="ax-iconbtn ax-hide-sm" href="{_e(root)}/account#notifications" aria-label="Notifications">🔔<span class="ax-count" data-ax-unread hidden></span></a>
 <button class="ax-iconbtn" type="button" data-ax-theme aria-label="Switch between light, dark and your device's theme">◐</button>
 <a class="ax-iconbtn ax-hide-sm" href="{_e(root)}/account" aria-label="Your profile">◯</a>
</header>
<nav class="ax-rail" id="ax-rail" aria-label="ATIP sections"><a class="ax-brand" href="{_e(root)}/home"><span class="ax-logo">A</span>ATIP</a>{"".join(rail)}
 <div class="ax-railfoot">Press <span class="ax-kbd">?</span> for shortcuts · <span class="ax-kbd">Ctrl K</span> to search</div></nav>
<div class="ax-scrim"></div>"""
    tabs = "".join(f'<a href="{_e(root + h)}"{" aria-current=page" if k == sec else ""}><span class="ax-ico" aria-hidden="true">'
                   f'{i}</span>{_e(l)}</a>' for l, i, h, k in TABBAR)
    bottom = f"""<nav class="ax-tabbar" aria-label="ATIP">{tabs}</nav>
<dialog class="ax-dialog ax-palette" id="ax-palette" aria-label="Search ATIP"><input type="search" placeholder="Search a symbol, a page or a command…" aria-label="Search" aria-controls="ax-palette-list" autocomplete="off"><ul id="ax-palette-list" role="listbox"></ul></dialog>
<dialog class="ax-dialog ax-danger" id="ax-kill-dialog" aria-labelledby="ax-kill-h"><form method="dialog">
 <div class="ax-dhead" id="ax-kill-h">Stop all trading now?</div>
 <div class="ax-dbody"><p>The kill switch stops <b>every</b> order ATIP would place — entries and exits, stop-losses included — until you resume it on Trade › Command Center. The <span data-ax-running>0</span> running session(s) are paused.</p>
 <p data-ax-impact></p>
 <div class="ax-field"><label for="ax-kill-reason">Why (goes into the log)</label><input class="ax-input" id="ax-kill-reason" name="reason" maxlength="200" placeholder="e.g. market falling fast"></div></div>
 <div class="ax-dfoot"><button class="ax-btn" type="button" data-ax-dclose>Keep trading</button><button class="ax-btn danger" type="submit">Stop all trading</button></div>
</form></dialog>
<dialog class="ax-dialog" id="ax-help" aria-labelledby="ax-help-h"><div class="ax-dhead" id="ax-help-h">Keyboard shortcuts</div><div class="ax-dbody"><dl class="ax-kv">
 <dt><span class="ax-kbd">Ctrl K</span> or <span class="ax-kbd">/</span></dt><dd>Search pages, symbols and commands</dd>
 <dt><span class="ax-kbd">g</span> then <span class="ax-kbd">h</span> · <span class="ax-kbd">s</span> · <span class="ax-kbd">t</span> · <span class="ax-kbd">a</span> · <span class="ax-kbd">r</span></dt><dd>Go to Home · Sessions · Trade · Accounting · Reports</dd>
 <dt><span class="ax-kbd">g</span> then <span class="ax-kbd">c</span> · <span class="ax-kbd">m</span> · <span class="ax-kbd">l</span> · <span class="ax-kbd">w</span> · <span class="ax-kbd">e</span></dt><dd>ATIP Scores · Markets · Strategy Lab · Wealth · Settings</dd>
 <dt><span class="ax-kbd">Esc</span></dt><dd>Close a drawer or the menu</dd><dt><span class="ax-kbd">?</span></dt><dd>This list</dd></dl></div>
 <div class="ax-dfoot"><button class="ax-btn" type="button" data-ax-dclose>Close</button></div></dialog>
<aside class="ax-drawer" id="ax-row-drawer" aria-hidden="true" aria-labelledby="ax-row-h"><header><h2 id="ax-row-h">Details</h2><button class="ax-iconbtn" type="button" data-ax-close aria-label="Close">✕</button></header><div class="ax-body"></div></aside>
<div id="ax-nav" hidden data-json="{_e(json.dumps(nav_index()))}"></div>
<div id="ax-state" hidden data-json="{_e(json.dumps(st, default=str))}"></div>
<script src="{_e(root)}/static/atip/atip.js?v={VERSION}" defer></script>"""
    return top, bottom


def head_tags(root: str = "", app: bool = False) -> str:
    return (f'<meta name="atip-root" content="{_e(root)}"><link rel="stylesheet" href="{_e(root)}/static/atip/atip.css?v={VERSION}">'
            "<script>(function(){var d=document.documentElement;try{var q=new URLSearchParams(location.search).get('theme');"
            "var t=q||localStorage.getItem('atip-theme');if(t==='dark'||t==='light')d.setAttribute('data-theme',t);}catch(e){}"
            + ("d.classList.add('ax-app');" if app else "") + "})();</script>")


# -- the state the shell shows -------------------------------------------------------------------

def light_state() -> dict:
    """Run state, env, halt and role -- no session arithmetic (the page renders with this, the script polls the rest)."""
    out = {"status": "RUNNING", "banner": "ATIP running", "env": "PAPER", "halted": False, "halt_reason": None,
           "role": "TRADER"}
    try:
        from dashboard.global_asia import status_payload
        s = status_payload()
        out.update({k: s.get(k) for k in ("status", "banner", "read_only", "next_stop", "next_resume", "now_ist",
                                          "last_updated", "live_feeds") if k in s})
        out["role"] = s.get("role") or "TRADER"
    except Exception as e:
        log.debug(f"  shell status: {e}")
    try:
        from orders.environment import broker_env
        out["env"] = broker_env()
    except Exception:
        pass
    out["halted"] = _flag_halt()
    if out["halted"]:
        try:
            from orders.risk import halted
            out["halt_reason"] = halted()[1] or None
        except Exception:
            pass
    return out


def _flag_halt() -> bool:
    """The kill switch itself (the flag file or config), not the nightly stop window, which also 'halts'."""
    try:
        from orders.risk import HALT_FLAG, _config
        return HALT_FLAG.exists() or bool(_config().get("trading_halted"))
    except Exception:
        return False


_cache = {"at": 0.0, "data": None}


def state(conn) -> dict:
    """GET /api/v2/shell/state: light_state plus the sessions running, open trades and their value, unread alerts."""
    if _cache["data"] and time.monotonic() - _cache["at"] < 10:
        return _cache["data"]
    out = light_state()
    try:
        from orders import sessions as S
        ls = S.list_sessions(conn)
        out["sessions_running"] = ls["running"]
        out["auto_send"] = ls["auto_bypass"]
        opens = [h for s in ls["sessions"] if s.get("pool") for h in s["pool"]["open"]]
        out["open_trades"] = len(opens)
        out["open_value"] = round(sum(float(h["value"]) for h in opens), 2)
    except Exception as e:
        log.debug(f"  shell sessions: {e}")
    try:
        r = conn.execute("SELECT COUNT(*) FROM alert_log WHERE created_at >= datetime('now', '-1 day') "
                         "AND severity IN ('warning', 'critical', 'error')").fetchone()
        out["unread"] = int(r[0] or 0)
    except Exception:
        out["unread"] = 0
    _cache.update(at=time.monotonic(), data=out)
    return out


# -- the middleware ------------------------------------------------------------------------------

_BODY = re.compile(rb"<body([^>]*)>", re.I)


def _is_app(headers: dict, query: str) -> bool:
    return "ATIPApp/" in headers.get("user-agent", "") or "app=1" in query


def wrap(body: bytes, path: str, query: str, root: str = "", app: bool = False, st: dict | None = None) -> bytes:
    """An older page's HTML with the shell put around it (its own markup untouched)."""
    if NATIVE_MARK.encode() in body[:4000]:
        return body
    m = _BODY.search(body)
    if not m:
        return body
    st = st if st is not None else light_state()
    top, bottom = chrome(path, query, st, root)
    attrs = m.group(1)
    if re.search(rb'class\s*=\s*"', attrs, re.I):
        attrs = re.sub(rb'class\s*=\s*"', b'class="ax-shelled ', attrs, count=1, flags=re.I)
    else:
        attrs += b' class="ax-shelled"'
    out = body[:m.start()] + b"<body" + attrs + b">" + top.encode() + b'<div id="ax-main">' + body[m.end():]
    i = out.lower().rfind(b"</body>")
    tail = b"</div>" + bottom.encode()
    out = out[:i] + tail + out[i:] if i >= 0 else out + tail
    h = out.lower().find(b"</head>")
    tags = head_tags(root, app).encode()
    return out[:h] + tags + out[h:] if h >= 0 else tags + out


class ShellMiddleware:
    """Pure ASGI: buffers an older page's HTML response (GET, 200, text/html, not compressed) and wraps it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET":
            return await self.app(scope, receive, send)
        root = scope.get("root_path") or ""
        path = scope.get("path") or "/"
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        query = (scope.get("query_string") or b"").decode("latin-1")
        if path.startswith(SKIP_PREFIXES) or "shell=0" in query:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        held = {"start": None, "pass": False, "chunks": []}

        async def capture(msg):
            if msg["type"] == "http.response.start":
                h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in msg.get("headers") or []}
                if msg.get("status") != 200 or "text/html" not in h.get("content-type", "") or h.get("content-encoding"):
                    held["pass"] = True
                    await send(msg)
                else:
                    held["start"] = msg
                return
            if msg["type"] != "http.response.body" or held["pass"]:
                await send(msg)
                return
            held["chunks"].append(msg.get("body", b""))
            if msg.get("more_body"):
                return
            body = b"".join(held["chunks"])
            try:
                body = wrap(body, path, query, root, _is_app(headers, query))
            except Exception as e:                       # never break a page over its frame
                log.warning(f"  shell wrap {path}: {e}")
            start = held["start"]
            hdrs = [(k, v) for k, v in start.get("headers") or [] if k.lower() != b"content-length"]
            hdrs.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": hdrs})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, capture)


def install(app) -> None:
    """/static and the shell middleware. Called once by dashboard/ux_routes.py."""
    from starlette.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="atip-static")
    app.add_middleware(ShellMiddleware)


# -- new pages -----------------------------------------------------------------------------------

def page(title: str, body: str, path: str, query: str = "", job: str = "", actions: str = "", root: str = "",
         app: bool = False, extra_head: str = "", mar: list | None = None) -> str:
    """A new page in the shell. `mar`: the module's Manage / Accounting / Reports links [(label, href, current)]."""
    from dashboard.security import page_token
    st = light_state()
    top, bottom = chrome(path, query, st, root)
    try:
        from dashboard.global_asia import status_banner_html
        banner = status_banner_html()
    except Exception:
        banner = ""
    mar_html = ""
    if mar:
        mar_html = '<nav class="ax-mar" aria-label="Manage, accounting and reports">' + "".join(
            f'<a href="{_e(root + h)}"{" aria-current=page" if cur else ""}>{_e(l)}</a>' for l, h, cur in mar) + "</nav>"
    return f"""<!DOCTYPE html><html lang="en-IN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{NATIVE_MARK}<meta name="atip-token" content="{_e(page_token())}">
<title>{_e(title)} · ATIP</title>{head_tags(root, app)}{extra_head}</head>
<body class="ax-native ax-shelled">{top}{banner}
<main id="ax-main" class="ax-page" tabindex="-1">
<div class="ax-head"><div><h1>{_e(title)}</h1>{f'<p class="ax-job">{_e(job)}</p>' if job else ""}</div>
<div class="ax-actions">{actions}</div></div>{mar_html}
{body}
</main>{bottom}</body></html>"""
