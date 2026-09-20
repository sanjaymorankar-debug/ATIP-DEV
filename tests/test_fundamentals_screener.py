"""
Screener.in is the only fundamentals source ATIP has without an Alpha Vantage
key, and it silently returned nothing for every symbol: fetch_screener called
re.sub while `re` was only imported inside the condition guarding it, so each
symbol raised NameError and was swallowed by the except (atip.log, 2026-09-19
08:00: "Screener AJANTPHARM: name 're' is not defined" for 35 symbols).
"""

import pytest

PAGE = """
<html><body><ul id="top-ratios">
  <li><span class="name">Stock P/E</span><span class="value"><span class="nowrap">23.4</span></span></li>
  <li><span class="name">Price to Book</span><span class="value">4.10</span></li>
  <li><span class="name">Return on equity</span><span class="value">18.2 %</span></li>
  <li><span class="name">Debt to equity</span><span class="value">0.35</span></li>
  <li><span class="name">Promoter holding</span><span class="value">62.5 %</span></li>
  <li><span class="name">Market Cap</span><span class="value">Rs. --</span></li>
</ul></body></html>
"""


class _Resp:
    status_code = 200
    text = PAGE


def test_screener_parses_ratios(monkeypatch):
    pytest.importorskip("bs4")
    from data import fundamentals
    monkeypatch.setattr(fundamentals.requests, "get", lambda *a, **k: _Resp())

    out = fundamentals.fetch_screener("AJANTPHARM")

    assert out, "a parsed page must not come back empty"
    assert out["symbol"] == "AJANTPHARM" and out["source"] == "screener_in"
    assert out["pe_ratio"] == 23.4
    assert out["pb_ratio"] == 4.10
    assert out["roe"] == 18.2
    assert out["debt_equity"] == 0.35
    assert out["promoter_hold"] == 62.5
    assert out["fundamental_score"] is not None


def test_screener_ignores_a_value_with_no_number(monkeypatch):
    """A loss-making company shows "Stock P/E  " with no figure at all."""
    pytest.importorskip("bs4")
    from data import fundamentals

    class _NoPE(_Resp):
        text = PAGE.replace('<span class="nowrap">23.4</span>', "")

    monkeypatch.setattr(fundamentals.requests, "get", lambda *a, **k: _NoPE())
    out = fundamentals.fetch_screener("IFCI")
    assert out["pe_ratio"] is None, "a blank P/E must stay empty, not become 0"
    assert out["roe"] == 18.2, "the rest of the page must still parse"
