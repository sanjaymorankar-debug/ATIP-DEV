"""
UX audit of 3 Nov 2026 (axe-core 4.10 baseline), the older pages: /trading, /quant, /market, /ml, /strategy-builder,
/data-platform, /execution-lab, /strategies, /backtests, /account, /admin, /admin/console, /wealth, /global-asia,
/brokers, /baskets, /assistant.

    M6  every <select> / <input> / <textarea> has an accessible name (<label for>, a wrapping <label>, aria-label);
        every horizontally scrollable table region is keyboard reachable (tabindex="0" role="region" aria-label)
    M3  plain names instead of config keys on /trading's risk limits (and /quant's data dependencies); no wave code
        (W4, W25, W40 ...) in an <h1>-<h3> or the <title>
    M7  /trading and /quant: a loading (ax-skel shimmer), empty (why + what to do) and error (message + what to try)
        state for every table; no bare "none" rows
    M1  tables scroll inside their own region (no page-wide overflow); "esc is not defined" on /account, /admin and
        /admin/console (the shared script came after the page's own) is gone
    H3  12px minimum text in the pages' own CSS

Pages are read with ?shell=0: the shell (dashboard/shell.py) is checked by its own tests.
"""

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tools import load_test as LT

ROOT = Path(__file__).resolve().parents[1]

PAGES = ["/trading", "/quant", "/market", "/ml", "/strategy-builder", "/data-platform", "/execution-lab", "/strategies",
         "/backtests", "/account", "/admin", "/admin/console", "/wealth", "/global-asia", "/brokers", "/baskets",
         "/assistant"]

SOURCES = ["execution_page.py", "quant_page.py", "market_page.py", "ml_page.py", "strategy_builder_page.py", "w35_page.py",
           "w34_page.py", "strategy_page.py", "backtest_page.py", "enterprise_page.py", "wealth_page.py", "global_asia.py",
           "w37_page.py", "w39_retail_page.py", "assistant_page.py"] + \
          [f"wealth_ui/{p.name}" for p in sorted((ROOT / "dashboard" / "wealth_ui").glob("*_tab.py"))]


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import init_db
    init_db()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    with LT.block_network():
        yield TestClient(server.app, raise_server_exceptions=False)


def _page(client, path):
    r = client.get(path + "?shell=0")
    assert r.status_code == 200, (path, r.status_code)
    return r.text


# ── M6: every control has a name ─────────────────────────────────────────────────────────────────────────

_CONTROL = re.compile(r"<(select|input|textarea)\b([^>]*)>", re.I)
_SKIP_TYPES = {"hidden", "button", "submit", "reset", "image"}


def _attr(attrs, name):
    m = re.search(r'\b' + name + r'\s*=\s*("([^"]*)"|\'([^\']*)\')', attrs, re.I)
    return None if not m else (m.group(2) if m.group(2) is not None else m.group(3))


def _unnamed_controls(html):
    """Every <select>/<input>/<textarea> -- in the markup and in the scripts' template strings -- with no aria-label /
    aria-labelledby, no <label for=its id> and no open <label> around it."""
    label_for = set(re.findall(r'<label\b[^>]*\bfor\s*=\s*["\']([^"\']+)["\']', html, re.I))
    bad = []
    for m in _CONTROL.finditer(html):
        tag, attrs = m.group(1).lower(), m.group(2)
        if tag == "input" and (_attr(attrs, "type") or "").lower() in _SKIP_TYPES:
            continue
        if _attr(attrs, "aria-label") or _attr(attrs, "aria-labelledby"):
            continue
        cid = _attr(attrs, "id")
        if cid and cid in label_for:
            continue
        before = html[:m.start()]
        if before.lower().rfind("<label") > before.lower().rfind("</label>"):
            continue                                                  # wrapped in a <label>
        bad.append(m.group(0)[:120])
    return bad


@pytest.mark.parametrize("path", PAGES)
def test_every_select_and_input_has_an_accessible_name(client, path):
    assert _unnamed_controls(_page(client, path)) == []


def test_the_controls_the_audit_counted_are_named(client):
    """Strategy builder 12, Market 3, Trading 2, Quant 1 unlabelled selects in the audit."""
    t = _page(client, "/trading")
    assert '<label for="pbook">' in t and '<label for="andays">' in t
    assert '<label for="key">' in _page(client, "/quant")
    m = _page(client, "/market")
    assert all(f'<label for="{i}">' in m for i in ("ser", "days", "kind", "sym"))
    b = _page(client, "/strategy-builder")
    assert 'id="emode" aria-label="Entry rule"' in b and 'id="xmode" aria-label="Exit rule"' in b
    for part in ("feature", "operator", "compare with", "value"):            # each condition row's controls
        assert "${L('" + part + "')}" in b
    assert 'aria-label="${L(\'side\')}"' in _page(client, "/baskets")       # each basket leg's controls


# ── M6: scroll regions are reachable by keyboard ────────────────────────────────────────────────────────

_SCROLLER = re.compile(r'<div class="(tw|tw [^"]*|ga-scroll|lf-wrap)"([^>]*)>')


@pytest.mark.parametrize("path", PAGES)
def test_scroll_wrappers_are_focusable_named_regions(client, path):
    html = _page(client, path)
    if path == "/global-asia":
        # the news panel's script is dashboard/news_impact_view.py's (not this page's): the page names its scroll
        # boxes as regions at run time instead (a MutationObserver over <main>)
        from dashboard.news_impact_view import NI_JS
        assert "querySelectorAll('.ga-scroll:not([tabindex])')" in html and "MutationObserver(reg)" in html
        html = html.replace(NI_JS, "")
    found = _SCROLLER.findall(html)
    if path not in ("/account", "/admin", "/admin/console", "/assistant", "/global-asia"):
        assert found, f"{path} has no table scroll region"
    for cls, attrs in found:
        assert 'tabindex="0"' in attrs and 'role="region"' in attrs and "aria-label=" in attrs, (path, cls, attrs)


@pytest.mark.parametrize("path", ["/trading", "/quant", "/market", "/ml", "/data-platform", "/execution-lab"])
def test_scroll_boxes_no_longer_scroll_themselves(client, path):
    """.scroll only bounds the height of the region inside it; the focusable .tw region is what scrolls."""
    css = _page(client, path).split("</style>")[0]
    assert not re.search(r"\.scroll\{[^}]*overflow", css)
    assert re.search(r"\.tw\{overflow:auto;max-width:100%", css)


def test_shared_live_feeds_and_asia_tables_are_regions_and_keep_their_ids():
    from dashboard.global_asia import ASIA_JS, asia_tab_html, status_banner_html
    from dashboard.market_page import LIVE_FEEDS_JS, live_tab_html
    assert 'class="lf-wrap" tabindex="0" role="region" aria-label="Live feed status"' in LIVE_FEEDS_JS
    tab = live_tab_html()
    assert 'id="livefeeds"' in tab and 'id="lfBox"' in tab and "atipLoadFeeds('lfBox')" in tab
    assert ASIA_JS.count('class="ga-scroll" tabindex="0" role="region"') == 2
    asia = asia_tab_html()
    assert 'id="asia"' in asia and 'id="gaBox"' in asia
    banner = status_banner_html()
    for i in ("atipOpBanner", "atipTokenBanner", "atipRoleBanner"):
        assert f'id="{i}"' in banner


# ── M3: no wave codes in headings or titles; plain names for config keys ───────────────────────────────

_HEADING = re.compile(r"<(h[1-3]|title)\b[^>]*>(.*?)</\1>", re.I | re.S)


@pytest.mark.parametrize("path", PAGES)
def test_no_wave_codes_in_headings_or_title(client, path):
    html = _page(client, path)
    for tag, inner in _HEADING.findall(html):
        text = re.sub(r"<[^>]*>", "", inner)
        assert not re.search(r"\bW\d+\b", text), (path, tag, text[:120])


def test_risk_limits_show_plain_labels_with_the_key_in_a_tooltip(client):
    t = _page(client, "/trading")
    assert "max_position_pct:'Max position size, % of equity'" in t
    assert "daily_loss_limit_pct:'Daily loss limit, % of equity'" in t
    lim = re.search(r"sec\('lim'.*?\}\),\n", t, re.S).group(0)
    assert 'title="Config key: ${esc(k)}">${esc(human(k))}' in lim         # the label, the key as the tooltip
    assert "<td>${k}</td>" not in lim                                     # the old raw key cell
    assert "W1 limits" not in lim


def test_quant_data_dependencies_have_plain_labels(client):
    q = _page(client, "/quant")
    assert "fundamentals:'Fundamentals'" in q and "events_pending:'Event types not collected yet'" in q
    assert 'title="Config key: ${esc(k)}"' in q
    assert "${k}: ${esc(typeof v" not in q                                # the old "key: value" line


# ── M7: loading / empty / error states on /trading and /quant ───────────────────────────────────────────

@pytest.mark.parametrize("path", ["/trading", "/quant"])
def test_tables_have_loading_empty_and_error_states(client, path):
    html = _page(client, path)
    js = html.split("<script>", 1)[1]
    assert 'class="ax-skel"' in js and 'aria-busy="true"' in js             # loading: shimmer rows
    assert "function skel()" in js and "skel();" in js
    assert "function fail(" in js and 'role="alert"' in js                  # error: the message + what to try
    assert "Check that the ATIP server is running, then reload this page." in js
    assert "async function sec(" in js                                      # one failing read never blanks the rest
    assert 'class="empty"' in js                                            # empty: a sentence, not "none"
    assert 'class="muted">none</td>' not in html and ">none<" not in html
    containers = re.findall(r'<div id="(\w+)"[^>]*\bdata-load\b', html)
    assert len(containers) >= (15 if path == "/trading" else 7), containers


def test_trading_empty_states_say_why_and_what_to_do(client):
    t = _page(client, "/trading")
    assert "No position intents yet. Strategies write them in the post-market run (16:45 IST on trading days)" in t
    assert "No strategy decisions yet." in t and "No risk decisions yet. Press “Evaluate risk on pending intents”" in t
    assert "No orders yet." in t and "No fills yet." in t and "No open positions in the paper book." in t
    # the data reads the risk dashboard depends on are still plain j('/api/...') calls (tests/test_w39_qa_suite.py)
    for u in ("/api/risk/limits", "/api/risk/exposure", "/api/risk/decisions?limit=100", "/api/risk/portfolio?book=",
              "/api/risk/emergency-exit?book=", "/api/portfolio/risk-model?book="):
        assert f"j('{u}'" in t, u


# ── M1: the account / admin pages' scripts run ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/account", "/admin", "/admin/console"])
def test_shared_helpers_load_before_the_page_script(client, path):
    html = _page(client, path)
    head, body = html.split("</head>", 1)
    assert "const esc=" in head and "async function j(" in head and "const table=" in head
    assert "const esc=" not in body                                         # defined once, before load() runs
    assert "load()" in body


def test_enterprise_read_errors_show_at_the_top_of_the_page():
    from dashboard.enterprise_page import render_account, render_admin
    for html in (render_account(), render_admin()):
        assert "load().catch(pageError)" in html and "function pageError(" in html


# ── H3: 12px minimum in the pages' own CSS ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src", SOURCES)
def test_no_text_smaller_than_12px(src):
    text = (ROOT / "dashboard" / src).read_text(encoding="utf-8")
    small = [m.group(0) for m in re.finditer(r"font-size\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)(?:px)?", text)
             if float(m.group(1)) < 12]
    assert small == [], (src, small)


def test_status_pills_use_paired_theme_tokens(client):
    """A dark-only fill with inherited text failed in the light theme: each status pill pairs a background token with
    its text token."""
    for path in ("/trading", "/quant", "/ml", "/strategies", "/execution-lab"):
        css = _page(client, path).split("</style>")[0]
        for m in re.finditer(r"\.pill\.[A-Z_]+[^{]*\{([^}]*)\}", css):
            assert "background:var(--" in m.group(1) and "color:var(--" in m.group(1), (path, m.group(0))
            bare = re.sub(r"var\([^)]*\)", "", m.group(1))                  # a hex fallback inside var() is fine
            assert not re.search(r"#[0-9a-f]{3,6}", bare, re.I), (path, m.group(0))


# ── every inline script still parses (data travels in data-json attributes, never in JSON script tags) ──

@pytest.mark.parametrize("path", PAGES)
def test_page_scripts_parse(client, path, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = _page(client, path)
    assert '<script type="application/json"' not in html
    scripts = [s for s in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S) if s.strip()]
    assert scripts
    for i, js in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (path, i, r.stderr[:400])


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v and "${" not in v:
                self.ids.append(v)


@pytest.mark.parametrize("path", PAGES)
def test_static_ids_are_unique(client, path):
    """Labels point at ids: two elements with one id would name the wrong control."""
    p = _Ids()
    html = _page(client, path)
    p.feed(re.sub(r"<script\b.*?</script>", "", html, flags=re.S))
    dup = {i for i in p.ids if p.ids.count(i) > 1}
    assert dup == set(), (path, dup)
