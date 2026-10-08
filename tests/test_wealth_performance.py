"""W15.5 PERF-001-14 edge-case suite: ledger, returns, costs, attribution, corporate actions."""

from datetime import timedelta

import pytest

from tests._wealth_seed import OTHER, OWNER, fresh
from wealth.perf import data as D
from wealth.perf import engine as E
from wealth.perf import ledger as L
from wealth.perf import model as MD
from wealth.perf import report as R


def _run(conn, ds, pf="MANUAL"):
    prices = D.Prices(conn, ds[0] - timedelta(days=40), ds[-1])
    return E.run_actual(conn, OWNER, pf, ds[0], ds[-1], prices, "NIFTY50")


def _txn(conn, d, kind, qty=None, price=None, fees=0, sym="ACME", **kw):
    return L.add_manual(conn, OWNER, {"portfolio": "MANUAL", "trade_date": str(d), "kind": kind, "symbol": sym,
                                      "quantity": qty, "price": price, "fees": fees, **kw})


def test_average_cost_realized_pnl_and_fees(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 10, 100, fees=10)
    _txn(conn, ds[20], "BUY", 10, 120, fees=10)
    _txn(conn, ds[30], "SELL", 10, 130, fees=5)
    a = _run(conn, ds)
    acme = next(p for p in a["positions"] if p["symbol"] == "ACME")
    # average cost (1000+10+1200+10)/20 = 111; realized 10 x (130-111) - 5 = 185
    assert acme["realized"] == pytest.approx(185, abs=0.01)
    assert acme["cost_basis"] == pytest.approx(1110, abs=0.01)
    assert a["costs"]["reconciliation"]["ok"] and a["costs"]["fees_total"] == pytest.approx(25)


def test_intraday_round_trip_is_counted(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[50], "BUY", 100, 100)
    _txn(conn, ds[50], "SELL", 100, 105)
    a = _run(conn, ds)
    day = next(x for x in a["daily"] if x["date"] == str(ds[50]))
    assert day["return"] == pytest.approx(0.05)
    assert a["returns"]["realized_pnl"] == pytest.approx(500)


def test_contributions_reconcile_to_the_total(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[5], "BUY", 10, 100, fees=3)
    _txn(conn, ds[5], "BUY", 5, 60, sym="GOLDBEES")
    _txn(conn, ds[100], "SELL", 4, 110, fees=2)
    L.add_manual(conn, OWNER, {"portfolio": "MANUAL", "trade_date": str(ds[120]), "kind": "FEE", "gross_value": 50})
    a = _run(conn, ds)
    c = a["contribution"]
    assert c["reconciles"]
    assert sum(x["contribution_pct"] for x in c["by_symbol"]) == pytest.approx(c["total_pct"], abs=1e-3)


def test_oversell_is_refused_on_entry(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 5, 100)
    with pytest.raises(ValueError, match="cannot sell"):
        _txn(conn, ds[11], "SELL", 6, 100)


def test_future_trade_date_is_refused(temp_db):
    conn, ds = fresh(temp_db)
    with pytest.raises(ValueError, match="future"):
        _txn(conn, ds[-1] + timedelta(days=30), "BUY", 1, 100)


def test_voided_transactions_are_ignored_but_kept(temp_db):
    conn, ds = fresh(temp_db)
    t = _txn(conn, ds[10], "BUY", 10, 100)
    L.void(conn, OWNER, t["txn_id"], "entered twice")
    assert _run(conn, ds)["status"] == "NO_TRANSACTIONS"
    assert len(L.transactions(conn, OWNER, "MANUAL", include_void=True)) == 1
    with pytest.raises(ValueError):
        L.void(conn, OWNER, t["txn_id"], "again")


def test_split_between_buy_and_today_moves_neither_value_nor_return(temp_db):
    conn, ds = fresh(temp_db)
    # a 1:2 split after the buy; prices_daily is (as in production) already on the post-split basis
    buy_day = ds[20]
    close_then = conn.execute("SELECT close FROM prices_daily WHERE symbol='ACME' AND date=?", (str(buy_day),)).fetchone()[0]
    conn.execute("INSERT INTO corporate_actions (symbol,ex_date,subject,kind,factor,status,price_factor) VALUES "
                 "('ACME',?,'Split 1:2','SPLIT',0.5,'adjusted',0.5)", (str(ds[40]),))
    conn.commit()
    _txn(conn, buy_day, "BUY", 10, close_then / 0.5)       # the raw (pre-split) price actually paid
    a = _run(conn, ds)
    acme = next(p for p in a["positions"] if p["symbol"] == "ACME")
    assert acme["quantity"] == pytest.approx(20)             # 10 old shares = 20 today
    day = next(x for x in a["daily"] if x["date"] == str(buy_day))
    assert day["return"] == pytest.approx(0.0, abs=1e-9)   # bought at the close: no phantom jump


def test_house_ledger_import_is_idempotent_and_owner_scoped(temp_db):
    conn, ds = fresh(temp_db)
    from orders.paper import ensure_tables
    ensure_tables(conn)
    conn.execute("INSERT INTO paper_order (order_id,created_at,symbol,security_id,exchange,transaction_type,quantity,"
                 "filled_qty,order_type,product_type,limit_price,fill_price,status,reason,brokerage,tag) VALUES "
                 "('P1',?, 'ACME','1','NSE_EQ','BUY',10,10,'MARKET','CNC',0,100,'TRADED',NULL,5,NULL)",
                 (str(ds[10]) + "T10:00:00",))
    conn.commit()
    assert L.sync(conn, OWNER)["PAPER"] == 1 and L.sync(conn, OWNER)["PAPER"] == 0
    assert L.sync(conn, OTHER)["PAPER"] == 0 and L.transactions(conn, OTHER) == []


def test_position_attribution_identity(temp_db):
    conn, ds = fresh(temp_db)
    sd = ds[100]
    conn.execute("INSERT INTO signal_log (id,run_id,logged_at,signal_date,symbol,`signal`,entry_price) VALUES "
                 "('S1','r',?,?,'ACME','BUY',?)", (str(sd), str(sd), conn.execute(
                     "SELECT close FROM prices_daily WHERE symbol='ACME' AND date=?", (str(sd),)).fetchone()[0]))
    conn.commit()
    _txn(conn, ds[101], "BUY", 10, 150, fees=2)
    _txn(conn, ds[103], "BUY", 5, 148, fees=1)
    _txn(conn, ds[130], "SELL", 12, 160, fees=3)
    rep = R.build(conn, OWNER, "MANUAL", ds[0], ds[-1], sync_first=False)
    link = rep["signal_attribution"]["linked"][0]
    a = link["position_attribution"]
    assert a["total"] == pytest.approx(a["check"], abs=0.01)
    assert link["entry"]["delay_sessions"] == 1 and len(link["add_ons"]) == 1


def test_model_and_executable_are_reported_separately(temp_db):
    conn, ds = fresh(temp_db)
    conn.execute("INSERT INTO signal_log (id,run_id,logged_at,signal_date,symbol,`signal`,entry_price) VALUES "
                 "('S2','r',?,?,'ACME','BUY',120)", (str(ds[60]), str(ds[60])))
    conn.commit()
    m = MD.build(conn, ds[0], ds[-1], D.Prices(conn, ds[0], ds[-1]), {})
    assert m["model"]["trades"] and m["executable"]["trades"]
    e = m["executable"]["trades"][0]
    assert e["entry_date"] == str(ds[61]) and e["costs"] > 0


def test_report_has_four_returns_and_an_audit_block(temp_db):
    conn, ds = fresh(temp_db)
    _txn(conn, ds[10], "BUY", 10, 100)
    rep = R.run(conn, OWNER, "MANUAL", ds[0], ds[-1])
    assert [c["return"] for c in rep["comparison"]] == ["MODEL", "EXECUTABLE", "ACTUAL", "BENCHMARK"]
    au = rep["audit"]
    assert au["methodology_version"] == "PERF-1.0" and au["inputs"]["ledger_sha256"]
    csv_text, media = R.export(R.get(conn, OWNER, rep["report_id"]), "csv")
    assert media == "text/csv" and "# comparison" in csv_text and "# audit" in csv_text
    with pytest.raises(LookupError):
        R.get(conn, OTHER, rep["report_id"])
