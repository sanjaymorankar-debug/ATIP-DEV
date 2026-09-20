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


@pytest.fixture
def tracked(monkeypatch):
    """The tracked universe the coverage guard measures against."""
    import data.dhan
    monkeypatch.setattr(data.dhan, "get_tracked_symbols", lambda conn=None: list(UNIVERSE))
    return UNIVERSE


# ── EOD coverage guard ────────────────────────────────────────────────────

def test_coverage_fails_when_the_days_bars_are_missing(db, tracked):
    """Exactly the 09-10..09-18 condition: scores exist for yesterday, no bars today."""
    from pipeline import scheduler as S
    _score(db, "2026-09-17", UNIVERSE)
    _bar(db, "2026-09-17", UNIVERSE)
    ok, n_eod, n_uni = S._eod_coverage(dt.date(2026, 9, 18))
    assert ok is False
    assert (n_eod, n_uni) == (0, 10)


def test_coverage_passes_when_the_days_bars_are_present(db, tracked):
    from pipeline import scheduler as S
    _score(db, "2026-09-17", UNIVERSE)
    _bar(db, "2026-09-18", UNIVERSE)
    assert S._eod_coverage(dt.date(2026, 9, 18))[0] is True


def test_coverage_uses_a_share_not_a_single_bar(db, tracked):
    """One stray bar must not pass for 'we have today's closes'."""
    from pipeline import scheduler as S
    _score(db, "2026-09-17", UNIVERSE)
    _bar(db, "2026-09-18", UNIVERSE[:2])
    assert S._eod_coverage(dt.date(2026, 9, 18))[0] is False


def test_coverage_allows_a_first_ever_run(db, monkeypatch):
    """With nothing tracked and nothing scored, there is nothing to judge."""
    import data.dhan
    from pipeline import scheduler as S
    monkeypatch.setattr(data.dhan, "get_tracked_symbols", lambda conn=None: [])
    assert S._eod_coverage(dt.date(2026, 9, 18))[0] is True


def test_coverage_bar_does_not_ratchet_down_after_a_partial_day(db, tracked):
    """
    Scoring only covers symbols that have a bar, so a half-covered day scores
    half the universe. Judged against the previous run's scored count, the
    requirement would halve every time: 10, 5, 2... Each day must be measured
    against the TRACKED universe instead.
    """
    from pipeline import scheduler as S
    _score(db, "2026-09-16", UNIVERSE)
    _bar(db, "2026-09-17", UNIVERSE[:5])              # half covered -> admitted
    assert S._eod_coverage(dt.date(2026, 9, 17)) == (True, 5, 10)
    _score(db, "2026-09-17", UNIVERSE[:5])            # so only 5 got scored
    _bar(db, "2026-09-18", UNIVERSE[:3])              # 3 of 10 the next day
    ok, n_eod, n_uni = S._eod_coverage(dt.date(2026, 9, 18))
    assert (n_eod, n_uni) == (3, 10), "the denominator must stay the tracked universe"
    assert ok is False, "3 of 10 is not a session, however few were scored yesterday"


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


# ── FII/DII dating ────────────────────────────────────────────────────────

class _NseSession:
    """NSE's fiidiiTradeReact: only ever the latest published day."""
    def __init__(self, nse_date):
        self.rows = [
            {"category": "DII **", "date": nse_date, "buyValue": "15,000.50", "sellValue": "13,650.86"},
            {"category": "FII/FPI *", "date": nse_date, "buyValue": "11,000.00", "sellValue": "11,123.19"},
        ]

    def get(self, *a, **k):
        rows = self.rows

        class R:
            def raise_for_status(self): pass
            def json(self): return rows
        return R()


@pytest.mark.parametrize("asked,served,stored", [
    # the backfill of 2026-09-18: asking for 09-10 got that evening's 09-18 figures
    (dt.date(2026, 9, 10), "18-Sep-2026", "2026-09-18"),
    # a run before NSE publishes gets the previous day's figures
    (dt.date(2026, 9, 18), "17-Sep-2026", "2026-09-17"),
    (dt.date(2026, 9, 18), "18-Sep-2026", "2026-09-18"),
    (dt.date(2026, 9, 8), "8-Sep-2026", "2026-09-08"),
])
def test_fii_dii_flows_are_stored_under_nses_date(tmp_path, monkeypatch, asked, served, stored):
    from data import bhavcopy
    monkeypatch.setattr(bhavcopy, "RAW_DIR", tmp_path)
    out = bhavcopy.download_fii_dii(asked, _NseSession(served))
    assert out["date"] == stored
    assert round(out["fii_net_cr"], 2) == -123.19
    assert round(out["dii_net_cr"], 2) == 1349.64
    # cached under the day the flows belong to, so a later run for `asked`
    # still fetches the real figures once NSE publishes them
    assert (tmp_path / f"fii_dii_{stored.replace('-', '')}.json").exists()
    if stored != str(asked):
        assert not (tmp_path / f"fii_dii_{asked:%Y%m%d}.json").exists()


def test_fii_dii_response_without_dates_keeps_the_asked_date(tmp_path, monkeypatch):
    from data import bhavcopy
    monkeypatch.setattr(bhavcopy, "RAW_DIR", tmp_path)
    s = _NseSession(None)
    for r in s.rows:
        del r["date"]
    assert bhavcopy.download_fii_dii(dt.date(2026, 9, 18), s)["date"] == "2026-09-18"


def test_fii_dii_response_mixing_dates_is_not_stored(tmp_path, monkeypatch):
    from data import bhavcopy
    monkeypatch.setattr(bhavcopy, "RAW_DIR", tmp_path)
    s = _NseSession("18-Sep-2026")
    s.rows[1]["date"] = "17-Sep-2026"
    assert bhavcopy.download_fii_dii(dt.date(2026, 9, 18), s) == {}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("text,expected", [
    ("18-Sep-2026", dt.date(2026, 9, 18)),
    ("8-sep-2026", dt.date(2026, 9, 8)),
    (" 01-JAN-2027 ", dt.date(2027, 1, 1)),
    ("31-Feb-2026", None),
    ("2026-09-18", None),
    ("", None),
    (None, None),
])
def test_nse_date_parsing(text, expected):
    from data.bhavcopy import _nse_date
    assert _nse_date(text) == expected


# ── FII/DII must still be fetched when prices are already stored ───────────

def test_flows_are_still_fetched_when_prices_are_already_stored(db, monkeypatch):
    """
    NSE publishes a session's flows in the evening, after 16:45 has already
    stored its prices. While the flows fetch sat behind the "bhavcopy already
    stored" early return, the 18:30 catch-up skipped it and no session's own
    flows were ever ingested.
    """
    from data import bhavcopy
    _bar(db, "2026-09-18", ["RELIANCE"])
    db.execute("UPDATE prices_daily SET source='bhavcopy' WHERE date='2026-09-18'")
    db.commit()
    called = []
    monkeypatch.setattr(bhavcopy, "store_fii_dii",
                        lambda conn, td, session=None: called.append(td) or 1)
    monkeypatch.setattr(bhavcopy, "get_nse_session", lambda: None)

    out = bhavcopy.run_bhavcopy_pipeline(dt.date(2026, 9, 18))

    assert out["status"] == "SKIPPED", "prices were already there"
    assert called == [dt.date(2026, 9, 18)], "but the flows must still be fetched"


def test_flows_are_stored_under_nses_date_via_the_pipeline(db, monkeypatch):
    from data import bhavcopy
    monkeypatch.setattr(bhavcopy, "download_fii_dii",
                        lambda td, session: {"date": "2026-09-17", "fii_net_cr": -100.0,
                                             "dii_net_cr": 250.0, "fii_buy_cr": 1.0,
                                             "fii_sell_cr": 101.0, "dii_buy_cr": 300.0,
                                             "dii_sell_cr": 50.0})
    assert bhavcopy.store_fii_dii(db, dt.date(2026, 9, 18), session=None) == 1
    rows = db.execute("SELECT date, fii_net_cr FROM fii_dii_market").fetchall()
    assert [(str(r[0]), r[1]) for r in rows] == [("2026-09-17", -100.0)]
