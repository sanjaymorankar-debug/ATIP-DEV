"""UXR-01 in a real browser: /command loads clean on the seeded market, the AUTO switch needs its
second step, and the kill switch halts through the guarded API. Skips without Chromium, like
tests/test_w39_ui_smoke.py, whose fixtures it reuses."""

import os

import pytest

from tests.test_w39_ui_smoke import Visit, browser, site  # noqa: F401  (pytest fixtures)

SHOTS = os.environ.get("ATIP_UX_SHOTS")      # set to a folder to keep screenshots for the test report


@pytest.fixture
def halt_flag(tmp_path, monkeypatch):
    from orders import risk
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    return risk


@pytest.mark.parametrize("width,height,name", [(1366, 900, "desktop"), (390, 844, "phone")])
def test_command_center_flows(browser, site, halt_flag, width, height, name):
    from playwright.sync_api import expect
    base, _ = site
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    try:
        v = Visit(ctx, base, "/command")
        p = v.page
        expect(p.locator("#headline")).to_contain_text("sessions running")
        expect(p.locator("#tiles .tile")).to_have_count(10)
        # no horizontal page scroll at this width
        assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        if SHOTS:
            p.screenshot(path=f"{SHOTS}/command-{name}.png", full_page=True)
        # AUTO needs the acknowledgement
        p.click('#tiles [data-sid="S1"]')
        p.click('#spModes [data-mode="AUTO"]')
        expect(p.locator("#autoBox")).to_have_class("autobox open")
        expect(p.locator("#abGo")).to_be_disabled()
        p.check("#abAck")
        p.click("#abGo")
        expect(p.locator('#tiles [data-sid="S1"] .mode')).to_have_text("AUTO")
        if SHOTS:
            p.screenshot(path=f"{SHOTS}/command-{name}-session.png", full_page=True)
        # kill switch
        p.click("#killBtn")
        expect(p.locator("#haltModal")).to_have_class("modal open")
        p.click("#hmGo")
        expect(p.locator("#haltBanner")).to_be_visible()
        expect(p.locator("#killBtn")).to_contain_text("Resume")
        assert halt_flag.halted()[0] is True
        if SHOTS:
            p.screenshot(path=f"{SHOTS}/command-{name}-halted.png", full_page=True)
        v.assert_clean()
    finally:
        ctx.close()
