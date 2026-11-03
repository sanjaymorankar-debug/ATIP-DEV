"""Full code review fixes (3 Nov 2026, 10:54 PM IST brief): the order engine, the ten-session board's rank and counts,
the scores' NULL handling and default day, the ingestion fixes, the alerts, the calendar alarm, the design system
(dark default, freshness stamp, score badges, the one order dialog) and the quant / ML fixes.

Every expected value is worked by hand in the comment beside it."""

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from tests.test_w39_qa_suite import paper, _rule, _paper_orders, _book  # noqa: F401  (paper: a throwaway database)


# ── order engine ───────────────────────────────────────────────────────────────────────────────

def _buy_and_hold(feed, qty=10, price=100.0):
    """A filled BUY of `qty` ACME at `price` through the real rules -> broker -> paper path."""
    from orders import rules as OR
    e = _rule(quantity_value=qty, trigger_value=price, reference_price=price)
    feed.prices["ACME"] = price
    OR.check_triggers()
    assert OR.confirm_rule(e["id"])["status"] == "PLACED"
    return e


def test_execute_rule_never_sends_for_a_cancelled_or_executed_rule(paper):
    from db.schema import get_connection
    from orders import rules as OR
    r = _rule()
    c = get_connection()
    c.execute("UPDATE order_rules SET status='CANCELLED' WHERE id=?", (r["id"],)); c.commit(); c.close()
    out = OR.execute_rule(r["id"], 100.0)
    assert out["status"] == "SKIPPED" and "CANCELLED" in out["error"]
    assert _paper_orders() == []                                          # nothing reached the paper broker


def test_one_pass_sells_once_when_the_stop_cancels_its_target(paper):
    """Hold 10 ACME. A TARGET (sell >= 100) and a STOP (sell <= 110, a stop trailed above the target) of one OCO group
    are both true at 105. The STOP (newer, tested first) sells 10 and cancels the TARGET; the stale TARGET in the same
    pass used to be sent as a second SELL of 10."""
    from orders import rules as OR
    e = _buy_and_hold(paper)
    tgt = _rule(side="SELL", trigger_value=100, trigger_direction="ABOVE", role="TARGET", oco_group_id="G",
                parent_rule_id=e["id"], require_confirmation=False)
    stp = _rule(side="SELL", trigger_value=110, trigger_direction="BELOW", role="STOP", oco_group_id="G",
                parent_rule_id=e["id"], require_confirmation=False)
    from db.schema import get_connection          # drop the entry's own bracket-less legs, if any, from the picture
    paper.prices["ACME"] = 105.0
    OR.check_triggers()
    sells = [o for o in _paper_orders() if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 10                  # one sale, not two
    assert {OR.get_rule(tgt["id"])["status"], OR.get_rule(stp["id"])["status"]} == {"EXECUTED", "CANCELLED"}


def test_a_failed_exit_leg_stays_active_and_is_tried_again(paper, monkeypatch):
    from orders import rules as OR
    _buy_and_hold(paper)
    stp = _rule(side="SELL", trigger_value=95, trigger_direction="BELOW", role="STOP", parent_rule_id="E",
                require_confirmation=False)
    monkeypatch.setattr(OR, "place_sell_order", lambda *a, **k: {"status": "FAILED", "error": "no live quote"})
    out = OR.execute_rule(stp["id"], 94.0)
    r = OR.get_rule(stp["id"])
    assert out.get("retrying") and r["status"] == "ACTIVE" and r["execution_error"].startswith("retrying:")
    # the shares are gone: that one is final
    monkeypatch.setattr(OR, "place_sell_order",
                        lambda *a, **k: {"status": "FAILED", "error": "cannot sell 10, paper position holds 0"})
    OR.execute_rule(stp["id"], 94.0)
    assert OR.get_rule(stp["id"])["status"] == "FAILED"


def test_an_entry_that_fails_still_fails(paper, monkeypatch):
    from orders import rules as OR
    e = _rule()
    monkeypatch.setattr(OR, "place_buy_order", lambda *a, **k: {"status": "FAILED", "error": "no live quote"})
    OR.execute_rule(e["id"], 100.0)
    assert OR.get_rule(e["id"])["status"] == "FAILED"


def test_halt_holds_an_automatic_exit_and_it_fires_after_the_halt_lifts(paper):
    from orders import risk, rules as OR
    _buy_and_hold(paper)
    stp = _rule(side="SELL", trigger_value=95, trigger_direction="BELOW", role="STOP", parent_rule_id="E",
                require_confirmation=False)
    risk.halt("test")
    paper.prices["ACME"] = 94.0
    ev = OR.check_triggers()
    assert ("HELD_HALTED", stp["id"]) in [(x["type"], x.get("rule_id")) for x in ev]
    assert OR.get_rule(stp["id"])["status"] == "ACTIVE"                    # used to be FAILED for good
    risk.resume()
    ev = OR.check_triggers()
    assert ("PLACED", stp["id"]) in [(x["type"], x.get("rule_id")) for x in ev]
    assert _book()[1] == 0


def test_an_expired_confirmation_cannot_be_confirmed(paper):
    from db.schema import get_connection
    from orders import rules as OR
    e = _rule()
    paper.prices["ACME"] = 100.0
    OR.check_triggers()
    assert OR.get_rule(e["id"])["status"] == "PENDING_CONFIRMATION"
    c = get_connection()
    c.execute("UPDATE order_rules SET confirmation_expires_at=? WHERE id=?",
              ((datetime.now() - timedelta(minutes=1)).isoformat(), e["id"])); c.commit(); c.close()
    out = OR.confirm_rule(e["id"])
    assert out["status"] == "FAILED" and OR.get_rule(e["id"])["status"] == "EXPIRED"
    assert _paper_orders() == []


def test_a_live_buy_with_no_price_is_refused_not_valued_at_zero(paper, monkeypatch):
    import orders.broker as B
    import ops.trading_safety as TS
    monkeypatch.setattr(B, "broker_env", lambda: B.LIVE)                    # real money: never valued at Rs 0
    monkeypatch.setattr(TS, "live_trading_enabled", lambda *a, **k: (True, "test"))
    out = B.place_buy_order("BETA", 10, order_type="MARKET", confirm=True)  # no quote, no stored close
    assert out["status"] == "FAILED" and "no price" in out["error"]


def test_a_market_order_with_no_live_quote_is_valued_at_the_last_close(paper):
    from db.schema import get_connection
    from orders.broker import _estimate_order_value
    c = get_connection()
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "VALUES ('BETA', '2026-11-03', 50, 50, 50, 50, 1, 'EQ')"); c.commit(); c.close()
    assert _estimate_order_value("BETA", 10, "MARKET", 0) == 500.0          # was 10 x Rs 0


@pytest.fixture
def api(paper, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    import pipeline.operating_schedule as OS
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    if hasattr(OS, "is_stopped"):
        monkeypatch.setattr(OS, "is_stopped", lambda *a, **k: False)
    return TestClient(server.app), {security.TOKEN_HEADER: security.token()}


def test_the_api_refuses_a_posted_exit_leg(api):
    client, h = api
    r = client.post("/api/orders", headers=h, json={"symbol": "ACME", "side": "BUY", "trigger_type": "PRICE",
                                                     "trigger_value": 100, "reference_price": 100, "quantity_type": "SHARES",
                                                     "quantity_value": 1, "role": "STOP", "require_confirmation": False})
    if r.status_code == 423:
        pytest.skip("ATIP's stop window refused the write before the route")
    assert r.status_code == 400 and "bracket leg" in r.json()["error"]


# ── auto-pick and the ten-session board ─────────────────────────────────────────────────────────

IDS = tuple(f"S{i}" for i in range(1, 11))


@pytest.fixture(autouse=True)
def _known_fill_symbols(monkeypatch):
    import tests.test_w39_qa_suite as qa
    monkeypatch.setattr(qa, "KNOWN", set(qa.KNOWN) | {f"FILL{i:02d}" for i in range(20)})


def test_board_gives_each_row_its_rank_and_says_how_many_are_held_queued_or_planned(paper):
    from tests.test_autopick import _conn, _sessions
    from tests.test_score_audit_2026_11_03 import _eligible
    from orders.session_board import board
    _eligible(10)
    _sessions(*IDS)
    c = _conn(); b = board(c); c.close()
    assert b["message"].startswith("10 of 10 sessions filled")
    assert [r["rank"] for r in b["rows"]] == list(range(1, 11))          # FILL00 (64.0) first ... FILL09 (63.1)
    assert all(r["rank_of"] == 10 for r in b["rows"])
    assert b["counts"] == {"holding": 0, "queued": 0, "planned": 10, "waiting": 0}
    assert b["detail"] == "0 holding · 0 queued · 10 to buy at the next pick"


def test_board_rank_puts_lower_cri_first_among_equal_scores(paper):
    from tests.test_autopick import _conn
    from orders.session_board import _ranks
    c = _conn()
    on = str(date.today())
    for sym, cri in (("RISKY", 60.0), ("SAFE", 10.0), ("MID", 30.0)):
        c.execute("INSERT INTO ai_scores (symbol, date, atip_score, cri, `signal`) VALUES (?, ?, 60, ?, 'HOLD')", (sym, on, cri))
    c.commit()
    ranks, n, d = _ranks(c)
    c.close()
    assert (ranks["SAFE"], ranks["MID"], ranks["RISKY"]) == (1, 2, 3) and n == 3 and d == on     # CRI ascending


def test_autopick_waits_a_day_after_an_entry_fails(paper):
    from tests.test_autopick import _conn, _sessions, _pick
    from tests.test_score_audit_2026_11_03 import _eligible
    from orders import sessions
    _eligible(3)
    _sessions("S1")
    r = _rule(symbol="FILL00", quantity_type="SESSION", quantity_value=100)
    c = _conn()
    sessions.link_rule(c, "S1", r["id"])
    c.execute("UPDATE order_rules SET status='FAILED', execution_error='risk limit', updated_at=? WHERE id=?",
              (datetime.now().isoformat(), r["id"])); c.commit(); c.close()
    p = _pick()["S1"]
    assert p["type"] == "WAITING" and "failed" in p["reason"] and "risk limit" in p["reason"]


# ── scores ─────────────────────────────────────────────────────────────────────────────────────

def test_a_market_health_of_zero_is_the_weakest_market_not_a_neutral_50():
    from scores import engine as E
    s = {"atip_score": 70, "vpi": 65, "zpi": 60, "cri": 30, "acs": 50, "mri": None}
    assert E.determine_signal(s, {"mh_score": 50, "regime": "NEUTRAL"}) == "BUY"       # loose gate, MH >= 40
    assert E.determine_signal(s, {"mh_score": 0, "regime": "NEUTRAL"}) != "BUY"        # 0 was read as 50
    assert E.determine_signal({**s, "cri": 65}, {"mh_score": 50}) in ("HOLD", "SELL")  # mri None no longer raises


def test_scoring_defaults_to_the_last_trading_day_and_refuses_a_day_with_no_data(temp_db):
    from db.schema import init_db, get_connection
    from scores import engine as E
    init_db()
    c = get_connection()
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "VALUES ('ACME', '2026-10-30', 1, 1, 1, 1, 1, 'EQ')"); c.commit()
    assert E.last_price_date(c, upto=date(2026, 11, 1)) == date(2026, 10, 30)        # Sunday -> Friday's session
    c.close()
    out = E.run_scoring_pipeline(date(2026, 11, 1))
    assert out["status"] == "NO_DATA" and out["rows"] == 0
    c = get_connection()
    assert c.execute("SELECT COUNT(*) FROM ai_scores WHERE date='2026-11-01'").fetchone()[0] == 0
    c.close()


def test_unknown_200_dma_is_null_and_leaves_the_trend_score_out():
    from data.technical import compute_tech_score
    with_dma = compute_tech_score({"rsi_14": 50, "above_200dma": 0})
    unknown = compute_tech_score({"rsi_14": 50, "above_200dma": None})
    assert with_dma != unknown                                            # Trend 20 counted only when it is known


# ── ingestion ──────────────────────────────────────────────────────────────────────────────────

def test_archive_rows_go_in_on_the_adjusted_basis(temp_db):
    """A 2:1 split (price factor 0.5) reconciled on its 2026-08-24 ex-date. The archive's RAW 2026-08-21 bar of
    1,534.80 x 1,378,044 is stored as 767.40 x 2,756,088 -- the basis of the rows already there."""
    from db.schema import init_db, get_connection
    from data import ema_gap
    init_db()
    c = get_connection()
    c.execute("INSERT INTO corporate_actions (symbol, ex_date, subject, kind, factor, status, price_factor, reconciled_at) "
              "VALUES ('TDP', '2026-08-24', 'Split 2:1', 'SPLIT', 2, 'adjusted', 0.5, '2026-10-05T16:46:05')")
    c.commit()
    raw = pd.DataFrame([{"symbol": "TDP", "open": 1520.0, "high": 1540.0, "low": 1510.0, "close": 1534.8,
                         "volume": 1378044, "series": "EQ"}])
    import data.bhavcopy as B
    orig = B.parse_bhavcopy_cm
    B.parse_bhavcopy_cm = lambda df, d: df
    try:
        n = ema_gap.fill_session(c, date(2026, 8, 21), {"TDP"}, None, download=lambda d, s: raw)
    finally:
        B.parse_bhavcopy_cm = orig
    c.commit()
    row = c.execute("SELECT open, close, volume FROM prices_daily WHERE symbol='TDP' AND date='2026-08-21'").fetchone()
    assert n == 1 and (row[0], row[1], row[2]) == (760.0, 767.4, 2756088)
    # the one-off repair finds raw rows inserted after the reconciliation, and only those
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series, source, created_at) "
              "VALUES ('TDP', '2026-08-20', 1500, 1510, 1490, 1507.5, 1106295, 'EQ', 'bhavcopy_archive', '2026-10-28 04:00:00')")
    c.commit()
    found = ema_gap.repair_unadjusted(c, apply=True)
    assert [(f["symbol"], f["rows"]) for f in found] == [("TDP", 1)]       # the raw one only: the fill above is already adjusted
    assert ema_gap.repair_unadjusted(c, apply=True) == []                  # a second run finds nothing
    assert c.execute("SELECT close FROM prices_daily WHERE symbol='TDP' AND date='2026-08-20'").fetchone()[0] == 753.75
    c.close()


def test_fii_dii_write_keeps_the_pcr_already_in_the_row(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    import data.bhavcopy as B
    init_db()
    c = get_connection()
    c.execute("INSERT INTO fii_dii_market (date, pcr) VALUES ('2026-11-03', 0.87)"); c.commit()
    monkeypatch.setattr(B, "download_fii_dii", lambda d, s: {"date": "2026-11-03", "fii_buy_cr": 10, "fii_sell_cr": 4,
                                                             "fii_net_cr": 6, "dii_buy_cr": 3, "dii_sell_cr": 1, "dii_net_cr": 2})
    B.store_fii_dii(c, date(2026, 11, 3), session=object())
    r = c.execute("SELECT fii_net_cr, dii_net_cr, pcr FROM fii_dii_market WHERE date='2026-11-03'").fetchone()
    c.close()
    assert (r[0], r[1], r[2]) == (6, 2, 0.87)                             # INSERT OR REPLACE used to drop the 0.87


# ── alerts and the calendar ────────────────────────────────────────────────────────────────────

def test_trade_of_the_day_alert_survives_missing_scores_and_market_health(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    import alerts.telegram as T
    init_db()
    c = get_connection()
    c.execute("INSERT INTO ai_scores (symbol, date, `signal`, is_tod, tod_score, atip_score) "
              "VALUES ('ACME', '2026-11-03', 'BUY', 1, NULL, NULL)"); c.commit(); c.close()
    sent = []
    monkeypatch.setattr(T, "send_telegram", lambda msg, **k: sent.append((msg, k.get("key"))) or True)
    assert T.send_tod_alert(date(2026, 11, 3)) is True
    msg, key = sent[0]
    assert "MH:—" in msg and "TOD Score:</b> —" in msg and "no close yet" in msg and key == "tod:2026-11-03"


def test_the_holiday_calendar_says_when_it_runs_out():
    from utils.trading_calendar import calendar_gap
    assert calendar_gap(date(2026, 11, 3)) is None
    assert "2027" in calendar_gap(date(2026, 12, 5))                      # December: next year's list is missing
    assert "every weekday reads as a trading day" in calendar_gap(date(2027, 1, 26))


# ── design system ──────────────────────────────────────────────────────────────────────────────

def test_dark_is_the_default_theme_and_light_stays_one_click_away():
    from pathlib import Path
    from dashboard.shell import head_tags
    js = Path("dashboard/static/atip/atip.js").read_text()
    assert "if(t!=='light'&&t!=='auto')t='dark'" in head_tags()           # before first paint: no flash of light
    assert 'return t === "light" || t === "auto" ? t : "dark"' in js
    css = Path("dashboard/static/atip/atip.css").read_text()
    assert "Themes: dark by default" in css


def test_feed_label_carries_the_seconds_stamp(temp_db):
    from db.schema import init_db, get_connection
    from dashboard import ux_kit
    init_db()
    c = get_connection()
    c.execute("CREATE TABLE IF NOT EXISTS live_quotes (symbol TEXT, timestamp TEXT, ltp REAL)")
    c.execute("INSERT INTO live_quotes (symbol, timestamp, ltp) VALUES ('ACME', '2026-11-03 10:15:07', 1)"); c.commit()
    f = ux_kit.feed_label(c, True, now=datetime(2026, 11, 3, 10, 15, 10))
    c.close()
    assert f["updated"] == "10:15:07 IST" and f["state"] == "live"


def test_score_badges_read_cri_low_as_strong_and_carry_the_formula():
    from dashboard import score_glossary as G
    low, high = G.badge("CRI", 20), G.badge("CRI", 80)
    assert 'data-band="strong"' in low and 'data-band="weak"' in high and "CRI↓" in low
    assert "lower is better" in low and "ATR % ×15" in low                # the formula is in the tooltip
    assert G.badge("VPI", None).endswith("VPI —</span>")                  # missing is a dash, never 0
    assert set(G.FORMULAS) == {"ATIP", "VPI", "MRI", "RRI", "ZPI", "CRI", "ACS"}
    assert "CRI below" in G._rules()[0]                                    # the legend states the CRI cap on BUY


def test_every_order_send_path_asks_first_with_qty_price_value_and_stop():
    from pathlib import Path
    js = Path("dashboard/static/atip/atip.js").read_text()
    for k in ("Quantity", "Order value", "Stop-loss", "Send LIVE order", "AX.confirmOrder = function"):
        assert k in js
    assert "AX.confirmOrder(pr,{{action:'send'}})" in Path("dashboard/server.py").read_text()          # "/" Confirm
    assert "AX.confirmOrder(pr0,{action:'send'})" in Path("dashboard/command_page.py").read_text()     # Command Center
    assert 'AX.confirmOrder({ symbol: st.symbol' in Path("dashboard/static/atip/workspace.js").read_text()  # ticket
    retail = Path("dashboard/w39_retail_page.py").read_text()
    assert "confirm('Place every leg" not in retail and "AX.ask({title:'Place every leg of '" in retail


def test_pages_render_the_kpi_strip_the_session_grid_and_the_stamp(api):
    client, _ = api
    from tests.test_autopick import _sessions
    from tests.test_score_audit_2026_11_03 import _eligible
    _eligible(10)
    _sessions(*IDS)
    home = client.get("/home").text
    assert 'class="ax-kpis"' in home and "Sessions filled" in home and "Market health" in home and "Nifty 50" in home
    assert "data-ax-updated" in home and home.count('class="ax-tcard"') == 10
    ses = client.get("/sessions").text
    assert ses.count('class="ax-tcard"') == 10 and 'class="ax-score"' in ses and "#1<span" in ses
    assert "Table view" in ses                                            # the same rows, one click away
    for path in ("/", "/command", "/reports", "/accounting", "/settings"):
        assert client.get(path).status_code == 200, path


def test_ticket_preview_blocks_a_buy_without_a_buy_signal(paper):
    from tests.test_autopick import _conn, _signals
    from dashboard.command_more import preview
    _signals(ACME=(60, "HOLD", 100.0), BETA=(80, "BUY", 50.0))
    paper.prices.update(ACME=100.0, BETA=50.0)
    c = _conn()
    hold = preview(c, "ACME", side="BUY", qty=1, entry=100)
    buy = preview(c, "BETA", side="BUY", qty=1, entry=50)
    c.close()
    chk = lambda p: next(x for x in p["checks"] if x["check"] == "Buy only on a BUY signal")
    assert not chk(hold)["ok"] and chk(hold)["blocking"] and not hold["ok"]
    assert chk(buy)["ok"]


# ── quant / ML ─────────────────────────────────────────────────────────────────────────────────

def test_tie_averaged_ranks_and_neutral_labels():
    from ml.validation import ORDER, _rank, _spearman
    assert list(_rank([3, 1, 1, 2])) == [3.0, 0.5, 0.5, 2.0]              # the two 1s share rank (0+1)/2
    assert ORDER["NEUTRAL"] == 0
    # a score that only drifts with row order has no rank IC against all-tied labels
    assert _spearman(list(range(20)), [1] * 10 + [1] * 10) is None


def test_max_hold_exit_with_an_unknown_hold_is_not_an_exit():
    from strategy_engine.kinds import Evaluator
    k = Evaluator.__new__(Evaluator)
    k.pos = {"max_hold_sessions": 5}
    assert k.max_hold_exit({"held_sessions": None}) is False               # None >= 5 raised TypeError
    assert k.max_hold_exit({"held_sessions": 5}) is True
