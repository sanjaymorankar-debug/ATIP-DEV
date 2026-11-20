"""
Session review, 20 Nov 2026 (owner: "Check today's ATIP trading sessions ... analyze the whole 1 month failures ...
and then make the corrections to the code"). Report: docs/reports/2026-11-20-session-review/SESSION_REVIEW.md.

SR-01  the daily RCA headline says how much was lost in all and that the cause totals overlap (16 Nov read Rs -1,203
       under each of three causes for Rs 1,203 lost)
SR-02  the weekly A/B report names a position bought before the session's arm was set: one position per session, so it
       keeps the arm from trading (S2 INDHOTEL from 13 Nov, S7 ICICIBANK from 12 Nov, no pick in the arms' first week)
"""
from datetime import date

from tests.test_kite_formula_fix_2026_11_16 import _conn, _sessions  # noqa: F401
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)


# ── SR-01 ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_rca_headline_says_what_was_lost_and_that_causes_overlap():
    from portfolio import post_market_rca as R
    tot = {"net": -66.01, "closed": 4, "day_mtm": 262.9}
    three = [{"code": k} for k in "ABC"]
    losses = [{"pnl_net": -612.46, "causes": three}, {"pnl_net": -590.42, "causes": three}]    # 16 Nov: SAILIFE, M&M
    by_cause = {k: {"code": k, "cause": n, "rupees": -1202.88}
                for k, n in (("A", "Signal quality"), ("B", "Stop-loss design"), ("C", "Position sizing / correlation"))}
    h = R._headline(tot, losses, by_cause, [])
    assert "Signal quality (Rs -1,203)" in h
    assert "Rs 1,203 lost in 2 trade(s) in all; a trade is counted under each of its causes, so these overlap" in h
    # two losses with one different cause each: the totals add up, nothing overlaps
    apart = [{"pnl_net": -612.46, "causes": [{"code": "A"}]}, {"pnl_net": -590.42, "causes": [{"code": "B"}]}]
    two = R._headline(tot, apart, {"A": {"code": "A", "cause": "Signal quality", "rupees": -612.46},
                                   "B": {"code": "B", "cause": "Stop-loss design", "rupees": -590.42}}, [])
    assert "overlap" not in two and "Stop-loss design (Rs -590)" in two
    assert "Top causes: none." in R._headline({"net": 10.0, "closed": 1, "day_mtm": 5.0}, [], {}, [])


# ── SR-02 ────────────────────────────────────────────────────────────────────────────────────────────────────────

TRADES = [{"symbol": "INDHOTEL", "date": "2026-11-13", "at": "2026-11-13T12:16:07", "closed": False, "pnl": 0.0,
           "value": 23907.0},
          {"symbol": "ACME", "date": "2026-11-10", "at": "2026-11-10T09:15:00", "closed": True, "pnl": 50.0,
           "value": 25000.0, "exit_date": "2026-11-11"}]


def _fake_trades(monkeypatch, trades):
    from orders import sessions
    monkeypatch.setattr(sessions, "_linked_rules", lambda conn, sid: [])
    monkeypatch.setattr(sessions, "_trades",
                        lambda rules, charges=None, since=None: [t for t in trades if not since or t["at"] >= since])


def test_the_ab_report_names_a_position_bought_before_the_arm(paper, monkeypatch):
    from orders import sessions
    from portfolio import ab_report
    _fake_trades(monkeypatch, TRADES)
    c = _conn()
    sessions.ensure_tables(c)
    st = ab_report.arm_stats(c, "S2", "2026-11-16T00:52:59", "2026-11-22T23:59:59")
    after = ab_report.arm_stats(c, "S2", "2026-11-01T00:00:00", "2026-11-22T23:59:59")
    c.close()
    assert st["held_from_before"] == "INDHOTEL (bought 2026-11-13)"
    assert (st["trades"], st["open"]) == (0, 0)              # nothing bought under the arm yet
    assert after["held_from_before"] is None                 # bought after the arm: the arm's own open trade
    assert (after["trades"], after["open"]) == (1, 1)


def test_the_weekly_verdict_says_the_arm_is_blocked(paper, monkeypatch):
    from orders import arms
    from portfolio import ab_report
    _sessions("S2")
    c = _conn()
    arms.set_arm(c, "S2", arms.ARMS["S2"], reason="test")    # the ARM event: now, after INDHOTEL's buy
    c.commit()
    _fake_trades(monkeypatch, TRADES)
    rep = ab_report.build(c, date.today().isoformat())
    c.close()
    row = next(r for r in rep["arms"] if r["session"] == "S2")
    assert row["verdict"].startswith("blocked: holds INDHOTEL (bought 2026-11-13), bought before the arm (old exits); "
                                     "collecting: 0/30 trades")
