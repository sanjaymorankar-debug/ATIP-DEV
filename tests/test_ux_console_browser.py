"""UXR-06..09 in a real browser: the four console screens load clean on the seeded market at desktop and
phone width, with no horizontal page scroll. Skips without Chromium, like tests/test_ux_command_browser.py."""

import os

import pytest

from tests.test_w39_ui_smoke import Visit, browser, site  # noqa: F401  (pytest fixtures)

SHOTS = os.environ.get("ATIP_UX_SHOTS")


@pytest.mark.parametrize("width,height,name", [(1366, 900, "desktop"), (390, 844, "phone")])
@pytest.mark.parametrize("path,ready", [("/command/signals", "#dStory"), ("/command/setup", "#kv"),
                                        ("/command/monitor", "#sub"), ("/command/journal", "#ses tr")])
def test_console_screen_loads_clean(browser, site, width, height, name, path, ready):
    from playwright.sync_api import expect
    base, _ = site
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    try:
        v = Visit(ctx, base, path)
        p = v.page
        expect(p.locator(ready).first).to_be_visible()
        expect(p.locator("#sub, #kv").first).not_to_contain_text("Could not load")
        assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        if SHOTS:
            p.screenshot(path=f"{SHOTS}/{path.rsplit('/', 1)[1]}-{name}.png", full_page=True)
        v.assert_clean()
    finally:
        ctx.close()
