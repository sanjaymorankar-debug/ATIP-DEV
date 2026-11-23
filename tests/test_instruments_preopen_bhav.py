"""
Owner items 5, 6, 7, 18 and 21 (16 Nov 2026), offline:

    5   data/instruments.py   the scrip master in instrument_master, the renames (ZOMATO -> ETERNAL, TATAMOTORS -> TMPV,
                              MOFSL -> MOTILALOFS) and the REITs resolve with no "security_id not found"; tick / lot sizes
    6   ops/preopen_health.py the 08:45 gate: DH-901, a stale quote, no scrip master today, no scores for the previous
                              session; the token renewal; the banner; Telegram's retry with backoff
    7   data/bhavcopy.py      Bhavcopy retried with backoff, then Dhan's EOD candles, both sources logged
    18  data/completeness.py  15-minute-bar completeness under 95% alerts once; tools/backfill_intraday_bars.py dry run
    21  brokers/connectors.py the Kite connector's new reads, and no method that changes an order or a GTT
    and the 08:00 / 08:45 / 16:15 jobs registered.

The scrip-master rows below are verbatim lines of Dhan's file of 14 Nov 2026. Nothing reaches Dhan, NSE, Kite or
Telegram: every probe is replaced, and tests/conftest.py stubs Telegram delivery.
"""

import datetime as dt
import logging
import re
import sqlite3

import pandas as pd
import pytest

MASTER = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME
BSE,C,1026077,FUTCUR,0,USDINR-28Aug2024-FUT,1.0,USDINR AUG FUT,2024-08-28 14:30:00,-0.01000,XX,0.2500,M,FUTCUR,,USDINR
NSE,E,5097,EQUITY,0,ETERNAL,1.0,Eternal,,,,5.0000,NA,ES,EQ,ETERNAL LIMITED
NSE,E,3456,EQUITY,0,TMPV,1.0,Tata Motors Passenger Vehicles,,,,5.0000,NA,ES,EQ,TATA MOTORS PASS VEH LTD
NSE,E,759782,EQUITY,0,TMCV,1.0,Tata Motors,,,,5.0000,NA,ES,EQ,TATA MOTORS LIMITED
NSE,E,14947,EQUITY,0,MOTILALOFS,1.0,Motilal Oswal Financial Services,,,,5.0000,NA,ES,EQ,MOTILAL OSWAL FIN LTD
NSE,E,9383,EQUITY,0,EMBASSY,1.0,Embassy Office Parks REIT,,,,1.0000,NA,REIT,RR,EMBASSY OFFICE PARKS REIT
NSE,E,2203,EQUITY,0,BIRET,1.0,Brookfield India Real Estate Trust,,,,1.0000,NA,REIT,RR,BROOKFIELD INDIA RET
NSE,E,763363,EQUITY,0,BAGMANE,1.0,Bagmane Prime Office REIT,,,,1.0000,NA,REIT,RR,BAGMANE PRIME OFFICE REIT
NSE,E,3150,EQUITY,0,SIEMENS,1.0,Siemens,,,,10.0000,NA,ES,EQ,SIEMENS LTD
NSE,E,1512,EQUITY,0,INDHOTEL,1.0,Indian Hotels Company,,,,5.0000,NA,ES,EQ,THE INDIAN HOTELS CO. LTD
NSE,E,25510,EQUITY,0,MOTHERSON,1.0,SMIL-6.5%-20092027-NCD,,,,5.0000,NA,DEB,D1,SAMVARDHANA MOTHERSON INT
NSE,E,4204,EQUITY,0,MOTHERSON,1.0,Samvardhana Motherson International,,,,1.0000,NA,ES,EQ,SAMVRDHNA MTHRSN INTL LTD
NSE,E,2277,EQUITY,0,MRF,1.0,MRF,,,,500.0000,NA,ES,EQ,MRF LTD
NSE,E,2885,EQUITY,0,RELIANCE,1.0,Reliance Industries,,,,10.0000,NA,ES,EQ,RELIANCE INDUSTRIES LTD
"""
SIX = {"ZOMATO": "5097", "TATAMOTORS": "3456", "MOFSL": "14947", "EMBASSY": "9383", "BIRET": "2203", "BAGMANE": "763363"}
TODAY = dt.date(2026, 11, 16)          # a Monday session; the previous one is Friday 13 Nov
PREV = dt.date(2026, 11, 13)


@pytest.fixture
def master(temp_db, tmp_path, monkeypatch):
    """A database with init_db's tables and the fixture scrip master stored as today's (source dhan)."""
    import data.dhan as D
    from data import instruments as I
    from db.schema import get_connection, init_db
    init_db()
    path = tmp_path / "security_id_list.csv"
    path.write_text(MASTER)
    monkeypatch.setattr(D, "SEC_LIST_PATH", path)
    monkeypatch.setattr(D, "_SECURITY_MAP", {})
    monkeypatch.setattr(D, "_NOT_FOUND_WARNED", {})
    monkeypatch.setattr(I, "_nifty500_isins", lambda: {"ETERNAL": "INE758T01015", "EMBASSY": "INE041025011"})
    out = I.refresh(csv_path=path, kite=False)
    assert out["status"] == "SUCCESS" and out["rows"] == 13
    yield path
    I._invalidate()


# ═════ 5. instrument master ═════════════════════════════════════════════

def test_the_six_symbols_resolve_with_no_security_id_warning(master, caplog):
    """3,534 warnings in atip.log from 28 Sep to 13 Nov came from these six; on a full day of lookups there are none."""
    from data.dhan import get_security_id
    caplog.set_level(logging.DEBUG)
    for _ in range(3):                                          # the quote refresh asks every 15 minutes
        for sym, sid in SIX.items():
            assert get_security_id(sym)["security_id"] == sid, sym
    assert not [r for r in caplog.records if "security_id not found" in r.getMessage()]
    # a symbol nothing knows is still warned about -- once a day, then at debug level
    caplog.clear()
    assert get_security_id("NOSUCHSCRIP") == {} and get_security_id("NOSUCHSCRIP") == {}
    warned = [r for r in caplog.records if "security_id not found" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warned) == 1


def test_old_tickers_are_gone_from_the_default_universe_and_reits_from_the_equity_universe():
    import data.dhan as D
    from data.index_constituents import _extract_symbols
    syms = set(D._default_symbols())
    assert not syms & {"ZOMATO", "TATAMOTORS", "MOFSL"} and {"ETERNAL", "TMPV", "MOTILALOFS"} <= syms
    df = pd.DataFrame({"Symbol": ["RELIANCE", "EMBASSY", "BIRET", "BAGMANE", "TMCV", "DUMMYHEG"],
                       "Series": ["EQ", "RR", "RR", "RR", "EQ", "EQ"]})
    assert _extract_symbols(df) == ["RELIANCE", "TMCV"]


def test_lookup_gives_rupee_ticks_lot_sizes_and_the_equity_series(master, monkeypatch):
    from data import instruments as I
    r = I.lookup("zomato")
    assert (r["symbol"], r["security_id"], r["tick_size"], r["lot_size"], r["isin"]) == ("ETERNAL", "5097", 0.05, 1.0,
                                                                                         "INE758T01015")
    assert I.lookup("NSE:SIEMENS-EQ")["tick_size"] == 0.1 and I.lookup("MRF")["tick_size"] == 5.0
    m = I.lookup("MOTHERSON")
    assert (m["series"], m["security_id"], m["tick_size"]) == ("EQ", "4204", 0.01), "the share, not the NCD"
    e = I.lookup("EMBASSY")
    assert e["series"] == "RR" and e["equity"] is False and I.is_equity_symbol("EMBASSY") is False
    assert I.lookup("NOSUCHSCRIP") is None and I.lookup("") is None
    # cheap: after the day's first read a lookup is a dictionary hit
    calls = []
    real = I._load
    monkeypatch.setattr(I, "_load", lambda c: calls.append(1) or real(c))
    for s in ("RELIANCE", "SIEMENS", "TMPV") * 20:
        I.lookup(s)
    assert calls == []
    # orders/ticks.py reads its tick from here
    from orders.ticks import tick_size, to_tick
    assert str(tick_size("SIEMENS")) == "0.1" and str(tick_size("MOTHERSON")) == "0.01"
    assert to_tick(3903.3 * 0.95, "SELL", "STOP", symbol="SIEMENS") == 3708.1
    assert to_tick(888.0 * 1.005, "BUY", "ENTRY", symbol="INDHOTEL") == 892.4


def test_status_says_whether_todays_master_is_stored(master):
    from data import instruments as I
    st = I.status()
    assert st["fresh"] is True and st["rows"] == 13 and st["as_of"] == str(dt.date.today())
    assert I.status(today=dt.date.today() + dt.timedelta(days=1))["fresh"] is False


def test_a_kite_instruments_csv_is_read_and_ranks_after_dhan(master):
    from data import instruments as I
    from db.schema import get_connection
    csv_text = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
                "instrument_type,segment,exchange\n"
                "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.05,1,EQ,NSE,NSE\n"
                "1234567,4823,KITEONLY,KITE ONLY LTD,0,,0,0.01,1,EQ,NSE,NSE\n"
                "13368834,52222,NIFTY26NOVFUT,NIFTY,0,2026-11-26,0,0.1,75,FUT,NFO-FUT,NFO\n")
    rows = I.parse_kite_instruments(csv_text)
    assert [(r["symbol"], r["tick_size"], r["lot_size"]) for r in rows] == [("RELIANCE", 0.05, 1.0), ("KITEONLY", 0.01, 1.0)]
    conn = get_connection()
    try:
        I.store(conn, rows, "kite")
        conn.commit()
    finally:
        conn.close()
    assert I.lookup("RELIANCE")["source"] == "dhan" and I.lookup("RELIANCE")["tick_size"] == 0.1
    k = I.lookup("KITEONLY")
    assert (k["source"], k["security_id"], k["tick_size"]) == ("kite", "1234567", 0.01)


def test_the_kite_refresh_only_runs_when_zerodha_is_switched_on(master, monkeypatch):
    from data import instruments as I
    import portfolio.zerodha as Z
    monkeypatch.setattr(Z, "enabled", lambda: False)
    assert I.refresh_kite(None) is None


# ═════ 6. the pre-open gate ═════════════════════════════════════════════

@pytest.fixture
def gate(master, monkeypatch, tmp_path):
    """Everything healthy at 08:45 on 16 Nov: the probes answer, today's master is stored, 13 Nov is scored."""
    import ops.preopen_health as P
    from data import instruments as I
    from db.schema import get_connection
    monkeypatch.delenv("ATIP_PREOPEN_GATE", raising=False)      # tests/conftest.py turns the gate off
    conn = get_connection()
    I._log(conn, "dhan", TODAY, "SUCCESS", 13)
    conn.execute("INSERT INTO ai_scores (symbol, date, zpi) VALUES ('RELIANCE', ?, 60)", (str(PREV),))
    conn.commit()
    calls = {"profile": 0, "quote": 0, "renew": 0}
    state = {"profile": {"ok": True, "data_plan": "Active"},
             "quote": {"ok": True, "ltp": 1512.4, "last_trade_time": "13/11/2026 15:59:52"},
             "renew": {"ok": False, "detail": "Invalid TOTP"}}

    def probe(kind):
        def f(*a, **k):
            calls[kind] += 1
            v = state[kind]
            return v.pop(0) if isinstance(v, list) else dict(v)
        return f
    monkeypatch.setattr(P, "_probe_profile", probe("profile"))
    monkeypatch.setattr(P, "_probe_quote", probe("quote"))
    monkeypatch.setattr(P, "_renew_token", probe("renew"))
    monkeypatch.setattr(I, "ensure_today", lambda conn=None: {"status": "SKIPPED"})
    monkeypatch.setattr(P, "_now", lambda: dt.datetime(2026, 11, 16, 8, 45))
    yield {"P": P, "conn": conn, "calls": calls, "state": state, "tmp": tmp_path}
    conn.close()


def _at(h, m, s=0):
    return dt.datetime(2026, 11, 16, h, m, s)


def test_before_0845_and_on_a_holiday_the_gate_is_open_without_checking(gate):
    P, conn = gate["P"], gate["conn"]
    assert P.gate_status(conn, now=_at(8, 44, 59)) == {"ok": True, "reason": None, "checked_at": None, "checks": [],
                                                       "state": "BEFORE_CHECK"}
    sunday = dt.datetime(2026, 11, 15, 10, 0)
    r = P.gate_status(conn, now=sunday)
    assert r["ok"] is True and r["reason"] is None and r["state"] == "NOT_A_MARKET_DAY"
    assert gate["calls"] == {"profile": 0, "quote": 0, "renew": 0}
    P.ensure_table(conn)
    assert conn.execute("SELECT COUNT(*) FROM preopen_health").fetchone()[0] == 0


def test_after_0845_with_nothing_stored_the_checks_run_on_demand_and_pass(gate):
    P, conn = gate["P"], gate["conn"]
    r = P.gate_status(conn, now=_at(9, 15))
    assert r["ok"] is True and r["reason"] is None and r["trigger"] == "on_demand" and r["state"] == "OK"
    assert [c["name"] for c in r["checks"]] == list(P.CHECKS) and all(c["ok"] for c in r["checks"])
    assert r["checked_at"] == "2026-11-16T09:15:00"
    # a passed gate is read back, not re-checked
    assert P.gate_status(conn, now=_at(9, 30))["id"] == r["id"] and gate["calls"]["profile"] == 1


def test_dh901_blocks_auto_pick_and_says_why(gate):
    P, conn = gate["P"], gate["conn"]
    gate["state"]["profile"] = {"ok": False, "code": "DH-901", "error": "Client ID or user generated access token "
                                                                         "is invalid or expired."}
    r = P.gate_status(conn, now=_at(9, 15))
    assert r["ok"] is False and r["state"] == "BLOCKED"
    assert r["reason"] == "Dhan refused the token (DH-901)"
    assert gate["calls"]["quote"] == 0, "no quote probe on a refused token"
    assert gate["calls"]["renew"] == 0, "an on-demand check never renews (the scheduled runs do)"


def test_a_stale_quote_feed_blocks(gate):
    P, conn = gate["P"], gate["conn"]
    gate["state"]["quote"] = {"ok": True, "ltp": 1490.0, "last_trade_time": "11/11/2026 15:29:59"}
    r = P.gate_status(conn, now=_at(9, 0))
    assert r["ok"] is False and r["reason"].startswith("quote feed stale")
    assert "11 Nov 15:29" in r["reason"] and "13 Nov" in r["reason"]
    gate["state"]["quote"] = {"ok": False, "error": "806: Data APIs not Subscribed"}
    r = P.run_checks(conn, TODAY, _at(9, 1))
    assert r["reason"] == "no quote from Dhan (806: Data APIs not Subscribed)"


def test_no_scrip_master_today_blocks(gate):
    P, conn = gate["P"], gate["conn"]
    conn.execute("DELETE FROM instrument_master_log")
    from data import instruments as I
    I._log(conn, "dhan", PREV, "SUCCESS", 13)
    conn.commit()
    r = P.gate_status(conn, now=_at(8, 46))
    assert r["ok"] is False and r["reason"] == "scrip master not refreshed today (last 2026-11-13)"


def test_no_scores_for_the_previous_session_blocks(gate):
    P, conn = gate["P"], gate["conn"]
    conn.execute("DELETE FROM ai_scores")
    conn.commit()
    r = P.gate_status(conn, now=_at(8, 46))
    assert r["ok"] is False and r["reason"] == "no scores for the previous session (13 Nov)"


def test_a_failing_gate_is_rechecked_on_demand_at_most_every_two_minutes(gate):
    P, conn = gate["P"], gate["conn"]
    gate["state"]["profile"] = [{"ok": False, "code": "DH-901"}, {"ok": True}]
    first = P.gate_status(conn, now=_at(9, 15))
    assert first["ok"] is False
    assert P.gate_status(conn, now=_at(9, 16))["id"] == first["id"], "inside RECHECK_SECONDS: the stored result"
    again = P.gate_status(conn, now=_at(9, 17, 1))
    assert again["ok"] is True and again["id"] != first["id"], "fixed by hand: the gate clears itself"


def test_the_scheduled_run_renews_the_token_alerts_once_and_retries_with_backoff(gate, monkeypatch):
    P, conn = gate["P"], gate["conn"]
    from db.schema import get_connection
    gate["state"]["profile"] = {"ok": False, "code": "DH-901"}
    out = P.run_scheduled()
    assert out["status"] == "BLOCKED" and "DH-901" in out["error"]
    assert gate["calls"]["renew"] == 1
    monkeypatch.setattr(P, "_now", lambda: _at(8, 50))
    P.run_scheduled("retry")                                     # still failing: renewal retried, no second alert
    assert gate["calls"]["renew"] == 2
    c = get_connection()
    try:
        alerts = c.execute("SELECT title, dedupe_key FROM alert_log ORDER BY id").fetchall()
        assert [tuple(a) for a in alerts] == [("Pre-open check failed", "preopen_gate:2026-11-16")]
        # 09:00: the renewal works this time -> the gate passes, the owner is told, later retries do nothing
        gate["state"]["renew"] = {"ok": True, "detail": "OK: new Dhan token saved via generateAccessToken"}
        gate["state"]["profile"] = [{"ok": False, "code": "DH-901"}, {"ok": True}]
        monkeypatch.setattr(P, "_now", lambda: _at(9, 0))
        assert P.run_scheduled("retry")["status"] == "SUCCESS"
        assert P.latest(c, TODAY)["renewed"] is True
        assert c.execute("SELECT title FROM alert_log ORDER BY id DESC").fetchone()[0] == "Pre-open check passed"
        monkeypatch.setattr(P, "_now", lambda: _at(9, 10))
        assert P.run_scheduled("retry") == {"status": "SKIPPED", "reason": "today's pre-open check already passed"}
        assert P.gate_status(c, now=_at(9, 15))["ok"] is True
    finally:
        c.close()
    assert P.RETRY_TIMES == ("08:50", "09:00", "09:10")         # 5, 10, 10 minutes


def test_the_banner_says_why_auto_pick_waits_and_reads_stored_rows_only(gate, monkeypatch):
    P, conn = gate["P"], gate["conn"]
    gate["state"]["profile"] = {"ok": False, "code": "DH-901"}
    P.run_checks(conn, TODAY, _at(8, 45), trigger="scheduled")
    b = P.banner(now=_at(8, 52))
    assert b["severity"] == "error" and b["failed"] == ["dhan_token", "quote_feed"]
    assert b["message"].startswith("Auto-pick is waiting: the pre-open check at 08:45 failed — "
                                   "Dhan refused the token (DH-901). Next automatic check at 09:00 IST")
    assert [f["cmd"] for f in b["fix"]] == ["tools/dhan_token_refresh.sh"]
    assert P.banner(now=_at(15, 30)) is None and P.banner(now=_at(8, 40)) is None
    calls = dict(gate["calls"])
    from dashboard.global_asia import status_banner_html, status_payload
    monkeypatch.setattr(P, "_now", lambda: _at(8, 52))
    assert status_payload()["preopen_gate"]["state"] == "BLOCKED"
    html = status_banner_html()
    assert 'id="atipGateBanner"' in html and "gate(d&amp;&amp;d.preopen_gate)" not in html
    assert "gate(d&&d.preopen_gate)" in html
    assert gate["calls"] == calls, "a page render never probes Dhan"


def test_switched_off_the_gate_is_always_open(gate, monkeypatch):
    """ATIP_PREOPEN_GATE=off (every other test, tests/conftest.py) or config preopen_health.enabled false."""
    P, conn = gate["P"], gate["conn"]
    gate["state"]["profile"] = {"ok": False, "code": "DH-901"}
    assert P.run_checks(conn, TODAY, _at(8, 45))["ok"] is False
    monkeypatch.setenv("ATIP_PREOPEN_GATE", "off")
    r = P.gate_status(conn, now=_at(9, 15))
    assert (r["ok"], r["reason"], r["state"]) == (True, None, "DISABLED")
    assert P.run_scheduled()["status"] == "SKIPPED" and P.banner(now=_at(9, 15)) is None
    monkeypatch.delenv("ATIP_PREOPEN_GATE")
    import json
    monkeypatch.setattr(P, "CONFIG_PATH", gate["tmp"] / "config.json")
    (gate["tmp"] / "config.json").write_text(json.dumps({"preopen_health": {"enabled": False}}))
    assert P.gate_status(conn, now=_at(9, 15))["state"] == "DISABLED"
    assert gate["calls"]["profile"] == 1


def test_the_viewer_never_checks_renews_or_alerts(gate, monkeypatch):
    P, conn = gate["P"], gate["conn"]
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    assert P.run_scheduled()["status"] == "SKIPPED"
    r = P.gate_status(conn, now=_at(9, 15))
    assert r["ok"] is True and r["state"] == "NOT_CHECKED"
    assert gate["calls"] == {"profile": 0, "quote": 0, "renew": 0}


# ═════ 6. Telegram retry with backoff ═══════════════════════════════════

class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: https://api.telegram.org/bot123:SECRET/x",
                                     response=self)


@pytest.fixture
def tg(monkeypatch):
    import alerts.telegram as tg
    monkeypatch.setattr(tg, "load_config", lambda: {"telegram_token": "123:SECRET", "telegram_chat_id": "9"})
    slept = []
    monkeypatch.setattr(tg, "_telegram_sleep", slept.append)
    return tg, slept


def test_telegram_retries_a_network_blip_with_backoff(tg, monkeypatch):
    import requests
    tg, slept = tg
    seq = [requests.ConnectionError("('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"),
           requests.ReadTimeout("HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out."),
           _Resp(200)]

    def post(url, **kw):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(tg.requests, "post", post)
    assert tg._real_deliver_telegram("hi") == (True, None)
    assert slept == [2.0, 5.0] and seq == []


def test_telegram_gives_up_after_three_attempts_without_leaking_the_token(tg, monkeypatch):
    import requests
    tg, slept = tg
    n = []

    def post(url, **kw):
        n.append(1)
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(tg.requests, "post", post)
    ok, err = tg._real_deliver_telegram("hi")
    assert not ok and len(n) == 3 and slept == [2.0, 5.0] and "SECRET" not in err and "<token>" in err


def test_telegram_does_not_retry_a_wrong_token_and_honours_429_retry_after(tg, monkeypatch):
    tg, slept = tg
    seq = [_Resp(401)]
    monkeypatch.setattr(tg.requests, "post", lambda url, **kw: seq.pop(0))
    ok, err = tg._real_deliver_telegram("hi")
    assert not ok and slept == [] and "SECRET" not in err
    seq[:] = [_Resp(429, {"parameters": {"retry_after": 7}}), _Resp(502), _Resp(200)]
    assert tg._real_deliver_telegram("hi") == (True, None) and slept == [7.0, 5.0]


# ═════ 7. Bhavcopy retry, then Dhan's EOD ═══════════════════════════════

class _Clock:
    def __init__(self, start):
        self.t, self.slept = start, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += dt.timedelta(seconds=s)


@pytest.fixture
def eod(temp_db, monkeypatch):
    from data import bhavcopy as B
    import data.dhan as D
    from db.schema import init_db
    init_db()
    clock = _Clock(dt.datetime(2026, 11, 16, 16, 45, 10))
    monkeypatch.setattr(B, "_eod_now", clock.now)
    monkeypatch.setattr(B, "_eod_sleep", clock.sleep)
    attempts = []

    def bhav(td):
        attempts.append((td, clock.now()))
        clock.t += dt.timedelta(seconds=31)                     # 12 Nov: three tries in 31 s
        return {"date": str(td), "rows": 0, "status": "FAILED", "error": f"Failed to download Bhavcopy for {td}"}
    monkeypatch.setattr(B, "run_bhavcopy_pipeline", bhav)
    fetched = []

    def daily(sym, frm, to, dhan=None):
        fetched.append((sym, frm, to))
        if sym == "CCC":
            return pd.DataFrame()
        return pd.DataFrame({"date": [TODAY - dt.timedelta(days=3), TODAY], "open": [99.0, 100.0], "high": [101.0, 104.0],
                             "low": [98.0, 99.5], "close": [100.0, 103.0], "volume": [1000, 2000],
                             "symbol": sym, "source": "dhan"})
    monkeypatch.setattr(D, "HAS_DHAN", True)
    monkeypatch.setattr(D, "get_dhan_client", lambda: (object(), None))
    monkeypatch.setattr(D, "get_tracked_symbols", lambda conn=None: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(D, "fetch_historical_daily", daily)
    monkeypatch.setattr(D, "fetch_live_quotes", lambda syms, dhan=None: pd.DataFrame(
        [{"symbol": "CCC", "ltp": 55.5, "open": 54.0, "high": 56.0, "low": 53.5, "volume": 7000}]))
    return {"B": B, "clock": clock, "attempts": attempts, "fetched": fetched, "bhav": bhav}


def test_bhavcopy_is_retried_with_backoff_then_dhan_fills_the_day_and_both_sources_are_logged(eod, caplog):
    from db.schema import get_connection
    B, clock = eod["B"], eod["clock"]
    caplog.set_level(logging.INFO)
    out = B.run_eod_prices(TODAY)
    # 16:45:10, 16:46:41, 16:49:12 (each attempt takes 31 s); a 4th after 180 s would end past 16:52
    assert [a[1].strftime("%H:%M:%S") for a in eod["attempts"]] == ["16:45:10", "16:46:41", "16:49:12"]
    assert clock.slept == [60, 120] and clock.t <= dt.datetime(2026, 11, 16, 16, 52), "Dhan by 16:52: scores by 17:00"
    assert out["status"] == "SUCCESS" and out["source"] == "dhan" and out["rows"] == 3 and out["attempts"] == 3
    # Dhan's toDate is exclusive: the window runs to td + 1
    assert {to for _, _, to in eod["fetched"]} == {TODAY + dt.timedelta(days=1)}
    conn = get_connection()
    try:
        bars = {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT symbol, open, high, low, close, volume, source FROM prices_daily WHERE date=? ORDER BY symbol",
            (str(TODAY),))}
        assert bars == {"AAA": (100.0, 104.0, 99.5, 103.0, 2000, "dhan"), "BBB": (100.0, 104.0, 99.5, 103.0, 2000, "dhan"),
                        "CCC": (54.0, 56.0, 53.5, 55.5, 7000, "dhan")}, "the candle, else the closing quote"
        src = [tuple(r) for r in conn.execute("SELECT source, attempt, status, `rows` FROM eod_price_source ORDER BY id")]
        assert src == [("bhavcopy", i, "FAILED", 0) for i in (1, 2, 3)] + [("dhan", None, "SUCCESS", 3)]
        assert B.day_source(TODAY) == "dhan"
        assert conn.execute("SELECT status, rows_processed FROM pipeline_log WHERE job_name='dhan_eod_fallback'"
                            ).fetchone()[:] == ("SUCCESS", 3)
        alert = conn.execute("SELECT title, dedupe_key FROM alert_log").fetchone()
        assert tuple(alert) == ("EOD prices: Bhavcopy late", "eod_prices:2026-11-16")
    finally:
        conn.close()
    text = caplog.text
    assert "Bhavcopy 2026-11-16: attempt 1 failed" in text and "no time for attempt 4 before 16:52" in text
    assert "Dhan EOD for 2026-11-16: 2 daily candles + 1 closing" in text


def test_a_catch_up_run_falls_back_at_once_and_a_late_bhavcopy_upgrades_the_day(eod, monkeypatch):
    B, clock = eod["B"], eod["clock"]
    clock.t = dt.datetime(2026, 11, 17, 18, 30)                 # the next evening: no waiting for 16 Nov
    out = B.run_eod_prices(TODAY)
    assert out["source"] == "dhan" and len(eod["attempts"]) == 1 and clock.slept == []
    # 17 Nov's run: NSE's archive has 16 Nov now -> the Dhan-filled day is upgraded first
    monkeypatch.setattr(B, "run_bhavcopy_pipeline", lambda td: {"date": str(td), "rows": 3339, "status": "SUCCESS"})
    out = B.run_eod_prices(dt.date(2026, 11, 17))
    assert out["source"] == "bhavcopy" and out["upgraded"] == ["2026-11-16"]
    assert B.day_source(TODAY) == "bhavcopy" and B.day_source(dt.date(2026, 11, 17)) == "bhavcopy"


def test_a_bhavcopy_that_arrives_on_the_second_try_needs_no_dhan(eod, monkeypatch):
    B = eod["B"]
    seq = [{"status": "FAILED", "rows": 0, "error": "404"}, {"status": "SUCCESS", "rows": 3339}]
    monkeypatch.setattr(B, "run_bhavcopy_pipeline", lambda td: seq.pop(0))
    out = B.run_eod_prices(TODAY)
    assert out["source"] == "bhavcopy" and out["attempts"] == 2 and eod["fetched"] == []
    assert eod["clock"].slept == [60] and B.day_source(TODAY) == "bhavcopy"


def test_the_post_market_job_runs_the_fallback_under_the_old_job_name():
    import inspect
    from pipeline import scheduler as S
    src = inspect.getsource(S._run_postmarket)
    assert 'run_job("bhavcopy_eod", run_eod_prices, td)' in src


# ═════ 18. completeness and the backfill tool ═══════════════════════════

def _bars(conn, day, symbols, n=25):
    t0 = dt.datetime.combine(day, dt.time(9, 15))
    conn.executemany("INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume, source) "
                     "VALUES (?,?,15,1,1,1,1,1,'dhan')",
                     [(s, (t0 + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S")) for s in symbols
                      for i in range(n)])


def test_completeness_under_95_percent_alerts_once_per_session(temp_db, monkeypatch):
    import data.dhan as D
    from data import completeness as C
    from db.schema import get_connection, init_db
    init_db()
    universe = [f"S{i:02d}" for i in range(40)]
    monkeypatch.setattr(D, "get_tracked_symbols", lambda conn=None: universe)
    conn = get_connection()
    _bars(conn, PREV, universe)                                  # 13 Nov: 40/40
    _bars(conn, TODAY, universe[:37])                            # 16 Nov: 37/40 = 92.5%
    conn.commit()
    out = C.run(conn, today=TODAY)
    assert out["status"] == "PARTIAL" and out["alerted"] == ["2026-11-16"]
    assert out["today"]["have"] == 37 and out["today"]["share"] == 0.925 and out["today"]["ok"] is False
    row = conn.execute("SELECT universe, have, ok, alerted, missing_sample FROM data_completeness WHERE "
                       "session_date='2026-11-16'").fetchone()
    assert tuple(row) == (40, 37, 0, 1, "S37,S38,S39")
    assert conn.execute("SELECT ok FROM data_completeness WHERE session_date='2026-11-13'").fetchone()[0] == 1
    body = conn.execute("SELECT message FROM alert_log WHERE title='Intraday bars incomplete'").fetchone()[0]
    assert "2026-11-16: 37/40 symbols (92%)" in body and "--from 2026-11-16 --to 2026-11-16 --apply" in body
    assert C.run(conn, today=TODAY)["alerted"] == [], "a session is alerted once"
    assert conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0] == 1
    _bars(conn, TODAY, universe[37:])
    conn.commit()
    assert C.run(conn, today=TODAY)["status"] == "SUCCESS"
    conn.close()


def test_the_backfill_tool_measures_the_gap_and_writes_nothing_in_a_dry_run(tmp_path, capsys):
    import tools.backfill_intraday_bars as T
    db = tmp_path / "copy.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE intraday_bars (symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, interval_min INTEGER NOT NULL, "
              "open REAL, high REAL, low REAL, close REAL, volume INTEGER, source TEXT DEFAULT 'dhan', created_at "
              "TIMESTAMP, PRIMARY KEY (symbol, interval_min, ts))")
    _bars(c, dt.date(2026, 10, 5), ["AAA", "BBB", "CCC"])
    _bars(c, dt.date(2026, 10, 26), ["AAA", "BBB", "CCC"])
    _bars(c, dt.date(2026, 10, 7), ["AAA"])
    c.commit()
    before = c.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
    c.close()
    assert T.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "13 sessions, universe 3 symbols" in out and "Gap: 38 symbol-sessions, about 950 bars" in out
    assert "Dry run: nothing written, Dhan not called" in out
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0] == before
    c.close()


def test_the_backfill_tool_can_count_a_session_cut_short_as_missing(tmp_path, capsys):
    # 16 Nov 2026: a stuck job lock stopped the 15-minute bars at 10:15 IST -- every symbol had 4-5 bars, so
    # "no bar at all" found nothing to fill. --min-bars counts a short session as missing (CAP-06).
    import tools.backfill_intraday_bars as T
    db = tmp_path / "copy.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE intraday_bars (symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, interval_min INTEGER NOT NULL, "
              "open REAL, high REAL, low REAL, close REAL, volume INTEGER, source TEXT DEFAULT 'dhan', created_at "
              "TIMESTAMP, PRIMARY KEY (symbol, interval_min, ts))")
    _bars(c, dt.date(2026, 11, 12), ["AAA", "BBB", "CCC"])
    _bars(c, dt.date(2026, 11, 13), ["AAA", "BBB", "CCC"])
    _bars(c, dt.date(2026, 11, 16), ["AAA", "BBB"], n=5)
    c.commit()
    c.close()
    assert T.main(["--db", str(db), "--from", "2026-11-16", "--to", "2026-11-16"]) == 0
    assert "Gap: 1 symbol-sessions, about 25 bars" in capsys.readouterr().out
    assert T.main(["--db", str(db), "--from", "2026-11-16", "--to", "2026-11-16", "--min-bars", "20"]) == 0
    out = capsys.readouterr().out
    assert "fewer than 20 bars counts as missing" in out
    assert "Gap: 3 symbol-sessions, about 65 bars" in out and "Dry run: nothing written" in out


def test_the_backfill_tool_refreshes_only_the_bars_stored_while_forming(tmp_path, capsys, monkeypatch):
    # 16 Nov 2026: the 10:01 IST run finished at 10:16 and stored each symbol's last bar while it was still forming;
    # the job's next run, which rewrites it, never came. --refresh-partial stores Dhan's finished values for those.
    import pandas as pd
    import data.dhan as D
    import tools.backfill_intraday_bars as T
    db = tmp_path / "copy.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE intraday_bars (symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, interval_min INTEGER NOT NULL, "
              "open REAL, high REAL, low REAL, close REAL, volume INTEGER, source TEXT DEFAULT 'dhan', created_at "
              "TIMESTAMP, PRIMARY KEY (symbol, interval_min, ts))")
    _bars(c, dt.date(2026, 11, 13), ["AAA"])
    _bars(c, dt.date(2026, 11, 16), ["AAA"], n=4)              # 09:15 .. 10:00, value 1
    c.execute("UPDATE intraday_bars SET created_at='2026-11-16 10:02:20.5' WHERE ts>='2026-11-16'")
    c.commit()
    c.close()
    finished = pd.DataFrame([{"datetime": pd.Timestamp(2026, 11, 16, 9, 15) + pd.Timedelta(minutes=15 * i),
                              "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 99} for i in range(25)])
    calls = []
    monkeypatch.setattr(D, "get_dhan_client", lambda: (object(), None))
    monkeypatch.setattr(D, "fetch_historical_intraday", lambda *a: calls.append(a[0]) or finished)
    monkeypatch.chdir(tmp_path)
    args = ["--db", str(db), "--from", "2026-11-16", "--to", "2026-11-16", "--refresh-partial"]
    assert T.main(args) == 0
    assert "Stored while still forming: 1 bars of 1 symbols" in capsys.readouterr().out and not calls
    assert T.main(args + ["--apply"]) == 0
    assert "Refreshed 1 bars stored while forming, 1 symbols" in capsys.readouterr().out and calls == ["AAA"]
    c = sqlite3.connect(db)
    rows = dict(c.execute("SELECT ts, close FROM intraday_bars WHERE ts>='2026-11-16'").fetchall())
    c.close()
    assert rows == {"2026-11-16 09:15:00": 1, "2026-11-16 09:30:00": 1, "2026-11-16 09:45:00": 1,
                    "2026-11-16 10:00:00": 2.5}                 # finished bars untouched; nothing inserted
    assert T.main(args) == 0 and "Stored while still forming: 0 bars" in capsys.readouterr().out


# ═════ the jobs ═════════════════════════════════════════════════════════

def test_the_0800_0845_and_1615_jobs_are_registered(monkeypatch):
    import schedule
    monkeypatch.delenv("ATIP_PREOPEN_GATE", raising=False)
    from pipeline import w39_jobs as W
    from ops import preopen_health as P
    schedule.clear()
    try:
        lines = W.schedule_jobs(schedule, lambda *a, **k: None)
        jobs = {}
        for j in schedule.get_jobs():
            args = j.job_func.args
            jobs.setdefault(args[0], []).append((j.at_time.strftime("%H:%M"), args[1:]))
        assert jobs["instrument_master"] == [("08:00", (W.instrument_master_run,))]
        assert jobs["preopen_health"] == [("08:45", (P.run_scheduled,)), ("08:50", (P.run_scheduled, "retry")),
                                          ("09:00", (P.run_scheduled, "retry")), ("09:10", (P.run_scheduled, "retry"))]
        assert jobs["intraday_completeness"] == [("16:15", (W.intraday_completeness_run,))]
        assert any(l.startswith("preopen_health daily 08:45") for l in lines)
    finally:
        schedule.clear()


# ═════ 21. the Kite connector stays read-only ═══════════════════════════

ORDER_CHANGING = re.compile(r"^(place|modify|cancel|delete|exit|convert)_|^(submit|cancel|modify)$")


def test_no_connector_exposes_a_method_that_changes_an_order_or_a_gtt():
    from brokers.registry import CONNECTORS
    for name, cls in CONNECTORS.items():
        bad = [m for m in dir(cls) if not m.startswith("_") and ORDER_CHANGING.search(m)]
        assert bad == [], f"{name} connector exposes {bad}"
    from brokers.connectors import ZerodhaConnector
    assert {"gtts", "instruments", "quote", "historical"} <= set(dir(ZerodhaConnector))


def test_the_kite_reads_call_only_read_endpoints_and_are_paced(monkeypatch):
    from brokers import connectors as K

    class FakeKite:
        calls = []

        def __getattr__(self, name):
            if ORDER_CHANGING.search(name) or name in ("place_gtt", "modify_gtt", "delete_gtt"):
                raise AssertionError(f"order-changing Kite call {name}")
            raise AttributeError(name)

        def get_gtts(self):
            self.calls.append("get_gtts")
            return [{"id": 1, "status": "active"}, {"id": 2, "status": "triggered"}]

        def instruments(self, exchange=None):
            self.calls.append(f"instruments:{exchange}")
            return [{"instrument_token": 738561, "exchange_token": 2885, "tradingsymbol": "RELIANCE", "name": "RELIANCE",
                     "tick_size": 0.05, "lot_size": 1, "instrument_type": "EQ", "segment": "NSE", "exchange": "NSE",
                     "expiry": "", "strike": 0.0}]

        def quote(self, *ins):
            self.calls.append(f"quote:{','.join(ins)}")
            return {i: {"last_price": 1.0} for i in ins}

        def historical_data(self, token, frm, to, interval):
            self.calls.append(f"historical:{token}:{interval}")
            return [{"date": frm, "close": 1.0}]

    fake = FakeKite()
    c = K.ZerodhaConnector({"api_key": "k", "access_token": "t"})
    monkeypatch.setattr(c, "_kite", lambda: fake)
    waits = []
    monkeypatch.setattr(K.time, "sleep", waits.append)
    monkeypatch.setattr(K, "_kite_next", {})
    assert [g["id"] for g in c.gtts()] == [1, 2]
    ins = c.instruments("NSE")
    assert (ins[0]["tradingsymbol"], ins[0]["tick_size"], ins[0]["lot_size"]) == ("RELIANCE", 0.05, 1.0)
    c.quote(["NSE:INFY"])
    c.quote(["NSE:TCS"])
    for _ in range(3):
        c.historical(738561, dt.date(2026, 11, 1), dt.date(2026, 11, 13))
    assert fake.calls[:4] == ["get_gtts", "instruments:NSE", "quote:NSE:INFY", "quote:NSE:TCS"]
    assert len(waits) == 3 and 0.9 < waits[0] <= 1.0, "quotes one a second, historical three a second"
    assert all(0 < w <= 2 / 3 + 1e-6 for w in waits[1:])
    from data.instruments import parse_kite_instruments
    assert parse_kite_instruments(ins)[0]["tick_size"] == 0.05


def test_csv_text_is_never_taken_for_a_file_name(monkeypatch):
    """23 Nov 2026: on Linux (the VPS, CI) asking the filesystem whether 100 kB of CSV text is a file raises
    OSError ENAMETOOLONG instead of answering False, so the Kite refresh failed there. CSV text is never
    looked up as a path, and a lookup that raises counts as "not a file"."""
    from pathlib import Path
    from data import instruments as I

    def linux_says_too_long(self):
        raise OSError(36, "File name too long")

    monkeypatch.setattr(Path, "is_file", linux_says_too_long)
    rows = I.parse_kite_instruments("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
                                    "tick_size,lot_size,instrument_type,segment,exchange\n"
                                    "1,2,ABC,ABC LTD,0,,0,0.05,1,EQ,NSE,NSE\n")
    assert [r["symbol"] for r in rows] == ["ABC"]
    assert I._is_file("x" * 300) is False
