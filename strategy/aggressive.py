"""
Aggressive exit management: book half at +3%, let the rest run.

    entry ─────────────────────────────────────────────────────────────
      │
      ├─ before T1 ....... initial stop protects 100%
      ├─ at +3% (T1) ..... sell 50%, keep 50% as a runner, start trailing
      ├─ at +6% (T2) ..... CHECKPOINT, not an exit. Momentum decides:
      │                      strong  -> hold, keep trailing
      │                      neutral -> hold, tighten the trail
      │                      weak    -> exit the runner now
      └─ trail hit ....... exit whatever is left

The point of T2 being a checkpoint rather than a sale is that a fixed 6% cap
truncates exactly the trades that pay for the losers. Last session's backtest on
this database said as much: a flat 3% target was negative after costs and 6% only
about breakeven, because both throw away the right tail.

Everything here is deliberately split in two:

  * the DECISION functions (`checkpoint_decision`, `trail_distance_pct`,
    `partial_exit_quantity`, `realised_pnl` ...) are pure — plain numbers in,
    plain values out, no database, no broker, no clock. They are the part worth
    testing exhaustively, and they are what the backtest replays.
  * the STATE functions (`open_position`, `record_partial_exit` ...) persist to
    `strategy_position` and are idempotent, so a repeated market event can never
    sell the same shares twice.

Nothing here changes VPI/SPI/ZPI/RRI/MRI/CRI/MSI/ACS or Market Health. It only
reads them.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

from db.schema import get_connection

log = logging.getLogger("atip.strategy")

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG  — one place, overridable from atip_data/config.json
# ═══════════════════════════════════════════════════════════════════════════

DEFAULTS = {
    "aggressive_enabled":        False,  # opt-in; existing brackets unchanged when off
    "target_1_pct":              3.0,    # first profit target
    "target_1_exit_pct":         50.0,   # % of the ORIGINAL position sold there
    "target_2_pct":              6.0,    # momentum checkpoint, NOT an auto-exit
    "trailing_enabled":          True,
    # These three are set from measurement, not taste. Replaying 3,000 real
    # entries through backtest_history at several multipliers, expectancy rose
    # monotonically as the trail widened — at 2x ATR the strategy lost 0.31% per
    # trade, at 4x it lost 0.06%, at 6x it made 0.12% and only then beat a plain
    # 6% target. A tight trail was cutting winners, not protecting them.
    #
    # 4x is a deliberate compromise: clearly better than the 2x this started at,
    # without sitting at the edge of the tested range where the trail fires on
    # barely 12% of trades and the result is really just "hold longer". Widening
    # further looked better in-sample on random entries over one regime, which is
    # exactly the kind of edge that does not survive contact with a live market.
    "trail_atr_mult":            4.0,    # trail distance = mult x ATR%, when ATR is known
    "trail_min_pct":             3.0,    # floor, so a quiet stock still has room
    "trail_max_pct":            20.0,    # ceiling, so a wild stock isn't given away
    "trail_tighten_factor":      0.5,    # multiply the distance by this when momentum fades
    "momentum_strong_score":     60.0,   # composite >= this  -> let it run
    "momentum_weak_score":       40.0,   # composite <  this  -> exit the runner
    "execution_mode":            "PAPER",  # PAPER | LIVE — LIVE needs explicit opt-in
}

_CONFIG_PATH = Path("atip_data") / "config.json"
_cache: dict = {}


def config(refresh: bool = False) -> dict:
    """Strategy settings: defaults, overlaid with anything in config.json."""
    global _cache
    if _cache and not refresh:
        return _cache
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    except Exception:
        pass                      # defaults are a complete, valid config on their own
    _cache = validate_config(cfg)
    return _cache


class ConfigError(ValueError):
    """A strategy configuration that would misbehave if it were allowed through."""


def validate_config(cfg: dict) -> dict:
    """
    Reject settings that are arithmetically impossible before they reach a live
    position, rather than discovering them as a stuck or oversized exit.
    """
    t1, t2 = float(cfg["target_1_pct"]), float(cfg["target_2_pct"])
    exit_pct = float(cfg["target_1_exit_pct"])
    if t1 <= 0:
        raise ConfigError(f"target_1_pct must be > 0, got {t1}")
    if t2 <= t1:
        raise ConfigError(f"target_2_pct ({t2}) must be above target_1_pct ({t1})")
    if not 0 < exit_pct < 100:
        raise ConfigError(f"target_1_exit_pct must be between 0 and 100 exclusive, "
                          f"got {exit_pct} — 100 would leave no runner, 0 no partial exit")
    lo, hi = float(cfg["trail_min_pct"]), float(cfg["trail_max_pct"])
    if lo <= 0 or hi <= 0 or lo > hi:
        raise ConfigError(f"trail_min_pct/trail_max_pct invalid: {lo}/{hi}")
    weak, strong = float(cfg["momentum_weak_score"]), float(cfg["momentum_strong_score"])
    if weak > strong:
        raise ConfigError(f"momentum_weak_score ({weak}) must not exceed "
                          f"momentum_strong_score ({strong})")
    if str(cfg["execution_mode"]).upper() not in ("PAPER", "LIVE"):
        raise ConfigError(f"execution_mode must be PAPER or LIVE, got {cfg['execution_mode']}")
    return cfg


def is_live(cfg: dict = None) -> bool:
    """True only when execution_mode is explicitly LIVE. Anything else is paper."""
    cfg = cfg or config()
    return str(cfg.get("execution_mode", "PAPER")).upper() == "LIVE"


# ═══════════════════════════════════════════════════════════════════════════
#  STATES / EXIT REASONS
# ═══════════════════════════════════════════════════════════════════════════

ST_OPEN      = "OPEN"            # full size, below T1
ST_RUNNER    = "RUNNER"          # T1 booked, remainder trailing
ST_CLOSED    = "CLOSED"

INITIAL_STOP_LOSS      = "INITIAL_STOP_LOSS"
TARGET_1_PARTIAL_EXIT  = "TARGET_1_PARTIAL_EXIT"
TRAILING_STOP          = "TRAILING_STOP"
TARGET_2_MOMENTUM_EXIT = "TARGET_2_MOMENTUM_EXIT"
MOMENTUM_REVERSAL      = "MOMENTUM_REVERSAL"
MARKET_RISK_EXIT       = "MARKET_RISK_EXIT"
MANUAL_EXIT            = "MANUAL_EXIT"
END_OF_DAY_EXIT        = "END_OF_DAY_EXIT"

# Checkpoint verdicts
HOLD_RUNNER   = "HOLD_RUNNER"
TIGHTEN_TRAIL = "TIGHTEN_TRAIL"
EXIT_RUNNER   = "EXIT_RUNNER"


# ═══════════════════════════════════════════════════════════════════════════
#  PURE DECISION LOGIC
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Momentum:
    """
    A read of the existing ATIP indicators at checkpoint time.

    Every field is optional because real data has holes, and a missing indicator
    must not be read as a bearish one — `composite()` averages only what is
    actually present and reports how much that was.
    """
    zpi: float | None = None            # momentum/trend composite
    msi: float | None = None            # market structure
    mri: float | None = None            # momentum-reversal risk (INVERTED: high = bad)
    cri: float | None = None            # crash risk (INVERTED: high = bad)
    rsi_14: float | None = None
    macd_hist: float | None = None
    volume_ratio: float | None = None   # vs 20-day average
    mh_score: float | None = None       # market health

    def composite(self) -> tuple[float | None, int]:
        """
        Blend the available readings onto one 0-100 scale where higher is
        stronger. Returns (score, how_many_inputs_contributed).

        MRI and CRI are risk measures, so they enter inverted — a CRI of 80 is
        not strength, it is the opposite. Getting that backwards would make the
        runner hold hardest exactly when a crash is most likely.
        """
        parts: list[float] = []
        if self.zpi is not None:
            parts.append(_clamp(self.zpi))
        if self.msi is not None:
            parts.append(_clamp(self.msi))
        if self.mri is not None:
            parts.append(100.0 - _clamp(self.mri))
        if self.cri is not None:
            parts.append(100.0 - _clamp(self.cri))
        if self.mh_score is not None:
            parts.append(_clamp(self.mh_score))
        if self.rsi_14 is not None:
            # RSI is already 0-100 and directional, but its useful band is
            # roughly 30-70; stretch that band across the scale.
            parts.append(_clamp((self.rsi_14 - 30.0) / 40.0 * 100.0))
        if self.macd_hist is not None:
            # Sign carries the information; magnitude varies by price level, so
            # only the direction is used.
            parts.append(65.0 if self.macd_hist > 0 else 35.0)
        if self.volume_ratio is not None:
            # Conviction: above-average volume supports the move, thin volume
            # undercuts it. 1.0 is neutral.
            parts.append(_clamp(50.0 + (self.volume_ratio - 1.0) * 25.0))
        if not parts:
            return None, 0
        return round(sum(parts) / len(parts), 2), len(parts)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


@dataclass
class Decision:
    verdict: str
    reason: str
    composite: float | None = None
    inputs: int = 0
    trail_pct: float | None = None
    exit_reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def checkpoint_decision(momentum: Momentum, cfg: dict = None,
                        current_trail_pct: float | None = None,
                        atr_pct: float | None = None) -> Decision:
    """
    The +6% question: does the runner keep running?

    Deliberately NOT a sell. Three outcomes:

      strong  -> HOLD_RUNNER   keep the trail where it is
      neutral -> TIGHTEN_TRAIL keep the position, halve the give-back
      weak    -> EXIT_RUNNER   take the 6% and leave

    With no usable indicator at all the answer is TIGHTEN_TRAIL, not EXIT: the
    trailing stop is already protecting a +6% gain, so the safe default is to
    keep that protection and let the market decide, rather than to guess.
    """
    cfg = cfg or config()
    score, n = momentum.composite()
    base_trail = trail_distance_pct(atr_pct, cfg)
    if current_trail_pct is not None:
        base_trail = current_trail_pct

    if score is None:
        return Decision(TIGHTEN_TRAIL,
                        "no momentum inputs available — protecting the gain instead of guessing",
                        None, 0, tighten(base_trail, cfg))

    if score >= float(cfg["momentum_strong_score"]):
        return Decision(HOLD_RUNNER,
                        f"momentum {score:.1f} at or above strong threshold "
                        f"{cfg['momentum_strong_score']:.0f} — let it run",
                        score, n, base_trail)

    if score < float(cfg["momentum_weak_score"]):
        return Decision(EXIT_RUNNER,
                        f"momentum {score:.1f} below weak threshold "
                        f"{cfg['momentum_weak_score']:.0f} — bank the runner",
                        score, n, base_trail, TARGET_2_MOMENTUM_EXIT)

    return Decision(TIGHTEN_TRAIL,
                    f"momentum {score:.1f} between {cfg['momentum_weak_score']:.0f} and "
                    f"{cfg['momentum_strong_score']:.0f} — hold, but give back less",
                    score, n, tighten(base_trail, cfg))


def tighten(trail_pct: float, cfg: dict = None) -> float:
    """Shrink the trailing distance, never below the configured floor."""
    cfg = cfg or config()
    return round(max(float(cfg["trail_min_pct"]),
                     trail_pct * float(cfg["trail_tighten_factor"])), 2)


def trail_distance_pct(atr_pct: float | None, cfg: dict = None) -> float:
    """
    How far below the high-water mark the trailing stop sits.

    Volatility-aware where ATR is known, because one fixed percentage is either
    too tight for a volatile stock (stopped out by noise) or too loose for a
    quiet one (gives back the whole move). Clamped at both ends so a bad ATR
    cannot produce an absurd stop.
    """
    cfg = cfg or config()
    lo, hi = float(cfg["trail_min_pct"]), float(cfg["trail_max_pct"])
    if atr_pct is None or atr_pct <= 0:
        return round(min(hi, max(lo, float(cfg["target_1_pct"]))), 2)
    return round(min(hi, max(lo, atr_pct * float(cfg["trail_atr_mult"]))), 2)


def partial_exit_quantity(original_qty: int, cfg: dict = None) -> int:
    """
    Shares to sell at T1, always leaving at least one share as the runner.

    Measured against the ORIGINAL quantity, so a re-entry or a second call can
    never compound it, and rounded down so the runner is never short-changed.
    A 1-share position cannot be split: it stays whole and rides to the trail.
    """
    cfg = cfg or config()
    original_qty = int(original_qty)
    if original_qty <= 1:
        return 0
    qty = int(original_qty * float(cfg["target_1_exit_pct"]) / 100.0)
    return max(1, min(qty, original_qty - 1))


def target_price(entry_price: float, pct: float, long_side: bool = True) -> float:
    """Price at which a long is up `pct`, mirrored for a short."""
    return round(entry_price * (1 + pct / 100.0) if long_side
                 else entry_price * (1 - pct / 100.0), 2)


def new_trail_stop(high_water: float, trail_pct: float, long_side: bool = True) -> float:
    return round(high_water * (1 - trail_pct / 100.0) if long_side
                 else high_water * (1 + trail_pct / 100.0), 2)


def ratchet(current_stop: float | None, high_water: float, trail_pct: float,
            long_side: bool = True) -> float:
    """
    Move the stop toward the high-water mark and NEVER away from it.

    This one-way property is the whole point of a trailing stop; a stop that can
    loosen on a pullback is just a wider stop that pretends otherwise.
    """
    candidate = new_trail_stop(high_water, trail_pct, long_side)
    if current_stop is None:
        return candidate
    return round(max(current_stop, candidate) if long_side
                 else min(current_stop, candidate), 2)


def realised_pnl(entry_price: float, exit_price: float, qty: int,
                 long_side: bool = True, cost_pct: float = 0.0) -> float:
    """
    Booked P&L on `qty` shares, optionally net of round-trip costs.

    cost_pct is the full round-trip (brokerage + STT + slippage) as a percentage
    of turnover — last session's backtest put a realistic figure near 0.25%, and
    a 3% target does not survive being measured without it.
    """
    gross = (exit_price - entry_price) * qty
    if not long_side:
        gross = -gross
    costs = (entry_price + exit_price) * qty * (cost_pct / 100.0)
    return round(gross - costs, 2)


def unrealised_pnl(entry_price: float, last_price: float, qty: int,
                   long_side: bool = True) -> float:
    if qty <= 0:
        return 0.0
    gross = (last_price - entry_price) * qty
    return round(gross if long_side else -gross, 2)
