"""W39: the Nifty 500 list download backs off after a failure and runs one at a time.

Before, while the cache was missing or stale, every caller (each wealth page read, the
scoring run, the technical run) retried NSE: up to 6 outbound calls per page read."""

import threading
import time

import pytest

CSV = b"Company Name,Industry,Symbol,Series,ISIN Code\nInfosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n" \
      b"Tata Consultancy,Information Technology,TCS,EQ,INE467B01029\n"


@pytest.fixture
def ic(tmp_path, monkeypatch):
    import data.index_constituents as ic
    monkeypatch.setattr(ic, "NIFTY500_CACHE", tmp_path / "ind_nifty500list.csv")
    monkeypatch.setattr(ic, "_last_failure", None)
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)
    return ic


def _session(ic, monkeypatch, ok, calls, delay=0.0):
    class R:
        content = CSV

        def raise_for_status(self):
            if not ok():
                raise ConnectionError("refused")

    class S:
        headers = {}

        def __init__(self):
            calls.append(1)

        def get(self, url, timeout=None):
            time.sleep(delay)
            if url == ic.NSE_HOME:
                return None
            return R()
    monkeypatch.setattr(ic.requests, "Session", S)


def test_a_failed_download_is_not_retried_by_every_caller(ic, monkeypatch):
    calls, up = [], {"ok": False}
    _session(ic, monkeypatch, lambda: up["ok"], calls)
    for _ in range(5):
        assert ic.get_symbol_industry_map() == {} and ic.fetch_nifty500_symbols() == []
    assert len(calls) == 1, "one attempt, then the backoff"
    up["ok"] = True
    assert ic.fetch_nifty500_symbols(force_refresh=True) == ["INFY", "TCS"]      # forced: always tries
    assert len(calls) == 2 and ic._last_failure is None
    assert ic.get_symbol_industry_map() == {"INFY": "Information Technology", "TCS": "Information Technology"}
    assert len(calls) == 2, "fresh cache: no download"


def test_the_backoff_expires_and_a_stale_cache_is_used_meanwhile(ic, monkeypatch):
    calls, up = [], {"ok": False}
    _session(ic, monkeypatch, lambda: up["ok"], calls)
    ic.NIFTY500_CACHE.write_bytes(CSV)
    old = time.time() - 10 * 86400
    import os
    os.utime(ic.NIFTY500_CACHE, (old, old))                                   # stale
    assert ic.fetch_nifty500_symbols() == ["INFY", "TCS"] and len(calls) == 1  # failed -> stale copy
    assert ic.fetch_nifty500_symbols() == ["INFY", "TCS"] and len(calls) == 1  # backoff
    monkeypatch.setattr(ic, "_last_failure", time.monotonic() - ic.FAIL_BACKOFF_MIN * 60 - 1)
    up["ok"] = True
    assert ic.fetch_nifty500_symbols() == ["INFY", "TCS"] and len(calls) == 2  # retried after the backoff
    assert ic._cache_is_fresh(ic.NIFTY500_CACHE)


def test_a_bad_download_never_overwrites_the_cache(ic, monkeypatch):
    calls = []
    _session(ic, monkeypatch, lambda: True, calls)
    ic.NIFTY500_CACHE.write_bytes(CSV)
    import os
    old = time.time() - 10 * 86400
    os.utime(ic.NIFTY500_CACHE, (old, old))
    import data.index_constituents as mod
    orig = mod.pd.read_csv

    def read(src, *a, **k):
        if not isinstance(src, (str, os.PathLike)):          # the downloaded bytes: an HTML error page
            raise ValueError("not a CSV")
        return orig(src, *a, **k)
    monkeypatch.setattr(mod.pd, "read_csv", read)
    assert ic.fetch_nifty500_symbols() == ["INFY", "TCS"]
    assert ic.NIFTY500_CACHE.read_bytes() == CSV and ic._last_failure is not None


def test_concurrent_callers_share_one_download(ic, monkeypatch):
    calls = []
    _session(ic, monkeypatch, lambda: True, calls, delay=0.2)
    out = []
    ts = [threading.Thread(target=lambda: out.append(ic.fetch_nifty500_symbols())) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and out == [["INFY", "TCS"]] * 6
