"""
Replay ATIP's paper sessions bar by bar: how close the replay is to the real fills, what other exits would have done
to the same entries, and how every exit does over the whole rank-fill population.

The engine is the 15 Nov 2026 evidence pack (.ux_build/kite-gap-2026-11-15/scripts: engine.py, exits.py, calib.py,
whatif.py, hybrid.py, pop2.py, summ2.py) made importable and tested; with fills=PACK_FILLS it reproduces the pack's
numbers to the rupee. It reads one SQLite file, read-only (a backup: atip_data/backups/atip-scheduled-*.db), and never
calls a broker.

THE ENGINE  simulate() walks a position through the bars after its entry bar (15-minute bars where ATIP captured them,
            else the day's OHLC as one bar at 09:15). Per bar, in this order:
              partial    a first leg sells `fraction` at +at % (the bar's open if it gapped above)
              stop       the bar OPENS at or under the stop -> sold at that open (a gap goes through a resting stop);
                         else its LOW reaches the stop -> sold at the stop. "Trail" once the trail is armed, else "Stop"
              target     live once `target_after_days` calendar days have passed (ATIP's 15-day sale) or at once;
                         an open above it sells at the open, a high through it at the target
              then       the high-water mark moves; breakeven lifts the stop; past `trail_after` the stop trails
                         `trail_by` under the high; `max_days` sells at the close of the Nth trading day after entry
            Still held at the last bar -> "OPEN", marked at that close. A stop and a target can only be ordered within a
            bar by guessing, so the stop is checked first (pessimistic). The stop moved by a bar's high is only checked
            from the next bar.

EXITS AS DATA  ExitSpec: stop, trail_after, trail_by and target are a % of the entry price, or Atr(k, lo, hi) =
            clamp(k x ATR14 %, lo, hi) of the day before the fill (lo when the ATR is unknown); plus target_after_days,
            breakeven (at %, lock %), partial (fraction, at %), max_days. ExitSpec.from_dict reads the pack's dicts
            (sl/act/tr/tp/...), ExitSpec.from_arm a session arm of orders/arms.py ({"type": "pct" | "atr", ...}).
            EXITS is the pack's 14 settings: X1 is today's (stop 2 %, trail 1 % from +3 %, +3 % sale after 15 days;
            S1 trails from +4 %).

FILLS (owner item 17, 16 Nov 2026)  "When a bar opens through the stop, fill at the open (BHEL filled at -2.61 %
            against a -2 % stop). Use 10-20 bps slippage on stop exits, not 5." Fills(entry_slip_bps, exit_slip_bps,
            stop_slip_bps, gap_fill_at_open). PACK_FILLS = 5/5/5 bps, gap at the open: what the pack ran (its engine
            already sold a gap at the open). REALISTIC_FILLS = the same with 15 bps on every stop and trail exit -- the
            default from now on. Slippage is always against the order; the "OPEN" mark has none.
            On the 34 cycles (16 Nov 2026): the median stop-exit price error vs the real fill was +4.1 bps at 5 bps
            (the replay sold too well), -0.9 at 10, -5.9 at 15, -10.9 at 20. No stop gapped overnight; the 4 exits sold
            at an open were trails whose 09:15 bar made the high and closed under the new stop. BHEL (6 Nov) was not a
            gap: its 09:15 bar opened 415.90 over the 410.78 stop and fell to 400.75; the monitor sold at 408.20 at
            09:16:46, a fall inside the bar no bar-level fill sees (replay 410.57 at 5 bps, 410.16 at 15).

CALIBRATION  calibrate(): each closed session cycle (session_cycles) replayed from its buy price and time with its
            session's exit and real quantity, on the raw 15-minute bars from 20 Oct. Agreement: exit reasons, correlation
            of net P&L, the sum of simulated vs actual net, and the exit-price error in bps.
            Pack (15 Nov): 34/34 reasons, corr 0.968, sim 4,069.89 vs actual 4,800.55.

WHAT-IF     whatif(): the real executed session entries (order_rules ENTRY/EXECUTED in a session; fill time = its STOP
            leg's creation) replayed under each exit on the hybrid bars to the last close; open ones marked to market.
            Pack: 44 entries; X1 Rs 3,064 (45.5 % win), X2 2,683, X6 6,679, X7 6,922, X8 11,957, X9 14,147.

POPULATION  population(): what rank-fill would have bought every day -- ai_scores BUY first, then HOLD (HOLD skipped at
            CRI >= 75, close < Rs 20 or 20-day turnover < Rs 2 Cr), by score, ten a day -- bought on the next session
            by one of four entries and sold by each exit, Rs 25,000 a trade:
              E0  rank-fill's trigger: the first bar of the next day at or under close x 1.005 (its open if under)
              E1  E0, skipped when that fill is more than 1 % under the signal close (a falling knife)
              E2  a breakout: the first bar through max(high, close x 1.005)
              E3  a pullback: the first bar in the next three sessions at or under the signal day's low
            mode "daily" = daily bars only; "intraday" = 15-minute bars where captured. Pack (daily from 28 Jul): 470
            candidates on 47 days; E0 446 trades, X1 31.6 % win, -0.895 % a trade; all 14 exits negative.

COSTS       charges(): NSE delivery (CNC) at ATIP's cost model, the same as session_cycles: STT 0.1 % both legs, stamp
            0.015 % on the buy, exchange 0.00297 %, SEBI 0.0001 %, GST 18 % on those two, DP Rs 15.93 on a sale.

USAGE
    python -m research.session_ab_replay --db atip_data/backups/atip-scheduled-20261115-191511.db --out OUT calib
    python -m research.session_ab_replay --db DB --out OUT whatif [--arms]
    python -m research.session_ab_replay --db DB --out OUT population --start 2026-07-28 --mode daily --split 2026-09-25
    python -m research.session_ab_replay --db DB --out OUT all --fills pack,realistic
"""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd

log = logging.getLogger("atip.research.session_ab_replay")

CAPITAL = 25_000.0                       # one pool's buy (orders/sessions.py)
CALIB_SINCE = "2026-10-20"               # raw 15-minute bars the calibration replays on (pools started 29 Oct)
WHATIF_START = "2026-10-27"              # the hybrid bars of the what-if start here
NS_PER_DAY = 86_400_000_000_000
STOP_REASONS = ("Stop", "Trail")


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  COSTS, LEVELS, EXIT SPECS, FILLS
# ═══════════════════════════════════════════════════════════════════════════════════════════

def charges(buy_value: float, sell_value: float) -> float:
    """NSE delivery (CNC) charges per ATIP's cost model; equals session_cycles (24,022.90 / 23,501.94 -> 68.78)."""
    B, S = buy_value, sell_value
    stt = 0.001 * (B + S); stamp = 0.00015 * B
    exch = 0.0000297 * (B + S); sebi = 0.000001 * (B + S)
    gst = 0.18 * (exch + sebi); dp = 15.93 if S > 0 else 0.0
    return stt + stamp + exch + sebi + gst + dp


@dataclass(frozen=True)
class Atr:
    """clamp(k x ATR14 %, lo, hi) % of the entry; lo when the ATR is unknown."""
    k: float
    lo: float
    hi: float

    def pct(self, atr_pct: float | None) -> float:
        x = self.lo if atr_pct is None or (isinstance(atr_pct, float) and math.isnan(atr_pct)) else self.k * atr_pct
        return min(max(x, self.lo), self.hi)

    def __str__(self):
        return f"{self.k:g}xATR[{self.lo:g}-{self.hi:g}]"


Level = Union[float, Atr]


def _level(v) -> Level | None:
    """A % (number), an Atr, the pack's ("atr", k, lo, hi) or an arm's {"type": "pct"|"atr", ...}."""
    if v is None or isinstance(v, Atr):
        return v
    if isinstance(v, (tuple, list)) and v and v[0] == "atr":
        return Atr(float(v[1]), float(v[2]), float(v[3]))
    if isinstance(v, dict):
        if v.get("type") == "atr":
            return Atr(float(v["mult"]), float(v["min"]), float(v["max"]))
        if v.get("type") == "pct":
            return float(v["value"])
        raise ValueError(f"a level is {{'type': 'pct'|'atr', ...}}, got {v!r}")
    return float(v)


def level_pct(v: Level | None, atr_pct: float = np.nan) -> float | None:
    """The level in % of the entry for this trade."""
    v = _level(v)
    if v is None:
        return None
    return v.pct(atr_pct) if isinstance(v, Atr) else float(v)


_PACK_KEYS = {"sl": "stop", "act": "trail_after", "tr": "trail_by", "tp": "target",
              "tp_after_days": "target_after_days", "be": "breakeven"}


@dataclass(frozen=True)
class ExitSpec:
    """One exit setting. Levels are % of the entry price or Atr(k, lo, hi). See the module docstring."""
    stop: Level = 2.0
    trail_after: Level | None = None        # the trail arms once the high is this far above the entry
    trail_by: Level | None = None           # ... and then follows the high this far under it
    target: Level | None = None             # a fixed sale at +target %
    target_after_days: int | None = None    # calendar days before the target is live (ATIP's 15-day sale); None = at once
    breakeven: tuple | None = None          # (at %, lock %): once the high reaches +at %, the stop is at least +lock %
    partial: tuple | None = None            # (fraction, at %): a first leg sold at +at %
    max_days: int | None = None             # sell at the close of the Nth trading day after the entry day

    def __post_init__(self):
        for k in ("stop", "trail_after", "trail_by", "target"):
            object.__setattr__(self, k, _level(getattr(self, k)))
        for k in ("breakeven", "partial"):
            v = getattr(self, k)
            if v is not None:
                object.__setattr__(self, k, tuple(float(x) for x in v))

    @classmethod
    def from_dict(cls, d: dict) -> "ExitSpec":
        """The pack's dict (sl, act, tr, tp, tp_after_days, be, partial, max_days) or this class's own field names."""
        if isinstance(d, ExitSpec):
            return d
        if isinstance(d.get("stop"), dict) or "trail" in d or "target_pct" in d:
            return cls.from_arm(d)
        return cls(**{_PACK_KEYS.get(k, k): v for k, v in d.items()})

    @classmethod
    def from_arm(cls, arm: dict) -> "ExitSpec":
        """
        The exit of a session arm (orders/arms.py): stop {"type": "pct", "value"} | {"type": "atr", "mult", "min",
        "max"}; trail {"after", "by"}; target_pct live from the fill; hold_days (with a trail): after that many days a
        price at +after % or more sells -- today's 15-day sale, i.e. target = after, live after hold_days; max_days.
        Entry, gate and sizing keys are not exits and are ignored.
        """
        trail = arm.get("trail") or {}
        after = _level(trail.get("after"))
        target, after_days = None, None
        if arm.get("target_pct") is not None:
            target = float(arm["target_pct"])
        elif trail and arm.get("hold_days"):
            target, after_days = after, int(arm["hold_days"])
        return cls(stop=_level(arm["stop"]), trail_after=after, trail_by=_level(trail.get("by")), target=target,
                   target_after_days=after_days, max_days=int(arm["max_days"]) if arm.get("max_days") else None)

    def describe(self) -> str:
        f = lambda v: "-" if v is None else (str(v) if isinstance(v, Atr) else f"{v:g}%")
        s = [f"stop {f(self.stop)}"]
        if self.trail_by is not None:
            s.append(f"trail {f(self.trail_by)} from +{f(self.trail_after)}")
        if self.target is not None:
            s.append(f"target {f(self.target)}" + (f" after {self.target_after_days}d" if self.target_after_days else ""))
        if self.breakeven:
            s.append(f"BE@{self.breakeven[0]:g}->{self.breakeven[1]:g}")
        if self.partial:
            s.append(f"{self.partial[0]:g}@+{self.partial[1]:g}%")
        if self.max_days:
            s.append(f"{self.max_days}d")
        return ", ".join(s)


@dataclass(frozen=True)
class Fills:
    """How a simulated order fills. bps are always against the order."""
    entry_slip_bps: float = 5.0             # a simulated buy (population entries; the calibration uses the real price)
    exit_slip_bps: float = 5.0              # target, partial and time exits
    stop_slip_bps: float = 15.0             # stop and trail exits (owner item 17: 10-20 bps, not 5)
    gap_fill_at_open: bool = True           # a bar that opens through the stop sells at that open, not at the stop
    name: str = "realistic"


PACK_FILLS = Fills(5.0, 5.0, 5.0, True, "pack")          # the 15 Nov 2026 evidence pack
REALISTIC_FILLS = Fills(5.0, 5.0, 15.0, True, "realistic")
DEFAULT_FILLS = REALISTIC_FILLS
FILLS = {"pack": PACK_FILLS, "realistic": REALISTIC_FILLS}

# The pack's 14 exit settings (exits.py), in its order.
EXITS = {
    "X1 current: SL2 trail+3/1":              ExitSpec(2, 3, 1, 3, 15),
    "X2 SL2 trail+3/1 + BE@1.5":              ExitSpec(2, 3, 1, 3, 15, breakeven=(1.5, 0.3)),
    "X3 SL2 trail+2/1":                       ExitSpec(2, 2, 1, 3, 15),
    "X4 SL2 trail+3/2":                       ExitSpec(2, 3, 2, 3, 15),
    "X5 SL3 trail+3/1.5":                     ExitSpec(3, 3, 1.5, 3, 15),
    "X6 SL3 trail+4/2":                       ExitSpec(3, 4, 2, 4, 15),
    "X7 SL4 trail+4/2":                       ExitSpec(4, 4, 2, 4, 15),
    "X8 SL5 trail+5/2.5 (Kite SL)":           ExitSpec(5, 5, 2.5, 5, 15),
    "X9 ATR: SL2xATR, trail+2xATR/1xATR":     ExitSpec(Atr(2, 2, 8), Atr(2, 2, 8), Atr(1, 1, 4)),
    "X10 bracket SL2/TP3":                    ExitSpec(2, target=3),
    "X11 bracket SL3/TP6":                    ExitSpec(3, target=6),
    "X12 study: SL2xATR, half@6, trail 4xATR": ExitSpec(Atr(2, 2, 10), 6, Atr(4, 3, 20), partial=(0.5, 6)),
    "X13 SL2.5 trail+3/1.5 + BE@1.5":         ExitSpec(2.5, 3, 1.5, 3, 15, breakeven=(1.5, 0.3)),
    "X14 SL3 trail+3/1.5 + BE@2":             ExitSpec(3, 3, 1.5, 3, 15, breakeven=(2.0, 0.5)),
}
CURRENT = "X1 current: SL2 trail+3/1"


def session_exit(session_id: str) -> ExitSpec:
    """The live exit of 29 Oct - 15 Nov 2026: X1; S1 trails (and sells after 15 days) from +4 %."""
    x1 = EXITS[CURRENT]
    return replace(x1, trail_after=4.0, target=4.0) if str(session_id) == "S1" else x1


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════════════════════════════════════

def connect_ro(db_path) -> "sqlite3.Connection":
    """Read-only. A backup opens immutable (no locks, no WAL); a file named atip.db (the live one) without it, so a
    write in progress is never misread -- and nothing here can write either way."""
    import sqlite3
    p = Path(db_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    flags = "mode=ro" if p.name == "atip.db" else "mode=ro&immutable=1"
    return sqlite3.connect(f"file:{p}?{flags}", uri=True)


@dataclass
class ReplayData:
    daily: pd.DataFrame      # prices_daily EQ of the scored universe: one row per (symbol, date), date normalised
    bars: pd.DataFrame       # intraday_bars, 15-minute: symbol, ts, open, high, low, close, volume
    scores: pd.DataFrame     # ai_scores
    cycles: pd.DataFrame     # session_cycles
    rules: pd.DataFrame      # order_rules + session_id (session_rules)
    db_path: str = ""

    @property
    def last_day(self) -> pd.Timestamp:
        d = self.daily["date"]
        return pd.Timestamp(d[d.dt.day_of_week < 5].max())


def load(db_path) -> ReplayData:
    """Everything the replay needs, read once (pack extract.py + common.load)."""
    con = connect_ro(db_path)
    try:
        scores = pd.read_sql("SELECT symbol, date, atip_score, atip_rank, `signal`, cri, mri, vpi, zpi, rri, mh_score, "
                             "regime FROM ai_scores", con)
        daily = pd.read_sql("SELECT symbol, date, open, high, low, close, volume, series, created_at FROM prices_daily "
                            "WHERE symbol IN (SELECT DISTINCT symbol FROM ai_scores)", con)
        bars = pd.read_sql("SELECT symbol, ts, open, high, low, close, volume FROM intraday_bars "
                           "WHERE interval_min = 15", con)
        cycles = pd.read_sql("SELECT * FROM session_cycles", con)
        rules = pd.read_sql("SELECT o.*, sr.session_id FROM order_rules o "
                            "LEFT JOIN session_rules sr ON sr.rule_id = o.id", con)
    finally:
        con.close()
    scores["date"] = pd.to_datetime(scores["date"])
    daily = daily[daily["series"] == "EQ"].copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.normalize()
    daily = (daily.sort_values(["symbol", "date", "created_at"], kind="stable")
             .drop_duplicates(["symbol", "date"], keep="last").reset_index(drop=True))
    for k in ("open", "high", "low", "close", "volume"):
        daily[k] = daily[k].astype(float)
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars = bars.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    log.info(f"loaded {len(daily):,} daily rows, {len(bars):,} 15-min bars, {len(cycles)} cycles from {db_path}")
    return ReplayData(daily, bars, scores, cycles, rules, str(db_path))


def hybrid_bars(daily: pd.DataFrame, bars: pd.DataFrame, start, end, intraday: bool = True) -> pd.DataFrame:
    """One bar path per symbol, weekdays start..end: the 15-minute bars of a day ATIP captured, else that day's OHLC
    as one bar stamped 09:15 (pack hybrid.py)."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    cols = ["symbol", "ts", "open", "high", "low", "close"]
    dd = daily[(daily["date"] >= start) & (daily["date"] <= end) & (daily["date"].dt.day_of_week < 5)]
    dd = dd[["symbol", "date", "open", "high", "low", "close"]].copy()
    dd["ts"] = dd["date"] + pd.Timedelta(hours=9, minutes=15)
    if not intraday:
        return dd[cols].sort_values(["symbol", "ts"], kind="stable")
    bb = bars[(bars["ts"] >= start) & (bars["ts"] <= end + pd.Timedelta(days=1)) & (bars["ts"].dt.day_of_week < 5)].copy()
    bb["date"] = bb["ts"].dt.normalize()
    have = set(zip(bb["symbol"], bb["date"]))
    dd = dd[[(s, t) not in have for s, t in zip(dd["symbol"], dd["date"])]]
    return pd.concat([bb[cols], dd[cols]]).sort_values(["symbol", "ts"], kind="stable")


def atr_pct(daily: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """daily + tr, atrp (ATR-n as % of the close, simple mean of true ranges) and turn20 (20-day mean turnover)."""
    d = daily.sort_values(["symbol", "date"], kind="stable").copy()
    pc = d.groupby("symbol")["close"].shift(1)
    d["tr"] = np.maximum(d["high"] - d["low"], np.maximum((d["high"] - pc).abs(), (d["low"] - pc).abs()))
    d["atrp"] = d.groupby("symbol")["tr"].transform(lambda s: s.rolling(n).mean()) / d["close"] * 100
    d["turn20"] = (d["close"] * d["volume"]).groupby(d["symbol"]).transform(lambda s: s.rolling(20).mean())
    return d


class Bars:
    """Per-symbol arrays of a bar frame (symbol, ts, open, high, low, close), sorted by time."""

    def __init__(self, frame: pd.DataFrame):
        self.d = {}
        for s, g in frame.groupby("symbol", sort=False):
            ts = g["ts"].to_numpy("datetime64[ns]")
            self.d[s] = (ts, ts.astype("datetime64[D]"), g["open"].to_numpy(float), g["high"].to_numpy(float),
                         g["low"].to_numpy(float), g["close"].to_numpy(float))

    def __contains__(self, sym):
        return sym in self.d


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  THE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════════════════

def simulate(bars: Bars, sym: str, entry_ts, entry: float, spec, atr_pct: float = np.nan, cap: float = CAPITAL,
             qty: float | None = None, fills: Fills = DEFAULT_FILLS) -> dict | None:
    """
    One position bought at `entry` at `entry_ts`, walked through the bars after the entry's 15-minute bar (module
    docstring). Returns exit_px (blended with a partial leg), reason (Stop | Trail | Target | Time | OPEN), exit_ts, net
    (Rs, after charges), netpct (% of the buy value), mfe (%), qty, B (buy value), gap (sold at a gapped open), or
    None when the symbol has no bar after the entry.
    """
    if sym not in bars.d:
        return None
    spec = spec if isinstance(spec, ExitSpec) else ExitSpec.from_dict(spec)
    ts, day, o, h, l, c = bars.d[sym]
    t0 = pd.Timestamp(entry_ts)
    i0 = int(np.searchsorted(ts, np.datetime64(t0.floor("15min"), "ns"), side="right"))
    n = len(ts)
    if i0 >= n:
        return None
    sl = level_pct(spec.stop, atr_pct)
    act = level_pct(spec.trail_after, atr_pct)
    tr = level_pct(spec.trail_by, atr_pct)
    tp = level_pct(spec.target, atr_pct)
    tp_after = spec.target_after_days
    be, part, max_days = spec.breakeven, spec.partial, spec.max_days
    xs = 1 - fills.exit_slip_bps / 1e4
    ss = 1 - fills.stop_slip_bps / 1e4
    q = qty if qty else max(1, int(cap // entry))
    stop = entry * (1 - sl / 100)
    tpp = entry * (1 + tp / 100) if tp is not None else None
    hw, trailing, rem, proceeds = entry, False, 1.0, 0.0
    reason, exit_i, px, gap = None, None, None, False
    tsi = ts.view("int64")
    e_ns = int(np.datetime64(t0, "ns").astype("int64"))
    eday = np.datetime64(t0.normalize(), "D")
    days_seen = set()
    for i in range(i0, n):
        dday = day[i]
        if dday > eday:
            days_seen.add(dday)
        # Timedelta.days of (bar - entry): whole calendar days, floored
        tp_live = tp is not None and (tp_after is None or (int(tsi[i]) - e_ns) // NS_PER_DAY >= tp_after)
        if part and rem == 1.0:
            pfrac, ptp = part
            pp = entry * (1 + ptp / 100)
            if o[i] >= pp or h[i] >= pp:
                proceeds += pfrac * max(o[i], pp) * xs
                rem -= pfrac
        if o[i] <= stop:
            gap = fills.gap_fill_at_open
            px = (o[i] if gap else stop) * ss
            reason, exit_i = ("Trail" if trailing else "Stop"), i
            break
        if l[i] <= stop:
            px, reason, exit_i = stop * ss, ("Trail" if trailing else "Stop"), i
            break
        if tp_live and o[i] >= tpp:
            px, reason, exit_i = o[i] * xs, "Target", i
            break
        if tp_live and h[i] >= tpp:
            px, reason, exit_i = tpp * xs, "Target", i
            break
        hw = max(hw, h[i])
        if be and hw >= entry * (1 + be[0] / 100):
            stop = max(stop, entry * (1 + be[1] / 100))
        if act is not None and hw >= entry * (1 + act / 100):
            trailing = True
        if trailing and tr is not None:
            stop = max(stop, hw * (1 - tr / 100))
        if max_days and len(days_seen) >= max_days and (i + 1 == n or day[i + 1] != dday):
            px, reason, exit_i = c[i] * xs, "Time", i
            break
    if reason is None:
        px, reason, exit_i = c[-1], "OPEN", n - 1
    exit_px = proceeds + rem * px
    B, S = entry * q, exit_px * q
    net = S - B - charges(B, S)
    return dict(exit_px=exit_px, reason=reason, exit_ts=pd.Timestamp(ts[exit_i]), net=net, netpct=net / B * 100,
                mfe=(hw / entry - 1) * 100, qty=q, B=B, gap=bool(gap and reason in STOP_REASONS))


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  CALIBRATION
# ═══════════════════════════════════════════════════════════════════════════════════════════

def calibrate(data: ReplayData, fills: Fills = DEFAULT_FILLS, since: str = CALIB_SINCE,
              weekend_bars: bool = True) -> pd.DataFrame:
    """
    Every closed session cycle replayed with its session's exit (session_exit), its real buy price, time and quantity,
    on the raw 15-minute bars from `since`. weekend_bars=False drops bars on Saturdays and Sundays (the 8 Nov 2026
    Muhurat session, when the paper monitor did not trade); the pack kept them.
    """
    b = data.bars[data.bars["ts"] >= pd.Timestamp(since)]
    if not weekend_bars:
        b = b[b["ts"].dt.day_of_week < 5]
    bars = Bars(b)
    rows = []
    for r in data.cycles.itertuples(index=False):
        s = simulate(bars, r.symbol, r.opened_at, r.buy_price, session_exit(r.session_id), qty=r.qty, fills=fills)
        if s is None:
            continue
        rows.append(dict(sid=r.session_id, sym=r.symbol, entry=r.buy_price, qty=r.qty, opened=str(r.opened_at)[:16],
                         act_exit=r.sell_price, act_reason=r.exit_reason, act_net=r.net_pnl,
                         sim_exit=round(s["exit_px"], 2), sim_reason=s["reason"], sim_net=round(s["net"], 2),
                         closed=str(r.closed_at)[:16], sim_ts=str(s["exit_ts"])[:16], sim_gap=s["gap"],
                         exit_err_bps=round((s["exit_px"] / r.sell_price - 1) * 1e4, 1)))
    return pd.DataFrame(rows)


def calibration_summary(R: pd.DataFrame) -> dict:
    """Agreement of a calibrate() table with the real cycles."""
    stops = R[R["act_reason"] == "Stop"]
    trails = R[R["act_reason"] == "Trail"]
    return dict(n=int(len(R)), reason_match=int((R["act_reason"] == R["sim_reason"]).sum()),
                corr=round(float(np.corrcoef(R["act_net"], R["sim_net"])[0, 1]), 3),
                sum_actual=round(float(R["act_net"].sum()), 2), sum_sim=round(float(R["sim_net"].sum()), 2),
                gap_exits=int(R["sim_gap"].sum()),
                stop_err_bps_median=round(float(stops["exit_err_bps"].median()), 1) if len(stops) else None,
                stop_err_bps_mae=round(float(stops["exit_err_bps"].abs().mean()), 1) if len(stops) else None,
                trail_err_bps_median=round(float(trails["exit_err_bps"].median()), 1) if len(trails) else None,
                trail_err_bps_mae=round(float(trails["exit_err_bps"].abs().mean()), 1) if len(trails) else None)


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  WHAT-IF: THE REAL ENTRIES UNDER OTHER EXITS
# ═══════════════════════════════════════════════════════════════════════════════════════════

def session_entries(data: ReplayData) -> pd.DataFrame:
    """Executed session entries with their fill time (the STOP leg is created at the fill)."""
    r = data.rules
    ent = r[(r["role"] == "ENTRY") & (r["status"] == "EXECUTED") & r["session_id"].notna()].copy()
    stops = r[r["role"] == "STOP"][["parent_rule_id", "created_at"]].rename(columns={"created_at": "filled_at"})
    return ent.merge(stops, left_on="id", right_on="parent_rule_id", how="left")


def whatif(data: ReplayData, exits: dict | None = None, fills: Fills = DEFAULT_FILLS, start: str = WHATIF_START,
           end=None, keep_s1_current: bool = True) -> pd.DataFrame:
    """
    Each real session entry (session_entries) replayed under each exit on the hybrid bars start..end (default: the
    last day), with its real quantity; ATR from the last daily bar before the fill day. `exits` maps a name to an
    ExitSpec, a pack dict or an arm dict (default EXITS). With keep_s1_current, S1 runs X1 at its own +4 %.
    """
    exits = {k: ExitSpec.from_dict(v) for k, v in (exits or EXITS).items()}
    end = pd.Timestamp(end) if end is not None else data.last_day
    bars = Bars(hybrid_bars(data.daily, data.bars, pd.Timestamp(start), end, True))
    dd = atr_pct(data.daily)
    rows = []
    for e in session_entries(data).itertuples(index=False):
        ft = pd.Timestamp(e.filled_at)
        prev = dd[(dd["symbol"] == e.symbol) & (dd["date"] < ft.normalize())].tail(1)
        atrp = float(prev["atrp"].iloc[0]) if len(prev) else np.nan
        for name, spec in exits.items():
            if keep_s1_current and e.session_id == "S1" and name == CURRENT:
                spec = session_exit("S1")
            s = simulate(bars, e.symbol, ft, float(e.execution_price), spec, atr_pct=atrp,
                         qty=float(e.execution_quantity), fills=fills)
            if s:
                rows.append(dict(sid=e.session_id, sym=e.symbol, filled=ft, exit=name, atrp=atrp, **s))
    return pd.DataFrame(rows)


def whatif_summary(R: pd.DataFrame) -> pd.DataFrame:
    """Per exit: trades, win %, total net Rs, average net %, still open (marked to market)."""
    S = R.groupby("exit", sort=False).agg(
        n=("net", "size"), win=("net", lambda s: round(100 * (s > 0).mean(), 1)),
        total_net=("net", lambda s: round(s.sum())), avg_pct=("netpct", lambda s: round(s.mean(), 2)),
        open=("reason", lambda s: int((s == "OPEN").sum())))
    return S.sort_values("total_net", ascending=False)


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  POPULATION: EVERY RANK-FILL PICK UNDER EVERY ENTRY AND EXIT
# ═══════════════════════════════════════════════════════════════════════════════════════════

ENTRIES = ("E0", "E1", "E2", "E3")


def rank_fill_candidates(data: ReplayData, start, end, per_day: int = 10, dd: pd.DataFrame | None = None) -> pd.DataFrame:
    """What rank-fill would have bought: per scored day, BUY then HOLD by score (CRI as the tie-break), HOLD skipped
    at CRI >= 75, close < 20 or 20-day turnover < Rs 2 Cr; `per_day` names; the next session is the buy day."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    d = dd if dd is not None else atr_pct(data.daily)
    cal = sorted(pd.Timestamp(x) for x in d[(d["date"] >= start) & (d["date"].dt.day_of_week < 5)]["date"].unique())
    row = {(s, t): v for s, t, v in zip(d["symbol"], d["date"], d[["close", "high", "low", "atrp", "turn20"]]
                                        .itertuples(index=False, name="Row"))}
    sc = data.scores[(data.scores["date"] >= start - pd.Timedelta(days=4)) & (data.scores["date"] < end)]
    out = []
    for day, s in sc.groupby("date"):
        nxt = [x for x in cal if x > day]
        if not nxt:
            continue
        s = s[s["signal"].isin(["BUY", "HOLD"])].copy()
        s["k"] = (s["signal"] != "BUY").astype(int)
        s = s.sort_values(["k", "atip_score", "cri"], ascending=[True, False, True])
        k = 0
        for r in s.itertuples(index=False):
            x = row.get((r.symbol, day))
            if x is None:
                continue
            if r.signal == "HOLD" and ((pd.notna(r.cri) and r.cri >= 75) or x.close < 20 or (x.turn20 or 0) < 2e7):
                continue
            out.append(dict(sym=r.symbol, sday=day, nday=nxt[0], score=r.atip_score, signal=r.signal, c=x.close,
                            h=x.high, l=x.low, atrp=x.atrp))
            k += 1
            if k >= per_day:
                break
    return pd.DataFrame(out)


def entry_fill(bars: Bars, cand, mode: str, cal: list, fills: Fills = DEFAULT_FILLS):
    """(ts, price) of a candidate's buy under entry mode E0-E3, or None (module docstring)."""
    if cand.sym not in bars.d:
        return None
    ts, day, o, h, l, c = bars.d[cand.sym]
    up = 1 + fills.entry_slip_bps / 1e4
    nday = np.datetime64(pd.Timestamp(cand.nday), "D")
    if mode in ("E0", "E1"):
        trig = cand.c * 1.005
        for i in np.flatnonzero(day == nday):
            if o[i] <= trig:
                px = o[i]
                break
            if l[i] <= trig:
                px = trig
                break
        else:
            return None
        px *= up
        if mode == "E1" and px < cand.c * 0.99:
            return None
        return pd.Timestamp(ts[i]), px
    if mode == "E2":
        lvl = max(cand.h, cand.c * 1.005)
        for i in np.flatnonzero(day == nday):
            if h[i] >= lvl:
                return pd.Timestamp(ts[i]), max(o[i], lvl) * up
        return None
    if mode == "E3":
        ok = [np.datetime64(x, "D") for x in cal if x >= pd.Timestamp(cand.nday)][:3]
        if not ok:
            return None
        for i in np.flatnonzero(np.isin(day, np.array(ok, dtype="datetime64[D]"))):
            if l[i] <= cand.l:
                return pd.Timestamp(ts[i]), min(o[i], cand.l) * up
        return None
    raise ValueError(f"entry mode {mode!r}")


def population(data: ReplayData, start, mode: str = "daily", end=None, exits: dict | None = None,
               entries=ENTRIES, fills: Fills = DEFAULT_FILLS, per_day: int = 10) -> pd.DataFrame:
    """Every rank-fill candidate from `start` bought by each entry mode and sold by each exit, Rs 25,000 a trade.
    mode "daily": daily bars only; "intraday": 15-minute bars where captured (pack pop2.py daily / intra)."""
    if mode not in ("daily", "intraday"):
        raise ValueError("mode is daily or intraday")
    start = pd.Timestamp(start)
    end = pd.Timestamp(end) if end is not None else data.last_day
    exits = {k: ExitSpec.from_dict(v) for k, v in (exits or EXITS).items()}
    bars = Bars(hybrid_bars(data.daily, data.bars, start, end, mode == "intraday"))
    dd = atr_pct(data.daily)
    cal = sorted(pd.Timestamp(x) for x in dd[(dd["date"] >= start) & (dd["date"].dt.day_of_week < 5)]["date"].unique())
    C = rank_fill_candidates(data, start, end, per_day, dd)
    log.info(f"population {mode} from {start.date()}: {len(C)} candidates on {C['sday'].nunique() if len(C) else 0} days")
    rows = []
    for m in entries:
        for r in C.itertuples(index=False):
            e = entry_fill(bars, r, m, cal, fills)
            if not e:
                continue
            for name, spec in exits.items():
                s = simulate(bars, r.sym, e[0], e[1], spec, atr_pct=r.atrp, fills=fills)
                if s:
                    rows.append(dict(entry=m, exit=name, sym=r.sym, sday=r.sday, ets=e[0], epx=e[1], c=r.c, **s))
    return pd.DataFrame(rows)


def population_summary(R: pd.DataFrame, split=None) -> pd.DataFrame:
    """Per entry and exit (pack summ2.py): n, win %, average net / win / loss %, profit factor, % still open, % stopped,
    and the average net % before (h1) and from (h2) `split`."""
    split = pd.Timestamp(split) if split is not None else None

    def summ(g):
        w, ls = g[g["net"] > 0], g[g["net"] <= 0]
        pf = w["net"].sum() / -ls["net"].sum() if len(ls) and ls["net"].sum() < 0 else np.nan
        out = dict(n=len(g), win=round(100 * len(w) / len(g), 1), avg_net=round(g["netpct"].mean(), 3),
                   avg_win=round(w["netpct"].mean(), 2), avg_loss=round(ls["netpct"].mean(), 2), pf=round(pf, 2),
                   open_pct=round(100 * (g["reason"] == "OPEN").mean(), 1),
                   stop_pct=round(100 * (g["reason"] == "Stop").mean(), 1))
        if split is not None:
            out["h1"] = round(g[g["sday"] < split]["netpct"].mean(), 3)
            out["h2"] = round(g[g["sday"] >= split]["netpct"].mean(), 3)
        return out

    rows = [dict(entry=e, exit=x, **summ(g)) for (e, x), g in R.groupby(["entry", "exit"], sort=False)]
    return pd.DataFrame(rows).sort_values(["entry", "avg_net"], ascending=[True, False]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════════════════

def _arm_exits() -> dict:
    """The exits of orders/arms.py ARMS (session -> ExitSpec), when that module is there."""
    try:
        from orders.arms import ARMS
    except Exception as e:                       # noqa: BLE001 -- the arms module is optional here
        log.warning(f"orders.arms not importable ({e}); --arms ignored")
        return {}
    return {f"{sid} {a.get('label', '')}".strip(): ExitSpec.from_arm(a) for sid, a in ARMS.items()}


def calibration_compare(data: ReplayData, fills_list=(PACK_FILLS, REALISTIC_FILLS)) -> tuple[pd.DataFrame, dict]:
    """The calibration under each fills setting, 10 and 20 bps on stop exits, and without the weekend bars."""
    variants = [(f.name, f, True) for f in fills_list]
    variants += [(f"stop {b} bps", replace(REALISTIC_FILLS, stop_slip_bps=float(b), name=f"stop{b}"), True)
                 for b in (10, 20)]
    variants += [("realistic, no weekend bars", REALISTIC_FILLS, False)]
    rows, tables = [], {}
    for label, f, wk in variants:
        R = calibrate(data, f, weekend_bars=wk)
        tables[label] = R
        rows.append(dict(setting=label, stop_slip_bps=f.stop_slip_bps, gap_fill_at_open=f.gap_fill_at_open,
                         weekend_bars=wk, **calibration_summary(R)))
    return pd.DataFrame(rows), tables


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research.session_ab_replay", description=__doc__.split("\n")[1])
    ap.add_argument("--db", required=True, help="SQLite file, opened read-only (a backup)")
    ap.add_argument("--out", required=True, help="directory for the CSV outputs")
    ap.add_argument("what", nargs="?", default="all", choices=["calib", "whatif", "population", "all"])
    ap.add_argument("--fills", default="pack,realistic", help="comma list of: pack, realistic")
    ap.add_argument("--stop-slip-bps", type=float, help="override the realistic fills' stop-exit slippage")
    ap.add_argument("--start", default="2026-07-28", help="population: first signal day")
    ap.add_argument("--end", help="population / what-if: last day (default: the last day in the file)")
    ap.add_argument("--mode", default="daily", choices=["daily", "intraday"], help="population bars")
    ap.add_argument("--split", default="2026-09-25", help="population: h1 / h2 split day")
    ap.add_argument("--arms", action="store_true", help="what-if: also the exits of orders/arms.py ARMS")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    fl = []
    for k in a.fills.split(","):
        f = FILLS[k.strip()]
        if a.stop_slip_bps is not None and f.name == "realistic":
            f = replace(f, stop_slip_bps=a.stop_slip_bps)
        fl.append(f)
    data = load(a.db)
    pd.set_option("display.width", 250)
    res = {}
    if a.what in ("calib", "all"):
        cmp_, tables = calibration_compare(data, fl)
        for f in fl:
            tables[f.name].to_csv(out / f"calibration_{f.name}.csv", index=False)
        cmp_.to_csv(out / "calibration_compare.csv", index=False)
        print(cmp_.to_string(index=False))
        res["calib"] = cmp_
    if a.what in ("whatif", "all"):
        exits = dict(EXITS)
        if a.arms:
            exits.update(_arm_exits())
        for f in fl:
            R = whatif(data, exits, f, end=a.end)
            S = whatif_summary(R)
            R.to_csv(out / f"whatif_trades_{f.name}.csv", index=False)
            S.to_csv(out / f"whatif_summary_{f.name}.csv")
            print(f"\nWHAT-IF ({f.name} fills): {R[['sid', 'sym', 'filled']].drop_duplicates().shape[0]} real entries")
            print(S.to_string())
            res[f"whatif_{f.name}"] = S
    if a.what in ("population", "all"):
        for f in fl:
            R = population(data, a.start, a.mode, a.end, fills=f)
            S = population_summary(R, a.split)
            R.to_csv(out / f"population_{a.mode}_{f.name}.csv", index=False)
            S.to_csv(out / f"population_summary_{a.mode}_{f.name}.csv", index=False)
            print(f"\nPOPULATION {a.mode} from {a.start} ({f.name} fills): "
                  + str({m: int((R['entry'] == m).sum() / len(EXITS)) for m in ENTRIES}))
            print(S.to_string(index=False))
            res[f"population_{f.name}"] = S
    return res


if __name__ == "__main__":
    main()
