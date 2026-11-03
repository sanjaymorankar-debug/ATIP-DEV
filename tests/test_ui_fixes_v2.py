"""UI fixes v2 (3 Nov 2026): H1-H10, G1-G8, W1-W5 -- the formatter, the alerts, Home against Accounting, the watchlist,
the order ticket's payload through the unchanged order-rule API, depth, the stock page, the P&L strip, freshness, and
the plain-English fixes. The browser half (DOM scan at 1440 and 360 px, keys, bottom sheet, layout shift) is
tests/test_ui_fixes_v2_browser.py. Seed: tools/ui_review.py seed_review (the 3 Nov findings)."""

import json
import re
from datetime import date

import pytest

from dashboard import fmt as F


# ── the one formatter ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("v,sign,out", [(123456.78, False, "₹1,23,456.78"), (-589.74, True, "−₹589.74"),
                                         (12.5, True, "+₹12.50"), (0, True, "₹0.00"), (10000000, False, "₹1,00,00,000.00"),
                                         (None, False, "—"), (float("nan"), True, "—"), ("", False, "—"), ("abc", False, "—")])
def test_fmt_inr_indian_grouping_sign_and_missing(v, sign, out):
    assert F.fmt_inr(v, sign) == out


def test_fmt_inr_compact_lakh_crore():
    assert F.fmt_inr(125000, compact=True) == "₹1.25 L"
    assert F.fmt_inr(21000000, compact=True) == "₹2.10 Cr"
    assert F.fmt_inr(-25700, compact=True) == "−₹25.7K"


def test_fmt_pct_and_inverse_tone():
    assert F.fmt_pct(3.48) == "+3.48%" and F.fmt_pct(-0.18) == "−0.18%" and F.fmt_pct(0) == "0.00%"
    assert F.fmt_pct(-0.001) == "0.00%" and F.fmt_pct(None) == "—"
    assert F.tone_class(3.48) == "ax-pos" and F.tone_class(-1) == "ax-neg" and F.tone_class(0) == "ax-flat"
    assert F.tone_class(3.48, inverse=True) == "ax-warn-t" and F.tone_class(-2, inverse=True) == "ax-pos"   # H9 VIX
    assert F.tone_class(None) == "ax-muted"
    h = F.pct_html(3.48, inverse=True)
    assert "▲" in h and "+3.48%" in h and "ax-warn-t" in h
    missing = F.pct_html(None, why="No GIFT Nifty quote since 06:30 IST")
    assert "—" in missing and "No GIFT Nifty quote since 06:30 IST" in missing and "0.00" not in missing   # G1


@pytest.mark.parametrize("v,out", [(60.73, "61"), (60.5, "61"), (60.49, "60"), (0, "0"), (None, "—"), ("72.4", "72")])
def test_fmt_score_whole_numbers(v, out):                                                # H7
    assert F.fmt_score(v) == out


@pytest.mark.parametrize("px,tick", [(113.27, 0.01), (254.11, 0.05), (999.99, 0.05), (1500, 0.10), (7500, 0.50),
                                     (15000, 1.0), (45000, 5.0)])
def test_nse_tick_bands(px, tick):
    assert F.tick_for(px) == tick


def test_fmt_price_rounds_to_the_tick():
    assert F.fmt_price(254.123) == "₹254.10" and F.fmt_price(113.27309768426964) == "₹113.27"
    assert F.fmt_price(1500.07) == "₹1,500.10" and F.fmt_price(None) == "—"


def test_live_stats_moves_the_52_week_range_with_the_ltp():                                # G6
    st = {"last_close": 240.0, "prev_close": 238.0, "high_52w": 245.5, "low_52w": 180.0, "ret_1w": 2.0, "ret_1m": -1.0}
    out = F.live_stats(st, 254.11)
    assert out["high_52w"] == 254.11 and out["low_52w"] == 180.0
    base_1w = 240.0 / 1.02
    assert out["ret_1w"] == round((254.11 / base_1w - 1) * 100, 2)
    assert out["change_pct"] == round((254.11 / 238.0 - 1) * 100, 2)
    assert F.live_stats(st, None) == st


def test_js_formatter_mirrors_python(tmp_path):
    """AX.fmt in atip.js gives the same text as dashboard/fmt.py (node runs the real file)."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "atip" / "atip.js"
    harness = tmp_path / "h.js"
    harness.write_text("""
const vm=require('vm'),fs=require('fs');
const noop=()=>{};const el={addEventListener:noop,querySelector:()=>null,querySelectorAll:()=>[],classList:{add:noop,remove:noop,toggle:noop,contains:()=>false},setAttribute:noop,removeAttribute:noop,getAttribute:()=>null,appendChild:noop};
const w={document:{documentElement:el,readyState:'loading',addEventListener:noop,querySelector:()=>null,querySelectorAll:()=>[],getElementById:()=>null,body:el,createElement:()=>el},
 location:{search:'',pathname:'/',href:'http://x/'},localStorage:{getItem:()=>null,setItem:noop},sessionStorage:{getItem:()=>null,setItem:noop},URLSearchParams,setInterval:noop,setTimeout:noop,console};
w.window=w;vm.createContext(w);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),w);const f=w.AX.fmt;
console.log(JSON.stringify([f.inr(123456.78),f.inr(-589.74,true),f.inr(null),f.pct(3.48),f.pct(-0.18),f.pct(0),f.score(60.73),f.price(254.123),f.price(1500.07),f.tick(7500),f.inr(21000000,false,true)]));
""")
    out = json.loads(subprocess.run([node, str(harness), str(js)], capture_output=True, text=True, check=True).stdout)
    assert out == [F.fmt_inr(123456.78), F.fmt_inr(-589.74, True), F.fmt_inr(None), F.fmt_pct(3.48), F.fmt_pct(-0.18),
                   F.fmt_pct(0), F.fmt_score(60.73), F.fmt_price(254.123), F.fmt_price(1500.07), F.tick_for(7500),
                   F.fmt_inr(21000000, compact=True)]


# ── alerts, jobs (H4, H6) ───────────────────────────────────────────────────────────────────────

def test_alert_text_is_unescaped_once_and_the_title_is_not_repeated():
    from dashboard.ux_pages import clean_alert
    t = clean_alert("Intraday signals", "<b>Intraday signals</b>\nWELSPUNLIV 2.4x the slot&#x27;s usual volume &amp; more")
    assert t == "WELSPUNLIV 2.4x the slot's usual volume & more"
    assert clean_alert("", "double &amp;#x27; escaped") == "double ' escaped"
    assert clean_alert("Dhan token expires today", "Dhan token expires today at 23:59 IST") == "At 23:59 IST"
    assert clean_alert("", "Job failed: technical_signals") == "Job failed: Technical signals"


def test_alerts_group_by_kind_with_symbol_chips():
    from dashboard.ux_pages import group_alerts
    rows = [{"created_at": "2026-11-03 13:5%d:00" % i, "category": "intraday", "severity": "info", "title": "Intraday signals",
             "message": f"<b>Intraday signals</b> LOAD00{i} crossed VWAP"} for i in range(4)]
    rows.append({"created_at": "2026-11-03 13:00:00", "category": "job", "severity": "warning", "title": "Job failed: x", "message": "m"})
    g = group_alerts(rows, known={"LOAD000"})
    assert [x["title"] for x in g] == ["Intraday signals", "Job failed: x"] and g[0]["count"] == 4
    assert "LOAD000" in g[0]["symbols"] and "VWAP" not in g[0]["symbols"]


def test_one_row_per_job_with_its_plain_name():
    from dashboard.ux_pages import latest_jobs
    rows = [{"job_name": "broker_health", "status": "SUCCESS"}, {"job_name": "global_intraday", "status": "SUCCESS"},
            {"job_name": "broker_health", "status": "FAILED"}, {"job_name": "saved_screens", "status": "SUCCESS"}]
    out = latest_jobs(rows)
    assert [r["label"] for r in out] == ["Broker connection", "Global markets (intraday)", "Saved screens"]
    assert out[0]["status"] == "SUCCESS"                   # the latest run of broker_health only


# ── the seeded app ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from accounting import tax as TX
    from dashboard import security
    from enterprise import session_reports as R
    from tools import load_test as LT
    from tools.ui_review import seed_review
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(TX, "CONFIG_PATH", tmp_path / "tax-config.json")
    rc = tmp_path / "reports.json"
    rc.write_text(json.dumps({"reports": {"pdf_engine": "builtin"}}))
    monkeypatch.setattr(R, "CONFIG_PATH", rc)
    with LT.block_network():
        LT.seed(tmp_path, extra_symbols=4)
        from db.schema import get_connection
        conn = get_connection()
        seed_review(conn)
        conn.close()
        from dashboard import server
        with TestClient(server.app) as c:
            c.token = security.token()
            yield c


def _h(c):
    return {"X-ATIP-Token": c.token}


def _figures(html: str) -> dict:
    import html as H
    m = re.search(r'data-ax-figures="([^"]*)"', html)
    return json.loads(H.unescape(m.group(1)))


def test_home_labels_every_figure_with_its_period_and_reconciles_with_accounting(client):     # H1
    r = client.get("/home")
    assert r.status_code == 200
    t = r.text
    assert "Making money today?" in t and "<b>Today</b>: realised" in t and "<b>Since start</b>" in t
    fig = _figures(t)
    assert fig["today"]["realised"] == 0.0 and fig["since_start"]["realised"] == -589.74
    api = client.get("/api/v2/periods").json()
    assert api["ok"] is True
    for row in api["rows"]:
        assert row["realised"] == fig[row["key"]]["realised"] and row["total"] == fig[row["key"]]["total"]
        assert row["ledger"] == row["realised"]                          # the general ledger agrees
    acc = client.get("/accounting?tab=pnl").text
    assert "✓ Home and the general ledger agree" in acc
    for key in ("today", "since_start", "month"):
        m = re.search(rf'<tr data-period="{key}">.*?data-k="realised"[^>]*?>', acc, re.S)
        v = re.search(r'data-v="([-0-9.]+)" data-k="realised"', m.group(0)).group(1)
        assert float(v) == fig[key]["realised"]


def test_home_cards_share_one_pattern_and_flat_is_grey(client):                               # H2, H3
    t = client.get("/home").text
    assert 'class="ax-five ax-answers"' in t
    assert t.count('class="ax-answer"') == 3 and t.count('<span class="ax-word">') == 3
    assert re.search(r'data-tone="flat" aria-label="Making money today\?".*?<span class="ax-word">Flat</span>', t, re.S)


def test_home_alerts_are_clean_grouped_and_have_symbol_chips(client):                          # H4
    t = client.get("/home").text
    w = t[t.index('data-widget="alerts"'):t.index('data-widget="pipeline"')]
    assert "&amp;#x27;" not in w and "slot&#x27;s" in w                 # unescaped once, escaped once on the way out
    assert "Intraday signals</b> <span class=\"ax-muted\">· 4</span>" in w and "data-ax-clamp" in w
    assert 'href="/stock/WELSPUNLIV"' in w and "Technical signals" in w


def test_home_idle_sessions_fold_into_one_row(client):                                         # H5
    t = client.get("/home").text
    assert "S2–S10 · no trades yet (9)" in t and t.count('class="ax-idle-row" hidden') == 9
    assert 'data-ax-period="since_start"' in t


def test_home_pipeline_score_vix_gift(client):                                                 # H6, H7, H9, G1
    t = client.get("/home").text
    p = t[t.index('data-widget="pipeline"'):]
    assert p.count(">Broker connection<") == 1 and "Global markets (intraday)" in p and ">broker_health<" not in p
    assert 'title="broker_health' in p                                   # the raw name in the tooltip
    assert '<span class="ax-num">61</span> BULL' in t and "60.73" not in t
    vix = t[t.index("India VIX"):t.index("GIFT Nifty")]
    assert "ax-warn-t" in vix and "▲" in vix and "+3.48%" in vix
    gift = t[t.index("<dt>GIFT Nifty</dt>"):t.index("Market health")]
    assert "ax-missing" in gift and "0.00%" not in gift
    assert "Technical signals · as of 30 Oct · stale" in t               # G8 chip


def test_shell_breadcrumb_overflow_menu_and_workspace_frames(client):                          # H8, G7, W1, W2
    t = client.get("/home").text
    assert '<b class="ax-crumb-sec">Home</b><span class="ax-crumb-page"> › Today</span>' in t
    assert 'data-ax-overflow' in t and 'id="ax-ovmenu"' in t and 'id="ax-wl"' in t and 'id="ax-ticket"' in t
    assert "/static/atip/workspace.js" in t and "/static/atip/workspace.css" in t
    assert 'data-ax-pnlstrip' not in t                                    # the strip is for the Trade screens only
    assert 'data-ax-pnlstrip' in client.get("/command").text              # W5


def test_banner_only_for_what_is_not_normal(client):                                          # H10
    t = client.get("/home").text
    assert 'id="atipOpBanner"' in t and "var abnormal=d.status!=='RUNNING'||d.halted||d.env==='LIVE'" in t
    st = client.get("/api/status").json()
    assert st["halted"] is False


def test_root_page_has_no_raw_floats_none_or_red_zero(client):                                # G1
    t = client.get("/?shell=0").text
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", t, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)                                 # what a person reads, not attributes
    assert "113.27309" not in text and " None " not in f" {text} " and "₹—" not in text
    kpi = t[t.index('<div class="kpi-l">GIFT Nifty</div>'):][:400]
    assert "—" in kpi and "No GIFT Nifty quote" in kpi and "#dc2626" not in kpi


def test_plain_english_and_how_it_works(client):                                               # G3, G4
    s = client.get("/settings").text
    body = re.sub(r'<details class="ax-tech">.*?</details>', "", s, flags=re.S)
    assert "config.json" not in re.sub(r"<script.*?</script>", "", body, flags=re.S)
    sig = client.get("/signals").text
    assert "ⓘ How this works" in sig and 'class="ax-term"' in sig
    head = sig[sig.index("End-of-day technical signals"):sig.index("ⓘ How this works")]
    assert ".py" not in head and "python -m" not in head


def test_freshness_flags_the_technical_signals(client):                                        # G8
    d = {x["key"]: x for x in client.get("/api/v2/freshness").json()["datasets"]}
    assert d["technical"]["stale"] is True and d["technical"]["date"] == "2026-10-30"


# ── W1-W5 ───────────────────────────────────────────────────────────────────────────────────────

def test_watchlists_save_in_order_and_need_the_token(client):                                  # W1
    first = client.get("/api/v2/watchlists").json()
    assert first["lists"][0].get("draft") is True and first["lists"][0]["symbols"]
    assert client.post("/api/v2/watchlists", json={"name": "x", "symbols": ["ACME"]}).status_code in (401, 403)
    r = client.post("/api/v2/watchlists", json={"name": "Momentum", "symbols": ["LOAD002", "ACME", "acme"]}, headers=_h(client))
    assert r.status_code == 200 and r.json()["symbols"] == ["LOAD002", "ACME"]
    wid = r.json()["id"]
    lists = client.get("/api/v2/watchlists").json()["lists"]
    assert [l["name"] for l in lists] == ["Momentum"] and lists[0]["symbols"] == ["LOAD002", "ACME"]
    assert client.post("/api/v2/watchlists", json={"name": "bad", "symbols": ["NOT A SYMBOL!"]}, headers=_h(client)).status_code == 400
    assert client.post(f"/api/v2/watchlists/{wid}/delete", json={}, headers=_h(client)).json() == {"deleted": wid}
    assert client.get("/api/v2/watchlists").json()["lists"][0].get("draft") is True


def test_quotes_fall_back_to_the_last_close(client):                                           # W1
    q = client.get("/api/v2/quotes?symbols=ACME,LOAD001,BAD!!").json()["quotes"]
    assert set(q) == {"ACME", "LOAD001"} and q["ACME"]["live"] is False and q["ACME"]["ltp"] > 0
    assert q["ACME"]["change_pct"] is not None and q["ACME"]["signal"] == "BUY"


def test_the_ticket_saves_through_the_unchanged_order_rule_api(client):                        # W2
    p = client.get("/api/command/preview?symbol=ACME&side=BUY&qty=10&entry=113.27&stop=108&target=125").json()
    assert p["rr"] and p["net_risk_rs"] > 0 and p["charges"]["entry"] > 0 and "loss_budget" in p
    gap = [c for c in p["checks"] if "signal price" in c["check"]]
    assert gap and "-0.00%" not in gap[0]["detail"]
    trig = 113.27
    payload = {"symbol": "ACME", "side": "BUY", "trigger_type": "PRICE", "trigger_value": trig, "trigger_percent": None,
               "reference_price": trig, "quantity_type": "SHARES", "quantity_value": 10, "product_type": "CNC",
               "order_type": "MARKET", "limit_price": None, "require_confirmation": True, "notes": "Order ticket",
               "bracket_stop_pct": round((trig - 108) / trig * 100, 3), "bracket_target_pct": round((125 - trig) / trig * 100, 3),
               "bracket_auto_exit": True}
    assert client.post("/api/orders", json=payload).status_code in (401, 403)          # token, as before
    from orders import rules as oe
    monkey_sec = oe.get_security_id
    try:
        oe.get_security_id = lambda s: "1"                                              # no Dhan security list offline
        r = client.post("/api/orders", json=payload, headers=_h(client))
    finally:
        oe.get_security_id = monkey_sec
    assert r.status_code == 200, r.text
    rule = r.json()
    assert rule["status"] == "ACTIVE" and rule["require_confirmation"] in (1, True) and rule["product_type"] == "CNC"
    assert rule["resolved_trigger_price"] == trig


def test_depth_says_why_when_the_feed_is_off(client):                                          # W4
    d = client.get("/api/v2/depth/ACME?levels=20").json()
    assert d["feed"] == "OFF" and "switched off" in d["message"] and d["levels"] == 20


def test_depth_reads_the_feed_book_without_changing_it(client, monkeypatch):                    # W4
    import threading
    from data import depth20 as D

    class FakeFeed:
        _lock = threading.Lock()
        _sid_to_sym = {"1": "ACME"}
        _book = {"1": {"bid": [(113.2, 500, 3), (113.15, 200, 1)], "ask": [(113.3, 400, 2)]}}
        last_at = None
    monkeypatch.setattr(D, "get_depth20", lambda: FakeFeed)
    monkeypatch.setattr(D, "feed_status", lambda: {"running": True, "enabled": True})
    d = client.get("/api/v2/depth/ACME?levels=5").json()
    assert d["feed"] == "ON" and d["bids"][0] == {"price": 113.2, "qty": 500, "orders": 3}
    assert d["total_bid_qty"] == 700 and d["imbalance"] == round((700 - 400) / 1100, 3)
    assert FakeFeed._book["1"]["bid"][0] == (113.2, 500, 3)


def test_stock_page_and_the_local_chart_library(client):                                       # W3
    r = client.get("/stock/ACME")
    assert r.status_code == 200 and 'id="ax-sv-chart"' in r.text and 'data-ax-trade="BUY"' in r.text
    assert "/static/vendor/lightweight-charts-5.0.9/lightweight-charts.standalone.production.js" in r.text
    js = client.get("/static/vendor/lightweight-charts-5.0.9/lightweight-charts.standalone.production.js")
    assert js.status_code == 200 and "Lightweight Charts" in js.text[:400] and "v5.0.9" in js.text[:400]
    assert client.get("/static/vendor/lightweight-charts-5.0.9/LICENSE").status_code == 200
    assert client.get("/stock/NOPE").status_code == 404
    assert client.get("/stock/" + "x" * 30).status_code == 404
    assert client.get("/watchlist").status_code == 200


def test_pnl_strip(client):                                                                     # W5
    d = client.get("/api/v2/pnl-strip").json()
    assert set(d) >= {"today", "open_positions", "waiting", "loss_budget", "env"}
    assert d["today"]["realised"] == 0.0 and d["waiting"] == 0


def test_order_paths_are_untouched():
    """The brief's acceptance test: nothing under orders/ or execution/ changed on this branch."""
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    try:
        base = subprocess.run(["git", "merge-base", "HEAD", "origin/master"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        out = subprocess.run(["git", "diff", "--name-only", base, "--", "orders", "execution"], cwd=repo, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "orders", "execution"], cwd=repo, capture_output=True,
                               text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no git history here")
    assert out == "" and dirty == ""


def test_new_tokens_meet_wcag_aa_in_both_themes():
    """The v2 tokens: amber (an inverse metric rising, a warn card's word) and the flat grey, on every surface."""
    from pathlib import Path
    from tests.test_ux_shell import _ratio
    css = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "atip" / "atip.css").read_text()
    light = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split("@media (prefers-color-scheme: dark)")[0]))
    dark = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split(':root[data-theme="dark"]')[1].split("}")[0]))
    auto = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split("@media (prefers-color-scheme: dark)")[1].split("}\n}")[0]))
    assert dark["amber"] == auto["amber"]                                 # the device's dark = the picked dark
    for name, t in (("light", light), ("dark", {**light, **dark})):
        for bg in ("surface", "bg", "surface-2"):
            assert _ratio(t["amber"], t[bg]) >= 4.5, (name, bg, round(_ratio(t["amber"], t[bg]), 2))
    ws = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "atip" / "workspace.css").read_text()
    assert not re.findall(r"(?<!-)#[0-9A-Fa-f]{6}\b", ws.replace("#fff", "")), "workspace.css uses tokens only"
