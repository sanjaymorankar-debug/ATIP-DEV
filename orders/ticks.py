"""
Tick-size rounding (KGF-04, 16 Nov 2026). Nothing in orders/, execution/, brokers/, data/ or strategy/ rounded a price
to the exchange tick, so ATIP wrote triggers such as 892.44 (INDHOTEL, tick 0.05) that NSE would reject. Paper accepted
them; a live order would not.

    tick_size(symbol)            the scrip master's tick (data/instruments.py, refreshed 08:00 IST), else NSE's price-band
                                 tick for the price (TICK_BANDS), else 0.05
    round_down(price, tick)      BUY limit, BUY trigger, a long position's stop trigger and its trailing moves
    round_up(price, tick)        SELL target (a long position's take-profit)
    to_tick(price, side, ...)    the same by order leg: ("BUY", "ENTRY") / ("SELL", "STOP") round down,
                                 ("SELL", "TARGET") rounds up

Arithmetic is decimal.Decimal throughout: a float such as 3708.135 is 3708.13499999... in binary, and a float floor
would turn it into 3708.1 by luck on one machine and 3708.0 on another input. Prices enter as Decimal(repr(float)),
the shortest exact decimal form of the float, and leave as float only at the end (the database columns are REAL).

    SIEMENS   3903.3 x 0.95  = 3708.135 -> 3708.1   (tick 0.10)
    INDHOTEL  888.00 x 1.005 = 892.44   -> 892.40   (tick 0.05)
    MOTHERSON 159.34                    -> 159.34   (tick 0.01)
"""

from __future__ import annotations

import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

log = logging.getLogger("atip.ticks")

# NSE cash-market tick by price band (NSE circular, effective 2025): the fallback when the scrip master has no row.
TICK_BANDS = (
    (Decimal("250"), Decimal("0.01")),
    (Decimal("1000"), Decimal("0.05")),
    (Decimal("5000"), Decimal("0.10")),
    (Decimal("10000"), Decimal("0.50")),
    (Decimal("20000"), Decimal("1.00")),
)
TOP_TICK = Decimal("5.00")
DEFAULT_TICK = Decimal("0.05")


def D(x) -> Decimal:
    """A price as Decimal: a Decimal stays, a float goes in by its shortest repr, an int or numeric string as written."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    try:
        return Decimal(str(x).strip())
    except InvalidOperation:
        raise ValueError(f"not a price: {x!r}")


def band_tick(price) -> Decimal:
    """NSE's tick for a price under the price-band table: 0.01 below Rs 250, 0.05 to 1,000, 0.10 to 5,000, ..."""
    p = D(price)
    for upto, tick in TICK_BANDS:
        if p < upto:
            return tick
    return TOP_TICK


def _floor_to(p: Decimal, tick: Decimal) -> Decimal:
    return (p / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _ceil_to(p: Decimal, tick: Decimal) -> Decimal:
    return (p / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def round_down(price, tick) -> Decimal:
    """The highest multiple of tick at or below price (BUY limit / trigger, long stop)."""
    t = D(tick)
    if t <= 0:
        raise ValueError(f"tick must be positive, got {tick!r}")
    return _floor_to(D(price), t).quantize(t)


def round_up(price, tick) -> Decimal:
    """The lowest multiple of tick at or above price (SELL target of a long)."""
    t = D(tick)
    if t <= 0:
        raise ValueError(f"tick must be positive, got {tick!r}")
    return _ceil_to(D(price), t).quantize(t)


def mul(price, factor) -> Decimal:
    """price x factor in Decimal (888 x 1.005 = 892.44 exactly, not 892.4399999)."""
    return D(price) * D(factor)


def tick_size(symbol: str | None, price=None, conn=None) -> Decimal:
    """The symbol's tick from the instrument master when it has one, else the price band's, else 0.05."""
    if symbol:
        try:
            from data.instruments import lookup
            row = lookup(symbol, conn=conn)
            if row and row.get("tick_size"):
                t = D(row["tick_size"])
                if t > 0:
                    return t
        except Exception as e:                  # no master yet (fresh install, tests): the band table
            log.debug(f"  tick for {symbol}: instrument master unavailable ({e})")
    if price is not None:
        try:
            return band_tick(price)
        except ValueError:
            pass
    return DEFAULT_TICK


def to_tick(price, side: str = "BUY", role: str = "ENTRY", symbol: str | None = None, tick=None, conn=None) -> float:
    """price rounded the safe way for its leg, as float for the REAL columns: a BUY (entry or trigger) and a long's
    STOP round down, a long's TARGET (a SELL above the fill) rounds up. A short's legs mirror that."""
    if price is None:
        return None
    t = D(tick) if tick is not None else tick_size(symbol, price, conn)
    side, role = str(side or "BUY").upper(), str(role or "ENTRY").upper()
    if role == "TARGET":
        up = side == "SELL"                 # a long's take-profit
    elif role == "STOP":
        up = side == "BUY"                  # a short's stop-buy sits above
    else:
        up = side == "SELL"                 # a SELL entry (a short) asks at or above
    return float(round_up(price, t) if up else round_down(price, t))


def is_on_tick(price, tick) -> bool:
    return D(price) % D(tick) == 0
