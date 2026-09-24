"""
Monte Carlo analysis of a finished backtest (BT-13).

Two resamplings, both seeded so a result can be reproduced exactly:

  trade_shuffle     re-orders the run's closed trades, applying each trade's
                    net return to the equity it would have been sized on. Only
                    the ORDER changes, so the final return is the same in every
                    path (returns compound multiplicatively) -- what varies is
                    the path: how deep the drawdowns could have been had the
                    same trades come in a different sequence.
  return_bootstrap  draws daily returns with replacement from the equity
                    curve (block_size > 1 draws runs of consecutive days, which
                    keeps some of their clustering). Final return, CAGR and
                    maximum drawdown all vary.

Each reports percentiles (5/25/50/75/95), the mean, and the share of paths
ending below the start. These describe how the historical result could have
varied under resampling; they are NOT a forecast and do not bound future
outcomes -- markets change, and resampling the past cannot see that.
"""

from __future__ import annotations

import random

from backtest import metrics as M

DISCLAIMER = ("Resampling of historical backtest results. Not a forecast or a guarantee: "
              "future returns can fall outside every range shown here.")
PCTS = (5, 25, 50, 75, 95)


def _percentile(sorted_xs, p):
    """Linear interpolation between closest ranks (numpy's default)."""
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_xs) - 1)
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def _dist(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {"n": 0}
    out = {f"p{p}": _percentile(xs, p) for p in PCTS}
    out.update({"mean": sum(xs) / len(xs), "min": xs[0], "max": xs[-1], "n": len(xs)})
    return out


def trade_shuffle(trade_returns_pct: list, n_sims: int = 1000, seed: int = 42,
                  start_equity: float = 1.0, position_fraction: float = 1.0) -> dict:
    """
    trade_returns_pct: each closed trade's net return on its own cost (%).
    position_fraction: the share of equity each trade is assumed to carry
    (the run's average position size / equity); 1.0 compounds whole returns.
    """
    rng = random.Random(seed)
    rets = [r / 100 * position_fraction for r in trade_returns_pct if r is not None]
    finals, mdds = [], []
    for _ in range(n_sims):
        seq = rets[:]
        rng.shuffle(seq)
        eq = [start_equity]
        for r in seq:
            eq.append(eq[-1] * (1 + r))
        finals.append(eq[-1] / start_equity - 1)
        mdds.append(M.max_drawdown(eq))
    return {"method": "trade_shuffle", "n_sims": n_sims, "seed": seed, "n_trades": len(rets),
            "position_fraction": position_fraction,
            "final_return": _dist(finals), "max_drawdown": _dist(mdds),
            "prob_loss": (sum(1 for f in finals if f < 0) / len(finals)) if finals else None,
            "disclaimer": DISCLAIMER}


def return_bootstrap(daily_returns: list, n_sims: int = 1000, seed: int = 42,
                     block_size: int = 1, periods: int | None = None) -> dict:
    """Resample the run's daily returns (in blocks) into n_sims paths of the same length."""
    rng = random.Random(seed)
    r = [x for x in daily_returns if x is not None]
    n = periods or len(r)
    finals, mdds, cagrs = [], [], []
    if not r or n < 1:
        return {"method": "return_bootstrap", "n_sims": 0, "seed": seed, "disclaimer": DISCLAIMER}
    block = max(1, min(int(block_size), len(r)))
    for _ in range(n_sims):
        path = []
        while len(path) < n:
            s = rng.randrange(0, len(r) - block + 1)
            path.extend(r[s:s + block])
        path = path[:n]
        eq = [1.0]
        for x in path:
            eq.append(eq[-1] * (1 + x))
        finals.append(eq[-1] - 1)
        mdds.append(M.max_drawdown(eq))
        cagrs.append(eq[-1] ** (M.PERIODS_PER_YEAR / n) - 1 if eq[-1] > 0 else None)
    return {"method": "return_bootstrap", "n_sims": n_sims, "seed": seed, "block_size": block,
            "periods": n, "final_return": _dist(finals), "max_drawdown": _dist(mdds),
            "cagr_session_based": _dist(cagrs),
            "prob_loss": sum(1 for f in finals if f < 0) / len(finals), "disclaimer": DISCLAIMER}
