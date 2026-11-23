"""The VPS review of 23 Nov 2026 (owner: "Cooldown + KSE/ADX"). The re-entry cooldown default is tested with the other
KGF-19 tests in tests/test_kgf_gaps_2026_11_16.py; here:

    KSE100 optional     every KSE100 source has failed from the VPS since 17 Nov; that alone no longer makes the Asia
                        run PARTIAL, and an index whose stored bars are stale no longer feeds the sentiment
    ADX short history   ADX(14) needs 28 bars; new listings with 20-27 days were counted as "adx" failures

No test reaches the network: every fetcher is replaced.
"""
from datetime import date, datetime

import pandas as pd
import pytest

from tests.test_asia_markets import IST, A, bars  # noqa: F401  (A is a pytest fixture)

LVL = {"N225": 62000, "TOPIX": 3970, "KOSPI": 5470, "KOSDAQ": 750, "TAIEX": 46000, "KSE100": 163000}
TICK = {".N225": "N225", ".TOPX": "TOPIX", "KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "TAIEX": "TAIEX", "49677": "KSE100"}
RUN_AT = datetime(2026, 10, 26, 18, 0, tzinfo=IST)


def _stub(A, monkeypatch, dead=()):
    """Every source answers with fresh bars, except the indices in `dead`, whose every source fails."""
    def f(t, n):
        code = TICK.get(t)
        if code is None or code in dead:
            raise ConnectionError("HTTP 403")
        return bars(min(n, 120), LVL[code], LVL[code] * 0.002)
    for k in A.FETCHERS:
        monkeypatch.setitem(A.FETCHERS, k, f)
    return A


def _store_old_kse(last_day):
    from data import asia_markets
    from db.schema import get_connection
    c = get_connection()
    asia_markets.ensure_tables(c)
    for b in bars(120, LVL["KSE100"], LVL["KSE100"] * 0.002, end=last_day):
        c.execute("INSERT INTO global_indices_daily (index_code, date, open, high, low, close) VALUES (?,?,?,?,?,?)",
                  ("KSE100", b["date"], b["open"], b["high"], b["low"], b["close"]))
    c.commit()
    c.close()


def test_kse100_failing_alone_leaves_the_run_successful(A, monkeypatch):
    r = _stub(A, monkeypatch, dead={"KSE100"}).run("close", now=RUN_AT)
    assert r["status"] == "SUCCESS" and r["failed"] == [] and r["optional_failed"] == ["KSE100"]
    assert r["error"] is None and r["sentiment"]["score"] is not None


def test_a_required_index_failing_is_still_partial(A, monkeypatch):
    r = _stub(A, monkeypatch, dead={"N225", "KSE100"}).run("close", now=RUN_AT)
    assert r["status"] == "PARTIAL" and r["failed"] == ["N225"] and r["optional_failed"] == ["KSE100"]


def test_stale_stored_kse100_bars_no_longer_feed_the_sentiment(A, monkeypatch):
    _store_old_kse(date(2026, 10, 9))                     # 17 days before the run: older than stale_after_days (7)
    r = _stub(A, monkeypatch, dead={"KSE100"}).run("close", now=RUN_AT)
    assert "KSE100" not in r["sentiment"]["components"] and r["markets"]["KSE100"]["signal"] is None
    assert r["sentiment"]["coverage"] < 1.0               # renormalised over what is present


def test_fresh_stored_bars_still_count_when_todays_fetch_fails(A, monkeypatch):
    _store_old_kse(date(2026, 10, 23))                    # Friday before the Monday run: 3 days, still fresh
    r = _stub(A, monkeypatch, dead={"KSE100"}).run("close", now=RUN_AT)
    assert "KSE100" in r["sentiment"]["components"] and r["markets"]["KSE100"]["signal"] is not None


def test_kse100_is_the_only_optional_index():
    from data import asia_markets
    idx = asia_markets.settings()["indexes"]
    assert [c for c, s in idx.items() if s.get("optional")] == ["KSE100"]


def _frame(n):
    import numpy as np
    close = 3000 + np.cumsum(np.sin(np.arange(n) / 5.0) * 8)
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n).astype(str), "open": close,
                         "high": close * 1.01, "low": close * 0.99, "close": close, "adj_close": [None] * n,
                         "volume": [100000] * n})


@pytest.mark.parametrize("n", [20, 25, 27])
def test_adx_is_skipped_not_failed_under_28_bars(temp_db, n):
    from data import technical
    if not technical.HAS_TA:
        pytest.skip("no TA library installed")
    technical._FAILED_INDICATORS.clear()
    ind = technical.compute_indicators("NEWLISTING", _frame(n))
    assert "adx" not in technical._FAILED_INDICATORS and ind.get("adx_14") is None
    assert ind.get("rsi_14") is not None                  # the rest of the block still runs


@pytest.mark.parametrize("n", [28, 60])
def test_adx_is_computed_from_28_bars(temp_db, n):
    from data import technical
    if not technical.HAS_TA:
        pytest.skip("no TA library installed")
    technical._FAILED_INDICATORS.clear()
    ind = technical.compute_indicators("SEASONED", _frame(n))
    assert "adx" not in technical._FAILED_INDICATORS and ind.get("adx_14") is not None
