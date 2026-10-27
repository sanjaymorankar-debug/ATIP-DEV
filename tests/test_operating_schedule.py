"""
Task 2 (2026-10-26): the operating schedule -- STOPPED 21:00 -> 06:00 IST, the 06:00 resume sequence, catch-up
after a missed 06:00, no duplicate runs, weekends / NSE holidays, the manual override, and no order placed or
triggered inside the window. Every test runs on a fixed IST clock (operating_schedule.now_ist is replaced).
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")


def _trading_day(after=date(2026, 10, 26)):
    from utils.trading_calendar import is_trading_day
    d = after
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def _weekday_holiday():
    from utils.trading_calendar import NSE_HOLIDAYS
    for h in sorted(NSE_HOLIDAYS):
        d = h if isinstance(h, date) else date.fromisoformat(str(h))
        if d.weekday() < 5:
            return d
    pytest.skip("no weekday NSE holiday in the calendar")


DAY = _trading_day()                                    # a Mon-Fri NSE session
SAT = DAY + timedelta(days=(5 - DAY.weekday()) % 7)     # the Saturday after it


def at(h, m=0, d=None):
    return datetime.combine(d or DAY, datetime.min.time()).replace(hour=h, minute=m, tzinfo=IST)


@pytest.fixture
def ops(temp_db, tmp_path, monkeypatch):
    """Operating schedule ON, a movable IST clock, an initialised temp database, no real config.json."""
    from db.schema import init_db
    from pipeline import operating_schedule as OS
    init_db()
    monkeypatch.delenv("ATIP_OPERATING_SCHEDULE", raising=False)
    monkeypatch.chdir(tmp_path)                          # atip_data/config.json is read relative to the cwd
    clock = {"now": at(10, 0)}
    monkeypatch.setattr(OS, "now_ist", lambda: clock["now"])
    OS._cfg_cache.update(at=0.0, cfg=None)
    OS._ovr_cache.update(at=0.0, row=None)
    OS._DEFERRED_FUNCS.clear()
    # no test reaches the network: a tick that resumes (an override, a startup) runs stub steps
    monkeypatch.setattr(OS, "STEP_FUNCTIONS", _stub_steps([]))

    def set_clock(dt):
        clock["now"] = dt
        OS._ovr_cache.update(at=0.0, row=None)
    OS.set_clock = set_clock
    yield OS
    del OS.set_clock


def _stub_steps(calls, statuses=None):
    statuses = statuses or {}

    def mk(name):
        def fn(ctx):
            calls.append((name, ctx["trading_day"]))
            return {"status": statuses.get(name, "SUCCESS"), "rows": 1}
        return fn
    return {n: mk(n) for n in ("backfill", "collect", "sentiment", "analysis", "report", "live")}


def _rows(sql, *a):
    from db.schema import get_connection
    c = get_connection()
    try:
        return [tuple(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


# ── the window ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hm,stopped", [((20, 59), False), ((21, 0), True), ((23, 59), True), ((0, 0), True),
                                         ((5, 59), True), ((6, 0), False), ((12, 0), False)])
def test_the_stop_window_is_21_00_to_06_00_ist(ops, hm, stopped):
    assert ops.in_stop_window(at(*hm)) is stopped
    ops.set_clock(at(*hm))
    assert ops.is_stopped() is stopped


def test_the_window_is_judged_in_ist_whatever_the_callers_timezone(ops):
    utc = at(21, 30).astimezone(ZoneInfo("UTC"))            # 16:00 UTC
    assert ops.in_stop_window(utc)
    assert ops.window_date(at(2, 0)) == str(DAY - timedelta(days=1)), "02:00 belongs to the previous evening"


def test_hours_come_from_config(ops, tmp_path):
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(json.dumps({"operating_schedule": {"stop_at": "22:30"}}))
    ops._cfg_cache.update(at=0.0, cfg=None)
    assert not ops.in_stop_window(at(22, 0)) and ops.in_stop_window(at(22, 30))


def test_disabled_by_env_means_never_stopped(ops, monkeypatch):
    monkeypatch.setenv("ATIP_OPERATING_SCHEDULE", "off")
    ops.set_clock(at(23, 0))
    assert not ops.is_stopped()


# ── STOPPED at 21:00, transitions recorded ─────────────────────────────────
def test_stop_at_21_00_is_recorded_once(ops):
    ops.set_clock(at(21, 0))
    assert ops.tick()["status"] == "STOPPED"
    ops.tick()
    ops.tick()
    rows = _rows("SELECT status, source FROM scheduler_status WHERE status='STOPPED'")
    assert rows == [("STOPPED", "schedule")], "one transition row, not one per tick"
    st = ops.status()
    assert st["status"] == "STOPPED" and st["read_only"] and st["orders_blocked"]
    assert st["banner"] == "ATIP stopped — resumes 06:00 IST"
    assert st["next_resume"].endswith("06:00")


def test_a_stopped_tick_alerts_once(ops):
    ops.set_clock(at(21, 0))
    ops.tick()
    ops.tick()
    assert len(_rows("SELECT 1 FROM alert_log WHERE title LIKE '%ATIP stopped%'")) == 1


# ── nothing runs, nothing is ordered, nothing is triggered ─────────────────
def test_run_job_refuses_inside_the_window(ops):
    from pipeline.scheduler import run_job
    ran = []
    ops.set_clock(at(22, 15))
    out = run_job("news_premarket", lambda: ran.append(1) or {"status": "SUCCESS", "rows": 1})
    assert ran == [] and out["status"] == "SKIPPED" and "resumes 06:00" in out["reason"]
    assert _rows("SELECT status FROM pipeline_log WHERE job_name='news_premarket'") == [("SKIPPED",)]
    ops.set_clock(at(10, 0))
    run_job("news_premarket", lambda: ran.append(1) or {"status": "SUCCESS", "rows": 1})
    assert ran == [1]


def test_the_kill_switch_is_closed_inside_the_window(ops, tmp_path, monkeypatch):
    from orders import risk
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", tmp_path / "config.json")
    ops.set_clock(at(23, 30))
    h, why = risk.halted()
    assert h and "ATIP stopped" in why and "06:00" in why
    ops.set_clock(at(9, 30))
    assert risk.halted() == (False, "")


@pytest.fixture
def order_path(ops, tmp_path, monkeypatch):
    from orders import broker, risk
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", tmp_path / "config.json")
    sent = []

    class Stub:
        accepts_symbol = True

        def place_order(self, **kw):
            sent.append(kw)
            return {"status": "success", "data": {"orderId": "OID1"}}
    monkeypatch.setattr(broker, "get_execution_client", lambda quote_source=None: (Stub(), "PAPER"))
    monkeypatch.setattr(broker, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    monkeypatch.setattr(broker, "_estimate_order_value", lambda *a: 10000.0)
    monkeypatch.setattr(broker, "check_funds", lambda v: {"ok": True, "available": 1e9, "message": "ok"})
    monkeypatch.setattr(broker, "available_balance", lambda: 1e9)
    monkeypatch.setattr(broker, "get_dhan_client", lambda: (None, None))
    return broker, sent


def test_no_order_can_be_placed_inside_the_window(order_path, ops):
    broker, sent = order_path
    ops.set_clock(at(21, 5))
    res = broker.place_buy_order("ACME", 10, confirm=True)
    assert res["status"] == "BLOCKED_HALTED" and "ATIP stopped" in res["reason"]
    assert sent == [], "nothing reaches the broker in the stop window"


def test_an_order_goes_through_after_the_resume(order_path, ops):
    broker, sent = order_path
    ops.set_clock(at(10, 0))
    res = broker.place_buy_order("ACME", 10, confirm=True)
    assert res["status"] != "BLOCKED_HALTED" and len(sent) == 1


def test_a_manual_stop_blocks_orders_in_market_hours(order_path, ops):
    broker, sent = order_path
    ops.set_clock(at(11, 0))
    ops.set_override("stop", "testing", by="test")
    assert broker.place_buy_order("ACME", 10, confirm=True)["status"] == "BLOCKED_HALTED" and sent == []


def test_order_trigger_monitor_is_gated(ops):
    import inspect
    import dashboard.server as srv
    src = inspect.getsource(srv)
    assert "if is_market_hours() and not is_stopped():" in src, "the order monitor checks the window every pass"


def test_the_live_feeds_do_not_hold_a_socket_while_stopped(ops):
    from data.dhan_ws import feed_window_open
    ten = datetime.combine(DAY, datetime.min.time()).replace(hour=10)
    ops.set_clock(at(10, 0))
    assert feed_window_open(ten)
    ops.set_override("stop", "feed test", by="test")
    assert not feed_window_open(ten)


# ── deferred jobs ──────────────────────────────────────────────────────────
def test_due_jobs_are_skipped_not_run_and_daily_ones_replayed_at_06_00(ops):
    import schedule
    sch = schedule.Scheduler()
    ran = []

    def overnight():
        ran.append("overnight-daily")

    def tick():
        ran.append("tick")
    sch.every().day.at("23:00").do(overnight)
    sch.every(15).minutes.do(tick)
    for j in sch.jobs:
        j.next_run = datetime.now() - timedelta(minutes=1)
    skipped = ops.gate_pending(sch, at(23, 0))
    assert ran == [] and sorted(skipped) == ["overnight", "tick"]
    assert all(j.next_run > datetime.now() for j in sch.jobs), "moved to their next slot, not left due"
    assert _rows("SELECT job FROM deferred_jobs") == [("overnight",)], "only daily jobs are deferred"
    out = ops.replay_deferred(ops.window_date(at(5, 59, DAY + timedelta(days=1))))
    assert ran == ["overnight-daily"] and out == [{"job": "overnight", "result": "REPLAYED"}]
    assert ops.replay_deferred(str(DAY)) == [], "replayed once"


def test_the_scheduler_loop_runs_nothing_while_stopped(ops, monkeypatch):
    import schedule
    from pipeline import scheduler as S
    sch = schedule.Scheduler()
    ran = []
    sch.every(1).minutes.do(lambda: ran.append(1))
    sch.jobs[0].next_run = datetime.now() - timedelta(seconds=5)
    monkeypatch.setattr(S, "schedule", sch)
    monkeypatch.setattr(ops, "tick", lambda *a, **k: {})
    ops.set_clock(at(1, 0))
    S._run_pending_gated()
    assert ran == []
    sch.jobs[0].next_run = datetime.now() - timedelta(seconds=5)
    ops.set_clock(at(7, 0))
    S._run_pending_gated()
    assert ran == [1]


# ── the 06:00 resume ───────────────────────────────────────────────────────
def test_resume_at_06_00_runs_the_six_steps_in_order(ops):
    calls = []
    ops.set_clock(at(21, 0, DAY - timedelta(days=1)))
    ops.tick()
    ops.set_clock(at(6, 0))
    out = ops.ensure_resumed(trigger="schedule", steps=_stub_steps(calls))
    assert [c[0] for c in calls] == ["backfill", "collect", "sentiment", "analysis", "report", "live"]
    assert out["status"] == "DONE"
    logged = [r[0] for r in _rows("SELECT job_name FROM pipeline_log WHERE job_name LIKE 'resume_%' AND status='SUCCESS' "
                                  "ORDER BY id")]
    assert logged == ["resume_1_backfill", "resume_2_collect", "resume_3_sentiment", "resume_4_analysis",
                      "resume_5_report", "resume_6_live"], "each step is logged"
    seq = [r[0] for r in _rows("SELECT status FROM scheduler_status WHERE status IN ('STOPPED','RESUMING','RUNNING') "
                               "ORDER BY id")]
    assert seq == ["STOPPED", "RESUMING", "RUNNING"]
    row = _rows("SELECT status, triggered_by, report_ready_at IS NOT NULL FROM resume_runs WHERE run_date=?", str(DAY))
    assert row == [("DONE", "schedule", 1)]
    assert ops.status()["status"] == "RUNNING"


def test_the_resume_runs_once_a_day_however_often_it_is_asked(ops):
    calls = []
    ops.set_clock(at(6, 0))
    ops.ensure_resumed(steps=_stub_steps(calls))
    ops.set_clock(at(6, 1))
    second = ops.ensure_resumed(steps=_stub_steps(calls))
    ops.set_clock(at(14, 0))
    ops.tick(trigger="startup")
    assert len(calls) == 6 and second == {"status": "DONE", "ran": False, "reason": "already ran today"}


def test_missed_06_00_is_caught_up_on_startup(ops, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "STEP_FUNCTIONS", _stub_steps(calls))
    ops.set_clock(at(10, 40))                               # the Mac was asleep until 10:40
    assert ops.status()["status"] == "RESUMING", "06:00 passed with no resume: the status says so"
    out = ops.tick(trigger="startup")
    assert out["acted"] == "resumed" and len(calls) == 6
    reason = _rows("SELECT reason FROM scheduler_status WHERE status='RESUMING'")[0][0]
    assert reason.startswith("catch-up")
    assert _rows("SELECT triggered_by FROM resume_runs")[0][0] == "startup"


def test_a_held_lock_means_another_runner_is_resuming(ops):
    from db.schema import get_connection
    import os
    c = get_connection()
    c.execute("CREATE TABLE IF NOT EXISTS ops_job_lock (job TEXT PRIMARY KEY, owner TEXT, pid INTEGER, acquired_at "
              "TIMESTAMP, heartbeat_at TIMESTAMP, expires_at TIMESTAMP)")
    c.execute("INSERT OR REPLACE INTO ops_job_lock VALUES (?,?,?,?,?,?)",
              ("operating_resume", "other-host:1:1", os.getppid(), datetime.now(), datetime.now(),
               datetime.now() + timedelta(hours=1)))
    c.commit()
    c.close()
    calls = []
    ops.set_clock(at(6, 0))
    assert ops.ensure_resumed(steps=_stub_steps(calls)) == {"status": "SKIPPED", "ran": False, "reason": "locked"}
    assert calls == []


def test_a_crashed_resume_is_rerun_after_two_hours(ops):
    from db.schema import get_connection
    ops.set_clock(at(6, 0))
    c = get_connection()
    ops.ensure_tables(c)
    c.execute("INSERT INTO resume_runs (run_date, started_at, status, triggered_by) VALUES (?,?,?,?)",
              (str(DAY), at(6, 0).replace(tzinfo=None), "RUNNING", "schedule"))
    c.commit()
    c.close()
    calls = []
    ops.set_clock(at(7, 0))
    assert ops.ensure_resumed(steps=_stub_steps(calls))["reason"] == "in progress"
    ops.set_clock(at(8, 5))
    assert ops.ensure_resumed(steps=_stub_steps(calls))["status"] == "DONE" and len(calls) == 6


def test_a_failed_step_does_not_stop_the_rest(ops):
    calls = []
    ops.set_clock(at(6, 0))
    out = ops.ensure_resumed(steps=_stub_steps(calls, {"collect": "FAILED"}))
    assert len(calls) == 6 and out["status"] == "PARTIAL"
    assert _rows("SELECT 1 FROM alert_log WHERE title LIKE '%resumed with problems%'")


# ── weekends and NSE holidays ──────────────────────────────────────────────
def test_weekend_runs_the_analysis_but_no_live_feeds(ops):
    ops.set_clock(at(6, 0, SAT))
    steps = _stub_steps([])
    steps["live"] = ops.step_live                       # the real step 6
    out = ops.ensure_resumed(steps=steps)
    live = [s for s in out["steps"] if s["name"] == "live"][0]
    assert live["status"] == "SKIPPED" and "weekend" in live["detail"]["reason"]
    assert [s["status"] for s in out["steps"][:5]] == ["SUCCESS"] * 5, "collection and analysis still run"
    st = ops.status()
    assert st["trading_day"] is False and st["live_feeds"] is False and st["status"] == "RUNNING"


def test_nse_holiday_is_treated_like_a_weekend(ops):
    hol = _weekday_holiday()
    ops.set_clock(at(6, 0, hol))
    steps = _stub_steps([])
    steps["live"] = ops.step_live
    out = ops.ensure_resumed(steps=steps)
    assert out["steps"][-1]["status"] == "SKIPPED"
    assert _rows("SELECT trading_day FROM resume_runs WHERE run_date=?", str(hol)) == [(0,)]


# ── manual override ────────────────────────────────────────────────────────
def test_manual_stop_and_auto_are_logged(ops):
    ops.set_clock(at(11, 0))
    r = ops.set_override("stop", "broker maintenance", by="dashboard")
    assert ops.is_stopped() and r["status"] == "STOPPED"
    assert r["expires_at"].startswith(str(DAY + timedelta(days=1))) and r["expires_at"].endswith("06:00:00")
    ops.set_override("auto", "", by="dashboard")
    assert not ops.is_stopped()
    ev = [r[0] for r in _rows("SELECT status FROM scheduler_status WHERE source='manual' ORDER BY id")]
    assert ev[:2] == ["OVERRIDE_STOP", "STOPPED"] and "OVERRIDE_AUTO" in ev


def test_manual_start_inside_the_window_runs_until_21_00(ops):
    ops.set_clock(at(22, 0))
    assert ops.is_stopped()
    r = ops.set_override("start", "evening run", by="dashboard")
    assert not ops.is_stopped() and r["status"] == "RUNNING"
    assert r["expires_at"].startswith(str(DAY + timedelta(days=1))) and r["expires_at"].endswith("21:00:00")
    ops.set_clock(at(5, 0, DAY + timedelta(days=1)))
    assert not ops.is_stopped(), "still started at 05:00"


def test_an_expired_override_is_ignored(ops):
    ops.set_clock(at(11, 0))
    ops.set_override("stop", "", by="test")
    ops.set_clock(at(6, 0, DAY + timedelta(days=1)))
    assert not ops.is_stopped()


def test_a_bad_override_action_is_refused(ops):
    with pytest.raises(ValueError):
        ops.set_override("pause")


# ── step 3: the loud sentiment fallback ────────────────────────────────────
def test_keyword_fallback_in_sentiment_alerts_loudly(ops, monkeypatch):
    from db.schema import get_connection
    import data.news_ai as NA
    monkeypatch.setattr(ops, "_news_ai_on", lambda: True)        # news AI switched on, then fell back
    monkeypatch.setattr(NA, "run_summary_job", lambda hours=None: {"status": "SUCCESS", "rows": 1})
    start = at(6, 0).replace(tzinfo=None)
    c = get_connection()
    for i, cl in enumerate(("rule", "rule", "claude")):
        c.execute("INSERT INTO news_articles (headline, fetched_at, classifier, sentiment) VALUES (?,?,?,?)",
                  (f"h{i}", start + timedelta(minutes=1), cl, 0.1))
    c.commit()
    c.close()
    ops.set_clock(at(6, 0))
    out = ops.step_sentiment({"started": start, "run_date": str(DAY)})
    assert out["status"] == "DEGRADED" and out["rule_classified"] == 2 and out["ai_classified"] == 1
    crit = _rows("SELECT severity, title FROM alert_log WHERE severity='critical'")
    assert crit and "NEWS SENTIMENT FALLBACK" in crit[0][1]


def test_keyword_rules_by_choice_raise_no_alert(ops, monkeypatch):
    """news.ai_enabled false: ATIP's keyword classifier is the engine, not a fall-back."""
    from db.schema import get_connection
    import data.news_ai as NA
    monkeypatch.setattr(NA, "run_summary_job", lambda hours=None: {"status": "SUCCESS", "rows": 1})
    monkeypatch.setattr(ops, "_news_ai_on", lambda: False)
    start = at(6, 0).replace(tzinfo=None)
    c = get_connection()
    c.execute("INSERT INTO news_articles (headline, fetched_at, classifier, sentiment) VALUES (?,?,?,?)",
              ("h", start + timedelta(minutes=1), "rule", 0.2))
    c.commit()
    c.close()
    out = ops.step_sentiment({"started": start, "run_date": str(DAY)})
    assert out["status"] == "SUCCESS" and not out["fallback"] and out["rule_classified"] == 1
    assert not _rows("SELECT 1 FROM alert_log WHERE severity='critical'")


def test_claude_only_sentiment_is_clean(ops, monkeypatch):
    from db.schema import get_connection
    import data.news_ai as NA
    monkeypatch.setattr(NA, "run_summary_job", lambda hours=None: {"status": "SUCCESS", "rows": 1})
    start = at(6, 0).replace(tzinfo=None)
    c = get_connection()
    c.execute("INSERT INTO news_articles (headline, fetched_at, classifier, sentiment) VALUES (?,?,?,?)",
              ("h", start + timedelta(minutes=1), "claude", 0.2))
    c.commit()
    c.close()
    out = ops.step_sentiment({"started": start, "run_date": str(DAY)})
    assert out["status"] == "SUCCESS" and not out["fallback"]


# ── the dashboard in the window ────────────────────────────────────────────
@pytest.fixture
def client(ops, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    return TestClient(srv.app)


def _token():
    from dashboard.security import token
    return token()


def test_dashboard_is_read_only_with_a_banner_while_stopped(ops, client):
    ops.set_clock(at(21, 30))
    st = client.get("/api/status").json()
    assert st["status"] == "STOPPED" and st["banner"] == "ATIP stopped — resumes 06:00 IST" and "last_updated" in st
    for page in ("/", "/command", "/m", "/global-asia"):
        r = client.get(page)
        assert r.status_code == 200 and 'id="atipOpBanner"' in r.text, page
    r = client.post("/api/orders", json={"symbol": "ACME"}, headers={"X-ATIP-Token": _token()})
    assert r.status_code == 423 and r.json()["read_only"]
    assert client.post("/api/sessions/S1/status", json={"status": "PAUSED"},
                       headers={"X-ATIP-Token": _token()}).status_code == 423


def test_override_from_the_dashboard_needs_the_token_and_is_allowed_while_stopped(ops, client):
    ops.set_clock(at(22, 0))
    assert client.post("/api/status/override", json={"action": "start"}).status_code == 401
    r = client.post("/api/status/override", json={"action": "start", "reason": "late check"},
                    headers={"X-ATIP-Token": _token()})
    assert r.status_code == 200 and r.json()["status"] == "RUNNING"
    assert client.get("/api/status").json()["override"]["mode"] == "start"


def test_dashboard_writes_work_in_operating_hours(ops, client):
    ops.set_clock(at(11, 0))
    r = client.post("/api/sessions/S1/status", json={"status": "PAUSED", "reason": "t"},
                    headers={"X-ATIP-Token": _token()})
    assert r.status_code != 423


# ── the pre-market report ──────────────────────────────────────────────────
def test_premarket_report_is_stored_with_its_caveats(ops, tmp_path):
    from pipeline.premarket_report import build_and_store, latest
    from db.schema import get_connection
    r = build_and_store(at(6, 5), label="resume")
    assert r["ok"] and (tmp_path / "atip_data" / "reports" / f"premarket_{DAY}.json").exists()
    rep = r["report"]
    assert rep["for_date"] == str(DAY) and "no scored session yet" in rep["caveats"]
    assert rep["outlook"]["informational_only"] is True
    c = get_connection()
    try:
        assert latest(c)["generated_at"].startswith(str(DAY))
    finally:
        c.close()
