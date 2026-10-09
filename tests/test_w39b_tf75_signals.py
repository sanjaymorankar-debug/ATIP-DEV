"""
W39B-TF75: the 75-minute rating on /signals and on demand. Today's signals carry the snapshot's 75-minute
label and its agreement with the daily rating (the page's "75m" column); GET /api/stock/{symbol}/rating-75
computes the rating from the stored 15-minute bars as of any moment -- completed 75-minute bars only, through
research/technicals.py rating_75 itself -- with the daily rating known at that moment (no look-ahead), and it
reproduces what the 20:30 run stored. The stock panel's Technicals tab shows it.
"""
import datetime as dt
import json
import os
import re
import subprocess

import numpy as np
import pandas as pd
import pytest

from research import technicals as T

NODE = "/opt/node22/bin/node"
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
DAY = dt.date(2026, 10, 6)                          # a Tuesday


def _sessions(end, n):
    from utils.trading_calendar import is_trading_day
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


def _trend(days, start=100.0, step=0.05, per_day=25):
    """15-minute bars from 09:15, `per_day` a session, drifting by `step` a bar with a small zigzag."""
    idx, rows, c = [], [], start
    for d in days:
        t0 = dt.datetime.combine(d, dt.time(9, 15))
        for i in range(per_day):
            o = c
            c = c + step + (0.04 if i % 2 else -0.04)
            idx.append(t0 + dt.timedelta(minutes=15 * i))
            rows.append((o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1e4))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])


def _daily(closes, end):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c,
                         "volume": np.full(len(c), 1e5)}, index=pd.bdate_range(end=end, periods=len(c)))


def _at(day, hhmm):
    return dt.datetime.combine(day, dt.time(*map(int, hhmm.split(":"))))


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    yield conn
    conn.close()


def _store15(conn, sym, df, interval=15):
    conn.executemany("INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume, source) "
                     "VALUES (?,?,?,?,?,?,?,?,'dhan')",
                     [(sym, t.strftime("%Y-%m-%d %H:%M:%S"), interval, r.open, r.high, r.low, r.close, int(r.volume))
                      for t, r in df.iterrows()])
    conn.commit()


def _store_daily(conn, sym, df):
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "(?,?,?,?,?,?,?,'dhan')", [(sym, str(t.date()), r.open, r.high, r.low, r.close, r.volume)
                                                for t, r in df.iterrows()])
    conn.commit()


def _snap(conn, sym, day, label, label75=None, m75=None):
    conn.execute("INSERT INTO technical_snapshot (symbol, date, close, tech_rating, tech_rating_label, "
                 "tech_rating_75_label, mtf_alignment_75, bar_75_end) VALUES (?,?,100,?,?,?,?,?)",
                 (sym, str(day), 0.4 if "BUY" in label else -0.4, label, label75, m75,
                  f"{day} 15:30" if label75 else None))
    conn.commit()


def _seed_midsession(conn):
    """Eight full sessions of 15-minute bars before DAY, and DAY's bars from 09:15 to 11:45 (the 11:45 bar,
    which ends at 12:00, is stored early: a feed can hand over a bar still in progress)."""
    days = _sessions(DAY, 9)
    full = _trend(days[:-1], start=100.0, step=0.04)
    today = _trend([DAY], start=float(full["close"].iloc[-1]), step=-0.2, per_day=11)
    b15 = pd.concat([full, today])
    _store15(conn, "UPCO", b15)
    junk = b15.iloc[-30:].copy()
    junk[["open", "high", "low", "close"]] = 5.0
    _store15(conn, "UPCO", junk.set_axis(junk.index + pd.Timedelta(minutes=1)), interval=1)   # 1-minute bars
    _store_daily(conn, "UPCO", _daily(np.linspace(80, 100, 80), days[-2]))
    _snap(conn, "UPCO", days[-2], "BUY")              # the 20:30 run of the session before DAY
    _snap(conn, "UPCO", DAY, "SELL")                  # DAY's own 20:30 run: not known during DAY's session
    return b15, days


# ── on demand: completed bars only, as rating_75 defines them ──────────────

def test_on_demand_rating_uses_only_the_75_minute_bars_complete_at_that_moment(db):
    from research import tech_signals as S
    b15, days = _seed_midsession(db)
    out = S.rating_75_now(db, "upco", "2026-10-06 11:50")
    done = b15[b15.index + pd.Timedelta(minutes=15) <= _at(DAY, "11:50")]       # the 11:45 bar is not done
    want = T.rating_75(done, now=_at(DAY, "11:50"))
    assert want["tech_rating_75"] is not None
    for k in ("tech_rating_75", "tech_rating_75_label", "rsi_14_75", "supertrend_dir_75", "bar_75_end", "bars_75"):
        assert out[k] == want[k], k
    # 8 sessions x 5 bars + DAY's 09:15-10:30 and 10:30-11:45 (its closing 11:30 bar ended at 11:45)
    assert out["bars_75"] == 42 and out["bars_75_on_day"] == 2 and out["bar_75_end"] == "2026-10-06 11:45"
    assert out["next_bar_75_end"] == "2026-10-06 13:00" and out["latest_15m_end"] == "2026-10-06 11:45"
    assert out["symbol"] == "UPCO" and out["as_of"] == "2026-10-06 11:50" and out["reason"] is None
    # its votes are the rating: the mean of the votes, rounded as rating() rounds it
    assert out["votes_75"] and round(sum(out["votes_75"].values()) / len(out["votes_75"]), 3) == out["tech_rating_75"]
    # a minute earlier the 10:30-11:45 bar is still open: one bar of DAY, the 10:30 end
    early = S.rating_75_now(db, "UPCO", "2026-10-06T11:44")
    assert early["bars_75_on_day"] == 1 and early["bar_75_end"] == "2026-10-06 10:30" and early["bars_75"] == 41
    assert early["tech_rating_75"] == T.rating_75(b15, now=_at(DAY, "11:44"))["tech_rating_75"]
    # before the session: yesterday's last bar, and no next bar
    pre = S.rating_75_now(db, "UPCO", "2026-10-06 09:00")
    assert pre["bars_75_on_day"] == 0 and pre["bar_75_end"] == f"{days[-2]} 15:30" and pre["next_bar_75_end"] is None
    assert out["warning"] is None and pre["warning"] is None
    # the next morning with no new bars stored: DAY's session is over, so its 11:45 bar alone closes the
    # 11:45-13:00 bar (a session cut short counts once over), and the rating says the feed is behind
    late = S.rating_75_now(db, "UPCO", "2026-10-07 11:00")
    assert late["bars_75_on_day"] == 0 and late["bar_75_end"] == "2026-10-06 13:00" and late["bars_75"] == 43
    assert "feed may be behind" in late["warning"] and "2026-10-06 13:00" in late["warning"]


def test_the_daily_rating_is_the_one_known_at_that_moment(db):
    from research import tech_signals as S
    _b15, days = _seed_midsession(db)
    during = S.rating_75_now(db, "UPCO", "2026-10-06 11:50")
    assert during["daily"]["date"] == str(days[-2]) and during["daily"]["tech_rating_label"] == "BUY", \
        "DAY's own snapshot (its close) is not known at 11:50"
    assert during["mtf_alignment_75"] == T.mtf_alignment("BUY", during["tech_rating_75_label"])
    after = S.rating_75_now(db, "UPCO", "2026-10-06")             # a date: that day's close, 15:30
    assert after["as_of"] == "2026-10-06 15:30" and after["daily"]["date"] == str(DAY)
    assert after["daily"]["tech_rating_label"] == "SELL" and after["next_bar_75_end"] is None


def test_as_of_a_day_reproduces_what_the_2030_run_stored(db):
    from research import tech_signals as S
    days = _sessions(DAY, 12)
    _store_daily(db, "UPCO", _daily(np.linspace(100, 170, 331), DAY))
    _store15(db, "UPCO", _trend(days, start=160.0, step=0.03))
    S.run_technical(["UPCO"], as_of=DAY, conn=db)
    snap = S.latest_snapshot(db, ["UPCO"])["UPCO"]
    out = S.rating_75_now(db, "UPCO", str(DAY))
    assert snap["tech_rating_75"] is not None and out["tech_rating_75"] == snap["tech_rating_75"]
    assert out["tech_rating_75_label"] == snap["tech_rating_75_label"] and out["bar_75_end"] == snap["bar_75_end"]
    assert out["mtf_alignment_75"] == snap["mtf_alignment_75"] == "BULL"
    assert out["stored_75"] == {"date": str(DAY), "tech_rating_75": snap["tech_rating_75"],
                                "tech_rating_75_label": snap["tech_rating_75_label"], "bar_75_end": snap["bar_75_end"]}


def test_too_few_bars_no_bars_unknown_symbols_and_bad_times(db):
    from research import tech_signals as S
    _store15(db, "THIN", _trend(_sessions(DAY, 3)))
    thin = S.rating_75_now(db, "THIN", str(DAY))
    assert thin["tech_rating_75"] is None and thin["bars_75"] == 15 and "35 are needed" in thin["reason"]
    assert thin["votes_75"] == {} and thin["daily"] is None and thin["mtf_alignment_75"] is None
    _store_daily(db, "NOBARS", _daily(np.linspace(80, 100, 80), DAY))
    nb = S.rating_75_now(db, "NOBARS", str(DAY))
    assert nb["tech_rating_75"] is None and "no 15-minute bars" in nb["reason"] and nb["latest_15m_end"] is None
    with pytest.raises(LookupError):
        S.rating_75_now(db, "NOSUCH", str(DAY))
    for bad in ("yesterday", "2026-13-01", "2026-10-06 25:00", "2026-10-06T11:50+05:30"):
        with pytest.raises(ValueError):
            S.rating_75_now(db, "THIN", bad)


# ── /signals: the 75m column ───────────────────────────────────────────────

def test_todays_signals_carry_the_75_minute_rating(db):
    from research import tech_signals as S
    _snap(db, "UPCO", DAY, "BUY", "STRONG_BUY", "BULL")
    _snap(db, "DNCO", DAY, "BUY", "SELL", "MIXED")
    for sid, sym in (("a", "UPCO"), ("b", "DNCO"), ("c", "BARE")):
        db.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, "
                   "confluence, status) VALUES (?, ?, ?, 'golden_cross', 'Golden cross', 'BULL', 100, 96, 108, 3, "
                   "'OPEN')", (sid, sym, str(DAY)))
    db.commit()
    sig = {s["symbol"]: s for s in S.todays_signals(db, str(DAY))}
    assert (sig["UPCO"]["tech_rating_75_label"], sig["UPCO"]["mtf_alignment_75"]) == ("STRONG_BUY", "BULL")
    assert sig["UPCO"]["bar_75_end"] == f"{DAY} 15:30" and sig["UPCO"]["tech_rating_label"] == "BUY"
    assert (sig["DNCO"]["tech_rating_75_label"], sig["DNCO"]["mtf_alignment_75"]) == ("SELL", "MIXED")
    assert sig["BARE"]["tech_rating_75_label"] is None and sig["BARE"]["mtf_alignment_75"] is None


def _page_js():
    from dashboard.w39_page import render_signals
    return "\n".join(re.findall(r"<script>(.*?)</script>", render_signals("t"), re.S))


def test_signals_page_script_parses_and_the_75m_cell_reads_right(tmp_path):
    if not os.path.exists(NODE):
        pytest.skip("node is not installed")
    js = _page_js()
    assert "'Weekly','75m','Patterns'" in js and "${c75(x)}" in js
    p = tmp_path / "signals.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    # the cell, on its own: the page's c75 with its esc
    esc = re.search(r"const esc=.*?;\n", js).group(0)
    c75 = js[js.index("const c75="):js.index("const recb=")]
    rows = [{"tech_rating_label": "BUY", "tech_rating_75_label": "STRONG_BUY", "mtf_alignment_75": "BULL",
             "bar_75_end": "2026-10-06 15:30"},
            {"tech_rating_label": "BUY", "tech_rating_75_label": "SELL", "mtf_alignment_75": "MIXED"},
            {"tech_rating_label": "NEUTRAL", "tech_rating_75_label": "BUY", "mtf_alignment_75": "MIXED"},
            {"tech_rating_label": "SELL", "tech_rating_75_label": None, "mtf_alignment_75": None}]
    q = tmp_path / "cell.js"
    q.write_text(esc + c75 + f"console.log(JSON.stringify({json.dumps(rows)}.map(c75)));", encoding="utf-8")
    out = json.loads(subprocess.run([NODE, str(q)], capture_output=True, text=True, timeout=60).stdout)
    assert out[0].startswith('<span class="ok"') and "✓ STRONG BUY" in out[0] and "they agree" in out[0]
    assert "15:30" in out[0]
    assert out[1].startswith('<span class="bad"') and "✗ SELL" in out[1] and "they disagree" in out[1]
    assert out[2].startswith('<span class="muted"') and "~ BUY" in out[2] and "neutral" in out[2]
    assert "—" in out[3] and "35+ completed 75-minute bars" in out[3]


# ── the route and the stock panel ──────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app)


def test_rating_75_route(api, db):
    from enterprise.authz import permission_for
    from research import tech_signals as S
    _seed_midsession(db)
    r = api.get("/api/stock/UPCO/rating-75", params={"as_of": "2026-10-06 11:50"})
    assert r.status_code == 200
    j = r.json()
    assert j["tech_rating_75"] == S.rating_75_now(db, "UPCO", "2026-10-06 11:50")["tech_rating_75"]
    assert j["bars_75_on_day"] == 2 and j["daily"]["tech_rating_label"] == "BUY"
    assert api.get("/api/stock/UPCO/rating-75").status_code == 200, "now: whatever is stored"
    assert api.get("/api/stock/UPCO/rating-75", params={"as_of": "soon"}).status_code == 400
    assert api.get("/api/stock/bad%20sym/rating-75").status_code == 400
    assert api.get("/api/stock/NOSUCH/rating-75", params={"as_of": "2026-10-06"}).status_code == 404
    assert api.post("/api/stock/UPCO/rating-75").status_code == 405
    assert permission_for("GET", "/api/stock/UPCO/rating-75") == "dashboard:read"
    page = api.get("/signals").text
    assert "'75m'" in page and "75-minute bars" in page


def test_stock_panel_shows_the_75_minute_rating(db, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not os.path.exists(CHROME):
        pytest.skip("Chromium is not installed")
    from dashboard import stock_view as SV
    _store_daily(db, "ACME", _daily(np.linspace(90, 110, 120), DAY))
    hist = json.dumps(SV.stock_history(db, "ACME", 400), default=str)
    r75 = json.dumps({"symbol": "ACME", "as_of": "2026-10-06 11:50", "tech_rating_75": 0.45,
                      "tech_rating_75_label": "BUY", "rsi_14_75": 61.2, "supertrend_dir_75": 1,
                      "bar_75_end": "2026-10-06 11:45", "bars_75": 42, "bars_75_on_day": 2,
                      "next_bar_75_end": "2026-10-06 13:00", "daily": {"date": "2026-10-05", "tech_rating": 0.3,
                                                                       "tech_rating_label": "BUY"},
                      "mtf_alignment_75": "BULL", "votes_75": {"RSI": 1}, "reason": None})
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<table><tr data-sym='ACME' id='row'><td>ACME</td></tr></table>" + SV.ASSETS + "</body></html>")
    errors = []
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=CHROME)
        except Exception as e:
            pytest.skip(f"Chromium does not start: {e}")
        try:
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.route("http://atip.test/", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
            page.route("**/api/stock/ACME/history**", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                           body=hist))
            page.route("**/api/stock/ACME/rating-75**", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                             body=r75))
            page.goto("http://atip.test/")
            page.click("#row")
            page.wait_for_selector("#svsvg")
            page.click("#sv .tabs2 div:text-is('Technicals')")
            page.wait_for_function("document.getElementById('sv-r75').innerText.length > 0")
            t = page.inner_text("#sv-r75")
            assert "75-minute rating (as of 2026-10-06 11:50): BUY" in t and "agrees with the daily rating" in t
            assert "bar to 2026-10-06 11:45" in t and "completes at 13:00" in t and "undefined" not in t
            page.click("#sv .bar button:text-is('3M')")                   # a re-render keeps it
            assert "agrees with the daily rating" in page.inner_text("#sv-r75")
        finally:
            browser.close()
    assert not errors, errors
