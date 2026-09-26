"""
Portfolio rebalancing (W15, ATIP-RBL-001): how far holdings have drifted from the
W14 target, whether that warrants action, and the cheapest set of trades that
fixes it. Recommendations only: nothing is ordered.

INPUTS
    current   W12 positions, investable classes only (real estate / other are held,
              not rebalanced); paper positions never count
    target    allocation.current_target(): the latest W14 run if under 7 days old,
              else computed now

CHECK (check())
    drift        per class: current - target (percentage points) and relative to target
    triggers     THRESHOLD   a class beyond wealth.rebalance_abs_band_pct (5 pts) or
                             wealth.rebalance_rel_band_pct (25%) of its target
                 CALENDAR    no plan accepted in 365 days and some drift > 1 pt
                 RISK        CMA volatility above 1.25x the target's, or a 1-in-20 bad year
                             beyond the investor's maximum loss
                 CONCENTRATION  a share above wealth.single_stock_cap_pct of net worth
                 SCORES      holdings ATIP flags (CRI >= 75 or SELL / EXIT / AVOID)
    verdict      REBALANCE / REVIEW / NO_ACTION

PLAN (plan(mode, new_cash))
    to_target    trade every class back to its target
    to_band      trade only classes outside their band, back to the band edge
                 (less turnover; the default)
    cash_flow    invest new_cash into underweight classes first, sell nothing
    Class legs become instrument suggestions:
      SELL  within an overweight class, in this order: single-stock cap breaches, holdings
            ATIP flags, lowest ATIP score, largest. Listed holdings get whole-share
            quantities; manual assets get "reduce by Rs X".
      BUY   the class's existing holding with the best ATIP score and a BUY signal, else
            an illustrative index ETF for the class (NIFTYBEES, MON100, LTGILTBEES,
            GOLDBEES, SILVERBEES, LIQUIDBEES).
    Costs  every listed leg priced with the backtest cost model (backtest/costs.py,
           NSE delivery; ETFs with ETF STT) plus SLIPPAGE_BPS; legs under
           wealth.rebalance_min_trade are dropped; the plan reports turnover, cost and
           cost as a share of turnover.
    After  projected weights, remaining drift, CMA stats before / after.

TAX-NEUTRAL: capital-gains tax is outside ATIP's scope (brief section 3); the plan
does not look at holding periods or gains. Stated on every plan.
"""

from __future__ import annotations

import math
from datetime import timedelta

from wealth import allocation as AL
from wealth import common as C
from wealth.config import settings

METHODOLOGY_VERSION = "RBL-1.0"
SLIPPAGE_BPS = 5.0
MODES = ("to_target", "to_band", "cash_flow")
DECISIONS = ("ACCEPTED", "DISMISSED", "EXECUTED_MANUALLY")
VEHICLES = {"EQUITY": "NIFTYBEES", "INTL_EQUITY": "MON100", "BONDS": "LTGILTBEES", "GOLD": "GOLDBEES",
            "SILVER": "SILVERBEES", "CASH": "LIQUIDBEES"}
TAX_NOTE = "Tax-neutral: capital-gains tax is not considered (outside ATIP's scope). Check the tax impact yourself."


def _cost_models():
    from dataclasses import replace

    from backtest.costs import cost_model, cost_model_from_config
    try:
        stock = cost_model_from_config()
    except Exception:
        stock = cost_model("nse_delivery")
    etf = replace(stock, stt_buy_pct=0.0, stt_sell_pct=0.001)
    return stock, etf


def _current(conn, owner):
    from wealth import holdings as H
    pos, _ = H.positions(conn, owner, include_paper=False)
    inv = [p for p in pos if p["include_in_net_worth"] and p["asset_class"] in AL.CLASSES]
    total = sum(p["value"] for p in inv)
    vals = {k: 0.0 for k in AL.CLASSES}
    for p in inv:
        vals[p["asset_class"]] += p["value"]
    w = {k: (v / total * 100 if total else 0.0) for k, v in vals.items()}
    gross = sum(p["value"] for p in pos if p["include_in_net_worth"])
    return inv, total, vals, w, gross


def _last_accepted(conn, owner):
    r = conn.execute("SELECT decided_at FROM wealth_rebalance_plan WHERE tenant_id=? AND owner_id=? AND status IN "
                     "('ACCEPTED','EXECUTED_MANUALLY') ORDER BY decided_at DESC LIMIT 1",
                     (owner["tenant_id"], owner["owner_id"])).fetchone()
    return r[0] if r else None


def check(conn, owner) -> dict:
    cfg = settings()
    tgt = AL.current_target(conn, owner)
    if not tgt:
        raise ValueError("an Investor DNA (and so a target allocation) is required first")
    inv, total, vals, cur, gross = _current(conn, owner)
    target = tgt["target"]
    drift = []
    for k in AL.CLASSES:
        t, c = target.get(k, 0.0), cur[k]
        # a class is out of band when it breaches EITHER the absolute or the relative band
        abs_band = cfg["rebalance_abs_band_pct"]
        rel_band = t * cfg["rebalance_rel_band_pct"] / 100 if t > 0 else 0.5
        eff = min(abs_band, rel_band) if t >= 2 else abs_band
        lo, hi = max(0.0, t - eff), t + eff
        drift.append({"class": k, "current_pct": round(c, 2), "target_pct": round(t, 2),
                      "drift_pp": round(c - t, 2), "relative_pct": round((c - t) / t * 100, 1) if t else None,
                      "band": [round(lo, 2), round(hi, 2)], "out_of_band": c < lo - 1e-9 or c > hi + 1e-9,
                      "current_value": round(vals[k], 2), "target_value": round(total * t / 100, 2)})
    triggers = []
    oob = [d for d in drift if d["out_of_band"]]
    if oob:
        triggers.append({"trigger": "THRESHOLD", "severity": "action",
                         "detail": ", ".join(f"{d['class']} {d['drift_pp']:+.1f} pts" for d in oob)})
    last = _last_accepted(conn, owner)
    maxd = max(abs(d["drift_pp"]) for d in drift) if drift else 0
    if total and maxd > 1:
        from datetime import datetime as _dt
        la = last if (last is None or isinstance(last, _dt)) else _dt.fromisoformat(str(last)[:19])
        if la is None or C.now() - la > timedelta(days=365):
            triggers.append({"trigger": "CALENDAR", "severity": "review",
                             "detail": "no rebalance accepted in the last year" if la else "never rebalanced"})
    cs, ts = AL.portfolio_stats(cur), AL.portfolio_stats(target)
    max_loss = tgt["investor"]["max_annual_loss_pct"]
    if total and (cs["volatility_pct"] > ts["volatility_pct"] * 1.25 or cs["bad_year_pct"] < -max_loss):
        triggers.append({"trigger": "RISK", "severity": "action",
                         "detail": f"current volatility {cs['volatility_pct']}% vs target {ts['volatility_pct']}%; "
                                   f"bad year {cs['bad_year_pct']}% vs limit -{max_loss}%"})
    from wealth import holdings as H
    summ = H.summary(conn, owner)
    br = summ["concentration"]["single_stock_breaches"]
    if br:
        triggers.append({"trigger": "CONCENTRATION", "severity": "action",
                         "detail": ", ".join(f"{b['symbol']} {b['weight_pct']}%" for b in br)})
    if summ["holdings_at_risk"]:
        triggers.append({"trigger": "SCORES", "severity": "review",
                         "detail": ", ".join(f"{h['symbol']} (CRI {h['cri']}, {h['signal']})"
                                             for h in summ["holdings_at_risk"])})
    verdict = "NO_ACTION" if not total else "REBALANCE" if any(t["severity"] == "action" for t in triggers) \
        else "REVIEW" if triggers else "NO_ACTION"
    return {"methodology_version": METHODOLOGY_VERSION, "as_of": str(C.now())[:16],
            "investable_value": round(total, 2), "net_worth_assets": round(gross, 2),
            "target_source": tgt["source"], "target_run_id": tgt.get("run_id"), "target": target,
            "current": {k: round(v, 2) for k, v in cur.items()}, "drift": drift, "triggers": triggers,
            "verdict": verdict, "last_accepted": last,
            "stats": {"current": cs, "target": ts}, "bands": {"abs_pct": settings()["rebalance_abs_band_pct"],
                                                              "rel_pct": settings()["rebalance_rel_band_pct"]},
            "tax_note": TAX_NOTE, "_positions": inv, "_summary": summ}


def _class_legs(chk, mode, new_cash):
    total = chk["investable_value"]
    new_total = total + new_cash
    legs = {}
    for d in chk["drift"]:
        k = d["class"]
        cur_v = d["current_value"]
        if mode == "to_target":
            want = new_total * d["target_pct"] / 100
        elif mode == "to_band":
            lo, hi = d["band"]
            cur_pct_new = cur_v / new_total * 100 if new_total else 0
            if cur_pct_new > hi:
                want = new_total * hi / 100
            elif cur_pct_new < lo:
                want = new_total * lo / 100
            else:
                want = cur_v
        else:
            want = cur_v
        legs[k] = want - cur_v
    if mode == "cash_flow":
        under = {d["class"]: max(0.0, new_total * d["target_pct"] / 100 - d["current_value"]) for d in chk["drift"]}
        need = sum(under.values())
        for k in legs:
            legs[k] = new_cash * under[k] / need if need else 0.0
    elif mode == "to_band":
        # to_band legs need not net to new_cash: settle the difference in the class
        # closest to its target on the side that absorbs it
        net = sum(legs.values()) - new_cash
        if abs(net) > 1:
            side = [d for d in chk["drift"] if not d["out_of_band"]] or chk["drift"]
            k = min(side, key=lambda d: abs(d["drift_pp"]))["class"]
            legs[k] -= net
    return legs


def _price(conn, symbol):
    from wealth import holdings as H
    return H.market_price(conn, symbol)


def _sell_candidates(pos_in_class, summ):
    br = {b["symbol"] for b in summ["concentration"]["single_stock_breaches"]}
    risk = {h["symbol"] for h in summ["holdings_at_risk"]}

    def key(p):
        a = p.get("atip") or {}
        return (0 if p["symbol"] in br else 1, 0 if p["symbol"] in risk else 1,
                a.get("atip_score") if a.get("atip_score") is not None else 50, -p["value"])
    return sorted([p for p in pos_in_class if p["value"] > 0], key=key)


def _reason(p, summ):
    if p["asset_class"] == "CASH":
        return "surplus cash above target funds the purchases"
    br = {b["symbol"]: b for b in summ["concentration"]["single_stock_breaches"]}
    risk = {h["symbol"]: h for h in summ["holdings_at_risk"]}
    if p["symbol"] in br:
        return f"above the single-stock cap ({br[p['symbol']]['weight_pct']}% of net worth)"
    if p["symbol"] in risk:
        h = risk[p["symbol"]]
        return f"ATIP flags it (CRI {h['cri']}, signal {h['signal']})"
    a = p.get("atip") or {}
    if a.get("atip_score") is not None:
        return f"lower ATIP score ({a['atip_score']:.0f}) in an overweight class"
    return "largest position in an overweight class"


def plan(conn, owner, mode: str = "to_band", new_cash: float = 0.0, store: bool = True, actor: str = "owner") -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    new_cash = C.num(new_cash, "new_cash", 0, 1e12, required=False) or 0.0
    if mode == "cash_flow" and new_cash <= 0:
        raise ValueError("cash_flow mode needs new_cash > 0")
    cfg = settings()
    chk = check(conn, owner)
    pos, summ = chk.pop("_positions"), chk.pop("_summary")
    if not chk["investable_value"] and not new_cash:
        raise ValueError("nothing to rebalance: no investable holdings")
    legs = _class_legs(chk, mode, new_cash)
    stock_cm, etf_cm = _cost_models()
    trades, skipped = [], []
    min_trade = cfg["rebalance_min_trade"]
    for k in AL.CLASSES:
        amt = legs[k]
        if abs(amt) < min_trade:
            if abs(amt) >= 1:
                skipped.append({"class": k, "amount": round(amt, 2), "reason": f"below minimum trade Rs {min_trade:,.0f}"})
            continue
        in_class = [p for p in pos if p["asset_class"] == k]
        if amt < 0:
            left = -amt
            for p in _sell_candidates(in_class, summ):
                if left < min_trade:
                    break
                take = min(left, p["value"])
                listed = p["symbol"] and p["price"] and p["source"] in ("BROKER", "MANUAL") and \
                    p["instrument"] in ("STOCK", "ETF") and p.get("price_source") in ("live_quote", "prices_daily",
                                                                                          "broker_reported")
                if listed:
                    qty = min(int(p["quantity"]), int(math.floor(take / p["price"])))
                    if qty <= 0 or qty * p["price"] < min_trade:
                        continue
                    val = qty * p["price"]
                    trades.append({"side": "SELL", "class": k, "symbol": p["symbol"], "name": p["name"],
                                   "instrument": p["instrument"], "quantity": qty, "price": p["price"],
                                   "value": round(val, 2), "source": p["source"], "reason": _reason(p, summ)})
                else:
                    val = take
                    trades.append({"side": "REDUCE", "class": k, "symbol": p["symbol"], "name": p["name"],
                                   "instrument": p["instrument"], "quantity": None, "price": None,
                                   "value": round(val, 2), "source": p["source"],
                                   "reason": _reason(p, summ) + " (manual asset: reduce by this amount)"})
                left -= val
            if left >= min_trade:
                skipped.append({"class": k, "amount": round(-left, 2),
                                "reason": "not enough sellable holdings in the class"})
        else:
            best = None
            for p in in_class:
                a = p.get("atip") or {}
                if p["symbol"] and p["price"] and a.get("signal") == "BUY" and \
                        (best is None or (a.get("atip_score") or 0) > (best.get("atip") or {}).get("atip_score", 0)):
                    best = p
            if best:
                sym, name, px, inst = best["symbol"], best["name"], best["price"], best["instrument"]
                why = f"existing holding with ATIP BUY signal (score {best['atip']['atip_score']:.0f})"
            else:
                sym = VEHICLES[k]
                m = _price(conn, sym)
                px, name, inst = m["price"], sym, "ETF"
                why = f"illustrative {k.replace('_', ' ').lower()} vehicle (index ETF); any equivalent works"
            if k == "CASH" and not best:
                trades.append({"side": "ADD", "class": k, "symbol": sym, "name": "Cash / liquid (bank or liquid ETF)",
                               "instrument": "CASH", "quantity": None, "price": None, "value": round(amt, 2),
                               "source": "SUGGESTED", "reason": "raise cash to target"})
                continue
            if not px:
                trades.append({"side": "BUY", "class": k, "symbol": sym, "name": name, "instrument": inst,
                               "quantity": None, "price": None, "value": round(amt, 2), "source": "SUGGESTED",
                               "reason": why + " (no ATIP price: amount only)"})
                continue
            qty = int(math.floor(amt / px))
            if qty <= 0:
                continue
            trades.append({"side": "BUY", "class": k, "symbol": sym, "name": name, "instrument": inst,
                           "quantity": qty, "price": px, "value": round(qty * px, 2),
                           "source": "HOLDING" if best else "SUGGESTED", "reason": why})
    # costs
    turnover = cost_total = 0.0
    for t in trades:
        if t["side"] in ("BUY", "SELL") and t["quantity"]:
            cm = etf_cm if t["instrument"] == "ETF" else stock_cm
            ch = cm.charges(t["side"], t["value"])
            slip = t["value"] * SLIPPAGE_BPS / 10000
            t["costs"] = {**{k: v for k, v in ch.items() if v}, "slippage_est": round(slip, 2),
                          "total": round(ch["total"] + slip, 2)}
            cost_total += ch["total"] + slip
        else:
            t["costs"] = {"total": 0.0, "note": "manual / cash leg: costs not modelled"}
        turnover += t["value"]
    # projected weights after the plan
    vals = {d["class"]: d["current_value"] for d in chk["drift"]}
    for t in trades:
        vals[t["class"]] += t["value"] if t["side"] in ("BUY", "ADD") else -t["value"]
    unallocated = new_cash - sum(t["value"] for t in trades if t["side"] in ("BUY", "ADD")) + \
        sum(t["value"] for t in trades if t["side"] in ("SELL", "REDUCE"))
    vals["CASH"] += max(0.0, unallocated)
    tot = sum(vals.values())
    after = {k: round(v / tot * 100, 2) if tot else 0.0 for k, v in vals.items()}
    res = {"methodology_version": METHODOLOGY_VERSION, "mode": mode, "new_cash": new_cash, "check": chk,
           "trades": trades, "skipped": skipped,
           "summary": {"legs": len(trades), "turnover": round(turnover, 2),
                       "turnover_pct": round(turnover / chk["investable_value"] * 100, 2) if chk["investable_value"]
                       else None, "estimated_costs": round(cost_total, 2),
                       "cost_pct_of_turnover": round(cost_total / turnover * 100, 3) if turnover else None,
                       "residual_cash": round(max(0.0, unallocated), 2)},
           "after": {"weights": after,
                     "max_drift_pp": round(max(abs(after[k] - chk["target"].get(k, 0)) for k in AL.CLASSES), 2),
                     "stats": AL.portfolio_stats(after)},
           "tax_note": TAX_NOTE, "disclaimer": C.DISCLAIMER,
           "execution_note": "Advisory only: ATIP places no order from a rebalance plan. Use your broker, or ATIP's "
                             "own order tools, if you decide to act."}
    if store:
        pid = C.new_id("rbl")
        conn.execute("INSERT INTO wealth_rebalance_plan (plan_id,tenant_id,owner_id,created_at,created_by,mode,new_cash,"
                     "target_run_id,verdict,status,result_json,methodology_version) VALUES (?,?,?,?,?,?,?,?,?,"
                     "'PROPOSED',?,?)",
                     (pid, owner["tenant_id"], owner["owner_id"], C.now(), actor, mode, new_cash, chk["target_run_id"],
                      chk["verdict"], C.dumps(res), METHODOLOGY_VERSION))
        conn.commit()
        res = {"plan_id": pid, "status": "PROPOSED", **res}
    return res


def check_public(conn, owner) -> dict:
    d = check(conn, owner)
    d.pop("_positions", None)
    d.pop("_summary", None)
    return d


def plans(conn, owner, limit=50) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT plan_id, created_at, created_by, mode, new_cash, target_run_id, verdict, status, decided_at, "
        "decided_by, decision_note FROM wealth_rebalance_plan WHERE tenant_id=? AND owner_id=? ORDER BY created_at "
        "DESC LIMIT ?", (owner["tenant_id"], owner["owner_id"], int(limit)))]


def get_plan(conn, owner, pid) -> dict:
    r = conn.execute("SELECT * FROM wealth_rebalance_plan WHERE plan_id=? AND tenant_id=? AND owner_id=?",
                     (pid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("plan not found")
    d = dict(r)
    d.update(C.loads(d.pop("result_json"), {}))
    return d


def decide(conn, owner, pid, decision: str, note: str | None = None, actor="owner") -> dict:
    dec = str(decision or "").upper()
    if dec not in DECISIONS:
        raise ValueError(f"decision must be one of {list(DECISIONS)}")
    cur = get_plan(conn, owner, pid)
    if cur["status"] != "PROPOSED" and not (cur["status"] == "ACCEPTED" and dec == "EXECUTED_MANUALLY"):
        raise ValueError(f"plan is already {cur['status']}")
    conn.execute("UPDATE wealth_rebalance_plan SET status=?, decided_at=?, decided_by=?, decision_note=? WHERE plan_id=? "
                 "AND tenant_id=? AND owner_id=?", (dec, C.now(), actor, C.text(note, "note", 500, required=False), pid,
                                                    owner["tenant_id"], owner["owner_id"]))
    conn.commit()
    return get_plan(conn, owner, pid)
