"""
Paper broker and environment routing.

The most important tests here are the negative ones: that a misconfiguration
resolves to PAPER, and that no simulated path can reach Dhan's trading API.
"""

import json

import pytest

from orders import environment as E


class FakeQuotes:
    """Stands in for the market-data client so tests need no network."""

    def __init__(self, prices):
        self.prices = prices


@pytest.fixture
def broker(temp_db, monkeypatch):
    """A paper broker whose 'live' prices are fixed, so fills are predictable."""
    import orders.paper as paper
    from db.schema import get_connection

    monkeypatch.setattr(paper.PaperBroker, "_ltp",
                        lambda self, symbol: {"ACME": 100.0, "PRICEY": 2000.0}.get(symbol))
    b = paper.PaperBroker(conn=get_connection(),
                          overrides={"paper_opening_balance": 100000.0,
                                     "paper_slippage_bps": 0.0,
                                     "paper_brokerage_pct": 0.0})
    b.reset(100000.0)
    return b


# ── environment routing ───────────────────────────────────────────────────

def test_default_environment_is_paper(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "CONFIG_PATH", tmp_path / "missing.json")
    assert E.broker_env() == E.PAPER
    assert E.is_live() is False


@pytest.mark.parametrize("value", ["", "live-ish", "PROD", "yes", None, 123, "SANDBOX2"])
def test_unrecognised_environment_falls_back_to_paper(tmp_path, monkeypatch, value):
    """
    A config typo must mean "nothing was traded", never "real money moved".
    """
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"broker_env": value}), encoding="utf-8")
    monkeypatch.setattr(E, "CONFIG_PATH", p)
    assert E.broker_env() == E.PAPER
    assert E.is_live() is False


def test_unreadable_config_falls_back_to_paper(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(E, "CONFIG_PATH", p)
    assert E.broker_env() == E.PAPER


@pytest.mark.parametrize("value,expected", [
    ("LIVE", E.LIVE), ("live", E.LIVE), (" Live ", E.LIVE),
    ("SANDBOX", E.SANDBOX), ("PAPER", E.PAPER),
])
def test_explicit_environments_are_honoured(tmp_path, monkeypatch, value, expected):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"broker_env": value}), encoding="utf-8")
    monkeypatch.setattr(E, "CONFIG_PATH", p)
    assert E.broker_env() == expected


def test_only_live_is_live(tmp_path, monkeypatch):
    for env, live in (("PAPER", False), ("SANDBOX", False), ("LIVE", True)):
        p = tmp_path / f"{env}.json"
        p.write_text(json.dumps({"broker_env": env}), encoding="utf-8")
        monkeypatch.setattr(E, "CONFIG_PATH", p)
        assert E.is_live() is live, f"{env} reported is_live={not live}"


def test_paper_client_is_never_a_real_dhan_client(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "CONFIG_PATH", tmp_path / "none.json")
    client, env = E.get_execution_client()
    from orders.paper import PaperBroker
    assert env == E.PAPER
    assert isinstance(client, PaperBroker)
    client.close()


# ── fills ─────────────────────────────────────────────────────────────────

def test_market_buy_fills_and_debits_cash(broker):
    r = broker.place_order(transaction_type="BUY", quantity=10, symbol="ACME")
    assert r["status"] == "success"
    assert r["data"]["orderStatus"] == "TRADED"
    assert r["data"]["averageTradedPrice"] == 100.0
    assert broker.balance == 99000.0
    assert broker._position_qty("ACME") == 10


def test_sell_credits_cash_and_reduces_position(broker):
    broker.place_order(transaction_type="BUY", quantity=10, symbol="ACME")
    broker.place_order(transaction_type="SELL", quantity=4, symbol="ACME")
    assert broker._position_qty("ACME") == 6
    assert broker.balance == 99400.0


def test_cannot_sell_more_than_held(broker):
    broker.place_order(transaction_type="BUY", quantity=5, symbol="ACME")
    r = broker.place_order(transaction_type="SELL", quantity=50, symbol="ACME")
    assert r["status"] == "failure"
    assert "paper position holds 5" in r["remarks"]["error_message"]
    assert broker._position_qty("ACME") == 5


def test_insufficient_funds_is_rejected(broker):
    r = broker.place_order(transaction_type="BUY", quantity=100, symbol="PRICEY")
    assert r["status"] == "failure"
    assert "insufficient paper funds" in r["remarks"]["error_message"]
    assert broker.balance == 100000.0


def test_no_live_quote_refuses_rather_than_inventing_a_price(broker):
    """Making up a fill price when the market is unavailable is worse than failing."""
    r = broker.place_order(transaction_type="BUY", quantity=1, symbol="UNKNOWN")
    assert r["status"] == "failure"
    assert "no live quote" in r["remarks"]["error_message"]


def test_zero_quantity_is_rejected(broker):
    r = broker.place_order(transaction_type="BUY", quantity=0, symbol="ACME")
    assert r["status"] == "failure"


def test_slippage_always_hurts(temp_db, monkeypatch):
    import orders.paper as paper
    from db.schema import get_connection
    monkeypatch.setattr(paper.PaperBroker, "_ltp", lambda self, s: 100.0)
    b = paper.PaperBroker(conn=get_connection(),
                          overrides={"paper_slippage_bps": 50.0, "paper_brokerage_pct": 0.0})
    b.reset(100000.0)
    buy = b.place_order(transaction_type="BUY", quantity=1, symbol="ACME")
    assert buy["data"]["averageTradedPrice"] > 100.0, "a buy must fill higher"
    sell = b.place_order(transaction_type="SELL", quantity=1, symbol="ACME")
    assert sell["data"]["averageTradedPrice"] < 100.0, "a sell must fill lower"


def test_limit_order_does_not_fill_until_crossed(broker):
    r = broker.place_order(transaction_type="BUY", quantity=1, symbol="ACME",
                           order_type="LIMIT", price=90.0)
    assert r["data"]["orderStatus"] == "PENDING"
    assert broker._position_qty("ACME") == 0
    r2 = broker.place_order(transaction_type="BUY", quantity=1, symbol="ACME",
                            order_type="LIMIT", price=110.0)
    assert r2["data"]["orderStatus"] == "TRADED"


def test_pending_limit_order_can_be_cancelled(broker):
    r = broker.place_order(transaction_type="BUY", quantity=1, symbol="ACME",
                           order_type="LIMIT", price=90.0)
    oid = r["data"]["orderId"]
    assert broker.cancel_order(oid)["status"] == "success"
    assert broker.cancel_order(oid)["status"] == "failure"   # already cancelled


def test_realized_pnl_is_tracked(broker):
    broker.place_order(transaction_type="BUY", quantity=10, symbol="ACME")
    broker._ltp = lambda s: 120.0
    broker.place_order(transaction_type="SELL", quantity=10, symbol="ACME")
    pos = broker.conn.execute(
        "SELECT realized_pnl FROM paper_position WHERE symbol='ACME'").fetchone()
    assert pos[0] == 200.0


def test_fund_limits_mirror_the_live_payload_shape(broker):
    """
    available_balance() reads Dhan's misspelled key. If the simulator "fixed"
    the spelling it would report a zero balance through the real code path.
    """
    data = broker.get_fund_limits()["data"]
    assert "availabelBalance" in data
    assert data["availabelBalance"] == broker.balance


def test_orders_and_positions_are_queryable(broker):
    broker.place_order(transaction_type="BUY", quantity=3, symbol="ACME")
    assert len(broker.get_order_list()["data"]) == 1
    assert broker.get_positions()["data"][0]["netQty"] == 3
    assert broker.get_holdings()["data"][0]["totalQty"] == 3


def test_summary_reports_state(broker):
    broker.place_order(transaction_type="BUY", quantity=2, symbol="ACME")
    s = broker.summary()
    assert s["balance"] == 99800.0
    assert s["orders"]["TRADED"] == 1


def test_reset_clears_only_paper_tables(broker):
    broker.place_order(transaction_type="BUY", quantity=2, symbol="ACME")
    broker.reset(50000.0)
    assert broker.balance == 50000.0
    assert broker.get_order_list()["data"] == []
    assert broker._position_qty("ACME") == 0


# ── quote pacing ──────────────────────────────────────────────────────────

def test_quote_is_cached_within_ttl(temp_db, monkeypatch):
    """
    Dhan's Quote API allows one request per second, and a single order costs two
    quote calls. Without a cache the second of two back-to-back orders was
    refused for "no live quote" — seen on the first end-to-end run.
    """
    import orders.paper as paper
    from db.schema import get_connection

    calls = []

    def fake_fetch(symbols, source=None):
        import pandas as pd
        calls.append(symbols[0])
        return pd.DataFrame([{"symbol": symbols[0], "ltp": 100.0}])

    monkeypatch.setattr("data.dhan.fetch_live_quotes", fake_fetch)
    b = paper.PaperBroker(conn=get_connection(),
                          overrides={"paper_slippage_bps": 0.0, "paper_brokerage_pct": 0.0})
    b.reset(100000.0)
    b._quote_cache.clear()

    b.place_order(transaction_type="BUY", quantity=1, symbol="ACME")
    b.place_order(transaction_type="BUY", quantity=1, symbol="ACME")
    assert len(calls) == 1, f"expected one quote fetch, got {len(calls)}"


def test_stale_cache_entry_is_refetched(temp_db, monkeypatch):
    """A cache that never expires would fill today's order at yesterday's price."""
    import orders.paper as paper
    from db.schema import get_connection

    calls = []

    def fake_fetch(symbols, source=None):
        import pandas as pd
        calls.append(symbols[0])
        return pd.DataFrame([{"symbol": symbols[0], "ltp": 100.0 + len(calls)}])

    monkeypatch.setattr("data.dhan.fetch_live_quotes", fake_fetch)
    monkeypatch.setattr(paper.PaperBroker, "QUOTE_TTL_SECONDS", 0.0)
    b = paper.PaperBroker(conn=get_connection(),
                          overrides={"paper_slippage_bps": 0.0, "paper_brokerage_pct": 0.0})
    b.reset(100000.0)
    b._quote_cache.clear()

    b.place_order(transaction_type="BUY", quantity=1, symbol="ACME")
    b.place_order(transaction_type="BUY", quantity=1, symbol="ACME")
    assert len(calls) == 2
