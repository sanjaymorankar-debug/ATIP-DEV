"""W39: alerts.telegram.notify records every alert in alert_log on its OWN connection. A caller that
still holds an uncommitted write on another connection to the same database makes that INSERT wait
the whole busy timeout (60 s in production) behind a lock its own thread will not release until
notify returns -- and then the alert is lost. ops/monitor.py was the first case found; these are the
other production paths where it could happen, each driven for real with a 200 ms busy timeout, so a
regression shows up as an alert missing from alert_log rather than a minute-long stall.

  * orders/sip.run_due: plan N's order (orders.broker: order_log, the paper fill, the risk alert)
    was placed while plan N-1's sip_execution / sip_plan writes were still uncommitted;
  * wealth/integrated.investor_cycle: a step that failed mid-write left its transaction open (the
    cycle isolates a failed step, it does not roll it back) and a later step's alert waited on it;
  * pipeline/scheduler.run_job's failure alert (and FAILED pipeline_log row) after a job that left
    its own connection mid-write: data/dhan.run_live_quote_refresh (no try/finally around its
    INSERT loop: the exception's traceback keeps the connection alive while run_job records the
    failure) and data/dhan.sync_dhan_portfolio (caught its failure without closing the connection).
    A dropped sqlite3 connection is not closed when its last reference goes -- it sits in a
    reference cycle (its statement cache) -- so its write lock lasts until a garbage collection,
    and none runs while a writer sleeps in SQLite's busy handler.
Telegram itself is never called (tests/conftest.py replaces alerts.telegram._deliver_telegram)."""

from datetime import date

import pandas as pd
import pytest


@pytest.fixture
def short_busy(temp_db, monkeypatch):
    """The schema created, then every NEW connection waits at most 200 ms for a writer."""
    import db.schema as S
    S.init_db()
    monkeypatch.setattr(S, "BUSY_TIMEOUT_MS", 200)
    return temp_db


def _rows(sql, args=()):
    from db.schema import get_connection
    c = get_connection()
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _alerts(category):
    return _rows("SELECT category, severity, title, message, dedupe_key FROM alert_log WHERE category=? ORDER BY id",
                 (category,))


# ── orders/sip.py ──────────────────────────────────────────────────────────

def test_sip_run_records_every_plans_refused_order_and_its_risk_alert(short_busy, tmp_path, monkeypatch):
    """Two due plans while the kill switch is on: each goes through the real orders.broker path,
    which logs BLOCKED_HALTED in order_log and raises a risk alert, both on their own connections.
    Before the fix the second plan's order_log INSERT waited out the busy timeout behind the first
    plan's uncommitted sip_execution row and raised 'database is locked' out of run_due -- which
    then rolled the first plan's record back -- and the second alert was never recorded."""
    monkeypatch.chdir(tmp_path)                          # config.json and the halt flag live under atip_data/
    from db.schema import get_connection
    from orders import risk as RK
    from orders import sip as SIP
    RK.HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
    RK.HALT_FLAG.write_text("notify drill", encoding="utf-8")
    c = get_connection()
    try:
        plans = [SIP.create(c, {"symbol": s, "quantity": 3, "frequency": "MONTHLY", "day": 1,
                                "start_date": "2026-01-01"})["plan_id"] for s in ("ACME", "BETA")]
    finally:
        c.close()

    r = SIP.run_due(on=date(2026, 1, 5))                 # the real broker path (place=None)

    assert [(x["symbol"], x["status"], x["detail"]) for x in r["results"]] == \
        [("ACME", "FAILED", "notify drill"), ("BETA", "FAILED", "notify drill")]
    assert [(x["plan_id"], x["status"], x["order_status"], x["attempts"]) for x in _rows(
        "SELECT plan_id, status, order_status, attempts FROM sip_execution ORDER BY symbol")] == \
        [(plans[0], "FAILED", "BLOCKED_HALTED", 1), (plans[1], "FAILED", "BLOCKED_HALTED", 1)]
    assert [(x["symbol"], x["status"]) for x in _rows("SELECT symbol, status FROM order_log ORDER BY id")] == \
        [("ACME", "BLOCKED_HALTED"), ("BETA", "BLOCKED_HALTED")]
    logged = _alerts("risk")
    assert [(a["title"], a["dedupe_key"]) for a in logged] == \
        [("Order Refused — Trading Halted", "blocked_halted:ACME:BUY"),
         ("Order Refused — Trading Halted", "blocked_halted:BETA:BUY")]
    assert "BUY 3 x BETA (PAPER) not sent: notify drill" in logged[1]["message"]


# ── wealth/integrated.py ─────────────────────────────────────────────────

def test_investor_cycle_alerts_are_recorded_after_a_step_failed_mid_write(short_busy, monkeypatch):
    """The allocation step's INSERT fails (a trigger stands in for any failed write) and the cycle
    carries on, as designed. A failed statement inside Python's implicit transaction leaves that
    transaction -- and SQLite's write lock -- open, so before the fix the rebalance and holdings
    alerts that followed waited 200 ms each and were not recorded."""
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda *a, **k: {})     # no NSE download
    from tests._wealth_seed import ANSWERS, OWNER, seed_market
    from db.schema import get_connection
    from wealth import dna
    from wealth import holdings as H
    from wealth import integrated as INT
    conn = get_connection()
    try:
        from scores.signal_log import ensure_tables
        ensure_tables(conn)
        ds = seed_market(conn)
        dna.save(conn, OWNER, ANSWERS)
        H.add_holding(conn, OWNER, {"asset_class": "CASH", "instrument": "BANK_ACCOUNT", "name": "Savings",
                                    "quantity": 600000})
        H.add_holding(conn, OWNER, {"asset_class": "EQUITY", "instrument": "STOCK", "name": "Acme",
                                    "symbol": "ACME", "quantity": 1000, "avg_cost": 90})
        conn.execute("UPDATE ai_scores SET cri=85, signal='SELL' WHERE symbol='ACME' AND date=?", (str(ds[-1]),))
        conn.execute("CREATE TRIGGER allocation_write_fails BEFORE INSERT ON wealth_allocation_run "
                     "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        conn.commit()

        out = INT.investor_cycle(conn, OWNER, alerts=True)
    finally:
        conn.close()

    assert out["status"] == "PARTIAL" and out["failed_steps"] == ["allocation"]
    assert "simulated write failure" in out["steps"]["allocation"]["error"]
    assert out["alerts"] and all(a["recorded"] for a in out["alerts"]), out["alerts"]
    titles = [a["message"].split("\n", 1)[0] for a in _alerts("wealth")]
    assert "<b>ATIP wealth: holdings flagged</b>" in titles
    assert len(titles) == len(out["alerts"])
    # the cycle's own record is still written at the end
    assert [r["status"] for r in _rows("SELECT status FROM wealth_cycle_run")] == ["PARTIAL"]


# ── pipeline/scheduler.py run_job ──────────────────────────────────────────

def test_a_job_that_dies_mid_write_still_gets_its_failed_row_and_alert(short_busy, monkeypatch):
    """A quote the database cannot store (here a field the API returned as an object) raises out of
    run_live_quote_refresh with the first row written and uncommitted. Its connection had no
    try/finally, so it stayed open -- kept alive by the traceback while run_job logged the failure
    and alerted from its except block: both waited out the busy timeout and neither was recorded."""
    import data.dhan as D
    from pipeline.scheduler import run_job
    monkeypatch.setattr(D, "HAS_DHAN", True)
    monkeypatch.setattr(D, "get_dhan_client", lambda: (object(), None))
    monkeypatch.setattr(D, "fetch_live_quotes", lambda symbols, dhan=None: pd.DataFrame([
        {"symbol": "ACME", "ltp": 101.5, "open": 100.0, "high": 102.0, "low": 99.5, "prev_close": 100.0,
         "volume": 1000, "chg_pct": 1.5},
        {"symbol": "BETA", "ltp": {"value": 55.0}, "open": 54.0, "high": 56.0, "low": 53.0, "prev_close": 54.0,
         "volume": 500, "chg_pct": 1.0}]))

    r = run_job("dhan_quotes_intraday", D.run_live_quote_refresh, ["ACME", "BETA"])

    assert r["status"] == "FAILED" and "type 'dict' is not supported" in r["error"]
    runs = _rows("SELECT status, error_msg FROM pipeline_log WHERE job_name='dhan_quotes_intraday' AND kind='run'")
    assert [x["status"] for x in runs] == ["FAILED"] and runs[0]["error_msg"].startswith("ProgrammingError: ")
    logged = _alerts("job")
    assert [(a["severity"], a["dedupe_key"]) for a in logged] == [("error", "job_failed:dhan_quotes_intraday")]
    assert "<b>Job:</b> dhan_quotes_intraday" in logged[0]["message"]
    assert _rows("SELECT symbol FROM live_quotes") == []            # the half-written batch was rolled back


def test_a_portfolio_sync_that_fails_mid_batch_still_records_the_failure_and_alerts(short_busy, monkeypatch):
    """sync_dhan_portfolio catches its own failure -- but used to do it with the holdings batch
    still uncommitted on a connection it never closed: its portfolio_sync FAILED row waited out the
    busy timeout and was lost, and the dropped connection (sqlite3 connections sit in a reference
    cycle, so only the garbage collector frees one) went on blocking run_job's FAILED row and the
    failure alert after the job had returned."""
    import data.dhan as D
    from pipeline.scheduler import run_job

    class Dhan:
        def get_holdings(self):
            return {"status": "success", "data": [
                {"tradingSymbol": "ACME", "totalQty": 10, "avgCostPrice": 90.0, "lastTradedPrice": 100.0},
                {"tradingSymbol": "BETA", "totalQty": 5, "avgCostPrice": "n/a", "lastTradedPrice": 50.0}]}
    monkeypatch.setattr(D, "get_dhan_client", lambda: (Dhan(), None))
    monkeypatch.setattr(D, "fetch_live_quotes", lambda symbols, dhan=None: pd.DataFrame())

    r = run_job("dhan_portfolio", D.sync_dhan_portfolio, date(2026, 1, 5))

    assert r["status"] == "FAILED" and "unsupported operand" in r["error"]
    assert [(x["status"], x["error"][:20]) for x in _rows("SELECT status, error FROM portfolio_sync")] == \
        [("FAILED", "unsupported operand ")]
    assert [x["status"] for x in _rows("SELECT status FROM pipeline_log WHERE job_name='dhan_portfolio' "
                                       "AND kind='run'")] == ["FAILED"]
    assert [a["dedupe_key"] for a in _alerts("job")] == ["job_failed:dhan_portfolio"]
    assert _rows("SELECT symbol FROM portfolio_holdings") == []      # the half-written batch was rolled back


class _Conn:
    """Records close(); execute raises when told to."""
    def __init__(self, fail=False):
        self.fail, self.closed = fail, False

    def execute(self, *a, **k):
        if self.fail and str(a[0]).lstrip().upper().startswith("INSERT"):
            raise RuntimeError("disk I/O error")
        return self

    def commit(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("fail", [False, True])
def test_log_job_and_tick_flush_close_their_connection_on_success_and_failure(monkeypatch, fail):
    from db import schema
    made = []
    monkeypatch.setattr(schema, "get_connection", lambda: made.append(_Conn(fail)) or made[-1])
    schema.log_job("x", "SUCCESS")
    from data import dhan
    monkeypatch.setattr(dhan, "get_connection", lambda: made.append(_Conn(fail)) or made[-1])
    feed = dhan.DhanLiveFeed.__new__(dhan.DhanLiveFeed)
    feed._buffer = [{"symbol": "ACME", "LTP": 1.0}]
    feed._flush_buffer()
    assert len(made) == 2 and all(c.closed for c in made)
    assert feed._buffer == []


def test_index_feed_flush_closes_its_connection(monkeypatch):
    from data import dhan_ws
    made = []
    monkeypatch.setattr(dhan_ws, "get_connection", lambda: made.append(_Conn()) or made[-1])
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda: True)
    m = dhan_ws.IndexFeedManager.__new__(dhan_ws.IndexFeedManager)
    import threading
    m._lock, m._last_tick_at = threading.Lock(), {}
    m.snapshot = lambda: {"nifty50": {"ltp": 25000.0, "chg_pct": 0.4}}
    m._flush_to_db()
    assert len(made) == 1 and made[0].closed
