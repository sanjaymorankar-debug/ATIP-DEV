"""
Model and executable returns (PERF-001-02, -03) and signal / position attribution
(PERF-001-07, -09).

SIGNALS: signal_log rows with signal BUY, not a re-run duplicate (duplicate_of IS
NULL), first row per (symbol, signal_date), signal_date inside the period. A
signal for a symbol the model already holds is a REPEAT (counted, not re-entered).

MODEL RETURN (reproducible from signal_log + prices_daily alone)
    entry  the signal's own entry_price (that session's close), on today's basis
    exit   the close `horizon` sessions later (default 20, the accuracy tracker's
           longest horizon), or the next SELL signal for the symbol, whichever comes
           first; still open at the period end -> marked at the last close
    gross  no costs, no slippage, unlimited liquidity
    series equal weight across open model positions, close-to-close

EXECUTABLE RETURN (what an account could actually have done)
    entry  the next session's OPEN (the signal is only known after the close) x
           (1 + slippage_bps)
    exit   the session after the model exit, at its open x (1 - slippage_bps)
    costs  NSE delivery charges (backtest/costs.py) on a `notional` position each side
    liquidity  skipped (NOT_EXECUTABLE) when the notional exceeds max_adv_pct of the
           20-session average traded value, or there is no next-session bar
    series equal weight, with entry / exit costs taken on those days

SIGNAL ATTRIBUTION (per signal, against an actual portfolio's ledger)
    entry     first BUY in the portfolio for the symbol within link_window sessions
              on / after the signal date, not already linked to an earlier signal
    add-ons   further BUYs while that position is open
    exits     SELLs until the position is flat
    outcome   actual P&L (realized + unrealized) and return vs model and executable, and
              the signal_outcome momentum hits (3 / 6 / 8%)
    Buys not linked to any signal are DISCRETIONARY; the report splits actual P&L into
    signal-driven and discretionary.

POSITION ATTRIBUTION (per linked signal, in rupees on the actual quantity Q)
    entry_timing   Q x (model entry - first fill)          bought cheaper / dearer than the model
    averaging      Q x first fill - sum(q_i x p_i)          effect of add-ons
    exit_timing    sell proceeds + open value - Q x model exit
    costs          - fees
    The four add up exactly to actual P&L - model P&L on the same quantity.
    sizing         actual capital deployed vs the model notional, and the P&L the model
                   return earns on each
"""

from __future__ import annotations

from datetime import date

from wealth.perf import data as D
from wealth.perf import metrics as M

DEFAULTS = {"horizon": 20, "slippage_bps": 10.0, "notional": 100000.0, "max_adv_pct": 5.0, "link_window": 5}


def load_signals(conn, start: date, end: date) -> list:
    # signal_log is created by scores/signal_log.ensure_tables on the first post-market run;
    # a fresh install has none yet, which used to crash the whole report.
    from scores.signal_log import ensure_tables
    ensure_tables(conn)
    rows = conn.execute("SELECT id, run_id, signal_date, symbol, `signal`, entry_price, atip_score, vpi, cri, zpi, regime, "
                        "model_version, logged_at FROM signal_log WHERE duplicate_of IS NULL AND signal_date>=? AND "
                        "signal_date<=? ORDER BY signal_date, logged_at", (str(start), str(end))).fetchall()
    seen, out = set(), []
    for r in rows:
        k = (r["symbol"], str(r["signal_date"])[:10], r["signal"])
        if k in seen:
            continue
        seen.add(k)
        d = dict(r)
        d["signal_date"] = D._d(d["signal_date"])
        out.append(d)
    return out


def _cost_model():
    from backtest.costs import cost_model, cost_model_from_config
    try:
        return cost_model_from_config()
    except Exception:
        return cost_model("nse_delivery")


def build(conn, start: date, end: date, prices: D.Prices, opts: dict) -> dict:
    o = {**DEFAULTS, **{k: v for k, v in (opts or {}).items() if k in DEFAULTS and v is not None}}
    sigs = load_signals(conn, start, end)
    sells = {}
    for s in sigs:
        if s["signal"] in ("SELL", "EXIT"):
            sells.setdefault(s["symbol"], []).append(s["signal_date"])
    cal = D.calendar(conn, start, end)
    fcache = {}
    cm = _cost_model()
    model_trades, exec_trades, repeats = [], [], 0
    open_until = {}
    for s in sigs:
        if s["signal"] != "BUY":
            continue
        sym, sd = s["symbol"], s["signal_date"]
        if sym in open_until and open_until[sym] >= sd:
            repeats += 1
            continue
        f = D.basis_factor(conn, sym, sd, fcache)
        entry = float(s["entry_price"]) * f if s["entry_price"] else prices.close(sym, sd, count_gap=False)
        if not entry:
            continue
        hz = prices.nth_session_after(sym, sd, int(o["horizon"]))
        exit_d, exit_px, reason = None, None, None
        nxt_sell = min([d for d in sells.get(sym, []) if d > sd], default=None)
        if hz and hz[0] <= end:
            exit_d, exit_px, reason = hz[0], hz[2], f"{int(o['horizon'])}-session horizon"
        if nxt_sell and (exit_d is None or nxt_sell < exit_d) and nxt_sell <= end:
            b = prices.bar(sym, nxt_sell)
            if b:
                exit_d, exit_px, reason = nxt_sell, b[1], "SELL signal"
        still_open = exit_d is None
        if still_open:
            last = min(end, prices.last_date(sym) or end)
            exit_d, exit_px, reason = last, prices.close(sym, last, count_gap=False), "open at period end (marked)"
        open_until[sym] = exit_d
        mt = {"signal_id": s["id"], "symbol": sym, "signal_date": str(sd), "entry_date": str(sd),
              "entry": round(entry, 4), "exit_date": str(exit_d), "exit": round(exit_px, 4) if exit_px else None,
              "exit_reason": reason, "open": still_open, "basis_factor": f,
              "ret": (exit_px / entry - 1) if exit_px else None, "days": (exit_d - sd).days,
              "scores": {"atip": s["atip_score"], "vpi": s["vpi"], "cri": s["cri"], "zpi": s["zpi"],
                         "regime": s["regime"]}, "model_version": s["model_version"]}
        mt["pnl"] = round(o["notional"] * mt["ret"], 2) if mt["ret"] is not None else None
        model_trades.append(mt)

        # executable
        nb = prices.next_session(sym, sd)
        et = {"signal_id": s["id"], "symbol": sym, "signal_date": str(sd), "status": "EXECUTABLE"}
        if not nb or nb[0] > end:
            et.update({"status": "NOT_EXECUTABLE", "reason": "no next-session bar in the period"})
            exec_trades.append(et)
            continue
        adv = prices.adv_value(sym, sd)
        if adv is not None and o["notional"] > adv * o["max_adv_pct"] / 100:
            et.update({"status": "NOT_EXECUTABLE", "reason": f"notional Rs {o['notional']:,.0f} above "
                       f"{o['max_adv_pct']}% of 20-session average traded value Rs {adv:,.0f}"})
            exec_trades.append(et)
            continue
        e_open = nb[1] or nb[2]
        e_px = e_open * (1 + o["slippage_bps"] / 10000)
        if still_open:
            x_d, x_raw = exit_d, exit_px
            x_reason = "open at period end (marked at close)"
        else:
            xb = prices.next_session(sym, exit_d)
            if xb and xb[0] <= end:
                x_d, x_raw, x_reason = xb[0], (xb[1] or xb[2]), "next-session open after the model exit"
            else:
                x_d, x_raw, x_reason = exit_d, exit_px, "model exit close (no later session in the period)"
        x_px = x_raw * (1 - o["slippage_bps"] / 10000) if x_raw else None
        buy_c = cm.total("BUY", o["notional"])
        qty = o["notional"] / e_px
        sell_c = cm.total("SELL", qty * x_px) if (x_px and not still_open) else 0.0
        pnl = qty * (x_px - e_px) - buy_c - sell_c if x_px else None
        et.update({"entry_date": str(nb[0]), "entry": round(e_px, 4), "exit_date": str(x_d),
                   "exit": round(x_px, 4) if x_px else None, "exit_reason": x_reason, "open": still_open,
                   "costs": round(buy_c + sell_c, 2), "slippage_bps": o["slippage_bps"],
                   "pnl": round(pnl, 2) if pnl is not None else None,
                   "ret": pnl / o["notional"] if pnl is not None else None,
                   "days": (x_d - nb[0]).days, "adv_value": round(adv, 0) if adv else None,
                   "buy_cost_pct": buy_c / o["notional"],
                   "sell_cost_pct": sell_c / (qty * x_px) if (x_px and sell_c) else 0.0})
        exec_trades.append(et)

    m_ser = _series(cal, prices, [(t["symbol"], D._d(t["entry_date"]), t["entry"], D._d(t["exit_date"]), t["exit"],
                                   0.0, 0.0, False) for t in model_trades if t["exit"]])
    ok = [t for t in exec_trades if t["status"] == "EXECUTABLE" and t.get("exit")]
    e_ser = _series(cal, prices, [(t["symbol"], D._d(t["entry_date"]), t["entry"], D._d(t["exit_date"]), t["exit"],
                                   t["buy_cost_pct"], t["sell_cost_pct"], True) for t in ok])
    return {"options": o, "signals": len(sigs), "repeats_ignored": repeats, "calendar": cal,
            "model": {"trades": model_trades, "series": m_ser, "trade_stats": M.trade_stats(
                [t for t in model_trades if t["ret"] is not None])},
            "executable": {"trades": exec_trades, "series": e_ser, "trade_stats": M.trade_stats(
                [t for t in ok if t["ret"] is not None]),
                "not_executable": sum(1 for t in exec_trades if t["status"] == "NOT_EXECUTABLE")}}


def _series(cal, prices, legs):
    """Equal-weight daily returns. leg = (sym, entry_date, entry_px, exit_date, exit_px,
    buy_cost_frac, sell_cost_frac, entry_at_open). A leg entered at the close (model) is
    not invested on its entry session; one entered at the open (executable) earns
    open -> close less the buy cost that session. The exit session earns prev close ->
    exit price less the sell cost."""
    out = []
    for i, d in enumerate(cal):
        prev = cal[i - 1] if i else None
        rs = []
        for sym, ed, ep, xd, xp, bc, sc, at_open in legs:
            if d < ed or d > xd:
                continue
            if d == ed:
                if at_open:
                    c = prices.close(sym, d, count_gap=False)
                    rs.append((c / ep - 1 - bc) if c else -bc)
                continue
            pc = prices.close(sym, prev, count_gap=False) if prev else None
            if not pc:
                continue
            if d == xd:
                rs.append(xp / pc - 1 - sc)
            else:
                c = prices.close(sym, d, count_gap=False)
                rs.append(c / pc - 1 if c else 0.0)
        out.append(sum(rs) / len(rs) if rs else None)
    return out


def link_signals(conn, owner, portfolio, model_trades, txns, prices, window: int) -> dict:
    """Signal lifecycle + position attribution against one actual portfolio.
    txns: basis-converted ledger rows (engine._basis) of that portfolio."""
    buys = [t for t in txns if t["kind"] == "BUY"]
    used = set()
    items = []
    by_sym = {}
    for t in txns:
        if t["symbol"]:
            by_sym.setdefault(t["symbol"], []).append(t)
    for m in model_trades:
        sym, sd = m["symbol"], D._d(m["signal_date"])
        limit = prices.nth_session_after(sym, sd, window)
        last_ok = limit[0] if limit else date.max
        first = next((t for t in buys if t["symbol"] == sym and sd <= t["trade_date"] <= last_ok
                      and t["txn_id"] not in used), None)
        if not first:
            continue
        seq, q, cost_in, fees, proceeds, sold_q = [], 0.0, 0.0, 0.0, 0.0, 0.0
        exits = []
        for t in by_sym[sym]:
            if t["trade_date"] < first["trade_date"] or (t["kind"] == "BUY" and t["txn_id"] != first["txn_id"]
                                                          and t["txn_id"] in used):
                continue
            if t["kind"] == "BUY":
                if q <= 1e-9 and seq:
                    break                      # a new position after this one was closed
                used.add(t["txn_id"])
                seq.append(t)
                q += t["q"]
                cost_in += t["q"] * t["p"]
                fees += t["fees"]
            elif t["kind"] == "SELL" and seq:
                take = min(q, t["q"])
                proceeds += take * t["p"]
                fees += t["fees"] * (take / t["q"] if t["q"] else 1)
                sold_q += take
                q -= take
                exits.append({"date": str(t["trade_date"]), "quantity": round(take, 6), "price": round(t["p"], 4)})
                if q <= 1e-9:
                    break
        Q = sum(t["q"] for t in seq)
        last_px = prices.close(sym, prices.last_date(sym) or date.today(), count_gap=False)
        open_val = q * last_px if (q > 1e-9 and last_px) else 0.0
        actual_pnl = proceeds + open_val - cost_in - fees
        me, mx = m["entry"], m["exit"]
        model_pnl_q = Q * (mx - me) if mx else None
        p1 = seq[0]["p"]
        attr = None
        if model_pnl_q is not None:
            attr = {"entry_timing": round(Q * (me - p1), 2), "averaging": round(Q * p1 - cost_in, 2),
                    "exit_timing": round(proceeds + open_val - Q * mx, 2), "costs": round(-fees, 2)}
            attr["total"] = round(sum(attr.values()), 2)
            attr["check"] = round(actual_pnl - model_pnl_q, 2)
        items.append({
            "signal_id": m["signal_id"], "symbol": sym, "signal_date": m["signal_date"],
            "model": {"entry": me, "exit": mx, "exit_date": m["exit_date"], "ret_pct": _p(m["ret"])},
            "entry": {"date": str(first["trade_date"]), "price": round(p1, 4), "quantity": round(first["q"], 6),
                      "delay_sessions": _sessions(prices, sym, sd, first["trade_date"])},
            "add_ons": [{"date": str(t["trade_date"]), "quantity": round(t["q"], 6), "price": round(t["p"], 4)}
                        for t in seq[1:]],
            "exits": exits, "open_quantity": round(q, 6),
            "actual": {"capital": round(cost_in, 2), "pnl": round(actual_pnl, 2),
                       "ret_pct": _p(actual_pnl / cost_in) if cost_in else None, "fees": round(fees, 2)},
            "position_attribution": attr,
            "sizing": {"actual_capital": round(cost_in, 2), "model_notional": None,
                       "model_pnl_on_actual_size": round(model_pnl_q, 2) if model_pnl_q is not None else None},
        })
    linked_buys = used
    disc = [t for t in buys if t["txn_id"] not in linked_buys]
    return {"linked": items, "discretionary_buys": len(disc),
            "discretionary_symbols": sorted({t["symbol"] for t in disc}), "linked_buys": len(linked_buys)}


def outcomes(conn, signal_ids: list) -> dict:
    if not signal_ids:
        return {}
    out = {}
    ph = ",".join("?" * len(signal_ids))
    try:
        for r in conn.execute(f"SELECT signal_id, threshold_pct, hit, sessions_to_hit, max_favourable_pct, "
                              f"max_adverse_pct FROM signal_outcome WHERE signal_id IN ({ph})", signal_ids):
            out.setdefault(r["signal_id"], []).append(dict(r))
    except Exception:
        pass
    return out


def _sessions(prices, sym, a, b):
    ds = prices._load(sym)[0]
    from bisect import bisect_right
    return max(0, bisect_right(ds, b) - bisect_right(ds, a))


def _p(x):
    return None if x is None else round(x * 100, 3)
