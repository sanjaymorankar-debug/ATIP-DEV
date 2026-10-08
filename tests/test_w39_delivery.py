"""
W39 Phase 2 (item 6): NSE delivery % (already stored in prices_daily from the bhavcopy) as a technical
field and a "delivery spike on an up day" scan.
"""
import numpy as np
import pandas as pd
import pytest

from research import technicals as T


def _frame(closes, deliv=None, start="2025-01-06"):
    c = np.asarray(closes, dtype=float)
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c * 1.005, "low": c * 0.995, "close": c,
                       "volume": np.full(len(c), 1e5)}, index=pd.bdate_range(start, periods=len(c)))
    if deliv is not None:
        df["delivery_pct"] = np.asarray(deliv, dtype=float)
    return df


def _hits(df):
    return {h[0] for h in T.run_scans(T.indicators(df))}


def test_delivery_spike_needs_the_ratio_the_level_and_an_up_day():
    closes = [100.0] * 80
    assert "delivery_spike_up" in _hits(_frame(closes + [101.0], [40.0] * 80 + [70.0]))
    assert "delivery_spike_up" not in _hits(_frame(closes + [99.0], [40.0] * 80 + [70.0])), "down day"
    assert "delivery_spike_up" not in _hits(_frame(closes + [101.0], [40.0] * 80 + [55.0])), "only 1.4x"
    assert "delivery_spike_up" not in _hits(_frame(closes + [101.0], [15.0] * 80 + [25.0])), "below 30 %"
    assert "delivery_spike_up" not in _hits(_frame(closes + [101.0])), "no delivery data"


def test_snapshot_carries_delivery_fields():
    snap = T.snapshot(_frame([100.0] * 80 + [101.0], [40.0] * 80 + [70.0]))
    assert snap["delivery_pct"] == 70.0 and snap["delivery_ratio"] == pytest.approx(1.75)
    bare = T.snapshot(_frame([100.0] * 81))
    assert bare["delivery_pct"] is None and bare["delivery_ratio"] is None


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    yield conn
    conn.close()


def test_load_bars_and_run_store_delivery(db):
    from research import tech_signals as S
    days = pd.bdate_range(end="2026-10-06", periods=81)
    for k, d in enumerate(days):
        c, dp = (100.0, 40.0) if k < 80 else (101.0, 70.0)
        db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,delivery_pct,source) VALUES "
                   "('DLV',?,?,?,?,?,1e5,?,'nse')", (str(d.date()), c, c, c, c, dp))
    db.commit()
    bars = S.load_bars(db, ["DLV"], days[-1].date())
    assert bars["DLV"]["delivery_pct"].iloc[-1] == 70.0
    S.run_technical(["DLV"], as_of=days[-1].date(), conn=db)
    snap = S.latest_snapshot(db, ["DLV"])["DLV"]
    assert snap["delivery_pct"] == 70.0 and snap["delivery_ratio"] == pytest.approx(1.75)
    assert any(s["scan"] == "delivery_spike_up" for s in S.todays_signals(db, str(days[-1].date())))


def test_screener_preset():
    from research import screener as SC
    p = {x["key"]: x for x in SC.PRESETS}["t_delivery_spike"]
    rows = [{"symbol": "A", "scan_delivery_spike_up": 1, "delivery_ratio": 1.8},
            {"symbol": "B", "scan_delivery_spike_up": 0, "delivery_ratio": 1.1}]
    assert [r["symbol"] for r in SC.run_screen(None, p["query"], rows=rows)["rows"]] == ["A"]
    assert SC.field_key("deliv_pct") == "delivery_pct"
