"""
W39 (OP-01..OP-05) — options strategy builder: the analysis Sensibull, Dhan Options Trader
and Streak's options tool give a retail trader, on ATIP's own option-chain data.

    legs        [{"kind": "CE"|"PE"|"FUT", "side": "BUY"|"SELL", "strike", "expiry" (YYYY-MM-DD),
                  "lots", "premium" (entry price per unit), "iv" (decimal, optional)}]
    templates   TEMPLATES: long/short call & put, bull/bear call & put spreads, long/short
                straddle & strangle, iron condor, iron butterfly, call butterfly, covered call,
                protective put, call ratio spread -- built around the ATM strike
    price_legs  fills missing premium / IV / lot size from the latest option_chain_snapshot
                (mid of bid/ask, else LTP) or the F&O bhavcopy (settle / close)
    analyse     payoff at the first expiry and at a chosen earlier date (Black-Scholes for legs
                still alive), breakevens, max profit / max loss (flagging unlimited),
                net premium, net Greeks, probability of profit (lognormal, the legs' average IV),
                reward / risk

Analysis only: nothing here places an order, and margin (SPAN + exposure) is not computed --
use the broker's margin calculator before trading. Index and stock options on NSE are
European-style, so Black-Scholes applies (quant/derivatives.py).
"""

from __future__ import annotations

import math
from datetime import date, datetime

from quant.derivatives import bs_price, greeks, implied_vol

RISK_FREE = 0.065
DEFAULT_IV = 0.18
INDEX_STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100, "BANKEX": 100}


class StrategyError(ValueError):
    pass


def _d(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def strike_step(symbol: str, spot: float, strikes=None) -> float:
    if strikes:
        s = sorted(set(float(x) for x in strikes))
        diffs = [b - a for a, b in zip(s, s[1:]) if b - a > 0]
        if diffs:
            return min(diffs)
    if symbol and symbol.upper() in INDEX_STEPS:
        return INDEX_STEPS[symbol.upper()]
    raw = spot * 0.01
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return min((m * mag for m in (1, 2, 2.5, 5, 10)), key=lambda x: abs(x - raw))


def atm(spot: float, step: float) -> float:
    return round(round(spot / step) * step, 2)


def _leg(kind, side, strike, expiry, lots=1):
    return {"kind": kind, "side": side, "strike": strike, "expiry": str(expiry), "lots": lots}


def _t(name, desc, view, fn):
    return {"name": name, "description": desc, "view": view, "build": fn}


TEMPLATES = {
    "long_call": _t("Long call", "Buy an ATM call", "bullish", lambda a, w, e: [_leg("CE", "BUY", a, e)]),
    "long_put": _t("Long put", "Buy an ATM put", "bearish", lambda a, w, e: [_leg("PE", "BUY", a, e)]),
    "short_call": _t("Short call", "Sell an OTM call", "neutral-bearish", lambda a, w, e: [_leg("CE", "SELL", a + w, e)]),
    "short_put": _t("Short put", "Sell an OTM put", "neutral-bullish", lambda a, w, e: [_leg("PE", "SELL", a - w, e)]),
    "bull_call_spread": _t("Bull call spread", "Buy ATM call, sell OTM call", "bullish",
                           lambda a, w, e: [_leg("CE", "BUY", a, e), _leg("CE", "SELL", a + w, e)]),
    "bear_put_spread": _t("Bear put spread", "Buy ATM put, sell OTM put", "bearish",
                          lambda a, w, e: [_leg("PE", "BUY", a, e), _leg("PE", "SELL", a - w, e)]),
    "bull_put_spread": _t("Bull put spread", "Sell ATM put, buy lower put (credit)", "bullish",
                          lambda a, w, e: [_leg("PE", "SELL", a, e), _leg("PE", "BUY", a - w, e)]),
    "bear_call_spread": _t("Bear call spread", "Sell ATM call, buy higher call (credit)", "bearish",
                           lambda a, w, e: [_leg("CE", "SELL", a, e), _leg("CE", "BUY", a + w, e)]),
    "long_straddle": _t("Long straddle", "Buy ATM call and put", "big move",
                        lambda a, w, e: [_leg("CE", "BUY", a, e), _leg("PE", "BUY", a, e)]),
    "short_straddle": _t("Short straddle", "Sell ATM call and put (unlimited risk)", "range-bound",
                         lambda a, w, e: [_leg("CE", "SELL", a, e), _leg("PE", "SELL", a, e)]),
    "long_strangle": _t("Long strangle", "Buy OTM call and put", "big move",
                        lambda a, w, e: [_leg("CE", "BUY", a + w, e), _leg("PE", "BUY", a - w, e)]),
    "short_strangle": _t("Short strangle", "Sell OTM call and put (unlimited risk)", "range-bound",
                         lambda a, w, e: [_leg("CE", "SELL", a + w, e), _leg("PE", "SELL", a - w, e)]),
    "iron_condor": _t("Iron condor", "Short strangle with long wings (defined risk)", "range-bound",
                      lambda a, w, e: [_leg("PE", "BUY", a - 2 * w, e), _leg("PE", "SELL", a - w, e),
                                       _leg("CE", "SELL", a + w, e), _leg("CE", "BUY", a + 2 * w, e)]),
    "iron_butterfly": _t("Iron butterfly", "Short straddle with long wings (defined risk)", "range-bound",
                         lambda a, w, e: [_leg("PE", "BUY", a - w, e), _leg("PE", "SELL", a, e),
                                          _leg("CE", "SELL", a, e), _leg("CE", "BUY", a + w, e)]),
    "long_call_butterfly": _t("Long call butterfly", "Buy 1 lower, sell 2 ATM, buy 1 higher call", "pin at ATM",
                              lambda a, w, e: [_leg("CE", "BUY", a - w, e), _leg("CE", "SELL", a, e, 2),
                                               _leg("CE", "BUY", a + w, e)]),
    "covered_call": _t("Covered call", "Long future, sell OTM call", "mildly bullish",
                       lambda a, w, e: [_leg("FUT", "BUY", None, e), _leg("CE", "SELL", a + w, e)]),
    "protective_put": _t("Protective put", "Long future, buy ATM put", "bullish, hedged",
                         lambda a, w, e: [_leg("FUT", "BUY", None, e), _leg("PE", "BUY", a, e)]),
    "call_ratio_spread": _t("Call ratio spread", "Buy 1 ATM call, sell 2 OTM calls (unlimited risk up)",
                            "mildly bullish", lambda a, w, e: [_leg("CE", "BUY", a, e), _leg("CE", "SELL", a + w, e, 2)]),
}


def build(template: str, spot: float, expiry, *, symbol: str = "", step: float | None = None,
          width_steps: int = 2, strikes=None) -> list:
    """Legs of a template around the ATM strike; wings `width_steps` strikes away."""
    if template not in TEMPLATES:
        raise StrategyError(f"unknown template {template!r}; one of {', '.join(TEMPLATES)}")
    if not spot or spot <= 0:
        raise StrategyError("spot must be positive")
    step = step or strike_step(symbol, spot, strikes)
    a = atm(spot, step)
    return TEMPLATES[template]["build"](a, step * max(1, int(width_steps)), str(_d(expiry)))


# ── pricing from stored chains ───────────────────────────────────────────────

def chain_quotes(conn, symbol: str) -> dict:
    """{(expiry, strike, CE|PE): {price, iv, source}}, the latest chain snapshot first, then the bhavcopy."""
    sym = symbol.upper()
    out, lot, spot = {}, None, None
    try:
        ts = conn.execute("SELECT MAX(ts) FROM option_chain_snapshot WHERE symbol=?", (sym,)).fetchone()[0]
        if ts:
            for r in conn.execute("SELECT expiry, strike, option_type, ltp, bid, ask, iv, underlying "
                                  "FROM option_chain_snapshot WHERE symbol=? AND ts=?", (sym, ts)):
                bid, ask, ltp = r[4], r[5], r[3]
                px = (bid + ask) / 2 if bid and ask and ask >= bid > 0 else ltp
                iv = r[6] / 100.0 if r[6] and r[6] > 3 else r[6]
                if px:
                    out[(str(r[0])[:10], float(r[1]), r[2])] = {"price": round(float(px), 2), "iv": iv,
                                                                "source": f"option chain {str(ts)[:16]}"}
                spot = spot or r[7]
    except Exception:
        pass
    try:
        d = conn.execute("SELECT MAX(date) FROM fo_contract_daily WHERE symbol=?", (sym,)).fetchone()[0]
        if d:
            for r in conn.execute("SELECT expiry, strike, option_type, settle, close, iv, lot_size, underlying, "
                                  "instrument FROM fo_contract_daily WHERE symbol=? AND date=?", (sym, d)):
                lot = lot or r[6]
                spot = spot or r[7]
                key = (str(r[0])[:10], float(r[1]), r[2] if r[2] in ("CE", "PE") else "FUT")
                px = r[3] or r[4]
                if px and key not in out:
                    iv = r[5] / 100.0 if r[5] and r[5] > 3 else r[5]
                    out[key] = {"price": round(float(px), 2), "iv": iv, "source": f"F&O bhavcopy {str(d)[:10]}"}
    except Exception:
        pass
    return {"quotes": out, "lot_size": lot, "spot": spot}


def price_legs(legs: list, quotes: dict) -> list:
    """Legs with premium / iv filled where the caller left them out; `price_source` says where from."""
    out = []
    for leg in legs:
        leg = dict(leg)
        key = (str(leg.get("expiry"))[:10], float(leg["strike"]) if leg.get("strike") is not None else 0.0,
               leg["kind"] if leg["kind"] in ("CE", "PE") else "FUT")
        q = quotes.get(key) if quotes else None
        if leg.get("premium") in (None, "") and q:
            leg["premium"], leg["price_source"] = q["price"], q["source"]
        if leg.get("iv") in (None, "") and q and q.get("iv"):
            leg["iv"] = q["iv"]
        out.append(leg)
    return out


# ── analysis ─────────────────────────────────────────────────────────────────

def _validate(legs: list, spot: float) -> list:
    if not legs:
        raise StrategyError("a strategy needs at least one leg")
    if len(legs) > 12:
        raise StrategyError("at most 12 legs")
    out = []
    for i, leg in enumerate(legs):
        kind = str(leg.get("kind", "")).upper()
        side = str(leg.get("side", "")).upper()
        if kind not in ("CE", "PE", "FUT"):
            raise StrategyError(f"leg {i + 1}: kind must be CE, PE or FUT")
        if side not in ("BUY", "SELL"):
            raise StrategyError(f"leg {i + 1}: side must be BUY or SELL")
        strike = leg.get("strike")
        if kind != "FUT":
            try:
                strike = float(strike)
            except (TypeError, ValueError):
                raise StrategyError(f"leg {i + 1}: strike is required for an option")
            if strike <= 0:
                raise StrategyError(f"leg {i + 1}: strike must be positive")
        lots = int(leg.get("lots") or 1)
        if lots < 1:
            raise StrategyError(f"leg {i + 1}: lots must be at least 1")
        prem = leg.get("premium")
        if prem in (None, ""):
            prem = spot if kind == "FUT" else None
        if prem is None:
            raise StrategyError(f"leg {i + 1}: no premium given and none in ATIP's option chain")
        iv = leg.get("iv")
        iv = float(iv) if iv not in (None, "") else None
        if iv is not None and iv > 3:
            iv /= 100.0
        out.append({**leg, "kind": kind, "side": side, "strike": strike, "lots": lots, "premium": float(prem),
                    "iv": iv, "expiry": str(_d(leg["expiry"])), "sign": 1 if side == "BUY" else -1})
    return out


def _years(expiry: str, on: date) -> float:
    return max(0.0, ((_d(expiry) - on).days) / 365.0)


def _leg_value(leg, S, on: date, r: float) -> float:
    """Value per unit of one leg at underlying S on date `on`."""
    if leg["kind"] == "FUT":
        return S
    T = _years(leg["expiry"], on)
    if T <= 0:
        return max(0.0, S - leg["strike"]) if leg["kind"] == "CE" else max(0.0, leg["strike"] - S)
    return bs_price(S, leg["strike"], T, r, leg["iv"] or DEFAULT_IV, "call" if leg["kind"] == "CE" else "put")


def pnl_at(legs, S, on: date, lot_size: int, r: float = RISK_FREE) -> float:
    return sum(l["sign"] * l["lots"] * lot_size * (_leg_value(l, S, on, r) - l["premium"]) for l in legs)


def _breakevens(xs, ys) -> list:
    out = []
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if y0 == 0:
            out.append(round(x0, 2))
        elif (y0 < 0 < y1) or (y0 > 0 > y1):
            out.append(round(x0 + (x1 - x0) * (-y0) / (y1 - y0), 2))
    return sorted(set(out))


def analyse(legs: list, spot: float, *, lot_size: int = 1, as_of=None, target_date=None, r: float = RISK_FREE,
            range_pct: float | None = None, points: int = 161) -> dict:
    """Everything the payoff screen shows, for legs priced in rupees per unit."""
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        raise StrategyError("spot is required")
    if spot <= 0:
        raise StrategyError("spot must be positive")
    lot_size = int(lot_size or 1)
    if lot_size < 1:
        raise StrategyError("lot size must be at least 1")
    legs = _validate(legs, spot)
    today = _d(as_of) if as_of else date.today()
    for leg in legs:                                   # missing IV: implied from the premium, else the default
        if leg["kind"] != "FUT" and leg["iv"] is None:
            T = _years(leg["expiry"], today)
            iv = implied_vol(leg["premium"], spot, leg["strike"], T, r, "call" if leg["kind"] == "CE" else "put") \
                if T > 0 else None
            leg["iv"], leg["iv_source"] = (iv, "implied from premium") if iv else (DEFAULT_IV, "default")
    first_exp = min(_d(l["expiry"]) for l in legs)
    if first_exp < today:
        raise StrategyError("a leg has already expired")
    tdate = _d(target_date) if target_date else today
    tdate = min(max(tdate, today), first_exp)

    strikes = [l["strike"] for l in legs if l["kind"] != "FUT"]
    if range_pct is None:                              # +-4 sd of the move to expiry, 3%..30%, and every strike
        ivs = [l["iv"] for l in legs if l["kind"] != "FUT" and l["iv"]] or [DEFAULT_IV]
        sd = sum(ivs) / len(ivs) * math.sqrt(max(_years(str(first_exp), today), 1 / 365))
        range_pct = min(30.0, max(3.0, 400 * sd))
    span = max([abs(k / spot - 1) * 100 for k in strikes] or [0])
    range_pct = max(range_pct, span * 1.5)
    lo = spot * (1 - range_pct / 100)
    hi = spot * (1 + range_pct / 100)
    lo = max(lo, 0.01)
    xs = sorted(set([lo + (hi - lo) * i / (points - 1) for i in range(points)] + strikes + [spot]))
    expiry_pnl = [pnl_at(legs, x, first_exp, lot_size, r) for x in xs]
    target_pnl = [pnl_at(legs, x, tdate, lot_size, r) for x in xs]

    # Tails: beyond the strikes the expiry payoff is linear; its slope says whether profit / loss is unbounded.
    # (a call alive past the first expiry is deep in the money up there, so it slopes like a future)
    up_slope = sum(l["sign"] * l["lots"] * lot_size for l in legs if l["kind"] in ("FUT", "CE"))
    at_zero = pnl_at(legs, 1e-9, first_exp, lot_size, r)
    finite = expiry_pnl + [at_zero]
    max_profit = None if up_slope > 0 else round(max(finite), 2)
    max_loss = None if up_slope < 0 else round(min(finite), 2)

    net_premium = sum(-l["sign"] * l["lots"] * lot_size * l["premium"] for l in legs if l["kind"] != "FUT")
    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in legs:
        q = l["sign"] * l["lots"] * lot_size
        if l["kind"] == "FUT":
            net["delta"] += q
            continue
        T = _years(l["expiry"], today)
        g = greeks(spot, l["strike"], T, r, l["iv"], "call" if l["kind"] == "CE" else "put") if T > 0 else {}
        for k in net:
            net[k] += q * (g.get(k) or 0.0)
    pop = probability_of_profit(legs, spot, first_exp, today, lot_size, r)
    bes = _breakevens(xs, expiry_pnl)
    rr = round(max_profit / -max_loss, 2) if (max_profit is not None and max_loss is not None and max_loss < 0) else None
    return {"spot": spot, "lot_size": lot_size, "as_of": str(today), "first_expiry": str(first_exp),
            "target_date": str(tdate), "legs": [{k: v for k, v in l.items() if k != "sign"} for l in legs],
            "net_premium": round(net_premium, 2), "premium_type": "credit" if net_premium > 0 else "debit",
            "max_profit": max_profit, "max_loss": max_loss, "unlimited_profit": max_profit is None,
            "unlimited_loss": max_loss is None, "breakevens": bes, "reward_to_risk": rr,
            "probability_of_profit_pct": pop, "greeks": {k: round(v, 4) for k, v in net.items()},
            "payoff": {"spot": [round(x, 2) for x in xs], "expiry": [round(y, 2) for y in expiry_pnl],
                       "target": [round(y, 2) for y in target_pnl]},
            "notes": ["Margin (SPAN + exposure) is not computed: check the broker's margin calculator.",
                      "Analysis only; nothing is ordered."]}


def probability_of_profit(legs, spot, first_exp: date, today: date, lot_size: int, r: float = RISK_FREE):
    """P(expiry P&L > 0) for a lognormal underlying with the legs' average IV (risk-neutral drift)."""
    T = _years(str(first_exp), today)
    ivs = [l["iv"] for l in legs if l["kind"] != "FUT" and l["iv"]]
    sigma = sum(ivs) / len(ivs) if ivs else DEFAULT_IV
    if T <= 0:
        return 100.0 if pnl_at(legs, spot, first_exp, lot_size, r) > 0 else 0.0
    sd = sigma * math.sqrt(T)
    mu = math.log(spot) + (r - 0.5 * sigma * sigma) * T
    n = 800
    p = 0.0
    for i in range(n):                                   # midpoint rule over +-5 sd in log space
        z0, z1 = -5 + 10 * i / n, -5 + 10 * (i + 1) / n
        s_mid = math.exp(mu + sd * (z0 + z1) / 2)
        if pnl_at(legs, s_mid, first_exp, lot_size, r) > 0:
            p += 0.5 * (math.erf(z1 / math.sqrt(2)) - math.erf(z0 / math.sqrt(2)))
    return round(p * 100, 1)


def templates() -> list:
    return [{"key": k, "name": t["name"], "description": t["description"], "view": t["view"]}
            for k, t in TEMPLATES.items()]
