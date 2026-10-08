"""W39 (PERF-001-06): contract-note charge components through the broker import (PF-12)."""

import json
from datetime import date

import pytest


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


OWNER = {"tenant_id": "default", "owner_id": "owner"}

SPLIT = ("Symbol,Trade Date,Trade Type,Quantity,Price,Trade ID,Brokerage,STT,Exchange Transaction Charges,"
         "SEBI Fees,Stamp Duty,CGST,SGST,Total Charges\n"
         "INFY,2025-09-01,buy,10,1500,T1,20,15,0.5,0.02,2.25,1.85,1.85,41.47\n"      # parts add up
         "INFY,2025-09-03,sell,4,1550,T2,20,6.2,0.2,0.01,,1.82,1.82,60\n"            # total above the parts
         "TCS,2025-09-03,buy,1,4000,T3,20,4,0.1,0.01,0.6,1.8,1.8,5\n")               # total below the parts


def _ledger(db):
    return {r["source_ref"]: dict(r) for r in db.execute("SELECT * FROM perf_ledger")}


def test_trade_rows_carry_the_split_and_the_total_wins(db):
    from portfolio.imports import commit, parse
    p = parse(SPLIT, "pnl.csv")
    assert p["kind"] == "tradebook" and p["fee_columns"]["total"] == "Total Charges"
    assert p["fee_columns"]["components"]["gst"] == ["CGST", "SGST"]
    r1, r2, r3 = p["rows"]
    assert r1["fees"] == 41.47 and r1["fee_breakdown"] == {"brokerage": 20, "stt": 15, "exchange": 0.5, "sebi": 0.02,
                                                           "stamp": 2.25, "gst": 3.7}
    assert r2["fees"] == 60 and r2["fee_breakdown"]["other"] == pytest.approx(60 - 30.05)
    assert r3["fees"] == 5 and r3["fee_breakdown"] == {} and p["summary"]["warnings"] == 1
    assert "total is 5.00" in p["warnings"][0]["problem"]
    assert commit(db, OWNER, p, broker="icici")["added"] == 3
    L = _ledger(db)
    assert L["T:T1"]["fees"] == 41.47 and json.loads(L["T:T1"]["fee_breakdown"])["gst"] == 3.7
    assert L["T:T3"]["fee_breakdown"] is None


def test_without_a_total_the_parts_are_the_fees_and_a_lone_brokerage_column_is_unchanged(db):
    from portfolio.imports import parse
    p = parse("Symbol,Date,Side,Qty,Price,Brokerage,STT,Stamp Duty\nINFY,2025-09-01,B,1,100,20,0.1,0.02\n", "a.csv")
    assert p["rows"][0]["fees"] == pytest.approx(20.12) and set(p["rows"][0]["fee_breakdown"]) == {"brokerage", "stt",
                                                                                                    "stamp"}
    old = parse("Symbol,Date,Side,Qty,Price,Brokerage\nINFY,2025-09-01,B,1,100,20\n", "b.csv")      # PF-12 files
    assert old["rows"][0]["fees"] == 20 and old["rows"][0]["fee_breakdown"] == {"brokerage": 20}
    plain = parse("Symbol,Date,Side,Qty,Price,Charges\nINFY,2025-09-01,B,1,100,7.5\n", "c.csv")
    assert plain["rows"][0]["fees"] == 7.5 and plain["rows"][0]["fee_breakdown"] == {}


CHARGES = ("Contract Note No,Trade Date,Brokerage,STT,Exchange Transaction Charges,SEBI Fees,Stamp Duty,IGST\n"
           "CN-101,2025-09-01,0,15,0.5,0.02,2.25,0.09\n"
           "CN-102,2025-09-03,0,6.2,0.2,0.01,0,0.04\n")


def test_a_contract_note_charges_file_becomes_fee_rows_once(db):
    from portfolio.imports import commit, parse
    from wealth.perf.engine import _fee_components, ledger_fees
    p = parse(CHARGES, "contract_notes.csv")
    assert p["kind"] == "charges" and [r["note_no"] for r in p["rows"]] == ["CN-101", "CN-102"]
    assert p["summary"]["fees"] == pytest.approx(17.86 + 6.45)
    trades = parse("Symbol,Trade Date,Trade Type,Quantity,Price,Trade ID\nINFY,2025-09-01,buy,10,1500,T1\n", "t.csv")
    commit(db, OWNER, trades, broker="zerodha")
    assert commit(db, OWNER, p, broker="zerodha")["added"] == 2
    assert commit(db, OWNER, parse(CHARGES, "contract_notes.csv"), broker="zerodha")["added"] == 0     # idempotent
    fee = [r for r in _ledger(db).values() if r["kind"] == "FEE"]
    assert {r["source_ref"] for r in fee} == {"FEE:CN-101", "FEE:CN-102"}
    assert all(r["symbol"] is None and r["fees"] == 0 for r in fee)
    assert sum(r["gross_value"] for r in fee) == pytest.approx(24.31)
    assert ledger_fees(db, OWNER, "LIVE", "2025-09-01", "2025-09-30") == pytest.approx(24.31)
    txns = [dict(r, trade_date=date.fromisoformat(str(r["trade_date"])[:10])) for r in
            db.execute("SELECT * FROM perf_ledger")]
    comp = _fee_components(txns, date(2025, 9, 1))
    assert comp["cover_pct"] == 100.0 and comp["recorded"]["stt"] == pytest.approx(21.2)
    assert sum(comp["recorded"].values()) == pytest.approx(24.31)


def test_charges_for_a_day_whose_trades_already_carry_fees_are_refused(db):
    from portfolio.imports import commit, parse
    commit(db, OWNER, parse(SPLIT, "pnl.csv"), broker="icici")
    with pytest.raises(ValueError, match="double count"):
        commit(db, OWNER, parse(CHARGES, "cn.csv"), broker="icici")
    assert commit(db, OWNER, parse(CHARGES, "cn.csv"), broker="icici", force=True)["added"] == 2
    assert commit(db, OWNER, parse(CHARGES, "cn.csv"), broker="kotak")["added"] == 2        # other broker's trades
