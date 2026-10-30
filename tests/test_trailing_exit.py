"""The owner's trailing exit for the cash pools (30 Oct 2026), orders/rules.py + orders/sessions.py:
no sale at +3 %; from +3 % the stop trails 1 % under the best price; after 15 days a price at +3 %
or more sells everything; a fall to -2 % sells everything. Orders run orders.rules -> orders.broker ->
orders.paper for real on a throwaway database (the `paper` fixture: no slippage, no charges)."""

import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from tests.test_session_pools import _do, _entry, _ledger, _limits, _pool, _tick, api  # noqa: F401  (fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

TRAIL = {"trail_pct": 1, "hold_days": 15}


def _legs(entry_id):
    from orders import rules
    return {l["role"]: l for l in rules.list_rules() if l.get("parent_rule_id") == entry_id}


def _bought(feed, **pool):
    """S1 with a Rs 25,000 pool and the trailing exit buys 250 ACME at 100."""
    _pool("S1", **{**TRAIL, **pool})
    r = _entry()
    _tick(feed, ACME=100.0)
    return r


def test_linking_an_entry_applies_the_trailing_exit(paper):
    _pool("S1", **TRAIL)
    r = _entry()
    assert (r["trail_enabled"], r["trail_value"], r["trail_after_pct"], r["bracket_target_after_days"]) == (1, 1.0, 3.0, 15.0)
    assert (r["bracket_target_pct"], r["bracket_stop_pct"]) == (3.0, 2.0)


def test_the_stop_waits_at_minus_2_then_trails_1_pct_under_the_high(paper):
    from orders import rules
    r = _bought(paper)
    legs = _legs(r["id"])
    stop, sell = legs["STOP"], legs["TARGET"]
    assert (stop["resolved_trigger_price"], stop["trail_activate_price"], stop["quantity_value"]) == (98.0, 103.0, 250)
    assert sell["resolved_trigger_price"] == 103.0 and sell["not_before"] > datetime.now().isoformat()
    _tick(paper, ACME=102.9)
    assert rules.get_rule(stop["id"])["resolved_trigger_price"] == 98.0          # under +3 %: no trailing yet
    _tick(paper, ACME=103.0)                                                     # +3 %: not sold, trailing starts
    assert rules.get_rule(sell["id"])["status"] == rules.ACTIVE
    assert rules.get_rule(stop["id"])["resolved_trigger_price"] == 101.97        # 103 x 0.99
    _tick(paper, ACME=106.0)
    assert rules.get_rule(stop["id"])["resolved_trigger_price"] == 104.94        # follows the price up
    _tick(paper, ACME=105.5)
    assert rules.get_rule(stop["id"])["resolved_trigger_price"] == 104.94        # and never down
    h = _ledger()["open"][0]
    assert (h["stop"], h["trailing"], h["sell_at"]) == (104.94, True, 103.0)
    _tick(paper, ACME=104.9)                                                     # 1 % off the high: all 250 go
    assert rules.get_rule(stop["id"])["status"] == rules.EXECUTED
    assert rules.get_rule(sell["id"])["status"] == rules.CANCELLED
    L = _ledger()
    assert L["open"] == [] and L["cash"] == 26225.0                              # 25,000 + 250 x 4.90


def test_a_fall_sells_everything_at_minus_2(paper):
    from orders import rules
    r = _bought(paper)
    legs = _legs(r["id"])
    _tick(paper, ACME=97.9)
    assert rules.get_rule(legs["STOP"]["id"])["execution_quantity"] == 250
    assert rules.get_rule(legs["TARGET"]["id"])["status"] == rules.CANCELLED
    assert _ledger()["cash"] == 24475.0                                          # 25,000 - 250 x 2.10


def test_after_15_days_a_price_at_3_pct_or_more_sells_everything(paper):
    from db.schema import get_connection
    from orders import rules
    r = _bought(paper)
    sell = _legs(r["id"])["TARGET"]
    c = get_connection()
    c.execute("UPDATE order_rules SET not_before = ? WHERE id = ?",
              ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), sell["id"]))
    c.commit(); c.close()                                                        # 15 days have passed
    _tick(paper, ACME=102.5)
    assert rules.get_rule(sell["id"])["status"] == rules.ACTIVE                  # under +3 %: it keeps holding
    _tick(paper, ACME=103.4)
    assert rules.get_rule(sell["id"])["execution_quantity"] == 250
    assert _ledger()["cash"] == 25850.0                                          # 25,000 + 250 x 3.40



def test_the_days_order_limit_never_refuses_the_stop(paper, tmp_path):
    """One order a day: the buy uses it up, and the stop-loss sale still goes through."""
    from orders import rules
    _limits(tmp_path, max_orders_per_day=1)
    r = _bought(paper)
    assert rules.get_rule(r["id"])["status"] == rules.EXECUTED
    _tick(paper, ACME=97.9)
    assert rules.get_rule(_legs(r["id"])["STOP"]["id"])["status"] == rules.EXECUTED
    assert _ledger()["open"] == []

def test_trailing_without_hold_days_has_no_sale_at_the_target(paper):
    _pool("S1", trail_pct=1)
    r = _entry()
    assert r["bracket_target_pct"] is None and r["trail_after_pct"] == 3.0
    _tick(paper, ACME=100.0)
    assert set(_legs(r["id"])) == {"STOP"}


def test_the_exit_is_validated_described_and_reaches_queued_entries(paper):
    from orders import sessions
    _pool("S1")
    r = _entry()                                                                 # queued with the fixed exit
    s = _do(sessions.get_session, "S1")
    assert sessions.exit_rule(s) == "sells all at +3%, stop −2%"
    with pytest.raises(ValueError, match="trail_pct"):
        _do(sessions.update_session, "S1", {"trail_pct": "abc"})
    with pytest.raises(ValueError, match="positive"):
        _do(sessions.update_session, "S1", {"hold_days": -3})
    _do(sessions.update_session, "S1", TRAIL)
    from orders import rules
    q = rules.get_rule(r["id"])
    assert (q["trail_enabled"], q["trail_after_pct"], q["bracket_target_after_days"]) == (1, 3.0, 15.0)
    assert _ledger()["exit_rule"] == ("stop −2%; from +3% the stop trails 1% under the high; "
                                      "after 15 days sells at +3% or more")
    _do(sessions.update_session, "S1", {"trail_pct": "off"})
    q = rules.get_rule(r["id"])
    assert (q["trail_enabled"], q["trail_after_pct"], q["bracket_target_after_days"]) == (0, None, None)
    assert _do(sessions.get_session, "S1")["hold_days"] == 15                    # kept for when it is back on


def test_the_pools_api_and_pnl_page_take_the_trailing_exit(api):
    client, h = api
    r = client.post("/api/sessions/pools", json={"total": 50000, "capital": 25000, **TRAIL}, headers=h)
    assert r.status_code == 200 and r.json()["sessions"] == ["S1", "S2"]
    s = client.get("/api/command/pnl?scope=pools").json()["sessions"]
    assert {x["exit_rule"] for x in s} == {"stop −2%; from +3% the stop trails 1% under the high; "
                                           "after 15 days sells at +3% or more"}
    assert client.post("/api/sessions/S1", json={"trail_pct": "x"}, headers=h).status_code == 400


@pytest.mark.parametrize("page", ["/command", "/command/pnl", "/command/setup"])
def test_page_scripts_parse(api, page, tmp_path):
    """Every inline script on the pages these exits show on passes node --check."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    client, h = api
    html = client.get(page, headers=h).text
    scripts = [s for s in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S) if s.strip()]
    assert scripts
    for i, js in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
