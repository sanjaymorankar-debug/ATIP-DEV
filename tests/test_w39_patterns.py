"""
W39 Phase 2 (item 4): chart patterns from swing pivots -- Darvas box, VCP, double bottom, ascending
triangle, head and shoulders -- found on the bars before today and broken on today's close; the
scans, screener fields and the alert hold-back. Each price path is built so the pattern is unambiguous.
"""
import numpy as np
import pandas as pd
import pytest

from research import patterns as P
from research import technicals as T


def _frame(closes, vols=None, start="2025-01-06"):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    v = np.asarray(vols, dtype=float) if vols is not None else np.full(len(c), 1e5)
    df = pd.DataFrame({"open": o, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": v},
                      index=pd.bdate_range(start, periods=len(c)))
    return T.indicators(df)


def _leg(a, b, n):
    """n bars from just after a to b."""
    return list(np.linspace(a, b, n + 1)[1:])


# ── Darvas box ────────────────────────────────────────────────────────────

BOX = list(np.linspace(80, 108, 60)) + [110, 106, 103, 101, 100.5, 102, 104, 106, 105, 107, 108, 106]


def test_darvas_box_and_breakout():
    d = _frame(BOX + [112])
    box = P.darvas(d)
    assert box["top"] == pytest.approx(110 * 1.005, abs=0.01) and box["bottom"] == pytest.approx(100.5 * 0.995, abs=0.01)
    assert box["days"] == 12 and box["height_pct"] == pytest.approx(10.6, abs=0.1)
    reason = P.breakout(d, "darvas_breakout")
    assert reason and "Darvas box" in reason and "closed above the top" in reason
    assert P.breakout(_frame(BOX + [109]), "darvas_breakout") is None, "still inside the box"


def test_darvas_breakdown_and_no_box_in_a_steady_climb():
    assert "below the bottom" in P.breakout(_frame(BOX + [98]), "darvas_breakdown")
    assert P.darvas(_frame(list(np.linspace(80, 120, 80)))) is None, "every bar a new high: no box"


# ── VCP ───────────────────────────────────────────────────────────────────

VCP = (list(np.linspace(50, 100, 220)) + _leg(100, 80, 8) + _leg(80, 98, 8) + _leg(98, 89, 5) + _leg(89, 97, 5)
       + _leg(97, 93, 4) + _leg(93, 96, 4) + [95, 95.5, 95, 95.5])
VCP_VOL = [1e5] * (len(VCP) - 10) + [5e4] * 10


def test_vcp_contractions_dry_up_and_breakout_on_volume():
    d = _frame(VCP + [99], VCP_VOL + [3e5])
    x = P.vcp(d)
    assert x["count"] == 3 and x["contractions_pct"][0] > x["contractions_pct"][1] > x["contractions_pct"][2]
    assert x["pivot"] == pytest.approx(97 * 1.005, abs=0.01) and x["volume_dry_up"] < 0.85
    assert "VCP: 3 contractions" in P.breakout(d, "vcp_breakout")
    assert P.breakout(_frame(VCP + [99], VCP_VOL + [1e5]), "vcp_breakout") is None, "needs 1.4x volume"


def test_vcp_rejects_widening_pullbacks_and_no_dry_up():
    wide = (list(np.linspace(50, 100, 220)) + _leg(100, 95, 5) + _leg(95, 99, 5) + _leg(99, 88, 6)
            + _leg(88, 98, 6) + _leg(98, 80, 8) + _leg(80, 96, 8) + [95, 95.5, 95, 95.5])
    assert P.vcp(_frame(wide + [97], [1e5] * (len(wide) - 10) + [5e4] * 10 + [3e5])) is None
    assert P.vcp(_frame(VCP + [96])) is None, "volume never dried up"


# ── double bottom, ascending triangle, head and shoulders ─────────────────

DOUBLE = list(np.linspace(125, 100, 40)) + _leg(100, 108, 8) + _leg(108, 101, 8) + _leg(101, 106, 6)


def test_double_bottom_breakout():
    d = _frame(DOUBLE + [110])
    x = P.double_bottom(d)
    assert x["neckline"] == pytest.approx(108 * 1.005, abs=0.01) and x["days"] == 16
    assert "Double bottom" in P.breakout(d, "double_bottom_breakout")
    uneven = list(np.linspace(125, 100, 40)) + _leg(100, 112, 8) + _leg(112, 108.5, 8) + _leg(108.5, 110, 6)
    assert P.double_bottom(_frame(uneven + [111])) is None, "lows 8% apart are not a double bottom"


TRIANGLE = (list(np.linspace(30, 50, 40)) + _leg(50, 44, 5) + _leg(44, 50, 5) + _leg(50, 46, 4) + _leg(46, 50, 4)
            + _leg(50, 48, 3) + _leg(48, 49.5, 3))


def test_ascending_triangle_breakout():
    d = _frame(TRIANGLE + [51.5])
    x = P.ascending_triangle(d)
    assert x["touches"] == 3 and x["resistance"] == pytest.approx(50 * 1.005, abs=0.01)
    assert x["rising_lows"] == sorted(x["rising_lows"]) and len(x["rising_lows"]) == 3
    assert "Ascending triangle" in P.breakout(d, "ascending_triangle_breakout")


HS = (list(np.linspace(80, 100, 40)) + _leg(100, 110, 5) + _leg(110, 100, 5) + _leg(100, 120, 6) + _leg(120, 101, 6)
      + _leg(101, 111, 5) + [108, 105, 103, 102])


def test_head_and_shoulders_breakdown():
    d = _frame(HS + [98])
    x = P.head_shoulders(d)
    assert x["head"] == pytest.approx(120 * 1.005, abs=0.01) and x["left_shoulder"] == pytest.approx(110 * 1.005, abs=0.01)
    assert 100 < x["neckline_today"] < 102
    assert "Head and shoulders" in P.breakout(d, "head_shoulders_breakdown")
    assert P.breakout(_frame(HS + [103]), "head_shoulders_breakdown") is None


# ── scans, snapshot, screener ─────────────────────────────────────────────

def test_pattern_breakouts_are_scans_with_their_levels_in_the_reason():
    hits = {h[0]: h for h in T.run_scans(_frame(VCP + [99], VCP_VOL + [3e5]))}
    assert "vcp_breakout" in hits and hits["vcp_breakout"][2] == "BULL" and "pivot" in hits["vcp_breakout"][3]
    assert T.PATTERN_SCANS <= set(T.SCANS)


def test_snapshot_lists_patterns_in_place():
    df = _frame(VCP, VCP_VOL)[["open", "high", "low", "close", "volume"]]
    snap = T.snapshot(df)
    assert snap["vcp_setup"] == 1 and "VCP setup, pivot" in snap["chart_patterns"]
    snap2 = T.snapshot(_frame(BOX)[["open", "high", "low", "close", "volume"]])
    assert "Darvas box" in (snap2["chart_patterns"] or "") and snap2["vcp_setup"] == 0


def test_screener_fields_and_presets():
    from research import screener as SC
    presets = {p["key"]: p for p in SC.PRESETS}
    rows = [{"symbol": "A", "vcp_setup": 1, "chart_patterns": "VCP setup, pivot 97.49 (3 contractions)"},
            {"symbol": "B", "vcp_setup": 0, "scan_darvas_breakout": 1}]
    assert [r["symbol"] for r in SC.run_screen(None, presets["t_vcp_setups"]["query"], rows=rows)["rows"]] == ["A"]
    assert [r["symbol"] for r in SC.run_screen(None, presets["t_pattern_breakouts"]["query"], rows=rows)["rows"]] == ["B"]
    assert SC.run_screen(None, 'chart_patterns CONTAINS "vcp"', rows=rows)["count"] == 1


# ── alerts wait for a record ──────────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    yield conn
    conn.close()


def _sig(conn, sid, scan, d, status="OPEN", r=None, confluence=5):
    conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, "
                 "confluence, status, r_multiple, market_gate, alignment) VALUES (?, ?, ?, ?, ?, 'BULL', 100, 96, 108, "
                 "?, ?, ?, 'OPEN', 'WITH')", (sid, sid, d, scan, scan, confluence, status, r))


def test_pattern_scans_alert_only_after_a_positive_record(db, monkeypatch):
    from research import tech_signals as S
    import alerts.telegram as TG
    sent = []
    monkeypatch.setattr(TG, "notify", lambda text, **k: sent.append(text))
    _sig(db, "today_vcp", "vcp_breakout", "2026-10-06")
    _sig(db, "today_gc", "golden_cross", "2026-10-06")
    db.commit()
    out = S.alert_top(db, "2026-10-06")
    assert out["alerted"] == 1 and "golden_cross" in sent[-1] and "vcp_breakout" not in sent[-1]
    held = {o["scan"]: o for o in S.scan_stats(db)}
    assert held["vcp_breakout"]["alerts"] == "held" and held["golden_cross"]["alerts"] == "on"
    for i in range(30):
        _sig(db, f"old{i}", "vcp_breakout", "2026-06-01", status="TARGET" if i % 2 else "STOPPED",
             r=2.0 if i % 2 else -1.0)
    db.commit()
    assert S.proven_scans(db) == {"vcp_breakout"}
    sent.clear()
    assert S.alert_top(db, "2026-10-06")["alerted"] == 2 and "vcp_breakout" in sent[-1]
