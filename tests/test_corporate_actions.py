"""
Corporate actions and delivery data.

Measured before this existed: Dhan's history is split/bonus-adjusted in price
but not in volume (BAJFINANCE 933.1 on 2025-06-13 against NSE's raw 9,331.0,
with NSE's raw volume 1,018,202), NSE's files are raw, and the daily re-sync
only re-fetches a few days -- so a re-base by Dhan after an event moved only
those rows. Delivery was never stored: the CM Bhavcopy has no delivery columns.
"""

import datetime as dt

import pandas as pd
import pytest


# ── NSE's terms -> a price factor ─────────────────────────────────────────

@pytest.mark.parametrize("subject,kind,factor", [
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "SPLIT", 0.2),
    ("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share", "SPLIT", 0.5),
    ("Consolidation Of Shares From Rs 1/- Per Share To Rs 10/- Per Share", "CONSOLIDATION", 10.0),
    ("Bonus 4:1", "BONUS", 0.2),            # 4 new for every 1 held: 5x the shares
    ("Bonus 1:1", "BONUS", 0.5),
    ("Bonus 1:3", "BONUS", 0.75),
    ("Scheme Of Arrangement - Bonus Ncrps 4:1", "SCHEME", None),   # preference shares, not equity
    ("Rights 1:8 @ Premium Rs 192/-", "RIGHTS", None),
    ("Demerger", "DEMERGER", None),
    ("Dividend - Rs 5 Per Share", None, None),
    ("Interim Dividend - Rs 7 Per Share", None, None),
    ("Buy Back", None, None),
])
def test_classify(subject, kind, factor):
    from data.corporate_actions import classify
    k, f = classify(subject)
    assert k == kind
    assert f == pytest.approx(factor) if factor is not None else f is None


# ── reconciliation against NSE's raw close ────────────────────────────────

def _db(temp_db):
    from db.schema import init_db
    init_db()


def _bars(conn, sym, rows):
    for d, o, h, l, c, v in rows:
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                     "VALUES (?,?,?,?,?,?,?,'dhan')", (sym, d, o, h, l, c, v))


def _event(conn, sym, ex, subject):
    from data.corporate_actions import classify
    kind, f = classify(subject)
    conn.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, factor) "
                 "VALUES (?,?,?,?,?)", (sym, ex, subject, kind, f))


def _reference(monkeypatch, table):
    from data import corporate_actions as ca
    monkeypatch.setattr(ca, "nse_reference",
                        lambda d, s, session=None: table.get((str(d), s), (None, None)))
    monkeypatch.setattr("data.dhan.get_tracked_symbols", lambda conn=None: ["ACME", "BAJFINANCE"])


def _rows(conn, sym):
    return [(str(r[0]),) + tuple(r)[1:] for r in conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol=? ORDER BY date", (sym,))]


def test_bonus_on_the_old_basis_is_applied_once(temp_db, monkeypatch):
    """A 1:1 bonus with the history still on the old basis: prices before the
    ex-date halve, volume doubles; the ex-date bar is untouched; a re-run
    changes nothing."""
    from db.schema import get_connection
    from data.corporate_actions import apply_corporate_actions
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2026-09-17", 200, 204, 198, 202, 1000),
                         ("2026-09-18", 202, 206, 200, 204, 1200),
                         ("2026-09-21", 102, 104, 100, 103, 2600)])    # ex-date, new basis
    _event(conn, "ACME", "2026-09-21", "Bonus 1:1")
    conn.commit(); conn.close()
    _reference(monkeypatch, {("2026-09-18", "ACME"): (204.0, 1200.0)})

    res = apply_corporate_actions("2026-09-21")
    assert res["events"] == {"adjusted": 1}
    conn = get_connection()
    try:
        assert _rows(conn, "ACME") == [("2026-09-17", 100, 102, 99, 101, 2000),
                                       ("2026-09-18", 101, 103, 100, 102, 2400),
                                       ("2026-09-21", 102, 104, 100, 103, 2600)]
    finally:
        conn.close()
    assert apply_corporate_actions("2026-09-21")["events"] == {}     # nothing pending
    conn = get_connection()
    try:
        assert _rows(conn, "ACME")[0] == ("2026-09-17", 100, 102, 99, 101, 2000)
    finally:
        conn.close()


def test_history_dhan_already_adjusted_gets_only_its_volume(temp_db, monkeypatch):
    """BAJFINANCE, real numbers: split 2->1 and bonus 4:1 on one ex-date, a
    combined factor of 0.1 (not 0.01 -- two rows, one factor). Dhan had
    adjusted the price; the volume was still NSE's raw figure."""
    from db.schema import get_connection
    from data.corporate_actions import apply_corporate_actions
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "BAJFINANCE", [("2025-06-13", 935.0, 940.0, 930.0, 933.1, 1018202),
                               ("2025-06-16", 956.0, 960.0, 930.0, 938.0, 7016290)])
    _event(conn, "BAJFINANCE", "2025-06-16", "Bonus 4:1")
    _event(conn, "BAJFINANCE", "2025-06-16",
           "Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share")
    conn.commit(); conn.close()
    _reference(monkeypatch, {("2025-06-13", "BAJFINANCE"): (9331.0, 1018202.0)})

    res = apply_corporate_actions("2025-06-16")
    assert res["events"] == {"already_adjusted": 1}
    conn = get_connection()
    try:
        assert _rows(conn, "BAJFINANCE")[0] == ("2025-06-13", 935.0, 940.0, 930.0, 933.1, 10182020)
        st = conn.execute("SELECT DISTINCT status, price_factor FROM corporate_actions").fetchall()
        assert [tuple(r) for r in st] == [("already_adjusted", pytest.approx(0.1))]
        from data.corporate_actions import entry_factor
        assert entry_factor(conn, "BAJFINANCE", "2025-06-13") == pytest.approx(0.1),             "two rows on one ex-date are one factor, not 0.1 x 0.1"
    finally:
        conn.close()


def test_a_basis_matching_neither_side_is_left_alone(temp_db, monkeypatch):
    from db.schema import get_connection
    from data.corporate_actions import apply_corporate_actions
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2026-09-18", 150, 150, 150, 150, 1000)])
    _event(conn, "ACME", "2026-09-21", "Bonus 1:1")
    conn.commit(); conn.close()
    _reference(monkeypatch, {("2026-09-18", "ACME"): (204.0, 1000.0)})
    assert apply_corporate_actions("2026-09-21")["events"] == {"unverified": 1}
    conn = get_connection()
    try:
        assert _rows(conn, "ACME")[0][4] == 150
    finally:
        conn.close()


def test_events_are_applied_newest_first(temp_db, monkeypatch):
    """Two events on one stock: the older is measured net of the newer."""
    from db.schema import get_connection
    from data.corporate_actions import apply_corporate_actions
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2026-08-03", 400, 400, 400, 400, 100),     # raw, before both
                         ("2026-08-04", 200, 200, 200, 200, 200),     # after the first bonus
                         ("2026-09-18", 200, 200, 200, 200, 200),
                         ("2026-09-21", 100, 100, 100, 100, 400)])
    _event(conn, "ACME", "2026-08-04", "Bonus 1:1")
    _event(conn, "ACME", "2026-09-21", "Bonus 1:1")
    conn.commit(); conn.close()
    _reference(monkeypatch, {("2026-08-03", "ACME"): (400.0, 100.0),
                             ("2026-09-18", "ACME"): (200.0, 200.0)})
    assert apply_corporate_actions("2026-09-21")["events"] == {"adjusted": 2}
    conn = get_connection()
    try:
        assert [(r[0], r[4], r[5]) for r in _rows(conn, "ACME")] == [
            ("2026-08-03", 100, 400), ("2026-08-04", 100, 400),
            ("2026-09-18", 100, 400), ("2026-09-21", 100, 400)]
    finally:
        conn.close()


# ── Dhan's bars onto the stored basis ─────────────────────────────────────

def test_bars_before_an_unreconciled_ex_date_are_held():
    from data.corporate_actions import to_stored_basis
    ev = [("2026-09-21", "pending", None, True)]
    bar = (200, 204, 198, 202, 1000)
    assert to_stored_basis("2026-09-18", bar, (202, 1000), ev, dt.date(2026, 9, 21)) is None
    assert to_stored_basis("2026-09-21", bar, None, ev, dt.date(2026, 9, 21)) == bar
    # an old pending event does not block a newly tracked stock's first fetch
    assert to_stored_basis("2025-01-10", bar, None, [("2025-06-16", "pending", None, True)],
                           dt.date(2026, 9, 21)) == bar


def test_a_re_sync_cannot_undo_an_adjustment():
    """After a 1:1 bonus applied here, Dhan still sending the old basis must be
    brought onto the stored one; Dhan's own adjusted price must not be scaled
    twice; its raw volume is always divided."""
    from data.corporate_actions import to_stored_basis
    ev = [("2026-09-21", "adjusted", 0.5, True)]
    stored = (102.0, 2400)
    assert to_stored_basis("2026-09-18", (202, 206, 200, 204, 1200), stored, ev,
                           dt.date(2026, 9, 22)) == (101.0, 103.0, 100.0, 102.0, 2400)
    assert to_stored_basis("2026-09-18", (101, 103, 100, 102, 1200), stored, ev,
                           dt.date(2026, 9, 22)) == (101, 103, 100, 102, 2400)


def test_the_re_sync_keeps_the_stored_basis(temp_db, monkeypatch):
    """End to end through run_historical_pipeline with a stub Dhan."""
    from db.schema import get_connection
    from data import dhan
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2026-09-18", 101, 103, 100, 102, 2400),
                         ("2026-09-21", 102, 104, 100, 103, 2600)])
    conn.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, factor, status, "
                 "price_factor) VALUES ('ACME','2026-09-21','Bonus 1:1','BONUS',0.5,'adjusted',0.5)")
    conn.commit(); conn.close()
    old_basis = pd.DataFrame({"date": [dt.date(2026, 9, 18), dt.date(2026, 9, 21)],
                              "open": [202.0, 102.0], "high": [206.0, 104.0], "low": [200.0, 100.0],
                              "close": [204.0, 103.0], "volume": [1200, 2600]})
    monkeypatch.setattr(dhan, "HAS_DHAN", True)
    monkeypatch.setattr(dhan, "get_dhan_client", lambda: (None, None))
    monkeypatch.setattr(dhan, "fetch_historical_daily", lambda *a, **k: old_basis.copy())
    monkeypatch.setattr(dhan.time, "sleep", lambda s: None)
    dhan.run_historical_pipeline(["ACME"], days=5, end_date=dt.date(2026, 9, 22))
    conn = get_connection()
    try:
        assert _rows(conn, "ACME") == [("2026-09-18", 101, 103, 100, 102, 2400),
                                       ("2026-09-21", 102, 104, 100, 103, 2600)]
    finally:
        conn.close()


# ── signal outcomes across an ex-date ─────────────────────────────────────

def test_a_bonus_after_a_signal_is_not_a_loss(temp_db, monkeypatch):
    """Entry recorded at 200 before a 1:1 bonus; the history is re-based to
    ~100. Without the factor the signal reads -50%; with it, +3.5% is a hit."""
    from db.schema import get_connection
    from scores import signal_log
    _db(temp_db)
    conn = get_connection()
    signal_log.ensure_tables(conn)
    _bars(conn, "ACME", [("2026-09-17", 100, 101, 99, 100, 2000),
                         ("2026-09-18", 100, 103.5, 99.5, 103, 2000)])
    conn.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, factor, status, "
                 "price_factor) VALUES ('ACME','2026-09-18','Bonus 1:1','BONUS',0.5,'adjusted',0.5)")
    conn.execute("INSERT INTO signal_log (id, run_id, logged_at, signal_date, symbol, signal, "
                 "entry_price) VALUES ('s1','r','2026-09-17T16:45','2026-09-17','ACME','BUY',200.0)")
    conn.commit(); conn.close()
    monkeypatch.setattr(signal_log, "momentum_thresholds", lambda: [3.0])
    signal_log.evaluate_outcomes()
    conn = get_connection()
    try:
        hit, mae = conn.execute("SELECT hit, max_adverse_pct FROM signal_outcome").fetchone()
        assert hit == 1 and mae > -1
    finally:
        conn.close()


# ── delivery ──────────────────────────────────────────────────────────────

_FULL_BHAV = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
    "AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    "RELIANCE, EQ, 21-Sep-2026, 1226.4, 1234.1, 1249.1, 1232.5, 1247.4, 1247.4, 1240.86, "
    "10007218, 124175.97, 186656, 6461366, 64.57\n"
    "ACME, BE, 21-Sep-2026, 10, 10, 10, 10, 10, 10, 10, 500, 0.05, 3, 500, 100.00\n"
    "ACME, EQ, 21-Sep-2026, 10, 10, 10, 10, 10, 10, 10, 800, 0.08, 4, 400, 50.00\n"
    "OLDDAY, EQ, 18-Sep-2026, 10, 10, 10, 10, 10, 10, 10, 800, 0.08, 4, 400, 50.00\n"
    "NODEL, EQ, 21-Sep-2026, 10, 10, 10, 10, 10, 10, 10, 800, 0.08, 4, -, -\n"
).encode()


def test_parse_delivery():
    from data.bhavcopy import parse_delivery
    df = parse_delivery(_FULL_BHAV, dt.date(2026, 9, 21)).set_index("symbol")
    assert df.loc["RELIANCE", "deliv_pct"] == 64.57 and df.loc["RELIANCE", "deliv_qty"] == 6461366
    assert df.loc["ACME", "deliv_pct"] == 50.0, "EQ is preferred over BE"
    assert "OLDDAY" not in df.index, "a row dated another session is not this one"
    assert "NODEL" not in df.index


def test_delivery_quantity_follows_the_stored_volume_basis(temp_db):
    """RELIANCE as NSE reports it; ACME's stored volume is on a post-bonus
    basis (2x NSE's raw count), so its delivery quantity is too."""
    from db.schema import get_connection
    from data.bhavcopy import parse_delivery, store_delivery
    _db(temp_db)
    conn = get_connection()
    try:
        _bars(conn, "RELIANCE", [("2026-09-21", 1234.1, 1249.1, 1232.5, 1247.4, 10007218)])
        _bars(conn, "ACME", [("2026-09-21", 5, 5, 5, 5, 1600)])
        n = store_delivery(conn, dt.date(2026, 9, 21), parse_delivery(_FULL_BHAV, dt.date(2026, 9, 21)))
        assert n == 2
        got = dict((r[0], (r[1], r[2])) for r in conn.execute(
            "SELECT symbol, delivery_qty, delivery_pct FROM prices_daily"))
        assert got == {"RELIANCE": (6461366, 64.57), "ACME": (800, 50.0)}
    finally:
        conn.close()


def test_delivery_pipeline_waits_for_nse_and_skips_what_it_has(temp_db, monkeypatch):
    from db.schema import get_connection
    from data import bhavcopy
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "RELIANCE", [("2026-09-18", 1, 1, 1, 1, 10), ("2026-09-21", 1, 1, 1, 1, 10)])
    conn.execute("UPDATE prices_daily SET delivery_pct=40 WHERE date='2026-09-18'")
    conn.commit(); conn.close()
    asked = []
    monkeypatch.setattr(bhavcopy, "get_nse_session", lambda: None)
    monkeypatch.setattr(bhavcopy, "download_delivery", lambda d, s: asked.append(str(d)))
    res = bhavcopy.run_delivery_pipeline(dt.date(2026, 9, 21), lookback=2)
    assert asked == ["2026-09-21"], "09-18 already has delivery"
    assert res["waiting"] == ["2026-09-21"] and res["rows"] == 0


# ── rights and demergers: no factor in NSE's terms ────────────────────────

def _rights_case(temp_db, monkeypatch, dhan_close):
    from db.schema import get_connection
    from data import corporate_actions as ca
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2026-09-17", 100, 100, 100, 100, 1000),
                         ("2026-09-18", 100, 100, 100, 100, 1000),
                         ("2026-09-21", 97, 97, 97, 97, 1000)])
    _event(conn, "ACME", "2026-09-21", "Rights 1:8 @ Premium Rs 192/-")
    conn.commit(); conn.close()
    _reference(monkeypatch, {("2026-09-18", "ACME"): (100.0, 1000.0)})
    monkeypatch.setattr(ca, "_dhan_series", lambda sym, ex, dhan=None: {
        "2026-09-17": (dhan_close,) * 4, "2026-09-18": (dhan_close,) * 4})
    return ca.apply_corporate_actions("2026-09-21")


def test_rights_take_the_factor_dhan_applied(temp_db, monkeypatch):
    from db.schema import get_connection
    res = _rights_case(temp_db, monkeypatch, 98.0)
    assert res["events"] == {"already_adjusted": 1}
    conn = get_connection()
    try:
        assert [(r[0], r[4], r[5]) for r in _rows(conn, "ACME")] == [
            ("2026-09-17", 98, 1000), ("2026-09-18", 98, 1000), ("2026-09-21", 97, 1000)]
        assert conn.execute("SELECT price_factor FROM corporate_actions").fetchone()[0] == pytest.approx(0.98)
    finally:
        conn.close()


def test_rights_dhan_has_not_adjusted_yet_are_rechecked(temp_db, monkeypatch):
    """Nothing to apply yet; the re-sync must not re-base the window alone
    meanwhile, and the next run looks again."""
    from data.corporate_actions import basis_events, to_stored_basis
    from db.schema import get_connection
    res = _rights_case(temp_db, monkeypatch, 100.0)
    assert res["events"] == {"unadjusted": 1}
    conn = get_connection()
    try:
        ev = basis_events(conn)["ACME"]
    finally:
        conn.close()
    assert to_stored_basis("2026-09-18", (98, 98, 98, 98, 1000), (100, 1000), ev,
                           dt.date(2026, 9, 22)) is None
    from data import corporate_actions as ca
    monkeypatch.setattr(ca, "_dhan_series", lambda sym, ex, dhan=None: {
        "2026-09-17": (98.0,) * 4, "2026-09-18": (98.0,) * 4})
    assert ca.apply_corporate_actions("2026-09-22")["events"] == {"already_adjusted": 1}


def test_an_older_event_is_measured_net_of_a_later_combined_one(temp_db, monkeypatch):
    """A 1:1 bonus in August, then split 2->1 plus bonus 4:1 on one September
    ex-date (0.1 combined). Reconciling August must divide by 0.1, not 0.01."""
    from db.schema import get_connection
    from data.corporate_actions import apply_corporate_actions
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2026-08-03", 2000, 2000, 2000, 2000, 10),
                         ("2026-08-04", 1000, 1000, 1000, 1000, 20),
                         ("2026-09-18", 1000, 1000, 1000, 1000, 20),
                         ("2026-09-21", 100, 100, 100, 100, 200)])
    _event(conn, "ACME", "2026-08-04", "Bonus 1:1")
    _event(conn, "ACME", "2026-09-21", "Bonus 4:1")
    _event(conn, "ACME", "2026-09-21",
           "Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share")
    conn.commit(); conn.close()
    _reference(monkeypatch, {("2026-08-03", "ACME"): (2000.0, 10.0),
                             ("2026-09-18", "ACME"): (1000.0, 20.0)})
    assert apply_corporate_actions("2026-09-21")["events"] == {"adjusted": 2}
    conn = get_connection()
    try:
        assert [(r[0], r[4], r[5]) for r in _rows(conn, "ACME")] == [
            ("2026-08-03", 100, 200), ("2026-08-04", 100, 200),
            ("2026-09-18", 100, 200), ("2026-09-21", 100, 200)]
    finally:
        conn.close()


def test_a_renamed_stock_is_not_retried_every_day(temp_db, monkeypatch):
    """TMPV's 2025-10-14 demerger: on 10-13 NSE's file lists TATAMOTORS."""
    from db.schema import get_connection
    from data.corporate_actions import apply_corporate_actions
    _db(temp_db)
    conn = get_connection()
    _bars(conn, "ACME", [("2025-10-13", 100, 100, 100, 100, 10)])
    _event(conn, "ACME", "2025-10-14", "Bonus 1:1")
    conn.commit(); conn.close()
    _reference(monkeypatch, {})
    assert apply_corporate_actions("2026-09-21")["events"] == {"no_data": 1}
    assert apply_corporate_actions("2026-09-22")["events"] == {}
