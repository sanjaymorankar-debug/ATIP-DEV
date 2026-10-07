"""
Robustness battery (W23, BT-14): does the result survive worse assumptions and
different slices of the data?

    robustness(request, n_subsamples=3, seed=42) -> parent run_id + verdict

Trials (each an ordinary backtest under a parent of kind robustness):
    baseline        the request as given
    costs_x2        every percentage cost field of the resolved cost model doubled
    slippage_x3     slippage value tripled (a none model becomes 0.15%)
    subsample_i     a seeded random half of the universe, n_subsamples times
    first_half / second_half   the period split in two by calendar date
Derived from the baseline, no extra run:
    regimes         the baseline's daily returns grouped by the market_health regime of
                    each session: sessions, total return, mean daily return per regime
    monte_carlo     trade-shuffle Monte Carlo (1,000 paths, seed) probability of a loss

CHECKS (PASS / FAIL): baseline return > 0; costs_x2 return > 0; slippage_x3 return > 0;
at least 2/3 of the subsamples positive; both halves positive; Monte Carlo P(loss) < 30%.
score = passes / checks; verdict ROBUST (>= 0.8), FRAGILE (>= 0.5), else NOT_ROBUST.
Refused on the test window.

Transaction-cost stress (BT-19): how much worse can costs get before the edge is gone?

    cost_sweep(request, multipliers=(0, 0.5, 1, 1.5, 2, 3, 5), scale="costs")
        -> parent run_id + one point per multiplier + break_even

NetPnL = gross - commissions - slippage - impact - taxes. Each multiplier x is one
ordinary backtest (kind cost_trial under a parent of kind cost_sweep) with
    costs      every rate AND rupee field of the resolved cost model times x, so each
               leg's itemised charges (brokerage, STT, exchange, SEBI, stamp, GST, DP)
               are exactly x times the base model's
    slippage   the slippage value times x (a none model has nothing to scale)
    both       the two together
x = 1 is the request exactly as given -- the plain backtest. Per point: net total
return, Sharpe, profit factor, trades, max drawdown, costs and slippage paid.
break_even: the multiplier at which net total return first crosses zero, linearly
interpolated between the grid points either side; None, with "positive across the
grid" or "negative even at zero cost", when it does not cross inside the grid.
Sizing and cash follow each run's own equity, so returns need not be exactly linear
in x (monotone reports whether they never rise with x). Refused on the test window.
"""

from __future__ import annotations

import random
from datetime import date

from backtest.optimize import _finish, _parent, _trial

PCT_FIELDS = ("brokerage_pct", "stt_buy_pct", "stt_sell_pct", "exchange_txn_pct", "sebi_fee_pct", "stamp_buy_pct",
              "flat_pct")
# rupee amounts of the cost model: scaled with the rates, a leg's charges scale exactly
# (GST is a % of brokerage + exchange + SEBI, so it follows them and is never scaled itself)
RS_FIELDS = ("brokerage_min", "brokerage_max", "dp_charge_per_sell")
COST_SCALES = ("costs", "slippage", "both")
SWEEP_MULTIPLIERS = (0, 0.5, 1, 1.5, 2, 3, 5)
POINT_KEYS = ("total_return", "sharpe", "profit_factor", "trades", "max_drawdown", "costs_paid", "slippage_paid")


def scaled_cost_overrides(snap: dict, x: float, fields: tuple = PCT_FIELDS + RS_FIELDS) -> dict:
    """cost_overrides that multiply the resolved cost model's `fields` by x (the
    request's own overrides kept for the rest)."""
    from backtest.costs import cost_model
    cm = cost_model(snap["cost_model"], snap["cost_overrides"] or None).as_dict()
    over = dict(snap["cost_overrides"] or {})
    for f in fields:
        if cm.get(f):
            over[f] = cm[f] * x
    return over


def scaled_slippage(snap: dict, x: float) -> dict:
    sl = dict(snap["slippage"])
    return {**sl, "value": (sl.get("value") or 0) * x}


def _universe(snap):
    u = snap["universe"]
    if isinstance(u, list):
        return u
    from data.dhan import get_tracked_symbols
    return sorted(get_tracked_symbols())


def _regimes(run_id):
    from backtest import store
    from db.schema import get_connection
    conn = get_connection()
    try:
        eq = store.get_rows(conn, "backtest_equity", run_id)
        reg = {str(r[0])[:10]: r[1] for r in conn.execute("SELECT date, regime FROM market_health")}
    finally:
        conn.close()
    out = {}
    for p in eq[1:]:
        g = reg.get(str(p["date"])[:10], "UNKNOWN")
        e = out.setdefault(g, {"sessions": 0, "growth": 1.0, "sum": 0.0})
        r = p["daily_return"] or 0.0
        e["sessions"] += 1
        e["growth"] *= 1 + r
        e["sum"] += r
    return {k: {"sessions": v["sessions"], "total_return": round(v["growth"] - 1, 5),
                "mean_daily_return": round(v["sum"] / v["sessions"], 6) if v["sessions"] else None}
            for k, v in sorted(out.items(), key=lambda kv: -kv[1]["sessions"])}


def robustness(request: dict, n_subsamples: int = 3, seed: int = 42) -> dict:
    from backtest import service
    if request.get("period_label") == "test" or request.get("allow_test"):
        raise ValueError("robustness testing on the test window is refused")
    if not 0 <= int(n_subsamples) <= 10:
        raise ValueError("n_subsamples must be 0..10")
    snap = service.resolve_config(request)
    if snap.get("periods"):
        raise ValueError("give start / end (not periods) for a robustness battery")
    pid, _ = _parent(request, "robustness", {"n_subsamples": n_subsamples, "seed": seed})
    base = dict(request)
    trials, idx = {}, [0]

    def run(name, req):
        rid, res = _trial({k: v for k, v in req.items() if k != "params"}, req.get("params") or snap["params"],
                          "robust_trial", pid, idx[0])
        idx[0] += 1
        m = res.get("metrics") or {}
        trials[name] = {"run_id": rid, "status": res["status"], "total_return": m.get("total_return"),
                        "sharpe": m.get("sharpe"), "max_drawdown": m.get("max_drawdown"), "trades": m.get("trades")}
        return trials[name]
    try:
        b = run("baseline", base)
        run("costs_x2", {**base, "cost_overrides": scaled_cost_overrides(snap, 2, PCT_FIELDS)})
        sl = dict(snap["slippage"])
        sl = {**sl, "value": (sl.get("value") or 0) * 3} if sl.get("kind") not in (None, "none") else \
            {"kind": "pct", "value": 0.15}
        run("slippage_x3", {**base, "slippage": sl})
        uni = _universe(snap)
        rng = random.Random(seed)
        for i in range(int(n_subsamples)):
            run(f"subsample_{i + 1}", {**base, "universe": sorted(rng.sample(uni, max(1, len(uni) // 2)))})
        s, e = date.fromisoformat(snap["start"]), date.fromisoformat(snap["end"])
        mid = s + (e - s) / 2
        run("first_half", {**base, "start": str(s), "end": str(mid)})
        run("second_half", {**base, "start": str(mid), "end": str(e)})
        extra = {}
        if b["status"] == "COMPLETED":
            extra["regimes"] = _regimes(b["run_id"])
            try:
                mc = service.run_montecarlo(b["run_id"], "trade_shuffle", 1000, seed)
                pl = mc.get("prob_loss")
                if pl is None:
                    pl = next((v.get("prob_loss") for v in mc.values() if isinstance(v, dict) and "prob_loss" in v),
                              None)
                extra["monte_carlo"] = {"p_loss": pl, "mc_id": mc.get("mc_id")}
            except ValueError as ex:
                extra["monte_carlo"] = {"error": str(ex)}
        pos = lambda t: (t.get("total_return") or 0) > 0 and t["status"] == "COMPLETED"            # noqa: E731
        subs = [t for k, t in trials.items() if k.startswith("subsample_")]
        checks = [("baseline positive", pos(trials["baseline"])), ("costs doubled still positive", pos(trials["costs_x2"])),
                  ("slippage tripled still positive", pos(trials["slippage_x3"]))]
        if subs:
            checks.append(("2/3 of universe halves positive", sum(pos(t) for t in subs) >= 2 * len(subs) / 3))
        checks.append(("both period halves positive", pos(trials["first_half"]) and pos(trials["second_half"])))
        pl = (extra.get("monte_carlo") or {}).get("p_loss")
        if pl is not None:
            checks.append(("Monte Carlo P(loss) < 30%", pl < 0.30))
        score = sum(ok for _, ok in checks) / len(checks)
        verdict = "ROBUST" if score >= 0.8 else "FRAGILE" if score >= 0.5 else "NOT_ROBUST"
        summary = {"trials": trials, "checks": [{"check": c, "result": "PASS" if ok else "FAIL"} for c, ok in checks],
                   "score": round(score, 3), "verdict": verdict, **extra}
        _finish(pid, "COMPLETED", summary, metrics=trials["baseline"])
        return {"run_id": pid, "status": "COMPLETED", **summary}
    except Exception as ex:
        from backtest import store
        from db.schema import get_connection
        conn = get_connection()
        try:
            store.mark_failed(conn, pid, f"{type(ex).__name__}: {ex}")
        finally:
            conn.close()
        raise


def break_even(multipliers: list, returns: list, what: str = "cost") -> tuple:
    """(multiplier, note) where net total return first goes from > 0 to <= 0 along the
    grid, linearly interpolated between the two points either side; (None, note) when
    it never crosses inside the grid. Points with a None return are skipped."""
    pts = sorted((float(m), float(r)) for m, r in zip(multipliers, returns) if r is not None)
    if not pts:
        return None, "no completed runs"
    m0, r0 = pts[0]
    if r0 < 0:
        return None, f"negative even at zero {what}" if m0 == 0 else f"negative even at the lowest multiplier ({m0:g}x)"
    if r0 == 0:
        return m0, f"net return is zero at {m0:g}x"
    for (a, ra), (b, rb) in zip(pts, pts[1:]):
        if rb <= 0:
            x = a + (b - a) * ra / (ra - rb)
            return round(x, 4), f"net return crosses zero at {x:.2f}x the configured {what}"
    return None, "positive across the grid"


def prepare_cost_sweep(request: dict, multipliers: list | None = None, scale: str = "costs") -> list:
    """Validate a cost sweep without running it; returns the sorted multiplier grid."""
    from backtest import service
    if request.get("period_label") == "test" or request.get("allow_test"):
        raise ValueError("cost stress on the test window is refused")
    if scale not in COST_SCALES:
        raise ValueError(f"scale must be one of {COST_SCALES}")
    mults = sorted({float(m) for m in (SWEEP_MULTIPLIERS if multipliers is None else multipliers)})
    if not 2 <= len(mults) <= 20 or mults[0] < 0 or mults[-1] > 100:
        raise ValueError("give 2..20 distinct multipliers, each 0..100")
    service.resolve_config(request)                 # validates the base request
    return mults


def cost_sweep(request: dict, multipliers: list | None = None, scale: str = "costs") -> dict:
    from backtest import service
    mults = prepare_cost_sweep(request, multipliers, scale)
    snap = service.resolve_config(request)
    pid, _ = _parent(request, "cost_sweep", {"multipliers": mults, "scale": scale})
    base = {k: v for k, v in request.items() if k != "params"}
    params = request.get("params") or snap["params"]
    points = []
    try:
        for i, x in enumerate(mults):
            req = dict(base)
            if x != 1:                      # x1 is the request exactly as given: the plain backtest
                if scale in ("costs", "both"):
                    req["cost_overrides"] = scaled_cost_overrides(snap, x)
                if scale in ("slippage", "both"):
                    req["slippage"] = scaled_slippage(snap, x)
            rid, res = _trial(req, params, "cost_trial", pid, i)
            m = res.get("metrics") or {}
            points.append({"multiplier": x, "run_id": rid, "status": res["status"],
                           **{k: m.get(k) for k in POINT_KEYS}})
        ok = [p for p in points if p["status"] == "COMPLETED" and p["total_return"] is not None]
        what = {"costs": "cost", "slippage": "slippage", "both": "cost and slippage"}[scale]
        if ok and not any(p["trades"] for p in ok):
            be, note = None, "no trades: nothing to stress"
        else:
            be, note = break_even([p["multiplier"] for p in ok], [p["total_return"] for p in ok], what)
        rets = [p["total_return"] for p in ok]
        notes = []
        if scale != "costs" and snap["slippage"].get("kind") in (None, "none"):
            notes.append("the slippage model is none: there is no slippage to scale")
        if len(ok) < len(points):
            notes.append(f"{len(points) - len(ok)} run(s) did not complete and are left out of break_even")
        summary = {"scale": scale, "multipliers": mults, "points": points, "break_even": be,
                   "break_even_note": note, "monotone": all(b <= a for a, b in zip(rets, rets[1:])),
                   "baseline": next((p for p in points if p["multiplier"] == 1), None), "notes": notes}
        status = "COMPLETED" if ok else "FAILED"
        _finish(pid, status, summary, metrics=summary["baseline"])
        return {"run_id": pid, "status": status, **summary}
    except Exception as ex:
        from backtest import store
        from db.schema import get_connection
        conn = get_connection()
        try:
            store.mark_failed(conn, pid, f"{type(ex).__name__}: {ex}")
        finally:
            conn.close()
        raise
