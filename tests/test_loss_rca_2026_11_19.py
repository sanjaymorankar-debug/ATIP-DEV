"""Loss RCA of 19 Nov 2026 (owner prompt 19 Nov 2026 06:48 PM IST): regression tests for the code-review fixes.

LRCA-01  check_triggers: one rule that raises no longer aborts the pass (every stop behind it went untested, every
         30 s); exit legs are tested before entries; a database lock is still reported after the others ran.
LRCA-02  the order monitor tests exits before auto-pick (auto-pick's broker / quote calls held every stop).
LRCA-03  the order monitor beats `order_monitor` and alerts once after 4 failing passes (it only logged).
LRCA-04  PostgreSQL: lock waits end after 60 s like SQLite's busy timeout, worded "database is locked".
LRCA-05  a NaN / missing LTP is not a price (pandas NaN is truthy and fails every <= test silently).
LRCA-10  PgConnection.in_transaction (the pre-open gate committed its caller's open transaction on PostgreSQL).
LRCA-11  job health: a run SKIPPED because another runner holds the lock did no work.
"""

import math
import os
import sqlite3
import threading

import pytest

from tests.test_w39_qa_suite import paper, _rule, _paper_orders, _book  # noqa: F401  (paper: a throwaway database)
from tests.test_rca13_monitor_lock import _Stop, _market, _run_loop, srv  # noqa: F401  (srv: fixture)


def _held(feed, symbol="ACME", qty=10, price=100.0):
    """A filled BUY of `qty` `symbol` at `price` through the real rules -> broker -> paper path."""
    from orders import rules as OR
    e = _rule(symbol=symbol, quantity_value=qty, trigger_value=price, reference_price=price)
    feed.prices[symbol] = price
    OR.check_triggers()
    assert OR.confirm_rule(e["id"])["status"] == "PLACED"
    return e


def _stop(symbol, trigger, parent):
    return _rule(symbol=symbol, side="SELL", trigger_value=trigger, trigger_direction="BELOW", role="STOP",
                 parent_rule_id=parent, require_confirmation=False)


# ── LRCA-01 ─────────────────────────────────────────────────────────────────────────────────────

def test_a_rule_that_raises_does_not_stop_the_other_stops(paper, monkeypatch):
    """Two held stocks fall through their stops in the same pass. ACME's stop raises inside execute_rule (as a ledger
    or funds-check error would); BETA's stop must still sell. Before the fix the exception left check_triggers and
    BETA's stop was never tested -- that pass and every pass after it."""
    from orders import rules as OR
    a, b = _held(paper, "ACME"), _held(paper, "BETA")
    sa, sb = _stop("ACME", 95, a["id"]), _stop("BETA", 95, b["id"])
    real = OR.execute_rule

    def flaky(rule_id, price):
        if rule_id == sa["id"]:
            raise ValueError("ledger row is malformed")
        return real(rule_id, price)

    monkeypatch.setattr(OR, "execute_rule", flaky)
    paper.prices.update(ACME=94.0, BETA=94.0)
    ev = OR.check_triggers()
    kinds = {(x["type"], x.get("rule_id")) for x in ev}
    assert ("RULE_ERROR", sa["id"]) in kinds
    assert ("PLACED", sb["id"]) in kinds and OR.get_rule(sb["id"])["status"] == "EXECUTED"
    assert _book("BETA")[1] == 0                                            # BETA sold
    assert OR.get_rule(sa["id"])["status"] == "ACTIVE"                      # ACME's stop is tried again next pass


def test_exit_legs_are_tested_before_entries(paper, monkeypatch):
    """list_rules is newest first: a new entry used to be tested ahead of every older stop."""
    from orders import rules as OR
    a = _held(paper, "ACME")
    _stop("ACME", 95, a["id"])
    _rule(symbol="BETA", trigger_value=100.0, reference_price=100.0)          # a newer entry
    seen = []
    monkeypatch.setattr(OR, "_check_one", lambda conn, rule, *a, **k: seen.append(rule.get("role") or "ENTRY"))
    paper.prices.update(ACME=99.0, BETA=99.0)
    OR.check_triggers()
    assert seen and seen[0] == "STOP" and seen[-1] == "ENTRY"
    assert seen.index("ENTRY") > max(i for i, r in enumerate(seen) if r != "ENTRY")


def test_a_lock_in_one_rule_is_raised_after_the_other_rules_ran(paper, monkeypatch):
    """The monitor's RCA-13 fast retry still sees a lock -- but only once the other stops of the pass were tested."""
    from orders import rules as OR
    a, b = _held(paper, "ACME"), _held(paper, "BETA")
    sa, sb = _stop("ACME", 95, a["id"]), _stop("BETA", 95, b["id"])
    real = OR.execute_rule

    def locked(rule_id, price):
        if rule_id == sa["id"]:
            raise sqlite3.OperationalError("database is locked")
        return real(rule_id, price)

    monkeypatch.setattr(OR, "execute_rule", locked)
    paper.prices.update(ACME=94.0, BETA=94.0)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        OR.check_triggers()
    assert OR.get_rule(sb["id"])["status"] == "EXECUTED"


# ── LRCA-05 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("v,ok", [(100.0, True), ("101.5", True), (0, False), (-1, False), (None, False),
                                  (float("nan"), False), (float("inf"), False), ("x", False)])
def test_usable_price(v, ok):
    from orders.rules import _usable_price
    assert _usable_price(v) is ok


def test_a_nan_ltp_neither_fires_nor_hides(paper):
    """A NaN LTP for ACME: its stop is not fired on a non-price, and BETA's stop beside it still fires."""
    from orders import rules as OR
    a, b = _held(paper, "ACME"), _held(paper, "BETA")
    sa, sb = _stop("ACME", 95, a["id"]), _stop("BETA", 95, b["id"])
    paper.prices.update(ACME=float("nan"), BETA=94.0)
    ev = OR.check_triggers()
    assert OR.get_rule(sa["id"])["status"] == "ACTIVE"
    assert OR.get_rule(sb["id"])["status"] == "EXECUTED"
    assert not [x for x in ev if x["type"] == "RULE_ERROR"]


# ── LRCA-02 / LRCA-03: the monitor ──────────────────────────────────────────────────────────────

def test_the_monitor_checks_exits_before_auto_pick(srv, monkeypatch):
    import orders.autopick as AP
    order = []
    monkeypatch.setattr(AP, "run", lambda: order.append("autopick") or [])
    monkeypatch.setattr(srv.oe, "check_triggers", lambda: order.append("exits") or [])
    srv._order_monitor_pass()
    assert order[:2] == ["exits", "autopick"]


def test_the_monitor_alerts_once_after_four_failing_passes_and_once_on_recovery(srv, monkeypatch, temp_db):
    import alerts.telegram as T
    import orders.autopick as AP
    import ops.jobs as J
    sent, beats = [], []
    monkeypatch.setattr(T, "notify", lambda text, **k: sent.append((k.get("severity"), k.get("key"), text)))
    monkeypatch.setattr(J, "beat", lambda component="scheduler", detail=None, every=60: beats.append(component))
    monkeypatch.setattr(AP, "run", lambda: [])
    _market(monkeypatch)
    srv._monitor_state.update(errors=0, failed_passes=0, alerted=False)

    def broken():
        raise RuntimeError("relation order_rules: column x does not exist")

    monkeypatch.setattr(srv.oe, "check_triggers", broken)
    _run_loop(srv, monkeypatch, sleeps=6)
    assert [s[:2] for s in sent] == [("error", "order_monitor_failing")]   # one alert, at the 4th pass
    assert "order_monitor_failing" in beats and "order_monitor" not in beats
    monkeypatch.setattr(srv.oe, "check_triggers", lambda: [])
    _run_loop(srv, monkeypatch, sleeps=1)
    assert [s[:2] for s in sent][-1] == ("info", "order_monitor_recovered")
    assert "order_monitor" in beats and srv._monitor_state["failed_passes"] == 0


def test_a_rule_error_inside_a_pass_counts_as_a_failing_pass(srv, monkeypatch):
    import orders.autopick as AP
    monkeypatch.setattr(AP, "run", lambda: [])
    monkeypatch.setattr(srv.oe, "check_triggers", lambda: [{"type": "RULE_ERROR", "rule_id": "R1"}])
    srv._order_monitor_pass()
    assert srv._monitor_state["errors"] >= 1


# ── LRCA-04 / LRCA-10: PostgreSQL adapter ───────────────────────────────────────────────────────

def test_a_postgres_lock_timeout_reads_as_sqlites_database_is_locked():
    psycopg = pytest.importorskip("psycopg")
    from db.postgres import as_sqlite_error
    e = as_sqlite_error(psycopg.errors.LockNotAvailable("canceling statement due to lock timeout"))
    assert isinstance(e, sqlite3.OperationalError) and str(e).startswith("database is locked")
    assert e.sqlstate == "55P03"
    other = as_sqlite_error(psycopg.errors.UndefinedTable("relation x does not exist"))
    assert "database is locked" not in str(other)


def test_pg_connection_reports_an_open_transaction_like_sqlite3():
    from db.postgres import PgConnection
    c = object.__new__(PgConnection)
    c._dirty = False
    assert c.in_transaction is False
    c._dirty = True
    assert c.in_transaction is True


@pytest.mark.skipif(not os.environ.get("ATIP_TEST_POSTGRES_URL"), reason="needs a throwaway PostgreSQL")
def test_a_live_pg_connection_has_the_sqlite_lock_timeout():
    from db.postgres import LOCK_TIMEOUT_S, PgConnection
    c = PgConnection(os.environ["ATIP_TEST_POSTGRES_URL"])
    try:
        v = c._conn.execute("SHOW lock_timeout").fetchone()[0]
        assert v in (f"{int(LOCK_TIMEOUT_S)}s", "1min", f"{int(LOCK_TIMEOUT_S * 1000)}ms")
    finally:
        c.close()


@pytest.mark.skipif(not os.environ.get("ATIP_TEST_POSTGRES_URL"), reason="needs a throwaway PostgreSQL")
def test_a_live_pg_row_lock_wait_ends_as_database_is_locked(monkeypatch):
    """Session A holds a row lock; session B's UPDATE of that row used to wait for ever on PostgreSQL."""
    import db.postgres as PG
    monkeypatch.setattr(PG, "LOCK_TIMEOUT_S", 1.0)
    url = os.environ["ATIP_TEST_POSTGRES_URL"]
    a, b = PG.PgConnection(url), PG.PgConnection(url)
    try:
        a._conn.execute("CREATE TABLE IF NOT EXISTS lrca_lock_t (id int primary key, v int)")
        a._conn.execute("INSERT INTO lrca_lock_t VALUES (1, 0) ON CONFLICT DO NOTHING")
        a._conn.commit()
        a._conn.execute("UPDATE lrca_lock_t SET v = v + 1 WHERE id = 1")          # A holds the row
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            b.execute("UPDATE lrca_lock_t SET v = v + 1 WHERE id = 1")
    finally:
        a._conn.rollback()
        a._conn.execute("DROP TABLE IF EXISTS lrca_lock_t")
        a._conn.commit()
        a.close(); b.close()


# ── LRCA-11: job health ─────────────────────────────────────────────────────────────────────────

def test_a_locked_skip_is_not_a_working_run():
    from pipeline.health import _ok
    locked = {"status": "SKIPPED", "error_msg": "locked: another runner holds this job", "rows_processed": None}
    window = {"status": "SKIPPED", "error_msg": "stop window 21:00-06:00", "rows_processed": None}
    good = {"status": "SUCCESS", "error_msg": None, "rows_processed": 10}
    assert _ok(locked, need_rows=False) is False
    assert _ok(window, need_rows=False) is True                     # a planned skip is still fine
    assert _ok(good, need_rows=True) is True


# ── strategy v2 switch (LRCA-06/07/08/09): OFF unless config.json turns it on ─────────────────────

def _sv2(monkeypatch, tmp_path, block):
    import json as _json
    import orders.strategy_v2 as SV2
    p = tmp_path / "sv2" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({} if block is None else {"strategy_v2": block}), encoding="utf-8")
    monkeypatch.setattr(SV2, "CONFIG_PATH", p)
    return SV2


def test_strategy_v2_is_off_without_a_block_and_with_enabled_false(monkeypatch, tmp_path):
    SV2 = _sv2(monkeypatch, tmp_path, None)
    assert SV2.enabled() is False and not SV2.on("freshness_gate") and SV2.value("nifty_gap_min_pct") is None
    SV2 = _sv2(monkeypatch, tmp_path, {"enabled": "true", "freshness_gate": True})     # only JSON true switches it on
    assert SV2.enabled() is False and not SV2.on("freshness_gate")
    SV2 = _sv2(monkeypatch, tmp_path, {"enabled": True, "retest_guard": False})
    assert SV2.on("freshness_gate") and not SV2.on("retest_guard") and SV2.value("nifty_gap_min_pct") is None


def test_sessions_back_and_stale_count_trading_sessions():
    from datetime import date
    import orders.strategy_v2 as SV2
    thu = date(2026, 11, 19)
    assert SV2.sessions_back(1, thu) == date(2026, 11, 18)
    assert SV2.sessions_back(1, date(2026, 11, 16)) == date(2026, 11, 13)          # Monday -> Friday
    assert SV2.stale("2026-11-18", 1, thu) is None
    assert SV2.stale("2026-11-17", 1, thu) is not None
    assert SV2.stale("2026-11-13", 1, date(2026, 11, 16)) is None                   # over the weekend is fresh


def _breadth(conn, day, b50):
    from scores.breadth import ensure_table
    ensure_table(conn)
    conn.execute("INSERT OR REPLACE INTO market_breadth (date, breadth20, breadth50, n, above20, above50, computed_at) "
                 "VALUES (?, ?, ?, 100, 0, 0, ?)", (day, b50, b50, day + "T21:20:00"))
    conn.commit()


def test_a_stale_breadth_closes_the_gate_only_with_v2_on(temp_db, monkeypatch, tmp_path):
    from db.schema import get_connection, init_db
    from scores.breadth import gate_state
    init_db()
    c = get_connection()
    try:
        _breadth(c, "2026-11-13", 70.0)                      # the 21:20 job stopped after Friday 13 Nov
        g50 = {"metric": "breadth50", "min": 50}
        _sv2(monkeypatch, tmp_path, None)
        assert gate_state(c, g50, "2026-11-19")["open"] is True          # today: any age opens it
        _sv2(monkeypatch, tmp_path, {"enabled": True})
        st = gate_state(c, g50, "2026-11-19")
        assert st["open"] is False and "stale" in st["reason"]
        _breadth(c, "2026-11-18", 70.0)
        assert gate_state(c, g50, "2026-11-19")["open"] is True          # yesterday's breadth: fresh
    finally:
        c.close()


def test_auto_pick_takes_no_stale_signal_with_v2_on(temp_db, monkeypatch, tmp_path):
    from datetime import date
    from db.schema import get_connection, init_db
    from orders import autopick as AP
    init_db()
    c = get_connection()
    try:
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES ('ACME', '2026-11-14', 80, 'BUY')")
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume) "
                  "VALUES ('ACME', '2026-11-14', 100, 101, 99, 100, 1000)")
        c.commit()
        _sv2(monkeypatch, tmp_path, None)
        assert [r["symbol"] for r in AP.candidates(c, today=date(2026, 11, 18))["rows"]][:1] == ["ACME"]
        _sv2(monkeypatch, tmp_path, {"enabled": True})
        out = AP.candidates(c, today=date(2026, 11, 18))
        assert out["rows"] == [] and "stale" in out["reason"]
    finally:
        c.close()


def test_nifty_gap_filter_blocks_only_a_gap_down_and_only_with_v2_on(temp_db, monkeypatch, tmp_path):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    try:
        c.execute("INSERT INTO index_levels (date, time, nifty50, nifty50_chg) VALUES ('2026-11-19', '09:15:30', 25800, -0.8)")
        c.execute("INSERT INTO index_levels (date, time, nifty50, nifty50_chg) VALUES ('2026-11-20', '09:15:30', 26100, 0.2)")
        c.commit()
        SV2 = _sv2(monkeypatch, tmp_path, None)
        assert SV2.nifty_gap_block(c, "2026-11-19") is None
        SV2 = _sv2(monkeypatch, tmp_path, {"enabled": True, "nifty_gap_min_pct": -0.5})
        assert "Nifty opened -0.80%" in SV2.nifty_gap_block(c, "2026-11-19")
        assert SV2.nifty_gap_block(c, "2026-11-20") is None
        assert SV2.nifty_gap_block(c, "2026-11-21") is None                 # no print yet: no block
    finally:
        c.close()


def test_retest_guard_refuses_a_resting_buy_whose_ltp_is_already_under_its_stop(paper, monkeypatch, tmp_path):
    """Buy 669.03 -> 669.00, stop x0.95 = 635.55. An LTP of 630 means the breakout already failed: v2 refuses the dummy
    (off: it is created, and its BUY-BELOW would fill at once). An LTP of 697 is created either way."""
    from tests.test_kite_formula_fix_2026_11_16 import _conn, _dummy, _no_dhan  # noqa: F401
    import data.dhan
    monkeypatch.setattr(data.dhan, "fetch_live_quotes", lambda *a, **k: None)
    monkeypatch.setattr("orders.algo_gtt.default_broker", lambda: None)
    c = _conn()
    try:
        _sv2(monkeypatch, tmp_path, None)
        assert _dummy(c, sym="ACME", last_px=630.0)["buy_status"] == "success"
        _sv2(monkeypatch, tmp_path, {"enabled": True})
        out = _dummy(c, sym="BETA", last_px=630.0)
        assert out["buy_status"] == "blocked" and "under the planned stop" in out["message"]
        assert _dummy(c, sym="GAMMA", last_px=697.0)["buy_status"] == "success"
    finally:
        c.close()


def test_retest_guard_holds_a_resting_retest_entry_while_its_gate_is_closed(temp_db, monkeypatch, tmp_path):
    from db.schema import get_connection, init_db
    from orders import arms, sessions
    init_db()
    c = get_connection()
    try:
        sessions.ensure_tables(c)
        monkeypatch.setattr(sessions, "session_of_rule", lambda conn, rule: "S2")
        monkeypatch.setattr(sessions, "get_session", lambda conn, sid: {"status": "RUNNING", "mode": "AUTO"})
        monkeypatch.setattr(sessions, "usage", lambda conn, sid: {"trades_today": 0, "realised_today": 0.0, "open_value": 0.0})
        monkeypatch.setattr(arms, "get", lambda conn, sid: {"regime_gate": {"metric": "breadth50", "min": 50}})
        monkeypatch.setattr(arms, "gate", lambda conn, arm, on=None: {"open": False, "reason": "regime gate closed: breadth50 49.9"})
        rule = {"role": "ENTRY", "notes": "Algo retest S2 occurrence #5: breakout ...", "product_type": "CNC"}
        _sv2(monkeypatch, tmp_path, None)
        assert sessions.gate(c, rule) is None
        _sv2(monkeypatch, tmp_path, {"enabled": True})
        assert "retest held" in sessions.gate(c, rule)
        assert sessions.gate(c, {**rule, "role": "STOP"}) is None                  # exits are never held
    finally:
        c.close()


def test_unknown_market_health_gives_no_buy_only_with_v2_on(monkeypatch, tmp_path):
    from scores.engine import determine_signal
    strong = {"atip_score": 80, "vpi": 80, "zpi": 70, "acs": 70, "cri": 30, "mri": 60}
    _sv2(monkeypatch, tmp_path, None)
    assert determine_signal(strong, {"mh_score": None, "regime": None}) == "BUY"         # as before: unknown = 50
    _sv2(monkeypatch, tmp_path, {"enabled": True})
    assert determine_signal(strong, {"mh_score": None, "regime": None}) == "HOLD"
    assert determine_signal(strong, {"mh_score": 60, "regime": "BULL"}) == "BUY"          # a known MH is unchanged


def test_an_index_flush_without_a_previous_close_stores_null_not_zero(temp_db, monkeypatch):
    """LRCA-13: a reconnect left no previous close; the flush wrote 0.00 % (a flat day) and kept NSE's close out."""
    from db.schema import get_connection, init_db
    from data import dhan_ws
    init_db()
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a, **k: True)
    feed = object.__new__(dhan_ws.IndexFeedManager)
    monkeypatch.setattr(dhan_ws.IndexFeedManager, "snapshot", lambda self: {"nifty50": {"ltp": 25800.0, "chg_pct": None}})
    feed._lock, feed._last_tick_at = threading.Lock(), {}
    feed._flush_to_db()
    c = get_connection()
    try:
        r = c.execute("SELECT nifty50, nifty50_chg, overall_sentiment FROM index_levels").fetchone()
        assert r["nifty50"] == 25800.0 and r["nifty50_chg"] is None and r["overall_sentiment"] is None
    finally:
        c.close()


# ── LRCA-15: POST /api/refresh in market hours ──────────────────────────────────────────────────

def test_refresh_is_refused_during_the_session_and_runs_after_it(monkeypatch, tmp_path, temp_db):
    pytest.importorskip("fastapi")
    from datetime import datetime as real_dt
    from tests.test_risk_controls import api as _api  # noqa: F401
    from fastapi.testclient import TestClient
    import datetime as dtmod
    import pipeline.scheduler as SC
    from db.schema import init_db
    from dashboard import security, server
    from orders import rules
    init_db()
    rules.init_orders_table()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    ran = []
    monkeypatch.setattr(SC, "run_postmarket", lambda *a, **k: ran.append(1))
    client, h = TestClient(server.app), {security.TOKEN_HEADER: security.token()}

    class At(real_dt):
        T = (11, 0)

        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 11, 19, *cls.T)

    monkeypatch.setattr(dtmod, "datetime", At)
    r = client.post("/api/refresh", headers=h)
    assert r.status_code == 409 and r.json()["status"] == "refused"
    assert client.post("/api/refresh?force=true", headers=h).status_code == 200
    At.T = (17, 30)
    assert client.post("/api/refresh", headers=h).status_code == 200
