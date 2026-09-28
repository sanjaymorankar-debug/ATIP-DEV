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
"""

from __future__ import annotations

import random
from datetime import date

from backtest.optimize import _finish, _parent, _trial

PCT_FIELDS = ("brokerage_pct", "stt_buy_pct", "stt_sell_pct", "exchange_txn_pct", "sebi_fee_pct", "stamp_buy_pct",
              "flat_pct")


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
    from backtest.costs import cost_model
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
        cm = cost_model(snap["cost_model"], snap["cost_overrides"] or None).as_dict()
        over = dict(snap["cost_overrides"] or {})
        for f in PCT_FIELDS:
            if cm.get(f):
                over[f] = cm[f] * 2
        run("costs_x2", {**base, "cost_overrides": over})
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
