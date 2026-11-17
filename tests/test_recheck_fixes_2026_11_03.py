"""
3 Nov 2026 recheck of the redesign + UX audit code (master 897f9af): what the full-code review and the every-screen
sweep found, one test each. Accounting fixes are in tests/test_session_cycles.py.

  - the hidden HOLD Trade of the Day card really stays hidden (the page's .tod-card{display:grid} beat [hidden]);
  - Trade signals offers a BUY set-up only on a BUY signal (C1 gap);
  - the stock view's Buy reads the signal of the latest scored date only, not a weeks-old one;
  - the price columns' "●live" follows the one feed label instead of always saying live;
  - "Halted" in the top bar focuses Command Center's Resume when it is already on the page;
  - /ops waits for the deferred atip.js before calling AX (it threw "AX is not defined");
  - the options builder drops an analysis result made for legs that have since changed;
  - suitability() matches each sold leg with a cover that outlives it (two spreads in two expiries = defined risk);
  - /login is on the design system (tokens, light and dark) without the shell.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_hidden_trade_of_day_card_stays_hidden():
    src = (ROOT / "dashboard/server.py").read_text()
    assert ".tod-card[hidden]{{display:none}}.tod-card{{" in src     # before the card's own display:grid


def test_trade_signals_offers_buy_set_up_only_on_buy():
    from dashboard.command_more import SIGNALS_JS
    assert "const side=r.signal==='SELL'?'SELL':r.signal==='BUY'?'BUY':'';" in SIGNALS_JS
    assert "No buy · signal is" in SIGNALS_JS


def test_stock_view_buy_reads_the_latest_scored_date(temp_db):
    from db.schema import get_connection, init_db
    from dashboard import stock_view as SV
    init_db()
    c = get_connection()
    try:
        c.execute("INSERT INTO ai_scores (symbol, date, `signal`, atip_score) VALUES ('OLDBUY','2026-10-10','BUY',70)")
        c.execute("INSERT INTO ai_scores (symbol, date, `signal`, atip_score) VALUES ('ACME','2026-11-02','HOLD',50)")
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES ('OLDBUY','2026-10-10',100,100,100,100,1000,'EQ')")
        c.commit()
        d = SV.stock_history(c, "OLDBUY")
    finally:
        c.close()
    assert d["scored_date"] == "2026-11-02" and d["scores"][-1]["date"] == "2026-10-10"   # old BUY, not current
    js = (ROOT / "dashboard/stock_view.py").read_text()
    assert "if(!l||(d.scored_date&&l.date!==d.scored_date))return '';" in js


def test_price_columns_follow_the_one_feed_label():
    src = (ROOT / "dashboard/server.py").read_text()
    assert "●live" not in src
    assert src.count('class="ax-feedmini" data-ax-feed') == 8
    css = (ROOT / "dashboard/static/atip/atip.css").read_text()
    assert '.ax-feedmini[data-state="stale"]' in css


def test_halted_click_focuses_resume_on_command_center():
    js = (ROOT / "dashboard/static/atip/atip.js").read_text()
    m = re.search(r"if \(d\.halted\) \{(.*?)\n    \}", js, re.S)
    assert m and 'getElementById("killBtn")' in m.group(1) and "res.focus()" in m.group(1)


def test_ops_page_waits_for_atip_js():
    from dashboard import ux_pages as P
    src = Path(P.__file__).read_text()
    i = src.index("Operating status")
    script = src[i:src.index("</script>", i)]
    assert "document.addEventListener('DOMContentLoaded',function(){function kv(o)" in script


def test_options_builder_drops_a_result_for_changed_legs():
    from dashboard.w39_page import render_options
    js = render_options("tok")
    assert "function schedule(){clearTimeout(TM);SEQ++;if(!LEGS.length){empty();return}$('ost')" in js


N, F = "2026-11-25", "2026-12-30"


@pytest.mark.parametrize("legs,tier", [
    ([("CE", "BUY", N), ("CE", "SELL", N), ("CE", "BUY", F), ("CE", "SELL", F)], "Intermediate"),   # 2 call spreads
    ([("PE", "SELL", N), ("PE", "BUY", N), ("PE", "SELL", F), ("PE", "BUY", F)], "Intermediate"),   # 2 put spreads
    ([("CE", "BUY", N), ("CE", "SELL", F)], "Advanced"),             # a near call does not cover a far short call
    ([("CE", "BUY", F), ("CE", "SELL", N)], "Intermediate"),         # a far call covers a near short call
])
def test_suitability_matches_each_sold_leg_with_a_cover_that_outlives_it(legs, tier):
    from quant.options_strategy import suitability
    out = suitability([{"kind": k, "side": s, "lots": 1, "expiry": e} for k, s, e in legs])
    assert out["tier"] == tier


def test_login_is_on_the_design_system_without_the_shell():
    from dashboard.enterprise_page import render_login
    h = render_login()
    assert "/static/atip/atip.css" in h and "ax-top" not in h and "class='ax-native'" in h
    for i, label in (("u", "Username"), ("p", "Password")):
        assert f'<label for="{i}">{label}</label>' in h and f'id="{i}"' in h
    assert "--bg:#0f172a" not in h                                   # not the old always-dark page
    # 18 Nov 2026: no tenant box; the MFA box is there but hidden until the server asks for a code
    assert 'id="t"' not in h and "Tenant" not in h and "tenant_id:null" in h
    assert "<div id='mfa' hidden>" in h and 'id="o"' in h and "mfa_required" in h
