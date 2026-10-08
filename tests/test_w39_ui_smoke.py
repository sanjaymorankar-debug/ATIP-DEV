"""W39 browser smoke tests (Wave 18 QA: browser / UI) -- headless Chromium through Playwright.

The dashboard is served by uvicorn in a background thread on a free local port, against the
seeded W18 market (tests/_wealth_seed.py via tools/load_test.seed: NIFTY50, ACME, GOLDBEES,
LIQUIDBEES plus 10 synthetic stocks, 260 sessions). Each page is opened, its scripts run, and
the test asserts that no uncaught JavaScript error reached the page ("pageerror"), that no
same-origin request came back 5xx, and that the page's key elements rendered with the seeded
data. /wealth has every tab clicked; on / the stock panel is opened with openStock('ACME').

The browser only talks to the local server: every other request is aborted, and the Python
side runs with outbound network refused (load_test.block_network), so the NSE / yfinance paths
some reads try fail at once and are not asserted on.

Skipped cleanly when Playwright or a Chromium binary is unavailable -- CI installs neither
(.github/workflows/tests.yml). Chromium: $ATIP_CHROMIUM, else the pre-installed
/opt/pw-browsers build, else Playwright's own download if one exists.
"""

import os
from pathlib import Path

import pytest

from tools import load_test as LT

CHROMIUM_CANDIDATES = [os.environ.get("ATIP_CHROMIUM"), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
WEALTH_TABS = ["ovw", "wlt", "gol", "aal", "rbl", "prf", "adv", "dna"]


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("uvicorn")
    pw = sync_api.sync_playwright().start()
    b, why = None, []
    for exe in [c for c in CHROMIUM_CANDIDATES if c and Path(c).exists()] + [None]:
        try:
            b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
            break
        except Exception as e:                       # no binary / missing system libraries
            why.append(f"{exe or 'playwright default'}: {str(e).splitlines()[0][:120]}")
    if b is None:
        pw.stop()
        pytest.skip("no launchable Chromium for Playwright (" + "; ".join(why) + ")")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def site(temp_db, tmp_path, monkeypatch):
    """The seeded dashboard over real HTTP. cwd = tmp_path, so the state cache, token, config
    and Nifty 500 list all live there; db.schema.DB_PATH is temp_db (conftest)."""
    pytest.importorskip("fastapi")
    from dashboard import security
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    with LT.block_network():
        info = LT.seed(tmp_path, extra_symbols=10)
        from dashboard import server
        with LT.serve(server.app) as base:
            yield base, info


class Visit:
    """A page plus everything it reported: uncaught JS errors and 5xx same-origin responses."""

    def __init__(self, ctx, base, path):
        self.page = ctx.new_page()
        self.errors, self.server_errors = [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", lambda r: self.server_errors.append((r.status, r.url))
                     if r.status >= 500 and r.url.startswith(base) else None)
        self.page.goto(base + path, wait_until="networkidle", timeout=30000)

    def settle(self, ms=300):
        self.page.wait_for_load_state("networkidle", timeout=15000)
        self.page.wait_for_timeout(ms)

    def assert_clean(self):
        self.settle()
        assert self.errors == [], f"JavaScript errors on {self.page.url}: {self.errors}"
        assert self.server_errors == [], f"server errors behind {self.page.url}: {self.server_errors}"


@pytest.fixture
def ctx(browser, site):
    base, _ = site
    c = browser.new_context(viewport={"width": 1280, "height": 900})
    c.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    yield c
    c.close()


def test_home_page_lists_the_seeded_scores_and_opens_the_stock_panel(ctx, site):
    from playwright.sync_api import expect
    base, info = site
    v = Visit(ctx, base, "/")
    assert v.page.title() == "ATIP Dashboard"
    expect(v.page.locator("#st tbody tr")).to_have_count(len(info["symbols"]))      # ACME + LOAD000..009
    expect(v.page.locator('#st tbody tr[data-sym="ACME"]')).to_have_count(1)
    v.page.evaluate("openStock('ACME')")
    expect(v.page.locator("#sv-back")).to_have_class("on")
    expect(v.page.locator("#sv .sym")).to_have_text("ACME")
    expect(v.page.locator("#sv svg").first).to_be_visible(timeout=15000)          # the price chart drew
    expect(v.page.locator("#sv")).to_contain_text("52W high", ignore_case=True)
    # the seed's last ACME close: 100 * 1.0005^259 * (1 + 0.01 sin(259/9)) = 113.822 * 0.99518 = 113.27,
    # against 113.33 the session before: -0.05%
    expect(v.page.locator("#sv .px")).to_contain_text("₹113.27")
    expect(v.page.locator("#sv")).to_contain_text("-0.05%")
    v.page.evaluate("svClose()")
    expect(v.page.locator("#sv-back")).not_to_have_class("on")
    v.assert_clean()


def test_wealth_page_every_tab_opens_and_loads(ctx, site):
    from playwright.sync_api import expect
    base, _ = site
    v = Visit(ctx, base, "/wealth")
    assert v.page.title() == "ATIP Wealth"
    buttons = v.page.locator("#tabs button")
    expect(buttons).to_have_count(len(WEALTH_TABS))
    assert [buttons.nth(i).get_attribute("data-t") for i in range(buttons.count())] == WEALTH_TABS
    for tab in WEALTH_TABS:
        v.page.click(f'#tabs button[data-t="{tab}"]')
        expect(v.page.locator(f"#t_{tab}")).to_be_visible()
        expect(v.page.locator(f'#tabs button[data-t="{tab}"]')).to_have_class("on")
        for other in WEALTH_TABS:
            if other != tab:
                expect(v.page.locator(f"#t_{other}")).to_be_hidden()
        v.settle(200)
        assert not v.page.locator("#ld").inner_text().startswith("could not load"), \
            f"tab {tab}: {v.page.locator('#ld').inner_text()}"
        assert v.errors == [], f"tab {tab}: {v.errors}"
    v.assert_clean()


PAGES = [
    # path, title, [(selector, text it must contain)] -- each text comes from the seed or an empty book
    ("/trading", "ATIP Trading", [("#st", "PAPER"), ("#lim", "max_position_pc"),
                                  ("#phead", "the PAPER book holds nothing"), ("#pmsg", "as of")]),
    ("/baskets", "ATIP Baskets & SIP", [("#env", "broker_env: PAPER"), ("#blist", "none")]),
    ("/data-platform", "ATIP Data Platform", [("#tabs", "Data lake"), ("#lake", "atip_data/lake")]),
    ("/compliance", "ATIP Compliance", [("#tabs", "Checks"), ("#chk", "No run yet")]),
    ("/m", "ATIP", [("#app", "Market health"), ("#app", "62"), ("#app", "BULL")]),    # seed: MH 62, BULL
]


@pytest.mark.parametrize("path,title,checks", PAGES, ids=[p[0] for p in PAGES])
def test_page_renders_its_key_elements_without_errors(ctx, site, path, title, checks):
    from playwright.sync_api import expect
    base, info = site
    v = Visit(ctx, base, path)
    assert v.page.title() == title
    for selector, text in checks:
        expect(v.page.locator(selector)).to_contain_text(text, timeout=15000)
    if path == "/m":
        expect(v.page.locator("#upd")).to_contain_text(f"scores {info['last_session']}")
    v.assert_clean()


def test_data_platform_and_compliance_tabs_switch_without_errors(ctx, site):
    base, _ = site
    for path in ("/data-platform", "/compliance"):
        v = Visit(ctx, base, path)
        tabs = v.page.locator("#tabs > *")
        n = tabs.count()
        assert n >= 3, f"{path}: only {n} tabs"
        for i in range(n):
            tabs.nth(i).click()
            v.settle(150)
        v.assert_clean()
