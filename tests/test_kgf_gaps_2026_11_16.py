"""The gaps the 16 Nov 2026 audit of KGF-01..21 found, closed (owner "Ok", 16 Nov 2026).

    sector cap (S8)                 at most 2 open or queued per industry across the sessions; the skip is recorded once
    re-entry cooldown (KGF-19)      off by default; when an arm sets it, a stopped symbol is skipped and recorded
    weekly A/B report               counts falling / sector / cooldown skips and the re-entries within an hour of a stop
    gate closed                     exits keep running: a stop leg fires while the arm's breadth gate holds entries out
    stop slippage (KGF-17)          a PAPER stop exit takes paper_stop_slippage_bps (default 3 x 5 = 15 bps) in all
    resting Kite buy (KGF-11)       valid for 10 trading sessions, the day it is made counting before the close

Orders run orders.rules -> orders.broker -> orders.paper for real on a throwaway database (the `paper` fixture)."""

import json
from datetime import date, datetime, timedelta

from tests.test_kite_formula_fix_2026_11_16 import (TODAY, _breadth, _conn, _do, _no_dhan, _pick,  # noqa: F401
                                                    _sessions, _signals)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

IT = {"ACME": "IT", "BETA": "IT", "GAMMA": "IT", "DELTA": "Banks"}


def _skips(sid=None):
    c = _conn()
    rows = [json.loads(r[0]) for r in c.execute(
        "SELECT detail FROM session_events WHERE event = 'AUTO_PICK_SKIP' AND (? IS NULL OR session_id = ?)", (sid, sid))]
    c.close()
    return rows


# -- S8 sector cap -------------------------------------------------------------------------------------------------------

def test_sector_full_counts_other_symbols_of_the_same_industry(monkeypatch):
    from orders import autopick
    monkeypatch.setattr("orders.entry_guards._industry", IT.get)
    assert "IT already has 2 (BETA, GAMMA; cap 2)" in autopick._sector_full("ACME", {"BETA", "GAMMA", "DELTA"}, 2)
    assert autopick._sector_full("ACME", {"BETA", "DELTA"}, 2) is None          # one IT name: room for another
    assert autopick._sector_full("ACME", {"ACME", "BETA"}, 2) is None           # the symbol itself does not count
    assert autopick._sector_full("ACME", {"BETA", "GAMMA"}, None) is None       # no cap on the arm
    assert autopick._sector_full("ZETA", {"BETA", "GAMMA"}, 2) is None          # no industry known: no cap


def test_a_full_sector_is_skipped_and_recorded_once_a_day(paper, monkeypatch):
    from orders import arms
    from orders.paper import ensure_tables
    monkeypatch.setattr("orders.entry_guards._industry", IT.get)
    _signals(ACME=(80, "BUY", 100.0), DELTA=(70, "BUY", 50.0))
    c = _conn()
    ensure_tables(c)
    for sym in ("BETA", "GAMMA"):                                              # two IT names already held
        c.execute("INSERT INTO paper_position (symbol, quantity, avg_price, updated_at) VALUES (?, 10, 100, ?)",
                  (sym, datetime.now().isoformat()))
    c.commit(); c.close()
    _sessions("S8")
    _do(arms.set_arm, "S8", arms.ARMS["S8"])
    _breadth(80.7, 56.8)
    assert _pick()["S8"]["symbol"] == "DELTA"
    _pick()
    rows = _skips("S8")
    assert len(rows) == 1 and (rows[0]["status"], rows[0]["symbol"]) == ("skipped_sector", "ACME")
    assert "cap 2" in rows[0]["reason"]


# -- KGF-19 re-entry cooldown --------------------------------------------------------------------------------------------

def _stopped(sym, minutes_ago):
    from orders import rules
    r = rules.create_rule({"symbol": sym, "side": "SELL", "trigger_type": "PRICE", "trigger_value": 90.0,
                           "reference_price": 100.0, "trigger_direction": "BELOW", "quantity_type": "FIXED",
                           "quantity_value": 1, "product_type": "CNC", "order_type": "MARKET"})
    c = _conn()
    c.execute("UPDATE order_rules SET role = 'STOP', status = 'EXECUTED', updated_at = ? WHERE id = ?",
              ((datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"), r["id"]))
    c.commit(); c.close()


def test_the_cooldown_is_off_by_default_and_skips_a_stopped_symbol_when_set(paper):
    from orders import arms, autopick
    _stopped("ACME", 10)
    c = _conn()
    assert autopick._cooled(c, "ACME", arms.setting(arms.ARMS["S1"], "reentry_cooldown_min")) is None   # default off
    assert "ACME was stopped out at" in autopick._cooled(c, "ACME", 30)
    assert autopick._cooled(c, "ACME", 5) is None                              # stopped 10 min ago: outside 5 min
    c.close()
    _signals(ACME=(80, "BUY", 100.0), DELTA=(70, "BUY", 50.0))
    _sessions("S1")
    _do(arms.set_arm, "S1", {**arms.ARMS["S1"], "reentry_cooldown_min": 30})
    _breadth(80.7, 56.8)
    assert _pick()["S1"]["symbol"] == "DELTA"
    rows = _skips("S1")
    assert len(rows) == 1 and (rows[0]["status"], rows[0]["symbol"]) == ("skipped_cooldown", "ACME")


# -- the weekly A/B report -----------------------------------------------------------------------------------------------

def test_the_ab_report_counts_every_kind_of_skip(paper):
    from orders import sessions
    from portfolio import ab_report
    c = _conn()
    sessions.ensure_tables(c)
    for status, sym in (("skipped_falling", "ACME"), ("skipped_sector", "BETA"), ("skipped_sector", "GAMMA"),
                        ("skipped_cooldown", "DELTA")):
        sessions._event(c, "S8", "AUTO_PICK_SKIP", {"status": status, "symbol": sym})
    sessions._event(c, "S8", "AUTO_PICK_WAIT_LTP", {"status": "waiting_for_ltp"})
    sessions._event(c, "S8", "AUTO_PICK_GATE", {"gate": {"open": False}})
    c.commit()
    st = ab_report.arm_stats(c, "S8", "2000-01-01T00:00:00", "2999-12-31T23:59:59")
    c.close()
    assert (st["skipped"], st["skipped_falling"], st["skipped_sector"], st["skipped_cooldown"], st["gate_days"]) == \
        (5, 1, 2, 1, 1)
    assert (st["reentries"], st["reentry_net"]) == (0, 0)


def test_the_ab_report_measures_re_entries_within_an_hour_of_a_stop(paper):
    from portfolio import ab_report
    _stopped("ACME", 90)                                                     # a stop 90 min ago
    now = datetime.now()
    trades = [{"symbol": "ACME", "at": (now - timedelta(minutes=60)).isoformat(timespec="seconds"), "pnl": -120.0},
              {"symbol": "ACME", "at": (now - timedelta(minutes=95)).isoformat(timespec="seconds"), "pnl": 50.0},
              {"symbol": "BETA", "at": (now - timedelta(minutes=60)).isoformat(timespec="seconds"), "pnl": 70.0},
              {"symbol": "ACME", "at": now.isoformat(timespec="seconds"), "pnl": 10.0}]
    c = _conn()
    out = ab_report._reentries(c, trades)
    c.close()
    # 30 min after the stop: a re-entry; before the stop, another symbol, 90 min after: not
    assert out == {"reentries": 1, "reentry_net": -120.0}


# -- the gate holds entries, never exits ---------------------------------------------------------------------------------

def test_a_stop_leg_fires_while_the_gate_is_closed(paper):
    from orders import arms, rules, sessions
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    _do(arms.set_arm, "S1", arms.ARMS["S1"])
    _breadth(80.7, 56.8)
    e = _pick()["S1"]
    paper.prices["ACME"] = 100.0
    rules.check_triggers()
    entry = rules.get_rule(e["rule_id"])
    assert entry["status"] == "EXECUTED"
    stop = next(l for l in rules.list_rules() if l.get("parent_rule_id") == entry["id"] and l["role"] == "STOP")
    _breadth(20.0, 12.0, on=TODAY)                                           # the gate closes
    c = _conn()
    assert arms.gate(c, arms.get(c, "S1"))["open"] is False
    assert sessions.gate(c, stop) is None                                    # an exit leg always passes the session gate
    c.close()
    paper.prices["ACME"] = float(stop["resolved_trigger_price"]) - 1
    rules.check_triggers()
    assert rules.get_rule(stop["id"])["status"] == "EXECUTED"


# -- KGF-17 stop slippage ------------------------------------------------------------------------------------------------

def test_a_paper_stop_takes_the_stop_slippage(paper, monkeypatch):
    import orders.paper as P
    from orders import rules
    real = P.settings
    monkeypatch.setattr(P, "settings", lambda o=None: {**real(o), "paper_slippage_bps": 5, "paper_stop_slippage_bps": None})
    assert rules._stop_extra_bps() == 10.0                                    # 15 bps in all, 5 of them the broker's
    stop_leg = {"role": "STOP", "side": "SELL", "order_type": "MARKET"}
    assert rules._paper_reference(stop_leg, 100.0) == 99.9
    monkeypatch.setattr(P, "settings", lambda o=None: {**real(o), "paper_slippage_bps": 5, "paper_stop_slippage_bps": 20})
    assert rules._stop_extra_bps() == 15.0
    assert rules._paper_reference({"role": "TARGET", "side": "SELL", "order_type": "MARKET"}, 100.0) is None
    assert rules._paper_reference({"role": "ENTRY", "side": "BUY", "order_type": "LIMIT"}, 100.0) == 100.0


# -- KGF-11 validity -----------------------------------------------------------------------------------------------------

def _sessions_in(start: date, end: str) -> int:
    from utils.trading_calendar import is_trading_day
    d, n = start, 0
    while d.isoformat() <= end:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return n


def test_a_resting_buy_lasts_ten_trading_sessions():
    from orders.algo_gtt import _valid_until
    from utils.trading_calendar import is_trading_day
    day = date(2026, 11, 16)                                                  # a Monday, a trading day
    assert is_trading_day(day)
    morning = _valid_until(day, 10, datetime(2026, 11, 16, 10, 0))
    evening = _valid_until(day, 10, datetime(2026, 11, 16, 16, 0))
    assert _sessions_in(day, morning) == 10                                   # 16 Nov is the first of the ten
    assert _sessions_in(day + timedelta(days=1), evening) == 10               # after the close: 17 Nov is the first
    assert evening > morning
    sat = date(2026, 11, 21)
    assert _sessions_in(sat, _valid_until(sat, 10, datetime(2026, 11, 21, 10, 0))) == 10
