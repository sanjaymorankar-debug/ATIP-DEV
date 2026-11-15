"""The Kite high-volume breakout and retest scanner (strategy/breakout_retest.py) and its Algo Signals tab
(dashboard/algo_routes.py), KGF-10, owner prompt 15 Nov 2026 item 10.

Golden tests on synthetic candles, one per accept / reject rule: >= 25 candles, >= 20 candles before the breakout,
the volume rule, the prior-high rule, the green candle, the later high, the +-1 % midpoint, age <= 20, one result per
stock (the most recent accepted), the sort order. Then normalisation, the tick-rounded bid, the stored run and the API
/ page on a seeded database, and the 13 Nov 2026 reference setups on the 15 Nov backup (skipped when it is absent)."""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from strategy import breakout_retest as B

KITE_FIELDS = {"symbol", "companyName", "currentPrice", "breakoutDate", "bIndex", "daysSince", "breakoutLow",
               "breakoutHigh", "midpoint", "distanceFromMidpoint", "volMultiple", "prevHigh", "maxPriceAfter",
               "returnFromCloseToSubHigh", "retracementHigh", "candles"}


def _days(n, start=date(2026, 6, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _dated(rows, start=date(2026, 6, 1)):
    for d, r in zip(_days(len(rows), start), rows):
        r["date"] = d
    return rows


def _c(o, h, l, c, v):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


def setup(base=22, age=2, bvol=4000, bopen=101.0, bclose=105.0, bhigh=106.0, blow=100.0, after_high=107.0,
          last_close=103.2, start=date(2026, 6, 1)):
    """`base` flat candles (high 101, volume 1,000), the breakout candle, then `age` candles: the first reaches
    after_high, every one closes at last_close. Defaults: a valid setup, midpoint 103, distance +0.19 %, age 2."""
    rows = [_c(100.0, 101.0, 99.0, 100.5, 1000) for _ in range(base)]
    rows.append(_c(bopen, bhigh, blow, bclose, bvol))
    lo = min(102.5, last_close) - 0.3
    for k in range(age):
        rows.append(_c(104.0, after_high if k == 0 else min(104.5, after_high), lo, last_close, 1000))
    return _dated(rows, start)


def scan(**k):
    return B.scan_symbol("ACME", setup(**k))


# -- the rule, one test per accept / reject rule ---------------------------------------------------------------------

def test_accepts_the_canonical_setup_with_every_kite_field():
    rows = setup()
    r = B.scan_symbol("ACME", rows, company_name="Acme Ltd", current_price=None)
    assert KITE_FIELDS <= set(r)
    assert r["symbol"] == "ACME" and r["companyName"] == "Acme Ltd"
    assert r["breakoutDate"] == rows[22]["date"] and r["daysSince"] == 2
    assert r["candles"][r["bIndex"]]["date"] == r["breakoutDate"] and r["bIndex"] == 20      # the 20 before it, then it
    assert len(r["candles"]) == 23 and r["candles"][-1]["date"] == rows[-1]["date"]
    assert (r["breakoutLow"], r["breakoutHigh"], r["prevHigh"], r["maxPriceAfter"]) == (100.0, 106.0, 101.0, 107.0)
    assert r["midpoint"] == 103.0 and r["volMultiple"] == 4.0 and r["currentPrice"] == 103.2
    assert r["distanceFromMidpoint"] == pytest.approx((103.2 - 103) / 103 * 100, abs=1e-4)
    assert r["returnFromCloseToSubHigh"] == pytest.approx((107 / 105 - 1) * 100, abs=1e-4)
    assert r["retracementHigh"] == pytest.approx((107 - 103.2) / 107 * 100, abs=1e-4)


def test_current_price_given_replaces_the_last_close():
    r = B.scan_symbol("ACME", setup(), current_price="103.9")
    assert r["currentPrice"] == 103.9 and r["distanceFromMidpoint"] == pytest.approx(0.8738, abs=1e-4)
    assert B.scan_symbol("ACME", setup(), current_price=104.2) is None                       # +1.17 %: out


def test_needs_at_least_25_candles():
    assert len(setup(base=21)) == 24 and scan(base=21) is None
    assert len(setup(base=22)) == 25 and scan(base=22) is not None


def test_breakout_needs_20_candles_before_it():
    assert scan(base=19, age=6) is None                                     # 26 candles, but only 19 before it
    assert scan(base=20, age=6)["daysSince"] == 6


def test_volume_rule_three_times_the_20_candle_average_excluding_the_candle():
    assert scan(bvol=2999) is None
    r = scan(bvol=3000)
    assert r is not None and r["volMultiple"] == 3.0


def test_close_must_be_above_the_previous_20_candle_high():
    assert scan(bopen=100.5, bclose=101.0) is None                          # equal to the prior high: not above it
    assert scan(bopen=100.5, bclose=101.01) is not None


def test_breakout_candle_must_be_green():
    assert scan(bopen=105.5, bclose=105.0) is None                          # red
    assert scan(bopen=105.0, bclose=105.0) is None                          # doji: close not above open
    assert scan(bopen=104.9, bclose=105.0) is not None


def test_a_later_high_must_exceed_the_breakout_high():
    assert scan(after_high=106.0) is None
    assert scan(after_high=106.01)["maxPriceAfter"] == 106.01


def test_latest_close_within_one_percent_of_the_midpoint():
    assert scan(last_close=104.04) is None                                  # +1.0097 %
    assert scan(last_close=104.02) is not None                              # +0.9903 %
    assert scan(last_close=101.95) is None                                  # -1.0194 %
    assert scan(last_close=101.98)["distanceFromMidpoint"] == pytest.approx(-0.9903, abs=1e-4)


def test_age_at_most_20_candles():
    assert scan(age=20)["daysSince"] == 20
    assert scan(age=21) is None


def _two_breakouts(last=103.1, b_low=98.6):
    """A at index 22 (midpoint 103), B at index 25 (closes above A's later high); the latest close is `last`."""
    rows = [_c(100.0, 101.0, 99.0, 100.5, 1000) for _ in range(22)]
    rows += [_c(101.0, 106.0, 100.0, 105.5, 4000),                          # A
             _c(105.0, 106.5, 102.5, 103.2, 1000), _c(103.0, 104.0, 102.5, 103.1, 1000),
             _c(104.0, 107.5, b_low, 107.0, 5000),                          # B: above 106.5 on 4.3x volume
             _c(106.0, 108.0, 102.5, 103.5, 1000), _c(103.5, 104.0, 102.2, last, 1000)]
    return _dated(rows)


def test_one_result_per_stock_the_most_recent_accepted():
    rows = _two_breakouts()
    r = B.scan_symbol("ACME", rows)                                         # both pass: B, the more recent
    assert r["breakoutDate"] == rows[25]["date"] and r["daysSince"] == 2
    rows = _two_breakouts(last=102.6, b_low=99.9)                           # B's midpoint 103.7 is now -1.06 % away
    r = B.scan_symbol("ACME", rows)
    assert r["breakoutDate"] == rows[22]["date"] and r["daysSince"] == 5 and r["maxPriceAfter"] == 108.0


def test_sort_by_age_then_absolute_distance():
    rows = [{"symbol": "C", "daysSince": 3, "distanceFromMidpoint": 0.1},
            {"symbol": "A", "daysSince": 1, "distanceFromMidpoint": -0.9},
            {"symbol": "B", "daysSince": 1, "distanceFromMidpoint": 0.2},
            {"symbol": "D", "daysSince": 3, "distanceFromMidpoint": -0.05},
            {"symbol": "E", "daysSince": 3, "distanceFromMidpoint": 0.05}]
    out = B.rank_rows(rows)
    assert [(r["rank"], r["symbol"]) for r in out] == [(1, "B"), (2, "A"), (3, "D"), (4, "E"), (5, "C")]


# -- normalisation --------------------------------------------------------------------------------------------------

def test_normalise_string_numbers_and_unsorted_dates():
    raw = [{"date": "2026-11-13", "open": "1,234.50", "high": "1,240", "low": " 1,230.0 ", "close": "₹1,238.25",
            "volume": "12,345"},
           {"date": "2026-11-11", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
           {"date": "2026-11-12", "open": "1", "high": "2", "low": "0.5", "close": "1.2", "volume": None},
           {"date": "2026-11-11", "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9}]          # repeat: the last wins
    out = B.normalise(raw)
    assert [c["date"] for c in out] == ["2026-11-11", "2026-11-12", "2026-11-13"]
    assert out[0]["close"] == 9 and out[1]["volume"] == 0
    assert out[2] == {"date": "2026-11-13", "open": 1234.5, "high": 1240.0, "low": 1230.0, "close": 1238.25,
                      "volume": 12345}


@pytest.mark.parametrize("raw", ["2026-11-13", "2026/11/13", "20261113", "13-11-2026", "13/11/2026", "13-Nov-2026",
                                 "13 Nov 2026", "2026-11-13T00:00:00+0530", "2026-11-13T00:00:00+05:30",
                                 "2026-11-12T18:30:00Z", "2026-11-13 00:00:00", date(2026, 11, 13),
                                 datetime(2026, 11, 13, 9, 15), datetime(2026, 11, 13, 0, 0, tzinfo=B.IST),
                                 int(datetime(2026, 11, 13, tzinfo=B.IST).timestamp()),
                                 int(datetime(2026, 11, 13, tzinfo=B.IST).timestamp()) * 1000])
def test_normalise_date_formats(raw):
    assert B.normalise([{"date": raw, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])[0]["date"] == "2026-11-13"


def test_normalise_kite_lists_rows_and_drops_bad_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT '2026-11-10' AS date, 5 AS open, 6 AS high, 4 AS low, 5.5 AS close, 7 AS volume").fetchone()
    out = B.normalise([["2026-11-13T00:00:00+0530", "10", 11, 9, 10.5, 100], row,
                       {"Date": "2026-11-12", "O": 1, "H": 2, "L": 1, "C": 2, "V": 3},             # any key case, o/h/l/c/v
                       {"date": "2026-11-14", "open": 1, "high": 2, "low": 1, "close": None},      # no close
                       {"date": "2026-11-15", "open": "abc", "high": 2, "low": 1, "close": 2},
                       {"date": "2026-11-16", "open": 0, "high": 2, "low": 1, "close": 2},         # zero price
                       {"date": "someday", "open": 1, "high": 2, "low": 1, "close": 2}, None, "x"])
    assert [(c["date"], c["close"]) for c in out] == [("2026-11-10", 5.5), ("2026-11-12", 2.0), ("2026-11-13", 10.5)]


def test_scan_of_messy_input_equals_scan_of_clean_input():
    clean = setup()
    messy = [{"timestamp": datetime.fromisoformat(r["date"]).strftime("%d-%b-%Y"), "open": str(r["open"]),
              "high": f'{r["high"]:,}', "low": str(r["low"]), "close": str(r["close"]), "volume": str(r["volume"])}
             for r in reversed(clean)]
    assert B.scan_symbol("ACME", messy) == B.scan_symbol("ACME", clean)


# -- the bid: breakout low and 5 % stop, tick-rounded down ------------------------------------------------------------

@pytest.mark.parametrize("low,buy,stop,tick", [(669.0, 669.0, 635.55, 0.05), (1408.13, 1408.1, 1337.6, 0.1),
                                                (5136.0, 5136.0, 4879.0, 0.5), (160.537, 160.53, 152.5, 0.01),
                                                (48900.0, 48900.0, 46455.0, 5.0)])
def test_entry_prices_round_down_on_one_tick(low, buy, stop, tick):
    assert B.entry_prices("ACME", low) == {"buy_price": buy, "stop_price": stop, "tick_size": tick}


def test_index_flags_from_lists_and_preopen(tmp_path):
    (tmp_path / "ind_nifty500list.csv").write_text("Company Name,Industry,Symbol,Series,ISIN Code\n"
                                                   "Acme Ltd.,Capital Goods,ACME,EQ,X1\nDummy,Metals,DUMMYX,EQ,X2\n")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE preopen_snapshot (date, symbol, in_nifty50)")
    conn.executemany("INSERT INTO preopen_snapshot VALUES (?,?,?)", [("2026-11-12", "ACME", 1), ("2026-11-13", "BETA", 0)])
    sets, names = B.index_flags(conn, tmp_path)
    assert sets["nifty500"] == {"ACME"} and names == {"ACME": "Acme Ltd."}
    assert sets["nifty50"] == {"ACME"} and sets["next50"] is None and sets["midcap150"] is None
    assert B._flags_for("BETA", sets) == {"nifty50": False, "next50": None, "midcap150": None, "smallcap250": None,
                                          "nifty500": False}


# -- the stored run, the API and the page -----------------------------------------------------------------------------

END = date(2026, 11, 13)


def _seed(conn, specs, scored=None, day=END):
    """specs: {symbol: candles (dated so the last is `day`)}. Every symbol in `scored` (default: all) is scored on `day`."""
    for sym, rows in specs.items():
        dates = []
        d = day
        while len(dates) < len(rows):
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d -= timedelta(days=1)
        for dt, r in zip(reversed(dates), rows):
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,series,source) "
                         "VALUES (?,?,?,?,?,?,?,'EQ','bhavcopy')", (sym, dt, r["open"], r["high"], r["low"], r["close"],
                                                                    r["volume"]))
    for sym in (specs if scored is None else scored):
        conn.execute("INSERT INTO ai_scores (symbol, date, atip_score) VALUES (?,?,50)", (sym, day.isoformat()))
    conn.commit()


@pytest.fixture
def db():
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), {security.TOKEN_HEADER: security.token()}


def _universe(conn):
    _seed(conn, {"AAA": setup(age=3, last_close=103.5), "BBB": setup(base=23, age=1, last_close=103.9),
                 "CCC": setup(base=23, age=1, last_close=102.9), "FLAT": setup(bvol=1000),
                 "UNSCORED": setup(base=23, age=1)},
          scored=["AAA", "BBB", "CCC", "FLAT"])


def test_scan_universe_ranks_the_scored_eq_universe_and_run_scan_stores_it(db):
    _universe(db)
    res = B.scan_universe(db, None)
    assert res["as_of"] == "2026-11-13" and res["universe"] == 4 and res["scanned"] == 4
    assert [(r["rank"], r["symbol"], r["daysSince"]) for r in res["rows"]] == [(1, "CCC", 1), (2, "BBB", 1), (3, "AAA", 3)]
    r = res["rows"][0]
    assert (r["buy_price"], r["stop_price"], r["tick_size"]) == (100.0, 95.0, 0.01)
    assert B.latest_scan(db) is None
    run = B.run_scan(db, None)
    got = B.latest_scan(db)
    assert got["run_id"] == run["run_id"] and [x["symbol"] for x in got["rows"]] == ["CCC", "BBB", "AAA"]
    assert got["rows"][0]["candles"] == res["rows"][0]["candles"] and got["scanned"] == 4
    stored = db.execute("SELECT symbol, buy_price, stop_price, tick_size FROM algo_scan_results ORDER BY `rank`").fetchall()
    assert [tuple(x) for x in stored][0] == ("CCC", 100.0, 95.0, 0.01)


def test_scan_universe_as_of_and_a_stock_without_a_candle_that_day(db):
    _universe(db)
    db.execute("DELETE FROM prices_daily WHERE symbol='AAA' AND date='2026-11-13'")
    db.commit()
    res = B.scan_universe(db, "2026-11-13")
    assert res["scanned"] == 3 and res["skipped"] == {"no candle on 2026-11-13": 1}
    assert [r["symbol"] for r in res["rows"]] == ["CCC", "BBB"]
    older = B.scan_universe(db, "2026-11-12")                               # one candle earlier: BBB / CCC have age 0
    assert older["as_of"] == "2026-11-12" and older["scored_date"] is None and older["rows"] == []


def test_live_prices_websocket_then_snapshot_then_close(db):
    _universe(db)
    now = datetime.now(B.IST).replace(tzinfo=None)
    ts = lambda s: (now - timedelta(seconds=s)).strftime("%Y-%m-%d %H:%M:%S")       # noqa: E731
    db.executemany("INSERT INTO live_quotes (symbol, ltp, timestamp, source) VALUES (?,?,?,?)",
                   [("AAA", 104.0, ts(30), "dhan_ws"), ("AAA", 103.0, ts(20), "dhan"),       # ws within a minute: ws
                    ("BBB", 104.5, ts(900), "dhan_ws"), ("BBB", 104.1, ts(10), "dhan_rest"),  # ws stale: the snapshot
                    ("CCC", 99.0, (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "dhan_ws")])  # yesterday
    db.commit()
    px = B.live_prices(db, ["AAA", "BBB", "CCC"], now=now)
    assert (px["AAA"]["ltp"], px["AAA"]["source"]) == (104.0, "ws")
    assert (px["BBB"]["ltp"], px["BBB"]["source"]) == (104.1, "snapshot")
    assert px["CCC"] == {"ltp": 102.9, "source": "close", "at": "2026-11-13"}


def test_api_returns_the_ranked_setups_and_keeps_the_ranking_until_a_refresh(client, db):
    c, h = client
    _universe(db)
    d = c.get("/api/scanner-data").json()
    assert d["count"] == 3 and d["scanned"] == 4 and d["universe"] == 4 and d["as_of"] == "2026-11-13"
    assert [r["symbol"] for r in d["rows"]] == ["CCC", "BBB", "AAA"] and d["stored"] is True
    assert KITE_FIELDS | {"buy_price", "stop_price", "tick_size", "rank", "ltp"} <= set(d["rows"][0])
    assert d["rows"][0]["ltp"]["source"] == "close" and d["feed"]["text"] and d["outdated"] is False
    # a new candle does not re-rank: the stored run is served until the scanner runs again
    db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,series) "
               "VALUES ('CCC','2026-11-16',100,100,90,91,1000,'EQ')")
    db.commit()
    again = c.get("/api/scanner-data").json()
    assert again["run_id"] == d["run_id"] and [r["symbol"] for r in again["rows"]] == ["CCC", "BBB", "AAA"]
    assert again["outdated"] is True and again["latest_candle"] == "2026-11-16"
    assert c.get("/api/scanner-data?refresh=1").status_code == 401                    # storing a ranking needs the token
    assert c.get("/api/scanner-data?refresh=1&as_of=13-11-2026", headers=h).status_code == 400
    new = c.get("/api/scanner-data?refresh=1&as_of=2026-11-13", headers=h).json()
    assert new["run_id"] != d["run_id"] and [r["symbol"] for r in new["rows"]] == ["CCC", "BBB", "AAA"]
    p = c.get("/api/scanner-data?prices=1").json()
    assert set(p["prices"]) == {"AAA", "BBB", "CCC"} and p["price_sources"]["close"] == 3


def test_api_empty_state_and_no_scores(client, db):
    c, _ = client
    d = c.get("/api/scanner-data").json()                                   # nothing scored yet
    assert d["count"] == 0 and d["rows"] == [] and d["scanned"] == 0
    _seed(db, {"FLAT": setup(bvol=1000), "RED": setup(bopen=105.5)})
    d = c.get("/api/scanner-data", headers=client[1]).json()                # the empty run above stays until a refresh
    assert d["count"] == 0
    d = c.get("/api/scanner-data?refresh=1", headers=client[1]).json()
    assert d["count"] == 0 and d["scanned"] == 2 and d["rows"] == []


def test_viewer_reads_but_never_stores(client, db, monkeypatch):
    c, h = client
    _universe(db)
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    assert c.get("/api/scanner-data?refresh=1", headers=h).status_code == 403
    d = c.get("/api/scanner-data").json()
    assert d["count"] == 3 and d["stored"] is False and B.latest_scan(db) is None
    page = c.get("/algo-signals")
    assert page.status_code == 200 and 'id="algo-run"' not in page.text


def test_page_renders_with_title_states_and_nav(client):
    c, _ = client
    r = c.get("/algo-signals")
    assert r.status_code == 200
    t = r.text
    assert "<title>High-volume breakout and retest setups · ATIP</title>" in t
    assert "<h1>High-volume breakout and retest setups</h1>" in t and "Triggered Breakouts" in t
    assert "Loading scanner cache..." in t and "No breakout retest setups match the current parameters." in t
    for col in ("Rank", "Symbol", "Current Price", "Breakout Date", "Age", "Breakout Vol", "Retest Distance",
                "Peak Gains", "Retracement"):
        assert f">{col}</th>" in t
    assert 'id="algo-error"' in t and t.index('id="algo-error"') < t.index('id="algo-table"')   # the error above it
    assert "BroadcastChannel" in t and "localStorage" in t and 'id="algo-run"' in t
    assert 'href="/algo-signals"' in t and "Algo Signals" in t                  # the rail entry
    from dashboard.shell import NAV
    assert ("Algo Signals", "/algo-signals") in [(l, h) for *_, kids in NAV for l, h, _ in kids]


def test_page_styles_use_tokens_only():
    import re
    from dashboard.algo_routes import _CSS
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", _CSS)


def test_scanner_never_touches_an_order():
    for f in ("strategy/breakout_retest.py", "dashboard/algo_routes.py"):
        src = (Path(__file__).resolve().parents[1] / f).read_text()
        for word in ("place_order", "modify_order", "cancel_order", "place_gtt", "get_execution_client", "dhanhq"):
            assert word not in src, f"{f} mentions {word}"


# -- the 13 Nov 2026 reference on the 15 Nov backup --------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
BACKUP = next((p for p in (_ROOT / "atip_data/backups/atip-scheduled-20261115-191511.db",          # the project folder
                           _ROOT.parents[2] / "atip_data/backups/atip-scheduled-20261115-191511.db")  # a .claude worktree
               if p.exists()), _ROOT / "atip_data/backups/atip-scheduled-20261115-191511.db")

REFERENCE_13_NOV = [("KALYANKJIL", 669.00, 635.55), ("CDSL", 1408.10, 1337.60), ("GLAXO", 2547.80, 2420.40),
                    ("TITAN", 4694.40, 4459.60), ("MOTHERSON", 160.50, 152.47), ("ACC", 1210.10, 1149.50),
                    ("NBCC", 77.50, 73.62), ("JKCEMENT", 5136.00, 4879.00), ("LALPATHLAB", 1901.00, 1805.90),
                    ("BOSCHLTD", 48900.00, 46455.00), ("MSUMI", 37.61, 35.72)]


@pytest.mark.skipif(not BACKUP.exists(), reason="the 15 Nov 2026 backup is not on this machine")
def test_reference_setups_on_13_nov_2026():
    conn = sqlite3.connect(f"file:{BACKUP}?mode=ro&immutable=1", uri=True)
    try:
        res = B.scan_universe(conn, "2026-11-13", raw_dir=BACKUP.parents[1] / "raw")
    finally:
        conn.close()
    assert res["as_of"] == "2026-11-13" and res["universe"] == 499
    assert [(r["symbol"], r["buy_price"], r["stop_price"]) for r in res["rows"]] == REFERENCE_13_NOV
    by = {r["symbol"]: r for r in res["rows"]}
    # The study (.ux_build/kite-gap-2026-11-15/scripts/kite_now.py) dropped weekend rows, so it never saw the Sunday
    # 8 Nov 2026 Muhurat session (499 EQ candles, source 'dhan'). The spec keeps every candle: a breakout before 8 Nov
    # is one candle older here (TITAN 5, study 4) and KALYANKJIL / CDSL / GLAXO's 20-candle average holds that short
    # session's low volume (GLAXO 14.05x, study 13.68x). Same 11 stocks, same order, same bids.
    assert by["TITAN"]["daysSince"] == 5 and by["KALYANKJIL"]["daysSince"] == 1
    assert by["GLAXO"]["volMultiple"] == pytest.approx(14.05, abs=0.01)
