"""
UX audit fixes, 3 Nov 2026 (H1-H7, M1-M7, L1-L3; prompt ATIP_UX_Audit_Fix_Prompt_2026-11-03.md). The shell, the
ATIP Scores page, Today, Command Center and /m. The options builder and the other older pages have their own files:
tests/test_ux_audit_fix_2026_11_03_options.py and tests/test_ux_audit_fix_2026_11_03_legacy.py.
"""

import html as _html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    import dashboard.shell as sh
    init_db()
    monkeypatch.chdir(tmp_path)
    sh._cache.update(at=0.0, data=None)
    sh._feed_memo.update(at=0.0, data=None)
    c = TestClient(srv.app)
    from dashboard import security
    c.token = security.token()
    return c


def _tick(at: datetime, sym="ACME"):
    from db.schema import get_connection
    c = get_connection()
    c.execute("INSERT INTO live_quotes (symbol, ltp, prev_close, timestamp) VALUES (?, 100, 99, ?)",
              (sym, at.strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    c.close()


# ── H1: one feed label ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("market_open,age_s,state", [(True, 30, "live"), (True, 300, "stale"), (False, 540 * 60, "closed"),
                                                     (True, None, "none"), (False, None, "none")])
def test_feed_label_states(temp_db, market_open, age_s, state):
    from db.schema import get_connection, init_db
    from dashboard.ux_kit import feed_label
    init_db()
    now = datetime(2026, 11, 3, 18, 0, 0)
    if age_s is not None:
        _tick(now - timedelta(seconds=age_s))
    conn = get_connection()
    f = feed_label(conn, market_open, now=now)
    conn.close()
    assert f["state"] == state, f
    if state == "closed":
        assert f["text"] == "Market closed · last tick 09:00 IST" and "live" not in f["text"].lower()
    if state == "stale":
        assert f["text"].startswith("Feed stale") and f["stale"] is True and f["cutoff_seconds"] == 120
    if state == "live":
        assert f["text"].startswith("Live") and f["stale"] is False
    if state == "none":
        assert f["stale"] is market_open


def test_every_page_shows_the_same_feed_text(client, monkeypatch):
    """Market closed, last tick 540 min old: Today, the shell state, Command Center's API and /m all say the same."""
    import pipeline.scheduler as S
    monkeypatch.setattr(S, "is_market_hours", lambda: False)
    _tick(datetime.now() - timedelta(minutes=540))
    from dashboard.ux_kit import feed_label
    from db.schema import get_connection
    conn = get_connection()
    want = feed_label(conn)["text"]
    from dashboard.w38_page import mobile_summary
    assert mobile_summary(conn)["price_feed"]["text"] == want
    conn.close()
    assert want.startswith("Market closed · last tick ")
    assert client.get("/api/command/summary").json()["feed"]["text"] == want
    assert client.get("/api/v2/shell/state").json()["feed"]["text"] == want
    home = client.get("/home").text
    assert _html.escape(want) in home and "feed live" not in home
    cmd = client.get("/command").text
    assert "$('pFeed').textContent=f.text" in cmd and "'Live feed · '" not in cmd


def test_live_quotes_say_when_they_were_fetched(client, monkeypatch):
    import pandas as pd
    import data.dhan as D
    import pipeline.scheduler as S
    import dashboard.server as srv
    from db.schema import get_connection
    c = get_connection()
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score) VALUES ('ACME', '2026-10-23', 60)")
    c.commit()
    c.close()
    monkeypatch.setattr(S, "is_market_hours", lambda: True)
    monkeypatch.setattr(srv, "latest_scored_date", lambda: "2026-10-23")
    monkeypatch.setattr(D, "fetch_live_quotes",
                        lambda syms, *a: pd.DataFrame([{"symbol": "ACME", "ltp": 110.0, "prev_close": 100.0}]))
    r = client.get("/api/live-quotes")
    assert r.json() == {"ACME": {"ltp": 110.0, "chg_pct": 10.0, "prev_close": 100.0}}     # body unchanged
    assert re.fullmatch(r"\d\d:\d\d:\d\d", r.headers["x-atip-quotes-at"])
    page = client.get("/").text
    assert "cell.title='price at '+at+' IST'" in page


# ── H7: no browser alert()/confirm() in order flows ───────────────────────────────────────────

def test_no_native_dialogs_in_order_flows():
    for f in ("dashboard/server.py", "dashboard/command_page.py", "dashboard/command_more.py", "dashboard/command_routes.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\b(alert|confirm)\(", src), f
    js = (ROOT / "dashboard/static/atip/atip.js").read_text(encoding="utf-8")
    assert "AX.ask = function" in js and "AX.priceMoved = function" in js
    assert '"Place at " + AX.inr(c)' in js and 'cancel: "Cancel"' in js


def test_price_moved_flow_uses_the_dialog(client):
    page = client.get("/").text
    assert "if(await uiPriceMoved(e,(window._pendRules||{})[id]||{}))" in page
    assert "/confirm?force=true" in page
    cmd = client.get("/command").text
    assert "await AX.priceMoved(t,pr)" in cmd


# ── M5: the order ticket ──────────────────────────────────────────────────────────────────────

def test_order_ticket_layout_and_copy(client):
    page = client.get("/").text
    assert "python -m orders.rules --probe-broker" not in page and "checkBroker()" in page and ">Check broker<" in page
    for sid in ("mBAuto", "mConfirm"):
        assert re.search(rf'<input type="checkbox" role="switch" id="{sid}"', page), sid
        assert f'<label for="{sid}">' in page
    assert 'id="mConfWarn" role="alert" hidden' in page
    assert 'id="mRisk"' in page and "Money at risk" in page and "Estimated charges" in page
    costs = json.loads(_html.unescape(re.search(r'data-costs="([^"]*)"', page).group(1)))
    assert set(costs) == {"CNC", "INTRADAY"} and costs["CNC"]["stt_sell_pct"] > 0
    # checkbox labels hold their box (no label wrapping under it)
    assert '<label class="chk"><input type="checkbox" id="mBrk"' in page
    assert '<label class="chk"><input type="checkbox" id="mTrail"' in page


def test_broker_capability_route_reads_the_sdk_only(client):
    r = client.get("/api/orders/broker-capability")
    assert r.status_code == 200
    assert set(r.json()) == {"sdk_version", "supports_bracket", "supports_super_order", "supports_gtt", "notes"}


# ── M4: money ─────────────────────────────────────────────────────────────────────────────────

def test_one_money_format():
    from dashboard.server import money
    from dashboard.ux_kit import inr
    assert money(9945.5) == inr(9945.5) == "₹9,945.50"
    assert money(1231900) == "₹12,31,900.00"
    assert money(None) == money(0) == "—"
    css = (ROOT / "dashboard/static/atip/atip.css").read_text()
    assert ".ax-num { font-variant-numeric: tabular-nums; }" in css


def test_no_double_rupee(client):
    for p in ("/home", "/"):
        assert "₹ ₹" not in client.get(p).text and "₹</span>₹" not in client.get(p).text


def test_change_carries_an_arrow():
    from dashboard.server import chg_span
    assert "▼ -25.98" in chg_span(205.22, 231.20) and "▲ +74.80" in chg_span(1231.30, 1156.50)


# ── H2: one navigation ────────────────────────────────────────────────────────────────────────

SHELL_PAGES = ["/", "/home", "/sessions", "/command", "/command/setup", "/trading", "/options-builder", "/screener",
               "/signals", "/market", "/research", "/wealth", "/accounting", "/reports", "/quant", "/strategy-builder",
               "/m", "/global-asia", "/market-pulse", "/ml", "/backtests", "/strategies"]


@pytest.mark.parametrize("path", SHELL_PAGES)
def test_one_main_nav_and_no_back_link(client, path):
    h = client.get(path).text
    assert h.count('<nav class="ax-rail" id="ax-rail" aria-label="Main">') == 1, path
    assert len(re.findall(r'<nav[^>]*aria-label="Main"', h)) == 1, path
    assert "← Dashboard" not in h, path


def test_bare_page_keeps_its_own_links(client):
    assert 'class="topbar legacy-only"' in client.get("/?shell=0").text
    assert '<nav aria-label="Main">' in client.get("/command?shell=0").text


# ── H3: the older pages follow the theme ──────────────────────────────────────────────────────

def test_legacy_bridge(client):
    css = (ROOT / "dashboard/static/atip/atip.css").read_text()
    bridge = css.split("legacy bridge")[1]
    for v in ("--bg: var(--ax-bg)", "--panel: var(--ax-surface)", "--muted: var(--ax-muted)", "--text: var(--ax-text)"):
        assert v in bridge, v
    h = client.get("/").text
    assert re.search(r'<body[^>]*class="ax-shelled ax-legacy', h)
    style = h.split("<style>")[1].split("</style>")[0]
    assert not re.search(r"background:#0f172a", style), "no hard-coded dark page background"


# ── L1: <title> = the breadcrumb's last part ─────────────────────────────────────────────────

def _routes():
    from dashboard.shell import NAV
    hrefs = [h for *_, kids in NAV for _, h, _ in kids]
    return sorted(set(h for h in hrefs if not h.startswith("/m")))


def test_title_matches_breadcrumb_on_every_page(client):
    bad = []
    for path in _routes() + ["/more", "/lab", "/markets"]:
        r = client.get(path)
        if r.status_code != 200 or "ax-crumbs" not in r.text:
            continue
        crumbs = re.search(r'<span class="ax-crumbs">(.*?)</span>', r.text).group(1)
        leaf = _html.unescape(re.sub(r"<[^>]+>", "", crumbs).split(" › ")[-1]).strip()
        title = _html.unescape(re.search(r"<title>(.*?)</title>", r.text, re.S).group(1)).strip()
        if leaf and title != f"{leaf} · ATIP":
            bad.append((path, leaf, title))
    assert not bad, bad
    assert "<title>ATIP Scores · ATIP</title>" in client.get("/").text


# ── L2: icons ─────────────────────────────────────────────────────────────────────────────────

def test_shell_icons_are_svg(client):
    h = client.get("/home").text
    top = h.split('<header class="ax-top"')[1].split("</header>")[0]
    rail = h.split('<nav class="ax-rail"')[1].split("</nav>")[0]
    for glyph in ("☰", "⌕", "◐", "🔔", "◯", "⌂", "◔", "⇅", "▦", "▤", "⚗", "◈", "⚙"):
        assert glyph not in top and glyph not in rail, glyph
    assert top.count('<svg class="ax-svg"') >= 6 and rail.count('<svg class="ax-svg"') == 10
    assert all('aria-hidden="true"' in s for s in re.findall(r'<svg class="ax-svg"[^>]*>', top + rail))


# ── H4: the Scores page before its table ──────────────────────────────────────────────────────

def test_scores_page_is_lighter(client):
    h = client.get("/").text
    assert h.count('<div class="ss-item"') == 4 and 'id="ssMore"' in h            # one status strip
    row = h.split('<div class="kpi-row"')[1].split('<details class="kpi-more">')[0]
    assert row.count('<div class="kpi">') == 5                                      # five KPIs, the rest folded
    assert '<details class="section sector">' in h                                  # sectors collapsed
    assert re.findall(r'data-g="(\w+)"', h) == ["scores", "lists", "flows", "activity"]
    assert "var TG={scores:['scores','port'],lists:['vpi','zpi','rri','mri','cri'],flows:['fiidii','news','asia']," \
           "activity:['hist','orders','livefeeds']};" in h


# ── H5: what the scores mean ──────────────────────────────────────────────────────────────────

def test_score_headers_are_explained(client):
    from dashboard.score_glossary import band, describe
    h = client.get("/").text
    head = h.split('<table id="st"><thead>')[1].split("</thead>")[0]
    for k in ("ATIP", "VPI", "MRI", "RRI", "ZPI", "CRI", "ACS", "Beta"):
        assert re.search(rf'<abbr title="[^"]+">{re.escape(k)}', head), k
    assert '<details class="ax-legendbox" id="scoreLegend"><summary>What do the scores mean?</summary>' in h
    assert "BUY: ATIP ≥ 65" in h
    assert (band("ATIP", 72), band("ATIP", 50), band("ATIP", 30)) == ("strong", "neutral", "weak")
    assert (band("CRI", 20), band("CRI", 80)) == ("strong", "weak")
    assert describe("CRI").startswith("Crash-Risk Index")


# ── H6 / M6: sizes, contrast tokens, names ────────────────────────────────────────────────────

def test_scores_css_has_no_text_under_12px(client):
    style = client.get("/").text.split("<style>")[1].split("</style>")[0]
    sizes = [float(x) for x in re.findall(r"font-size:(\d+(?:\.\d+)?)px", style)]
    assert sizes and min(sizes) >= 12


def test_drawer_is_inert_and_controls_are_named(client):
    h = client.get("/home").text
    assert '<aside class="ax-drawer" id="ax-row-drawer" inert' in h and 'id="ax-row-drawer" aria-hidden' not in h
    s = client.get("/").text
    for sel in ('<select id="sf" aria-label=', '<select id="slim" aria-label=', '<select id="hsf" aria-label=',
                '<select id="mTT" aria-label=', '<select id="mQT" aria-label=', '<select id="mOT" aria-label=',
                '<input id="srch" placeholder="Search symbol or company…" aria-label=', 'id="mLimit" class="hide" aria-label='):
        assert sel in s, sel
    js = (ROOT / "dashboard/static/atip/atip.js").read_text()
    assert 'el.setAttribute("inert", "")' in js and 'el.removeAttribute("inert")' in js


# ── M7: loading, empty and error on Command Center ───────────────────────────────────────────

def test_command_center_states(client):
    h = client.get("/command").text
    for tid in ("pend", "sigs", "tiles", "recent"):
        assert re.search(rf'id="{tid}"[^>]*aria-busy="true"', h), tid
    assert "ax-skel" in h
    assert "No BUY or SELL signal in the scores of" in h and "Retrying every 15 s." in h
    assert "Nothing needs you right now. A rule that triggers" in h


# ── L3: the bell and the countdown ────────────────────────────────────────────────────────────

def test_bell_previews_five_alerts_in_ist(client):
    from db.schema import get_connection
    c = get_connection()
    for i in range(7):
        c.execute("INSERT INTO alert_log (created_at, category, severity, title, message) VALUES (?, 'x', 'warning', ?, 'm')",
                  (f"2026-11-03 0{i}:15:00", f"<b>alert {i}</b>"))
    c.commit()
    c.close()
    a = client.get("/api/v2/shell/state").json()["alerts"]
    assert len(a) == 5 and a[0]["title"] == "alert 6" and a[0]["at"] == "2026-11-03 06:15"
    h = client.get("/home").text
    assert 'id="ax-bellpop" role="region" aria-label="Latest alerts" hidden' in h and "data-ax-alerts" in h
    js = (ROOT / "dashboard/static/atip/atip.js").read_text()
    assert "' IST</time>" in js


def test_countdown_is_one_line(client):
    h = client.get("/").text
    assert 'id="clk" class="nowrap">Refresh in 5:00<' in h
    assert "el.textContent='Refresh in '+Math.floor(t/60)+':'" in h


# ── the page's own script still parses ───────────────────────────────────────────────────────

def test_scores_and_command_scripts_parse(client, tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for path in ("/", "/command", "/command/setup", "/home"):
        for i, js in enumerate(re.findall(r"<script>(.*?)</script>", client.get(path).text, re.S)):
            f = tmp_path / f"s{i}.js"
            f.write_text(js)
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (path, r.stderr[:300])
    f = tmp_path / "atip.js"
    f.write_text((ROOT / "dashboard/static/atip/atip.js").read_text())
    assert subprocess.run([node, "--check", str(f)]).returncode == 0


# ── owner decision 3 Nov 2026: a Trade of the Day only on a BUY signal ────────────────────────

def _tod(sym, signal, td="2026-11-02"):
    from db.schema import get_connection
    c = get_connection()
    c.execute("INSERT INTO ai_scores (symbol, date, `signal`, atip_score, acs, is_tod) VALUES (?,?,?,?,?,1)",
              (sym, td, signal, 58.8, 70))
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "VALUES (?,?,300,300,300,300,1000,'EQ')", (sym, td))
    c.execute("INSERT INTO predictions (pred_date, symbol, `signal`, atip_score, is_tod) VALUES (?,?,?,?,1)",
              (td, sym, signal, 58.8))
    c.commit()
    c.close()


@pytest.mark.parametrize("signal", ["HOLD", "WAIT", "SELL"])
def test_no_trade_of_the_day_without_a_buy(client, monkeypatch, signal):
    import dashboard.server as srv
    from db.schema import get_connection
    from dashboard.w38_page import mobile_summary
    _tod("PETRONET", signal)
    monkeypatch.setattr(srv, "latest_scored_date", lambda: "2026-11-02")
    page = srv.build_html(srv.generate_state("2026-11-02"))
    assert "No BUY signal for today" in page and "0 BUY · " in page
    assert '<div class="tod-card" data-sym="PETRONET" hidden>' in page and "PETRONET</b>⭐" not in page
    assert "No BUY signal for today (2026-11-02)" in client.get("/home").text
    c = get_connection()
    assert mobile_summary(c)["tod"] is None
    c.close()


def test_a_buy_pick_is_still_shown(client, monkeypatch):
    import dashboard.server as srv
    _tod("ACME", "BUY")
    monkeypatch.setattr(srv, "latest_scored_date", lambda: "2026-11-02")
    page = srv.build_html(srv.generate_state("2026-11-02"))
    assert "No BUY signal for today" not in page and '<div class="tod-card" data-sym="ACME">' in page
    assert "ACME</b>⭐" in page
    home = client.get("/home").text
    assert 'href="/?stock=ACME">ACME</a>' in home and "No BUY signal for today" not in home


def test_telegram_says_no_buy_signal(temp_db, monkeypatch):
    from db.schema import init_db
    import alerts.telegram as T
    init_db()
    _tod("PETRONET", "HOLD")
    sent = []
    monkeypatch.setattr(T, "send_telegram", lambda msg, *a, **k: sent.append(msg) or True)
    assert T.send_tod_alert("2026-11-02") is True
    assert len(sent) == 1 and "No BUY signal for today" in sent[0] and "Stop Loss" not in sent[0]
