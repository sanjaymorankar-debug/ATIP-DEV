"""
Scoring each session on its own FII/DII flows, and ZPI's delivery component.

NSE publishes FII/DII once a day after the close; at 16:45 it was still
serving the previous session's (2026-09-21), so every session was scored --
and its signals logged -- on the day before's flows. ZPI's "Institutional"
component read institutional_data, which nothing populates, so it was always
absent.
"""

import datetime as dt

import pandas as pd
import pytest

TD = dt.date(2026, 9, 22)


@pytest.fixture
def db(temp_db):
    from db.schema import init_db
    init_db()
    return temp_db


def _flows(conn, d, fii, dii):
    conn.execute("INSERT OR REPLACE INTO fii_dii_market (date, fii_net_cr, dii_net_cr) VALUES (?,?,?)",
                 (str(d), fii, dii))


# ── 5-day averages include the day ────────────────────────────────────────

def test_five_day_average_includes_the_session(db, monkeypatch):
    from db.schema import get_connection
    from data import bhavcopy
    conn = get_connection()
    for i, d in enumerate(["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]):
        _flows(conn, d, 100 * (i + 1), 10)
    conn.commit()
    monkeypatch.setattr(bhavcopy, "download_fii_dii", lambda d, s: {
        "date": "2026-09-21", "fii_buy_cr": 0, "fii_sell_cr": 0, "fii_net_cr": 900,
        "dii_buy_cr": 0, "dii_sell_cr": 0, "dii_net_cr": 60})
    avg = lambda: tuple(conn.execute("SELECT fii_5d_avg, dii_5d_avg FROM fii_dii_market "
                                     "WHERE date='2026-09-21'").fetchone())
    want = (pytest.approx((100 + 200 + 300 + 400 + 900) / 5), pytest.approx((10 * 4 + 60) / 5))
    try:
        bhavcopy.store_fii_dii(conn, dt.date(2026, 9, 21), session=object())
        assert avg() == want, "the session's own flows are in its 5-day average"
        bhavcopy.store_fii_dii(conn, dt.date(2026, 9, 21), session=object())   # a re-run
        assert avg() == want, "and a re-run does not count them twice"
    finally:
        conn.close()


# ── the watcher ───────────────────────────────────────────────────────────

@pytest.fixture
def watch(db, monkeypatch):
    """A held session and stubs for everything settle_session_flows calls."""
    from db.schema import log_job
    from pipeline import scheduler as S
    import data.bhavcopy, scores.engine, scores.portfolio_health, scores.signal_log, alerts.telegram
    calls = []
    published = {"yes": False}

    def store(conn, d, session=None):
        if published["yes"]:
            _flows(conn, d, -1500.0, 2500.0); conn.commit()
        return 1
    monkeypatch.setattr(data.bhavcopy, "store_fii_dii", store)
    monkeypatch.setattr(scores.engine, "run_scoring_pipeline", lambda d: calls.append("score") or {})
    monkeypatch.setattr(scores.portfolio_health, "store_phs", lambda d: calls.append("phs") or {})
    monkeypatch.setattr(scores.signal_log, "log_signals",
                        lambda d: calls.append("log") or log_job("signal_log", "SUCCESS", 0, run_date=d) or {})
    monkeypatch.setattr(scores.signal_log, "evaluate_outcomes", lambda: {})
    monkeypatch.setattr(alerts.telegram, "check_fii_alert", lambda d: calls.append("alert"))
    monkeypatch.setattr(S, "_rebuild_dashboard", lambda d: calls.append("dashboard") or {})
    monkeypatch.setattr(S, "postmarket_target_date", lambda: TD)
    S._hold_signals(TD)
    return S, calls, published


def test_nothing_is_logged_while_nse_has_not_published(watch):
    S, calls, published = watch
    assert S.settle_session_flows(TD)["reason"] == "not published yet"
    assert calls == [] and S._signal_log_state(TD) == "held"


def test_the_session_is_rescored_on_its_own_flows_when_they_appear(watch):
    S, calls, published = watch
    published["yes"] = True
    res = S.settle_session_flows(TD)
    assert res["rescored"] is True
    assert calls == ["score", "phs", "log", "alert", "dashboard"]
    assert S._signal_log_state(TD) == "logged"
    assert S.settle_session_flows(TD)["reason"] == "signals not held", "only once"
    assert calls.count("score") == 1


def test_by_the_deadline_signals_are_logged_anyway(watch):
    """No session is left unlogged because NSE was late; it is scored on the
    previous session's flows, as before, and says so in the log."""
    S, calls, published = watch
    res = S.settle_session_flows(TD, final=True)
    assert res == {"status": "PARTIAL", "rows": 1, "rescored": False}
    assert calls == ["log"]


def test_a_session_held_while_atip_was_down_is_released(watch, monkeypatch):
    S, calls, published = watch
    monkeypatch.setattr(S, "postmarket_target_date", lambda: dt.date(2026, 9, 23))
    ran = []
    monkeypatch.setattr(S, "run_job", lambda name, fn, *a, **k: ran.append((name, a)) or fn(*a, **k))
    S.release_held_signals(now=dt.datetime(2026, 9, 22, 20, 0))      # inside the window
    assert ran == []
    S.release_held_signals(now=dt.datetime(2026, 9, 23, 8, 0))       # the next morning
    assert ran[0] == ("fii_dii_watch", (TD, True)) and "signal_log" in [r[0] for r in ran]
    assert S._signal_log_state(TD) == "logged"


def test_the_watch_covers_the_evening():
    from pipeline import scheduler as S
    assert S.FII_DII_WATCH_START > S.POSTMARKET_RUN_TIME
    assert S.FII_DII_WATCH_END >= "21:00"


# ── ZPI's delivery component ──────────────────────────────────────────────

def _prices(recent, base, n_base=60):
    """newest first, as get_prices() returns them"""
    return pd.DataFrame({"delivery_pct": [recent] * 10 + [base] * (n_base - 10)})


@pytest.mark.parametrize("recent,base,expected", [
    (50.0, 50.0, 50.0),        # delivery as usual: neutral
    (70.0, 50.0, 100.0),       # 10-day average well above the 60-day one: accumulation
    (30.0, 50.0, 0.0),
])
def test_delivery_accumulation(recent, base, expected):
    from scores.engine import compute_delivery_accumulation
    got = compute_delivery_accumulation(_prices(recent, base))
    assert got == pytest.approx(expected, abs=0.5)


def test_delivery_accumulation_scale():
    """+12% against its own baseline is half-way to full scale (0.24)."""
    from scores.engine import compute_delivery_accumulation
    p = pd.DataFrame({"delivery_pct": [56.0] * 10 + [50.0] * 50})   # 60-day mean 51.0
    assert compute_delivery_accumulation(p) == pytest.approx(50 + 50 * (56 / 51 - 1) / 0.24, abs=0.01)


def test_delivery_accumulation_needs_history():
    from scores.engine import compute_delivery_accumulation
    assert compute_delivery_accumulation(_prices(50, 50, n_base=30)) is None
    sparse = _prices(50, 50)
    sparse.loc[:3, "delivery_pct"] = None                          # 6 of the last 10
    assert compute_delivery_accumulation(sparse) is None
    assert compute_delivery_accumulation(pd.DataFrame({"close": [1.0] * 60})) is None


def test_zpi_uses_the_component_when_there_is_one():
    from scores.engine import compute_zpi
    tech = {"above_200dma": 1, "adx_14": 20, "rsi_14": 45, "atr_pct": 2, "volume_ratio": 0.8}
    w = {"Support": 0.15, "Resistance": 0.15, "RSI": 0.10, "ATR": 0.10, "Volume": 0.10,
         "Trend": 0.10, "Institutional": 0.10, "News": 0.10, "Sector": 0.10}
    without = compute_zpi(tech, pd.DataFrame(), 50, 50, w)
    assert compute_zpi(tech, pd.DataFrame(), 50, 50, w, accumulation=None) == without
    assert compute_zpi(tech, pd.DataFrame(), 50, 50, w, accumulation=100) > without
    assert compute_zpi(tech, pd.DataFrame(), 50, 50, w, accumulation=0) < without
