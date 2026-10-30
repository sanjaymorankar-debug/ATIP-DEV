"""
The owner's Zerodha switch (30 Oct 2026: "disable the ZERODHA related alerts till I confirm to enable
it"). portfolio/zerodha.enabled() is off until "zerodha_enabled": true is in atip_data/config.json;
while it is off the holdings sync, the pre-market token check and the broker-health line leave Zerodha
alone -- so a failed Dhan sync is no longer reported as "last SKIPPED: Zerodha not connected".
"""
import json

import pytest

from portfolio import zerodha


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(zerodha, "CONFIG_PATH", path)

    def write(**kv):
        path.write_text(json.dumps(kv))
    return write


def test_off_until_the_owner_turns_it_on(cfg):
    assert zerodha.enabled() is False                                   # no config.json at all
    cfg(kite_api_key="x")
    assert zerodha.enabled() is False
    cfg(zerodha_enabled="yes")
    assert zerodha.enabled() is False, "only a JSON true turns it on"
    cfg(zerodha_enabled=True)
    assert zerodha.enabled() is True


def test_a_failed_dhan_sync_falls_back_to_zerodha_only_when_on(cfg, monkeypatch):
    from data import dhan
    from pipeline import scheduler as S
    ran = []
    monkeypatch.setattr(S, "run_job", lambda name, fn, *a: ran.append(name) or {"status": "FAILED"})
    monkeypatch.setattr(dhan, "sync_dhan_portfolio", lambda td: {"status": "FAILED"})
    cfg()
    S._run_portfolio_sync("2026-10-30")
    assert ran == ["dhan_portfolio"]
    cfg(zerodha_enabled=True)
    ran.clear()
    S._run_portfolio_sync("2026-10-30")
    assert ran == ["dhan_portfolio", "zerodha_portfolio"]


def test_the_premarket_token_check_waits_for_the_switch():
    import inspect
    from pipeline import scheduler as S
    src = inspect.getsource(S)
    i = src.index('run_job("zerodha_token_check"')
    assert "if enabled():" in src[i - 200:i], "the Kite token check runs only while Zerodha is on"


def test_broker_health_says_zerodha_is_off(cfg, monkeypatch):
    from execution import broker_health
    monkeypatch.setattr(zerodha, "token_status", lambda: pytest.fail("not asked while Zerodha is off"))
    cfg()
    c = broker_health._zerodha()
    assert (c["status"], c["detail"]) == ("SKIPPED", "Zerodha is off (zerodha_enabled in atip_data/config.json)")
    cfg(zerodha_enabled=True)
    monkeypatch.setattr(zerodha, "token_status", lambda: {"state": "MISSING", "detail": ""})
    assert broker_health._zerodha()["detail"] == "Zerodha not connected"
