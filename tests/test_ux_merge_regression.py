"""
Task 1 (2026-10-26): regression checks for the merged UI/UX changes (UXR-01..03 Command Center, kill switch,
sessions; the Task 2 status banner; the Task 3 Global & Asia tab) against the existing dashboard.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    init_db()
    monkeypatch.chdir(tmp_path)
    return TestClient(srv.app)


EXISTING_TABS = ["ATIP Scores", "Portfolio", "Top VPI", "Buy Zones", "Recovery (RRI)", "Momentum (MRI)", "CRI Risk",
                 "FII / DII", "News", "Signal History", "Orders"]


def test_every_existing_tab_is_still_there_and_in_order(client):
    html = client.get("/").text
    tabs = re.findall(r'<div class="tab[^"]*" onclick="showTab\(\'(\w+)\',this\)[^>]*>([^<]+)</div>', html)
    labels = [t[1].replace("&amp;", "&") for t in tabs]
    assert labels[:len(EXISTING_TABS)] == EXISTING_TABS
    assert labels[-1] == "Global & Asia"
    for tid in ("scores", "port", "vpi", "zpi", "rri", "mri", "cri", "fiidii", "news", "hist", "orders", "asia"):
        assert f'id="{tid}" class="tc' in html, tid


def test_trade_of_the_day_and_kpis_render(client):
    html = client.get("/").text
    for s in ("🎯 Trade of the Day", 'class="tod-card"', "Market Health", "Nifty 50", "India VIX", "GIFT Nifty"):
        assert s in html


def test_buy_sell_target_and_stoploss_config_ui_unchanged(client):
    html = client.get("/").text
    for el in ('id="mSide"', 'id="mTT"', 'id="mTV"', 'id="mQT"', 'id="mBrk"', 'id="mBT1"', 'id="mBT2"', 'id="mBSL"',
               'id="mTrail"', 'id="mConfirm"', "saveOrderRule()"):
        assert el in html, el


def test_live_cmp_feed_still_polls_every_15s_and_updates_cells_in_place(client):
    html = client.get("/").text
    assert "setInterval(pollLiveQuotes,15000)" in html and "fetch('/api/live-quotes')" in html
    assert 'class="tod-v cmpcell"' in html, "CMP cells carry the class the poller updates in place"


def test_live_quotes_api_shape_in_market_hours(client, monkeypatch):
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
    assert client.get("/api/live-quotes").json() == {"ACME": {"ltp": 110.0, "chg_pct": 10.0, "prev_close": 100.0}}


@pytest.mark.parametrize("page", ["/", "/command", "/m", "/global-asia"])
def test_pages_are_phone_ready_and_share_the_font_stack(client, page):
    html = client.get(page).text
    assert re.search(r'<meta name="viewport" content="width=device-width,initial-scale=1', html), page
    assert "system-ui" in html
    assert 'id="atipOpBanner"' in html, "every page carries the operating-status banner"


def test_command_center_is_linked_and_links_back(client):
    assert 'href="/command"' in client.get("/").text
    cmd = client.get("/command").text
    assert 'href="/global-asia"' in cmd and 'id="killBtn"' in cmd


def test_mobile_page_has_the_three_screens(client):
    m = client.get("/m").text
    assert "Today" in m and "Global &amp; Asia" in m and "location.href='/command'" in m
    assert "atip-shell-v3" in client.get("/sw.js").text, "the PWA cache version moved so phones fetch the new shell"


MERGED_ROUTE_FILES = ["dashboard/command_routes.py", "dashboard/command_page.py", "dashboard/global_asia.py"]


@pytest.mark.parametrize("path", MERGED_ROUTE_FILES)
def test_no_hardcoded_date_today_in_merged_dashboard_routes(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "date.today()" not in src, f"{path}: dates come from the data or an explicit IST clock"


def test_new_dashboard_routes_add_no_date_today_to_server():
    """server.py had 6 date.today() calls before the merge; the merge adds none."""
    assert (ROOT / "dashboard/server.py").read_text(encoding="utf-8").count("date.today()") <= 6
