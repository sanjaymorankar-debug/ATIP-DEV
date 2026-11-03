"""Score audit and ten-session fix (3 Nov 2026): every formula against a hand calculation, CRI read
lower-is-better wherever it ranks, and ten sessions filled whenever ten eligible stocks exist.

Weights are the seeded ones (db/schema.py weight_config) -- the values in the comments are worked by hand."""

import math
import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

from scores import engine as E
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture: a throwaway database)

W_MRI = {"MACD": 0.25, "RSI": 0.2, "ADX": 0.15, "Volume": 0.15, "EMA": 0.15, "News": 0.1}
W_ZPI = {"ATR": 0.1, "Institutional": 0.1, "News": 0.1, "RSI": 0.1, "Resistance": 0.15, "Sector": 0.1,
         "Support": 0.15, "Trend": 0.1, "Volume": 0.1}
W_CRI = {"Debt": 0.2, "Distribution": 0.15, "MarketWeakness": 0.15, "NegativeNews": 0.15, "Volatility": 0.2,
         "WeakTrend": 0.15}
W_RRI = {"Recovery": 0.25, "RSIRecovery": 0.15, "Volume": 0.15, "Institutional": 0.15, "Support": 0.2, "News": 0.1}
W_VPI = {"V": 0.18, "TS": 0.15, "RS": 0.12, "LQ": 0.1, "VOL": 0.1, "MR": 0.1, "FG": 0.1, "IS": 0.08, "NS": 0.07}
W_ATIP = {"VPI": 0.2, "SPI": 0.15, "RRI": 0.1, "MRI": 0.1, "MSI": 0.1, "ZPI": 0.1, "TS": 0.1, "FS": 0.1, "INS": 0.05,
          "SAFETY": 0.2}
W_ACS = {"Agreement": 0.2, "DataQuality": 0.15, "HistoricalAccuracy": 0.25, "Liquidity": 0.1, "MarketRegime": 0.2,
         "NewsConfidence": 0.1}


# ── building blocks ─────────────────────────────────────────────────────────────────────────────

def test_minmax_by_hand_and_nan_is_not_a_top_score():
    assert E.minmax(5, 0, 10) == 50.0
    assert E.minmax(2, 0, 10, invert=True) == 80.0
    assert E.minmax(20, 0, 10) == 100.0
    assert E.minmax(float("nan"), 0, 10) == 50.0          # was 100: max(0, min(100, nan)) is 100


def test_weighted_score_renormalises_over_what_is_present():
    # (80*0.5 + 20*0.2) / 0.7 = 44 / 0.7 = 62.857
    assert E.weighted_score({"A": 80, "B": None, "C": 20}, {"A": 0.5, "B": 0.3, "C": 0.2}) == 62.86
    assert E.weighted_score({}, {"A": 1}) == 50.0


def test_above_200_unknown_without_a_200_day_average():
    assert E.above_200({"above_200dma": 0}) is None                       # < 200 sessions: not "below"
    assert E.above_200({"sma_200": 100, "above_200dma": 0}) is False
    assert E.above_200({"sma_200": 100, "above_200dma": 1}) is True


# ── each score, worked by hand ──────────────────────────────────────────────────────────────────

def test_mri_by_hand_macd_is_reversal_and_adx_is_straight():
    tech = {"macd_hist_pct": 0.8335, "rsi_14": 50, "adx_14": 30, "volume_ratio": 1.0, "ema_9": 10, "ema_21": 9}
    # MACD: macd_component = 50 + 0.8335/1.667*50 = 75 -> reversal 25; RSI 50 -> 85; ADX minmax(30,0,60) = 50;
    # Volume 1.0/2*100 = 50; EMA 9 > 21 -> 75; News 50
    # 25*.25 + 85*.2 + 50*.15 + 50*.15 + 75*.15 + 50*.1 = 6.25 + 17 + 7.5 + 7.5 + 11.25 + 5 = 54.5
    assert E.compute_mri(tech, 50.0, W_MRI) == 54.5
    # a stronger trend scores higher now (ADX was inverted), a rising histogram lower (it was momentum)
    assert E.compute_mri({**tech, "adx_14": 50}, 50.0, W_MRI) > E.compute_mri(tech, 50.0, W_MRI)
    assert E.compute_mri({**tech, "macd_hist_pct": 1.6}, 50.0, W_MRI) < E.compute_mri(tech, 50.0, W_MRI)


def test_mri_null_ema_is_left_out_not_zero():
    tech = {"rsi_14": 50, "adx_14": 30}
    # RSI 85 (.2), ADX 50 (.15), News 50 (.1): (17 + 7.5 + 5) / 0.45 = 65.56
    assert E.compute_mri(tech, 50.0, W_MRI) == 65.56


def test_zpi_by_hand_calm_atr_and_straight_adx():
    tech = {"sma_200": 100, "above_200dma": 1, "adx_14": 20, "rsi_14": 40, "atr_pct": 2.5, "volume_ratio": 0.8}
    # Support 80, Resistance min(100, 40) = 40, RSI 90, ATR 100 - 30 = 70, Volume 65, Trend 85, News 50, Sector 60;
    # Institutional absent: (7 + 5 + 9 + 6 + 6 + 12 + 8.5 + 6.5) / 0.9 = 60 / 0.9 = 66.67
    assert E.compute_zpi(tech, pd.DataFrame(), 50.0, 60.0, W_ZPI) == 66.67


def test_zpi_new_listing_is_not_scored_as_below_its_200_dma():
    tech = {"above_200dma": 0, "adx_14": 20, "rsi_14": 40, "atr_pct": 2.5, "volume_ratio": 0.8}
    # Support and Trend absent: (6 + 9 + 7 + 6.5 + 5 + 6) / 0.65 = 39.5 / 0.65 = 60.77
    assert E.compute_zpi(tech, pd.DataFrame(), 50.0, 60.0, W_ZPI) == 60.77


def test_cri_by_hand_higher_is_riskier():
    tech = {"atr_pct": 2, "volume_ratio": 2, "sma_200": 100, "above_200dma": 0, "death_cross": 1}
    mh = {"mh_score": 35, "vix_level": 20}
    # Volatility 30, Distribution 60, WeakTrend 80, NegativeNews 100-40 = 60, MarketWeakness 50+30 = 80; Debt absent
    # (6 + 9 + 12 + 9 + 12) / 0.8 = 60.0
    assert E.compute_cri(tech, {}, 40.0, mh, W_CRI) == 60.0
    # unknown 200-DMA: WeakTrend left out, not +40: (6 + 9 + 9 + 12) / 0.65 = 55.38
    assert E.compute_cri({k: v for k, v in tech.items() if k != "sma_200"}, {}, 40.0, mh, W_CRI) == 55.38


def test_rri_by_hand():
    tech = {"rsi_14": 45, "volume_ratio": 1.5, "sma_200": 100, "above_200dma": 1}
    prices = pd.DataFrame({"close": [120.0, 100.0]})          # newest first: 20 % above the 52-week low
    # Recovery 20/100*200 = 40, RSIRecovery (45-30)*2 = 30, Volume 75, Support 80, News 50; Institutional absent
    # (10 + 4.5 + 11.25 + 16 + 5) / 0.85 = 46.75 / 0.85 = 55.0
    assert E.compute_rri(tech, prices, pd.DataFrame(), 50.0, W_RRI) == 55.0


def test_vpi_by_hand():
    tech = {"atr_pct": 2, "adx_14": 30, "volume_ratio": 1.5, "rsi_14": 45}
    # V 24, TS 50, RS 60, LQ 40, VOL 50, MR 80+(50-45) = 85, NS 50; FG and IS absent
    # (4.32 + 7.5 + 7.2 + 4 + 5 + 8.5 + 3.5) / 0.82 = 40.02 / 0.82 = 48.80
    assert E.compute_vpi(tech, pd.DataFrame(), {}, pd.DataFrame(), 50.0, W_VPI, rs=60, liquidity=40) == 48.8


def test_relative_strength_vs_nifty_by_hand():
    d = pd.date_range("2026-01-01", periods=21).astype(str)[::-1]
    stock = pd.DataFrame({"date": d, "close": [110.0] + [100.0] * 20})      # +10 % over 20 sessions
    bench = pd.DataFrame({"date": d, "close": [105.0] + [100.0] * 20})      # +5 %
    # excess +5 pts on a -15..+15 scale: (5+15)/30*100 = 66.67
    assert E.compute_relative_strength(stock, bench) == 66.67


def test_atip_composite_by_hand_with_safety():
    comp = {"VPI": 60, "RRI": 50, "MRI": 40, "MSI": 50, "ZPI": 70, "SAFETY": 100 - 30, "SPI": None, "FS": None,
            "TS": None, "INS": None}
    # (12 + 5 + 4 + 5 + 7 + 14) / 0.8 = 47 / 0.8 = 58.75
    assert E.weighted_score({k: v for k, v in comp.items() if v is not None}, W_ATIP) == 58.75
    # lower CRI, higher ATIP
    lo = E.weighted_score({**{k: v for k, v in comp.items() if v is not None}, "SAFETY": 90}, W_ATIP)
    assert lo > 58.75


def test_acs_by_hand():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE accuracy_tracker (symbol TEXT, pred_date TEXT, correct_5d INTEGER)")
    scores = {"vpi": 70, "mri": 50, "rri": 65, "zpi": 40, "msi": 61, "cri": 30, "spi": None, "tech_score": None}
    # Agreement 3 of 5 above 60 = 60, DataQuality 6/8 = 75, MarketRegime 55, NewsConfidence 40, Liquidity 80;
    # HistoricalAccuracy absent: (12 + 11.25 + 11 + 4 + 8) / 0.75 = 46.25 / 0.75 = 61.67
    assert E.compute_acs("X", date(2026, 11, 3), scores, {"mh_score": 55}, c, W_ACS, liquidity=80, news_conf=40) == 61.67


def test_composite_weights_sum_to_one_before_safety():
    for w in (W_MRI, W_ZPI, W_CRI, W_RRI, W_ACS):
        assert math.isclose(sum(w.values()), 1.0)
    assert math.isclose(sum(v for k, v in W_ATIP.items() if k != "SAFETY"), 1.0)


# ── CRI: lower is better everywhere it ranks ────────────────────────────────────────────────────

def test_buy_needs_a_low_cri_and_a_zero_cri_is_the_safest():
    good = {"atip_score": 70, "vpi": 65, "zpi": 60, "acs": 70, "mri": 50}
    mh = {"mh_score": 55, "regime": "BULL"}
    assert E.determine_signal({**good, "cri": 20}, mh) == "BUY"
    assert E.determine_signal({**good, "cri": 0}, mh) == "BUY"          # 0 used to be read as 100
    assert E.determine_signal({**good, "cri": 65}, mh) == "HOLD"        # above BUY_MAX_CRI
    assert E.determine_signal({**good, "cri": 80, "atip_score": 35}, mh) == "SELL"


def test_cri_colour_band_reads_low_as_strong():
    from dashboard.score_glossary import band
    assert band("CRI", 20) == "strong" and band("CRI", 85) == "weak"
    assert band("VPI", 85) == "strong"


def test_rank_fill_orders_cri_ascending_among_equal_scores(paper):
    from tests.test_autopick import _conn
    from orders.autopick import _fill_rows
    c = _conn()
    on = str(date.today())
    for sym, cri in (("HIGHRISK", 70.0), ("LOWRISK", 20.0), ("MIDRISK", 45.0), ("DANGER", 80.0)):
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, cri, `signal`) VALUES (?, ?, 60, ?, 'HOLD')",
                  (sym, on, cri))
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES (?, ?, 100, 100, 100, 100, 1000000, 'EQ')", (sym, on))
    c.commit()
    got = [r["symbol"] for r in _fill_rows(c, on, set())]
    c.close()
    assert got == ["LOWRISK", "MIDRISK", "HIGHRISK"]                    # ascending CRI; 80 >= the sell level is out


# ── ten sessions ────────────────────────────────────────────────────────────────────────────────

def _eligible(n, prefix="FILL", score0=64.0, on=None):
    from tests.test_autopick import _conn
    on = str(on or date.today())
    c = _conn()
    for i in range(n):
        sym = f"{prefix}{i:02d}"
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, cri, `signal`) VALUES (?, ?, ?, 40, 'HOLD')",
                  (sym, on, score0 - i * 0.1))
        c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
                  "VALUES (?, ?, 100, 100, 100, 100, 1000000, 'EQ')", (sym, on))
    c.commit(); c.close()


IDS = tuple(f"S{i}" for i in range(1, 11))


@pytest.fixture(autouse=True)
def _known_fill_symbols(monkeypatch):
    """The paper fixture gives a security id to tests.test_w39_qa_suite.KNOWN only; add the FILL stocks."""
    import tests.test_w39_qa_suite as qa
    monkeypatch.setattr(qa, "KNOWN", set(qa.KNOWN) | {f"FILL{i:02d}" for i in range(20)})


def test_ten_sessions_are_filled_when_ten_eligible_stocks_exist_and_no_buy_signal(paper):
    from tests.test_autopick import _sessions, _pick
    _eligible(12)
    _sessions(*IDS)
    p = _pick()
    picked = [p[s]["symbol"] for s in IDS if p[s]["type"] == "PICKED"]
    assert len(picked) == 10 and len(set(picked)) == 10
    assert all(p[s]["source"] == "RANK-FILL" for s in IDS)
    assert picked[0] == "FILL00"                                       # best-ranked first


def test_buy_signals_come_first_then_rank_fill(paper):
    from tests.test_autopick import _sessions, _pick, _signals
    _signals(ACME=(80, "BUY", 100.0), BETA=(70, "BUY", 50.0), GAMMA=(66, "BUY", 60.0))
    _eligible(9)
    _sessions(*IDS)
    p = _pick()
    assert [p[s]["symbol"] for s in ("S1", "S2", "S3")] == ["ACME", "BETA", "GAMMA"]
    assert sum(1 for s in IDS if p[s]["type"] == "PICKED") == 10


def test_board_has_ten_rows_and_says_ten_of_ten(paper):
    from tests.test_autopick import _conn, _sessions
    from orders.session_board import board
    _eligible(10)
    _sessions(*IDS)
    c = _conn()
    b = board(c)
    c.close()
    assert len(b["rows"]) == 10 and b["filled"] == 10 and b["complete"]
    assert b["message"].startswith("10 of 10 sessions filled")
    r = b["rows"][0]
    for k in ("symbol", "rationale", "score_snapshot", "entry", "target", "stoploss", "status", "pnl", "successful_trades"):
        assert k in r
    assert r["target"] > r["entry"] > r["stoploss"]
    assert r["score_snapshot"]["cri_note"] == "lower is better"


def test_board_says_why_when_fewer_than_ten_are_eligible(paper):
    from tests.test_autopick import _conn, _sessions
    from orders.session_board import board
    _eligible(6)
    _sessions(*IDS)
    c = _conn()
    b = board(c)
    c.close()
    assert b["filled"] == 6 and not b["complete"] and len(b["rows"]) == 10
    assert b["message"].startswith("6 of 10 sessions filled") and "S7" in b["message"] and "—" in b["message"]


def test_rank_fill_can_be_switched_off(paper):
    from tests.test_autopick import _conn, _sessions, _pick
    from orders import autopick
    _eligible(3)
    _sessions("S1")
    c = _conn()
    c.execute("INSERT OR REPLACE INTO session_settings (name, value, updated_at) VALUES (?, 'OFF', 'now')",
              (autopick.FILL_SETTING,))
    c.commit(); c.close()
    assert _pick()["S1"]["type"] == "WAITING"


# ── the validation harness ──────────────────────────────────────────────────────────────────────

def test_ic_t_stat_uses_the_effective_sample():
    from scores.validation import ic_summary
    ic = pd.Series([0.1, 0.2, 0.3, 0.2])
    s = ic_summary(ic, 1)
    # mean 0.2, sd 0.08165, t = 0.2 / (0.08165 / 2) = 4.90; at h = 4, n_eff = 1: t = 2.45
    assert s["mean"] == 0.2 and s["t"] == 4.9
    assert ic_summary(ic, 4)["t"] == 2.45


def test_forward_return_is_next_open_to_close_and_masks_jumps():
    from scores.validation import forward_returns
    idx = ["d1", "d2", "d3", "d4"]
    p = {"open": pd.DataFrame({"A": [10, 11, 12, 13], "B": [10, 10, 10, 10]}, index=idx, dtype=float),
         "close": pd.DataFrame({"A": [10, 12, 12, 13], "B": [10, 10, 20, 20]}, index=idx, dtype=float),
         "low": pd.DataFrame({"A": [9, 10, 11, 12], "B": [9, 9, 9, 9]}, index=idx, dtype=float)}
    f = forward_returns(p, horizons=(1, 2))
    assert f[1].loc["d1", "A"] == pytest.approx(12 / 11 - 1)        # signal at d1 close: buy d2 open, sell d2 close
    assert f[2].loc["d1", "A"] == pytest.approx(12 / 11 - 1)        # ... sell d3 close
    assert np.isnan(f[2].loc["d1", "B"])                            # +100 % on d3: an unadjusted action, dropped


def test_rank_ic_is_spearman():
    from scores.validation import _rank_ic
    a = pd.Series(range(40), dtype=float)
    assert _rank_ic(a, a ** 3) == pytest.approx(1.0)
    assert _rank_ic(a, -a) == pytest.approx(-1.0)
