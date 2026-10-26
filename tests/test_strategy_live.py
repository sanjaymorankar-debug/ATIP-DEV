"""
The wiring between strategy, state and broker.

The interesting tests here are the failure paths: a rejected order must not
leave the position claiming shares it still holds, and a repeated pass must not
place a second order.
"""

import pytest

from strategy import aggressive as A
from strategy import live as L
from strategy import positions as P


@pytest.fixture
def orders(monkeypatch):
    """Capture every order the strategy sends, and control whether it fills."""
    sent = []
    state = {"fill": True, "fill_price": None}

    def fake_sell(symbol, quantity, confirm=False, tag=None, **kw):
        sent.append({"side": "SELL", "symbol": symbol, "qty": quantity, "tag": tag})
        if not state["fill"]:
            return {"status": "FAILED", "error": "simulated rejection"}
        px = state["fill_price"] if state["fill_price"] is not None else 100.0
        return {"status": "PLACED", "order_id": "X",
                "response": {"data": {"averageTradedPrice": px, "filledQty": quantity}}}

    def fake_buy(symbol, quantity, confirm=False, tag=None, **kw):
        sent.append({"side": "BUY", "symbol": symbol, "qty": quantity, "tag": tag})
        if not state["fill"]:
            return {"status": "FAILED", "error": "simulated rejection"}
        px = state["fill_price"] if state["fill_price"] is not None else 100.0
        return {"status": "PLACED", "order_id": "X",
                "response": {"data": {"averageTradedPrice": px, "filledQty": quantity}}}

    monkeypatch.setattr("orders.broker.place_sell_order", fake_sell)
    monkeypatch.setattr("orders.broker.place_buy_order", fake_buy)
    monkeypatch.setattr(L, "momentum_for", lambda s, conn=None: A.Momentum())
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 1.5)
    return {"sent": sent, "state": state}


@pytest.fixture
def pos(temp_db, cfg, orders):
    """A filled 100-share entry at 100.00, managed by the live wiring."""
    r = L.enter("ACME", 100, cfg=cfg)
    assert r["opened"] is True
    return r["position"]


# ── entry ─────────────────────────────────────────────────────────────────

def test_entry_places_a_buy_and_tracks_the_actual_fill(temp_db, cfg, orders):
    orders["state"]["fill_price"] = 101.37       # not the quoted price
    r = L.enter("ACME", 10, cfg=cfg)
    assert orders["sent"][0]["side"] == "BUY"
    assert r["position"]["entry_price"] == 101.37, \
        "levels must be measured from the price actually paid"


def test_entry_that_does_not_fill_creates_no_position(temp_db, cfg, orders):
    orders["state"]["fill"] = False
    r = L.enter("ACME", 10, cfg=cfg)
    assert r["opened"] is False
    assert P.open_positions() == []


def test_entry_order_is_tagged(temp_db, cfg, orders):
    L.enter("ACME", 5, cfg=cfg)
    assert orders["sent"][0]["tag"] == "ENTRY:ACME"


# ── target 1 ──────────────────────────────────────────────────────────────

def test_reaching_target_1_sells_half(pos, cfg, orders):
    L.manage_position(pos, 103.0, cfg)
    sells = [o for o in orders["sent"] if o["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["qty"] == 50
    p = P.get_position(pos["id"])
    assert p["remaining_qty"] == 50
    assert p["status"] == A.ST_RUNNER


def test_below_target_1_does_nothing(pos, cfg, orders):
    L.manage_position(pos, 102.0, cfg)
    assert [o for o in orders["sent"] if o["side"] == "SELL"] == []
    assert P.get_position(pos["id"])["t1_state"] == "PENDING"


def test_repeated_passes_place_only_one_order(pos, cfg, orders):
    """A price feed repeating a tick must not sell twice."""
    for _ in range(5):
        fresh = P.get_position(pos["id"])
        L.manage_position(fresh, 103.0, cfg)
    assert len([o for o in orders["sent"] if o["side"] == "SELL"]) == 1
    assert P.get_position(pos["id"])["remaining_qty"] == 50


def test_rejected_exit_leaves_the_position_intact_and_retryable(pos, cfg, orders):
    """
    The reason reserve/place/apply exists. A rejected order must not book a
    sale, and must not permanently block the exit either.
    """
    orders["state"]["fill"] = False
    L.manage_position(pos, 103.0, cfg)
    p = P.get_position(pos["id"])
    assert p["remaining_qty"] == 100, "nothing was sold, so nothing may be recorded"
    assert p["t1_state"] == "PENDING"

    orders["state"]["fill"] = True
    L.manage_position(P.get_position(pos["id"]), 103.0, cfg)
    p = P.get_position(pos["id"])
    assert p["remaining_qty"] == 50, "the retry must succeed"


def test_partial_exit_records_the_real_fill_not_the_target(pos, cfg, orders):
    orders["state"]["fill_price"] = 104.25      # gapped through the target
    L.manage_position(pos, 104.25, cfg)
    p = P.get_position(pos["id"])
    assert p["t1_price"] == 104.25
    assert p["realized_pnl"] == pytest.approx(50 * 4.25)


# ── the initial stop ──────────────────────────────────────────────────────

def test_initial_stop_exits_the_whole_position(pos, cfg, orders):
    L.manage_position(pos, 96.9, cfg, initial_stop_pct=3.0)
    sells = [o for o in orders["sent"] if o["side"] == "SELL"]
    assert sells[0]["qty"] == 100
    p = P.get_position(pos["id"])
    assert p["status"] == A.ST_CLOSED
    assert p["exit_reason"] == A.INITIAL_STOP_LOSS


def test_stop_takes_precedence_within_one_pass(pos, cfg, orders):
    """Ambiguity resolves against the position, exactly as the backtest scores it."""
    L.manage_position(pos, 96.0, cfg, initial_stop_pct=3.0)
    assert P.get_position(pos["id"])["exit_reason"] == A.INITIAL_STOP_LOSS


# ── the ATR stop (stop_atr_mult) ──────────────────────────────────────────

def _atr_on(cfg, mult=2.0):
    return A.validate_config(dict(cfg, stop_atr_mult=mult))


def test_atr_stop_is_fixed_at_entry_and_replaces_the_fixed_stop(temp_db, cfg, orders, monkeypatch):
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 4.0)
    on = _atr_on(cfg)
    pos = L.enter("ACME", 100, cfg=on)["position"]
    assert pos["initial_stop_pct"] == 8.0                        # 2 x 4 %
    L.manage_position(pos, 95.0, on)                             # -5 %: inside the ATR stop
    assert not [o for o in orders["sent"] if o["side"] == "SELL"]
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 1.0)  # a later ATR does not move it
    L.manage_position(P.get_position(pos["id"]), 92.5, on)
    assert not [o for o in orders["sent"] if o["side"] == "SELL"]
    L.manage_position(P.get_position(pos["id"]), 91.9, on)
    p = P.get_position(pos["id"])
    assert p["status"] == A.ST_CLOSED and p["exit_reason"] == A.INITIAL_STOP_LOSS
    assert [o["qty"] for o in orders["sent"] if o["side"] == "SELL"] == [100]


def test_explicit_stop_pct_still_overrides_the_atr_stop(temp_db, cfg, orders, monkeypatch):
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 4.0)
    on = _atr_on(cfg)
    pos = L.enter("ACME", 100, cfg=on)["position"]
    L.manage_position(pos, 96.9, on, initial_stop_pct=3.0)
    assert P.get_position(pos["id"])["exit_reason"] == A.INITIAL_STOP_LOSS


def test_turning_the_atr_stop_on_leaves_open_positions_on_their_stop(pos, cfg, orders):
    """`pos` opened with the setting off, so it keeps the fixed stop (target_1_pct, 3 %)."""
    assert pos["initial_stop_pct"] is None
    L.manage_position(pos, 96.9, _atr_on(cfg))
    assert P.get_position(pos["id"])["exit_reason"] == A.INITIAL_STOP_LOSS


def test_unknown_atr_keeps_the_fixed_stop(temp_db, cfg, orders, monkeypatch):
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: None)
    on = _atr_on(cfg)
    pos = L.enter("ACME", 100, cfg=on)["position"]
    assert pos["initial_stop_pct"] is None
    L.manage_position(pos, 96.9, on)
    assert P.get_position(pos["id"])["exit_reason"] == A.INITIAL_STOP_LOSS


# ── risk-based sizing (risk_sizing) ───────────────────────────────────────

@pytest.fixture
def sizing(monkeypatch):
    """Rs 10 lakh capital, 1 % risk per trade, at most 20 % in one name; the quote is 100."""
    monkeypatch.setattr("orders.risk.sizing_config", lambda: {
        "capital": 1_000_000, "risk_per_trade_pct": 1.0, "max_position_pct": 20.0})
    monkeypatch.setattr(L, "_last_price", lambda s: 100.0)


def _sizing_on(cfg, **kw):
    return A.validate_config(dict(cfg, risk_sizing=True, **kw))


def test_risk_sized_entry_buys_what_the_stop_allows(temp_db, cfg, orders, sizing, monkeypatch):
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 4.0)
    r = L.enter("ACME", cfg=_sizing_on(cfg, stop_atr_mult=2.0))
    # Rs 10,000 at risk / Rs 8 a share to the 8 % ATR stop = 1,250 shares (under the 2,000 cap)
    assert [o["qty"] for o in orders["sent"] if o["side"] == "BUY"] == [1250]
    assert r["sizing"]["capped_by"] == "risk_per_trade_pct"
    assert r["position"]["initial_stop_pct"] == 8.0


def test_a_tight_stop_is_capped_by_max_position_pct(temp_db, cfg, orders, sizing):
    L.enter("ACME", cfg=_sizing_on(cfg))        # 3 % stop: 3,333 by risk, 2,000 by the cap
    assert [o["qty"] for o in orders["sent"] if o["side"] == "BUY"] == [2000]


def test_risk_sized_entry_keeps_the_stop_it_was_sized_on(temp_db, cfg, orders, sizing):
    """Sized on target_1_pct (3 %); raising the target afterwards must not widen the stop."""
    pos = L.enter("ACME", cfg=_sizing_on(cfg))["position"]
    assert pos["initial_stop_pct"] == 3.0
    later = A.validate_config(dict(cfg, target_1_pct=6.0, target_2_pct=12.0))
    L.manage_position(pos, 96.9, later)
    assert P.get_position(pos["id"])["exit_reason"] == A.INITIAL_STOP_LOSS


def test_entry_stop_pct_is_sized_on_and_stored(temp_db, cfg, orders, sizing):
    r = L.enter("ACME", cfg=_sizing_on(cfg), stop_pct=5.0)
    assert [o["qty"] for o in orders["sent"] if o["side"] == "BUY"] == [2000]   # 10,000 / 5
    assert r["position"]["initial_stop_pct"] == 5.0


def test_an_explicit_quantity_is_not_resized(temp_db, cfg, orders, monkeypatch):
    def no_quote(s):
        raise AssertionError("an explicit quantity must not fetch a quote to size on")
    monkeypatch.setattr(L, "_last_price", no_quote)
    L.enter("ACME", 7, cfg=_sizing_on(cfg))
    assert [o["qty"] for o in orders["sent"] if o["side"] == "BUY"] == [7]


def test_no_fresh_quote_means_no_risk_sized_entry(temp_db, cfg, orders, sizing, monkeypatch):
    monkeypatch.setattr(L, "_last_price", lambda s: None)
    r = L.enter("ACME", cfg=_sizing_on(cfg))
    assert r["opened"] is False and "no fresh quote" in r["reason"]
    assert orders["sent"] == [] and P.open_positions() == []


def test_without_risk_sizing_an_entry_with_no_quantity_buys_one_share(temp_db, cfg, orders):
    r = L.enter("ACME", cfg=cfg)
    assert [o["qty"] for o in orders["sent"] if o["side"] == "BUY"] == [1]
    assert r["position"]["initial_stop_pct"] is None


def test_an_existing_position_table_gains_the_stop_column(temp_db):
    from db.schema import get_connection
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE strategy_position (id TEXT PRIMARY KEY, entry_rule_id TEXT, "
                     "symbol TEXT NOT NULL, status TEXT DEFAULT 'OPEN')")   # indexed columns only
        conn.execute("INSERT INTO strategy_position (id, symbol) VALUES ('old', 'ACME')")
        conn.commit()
        P.ensure_tables(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_position)").fetchall()}
        assert "initial_stop_pct" in cols
        row = conn.execute("SELECT symbol, initial_stop_pct FROM strategy_position").fetchone()
        assert tuple(row) == ("ACME", None)
    finally:
        conn.close()


# ── the runner, checkpoint and trail ──────────────────────────────────────

def test_checkpoint_runs_at_target_2_and_holds_on_strong_momentum(pos, cfg, orders, monkeypatch):
    monkeypatch.setattr(L, "momentum_for", lambda s, conn=None: A.Momentum(
        zpi=85, msi=80, cri=10, mri=15, rsi_14=68, macd_hist=0.7, volume_ratio=2.0))
    L.manage_position(pos, 103.0, cfg)
    L.manage_position(P.get_position(pos["id"]), 106.5, cfg)
    p = P.get_position(pos["id"])
    assert p["t2_verdict"] == A.HOLD_RUNNER
    assert p["remaining_qty"] == 50, "+6% must not force an exit"
    assert len([o for o in orders["sent"] if o["side"] == "SELL"]) == 1


def test_weak_momentum_at_the_checkpoint_banks_the_runner(pos, cfg, orders, monkeypatch):
    monkeypatch.setattr(L, "momentum_for", lambda s, conn=None: A.Momentum(
        zpi=15, msi=20, cri=85, mri=80, rsi_14=32, macd_hist=-0.6, volume_ratio=0.4))
    L.manage_position(pos, 103.0, cfg)
    L.manage_position(P.get_position(pos["id"]), 106.5, cfg)
    p = P.get_position(pos["id"])
    assert p["t2_verdict"] == A.EXIT_RUNNER
    assert p["status"] == A.ST_CLOSED
    assert p["exit_reason"] == A.TARGET_2_MOMENTUM_EXIT


def test_trailing_stop_closes_the_runner(pos, cfg, orders):
    L.manage_position(pos, 103.0, cfg)
    L.manage_position(P.get_position(pos["id"]), 130.0, cfg)
    stop = P.get_position(pos["id"])["trail_stop"]
    assert stop is not None
    L.manage_position(P.get_position(pos["id"]), stop - 1, cfg)
    p = P.get_position(pos["id"])
    assert p["status"] == A.ST_CLOSED
    assert p["exit_reason"] == A.TRAILING_STOP


def test_trail_only_ratchets_upward_through_the_wiring(pos, cfg, orders):
    L.manage_position(pos, 103.0, cfg)
    L.manage_position(P.get_position(pos["id"]), 130.0, cfg)
    peak = P.get_position(pos["id"])["trail_stop"]
    L.manage_position(P.get_position(pos["id"]), 125.0, cfg)
    assert P.get_position(pos["id"])["trail_stop"] == peak


def test_exit_orders_are_tagged_with_position_and_event(pos, cfg, orders):
    L.manage_position(pos, 103.0, cfg)
    tag = [o for o in orders["sent"] if o["side"] == "SELL"][0]["tag"]
    assert tag.endswith(":T1")
    assert tag.startswith(pos["id"][:8])


# ── run_once ──────────────────────────────────────────────────────────────

def test_run_once_manages_every_open_position(temp_db, cfg, orders):
    a = L.enter("AAA", 100, cfg=cfg)["position"]
    b = L.enter("BBB", 100, cfg=cfg)["position"]
    r = L.run_once(cfg, prices={"AAA": 103.0, "BBB": 101.0})
    assert r["positions"] == 2
    assert P.get_position(a["id"])["remaining_qty"] == 50
    assert P.get_position(b["id"])["remaining_qty"] == 100


def test_run_once_is_idle_with_no_positions(temp_db, cfg):
    assert L.run_once(cfg, prices={})["status"] == "IDLE"


def test_run_once_skips_symbols_with_no_price(temp_db, cfg, orders):
    L.enter("AAA", 100, cfg=cfg)
    r = L.run_once(cfg, prices={})
    assert r["events"] == []


def test_closed_positions_are_not_managed(pos, cfg, orders):
    P.close_position(pos["id"], 99.0, A.MANUAL_EXIT)
    before = len(orders["sent"])
    L.manage_position(P.get_position(pos["id"]), 103.0, cfg)
    assert len(orders["sent"]) == before


# ── momentum sourcing ─────────────────────────────────────────────────────

def test_momentum_reads_existing_scores_without_inventing_them(temp_db):
    """Absent indicators stay None; a zero would read as maximum weakness."""
    from db.schema import get_connection
    conn = get_connection()
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_scores
                    (symbol TEXT, date TEXT, zpi REAL, msi REAL, mri REAL,
                     cri REAL, mh_score REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS technical_indicators
                    (symbol TEXT, date TEXT, rsi_14 REAL, macd_hist REAL,
                     volume_ratio REAL)""")
    conn.execute("INSERT INTO ai_scores VALUES ('ACME','2026-09-07',71.0,NULL,NULL,22.0,55.0)")
    conn.commit()
    m = L.momentum_for("ACME", conn=conn)
    assert m.zpi == 71.0
    assert m.cri == 22.0
    assert m.msi is None
    assert m.rsi_14 is None
    conn.close()


# ── decision price and fill price must agree ──────────────────────────────

def test_exit_fills_at_the_price_the_decision_was_made_on(temp_db, cfg, monkeypatch):
    """
    Regression. The broker used to fetch its own quote, so a decision made at
    one price filled at another. End to end, a trailing stop that triggered at
    1398.22 filled at 1289.85 and turned a +12% runner into a booked loss.
    """
    import orders.paper as paper
    from db.schema import get_connection

    # The "live" market sits far from the price the strategy will act on.
    monkeypatch.setattr(paper.PaperBroker, "_ltp", lambda self, s: 50.0)
    monkeypatch.setattr(L, "atr_for", lambda s, conn=None: 1.5)
    monkeypatch.setattr(L, "momentum_for", lambda s, conn=None: A.Momentum())

    b = paper.PaperBroker(conn=get_connection(),
                          overrides={"paper_slippage_bps": 0.0, "paper_brokerage_pct": 0.0})
    b.reset(1000000.0)
    # orders.broker does `from orders.environment import get_execution_client`,
    # so the name is already bound there — patching the environment module would
    # have no effect on the code actually under test.
    monkeypatch.setattr("orders.broker.get_execution_client",
                        lambda quote_source=None: (b, "PAPER"))
    # This test drives the genuine _place_order path, which resolves the ticker
    # through Dhan's security master — a 200k-row CSV that is not in the repo.
    # Relying on it made this pass locally and fail in CI. Stub the lookup: this
    # test is about the fill price matching the decision price, not about symbol
    # resolution, and a test should not need a downloaded data file to run.
    monkeypatch.setattr("orders.broker.get_security_id",
                        lambda symbol: {"security_id": "1", "exchange": "NSE_EQ"})
    sym = "ACME"

    r = b.place_order(transaction_type="BUY", quantity=100, symbol=sym,
                      reference_price=100.0)
    assert r["data"]["averageTradedPrice"] == 100.0, "entry must honour the reference"

    pos = P.open_position(sym, 100.0, 100, atr_pct=1.5, cfg=cfg)
    L.manage_position(pos, 103.0, cfg)
    p = P.get_position(pos["id"])
    assert p["t1_price"] == 103.0, \
        f"filled at {p['t1_price']} but the decision was made at 103.0"
    assert p["realized_pnl"] > 0, "a +3% partial must not book a loss"


def test_reference_price_is_never_sent_to_a_real_broker(temp_db, cfg, monkeypatch):
    """
    Only the simulator may be told what price to fill at. Handing this to a real
    broker would be meaningless at best and a rejected order at worst.
    """
    seen = {}

    class FakeRealClient:
        accepts_symbol = False        # a real dhanhq client advertises nothing

        def get_fund_limits(self):
            return {"status": "success", "data": {"availabelBalance": 1e9}}

        def place_order(self, **kw):
            seen.update(kw)
            return {"status": "success", "data": {"orderId": "R1",
                                                  "averageTradedPrice": 103.0,
                                                  "filledQty": 50}}

    monkeypatch.setattr("orders.broker.get_execution_client",
                        lambda quote_source=None: (FakeRealClient(), "LIVE"))
    from orders.broker import place_sell_order
    place_sell_order("ACME", 50, confirm=True, tag="t", reference_price=103.0)
    assert "reference_price" not in seen
    assert "symbol" not in seen


# ── W39b (EX-17): the enable flag and the crash-risk exit ───────────────────

def test_disabled_flag_refuses_new_entries_but_keeps_managing_open_positions(pos, cfg, orders):
    off = dict(cfg, aggressive_enabled=False)
    r = L.enter("BETA", 10, cfg=off)
    assert r["opened"] is False and "no new entries" in r["reason"]
    assert not any(o["symbol"] == "BETA" for o in orders["sent"])
    L.manage_position(pos, 96.9, off, initial_stop_pct=3.0)           # the open one still exits
    assert P.get_position(pos["id"])["exit_reason"] == A.INITIAL_STOP_LOSS


def test_cri_spike_exits_the_whole_position_only_when_configured(pos, cfg, orders, monkeypatch):
    monkeypatch.setattr(L, "momentum_for", lambda s, conn=None: A.Momentum(cri=85.0))
    L.manage_position(pos, 100.5, cfg)                                  # threshold off: nothing
    assert not [o for o in orders["sent"] if o["side"] == "SELL"]
    on = A.validate_config(dict(cfg, cri_exit_threshold=80))
    L.manage_position(pos, 100.5, on)
    sells = [o for o in orders["sent"] if o["side"] == "SELL"]
    assert len(sells) == 1 and sells[0]["qty"] == 100
    p = P.get_position(pos["id"])
    assert p["status"] == A.ST_CLOSED and p["exit_reason"] == A.MARKET_RISK_EXIT
    L.manage_position(P.get_position(pos["id"]), 100.5, on)             # idempotent
    assert len([o for o in orders["sent"] if o["side"] == "SELL"]) == 1


def test_cri_below_threshold_or_unknown_does_not_exit(pos, cfg, orders, monkeypatch):
    on = A.validate_config(dict(cfg, cri_exit_threshold=80))
    for m in (A.Momentum(cri=79.9), A.Momentum()):
        monkeypatch.setattr(L, "momentum_for", lambda s, conn=None, m=m: m)
        L.manage_position(pos, 100.5, on)
    assert not [o for o in orders["sent"] if o["side"] == "SELL"]


def test_cri_threshold_is_validated():
    for bad in (0, 101, "80", True):
        with pytest.raises(A.ConfigError, match="cri_exit_threshold"):
            A.validate_config(dict(A.DEFAULTS, cri_exit_threshold=bad))
