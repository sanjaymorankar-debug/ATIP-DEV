"""
Actual-return engine (PERF-001-04, -06, -08, -10, -14).

run_actual(conn, owner, portfolio, start, end, prices, benchmark_symbol) processes the
ledger of one portfolio and returns:

  positions      per symbol: quantity, average cost, cost basis, market value,
                 realized (average-cost method) and unrealized P&L, fees
  daily          per session: market value, net flow, TWR return, benchmark return
  returns        TWR metrics (series_metrics), XIRR of the actual cash flows, the same
                 flows invested in the benchmark (PME: "what if this money had gone into
                 NIFTY 50"), realized / unrealized totals
  trades         FIFO round trips (holding days, return, P&L) for trade statistics
  costs          fees by kind and symbol, explicit slippage where a reference price
                 exists, and a reconciliation of fees to the ledger (must be exact)
  contribution   gain by symbol, sector and asset class over average invested capital;
                 the parts sum exactly to the portfolio total

CONVENTIONS
  * Everything is converted onto today's share basis first (data.py): qty / F, price x F.
  * The book is a "positions sleeve": money into positions (buy value + fees, OPENING at
    that day's close) is a positive flow, money out (sell value - fees, dividends) a
    negative flow. Cash balances are not modelled, so idle cash neither dilutes nor
    flatters the return.
  * Inflows (buys, fees) count at the start of their session, outflows (sells, dividends)
    at its end, so an intraday round trip earns its P&L that day. A trade on a
    non-trading day moves to the next session; valuation at each session's close,
    carried forward over gaps.
  * OPENING: cost basis = the broker's average cost (for P&L), flow = that day's close
    (for returns), so gains made before the ledger began are never counted as return.
  * A SELL larger than the position is capped at the position and reported in
    data_quality (EXCESS_SELL); nothing goes short.
"""

from __future__ import annotations

from datetime import date

from wealth.perf import data as D
from wealth.perf import metrics as M


def _txns(conn, owner, portfolio, end):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM perf_ledger l WHERE tenant_id=? AND owner_id=? AND portfolio=? AND trade_date<=? AND NOT EXISTS "
        "(SELECT 1 FROM perf_ledger_void v WHERE v.txn_id=l.txn_id) ORDER BY trade_date, ts, txn_id",
        (owner["tenant_id"], owner["owner_id"], portfolio, str(end)))]


def _basis(conn, txns, fcache):
    out = []
    for t in txns:
        t = dict(t)
        t["trade_date"] = D._d(t["trade_date"])
        if t["symbol"] and t["kind"] in ("BUY", "SELL", "OPENING", "DIVIDEND"):
            f = D.basis_factor(conn, t["symbol"], t["trade_date"], fcache)
            t["basis_factor"] = f
            if t["quantity"]:
                t["q"] = float(t["quantity"]) / f
            if t["price"] is not None:
                t["p"] = float(t["price"]) * f
            t["ref"] = float(t["reference_price"]) * f if t["reference_price"] is not None else None
        else:
            t["basis_factor"] = 1.0
        t.setdefault("q", float(t["quantity"] or 0))
        t.setdefault("p", float(t["price"]) if t["price"] is not None else None)
        t.setdefault("ref", float(t["reference_price"]) if t["reference_price"] is not None else None)
        t["fees"] = float(t["fees"] or 0)
        out.append(t)
    return out


def _session_of(cal, d):
    for s in cal:
        if s >= d:
            return s
    return None


def _classify(sym):
    from wealth.assets import classify_symbol
    return classify_symbol(sym)["asset_class"]


def run_actual(conn, owner, portfolio: str, start: date, end: date, prices: D.Prices, bench_sym: str,
               rf_pct: float = 6.5, sectors: dict | None = None) -> dict:
    fcache = {}
    txns = _basis(conn, _txns(conn, owner, portfolio, end), fcache)
    quality = []
    cal = D.calendar(conn, start, end)
    if not cal:
        return {"status": "NO_CALENDAR", "message": f"no NIFTY50 sessions between {start} and {end}"}
    pos = {}                      # sym -> {"q", "cost", "realized", "fees", "div"}
    lots = {}                     # sym -> FIFO [[date, q, unit_cost]]
    trips = []
    fees_by = {"BUY": 0.0, "SELL": 0.0, "FEE": 0.0, "OPENING": 0.0}
    slip = {"total": 0.0, "trades_with_reference": 0, "items": []}
    flows_by_session = {}
    in_by_session, out_by_session = {}, {}
    sym_flows = {}                # sym -> {session: flow}
    xirr_flows = []

    def apply(t, in_period):
        s, k = t["symbol"], t["kind"]
        p = pos.setdefault(s, {"q": 0.0, "cost": 0.0, "realized": 0.0, "fees": 0.0, "div": 0.0}) if s else None
        flow = 0.0
        if k in ("BUY", "OPENING"):
            mkt = t["p"]
            if k == "OPENING":
                mkt = t["ref"] if t["ref"] else prices.close(s, t["trade_date"], count_gap=False) or t["p"]
            p["q"] += t["q"]
            p["cost"] += t["q"] * t["p"] + t["fees"]
            p["fees"] += t["fees"]
            lots.setdefault(s, []).append([t["trade_date"], t["q"], (t["q"] * t["p"] + t["fees"]) / t["q"]])
            flow = t["q"] * mkt + t["fees"]
            fees_by[k] += t["fees"]
            if k == "BUY" and t["ref"]:
                sl = (t["p"] - t["ref"]) * t["q"]
                slip["total"] += sl
                slip["trades_with_reference"] += 1
                slip["items"].append({"txn_id": t["txn_id"], "symbol": s, "side": "BUY", "slippage": round(sl, 2)})
        elif k == "SELL":
            q = t["q"]
            if q > p["q"] + 1e-9:
                quality.append({"issue": "EXCESS_SELL", "txn_id": t["txn_id"], "symbol": s,
                                "detail": f"sell {q:g} > held {p['q']:g}; capped"})
                q = p["q"]
            if q <= 0:
                return 0.0
            avg = p["cost"] / p["q"] if p["q"] else 0.0
            share_fee = t["fees"] * (q / t["q"]) if t["q"] else t["fees"]
            p["realized"] += q * (t["p"] - avg) - share_fee
            p["cost"] -= avg * q
            p["q"] -= q
            p["fees"] += share_fee
            fees_by["SELL"] += share_fee
            left = q
            net_px = t["p"] - share_fee / q
            while left > 1e-9 and lots.get(s):
                lot = lots[s][0]
                take = min(left, lot[1])
                trips.append({"symbol": s, "entry_date": str(lot[0]), "exit_date": str(t["trade_date"]),
                              "days": (t["trade_date"] - lot[0]).days, "quantity": round(take, 6),
                              "entry": round(lot[2], 4), "exit": round(net_px, 4),
                              "pnl": round(take * (net_px - lot[2]), 2), "ret": net_px / lot[2] - 1 if lot[2] else None,
                              "in_period": in_period})
                lot[1] -= take
                left -= take
                if lot[1] <= 1e-9:
                    lots[s].pop(0)
            flow = -(q * t["p"] - share_fee)
            if t["ref"]:
                sl = (t["ref"] - t["p"]) * q
                slip["total"] += sl
                slip["trades_with_reference"] += 1
                slip["items"].append({"txn_id": t["txn_id"], "symbol": s, "side": "SELL", "slippage": round(sl, 2)})
        elif k == "DIVIDEND":
            amt = float(t["gross_value"] or 0)
            p["div"] += amt
            p["realized"] += amt
            flow = -amt
        elif k == "FEE":
            amt = float(t["gross_value"] or t["fees"] or 0)
            fees_by["FEE"] += amt
            flow = amt
        return flow

    first_session = cal[0]
    for t in txns:
        in_period = t["trade_date"] >= start
        f = apply(t, in_period)
        if not in_period:
            continue
        s = _session_of(cal, t["trade_date"])
        if s is None:
            quality.append({"issue": "AFTER_LAST_SESSION", "txn_id": t["txn_id"], "detail": str(t["trade_date"])})
            continue
        flows_by_session[s] = flows_by_session.get(s, 0.0) + f
        if f >= 0:
            in_by_session[s] = in_by_session.get(s, 0.0) + f
        else:
            out_by_session[s] = out_by_session.get(s, 0.0) - f
        if t["symbol"]:
            sym_flows.setdefault(t["symbol"], {})
            sym_flows[t["symbol"]][s] = sym_flows[t["symbol"]].get(s, 0.0) + f
        xirr_flows.append((s, -f))

    # value before the period (opening value) and each session's close value
    pre = [t for t in txns if t["trade_date"] < start]

    def holdings_on(d):
        q = {}
        for t in txns:
            if t["trade_date"] > d:
                break
            if t["kind"] in ("BUY", "OPENING"):
                q[t["symbol"]] = q.get(t["symbol"], 0.0) + t["q"]
            elif t["kind"] == "SELL":
                q[t["symbol"]] = max(0.0, q.get(t["symbol"], 0.0) - t["q"])
        return {k: v for k, v in q.items() if v > 1e-9}

    prev_session = None
    rows = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol='NIFTY50' AND date<?", (str(start),)).fetchone()
    if rows and rows[0]:
        prev_session = D._d(rows[0])
    q_prev = holdings_on(prev_session) if (pre and prev_session) else {}
    v_prev_by = {s: q * (prices.close(s, prev_session) or 0.0) for s, q in q_prev.items()} if q_prev else {}
    v0 = sum(v_prev_by.values())
    if v0:
        xirr_flows.insert(0, (prev_session, -v0))

    daily, values, flows, ins, outs, bench = [], [], [], [], [], []
    b_prev = prices.close(bench_sym, prev_session) if prev_session else None
    sym_val_end = {}
    ti = 0
    running = dict(q_prev)
    in_txns = [t for t in txns if t["trade_date"] >= start]
    for d in cal:
        while ti < len(in_txns) and (_session_of(cal, in_txns[ti]["trade_date"]) or date.max) <= d:
            t = in_txns[ti]
            if t["kind"] in ("BUY", "OPENING"):
                running[t["symbol"]] = running.get(t["symbol"], 0.0) + t["q"]
            elif t["kind"] == "SELL":
                running[t["symbol"]] = max(0.0, running.get(t["symbol"], 0.0) - t["q"])
            ti += 1
        val_by = {}
        for s, q in running.items():
            if q > 1e-9:
                px = prices.close(s, d)
                if px is None:
                    quality.append({"issue": "NO_PRICE", "symbol": s, "detail": str(d)})
                    px = 0.0
                val_by[s] = q * px
        v = sum(val_by.values())
        f = flows_by_session.get(d, 0.0)
        values.append(v)
        flows.append(f)
        ins.append(in_by_session.get(d, 0.0))
        outs.append(out_by_session.get(d, 0.0))
        bc = prices.close(bench_sym, d, count_gap=False)
        bench.append(bc / b_prev - 1 if (bc and b_prev) else None)
        b_prev = bc or b_prev
        daily.append({"date": str(d), "value": round(v, 2), "inflow": round(ins[-1], 2),
                      "outflow": round(outs[-1], 2)})
        sym_val_end = val_by
    rets = M.twr(values, ins, outs, v0)
    bases = [b for b, r in zip(M.capital_bases(values, ins, v0), rets) if r is not None and b > 0]
    avg_cap = sum(bases) / len(bases) if bases else 0.0
    for i, r in enumerate(rets):
        daily[i]["return"] = round(r, 6) if r is not None else None
        daily[i]["benchmark_return"] = round(bench[i], 6) if bench[i] is not None else None
    v_end = values[-1] if values else 0.0
    if v_end:
        xirr_flows.append((cal[-1], v_end))
    xr = M.xirr(xirr_flows)

    # PME: the same flows into the benchmark
    units = 0.0
    pme_flows = []
    for d0, amt in xirr_flows[:-1] if v_end else xirr_flows:
        bpx = prices.close(bench_sym, d0, count_gap=False)
        if bpx:
            units += (-amt) / bpx
            pme_flows.append((d0, amt))
    bend = prices.close(bench_sym, cal[-1], count_gap=False)
    pme_val = units * bend if bend else None
    pme_xirr = M.xirr(pme_flows + [(cal[-1], pme_val)]) if pme_val and pme_val > 0 else None

    # contribution on average invested capital (the mean of the sessions' TWR bases):
    # one denominator for every symbol, so the parts sum exactly to the total
    denom = avg_cap
    total_gain = v_end - v0 - sum(flows_by_session.values())
    contrib = []
    for s in sorted(set(sym_val_end) | set(v_prev_by) | set(sym_flows)):
        g = sym_val_end.get(s, 0.0) - v_prev_by.get(s, 0.0) - sum(sym_flows.get(s, {}).values())
        contrib.append({"symbol": s, "gain": round(g, 2), "contribution_pct": round(g / denom * 100, 4) if denom else None,
                        "sector": (sectors or {}).get(s), "asset_class": _classify(s)})
    unalloc = sum(flows_by_session.values()) - sum(sum(v.values()) for v in sym_flows.values())
    if abs(unalloc) > 1e-6:           # FEE rows without a symbol
        contrib.append({"symbol": "(fees not tied to a trade)", "gain": round(-unalloc, 2),
                        "contribution_pct": round(-unalloc / denom * 100, 4) if denom else None,
                        "sector": None, "asset_class": "FEES"})
    md_ret = total_gain / denom if denom else None
    group = lambda key: _group(contrib, key, denom)                                          # noqa: E731

    realized = sum(p["realized"] for p in pos.values())
    last = cal[-1]
    positions = []
    for s, p in sorted(pos.items()):
        px = prices.close(s, last, count_gap=False) if p["q"] > 1e-9 else None
        mv = p["q"] * px if px else 0.0
        positions.append({"symbol": s, "quantity": round(p["q"], 6), "avg_cost": round(p["cost"] / p["q"], 4)
                          if p["q"] > 1e-9 else None, "cost_basis": round(p["cost"], 2), "price": px,
                          "market_value": round(mv, 2), "unrealized": round(mv - p["cost"], 2) if p["q"] > 1e-9 else 0.0,
                          "realized": round(p["realized"], 2), "fees": round(p["fees"], 2),
                          "dividends": round(p["div"], 2)})
    unreal = sum(x["unrealized"] for x in positions)
    ledger_fees = sum(t["fees"] for t in txns) + sum(float(t["gross_value"] or 0) for t in txns if t["kind"] == "FEE")
    engine_fees = sum(fees_by.values())
    in_period_trips = [x for x in trips if x["in_period"]]
    approx = sum(1 for t in txns if t.get("price_quality") not in (None, "EXACT"))
    if approx:
        quality.append({"issue": "APPROXIMATE_PRICES", "detail": f"{approx} transaction(s) carry an inferred or "
                        f"approximate price (see price_quality)"})
    gaps = {k: v for k, v in prices.gaps.items()}
    return {
        "status": "OK" if txns else "NO_TRANSACTIONS", "portfolio": portfolio,
        "period": {"start": str(cal[0]), "end": str(cal[-1]), "sessions": len(cal),
                   "opening_value": round(v0, 2), "closing_value": round(v_end, 2),
                   "net_flows": round(sum(flows_by_session.values()), 2)},
        "returns": {"twr": M.series_metrics(cal, rets, bench, rf_pct), "xirr_pct": _p(xr),
                    "xirr_note": (("period under 90 days: an annualised money-weighted return is not "
                                   "meaningful") if (cal[-1] - cal[0]).days < 90 else None)
                    if xr is not None else "not computable (needs money in and out, and a root below "
                                           "1,000,000% a year)",
                    "return_on_avg_capital_pct": _p(md_ret), "average_invested_capital": round(avg_cap, 2),
                    "gain": round(total_gain, 2),
                    "pme": {"benchmark": bench_sym, "xirr_pct": _p(pme_xirr),
                            "value_if_invested": round(pme_val, 2) if pme_val else None,
                            "note": "the same cash flows invested in the benchmark on the same days"},
                    "realized_pnl": round(realized, 2), "unrealized_pnl": round(unreal, 2)},
        "positions": positions, "daily": daily, "trades": in_period_trips,
        "trade_stats": M.trade_stats(in_period_trips),
        "costs": {"fees_total": round(engine_fees, 2), "by_kind": {k: round(v, 2) for k, v in fees_by.items()},
                  "slippage_vs_reference": round(slip["total"], 2),
                  "trades_with_reference_price": slip["trades_with_reference"], "slippage_items": slip["items"][:200],
                  "reconciliation": {"ledger_fees": round(ledger_fees, 2), "engine_fees": round(engine_fees, 2),
                                     "difference": round(ledger_fees - engine_fees, 2),
                                     "ok": abs(ledger_fees - engine_fees) < 0.01}},
        "contribution": {"method": "gain / average invested capital (mean of the daily TWR bases); every "
                                   "symbol shares the denominator, so the parts sum to the total",
                         "total_pct": _p(md_ret), "by_symbol": sorted(contrib, key=lambda x: -abs(x["gain"])),
                         "by_sector": group("sector"), "by_asset_class": group("asset_class"),
                         "reconciles": abs(sum(c["gain"] for c in contrib) - total_gain) < 0.05},
        "data_quality": quality[:500] + ([{"issue": "PRICE_GAPS", "detail": gaps}] if gaps else []),
        "transactions_used": len(txns),
    }


def _group(contrib, key, denom):
    g = {}
    for c in contrib:
        k = c.get(key) or "UNKNOWN"
        g[k] = g.get(k, 0.0) + c["gain"]
    return [{key: k, "gain": round(v, 2), "contribution_pct": round(v / denom * 100, 4) if denom else None}
            for k, v in sorted(g.items(), key=lambda kv: -abs(kv[1]))]


def _p(x):
    return None if x is None else round(x * 100, 3)
