"""UX audit H7 in a real browser (3 Nov 2026): a confirmation refused with 409 (the price moved past the slippage
limit) opens the shell's dialog with the trigger price, the price now and the move in ₹ and %. Cancel sends nothing;
"Place at ₹X" re-confirms with force. The 409 and the forced confirm are mocked: no order is placed. Skips without
Playwright / Chromium, like tests/test_w39_ui_smoke.py, whose fixtures it reuses."""

import pytest

from tests.test_w39_ui_smoke import Visit, browser, site  # noqa: F401  (pytest fixtures)

MOVED = {"error": "price moved +2.10% against you since the trigger", "slippage": True,
         "trigger_price": 100.0, "current_price": 102.1, "drift_pct": 2.1}


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_price_moved_dialog(browser, site, width, height):
    import json
    from playwright.sync_api import expect
    base, _ = site
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
    forced = []

    def confirm(route):
        if "force=true" in route.request.url:
            forced.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"result": {"bracket_legs": []}}))
        else:
            route.fulfill(status=409, content_type="application/json", body=json.dumps(MOVED))
    ctx.route("**/api/orders/R1/confirm**", confirm)
    try:
        v = Visit(ctx, base, "/")
        p = v.page
        p.evaluate("window._pendRules={R1:{id:'R1',symbol:'ACME',side:'BUY'}}; setTimeout(function(){confirmRule('R1')},0); 1")
        d = p.locator("#ax-ask")
        expect(d).to_be_visible()
        for txt in ("Trigger price", "₹100.00", "Price now", "₹102.10", "▲ +₹2.10", "+2.10%", "BUY ACME"):
            expect(d).to_contain_text(txt)
        expect(d.locator('[data-a="1"]')).to_have_text("Place at ₹102.10")
        expect(d.locator('[data-a="0"]')).to_be_focused()               # the safe answer has the focus
        d.locator('[data-a="0"]').click()
        expect(d).to_be_hidden()
        assert forced == []
        p.evaluate("setTimeout(function(){confirmRule('R1')},0); 1")
        expect(d).to_be_visible()
        d.locator('[data-a="1"]').click()
        p.wait_for_timeout(300)
        assert len(forced) == 1
        v.assert_clean()
    finally:
        ctx.close()
