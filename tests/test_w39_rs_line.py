"""
W39 Phase 2 (item 5): the RS line (price / Nifty) at a new 52-week high, and before price, and the RS
rating ranked within the stock's size group (AMFI's rank rule: top 100 large, 101-250 mid, rest small).
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from research import technicals as T


def _frame(closes, start="2025-01-06"):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": np.full(len(c), 1e5)},
                        index=pd.bdate_range(start, periods=len(c)))


def _bench(values, start="2025-01-06"):
    return pd.Series(np.asarray(values, dtype=float), index=pd.bdate_range(start, periods=len(values)))


def _hits(df, bench):
    return {h[0] for h in T.run_scans(T.indicators(df, bench))}


# ── the RS line ───────────────────────────────────────────────────────────

def test_rs_line_new_high_fires_on_the_first_day_only():
    flat = [100 + (i % 3) * 0.2 for i in range(280)]
    bench = _bench([100.0] * 284)
    first = _hits(_frame(flat + [102.0]), bench)                    # 102 / 100 beats every earlier reading
    assert "rs_line_new_high" in first
    again = _hits(_frame(flat + [102.0, 103.0]), bench)             # a second new high in a row: not a new event
    assert "rs_line_new_high" not in again


def test_rs_line_leads_when_relative_strength_breaks_out_before_price():
    stock = [100.0] * 40 + [120.0] * 10 + [100.0] * 250
    bench = list(np.full(240, 100.0)) + list(np.linspace(100, 84, 59)) + [82.0]   # RS 100/82 tops the 120 day
    d = T.indicators(_frame(stock), _bench(bench))
    hit = {h[0] for h in T.run_scans(d)}
    assert {"rs_line_new_high", "rs_line_leads"} <= hit, "RS line at a high, price still below its 120 high"
    up = [100.0] * 280 + [101.0, 104.0]                             # price itself at a new high: not "before price"
    assert "rs_line_leads" not in _hits(_frame(up), _bench([100.0] * 282))


def test_snapshot_flags_the_rs_line_at_a_high():
    snap = T.snapshot(_frame([100.0] * 280 + [101.0, 102.0, 103.0]), _bench([100.0] * 283))
    assert snap["rs_line_at_high"] == 1
    assert T.snapshot(_frame([100.0] * 283))["rs_line_at_high"] is None, "no benchmark, no RS line"


# ── size groups ───────────────────────────────────────────────────────────

def test_cap_rank_uses_amfi_cut_offs_and_ranks_rs_within_each_group():
    from research import tech_signals as S
    snaps = {f"S{i:03d}": {"_rs_raw": float((i * 7) % 307)} for i in range(300)}
    mcaps = {f"S{i:03d}": 100000.0 - i for i in range(300)}          # S000 biggest
    S.cap_rank(snaps, mcaps)
    groups = {b: [s for s, v in snaps.items() if v["cap_bucket"] == b] for b in ("LARGE", "MID", "SMALL")}
    assert len(groups["LARGE"]) == 100 and len(groups["MID"]) == 150 and len(groups["SMALL"]) == 50
    assert "S000" in groups["LARGE"] and "S100" in groups["MID"] and "S250" in groups["SMALL"]
    strongest_mid = max(groups["MID"], key=lambda s: snaps[s]["_rs_raw"])
    assert snaps[strongest_mid]["rs_rating_cap"] == 99
    snaps2 = {"A": {"_rs_raw": 1.0}}
    S.cap_rank(snaps2, {})
    assert snaps2["A"]["cap_bucket"] is None and snaps2["A"]["rs_rating_cap"] is None


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    yield conn
    conn.close()


def test_market_caps_use_the_latest_shares_on_or_before_the_date(db):
    from research import tech_signals as S
    for sym, pe, sh in (("A", "2026-03-31", 1e8), ("A", "2026-06-30", 2e8), ("A", "2026-12-31", 9e9), ("B", "2026-06-30", 5e7)):
        db.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, shares_out) VALUES (?,?,?,?)",
                   (sym, pe, pe, sh))
    db.commit()
    caps = S.market_caps(db, ["A", "B", "C"], dt.date(2026, 10, 6), {"A": 100.0, "B": 50.0, "C": 10.0})
    assert caps == {"A": pytest.approx(2000.0), "B": pytest.approx(250.0)}, "future shares and missing ones ignored"


def test_run_technical_stores_size_group_and_rs_line(db):
    from research import tech_signals as S
    days = pd.bdate_range(end="2026-10-06", periods=300)
    for sym, slope in (("BIG", 0.5), ("SMALLCO", 0.2)):
        for k, d in enumerate(days):
            c = 100 + slope * k
            db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,1e5,"
                       "'dhan')", (sym, str(d.date()), c, c * 1.005, c * 0.995, c))
    for k, d in enumerate(days):
        db.execute("INSERT INTO market_health (date, nifty_close) VALUES (?, ?)", (str(d.date()), 20000 + 5 * k))
    db.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, shares_out) VALUES "
               "('BIG', 'Q1', '2026-06-30', 1e9)")
    db.execute("INSERT INTO fundamental_data (symbol, quarter, period_end, shares_out) VALUES "
               "('SMALLCO', 'Q1', '2026-06-30', 1e6)")
    db.commit()
    S.run_technical(["BIG", "SMALLCO"], as_of=days[-1].date(), conn=db)
    snap = S.latest_snapshot(db, ["BIG", "SMALLCO"])
    assert snap["BIG"]["cap_bucket"] == "LARGE" and snap["SMALLCO"]["cap_bucket"] == "LARGE", "2 stocks: both top 100"
    assert snap["BIG"]["rs_rating_cap"] == 99 and snap["BIG"]["rs_line_at_high"] in (0, 1)


def test_screener_fields_and_presets():
    from research import screener as SC
    presets = {p["key"]: p for p in SC.PRESETS}
    rows = [{"symbol": "A", "rs_rating_cap": 95, "cap_bucket": "MID", "scan_rs_line_leads": 1},
            {"symbol": "B", "rs_rating_cap": 60, "cap_bucket": "SMALL", "scan_rs_line_leads": 0}]
    assert [r["symbol"] for r in SC.run_screen(None, presets["t_rs_in_size"]["query"], rows=rows)["rows"]] == ["A"]
    assert [r["symbol"] for r in SC.run_screen(None, presets["t_rs_line_leads"]["query"], rows=rows)["rows"]] == ["A"]
    assert SC.run_screen(None, 'cap_bucket = "SMALL"', rows=rows)["count"] == 1
    assert SC.field_key("size") == "cap_bucket"
