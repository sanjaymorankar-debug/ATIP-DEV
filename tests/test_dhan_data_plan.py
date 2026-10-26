"""
Dhan's Data API plan, named where it matters (2026-10-26).

From 2026-09-18 every Dhan quote, WebSocket feed, intraday bar and history call failed: Dhan's Data API
plan had lapsed (profile: dataPlan "Deactive"; quotes: 806 "Data APIs not Subscribed"; history: DH-902).
The SDK reduced that to an empty failure, so broker health said "empty quote response", the scheduled
exit manager returned NO_QUOTES into pipeline_log without an alert, and the phone showed nothing.
"""

import json
from datetime import datetime

import pandas as pd
import pytest


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def creds(monkeypatch):
    import data.dhan as D
    monkeypatch.setattr(D, "load_dhan_config", lambda: {"dhan_client_id": "100", "dhan_access_token": "t"})
    return D


def test_data_plan_status_reads_the_profile(creds, monkeypatch):
    import requests
    seen = {}
    monkeypatch.setattr(requests, "get", lambda url, timeout=None, headers=None: seen.update(url=url, h=headers) or
                        _Resp(200, {"dataPlan": "Deactive", "dataValidity": "NA", "tokenValidity": "27/10/2026 18:47"}))
    s = creds.data_plan_status()
    assert s == {"ok": True, "data_plan": "Deactive", "active": False, "data_validity": "NA",
                 "token_validity": "27/10/2026 18:47"}
    assert seen["url"].endswith("/v2/profile") and seen["h"]["client-id"] == "100"


def test_an_active_plan_and_the_failure_paths(creds, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, {"dataPlan": "Active"}))
    assert creds.data_plan_status()["active"] is True
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(401, {}))
    assert creds.data_plan_status() == {"ok": False, "error": "HTTP 401"}
    monkeypatch.setattr(creds, "load_dhan_config", lambda: {"dhan_client_id": "", "dhan_access_token": ""})
    assert creds.data_plan_status()["ok"] is False


def _broker(monkeypatch, plan):
    import data.dhan as D

    class _Dhan:
        def get_fund_limits(self):
            return {"status": "success", "data": {}}
    monkeypatch.setattr(D, "get_dhan_client", lambda: (_Dhan(), None))
    monkeypatch.setattr(D, "fetch_index_quotes", lambda keys, dhan=None: pd.DataFrame())
    monkeypatch.setattr(D, "data_plan_status", lambda timeout=10: plan)
    from execution.broker_health import _dhan_checks
    return {c["check"]: c for c in _dhan_checks(True)}


def test_broker_health_names_an_inactive_data_plan(monkeypatch):
    c = _broker(monkeypatch, {"ok": True, "active": False, "data_plan": "Deactive"})
    assert c["dhan_market_data"]["status"] == "DOWN"
    assert "Data API plan is Deactive" in c["dhan_market_data"]["detail"]
    assert c["dhan_token"]["status"] == "OK"


def test_broker_health_keeps_the_plain_reason_otherwise(monkeypatch):
    for plan in ({"ok": True, "active": True, "data_plan": "Active"}, {"ok": False, "error": "HTTP 500"}):
        assert _broker(monkeypatch, plan)["dhan_market_data"]["detail"] == "empty quote response"


# ── the scheduled exit manager ─────────────────────────────────────────────

def test_unmanaged_open_positions_raise_one_alert_a_day(temp_db, cfg, monkeypatch):
    from strategy import aggressive as A
    from strategy import live as L
    from strategy import positions as P
    import alerts.telegram as T
    import data.dhan as D
    P.open_position("ACME", entry_price=100.0, quantity=10, atr_pct=1.5, cfg=cfg)
    monkeypatch.setattr(A, "config", lambda refresh=False: cfg)
    monkeypatch.setattr("orders.environment.broker_env", lambda: "PAPER")
    monkeypatch.setattr(D, "fetch_live_quotes", lambda symbols, dhan=None: pd.DataFrame())
    monkeypatch.setattr(D, "data_plan_status", lambda timeout=10: {"ok": True, "active": False, "data_plan": "Deactive"})
    sent = []
    monkeypatch.setattr(T, "notify", lambda message, **k: sent.append((message, k)) or {"sent": True, "recorded": True})
    run_job = lambda name, fn, *a, **k: fn(*a, **k)
    r = L.scheduled_pass(run_job, now=datetime(2026, 10, 26, 11, 0))
    assert r["status"] == "NO_QUOTES" and r["positions"] == 1
    assert len(sent) == 1
    msg, kw = sent[0]
    assert "NOT running" in msg and "1 open position(s)" in msg and "Data API plan is Deactive" in msg
    assert kw["key"] == "aggressive_unmanaged:2026-10-26" and kw["severity"] == "error"


def test_a_managed_pass_raises_no_alert(temp_db, cfg, monkeypatch):
    from strategy import aggressive as A
    from strategy import live as L
    from strategy import positions as P
    import alerts.telegram as T
    import data.dhan as D
    P.open_position("ACME", entry_price=100.0, quantity=10, atr_pct=1.5, cfg=cfg)
    monkeypatch.setattr(A, "config", lambda refresh=False: cfg)
    monkeypatch.setattr("orders.environment.broker_env", lambda: "PAPER")
    monkeypatch.setattr(D, "fetch_live_quotes", lambda symbols, dhan=None: pd.DataFrame(
        [{"symbol": s, "ltp": 101.0, "timestamp": pd.Timestamp(datetime.now())} for s in symbols]))
    monkeypatch.setattr(L, "momentum_for", lambda s, conn=None: A.Momentum())
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 1.5)
    sent = []
    monkeypatch.setattr(T, "notify", lambda message, **k: sent.append(message) or {"sent": True, "recorded": True})
    r = L.scheduled_pass(lambda name, fn, *a, **k: fn(*a, **k), now=datetime(2026, 10, 26, 11, 0))
    assert r["status"] == "SUCCESS" and sent == []


# ── the phone ──────────────────────────────────────────────────────────────

def test_the_mobile_app_shows_the_data_feeds(temp_db):
    from db.schema import get_connection, init_db
    from dashboard.w38_page import SERVICE_WORKER, mobile_summary, render_mobile
    init_db()
    conn = get_connection()
    try:
        checks = [{"check": "dhan_market_data", "status": "DOWN", "detail": "Dhan Data API plan is Deactive: ..."},
                  {"check": "dhan_token", "status": "OK", "detail": "fund-limits call accepted"}]
        conn.execute("INSERT INTO broker_health_check (checked_at, overall, in_session, checks_json) VALUES (?,?,?,?)",
                     ("2026-10-26 08:45:04", "DOWN", 0, json.dumps(checks)))
        conn.execute("INSERT INTO pipeline_log (run_date, job_name, start_time, status, kind) VALUES (?,?,?,?,?)",
                     ("2026-10-26", "news_premarket", "2026-10-26 07:00:01", "SUCCESS", "run"))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for h, c in (("A", "rule"), ("B", "rule")):
            conn.execute("INSERT INTO news_articles (fetched_at, headline, classifier) VALUES (?,?,?)", (now, h, c))
        conn.commit()
        s = mobile_summary(conn)
    finally:
        conn.close()
    assert s["data_feed"]["overall"] == "DOWN"
    assert s["data_feed"]["market_data"] == {"status": "DOWN", "detail": "Dhan Data API plan is Deactive: ..."}
    assert "checks_json" not in s["data_feed"]
    assert str(s["news"]["last_fetch"]).startswith("2026-10-26 07:00") and s["news"]["n"] == 2 and not s["news"]["ai"]
    page = render_mobile()
    assert "Data feeds" in page and "keyword only" in page
    assert "atip-shell-v3" in SERVICE_WORKER          # installed PWAs pick up the new shell (v3: Task 2/3 banner + Global & Asia)
