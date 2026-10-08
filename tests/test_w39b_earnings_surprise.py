"""
W39b (gap analysis §4 item 7): earnings surprise from ATIP's own quarterly EPS history -- SUE against the
seasonal random walk (hand-computed on seeded quarters), the revenue analogue and the EPS-trend revisions
proxy; insufficient history says why; point in time from each filing's broadcast (a quarter filed later is
invisible earlier); the post-earnings-drift scan inside the technical signal engine (fires once, on the first
session after the filing, not on stale filings, alerts held until 30 closed signals with a positive average R);
the screener fields and preset; the idempotent nightly job; the API and the research page.
"""
import datetime as dt
import json
import math
import statistics

import pandas as pd
import pytest

from research import earnings_surprise as ES

# 13 quarter ends, FY23Q1 (June 2022) .. FY26Q1 (June 2025); each filed 39 days later at 18:05
QE = [dt.date(2022, 6, 30), dt.date(2022, 9, 30), dt.date(2022, 12, 31), dt.date(2023, 3, 31),
      dt.date(2023, 6, 30), dt.date(2023, 9, 30), dt.date(2023, 12, 31), dt.date(2024, 3, 31),
      dt.date(2024, 6, 30), dt.date(2024, 9, 30), dt.date(2024, 12, 31), dt.date(2025, 3, 31),
      dt.date(2025, 6, 30)]
LAG = 39                                    # FY26Q1 is broadcast Friday 2025-08-08 18:05
EPS = [10, 11, 12, 13, 11, 11, 14, 13, 12, 12, 15, 15, 17]
REV = [100, 100, 100, 100, 110, 105, 110, 105, 120, 110, 120, 110, 140]
FILED = dt.date(2025, 8, 8)                 # the last quarter's broadcast day
FIRST = dt.date(2025, 8, 11)                # the first session after it (Monday)
DAYS = pd.bdate_range("2025-01-01", "2025-09-30")

# Hand computation for FY26Q1 (EPS 17 vs 12 a year earlier):
#   the 8 earlier seasonal changes FY24Q1..FY25Q4: 1, 0, 2, 0, 1, 1, 1, 2 -> mean 1, squares 4, sd = sqrt(4/7)
#   SUE = 5 / sqrt(4/7) = 6.614
#   revenue: changes 10, 5, 10, 5, 10, 5, 10, 5 -> sd = sqrt(50/7); 140 - 120 = 20 -> SUE 7.483
#   TTM EPS: FY26Q1 59 vs 50 a year earlier -> 18 %; FY25Q4 54 vs 49 -> 10.204 %; trend +7.80 pts, ACCELERATING
SUE_HAND = 5 / math.sqrt(4 / 7)
SUE_REV_HAND = 20 / math.sqrt(50 / 7)


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as TS
    init_db()
    conn = get_connection()
    TS.ensure_tables(conn)
    ES.ensure_tables(conn)
    yield conn
    conn.close()


def _seed(conn, sym, eps=EPS, rev=REV, broadcast=None, exact=True, nature="CONSOLIDATED"):
    """fundamental_data rows as data/nse_filings.py writes them; broadcast {index: 'YYYY-MM-DD HH:MM:SS'} overrides."""
    from data.nse_filings import quarter_label
    for i, pe in enumerate(QE[:len(eps)]):
        av = (broadcast or {}).get(i, f"{pe + dt.timedelta(days=LAG)} 18:05:00") if exact else None
        conn.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, report_date, available_from, nature, "
                     "eps_q, revenue_cr, source) VALUES (?,?,?,?,?,?,?,?,'nse_xbrl')",
                     (sym, quarter_label(pe), str(pe), str(pe), av, nature, eps[i], rev[i] if rev else None))
    conn.commit()


def _bars(conn, sym, base=100.0, slope=0.1):
    for i, d in enumerate(DAYS):
        c = base + slope * i + math.sin(i / 3)
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,"
                     "'dhan')", (sym, str(d.date()), c - 0.3, c + 1.0, c - 1.0, c, 1e5))
    conn.commit()


def _market(conn):
    """A flat Nifty (NIFTYBEES) and an OPEN market gate on every session."""
    _bars(conn, "NIFTYBEES", base=250.0, slope=0.0)
    conn.execute("UPDATE prices_daily SET close=250, open=250, high=250.5, low=249.5 WHERE symbol='NIFTYBEES'")
    for d in DAYS:
        conn.execute("INSERT INTO market_regime_gate (date, gate, status) VALUES (?, 'OPEN', 'CONFIRMED_UPTREND')",
                     (str(d.date()),))
    conn.commit()


def _rows(conn, sym):
    return {r["quarter"]: r for r in ES.compute(ES.load_quarters(conn, [sym])[sym])}


def _signals(conn, sym=None):
    sql = "SELECT * FROM technical_signal" + (" WHERE symbol=?" if sym else "") + " ORDER BY symbol, date"
    cur = conn.execute(sql, (sym,) if sym else ())
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]


# ── SUE, revenue SUE and the EPS trend ─────────────────────────────────────

def test_sue_hand_computed_on_seeded_quarters(db):
    _seed(db, "ACME")
    last = _rows(db, "ACME")["FY26Q1"]
    assert last["eps_q"] == 17 and last["eps_year_ago"] == 12 and last["sue_n"] == 8
    assert last["eps_sd"] == pytest.approx(math.sqrt(4 / 7), abs=1e-4)
    assert last["sue"] == pytest.approx(SUE_HAND, abs=1e-3) and last["sue"] == pytest.approx(6.614, abs=1e-3)
    assert last["sue_revenue"] == pytest.approx(SUE_REV_HAND, abs=1e-3) and last["sue_revenue_n"] == 8
    assert last["revenue_year_ago"] == 120 and last["revenue_sd"] == pytest.approx(math.sqrt(50 / 7), abs=1e-4)
    assert last["ttm_eps"] == 59 and last["ttm_growth_pct"] == pytest.approx(18.0)
    assert last["ttm_growth_prev_pct"] == pytest.approx(500 / 49, abs=0.01)
    assert last["eps_trend"] == "ACCELERATING" and last["eps_trend_pts"] == pytest.approx(18 - 500 / 49, abs=0.01)
    assert last["known_on"] == FILED and last["exact_date"] == 1 and last["sue_reason"] is None


def test_negative_surprise_and_a_decelerating_trend(db):
    _seed(db, "DOWN", eps=EPS[:-1] + [9])
    last = _rows(db, "DOWN")["FY26Q1"]
    assert last["sue"] == pytest.approx(-3 / math.sqrt(4 / 7), abs=1e-3)                 # -3.969
    assert last["ttm_growth_pct"] == pytest.approx(2.0)                                     # 51 vs 50
    assert last["eps_trend"] == "DECELERATING" and last["eps_trend_pts"] == pytest.approx(2 - 500 / 49, abs=0.01)


def test_insufficient_history_says_why(db):
    _seed(db, "ACME")
    q = _rows(db, "ACME")
    assert q["FY23Q4"]["sue"] is None and "quarter a year before FY23Q4 is not known" in q["FY23Q4"]["sue_reason"]
    assert q["FY24Q4"]["sue"] is None and q["FY24Q4"]["sue_n"] == 3                         # FY24Q1..Q3 only
    assert "only 3 of the 8 earlier year-on-year EPS changes are known (needs 4)" == q["FY24Q4"]["sue_reason"]
    assert q["FY25Q1"]["sue"] is not None and q["FY25Q1"]["sue_n"] == 4, "four changes are enough"
    assert q["FY24Q3"]["eps_trend"] is None and "needs EPS for 8 quarters" in q["FY24Q3"]["eps_trend_reason"]
    assert q["FY24Q4"]["eps_trend"] is None and "for the filing before FY24Q4" in q["FY24Q4"]["eps_trend_reason"]
    assert q["FY25Q1"]["eps_trend"] == "DECELERATING"                                        # 6.38 % vs 6.52 %


def test_no_variation_missing_eps_and_consolidated_vs_standalone():
    def q(i, eps, nature="CONSOLIDATED"):
        return {"quarter": f"Q{i}", "period_end": QE[i], "known_on": QE[i] + dt.timedelta(days=LAG), "exact": True,
                "available_from": None, "nature": nature, "eps": eps, "revenue": None}
    steady = [q(i, 10 + i // 4) for i in range(12)] + [q(12, 15)]        # every year-on-year change exactly 1
    last = ES.compute(steady)[-1]
    assert last["sue"] is None and "all equal (standard deviation 0)" in last["sue_reason"]
    assert last["sue_revenue"] is None and last["sue_revenue_reason"] == "no revenue reported for Q12"
    switched = [q(i, EPS[i], "STANDALONE") for i in range(12)] + [q(12, 17)]
    last = ES.compute(switched)[-1]
    assert last["sue"] is None and "not comparable" in last["sue_reason"]


# ── point in time ──────────────────────────────────────────────────────────

def test_point_in_time_from_the_broadcast_not_the_period_end(db):
    _seed(db, "ACME")
    ES.refresh(db)
    before = ES.latest(db, ["ACME"], FILED - dt.timedelta(days=1))["ACME"]
    assert before["quarter"] == "FY25Q4" and before["days_since_result"] == 90          # filed 2025-05-09
    assert ES.latest(db, ["ACME"], dt.date(2025, 7, 31))["ACME"]["quarter"] == "FY25Q4", \
        "June's quarter has ended but is not public yet"
    on = ES.latest(db, ["ACME"], FILED)["ACME"]
    assert on["quarter"] == "FY26Q1" and on["days_since_result"] == 0 and on["sue"] == pytest.approx(SUE_HAND, abs=1e-3)
    assert ES.latest(db, ["ACME"], dt.date(2022, 8, 1)) == {}, "nothing was public yet"


def test_a_quarter_filed_later_is_invisible_earlier(db):
    _seed(db, "LATE", broadcast={11: "2025-08-20 10:00:00"})      # FY25Q4 filed after FY26Q1
    q = _rows(db, "LATE")
    diffs = [1, 0, 2, 0, 1, 1, 1]                                   # FY25Q4's change is not known on 8 August
    assert q["FY26Q1"]["sue_n"] == 7 and q["FY26Q1"]["sue"] == pytest.approx(5 / statistics.stdev(diffs), abs=1e-3)
    assert q["FY26Q1"]["eps_trend"] is None, "TTM growth needs FY25Q4"
    ES.refresh(db)
    assert ES.latest(db, ["LATE"], dt.date(2025, 8, 10))["LATE"]["quarter"] == "FY26Q1"
    assert ES.latest(db, ["LATE"], dt.date(2025, 8, 7))["LATE"]["quarter"] == "FY25Q3"


def test_without_a_broadcast_time_the_quarter_counts_45_days_after_its_end(db):
    _seed(db, "EST", exact=False)
    last = _rows(db, "EST")["FY26Q1"]
    assert last["known_on"] == dt.date(2025, 8, 14) and last["exact_date"] == 0 and last["available_from"] is None


# ── the post-earnings-drift scan in the signal engine ─────────────────────

def test_pead_fires_once_on_the_first_session_after_the_filing(db):
    from research import tech_signals as TS
    _seed(db, "ACME")
    _seed(db, "MILD", eps=EPS[:-1] + [13])                           # SUE +1.32: below the trigger
    _seed(db, "EST", exact=False)                                    # an estimated date is no event date
    for s in ("ACME", "MILD", "EST"):
        _bars(db, s)
    _market(db)
    ES.refresh(db)
    assert ES.scan(db, FILED)["signals"] == 0, "filed after the close: not tradable that day"
    out = ES.scan(db, FIRST)
    assert out["signals"] == 1 and out["late"] == 0
    sig = _signals(db)
    assert len(sig) == 1
    s = sig[0]
    close = db.execute("SELECT close FROM prices_daily WHERE symbol='ACME' AND date=?", (str(FIRST),)).fetchone()[0]
    assert (s["symbol"], str(s["date"]), s["scan"], s["direction"], s["horizon"]) == ("ACME", str(FIRST), "pead_bull",
                                                                                    "BULL", 60)
    assert s["name"] == TS.EVENT_SCANS["pead_bull"][0] and s["status"] == "OPEN"
    assert set(ES.PEAD_SCANS) == set(TS.EVENT_SCANS) and set(ES.PEAD_SCANS) <= TS.HELD_SCANS
    assert s["entry"] == pytest.approx(close) and s["atr"] > 0
    assert (s["stop"], s["target"]) == TS.levels("BULL", s["entry"], s["atr"])          # 2 x / 4 x ATR
    assert set(json.loads(s["evidence_json"])) == {"technical rating", "volume > 1.5x", "relative strength",
                                                   "market regime", "candle pattern", "research rating"}
    assert 0 <= s["confluence"] <= 6 and (s["market_gate"], s["alignment"]) == ("OPEN", "WITH")
    assert "FY26Q1 EPS 17.00 vs 12.00 a year earlier: SUE +6.61" in s["reason"] and "revenue SUE +7.48" in s["reason"]
    for later in (dt.date(2025, 8, 12), dt.date(2025, 8, 20), dt.date(2025, 9, 30)):
        assert ES.scan(db, later)["signals"] == 0
    assert len(_signals(db)) == 1, "once, and dated on its first session"
    TS.evaluate_forward(db)
    s = _signals(db)[0]
    assert s["ret_5d"] is not None and s["excess_5d"] == pytest.approx(s["ret_5d"]), "the Nifty is flat"
    assert s["ret_60d"] is None, "60 sessions have not passed"
    assert "pead_bull" in {o["scan"] for o in TS.forward_stats(db, 5)["by_scan"]}


def test_stale_filings_do_not_fire_and_a_late_one_is_dated_on_its_session(db):
    _seed(db, "ACME")
    _seed(db, "DOWN", eps=EPS[:-1] + [9])
    for s in ("ACME", "DOWN"):
        _bars(db, s)
    _market(db)
    ES.refresh(db)
    # FY25Q4 (filed 2025-05-09) also beat: EPS 15 vs 13, SUE 2 / sd(1, 0, 2, 0, 1, 1, 1) = +2.90 -- long stale here
    assert _rows(db, "ACME")["FY25Q4"]["sue"] == pytest.approx(2 / statistics.stdev([1, 0, 2, 0, 1, 1, 1]), abs=1e-3)
    out = ES.scan(db, dt.date(2025, 9, 30))              # 35 sessions after FY26Q1's first: stale too
    assert out["signals"] == 0 and out["stale"] == 4 and _signals(db) == []
    out = ES.scan(db, dt.date(2025, 8, 14))              # found three sessions late: inside the catch-up window
    assert out["signals"] == 2 and out["late"] == 2 and out["stale"] == 2
    sig = {s["symbol"]: s for s in _signals(db)}
    assert {str(s["date"]) for s in sig.values()} == {str(FIRST)}
    assert sig["DOWN"]["scan"] == "pead_bear" and sig["DOWN"]["stop"] > sig["DOWN"]["entry"] > sig["DOWN"]["target"]
    assert "found 3 sessions after its first session: recorded, not alerted" in sig["ACME"]["reason"]


def test_only_the_newest_quarter_of_a_filing_day_fires(db):
    _seed(db, "ACME", broadcast={11: f"{FILED} 18:05:00"})           # FY25Q4 and FY26Q1 broadcast together
    _bars(db, "ACME")
    _market(db)
    ES.refresh(db)
    q = _rows(db, "ACME")
    assert q["FY25Q4"]["sue"] > ES.SUE_TRIGGER and q["FY26Q1"]["sue"] == pytest.approx(SUE_HAND, abs=1e-3)
    out = ES.scan(db, FIRST)
    assert out["signals"] == 1 and out["superseded"] == 1
    assert "FY26Q1" in _signals(db)[0]["reason"]


def test_backfill_records_old_filings_on_their_own_session(db):
    _seed(db, "ACME")
    _bars(db, "ACME")                                    # bars from 2025-01-01
    _market(db)
    ES.refresh(db)
    out = ES.scan(db, dt.date(2025, 9, 30), catchup_sessions=None)
    # FY25Q4 on 2025-05-12 (the Monday after its filing) and FY26Q1 on 2025-08-11; FY25Q2 (SUE +1.3) never;
    # nothing before the stored prices begin
    assert out["signals"] == 2 and [str(s["date"]) for s in _signals(db)] == ["2025-05-12", str(FIRST)]
    db.execute("DELETE FROM technical_signal")
    db.execute("DELETE FROM prices_daily WHERE symbol='ACME' AND date<'2025-05-10'")   # 66 bars before 08-11
    db.commit()
    out = ES.scan(db, dt.date(2025, 9, 30), catchup_sessions=None)
    assert out["signals"] == 1 and out["no_prices"] == 1, "no bar before FY25Q4's filing: no true first session"


def test_pead_alerts_are_held_until_30_closed_signals_with_positive_expectancy(db, monkeypatch):
    from research import tech_signals as TS
    import alerts.telegram as TG
    sent = []
    monkeypatch.setattr(TG, "notify", lambda text, **k: sent.append(text))
    _seed(db, "ACME")
    _bars(db, "ACME")
    _market(db)
    ES.run(db, as_of=FIRST)
    assert ES.alert(db, FIRST)["alerted"] == 0 and not sent
    st = {o["scan"]: o for o in TS.scan_stats(db)}
    assert st["pead_bull"]["alerts"] == "held" and st["pead_bull"]["event"] and not st["pead_bull"]["pattern"]
    for i in range(30):                                   # 15 targets at +2 R, 15 stops at -1 R: +0.5 R
        db.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, "
                   "confluence, status, r_multiple, market_gate, alignment) VALUES (?, ?, '2024-01-15', 'pead_bull', "
                   "'drift', 'BULL', 100, 96, 108, 3, ?, ?, 'OPEN', 'WITH')",
                   (f"old{i}", f"OLD{i}", "TARGET" if i % 2 else "STOPPED", 2.0 if i % 2 else -1.0))
    db.commit()
    assert "pead_bull" in TS.proven_scans(db) and "pead_bear" not in TS.proven_scans(db)
    out = ES.alert(db, FIRST)
    assert out["alerted"] == 1 and "ACME" in sent[-1] and "Post-earnings drift" in sent[-1]
    assert "not vs consensus" in sent[-1]
    assert ES.alert(db, dt.date(2025, 8, 14))["alerted"] == 0, "only the day's own signals alert"
    # the technical alert leaves it to this module (no double alert)
    db.execute("UPDATE technical_signal SET confluence=6 WHERE symbol='ACME'")
    db.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, "
               "confluence, status, market_gate, alignment) VALUES ('gc', 'ACME', ?, 'golden_cross', 'Golden cross', "
               "'BULL', 100, 96, 108, 5, 'OPEN', 'OPEN', 'WITH')", (str(FIRST),))
    db.commit()
    sent.clear()
    assert TS.alert_top(db, str(FIRST))["alerted"] == 1 and "Golden cross" in sent[-1]
    assert "drift" not in sent[-1].lower()
    db.execute("UPDATE technical_signal SET alignment='AGAINST' WHERE scan='pead_bull' AND symbol='ACME'")
    db.commit()
    assert ES.alert(db, FIRST)["alerted"] == 0, "never against the market gate"


# ── screener ───────────────────────────────────────────────────────────────

def test_screener_fields_and_the_positive_surprise_preset(db, monkeypatch):
    from research import screener as SC
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: {})
    _seed(db, "ACME")
    _seed(db, "DOWN", eps=EPS[:-1] + [9])
    for s in ("ACME", "DOWN"):
        _bars(db, s)
    ES.refresh(db)
    SC.clear_cache()
    rows = {r["symbol"]: r for r in SC.build_snapshot(db, as_of=dt.date(2025, 8, 20))}
    a = rows["ACME"]
    assert a["sue"] == pytest.approx(SUE_HAND, abs=1e-3) and a["sue_revenue"] == pytest.approx(SUE_REV_HAND, abs=1e-3)
    assert a["eps_trend"] == "ACCELERATING" and a["days_since_result"] == 12
    assert rows["DOWN"]["eps_trend"] == "DECELERATING"
    preset = next(p for p in SC.PRESETS if p["key"] == "earnings_surprise")
    assert preset["name"] == "Positive earnings surprise" and preset["group"] == "fundamental"
    res = SC.run_screen(db, preset["query"], preset["sort"], rows=list(rows.values()))
    assert [r["symbol"] for r in res["rows"]] == ["ACME"] and "sue" in res["columns"]
    assert [r["symbol"] for r in SC.run_screen(db, 'eps_trend = "decelerating"', rows=list(rows.values()))["rows"]] \
        == ["DOWN"]
    assert SC.field_key("SUE") == "sue" and SC.field_key("revisions_proxy") == "eps_trend"
    assert SC.field_key("revenue_surprise") == "sue_revenue" and SC.field_key("result_age") == "days_since_result"
    assert {"sue", "sue_revenue", "eps_trend", "days_since_result"} <= set(SC.catalog()["groups"]["Earnings"][i]["key"]
                                                                          for i in range(4))
    SC.clear_cache()
    early = {r["symbol"]: r for r in SC.build_snapshot(db, as_of=dt.date(2025, 8, 7))}["ACME"]
    assert early["days_since_result"] == 90 and early["sue"] != pytest.approx(SUE_HAND, abs=0.1), \
        "a day before the filing the screener sees the March quarter"


# ── the nightly job ────────────────────────────────────────────────────────

def _table(conn):
    cur = conn.execute("SELECT * FROM earnings_surprise ORDER BY symbol, period_end")
    cols = [c[0] for c in cur.description]
    return [{k: v for k, v in zip(cols, r) if k != "computed_at"} for r in cur.fetchall()]


def test_nightly_run_is_idempotent(db):
    _seed(db, "ACME")
    _bars(db, "ACME")
    _market(db)
    one = ES.run(db, as_of=FIRST)
    assert one["status"] == "SUCCESS" and one["rows"] == 13 and one["scan"]["signals"] == 1
    t1, s1 = _table(db), _signals(db)
    two = ES.run(db, as_of=FIRST)
    assert two["rows"] == 13 and two["scan"]["signals"] == 0 and two["scan"]["existing"] == 1
    assert _table(db) == t1 and _signals(db) == s1
    db.execute("DELETE FROM fundamental_data WHERE symbol='ACME' AND quarter='FY23Q1'")
    db.commit()
    assert ES.run(db, as_of=FIRST)["rows"] == 12 and len(_table(db)) == 12, "a quarter no longer stored is dropped"


def test_nightly_job_without_fundamentals_is_skipped(db):
    assert ES.run(db)["status"] == "SKIPPED"


def test_scheduled_at_2035_and_logged_via_run_job(db, monkeypatch):
    import pipeline.scheduler as S

    class _Job:
        def __init__(self, jobs):
            self.jobs, self.t = jobs, None

        def __getattr__(self, name):
            return self

        def at(self, t):
            self.t = t
            return self

        def do(self, fn, *a, **k):
            self.jobs.append((self.t, fn))
            return self

    class _Schedule:
        def __init__(self):
            self.jobs = []

        def every(self, *a):
            return _Job(self.jobs)

    fake = _Schedule()
    monkeypatch.setattr(S, "schedule", fake, raising=False)
    S._schedule_w39_jobs()
    times = [t for t, fn in fake.jobs if fn is S._w39_earnings_surprise]
    assert times == ["20:35"]
    _seed(db, "ACME")
    _bars(db, "ACME")
    monkeypatch.setattr(S, "is_market_day", lambda *a, **k: True)
    S._w39_earnings_surprise()
    r = db.execute("SELECT status, rows_processed FROM pipeline_log WHERE job_name='earnings_surprise' AND kind='run'"
                   ).fetchall()
    assert [(x[0], x[1]) for x in r] == [("SUCCESS", 13)]


# ── API, page, permissions ─────────────────────────────────────────────────

def test_api_and_research_page(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    _seed(db, "ACME")
    r = client.get("/api/research/earnings-surprise/ACME")
    assert r.status_code == 200
    j = r.json()
    assert j["latest"]["quarter"] == "FY26Q1" and j["latest"]["sue"] == pytest.approx(SUE_HAND, abs=1e-3)
    assert j["latest"]["eps_trend"] == "ACCELERATING" and len(j["quarters"]) == 12 and "consensus" in j["method"]
    early = client.get("/api/research/earnings-surprise/ACME", params={"as_of": "2025-08-07"}).json()
    assert early["latest"]["quarter"] == "FY25Q4" and early["latest"]["days_since_result"] == 90
    assert client.get("/api/research/earnings-surprise/NOPE").status_code == 404
    assert client.get("/api/research/earnings-surprise/bad sym").status_code == 400
    assert client.get("/api/research/earnings-surprise/ACME", params={"as_of": "soon"}).status_code == 400
    page = client.get("/research").text
    assert "Earnings surprise" in page and "/api/research/earnings-surprise/" in page


def test_permissions_and_classification():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/research/earnings-surprise/ACME") == "research:read"
    assert classify("earnings_surprise") is not None and TABLES["earnings_surprise"] == "GLOBAL"
