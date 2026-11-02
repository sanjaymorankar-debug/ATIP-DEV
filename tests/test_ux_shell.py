"""The redesign's shell, pages and /api/v2 (dashboard/shell.py, ux_pages.py, ux_routes.py; UXD / SES / ACC / RPT).

Every one of the old HTML routes still answers 200 and now renders inside the shell; the new pages render; writes
need the token, are refused with 423 while ATIP is stopped and 403 on a viewer; session setup validates in plain
English and keeps AUTO's ack; the pages pass a structural accessibility check (lang, one h1, a skip link, labelled
fields, named buttons, landmarks) and the design tokens meet WCAG AA contrast in both themes."""

import json
import re
from html.parser import HTMLParser

import pytest

from accounting import tax as TX
from enterprise import session_reports as R

OLD_PAGES = ["/", "/backtests", "/strategies", "/trading", "/market", "/execution-lab", "/data-platform", "/assistant",
             "/strategy-builder", "/brokers", "/compliance", "/privacy", "/m", "/research", "/options-builder",
             "/market-pulse", "/signals", "/screener", "/baskets", "/command", "/command/signals", "/command/setup",
             "/command/monitor", "/command/journal", "/command/pnl", "/global-asia", "/ml", "/quant", "/wealth",
             "/account", "/admin", "/app", "/admin/console"]
NEW_PAGES = ["/home", "/sessions", "/sessions/S1", "/sessions/S1?tab=accounting", "/sessions/S1?tab=reports",
             "/accounting", *[f"/accounting?tab={t}" for t in ("session", "charges", "tax", "funds", "recon", "transfers",
                                                              "trial")],
             "/reports", "/reports?tab=schedules", "/reports?tab=archive", "/reports?tab=custom", "/settings", "/more",
             "/markets", "/trade", "/lab", "/ops"]


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security
    from tools import load_test as LT
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(TX, "CONFIG_PATH", tmp_path / "tax-config.json")
    rc = tmp_path / "reports.json"
    rc.write_text(json.dumps({"reports": {"pdf_engine": "builtin"}}))
    monkeypatch.setattr(R, "CONFIG_PATH", rc)
    with LT.block_network():
        LT.seed(tmp_path, extra_symbols=4)
        from dashboard import server
        with TestClient(server.app) as c:
            c.token = security.token()
            yield c


def _post(c, url, body, token=True):
    return c.post(url, json=body, headers={"X-ATIP-Token": c.token} if token else {})


@pytest.mark.parametrize("path", OLD_PAGES)
def test_every_old_url_still_works_inside_the_shell(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    assert 'class="ax-top"' in r.text and "/static/atip/atip.css" in r.text and "ax-shelled" in r.text
    assert r.text.count('class="ax-top"') == 1 and re.search(r"<body[^>]*ax-shelled", r.text)
    assert r.text.rstrip().endswith("</html>")


def test_bare_and_non_html_routes_are_untouched(client):
    assert "ax-top" not in client.get("/command?shell=0").text
    assert "ax-top" not in client.get("/login").text
    assert client.get("/api/status").headers["content-type"].startswith("application/json")
    assert client.get("/static/atip/atip.css").status_code == 200
    assert "ATIP" in client.get("/static/atip/atip.js").text


@pytest.mark.parametrize("path", NEW_PAGES)
def test_new_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:300])
    assert '<meta name="atip-shell" content="native">' in r.text and r.text.count('class="ax-top"') == 1
    assert "Traceback" not in r.text


def test_app_mode_hides_the_rail(client):
    r = client.get("/home", headers={"User-Agent": "Mozilla/5.0 ATIPApp/2026.11.02"})
    assert "d.classList.add('ax-app')" in r.text


def test_shell_state_and_status_env(client):
    s = client.get("/api/v2/shell/state").json()
    assert s["env"] == "PAPER" and s["status"] in ("RUNNING", "STOPPED", "RESUMING") and s["banner"]
    assert client.get("/api/status").json()["env"] == "PAPER"


def test_setup_validates_in_plain_english_and_logs(client):
    r = _post(client, "/api/v2/sessions/S3/setup", {"start_amount": "25,000", "min_buy_value": "30000"})
    assert r.status_code == 400
    assert r.json()["errors"]["min_buy_value"] == "Minimum ₹30,000 is more than the ₹25,000 starting amount."
    r = _post(client, "/api/v2/sessions/S3/setup", {"start_amount": 40000, "min_buy_value": 15000, "floor_rule": "RATCHET_PCT",
                                                    "floor_value": 80, "compound_basis": "AFTER_TAX_RESERVE"})
    assert r.status_code == 200, r.text
    s = r.json()["session"]
    assert (s["start_capital"], s["floor_rule"], s["floor_value"], s["compound_basis"]) == \
        (40000.0, "RATCHET_PCT", 80.0, "AFTER_TAX_RESERVE")
    assert r.json()["pool"]["floor"] == 32000.0                                 # 80 % of the 40,000 start
    r = _post(client, "/api/v2/sessions/S3/setup", {"start_amount": 50000})
    assert r.status_code == 400 and "restarts S3's books" in r.json()["errors"]["restart"]
    r = _post(client, "/api/v2/sessions/S3/setup", {"mode": "AUTO"})
    assert r.status_code == 400 and "I understand" in r.json()["errors"]["ack"]
    ev = client.get("/api/sessions/events?sid=S3").json()
    assert any(e["event"] == "POOL" for e in ev) and any(e["event"] == "EDIT" for e in ev)
    assert "S3" in client.get("/sessions").text


def test_each_session_its_own_amount_and_split_saves_nothing(client):
    for sid, amt in (("S1", 25000), ("S2", 40000)):
        assert _post(client, f"/api/v2/sessions/{sid}/setup", {"start_amount": amt}).status_code == 200
    ov = client.get("/api/v2/sessions").json()
    caps = {s["id"]: s["setup"]["start_capital"] for s in ov["sessions"]}
    assert caps["S1"] == 25000 and caps["S2"] == 40000
    sp = _post(client, "/api/v2/sessions/split", {"total": 90000, "ids": ["S1", "S2", "S4"]}).json()
    assert sp["amounts"] == {"S1": 30000.0, "S2": 30000.0, "S4": 30000.0}
    assert client.get("/api/v2/sessions").json()["sessions"][0]["setup"]["start_capital"] == 25000


def test_writes_need_the_token(client):
    assert _post(client, "/api/v2/sessions/S1/setup", {"start_amount": 1000}, token=False).status_code == 401
    assert _post(client, "/api/v2/tax/config", {"stcg_pct": 15}, token=False).status_code == 401
    assert _post(client, "/api/v2/reports/generate", {"kind": "monthly_pnl"}, token=False).status_code == 401


def test_writes_are_refused_while_atip_is_stopped(client, monkeypatch):
    import pipeline.operating_schedule as OS
    monkeypatch.setattr(OS, "is_stopped", lambda now=None: True)
    r = _post(client, "/api/v2/sessions/S1/setup", {"start_amount": 25000})
    assert r.status_code == 423 and r.json()["read_only"]
    assert client.get("/sessions").status_code == 200                           # reading still works


def test_a_viewer_refuses_every_write(client, monkeypatch):
    import ops.role as role
    monkeypatch.setattr(role, "is_viewer", lambda: True)
    assert _post(client, "/api/v2/accounting/transfer", {"from": "S1", "to": "S2", "amount": 1, "reason": "x"}).status_code == 403


def test_reports_generate_and_download(client):
    _post(client, "/api/v2/sessions/S1/setup", {"start_amount": 25000})
    r = _post(client, "/api/v2/reports/generate", {"kind": "fy_tax_statement", "formats": ["pdf", "xlsx"]})
    assert r.status_code == 200, r.text
    for f in r.json()["files"]:
        d = client.get(f["url"])
        assert d.status_code == 200 and d.content[:4] in (b"%PDF", b"PK\x03\x04")
    assert client.get("/api/v2/reports/schedules").json()["schedules"]
    bad = _post(client, "/api/v2/reports/schedules", {"kind": "monthly_pnl", "freq": "monthly", "day": 31})
    assert bad.status_code == 400 and "1 to 28" in bad.json()["error"]


def test_tax_config_round_trip(client):
    r = _post(client, "/api/v2/tax/config", {"stcg_pct": 15, "addons_enabled": True})
    assert r.status_code == 200 and r.json()["stcg_pct"] == 15.0
    assert client.get("/api/v2/tax/config").json()["addons_enabled"] is True
    assert _post(client, "/api/v2/tax/config", {"stcg_pct": 200}).status_code == 400


def test_search_finds_symbols_and_pages(client):
    syms = client.get("/api/v2/search?q=AC").json()["symbols"]
    assert any(s["symbol"] == "ACME" for s in syms)
    page = client.get("/home").text
    import html as H
    nav = json.loads(H.unescape(re.search(r'<div id="ax-nav" hidden data-json="(.*?)"></div>', page, re.S).group(1)))
    assert {"/sessions", "/accounting?tab=tax", "/reports?tab=schedules", "/command/setup"} <= {n["href"] for n in nav}


# ── accessibility (structural) ─────────────────────────────────────────────────────────────

class _A11y(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = None; self.h1 = 0; self.skip = False; self.main = False; self.nav = False
        self.ids, self.labels_for, self.fields, self.buttons, self.cur_btn, self.problems = set(), set(), [], [], None, []
        self.in_label = 0

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "h1":
            self.h1 += 1
        if tag == "a" and a.get("class") == "ax-skip":
            self.skip = True
        if tag == "main":
            self.main = True
        if tag == "nav" and a.get("aria-label"):
            self.nav = True
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden",):
            self.fields.append((a.get("id"), a.get("aria-label"), a.get("aria-labelledby"), self.in_label > 0, a.get("name")))
        if tag == "button":
            self.cur_btn = {"label": a.get("aria-label"), "text": ""}

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label -= 1
        if tag == "button" and self.cur_btn is not None:
            self.buttons.append(self.cur_btn)
            self.cur_btn = None

    def handle_data(self, data):
        if self.cur_btn is not None:
            self.cur_btn["text"] += data.strip()


@pytest.mark.parametrize("path", ["/home", "/sessions", "/sessions/S1", "/accounting?tab=transfers", "/accounting?tab=recon",
                                  "/reports", "/reports?tab=schedules", "/settings"])
def test_pages_pass_the_structural_accessibility_audit(client, path):
    p = _A11y()
    p.feed(client.get(path).text)
    assert p.lang == "en-IN" and p.h1 == 1 and p.skip and p.main and p.nav
    unlabelled = [f for f in p.fields if not (f[1] or f[2] or f[3] or (f[0] and f[0] in p.labels_for))]
    assert not unlabelled, unlabelled
    assert all(b["label"] or b["text"] for b in p.buttons), [b for b in p.buttons if not (b["label"] or b["text"])]


def _lum(hexc):
    c = [int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_design_tokens_meet_wcag_aa_contrast_in_both_themes():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "atip" / "atip.css").read_text()
    light = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split("@media (prefers-color-scheme: dark)")[0]))
    dark = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split(':root[data-theme="dark"]')[1].split("}")[0]))
    for name, t in (("light", light), ("dark", {**light, **dark})):
        for fg in ("text", "text-2", "muted", "pos", "neg", "warn", "info", "brand"):
            for bg in ("surface", "bg", "surface-2"):
                assert _ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg, round(_ratio(t[fg], t[bg]), 2))
        for fg, bg in (("pos", "pos-bg"), ("neg", "neg-bg"), ("warn", "warn-bg"), ("info", "info-bg"), ("brand-ink", "brand")):
            assert _ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg, round(_ratio(t[fg], t[bg]), 2))
    for env in ("env-paper", "env-sandbox", "env-live", "kill"):
        assert _ratio("#FFFFFF", light[env]) >= 4.5, env
    for bg, fg in (("run", "run-ink"), ("stop", "stop-ink"), ("resume", "resume-ink")):
        assert _ratio(light[fg], light[bg]) >= 4.5, bg
