"""Daily post-market RCA (portfolio/post_market_rca.py), its 16:00 IST job, the /rca page and the entry guards
(orders/entry_guards.py, every one OFF until the owner turns it on). Owner prompt 6 Nov 2026."""

from datetime import date, datetime, timedelta

import pytest

from tests.test_session_pools import api  # noqa: F401  (pytest fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

D = "2026-11-06"


@pytest.fixture(autouse=True)
def _offline_industries(monkeypatch):
    """No NSE download in tests: a fixed industry map."""
    import data.index_constituents as IC
    from orders import entry_guards as G
    from portfolio import post_market_rca as R
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: {"TCS": "Information Technology", "HCLTECH": "Information Technology",
                                                                  "LODHA": "Realty", "ACME": "Capital Goods"})
    G._IND.clear()
    R._IND.clear()


def _bars(prices, start="09:15", day=D, spread=0.0):
    """1-minute bars from a list of closes (open = previous close; low/high = min/max -/+ spread)."""
    t = datetime.fromisoformat(f"{day} {start}:00")
    out, prev = [], prices[0]
    for i, c in enumerate(prices):
        o = prev
        out.append({"t": t + timedelta(minutes=i), "o": o, "h": max(o, c) + spread, "l": min(o, c) - spread, "c": c, "v": 1})
        prev = c
    return out


# -- pure parts ------------------------------------------------------------------------------------------------------

def test_resample_aligns_five_minute_candles_on_0915():
    from portfolio.post_market_rca import resample
    five = resample(_bars([100, 101, 99, 102, 103, 104, 98]))
    assert [b["t"].strftime("%H:%M") for b in five] == ["09:15", "09:20"]
    assert (five[0]["o"], five[0]["h"], five[0]["l"], five[0]["c"]) == (100, 103, 99, 103)
    assert (five[1]["o"], five[1]["l"], five[1]["c"]) == (103, 98, 98)


def test_simulate_stop_gap_trail_breakeven_and_late_stop():
    from portfolio.post_market_rca import simulate
    gap = [{"t": datetime(2026, 11, 6, 9, 15), "o": 95.0, "h": 96.0, "l": 94.0, "c": 95.5}]
    assert simulate(gap, 100, 1, 98, 3, 1)["exit"] == 95.0                      # opened through the stop: fills at the open
    up = _bars([100, 101, 103.5, 104, 102.5])                                   # +4 % then back: the 1 % trail sells at 102.96
    r = simulate(up, 100, 1, 98, 3, 1)
    assert r["reason"] == "stop" and r["exit"] == pytest.approx(104 * 0.99)
    assert simulate(_bars([100, 101]), 100, 1, 98, 3, 1)["reason"] == "held"
    be = _bars([100, 102.2, 101, 99.5])                                         # +2.2 %, then under the entry
    assert simulate(be, 100, 1, 98, 3, 1, breakeven_r=2.0)["exit"] == 100
    early = _bars([100, 99.5, 100, 101], start="09:43")
    early[1]["l"] = 97.5                                                        # a wick under the stop at 09:44 only
    assert simulate(early, 100, 1, 98, 3, 1)["at"].strftime("%H:%M") == "09:44"
    assert simulate(early, 100, 1, 98, 3, 1, stop_from="09:45")["reason"] == "held"
    assert simulate(_bars([100, 103.5, 103]), 100, 1, 98, 3, 1, high_water=110)["reason"] == "stop"  # carried-in trail


def _trade(**k):
    t = {"session": "S9", "symbol": "LODHA", "rule_id": "r1", "qty": 22.0, "entry": 1156.68,
         "entry_at": datetime(2026, 11, 6, 9, 16, 42), "picked_at": datetime(2026, 11, 6, 9, 16, 40), "stop": 1133.55,
         "stop_pct": 2.0, "target_pct": 3.0, "trail_pct": 1.0, "trigger": 1166.0, "signal": "HOLD", "signal_date": "2026-11-05",
         "score": 63.5, "source": "RANK-FILL", "closed_today": True, "exit_price": 1132.63,
         "exit_at": datetime(2026, 11, 6, 11, 24, 58), "exit_reason": "Stop-loss", "pnl_net": -601.04, "charges": 71.94,
         "stop_trigger": 1133.55, "stop_leg_hw": None, "env": "VIRTUAL"}
    t.update(k)
    return t


def _lodha_bars():
    """LODHA's 6 Nov shape: opens 1164, slides to 1131 by ~11:29, closes 1179 at the high."""
    import numpy as np
    pts = list(np.linspace(1164, 1156, 2)) + list(np.linspace(1156, 1131.5, 132)) + [1131.0] + \
        list(np.linspace(1132, 1179, 239))
    return _bars([round(float(p), 2) for p in pts], spread=0.3)


def test_path_finds_the_low_the_recovery_and_a_stop_before_it():
    from portfolio.post_market_rca import path
    p = path(_trade(), _lodha_bars(), D)
    assert p["low"] == pytest.approx(1130.7, abs=0.5) and p["low_at"].strftime("%H:%M") == "11:29"
    assert p["recovery_at"] is not None and p["recovery_at"] > p["low_at"]
    assert p["back_to_entry_at"] is not None and p["close"] == 1179.0
    assert p["stop_at_low"] is True and p["stopped_before_recovery"] is True


def test_classify_names_the_tight_stop_and_not_the_market():
    from portfolio.post_market_rca import classify, path
    bars = _lodha_bars()
    p = path(_trade(), bars, D)
    p["_day"] = D
    ctx = {"scores": {"beta_1y": 1.8}, "ind": {"atr_14": 43.37, "atr_pct": 3.69, "ema_9": 1073, "ema_21": 1066, "ema_50": 1094},
           "prev_close": 1175.8, "score_before_day": {}, "industry": "Realty", "null_fields": ["SPI"], "news": {"s": 0.6, "n": 6},
           "announcements": []}
    nifty = _bars([24538 + i for i in range(len(bars))])                    # Nifty rising all day
    t = _trade()
    codes = {c[0]: c for c in classify(t, ctx, p, nifty, 24536.6, [])}
    assert "d" in codes and "0.53x ATR14" in codes["d"][2]
    assert "b" in codes and "RANK-FILL" in codes["b"][2] and "NULL inputs: SPI" in codes["b"][2]
    assert "c" in codes and "gap-down" in codes["c"][2]
    assert "h" in codes and "beta 1.80" in codes["h"][2]
    assert "a" not in codes and t["market_note"].startswith("Not market-wide")
    assert "g" not in codes and t["news_note"].startswith("No negative news")


def test_what_if_is_measured_like_the_day_mtm():
    from portfolio.post_market_rca import what_if
    bars = {"LODHA": _lodha_bars(), "NIFTY50": _bars([24538.0] * 375)}
    ctxs = {"r1": {"ind": {"atr_14": 43.37}, "prev_close": 1175.8, "industry": "Realty", "prior_high": None},
            "_nifty_prev": 24536.6}
    w = what_if([_trade()], bars, ctxs, D)
    from backtest.costs import cost_model_from_config
    m = cost_model_from_config()
    real = (1132.63 - 1156.68) * 22 - m.total("BUY", 1156.68 * 22) - m.total("SELL", 1132.63 * 22)
    assert w["actual"]["pnl"] == pytest.approx(real, abs=0.01)
    assert w["atr_1.5"]["trades"][0]["how"] == "held" and w["atr_1.5"]["delta"] > 0     # a wide stop rode it out
    assert w["skip_gap_1"]["pnl"] == 0 and w["skip_gap_1"]["delta"] == pytest.approx(-real, abs=0.01)  # opened -1.004 %
    assert w["min_score_63"]["pnl"] == w["actual"]["pnl"] and w["buy_only"]["pnl"] == 0
    assert w["entry_after_0945"]["trades"][0]["how"] in ("stop", "held")


# -- the report, stored and shown ---------------------------------------------------------------------------------------

def _closed_losing_trade(paper):
    """S1 buys ACME at 100 through the real order path, then its 2 % stop sells at 97.9: one losing trade today."""
    from tests.test_autopick import _do, _pick, _sessions, _signals, _tick
    from orders import rules
    _signals(on=date.today() - timedelta(days=1), ACME=(80, "BUY", 100.0))
    _sessions("S1")
    e = _pick()["S1"]
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=97.9)
    from db.schema import get_connection
    c = get_connection()
    today = str(date.today())
    c.execute("UPDATE paper_order SET created_at = ? WHERE symbol = 'ACME' AND transaction_type = 'BUY'", (f"{today}T09:16:00",))
    c.execute("UPDATE paper_order SET created_at = ? WHERE symbol = 'ACME' AND transaction_type = 'SELL'", (f"{today}T10:00:30",))
    c.commit()
    c.close()
    return e, rules


def _acme_bars():
    today = str(date.today())
    return {"ACME": _bars([100.0] * 2 + [99.0, 98.5, 97.9] + [97.5] * 40 + [99, 100, 101] * 20, day=today),
            "NIFTY50": _bars([25000.0] * 120, day=today)}


def test_build_run_and_latest_on_a_real_session_trade(paper):
    from portfolio import post_market_rca as R
    _closed_losing_trade(paper)
    today = str(date.today())
    rep = R.build(today, bars=_acme_bars())
    t = next(x for x in rep["trades"] if x["symbol"] == "ACME")
    assert t["exit_reason"] == "Stop-loss" and t["pnl_net"] < 0 and t["losing"] and t["entry_at"].endswith("09:16:00")
    s1 = next(s for s in rep["sessions"] if s["session"] == "S1")
    assert (s1["bought"], s1["closed"], s1["losses"]) == (1, 1, 1) and s1["net"] == pytest.approx(t["pnl_net"])
    assert rep["what_if"]["actual"]["pnl"] == pytest.approx(rep["totals"]["day_mtm"], abs=0.05)
    assert t["chart"] and t["chart"][0][0] == "09:15"
    from db.schema import get_connection
    c = get_connection()
    R.ensure_table(c)
    import json
    c.execute("INSERT OR REPLACE INTO post_market_rca (day, built_at, source, headline, payload) VALUES (?,?,?,?,?)",
              (today, rep["built_at"], "test", rep["headline"], json.dumps(rep, default=R._jsonable)))
    c.commit()
    assert R.latest(c)["day"] == today and R.days(c) == [today]
    c.close()


def test_run_scheduled_skips_holidays_and_the_viewer(monkeypatch, temp_db):
    from portfolio import post_market_rca as R
    import utils.trading_calendar as TC
    monkeypatch.setattr(TC, "is_trading_day", lambda d: False)
    assert R.run_scheduled(datetime(2026, 11, 8, 16, 0))["reason"] == "not a market day"
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    assert R.run_scheduled(datetime(2026, 11, 6, 16, 0))["reason"] == "view-only server"


def test_the_job_is_registered_at_1600_ist():
    from pipeline import w39_jobs

    class Sched:
        def __init__(self):
            self.jobs = []

        def every(self):
            return self

        @property
        def day(self):
            return self

        def at(self, t):
            self.t = t
            return self

        def do(self, fn, name, *a):
            self.jobs.append((self.t, name))

    s = Sched()
    lines = w39_jobs.schedule_jobs(s, lambda *a: None)
    assert ("16:00", "post_market_rca") in s.jobs and any("post_market_rca daily 16:00 IST" in x for x in lines)


def test_rca_page_and_api(api, paper):
    client, h = api
    assert client.get("/api/rca").status_code == 404
    page = client.get("/rca")
    assert page.status_code == 200 and "No post-market RCA stored yet" in page.text
    from portfolio import post_market_rca as R
    _closed_losing_trade(paper)
    rep = R.build(str(date.today()), bars=_acme_bars())
    from db.schema import get_connection
    import json
    c = get_connection()
    R.ensure_table(c)
    c.execute("INSERT INTO post_market_rca (day, built_at, source, headline, payload) VALUES (?,?,?,?,?)",
              (rep["day"], rep["built_at"], "test", rep["headline"], json.dumps(rep, default=R._jsonable)))
    c.commit()
    c.close()
    page = client.get("/rca")
    assert page.status_code == 200 and "Intraday path of every stock held" in page.text and "ACME" in page.text
    assert "<svg" in page.text and "Proposed guards" in page.text and "What would have avoided" in page.text
    assert client.get("/api/rca").json()["day"] == rep["day"]
    app_page = client.get("/rca", headers={"user-agent": "x ATIPApp/2026.11.06.1"})
    assert app_page.status_code == 200 and 'data-guard="' in app_page.text        # parity: the apps get the same switches
    from dashboard.shell import NAV
    assert ("/rca" in [href for *_, kids in NAV for _, href, _ in kids])


def test_guard_api_needs_the_token_and_an_ack(api):
    client, h = api
    assert client.post("/api/rca/guards", json={"name": "guard_min_score", "value": 63, "ack": True}).status_code == 401
    assert client.post("/api/rca/guards", json={"name": "guard_min_score", "value": 63}, headers=h).status_code == 409
    assert client.post("/api/rca/guards", json={"name": "guard_nope", "value": 1, "ack": True}, headers=h).status_code == 400
    r = client.post("/api/rca/guards", json={"name": "guard_min_score", "value": 63, "ack": True}, headers=h)
    assert r.status_code == 200 and r.json()["value"] == 63
    g = {x["name"]: x for x in client.get("/api/rca/guards").json()["guards"]}
    assert g["guard_min_score"]["on"] and not g["guard_orb_minutes"]["on"]
    assert client.post("/api/rca/guards", json={"name": "guard_min_score", "value": "off"}, headers=h).json()["value"] is None


# -- the guards ----------------------------------------------------------------------------------------------------------

def _gconn():
    from db.schema import get_connection, init_db
    init_db()
    return get_connection()


def test_every_guard_is_off_by_default_and_on_needs_an_ack(temp_db):
    from orders import entry_guards as G
    c = _gconn()
    assert all(v is None for v in G.settings(c).values())
    assert G.check_entry(c, {"symbol": "ACME", "score": 50, "signal": "HOLD"}) is None
    with pytest.raises(PermissionError):
        G.set_guard(c, "guard_min_score", 63)
    assert G.set_guard(c, "guard_min_score", 63, ack=True)["value"] == 63
    assert "scores 50" in G.check_entry(c, {"symbol": "ACME", "score": 50, "signal": "HOLD"})
    assert G.check_entry(c, {"symbol": "ACME", "score": 50, "signal": "BUY"}) is None      # a BUY signal is not a fill
    with pytest.raises(ValueError):
        G.set_guard(c, "guard_orb_minutes", 20, ack=True)                                  # 15-minute steps only
    ev = c.execute("SELECT event FROM session_events WHERE event = 'GUARD'").fetchall()
    assert len(ev) == 1
    c.close()


def test_time_nifty_sector_and_orb_guards(temp_db, monkeypatch):
    from orders import entry_guards as G
    c = _gconn()
    for k, v in (("guard_entry_not_before", "09:45"), ("guard_nifty_weak", "ON"), ("guard_sector_cap", 1)):
        G.set_guard(c, k, v, ack=True)
    cand = {"symbol": "TCS", "score": 64, "signal": "HOLD", "price": 2510.0}
    assert "before 09:45" in G.check_entry(c, cand, now=datetime(2026, 11, 6, 9, 20))
    c.execute("CREATE TABLE IF NOT EXISTS index_levels (date TEXT, time TEXT, nifty50 REAL, nifty50_chg REAL)")
    c.execute("INSERT INTO index_levels (date, time, nifty50, nifty50_chg) VALUES (?, '09:15:20', 24538, 0.0)", (D,))
    c.execute("INSERT INTO index_levels (date, time, nifty50, nifty50_chg) VALUES (?, '09:50:00', 24500, -0.15)", (D,))
    c.commit()
    assert "Nifty is weak" in G.check_entry(c, cand, now=datetime(2026, 11, 6, 10, 0))
    c.execute("INSERT INTO index_levels (date, time, nifty50, nifty50_chg) VALUES (?, '10:30:00', 24700, 0.66)", (D,))
    c.commit()
    assert "already held" in G.check_entry(c, cand, busy={"HCLTECH"}, now=datetime(2026, 11, 6, 10, 31))
    assert G.check_entry(c, cand, busy={"LODHA"}, now=datetime(2026, 11, 6, 10, 31)) is None
    G.set_guard(c, "guard_orb_minutes", 30, ack=True)
    assert "no 30-minute opening range" in G.check_entry(c, cand, now=datetime(2026, 11, 6, 10, 31))
    G._ORH.clear()
    for t, hi in (("09:15:00", 2533.6), ("09:30:00", 2565.0)):
        c.execute("INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume) "
                  "VALUES ('TCS', ?, 15, 1, ?, 1, 1, 1)", (f"{D} {t}", hi))
    c.commit()
    assert "not above its opening-range high 2,565.00" in G.check_entry(c, cand, now=datetime(2026, 11, 6, 10, 31))
    assert G.check_entry(c, dict(cand, price=2570.0), now=datetime(2026, 11, 6, 10, 31)) is None
    c.close()


def test_auto_pick_passes_over_a_stock_a_guard_refuses(paper):
    from tests.test_autopick import _do, _pick, _sessions, _signals
    from orders import entry_guards as G
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    _do(G.set_guard, "guard_entry_not_before", "23:59", ack=True)
    e = _pick()["S1"]
    if datetime.now().strftime("%H:%M") < "23:59":
        assert e["type"] == "WAITING" and "guard: no auto-pick before 23:59 IST" in e["reason"]
    _do(G.set_guard, "guard_entry_not_before", "off")
    assert _pick()["S1"]["type"] == "PICKED"


def test_the_viewer_cannot_change_a_guard(temp_db, monkeypatch):
    from orders import entry_guards as G
    c = _gconn()
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    with pytest.raises(RuntimeError):
        G.set_guard(c, "guard_min_score", 63, ack=True)
    c.close()
