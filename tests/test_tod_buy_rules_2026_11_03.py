"""
3 Nov 2026 audit fixes (Critical C1-C4 of the Step 1 UX audit):

  C1  Buy appears only on a BUY signal (score rows, Top-25 lists, Trade of the Day,
      stock view) and the order modal locks its Buy side for any other signal.
  C2  The order modal shows where the order goes: PAPER / SANDBOX / LIVE.
  C3  Trade of the Day: the ATIP Scores page and /home use ONE trade-plan formula
      (scores/predictions.compute_trade_plan), so a HOLD shows no stop/targets on either.
  C4  One kill switch: the shell's top-bar button. Command Center's own button is a
      Resume control, hidden until trading is halted (covered in test_ux_console.py,
      test_ux_command_center.py and test_ux_command_browser.py).
"""

import pytest

TD = "2026-11-02"


@pytest.fixture
def db(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db, get_connection
    init_db()
    monkeypatch.chdir(tmp_path)
    c = get_connection()
    yield c
    c.close()


def _seed(c, sym, signal, close=300.95, atr=7.2656):
    c.execute("INSERT INTO ai_scores (symbol, date, `signal`, atip_score, is_tod) VALUES (?,?,?,?,1)",
              (sym, TD, signal, 58.84))
    c.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, series) "
              "VALUES (?,?,?,?,?,?,1000,'EQ')", (sym, TD, close, close, close, close))
    c.execute("INSERT INTO technical_indicators (symbol, date, atr_14) VALUES (?,?,?)", (sym, TD, atr))
    c.commit()


# ── C3: one trade-plan formula ────────────────────────────────────────────────

def test_hold_trade_of_day_has_no_invented_levels(db):
    """The bug: PETRONET (HOLD) showed stop 290.05 / targets 315.48, 326.38 from close ± k×ATR."""
    import dashboard.server as srv
    _seed(db, "PETRONET", "HOLD")
    tod = srv.get_tod(TD)
    assert tod["symbol"] == "PETRONET"
    assert tod["sl"] is None and tod["t1"] is None and tod["t2"] is None
    assert 290.05 not in (tod["sl"], tod["t1"], tod["t2"])


def test_buy_trade_of_day_matches_the_stored_plan_formula(db):
    import dashboard.server as srv
    from scores.predictions import compute_trade_plan
    _seed(db, "ACME", "BUY", close=1000.0, atr=20.0)
    tod = srv.get_tod(TD)
    plan = compute_trade_plan("BUY", 1000.0, 20.0)
    assert (tod["sl"], tod["t1"], tod["t2"]) == (plan["stop_loss"], plan["target_1"], plan["target_2"])
    assert (tod["sl"], tod["t1"], tod["t2"]) == (970.0, 1030.0, 1060.0)   # 3 % ATR stop, +3 % / +6 %


# ── C1: Buy only on a BUY signal ──────────────────────────────────────────────

@pytest.mark.parametrize("sig", ["HOLD", "WAIT", "SELL", "", None])
def test_no_buy_button_unless_signal_is_buy(sig):
    import dashboard.server as srv
    html = srv.order_btns("ACME", 100, signal=sig)
    assert ",'BUY'," not in html and ">Buy</button>" not in html
    assert 'class="nobuy"' in html and "No buy" in html
    assert ">Sell</button>" in html, "Sell stays available to exit a holding"


def test_buy_button_on_buy_signal_passes_signal_to_modal():
    import dashboard.server as srv
    html = srv.order_btns("ACME", 100, signal="BUY")
    assert "openOrderModal('ACME',100,'BUY','BUY')" in html and ">Buy</button>" in html
    assert "nobuy" not in html


def test_modal_locks_buy_side_for_non_buy_signal(db):
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    html = TestClient(srv.app).get("/").text
    assert "function openOrderModal(sym,cmp,side,signal)" in html
    assert "buyLocked=(mSig!==null&&mSig!=='BUY')" in html
    assert "if(v==='BUY'&&mSig!==null&&mSig!=='BUY')return;" in html          # setSide guard
    assert "Buy is available only on a BUY signal (signal now: " in html     # saveOrderRule guard
    assert 'id="mNoBuy"' in html


def test_stock_view_hides_buy_unless_latest_signal_is_buy():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "stock_view.py").read_text(encoding="utf-8")
    assert "svSig(d)==='BUY'?'<button class=\"b-buy\"" in src
    assert "openOrderModal(d.symbol,(live&&live>0)?live:d.stats.last_close,side,svSig(d))" in src


# ── C2: PAPER / LIVE in the order modal ───────────────────────────────────────

def test_modal_shows_paper_by_default(db):
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    html = TestClient(srv.app).get("/").text
    assert 'class="envbadge paper" id="mEnv">PAPER<' in html
    assert "no order reaches Dhan" in html and "Save rule · PAPER" in html


def test_modal_shows_live_in_red_when_live(db, monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    import orders.environment as E
    monkeypatch.setattr(E, "broker_env", lambda: E.LIVE)
    monkeypatch.setattr(E, "describe", lambda: "LIVE — real orders, real money")
    html = TestClient(srv.app).get("/").text
    assert 'class="envbadge live" id="mEnv">LIVE<' in html
    assert "LIVE — real orders, real money" in html and "Save rule · LIVE" in html
