"""
OPTION intents through the W4 risk engine (W40: ENT-15 "strategies do not emit option intents yet").

An option-overlay strategy (strategy_engine/option_overlay.py) decides OPTION_OPEN / OPTION_CLOSE for an
underlying; its PositionIntent carries the plan in the decision's features (`option_plan`): template,
underlying, expiry, lots and every leg (CE / PE, strike, BUY / SELL). This module is what
risk_engine.evaluate() runs for those intents -- the option twin of its W30 futures branch
(_futures_leg) -- and what order_manager.create_order() asks for the order's legs.

    evaluate_option(...)   GATES       owner book only; PAPER only -- a LIVE book or execution.mode LIVE is
                                       BLOCKED ("LIVE options are not built"); options.enabled must be true
                           OPTION_CLOSE the position must be open and the strategy's own; reducing risk is
                                       never sized down -- approved for the position's lots
                           OPTION_OPEN  (each check recorded PASS / WARN / FAIL like every W4 check)
        option_definition   the strategy's stored definition is an option_overlay (the authority for
                            allow_naked_short_calls and option_risk -- not the intent)
        option_position     no open position of this strategy on the underlying (one at a time)
        option_chain        a stored chain no older than options.max_chain_age_sessions sessions
        option_expiry       no new position within options.no_new_days_to_expiry days of expiry
        option_legs         every leg priced by the FILL RULE (options_paper.leg_quote: chain mid moved
                            against the order by slippage_bps) and traded (volume or OI > 0)
        naked_short_call    short calls not matched by long calls are covered by free held shares
                            (covered_call) or REFUSED unless the definition sets allow_naked_short_calls
        option_risk         per structure lot: net premium, max profit / max loss, risk amount, margin
                            (options_paper.structure_metrics: the documented SPAN-like approximation)
        sizing (lots)       requested = the definition's lots, then capped -- a binding cap is WARN,
                            no room is FAIL (REJECTED with the cap's name), as for cash orders:
            covered_shares             free shares / (lot size x short calls per lot)  (covered_call)
            max_loss_per_strategy      sum of the strategy's open risk amounts + this one <=
                                       min(option_risk.max_loss_pct% of paper equity, max_loss_rupees)
            option_premium_trade       the debit <= options.max_premium_trade_pct% of paper cash
            option_premium_total       debits held (owner book + strategies) + this one <=
                                       options.max_premium_total_pct% of paper cash
            option_margin_total        margin blocked by strategy positions + this one <=
                                       options.max_margin_total_pct% of paper cash
            max_capital_allocation_pct debit + margin + fees <= that W4 limit's % of paper cash
        max_daily_trades, daily_loss_limit_pct, portfolio_drawdown_limit_pct   the W4 portfolio limits
                            (fail closed when unmeasurable, as for cash orders)
        manual_review       require_manual_review -> REVIEW_REQUIRED (as every W4 approval)

    approved_quantity = structure LOTS (each leg trades lots x lot size units); est_value = the risk
    amount; reference_price = the underlying's price.

    order_plan(conn, rd)  the legs_json an OPT order carries to the paper options book (options_paper.
                          fill_strategy_order): the decision's legs and the definition's naked-call flag
                          for OPTION_OPEN; the position id for OPTION_CLOSE.
    preview(conn, it)     the option checks alone, nothing stored -- the dry run's view for a strategy not
                          yet in a decision state (the generic W4 gates would stop it first).
"""

from __future__ import annotations

import json
import math

from execution.config import PAPER
from execution.models import APPROVED, BLOCKED, FAIL, PASS, REJECTED, SKIP, WARN, RiskCheck, RiskDecision

OPTION_ACTIONS = ("OPTION_OPEN", "OPTION_CLOSE")
DEFAULT_MAX_LOSS_PCT = 2.0


def plan_of(conn, it: dict) -> dict | None:
    """The intent's option plan: carried in-memory by a dry run, else the decision's stored features."""
    if it.get("_option_plan"):
        return it["_option_plan"]
    r = conn.execute("SELECT features_json FROM strategy_decision WHERE decision_id=?", (it["decision_id"],)).fetchone()
    try:
        feats = json.loads(r[0] or "{}") if r else {}
    except ValueError:
        feats = {}
    return feats.get("option_plan")


def _definition(conn, it) -> dict | None:
    try:
        from strategy_engine import registry
        v = registry.get_version(conn, it["strategy_id"], it.get("version"))
        return v["definition"] if v else None
    except Exception:
        return None


def evaluate_option(conn, it, rd, settings, lim, add, finish, review):
    """The OPTION branch of risk_engine.evaluate (see the module docstring for every check)."""
    from execution import options_paper as O
    from execution.tenant_books import is_default, tenant_of_strategy
    tenant = tenant_of_strategy(conn, it["strategy_id"])
    if not is_default(tenant):
        add("instrument", FAIL, f"tenant {tenant} has no options book")
        return finish(BLOCKED, "option strategies are owner-book only")
    if it["book"] != PAPER or settings["mode"] != PAPER:
        add("instrument", FAIL, "OPTION intents exist only in the PAPER book; LIVE options are not built")
        return finish(BLOCKED, "LIVE options are not built")
    s = O.settings()
    if not s["enabled"]:
        add("options_book", FAIL, "options.enabled is false")
        return finish(REJECTED, "the paper options book is off (config.json options.enabled)")
    add("instrument", PASS, "PAPER options book (owner)")
    O.ensure_tables(conn)
    plan = plan_of(conn, it)
    if not plan or plan.get("action") != it.get("action"):
        add("option_plan", FAIL, "the intent's decision carries no option plan for this action")
        return finish(REJECTED, "no option plan on the intent")

    if it["action"] == "OPTION_CLOSE":
        pos = O.get_position(conn, plan.get("position_id") or "")
        if not pos or pos["status"] != "OPEN" or pos["strategy_id"] != it["strategy_id"]:
            add("position_held", FAIL, f"no open option position {plan.get('position_id')} of this strategy")
            return finish(REJECTED, f"no open option position {plan.get('position_id')} to close")
        add("position_held", PASS, f"{pos['template']} {pos['underlying']} {pos['expiry']}: {pos['lots']} lot(s) x "
                                   f"{pos['lot_size']}; closing all ({plan.get('exit_reason') or 'exit'})", pos["lots"])
        rd.est_value = round(abs(pos.get("liq_value") or 0.0), 2)
        return review(finish, settings, it, add, int(pos["lots"]))
    return _open(conn, it, rd, settings, lim, add, finish, review, plan, s)


def _open(conn, it, rd, settings, lim, add, finish, review, plan, s):
    from datetime import date
    from execution import options_paper as O
    from execution import positions as P
    defn = _definition(conn, it)
    if not defn or defn.get("kind") != "option_overlay":
        add("option_definition", FAIL, "the strategy version is not an option_overlay definition")
        return finish(REJECTED, "OPTION intents come only from option_overlay strategies")
    allow_naked = defn.get("allow_naked_short_calls") is True
    orisk = defn.get("option_risk") or {}
    add("option_definition", PASS, f"{defn['strategy_id']} {defn['version']}: {plan['template']}"
                                   + (" (naked short calls allowed)" if allow_naked else ""))
    sym, u = it["symbol"], plan["underlying"]
    held = O.open_positions(conn, it["strategy_id"], sym)
    if held:
        add("option_position", FAIL, f"already holds {held[0]['position_id']} ({held[0]['template']} {u})")
        return finish(REJECTED, f"{it['strategy_id']} already holds an open option position on {sym}")
    add("option_position", PASS, f"no open option position on {sym}")

    ch = O.chain_for_fill(conn, u, s)
    age = O.chain_age(ch)
    if age is None:
        add("option_chain", FAIL, f"no option chain stored for {u}")
        return finish(REJECTED, f"no option chain for {u}")
    if age > int(s["max_chain_age_sessions"]):
        add("option_chain", FAIL, f"chain is {age} sessions old ({ch['session']})", age, s["max_chain_age_sessions"])
        return finish(REJECTED, f"stale option chain for {u} ({age} sessions)")
    add("option_chain", PASS, ch["source"], age, s["max_chain_age_sessions"])

    today = date.today()
    dte = min((O._d(l["expiry"]) - today).days for l in plan["legs"])
    if dte < int(s["no_new_days_to_expiry"]):
        add("option_expiry", FAIL, f"{dte} day(s) to expiry", dte, s["no_new_days_to_expiry"])
        return finish(REJECTED, f"no new option positions within {s['no_new_days_to_expiry']} day(s) of expiry")
    add("option_expiry", PASS, f"{plan['expiry']}: {dte} day(s) to expiry", dte, s["no_new_days_to_expiry"])

    priced, bad = [], []
    for leg in plan["legs"]:
        q = O.leg_quote(conn, u, leg["expiry"], leg["strike"], leg["option_type"], leg["side"], s, chain=ch)
        label = f"{leg['side']} {float(leg['strike']):g} {leg['option_type']}"
        if not q:
            bad.append(f"{label}: no price")
        elif not q["traded"]:
            bad.append(f"{label}: no volume or OI")
        else:
            priced.append({**leg, "price": q["price"], "iv": q["iv"], "lot_size": q["lot_size"], "source": q["source"]})
    if bad:
        add("option_legs", FAIL, "; ".join(bad))
        return finish(REJECTED, "option legs unpriced or untraded: " + "; ".join(bad))
    sizes = {p["lot_size"] for p in priced}
    if len(sizes) != 1 or not next(iter(sizes)):
        add("option_legs", FAIL, f"lot size unknown / inconsistent {sorted(map(str, sizes))}")
        return finish(REJECTED, "option lot size unknown")
    L = int(next(iter(sizes)))
    add("option_legs", PASS, ", ".join(f"{p['side']} {float(p['strike']):g} {p['option_type']} @ {p['price']}"
                                       for p in priced) + f" (lot {L}; fill rule: mid -/+ {s['slippage_bps']:g} bps)")
    spot = float(ch["spot"] or it.get("entry_reference") or rd.reference_price or 0)
    if spot <= 0:
        add("market_data", FAIL, f"no price for the underlying {u}")
        return finish(REJECTED, f"no underlying price for {u}")
    rd.reference_price = spot

    uses_shares = bool(plan.get("uses_shares"))
    free = O.cover_available(conn, sym) if uses_shares else 0
    try:
        m1 = O.structure_metrics(priced, spot, L, 1, cover_shares=free, uses_shares=uses_shares,
                                 allow_naked=allow_naked, s=s)
    except ValueError as e:
        add("option_risk", FAIL, f"payoff not computable: {e}")
        return finish(REJECTED, f"option payoff not computable: {e}")
    shares_cap = None
    if uses_shares and m1["covered_shares"] == 0 and not m1["refused"] and allow_naked and m1["naked_call_lots"]:
        add("naked_short_call", WARN, f"{free} free shares cannot cover one lot ({L}); written NAKED "
                                      f"(allow_naked_short_calls)")
    elif m1["refused"]:
        add("naked_short_call", FAIL, m1["refused"], free if uses_shares else None)
        return finish(REJECTED, m1["refused"])
    elif m1["covered_shares"]:
        per_lot = m1["covered_shares"]
        shares_cap = free // per_lot
        add("naked_short_call", PASS, f"short call covered by held shares: {free} free, {per_lot} per lot", free)
    elif m1["naked_call_lots"]:
        add("naked_short_call", WARN, "NAKED short call allowed by the definition (allow_naked_short_calls); "
                                      f"margin {s['naked_margin_pct']:g}% of notional, risk at a "
                                      f"{s['naked_stress_move_pct']:g}% rally")
    else:
        add("naked_short_call", SKIP, "no unmatched short call")
    mp = "unlimited" if m1["max_profit"] is None else f"{m1['max_profit']:,.0f}"
    mlx = "unlimited" if m1["max_loss"] is None else f"{-m1['max_loss']:,.0f}"
    add("option_risk", PASS, f"per lot: net {'credit' if m1['net_premium'] > 0 else 'debit'} "
                             f"{abs(m1['net_premium']):,.0f}, max profit {mp}, max loss {mlx}, risk amount "
                             f"{m1['risk_amount']:,.0f}, margin {m1['margin']:,.0f} ({m1['margin_rule']}), "
                             f"POP {m1['pop_pct']}%", m1["risk_amount"])

    try:
        bk = P.book(conn)
    except Exception as e:
        bk = {"equity": None, "cash": None, "error": str(e)}
    equity, cash = bk.get("equity"), float(bk.get("cash") or 0.0)
    rd.equity = equity
    if not equity or equity <= 0:
        add("equity", FAIL, f"paper equity unknown ({bk.get('error') or 'no account'})")
        return finish(REJECTED, "equity cannot be measured")
    add("equity", PASS, f"paper equity {equity:,.2f}, cash {cash:,.2f}", equity)

    requested = int(plan.get("lots") or 1)
    rd.requested_quantity = requested
    caps = {}

    def cap(name, lots, what):
        caps[name] = (int(lots), what)

    if shares_cap is not None:
        cap("covered_shares", shares_cap, f"{free} free shares, {m1['covered_shares']} per lot")
    open_all = O.open_positions(conn)
    mine = [p for p in open_all if p["strategy_id"] == it["strategy_id"]]
    used = sum(float(p.get("risk_amount") or 0) for p in mine)
    limits = []
    if orisk.get("max_loss_pct", DEFAULT_MAX_LOSS_PCT if "max_loss_rupees" not in orisk else None) is not None:
        pct = float(orisk.get("max_loss_pct", DEFAULT_MAX_LOSS_PCT))
        limits.append((equity * pct / 100, f"{pct:g}% of equity"))
    if orisk.get("max_loss_rupees") is not None:
        limits.append((float(orisk["max_loss_rupees"]), f"Rs {float(orisk['max_loss_rupees']):,.0f}"))
    loss_cap, loss_what = min(limits)
    room = loss_cap - used
    if m1["risk_amount"] > 0:
        cap("max_loss_per_strategy", math.floor(max(0.0, room) / m1["risk_amount"]),
            f"max loss {loss_what} = {loss_cap:,.0f}; open {used:,.0f}; {m1['risk_amount']:,.0f} per lot")
    else:
        add("max_loss_per_strategy", PASS, f"no loss beyond the premium in hand (open {used:,.0f} of "
                                           f"{loss_cap:,.0f})", used, loss_cap)
    if m1["debit"] > 0:
        cap("option_premium_trade", math.floor(cash * float(s["max_premium_trade_pct"]) / 100 / m1["debit"]),
            f"{s['max_premium_trade_pct']:g}% of paper cash, debit {m1['debit']:,.0f} per lot")
        held_prem = O._premium_held(conn) + sum(max(0.0, -float(p.get("net_premium") or 0)) for p in open_all)
        cap("option_premium_total",
            math.floor(max(0.0, cash * float(s["max_premium_total_pct"]) / 100 - held_prem) / m1["debit"]),
            f"{s['max_premium_total_pct']:g}% of paper cash; {held_prem:,.0f} of premium already held")
    if m1["margin"] > 0:
        blocked = sum(float(p.get("margin_blocked") or 0) for p in open_all)
        cap("option_margin_total",
            math.floor(max(0.0, cash * float(s["max_margin_total_pct"]) / 100 - blocked) / m1["margin"]),
            f"{s['max_margin_total_pct']:g}% of paper cash; {blocked:,.0f} already blocked, "
            f"{m1['margin']:,.0f} per lot")
    alloc = lim.get("max_capital_allocation_pct")
    if alloc is not None and m1["cash_needed"] > 0:
        per = m1["debit"] + m1["margin"]
        room_c = cash * float(alloc) / 100 - m1["fees"]
        cap("max_capital_allocation_pct", math.floor(max(0.0, room_c) / per) if per > 0 else requested,
            f"{alloc:g}% of paper cash; {per:,.0f} per lot + fees {m1['fees']:,.0f}")

    lots, binding = requested, None
    for name, (q, what) in caps.items():
        if q < lots:
            binding = binding or name
        if q <= 0:
            add(name, FAIL, f"no room for one lot: {what}", q)
        elif q < requested:
            add(name, WARN, f"caps the position at {q} lot(s): {what}", q)
        else:
            add(name, PASS, f"room for {q} lot(s): {what}", q)
        lots = min(lots, q)
    if lots <= 0:
        return finish(REJECTED, f"no room for one option lot under {binding}")

    blocked = _portfolio_limits(conn, lim, add, equity)
    if blocked:
        return finish(REJECTED, blocked)

    m = O.structure_metrics(priced, spot, L, lots, cover_shares=free, uses_shares=uses_shares,
                            allow_naked=allow_naked, s=s)
    add("lot_sizing", PASS, f"{lots} lot(s) x {L}: net {'credit' if m['net_premium'] > 0 else 'debit'} "
                            f"{abs(m['net_premium']):,.0f}, risk {m['risk_amount']:,.0f}, margin {m['margin']:,.0f}, "
                            f"fees {m['fees']:,.0f}", lots, requested)
    add("w1_pretrade", SKIP, "W1 limits are cash-equity order limits; options use the checks above")
    rd.est_value = round(m["risk_amount"] or m["debit"], 2)
    return review(finish, settings, it, add, lots)


def _portfolio_limits(conn, lim, add, equity) -> str | None:
    """max_daily_trades, daily_loss_limit_pct and portfolio_drawdown_limit_pct, as risk_engine applies them
    to a BUY (fail closed when they cannot be measured). The rejection reason, or None."""
    if lim.get("max_daily_trades") is not None:
        from execution.risk_engine import _orders_today
        n = _orders_today(conn)
        if n + 1 > lim["max_daily_trades"]:
            add("max_daily_trades", FAIL, f"{n} orders today", n + 1, lim["max_daily_trades"])
            return f"max_daily_trades {lim['max_daily_trades']} reached"
        add("max_daily_trades", PASS, f"{n} orders today", n + 1, lim["max_daily_trades"])
    if lim.get("daily_loss_limit_pct") is None and lim.get("portfolio_drawdown_limit_pct") is None:
        return None
    try:
        from portfolio.pnl import risk_state
        st = risk_state(conn, PAPER)
    except Exception as e:
        st = {"day_pnl": None, "drawdown_pct": None, "error": str(e)}
    if lim.get("daily_loss_limit_pct") is not None:
        if st.get("day_pnl") is None:
            add("daily_loss_limit_pct", FAIL, "today's P&L cannot be measured")
            return "daily loss cannot be measured (fail closed)"
        loss = max(0.0, -st["day_pnl"]) / equity * 100
        if loss > lim["daily_loss_limit_pct"]:
            add("daily_loss_limit_pct", FAIL, f"lost {loss:.2f}% today", loss, lim["daily_loss_limit_pct"])
            return f"daily loss {loss:.2f}% > {lim['daily_loss_limit_pct']}%"
        add("daily_loss_limit_pct", PASS, f"today {st['day_pnl']:+,.2f}", loss, lim["daily_loss_limit_pct"])
    if lim.get("portfolio_drawdown_limit_pct") is not None:
        if st.get("drawdown_pct") is None:
            add("portfolio_drawdown_limit_pct", FAIL, "drawdown cannot be measured")
            return "drawdown cannot be measured (fail closed)"
        if st["drawdown_pct"] > lim["portfolio_drawdown_limit_pct"]:
            add("portfolio_drawdown_limit_pct", FAIL, "drawdown from peak", st["drawdown_pct"],
                lim["portfolio_drawdown_limit_pct"])
            return f"portfolio drawdown {st['drawdown_pct']}% > {lim['portfolio_drawdown_limit_pct']}%"
        add("portfolio_drawdown_limit_pct", PASS, "drawdown from peak", st["drawdown_pct"],
            lim["portfolio_drawdown_limit_pct"])
    return None


def order_plan(conn, rd: dict) -> dict:
    """The legs an APPROVED OPTION risk decision becomes, for oms_order.legs_json."""
    it = conn.execute("SELECT * FROM strategy_position_intent WHERE intent_id=?", (rd["intent_id"],)).fetchone()
    it = dict(it) if it else {"decision_id": rd.get("decision_id"), "strategy_id": rd["strategy_id"],
                              "version": rd.get("strategy_version")}
    plan = plan_of(conn, it)
    if not plan or plan.get("action") != rd.get("action"):
        raise ValueError(f"risk decision {rd['risk_decision_id']} has no option plan")
    out = {k: plan.get(k) for k in ("action", "symbol", "underlying", "template", "expiry", "legs", "uses_shares",
                                    "exits", "position_id", "exit_reason")}
    out["lots"] = int(rd["approved_quantity"])
    if rd.get("action") == "OPTION_OPEN":
        defn = _definition(conn, {"strategy_id": rd["strategy_id"], "version": rd.get("strategy_version")}) or {}
        out["allow_naked_short_calls"] = defn.get("allow_naked_short_calls") is True
    return out


def preview(conn, it: dict, actor: str = "dry_run") -> RiskDecision:
    """The option checks alone for an in-memory intent (no gates, nothing stored)."""
    from execution.config import execution_settings, limit_values
    from execution.risk_engine import _review
    settings = execution_settings()
    lim = limit_values(conn)
    checks = []
    rd = RiskDecision(intent_id=it.get("intent_id") or "DRYRUN", symbol=it["symbol"], side=it["side"],
                      risk_status=APPROVED, requested_quantity=it.get("quantity"), approved_quantity=0,
                      decision_id=it.get("decision_id"), strategy_id=it["strategy_id"],
                      strategy_version=it.get("version"), action=it.get("action"), book=it.get("book") or PAPER,
                      mode=settings["mode"], limits=lim, reference_price=it.get("entry_reference"))

    def add(check, status, message, value=None, limit=None):
        checks.append(RiskCheck(check, status, message, None if value is None else round(float(value), 4),
                                None if limit is None else float(limit)))

    def finish(status, reason=None, qty=0):
        rd.risk_status, rd.rejection_reason, rd.approved_quantity = status, reason, int(qty or 0)
        rd.risk_checks = [c.__dict__ for c in checks]
        return rd
    return evaluate_option(conn, it, rd, settings, lim, add, finish, _review)
