"""W40 (PERF-001-05): the estimated Nifty total-return index (data/total_return.py) and its use as a
wealth-performance benchmark ("nifty50tr"). Expected numbers are worked by hand in each test."""

from datetime import date, timedelta

import pytest

from data import total_return as TR

D0 = date(2026, 1, 26)             # a Monday; sessions are consecutive weekdays from here


def _days(n, start=D0):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def conn(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    yield c
    c.close()


def _px(conn, sym, days, closes):
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                     [(sym, str(d), c, c, c, c, 1000) for d, c in zip(days, closes)])


def _shares(conn, sym, shares, as_of=date(2025, 12, 31)):
    conn.execute("INSERT INTO fundamental_data (symbol, quarter, report_date, period_end, shares_out) "
                 "VALUES (?,?,?,?,?)", (sym, f"Q{as_of}", str(as_of), str(as_of), shares))


def _ca(conn, sym, ex, subject, kind=None, pf=None):
    conn.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, factor, status, price_factor) "
                 "VALUES (?,?,?,?,?,?,?)", (sym, str(ex), subject, kind, pf, "reconciled", pf))


def _market(conn, n=5, index=(100, 101, 102, 101, 103)):
    """NIFTY50 + three stocks. Caps on every day: AAA 10 x 50 = 500, BBB 5 x 60 = 300, CCC 2 x 100 = 200."""
    ds = _days(n)
    _px(conn, "NIFTY50", ds, index)
    for sym, close, sh in (("AAA", 50, 10), ("BBB", 60, 5), ("CCC", 100, 2)):
        _px(conn, sym, ds, [close] * n)
        _shares(conn, sym, sh)
    conn.commit()
    return ds


def test_dividend_points_and_the_tri_by_hand(conn):
    """Top 2 by cap: AAA (500) and BBB (300) -> weights 0.625 / 0.375; CCC (200) is not a member.
    Day 2: AAA goes ex Rs 1 (yield 1/50 = 2 %), CCC ex Rs 5 (ignored).
       yield = 0.625 x 0.02 = 0.0125; points = 101 x 0.0125 = 1.2625
       TRI2 = TRI1 x (102 + 1.2625) / 101 = 101 x 103.2625 / 101 = 103.2625
    Day 3: price 101 -> TRI3 = 103.2625 x 101 / 102; day 4: x 103 / 101."""
    ds = _market(conn)
    _ca(conn, "AAA", ds[2], "Interim Dividend - Rs 1 Per Share")
    _ca(conn, "CCC", ds[2], "Dividend - Rs 5 Per Share")
    conn.commit()
    rows = TR.build(conn, "NIFTY50", members=2)
    assert [r["price"] for r in rows] == [100, 101, 102, 101, 103]
    assert rows[1]["tri"] == pytest.approx(101) and rows[1]["div_points"] == 0
    assert rows[2]["div_yield_bp"] == pytest.approx(125) and rows[2]["div_points"] == pytest.approx(1.2625)
    assert rows[2]["tri"] == pytest.approx(103.2625)
    assert rows[3]["tri"] == pytest.approx(103.2625 * 101 / 102)
    assert rows[4]["tri"] == pytest.approx(103.2625 * 101 / 102 * 103 / 101)
    assert {r["members"] for r in rows} == {2}
    assert [r["covered"] for r in rows] == [0, 0, 1, 1, 1]      # the calendar starts at the first ex-date
    s = TR.summary(rows)
    assert s["price_return_pct"] == pytest.approx(3.0)
    assert s["total_return_pct"] == pytest.approx((rows[4]["tri"] / 100 - 1) * 100, abs=1e-3)
    assert s["dividend_gap_pct"] == pytest.approx(s["total_return_pct"] - 3.0, abs=1e-3)


def test_weights_follow_the_previous_close(conn):
    """AAA's close doubles to 100 on day 1 (cap 1,000 vs BBB 300): on day 2 its weight is
    1000 / 1300 and its yield 1 / 100, so yield = 1000/1300 x 0.01."""
    ds = _market(conn)
    conn.execute("UPDATE prices_daily SET close=100 WHERE symbol='AAA' AND date>=?", (str(ds[1]),))
    _ca(conn, "AAA", ds[2], "Dividend - Rs 1 Per Share")
    conn.commit()
    rows = TR.build(conn, "NIFTY50", members=2)
    assert rows[2]["div_yield_bp"] == pytest.approx(1000 / 1300 * 0.01 * 1e4)


def test_a_later_split_puts_the_dividend_on_todays_basis(conn):
    """Rs 2 paid on day 2, then a 1:2 split on day 3 (price factor 0.5): prices_daily is on today's
    basis (AAA's 50 is the post-split price), so the old dividend counts as Rs 1."""
    ds = _market(conn)
    _ca(conn, "AAA", ds[2], "Dividend - Rs 2 Per Share")
    _ca(conn, "AAA", ds[3], "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share",
        kind="SPLIT", pf=0.5)
    conn.commit()
    rows = TR.build(conn, "NIFTY50", members=2)
    assert rows[2]["div_yield_bp"] == pytest.approx(0.625 * 1 / 50 * 1e4)


def test_before_the_calendar_starts_the_tri_follows_the_price(conn):
    """The corporate-action calendar starts on day 3: earlier days are not covered (dividends unknown),
    so the TRI moves with the price there and the summary says from when dividends count."""
    ds = _market(conn)
    _ca(conn, "AAA", ds[3], "Dividend - Rs 1 Per Share")
    conn.commit()
    rows = TR.build(conn, "NIFTY50", members=2)
    assert [r["covered"] for r in rows] == [0, 0, 0, 1, 1]
    assert rows[2]["tri"] == pytest.approx(102)
    assert rows[3]["div_points"] == pytest.approx(102 * 0.625 * 1 / 50)
    assert TR.summary(rows)["dividends_from"] == str(ds[3])


def test_members_are_repicked_on_the_first_session_of_a_month(conn):
    """January's top two are AAA (500) and BBB (300). CCC's shares jump to 20 at 30 January (cap 2,000),
    so from February's first session the top two are CCC and AAA: BBB drops out and its dividend in
    March no longer counts, while AAA's does with weight 500 / 2,500."""
    ds = _days(30, date(2026, 1, 26))
    _px(conn, "NIFTY50", ds, [100] * len(ds))
    for sym, close, sh in (("AAA", 50, 10), ("BBB", 60, 5), ("CCC", 100, 2)):
        _px(conn, sym, ds, [close] * len(ds))
        _shares(conn, sym, sh)
    _shares(conn, "CCC", 20, as_of=date(2026, 1, 30))
    jan = ds[2]
    first_march = next(d for d in ds if d.month == 3)
    _ca(conn, "BBB", jan, "Dividend - Rs 3 Per Share")             # January: BBB is a member
    _ca(conn, "AAA", first_march, "Dividend - Rs 1 Per Share")
    _ca(conn, "BBB", first_march, "Dividend - Rs 3 Per Share")
    conn.commit()
    rows = {r["date"]: r for r in TR.build(conn, "NIFTY50", members=2)}
    assert rows[jan]["div_yield_bp"] == pytest.approx(300 / 800 * 3 / 60 * 1e4)
    assert rows[first_march]["div_yield_bp"] == pytest.approx(500 / 2500 * 1 / 50 * 1e4)


def test_store_and_read_back_and_the_benchmark_symbol(conn):
    ds = _market(conn)
    _ca(conn, "AAA", ds[2], "Dividend - Rs 1 Per Share")
    conn.commit()
    out = TR.run(conn, "NIFTY50", 2)
    assert out["status"] == "COMPLETED" and out["rows"] == 5
    again = TR.run(conn, "NIFTY50", 2)                     # rebuilt, not appended
    assert again["rows"] == 5 and conn.execute("SELECT COUNT(*) FROM index_total_return").fetchone()[0] == 5
    back = TR.series(conn, "nifty50")
    assert back[2]["tri"] == pytest.approx(103.2625)
    assert TR.tr_symbol("nifty50") == "NIFTY50_TR" and TR.is_tr_symbol("NIFTY50_TR")
    from wealth.perf import data as D
    assert D.benchmark_symbol("nifty50tr") == "NIFTY50_TR" and D.has_prices(conn, "NIFTY50_TR")
    p = D.Prices(conn, ds[0], ds[-1])
    assert p.close("NIFTY50_TR", ds[2]) == pytest.approx(103.2625)
    assert D.last_date(conn, "NIFTY50_TR", before=ds[2]) == str(ds[1])
    assert [r[2] for r in D.rows(conn, "NIFTY50_TR", ds[3], ds[4])] == pytest.approx([back[3]["tri"], back[4]["tri"]])
    assert TR.run(conn, "NOINDEX", 2)["status"] == "SKIPPED"


def test_the_wealth_report_measures_against_the_total_return_benchmark(temp_db):
    """The same book against NIFTY50 and NIFTY50_TR: the total-return benchmark's period return is the
    price return plus the stored dividend points, so the book's excess over it is that much smaller."""
    from tests._wealth_seed import OWNER, fresh
    from wealth.perf import report as R
    conn, ds = fresh(temp_db)
    syms = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM prices_daily WHERE symbol<>'NIFTY50'")]
    for s in syms:
        _shares(conn, s, 1_000_000, as_of=ds[0] - timedelta(days=30))
    mid = ds[len(ds) // 2]
    for s in syms:
        _ca(conn, s, mid, "Dividend - Rs 2 Per Share")
    conn.commit()
    assert TR.run(conn, "NIFTY50", 50)["status"] == "COMPLETED"
    with pytest.raises(ValueError, match="no prices"):
        R.build(conn, OWNER, "MANUAL", ds[1], ds[-1], "NIFTYNOPE", sync_first=False)
    price = R.build(conn, OWNER, "MANUAL", ds[1], ds[-1], "nifty50", sync_first=False)
    total = R.build(conn, OWNER, "MANUAL", ds[1], ds[-1], "nifty50tr", sync_first=False)
    assert total["benchmark"] == "NIFTY50_TR"
    tri = {r["date"]: r["tri"] for r in TR.series(conn, "NIFTY50")}
    pxs = {r["date"]: r["price"] for r in TR.series(conn, "NIFTY50")}
    gap = (tri[ds[-1]] / tri[ds[0]]) / (pxs[ds[-1]] / pxs[ds[0]])
    assert gap > 1.0
    ti, pi = total["audit"]["inputs"], price["audit"]["inputs"]
    assert ti["last_price_date"]["NIFTY50_TR"] == str(ds[-1]) and ti["benchmark_last_price_date"] == str(ds[-1])
    assert ti["prices_sha256"] != pi["prices_sha256"]      # the TR rows are part of the audited inputs


def test_routes_and_the_nightly_job(conn, tmp_path, monkeypatch):
    ds = _market(conn)
    _ca(conn, "AAA", ds[2], "Dividend - Rs 1 Per Share")
    conn.commit()
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    tok = {security.TOKEN_HEADER: security.token()}
    assert c.post("/api/data/total-return/rebuild", json={"members": 2}).status_code == 401
    assert c.get("/api/data/total-return").status_code == 404
    rb = c.post("/api/data/total-return/rebuild", json={"members": 2}, headers=tok)
    assert rb.json().get("status") == "COMPLETED", rb.text
    for bad in (0, 501, "50", True):
        assert c.post("/api/data/total-return/rebuild", json={"members": bad}, headers=tok).status_code == 400
    r = c.get("/api/data/total-return", params={"start": str(ds[1])}).json()
    assert r["symbol"] == "NIFTY50_TR" and len(r["rows"]) == 4 and r["summary"]["price_return_pct"] is not None
    from pipeline import w39_jobs

    class Sched:
        def __init__(self):
            self.jobs = []

        def every(self):
            return self

        @property
        def day(self):
            return self

        def at(self, t):
            self.t = t
            return self

        def do(self, fn, name, job):
            self.jobs.append((self.t, name))

    s = Sched()
    w39_jobs.schedule_jobs(s, lambda *a: None)
    assert ("21:10", "total_return") in s.jobs
