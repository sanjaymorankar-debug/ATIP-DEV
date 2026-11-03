"""UI fixes v2 in a real browser (skips without Chromium, like tests/test_w39_ui_smoke.py): the automated DOM scan of
the brief's section 5 over every route at 1440 and 360 px (no raw floats, None / NaN / ₹— / escaped entities, red
0.00 %, developer text, second navigation bars, unreachable clickables, horizontal page scroll on the phone), the
order ticket as a bottom sheet on a phone with B / Esc, the watchlist with W, and no layout shift while live prices
refresh. The scan itself is tools/ui_review.py scan_page, shared with `python -m tools.ui_review scan`."""

import pytest

from tests.test_w39_ui_smoke import browser  # noqa: F401  (pytest fixture)
from tools import load_test as LT
from tools import ui_review as UR


@pytest.fixture
def site(temp_db, tmp_path, monkeypatch):
    """The seeded dashboard with the 3 Nov findings, over real HTTP (cwd = tmp_path; DB = temp_db, conftest)."""
    pytest.importorskip("fastapi")
    import db.schema as schema
    from dashboard import security
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    with LT.block_network():
        LT.seed(tmp_path, extra_symbols=10)
        conn = schema.get_connection()
        UR.seed_review(conn)
        conn.close()
        from dashboard import server
        with LT.serve(server.app) as base:
            yield base


def _ctx(browser, base, w, h):
    c = browser.new_context(viewport={"width": w, "height": h})
    c.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    return c


def test_every_route_passes_the_dom_scan(browser, site):
    bad = {}
    for width, height in ((1440, 900), (360, 780)):
        _scan(browser, site, width, height, bad)
    assert bad == {}, bad


def _scan(browser, site, width, height, bad):
    ctx = _ctx(browser, site, width, height)
    try:
        for r in UR.ROUTES:
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            resp = pg.goto(site + r, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(500)
            res = UR.scan_page(pg)
            res["js_errors"] = errs
            if resp is not None and resp.status >= 400:
                res["load_error"] = f"HTTP {resp.status}"
            p = UR.problems(res, phone=width < 768)
            if p:
                bad[f"{r} @{width}"] = p
            pg.close()
    finally:
        ctx.close()


def test_phone_ticket_is_a_bottom_sheet_with_keys(browser, site):
    ctx = _ctx(browser, site, 375, 812)
    try:
        pg = ctx.new_page()
        pg.goto(site + "/stock/ACME", wait_until="networkidle")
        pg.wait_for_timeout(500)
        pg.keyboard.press("b")
        pg.wait_for_timeout(400)
        box = pg.locator("#ax-ticket").bounding_box()
        assert box and abs(box["x"]) < 1 and abs(box["width"] - 375) < 2 and abs(box["y"] + box["height"] - 812) < 2
        assert "Buy ACME" in pg.locator("#ax-ticket-h").inner_text()
        pg.fill("#tk-q", "10")
        pg.fill("#tk-sl", "108")
        pg.wait_for_timeout(1200)
        assert "₹ at risk" in pg.locator("[data-tk-sum]").inner_text()
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        assert "open" not in (pg.locator("#ax-ticket").get_attribute("class") or "")
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        ctx.close()


def test_watchlist_rail_with_w_and_no_layout_shift(browser, site):
    ctx = _ctx(browser, site, 1440, 900)
    try:
        pg = ctx.new_page()
        pg.add_init_script("""window.__cls=0;new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)window.__cls+=e.value})
          .observe({type:'layout-shift',buffered:true});""")
        pg.goto(site + "/stock/ACME", wait_until="networkidle")
        pg.wait_for_timeout(3500)                                # quotes, depth and the P&L strip refresh in place
        assert pg.evaluate("window.__cls") < 0.1
        assert pg.locator("#ax-wl").is_visible()                 # open by default on the Trade screens at 1440 px
        pg.keyboard.press("w")
        pg.wait_for_timeout(200)
        assert not pg.locator("#ax-wl").is_visible()
        pg.goto(site + "/home", wait_until="networkidle")
        pg.wait_for_timeout(300)
        assert not pg.locator("#ax-wl").is_visible()             # the choice is remembered
        pg.keyboard.press("w")
        pg.wait_for_timeout(600)
        assert pg.locator("#ax-wl .ax-wl-row").count() >= 1
    finally:
        ctx.close()
