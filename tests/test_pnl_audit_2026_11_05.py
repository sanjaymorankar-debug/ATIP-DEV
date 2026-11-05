"""Profit audit, 5 Nov 2026: realised P&L of the ten cash-pool sessions (orders/sessions.py _trades / usage / ledger,
accounting/cycles.py and periods.py, portfolio/paper_report.py, portfolio/pnl.py) -- long and short, partial exits,
charges, the IST day, session attribution, duplicates, None -- and the three fixes:

    PA-01  the LIVE book's cash is the real Dhan account's, never the paper simulator's; day P&L compares like with like
    PA-02  a partial fill records (and protects) the quantity that filled
    PA-03  a trade sold in parts over two days books each part on its own day

Every expected figure is worked out by hand beside it."""

import importlib.util
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from tests.test_w39_qa_suite import _rule, paper  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[1]


def _conn():
    from db.schema import get_connection
    return get_connection()


def _do(fn, *a, **k):
    c = _conn()
    try:
        return fn(c, *a, **k)
    finally:
        c.close()


def _entry_rule(rid="E1", side="BUY", px=100.0, qty=10, at="2026-11-04T09:16:00"):
    return {"id": rid, "role": "ENTRY", "side": side, "symbol": "ACME", "status": "EXECUTED", "execution_price": px,
            "execution_quantity": qty, "updated_at": at, "parent_rule_id": None}


def _leg(rid, px, qty, at, role="TARGET", status="EXECUTED", parent="E1", side="SELL"):
    return {"id": rid, "role": role, "side": side, "symbol": "ACME", "status": status,
            "execution_price": px if status == "EXECUTED" else None,
            "execution_quantity": qty if status == "EXECUTED" else None, "quantity_value": qty,
            "updated_at": at, "parent_rule_id": parent}


# ── (a) long and short: (exit - entry) x qty for a long, reversed for a short; only closed quantity is realised ─────

def test_long_trade_realises_exit_minus_entry_net_of_both_sides_charges():
    from orders.sessions import _trades
    t, = _trades([_entry_rule(), _leg("T", 103.0, 10, "2026-11-05T10:00:00")], {"E1": 5.0, "T": 7.0})
    assert t["closed"] and t["held_qty"] == 0
    assert t["pnl"] == 18.0                                      # 10 x (103 - 100) = 30, less 7 sold + 5 bought
    assert t["charges"] == 12.0 and t["exit_price"] == 103.0
    assert t["realised_by_day"] == {"2026-11-05": 18.0}


def test_short_trade_realises_entry_minus_exit():
    from orders.sessions import _trades
    won, = _trades([_entry_rule(side="SELL"), _leg("T", 97.0, 10, "2026-11-05T10:00:00", side="BUY")])
    lost, = _trades([_entry_rule(side="SELL"), _leg("S", 104.0, 10, "2026-11-05T10:00:00", "STOP", side="BUY")])
    assert won["pnl"] == 30.0                                    # 10 x (100 - 97)
    assert lost["pnl"] == -40.0                                  # 10 x (100 - 104)


def test_open_quantity_is_not_realised():
    from orders.sessions import _trades
    t, = _trades([_entry_rule(), _leg("S", None, 10, "2026-11-04T09:16:00", "STOP", status="ACTIVE")], {"E1": 5.0})
    assert not t["closed"] and t["pnl"] == 0 and t["held_qty"] == 10
    assert t["held_cost"] == 1005.0                              # 10 x 100 + the 5 the buy cost
    assert t["realised_by_day"] == {}


# ── (b) partial exits: each part against the one entry price, the entry's charges shared per share ────────────────

def test_partial_exit_books_the_part_sold_with_its_share_of_the_buy_charges():
    from orders.sessions import _trades
    rules = [_entry_rule(), _leg("T1", 110.0, 4, "2026-11-04T11:00:00"),
             _leg("S", None, 6, "2026-11-04T09:16:00", "STOP", status="ACTIVE")]
    t, = _trades(rules, {"E1": 10.0, "T1": 2.0})
    assert not t["closed"] and t["held_qty"] == 6
    assert t["pnl"] == 34.0                                      # 4 x 10 = 40, less 2 sold, less 4 x Rs 1 of the buy
    assert t["held_cost"] == 606.0                               # 6 x 100 + 6 x Rs 1 of the buy charges


def test_a_trade_sold_in_parts_over_two_days_books_each_part_on_its_day(monkeypatch):
    """PA-03: usage() counted the first day's part again on the day of the last one."""
    from orders import sessions as S
    rules = [_entry_rule(), _leg("T1", 110.0, 4, "2026-11-04T11:00:00"), _leg("T2", 90.0, 6, "2026-11-05T10:00:00")]
    monkeypatch.setattr(S, "_linked_rules", lambda conn, sid: rules)
    monkeypatch.setattr(S, "_charges", lambda conn, r: {})
    assert S.usage(None, "S1", on="2026-11-04")["realised_today"] == 40.0      # 4 x (110 - 100)
    assert S.usage(None, "S1", on="2026-11-05")["realised_today"] == -60.0     # 6 x (90 - 100); was -20.0
    t, = S._trades(rules)
    assert t["pnl"] == -20.0 and sum(t["realised_by_day"].values()) == t["pnl"]


def test_fill_based_report_uses_the_average_cost_and_skips_a_sale_with_nothing_held():
    from portfolio.paper_report import _replay
    f = lambda side, q, px, fee, at: {"order_id": at, "symbol": "ACME", "side": side, "qty": q, "price": px,
                                      "fees": fee, "at": at, "session": "S1"}
    trips, events = _replay([f("BUY", 10, 100.0, 10.0, "2026-11-04T09:15"), f("BUY", 10, 110.0, 10.0, "2026-11-04T09:20"),
                             f("SELL", 5, 120.0, 4.0, "2026-11-05T10:00"), f("SELL", 15, 100.0, 12.0, "2026-11-05T11:00"),
                             f("SELL", 3, 100.0, 1.0, "2026-11-05T12:00")])
    # average cost (2,100 + 20) / 20 = 106 a share: 5 x 120 - 4 - 530 = 66; 15 x 100 - 12 - 1,590 = -102
    assert [round(e["pnl"], 2) for e in events] == [66.0, -102.0]
    assert trips[0]["closed"] and round(trips[0]["net"], 2) == -36.0
    assert len(events) == 2                                      # the 3 sold with nothing held book nothing


# ── (c) charges: the statutory NSE delivery rates, gross and net kept apart ───────────────────────────────────────

def test_delivery_charges_are_the_statutory_rates():
    from backtest.costs import cost_model
    m = cost_model("nse_delivery")
    buy, sell = m.charges("BUY", 100000), m.charges("SELL", 100000)
    # STT 0.1 % = 100; NSE 0.00297 % = 2.97; SEBI Rs 10 / crore = 0.10; stamp 0.015 % (buy) = 15;
    # GST 18 % of (2.97 + 0.10) = 0.5526; DP Rs 15.93 a sale; Dhan delivery brokerage 0
    assert (buy["stt"], buy["exchange"], buy["sebi"], buy["stamp"], buy["gst"], buy["dp"], buy["brokerage"]) == \
        (100.0, 2.97, 0.1, 15.0, 0.5526, 0.0, 0.0)
    assert buy["total"] == 118.6226 and sell["total"] == 119.5526 and sell["stamp"] == 0.0


def test_cycle_figures_keep_gross_charges_net_and_tax_apart():
    from accounting import tax as TX
    from accounting.cycles import figures
    cfg = TX.settings({})
    ch = {"stt": 50.75, "exchange": 1.5, "sebi": 0.05, "stamp": 3.75, "gst": 0.28, "dp": 15.93}
    f = figures(25000, 25000, 25750, ch, "STCG", cfg=cfg)
    assert (f["gross_pnl"], f["charges"], f["net_pnl"]) == (750.0, 72.26, 677.74)
    assert f["tax_est"] == 151.53                                # (677.74 + 50.75 STT, not deductible) x 20 % x 1.04
    assert f["actual_pnl"] == 526.21 and f["end_amount"] == 25677.74          # the pool compounds the net
    loss = figures(25000, 25000, 24500, ch, "STCG", cfg=cfg)
    assert loss["net_pnl"] == -572.26 and loss["tax_est"] == 0.0             # a loss carries no tax


# ── (d) the IST day: the fill's own clock, the calendar day; the processes run on Asia/Kolkata ─────────────────────

def test_a_sale_at_23_59_and_one_at_00_01_fall_on_their_own_days(monkeypatch):
    from orders import sessions as S
    rules = [_entry_rule(), _leg("T1", 110.0, 4, "2026-11-04T23:59:59"), _leg("T2", 110.0, 6, "2026-11-05T00:00:01")]
    monkeypatch.setattr(S, "_linked_rules", lambda conn, sid: rules)
    monkeypatch.setattr(S, "_charges", lambda conn, r: {})
    assert S.usage(None, "S1", on="2026-11-04")["realised_today"] == 40.0
    assert S.usage(None, "S1", on="2026-11-05")["realised_today"] == 60.0


def test_home_today_counts_the_cycles_closed_on_the_ist_day(temp_db):
    from accounting import cycles as C
    from accounting.periods import periods
    c = _conn()
    try:
        C.ensure_tables(c)
        since = "2026-10-29T23:09:38"
        for rid, closed, net in (("A", "2026-11-04T23:59:59", 100.0), ("B", "2026-11-05T00:00:01", 50.0)):
            c.execute("INSERT INTO session_cycles (session_id, n, rule_id, gross_pnl, charges, net_pnl, tax_est, "
                      "actual_pnl, closed_at, pool_started_at) VALUES ('S1', 1, ?, ?, 0, ?, 0, ?, ?, ?)",
                      (rid, net, net, net, closed, since))
        c.commit()
        ov = {"sessions": [{"id": "S1", "tiles": {"unrealised": 0.0}, "setup": {"pool_started_at": since}}]}
        p = periods(c, ov, today=date(2026, 11, 5))
    finally:
        c.close()
    assert (p["today"]["cycles"], p["today"]["realised"]) == (1, 50.0)
    assert (p["since_start"]["cycles"], p["since_start"]["realised"]) == (2, 150.0)


def test_the_trading_and_viewer_processes_run_on_ist():
    """date.today() and every stored timestamp are the process clock: launchd and the image pin it to IST."""
    plist = (ROOT / "deploy/launchd/com.atip.platform.plist").read_text()
    assert "<key>TZ</key>" in plist and "<string>Asia/Kolkata</string>" in plist
    assert "TZ=Asia/Kolkata" in (ROOT / "Dockerfile").read_text()


# ── (e) session attribution: one session per rule, the legs inherit it, nothing counted twice or orphaned ──────────

def _pool(sid, capital=25000.0):
    from orders import sessions
    _do(sessions.seed)
    _do(sessions.start_pool, sid, capital)
    _do(sessions.set_status, sid, "RUNNING")


def _linked_entry(symbol, trigger, sid):
    from orders import rules, sessions
    r = _rule(symbol=symbol, trigger_value=trigger, require_confirmation=False)
    _do(sessions.link_rule, sid, r["id"])
    return rules.get_rule(r["id"])


def _tick(feed, **prices):
    from orders import rules
    feed.prices.update(prices)
    return rules.check_triggers()


def test_every_fill_belongs_to_exactly_one_session_and_the_totals_add_up(paper):
    from orders import rules, sessions
    from portfolio.paper_report import report
    _pool("S1")
    _pool("S2")
    a = _linked_entry("ACME", 100.0, "S1")
    b = _linked_entry("BETA", 50.0, "S2")
    _tick(paper, ACME=100.0, BETA=50.0)                          # both buy with all their cash: 250 and 500 shares
    _tick(paper, ACME=103.0, BETA=49.0)                          # ACME +3 % target; BETA -2 % stop
    s1 = {r["id"] for r in _do(sessions._linked_rules, "S1")}
    s2 = {r["id"] for r in _do(sessions._linked_rules, "S2")}
    legs = [r for r in rules.list_rules() if r.get("parent_rule_id")]
    assert not s1 & s2 and s1 | s2 == {a["id"], b["id"], *(l["id"] for l in legs)}
    assert all(_do(sessions.session_of_rule, l) == ("S1" if l["parent_rule_id"] == a["id"] else "S2") for l in legs)
    c = _conn()
    try:
        tags = {r[0]: r[1] for r in c.execute("SELECT symbol, GROUP_CONCAT(DISTINCT tag) FROM paper_order GROUP BY symbol")}
    finally:
        c.close()
    assert tags == {"ACME": "ATIP-S1", "BETA": "ATIP-S2"}
    l1, l2 = _do(sessions.ledger, "S1"), _do(sessions.ledger, "S2")
    assert (l1["booked"], l2["booked"]) == (750.0, -500.0)       # 250 x 3; 500 x -1
    pools = _do(report, "pools", "day")
    assert pools["booked"] == l1["booked"] + l2["booked"] == 250.0


def test_relinking_a_rule_moves_it_rather_than_counting_it_twice(paper):
    from orders import sessions
    _pool("S1")
    _pool("S2")
    r = _linked_entry("ACME", 100.0, "S1")
    _do(sessions.link_rule, "S2", r["id"])
    assert r["id"] not in {x["id"] for x in _do(sessions._linked_rules, "S1")}
    assert r["id"] in {x["id"] for x in _do(sessions._linked_rules, "S2")}


# ── (f) only fills count: a partial fill books and protects what filled (PA-02) ───────────────────────────────────

def test_a_partial_fill_records_and_protects_only_the_shares_bought(paper, tmp_path):
    import json
    from orders import rules
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["paper_partial_fill_odds"] = 1.0                           # the simulator fills half of every order
    cfg.write_text(json.dumps(c))
    r = _rule(require_confirmation=False, bracket_target_pct=5, bracket_stop_pct=4)
    _tick(paper, ACME=100.0)
    e = rules.get_rule(r["id"])
    legs = {l["role"]: l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"]}
    assert e["status"] == rules.EXECUTED and e["execution_quantity"] == 5          # 5 of the 10 asked for
    assert legs["TARGET"]["quantity_value"] == 5 and legs["STOP"]["quantity_value"] == 5


# ── (i) duplicates: an order id is stored once, a cycle once, the ledger once ─────────────────────────────────────

def test_a_fill_or_a_cycle_cannot_be_stored_twice(paper):
    from accounting import cycles as C
    from accounting.periods import ledger_realised
    _pool("S1")
    _linked_entry("ACME", 100.0, "S1")
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    c = _conn()
    try:
        oid, = c.execute("SELECT order_id FROM paper_order LIMIT 1").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO paper_order (order_id, symbol, transaction_type, quantity, filled_qty, fill_price, "
                      "status) VALUES (?, 'ACME', 'BUY', 1, 1, 1.0, 'TRADED')", (oid,))
        C.sync(c)
        first = ledger_realised(c)
        C.sync(c)
        C.sync(c, force=True)
        # one cycle, posted once: 250 x 3 = 750 (no charges here), less the 20 % + 4 % cess estimate, 156
        assert len(C.stored(c, "S1")) == 1 and ledger_realised(c) == first == 594.0
    finally:
        c.close()


# ── (g) None: a leg with no recorded quantity closes nothing and books nothing ────────────────────────────────────

def test_an_executed_leg_without_a_quantity_is_not_booked_as_a_close():
    from orders.sessions import _trades
    leg = _leg("T", 103.0, 10, "2026-11-05T10:00:00")
    leg["execution_quantity"] = None
    t, = _trades([_entry_rule(), leg])
    assert not t["closed"] and t["held_qty"] == 10 and t["pnl"] == 0.0


# ── PA-01: the LIVE book's cash is the real account's ─────────────────────────────────────────────────────────────

def test_live_cash_is_the_real_dhan_account_even_under_paper(monkeypatch):
    import orders.broker as B
    from portfolio.pnl import _live_cash

    class Funds:
        def __init__(self, v):
            self.v = v

        def get_fund_limits(self):
            return {"status": "success", "data": {"availabelBalance": self.v}}

    monkeypatch.setattr(B, "get_dhan_client", lambda: (Funds(2350.16), None))
    monkeypatch.setattr(B, "get_execution_client", lambda *a, **k: (Funds(317449.33), None))
    assert _live_cash() == 2350.16                               # not the paper simulator's balance
    assert B.available_balance() == 317449.33                    # the pre-trade funds check still asks the executor


def test_day_pnl_compares_equity_with_equity_and_unrealised_with_unrealised():
    from portfolio.pnl import _day_pnl
    prev_unknown = {"equity": None, "unrealised": -12061.49, "realised_cum": None}
    prev_known = {"equity": 76000.0, "unrealised": -12061.49, "realised_cum": None}
    now = {"equity": 76854.78 + 2350.16, "unrealised": -11643.18, "realised_cum": None}
    assert _day_pnl(now, prev_unknown) == 418.31                 # not 79,204.94 - (-12,061.49)
    assert _day_pnl(now, prev_known) == 3204.94
    assert _day_pnl(now, None) is None


def test_the_repair_takes_the_paper_cash_out_of_stored_live_rows(temp_db):
    from db.schema import init_db
    from tools.repair_live_pnl_daily import main
    init_db()
    c = _conn()
    try:
        for d, cash, eq, unreal, dpl in (("2026-11-03", 504275.5, 580613.7, -12159.76, 206.03),
                                         ("2026-11-04", 360581.04, 437017.51, -12061.49, -143596.19)):
            c.execute("INSERT INTO pnl_daily (date, env, unrealised, cash, equity, day_pnl, peak_equity, drawdown_pct) "
                      "VALUES (?, 'LIVE', ?, ?, ?, ?, 583357.74, 1.0)", (d, unreal, cash, eq, dpl))
        c.commit()
    finally:
        c.close()
    assert main([]) == 0
    c = _conn()
    try:
        assert c.execute("SELECT COUNT(*) FROM pnl_daily WHERE cash IS NOT NULL").fetchone()[0] == 2   # dry run
    finally:
        c.close()
    main(["--apply"])
    main(["--apply"])                                            # a second run finds nothing to do
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM pnl_daily ORDER BY date")]
    finally:
        c.close()
    assert [(r["cash"], r["equity"], r["peak_equity"], r["drawdown_pct"]) for r in rows] == [(None,) * 4] * 2
    assert [r["day_pnl"] for r in rows] == [None, 98.27]         # -12,061.49 - (-12,159.76)


# ── the independent reconciliation agrees with the app on a replayed day ─────────────────────────────────────────

def test_independent_reconciliation_matches_the_app_to_the_paisa(paper, tmp_path):
    import json
    from accounting import cycles as C
    from db.schema import DB_PATH
    from orders import sessions
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["paper_cost_model"] = "nse_delivery"                       # statutory charges on every fill
    cfg.write_text(json.dumps(c))
    _pool("S1")
    _pool("S2")
    _linked_entry("ACME", 100.0, "S1")
    _linked_entry("BETA", 50.0, "S2")
    _tick(paper, ACME=100.0, BETA=50.0)
    _tick(paper, ACME=103.0, BETA=49.0)
    _do(C.sync)
    spec = importlib.util.spec_from_file_location("pnl_reconcile", ROOT / "tests/verification/pnl_reconcile.py")
    rec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rec)
    today = date.today().isoformat()
    res = rec.build(str(DB_PATH), today, None, None, 15.93)
    rows = {r["session"]: r for r in res["rows"]["since_start"]}
    for sid in ("S1", "S2"):
        app = _do(sessions.ledger, sid)["booked"]
        assert abs(rows[sid]["net_re"] - app) <= 0.01            # per-order rounding of charges at most
        assert rows[sid]["max_abs_diff"] <= 0.01
    assert not any(res["integrity"][k] for k in ("tag_vs_rule", "orphans", "dup_order_refs", "rule_vs_fill",
                                                 "charge_diffs", "null_charges", "pending_marked_executed"))
