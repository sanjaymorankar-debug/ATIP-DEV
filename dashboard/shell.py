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

# Inline SVG icons (UX audit L2, 3 Nov 2026): 24 px grid, currentColor, aria-hidden -- the label beside each one
# stays text. They replace the text glyphs (◐ ⌂ ⇅ …) that rendered as half or tiny characters.
_SVG = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    "markets": '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    "research": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "trade": '<path d="M7 20V4"/><path d="M3 8l4-4 4 4"/><path d="M17 4v16"/><path d="M13 16l4 4 4-4"/>',
    "sessions": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
                '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "accounting": '<path d="M6 4h12"/><path d="M6 9h12"/><path d="M9 4c3.5 0 6 1.8 6 5s-2.5 5-6 5H7l8 7"/>',
    "reports": '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/>'
               '<path d="M8 13h8M8 17h5"/>',
    "lab": '<path d="M9 3h6"/><path d="M10 3v6l-5.4 9.7A1.5 1.5 0 0 0 5.9 21h12.2a1.5 1.5 0 0 0 1.3-2.3L14 9V3"/><path d="M7.5 15h9"/>',
    "wealth": '<path d="M21 12a9 9 0 1 1-9-9v9z"/><path d="M15 3.5A9 9 0 0 1 20.5 9H15z"/>',
    "settings": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
                '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "more": '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "bell": '<path d="M6 9a6 6 0 1 1 12 0c0 6 3 8 3 8H3s3-2 3-8"/><path d="M10 21a2.2 2.2 0 0 0 4 0"/>',
    "theme": '<path d="M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.6 6.6 0 0 0 10 10z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c.8-3.8 4-6 8-6s7.2 2.2 8 6"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "density": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9.5h18M3 14.5h18"/>',
}


def icon(name: str, size: int = 20) -> str:
    """One inline SVG icon; decorative (the control carries its own text or aria-label)."""
    return (f'<svg class="ax-svg" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            f'{_SVG.get(name, "")}</svg>')


# (key, label, icon, landing href, [(label, href, one-line job)])
NAV = [
    ("home", "Home", "home", "/home", [
        ("Today", "/home", "Is ATIP safe, running and making money today?")]),
    ("markets", "Markets", "markets", "/markets", [
        ("Markets overview", "/markets", "Every market screen in one place"),
        ("Market & FII/DII", "/market", "Index levels, breadth, FII / DII flows"),
        ("Market pulse", "/market-pulse", "Sector heat, movers and breadth right now"),
        ("Global & Asia", "/global-asia", "Asian sessions, global cues and the Asia sentiment score"),
        ("News & impact", "/?tab=news", "Headlines scored for their impact on your stocks"),
        ("Live feeds", "/m?tab=live", "Dhan real-time feed health"),
        ("Data platform", "/data-platform", "Data sources, freshness and quality")]),
    ("research", "Research", "research", "/", [
        ("ATIP Scores", "/", "Every stock's ATIP score with VPI, ZPI, RRI, MRI and CRI tabs"),
        ("Research", "/research", "One stock in depth"),
        ("Screener", "/screener", "Filter the universe by score, factor and price"),
        ("Signals", "/signals", "Signal explorer: today's and past signals"),
        ("Signal history", "/?tab=hist", "How past signals played out"),
        ("Options builder", "/options-builder", "Build and price option strategies")]),
    ("trade", "Trade", "trade", "/command", [
        ("Command Center", "/command", "Is it safe, what needs me, what is running"),
        ("Trade signals", "/command/signals", "Today's EOD signals with live prices"),
        ("Trade setup", "/command/setup", "Size a trade with its ₹ risk before sending"),
        ("Live monitor", "/command/monitor", "Open trades, stops and each session's cycle progress"),
        ("Journal", "/command/journal", "Per-session trades, win rate and R"),
        ("Paper P&L", "/command/pnl", "The paper account's P&L by day, week and month"),
        ("Orders & execution", "/trading", "Order book, fills and execution quality"),
        ("Baskets & SIP", "/baskets", "Baskets and stock SIPs (paper)"),
        ("Execution lab", "/execution-lab", "Slippage, TCA and order-type experiments")]),
    ("sessions", "Sessions", "sessions", "/sessions", [
        ("All sessions", "/sessions", "The ten sessions as mini-funds: gross vs actual P&L, cycles, floors")]),
    ("accounting", "Accounting", "accounting", "/accounting", [
        ("General ledger", "/accounting?tab=ledger", "Every rupee every session touched, double-entry"),
        ("Session ledger", "/accounting?tab=session", "One session's cash, line by line"),
        ("Charges register", "/accounting?tab=charges", "Charges by type, session and month"),
        ("Tax register", "/accounting?tab=tax", "Gains by tax head and the FY estimate"),
        ("Funds", "/accounting?tab=funds", "Virtual (paper) and actual (broker) money, apart"),
        ("Reconciliation", "/accounting?tab=recon", "ATIP's charges and cash against the broker's"),
        ("Transfers", "/accounting?tab=transfers", "Move money between sessions")]),
    ("reports", "Reports", "reports", "/reports", [
        ("Generate", "/reports", "Make any report now in HTML, CSV, Excel or PDF"),
        ("Schedules", "/reports?tab=schedules", "When each report runs, in IST"),
        ("Archive", "/reports?tab=archive", "Every report generated, to download again"),
        ("Custom report", "/reports?tab=custom", "Pick sections, sessions and dates")]),
    ("lab", "Strategy Lab", "lab", "/lab", [
        ("Run → compare → promote", "/lab", "Backtest, compare, then promote a strategy"),
        ("Strategies", "/strategies", "Your strategies and their status"),
        ("Strategy builder", "/strategy-builder", "Build a rule-based strategy"),
        ("Backtests", "/backtests", "Run and compare backtests"),
        ("Quant", "/quant", "Factor research and experiments"),
        ("ML", "/ml", "Models, versions and their metrics")]),
    ("wealth", "Wealth", "wealth", "/wealth", [
        ("Wealth", "/wealth", "Goals, allocation, rebalancing and the advisor")]),
    ("settings", "Settings & Admin", "settings", "/settings", [
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
TABBAR = [("Home", "home", "/home", "home"), ("Trade", "trade", "/command", "trade"),
          ("Sessions", "sessions", "/sessions", "sessions"), ("Reports", "reports", "/reports", "reports"),
          ("More", "more", "/more", "more")]
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


def leaf_of(path: str, query: str = "", title: str = "") -> tuple[str, str, str]:
    """(section key, section label, leaf): the breadcrumb, and the page name its <title> carries (UX audit L1).
    A page that renders itself (title given) is its own leaf unless a query picks one of its tabs; an older page is
    the rail item it sits under."""
    sec, page_label = section_of(path, query)
    sec_label = next((l for k, l, *_ in NAV if k == sec), "More" if sec == "more" else "")
    if title:
        exact = query and any(h == path + "?" + query for *_, kids in NAV for _, h, _ in kids)
        leaf = page_label if exact and page_label else title
    else:
        leaf = page_label or sec_label
    return sec, sec_label, leaf


def chrome(path: str, query: str, st: dict, root: str = "", title: str = "") -> tuple[str, str]:
    """(markup after <body>, markup before </body>) of the shell for one page."""
    sec, sec_label, leaf = leaf_of(path, query, title)
    page_label = section_of(path, query)[1]
    rail = []
    for key, label, ico, href, kids in NAV:
        on = key == sec
        sub = "".join(f'<a href="{_e(root + h)}"{" aria-current=page" if on and kl == page_label else ""}>{_e(kl)}</a>'
                      for kl, h, _ in kids) if len(kids) > 1 else ""
        rail.append(f'<div class="ax-sec{" on" if on else ""}"><a href="{_e(root + href)}"'
                    f'{" aria-current=true" if on else ""}><span class="ax-ico">{icon(ico)}</span>'
                    f'{_e(label)}</a>{f"<div class=ax-sub>{sub}</div>" if sub else ""}</div>')
    crumbs = (f'<b>{_e(sec_label)}</b>' + (f" › {_e(leaf)}" if leaf and leaf != sec_label else "")) if sec_label else ""
    env = st.get("env") or "PAPER"
    status = st.get("status") or "RUNNING"
    sym = {"RUNNING": "●", "STOPPED": "⏸", "RESUMING": "↻"}.get(status, "●")
    viewer = st.get("role") == "VIEWER"
    kill = ('<button class="ax-killbtn" data-ax-kill type="button" disabled>View only</button>' if viewer else
            f'<button class="ax-killbtn" data-ax-kill type="button" data-halted="{1 if st.get("halted") else 0}" '
            f'aria-haspopup="dialog">{"Halted" if st.get("halted") else "Kill switch"}</button>')
    fd = st.get("feed") or {}
    feed = (f'<span class="ax-pill ax-feed ax-hide-sm" data-ax-feed data-state="{_e(fd.get("state"))}" role="status" '
            f'title="Live price feed"{"" if fd else " hidden"}><span class="ax-dot" aria-hidden="true"></span>'
            f'<span class="ax-feedtext">{_e(fd.get("text"))}</span><span class="ax-feedshort" aria-hidden="true">'
            f'{_e(fd.get("short"))}</span></span>')
    top = f"""<a class="ax-skip" href="#ax-main">Skip to content</a>
<header class="ax-top" role="banner">
 <button class="ax-iconbtn ax-menu" type="button" aria-label="Open the menu" aria-expanded="false" aria-controls="ax-rail">{icon("menu")}</button>
 <span class="ax-crumbs">{crumbs}</span><span class="ax-spacer"></span>
 <button class="ax-search" type="button" data-ax-search aria-label="Search pages, symbols and commands (Ctrl K)">{icon("search", 18)} <span class="ax-searchlabel">Search</span> <kbd>Ctrl K</kbd></button>
 <span class="ax-pill ax-env ax-env-top" data-ax-env data-env="{_e(env)}" role="status">{_e(env)}</span>
 <span class="ax-pill ax-run" data-ax-run data-status="{_e(status)}" role="status" aria-live="polite">{sym} <span class="ax-runtext">{_e(st.get("banner") or "ATIP running")}</span></span>
 {feed}
 {kill}
 <span class="ax-bellwrap ax-hide-sm"><button class="ax-iconbtn" type="button" data-ax-bell aria-haspopup="true" aria-expanded="false" aria-controls="ax-bellpop" aria-label="Alerts: the latest five">{icon("bell")}<span class="ax-count" data-ax-unread hidden></span></button>
 <div class="ax-pop" id="ax-bellpop" role="region" aria-label="Latest alerts" hidden><div class="ax-pophead">Latest alerts <span class="ax-muted ax-small">IST</span></div><ul class="ax-poplist" data-ax-alerts><li class="ax-muted">Loading…</li></ul><a class="ax-popfoot" href="{_e(root)}/ops">All alerts and job health →</a></div></span>
 <button class="ax-iconbtn ax-hide-sm" type="button" data-ax-density aria-pressed="false" aria-label="Compact rows (denser tables)">{icon("density")}</button>
 <button class="ax-iconbtn" type="button" data-ax-theme aria-label="Theme: switch between light, dark and your device's setting">{icon("theme")}</button>
 <a class="ax-iconbtn ax-hide-sm" href="{_e(root)}/account" aria-label="Your profile">{icon("user")}</a>
</header>
<nav class="ax-rail" id="ax-rail" aria-label="Main"><a class="ax-brand" href="{_e(root)}/home"><span class="ax-logo">A</span>ATIP</a>{"".join(rail)}
 <div class="ax-railfoot">Press <span class="ax-kbd">?</span> for shortcuts · <span class="ax-kbd">Ctrl K</span> to search</div></nav>
<div class="ax-scrim"></div>"""
    tabs = "".join(f'<a href="{_e(root + h)}"{" aria-current=page" if k == sec else ""}><span class="ax-ico">'
                   f'{icon(i, 22)}</span>{_e(l)}</a>' for l, i, h, k in TABBAR)
    bottom = f"""<nav class="ax-tabbar" aria-label="Sections">{tabs}</nav>
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
<aside class="ax-drawer" id="ax-row-drawer" inert aria-labelledby="ax-row-h"><header><h2 id="ax-row-h">Details</h2><button class="ax-iconbtn" type="button" data-ax-close aria-label="Close">{icon("close")}</button></header><div class="ax-body"></div></aside>
<div id="ax-nav" hidden data-json="{_e(json.dumps(nav_index()))}"></div>
<div id="ax-state" hidden data-json="{_e(json.dumps(st, default=str))}"></div>
<script src="{_e(root)}/static/atip/atip.js?v={VERSION}" defer></script>"""
    return top, bottom


def head_tags(root: str = "", app: bool = False) -> str:
    return (f'<meta name="atip-root" content="{_e(root)}"><link rel="stylesheet" href="{_e(root)}/static/atip/atip.css?v={VERSION}">'
            "<script>(function(){var d=document.documentElement;try{var q=new URLSearchParams(location.search).get('theme');"
            "var t=q||localStorage.getItem('atip-theme');if(t==='dark'||t==='light')d.setAttribute('data-theme',t);"
            "if(localStorage.getItem('atip-density')==='compact')d.setAttribute('data-density','compact');}catch(e){}"
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
    out["feed"] = _feed_cached()
    out["halted"] = _flag_halt()
    if out["halted"]:
        try:
            from orders.risk import halted
            out["halt_reason"] = halted()[1] or None
        except Exception:
            pass
    return out


_feed_memo = {"at": 0.0, "data": None}


def _feed_cached() -> dict | None:
    """dashboard/ux_kit.feed_label for the top bar (UX audit H1), read at most every 10 s across page loads."""
    if _feed_memo["data"] is not None and time.monotonic() - _feed_memo["at"] < 10:
        return _feed_memo["data"]
    try:
        from db.schema import get_connection
        from dashboard.ux_kit import feed_label
        conn = get_connection()
        try:
            d = feed_label(conn)
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"  shell feed: {e}")
        d = None
    _feed_memo.update(at=time.monotonic(), data=d)
    return d


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
    try:                                   # the bell's preview (UX audit L3): the latest five, times in IST
        out["alerts"] = [{"at": str(a[0])[:16], "severity": a[1], "title": re.sub(r"<[^>]+>", "", str(a[2] or ""))[:120]}
                         for a in conn.execute("SELECT created_at, severity, title FROM alert_log ORDER BY id DESC LIMIT 5")]
    except Exception:
        out["alerts"] = []
    _cache.update(at=time.monotonic(), data=out)
    return out


# -- the middleware ------------------------------------------------------------------------------

_BODY = re.compile(rb"<body([^>]*)>", re.I)
# Inside the shell the rail is the only navigation (UX audit H2): an older page's own "Main" nav is kept for the bare
# ?shell=0 page but hidden here, and its "← Dashboard" link goes (the rail and the breadcrumb replace both).
_LEGACY_NAV = re.compile(rb'<nav aria-label="Main">', re.I)
_BACK = re.compile('<a href="/">(?:←|&larr;) Dashboard</a>'.encode())
_TITLE = re.compile(rb"<title>.*?</title>", re.I | re.S)


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
        attrs = re.sub(rb'class\s*=\s*"', b'class="ax-shelled ax-legacy ', attrs, count=1, flags=re.I)
    else:
        attrs += b' class="ax-shelled ax-legacy"'
    rest = _LEGACY_NAV.sub(b'<nav class="ax-legacy-nav" aria-label="Page links" hidden>', body[m.end():])
    rest = _BACK.sub(b"", rest)
    out = body[:m.start()] + b"<body" + attrs + b">" + top.encode() + b'<div id="ax-main">' + rest
    _, _, leaf = leaf_of(path, query)
    if leaf:
        out = _TITLE.sub(b"<title>" + html.escape(leaf + " · ATIP").encode() + b"</title>", out, count=1)
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
    top, bottom = chrome(path, query, st, root, title=title)
    leaf = leaf_of(path, query, title)[2] or title
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
<title>{_e(leaf)} · ATIP</title>{head_tags(root, app)}{extra_head}</head>
<body class="ax-native ax-shelled">{top}{banner}
<main id="ax-main" class="ax-page" tabindex="-1">
<div class="ax-head"><div><h1>{_e(title)}</h1>{f'<p class="ax-job">{_e(job)}</p>' if job else ""}</div>
<div class="ax-actions">{actions}</div></div>{mar_html}
{body}
</main>{bottom}</body></html>"""
