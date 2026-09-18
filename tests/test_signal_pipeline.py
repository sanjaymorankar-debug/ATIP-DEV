"""
Regression tests for the signal pipeline breakage found on 2026-09-18.

From 2026-09-10 every signal was logged with a NULL entry price and could
never be tracked. Causes, each covered below:
  * post-market ran at 16:05, before NSE publishes the Bhavcopy (16:33), so a
    day's closing prices were never there when that day was scored;
  * a day was scored anyway, on the previous day's bars;
  * a missed post-market slot was never caught up;
  * the holiday calendar lacked every festival holiday, so Ganesh Chaturthi
    was scored as a session.
"""

import datetime as dt

import pytest


# ── trading calendar ──────────────────────────────────────────────────────

@pytest.mark.parametrize("d,trading", [
    (dt.date(2026, 9, 14), False),   # Ganesh Chaturthi -- was scored as a session
    (dt.date(2026, 10, 20), False),  # Dussehra
    (dt.date(2026, 11, 10), False),  # Diwali-Balipratipada
    (dt.date(2026, 11, 24), False),  # Guru Nanak Jayanti
    (dt.date(2026, 9, 15), True),
    (dt.date(2026, 9, 18), True),
    (dt.date(2026, 9, 19), False),   # Saturday
])
def test_nse_calendar_knows_festival_holidays(d, trading):
    from utils.trading_calendar import is_trading_day
    assert is_trading_day(d) is trading


def test_postmarket_after_a_holiday_targets_the_last_session():
    from utils.trading_calendar import postmarket_target_date
    # Monday 14 Sep was a holiday; at 20:00 that day the last session is Fri 11 Sep.
    assert postmarket_target_date(dt.datetime(2026, 9, 14, 20, 0)) == dt.date(2026, 9, 11)


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import init_db, get_connection
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


def _score(conn, d, symbols):
    for s in symbols:
        conn.execute("INSERT INTO ai_scores (symbol, date, atip_score, signal) VALUES (?,?,?,?)",
                     (s, str(d), 60.0, "HOLD"))
    conn.commit()


def _bar(conn, d, symbols, close=100.0):
    for s in symbols:
        conn.execute("INSERT OR REPLACE INTO prices_daily (symbol, date, open, high, low, close, "
                     "volume, source) VALUES (?,?,?,?,?,?,?,?)",
                     (s, str(d), close, close, close, close, 1000, "test"))
    conn.commit()


UNIVERSE = [f"S{i}" for i in range(10)]


# ── EOD coverage guard ────────────────────────────────────────────────────

def test_coverage_fails_when_the_days_bars_are_missing(db):
    """Exactly the 09-10..09-18 condition: scores exist for yesterday, no bars today."""
    from pipeline import scheduler as S
    _score(db, "2026-09-17", UNIVERSE)
    _bar(db, "2026-09-17", UNIVERSE)
    ok, n_eod, n_uni = S._eod_coverage(dt.date(2026, 9, 18))
    assert ok is False
    assert (n_eod, n_uni) == (0, 10)


def test_coverage_passes_when_the_days_bars_are_present(db):
    from pipeline import scheduler as S
    _score(db, "2026-09-17", UNIVERSE)
    _bar(db, "2026-09-18", UNIVERSE)
    assert S._eod_coverage(dt.date(2026, 9, 18))[0] is True


def test_coverage_uses_a_share_not_a_single_bar(db):
    """One stray bar must not pass for 'we have today's closes'."""
    from pipeline import scheduler as S
    _score(db, "2026-09-17", UNIVERSE)
    _bar(db, "2026-09-18", UNIVERSE[:2])
    assert S._eod_coverage(dt.date(2026, 9, 18))[0] is False


def test_coverage_allows_a_first_ever_run(db):
    """With nothing scored before, there is no universe to judge -- don't block."""
    from pipeline import scheduler as S
    assert S._eod_coverage(dt.date(2026, 9, 18))[0] is True


def test_postmarket_does_not_score_a_day_without_its_closes(db, monkeypatch):
    """The core regression: no EOD -> no indicators, no scoring, no signal log."""
    from pipeline import scheduler as S
    ran = []
    monkeypatch.setattr(S, "run_job", lambda name, *a, **k: ran.append(name) or {})
    monkeypatch.setattr(S, "_run_dhan_quotes", lambda *a, **k: None)
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 18))
    monkeypatch.setattr(S, "_eod_coverage", lambda td: (False, 0, 502))

    out = S.run_postmarket(force=True)

    assert out["status"] == "SKIPPED_NO_EOD"
    for forbidden in ("technical_indicators", "ai_scoring_engine", "signal_log"):
        assert forbidden not in ran, f"{forbidden} ran on a day with no closing prices"
    assert "signal_outcomes" in ran, "older signals must keep being tracked"
    assert "dashboard_rebuild" in ran


def test_postmarket_scores_normally_when_closes_are_present(db, monkeypatch):
    from pipeline import scheduler as S
    ran = []
    monkeypatch.setattr(S, "run_job", lambda name, *a, **k: ran.append(name) or {})
    monkeypatch.setattr(S, "_run_dhan_quotes", lambda *a, **k: None)
    monkeypatch.setattr(S, "_run_portfolio_sync", lambda *a, **k: None)
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 18))
    monkeypatch.setattr(S, "_eod_coverage", lambda td: (True, 501, 502))

    S.run_postmarket(force=True)

    for required in ("technical_indicators", "ai_scoring_engine", "signal_log"):
        assert required in ran


# ── schedule ──────────────────────────────────────────────────────────────

def test_postmarket_runs_after_nse_publishes():
    """NSE's Bhavcopy carries Last-Modified 16:33 IST; 16:05 always 404'd."""
    from pipeline import scheduler as S
    hh, mm = (int(x) for x in S.POSTMARKET_RUN_TIME.split(":"))
    assert (hh, mm) > (16, 33)
    ch, cm = (int(x) for x in S.POSTMARKET_CATCHUP_TIME.split(":"))
    assert (ch, cm) > (hh, mm)


# ── catch-up ──────────────────────────────────────────────────────────────

class _FrozenNow(dt.datetime):
    frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


def _freeze(monkeypatch, S, when):
    _FrozenNow.frozen = when
    monkeypatch.setattr(S, "datetime", _FrozenNow)


def test_catchup_runs_a_missed_postmarket(db, monkeypatch):
    """09-15: ATIP started at 16:59, after the slot -- that day was never scored."""
    from pipeline import scheduler as S
    calls = []
    monkeypatch.setattr(S, "run_postmarket", lambda force=False: calls.append(force))
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 15))
    _freeze(monkeypatch, S, dt.datetime(2026, 9, 15, 16, 59))
    S.run_postmarket_if_missing()
    assert calls == [True]


def test_catchup_is_a_noop_when_the_day_is_done(db, monkeypatch):
    from pipeline import scheduler as S
    _score(db, "2026-09-15", UNIVERSE)
    calls = []
    monkeypatch.setattr(S, "run_postmarket", lambda force=False: calls.append(force))
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 15))
    _freeze(monkeypatch, S, dt.datetime(2026, 9, 15, 18, 30))
    S.run_postmarket_if_missing()
    assert calls == []


def test_catchup_waits_for_the_scheduled_run_on_the_day(db, monkeypatch):
    """Before 16:45 today's run hasn't happened yet -- don't fire a doomed early one."""
    from pipeline import scheduler as S
    calls = []
    monkeypatch.setattr(S, "run_postmarket", lambda force=False: calls.append(force))
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 18))
    _freeze(monkeypatch, S, dt.datetime(2026, 9, 18, 16, 10))
    S.run_postmarket_if_missing()
    assert calls == []


def test_catchup_recovers_the_previous_session_on_a_weekend_start(db, monkeypatch):
    """09-11: machine asleep from 10:44; ATIP next started Saturday 09-12 11:31."""
    from pipeline import scheduler as S
    calls = []
    monkeypatch.setattr(S, "run_postmarket", lambda force=False: calls.append(force))
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 11))
    _freeze(monkeypatch, S, dt.datetime(2026, 9, 12, 11, 31))
    S.run_postmarket_if_missing()
    assert calls == [True]


# ── entry-price backfill ──────────────────────────────────────────────────

def _log_signal(conn, d, sym, price):
    import uuid
    from scores.signal_log import ensure_tables
    ensure_tables(conn)
    conn.execute("INSERT INTO signal_log (id, run_id, logged_at, signal_date, symbol, signal, "
                 "entry_price) VALUES (?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), "r", "2026-09-18T16:09:48", d, sym, "BUY", price))
    conn.commit()


def test_null_entry_price_is_filled_from_the_signal_dates_close(db):
    from scores.signal_log import fill_missing_entry_prices
    _log_signal(db, "2026-09-17", "ACME", None)
    _bar(db, "2026-09-17", ["ACME"], close=123.45)
    assert fill_missing_entry_prices(db) == 1
    px = db.execute("SELECT entry_price FROM signal_log WHERE symbol='ACME'").fetchone()[0]
    assert px == 123.45


def test_recorded_entry_price_is_never_overwritten(db):
    """Fill-once: completing a record is fine, rewriting one is not."""
    from scores.signal_log import fill_missing_entry_prices
    _log_signal(db, "2026-09-09", "ACME", 99.0)
    _bar(db, "2026-09-09", ["ACME"], close=200.0)
    assert fill_missing_entry_prices(db) == 0
    px = db.execute("SELECT entry_price FROM signal_log WHERE symbol='ACME'").fetchone()[0]
    assert px == 99.0


def test_holiday_signal_stays_null(db):
    """A day with no session has no close to find -- leave it honestly empty."""
    from scores.signal_log import fill_missing_entry_prices
    _log_signal(db, "2026-09-14", "ACME", None)
    _bar(db, "2026-09-15", ["ACME"], close=101.0)       # next day's bar must NOT be used
    assert fill_missing_entry_prices(db) == 0
    px = db.execute("SELECT entry_price FROM signal_log WHERE symbol='ACME'").fetchone()[0]
    assert px is None


def test_filled_signal_becomes_trackable(db):
    """The point of the fill: evaluate_outcomes() skips NULL entries entirely."""
    from scores.signal_log import evaluate_outcomes
    _log_signal(db, "2026-09-17", "ACME", None)
    _bar(db, "2026-09-17", ["ACME"], close=100.0)
    res = evaluate_outcomes()
    assert res["entry_prices_filled"] == 1
    n = db.execute("SELECT COUNT(*) FROM signal_outcome").fetchone()[0]
    assert n > 0, "a signal with a filled entry price must get outcome rows"
