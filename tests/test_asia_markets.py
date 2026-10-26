"""
Task 3 (2026-10-26): South Korea, Taiwan, Pakistan and Japan market signals -- config-driven sessions and holidays,
primary -> fallback sources with health flags, storage + 600-day retention, per-market signals, the Asia Sentiment
Score (with the Pakistan risk flag as its own factor), Market Health input, morning-brief line, dashboard tab.
No test reaches the network: every fetcher is replaced.
"""

import json
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")


def local(tz, y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz))


def bars(n, start=100.0, step=0.5, end=date(2026, 10, 26), gap_open=None, noise=0.0):
    out, d, px, i = [], end, start, 0
    days = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    for k, day in enumerate(sorted(days)):
        px = start + step * k + (noise * math.sin(k * 1.7))
        out.append({"date": str(day), "open": px - step / 2, "high": px + 1, "low": px - 1, "close": px, "volume": 1000})
    if gap_open is not None:
        out[-1]["open"] = out[-2]["close"] * (1 + gap_open / 100)
    return out


@pytest.fixture
def A(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db, seed_weights
    from data import asia_markets
    init_db()
    seed_weights()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(asia_markets, "RETRY_PAUSE", 0)
    return asia_markets


def _conn():
    from db.schema import get_connection
    return get_connection()


# ── sessions and holidays come from config ─────────────────────────────────
@pytest.mark.parametrize("ex,when,expect", [
    ("JPX", ("Asia/Tokyo", 2026, 10, 27, 10, 0), "OPEN"),
    ("JPX", ("Asia/Tokyo", 2026, 10, 27, 12, 0), "BREAK"),
    ("JPX", ("Asia/Tokyo", 2026, 10, 27, 8, 0), "PRE_OPEN"),
    ("JPX", ("Asia/Tokyo", 2026, 10, 27, 15, 45), "CLOSED"),
    ("JPX", ("Asia/Tokyo", 2026, 11, 3, 10, 0), "HOLIDAY"),
    ("JPX", ("Asia/Tokyo", 2026, 10, 31, 10, 0), "WEEKEND"),
    ("KRX", ("Asia/Seoul", 2026, 10, 27, 9, 30), "OPEN"),
    ("KRX", ("Asia/Seoul", 2026, 10, 9, 10, 0), "HOLIDAY"),
    ("TWSE", ("Asia/Taipei", 2026, 10, 27, 14, 0), "CLOSED"),
    ("TWSE", ("Asia/Taipei", 2026, 10, 27, 9, 5), "OPEN"),
    ("PSX", ("Asia/Karachi", 2026, 10, 26, 10, 0), "OPEN"),
    ("PSX", ("Asia/Karachi", 2026, 10, 30, 13, 0), "BREAK"),     # Friday prayer break
    ("PSX", ("Asia/Karachi", 2026, 11, 9, 10, 0), "HOLIDAY"),
])
def test_session_status_from_the_exchange_config(A, ex, when, expect):
    assert A.session_status(ex, local(*when)) == expect


def test_japan_and_korea_open_before_atip_resumes(A):
    """05:30 IST: the 06:00 run sees their opening move."""
    assert A.session_times_ist("JPX", date(2026, 10, 27))[0][0] == "05:30"
    assert A.session_times_ist("KRX", date(2026, 10, 27))[0][0] == "05:30"
    assert A.session_status("JPX", datetime(2026, 10, 27, 6, 0, tzinfo=IST)) == "OPEN"
    assert A.session_status("PSX", datetime(2026, 10, 27, 6, 0, tzinfo=IST)) == "PRE_OPEN"


def test_hours_and_holidays_are_overridable_from_config_json(A, tmp_path):
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(json.dumps({"asia_markets": {"exchanges": {
        "TWSE": {"sessions": [["09:00", "14:30"]], "holidays": ["2026-10-27"]}}}}))
    cfg = A.settings()
    assert A.session_status("TWSE", local("Asia/Taipei", 2026, 10, 27, 10, 0), cfg) == "HOLIDAY"
    assert A.session_status("TWSE", local("Asia/Taipei", 2026, 10, 28, 14, 0), cfg) == "OPEN"
    assert cfg["exchanges"]["JPX"]["tz"] == "Asia/Tokyo", "the rest of the file is kept"


def test_no_trading_hours_are_hardcoded_in_the_module():
    import inspect
    from data import asia_markets
    src = inspect.getsource(asia_markets)
    for t in ('"09:00"', '"15:30"', '"13:30"', '"09:30"'):
        assert t not in src, f"{t} belongs in data/asia_exchanges.json"


# ── sources: primary, fallback, health flags ───────────────────────────────
def test_fallback_is_used_and_the_failed_primary_flagged(A, monkeypatch):
    def dead(t, n):
        raise ConnectionError("HTTP 462")
    monkeypatch.setitem(A.FETCHERS, "investing", dead)
    monkeypatch.setitem(A.FETCHERS, "psx_dps", lambda t, n: bars(60, 160000, 50))
    c = _conn()
    try:
        A.ensure_tables(c)
        f = A.fetch_index(c, "KSE100", 60, today=date(2026, 10, 26))
        c.commit()
        h = {(r["kind"], r["ok"]) for r in c.execute("SELECT kind, ok FROM asia_source_health").fetchall()}
    finally:
        c.close()
    assert f["fallback"] and f["source"] == "psx_dps:KSE100" and len(f["bars"]) == 60
    assert ("investing", 0) in h and ("psx_dps", 1) in h


@pytest.mark.parametrize("bad,why", [([], "no data"), (bars(10, end=date(2026, 10, 1)), "stale"),
                                     ([{**b, "close": 100.0} for b in bars(10)], "flat series")])
def test_a_source_returning_bad_data_counts_as_failed(A, bad, why):
    assert why in A.validate_bars(bad, 7, date(2026, 10, 26))


def test_every_source_failing_is_reported_not_hidden(A, monkeypatch):
    for k in A.FETCHERS:
        monkeypatch.setitem(A.FETCHERS, k, lambda t, n: [])
    r = A.run("test", now=datetime(2026, 10, 26, 10, 0, tzinfo=IST))
    assert r["status"] == "FAILED" and set(r["failed"]) == set(A.settings()["indexes"])
    assert r["sentiment"]["score"] is None or r["sentiment"]["coverage"] <= 0.11


def test_verify_probes_every_source_not_just_the_first(A, monkeypatch):
    seen = []
    for k in A.FETCHERS:
        monkeypatch.setitem(A.FETCHERS, k, lambda t, n, k=k: seen.append(k) or bars(30))
    out = A.verify_sources()
    n_sources = sum(len(v["sources"]) for v in A.settings()["indexes"].values())
    assert len(out) == n_sources == len(seen) and all(r["ok"] for r in out)


# ── parsers (recorded payload shapes from the 2026-10-26 probe) ────────────
def test_naver_korea_xml_parser(A, monkeypatch):
    class R:
        text = ('<chartdata symbol="KOSPI"><item data="20261023|5403.17|5550.3|5305.98|5550.3|249517" />'
                '<item data="20261026|5499.05|5565.67|5455.11|5469.47|202295" /></chartdata>')
    monkeypatch.setattr(A, "_get", lambda u, h=None: R())
    b = A.fetch_naver_kr("KOSPI", 5)
    assert b[-1] == {"date": "2026-10-26", "open": 5499.05, "high": 5565.67, "low": 5455.11, "close": 5469.47,
                     "volume": 202295.0}


def test_twse_parser_handles_commas_and_walks_back_months(A, monkeypatch):
    class R:
        def __init__(self, u):
            self.u = u

        def json(self):
            if "20261001" in self.u:
                return {"stat": "OK", "data": [["2026/10/01", "47,961.98", "48,353.49", "47,893.42", "48,353.49"]]}
            return {"stat": "OK", "data": [["115/09/30", "47,000.00", "47,500.00", "46,900.00", "47,400.00"]]}
    monkeypatch.setattr(A, "_get", lambda u, h=None: R(u))
    b = A.fetch_twse("TAIEX", 2, today=date(2026, 10, 26))
    assert [x["date"] for x in b] == ["2026-09-30", "2026-10-01"] and b[-1]["close"] == 48353.49


def test_investing_parser(A, monkeypatch):
    class R:
        def json(self):
            return {"data": [{"rowDateTimestamp": "2026-10-26T00:00:00Z", "last_closeRaw": "163038.47",
                              "last_openRaw": "162335.0", "last_maxRaw": "163477.09", "last_minRaw": "161250.06",
                              "volumeRaw": 114584856}]}
    monkeypatch.setattr(A, "_get", lambda u, h=None: R())
    assert A.fetch_investing("49677", 5, today=date(2026, 10, 26))[0]["close"] == 163038.47


# ── signals ────────────────────────────────────────────────────────────────
def test_an_uptrend_is_bullish_a_downtrend_bearish(A):
    cfg = A.settings()
    up = A.compute_signal(bars(80, 100, 1.0, gap_open=0.8), cfg)
    dn = A.compute_signal(bars(80, 200, -1.0, gap_open=-0.8), cfg)
    assert up["signal"] == "BULLISH" and up["score"] > 20 and 0 < up["confidence"] <= 100
    assert dn["signal"] == "BEARISH" and dn["score"] < -20
    assert up["gap_pct"] == pytest.approx(0.8, abs=0.01)
    for k in ("trend", "momentum", "volatility", "gap"):
        assert -1 <= up[k] <= 1


def test_a_sideways_tape_is_neutral(A):
    """Regression for the defect found 2026-10-26: a flat, choppy series scored a full +1 trend."""
    b = bars(80, 100, 0.0)
    for k, x in enumerate(b):
        x["close"] = 100 + (0.1 if k % 2 else -0.1)
        x["open"] = b[k - 1]["close"] if k else x["close"]
    s = A.compute_signal(b, A.settings())
    assert s["signal"] == "NEUTRAL" and s["trend"] == 0


def test_too_little_history_gives_no_signal(A):
    assert A.compute_signal(bars(10), A.settings()) is None


def test_a_volatility_spike_reads_risk_off(A):
    b = bars(120, 100, 0.05)
    for i, x in enumerate(b[-20:]):
        x["close"] = x["close"] * (1.04 if i % 2 else 0.96)
    s = A.compute_signal(b, A.settings())
    assert s["vol_regime"] == "HIGH" and s["volatility"] < 0


# ── Asia Sentiment Score + Pakistan risk ───────────────────────────────────
def _sig(score, conf=80):
    return {"score": score, "confidence": conf}


def test_sentiment_is_the_configured_weighted_mean(A):
    cfg = A.settings()
    allbull = {k: _sig(60) for k in cfg["indexes"]}
    s = A.asia_sentiment(allbull, {"level": "NONE"}, cfg)
    assert s["signal"] == "BULLISH" and s["coverage"] == 1.0
    assert s["score"] == pytest.approx((80 * 0.9 + 50 * 0.1) / 1.0, abs=0.01)


def test_pakistan_risk_is_a_separate_factor_that_pulls_the_score_down(A):
    cfg = A.settings()
    sig = {k: _sig(0) for k in cfg["indexes"]}
    none = A.asia_sentiment(sig, {"level": "NONE"}, cfg)["score"]
    high = A.asia_sentiment(sig, {"level": "HIGH"}, cfg)["score"]
    assert none == 50.0 and high == pytest.approx(45.0) and A.asia_sentiment(sig, {"level": "HIGH"}, cfg)["components"]["PK_GEO_RISK"] == 0


def test_weights_are_configurable(A, tmp_path):
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(json.dumps({"asia_markets": {"weights": {
        "N225": 1.0, "TOPIX": 0, "KOSPI": 0, "KOSDAQ": 0, "TAIEX": 0, "KSE100": 0, "PK_GEO_RISK": 0}}}))
    cfg = A.settings()
    s = A.asia_sentiment({"N225": _sig(-100), "KOSPI": _sig(100)}, {"level": "HIGH"}, cfg)
    assert s["score"] == 0.0 and s["signal"] == "BEARISH"


def test_a_missing_market_renormalises(A):
    cfg = A.settings()
    s = A.asia_sentiment({"N225": _sig(40), "KSE100": None}, {"level": "NONE"}, cfg)
    assert s["coverage"] == pytest.approx(0.35) and s["score"] == pytest.approx((70 * 0.25 + 50 * 0.10) / 0.35, abs=0.01)


def _news(rows):
    c = _conn()
    now = datetime(2026, 10, 26, 6, 0)
    for h, s, imp, cat, cl in rows:
        c.execute("INSERT INTO news_articles (headline, sentiment, importance, category, classifier, fetched_at) "
                  "VALUES (?,?,?,?,?,?)", (h, s, imp, cat, cl, str(now - timedelta(hours=2))))
    c.commit()
    return c


@pytest.mark.parametrize("news_rows,level", [
    ([], "NONE"),
    ([("Infosys wins large order", -0.5, "HIGH", "EARNINGS", "claude")], "NONE"),
    ([("Pakistan army reports border clash near LoC", -0.4, "MEDIUM", "GEOPOLITICS", "claude")], "ELEVATED"),
    ([("Pakistan border tension escalates", -0.6, "MEDIUM", "GEOPOLITICS", "claude"),
      ("Missile strike reported in Pakistan's Balochistan", -0.7, "MEDIUM", "GEOPOLITICS", "rule"),
      ("Pakistan IMF talks stall, default fears grow", -0.5, "MEDIUM", "GLOBAL", "claude")], "HIGH"),
    ([("Karachi blast kills several", -0.8, "HIGH", "GENERAL", "claude")], "HIGH"),
])
def test_pakistan_risk_flag_from_news(A, news_rows, level):
    c = _news(news_rows)
    try:
        r = A.pakistan_risk(c, datetime(2026, 10, 26, 6, 0, tzinfo=IST))
    finally:
        c.close()
    assert r["level"] == level


# ── the job: storage, retention, Market Health, brief, dashboard ───────────
@pytest.fixture
def stubbed(A, monkeypatch):
    lvl = {"N225": 62000, "TOPIX": 3970, "KOSPI": 5470, "KOSDAQ": 750, "TAIEX": 46000, "KSE100": 163000}
    tick = {".N225": "N225", ".TOPX": "TOPIX", "KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "TAIEX": "TAIEX", "49677": "KSE100"}

    def f(t, n):
        code = tick.get(t)
        if code is None:
            raise ConnectionError("not used")
        return bars(min(n, 120), lvl[code], lvl[code] * 0.002)
    for k in A.FETCHERS:
        monkeypatch.setitem(A.FETCHERS, k, f)
    return A


def test_run_stores_ohlc_session_status_signals_and_sentiment(stubbed):
    A = stubbed
    r = A.run("open", now=datetime(2026, 10, 26, 6, 0, tzinfo=IST))
    assert r["status"] == "SUCCESS" and not r["failed"] and r["sentiment"]["score"] is not None
    c = _conn()
    try:
        n = c.execute("SELECT COUNT(DISTINCT index_code) FROM global_indices_daily").fetchone()[0]
        st = dict(c.execute("SELECT index_code, session_status FROM global_indices_daily WHERE date='2026-10-26'").fetchall())
        sig = c.execute("SELECT COUNT(*) FROM asia_signals WHERE `signal` IN ('BULLISH','NEUTRAL','BEARISH')").fetchone()[0]
        sent = c.execute("SELECT run_label, pk_risk_level FROM asia_sentiment").fetchall()
        orders = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='order_rules'").fetchone()[0]
        placed = c.execute("SELECT COUNT(*) FROM order_rules").fetchone()[0] if orders else 0
    finally:
        c.close()
    assert n == 6 and sig == 6 and [tuple(x) for x in sent] == [("open", "NONE")]
    assert st["N225"] == "OPEN" and st["KSE100"] == "PRE_OPEN", "06:00 IST: Tokyo open, Karachi not yet"
    assert placed == 0, "informational only: nothing is ordered"


def test_new_tables_are_on_the_600_day_retention(stubbed):
    from db import purge
    for t in ("global_indices_daily", "asia_signals", "asia_sentiment"):
        assert t in purge.LONG_RETENTION_TABLES
    stubbed.run("open", now=datetime(2026, 10, 26, 6, 0, tzinfo=IST))
    c = _conn()
    c.execute("INSERT INTO global_indices_daily (index_code, date, close) VALUES ('N225', '2024-01-05', 1.0)")
    c.commit()
    c.close()
    purge.purge_old_data(long_days=600, short_days=90, dry_run=False)
    c = _conn()
    try:
        assert not c.execute("SELECT 1 FROM global_indices_daily WHERE date='2024-01-05'").fetchone()
        assert c.execute("SELECT COUNT(*) FROM global_indices_daily").fetchone()[0] > 0
    finally:
        c.close()


def test_new_tables_are_classified_for_the_data_inventory(stubbed):
    from enterprise.privacy import classify
    for t in ("global_indices_daily", "asia_signals", "asia_sentiment", "asia_source_health", "scheduler_status",
              "resume_runs", "deferred_jobs", "premarket_reports", "ema_gap_sessions"):
        assert classify(t) is not None, t


def test_market_health_takes_the_asia_score_with_its_configured_weight(stubbed):
    from scores.engine import compute_mh, load_weights
    stubbed.run("close", now=datetime(2026, 10, 26, 17, 15, tzinfo=IST))
    c = _conn()
    try:
        w = load_weights(c, "MH")
        assert w["Asia"] == 0.05
        c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('NIFTY50', '2026-10-26', 25000)")
        out = compute_mh("2026-10-26", c, w, breadth={"pct_above_200dma": 55.0, "pct_advancing": 50.0}, quiet=True)
        inputs = c.execute("SELECT mh_inputs FROM market_health WHERE date='2026-10-26'").fetchone()[0]
    finally:
        c.close()
    assert "Asia" in inputs.split(",")


def test_mh_weight_zero_switches_the_input_off(A, tmp_path):
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(json.dumps({"asia_markets": {"mh_weight": 0}}))
    c = _conn()
    try:
        A.ensure_tables(c)
        assert c.execute("SELECT active FROM weight_config WHERE index_name='MH' AND variable='Asia'").fetchone()[0] == 0
    finally:
        c.close()


def test_morning_brief_carries_the_asia_line(stubbed):
    from alerts.telegram import _market_lines
    stubbed.run("open", now=datetime(2026, 10, 26, 6, 0, tzinfo=IST))
    c = _conn()
    try:
        lines = _market_lines(c, "2026-10-23", now=datetime(2026, 10, 26, 8, 30))
    finally:
        c.close()
    asia = [x for x in lines if x.startswith("<b>Asia:</b>")]
    assert asia and "Nikkei 225" in asia[0]


def test_global_and_asia_tab_and_page(stubbed, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    stubbed.run("open", now=datetime(2026, 10, 26, 6, 0, tzinfo=IST))
    c = TestClient(srv.app)
    d = c.get("/api/asia/markets").json()
    assert [m["code"] for m in d["markets"]] == ["N225", "TOPIX", "KOSPI", "KOSDAQ", "TAIEX", "KSE100"]
    for m in d["markets"]:
        assert m["last"] and m["chg_pct"] is not None and m["signal"] in ("BULLISH", "NEUTRAL", "BEARISH")
        assert m["session_status"] in ("OPEN", "BREAK", "PRE_OPEN", "CLOSED", "HOLIDAY", "WEEKEND")
    assert d["informational_only"] is True and "global" in d
    home = c.get("/").text
    assert "Global &amp; Asia</div>" in home and 'id="asia" class="tc section"' in home
    page = c.get("/global-asia")
    assert page.status_code == 200 and 'name="viewport"' in page.text and "atipLoadAsia" in page.text
    assert c.get("/m").text.count("Global &amp; Asia") >= 1
    src = c.get("/api/asia/sources").json()
    assert {"sources", "calendar_check"} <= set(src)


def test_calendar_check_flags_a_missing_weekday(stubbed):
    A = stubbed
    A.run("open", now=datetime(2026, 10, 26, 6, 0, tzinfo=IST))
    c = _conn()
    c.execute("DELETE FROM global_indices_daily WHERE index_code='KOSPI' AND date='2026-10-22'")
    c.commit()
    try:
        issues = A.calendar_check(c, today=date(2026, 10, 26))
    finally:
        c.close()
    assert {"index": "KOSPI", "exchange": "KRX", "date": "2026-10-22",
            "issue": "weekday with no bar and not listed as a holiday"} in issues
