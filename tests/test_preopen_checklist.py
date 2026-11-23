"""
The 9:00 AM Pre-Open Checklist (strategy/preopen_checklist.py, owner prompt 22 Nov 2026): each check green / red /
neutral / missing, the vote sum, configurable thresholds, the holiday skip, stale-data fallbacks, storage, the
learning loop, the screens and the API. Every source is a fake: nothing reaches Yahoo, Dhan or NSE.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
DAY = datetime(2026, 11, 25, 9, 0, 20, tzinfo=IST)        # Wed; 24 Nov (Guru Nanak Jayanti) is an NSE holiday
PREV = "2026-11-23"                                       # so the previous session is Mon 23 Nov


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research.global_sync import ensure_tables as gs
    from strategy import preopen_checklist as PC
    init_db()
    conn = get_connection()
    PC.ensure_tables(conn)
    gs(conn)
    yield conn
    conn.close()


@pytest.fixture
def cfg(monkeypatch):
    """The checklist's settings with no config.json block (the defaults), changeable per test."""
    from ops import config as C
    block = {}
    monkeypatch.setattr(C, "load", lambda: {"preopen_checklist": block})
    return block


def _us(nasdaq=1.0, dow=0.5, nq=0.3, ym=0.2, fut_at=None, close_date="2026-11-24"):
    """Fake Yahoo answer: each leg's % change (None = missing)."""
    fut_at = fut_at or (DAY - timedelta(minutes=5)).astimezone(UTC).isoformat()
    out = {}
    for k, v, at in (("nasdaq", nasdaq, close_date), ("dow", dow, close_date), ("nq", nq, fut_at), ("ym", ym, fut_at)):
        if v is not None:
            out[k] = {"last": 100.0 * (1 + v / 100), "prev": 100.0, "as_of": at}
    return lambda: out


def _preopen(adv, dec, when="25-Nov-2026 09:07:58", top_level=True):
    items = [{"metadata": {"symbol": f"S{i}", "previousClose": 100},
              "detail": {"preOpenMarket": {"IEP": 101 if i < adv else 99, "prevClose": 100, "totalBuyQuantity": 10,
                                           "totalSellQuantity": 5, "lastUpdateTime": when}}}
             for i in range(adv + dec)]
    payload = {"data": items}
    if top_level:
        payload.update(advances=adv, declines=dec, unchanged=3)
    return lambda: payload


def _gift(price=26100.0, chg=None, age_min=2, expiry="30-Dec-2026"):
    return lambda: {"price": price, "chg_pct": chg, "expiry": expiry,
                    "as_of": DAY - timedelta(minutes=age_min), "source": "fake NSE marketStatus"}


def _dhan(vix=13.2, nifty_prev=26000.0):
    return lambda: {"india_vix": {"ltp": vix} if vix else {}, "nifty50": {"prev_close": nifty_prev, "ltp": nifty_prev}}


def _src(**kw):
    from strategy.preopen_checklist import Sources
    base = {"us": _us(), "dhan": _dhan(), "preopen": _preopen(1200, 800), "gift": _gift()}
    base.update(kw)
    return Sources(**base)


def _compute(db, cfg_block=None, **kw):
    from strategy import preopen_checklist as PC
    return PC.compute(db, "0900", DAY, _src(**kw))


# ── 1. US cues ───────────────────────────────────────────────────────────────────────────────────────────────

def test_us_all_green_votes_bullish_and_all_red_bearish(db, cfg):
    r = _compute(db)
    assert r["us_vote"] == 1 and r["us_green"] == 4 and r["us_red"] == 0 and r["us_status"] == "LIVE"
    assert r["nasdaq_chg_pct"] == pytest.approx(1.0) and r["us_close_date"] == "2026-11-24"
    r = _compute(db, us=_us(-1.0, -0.4, -0.3, -0.2))
    assert r["us_vote"] == -1 and r["us_red"] == 4


def test_us_mixed_or_inside_the_band_is_flat(db, cfg):
    assert _compute(db, us=_us(1.0, -1.0, 0.5, -0.5))["us_vote"] == 0          # 2 green, 2 red
    r = _compute(db, us=_us(0.05, -0.05, 0.09, 0.0))                            # all inside +-0.10 %
    assert r["us_vote"] == 0 and r["us_green"] == 0 and r["us_red"] == 0
    cfg["us_band_pct"] = 0.01                                                   # a tighter band: now 2 green 1 red
    assert _compute(db, us=_us(0.05, -0.05, 0.09, 0.0))["us_vote"] == 1


def test_us_missing_is_unavailable_not_a_crash(db, cfg):
    def boom():
        raise ConnectionError("yahoo down")
    r = _compute(db, us=boom)
    assert r["us_vote"] is None and r["us_status"] == "UNAVAILABLE"
    assert any("no US close" in c and "yahoo down" in c for c in json.loads(r["caveats_json"]))


def test_us_falls_back_to_the_stored_closes_and_drops_a_stale_future(db, cfg):
    db.execute("INSERT INTO global_markets (date, time, nasdaq, nasdaq_chg, dow, dow_chg) VALUES "
               "('2026-11-25', 'premarket', 19000, -0.8, 44000, -0.6)")
    db.commit()
    old = (DAY - timedelta(hours=5)).astimezone(UTC).isoformat()
    r = _compute(db, us=_us(None, None, 0.4, 0.4, fut_at=old))
    assert r["nasdaq_chg_pct"] == pytest.approx(-0.8) and r["us_close_date"] == "stored 2026-11-25"
    assert r["us_vote"] == -1 and r["us_status"] == "FALLBACK"         # the two stale futures do not count
    assert any("older than 90 min" in c for c in json.loads(r["caveats_json"]))


# ── 2. India VIX (modifier, never a vote) ───────────────────────────────────────────────────────────────────

def test_vix_above_threshold_is_volatile_with_advice_and_does_not_vote(db, cfg):
    r = _compute(db, dhan=_dhan(vix=16.4))
    assert r["vix_regime"] == "Volatile" and r["size_multiplier"] == 0.5 and r["stop_multiplier"] == 1.5
    assert "defined-risk" in r["options_note"]
    calm = _compute(db, dhan=_dhan(vix=15.0))                           # exactly 15 is Stable (<= 15)
    assert calm["vix_regime"] == "Stable" and calm["size_multiplier"] == 1.0
    assert r["vote_sum"] == calm["vote_sum"]                            # VIX changes no vote
    cfg["vix_threshold"] = 17
    assert _compute(db, dhan=_dhan(vix=16.4))["vix_regime"] == "Stable"


def test_vix_falls_back_to_the_stored_prior_close_then_unknown(db, cfg):
    db.execute("INSERT INTO index_levels (date, time, india_vix, nifty50) VALUES (?, '15:30:10', 14.1, 26000)", (PREV,))
    db.commit()
    r = _compute(db, dhan=_dhan(vix=None))
    assert r["vix"] == 14.1 and r["vix_status"] == "FALLBACK" and r["vix_as_of"] == PREV
    db.execute("DELETE FROM index_levels")
    db.commit()
    r = _compute(db, dhan=_dhan(vix=None))
    assert r["vix_regime"] == "Unknown" and r["vix_status"] == "UNAVAILABLE"


# ── 3. NSE advances / declines ──────────────────────────────────────────────────────────────────────────────

def test_ad_from_todays_preopen_book(db, cfg):
    r = _compute(db, preopen=_preopen(1200, 800))
    assert (r["advances"], r["declines"], r["ad_vote"], r["ad_status"]) == (1200, 800, 1, "LIVE")
    assert r["ad_basis"] == "pre-open (after matching)" and r["ad_as_of"] == "2026-11-25 09:07:58"
    r = _compute(db, preopen=_preopen(700, 900, when="25-Nov-2026 09:01:05"))
    assert r["ad_vote"] == -1 and r["ad_basis"] == "pre-open (indicative)"
    r = _compute(db, preopen=_preopen(6, 4, top_level=False))           # no top-level counts: count the items
    assert (r["advances"], r["declines"]) == (6, 4)


def test_ad_neutral_band_is_configurable(db, cfg):
    assert _compute(db, preopen=_preopen(1050, 1000))["ad_vote"] == 1  # default: any ratio above 1.0
    cfg.update(ad_ratio_bull=1.1, ad_ratio_bear=0.9)
    assert _compute(db, preopen=_preopen(1050, 1000))["ad_vote"] == 0
    assert _compute(db, preopen=_preopen(800, 1000))["ad_vote"] == -1


def test_ad_before_nse_publishes_today_falls_back_to_prior_day_labelled(db, cfg):
    db.execute("INSERT INTO market_health (date, advances, declines, breadth_universe) VALUES (?, 180, 320, 500)", (PREV,))
    db.commit()
    r = _compute(db, preopen=_preopen(1200, 800, when="23-Nov-2026 09:07:58"))   # NSE still shows Monday's page
    assert r["ad_basis"] == "prior-day" and r["ad_status"] == "FALLBACK" and r["ad_vote"] == -1
    assert r["ad_as_of"] == PREV and "ATIP universe" in r["ad_source"]
    assert any("PRIOR-DAY" in c for c in json.loads(r["caveats_json"]))
    db.execute("DELETE FROM market_health")
    db.commit()
    r = _compute(db, preopen=lambda: None)
    assert r["ad_vote"] is None and r["ad_status"] == "UNAVAILABLE"


# ── 4. GIFT Nifty ───────────────────────────────────────────────────────────────────────────────────────────

def _gift_ref(db, price):
    db.execute("INSERT INTO global_snapshot (date, label, series, price, quote_ts, captured_at) VALUES "
               "(?, 'close', 'gift_nifty', ?, ?, ?)", (PREV, price, f"{PREV} 15:30:40", f"{PREV} 15:31:02"))
    db.commit()


def test_gift_votes_on_the_basis_adjusted_move_and_keeps_the_raw_one(db, cfg):
    _gift_ref(db, 26050.0)                     # GIFT closed 26,050 while the Nifty closed 26,000 (premium 50)
    r = _compute(db, gift=_gift(26100.0))
    assert r["gift_move_pct"] == pytest.approx(0.192, abs=1e-3) and r["gift_vs_close_pct"] == pytest.approx(0.385, abs=1e-3)
    assert r["gift_vote"] == 1 and r["expected_open"] == "Gap-Up" and "15:30" in r["gift_move_basis"]
    r = _compute(db, gift=_gift(26070.0))       # +0.08 % on the adjusted basis: Flat, though +0.27 % raw
    assert r["gift_vote"] == 0 and r["expected_open"] == "Flat"
    r = _compute(db, gift=_gift(25990.0))
    assert r["gift_vote"] == -1 and r["expected_open"] == "Gap-Down"


def test_gift_without_reference_uses_nse_change_then_raw_labelled(db, cfg):
    r = _compute(db, gift=_gift(26100.0, chg=-0.3))
    assert r["gift_vote"] == -1 and r["gift_move_basis"] == "NSE day change of the contract"
    r = _compute(db, gift=_gift(26100.0, chg=None))
    assert r["gift_vote"] == 1 and "basis not removed" in r["gift_move_basis"]
    cfg["gift_vote_basis"] = "raw"
    _gift_ref(db, 26090.0)
    r = _compute(db, gift=_gift(26100.0))       # adjusted would be +0.04 % (Flat); raw +0.38 % Gap-Up
    assert r["gift_vote"] == 1 and "gift_vote_basis=raw" in r["gift_move_basis"]


def test_gift_band_is_configurable_and_stale_or_missing_quotes_do_not_vote(db, cfg):
    _gift_ref(db, 26000.0)
    cfg["gift_band_pct"] = 0.5
    assert _compute(db, gift=_gift(26100.0))["expected_open"] == "Flat"     # +0.38 % inside +-0.5 %
    r = _compute(db, gift=_gift(26300.0, age_min=45))
    assert r["gift_status"] == "STALE" and r["gift_vote"] is None and r["expected_open"] == "Unknown"
    r = _compute(db, gift=lambda: None)
    assert r["gift_status"] == "UNAVAILABLE" and r["gift_vote"] is None
    assert any("Dhan's GIFT feed is frozen" in c for c in json.loads(r["caveats_json"]))


def test_parse_gift_marketstatus_reads_nse_shapes():
    from strategy.preopen_checklist import parse_gift_marketstatus as P
    q = P({"marketState": [], "giftnifty": {"INSTRUMENTTYPE": "FUTIDX", "SYMBOL": "NIFTY", "EXPIRYDATE": "30-Dec-2026",
                                            "LASTPRICE": "26,104.50", "PERCHANGE": "0.21", "TIMESTMP": "25-Nov-2026 08:59"}})
    assert q["price"] == 26104.5 and q["chg_pct"] == 0.21 and q["expiry"] == "30-Dec-2026"
    assert q["as_of"] == datetime(2026, 11, 25, 8, 59, tzinfo=IST)
    assert P({"giftNifty": [{"lastPrice": 26000, "timestamp": "2026-11-25T08:58:00+05:30"}]})["price"] == 26000
    assert P({"giftnifty": {"LASTPRICE": "-"}}) is None and P({"marketState": []}) is None and P(None) is None


def test_preopen_counts_refuses_yesterdays_page():
    from datetime import date
    from strategy.preopen_checklist import preopen_counts
    assert preopen_counts(_preopen(5, 5, when="24-Nov-2026 09:07:58")(), date(2026, 11, 25)) is None
    assert preopen_counts(_preopen(5, 4)(), date(2026, 11, 25))["advances"] == 5


# ── votes, bias, config ─────────────────────────────────────────────────────────────────────────────────────

def test_vote_sum_and_bias(db, cfg):
    from strategy.preopen_checklist import aggregate, settings
    c = settings()
    assert aggregate(1, 1, 0, c) == {"vote_sum": 2, "votes_counted": 3, "bias": "Bullish"}
    assert aggregate(-1, 1, -1, c)["bias"] == "Bearish"
    assert aggregate(1, -1, 0, c)["bias"] == "Neutral"
    assert aggregate(None, None, None, c) == {"vote_sum": 0, "votes_counted": 0, "bias": "Neutral"}
    assert aggregate(1, None, None, c)["bias"] == "Bullish"
    c["bias_min_votes"] = 2
    assert aggregate(1, 1, -1, c)["bias"] == "Neutral" and aggregate(1, 1, 0, c)["bias"] == "Bullish"
    r = _compute(db)                            # all green: US +1, A/D +1, GIFT (raw, no ref) +1
    assert r["vote_sum"] == 3 and r["bias"] == "Bullish" and json.loads(r["config_json"])["vix_threshold"] == 15.0


def test_settings_overlay_and_bad_values(cfg):
    from strategy.preopen_checklist import DEFAULTS, settings
    cfg.update(vix_threshold="18", gift_band_pct=-1, gift_vote_basis="weird", bias_min_votes=0, junk=1)
    s = settings()
    assert s["vix_threshold"] == 18.0 and s["gift_band_pct"] == DEFAULTS["gift_band_pct"]
    assert s["gift_vote_basis"] == "adjusted" and s["bias_min_votes"] == 1 and "junk" not in s


# ── run, store, holiday skip, Telegram ──────────────────────────────────────────────────────────────────────

def test_run_skips_weekends_and_nse_holidays(db, cfg):
    from strategy import preopen_checklist as PC
    hol = datetime(2026, 11, 24, 9, 0, tzinfo=IST)            # Prakash Gurpurb
    sun = datetime(2026, 11, 22, 9, 0, tzinfo=IST)
    for when in (hol, sun):
        out = PC.run("0900", now=when, conn=db, sources=_src())
        assert out["status"] == "SKIPPED" and "not an NSE trading day" in out["reason"]
    assert db.execute("SELECT COUNT(*) FROM preopen_signals").fetchone()[0] == 0
    cfg["enabled"] = False
    assert PC.run("0900", now=DAY, conn=db, sources=_src())["status"] == "SKIPPED"


def test_run_stores_one_row_per_label_with_ist_time_and_sends_the_0900_line(db, cfg, monkeypatch):
    from strategy import preopen_checklist as PC
    import alerts.telegram as tg
    sent = []
    monkeypatch.setattr(tg, "send_telegram", lambda msg, **k: sent.append((msg, k)) or True)
    out = PC.run("0900", now=DAY, conn=db, sources=_src(), notify=True)
    assert out["status"] == "SUCCESS" and out["bias"] == "Bullish"
    row = PC.latest_run(db, "2026-11-25")
    assert row["run_label"] == "0900" and row["fetched_at_ist"] == "2026-11-25 09:00:20"
    assert len(sent) == 1 and "Pre-Open Checklist (09:00 IST)" in sent[0][0] and sent[0][1]["key"] == "preopen:2026-11-25:0900"
    later = DAY.replace(minute=9, second=5)
    PC.run("0909", now=later, conn=db, sources=_src(), notify=True)          # same read: no second message
    assert len(sent) == 1 and PC.latest_run(db, "2026-11-25")["run_label"] == "0909"
    PC.run("0909", now=later, conn=db, sources=_src(preopen=_preopen(500, 1500), us=_us(-1, -1, -1, -1)), notify=True)
    assert len(sent) == 2 and "09:09 IST update" in sent[1][0]               # the read changed
    assert db.execute("SELECT COUNT(*) FROM preopen_signals").fetchone()[0] == 2


def test_a_broken_check_does_not_cost_the_others(db, cfg, monkeypatch):
    from strategy import preopen_checklist as PC
    monkeypatch.setattr(PC, "check_ad", lambda *a: 1 / 0)
    r = PC.compute(db, "0900", DAY, _src())
    assert r["ad_status"] == "UNAVAILABLE" and r["us_vote"] == 1 and r["votes_counted"] == 2


# ── learning loop ───────────────────────────────────────────────────────────────────────────────────────────

def _day(db, d, open_, close, prev=26000.0, **row):
    from strategy import preopen_checklist as PC
    base = {"date": d, "run_label": "0909", "nifty_prev_close": prev, "bias": "Bullish", "vote_sum": 2,
            "votes_counted": 3, "us_vote": 1, "ad_vote": 1, "ad_basis": "pre-open (after matching)", "gift_vote": 1,
            "expected_open": "Gap-Up", "vix_regime": "Stable", "config_json": json.dumps({"gift_band_pct": 0.15}),
            "fetched_at_ist": f"{d} 09:09:05"}
    base.update(row)
    PC.store(db, base)
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES (?, '09:15:04', ?)", (d, open_))
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES (?, '15:30:00', ?)", (d, close))
    db.commit()


def test_evaluate_and_rolling_record(db, cfg):
    from strategy import preopen_checklist as PC
    _day(db, "2026-11-16", 26100, 26200)                                    # gap +0.38 Gap-Up, day up: all right
    _day(db, "2026-11-17", 25950, 25900, bias="Bearish", us_vote=-1, ad_vote=-1, gift_vote=-1, expected_open="Gap-Down")
    _day(db, "2026-11-18", 26010, 25800, vix_regime="Volatile")              # open Flat (+0.04), day down: wrong
    _day(db, "2026-11-19", 26000, 26050, bias="Neutral", us_vote=0, ad_basis="prior-day")
    out = PC.evaluate(db, today=DAY.date())
    assert out["rows"] == 4
    r = db.execute("SELECT actual_gap_pct, actual_open_class, day_direction FROM preopen_signals WHERE date='2026-11-18'").fetchone()
    assert r[0] == pytest.approx(0.038, abs=1e-3) and r[1] == "Flat" and r[2] == "Down"
    rec = PC.record(db)["last_20"]
    assert rec["days"] == 4 and rec["neutral_days"] == 1
    assert rec["bias"] == {"n": 3, "hits": 2, "hit_rate_pct": 66.7}
    assert rec["gift_open_3way"]["n"] == 4 and rec["gift_open_3way"]["hits"] == 2      # 16 and 17 right
    assert rec["ad_preopen_only"]["n"] == 3 and rec["ad"]["n"] == 4
    assert rec["vix"]["volatile_days"] == 1 and rec["vix"]["volatile_mean_abs_day_pct"] == pytest.approx(0.769, abs=1e-3)
    assert PC.evaluate(db, today=DAY.date())["rows"] == 0                     # never twice


def test_evaluate_waits_for_the_open_then_gives_up(db, cfg):
    from strategy import preopen_checklist as PC
    PC.store(db, {"date": "2026-11-02", "run_label": "0900", "bias": "Neutral", "fetched_at_ist": "2026-11-02 09:00:00",
                  "nifty_prev_close": 26000.0})
    assert PC.evaluate(db, today=DAY.date().replace(day=5))["waiting"] == 1
    PC.evaluate(db, today=DAY.date())
    assert db.execute("SELECT eval_note FROM preopen_signals").fetchone()[0].startswith("no open")


# ── DEF-06: the GIFT contract switch (found from the VPS's live marketStatus payload, 23 Nov 2026) ──────────

def _gift_ref_contract(db, price, expiry):
    db.execute("INSERT INTO preopen_gift_ref (date, price, expiry, quote_ts, source, captured_at) VALUES "
               "(?, ?, ?, ?, 'fake NSE marketStatus', ?)", (PREV, price, expiry, f"{PREV} 15:30:40", f"{PREV} 15:31:02"))
    db.commit()


def test_gift_reference_from_another_contract_is_not_used(db, cfg):
    _gift_ref_contract(db, 25950.0, "25-Nov-2026")    # the old contract; the morning quote is the December one
    r = _compute(db, gift=_gift(26100.0, chg=-0.3, expiry="30-Dec-2026"))
    caveats = json.loads(r["caveats_json"])
    # vs the old contract's 25,950 the move would read +0.58 % (Gap-Up): the calendar spread, not a gap
    assert r["gift_vote"] == -1 and r["expected_open"] == "Gap-Down"
    assert r["gift_move_basis"] == "NSE day change of the contract" and r["gift_move_pct"] == pytest.approx(-0.3)
    assert any("contract changed" in c and "25-Nov-2026" in c and "30-Dec-2026" in c for c in caveats)
    assert not any("no 15:31 GIFT reference stored yet" in c for c in caveats)
    assert r["gift_ref"] == 25950.0 and r["gift_ref_source"].endswith("not used: contract changed")
    r = _compute(db, gift=_gift(26100.0, chg=None, expiry="30-Dec-2026"))
    assert "basis not removed" in r["gift_move_basis"] and r["gift_vote"] == 1    # raw +0.38 % vs the Nifty 26,000


def test_gift_reference_from_the_same_contract_is_used_however_nse_writes_it(db, cfg):
    _gift_ref_contract(db, 26050.0, "30-DEC-2026")
    r = _compute(db, gift=_gift(26100.0, chg=-0.3, expiry="30-Dec-2026"))
    caveats = json.loads(r["caveats_json"])
    assert r["gift_move_pct"] == pytest.approx(0.192, abs=1e-3) and r["gift_vote"] == 1 and "15:30" in r["gift_move_basis"]
    assert not any("contract" in c for c in caveats)
    assert "30-DEC-2026 contract" in r["gift_ref_source"]


def test_a_reference_without_a_contract_is_still_used_with_a_caveat(db, cfg):
    _gift_ref(db, 26050.0)                            # a global_snapshot-only row, as 86db41e captured it
    r = _compute(db, gift=_gift(26100.0))
    assert r["gift_move_pct"] == pytest.approx(0.192, abs=1e-3) and "15:30" in r["gift_move_basis"]
    assert any("roll check was skipped" in c for c in json.loads(r["caveats_json"]))


def test_expiry_key_reads_nse_spellings():
    from strategy.preopen_checklist import expiry_key
    assert expiry_key("29-Dec-2026") == expiry_key("29-DEC-2026") == expiry_key("2026-12-29") == "2026-12-29"
    assert expiry_key(None) is None and expiry_key("-") is None and expiry_key("soon") is None


def test_the_vps_live_marketstatus_payload_of_23_nov_parses():
    """The giftnifty block the VPS read through ATIP's NSE client at 08:46:14 IST on 23 Nov 2026."""
    from strategy.preopen_checklist import expiry_key, parse_gift_marketstatus
    ms = {"marketState": [], "marketcap": {}, "indicativenifty50": {}, "niftyusd": {},
          "giftnifty": {"CONTRACTSTRADED": 4365, "DAYCHANGE": 130, "EXPIRYDATE": "29-Dec-2026",
                        "INSTRUMENTTYPE": "FUTIDX", "LASTPRICE": 26085, "OPTIONTYPE": "-", "PERCHANGE": 0.5,
                        "STRIKEPRICE": 0, "SYMBOL": "NIFTY", "TIMESTMP": "23-Nov-2026 08:43",
                        "id": "0/NIFTYFUTIDX29-Dec-2026--"}}
    q = parse_gift_marketstatus(ms)
    assert q["price"] == 26085.0 and q["chg_pct"] == 0.5 and expiry_key(q["expiry"]) == "2026-12-29"
    assert q["as_of"] == datetime(2026, 11, 23, 8, 43, tzinfo=IST)


def test_market_pulse_skips_a_reference_from_another_contract(db, cfg):
    from datetime import date
    from research import market_pulse as MP
    _gift_ref_contract(db, 25950.0, "25-Nov-2026")
    ref, src = MP._gift_ref(db, date(2026, 11, 25), same_source_only=True, expiry="30-Dec-2026")
    assert ref is None and "contract changed" in src
    ref, src = MP._gift_ref(db, date(2026, 11, 25), same_source_only=True, expiry="25-NOV-2026")
    assert ref == 25950.0 and "15:31 reference" in src
    ref, _ = MP._gift_ref(db, date(2026, 11, 25), same_source_only=True)            # no contract on the quote
    assert ref == 25950.0


# ── GIFT reference, latest GIFT, compact payload ────────────────────────────────────────────────────────────

def test_capture_gift_close_feeds_the_next_mornings_reference(db, cfg):
    from strategy import preopen_checklist as PC
    at = datetime(2026, 11, 23, 15, 31, 2, tzinfo=IST)
    out = PC.capture_gift_close(db, now=at, getter=lambda: {"price": 26050.0, "expiry": "29-Dec-2026",
                                                             "as_of": at - timedelta(seconds=20)})
    assert out["status"] == "SUCCESS" and out["expiry"] == "29-Dec-2026"
    ref = PC.gift_reference(db, DAY.date())                                       # across the 24 Nov holiday
    assert ref["price"] == 26050.0 and ref["expiry"] == "29-Dec-2026" and "29-Dec-2026 contract" in ref["source"]
    assert db.execute("SELECT price FROM global_snapshot WHERE date=? AND series='gift_nifty'",
                      (PREV,)).fetchone()[0] == 26050.0                           # still written for older readers
    assert PC.capture_gift_close(db, now=at.replace(day=24), getter=lambda: None)["status"] == "SKIPPED"
    assert PC.latest_gift(db)["price"] == 26050.0


def test_notify_payload_for_the_apps(db, cfg):
    from strategy import preopen_checklist as PC
    assert PC.notify_payload(db, now=DAY)["ready"] is False
    PC.run("0900", now=DAY, conn=db, sources=_src())
    p = PC.notify_payload(db, now=DAY)
    assert p["ready"] and p["trading_day"] and p["notify"]["title"] == "ATIP pre-open: Bullish"
    assert "VIX 13.2 (Stable)" in p["notify"]["body"] and "09:00 IST" in p["notify"]["body"]
    hol = PC.notify_payload(db, now=datetime(2026, 11, 24, 9, 0, tzinfo=IST))
    assert hol["trading_day"] is False and hol["notify"] is None


# ── paper only, migration, scheduler ────────────────────────────────────────────────────────────────────────

def test_the_checklist_is_informational_and_never_touches_orders():
    src = (Path(__file__).resolve().parents[1] / "strategy" / "preopen_checklist.py").read_text()
    assert "from orders" not in src and "import orders" not in src and "place_order" not in src


def test_migration_0007_creates_the_table(temp_db):
    from db.schema import get_connection, init_db
    from ops import migrations as M
    init_db()
    conn = get_connection()
    try:
        assert "0007" in [m[0] for m in M.MIGRATIONS]
        M.apply(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(preopen_signals)")}
        assert {"date", "run_label", "us_vote", "vix_regime", "ad_vote", "expected_open", "bias", "fetched_at_ist",
                "actual_gap_pct", "day_direction"} <= cols
        assert conn.execute("SELECT status FROM schema_migrations WHERE version='0007'").fetchone()[0] == "APPLIED"
    finally:
        conn.close()


def test_migration_0008_creates_the_gift_reference_table(temp_db):
    from db.schema import get_connection, init_db
    from ops import migrations as M
    init_db()
    conn = get_connection()
    try:
        assert [m[0] for m in M.MIGRATIONS][-2:] == ["0007", "0008"]
        M.apply(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(preopen_gift_ref)")}
        assert {"date", "price", "expiry", "quote_ts", "source", "captured_at"} <= cols
        assert conn.execute("SELECT status FROM schema_migrations WHERE version='0008'").fetchone()[0] == "APPLIED"
        assert not [w for w in M.validate(conn) if w.get("version") in ("0007", "0008")]    # no drift, no failure
    finally:
        conn.close()


def test_scheduler_registers_the_runs_and_skips_holidays(monkeypatch):
    from pipeline import scheduler as S

    class _Job:
        def __init__(self, jobs):
            self.jobs, self.t = jobs, None

        @property
        def day(self):
            return self

        def at(self, t):
            self.t = t
            return self

        def do(self, fn, *a):
            self.jobs.append((self.t, fn, a))

    class _Schedule:
        def __init__(self):
            self.jobs = []

        def every(self, *a):
            return _Job(self.jobs)
    fake = _Schedule()
    monkeypatch.setattr(S, "schedule", fake, raising=False)
    S._schedule_preopen_checklist_jobs()
    assert [(t, fn.__name__, a) for t, fn, a in fake.jobs] == [
        ("09:00", "_preopen_checklist", ("0900",)), ("09:09", "_preopen_checklist", ("0909",)),
        ("15:31", "_preopen_gift_close", ()), ("15:45", "_preopen_evaluate", ())]
    ran = []
    monkeypatch.setattr(S, "run_job", lambda name, fn, *a, **k: ran.append(name))
    monkeypatch.setattr(S, "is_market_day", lambda: False)
    S._preopen_checklist("0900")
    S._preopen_evaluate()
    assert ran == []
    monkeypatch.setattr(S, "is_market_day", lambda: True)
    S._preopen_checklist("0900")
    assert ran == ["preopen_checklist_0900"]


# ── screens and API ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(temp_db, tmp_path, monkeypatch, cfg):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import init_db
    init_db()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), {security.TOKEN_HEADER: security.token()}


def test_api_page_and_home_card(client, monkeypatch):
    from db.schema import get_connection
    from strategy import preopen_checklist as PC
    c, h = client
    assert c.get("/api/preopen").json()["today"] is None
    assert c.get("/api/preopen?compact=1").json()["notify"] is None
    conn = get_connection()
    try:
        now = PC.now_ist().replace(hour=9, minute=0, second=20, microsecond=0)
        PC.compute  # noqa: B018
        row = PC.compute(conn, "0900", now, _src())
        PC.store(conn, row)
    finally:
        conn.close()
    j = c.get("/api/preopen").json()
    assert j["today"]["bias"] == "Bullish" and j["times"] == "IST" and j["paper_only"] is True
    assert "last_20" in j["record"] and j["config"]["vix_threshold"] == 15.0
    page = c.get("/preopen")
    assert page.status_code == 200 and "Track record" in page.text and "Thresholds" in page.text
    home = c.get("/home")
    assert home.status_code == 200 and 'data-widget="preopen"' in home.text and "Pre-Open Checklist" in home.text
    assert "▲ Green" in home.text and "Details and track record" in home.text
    assert c.post("/api/preopen/run").status_code == 401                         # needs the token
    monkeypatch.setattr("ops.role.is_viewer", lambda: True)
    assert c.post("/api/preopen/run", headers=h).status_code == 403              # the view-only server never runs it


# ── backtest scoring (tools/preopen_backtest.py) ────────────────────────────────────────────────────────────

def test_backtest_score_compares_with_the_base_rate_not_fifty_percent():
    import pandas as pd
    from tools.preopen_backtest import score, sign_band
    df = pd.DataFrame({"v": [1, 1, 1, 1, -1, 0, None], "t": [0.5, 0.2, 0.1, -0.3, -0.4, 0.9, 0.2]})
    s = score(df, "v", "t")                               # 5 voted days with a move: 4 right; 3 of 5 were up
    assert s["n"] == 5 and s["hits"] == 4 and s["hit_rate_pct"] == 80.0 and s["base_rate_pct"] == 60.0
    assert s["edge_pp"] == 20.0
    assert sign_band(0.05, 0.10) == 0 and sign_band(-0.2, 0.10) == -1 and sign_band(None, 0.1) is None
