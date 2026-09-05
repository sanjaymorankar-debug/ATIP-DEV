"""
ATIP — Backtest Harness
=======================
Replays prices_daily to measure what a fixed-percentage-target strategy would
actually have returned, so a strategy can be judged on evidence instead of on
how good its win rate sounds.

WHY THIS EXISTS
    Nothing in ATIP could measure outcomes: the predictions table was never
    written (see scores/predictions.py) and accuracy_tracker was empty, so
    "what is my success ratio?" had no answer. This module answers it from
    price history alone, without waiting months for forward data to accumulate.

ENTRY MODES
    dip     "buy when it's at its low" — enters when price sits in the bottom
            DIP_ZONE_PCT of its N-day range (optionally RSI-oversold). This is
            the mean-reversion thesis most 3%-target plans are really making.
    vol50   the 50 most volatile liquid names, no timing signal. The baseline
            any real strategy must beat.
    signal  ATIP's own ai_scores (BUY signal, ZPI/CRI thresholds). Needs score
            history to be meaningful — with only a handful of scored days it
            will say so rather than print a number built on nothing.

EXIT
    Whichever comes first, walking forward on daily bars:
        target hit (high >= target)  |  stop hit (low <= stop)  |  max-hold timeout
    A daily bar whose range covers BOTH stop and target cannot tell us which
    came first, so those are counted as STOPS (pessimistic) and reported
    separately as `ambiguous` — that share is how much of the result rests on
    the assumption.

COSTS
    Reported both gross and net. Net subtracts COST_PCT round-trip (brokerage +
    STT + exchange + GST + stamp + slippage). This matters more than the hit
    rate: a 1-2 day median hold at ~0.25% round trip needs ~0.25% of edge per
    trade just to break even, which is larger than the gross edge of most
    fixed-target strategies.

USAGE
    python -m scores.backtest                          # dip mode, 3% and 6% targets
    python -m scores.backtest --mode vol50 --target 3 --stop 2 --hold 10
    python -m scores.backtest --mode dip --sweep       # grid over stops/holds
    python -m scores.backtest --mode signal --target 3
"""
from __future__ import annotations

import argparse
import logging
import math
import statistics
from collections import defaultdict

from db.schema import get_connection

log = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
VOL_LOOKBACK = 20          # trailing window for realised vol + turnover
RANGE_LOOKBACK = 20        # window defining "its low" for dip mode
DIP_ZONE_PCT = 25.0        # dip entry: price in bottom X% of that range
MIN_TURNOVER_CR = 5.0      # liquidity floor — below this you can't get 3% out cleanly
TOP_N = 50                 # positions considered per entry date
COST_PCT = 0.25            # round-trip cost assumption, %
DEFAULT_TARGETS = (3.0, 6.0)


# ═════════════════════════════════════════════════════════════════════════════
#  DATA
# ═════════════════════════════════════════════════════════════════════════════

def load_bars(conn) -> dict:
    """symbol -> [(date, high, low, close, volume, open), ...] ascending by date."""
    rows = conn.execute(
        "SELECT symbol,date,high,low,close,volume,open FROM prices_daily "
        "WHERE series='EQ' AND source!='dhan_index' AND close>0 AND high>0 AND low>0 "
        "ORDER BY symbol,date"
    ).fetchall()
    bars = defaultdict(list)
    for r in rows:
        bars[r["symbol"]].append((r["date"], r["high"], r["low"], r["close"],
                                  r["volume"], r["open"]))
    return dict(bars)


def load_signals(conn) -> dict:
    """(date, symbol) -> ai_scores row, for signal mode."""
    out = {}
    for r in conn.execute("SELECT date,symbol,signal,zpi,cri,acs,atip_score FROM ai_scores").fetchall():
        out[(r["date"], r["symbol"])] = dict(r)
    return out


def build_features(bars: dict) -> dict:
    """
    symbol -> {date: (index, vol_pct, turnover_cr, range_pos_pct)}

    range_pos_pct is where the close sits inside its RANGE_LOOKBACK high/low
    band: 0 = at the low, 100 = at the high. That's the "is it at its low?"
    measure dip mode filters on.
    """
    need = max(VOL_LOOKBACK, RANGE_LOOKBACK)
    feats = {}
    for sym, series in bars.items():
        if len(series) < need + 2:
            continue
        closes = [b[3] for b in series]
        rets = [0.0] + [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
        per_date = {}
        for i in range(need, len(series)):
            window = rets[i - VOL_LOOKBACK + 1:i + 1]
            try:
                sd = statistics.stdev(window)
            except statistics.StatisticsError:
                continue
            vol_pct = sd * math.sqrt(252) * 100
            tw = series[i - VOL_LOOKBACK + 1:i + 1]
            turnover_cr = sum(b[3] * b[4] for b in tw) / len(tw) / 1e7
            rw = series[i - RANGE_LOOKBACK + 1:i + 1]
            hi, lo = max(b[1] for b in rw), min(b[2] for b in rw)
            rng_pos = ((closes[i] - lo) / (hi - lo) * 100) if hi > lo else 50.0
            per_date[series[i][0]] = (i, vol_pct, turnover_cr, rng_pos)
        feats[sym] = per_date
    return feats


# ═════════════════════════════════════════════════════════════════════════════
#  SIMULATION
# ═════════════════════════════════════════════════════════════════════════════

def _pick_candidates(mode, feats, signals, d, top_n):
    cands = []
    for sym, per_date in feats.items():
        f = per_date.get(d)
        if not f:
            continue
        idx, vol_pct, turnover, rng_pos = f
        if turnover < MIN_TURNOVER_CR:
            continue
        if mode == "dip":
            if rng_pos > DIP_ZONE_PCT:
                continue
            rank = -rng_pos                     # deepest in its range first
        elif mode == "vol50":
            rank = vol_pct                      # most volatile first
        elif mode == "signal":
            s = signals.get((d, sym))
            if not s or s.get("signal") != "BUY":
                continue
            rank = s.get("atip_score") or 0
        elif mode == "all":
            # BENCHMARK — no selection at all, just liquid names. This is the
            # control that matters: over a rising market ANY long strategy
            # looks profitable, so a strategy is only interesting if it beats
            # this. Ranked by symbol hash so the sample is stable but arbitrary.
            rank = hash(sym) % 100000
        else:
            raise ValueError(f"unknown mode {mode}")
        cands.append((rank, sym, idx))
    cands.sort(reverse=True)
    return cands[:top_n]


def simulate(bars, feats, signals, dates, mode, target_pct, stop_pct,
             max_hold, top_n=TOP_N, cost_pct=COST_PCT, entry_timing="next_open") -> dict:
    """
    entry_timing:
      next_open  entry at the NEXT session's open (default). ATIP's scoring
                 pipeline runs post-market at ~4-5 PM, so the earliest you can
                 actually act on a signal is the following morning. Entering at
                 the same day's close would be look-ahead bias — using a price
                 that had already printed before the signal existed.
      close      entry at the signal day's close. Only valid if you generate
                 the signal intraday before the close; reported for comparison
                 so the size of that bias is visible rather than hidden.
    """
    res = {"mode": mode, "target": target_pct, "stop": stop_pct, "hold": max_hold,
           "entry_timing": entry_timing,
           "TARGET": 0, "STOP": 0, "TIMEOUT": 0, "ambiguous": 0,
           "trades": 0, "rets": [], "held": [], "entry_dates": set()}
    need = max(VOL_LOOKBACK, RANGE_LOOKBACK)
    usable = dates[need:len(dates) - max_hold - 2]
    for d in usable:
        for _rank, sym, idx in _pick_candidates(mode, feats, signals, d, top_n):
            series = bars[sym]
            if entry_timing == "next_open":
                if idx + 1 >= len(series):
                    continue
                entry = series[idx + 1][5] or series[idx + 1][3]   # open, close fallback
                start = idx + 1                                    # exits scan from entry day
            else:
                entry = series[idx][3]
                start = idx
            if not entry or entry <= 0:
                continue
            target = entry * (1 + target_pct / 100)
            stop = entry * (1 - stop_pct / 100)
            outcome = ret = held = None
            for k in range(1, max_hold + 1):
                if start + k >= len(series):
                    break
                _dt, hi, lo, cl, _v, _o = series[start + k]
                hit_t, hit_s = hi >= target, lo <= stop
                if hit_t and hit_s:
                    res["ambiguous"] += 1
                    outcome, ret, held = "STOP", -stop_pct, k    # pessimistic
                    break
                if hit_s:
                    outcome, ret, held = "STOP", -stop_pct, k
                    break
                if hit_t:
                    outcome, ret, held = "TARGET", target_pct, k
                    break
            if outcome is None:
                j = min(start + max_hold, len(series) - 1)
                outcome, ret, held = "TIMEOUT", (series[j][3] / entry - 1) * 100, max_hold
            res[outcome] += 1
            res["trades"] += 1
            res["rets"].append(ret)
            res["held"].append(held)
            res["entry_dates"].add(d)

    n = res["trades"]
    if n:
        gross = sum(res["rets"]) / n
        res["gross_avg"] = round(gross, 4)
        res["net_avg"] = round(gross - cost_pct, 4)
        res["hit_rate"] = round(res["TARGET"] / n * 100, 1)
        res["stop_rate"] = round(res["STOP"] / n * 100, 1)
        res["timeout_rate"] = round(res["TIMEOUT"] / n * 100, 1)
        res["any_profit_rate"] = round(sum(1 for x in res["rets"] if x > 0) / n * 100, 1)
        res["median_hold"] = statistics.median(res["held"])
        res["ambiguous_pct"] = round(res["ambiguous"] / n * 100, 1)
        # Expectancy decomposition — how much each outcome bucket contributes
        # to the gross average. Deliberately NOT a "break-even hit rate": that
        # formula only holds when every trade ends at target or stop, and here
        # timeouts can be 20-40% of trades and carry most of the return, which
        # would make a break-even figure quietly wrong.
        res["contrib_target"] = round(res["TARGET"] / n * target_pct, 4)
        res["contrib_stop"] = round(res["STOP"] / n * -stop_pct, 4)
        timeout_rets = [x for x, h in zip(res["rets"], res["held"])
                        if h == max_hold and x not in (target_pct, -stop_pct)]
        res["contrib_timeout"] = round(sum(timeout_rets) / n, 4) if timeout_rets else 0.0
        res["timeout_avg"] = round(sum(timeout_rets) / len(timeout_rets), 3) if timeout_rets else None
        res["trades_per_entry_day"] = round(n / max(len(res["entry_dates"]), 1), 1)
    res["entry_days"] = len(res["entry_dates"])
    del res["entry_dates"]
    return res


def print_result(r: dict):
    if not r["trades"]:
        print(f"  {r['mode']}: no trades matched (target {r['target']}%, stop {r['stop']}%)")
        return
    verdict = "POSITIVE" if r["net_avg"] > 0 else "NEGATIVE"
    print(f"  target +{r['target']}%  stop -{r['stop']}%  hold {r['hold']}d   "
          f"({r['trades']} trades over {r['entry_days']} entry days)")
    print(f"    hit target {r['hit_rate']:5.1f}%   stopped {r['stop_rate']:5.1f}%   "
          f"timeout {r['timeout_rate']:5.1f}%   any-profit {r['any_profit_rate']:5.1f}%")
    print(f"    avg/trade  gross {r['gross_avg']:+.3f}%   net of {COST_PCT}% costs "
          f"{r['net_avg']:+.3f}%  -> {verdict}")
    print(f"    contribution: target {r['contrib_target']:+.3f}  stop {r['contrib_stop']:+.3f}  "
          f"timeout {r['contrib_timeout']:+.3f}"
          + (f" (timeouts avg {r['timeout_avg']:+.2f}%)" if r.get("timeout_avg") is not None else ""))
    print(f"    median hold {r['median_hold']:.0f}d   ambiguous {r['ambiguous_pct']:.1f}%")


def run(mode="dip", targets=DEFAULT_TARGETS, stops=(2.0, 3.0), holds=(5, 10),
        top_n=TOP_N, cost_pct=COST_PCT, sweep=False, entry_timing="next_open") -> list:
    conn = get_connection()
    try:
        log.info("loading price history…")
        bars = load_bars(conn)
        signals = load_signals(conn) if mode == "signal" else {}
    finally:
        conn.close()
    if not bars:
        print("No price history in prices_daily — run the pipeline first.")
        return []
    dates = sorted({b[0] for s in bars.values() for b in s})
    feats = build_features(bars)
    print(f"\nUniverse: {len(feats)} symbols with features · {len(dates)} trading dates "
          f"{dates[0]} .. {dates[-1]}")
    print(f"Mode: {mode}" + (f" · dip zone = bottom {DIP_ZONE_PCT}% of {RANGE_LOOKBACK}d range"
                             if mode == "dip" else ""))
    print(f"Liquidity floor Rs{MIN_TURNOVER_CR}Cr/day · max {top_n} positions/day · "
          f"cost {cost_pct}% round trip\n")

    if mode == "signal":
        scored_days = len({d for (d, _s) in signals})
        if scored_days < 30:
            print(f"⚠  Only {scored_days} scored day(s) in ai_scores — far too few to draw any "
                  f"conclusion about signal quality.\n   Results below are printed for "
                  f"plumbing verification only; ignore the percentages until you have "
                  f"several months of scored history.\n")

    if not sweep:
        stops, holds = (stops[-1],), (holds[-1],)

    out = []
    for target in targets:
        print(f"── target +{target}% " + "─" * 56)
        for stop in stops:
            for hold in holds:
                r = simulate(bars, feats, signals, dates, mode, target, stop, hold,
                             top_n=top_n, cost_pct=cost_pct, entry_timing=entry_timing)
                print_result(r)
                out.append(r)
        print()
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Backtest fixed-percentage-target strategies on ATIP price history")
    ap.add_argument("--mode", choices=["dip", "vol50", "signal", "all"], default="dip")
    ap.add_argument("--target", type=float, action="append",
                    help="profit target %% (repeatable; default 3 and 6)")
    ap.add_argument("--stop", type=float, action="append", help="stop-loss %% (repeatable)")
    ap.add_argument("--hold", type=int, action="append", help="max holding days (repeatable)")
    ap.add_argument("--top", type=int, default=TOP_N, help=f"positions per entry day (default {TOP_N})")
    ap.add_argument("--cost", type=float, default=COST_PCT, help=f"round-trip cost %% (default {COST_PCT})")
    ap.add_argument("--sweep", action="store_true", help="grid over all stops x holds")
    ap.add_argument("--entry", choices=["next_open", "close"], default="next_open",
                    help="next_open (realistic for a post-market signal) or close (look-ahead; for comparison)")
    args = ap.parse_args()

    run(mode=args.mode,
        targets=tuple(args.target) if args.target else DEFAULT_TARGETS,
        stops=tuple(args.stop) if args.stop else (2.0, 3.0, 4.0),
        holds=tuple(args.hold) if args.hold else (5, 10, 20),
        top_n=args.top, cost_pct=args.cost, sweep=args.sweep, entry_timing=args.entry)
