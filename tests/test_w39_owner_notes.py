"""W39 owner notes: 7-year history (DP-23), Dhan TOTP token renewal (OPS-12), stock chart (DB-20)."""

import json
from datetime import date, timedelta

import pytest


def _db():
    from db.schema import get_connection, init_db
    init_db()
    return get_connection()


def _bars(conn, sym, start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "(?,?,?,?,?,?,?)", (sym, str(d), 100, 101, 99, 100, 1000))
        d += timedelta(days=1)
    conn.commit()


# ── DP-23 long history ─────────────────────────────────────────────────

def test_plan_asks_only_for_the_missing_years_in_shared_windows(temp_db):
    from data import history_backfill as HB
    conn = _db()
    end = date(2026, 10, 7)
    _bars(conn, "OLD", HB.target_start(7, end), end)                  # already 7 years
    _bars(conn, "NEW", end - timedelta(days=500), end)                # ~1.4 years
    plan = HB.plan(conn, ["OLD", "NEW", "NONE"], 7, end)
    assert all("OLD" not in syms for _, _, syms in plan)
    new_windows = [(a, b) for a, b, syms in plan if "NEW" in syms]
    assert min(a for a, _ in new_windows) == HB.target_start(7, end)
    assert max(b for _, b in new_windows) >= end - timedelta(days=501)
    assert len(new_windows) == 6                                       # years 2..7 back
    assert len([1 for _, _, syms in plan if "NONE" in syms]) == 7      # nothing stored: every year
    for a, b, _ in plan:
        assert (b - a).days < HB.WINDOW_DAYS + HB.MIN_STUB_DAYS


def test_backfill_stores_through_the_fetcher_and_remembers_listing_dates(temp_db):
    from data import history_backfill as HB
    conn = _db()
    end = date(2026, 10, 7)
    _bars(conn, "AAA", end - timedelta(days=400), end)
    _bars(conn, "IPO", end - timedelta(days=200), end)                 # listed 200 days ago
    calls = []

    def fetch(symbols, interval_min, start_date, end_date, basis_date):
        calls.append((tuple(symbols), start_date, end_date))
        assert basis_date == date.today()                             # Dhan's basis is today's
        c = _db()
        if "AAA" in symbols:
            _bars(c, "AAA", start_date, end_date)
        c.close()
        return {"status": "SUCCESS", "rows": 1, "refused": 0}

    r = HB.backfill(years=3, symbols=["AAA", "IPO"], end_date=end, include_indices=False, fetch=fetch,
                    index_fetch=lambda **k: {"status": "SUCCESS", "rows": 0})
    assert r["status"] == "SUCCESS" and calls
    cov = {c["symbol"]: c for c in HB.coverage(conn, ["AAA", "IPO"], end, 3)}
    assert cov["AAA"]["covered"] and cov["IPO"]["covered"] and cov["IPO"]["listed_later"]
    again = HB.backfill(years=3, symbols=["AAA", "IPO"], end_date=end, include_indices=False, dry_run=True)
    assert again["calls_planned"] == 0                                # resumable: nothing left to ask


def test_a_refused_window_is_retried_next_time(temp_db):
    from data import history_backfill as HB
    conn = _db()
    end = date(2026, 10, 7)
    _bars(conn, "AAA", end - timedelta(days=100), end)
    r = HB.backfill(years=2, symbols=["AAA"], end_date=end, include_indices=False,
                    fetch=lambda **k: {"status": "FAILED", "rows": 0, "refused": 1},
                    index_fetch=lambda **k: {})
    assert r["status"] == "FAILED"
    assert HB.backfill(years=2, symbols=["AAA"], end_date=end, include_indices=False,
                       dry_run=True)["calls_planned"] > 0


def test_the_purge_keeps_price_history_for_history_years(temp_db, monkeypatch):
    from db import purge
    conn = _db()
    old = date.today() - timedelta(days=1500)                         # ~4 years
    conn.execute("INSERT INTO prices_daily (symbol,date,close) VALUES ('X',?,1)", (str(old),))
    conn.execute("INSERT INTO ai_scores (symbol,date,atip_score) VALUES ('X',?,50)", (str(old),))
    conn.commit()
    res = purge.purge_old_data(dry_run=True, history_days_=int(7 * 365.25))
    assert res["prices_daily"] == 0 and res["ai_scores"] == 1
    assert purge.purge_old_data(dry_run=True, history_days_=1000)["prices_daily"] == 1
    monkeypatch.setattr("data.history_backfill.history_years", lambda cfg=None: 7)
    assert purge.history_days() >= int(7 * 365)


def test_history_years_is_bounded():
    from data.history_backfill import history_years
    assert history_years({}) == 7 and history_years({"history_years": 99}) == 25
    assert history_years({"history_years": "x"}) == 7 and history_years({"history_years": 3}) == 3


# ── OPS-12 Dhan token renewal ──────────────────────────────────────────

def test_token_renewal_is_scheduled_and_runs_at_start_only_when_needed(monkeypatch, tmp_path):
    from pipeline import w39_jobs as J
    monkeypatch.setattr(J, "config", lambda: {"dhan_client_id": "1", "dhan_token_refresh_time": "06:40"})
    seen = []

    class FakeRefresher:
        @staticmethod
        def run(check=False, only_if_invalid=False):
            seen.append(only_if_invalid)
            return 0
    monkeypatch.setattr(J, "_refresher", lambda: FakeRefresher)

    class Every:
        def __init__(self, log):
            self.log = log

        def __getattr__(self, name):
            self.log.append(name)
            return self

        def __call__(self, *a, **k):
            return self

        def at(self, t):
            self.log.append(t)
            return self

        def do(self, *a, **k):
            return self

    class Sched:
        def __init__(self):
            self.log = []

        def every(self, *a):
            return Every(self.log)
    s = Sched()
    lines = J.schedule_jobs(s, lambda *a, **k: None)
    assert "dhan_token_refresh daily 06:40" in lines and "06:40" in s.log
    assert any(x.startswith("history_backfill Sunday") for x in lines)
    J.startup(lambda name, fn, **kw: fn(**kw))
    assert seen == [True]
    assert J.dhan_token_refresh()["status"] == "SUCCESS" and seen == [True, False]


def test_token_renewal_switches_off_and_skips_without_a_client(monkeypatch):
    from pipeline import w39_jobs as J
    monkeypatch.setattr(J, "config", lambda: {"dhan_token_auto_refresh": False, "history_backfill_weekly": False})
    assert J.schedule_jobs(None, None) == []
    monkeypatch.setattr(J, "config", lambda: {})
    monkeypatch.setattr(J, "_vaulted_client_id", lambda: False)
    assert J.dhan_token_refresh()["status"] == "SKIPPED"
    assert J._hhmm("25:00", "06:45") == "06:45" and J._hhmm("6:5", "06:45") == "06:05"


def test_refresher_only_if_invalid_keeps_a_working_token(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("dtr39", Path("tools/dhan_token_refresh.py").resolve())
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.chdir(tmp_path)                 # run() chdirs to ROOT; restored after the test
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dhan_client_id": "1", "dhan_access_token": "tok"}))
    monkeypatch.setattr(m, "CFG", cfg)
    monkeypatch.setattr(m, "LOG", tmp_path / "log.txt")
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "token_valid", lambda cid, tok: True)
    monkeypatch.setattr(m, "renew", lambda cid, tok: pytest.fail("a valid token must not be renewed"))
    assert m.run(only_if_invalid=True) == 0
    monkeypatch.setattr(m, "token_valid", lambda cid, tok: tok == "new")
    monkeypatch.setattr(m, "renew", lambda cid, tok: "new")
    assert m.run(only_if_invalid=True) == 0
    assert json.loads(cfg.read_text())["dhan_access_token"] == "new"


def test_config_template_documents_the_new_keys():
    from pathlib import Path
    t = json.loads((Path(__file__).resolve().parents[1] / "config_template.json").read_text(encoding="utf-8"))
    for k in ("dhan_pin", "dhan_totp_secret", "dhan_token_auto_refresh", "dhan_token_refresh_time", "history_years",
              "history_backfill_weekly", "history_backfill_time"):
        assert k in t, k
    assert t["dhan_pin"] == "" and t["dhan_totp_secret"] == ""              # never a real secret in git


# ── DB-20 stock history ────────────────────────────────────────────────

def test_stock_history_reports_multi_year_returns(temp_db):
    from dashboard.stock_view import stock_history
    conn = _db()
    d0 = date.today() - timedelta(days=3700)
    i, d = 0, d0
    while d < date.today():
        if d.weekday() < 5:
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "('ACME',?,?,?,?,?,1000)", (str(d), 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1))
            i += 1
        d += timedelta(days=1)
    conn.commit()
    h = stock_history(conn, "ACME", 2500)
    s = h["stats"]
    closes = [p["close"] for p in h["prices"]]
    assert len(closes) == 2500
    assert s["ret_5y"] == pytest.approx(round((closes[-1] / closes[-1 - 1250] - 1) * 100, 2))
    assert s["ret_all"] == pytest.approx(round((closes[-1] / closes[0] - 1) * 100, 2))
    assert s["first_date"] == h["prices"][0]["date"] and s["high_all"] >= s["high_52w"]


def test_history_routes(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    _db().close()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("data.dhan.get_tracked_symbols", lambda conn=None: ["AAA"])
    c = TestClient(server.app)
    st = c.get("/api/data/history/status")
    assert st.status_code == 200 and st.json()["history_years"] >= 1
    assert c.post("/api/data/history/backfill", json={}).status_code == 401
    r = c.post("/api/data/history/backfill", json={"years": 2},
               headers={security.TOKEN_HEADER: security.token()})
    assert r.status_code == 200 and r.json()["status"] == "DRY_RUN" and r.json()["calls_planned"] >= 2
