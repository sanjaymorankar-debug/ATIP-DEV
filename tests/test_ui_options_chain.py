"""
W6 (options builder upgrade): GET /api/v2/options/chain/{symbol} (dashboard/options_v2.py) and the browser panel
(dashboard/static/atip/options.js + options.css) -- ATM-centred rows, Greeks with the right signs, PCR and max pain,
null (never 0) for what is not stored, 400s, the no-data message, live vs bhavcopy and the stale flag.
"""
import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from quant.derivatives import bs_price
from quant.options_strategy import RISK_FREE

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "dashboard" / "static" / "atip" / "options.js"
CSS = ROOT / "dashboard" / "static" / "atip" / "options.css"

SESSION = "2026-10-30"                       # a Friday
EXP1, EXP2 = "2026-11-03", "2026-11-17"
SPOT = 24010.0
STRIKES = [23500.0 + 100 * i for i in range(11)]          # 23500 .. 24500
AFTER_BHAV = dt.datetime(2026, 10, 30, 20, 0)             # the bhavcopy for 30 Oct is out
# small, hand-checked OI grid on EXP2 for PCR / max pain (see test_pcr_and_max_pain)
OI2 = {23800.0: (100, 900), 23900.0: (200, 700), 24000.0: (500, 600), 24100.0: (800, 200), 24200.0: (1000, 100)}


def _bhav(conn, symbol="NIFTY", day=SESSION, spot=SPOT):
    T1 = (dt.date.fromisoformat(EXP1) - dt.date.fromisoformat(day)).days / 365
    sql = ("INSERT INTO fo_contract_daily (date, symbol, instrument, expiry, strike, option_type, close, settle, "
           "prev_close, oi, oi_chg, volume, underlying, lot_size, iv) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    for K in STRIKES:
        for kind in ("CE", "PE"):
            px = round(bs_price(spot, K, T1, RISK_FREE, 0.14, "call" if kind == "CE" else "put"), 2)
            if K == 24500.0 and kind == "CE":          # stored without a price, OI or IV: everything null
                conn.execute(sql, (day, symbol, "OPTIDX", EXP1, K, kind, None, None, None, None, None, None, spot, 75,
                                   None))
                continue
            iv = 14.0 if K <= 24000 else None          # half stored, half to be implied from the price
            oi = 1000 + 100 * (K - 23500) / 100 if kind == "CE" else 2000 - 100 * (K - 23500) / 100
            conn.execute(sql, (day, symbol, "OPTIDX", EXP1, K, kind, px, px, px - 5, oi, 50 if kind == "CE" else -40,
                               500, spot, 75, iv))
    for K, (c, p) in OI2.items():
        for kind, oi in (("CE", c), ("PE", p)):
            conn.execute(sql, (day, symbol, "OPTIDX", EXP2, K, kind, 100.0, 100.0, 90.0, oi, 0, 10, spot, 75, None))
    conn.execute(sql, (day, symbol, "OPTIDX", EXP2, 24300.0, "CE", 50.0, 50.0, 40.0, None, None, None, spot, 75, None))
    conn.commit()


@pytest.fixture
def api(temp_db, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.concurrency import run_in_threadpool

    from dashboard import options_v2
    from db.schema import get_connection, init_db
    init_db()
    clock = {"now": AFTER_BHAV}
    monkeypatch.setattr(options_v2, "now_ist", lambda: clock["now"])

    def call(fn):                                   # as dashboard/w39_routes.py's run
        conn = get_connection()
        try:
            return JSONResponse(fn(conn))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(call, fn)

    app = FastAPI()
    options_v2.register(app, guard=[], run=run)
    conn = get_connection()
    try:
        _bhav(conn)
    finally:
        conn.close()
    return TestClient(app), clock, get_connection


def _get(client, url):
    r = client.get(url)
    body = json.loads(r.text, parse_constant=lambda c: pytest.fail(f"{c} in the JSON"))
    return r.status_code, body


def test_rows_are_centred_on_the_atm_strike(api):
    client, _, _ = api
    code, c = _get(client, "/api/v2/options/chain/NIFTY?strikes=3")
    assert code == 200, c
    assert c["status"] == "OK" and c["source"] == "bhavcopy" and c["as_of"] == "2026-10-30 15:30"
    assert c["spot"] == SPOT and c["lot_size"] == 75 and c["strike_step"] == 100
    assert c["expiries"] == [EXP1, EXP2] and c["expiry"] == EXP1, "nearest expiry by default"
    assert c["days_to_expiry"] == 4 and c["stale"] is False
    assert c["atm_strike"] == 24000.0
    ks = [r["strike"] for r in c["rows"]]
    assert ks == [23700.0, 23800.0, 23900.0, 24000.0, 24100.0, 24200.0, 24300.0]
    assert [r["atm"] for r in c["rows"]] == [False, False, False, True, False, False, False]
    first = c["rows"][0]
    assert first["ce"]["ltp"] > first["pe"]["ltp"] and first["ce"]["itm"] is True and first["pe"]["itm"] is False
    assert first["ce"]["change"] == 5.0 and first["ce"]["oi_change"] == 50 and first["pe"]["oi_change"] == -40
    # strikes=N clamps to 1..40; the default is 10 each side (here: every stored strike)
    assert len(_get(client, "/api/v2/options/chain/NIFTY?strikes=1")[1]["rows"]) == 3
    assert len(_get(client, "/api/v2/options/chain/NIFTY")[1]["rows"]) == len(STRIKES)
    assert _get(client, "/api/v2/options/chain/NIFTY?strikes=0")[1]["strikes"] == 1


def test_greeks_have_the_right_signs_and_iv_is_implied_when_not_stored(api):
    client, _, _ = api
    c = _get(client, "/api/v2/options/chain/NIFTY")[1]
    priced = [r for r in c["rows"] if r["strike"] != 24500.0]
    for r in priced:
        ce, pe = r["ce"], r["pe"]
        assert 0 < ce["delta"] < 1 and -1 < pe["delta"] < 0, r["strike"]
        assert ce["gamma"] > 0 and pe["gamma"] > 0
        assert ce["theta"] < 0 and pe["theta"] < 0, "long options lose value each day"
        assert ce["vega"] > 0 and pe["vega"] > 0
        assert ce["delta"] - pe["delta"] == pytest.approx(1, abs=0.01), "put-call parity in delta (q = 0)"
        if r["strike"] <= 24000:
            assert ce["iv"] == 14.0 and ce["iv_source"] == "stored"
        else:                                      # priced at 14%: the implied IV comes back near 14%
            assert ce["iv"] == pytest.approx(14.0, abs=0.3) and ce["iv_source"] == "implied"
    atm = next(r for r in c["rows"] if r["atm"])
    assert 0.4 < atm["ce"]["delta"] < 0.65 and -0.6 < atm["pe"]["delta"] < -0.35


def test_pcr_and_max_pain(api):
    client, _, _ = api
    c = _get(client, f"/api/v2/options/chain/NIFTY?expiry={EXP2}")[1]
    assert c["expiry"] == EXP2
    ce_tot, pe_tot = sum(v[0] for v in OI2.values()), sum(v[1] for v in OI2.values())
    assert c["totals"]["ce_oi"] == ce_tot == 2600 and c["totals"]["pe_oi"] == pe_tot == 2500
    assert c["pcr"] == round(2500 / 2600, 2) == 0.96
    # holders' expiry value at 23800 / 23900 / 24000 / 24100 / 24200 = 290k / 140k / 80k / 130k / 280k
    assert c["max_pain"] == 24000.0
    assert c["max_oi"] == {"ce": 1000, "pe": 900}

    from dashboard.options_v2 import _max_pain
    assert _max_pain({}) is None and _max_pain({100.0: (0.0, 0.0)}) is None


def test_missing_values_are_null_not_zero(api):
    client, _, _ = api
    c = _get(client, "/api/v2/options/chain/NIFTY")[1]
    bare = next(r for r in c["rows"] if r["strike"] == 24500.0)["ce"]
    for k in ("ltp", "change", "change_pct", "oi", "oi_change", "volume", "iv", "iv_source", "delta", "gamma", "theta",
              "vega", "bid", "ask"):
        assert bare[k] is None, k
    c2 = _get(client, f"/api/v2/options/chain/NIFTY?expiry={EXP2}")[1]
    k24300 = next(r for r in c2["rows"] if r["strike"] == 24300.0)
    assert k24300["ce"]["oi"] is None and k24300["ce"]["oi_change"] is None and k24300["ce"]["volume"] is None
    assert k24300["ce"]["ltp"] == 50.0
    assert k24300["pe"] is None, "a contract ATIP has no row for is null, not a row of zeros"


def test_bad_symbol_or_expiry_is_400(api):
    client, _, _ = api
    for url in ("/api/v2/options/chain/BAD%20SYM", "/api/v2/options/chain/" + "X" * 40,
                "/api/v2/options/chain/NIFTY?expiry=03-11-2026", "/api/v2/options/chain/NIFTY?expiry=2026-13-01",
                "/api/v2/options/chain/NIFTY?expiry=2026-11-10", "/api/v2/options/chain/NIFTY?strikes=ten"):
        code, body = _get(client, url)
        assert code == 400 and body.get("error"), url
    assert "stored" in _get(client, "/api/v2/options/chain/NIFTY?expiry=2026-11-10")[1]["error"]


def test_no_chain_stored_says_so(api):
    client, _, _ = api
    code, c = _get(client, "/api/v2/options/chain/RELIANCE")
    assert code == 200 and c["status"] == "NO_DATA" and c["rows"] == [] and c["expiries"] == []
    assert c["message"] == "No option chain stored for RELIANCE: the F&O bhavcopy job fills it after 18:30 IST"
    assert _get(client, "/api/v2/options/chain/nifty50")[1]["symbol"] == "NIFTY", "ATIP's index name maps to F&O"


def test_stale_when_the_data_is_behind_the_last_session(api):
    client, clock, _ = api
    clock["now"] = dt.datetime(2026, 11, 2, 12, 0)        # Monday noon: Friday's bhavcopy is still the latest due
    assert _get(client, "/api/v2/options/chain/NIFTY")[1]["stale"] is False
    clock["now"] = dt.datetime(2026, 11, 3, 19, 0)        # Tuesday evening: Monday's and Tuesday's are due
    c = _get(client, "/api/v2/options/chain/NIFTY")[1]
    assert c["stale"] is True and c["last_trading_day"] == "2026-11-03" and "2026-10-30" in c["stale_reason"]
    assert c["expiry"] == EXP2, "3 Nov's expiry has closed by 19:00 on the 3rd"


def test_a_newer_live_snapshot_wins_over_the_bhavcopy(api):
    client, clock, get_connection = api
    conn = get_connection()
    try:
        T = (dt.datetime(2026, 11, 3, 15, 30) - dt.datetime(2026, 11, 2, 11, 0)).total_seconds() / (365 * 86400)
        for K in (23900.0, 24000.0, 24100.0):
            for kind in ("CE", "PE"):
                px = round(bs_price(24050, K, T, RISK_FREE, 0.16, "call" if kind == "CE" else "put"), 2)
                conn.execute("INSERT INTO option_chain_snapshot (ts, symbol, expiry, strike, option_type, ltp, `change`, "
                             "iv, oi, oi_chg, volume, bid, ask, underlying) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             ("2026-11-02 11:00:00", "NIFTY", EXP1, K, kind, px, -3.5, None, 5000, 120, 900, None, None,
                              24050))
        conn.commit()
    finally:
        conn.close()
    clock["now"] = dt.datetime(2026, 11, 2, 11, 5)
    c = _get(client, "/api/v2/options/chain/NIFTY")[1]
    assert c["source"] == "live" and c["as_of"] == "2026-11-02 11:00" and c["stale"] is False
    assert c["spot"] == 24050 and c["lot_size"] == 75, "lot size from the bhavcopy"
    assert [r["strike"] for r in c["rows"]] == [23900.0, 24000.0, 24100.0]
    assert c["atm_strike"] == 24000.0, "a tie goes to the lower strike"
    ce = c["rows"][0]["ce"]
    assert ce["iv_source"] == "implied" and ce["iv"] == pytest.approx(16.0, abs=0.3)
    assert ce["change"] == -3.5 and ce["oi_change"] == 120
    clock["now"] = dt.datetime(2026, 11, 3, 10, 0)        # next morning, the feed has not ticked today
    assert _get(client, "/api/v2/options/chain/NIFTY")[1]["stale"] is True


def test_the_chain_prices_a_leg_for_the_existing_analyser(api):
    """The leg options.js sends to POST /api/options/analyse: premium = the LTP, iv = IV % / 100."""
    from quant import options_strategy as O
    client, _, _ = api
    c = _get(client, "/api/v2/options/chain/NIFTY")[1]
    atm = next(r for r in c["rows"] if r["atm"])
    leg = {"kind": "CE", "side": "BUY", "strike": atm["strike"], "expiry": c["expiry"], "lots": 1,
           "premium": atm["ce"]["ltp"], "iv": atm["ce"]["iv"] / 100}
    a = O.analyse([leg], c["spot"], lot_size=c["lot_size"], as_of=c["as_of_date"])
    assert a["unlimited_profit"] and a["max_loss"] == pytest.approx(-atm["ce"]["ltp"] * 75)
    assert a["breakevens"] == [pytest.approx(atm["strike"] + atm["ce"]["ltp"], abs=0.5)]


def test_register_takes_a_sync_call_or_builds_its_own_run(temp_db, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from dashboard import options_v2
    from db.schema import get_connection, init_db
    init_db()
    monkeypatch.setattr(options_v2, "now_ist", lambda: AFTER_BHAV)
    conn = get_connection()
    _bhav(conn)
    conn.close()

    def call(fn):                                   # as dashboard/ux_routes.py's call (synchronous)
        conn = get_connection()
        try:
            return JSONResponse(fn(conn))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    for run in (call, None):
        app = FastAPI()
        options_v2.register(app, [], run)
        client = TestClient(app)
        assert client.get("/api/v2/options/chain/NIFTY?strikes=2").json()["atm_strike"] == 24000.0
        assert client.get("/api/v2/options/chain/BAD%20SYM").status_code == 400


# ── the browser panel ─────────────────────────────────────────────────────────────────────────

def test_options_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = JS.read_text(encoding="utf-8")
    r = subprocess.run([node, "-e", "new (require('vm').Script)(require('fs').readFileSync(process.argv[1], 'utf8'), "
                                    "{filename: 'options.js'})", str(JS)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "/api/v2/options/chain/" in src and "/api/options/analyse" in src
    assert "AX.api" in src or "ax.api(" in src.lower()


def test_options_js_formats_through_ax_fmt_and_never_prints_raw_numbers():
    src = JS.read_text(encoding="utf-8")
    assert "F.number(" in src and "F.inr(" in src and "F.pct(" in src
    assert ".toFixed(" not in src.split("function shim")[0], "numbers go through AX.fmt"
    assert "Unlimited" in src, "an unbounded max profit / loss reads Unlimited"
    assert "onclick=" not in src, "buttons with listeners, not inline handlers"
    assert "placeOrder" not in src and "/api/orders" not in src, "nothing here places an order"


def test_options_css_uses_only_design_tokens():
    css = CSS.read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "no hard-coded colours"
    assert not re.search(r"rgba?\(", css), "no hard-coded colours"
    selectors = re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    for sel in selectors:
        sel = sel.strip()
        if sel.startswith("@"):
            continue
        for part in sel.split(","):
            assert ".ax-oc" in part or part.strip().startswith("#ax-oc"), f"unprefixed selector: {part.strip()}"
    assert "tabular-nums" in css and "position: sticky" in css and "focus-visible" in css
