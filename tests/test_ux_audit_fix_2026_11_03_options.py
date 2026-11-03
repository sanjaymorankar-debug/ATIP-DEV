"""
UX audit fixes of 3 Nov 2026 on the research pages (dashboard/w39_page.py, routes in dashboard/w39_routes.py):

    M2  the options builder: a visible <label for> on every field, the symbol list from the stored chains, presets
        (iron condor, bull call spread, bear put spread, ...), results without pressing Analyse (payoff chart, net
        Greeks, max loss, a Beginner / Intermediate / Advanced tier from quant.options_strategy.suitability), an empty
        state, and no order button anywhere
    M6  every control on /options-builder, /screener, /signals, /research and /market-pulse has an accessible name;
        every table scrolls inside its own focusable, labelled region (M1)
    M3  no wave code (W39, ...) in an <h1>-<h3> or the <title>
    H5  the screener carries the score legend and <abbr title> on score columns (dashboard/score_glossary.py)
    M7  loading (.ax-skel), empty (why and what to do) and error states; never a bare "none"
    H3 / H6  colours are shell tokens (var(--ax-..., old hex)), no text under 12px
"""

import datetime as dt
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from quant import options_strategy as O

PAGES = {"/options-builder": "render_options", "/screener": "render_screener", "/signals": "render_signals",
         "/research": "render_research", "/market-pulse": "render_pulse"}
WAVE = re.compile(r"\bW\d+[A-Za-z]?\b")


def _render(fn: str) -> str:
    from dashboard import w39_page as P
    return getattr(P, fn)("tok")


class _Markup(HTMLParser):
    """Static markup only (the parser does not look inside <script>): fields, labels, headings, buttons."""

    def __init__(self):
        super().__init__()
        self.fields, self.label_for, self.in_label, self.heads, self.cur, self.buttons, self.btn = [], set(), 0, [], None, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.fields.append((tag, a.get("id"), a.get("aria-label"), self.in_label > 0))
        if tag in ("h1", "h2", "h3", "title"):
            self.cur = [tag, ""]
        if tag == "button":
            self.btn = a.get("aria-label") or ""

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label -= 1
        if self.cur and tag == self.cur[0]:
            self.heads.append(tuple(self.cur))
            self.cur = None
        if tag == "button" and self.btn is not None:
            self.buttons.append(self.btn.strip())
            self.btn = None

    def handle_data(self, d):
        if self.cur:
            self.cur[1] += d
        if self.btn is not None:
            self.btn += d


def _parse(h: str) -> _Markup:
    p = _Markup()
    p.feed(h)
    return p


# ── M6 / M2: names on every control ─────────────────────────────────────────

@pytest.mark.parametrize("fn", PAGES.values())
def test_every_static_field_has_a_label(fn):
    p = _parse(_render(fn))
    assert p.fields, fn
    bare = [f for f in p.fields if not (f[2] or f[3] or (f[1] and f[1] in p.label_for))]
    assert not bare, (fn, bare)
    assert all(b for b in p.buttons), (fn, "a button with no name")


@pytest.mark.parametrize("fn", PAGES.values())
def test_controls_built_in_script_are_named_too(fn):
    """A <select>/<input> written by the page's script carries an id with a <label for> beside it, or an aria-label."""
    h = _render(fn)
    js = "\n".join(re.findall(r"<script>(.*?)</script>", h, re.S))
    for m in re.finditer(r"<(select|input|textarea)\b([^>]*)>", js):
        attrs = m.group(2)
        if "aria-label=" in attrs or 'type="hidden"' in attrs:
            continue
        idm = re.search(r'id="([^"]+)"', attrs)
        assert idm, (fn, m.group(0)[:120])
        assert f'<label for="{idm.group(1)}"' in js, (fn, idm.group(1))


def test_the_options_form_has_a_visible_label_for_each_field():
    h = _render("render_options")
    for fid, text in (("sym", "Underlying"), ("spot", "Spot price"), ("lot", "Lot size"), ("exp", "Expiry"),
                      ("w", "Wing width"), ("td", "Value the P&amp;L on"), ("tpl", "Other strategies")):
        assert re.search(rf'<label for="{fid}">{re.escape(text)}', h), fid
    for k in ("side", "kind", "strike", "expiry", "lots", "premium"):          # each leg's fields
        assert f"<label for=\"${{id('{k}')}}\">" in h and f"id=\"${{id('{k}')}}\"" in h, k
    assert 'list="symlist"' in h and "/api/options/symbols" in h               # searchable symbol list


# ── M2: presets, live results, tier, no order button ───────────────────────

def test_presets_are_on_the_page_and_exist():
    h = _render("render_options")
    for key in ("iron_condor", "bull_call_spread", "bear_put_spread", "long_call"):
        assert f'data-tpl="{key}"' in h and key in O.TEMPLATES
    assert "/api/options/build" in h


def test_results_follow_the_legs_without_pressing_analyse():
    h = _render("render_options")
    assert "TM=setTimeout(analyse,300)" in h                                    # debounced, ~300 ms
    for k in ("strike", "lots", "premium"):
        assert f"oninput=\"upd(${{i}},'{k}',this.value)\"" in h
    assert "function upd(" in h and "EDITED=true;schedule()}" in h
    assert "'X-ATIP-Token':TOKEN" in h                                           # the page's own token header
    assert 'onclick="analyse()">Analyse</button>' in h                           # the button still works
    for s in ("Three steps to a payoff", "Pick the underlying", "Add legs", "Read the result"):
        assert s in h                                                            # empty state
    for s in ("Max loss", "Unlimited", "Δ Delta", "Γ Gamma", "Θ Theta", "ν Vega", "Breakevens", "su.tier", "su.reason"):
        assert s in h
    assert 'role="img" aria-label="${esc(lab)}"' in h and "<title>${esc(lab)}</title>" in h
    assert "sv-zero" in h and "sv-be" in h and "stroke-width:2" in h


def test_no_order_or_trade_button_on_the_options_builder():
    h = _render("render_options")
    p = _parse(h)
    js_buttons = re.findall(r"<button\b[^>]*>(.*?)</button>", h, re.S)
    for text in p.buttons + js_buttons:
        assert not re.search(r"\b(order|trade|execute|place|send|buy now|sell now)\b", text, re.I), text
    for url in ("/api/orders", "/api/trading", "/api/brokers", "/api/command", "/order"):
        assert url not in h, url
    assert "nothing is ordered" in h


@pytest.mark.parametrize("template,tier", [
    ("long_call", "Beginner"), ("long_put", "Beginner"), ("long_straddle", "Beginner"), ("long_strangle", "Beginner"),
    ("bull_call_spread", "Intermediate"), ("bear_put_spread", "Intermediate"), ("bull_put_spread", "Intermediate"),
    ("bear_call_spread", "Intermediate"), ("iron_condor", "Intermediate"), ("iron_butterfly", "Intermediate"),
    ("long_call_butterfly", "Intermediate"), ("protective_put", "Intermediate"),
    ("short_straddle", "Advanced"), ("short_strangle", "Advanced"), ("call_ratio_spread", "Advanced"),
    ("short_put", "Advanced"), ("short_call", "Advanced"), ("covered_call", "Advanced"),
])
def test_suitability_tier_of_every_template(template, tier):
    legs = O.build(template, 24000, "2026-11-24", symbol="NIFTY")
    s = O.suitability(legs)
    assert s["tier"] == tier and s["level"] == O.TIERS.index(tier) + 1 and s["reason"]


def test_suitability_reads_expiries_lots_and_the_analysis():
    near, far = "2026-11-10", "2026-12-29"
    cal = [{"kind": "CE", "side": "SELL", "strike": 24000, "expiry": near}, {"kind": "CE", "side": "BUY", "strike": 24000, "expiry": far}]
    rev = [{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": near}, {"kind": "CE", "side": "SELL", "strike": 24000, "expiry": far}]
    assert O.suitability(cal)["tier"] == "Intermediate"                          # the bought leg outlives the sold one
    assert O.suitability(rev)["tier"] == "Advanced" and O.suitability(rev)["uncovered_up_lots"] == 1
    two_short = [{"kind": "PE", "side": "SELL", "strike": 23500, "expiry": near, "lots": 3},
                 {"kind": "PE", "side": "BUY", "strike": 23000, "expiry": near, "lots": 1}]
    s = O.suitability(two_short)
    assert s["tier"] == "Advanced" and s["uncovered_down_lots"] == 2 and "sharp fall" in s["reason"]
    assert O.suitability([{"kind": "FUT", "side": "SELL", "expiry": near}])["tier"] == "Advanced"
    covered = [{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": near}]
    assert O.suitability(covered, {"unlimited_loss": True})["tier"] == "Advanced"
    assert O.suitability([])["tier"] is None and O.suitability([{"kind": "XX"}])["level"] == 0
    assert "premium you pay" in O.suitability(covered)["reason"]


# ── M3: no wave codes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", PAGES.values())
def test_no_wave_code_in_headings_or_the_title(fn):
    h = _render(fn)
    for tag, text in re.findall(r"<(h[1-3]|title)\b[^>]*>(.*?)</\1>", h, re.S):    # markup and script templates
        assert not WAVE.search(re.sub(r"<[^>]+>", "", text)), (fn, tag, text[:120])
    assert len(re.findall(r"<h1\b", h)) == 1, fn


# ── H5: the score legend on the screener ───────────────────────────────────

def test_the_screener_explains_its_score_columns():
    from dashboard.score_glossary import describe
    h = _render("render_screener")
    assert 'class="ax-legendbox"' in h and "What do the scores mean?" in h
    assert h.index('class="ax-legendbox"') < h.index('id="res"'), "the legend sits above the results table"
    m = re.search(r'id="sgl" hidden data-json="([^"]*)"', h)
    import html as H
    import json
    gloss = json.loads(H.unescape(m.group(1)))
    assert gloss["atip_score"] == describe("ATIP") and gloss["cri"] == describe("CRI")
    assert '<abbr title="${esc(GL[c])}">' in h                                   # the JS-built header uses it
    assert "<script type=\"application/json\"" not in h


# ── M1 / M6 / M7: regions, states ──────────────────────────────────────────

@pytest.mark.parametrize("fn", PAGES.values())
def test_tables_scroll_in_labelled_regions_and_states_are_distinct(fn):
    h = _render(fn)
    assert '<div class="tw ax-scrollx${o.cls?\' \'+o.cls:\'\'}" tabindex="0" role="region" aria-label="${esc(lab)}">' in h
    for m in re.finditer(r'<div\b[^>]*class="(?:scroll|tw)(?=[\s"$])[^"]*"[^>]*>', h):
        tag = m.group(0)
        assert 'tabindex="0"' in tag and 'role="region"' in tag and "aria-label=" in tag, (fn, tag)
    assert "ax-skel" in h and "const errbox=" in h and 'role="alert"' in h
    assert not re.search(r'class="muted">none<', h), fn                          # never a bare "none"
    assert "<table>" not in re.sub(r"<script>.*?</script>", "", h, flags=re.S)  # no table outside a region


# ── H3 / H6: tokens, sizes ─────────────────────────────────────────────────

@pytest.mark.parametrize("fn", PAGES.values())
def test_colours_are_tokens_and_text_is_at_least_12px(fn):
    h = _render(fn)
    rest = re.sub(r"var\(--[\w-]+,\s*#[0-9a-fA-F]{3,8}\)", "", h)               # a token with its bare-page fallback
    assert not re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", rest), (fn, re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", rest)[:5])
    assert not re.search(r"font-size:\s*(?:[0-9]|1[01])(?:\.\d+)?px", h), fn
    assert not re.search(r'font-size="(?:[0-9]|1[01])(?:\.\d+)?"', h), fn
    for bad in ("#64748b", "#94a3b8\"", "fill=\"#", "stroke=\"#"):
        assert bad not in h, (fn, bad)
    assert "font-variant-numeric:tabular-nums" in h


@pytest.mark.parametrize("fn", PAGES.values())
def test_page_scripts_parse(fn, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    h = _render(fn)
    scripts = [s for s in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S) if s.strip()]
    assert scripts
    for i, js in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# ── the API the builder uses ───────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import get_connection, init_db
    init_db()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    exp = str(dt.date.today() + dt.timedelta(days=14))
    conn = get_connection()
    try:
        for strike, kind, px in ((23800, "PE", 60), (23900, "PE", 90), (24100, "CE", 95), (24200, "CE", 62)):
            conn.execute("INSERT INTO fo_contract_daily (date, symbol, instrument, expiry, strike, option_type, settle, "
                         "lot_size, underlying) VALUES (?,?,?,?,?,?,?,?,?)",
                         (str(dt.date.today()), "NIFTY", "OPTIDX", exp, strike, kind, px, 75, 24000))
        conn.execute("INSERT INTO fo_contract_daily (date, symbol, instrument, expiry, strike, option_type, settle, "
                     "lot_size, underlying) VALUES (?,?,?,?,?,?,?,?,?)",
                     (str(dt.date.today()), "RELIANCE", "OPTSTK", exp, 1400, "CE", 20, 500, 1390))
        conn.commit()
    finally:
        conn.close()
    return TestClient(server.app), {security.TOKEN_HEADER: security.token()}, exp


def test_symbols_route_lists_the_stored_chains_indices_first(api):
    client, _, _ = api
    r = client.get("/api/options/symbols")
    assert r.status_code == 200
    syms = [s["symbol"] for s in r.json()["symbols"]]
    assert syms == ["NIFTY", "RELIANCE"] and r.json()["symbols"][0]["source"] == "F&O bhavcopy"


def test_an_iron_condor_comes_back_with_payoff_greeks_max_loss_and_tier(api):
    client, h, exp = api
    b = {"template": "iron_condor", "symbol": "NIFTY", "expiry": exp, "width_steps": 1}
    assert client.post("/api/options/build", json=b).status_code == 401            # the token is still required
    r = client.post("/api/options/build", headers=h, json=b)
    assert r.status_code == 200, r.text
    x = r.json()
    assert len(x["legs"]) == 4 and all("bhavcopy" in l["price_source"] for l in x["legs"])
    assert x["max_loss"] < 0 and not x["unlimited_loss"] and len(x["breakevens"]) == 2
    assert set(x["greeks"]) == {"delta", "gamma", "theta", "vega"} and x["payoff"]["expiry"]
    assert x["suitability"]["tier"] == "Intermediate" and "capped" in x["suitability"]["reason"]
    legs = x["legs"][1:3]                                                       # just the two sold legs
    y = client.post("/api/options/analyse", headers=h, json={"symbol": "NIFTY", "legs": legs, "lot_size": 75}).json()
    assert y["unlimited_loss"] and y["suitability"]["tier"] == "Advanced"


@pytest.mark.parametrize("path", PAGES)
def test_the_pages_still_serve_inside_the_shell_without_wave_codes(api, path):
    client, _, _ = api
    r = client.get(path)
    assert r.status_code == 200
    title = re.search(r"<title>(.*?)</title>", r.text, re.S).group(1)
    assert not WAVE.search(title), title
    assert "/static/atip/atip.css" in r.text                                    # the shell's tokens are loaded
