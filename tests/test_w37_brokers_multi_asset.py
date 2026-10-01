"""W37: credential vault completion (ENT-06), broker connectors (BR-07), multi-broker import (PF-12),
paper options book (ENT-15)."""

import base64
import os
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def db(temp_db, monkeypatch):
    monkeypatch.setenv("ATIP_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


OWNER = {"tenant_id": "default", "owner_id": "owner"}


# ── ENT-06 ──
def test_vault_resolves_a_live_credential_and_refuses_an_expired_one(db):
    from enterprise import vault
    vault.store(db, "t1", "u1", "upstox", {"api_key": "k", "api_secret": "s", "access_token": "tok"})
    c = vault.credentials_for(db, "t1", "u1", "upstox", "test")
    assert c["source"] == "vault" and c["fields"]["access_token"] == "tok"
    assert db.execute("SELECT access_count FROM enterprise_vault_credential").fetchone()[0] == 1
    db.execute("UPDATE enterprise_vault_credential SET expires_at=?", (datetime.now() - timedelta(hours=1),))
    db.commit()
    with pytest.raises(LookupError, match="expired"):
        vault.credentials_for(db, "t1", "u1", "upstox", "test")
    assert vault.expiring(db, 1)[0]["expired"] is True
    with pytest.raises(LookupError):
        vault.credentials_for(db, "t1", "u1", "angel", "test")


def test_vault_verify_records_the_result(db, monkeypatch):
    from enterprise import vault
    r = vault.store(db, "t1", "u1", "zerodha", {"api_key": "k", "api_secret": "s", "access_token": "t"})

    class Fake:
        def __init__(self, fields):
            assert fields["access_token"] == "t"

        def profile(self):
            return {"name": "Test User"}
    import brokers.registry as R
    monkeypatch.setattr(R, "connector", lambda b, f: Fake(f))
    out = vault.verify(db, r["credential_id"])
    assert out["verify_status"] == "OK" and "Test User" in out["detail"]


def test_key_rotation_covers_broker_credentials(db):
    from enterprise import vault
    from ops.crypto import rotate_key
    vault.store(db, "t1", "u1", "dhan", {"client_id": "c", "access_token": "a"})
    dry = rotate_key(db, apply=False)
    assert dry["values"] >= 1 and "evault:secret_enc" in dry["fields"]


# ── BR-07 ──
def test_order_adapters_map_but_never_send():
    from brokers.registry import ORDER_ADAPTERS
    from execution.errors import LiveTradingDisabled
    o = {"order_id": "OMS1", "symbol": "TCS", "side": "SELL", "quantity": 3, "order_type": "SL", "limit_price": 3400,
         "trigger_price": 3410, "product_type": "CNC"}
    for b, cls in ORDER_ADAPTERS.items():
        a = cls()
        p = a.build_payload(o)
        assert p and "3" in str(p)
        with pytest.raises(LiveTradingDisabled):
            a.submit(o)


def test_upstox_connector_normalises_holdings(monkeypatch):
    from brokers.connectors import UpstoxConnector
    c = UpstoxConnector({"access_token": "x"})
    monkeypatch.setattr(c, "_get", lambda path: [{"tradingsymbol": "RELIANCE-EQ", "quantity": 10, "average_price": 2400,
                                                  "last_price": 2500, "isin": "INE002A01018", "exchange": "NSE"},
                                                 {"tradingsymbol": "ZERO", "quantity": 0}])
    hs = c.holdings()
    assert len(hs) == 1 and hs[0].symbol == "RELIANCE" and hs[0].avg_price == 2400


# ── PF-12 ──
ZERODHA = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,"
           "order_execution_time\nTCS,INE467B01029,2025-09-12,NSE,EQ,EQ,buy,false,5,3501.5,111,9001,2025-09-12T10:15:02\n"
           "TCS,INE467B01029,2025-09-15,NSE,EQ,EQ,sell,false,2,3600,112,9002,2025-09-15T11:00:00\n"
           "???,,2025-09-15,NSE,EQ,EQ,buy,false,1,10,113,9003,2025-09-15T11:00:00\n")


def test_import_is_idempotent_and_reports_bad_rows(db):
    from portfolio.imports import commit, parse
    p = parse(ZERODHA, "tradebook.csv")
    assert p["broker_detected"] == "zerodha" and p["summary"]["rows"] == 2 and p["summary"]["errors"] == 1
    r1 = commit(db, OWNER, p)
    r2 = commit(db, OWNER, parse(ZERODHA, "tradebook.csv"))
    assert r1["added"] == 2 and r2["added"] == 0 and r2["already_imported"] == 2
    rows = db.execute("SELECT kind, quantity, source FROM perf_ledger ORDER BY trade_date").fetchall()
    assert [tuple(r) for r in rows] == [("BUY", 5.0, "import:zerodha"), ("SELL", 2.0, "import:zerodha")]


def test_holdings_open_once_and_dhan_double_count_is_refused(db):
    from portfolio.imports import commit, parse
    h = parse("Instrument,Qty.,Avg. cost\nINFY,10,1500\n", "holdings.csv")
    assert commit(db, OWNER, h, broker="upstox", as_of="2025-09-01")["added"] == 1
    assert commit(db, OWNER, h, broker="upstox", as_of="2025-10-01")["added"] == 0
    from wealth.perf.ledger import _insert
    _insert(db, OWNER, "LIVE", "broker_sync", "x", "2025-09-01", "2025-09-01", "OPENING", "TCS", 1, 1, 0)
    with pytest.raises(ValueError, match="double count"):
        commit(db, OWNER, h, broker="dhan")


# ── ENT-15 ──
def _chain(db, expiry):
    today = date.today()
    for strike, close in ((100.0, 8.0), (110.0, 3.0)):
        db.execute("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,settle,oi,"
                   "volume,underlying,lot_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (str(today), "ACME", "STO", str(expiry), strike, "CE", close, close, 1000, 500, 105.0, 50))
    db.commit()


def test_options_book_rules_fill_and_settle(db, monkeypatch):
    from execution import options_paper as O
    monkeypatch.setattr(O, "settings", lambda: {**O.DEFAULTS, "enabled": True})
    O.ensure_tables(db)
    db.execute("INSERT OR REPLACE INTO paper_account (id,balance,opened_at) VALUES (1, 1000000, '2025-01-01')")
    exp = date.today() + timedelta(days=10)
    _chain(db, exp)
    order = {"underlying": "ACME", "expiry": str(exp), "strike": 100, "option_type": "CE", "side": "BUY", "lots": 2}
    r = O.place(db, order)
    assert r["qty"] == 100 and r["price"] > 8.0                     # bought above close (slippage)
    with pytest.raises(O.OptionsRiskError, match="writing"):
        O.place(db, {**order, "side": "SELL", "lots": 3})            # more than held = naked short
    with pytest.raises(O.OptionsRiskError, match="premium"):
        O.place(db, {**order, "lots": 400})                          # premium cap
    s = O.place(db, {**order, "side": "SELL", "lots": 1})
    assert s["realized"] < 0                                         # sold below the slipped buy price, minus fees
    db.execute("UPDATE paper_options_position SET expiry=?", (str(date.today() - timedelta(days=1)),))
    db.execute("INSERT INTO fo_underlying_daily (date,symbol,kind,underlying_price) VALUES (?,?,?,?)",
               (str(date.today() - timedelta(days=1)), "ACME", "STOCK", 112.0))
    db.commit()
    assert O.settle_expired(conn=db)["rows"] == 1
    last = db.execute("SELECT price, reason FROM paper_options_trade ORDER BY at DESC LIMIT 1").fetchone()
    assert last[0] == pytest.approx(12.0) and "EXPIRY" in last[1]   # intrinsic: 112 - 100
    assert db.execute("SELECT qty FROM paper_options_position").fetchone()[0] == 0


def test_options_refused_when_off(db):
    from execution import options_paper as O
    with pytest.raises(O.OptionsRiskError, match="off"):
        O.place(db, {"underlying": "ACME"})
