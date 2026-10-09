"""
W40: the stock panel's RSI 14 / MACD 12-26-9 sub-panels and its intraday chart (dashboard/stock_view.py).

  * the panel's indicator math (the block between the "indicator math" markers in ASSETS) is run in node and
    compared with research/technicals.py on the same closes -- daily, weekly and monthly bars -- and with the
    values data/technical.py stores (the `ta` library) once the series is past its warm-up;
  * GET /api/stock/{symbol}/intraday on seeded bars: regular session only, one interval only, the session
    VWAP worked by hand, the previous daily close, the intraday scan marks, 2-5 sessions with the overnight
    gaps collapsed, a missing day / stock / table, bad arguments, and the authz rule that covers it;
  * the panel in headless Chromium: RSI / MACD toggles, the 5D / 1D intraday views and the no-bars message,
    with no JavaScript error (skipped when Playwright or Chromium is missing, like the other browser tests).
"""
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from research import technicals as T

NODE = "/opt/node22/bin/node"
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def _node():
    node = NODE if os.path.exists(NODE) else shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def _assets():
    from dashboard import stock_view as SV
    return SV.ASSETS


def _math_js():
    s = _assets()
    a, b = s.index("/* ---- indicator math"), s.index("/* ---- end of indicator math ---- */")
    return s[a:b]


def _run_js(tmp_path, body, data):
    """Run the panel's indicator math plus `body` in node with D = data; body prints JSON."""
    (tmp_path / "in.json").write_text(json.dumps(data), encoding="utf-8")
    src = _math_js() + "\nconst D = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n" + body
    (tmp_path / "math.js").write_text(src, encoding="utf-8")
    r = subprocess.run([_node(), str(tmp_path / "math.js"), str(tmp_path / "in.json")], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


IND_JS = ("const ind = c => ({rsi: rsi(c, 14), macd: macd(c, 12, 26, 9)});\n"
          "const out = {D: ind(D.closes)};\n"
          "for (const iv of ['W', 'M']) { const A = agg(D.prices, iv), c = A.map(p => +p.close);"
          " out[iv] = Object.assign({close: c, date: A.map(p => p.date)}, ind(c)); }\n"
          "process.stdout.write(JSON.stringify(out));")


def _closes(n=420, seed=7):
    """20 rising closes (no loss yet: Wilder's average loss is 0, RSI 100), then a random walk."""
    rng = np.random.default_rng(seed)
    up = 100 + np.arange(20) * 0.5
    return np.r_[up, up[-1] * np.exp(np.cumsum(rng.normal(0, 0.015, n - 20)))]


def _frame(closes, index):
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c * 1.006, "low": c * 0.994, "close": c,
                         "volume": np.full(len(c), 1e5)}, index=index)


def _same(js, py, what, warm=None):
    """js (None before the warm-up) against the pandas series py, value for value."""
    py = np.asarray(py, dtype=float)
    assert len(js) == len(py), what
    if warm is None:
        assert [v is None for v in js] == list(np.isnan(py)), f"{what}: the same bars are undefined"
    else:
        assert all(v is None for v in js[:warm]) and all(v is not None for v in js[warm:]), what
    got = [(a, b) for a, b in zip(js, py) if a is not None]
    assert got, what
    for k, (a, b) in enumerate(got):
        assert a == pytest.approx(b, rel=1e-11, abs=1e-9), f"{what} at defined bar {k}: {a} vs {b}"


def _check(js, d, what):
    _same(js["rsi"], T.rsi(d["close"]), f"{what} RSI")
    m = T.indicators(d)
    _same(js["macd"]["line"], m["macd"], f"{what} MACD", warm=25)
    _same(js["macd"]["signal"], m["macd_signal"], f"{what} signal", warm=33)
    _same(js["macd"]["hist"], m["macd_hist"], f"{what} histogram", warm=33)


# ── RSI / MACD parity, client vs server ─────────────────────────────────────

def test_rsi_and_macd_match_research_technicals_on_daily_weekly_and_monthly_bars(tmp_path):
    days = pd.bdate_range("2023-01-02", periods=1100)               # a Monday; 1100 is 220 full weeks
    days = days.delete([37, 38, 400, 731])                         # a few holidays, none on the last Friday
    c = _closes(len(days))
    df = _frame(c, days)
    prices = [{"date": str(t.date()), "open": float(r.open), "high": float(r.high), "low": float(r.low),
               "close": float(r.close), "volume": 1e5} for t, r in df.iterrows()]
    js = _run_js(tmp_path, IND_JS, {"closes": [float(x) for x in c], "prices": prices})

    _check(js["D"], df, "daily")
    assert js["D"]["rsi"][14:20] == [100] * 6, "only gains so far: RSI 100, as research/technicals.rsi()"

    wk = T.weekly_bars(df)                                         # completed W-FRI bars: the weekly rating's
    assert js["W"]["close"] == pytest.approx(list(wk["close"])), "the panel's weekly bars are the server's"
    _check(js["W"], wk, "weekly")
    mo = df.resample("ME").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    assert js["M"]["close"] == pytest.approx(list(mo["close"]))
    _check(js["M"], mo, "monthly")


def test_last_values_match_what_data_technical_stores(tmp_path):
    """data/technical.py (the technical_indicators table, the `ta` library) seeds Wilder's averages and the MACD
    signal a few bars differently; past the warm-up the values agree to its 4-decimal rounding."""
    pytest.importorskip("ta")
    from data import technical as DT
    c = _closes(300, seed=11)
    df = pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=len(c)).strftime("%Y-%m-%d"), "open": c,
                       "high": c * 1.01, "low": c * 0.99, "close": c, "adj_close": [None] * len(c),
                       "volume": [1000] * len(c)})
    stored = DT.compute_indicators("ACME", df)
    js = _run_js(tmp_path, "const m = macd(D.c, 12, 26, 9);\nprocess.stdout.write(JSON.stringify({rsi: rsi(D.c, 14)"
                           ".pop(), line: m.line.pop(), signal: m.signal.pop(), hist: m.hist.pop()}));",
                 {"c": [float(x) for x in c]})
    assert js["rsi"] == pytest.approx(stored["rsi_14"], abs=1e-4)
    assert js["line"] == pytest.approx(stored["macd_line"], abs=1e-4)
    assert js["signal"] == pytest.approx(stored["macd_signal"], abs=1e-4)
    assert js["hist"] == pytest.approx(stored["macd_hist"], abs=1e-4)


def test_stock_panel_js_passes_node_check(tmp_path):
    p = tmp_path / "stock_view.js"
    p.write_text("\n".join(re.findall(r"<script>(.*?)</script>", _assets(), re.S)), encoding="utf-8")
    r = subprocess.run([_node(), "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "/* ---- end of indicator math ---- */" in _assets()
    assert "document" not in _math_js() and "window" not in _math_js(), "the math block runs without a DOM"


# ── the intraday endpoint ────────────────────────────────────────────────────

SESS = ["2026-10-01", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]   # 10-02 holiday
TODAY_BARS = 6                                                     # 10-09 is in progress: 09:15 .. 10:30
DAILY = {"2026-09-30": 99.0, "2026-10-01": 101.5, "2026-10-05": 103.5, "2026-10-06": 105.5, "2026-10-07": 107.5,
         "2026-10-08": 109.5}


def _bar(k, j):
    """session k, slot j: (open, high, low, close, volume)"""
    o = 100 + 2 * k + 0.1 * j
    c = o + (0.3 if j % 3 else -0.2)
    return o, max(o, c) + 0.4, min(o, c) - 0.3, c, 1000 + 100 * j + 7 * k


def _ts(d, hh, mm):
    return datetime.combine(date.fromisoformat(d), datetime.min.time()).replace(hour=hh, minute=mm)


def _seed(conn):
    ins = ("INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume, source) "
           "VALUES (?,?,?,?,?,?,?,?,?)")
    for k, d in enumerate(SESS):
        for j in range(TODAY_BARS if d == SESS[-1] else 25):
            t = _ts(d, 9, 15) + timedelta(minutes=15 * j)
            conn.execute(ins, ("ACME", t.strftime("%Y-%m-%d %H:%M:%S"), 15) + _bar(k, j) + ("dhan",))
    for hh, mm in ((9, 0), (15, 30)):                              # pre-open and after the close: not drawn
        conn.execute(ins, ("ACME", f"2026-10-09 {hh:02d}:{mm:02d}:00", 15, 50, 51, 49, 50, 99999, "dhan"))
    for m in range(-1, 10):                                        # tick capture's 1-minute bars, 09:14 .. 09:24
        t = _ts("2026-10-09", 9, 15) + timedelta(minutes=m)
        conn.execute(ins, ("ACME", t.strftime("%Y-%m-%d %H:%M:%S"), 1, 500 + m, 501 + m, 499 + m, 500 + m, 10, "ticks"))
    conn.execute(ins, ("OTHER", "2026-10-09 09:15:00", 15, 7, 8, 6, 7, 1, "dhan"))
    start = date(2025, 6, 2)                                       # ~330 sessions of daily history before
    for i in range(400):
        d = start + timedelta(days=i)
        if d.weekday() < 5 and str(d) < "2026-09-30":
            c = 80 + 10 * np.sin(i / 23) + i * 0.02
            conn.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, source) VALUES "
                         "('ACME', ?, ?, ?, ?, ?, 100000, 'test')", (str(d), c - 0.3, c + 0.8, c - 0.9, c))
    for d, c in DAILY.items():
        conn.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, source) VALUES "
                     "('ACME', ?, ?, ?, ?, ?, 100000, 'test')", (d, c - 1, c + 1, c - 2, c))
    for sid, sym, d, scan, side, bar_ts in (
            ("ACME:2026-10-09:orb_up", "ACME", "2026-10-09", "orb_up", "BULL", "2026-10-09 09:45:00"),
            ("ACME:2026-10-08:open_high", "ACME", "2026-10-08", "open_high", "BEAR", "2026-10-08 10:00:00"),
            ("ACME:2026-10-01:squeeze_up", "ACME", "2026-10-01", "squeeze_up", "BULL", "2026-10-01 11:00:00"),
            ("OTHER:2026-10-09:orb_up", "OTHER", "2026-10-09", "orb_up", "BULL", "2026-10-09 09:45:00")):
        conn.execute("INSERT INTO intraday_signal (signal_id, symbol, date, scan, direction, bar_ts, price, level, "
                     "reason, seen_at, seen_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, sym, d, scan, side, bar_ts, 110.5, 110.2, f"{scan} reason", f"{d} 10:00:00", 110.6))
    conn.execute("INSERT INTO intraday_scan_hit (run_id, run_at, session, scan, symbol, price, score, details_json) "
                 "VALUES ('r1', '2026-10-09 10:30:05', '2026-10-09', 'breakout', 'ACME', 110.9, 72.5, '{}')")
    conn.commit()


@pytest.fixture
def api(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import get_connection, init_db
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    init_db()
    conn = get_connection()
    _seed(conn)
    conn.close()
    return TestClient(server.app)


def _get(client, **params):
    r = client.get("/api/stock/ACME/intraday", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _vwaps(k, n):
    pv = v = 0.0
    out = []
    for j in range(n):
        o, h, lo, c, vol = _bar(k, j)
        pv += (h + lo + c) / 3 * vol
        v += vol
        out.append(pv / v)
    return out


def test_intraday_defaults_to_the_latest_session_and_keeps_the_regular_session_only(api):
    j = _get(api)
    assert j["date"] == "2026-10-09" and j["interval"] == 15 and j["days"] == 1 and j["message"] is None
    assert [b["time"] for b in j["bars"]] == ["09:15", "09:30", "09:45", "10:00", "10:15", "10:30"], \
        "no 09:00 pre-open or 15:30 bar, no 1-minute bar, no other stock"
    assert [b["close"] for b in j["bars"]] == pytest.approx([_bar(5, i)[3] for i in range(TODAY_BARS)])
    assert all(b["date"] == "2026-10-09" and b["volume"] < 99999 for b in j["bars"])
    assert [b["vwap"] for b in j["bars"]] == pytest.approx(_vwaps(5, TODAY_BARS), rel=1e-12)
    assert j["prev_close"] == 109.5 and j["prev_close_date"] == "2026-10-08"
    (s,) = j["sessions"]
    assert (s["first"], s["last"], s["prev_close"]) == (0, 5, 109.5)
    assert (s["open"], s["close"]) == (_bar(5, 0)[0], _bar(5, 5)[3])
    assert s["vwap"] == pytest.approx(_vwaps(5, TODAY_BARS)[-1])


def test_intraday_marks_land_on_their_bars(api):
    m = {x["scan"]: x for x in _get(api)["marks"]}
    assert set(m) == {"orb_up", "breakout"}, "OTHER's hit and the earlier days' are not ACME's 10-09"
    orb = m["orb_up"]
    assert (orb["source"], orb["bar"], orb["time"], orb["direction"]) == ("signal", 2, "10:00", "BULL"), \
        "the 09:45 trigger bar, closed at 10:00"
    assert orb["name"] == "Opening-range breakout" and orb["reason"] == "orb_up reason" and orb["price"] == 110.5
    bo = m["breakout"]
    assert (bo["source"], bo["bar"], bo["time"], bo["direction"]) == ("scan", 4, "10:30", "BULL"), \
        "the scan ran at 10:30:05: the 10:15 bar was the last complete one"
    assert bo["name"] == "20-session high breakout" and bo["reason"] == "score 72.5"


def test_intraday_five_sessions_collapse_the_gaps_and_restart_the_vwap(api):
    j = _get(api, days=5)
    ss = j["sessions"]
    assert [s["date"] for s in ss] == SESS[1:], "the last five sessions with bars; 10-01 drops out"
    assert [(s["first"], s["last"]) for s in ss] == [(0, 24), (25, 49), (50, 74), (75, 99), (100, 105)]
    assert len(j["bars"]) == 4 * 25 + TODAY_BARS
    for k, s in enumerate(ss, start=1):
        seg = j["bars"][s["first"]:s["last"] + 1]
        assert {b["date"] for b in seg} == {s["date"]} and seg[0]["time"] == "09:15"
        assert [b["vwap"] for b in seg] == pytest.approx(_vwaps(k, len(seg)), rel=1e-12), "VWAP restarts"
    assert [s["prev_close"] for s in ss] == [101.5, 103.5, 105.5, 107.5, 109.5], \
        "10-05's previous close is 10-01's: the holiday and the weekend have no row"
    assert ss[0]["prev_close_date"] == "2026-10-01"
    m = {x["scan"]: x for x in j["marks"]}
    assert set(m) == {"open_high", "orb_up", "breakout"}
    assert (m["open_high"]["bar"], m["open_high"]["direction"]) == (78, "BEAR"), "10-08 10:00 = 75 + 3"
    assert (m["orb_up"]["bar"], m["breakout"]["bar"]) == (102, 104)
    assert [x["scan"] for x in j["marks"]] == ["open_high", "orb_up", "breakout"], "in time order"


def test_intraday_for_a_given_date_and_number_of_days(api):
    j = _get(api, date="2026-10-07", days=2)
    assert [s["date"] for s in j["sessions"]] == ["2026-10-06", "2026-10-07"] and j["date"] == "2026-10-07"
    assert j["prev_close"] == 105.5 and j["requested_date"] == "2026-10-07" and j["marks"] == []
    j = _get(api, date="2026-10-02")                                   # Gandhi Jayanti: no session
    assert j["bars"] == [] and j["sessions"] == [] and "2026-10-01" in j["message"] and "2026-10-02" in j["message"]


def test_intraday_reads_one_interval_only(api):
    j = _get(api, interval=1)
    assert [b["time"] for b in j["bars"]] == [f"09:{m:02d}" for m in range(15, 25)], "09:14 is before the open"
    assert [b["close"] for b in j["bars"]] == list(range(500, 510))
    m = {x["scan"]: x for x in j["marks"]}
    assert m["orb_up"]["bar"] is None and m["breakout"]["bar"] is None, \
        "the 09:59 / 10:29 one-minute bars are not stored: listed, not drawn"


def test_intraday_explains_why_there_is_nothing_to_draw(api):
    from db.schema import get_connection
    j = api.get("/api/stock/NOPE/intraday").json()
    assert j["bars"] == [] and "NOPE" in j["message"] and "tracked" in j["message"]
    j = _get(api, interval=5)
    assert j["bars"] == [] and "1-minute, 15-minute" in j["message"]
    conn = get_connection()
    conn.execute("DELETE FROM intraday_bars")
    conn.commit()
    conn.close()
    j = _get(api)
    assert j["bars"] == [] and j["marks"] == [] and "Dhan Data API" in j["message"]


@pytest.mark.parametrize("params", [{"days": 0}, {"days": 6}, {"interval": 0}, {"interval": 120},
                                    {"date": "2026-13-01"}, {"date": "yesterday"}])
def test_intraday_rejects_bad_arguments(api, params):
    assert api.get("/api/stock/ACME/intraday", params=params).status_code == 400
    assert api.get("/api/stock/bad sym!/intraday").status_code == 400


def test_intraday_route_is_a_dashboard_read():
    from enterprise.authz import permission_for
    assert permission_for("GET", "/api/stock/ACME/intraday") == "dashboard:read"
    assert permission_for("GET", "/api/stock/ACME/history") == "dashboard:read"


# ── the panel in Chromium ────────────────────────────────────────────────────

def test_panel_draws_rsi_macd_and_the_intraday_chart_without_js_errors(api):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not os.path.exists(CHROME):
        pytest.skip("Chromium is not installed")
    from dashboard import stock_view as SV
    from db.schema import get_connection
    c = get_connection()      # EMPTY has a daily chart in this page (canned below) but no 15-minute bars:
    c.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "   # give it a price so
              "('EMPTY','2026-10-01',10,10,10,10,100)")                                    # /rating-75 says why
    c.commit()
    c.close()
    canned = {}
    for path in ("/api/stock/ACME/history?sessions=2500", "/api/stock/ACME/intraday?days=5&interval=15",
                 "/api/stock/EMPTY/intraday?days=5&interval=15",
                 # the panel also asks for the on-demand 75-minute rating (W39B-TF75) when it opens
                 "/api/stock/ACME/rating-75", "/api/stock/EMPTY/rating-75"):
        r = api.get(path)
        canned[path] = (r.status_code, r.text)
    hist = json.loads(canned["/api/stock/ACME/history?sessions=2500"][1])
    canned["/api/stock/EMPTY/history?sessions=2500"] = (200, json.dumps(dict(hist, symbol="EMPTY")))

    def serve(route):
        u = route.request.url
        status, body = canned.get(u[u.index("/api/"):], (404, '{"error": "not canned"}'))
        route.fulfill(status=status, content_type="application/json", body=body)

    page_html = ("<!doctype html><html><head><meta charset='utf-8'></head><body><table>"
                 "<tr data-sym='ACME' id='row'><td>ACME</td></tr><tr data-sym='EMPTY' id='row2'><td>EMPTY</td></tr>"
                 "</table>" + SV.ASSETS + "</body></html>")
    errors = []
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=CHROME)
        except Exception as e:                                   # a sandbox without the browser's libraries
            pytest.skip(f"Chromium does not start: {e}")
        try:
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.route("http://atip.test/", lambda r: r.fulfill(status=200, content_type="text/html", body=page_html))
            page.route("**/api/stock/**", serve)
            page.goto("http://atip.test/")
            page.click("#row")
            page.wait_for_selector("#svsvg")
            loc = page.locator
            assert loc("#svrsi").count() == 0 and loc("#svmacd").count() == 0, "off by default: the old look"
            h0 = float(page.get_attribute("#svsvg", "viewBox").split()[3])

            page.click("#svrsibtn")
            assert loc("#svrsi .svi-rsi").count() == 1 and loc("#svrsi .svi-guide").count() == 2, "30 / 70"
            page.click("#svmacdbtn")
            assert loc("#svmacd .svi-macd").count() == 1 and loc("#svmacd .svi-signal").count() == 1
            assert loc("#svmacd .svi-hist").count() > 100
            assert float(page.get_attribute("#svsvg", "viewBox").split()[3]) > h0 + 150, "two panels added"
            assert float(page.get_attribute("#svhit", "height")) == float(page.get_attribute("#svx", "y2")), \
                "the crosshair spans the panels"
            assert "ATIP score" in page.inner_html("#svsvg")
            assert json.loads(page.evaluate("sessionStorage.getItem('atip.dash')"))["svrsi"] is True

            def hover(frac):
                return page.evaluate("""(f) => {const svg = document.getElementById('svsvg'), r = svg.getBoundingClientRect();
                    document.getElementById('svhit').dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + r.width * f,
                        clientY: r.bottom - 30, bubbles: true}));
                    return document.getElementById('svtip').innerText}""", frac)
            tip = hover(0.8)
            assert "RSI 14" in tip and "MACD" in tip and "signal" in tip, tip
            page.click("#sv .bar button:text-is('Weekly')")
            assert loc("#svrsi .svi-rsi").count() == 1 and loc("#svmacd .svi-macd").count() == 1

            page.click("#sv .bar button:text-is('5D 15m')")
            page.wait_for_selector("#svvwap")
            assert loc("#sv .bar button.on:text-is('5D 15m')").count() == 1
            assert loc("#sv .bar button.on:text-is('Weekly')").count() == 0
            assert loc("#svsvg .svi-sep").count() == 4, "five sessions side by side"
            assert loc("#svsvg .svi-pc").count() == 5 and "prev close 109.5" in page.inner_html("#svsvg")
            assert loc("#svsvg .svi-day").count() == 5
            assert loc("#svimarks .svm-ihit").count() == 2 and loc("#svimarks .svm-iscan").count() == 1
            assert page.get_attribute("#svimarks .svm-iscan", "fill") == "#22c55e"
            assert loc("#svrsi .svi-rsi").count() == 1 and loc("#svmacd .svi-macd").count() == 1, \
                "the panels follow the intraday bars"
            assert "ATIP score" not in page.inner_html("#svsvg")
            assert "Opening-range breakout" in page.inner_text("#svpl") and "Open = high" in page.inner_text("#svpl")
            tip = hover(0.99)
            assert "VWAP" in tip and "prev close 109.5" in tip and "10:30" in tip, tip

            page.click("#sv .bar button:text-is('1D 15m')")
            assert loc("#svsvg .svi-sep").count() == 0 and loc("#svsvg .svi-pc").count() == 1
            assert loc("#svsvg .svi-hr").count() == 6, "10:00 .. 15:00 on a full-session axis"
            assert loc("#svimarks .svm-ihit").count() == 1 and loc("#svimarks .svm-iscan").count() == 1
            page.click("#svpatbtn")
            assert loc("#svimarks").count() == 0 and "hidden" in page.inner_text("#svpl")
            page.click("#svpatbtn")
            page.click("#svrsibtn")
            assert loc("#svrsi").count() == 0 and loc("#svmacd .svi-macd").count() == 1
            svg = page.inner_html("#svc")
            assert "undefined" not in svg and "NaN" not in svg

            page.evaluate("svClose()")
            page.click("#row2")                                       # a stock without intraday bars
            page.wait_for_selector("#svnoid")
            assert "EMPTY" in page.inner_text("#svnoid") and loc("#svsvg").count() == 0
            page.click("#sv .bar button:text-is('Daily')")
            page.wait_for_selector("#svsvg")
            assert loc("#svmacd .svi-macd").count() == 1 and loc("#svrsi").count() == 0
            assert "ATIP score" in page.inner_html("#svsvg")

            page.reload()                                            # the page's auto-refresh: same session
            page.click("#row")
            page.wait_for_selector("#svsvg")
            assert loc("#svmacd .svi-macd").count() == 1 and loc("#svrsi").count() == 0, "remembered for the session"
            assert loc("#sv .bar button.on:text-is('1Y')").count() == 1, "back on the daily chart"
        finally:
            browser.close()
    assert not errors, errors
