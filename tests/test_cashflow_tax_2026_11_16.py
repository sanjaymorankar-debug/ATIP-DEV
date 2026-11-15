"""Owner items 8 and 14, 16 Nov 2026.

 8  External cash flows (portfolio/cash_flows.py, portfolio/pnl.py, orders/risk.py): a deposit, withdrawal or transfer
    is not P&L. A recorded withdrawal cannot trip max_daily_loss_value or max_drawdown_pct; a real loss still does; a
    book with no flow on record is measured exactly as before (PAPER today); a LIVE cash change that trades cannot
    explain is stored PENDING and counts as P&L until confirmed. The PA-01 repair can pick the paper-cash rows by their
    signature (--paper-match), since the Mac ran without the fix until the restart.
14  FY STCG estimate after set-off (accounting/tax.py fy_stcg_estimate): a loss cycle reduces the FY tax, an all-loss
    FY owes nothing, the FY is Apr-Mar in IST, 4 % cess on the 20 %, STT is not deductible; the per-cycle figure is
    shown only as "Tax reserve (per-cycle, cash set aside)" and the FY figure carries the CA line.

Every expected figure is worked out by hand beside it."""

import json
from datetime import date, timedelta

import pytest

from accounting import tax as TX

TODAY = date.today()
YDAY = TODAY - timedelta(days=1)


def _conn():
    from db.schema import get_connection
    return get_connection()


# ══ item 8: external cash flows ═════════════════════════════════════════════════════════════════════════════════

def test_a_flow_is_signed_by_its_kind():
    from portfolio.cash_flows import signed
    assert signed("WITHDRAWAL", 50000) == -50000.0 and signed("TRANSFER_OUT", "₹1,000") == -1000.0
    assert signed("DEPOSIT", -2500) == 2500.0 and signed("TRANSFER_IN", 10) == 10.0
    assert signed("ADJUSTMENT", -260510.78) == -260510.78                    # signed as entered
    for kind, amt in (("GIFT", 1), ("DEPOSIT", 0), ("DEPOSIT", "abc"), ("DEPOSIT", float("nan"))):
        with pytest.raises(ValueError):
            signed(kind, amt)


def test_the_series_takes_a_withdrawal_out_of_day_pnl_and_keeps_the_drawdown_percent():
    from portfolio.cash_flows import adjusted_series
    rows = [{"date": "2026-11-02", "equity": 500000.0}, {"date": "2026-11-03", "equity": 450000.0},   # -10 % loss
            {"date": "2026-11-04", "equity": 225000.0}]                                             # half withdrawn
    out = adjusted_series(rows, [("2026-11-04", -225000.0)])
    s = out["series"]
    assert [r["day_pnl"] for r in s] == [None, -50000.0, 0.0]               # 225,000 - 450,000 + 225,000
    assert [r["drawdown_pct"] for r in s] == [0.0, 10.0, 10.0]              # peak 500,000 x 225/450 = 250,000
    assert s[-1]["peak"] == 250000.0
    # without the flow the same rows read as a 55 % drawdown
    assert adjusted_series(rows, [])["drawdown_pct"] == 55.0
    # a deposit does not hide a loss: 10 % down, then 450,000 more in -> still 10 % down
    dep = adjusted_series(rows[:2], [(str(date(2026, 11, 4)), 450000.0)], equity_now=900000.0, on="2026-11-04")
    assert (dep["peak"], dep["drawdown_pct"], dep["flow_today"]) == (1000000.0, 10.0, 450000.0)


def _live_book(monkeypatch, cfgfile, real_cash):
    """A LIVE book: yesterday cash 4,00,000 + ACME 100 x 1,000 = equity 5,00,000 (the peak); today the real account
    reports `real_cash` through the PA-01 path (available_balance(account=True)); limits 10,000 Rs / 10 %."""
    import orders.broker as B
    from db.schema import init_db
    from orders.broker import _ensure_order_log_table

    class Funds:
        def get_fund_limits(self):
            return {"status": "success", "data": {"availabelBalance": real_cash[0]}}

    monkeypatch.setattr(B, "get_dhan_client", lambda: (Funds(), None))
    cfgfile(risk_limits={"max_daily_loss_value": 10000, "max_drawdown_pct": 10})
    init_db()
    c = _conn()
    _ensure_order_log_table(c)
    for d in (YDAY, TODAY):
        c.execute("INSERT INTO portfolio_holdings (date, symbol, qty, avg_price, cmp) VALUES (?, 'ACME', 100, 1000, 1000)",
                  (str(d),))
    c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('ACME', ?, 1000)", (str(YDAY),))
    c.execute("INSERT INTO pnl_daily (date, env, n_positions, positions_value, cost, unrealised, cash, equity, day_pnl, "
              "peak_equity, drawdown_pct) VALUES (?, 'LIVE', 1, 100000, 100000, 0, 400000, 500000, 0, 500000, 0)",
              (str(YDAY),))
    c.commit()
    return c


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    from orders import risk
    from orders import environment
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", cfg)
    monkeypatch.setattr(environment, "CONFIG_PATH", cfg)

    def write(**keys):
        cfg.write_text(json.dumps(keys), encoding="utf-8")
    return write


def _check(c):
    from orders.risk import pretrade_check
    return pretrade_check(c, "ACME", "BUY", 1, 1000.0, env="LIVE", gateway=False)


def test_a_recorded_withdrawal_cannot_trip_the_loss_limits_but_a_real_loss_still_does(temp_db, cfgfile, monkeypatch):
    from portfolio import cash_flows as CF
    cash = [350000.0]                                       # Rs 50,000 withdrawn today; ACME unchanged at 1,000
    c = _live_book(monkeypatch, cfgfile, cash)
    try:
        c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('ACME', ?, 1000)", (str(TODAY),))
        c.commit()
        r = _check(c)                                       # before it is recorded: equity 4,50,000 vs 5,00,000
        assert not r["ok"] and r["blocked_by"] == "max_daily_loss_value"
        lost = {k["limit"]: k["value"] for k in r["checks"]}
        assert lost == {"max_daily_loss_value": 50000.0, "max_drawdown_pct": 10.0}
        assert "cash moved -50,000" in r["message"] and "python -m portfolio.cash_flows" in r["message"]

        CF.record(c, "LIVE", "WITHDRAWAL", 50000, TODAY, "to the bank")
        r = _check(c)
        assert r["ok"], r["message"]
        assert {k["limit"]: k["value"] for k in r["checks"]} == {"max_daily_loss_value": 0.0, "max_drawdown_pct": 0.0}

        # a real loss on top of the recorded withdrawal: ACME 1,000 -> 400 = -60,000 on the holdings
        c.execute("UPDATE prices_daily SET close = 400 WHERE symbol = 'ACME' AND date = ?", (str(TODAY),))
        c.commit()
        r = _check(c)
        assert not r["ok"] and r["blocked_by"] == "max_daily_loss_value"
        got = {k["limit"]: k["value"] for k in r["checks"]}
        assert got["max_daily_loss_value"] == 60000.0      # 3,90,000 - 5,00,000 + 50,000
        assert got["max_drawdown_pct"] == 13.33            # peak 5,00,000 x 4.5/5 = 4,50,000; 60,000 / 4,50,000
        assert "cash moved" not in r["message"]            # nothing unexplained: the withdrawal is on record
    finally:
        c.close()


def test_a_real_loss_alone_trips_the_drawdown_limit(temp_db, cfgfile, monkeypatch):
    cfgfile(risk_limits={"max_drawdown_pct": 10})
    c = _live_book(monkeypatch, cfgfile, [400000.0])
    try:
        cfgfile(risk_limits={"max_drawdown_pct": 10})
        c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('ACME', ?, 400)", (str(TODAY),))
        c.commit()
        r = _check(c)
        assert not r["ok"] and r["blocked_by"] == "max_drawdown_pct"
        assert r["checks"][0]["value"] == 12.0              # (5,00,000 - 4,40,000) / 5,00,000
    finally:
        c.close()


def test_a_book_without_flows_is_measured_exactly_as_before(temp_db):
    """PAPER today: no flow on record -> MAX(peak_equity) and equity - previous equity, untouched."""
    from db.schema import init_db
    from orders.paper import ensure_tables
    from portfolio.pnl import risk_state
    init_db()
    c = _conn()
    try:
        ensure_tables(c)
        c.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1, 495000, '2026-09-08')")
        for d, eq, pk in ((YDAY - timedelta(days=1), 520000, 530000), (YDAY, 510000, 530000)):
            c.execute("INSERT INTO pnl_daily (date, env, cash, equity, peak_equity, drawdown_pct) "
                      "VALUES (?, 'PAPER', ?, ?, ?, 0)", (str(d), eq, eq, pk))
        c.commit()
        st = risk_state(c, "PAPER")
    finally:
        c.close()
    assert (st["equity"], st["day_pnl"], st["peak_equity"]) == (495000.0, -15000.0, 530000)
    assert st["drawdown_pct"] == 6.6 and st["flow_adjusted"] is False and st["external_flow"] == 0.0
    assert st["pending_flows"] == [] and st["unexplained_cash"] is None    # PAPER is never "detected"


def test_the_daily_job_stores_an_unexplained_live_cash_change_as_pending(temp_db, cfgfile, monkeypatch):
    import portfolio.pnl as P
    from portfolio import cash_flows as CF
    c = _live_book(monkeypatch, cfgfile, [0.0])
    c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('ACME', ?, 1000)", (str(TODAY),))
    c.commit()
    c.close()
    monkeypatch.setattr(P, "_live_cash", lambda: 340000.0)              # Rs 60,000 left the account, no trade
    P.record_daily_pnl(TODAY, envs=("LIVE",))
    c = _conn()
    try:
        flow, = CF.listing(c, "LIVE")
        assert (flow["status"], flow["kind"], flow["amount"], flow["source"]) == ("PENDING", "WITHDRAWAL", -60000.0,
                                                                                 "detected")
        row = dict(c.execute("SELECT * FROM pnl_daily WHERE env = 'LIVE' AND date = ?", (str(TODAY),)).fetchone())
        assert row["day_pnl"] == -60000.0 and row["drawdown_pct"] == 12.0    # pending: still counted
        CF.set_status(c, flow["id"], CF.CONFIRMED, "bank statement checked")
    finally:
        c.close()
    P.record_daily_pnl(TODAY, envs=("LIVE",))                           # re-run: the confirmed flow is taken out
    c = _conn()
    try:
        assert len(CF.listing(c, "LIVE")) == 1                          # not stored twice
        row = dict(c.execute("SELECT * FROM pnl_daily WHERE env = 'LIVE' AND date = ?", (str(TODAY),)).fetchone())
        assert (row["day_pnl"], row["peak_equity"], row["drawdown_pct"]) == (0.0, 440000.0, 0.0)
    finally:
        c.close()


def test_auto_confirm_counts_a_detected_flow_at_once(temp_db, cfgfile, monkeypatch):
    import portfolio.pnl as P
    c = _live_book(monkeypatch, cfgfile, [0.0])
    c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('ACME', ?, 1000)", (str(TODAY),))
    c.commit()
    c.close()
    cfgfile(risk_limits={"max_drawdown_pct": 10}, cash_flows={"auto_confirm_detected": True})
    monkeypatch.setattr(P, "_live_cash", lambda: 425000.0)              # Rs 25,000 deposited
    P.record_daily_pnl(TODAY, envs=("LIVE",))
    c = _conn()
    try:
        row = dict(c.execute("SELECT * FROM pnl_daily WHERE env = 'LIVE' AND date = ?", (str(TODAY),)).fetchone())
    finally:
        c.close()
    assert (row["day_pnl"], row["peak_equity"], row["drawdown_pct"]) == (0.0, 525000.0, 0.0)   # not a 25,000 profit


def test_detection_stands_down_when_trades_explain_the_cash(temp_db, cfgfile, monkeypatch):
    from portfolio import cash_flows as CF
    c = _live_book(monkeypatch, cfgfile, [0.0])
    try:
        prev = {"date": str(YDAY), "cash": 400000.0}
        assert CF.detect(c, "LIVE", prev, 340000.0, TODAY)["amount"] == -60000.0
        assert CF.detect(c, "LIVE", prev, 399500.0, TODAY) is None          # below detect_min_value (1,000)
        assert CF.detect(c, "PAPER", prev, 340000.0, TODAY) is None         # PAPER is never detected
        c.execute("INSERT INTO order_log (timestamp, symbol, transaction_type, quantity, mode, status) "
                  "VALUES (?, 'ACME', 'BUY', 60, 'LIVE', 'PLACED')", (f"{TODAY} 10:00:00",))
        c.commit()
        assert CF.detect(c, "LIVE", prev, 340000.0, TODAY) is None          # a LIVE order: cannot attribute
        c.execute("DELETE FROM order_log")
        c.execute("UPDATE portfolio_holdings SET qty = 160 WHERE date = ?", (str(TODAY),))
        c.commit()
        assert CF.detect(c, "LIVE", prev, 340000.0, TODAY) is None          # holdings changed: a trade
    finally:
        c.close()


def test_a_voided_flow_never_counts_and_a_confirm_keeps_the_direction(temp_db):
    from portfolio import cash_flows as CF
    c = _conn()
    try:
        f = CF.record(c, "LIVE", "WITHDRAWAL", 1000, "2026-11-10", "typo")
        assert CF.net_flow(c, "LIVE", "2026-11-09", "2026-11-10") == -1000.0
        CF.set_status(c, f["id"], CF.VOID, "entered twice")
        assert CF.net_flow(c, "LIVE", None, "2026-11-30") == 0.0
        assert CF.listing(c, "LIVE")[0]["status"] == "VOID"                 # kept, not deleted
        g = CF.record(c, "LIVE", "WITHDRAWAL", 500, "2026-11-11", status=CF.PENDING, source="detected")
        with pytest.raises(ValueError, match="direction"):
            CF.set_status(c, g["id"], CF.CONFIRMED, kind="DEPOSIT")
        assert CF.set_status(c, g["id"], CF.CONFIRMED, kind="TRANSFER_OUT")["kind"] == "TRANSFER_OUT"
        with pytest.raises(ValueError):
            CF.record(c, "SANDBOX", "DEPOSIT", 1)
    finally:
        c.close()


def test_the_repair_can_pick_the_paper_cash_rows_by_their_signature(temp_db):
    """6-13 Nov were written without the fix (no restart): --paper-match finds them whatever their date; a row
    recorded after the restart (real cash) is left alone."""
    from db.schema import init_db
    from tools.repair_live_pnl_daily import main, plan
    init_db()
    c = _conn()
    try:
        for d, live_cash, paper_cash in (("2026-11-05", 317449.33, 317449.33), ("2026-11-13", 262860.94, 262860.94),
                                         ("2026-11-17", 2350.16, 262860.94)):
            c.execute("INSERT INTO pnl_daily (date, env, unrealised, cash, equity) VALUES (?, 'LIVE', -11850.45, ?, ?)",
                      (d, live_cash, live_cash + 76647.51))
            c.execute("INSERT INTO pnl_daily (date, env, cash, equity) VALUES (?, 'PAPER', ?, ?)", (d, paper_cash, paper_cash))
        c.commit()
        assert [str(r["date"])[:10] for r, _ in plan(c, "2026-11-05")] == ["2026-11-05"]     # the old default misses 13 Nov
        assert [str(r["date"])[:10] for r, _ in plan(c, "2026-11-05", paper_match=True)] == ["2026-11-05", "2026-11-13"]
    finally:
        c.close()
    main(["--paper-match", "--apply"])
    c = _conn()
    try:
        cash = {str(r["date"])[:10]: r["cash"] for r in c.execute("SELECT date, cash FROM pnl_daily WHERE env = 'LIVE'")}
    finally:
        c.close()
    assert cash == {"2026-11-05": None, "2026-11-13": None, "2026-11-17": 2350.16}


# ══ item 14: FY STCG estimate after set-off ═════════════════════════════════════════════════════════════════════

LAW = TX.settings({})                                    # defaults: 20 %, 4 % cess, STT not deductible, no surcharge


def _cyc(net, stt=0.0, closed="2026-11-04T10:00:00", tax=None, head="STCG", env="VIRTUAL"):
    return {"net_pnl": net, "ch_stt": stt, "closed_at": closed, "tax_head": head, "env": env,
            "tax_est": TX.cycle_tax(net, head, stt, LAW) if tax is None else tax}


def test_a_loss_cycle_reduces_the_fy_tax():
    win, loss = _cyc(1000.0, 50.0), _cyc(-600.0, 40.0)
    alone = TX.fy_stcg_estimate([win], "FY 2026-27", LAW)
    both = TX.fy_stcg_estimate([win, loss], "FY 2026-27", LAW)
    assert alone["estimate"] == 218.4                     # (1,000 + 50 STT) x 20.8 %
    assert (both["gains"], both["losses"], both["net_stcg"]) == (1050.0, 560.0, 490.0)   # the loss less its STT
    assert both["estimate"] == 101.92                     # 490 x 20 % = 98, + 4 % cess 3.92
    assert both["per_cycle_reserve"] == 218.4             # the per-cycle figure taxed only the winner
    assert both["reserve_over_estimate"] == 116.48 and both["winning_cycles"] == 1


def test_an_all_loss_fy_owes_nothing():
    est = TX.fy_stcg_estimate([_cyc(-600.0, 40.0), _cyc(-100.0, 10.0)], "FY 2026-27", LAW)
    assert (est["estimate"], est["tax"], est["cess"], est["taxable"]) == (0.0, 0.0, 0.0, 0.0)
    assert est["net_stcg"] == -650.0 and est["loss_to_carry_forward"] == 650.0 and est["per_cycle_reserve"] == 0.0


def test_the_fy_runs_1_april_to_31_march_in_ist():
    rows = [_cyc(100.0, closed="2027-03-31T23:59:59"),            # stored IST (the Mac's clock): FY 2026-27
            _cyc(200.0, closed="2027-04-01T00:00:00"),            # FY 2027-28
            _cyc(400.0, closed="2027-03-31T19:00:00+00:00"),      # = 1 Apr 00:30 IST: FY 2027-28, not 31 Mar
            _cyc(800.0, closed="2027-03-31T18:00:00Z"),           # = 31 Mar 23:30 IST: FY 2026-27
            _cyc(1600.0, closed="2026-03-31T12:00:00")]           # FY 2025-26
    assert TX.fy_stcg_estimate(rows, "FY 2026-27", LAW)["net_stcg"] == 900.0
    assert TX.fy_stcg_estimate(rows, "FY 2027-28", LAW)["net_stcg"] == 600.0
    assert TX.fy_of("2027-03-31T19:00:00+00:00") == "FY 2027-28" and TX.fy_of("2027-03-31T23:59:59") == "FY 2026-27"


def test_cess_is_four_percent_of_the_twenty_percent():
    est = TX.fy_stcg_estimate([_cyc(10000.0)], "FY 2026-27", LAW)
    assert (est["tax"], est["cess"], est["estimate"], est["effective_pct"]) == (2000.0, 80.0, 2080.0, 20.8)
    brief = TX.settings({"tax": {"addons_enabled": False}})
    assert TX.fy_stcg_estimate([_cyc(10000.0)], "FY 2026-27", brief)["estimate"] == 2000.0


def test_stt_is_not_deductible_but_the_other_charges_are():
    """net_pnl is after every charge (brokerage, exchange, SEBI, stamp, GST, DP deducted); STT is added back."""
    est = TX.fy_stcg_estimate([_cyc(1000.0, stt=100.0)], "FY 2026-27", LAW)
    assert (est["stt_added_back"], est["taxable"], est["estimate"]) == (100.0, 1100.0, 228.8)
    alt = TX.fy_stcg_estimate([_cyc(1000.0, stt=100.0)], "FY 2026-27", TX.settings({"tax": {"stt_deductible": True}}))
    assert alt["taxable"] == 1000.0 and "STT is not deductible" in est["assumption"]


def test_paper_and_broker_cycles_are_never_added_together_and_other_heads_stay_out():
    rows = [_cyc(1000.0), _cyc(5000.0, env="ACTUAL"), _cyc(3000.0, head="SPECULATIVE")]
    v = TX.fy_stcg_estimate(rows, "FY 2026-27", LAW, env="VIRTUAL")
    assert (v["cycles"], v["net_stcg"], v["other_head_cycles"]) == (1, 1000.0, 1)
    assert TX.fy_stcg_estimate(rows, "FY 2026-27", LAW, env="ACTUAL")["net_stcg"] == 5000.0


def test_the_estimate_reaches_the_api_and_the_pages_with_the_ca_line(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from accounting import cycles as C
    from dashboard import security
    from enterprise import session_reports as R
    from tools import load_test as LT
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(TX, "CONFIG_PATH", tmp_path / "tax-config.json")
    rc = tmp_path / "reports.json"
    rc.write_text(json.dumps({"reports": {"pdf_engine": "builtin"}}))
    monkeypatch.setattr(R, "CONFIG_PATH", rc)
    with LT.block_network():
        LT.seed(tmp_path, extra_symbols=4)
        c = _conn()
        try:
            C.ensure_tables(c)
            for rid, net, stt in (("W", 1000.0, 50.0), ("L", -600.0, 40.0)):
                c.execute("INSERT INTO session_cycles (session_id, n, rule_id, symbol, net_pnl, ch_stt, tax_est, tax_head, "
                          "actual_pnl, closed_at, opened_at, env, fy) VALUES ('S1', 1, ?, 'ACME', ?, ?, ?, 'STCG', ?, ?, ?, "
                          "'VIRTUAL', ?)", (rid, net, stt, TX.cycle_tax(net, "STCG", stt, LAW),
                                            net - TX.cycle_tax(net, "STCG", stt, LAW), f"{TODAY}T11:00:00",
                                            f"{TODAY}T09:30:00", TX.fy_of(TODAY)))
            c.commit()
        finally:
            c.close()
        from dashboard import server
        with TestClient(server.app) as client:
            token = security.token()
            j = client.get("/api/v2/tax/fy-estimate").json()
            est = j["by_env"]["VIRTUAL"]
            assert (est["estimate"], est["per_cycle_reserve"], j["note"]) == (101.92, 218.4, TX.FY_NOTE)
            page = client.get("/accounting?tab=tax").text
            assert TX.FY_NOTE in page and "Tax reserve (per-cycle)" in page and "Tax (est.)" not in page
            assert TX.FY_NOTE in client.get("/home").text
            # the manual cash-flow path: token needed, then stored and listed with each book's series
            body = {"env": "LIVE", "kind": "WITHDRAWAL", "amount": 5000, "date": str(TODAY), "note": "to bank"}
            assert client.post("/api/v2/cash-flows", json=body).status_code == 401
            r = client.post("/api/v2/cash-flows", json=body, headers={"X-ATIP-Token": token})
            assert r.status_code == 200 and r.json()["amount"] == -5000.0
            bad = client.post("/api/v2/cash-flows", json={**body, "kind": "GIFT"}, headers={"X-ATIP-Token": token})
            assert bad.status_code == 400
            listed = client.get("/api/v2/cash-flows?env=LIVE").json()
            assert listed["flows"][0]["kind"] == "WITHDRAWAL" and "LIVE" in listed["flow_adjusted"]


def test_the_manual_path_on_the_command_line(temp_db, capsys):
    from portfolio import cash_flows as CF
    CF.main(["add", "--env", "LIVE", "--kind", "TRANSFER_OUT", "--amount", "12,500", "--date", "2026-11-17",
             "--note", "to the family account"])
    CF.main(["list", "--env", "LIVE"])
    out = capsys.readouterr().out
    assert "TRANSFER_OUT" in out and "-12,500.00" in out and "CONFIRMED" in out
    CF.main(["void", "1", "--reason", "wrong account"])
    c = _conn()
    try:
        assert CF.net_flow(c, "LIVE") == 0.0 and "void: wrong account" in CF.listing(c)[0]["note"]
    finally:
        c.close()
