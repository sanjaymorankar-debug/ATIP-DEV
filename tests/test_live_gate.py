"""
Real money needs every switch, on every path.

ATIP's order paths reach a real Dhan account only when ALL of these hold:

  W4 / OMS  (execution/config.live_gate):  execution.mode LIVE, execution.live_trading_enabled
            a JSON true, and the ops environment production;
  W1 rules  (orders/broker._place_order):  broker_env LIVE and confirm=True, and then the same
            master switch (ops/trading_safety.live_trading_enabled), else BLOCKED_LIVE_DISABLED.

The paper-default side is pinned by tests/test_paper_broker.py. These pin the gate
itself, which nothing tested directly: a regression that opened it would have
shown up only as a real order. Nothing here can reach a broker: the execution
client is a stub that records what it is sent.
"""

import json

import pytest


@pytest.fixture
def config(tmp_path, monkeypatch):
    """One throwaway config.json for every module that reads one; returns a writer."""
    from execution import config as X
    from ops import config as O
    from orders import environment, risk
    cfg = tmp_path / "config.json"
    for mod in (environment, X, O, risk):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.delenv("ATIP_ENV", raising=False)

    def write(**keys):
        cfg.write_text(json.dumps(keys), encoding="utf-8")
    write()
    return write


def test_the_live_gate_opens_only_with_mode_switch_and_production(config, monkeypatch):
    from execution.config import live_gate
    from ops.trading_safety import live_trading_enabled
    live = {"mode": "LIVE", "live_trading_enabled": True}
    for keys, why in [({}, "execution.mode is PAPER"),
                      ({"execution": {"mode": "LIVE"}}, "live_trading_enabled is false"),
                      ({"execution": {"mode": "LIVE", "live_trading_enabled": "true"}}, "live_trading_enabled is false"),
                      ({"execution": {"mode": "LIVE", "live_trading_enabled": 1}}, "live_trading_enabled is false"),
                      ({"execution": {"mode": "PAPER", "live_trading_enabled": True}, "environment": "production"},
                       "execution.mode is PAPER"),
                      ({"execution": live}, "environment is development"),
                      ({"execution": live, "environment": "staging"}, "environment is staging"),
                      ({"execution": {"mode": "Live!", "live_trading_enabled": True}, "environment": "production"},
                       "execution.mode is PAPER")]:
        config(**keys)
        ok, reason = live_gate()
        assert not ok and why in reason, (keys, reason)
    config(execution=live, environment="production")
    assert live_gate()[0] is True and live_trading_enabled()[0] is True
    # ATIP_ENV outranks config.json, in both directions
    monkeypatch.setenv("ATIP_ENV", "development")
    assert live_gate()[0] is False and live_trading_enabled()[0] is False
    config(execution=live)
    monkeypatch.setenv("ATIP_ENV", "production")
    assert live_gate()[0] is True


@pytest.fixture
def order_path(temp_db, config, monkeypatch):
    """_place_order with its broker, quote and funds lookups stubbed: what reaches the broker is recorded."""
    from orders import broker
    sent = []

    class Stub:
        accepts_symbol = True

        def place_order(self, **kw):
            sent.append(kw)
            return {"status": "success", "data": {"orderId": "OID1"}}
    monkeypatch.setattr(broker, "get_execution_client", lambda quote_source=None: (Stub(), "LIVE"))
    monkeypatch.setattr(broker, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    monkeypatch.setattr(broker, "_estimate_order_value", lambda *a: 10000.0)
    monkeypatch.setattr(broker, "check_funds", lambda v: {"ok": True, "available": 1e9, "message": "ok"})
    monkeypatch.setattr(broker, "available_balance", lambda: 1e9)
    monkeypatch.setattr(broker, "get_dhan_client", lambda: (None, None))
    return broker, sent


def _statuses():
    from db.schema import get_connection
    conn = get_connection()
    try:
        return [r[0] for r in conn.execute("SELECT status FROM order_log ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("keys", [
    {"broker_env": "LIVE"},
    {"broker_env": "LIVE", "execution": {"mode": "LIVE", "live_trading_enabled": True}},     # not production
    {"broker_env": "LIVE", "environment": "production", "execution": {"live_trading_enabled": "yes"}},
])
def test_a_confirmed_real_order_is_refused_without_the_master_switch(order_path, config, keys):
    broker, sent = order_path
    config(**keys)
    res = broker.place_buy_order("ACME", 10, confirm=True)
    assert res["status"] == "BLOCKED_LIVE_DISABLED", res
    assert sent == [], "nothing may reach the broker"
    assert _statuses() == ["BLOCKED_LIVE_DISABLED"], "the refusal is on the record"


def test_live_without_confirm_is_only_a_dry_run(order_path, config):
    broker, sent = order_path
    config(broker_env="LIVE", environment="production", execution={"live_trading_enabled": True})
    res = broker.place_buy_order("ACME", 10)
    assert res["status"] == "DRY_RUN_OK" and sent == []


def test_every_switch_on_is_the_only_way_through(order_path, config):
    """The positive control, so the refusals above are known to be the gate and not a broken stub."""
    broker, sent = order_path
    config(broker_env="LIVE", environment="production", execution={"live_trading_enabled": True})
    res = broker.place_buy_order("ACME", 10, confirm=True)
    assert res["status"] == "PLACED" and len(sent) == 1
