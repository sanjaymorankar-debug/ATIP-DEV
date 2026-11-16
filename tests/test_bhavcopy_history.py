"""
Seven years of NSE's CM Bhavcopy kept locally (owner, 16 Nov 2026): data/bhavcopy_history.py.

A fake NSE archive serves what nsearchives.nseindia.com served on 16 Nov 2026 -- the old cmDDMONYYYYbhav.csv.zip up to
5 Jul 2024, the UDiFF BhavCopy_NSE_CM_..._F_0000.csv.zip from 5 Jul 2024, sec_bhavdata_full_DDMMYYYY.csv for
delivery, and a 404 for a day with no session. Nothing reaches NSE.
"""
import datetime as dt
import gzip
import io
import re
import zipfile

import pandas as pd
import pytest

MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
OLD_HEAD = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n"
UDIFF_HEAD = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
              "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
              "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n")
DELIV_HEAD = (" SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
              "AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n")


class Resp:
    def __init__(self, code, content=b""):
        self.status_code, self.content = code, content


class FakeNSE:
    """bars: {date: [(symbol, series, close, prev_close, volume)]}; a date not in bars is a 404 everywhere.
    unposted / no_delivery: dates whose CM / delivery file is not up yet; down: dates answering HTTP 503."""

    def __init__(self, bars, unposted=(), no_delivery=(), down=()):
        self.bars, self.unposted, self.no_delivery, self.down = bars, set(unposted), set(no_delivery), set(down)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        m = re.search(r"cm(\d{2})([A-Z]{3})(\d{4})bhav\.csv\.zip$", url)
        if m:
            d = dt.date(int(m[3]), MON.index(m[2]) + 1, int(m[1]))
            return self._cm(d, "old") if d <= dt.date(2024, 7, 5) else Resp(404, b"<html>")
        m = re.search(r"BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv\.zip$", url)
        if m:
            d = dt.datetime.strptime(m[1], "%Y%m%d").date()
            return self._cm(d, "udiff") if d >= dt.date(2024, 7, 5) else Resp(404, b"<html>")
        m = re.search(r"sec_bhavdata_full_(\d{8})\.csv$", url)
        d = dt.datetime.strptime(m[1], "%d%m%Y").date()
        if d not in self.bars or d in self.no_delivery:
            return Resp(404, b"<html>")
        rows = "".join(f" {s}, {sr}, {d:%d-%b-%Y}, {p}, {c}, {c}, {c}, {c}, {c}, {c}, {v}, 1.0, 10, {v // 2}, 50.00\n"
                       for s, sr, c, p, v in self.bars[d])
        return Resp(200, (DELIV_HEAD + rows).encode())

    def _cm(self, d, layout):
        if d in self.down:
            return Resp(503)
        if d not in self.bars or d in self.unposted:
            return Resp(404, b"<html>")
        if layout == "old":
            rows = "".join(f"{s},{sr},{c},{c},{c},{c},{c},{p},{v},{v * c},{d.day:02d}-{MON[d.month - 1]}-{d.year},10,"
                           f"INE{s[:3]}01010,\n" for s, sr, c, p, v in self.bars[d])
            name, text = f"cm{d.day:02d}{MON[d.month - 1]}{d.year}bhav.csv", OLD_HEAD + rows
        else:
            rows = "".join(f"{d},{d},CM,NSE,STK,1,INE{s[:3]}01010,{s},{sr},,,,,{s} LTD,{c},{c},{c},{c},{c},{p},,{c},,,"
                           f"{v},{v * c},10,F1,1,,,,,\n" for s, sr, c, p, v in self.bars[d])
            name, text = f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv", UDIFF_HEAD + rows
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, text)
        return Resp(200, buf.getvalue())


def _weekdays(a, b, skip=()):
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)
            if (a + dt.timedelta(days=i)).weekday() < 5 and a + dt.timedelta(days=i) not in skip]


@pytest.fixture
def bh(temp_db, tmp_path, monkeypatch):
    from data import bhavcopy_history as B
    from data import lake
    from db.schema import init_db
    init_db()
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    monkeypatch.setattr(B, "RAW_DIR", tmp_path / "raw" / "nse_archive")
    monkeypatch.setattr(B, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(B, "settings", lambda: {**B.DEFAULTS})
    return B


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# ── files ─────────────────────────────────────────────────────────────────

def test_both_layouts_and_the_delivery_file_parse_to_one_column_set(bh):
    fake = FakeNSE({dt.date(2019, 11, 15): [("ACME", "EQ", 100.5, 99.0, 1000), ("ACME", "N1", 101.0, 101.0, 5)],
                    dt.date(2024, 7, 8): [("ACME", "EQ", 210.0, 205.0, 3000)]})
    old = bh.parse_cm(fake.get(bh.cm_urls(dt.date(2019, 11, 15))[0][1]).content, dt.date(2019, 11, 15))
    new = bh.parse_cm(fake.get(bh.cm_urls(dt.date(2024, 7, 8))[0][1]).content, dt.date(2024, 7, 8))
    assert list(old.columns) == list(new.columns) == bh.COLUMNS
    assert old.iloc[0][["symbol", "series", "close", "prev_close", "volume"]].tolist() == ["ACME", "EQ", 100.5, 99.0,
                                                                                           1000]
    assert len(old) == 2, "every row of the file, every series"
    assert new.iloc[0][["close", "prev_close", "volume", "trades"]].tolist() == [210.0, 205.0, 3000, 10]
    with pytest.raises(ValueError, match="dated"):
        bh.parse_cm(fake.get(bh.cm_urls(dt.date(2019, 11, 15))[0][1]).content, dt.date(2019, 11, 18))
    dl = bh.parse_delivery_full(fake.get(bh.DELIVERY_URL.format(dmy="15112019")).content, dt.date(2019, 11, 15))
    assert dl[dl["series"] == "EQ"][["symbol", "delivery_qty", "delivery_pct"]].values.tolist() == [["ACME", 500, 50.0]]


def test_the_layout_follows_the_date_and_falls_back_on_5_july_2024(bh):
    assert [l for l, _ in bh.cm_urls(dt.date(2024, 7, 4))] == ["old", "udiff"]
    assert [l for l, _ in bh.cm_urls(dt.date(2024, 7, 5))] == ["udiff", "old"]
    assert bh.cm_urls(dt.date(2019, 11, 15))[0][1].endswith("/content/historical/EQUITIES/2019/NOV/cm15NOV2019bhav.csv.zip")
    assert bh.cm_urls(dt.date(2026, 11, 13))[0][1].endswith("/content/cm/BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip")


# ── the archive run ───────────────────────────────────────────────────────

def test_a_run_keeps_each_session_newest_first_marks_holidays_and_resumes(bh, tmp_path):
    from data import lake
    from db.schema import get_connection
    holiday = dt.date(2024, 7, 1)
    days = _weekdays(dt.date(2024, 6, 24), dt.date(2024, 7, 12), skip={holiday})
    fake = FakeNSE({d: [("ACME", "EQ", 100.0 + i, 99.0 + i, 1000)] for i, d in enumerate(days)},
                   unposted={dt.date(2024, 7, 12)}, no_delivery={dt.date(2024, 7, 11)}, down={dt.date(2024, 7, 3)})
    clock = Clock()
    now = dt.datetime(2024, 7, 12, 19, 50)
    out = bh.run_archive(minutes=0, max_sessions=11, session=fake, now=now, clock=clock, sleep=clock.sleep)
    assert out["dates"] == 11 and out["session"] == 8 and out["holiday"] == 1 and out["pending"] == 1 \
        and out["error"] == 1 and out["status"] == "PARTIAL"
    conn = get_connection()
    try:
        st = bh.states(conn)
        assert st["2024-07-12"]["status"] == "pending", "today's file not posted yet"
        assert st["2024-07-01"]["status"] == "holiday" and st["2024-07-01"]["attempts"] == 0
        assert st["2024-07-03"]["status"] == "error" and "503" in st["2024-07-03"]["last_error"]
        assert st["2024-07-11"]["status"] == "session" and st["2024-07-11"]["delivery_status"] == "pending"
        assert st["2024-07-08"]["cm_format"] == "udiff" and st["2024-07-04"]["cm_format"] == "old"
        assert st["2024-07-05"]["cm_format"] == "udiff"
        assert min(st) == "2024-06-28", "newest first: 11 dates back from 12 Jul"
        assert set(clock.slept) == {1.2} and len(clock.slept) == len(fake.calls) - 1, "one request every 1.2 s"
        part = lake.read(bh.DATASET, "2024-07-10", "2024-07-10", conn=conn)
        assert part[["symbol", "close", "delivery_qty", "delivery_pct"]].values.tolist() == [["ACME", 111.0, 500, 50.0]]
        assert (bh.RAW_DIR / "cm" / "2024" / "BhavCopy_NSE_CM_0_0_0_20240710_F_0000.csv.zip").exists()
        assert (bh.RAW_DIR / "cm" / "2024" / "cm04JUL2024bhav.csv.zip").exists()
        kept = bh.RAW_DIR / "delivery" / "2024" / "sec_bhavdata_full_10072024.csv.gz"
        assert b"DELIV_QTY" in gzip.decompress(kept.read_bytes())
    finally:
        conn.close()

    fake.unposted.clear(); fake.no_delivery.clear(); fake.down.clear(); fake.calls.clear()
    again = bh.run_archive(minutes=0, max_sessions=3, session=fake, now=now, clock=clock, sleep=clock.sleep)
    assert again["dates"] == 3 and again["session"] == 3
    asked = [u.rsplit("/", 1)[1] for u in fake.calls]
    assert asked[:3] == ["BhavCopy_NSE_CM_0_0_0_20240712_F_0000.csv.zip", "sec_bhavdata_full_12072024.csv",
                         "sec_bhavdata_full_11072024.csv"], "only what was missing: 12 Jul, then 11 Jul's delivery"
    conn = get_connection()
    try:
        st = bh.states(conn)
        assert st["2024-07-11"]["delivery_status"] == "stored" and st["2024-07-03"]["status"] == "session"
        assert lake.read(bh.DATASET, "2024-07-11", "2024-07-11", conn=conn)["delivery_pct"].tolist() == [50.0]
        assert conn.execute("SELECT MAX(version) FROM lake_partition WHERE partition_date='2024-07-11'").fetchone()[0] == 2
        assert lake.read(bh.DATASET, "2024-07-11", "2024-07-11", conn=conn)["close"].tolist() == [112.0], "one row"
    finally:
        conn.close()


def test_the_time_budget_stops_the_run_and_a_404_old_enough_is_a_holiday(bh):
    from db.schema import get_connection
    days = _weekdays(dt.date(2023, 11, 6), dt.date(2023, 11, 17), skip={dt.date(2023, 11, 14)})
    fake = FakeNSE({d: [("ACME", "EQ", 100.0, 100.0, 10)] for d in days})
    clock = Clock()
    out = bh.run_archive(minutes=0.05, session=fake, now=dt.datetime(2023, 11, 20, 10, 0), clock=clock,
                         sleep=clock.sleep)
    assert out["dates"] < 10 and out["remaining"] > 0, "3 s of budget at 1.2 s a request"
    bh.run_archive(minutes=0, max_sessions=10, session=fake, now=dt.datetime(2023, 11, 20, 10, 0), clock=clock,
                   sleep=clock.sleep)
    conn = get_connection()
    try:
        assert bh.calendar(conn)["2023-11-14"] == "holiday", "Diwali Balipratipada 2023: no file in either layout"
    finally:
        conn.close()


def test_scheduled_runs_and_their_registration(bh, monkeypatch):
    import schedule
    from pipeline import w39_jobs as W
    calls = []
    monkeypatch.setattr(bh, "run_archive", lambda minutes=None, now=None, **k: calls.append(minutes) or {"status": "SUCCESS"})
    monkeypatch.setattr(bh, "verify", lambda write_report=False, **k: {"days_compared": 7, "start": "2019-11-15",
                                                                        "symbols": 1, "mismatch": 0, "missing": 0,
                                                                        "phantom": 0, "report": "x.json"})
    mon, sat, sun = dt.datetime(2026, 11, 16, 19, 50), dt.datetime(2026, 11, 21, 10, 0), dt.datetime(2026, 11, 22, 12, 0)
    assert bh.run_scheduled("evening", mon)["status"] == "SUCCESS" and calls == [5]
    assert bh.run_scheduled("weekend", mon)["status"] == "SKIPPED"
    assert bh.run_scheduled("weekend", sat)["status"] == "SUCCESS" and calls == [5, 90]
    assert bh.run_scheduled("verify", sat)["status"] == "SKIPPED"
    assert bh.run_scheduled("verify", sun) == {"status": "SUCCESS", "rows": 7, "start": "2019-11-15", "symbols": 1,
                                               "mismatch": 0, "missing": 0, "phantom": 0, "report": "x.json"}
    monkeypatch.setattr("ops.role.is_viewer", lambda: True)
    assert bh.run_scheduled("evening", mon)["reason"] == "view-only server"

    schedule.clear()
    try:
        lines = W.schedule_jobs(schedule, lambda *a, **k: None)
        jobs = {}
        for j in schedule.get_jobs():
            args = j.job_func.args
            jobs.setdefault(args[0], []).append((j.at_time.strftime("%H:%M"), args[1:]))
        assert jobs["bhavcopy_history"] == [("19:50", (W.bhavcopy_history_run, "evening"))]
        assert jobs["bhavcopy_history_weekend"] == [("10:00", (W.bhavcopy_history_run, "weekend"))]
        assert jobs["price_check"] == [("12:00", (W.bhavcopy_history_run, "verify"))]
        assert any(l.startswith("bhavcopy_history daily 19:50 IST (5 min), Sat/Sun 10:00 (90 min)") for l in lines)
    finally:
        schedule.clear()


# ── the check against the stored history ──────────────────────────────────

@pytest.fixture
def checked(bh):
    """Ten sessions archived. ACME: a 1:1 bonus with ex-date 8 Jul 2024 (NSE's previous close that day is the
    adjusted one), stored on today's basis, one day (4 Jul) missing. BETA: one stored close 3 % off (3 Jul). A stored
    bar on the 1 Jul holiday (phantom)."""
    from db.schema import get_connection
    holiday = dt.date(2024, 7, 1)
    days = _weekdays(dt.date(2024, 6, 26), dt.date(2024, 7, 10), skip={holiday})
    ex = dt.date(2024, 7, 8)
    bars, acme, beta = {}, {}, {}
    prev_a = prev_b = None
    for i, d in enumerate(days):
        a_raw = (200.0 + 2 * i) if d < ex else (100.0 + i)
        b_raw = 50.0 + i
        a_prev = prev_a if prev_a is None or d != ex else prev_a / 2
        bars[d] = [("ACME", "EQ", a_raw, a_prev or a_raw, 1000 if d < ex else 2000), ("BETA", "EQ", b_raw, prev_b or b_raw, 500)]
        acme[d], beta[d] = (a_raw / 2 if d < ex else a_raw), b_raw
        prev_a, prev_b = a_raw, b_raw
    bh.run_archive(minutes=0, session=FakeNSE(bars), now=dt.datetime(2024, 7, 20, 20, 0), years=1, max_sessions=20,
                   clock=Clock(), sleep=lambda s: None)
    conn = get_connection()
    try:
        for d in days:
            if d != dt.date(2024, 7, 4):
                conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                             "VALUES ('ACME',?,?,?,?,?,?,'dhan')", (str(d), acme[d], acme[d], acme[d], acme[d], 2000))
            off = 1.03 if d == dt.date(2024, 7, 3) else 1.0
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                         "VALUES ('BETA',?,?,?,?,?,?,'dhan')", (str(d), beta[d] * off, beta[d], beta[d], beta[d] * off,
                                                                 500))
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                     "VALUES ('BETA',?,51,51,51,51,500,'dhan')", (str(holiday),))
        conn.commit()
    finally:
        conn.close()
    return bh


def test_verify_finds_the_bad_close_the_missing_day_and_the_phantom_but_not_the_bonus(checked):
    out = checked.verify(symbols=["ACME", "BETA"], write_report=True)
    assert out["start"] == "2024-06-24" and out["symbols"] == 2, "24-25 Jun: archived, no file (holidays here)"
    assert (out["mismatch"], out["missing"], out["phantom"]) == (1, 1, 1)
    assert out["per_symbol"] == {"ACME": {"missing": 1}, "BETA": {"mismatch": 1, "phantom": 1}}
    bad = {(e["symbol"], e["date"], e["flag"]) for e in out["issues"]}
    assert bad == {("BETA", "2024-07-03", "mismatch"), ("ACME", "2024-07-04", "missing"), ("BETA", "2024-07-01", "phantom")}
    assert next(e for e in out["issues"] if e["flag"] == "mismatch")["deviation_pct"] == 3.0
    assert checked.REPORT_DIR.joinpath(f"{dt.date.today()}.json").exists()


def test_fill_writes_the_missing_day_on_the_stored_basis_once(checked):
    from db.schema import get_connection
    dry = checked.fill(symbols=["ACME", "BETA"])
    assert dry["missing"] == 1 and dry["written"] == 0
    r = dry["rows"][0]
    assert (r["symbol"], r["date"], r["close"]) == ("ACME", "2024-07-04", 105.0), "NSE's 210 halved: before the bonus"
    assert r["volume"] == 2000 and r["delivery_pct"] == 50.0, "volume on the stored share basis"
    out = checked.fill(symbols=["ACME", "BETA"], apply=True)
    assert out["written"] == 1
    conn = get_connection()
    try:
        assert conn.execute("SELECT close, source FROM prices_daily WHERE symbol='ACME' AND date='2024-07-04'"
                            ).fetchone()[:] == (105.0, "bhavcopy_hist")
    finally:
        conn.close()
    assert checked.fill(symbols=["ACME", "BETA"])["missing"] == 0
    assert checked.verify(symbols=["ACME"])["missing"] == 0


def test_the_check_stops_where_the_archive_has_a_gap(checked):
    from db.schema import get_connection
    conn = get_connection()
    try:
        conn.execute("UPDATE nse_archive_session SET status='error' WHERE trade_date='2024-07-02'")
        conn.commit()
        assert str(checked.covered_from(conn)) == "2024-07-03"
    finally:
        conn.close()
    out = checked.verify(symbols=["ACME", "BETA"])
    assert out["start"] == "2024-07-03" and out["phantom"] == 0 and out["mismatch"] == 1


# ── retention and the boundary tool ───────────────────────────────────────

def test_purge_removes_files_partitions_and_sessions_before_the_cutoff(checked):
    from db.schema import get_connection
    conn = get_connection()
    try:
        dry = checked.purge("2024-07-03", conn=conn)
        assert dry == {"lake_rows": 8, "files": 8, "sessions": 7}, "4 sessions (2 rows, 2 files each) + 3 holidays"
        assert (checked.RAW_DIR / "cm" / "2024" / "cm02JUL2024bhav.csv.zip").exists(), "a dry run deletes nothing"
        checked.purge("2024-07-03", conn=conn, dry_run=False)
        assert not (checked.RAW_DIR / "cm" / "2024" / "cm02JUL2024bhav.csv.zip").exists()
        assert (checked.RAW_DIR / "cm" / "2024" / "cm03JUL2024bhav.csv.zip").exists()
        assert min(checked.states(conn)) == "2024-07-03"
    finally:
        conn.close()


def test_the_boundary_tool_takes_nse_s_word_for_a_day(bh):
    from tools.dhan_boundary_days import _session
    nse = {"2023-11-14": "holiday", "2023-03-07": "session"}
    assert _session(dt.date(2023, 11, 14), set(), nse) is False, "Diwali Balipratipada: NSE has no file"
    assert _session(dt.date(2023, 3, 7), set(), nse) is True
    assert _session(dt.date(2023, 3, 8), set(), nse) is None, "not archived, nobody traded: unconfirmed"


@pytest.mark.parametrize("recorded", [True, False])
def test_a_bonus_nse_leaves_unadjusted_is_a_re_basing_not_a_mismatch(bh, recorded):
    """CONCORDBIO, 1:1 bonus ex 4 Nov 2026: the stored history is halved before the ex-date, and NSE's UDiFF file kept
    the unadjusted previous close that day (1,227.9). The level change is a re-basing -- explained when
    corporate_actions has the ex-date, listed as unexplained when it does not -- and never a run of mismatches."""
    from db.schema import get_connection
    days = _weekdays(dt.date(2026, 10, 27), dt.date(2026, 11, 13), skip={dt.date(2026, 11, 10)})
    ex = dt.date(2026, 11, 4)
    raw = {d: (1240.0 + i) if d < ex else (650.0 + i) for i, d in enumerate(days)}
    bars, prev = {}, None
    for d in days:
        bars[d] = [("ACME", "EQ", raw[d], prev or raw[d], 1000)]       # prev close never adjusted
        prev = raw[d]
    bh.run_archive(minutes=0, session=FakeNSE(bars), now=dt.datetime(2026, 11, 20, 20, 0), years=1, max_sessions=20,
                   clock=Clock(), sleep=lambda s: None)
    conn = get_connection()
    try:
        for d in days:
            c = raw[d] / 2 if d < ex else raw[d]
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                         "VALUES ('ACME',?,?,?,?,?,1000,'bhavcopy')", (str(d), c, c, c, c))
        if recorded:
            conn.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, price_factor, status) "
                         "VALUES ('ACME', '2026-11-04', 'Bonus 1:1', 'BONUS', 0.5, 'adjusted')")
        conn.commit()
    finally:
        conn.close()
    out = bh.verify(symbols=["ACME"])
    assert out["mismatch"] == 0 and out["ok"] == len(days) and out["steps"] == 1
    assert out["unexplained_steps"] == ([] if recorded else
                                        [{"symbol": "ACME", "date": "2026-11-04", "factor": 0.5, "explained": False}])


def test_a_wrong_first_close_is_a_mismatch_not_a_step(bh):
    days = _weekdays(dt.date(2024, 7, 1), dt.date(2024, 7, 12))
    bh.run_archive(minutes=0, session=FakeNSE({d: [("ACME", "EQ", 100.0, 100.0, 10)] for d in days}),
                   now=dt.datetime(2024, 7, 12, 20, 0), years=1, max_sessions=10, clock=Clock(), sleep=lambda s: None)
    from db.schema import get_connection
    conn = get_connection()
    try:
        for d in days:
            c = 90.0 if d == days[0] else 100.0
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                         "VALUES ('ACME',?,?,?,?,?,10,'dhan')", (str(d), c, c, c, c))
        conn.commit()
    finally:
        conn.close()
    out = bh.verify(symbols=["ACME"])
    assert (out["mismatch"], out["steps"]) == (1, 0) and out["issues"][0]["date"] == "2024-07-01"
