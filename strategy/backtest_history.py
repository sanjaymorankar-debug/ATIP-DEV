"""
Measure the aggressive exit policy against real bars.

The scenarios in `backtest_aggressive` prove the mechanics work. They cannot say
whether the policy is worth using, because the price paths are hand-written.
This module answers that question the only way it can be answered: replay the
same entries through three different exit policies over real `prices_daily`
history and compare what actually came out.

  flat_3      sell everything at +3%
  flat_6      sell everything at +6%
  aggressive  half at +3%, the rest on a trailing stop past a +6% checkpoint

Entry, stop, holding window and costs are identical across all three, so the
only difference is the exit policy. That is the point — it isolates the thing
being decided instead of measuring signal quality at the same time.

Deliberate constraints, so the answer is honest:

  * Entries come from a fixed, mechanical rule, not from hindsight.
  * Same-bar ambiguity resolves against the strategy (stop before target).
  * Costs are charged on every leg; the aggressive policy pays for two exits and
    is not quietly given the extra leg for free.
  * The runner's momentum reading comes from the bars available AT the
    checkpoint, never from later ones.
"""

from __future__ import annotations

import logging
from datetime import date

from db.schema import get_connection
from strategy.aggressive import Momentum, config, validate_config
from strategy.backtest_aggressive import Bar, metrics, simulate

log = logging.getLogger("atip.strategy")

DEFAULT_COST_PCT = 0.25      # round-trip brokerage + STT + slippage
HOLD_BARS = 30               # tracking window, matching the signal log's
STOP_PCT = 3.0


def _load_bars(conn, symbol: str, start: str, limit: int = 4000) -> list[tuple]:
    return conn.execute(
        "SELECT date, open, high, low, close FROM prices_daily "
        "WHERE symbol=? AND date>=? AND high IS NOT NULL AND low IS NOT NULL "
        "ORDER BY date LIMIT ?", (symbol, start, limit)).fetchall()


def _atr_pct(window) -> float | None:
    """
    True-range average over the pre-entry window, as a percentage of price.

    Computed from the same bars the strategy would have had, never from the
    forward window — using later bars would leak the future into the stop.
    """
    if len(window) < 5:
        return None
    trs, prev_close = [], None
    for _d, _o, h, l, c in window:
        if None in (h, l, c):
            continue
        tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close),
                                                    abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if not trs or not prev_close:
        return None
    return round(sum(trs) / len(trs) / prev_close * 100, 3)


def _momentum_from_bars(window) -> Momentum:
    """
    A momentum reading built only from bars up to the checkpoint.

    Intentionally crude — this is not ATIP's scoring engine, and it must not
    pretend to be. It exists so the checkpoint has *something* causal to decide
    on during a historical replay, using only information that existed at that
    moment. Live, `record_checkpoint()` is handed the real ZPI/MSI/CRI instead.
    """
    if len(window) < 6:
        return Momentum()
    closes = [b[4] for b in window]
    recent = closes[-5:]
    older = closes[-10:-5] or closes[:5]
    trend = (sum(recent) / len(recent)) / (sum(older) / len(older)) - 1.0
    ups = sum(1 for a, b in zip(closes[-6:], closes[-5:]) if b > a)
    return Momentum(
        zpi=max(0.0, min(100.0, 50.0 + trend * 1000.0)),
        msi=ups / 5.0 * 100.0,
    )


def run(symbols: list[str] = None, start: str = "2025-01-01",
        entry_every: int = 20, cost_pct: float = DEFAULT_COST_PCT,
        max_entries: int = 4000, cfg: dict = None) -> dict:
    """
    Replay every policy over the same entries.

    `entry_every` takes an entry every Nth bar per symbol — a mechanical,
    hindsight-free sampling of "you are in a trade, now what?".
    """
    cfg = validate_config(dict(cfg or config()))
    conn = get_connection()
    try:
        if not symbols:
            symbols = [r[0] for r in conn.execute(
                "SELECT symbol FROM prices_daily WHERE date>=? "
                "GROUP BY symbol HAVING COUNT(*)>=? ORDER BY symbol",
                (start, HOLD_BARS * 2)).fetchall()]

        # (config, exit_everything_at_target_1)
        policies = {
            "flat_3":     (cfg, True),
            "flat_6":     ({**cfg, "target_1_pct": 6.0}, True),
            "aggressive": (cfg, False),
        }
        trades = {k: [] for k in policies}
        entries = 0

        for sym in symbols:
            rows = _load_bars(conn, sym, start)
            if len(rows) < HOLD_BARS + 12:
                continue
            for i in range(10, len(rows) - HOLD_BARS, entry_every):
                if entries >= max_entries:
                    break
                entry_price = rows[i][4]                 # previous close
                if not entry_price or entry_price <= 0:
                    continue
                window = rows[i + 1: i + 1 + HOLD_BARS]
                bars = [Bar(high=b[2], low=b[3], close=b[4]) for b in window]
                if len(bars) < HOLD_BARS:
                    continue
                pre = rows[max(0, i - 14): i + 1]
                mom = _momentum_from_bars(pre)
                atr = _atr_pct(pre)
                qty = max(1, int(100000 / entry_price))  # equal rupee size
                for name, (pcfg, exit_all) in policies.items():
                    trades[name].append(simulate(
                        entry_price, qty, bars, mom, pcfg,
                        stop_pct=STOP_PCT, cost_pct=cost_pct, symbol=sym,
                        exit_all_at_t1=exit_all, atr_pct=atr))
                entries += 1
            if entries >= max_entries:
                break

        return {
            "entries": entries,
            "symbols": len(symbols),
            "cost_pct": cost_pct,
            "hold_bars": HOLD_BARS,
            "stop_pct": STOP_PCT,
            "results": {k: metrics(v) for k, v in trades.items()},
        }
    finally:
        conn.close()


def print_report(**kw):
    r = run(**kw)
    print(f"\n{'=' * 78}")
    print(f"  Exit-policy comparison on real bars — {r['entries']} entries, "
          f"{r['symbols']} symbols")
    print(f"  costs {r['cost_pct']}% round trip, {r['stop_pct']}% stop, "
          f"{r['hold_bars']}-session window")
    print(f"{'=' * 78}")
    if not r["entries"]:
        print("  no entries — not enough price history for this window")
        return r
    keys = ["total_trades", "win_rate_pct", "expectancy_pct", "expectancy",
            "net_profit", "profit_factor", "avg_win", "avg_loss", "max_drawdown",
            "target_1_hit_rate_pct", "trailing_exit_pct", "initial_stop_exit_pct",
            "profit_after_target_1"]
    names = list(r["results"])
    print(f"  {'metric':26s}" + "".join(f"{n:>16s}" for n in names))
    print(f"  {'-' * 74}")
    for k in keys:
        row = "".join(f"{str(r['results'][n].get(k)):>16s}" for n in names)
        print(f"  {k:26s}{row}")
    print()
    best = max(names, key=lambda n: r["results"][n]["expectancy_pct"] or -9e9)
    e = r["results"][best]["expectancy_pct"]
    print(f"  Highest expectancy: {best} ({e:+.4f}% of capital per trade, after costs)")
    if e is not None and e <= 0:
        print("  NOTE: every policy tested is negative after costs on these entries.")
        print("        That is a result about the ENTRIES, not only the exits — "
              "see initial_stop_exit_pct.")
    print()
    return r


if __name__ == "__main__":
    print_report()
