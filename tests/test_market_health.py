"""
Market breadth (DP-17), Market Health (SC-09) and ACS (SC-08).

Each of these inputs used to be a constant dressed as a measurement: breadth
scored 25.93 every session, missing index changes scored as flat days, missing
flows as zero, VIX as 15, ACS's news confidence as 50 and its accuracy as 50 --
and the accuracy query read outcomes the scored day could not have known.
"""

import datetime as dt


def _bars(conn, sym, closes, start=dt.date(2025, 1, 1)):
    """One bar per weekday from start, high = low = close; returns the dates."""
    days, d = [], start
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    for day, c in zip(days, closes):
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                     "VALUES (?,?,?,?,?,?,1000,'bhavcopy')", (sym, str(day), c, c, c, c))
    return days


# ── DP-17: market breadth ────────────────────────────────────────────────────

def test_breadth_counts_advances_declines_the_200dma_and_52_week_extremes(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import breadth_series
    init_db()
    conn = get_connection()
    try:
        n = 260
        days = _bars(conn, "UP", [100 + i for i in range(n)])     # above its 200-DMA, a new high
        _bars(conn, "DOWN", [400 - i for i in range(n)])          # below, a new low
        _bars(conn, "FLAT", [50.0] * (n - 1) + [49.0])            # falls on the last day only
        last = str(days[-1])
        conn.execute("INSERT INTO prices_daily (symbol,date,close,source) VALUES ('NIFTY50',?,1,'nse_index')",
                     (last,))                                      # outside the universe: ignored
        b = breadth_series(conn, last, last, universe=["UP", "DOWN", "FLAT"])[last]
        assert (b["advances"], b["declines"], b["universe"]) == (1, 2, 3)
        assert (b["pct_advancing"], b["ad_ratio"]) == (33.33, 0.5)
        assert b["pct_above_200dma"] == 33.33                     # UP only: FLAT's 49 is under its average
        assert (b["new_highs"], b["new_lows"]) == (1, 2)          # FLAT's 49 is a 52-week low too
    finally:
        conn.close()


def test_breadth_is_withheld_when_too_few_stocks_have_bars(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import breadth_series
    init_db()
    conn = get_connection()
    try:
        days = _bars(conn, "A", [10, 11])
        _bars(conn, "B", [10, 9])
        last = str(days[-1])
        b = breadth_series(conn, last, last, universe=["A", "B", "C", "D", "E"])[last]
        assert "advances" not in b and b["universe"] == 2, "2 of 5 measured is not market breadth"
        b2 = breadth_series(conn, last, last, universe=["A", "B"])[last]
        assert b2["advances"] == 1 and b2["pct_above_200dma"] is None, "no 200 sessions of history"
    finally:
        conn.close()


# ── SC-09: Market Health ─────────────────────────────────────────────────────

MH_W = {"NiftyTrend": 0.20, "BankNifty": 0.15, "Breadth": 0.10, "VIX": 0.10, "FII": 0.10,
        "DII": 0.10, "Global": 0.10, "Sector": 0.10, "AdvanceDecline": 0.05}


def test_market_health_reads_breadth_not_the_25_93_constant(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import compute_mh
    init_db()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO index_levels (date,time,nifty50_chg,banknifty_chg,midcap150_chg,"
                     "smallcap250_chg,india_vix) VALUES ('2026-09-23','15:30:00',0,0,0,0,21.5)")
        br = {"advances": 300, "declines": 200, "universe": 500, "pct_advancing": 60.0,
              "ad_ratio": 1.5, "pct_above_200dma": 70.0, "new_highs": 12, "new_lows": 3}
        mh = compute_mh("2026-09-23", conn, MH_W, breadth=br)
        row = dict(conn.execute("SELECT * FROM market_health WHERE date='2026-09-23'").fetchone())
        assert (row["breadth"], row["adv_decline"], row["pct_advancing"]) == (70.0, 1.5, 60.0)
        assert (row["advances"], row["declines"], row["new_highs"], row["new_lows"]) == (300, 200, 12, 3)
        # no flows and no global snapshot: left out, not scored as zero flows / 50
        assert row["fii_score"] is None and row["global_score"] is None
        assert row["mh_inputs"] == "AdvanceDecline,BankNifty,Breadth,NiftyTrend,Sector,VIX"
        assert row["mh_coverage"] == 0.7
        vix = round(100 - (21.5 - 8) / 27 * 100, 2)
        expected = (0.20 * 50 + 0.15 * 50 + 0.10 * 70 + 0.10 * vix + 0.10 * 50 + 0.05 * 60) / 0.70
        assert mh["mh_score"] == round(expected, 2)
    finally:
        conn.close()


def test_index_change_falls_back_to_the_daily_series_but_never_across_a_gap(temp_db):
    """A session index_levels never covered (all of 2025) is read from the
    daily index series."""
    from db.schema import init_db, get_connection
    from scores.engine import get_index_change
    init_db()
    conn = get_connection()
    try:
        for d, c in (("2025-03-03", 100.0), ("2025-03-04", 102.0), ("2025-03-06", 101.0)):
            conn.execute("INSERT INTO prices_daily (symbol,date,close,source) "
                         "VALUES ('NIFTYBANK',?,?,'dhan_index')", (d, c))
        assert get_index_change("2025-03-04", conn, "banknifty") == 2.0
        assert get_index_change("2025-03-06", conn, "banknifty") is None, "03-05 missing: not a 1-day change"
        assert get_index_change("2025-03-07", conn, "banknifty") is None
    finally:
        conn.close()


def test_flows_and_global_must_belong_to_the_session(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import get_session_fii, get_recent_global
    init_db()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fii_dii_market (date,fii_net_cr) VALUES ('2026-09-18',-500)")
        conn.execute("INSERT INTO global_markets (date,global_score) VALUES ('2026-09-16',61)")
        assert get_session_fii("2026-09-21", conn)["fii_net_cr"] == -500, "Friday's flows at Monday 16:45"
        assert get_session_fii("2026-09-22", conn) == {}, "two sessions old is not this session's"
        assert get_recent_global("2026-09-18", conn)["global_score"] == 61
        assert get_recent_global("2026-09-21", conn) == {}
    finally:
        conn.close()


def test_market_health_without_enough_inputs_stores_no_score(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import compute_mh
    init_db()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO index_levels (date,time,india_vix) VALUES ('2026-09-23','15:30:00',12)")
        mh = compute_mh("2026-09-23", conn, MH_W, breadth={})
        assert (mh["mh_score"], mh["regime"], mh["coverage"]) == (None, None, 0.1)
    finally:
        conn.close()


def test_rescoring_market_health_keeps_portfolio_health(temp_db):
    """INSERT OR REPLACE deleted the row first, taking portfolio_health with it."""
    from db.schema import init_db, get_connection
    from scores.engine import compute_mh
    init_db()
    conn = get_connection()
    try:
        conn.execute("ALTER TABLE market_health ADD COLUMN portfolio_health REAL")
        conn.execute("INSERT INTO market_health (date,portfolio_health) VALUES ('2026-09-23',71.5)")
        conn.execute("INSERT INTO index_levels (date,time,nifty50_chg,banknifty_chg,india_vix) "
                     "VALUES ('2026-09-23','15:30:00',0.5,0.5,12)")
        compute_mh("2026-09-23", conn, MH_W, breadth={"pct_above_200dma": 55.0, "pct_advancing": 50.0})
        assert conn.execute("SELECT portfolio_health FROM market_health WHERE date='2026-09-23'"
                            ).fetchone()[0] == 71.5
    finally:
        conn.close()


def test_backfill_scores_past_sessions_and_leaves_live_rows(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    from scores import engine
    init_db()
    conn = get_connection()
    conn.execute("DELETE FROM weight_config WHERE index_name='MH'")
    conn.executemany("INSERT INTO weight_config (index_name,variable,weight) VALUES ('MH',?,?)",
                     list(MH_W.items()))
    for d, n, b, v in (("2025-03-03", 100, 50, 14), ("2025-03-04", 101, 51, 15), ("2025-03-05", 102, 50, 16)):
        for sym, c in (("NIFTY50", n), ("NIFTYBANK", b), ("INDIAVIX", v)):
            conn.execute("INSERT INTO prices_daily (symbol,date,close,source) VALUES (?,?,?,'dhan_index')",
                         (sym, d, c))
    conn.execute("INSERT INTO market_health (date,mh_score,regime) VALUES ('2025-03-05',55.0,'NEUTRAL')")
    conn.commit(); conn.close()
    monkeypatch.setattr(engine, "breadth_series", lambda conn, s, e, universe=None: {})
    monkeypatch.setattr(engine, "MH_MIN_COVERAGE", 0.40)

    res = engine.backfill_market_health()

    conn = get_connection()
    try:
        rows = {str(r[0]): tuple(r)[1:] for r in conn.execute(
            "SELECT date, mh_score, regime, backfilled, mh_inputs FROM market_health ORDER BY date")}
        assert rows["2025-03-05"] == (55.0, "NEUTRAL", 0, None), "a live row is not rewritten"
        assert rows["2025-03-03"][0] is None, "no previous session: no change, too little coverage"
        assert rows["2025-03-04"][2:] == (1, "BankNifty,NiftyTrend,VIX")
        assert res["rows"] == 1
    finally:
        conn.close()


# ── SC-08: ACS ───────────────────────────────────────────────────────────────

def test_acs_accuracy_uses_only_outcomes_known_on_the_day(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import get_historical_accuracy
    init_db()
    conn = get_connection()
    try:
        # 2026-09-23's close decides predictions made up to 09-16, 5 sessions earlier
        for d, ok in (("2026-06-01", 1), ("2026-09-07", 0), ("2026-09-08", 0), ("2026-09-09", 1),
                      ("2026-09-10", 1), ("2026-09-11", 1), ("2026-09-16", 0), ("2026-09-17", 1)):
            conn.execute("INSERT INTO accuracy_tracker (pred_date,symbol,correct_5d) VALUES (?,'ACME',?)",
                         (d, ok))
        assert get_historical_accuracy("ACME", "2026-09-23", conn) == 50.0, \
            "09-07..09-16 only: 06-01 is past 90 days, 09-17's outcome is not known yet"
        assert get_historical_accuracy("ACME", "2026-09-15", conn) is None, "4 known outcomes are too few"
        conn.execute("DELETE FROM accuracy_tracker WHERE correct_5d=1")
        conn.execute("INSERT INTO accuracy_tracker (pred_date,symbol,correct_5d) VALUES ('2026-09-14','ACME',0)")
        conn.execute("INSERT INTO accuracy_tracker (pred_date,symbol,correct_5d) VALUES ('2026-09-15','ACME',0)")
        assert get_historical_accuracy("ACME", "2026-09-23", conn) == 0.0, "never right is 0, not 50"
    finally:
        conn.close()


def test_acs_ignores_rule_based_news_confidence_and_later_news(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import get_news_confidence
    init_db()
    conn = get_connection()
    try:
        ins = ("INSERT INTO news_articles (fetched_at,headline,symbols_mentioned,confidence,classifier) "
               "VALUES (?,?,?,?,?)")
        conn.execute(ins, ("2026-09-22 10:00:00", "a", '["ACME"]', 0.5, "rule"))
        assert get_news_confidence("ACME", "2026-09-23", conn) is None, "the fallback's 0.5 is no reading"
        conn.execute(ins, ("2026-09-22 11:00:00", "b", '["ACME"]', 0.9, "claude"))
        conn.execute(ins, ("2026-09-24 09:00:00", "c", '["ACME"]', 0.1, "claude"))   # after the session
        assert get_news_confidence("ACME", "2026-09-23", conn) == 90.0
    finally:
        conn.close()


def test_acs_leaves_out_missing_inputs_instead_of_inventing_them(temp_db):
    from db.schema import init_db, get_connection
    from scores.engine import compute_acs
    init_db()
    conn = get_connection()
    try:
        w = {"HistoricalAccuracy": 0.25, "Agreement": 0.20, "MarketRegime": 0.20, "DataQuality": 0.15,
             "NewsConfidence": 0.10, "Liquidity": 0.10}
        acs = compute_acs("ACME", "2026-09-23", {"vpi": 70, "zpi": 40}, {"mh_score": None}, conn, w)
        # measured: Agreement 50 (1 of 2 above 60) and DataQuality 100
        assert acs == round((0.20 * 50 + 0.15 * 100) / 0.35, 2)
    finally:
        conn.close()


def test_existing_rule_based_articles_are_marked_on_migration(temp_db):
    import sqlite3
    from db import schema
    raw = sqlite3.connect(str(temp_db))
    raw.execute("CREATE TABLE news_articles (id INTEGER PRIMARY KEY, fetched_at TIMESTAMP, "
                "headline TEXT, category TEXT, confidence REAL)")
    raw.executemany("INSERT INTO news_articles (fetched_at,headline,category,confidence) VALUES (?,?,?,?)",
                    [("2026-09-01", "x", "GENERAL", 0.5), ("2026-09-01", "y", "EARNINGS", 0.8)])
    raw.commit(); raw.close()
    conn = schema.get_connection()
    try:
        assert [r[0] for r in conn.execute("SELECT classifier FROM news_articles ORDER BY id")] == ["rule", None]
    finally:
        conn.close()
