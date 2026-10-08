"""
The central risk engine (W4): PositionIntent -> RiskDecision.

A rejected, blocked or unreviewed intent never reaches the order manager: an
order can only be created from an APPROVED risk decision (order_manager.py).

Checks, in order. Every check is recorded (PASS / WARN / FAIL / SKIP) with its
value and limit, so a decision can be read back without re-deriving it.

  GATES (-> BLOCKED)
    kill_switch          orders/risk.py halted() (W1)
    strategy_enabled     the strategy is PAPER / READY / ACTIVE, and the intent is
                         for its current version (else REJECTED: superseded)
    ml_model_active      (W5) when the decision used ML features (ml_score ...): the
                         prediction behind it must exist, come from the configured
                         model, and that model version must STILL be ACTIVE -- a
                         paused / retired model's signals are BLOCKED. Recorded with
                         model, version, score and confidence (ML provenance)
    tenant_profile       (W7) the strategy's tenant trading profile (enterprise/profiles.py):
                         trading disabled or the strategy not in allowed_strategies ->
                         BLOCKED; max_order_value caps a BUY's quantity. Profiles only
                         ever tighten W4 -- they cannot loosen a limit or open live
    live_gate            a LIVE-book intent needs execution.mode LIVE and
                         live_trading_enabled; both default off
  VALIDITY (-> REJECTED)
    broker_health        (W29, RK-17) a BUY is REJECTED while the latest broker health check
                         (execution/broker_health.py, <= broker_health_max_age_minutes old)
                         is DOWN or STALE; SKIP with no recent check; exits never blocked
    intent_valid         side, symbol, action, quantity sane
    intent_fresh         as_of within max_intent_age_days
    market_data          a reference price (the decision close, else the latest close)
    max_price_band_pct   (W39, RK-21) the price the order will carry -- an execution.order_type
                         LIMIT / SL is created at the reference price -- against the freshest
                         live LTP, else the last close (orders/risk.py price_band); REJECTED
                         beyond the band, BUY or SELL. MARKET, or no reference price: SKIP.
                         The limit is w4 max_price_band_pct once execution/config.py declares
                         it, else config.json risk_limits.max_price_band_pct (default 20)
    circuit_limit        (W39, RK-21) that price against today's NSE upper / lower circuit
                         (live_quotes, from the Dhan REST quote); REJECTED outside it. MARKET,
                         or no circuit stored today: SKIP
  SELL / EXIT / REDUCE   reduce risk: only the gates and a held position are
                         required. EXIT sells everything held; REDUCE the
                         intent's quantity (else half), capped at what is held.
  BUY / ADD (-> resized, or REJECTED when nothing fits)
    sizing               intent quantity, else orders/risk.py size_position() at
                         per_trade_loss_pct with the intent's stop (default_stop_pct)
    max_order_quantity, max_order_value_pct, max_position_pct,
    max_portfolio_exposure_pct, max_sector_exposure_pct,
    max_strategy_exposure_pct, max_capital_allocation_pct (cash), per_trade_loss_pct
                         each caps the quantity; a binding cap is a WARN (resized).
                         (W39, RK-21) max_portfolio_exposure_pct counts the open paper
                         futures notional (futures_paper.gross_notional) with the positions,
                         and caps a SHORT leg's lots the same way
    max_open_positions, max_daily_trades
    daily_loss_limit_pct, portfolio_drawdown_limit_pct (portfolio/pnl.py risk_state)
  W8  market_data_fresh  a BUY is REJECTED when the symbol's last daily bar is more
                         than execution.max_market_data_age_sessions (2) sessions old
    strategy_drawdown_limit_pct (the strategy's fills P&L)
                         pass/fail; a limit that cannot be measured FAILS (fail closed)
  W25 (each off until set; portfolio/limits.py measures them)
    max_adv_participation_pct   caps the quantity at a share of 20-session ADV (RK-10)
    max_symbol_volatility_pct   the symbol's annualised volatility (RK-09)
    max_avg_correlation         value-weighted correlation with the held book (RK-11)
    max_portfolio_var_pct       post-trade 1-day 95% historical VaR, % of equity (RK-12)
                                pass/fail, fail closed like the rest
    w1_pretrade          orders/risk.py pretrade_check() -- the W1 limits in
                         config.json risk_limits, unchanged
  REVIEW               require_manual_review, or any LIVE intent -> REVIEW_REQUIRED

evaluate() stores the decision (risk_decision) and moves the intent's
authorization_status. Evaluation is once per intent.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta

from execution import positions as P
from execution.config import LIVE, PAPER, execution_settings, limit_values, live_gate
from execution.errors import DuplicateDecisionError, InvalidIntentError
from execution.models import (APPROVED, BLOCKED, FAIL, INTENT_STATUS_FOR, PASS, REJECTED, REVIEW_REQUIRED, SKIP,
                              WARN, RiskCheck, RiskDecision)

log = logging.getLogger("atip.execution")

SELL_ACTIONS = ("EXIT", "REDUCE", "SELL")


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def load_intent(conn, intent_id: str) -> dict:
    r = conn.execute("SELECT * FROM strategy_position_intent WHERE intent_id=?", (intent_id,)).fetchone()
    if not r:
        raise InvalidIntentError(f"no position intent {intent_id}")
    it = dict(r)
    if not it.get("book"):                       # intents stored before W4: the book is on the run
        b = conn.execute("SELECT r.book FROM strategy_decision d JOIN strategy_decision_run r ON r.run_id=d.run_id "
                         "WHERE d.decision_id=?", (it["decision_id"],)).fetchone()
        it["book"] = b[0] if b else PAPER
    return it


def _orders_today(conn) -> int:
    """Trades created today: an algo parent (W34, EX-11) counts once, not once per child slice."""
    d = str(date.today())
    return conn.execute("SELECT (SELECT COUNT(*) FROM oms_order WHERE DATE(created_at)=? AND algo_parent_id IS NULL) + "
                        "(SELECT COUNT(*) FROM exec_algo_parent WHERE DATE(created_at)=?)", (d, d)).fetchone()[0]


def evaluate(conn, intent_id: str, actor: str = "risk_engine", store: bool = True) -> RiskDecision:
    it = load_intent(conn, intent_id)
    if store and it["authorization_status"] != "NOT_AUTHORIZED":
        raise DuplicateDecisionError(f"intent {intent_id} is already {it['authorization_status']} "
                                     f"(risk decision {it.get('risk_decision_id')})")
    settings = execution_settings()
    lim = limit_values(conn)
    checks = []

    def add(check, status, message, value=None, limit=None):
        checks.append(RiskCheck(check, status, message,
                                None if value is None else round(float(value), 4),
                                None if limit is None else float(limit)))

    rd = RiskDecision(intent_id=intent_id, symbol=it["symbol"], side=it["side"], risk_status=APPROVED,
                      requested_quantity=it.get("quantity"), approved_quantity=0, decision_id=it["decision_id"],
                      strategy_id=it["strategy_id"], strategy_version=it["version"], action=it.get("action"),
                      book=it["book"], mode=settings["mode"], limits=lim)

    def finish(status, reason=None, qty=0):
        rd.risk_status, rd.rejection_reason, rd.approved_quantity = status, reason, int(qty or 0)
        rd.risk_checks = [c.__dict__ for c in checks]
        if store:
            _store(conn, rd, it, actor)
        return rd

    # -- gates ------------------------------------------------------------------
    try:
        from orders.risk import halted
        h, why = halted()
    except Exception as e:
        h, why = True, f"kill switch state unreadable ({e})"
    if h:
        add("kill_switch", FAIL, f"trading halted: {why}")
        return finish(BLOCKED, f"kill switch: {why}")
    add("kill_switch", PASS, "trading not halted")

    s = conn.execute("SELECT status, current_version FROM strategy WHERE strategy_id=?",
                     (it["strategy_id"],)).fetchone()
    from strategy_engine.lifecycle import DECISION_STATES
    if not s:
        add("strategy_enabled", FAIL, "strategy not in the registry")
        return finish(REJECTED, f"unknown strategy {it['strategy_id']}")
    if s[0] not in DECISION_STATES:
        add("strategy_enabled", FAIL, f"strategy is {s[0]}; only {DECISION_STATES} may trade")
        return finish(BLOCKED, f"strategy {it['strategy_id']} is {s[0]}")
    if s[1] != it["version"]:
        add("strategy_enabled", FAIL, f"intent is for version {it['version']}, current is {s[1]}")
        return finish(REJECTED, f"superseded strategy version {it['version']} (current {s[1]})")
    add("strategy_enabled", PASS, f"{s[0]}, version {s[1]}")

    from execution import tenant_books as TB
    tenant = TB.tenant_of_strategy(conn, it["strategy_id"])
    own_book = TB.is_default(tenant)          # W9: the owner's W1 paper book, else the tenant's own book
    if not own_book and (it["book"] == LIVE or settings["mode"] == LIVE):
        add("tenant_book", FAIL, f"tenant {tenant}: LIVE execution is owner-only")
        return finish(BLOCKED, f"tenant {tenant}: only PAPER execution exists for tenants")
    add("tenant_book", PASS if not own_book else SKIP,
        f"tenant {tenant} paper book" if not own_book else "owner's paper book")

    tp = _tenant_profile(conn, it)
    if tp is not None:
        if not tp.get("trading_enabled", True):
            add("tenant_profile", FAIL, f"trading disabled for tenant {tp['_tenant']}")
            return finish(BLOCKED, f"tenant {tp['_tenant']} trading disabled")
        allowed = tp.get("allowed_strategies")
        if allowed is not None and it["strategy_id"] not in allowed:
            add("tenant_profile", FAIL, f"strategy not in tenant {tp['_tenant']} allowed_strategies")
            return finish(BLOCKED, f"strategy {it['strategy_id']} not allowed for tenant {tp['_tenant']}")
        add("tenant_profile", PASS, f"tenant {tp['_tenant']} profile allows trading")
    else:
        add("tenant_profile", SKIP, "no tenant profile stored")

    ml = _ml_provenance(conn, it)
    if ml is not None:
        if not ml["ok"]:
            add("ml_model_active", FAIL, ml["message"])
            return finish(BLOCKED, f"ML: {ml['message']}")
        add("ml_model_active", PASS, ml["message"], ml.get("score"))
    else:
        add("ml_model_active", SKIP, "decision used no ML features")

    if it["book"] == LIVE or settings["mode"] == LIVE:
        ok, why = live_gate()
        if not ok:
            add("live_gate", FAIL, f"LIVE execution not allowed: {why}")
            return finish(BLOCKED, f"live trading disabled ({why})")
        add("live_gate", PASS, why)
    else:
        add("live_gate", SKIP, "PAPER book in PAPER mode")
    if it["book"] not in settings["execute_books"]:
        add("book", FAIL, f"book {it['book']} not in execution.execute_books {settings['execute_books']}")
        return finish(BLOCKED, f"book {it['book']} is not executed")

    # -- validity -------------------------------------------------------------
    if it["side"] not in ("BUY", "SELL") or not it["symbol"]:
        add("intent_valid", FAIL, f"side {it['side']!r} / symbol {it['symbol']!r}")
        return finish(REJECTED, "invalid intent")
    if it.get("quantity") is not None and int(it["quantity"]) < 0:
        add("intent_valid", FAIL, "negative quantity")
        return finish(REJECTED, "invalid intent quantity")
    add("intent_valid", PASS, f"{it['side']} {it.get('action')} {it['symbol']}")
    age = (date.today() - _d(it["as_of"])).days
    max_age = settings["max_intent_age_days"]
    if max_age is not None and age > max_age:
        add("intent_fresh", FAIL, f"decided {age} days ago", age, max_age)
        return finish(REJECTED, f"stale intent ({age} days old > {max_age})")
    add("intent_fresh", PASS, f"{age} day(s) old", age, max_age)
    ref = it.get("entry_reference")
    src = "decision close"
    if not ref:
        ref, _d_ = P.latest_close(conn, it["symbol"])
        src = f"latest close {_d_}"
    if not ref:
        add("market_data", FAIL, "no price for the symbol")
        return finish(REJECTED, f"no market data for {it['symbol']}")
    rd.reference_price = float(ref)
    add("market_data", PASS, f"reference {ref} ({src})", ref)
    # W8: never size a BUY off stale data (a feed outage must not look like a price)
    max_behind = settings.get("max_market_data_age_sessions")
    if it["side"] == "BUY" and max_behind is not None:
        from ops.data_health import symbol_bar_age
        behind = symbol_bar_age(conn, it["symbol"])
        if behind is None or behind > max_behind:
            add("market_data_fresh", FAIL, f"last daily bar {behind} session(s) behind", behind, max_behind)
            return finish(REJECTED, f"stale market data for {it['symbol']} ({behind} sessions behind)")
        add("market_data_fresh", PASS, f"last daily bar {behind} session(s) behind", behind, max_behind)
    # W29 (RK-17): broker / operational risk -- no new BUY while the latest broker health
    # check (execution/broker_health.py) says DOWN or STALE. Exits are never blocked here.
    try:
        from execution.broker_health import gate as _bh_gate
        bst, bmsg = _bh_gate(conn, it["side"])
    except Exception as e:
        bst, bmsg = SKIP, f"broker health unreadable ({e})"
    if bst == FAIL:
        add("broker_health", FAIL, bmsg)
        return finish(REJECTED, f"broker / data health: {bmsg}")
    add("broker_health", PASS if bst == "PASS" else SKIP, bmsg)

    # -- W30 (QR-05 / QR-06): short legs through the paper stock-futures book --------
    if it.get("action") in ("SHORT", "COVER"):
        return _futures_leg(conn, it, rd, settings, add, finish)

    # -- W39 (RK-21): the order's price against the market, before it is sized or sent
    blocked = _price_band(conn, it, rd, settings, lim, add)
    if blocked:
        return finish(REJECTED, blocked)

    # -- reducing risk ----------------------------------------------------------
    if it["side"] == "SELL":
        held = P.held_quantity(conn, it["symbol"]) if own_book else TB.held_quantity(conn, tenant, it["symbol"])
        if held <= 0:
            add("position_held", FAIL, "nothing held in the paper book", 0)
            return finish(REJECTED, f"no {it['symbol']} position to sell")
        if it.get("action") == "REDUCE":
            qty = min(held, int(it["quantity"]) if it.get("quantity") else max(1, held // 2))
        else:
            qty = held
        add("position_held", PASS, f"{held} held; selling {qty}", held)
        rd.est_value = round(qty * rd.reference_price, 2)
        return _review(finish, settings, it, add, qty)

    # -- adding risk ------------------------------------------------------------
    try:
        bk = P.book(conn) if own_book else TB.book(conn, tenant)
    except Exception as e:
        bk = {"equity": None, "cash": None, "positions": [], "error": str(e)}
    equity, cash = bk.get("equity"), bk.get("cash")
    rd.equity = equity
    if not equity or equity <= 0:
        add("equity", FAIL, f"paper equity unknown ({bk.get('error') or 'no account'})")
        return finish(REJECTED, "equity cannot be measured")
    add("equity", PASS, f"paper equity {equity:,.2f}, cash {cash:,.2f}", equity)
    px = rd.reference_price
    stop = it.get("stop_price")
    if not stop or stop >= px:
        stop = px * (1 - (lim.get("default_stop_pct") or 5.0) / 100)
        add("stop", WARN, f"no usable stop on the intent; assumed {lim.get('default_stop_pct')}% below entry", stop)
    else:
        add("stop", PASS, f"stop {stop}", stop)

    requested = it.get("quantity")
    if not requested:
        from orders.risk import size_position
        sz = size_position(px, stop, capital=equity, risk_per_trade_pct=lim.get("per_trade_loss_pct") or 1.0,
                           max_position_pct=min(x for x in (it.get("target_position_pct"), lim.get("max_position_pct"),
                                                             100.0) if x))
        requested = int(sz.get("quantity") or 0)
        add("sizing", PASS if requested else FAIL, f"sized by W1 size_position: {sz.get('reason') or requested}",
            requested)
    rd.requested_quantity = int(requested)
    if requested <= 0:
        return finish(REJECTED, "position sizing gave 0 shares")

    positions = {p["symbol"]: p for p in bk["positions"]}
    held_value = (positions.get(it["symbol"]) or {}).get("value") or 0.0
    total_value = sum((p.get("value") or 0.0) for p in bk["positions"])
    caps = {}

    def cap(name, limit, room_value=None, qty=None, what=""):
        if limit is None:
            add(name, SKIP, "limit disabled"); return
        q = qty if qty is not None else math.floor(max(0.0, room_value) / px)
        caps[name] = (q, f"{what} (limit {limit})")

    cap("max_order_quantity", lim["max_order_quantity"], qty=lim["max_order_quantity"], what="shares per order")
    try:                                                        # W32 (ENT-03): profile max_capital
        from enterprise.w32 import capital_usage
        cu = capital_usage(conn, it["strategy_id"])
    except Exception:
        cu = None
    if cu is not None:
        used = [f"tenant {cu['tenant']} Rs {cu['tenant_used']:,.0f}/{cu['tenant_max']:,.0f}"] if cu["tenant_max"] is not None else []
        used += [f"user {cu['user_id']} Rs {cu['user_used']:,.0f}/{cu['user_max']:,.0f}"] if cu["user_max"] is not None else []
        cap("profile_max_capital", cu["tenant_max"] if cu["tenant_max"] is not None else cu["user_max"], cu["room"],
            what="deployed " + "; ".join(used))
    if tp is not None and tp.get("max_order_value"):
        cap("tenant_max_order_value", tp["max_order_value"], tp["max_order_value"],
            what=f"tenant {tp['_tenant']} max order value Rs")
    if lim["max_order_value_pct"] is not None:
        cap("max_order_value_pct", lim["max_order_value_pct"], equity * lim["max_order_value_pct"] / 100,
            what="order value % of equity")
    if lim["max_position_pct"] is not None:
        cap("max_position_pct", lim["max_position_pct"], equity * lim["max_position_pct"] / 100 - held_value,
            what="position % of equity")
    if lim["max_portfolio_exposure_pct"] is not None:
        fut = _futures_notional(conn) if own_book else 0.0      # W30 futures are owner-book only
        if fut is None:
            add("max_portfolio_exposure_pct", FAIL, "paper futures notional cannot be measured")
            return finish(REJECTED, "gross exposure cannot be measured (fail closed)")
        cap("max_portfolio_exposure_pct", lim["max_portfolio_exposure_pct"],
            equity * lim["max_portfolio_exposure_pct"] / 100 - total_value - fut,
            what="portfolio exposure % of equity" + (f", incl. futures notional {fut:,.0f}" if fut else ""))
    if lim["max_sector_exposure_pct"] is not None:
        sec = P.sectors()
        if not sec:
            add("max_sector_exposure_pct", FAIL, "sector map unavailable — cannot measure sector exposure")
            return finish(REJECTED, "sector exposure cannot be measured")
        mine = sec.get(it["symbol"], "UNKNOWN")
        sv = sum((p.get("value") or 0.0) for p in bk["positions"] if sec.get(p["symbol"], "UNKNOWN") == mine)
        cap("max_sector_exposure_pct", lim["max_sector_exposure_pct"],
            equity * lim["max_sector_exposure_pct"] / 100 - sv, what=f"sector {mine} % of equity")
    strat = [p for p in P.strategy_positions(conn, it["strategy_id"])]
    strat_value = sum((p["value"] or 0.0) for p in strat)
    if lim["max_strategy_exposure_pct"] is not None:
        cap("max_strategy_exposure_pct", lim["max_strategy_exposure_pct"],
            equity * lim["max_strategy_exposure_pct"] / 100 - strat_value, what="strategy exposure % of equity")
    if lim["max_capital_allocation_pct"] is not None:
        cap("max_capital_allocation_pct", lim["max_capital_allocation_pct"],
            (cash or 0.0) * lim["max_capital_allocation_pct"] / 100 / 1.002, what="% of available cash")
    if lim["per_trade_loss_pct"] is not None:
        cap("per_trade_loss_pct", lim["per_trade_loss_pct"],
            qty=math.floor(equity * lim["per_trade_loss_pct"] / 100 / (px - stop)), what="loss at stop % of equity")
    if lim.get("max_adv_participation_pct") is not None:           # W25 RK-10
        from portfolio.limits import average_daily_volume
        adv = average_daily_volume(conn, it["symbol"])
        if adv is None:
            add("max_adv_participation_pct", FAIL, "average daily volume cannot be measured")
            return finish(REJECTED, "liquidity cannot be measured (fail closed)")
        cap("max_adv_participation_pct", lim["max_adv_participation_pct"],
            qty=math.floor(adv * lim["max_adv_participation_pct"] / 100),
            what=f"% of 20-session ADV {adv:,.0f} shares")

    qty = rd.requested_quantity
    binding = None
    for name, (q, what) in caps.items():
        if q < qty:
            binding = binding or name
        if q <= 0:
            add(name, FAIL, f"no room: {what}", q)
        elif q < rd.requested_quantity:
            add(name, WARN, f"caps the order at {q} shares: {what}", q)
        else:
            add(name, PASS, f"room for {q} shares: {what}", q)
        qty = min(qty, q)
    if qty <= 0:
        return finish(REJECTED, f"no room under {binding}")

    # pass/fail limits
    n_pos = len([p for p in bk["positions"] if (p.get("qty") or 0) > 0])
    if lim["max_open_positions"] is not None and it["symbol"] not in positions:
        if n_pos + 1 > lim["max_open_positions"]:
            add("max_open_positions", FAIL, f"{n_pos} open", n_pos + 1, lim["max_open_positions"])
            return finish(REJECTED, f"max_open_positions {lim['max_open_positions']} reached")
        add("max_open_positions", PASS, f"{n_pos} open", n_pos + 1, lim["max_open_positions"])
    if lim["max_daily_trades"] is not None:
        n = _orders_today(conn) if own_book else TB.orders_today(conn, tenant)
        if n + 1 > lim["max_daily_trades"]:
            add("max_daily_trades", FAIL, f"{n} orders today", n + 1, lim["max_daily_trades"])
            return finish(REJECTED, f"max_daily_trades {lim['max_daily_trades']} reached")
        add("max_daily_trades", PASS, f"{n} orders today", n + 1, lim["max_daily_trades"])
    if lim["daily_loss_limit_pct"] is not None or lim["portfolio_drawdown_limit_pct"] is not None:
        try:
            if own_book:
                from portfolio.pnl import risk_state
                st = risk_state(conn, PAPER)
            else:
                st = TB.risk_state(conn, tenant)
        except Exception as e:
            st = {"day_pnl": None, "drawdown_pct": None, "error": str(e)}
        if lim["daily_loss_limit_pct"] is not None:
            if st.get("day_pnl") is None:
                add("daily_loss_limit_pct", FAIL, "today's P&L cannot be measured (no earlier pnl_daily row)")
                return finish(REJECTED, "daily loss cannot be measured (fail closed)")
            loss_pct = max(0.0, -st["day_pnl"]) / equity * 100
            if loss_pct > lim["daily_loss_limit_pct"]:
                add("daily_loss_limit_pct", FAIL, f"lost {loss_pct:.2f}% today", loss_pct, lim["daily_loss_limit_pct"])
                return finish(REJECTED, f"daily loss {loss_pct:.2f}% > {lim['daily_loss_limit_pct']}%")
            add("daily_loss_limit_pct", PASS, f"today {st['day_pnl']:+,.2f}", loss_pct, lim["daily_loss_limit_pct"])
        if lim["portfolio_drawdown_limit_pct"] is not None:
            if st.get("drawdown_pct") is None:
                add("portfolio_drawdown_limit_pct", FAIL, "drawdown cannot be measured")
                return finish(REJECTED, "drawdown cannot be measured (fail closed)")
            if st["drawdown_pct"] > lim["portfolio_drawdown_limit_pct"]:
                add("portfolio_drawdown_limit_pct", FAIL, "drawdown from peak", st["drawdown_pct"],
                    lim["portfolio_drawdown_limit_pct"])
                return finish(REJECTED, f"portfolio drawdown {st['drawdown_pct']}% > "
                                        f"{lim['portfolio_drawdown_limit_pct']}%")
            add("portfolio_drawdown_limit_pct", PASS, "drawdown from peak", st["drawdown_pct"],
                lim["portfolio_drawdown_limit_pct"])
    if lim["strategy_drawdown_limit_pct"] is not None:
        pnl = sum(p["pnl"] for p in strat)
        base = equity * (lim["max_strategy_exposure_pct"] or 100) / 100
        dd = max(0.0, -pnl) / base * 100 if base else 0.0
        if dd > lim["strategy_drawdown_limit_pct"]:
            add("strategy_drawdown_limit_pct", FAIL, f"strategy P&L {pnl:+,.2f}", dd, lim["strategy_drawdown_limit_pct"])
            return finish(REJECTED, f"strategy {it['strategy_id']} loss {dd:.2f}% of its allocation > "
                                    f"{lim['strategy_drawdown_limit_pct']}%")
        add("strategy_drawdown_limit_pct", PASS, f"strategy P&L {pnl:+,.2f}", dd, lim["strategy_drawdown_limit_pct"])

    blocked = _w25_limits(conn, it, lim, add, bk, qty * px, equity)
    if blocked:
        return finish(REJECTED, blocked)

    # W1 limits (config.json risk_limits), unchanged -- they measure the owner's book only
    if not own_book:
        add("w1_pretrade", SKIP, f"W1 limits apply to the owner's book; tenant {tenant} uses W4 limits + profile")
        rd.est_value = round(qty * px, 2)
        return _review(finish, settings, it, add, qty)
    try:
        from orders.risk import pretrade_check
        # gateway=False: the price band ran above and max_portfolio_exposure_pct is W4's gross limit
        w1 = pretrade_check(conn, it["symbol"], "BUY", qty, qty * px, env=PAPER, gateway=False)
    except Exception as e:
        w1 = {"ok": False, "message": f"W1 pre-trade check failed to run: {e}"}
    if not w1["ok"]:
        add("w1_pretrade", FAIL, w1["message"])
        return finish(REJECTED, f"W1 limit: {w1['message']}")
    add("w1_pretrade", PASS, w1["message"])

    rd.est_value = round(qty * px, 2)
    return _review(finish, settings, it, add, qty)


def _w25_limits(conn, it, lim, add, bk, order_value, equity) -> str | None:
    """RK-09 / RK-11 / RK-12 pass/fail checks; the rejection reason, or None."""
    keys = ("max_symbol_volatility_pct", "max_avg_correlation", "max_portfolio_var_pct")
    if all(lim.get(k) is None for k in keys):
        return None
    from portfolio import limits as PL
    sym = it["symbol"]
    if lim.get("max_symbol_volatility_pct") is not None:
        v, cap_ = PL.symbol_volatility(conn, sym), lim["max_symbol_volatility_pct"]
        if v is None:
            add("max_symbol_volatility_pct", FAIL, "volatility cannot be measured (short history)")
            return "symbol volatility cannot be measured (fail closed)"
        if v > cap_:
            add("max_symbol_volatility_pct", FAIL, f"annualised volatility {v}%", v, cap_)
            return f"{sym} volatility {v}% > {cap_}%"
        add("max_symbol_volatility_pct", PASS, f"annualised volatility {v}%", v, cap_)
    pos = [{"symbol": p["symbol"], "value": p.get("value")} for p in bk.get("positions", [])]
    if lim.get("max_avg_correlation") is not None:
        c, cap_ = PL.correlation_to_book(conn, sym, pos), lim["max_avg_correlation"]
        if c is None:
            add("max_avg_correlation", FAIL, "correlation with the book cannot be measured")
            return "correlation cannot be measured (fail closed)"
        if c["avg"] > cap_:
            add("max_avg_correlation", FAIL, f"mean correlation {c['avg']} with {c['n']} held (max {c['max']} "
                                             f"with {c['max_with']})", c["avg"], cap_)
            return f"{sym} correlation with the book {c['avg']} > {cap_}"
        add("max_avg_correlation", PASS, f"mean correlation {c['avg']} with {c['n']} held", c["avg"], cap_)
    if lim.get("max_portfolio_var_pct") is not None:
        v, cap_ = PL.post_trade_var(conn, pos, sym, order_value, equity), lim["max_portfolio_var_pct"]
        if v is None:
            add("max_portfolio_var_pct", FAIL, "post-trade VaR cannot be measured")
            return "portfolio VaR cannot be measured (fail closed)"
        if v > cap_:
            add("max_portfolio_var_pct", FAIL, f"post-trade 1-day 95% VaR {v}% of equity", v, cap_)
            return f"post-trade VaR {v}% of equity > {cap_}%"
        add("max_portfolio_var_pct", PASS, f"post-trade 1-day 95% VaR {v}% of equity", v, cap_)
    return None


def _price_band(conn, it, rd, settings, lim, add) -> str | None:
    """RK-21: the rejection reason when the order's price is outside the band, else None.
    The order is created at execution.order_type (order_manager.create_order): a LIMIT / SL
    at rd.reference_price, a MARKET with no price. An order created later through the API
    with its own limit / trigger price is not seen here."""
    from orders.risk import gateway_limits, price_band
    if "max_price_band_pct" not in lim:          # until RISK_DEFAULTS declares it: config.json risk_limits
        lim["max_price_band_pct"] = gateway_limits().get("max_price_band_pct")
    band = lim["max_price_band_pct"]
    otype = str(settings.get("order_type") or "MARKET").upper()
    if band is None:
        add("max_price_band_pct", SKIP, "limit disabled")
        return _circuit(conn, it, rd, otype, add)
    try:
        pb = price_band(conn, it["symbol"], otype, rd.reference_price if otype in ("LIMIT", "SL") else None,
                        None, band)
    except Exception as e:
        pb = {"skipped": True, "reason": f"price band not measured ({e})"}
    if pb.get("skipped"):
        add("max_price_band_pct", SKIP, pb["reason"], limit=band)
        return _circuit(conn, it, rd, otype, add)
    if pb["breached"]:
        add("max_price_band_pct", FAIL, pb["reason"], pb["value"], band)
        return f"price band: {otype} {pb['price']:,.2f} is {pb['value']:.1f}% from {pb['reference_source']} " \
               f"{pb['reference']:,.2f} (> {band:g}%)"
    add("max_price_band_pct", PASS, pb["reason"], pb["value"], band)
    return _circuit(conn, it, rd, otype, add)


def _circuit(conn, it, rd, otype, add) -> str | None:
    """RK-21 (W39): today's NSE circuit (orders/risk.py circuit_check) on the price the order will
    carry; REJECTED outside it. MARKET, or no circuit stored today: SKIP."""
    from orders.risk import circuit_check
    try:
        cc = circuit_check(conn, it["symbol"], otype, rd.reference_price if otype in ("LIMIT", "SL") else None)
    except Exception as e:
        cc = {"skipped": True, "reason": f"circuit limits not checked ({e})"}
    if cc.get("skipped"):
        add("circuit_limit", SKIP, cc["reason"])
        return None
    if cc["breached"]:
        add("circuit_limit", FAIL, cc["reason"], cc["value"])
        return cc["reason"]
    add("circuit_limit", PASS, cc["reason"], cc["value"])
    return None


def _futures_notional(conn) -> float | None:
    """Open paper futures notional (RK-21), None when it cannot be read (the caller fails closed)."""
    try:
        from execution.futures_paper import gross_notional
        return gross_notional(conn)
    except Exception as e:
        log.warning(f"  paper futures notional unavailable: {e}")
        return None


def _tenant_profile(conn, it) -> dict | None:
    """The owning tenant's stored trading profile, or None (no enterprise tables /
    no stored profile -> W4 behaves exactly as before)."""
    try:
        r = conn.execute("SELECT COALESCE(tenant_id,'default') FROM strategy WHERE strategy_id=?",
                         (it["strategy_id"],)).fetchone()
        tenant = r[0] if r else "default"
        if not conn.execute("SELECT 1 FROM enterprise_risk_profile WHERE scope='TENANT' AND scope_id=?",
                            (tenant,)).fetchone():
            return None
        from enterprise.profiles import tenant_constraints
        return {**tenant_constraints(conn, tenant), "_tenant": tenant}
    except Exception:
        return None


def _ml_provenance(conn, it) -> dict | None:
    """None when the decision used no ml_* feature; else {"ok", "message", ...}.
    ML stays subordinate: a signal whose model is no longer ACTIVE is not traded."""
    r = conn.execute("SELECT features_json FROM strategy_decision WHERE decision_id=?", (it["decision_id"],)).fetchone()
    feats = json.loads(r[0] or "{}") if r else {}
    used = {k: v for k, v in feats.items() if k.startswith("ml_") and v is not None}
    if not used:
        return None
    try:
        from ml.config import settings as ml_settings
        model_id = ml_settings().get("default_model")
    except Exception as e:
        return {"ok": False, "message": f"ML configuration unreadable ({e})"}
    if not model_id:
        return {"ok": False, "message": "decision used ML features but no ml.default_model is configured"}
    p = conn.execute("SELECT model_version, ml_score, confidence FROM ml_prediction WHERE model_id=? AND symbol=? "
                     "AND as_of=? AND version_status='ACTIVE' ORDER BY created_at DESC LIMIT 1",
                     (model_id, it["symbol"], str(it["as_of"]))).fetchone()
    if not p:
        return {"ok": False, "message": f"no ACTIVE {model_id} prediction for {it['symbol']} {it['as_of']} "
                                        f"(ML provenance missing)"}
    st = conn.execute("SELECT status FROM ml_model_version WHERE model_id=? AND version=?",
                      (model_id, p[0])).fetchone()
    if not st or st[0] != "ACTIVE":
        return {"ok": False, "message": f"model {model_id} {p[0]} is now {st[0] if st else 'missing'}, not ACTIVE",
                "model_id": model_id, "version": p[0]}
    return {"ok": True, "model_id": model_id, "version": p[0], "score": p[1],
            "message": f"model {model_id} {p[0]} ACTIVE; ml_score {p[1]}, confidence {p[2]}"}


def _futures_leg(conn, it, rd, settings, add, finish):
    """
    W30 (QR-05 / QR-06). SHORT opens / adds a short in the paper stock-futures book;
    COVER buys it back. LIVE futures do not exist (BLOCKED). Sizing is in whole lots:
    lots = floor(target_position_pct% x equity / (future price x lot size)); fewer than one
    lot is REJECTED rather than rounded up. The margin (futures.margin_pct of notional)
    must fit the paper cash under max_capital_allocation_pct, and the notional must fit
    max_position_pct -- the same limits a long position meets.
    """
    from execution import futures_paper as FP
    from execution.tenant_books import is_default, tenant_of_strategy
    tenant = tenant_of_strategy(conn, it["strategy_id"])
    if not is_default(tenant):
        add("instrument", FAIL, f"tenant {tenant} has no futures book")
        return finish(BLOCKED, "futures short legs are owner-book only")
    if it["book"] != "PAPER" or settings["mode"] != "PAPER":
        add("instrument", FAIL, "futures short legs exist only in the PAPER book")
        return finish(BLOCKED, "LIVE futures are not built")
    if it["action"] == "COVER":
        pos = FP.position(conn, it["strategy_id"], it["symbol"])
        if not pos or pos["lots"] >= 0:
            add("position_held", FAIL, "no futures short held by this strategy")
            return finish(REJECTED, f"no {it['symbol']} futures short to cover")
        qty = -pos["lots"] * pos["lot_size"]
        rd.reference_price = pos["avg_price"]
        add("position_held", PASS, f"short {-pos['lots']} lot(s) x {pos['lot_size']} ({pos['expiry']}); covering all",
            -pos["lots"])
        rd.est_value = round(qty * pos["avg_price"], 2)
        return _review(finish, settings, it, add, qty)
    # SHORT opens risk: the broker / data health gate applies as it does to a BUY
    try:
        from execution.broker_health import gate as _bh_gate
        bst, bmsg = _bh_gate(conn, "BUY")
    except Exception as e:
        bst, bmsg = SKIP, f"broker health unreadable ({e})"
    if bst == FAIL:
        add("broker_health_short", FAIL, bmsg)
        return finish(REJECTED, f"broker / data health: {bmsg}")
    ok, c, why = FP.shortable(conn, it["symbol"], it["as_of"])
    if not ok:
        add("shortable", FAIL, why)
        return finish(REJECTED, f"not shortable: {why}")
    from utils.trading_calendar import is_trading_day
    behind, d = 0, _d(c["session"])
    probe = _d(it["as_of"])
    while probe > d and behind < 10:
        if is_trading_day(probe):
            behind += 1
        probe = probe - timedelta(days=1)
    if behind > 2:
        add("shortable", FAIL, f"futures data is {behind} sessions old ({c['session']})")
        return finish(REJECTED, "stale futures data")
    add("shortable", PASS, why)
    rd.reference_price = c["price"]
    try:
        bk = P.book(conn)
    except Exception as e:
        bk = {"equity": None, "cash": None, "error": str(e)}
    equity, cash = bk.get("equity"), bk.get("cash")
    rd.equity = equity
    if not equity or equity <= 0:
        add("equity", FAIL, "paper equity unknown")
        return finish(REJECTED, "equity cannot be measured")
    lim = rd.limits or {}
    target = float(it.get("target_position_pct") or 0) or float(lim.get("max_position_pct") or 10)
    target = min(target, float(lim.get("max_position_pct") or target))
    lot_val = c["price"] * c["lot_size"]
    lots = int(equity * target / 100 // lot_val)
    if lots < 1:
        add("lot_sizing", FAIL, f"one lot is {lot_val:,.0f} > {target}% of equity ({equity * target / 100:,.0f})",
            lot_val, equity * target / 100)
        return finish(REJECTED, "one futures lot exceeds the position target")
    gx = lim.get("max_portfolio_exposure_pct")              # W39 (RK-21): a short is gross exposure too
    if gx is not None:
        fut = _futures_notional(conn)
        if fut is None:
            add("max_portfolio_exposure_pct", FAIL, "paper futures notional cannot be measured")
            return finish(REJECTED, "gross exposure cannot be measured (fail closed)")
        held = sum((p.get("value") or 0.0) for p in bk.get("positions") or [])
        fit = max(0, int((equity * gx / 100 - held - fut) // lot_val))
        what = f"positions {held:,.0f} + futures notional {fut:,.0f} (limit {gx:g}% of equity)"
        if fit < 1:
            add("max_portfolio_exposure_pct", FAIL, f"no room for one lot: {what}", fit)
            return finish(REJECTED, "no room under max_portfolio_exposure_pct")
        add("max_portfolio_exposure_pct", WARN if fit < lots else PASS,
            f"{'caps the short at' if fit < lots else 'room for'} {fit} lot(s): {what}", fit)
        lots = min(lots, fit)
    fs = FP.settings()
    margin = lots * lot_val * float(fs["margin_pct"]) / 100
    cap = (cash or 0) * float(lim.get("max_capital_allocation_pct") or 95) / 100
    while lots > 0 and margin > cap:
        lots -= 1
        margin = lots * lot_val * float(fs["margin_pct"]) / 100
    if lots < 1:
        add("margin", FAIL, f"margin for one lot exceeds available cash allocation ({cap:,.0f})")
        return finish(REJECTED, "insufficient cash for futures margin")
    add("lot_sizing", PASS, f"{lots} lot(s) x {c['lot_size']} @ {c['price']} = {lots * lot_val:,.0f} notional "
                            f"({lots * lot_val / equity * 100:.1f}% of equity)", lots * lot_val, equity * target / 100)
    add("margin", PASS, f"margin {margin:,.0f} ({fs['margin_pct']}%) of cash allocation {cap:,.0f}", margin, cap)
    rd.est_value = round(lots * lot_val, 2)
    return _review(finish, settings, it, add, lots * c["lot_size"])


def _review(finish, settings, it, add, qty):
    if settings["require_manual_review"] or it["book"] == LIVE or settings["mode"] == LIVE:
        add("manual_review", WARN, "a person must approve this order (require_manual_review / LIVE)")
        return finish(REVIEW_REQUIRED, "manual review required", qty)
    add("manual_review", SKIP, "not required")
    return finish(APPROVED, None, qty)


def _store(conn, rd: RiskDecision, it: dict, actor: str):
    conn.execute(
        "INSERT INTO risk_decision (risk_decision_id,intent_id,decision_id,strategy_id,strategy_version,symbol,side,"
        "action,book,mode,requested_quantity,approved_quantity,reference_price,est_value,equity,risk_status,"
        "rejection_reason,risk_checks_json,limits_json,engine_version,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rd.risk_decision_id, rd.intent_id, rd.decision_id, rd.strategy_id, rd.strategy_version, rd.symbol, rd.side,
         rd.action, rd.book, rd.mode, rd.requested_quantity, rd.approved_quantity, rd.reference_price, rd.est_value,
         rd.equity, rd.risk_status, rd.rejection_reason, json.dumps(rd.risk_checks, default=str),
         json.dumps(rd.limits, default=str), rd.engine_version, rd.timestamp))
    conn.execute("UPDATE strategy_position_intent SET authorization_status=?, risk_decision_id=?, authorized_at=? "
                 "WHERE intent_id=?", (INTENT_STATUS_FOR[rd.risk_status], rd.risk_decision_id,
                                       datetime.now() if rd.risk_status == APPROVED else None, rd.intent_id))
    conn.commit()
    lvl = log.info if rd.risk_status == APPROVED else log.warning
    lvl(f"  risk {rd.risk_status}: {rd.side} {rd.approved_quantity}/{rd.requested_quantity} {rd.symbol} "
        f"[{rd.strategy_id} {rd.strategy_version}] {rd.rejection_reason or ''}")


def approve_review(conn, risk_decision_id: str, actor: str = "owner") -> dict:
    """REVIEW_REQUIRED -> APPROVED by a person. LIVE decisions stay unexecutable
    in W4 regardless (the Dhan adapter refuses)."""
    r = conn.execute("SELECT risk_status, intent_id FROM risk_decision WHERE risk_decision_id=?",
                     (risk_decision_id,)).fetchone()
    if not r:
        raise InvalidIntentError(f"no risk decision {risk_decision_id}")
    if r[0] != REVIEW_REQUIRED:
        raise InvalidIntentError(f"risk decision is {r[0]}, not REVIEW_REQUIRED")
    now = datetime.now()
    conn.execute("UPDATE risk_decision SET risk_status=?, reviewed_by=?, reviewed_at=? WHERE risk_decision_id=?",
                 (APPROVED, actor, now, risk_decision_id))
    conn.execute("UPDATE strategy_position_intent SET authorization_status='AUTHORIZED', authorized_at=? "
                 "WHERE intent_id=?", (now, r[1]))
    conn.commit()
    return get_decision(conn, risk_decision_id)


def get_decision(conn, risk_decision_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM risk_decision WHERE risk_decision_id=?", (risk_decision_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["risk_checks"] = json.loads(d.pop("risk_checks_json") or "[]")
    d["limits"] = json.loads(d.pop("limits_json") or "{}")
    return d
