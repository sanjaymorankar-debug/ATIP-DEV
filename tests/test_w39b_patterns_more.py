"""
W39 Phase 2 (item 4, the rest of plan §7): inverse head and shoulders, descending triangle, rising /
falling channels and rising / falling wedges -- found on the bars before today and broken on today's
close, like the first five patterns; their scans, screener fields, presets and alert hold-back; how
rare they are on random walks; and the stock panel that draws stored signals, candle patterns and the
lines of the patterns in place (dashboard/stock_view.py). Each price path is built so the pattern is
unambiguous, and each has a near miss that must not count.
"""
import os
import re
import shutil
import subprocess

import numpy as np
import pandas as pd
import pytest

from research import patterns as P
from research import technicals as T

NODE = "/opt/node22/bin/node"
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
NEW_SCANS = ("inverse_head_shoulders_breakout", "descending_triangle_breakdown", "rising_channel_breakout",
             "rising_channel_breakdown", "falling_channel_breakout", "falling_channel_breakdown",
             "rising_wedge_breakdown", "falling_wedge_breakout")


def _bars(closes, start="2025-01-06"):
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c * 1.005, "low": c * 0.995, "close": c,
                         "volume": np.full(len(c), 1e5)}, index=pd.bdate_range(start, periods=len(c)))


def _frame(closes):
    return T.indicators(_bars(closes))


def _leg(a, b, n):
    """n bars from just after a to b."""
    return list(np.linspace(a, b, n + 1)[1:])


def _zigzag(lower, upper, touches=7, step=8, first="lower"):
    """A path bouncing between the lines lower(t) and upper(t), touching one of them every `step` bars."""
    pts = [(lower if (k % 2 == 0) == (first == "lower") else upper)(k * step) for k in range(touches)]
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        out += _leg(a, b, step)
    return out


LEAD_DOWN, LEAD_UP = list(np.linspace(112, 100, 30)), list(np.linspace(100, 110, 30))


# ── inverse head and shoulders ────────────────────────────────────────────

IHS = (list(np.linspace(120, 100, 40)) + _leg(100, 90, 5) + _leg(90, 100, 5) + _leg(100, 80, 6) + _leg(80, 99, 6)
       + _leg(99, 89, 5) + [92, 95, 97, 98])


def test_inverse_head_and_shoulders_breakout():
    d = _frame(IHS + [102])
    x = P.inverse_head_shoulders(d)
    assert x["head"] == pytest.approx(80 * 0.995, abs=0.01) and x["left_shoulder"] == pytest.approx(90 * 0.995, abs=0.01)
    assert x["right_shoulder"] == pytest.approx(89 * 0.995, abs=0.01)
    assert 98 < x["neckline_today"] < 99.5, "the neckline joins the two peaks, extended to today"
    reason = P.breakout(d, "inverse_head_shoulders_breakout")
    assert reason and "Inverse head and shoulders" in reason and "closed above the neckline" in reason
    assert P.breakout(_frame(IHS + [97]), "inverse_head_shoulders_breakout") is None, "still below the neckline"
    assert P.inverse_head_shoulders(_frame(IHS + [102, 101])) is None, "broke out yesterday: no longer in place"


def test_inverse_head_and_shoulders_needs_even_shoulders_and_a_decline():
    uneven = (list(np.linspace(120, 100, 40)) + _leg(100, 90, 5) + _leg(90, 100, 5) + _leg(100, 78, 6)
              + _leg(78, 99, 6) + _leg(99, 82, 5) + [88, 92, 95, 97])
    assert P.inverse_head_shoulders(_frame(uneven + [102])) is None, "shoulders 10% apart"
    no_decline = (list(np.linspace(80, 95, 40)) + _leg(95, 90, 5) + _leg(90, 100, 5) + _leg(100, 80, 6)
                  + _leg(80, 99, 6) + _leg(99, 89, 5) + [92, 95, 97, 98])
    assert P.inverse_head_shoulders(_frame(no_decline + [102])) is None, "a bottom needs a decline into it"
    assert P.head_shoulders(_frame(IHS + [102])) is None, "a bottom is not a top"


# ── descending triangle ───────────────────────────────────────────────────

DESC = (list(np.linspace(70, 50, 40)) + _leg(50, 56, 5) + _leg(56, 50, 5) + _leg(50, 54, 4) + _leg(54, 50, 4)
        + _leg(50, 52, 3) + _leg(52, 50.5, 3))


def test_descending_triangle_breakdown():
    d = _frame(DESC + [48.5])
    x = P.descending_triangle(d)
    assert x["touches"] == 3 and x["support"] == pytest.approx(50 * 0.995, abs=0.01)
    assert x["falling_highs"] == sorted(x["falling_highs"], reverse=True) and len(x["falling_highs"]) == 3
    reason = P.breakout(d, "descending_triangle_breakdown")
    assert reason and "Descending triangle" in reason and "closed below it" in reason
    assert P.breakout(_frame(DESC + [50.2]), "descending_triangle_breakdown") is None, "held the support"
    assert P.ascending_triangle(d) is None


def test_descending_triangle_needs_falling_highs():
    rising_highs = (list(np.linspace(70, 50, 40)) + _leg(50, 53, 5) + _leg(53, 50, 5) + _leg(50, 55, 4)
                    + _leg(55, 50, 4) + _leg(50, 57, 3) + _leg(57, 50.5, 3))
    assert P.descending_triangle(_frame(rising_highs + [48.5])) is None


# ── channels ──────────────────────────────────────────────────────────────

RISING = LEAD_DOWN + _zigzag(lambda t: 100 + 0.25 * t, lambda t: 110 + 0.25 * t)
FALLING = LEAD_UP + _zigzag(lambda t: 100 - 0.25 * t, lambda t: 110 - 0.25 * t, first="upper")


def test_rising_channel_breakout_and_breakdown():
    up = _frame(RISING + _leg(112, 119, 6) + [126])
    x = P.channel(up)
    assert x["kind"] == "rising" and 9 < x["height_pct"] < 11 and x["days"] >= 20
    assert x["upper_today"] - x["lower_today"] == pytest.approx(x["upper_prev"] - x["lower_prev"], rel=0.05), "parallel"
    assert "closed above the upper line" in P.breakout(up, "rising_channel_breakout")
    assert P.breakout(up, "falling_channel_breakout") is None, "a rising channel is not a falling one"
    down = _frame(RISING + _leg(112, 116, 4) + [110])
    assert "closed below the lower line" in P.breakout(down, "rising_channel_breakdown")
    assert P.breakout(_frame(RISING + _leg(112, 119, 6) + [121]), "rising_channel_breakout") is None, "inside"
    assert P.wedge(up) is None, "parallel lines are a channel, not a wedge"


def test_falling_channel_breakout_and_breakdown():
    down = _frame(FALLING + _leg(98, 91, 6) + [84])
    assert P.channel(down)["kind"] == "falling"
    assert "Falling channel" in P.breakout(down, "falling_channel_breakdown")
    up = _frame(FALLING + _leg(98, 94, 4) + [99])
    assert "closed above the upper line" in P.breakout(up, "falling_channel_breakout")
    assert P.breakout(_frame(FALLING + _leg(98, 91, 6) + [88]), "falling_channel_breakdown") is None


def test_no_channel_when_the_lines_widen_or_run_flat():
    widening = LEAD_DOWN + _zigzag(lambda t: 100 + 0.1 * t, lambda t: 108 + 0.4 * t) + _leg(104.8, 112, 6)
    assert P.channel(_frame(widening + [113])) is None and P.wedge(_frame(widening + [113])) is None
    flat = LEAD_DOWN + _zigzag(lambda t: 100, lambda t: 110) + _leg(100, 105, 6)
    assert P.channel(_frame(flat + [106])) is None, "a flat range is neither rising nor falling"


# ── wedges ────────────────────────────────────────────────────────────────

RWEDGE = (LEAD_DOWN + _zigzag(lambda t: 100 + 0.32 * t, lambda t: 110 + 0.2 * t) + _leg(115.36, 118, 3)
          + _leg(118, 117, 2))
FWEDGE = (LEAD_UP + _zigzag(lambda t: 100 - 0.2 * t, lambda t: 110 - 0.32 * t, first="upper") + _leg(94.64, 92, 3)
          + _leg(92, 92.5, 2))


def test_rising_wedge_breakdown():
    d = _frame(RWEDGE + [114])
    x = P.wedge(d)
    assert x["kind"] == "rising" and x["narrowing_pct"] > 40
    assert x["upper_today"] > x["lower_today"], "the apex is still ahead"
    assert "Rising wedge" in P.breakout(d, "rising_wedge_breakdown")
    assert P.breakout(_frame(RWEDGE + [117.5]), "rising_wedge_breakdown") is None, "inside the wedge"
    assert P.channel(d) is None, "converging lines are a wedge, not a channel"


def test_falling_wedge_breakout():
    d = _frame(FWEDGE + [96])
    x = P.wedge(d)
    assert x["kind"] == "falling" and x["upper_today"] < x["upper_prev"] and x["lower_today"] < x["lower_prev"]
    assert "closed above the upper line" in P.breakout(d, "falling_wedge_breakout")
    assert P.breakout(d, "rising_wedge_breakdown") is None
    assert P.breakout(_frame(FWEDGE + [92.8]), "falling_wedge_breakout") is None


# ── scans, snapshot, screener, the chart's lines ──────────────────────────

def test_new_breakouts_are_scans_held_like_the_first_five():
    assert set(NEW_SCANS) <= T.PATTERN_SCANS <= set(T.SCANS) and len(T.PATTERN_SCANS) == 14
    assert set(P.SCAN_PATTERN) == T.PATTERN_SCANS
    for key in NEW_SCANS:
        assert T.SCANS[key][1] == ("BULL" if key.endswith("breakout") else "BEAR"), key
    hits = {h[0]: h for h in T.run_scans(_frame(IHS + [102]))}
    assert hits["inverse_head_shoulders_breakout"][2] == "BULL" and "neckline" in hits["inverse_head_shoulders_breakout"][3]
    hits = {h[0]: h for h in T.run_scans(_frame(RWEDGE + [114]))}
    assert hits["rising_wedge_breakdown"][2] == "BEAR" and "lower line" in hits["rising_wedge_breakdown"][3]


def test_snapshot_lists_the_new_patterns_in_place():
    snap = T.snapshot(_bars(IHS))
    assert "Inverse head and shoulders, neckline" in snap["chart_patterns"]
    assert "Falling wedge" in T.snapshot(_bars(FWEDGE))["chart_patterns"]
    assert "Descending triangle, support 49.75" in T.snapshot(_bars(DESC))["chart_patterns"]
    assert "Rising channel" in T.snapshot(_bars(RISING + _leg(112, 119, 6)))["chart_patterns"]
    assert snap["scan_inverse_head_shoulders_breakout"] == 0


def test_screener_fields_and_presets_cover_the_new_scans():
    from research import screener as SC
    for key in NEW_SCANS:
        assert f"scan_{key}" in SC.FIELDS
    presets = {p["key"]: p for p in SC.PRESETS}
    rows = [{"symbol": "UP", "scan_falling_wedge_breakout": 1}, {"symbol": "IHS", "scan_inverse_head_shoulders_breakout": 1},
            {"symbol": "DN", "scan_rising_wedge_breakdown": 1}, {"symbol": "DT", "scan_descending_triangle_breakdown": 1},
            {"symbol": "CH", "scan_rising_channel_breakdown": 1, "chart_patterns": "Rising channel 112.93-124.12"}]
    run = lambda q: [r["symbol"] for r in SC.run_screen(None, q, rows=rows)["rows"]]
    assert sorted(run(presets["t_pattern_breakouts"]["query"])) == ["IHS", "UP"]
    assert sorted(run(presets["t_pattern_breakdowns"]["query"])) == ["CH", "DN", "DT"]
    assert run('chart_patterns CONTAINS "channel"') == ["CH"]


def test_active_patterns_carry_their_lines_to_the_last_bar():
    d = _frame(IHS)
    act = {a["pattern"]: a for a in P.active(d)}
    ihs = act["inverse_head_shoulders"]
    assert ihs["name"] == "Inverse head and shoulders" and ihs["side"] == "BULL" and ihs["triggered"] is None
    neck = ihs["lines"][0]
    assert neck["label"] == "Neckline" and neck["to"] == str(d.index[-1].date()) and neck["from"] < neck["to"]
    assert neck["from_value"] == pytest.approx(100 * 1.005, abs=0.01) and 98 < neck["to_value"] < 99.5
    fired = {a["pattern"]: a for a in P.active(_frame(RWEDGE + [114]))}["wedge"]
    assert fired["name"] == "Rising wedge" and fired["side"] == "BEAR" and fired["triggered"] == "rising_wedge_breakdown"
    assert [ln["label"] for ln in fired["lines"]] == ["Upper line", "Lower line"]
    assert P.active(_frame(list(np.linspace(80, 120, 80)))) == [], "a steady climb draws nothing"


# ── calibration: rare on random walks ─────────────────────────────────────

def _walk(rng, n, vol=0.02):
    c = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, vol / 4, n))
    hi = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": rng.lognormal(12, 0.5, n)},
                        index=pd.bdate_range("2024-01-01", periods=n))


def test_new_breakouts_are_rare_on_random_walks():
    """Random walks (2 % daily volatility, independent volume) have no patterns to find, so whatever fires
    is the rules' false-positive floor. Measured on 200 walks x 250 sessions (seed 11, 50,000 stock-days):
    inverse H&S 0.078 %, descending triangle 0.038 %, rising channel up 0.032 % / down 0.092 %, falling
    channel up 0.098 % / down 0.020 %, rising wedge 0.046 %, falling wedge 0.084 %; setups listed on
    0.15-0.69 %. This smaller sample keeps the suite quick and checks the same bounds (0.02-1.2 % in the plan)."""
    rng = np.random.default_rng(11)
    hits, setups, days = dict.fromkeys(NEW_SCANS, 0), {}, 0
    for _ in range(12):
        d = T.indicators(_walk(rng, 460))
        for t in range(260, 460):
            x = d.iloc[:t + 1]
            days += 1
            for key in NEW_SCANS:
                hits[key] += P.breakout(x, key) is not None
            for label, _ in P.setups(x):
                setups[label] = setups.get(label, 0) + 1
    for key, n in hits.items():
        assert n / days <= 0.012, (key, n, days)
    assert 0 < sum(hits.values()) / days < 0.02, hits
    for label in ("Inverse head and shoulders", "Descending triangle", "Rising channel", "Falling channel",
                  "Rising wedge", "Falling wedge"):
        assert setups.get(label, 0) / days <= 0.07, (label, setups)


def test_alerts_for_the_new_scans_wait_for_a_record(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    import alerts.telegram as TG
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    sent = []
    monkeypatch.setattr(TG, "notify", lambda text, **k: sent.append(text))
    for sid, scan in (("a", "falling_wedge_breakout"), ("b", "golden_cross")):
        conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, "
                     "confluence, status, market_gate, alignment) VALUES (?, ?, '2026-10-06', ?, ?, 'BULL', 100, 96, 108, "
                     "5, 'OPEN', 'OPEN', 'WITH')", (sid, sid, scan, scan))
    conn.commit()
    assert S.alert_top(conn, "2026-10-06")["alerted"] == 1 and "falling_wedge_breakout" not in sent[-1]
    assert {o["scan"]: o for o in S.scan_stats(conn)}["falling_wedge_breakout"]["alerts"] == "held"
    conn.close()


# ── the stock panel: stored marks and the lines of the patterns in place ───

def _seed(conn):
    from research import tech_signals as S
    S.ensure_tables(conn)
    bars = _bars(IHS)
    for day, r in bars.iterrows():
        conn.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
                     "VALUES ('ACME', ?, ?, ?, ?, ?, ?, 'test')",
                     (str(day.date()), r.open, r.high, r.low, r.close, int(r.volume)))
    days = [str(x.date()) for x in bars.index]
    for sid, i, scan, name, direction, reason in (
            ("s1", 45, "double_bottom_breakout", "Double bottom breakout", "BULL",
             "Double bottom 89.55 / 88.55, 22 sessions apart; closed above the neckline 100.50"),
            ("s2", 50, "golden_cross", "Golden cross", "BULL", "SMA 50 crossed above SMA 200 today"),
            ("s3", 55, "macd_bear", "MACD bearish crossover", "BEAR", "MACD crossed below its signal line")):
        conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, reason, entry, stop, "
                     "target, confluence, status) VALUES (?, 'ACME', ?, ?, ?, ?, ?, 100, 96, 108, 3, 'OPEN')",
                     (sid, days[i], scan, name, direction, reason))
    conn.execute("INSERT INTO technical_snapshot (symbol, date, close, patterns, chart_patterns) VALUES "
                 "('ACME', ?, 95, 'Hammer, NR7', 'Double bottom, neckline 100.50')", (days[52],))
    conn.execute("INSERT INTO technical_snapshot (symbol, date, close, patterns) VALUES ('ACME', ?, 96, "
                 "'Bearish engulfing')", (days[60],))
    conn.commit()
    return days


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
    days = _seed(conn)
    conn.close()
    return TestClient(server.app), days


def test_stock_history_returns_the_chart_marks(api):
    client, days = api
    r = client.get("/api/stock/ACME/history", params={"sessions": 2500})
    assert r.status_code == 200
    j = r.json()
    assert len(j["prices"]) == len(IHS) and "signals" in j and "scores" in j, "the old payload is unchanged"
    m = j["chart_marks"]
    sig = {s["scan"]: s for s in m["signals"]}
    assert [s["date"] for s in m["signals"]] == [days[45], days[50], days[55]]
    assert sig["double_bottom_breakout"]["pattern"] is True and "neckline 100.50" in sig["double_bottom_breakout"]["reason"]
    assert sig["golden_cross"]["pattern"] is False and sig["golden_cross"]["reason"] is None
    assert sig["macd_bear"]["direction"] == "BEAR"
    assert {(c["date"], c["name"], c["side"]) for c in m["candles"]} == {
        (days[52], "Hammer", "BULL"), (days[52], "NR7", "NEUTRAL"), (days[60], "Bearish engulfing", "BEAR")}
    assert m["in_place"] == [{"date": days[52], "text": "Double bottom, neckline 100.50"}]
    pats = {p["pattern"]: p for p in m["patterns"]}
    neck = pats["inverse_head_shoulders"]["lines"][0]
    assert neck["label"] == "Neckline" and neck["to"] == days[-1] and days[0] < neck["from"] < days[-1]
    assert client.get("/api/stock/NOPE/history").status_code == 404


def test_candle_sides_cover_every_candle_name():
    src = open(T.__file__, encoding="utf-8").read()
    body = src[src.index("def candles("):src.index("# ── scans")]
    names = set(re.findall(r'\("([A-Z][A-Za-z0-9 ]+)", "(?:BULL|BEAR|NEUTRAL)"\)', body))
    assert names and names == set(T.CANDLE_SIDES), names ^ set(T.CANDLE_SIDES)
    for name, side in re.findall(r'\("([A-Z][A-Za-z0-9 ]+)", "(BULL|BEAR|NEUTRAL)"\)', body):
        assert T.CANDLE_SIDES[name] == side, name


def _panel_js():
    from dashboard import stock_view as SV
    return "\n".join(re.findall(r"<script>(.*?)</script>", SV.ASSETS, re.S))


def test_stock_panel_js_parses(tmp_path):
    node = NODE if os.path.exists(NODE) else shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    p = tmp_path / "stock_view.js"
    p.write_text(_panel_js(), encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_stock_panel_draws_the_marks_without_js_errors(api):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not os.path.exists(CHROME):
        pytest.skip("Chromium is not installed")
    from dashboard import stock_view as SV
    client, days = api
    body = client.get("/api/stock/ACME/history", params={"sessions": 2500}).text
    page_html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
                 "<table><tr data-sym='ACME' id='row'><td>ACME</td></tr></table>" + SV.ASSETS + "</body></html>")
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
            page.route("**/api/stock/**", lambda r: r.fulfill(status=200, content_type="application/json", body=body))
            page.goto("http://atip.test/")
            page.click("#row")
            page.wait_for_selector("#svmarks")
            count = lambda sel: page.locator(sel).count()
            assert count("#svmarks .svm-pat") == 1, "one chart-pattern breakout day"
            assert count("#svmarks .svm-sig") == 2, "golden cross (below) and MACD bearish (above)"
            assert count("#svmarks .svm-cdl") == 2, "Hammer and Bearish engulfing; NR7 is neutral and not drawn"
            assert count("#svmarks .svm-line") >= 1
            assert "undefined" not in page.inner_html("#svmarks") and "NaN" not in page.inner_html("#svmarks")
            assert page.get_attribute("#svmarks .svm-pat", "fill") == "#22c55e", "a BULL breakout is green"
            assert "Inverse head and shoulders" in page.inner_text("#svpl")
            i = 45                                     # the pattern-breakout day (all bars are in the 1Y range)
            tip = page.evaluate("""(i) => {const svg = document.getElementById('svsvg'), r = svg.getBoundingClientRect();
                const n = %d, cw = (1000 - 8 - 62) / n, x = (8 + cw * (i + 0.5)) / 1000 * r.width + r.left;
                document.getElementById('svhit').dispatchEvent(new MouseEvent('mousemove', {clientX: x,
                    clientY: r.top + 40, bubbles: true}));
                return document.getElementById('svtip').innerText}""" % len(IHS), i)
            assert days[45] in tip and "Double bottom breakout" in tip and "neckline 100.50" in tip
            page.click("#svpatbtn")
            assert count("#svmarks") == 0 and "hidden" in page.inner_text("#svpl"), "the toggle hides them"
            page.click("#svpatbtn")
            assert count("#svmarks .svm-pat") == 1
            page.click("#sv .bar button:text-is('Weekly')")             # the marks of a week gather on its bar
            assert count("#svmarks .svm-pat") == 1 and count("#svmarks .svm-line") >= 1
            assert "undefined" not in page.inner_html("#svmarks") and "NaN" not in page.inner_html("#svmarks")
        finally:
            browser.close()
    assert not errors, errors
