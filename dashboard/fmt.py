"""
The one number formatter of the UI (UI fixes v2, 3 Nov 2026; docs/UI_DESIGN_SYSTEM.md "Numbers").

Every money figure, percentage, price and score on a page goes through here (server side) or through its mirror
AX.fmt in dashboard/static/atip/atip.js (browser side): the two give the same text for the same value.

    fmt_inr(v, sign=False, compact=False)   ₹1,23,456.78 (Indian grouping, paise); sign: + / − on P&L;
                                            compact: ₹1.23 L / ₹2.10 Cr / ₹25.7K for tiles and axes
    fmt_pct(v, sign=True, inverse=False)    +3.48% / −0.18%; tone() says up / down / flat;
                                            inverse=True for metrics where a rise is bad (India VIX):
                                            a rise reads "warn" (amber/red), a fall "good"
    fmt_price(v, tick=None)                 a price rounded to the NSE tick for its band (tick_for), 2 dp
    fmt_score(v)                            a 0-100 score as a whole number (60.73 -> 61), everywhere
    fmt_num(v, dp=2)                        a plain number, Indian grouping
    tone(v, inverse=False)                  "up" | "down" | "flat" | "none" (missing) -- with inverse, "up" means
                                            the value rose and that is bad
    pct_html / inr_html / score_html        the same, as HTML: colour AND an arrow AND a sign (colour is never
                                            the only cue); a missing value is a grey "—" with a title saying why

Missing is never a number: None, "", NaN and inf all read "—" (grey), never ₹0.00, never 0.00%, never "None".
A change of exactly zero is "0.00%" in the flat (grey) tone, never red.
"""

from __future__ import annotations

import html
import math
import re

DASH = "—"
MINUS = "−"

# NSE equity tick sizes by price band (NSE circular, effective 10 Jun 2025): (upper bound exclusive, tick)
TICK_BANDS = ((250.0, 0.01), (1000.0, 0.05), (5000.0, 0.10), (10000.0, 0.50), (20000.0, 1.00), (math.inf, 5.00))


def _e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def num(v):
    """float(v), or None when v is missing / not a finite number."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "").replace("₹", "").strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _group(whole: str) -> str:
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    return ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1] + "," + tail


def fmt_num(v, dp: int = 2) -> str:
    f = num(v)
    if f is None:
        return DASH
    whole, _, frac = f"{abs(f):.{dp}f}".partition(".")
    return (MINUS if f < 0 and round(abs(f), dp) else "") + _group(whole) + (f".{frac}" if dp else "")


def fmt_inr(v, sign: bool = False, compact: bool = False, symbol: bool = True) -> str:
    f = num(v)
    if f is None:
        return DASH
    neg, pos = f < -0.004, f > 0.004
    pre = MINUS if neg else ("+" if sign and pos else "")
    cur = "₹" if symbol else ""
    a = abs(f)
    if compact:
        if a >= 1e7:
            return f"{pre}{cur}{a / 1e7:.2f} Cr"
        if a >= 1e5:
            return f"{pre}{cur}{a / 1e5:.2f} L"
        if a >= 1e3:
            return f"{pre}{cur}{a / 1e3:.1f}K"
        return f"{pre}{cur}{a:.0f}"
    whole, frac = f"{a:.2f}".split(".")
    return f"{pre}{cur}{_group(whole)}.{frac}"


def tick_for(price) -> float:
    f = num(price)
    if f is None:
        return 0.05
    for upper, tick in TICK_BANDS:
        if abs(f) < upper:
            return tick
    return 5.0


def round_tick(price, tick=None):
    f = num(price)
    if f is None:
        return None
    t = float(tick or tick_for(f))
    return round(round(f / t) * t, 2)


def fmt_price(v, tick=None, symbol: bool = True) -> str:
    f = round_tick(v, tick)
    if f is None:
        return DASH
    return fmt_inr(f, symbol=symbol)


def fmt_score(v) -> str:
    f = num(v)
    if f is None:
        return DASH
    return str(int(math.floor(f + 0.5)))


def tone(v, inverse: bool = False, eps: float = 0.005) -> str:
    """up / down / flat / none. The caller decides the colour from (tone, inverse)."""
    f = num(v)
    if f is None:
        return "none"
    if abs(f) < eps:
        return "flat"
    return "up" if f > 0 else "down"


def fmt_pct(v, sign: bool = True, dp: int = 2) -> str:
    f = num(v)
    if f is None:
        return DASH
    if abs(f) < 0.5 * 10 ** -dp:
        return f"{0:.{dp}f}%"
    return f"{('+' if f > 0 else MINUS) if sign else (MINUS if f < 0 else '')}{abs(f):.{dp}f}%"


def tone_class(v, inverse: bool = False) -> str:
    """The CSS class for a change: ax-pos (good), ax-neg (bad), ax-warn (an inverse metric rising), ax-flat."""
    t = tone(v)
    if t == "none":
        return "ax-muted"
    if t == "flat":
        return "ax-flat"
    if inverse:
        return "ax-warn-t" if t == "up" else "ax-pos"
    return "ax-pos" if t == "up" else "ax-neg"


def arrow(v) -> str:
    t = tone(v)
    return "▲" if t == "up" else "▼" if t == "down" else ""


def missing_html(why: str = "") -> str:
    return (f'<span class="ax-muted ax-missing" title="{_e(why or "No value")}" '
            f'aria-label="{_e(why or "No value")}">{DASH}</span>')


def pct_html(v, inverse: bool = False, why: str = "", sign: bool = True) -> str:
    """A change as HTML: tone colour + ▲/▼ + sign. Missing -> grey dash with `why` as its tooltip."""
    if num(v) is None:
        return missing_html(why)
    a = arrow(v) if sign else ""
    return (f'<span class="ax-num {tone_class(v, inverse) if sign else ""}">'
            f'{a + " " if a else ""}{_e(fmt_pct(v, sign))}</span>')


def inr_html(v, sign: bool = False, why: str = "", compact: bool = False) -> str:
    if num(v) is None:
        return missing_html(why)
    cls = tone_class(v) if sign else ""
    a = arrow(v) if sign else ""
    return f'<span class="ax-num {cls}">{a + " " if a else ""}{_e(fmt_inr(v, sign, compact))}</span>'


def score_html(v, why: str = "") -> str:
    if num(v) is None:
        return missing_html(why)
    return f'<span class="ax-num">{fmt_score(v)}</span>'


def live_stats(stats: dict, ltp) -> dict:
    """G6: a stock's 52-week range and returns moved by the live price -- the high / low take the LTP in, and the
    returns are measured from it, so a 52-week high is never below the price on the screen."""
    f = num(ltp)
    out = dict(stats or {})
    if f is None or f <= 0:
        return out
    last = num(out.get("last_close"))
    for k, fn in (("high_52w", max), ("high_all", max), ("low_52w", min), ("low_all", min)):
        if num(out.get(k)) is not None:
            out[k] = fn(float(out[k]), f)
    if last:
        for k in [k for k in out if k.startswith("ret_") and num(out.get(k)) is not None]:
            base = last / (1 + float(out[k]) / 100)
            out[k] = round((f / base - 1) * 100, 2) if base else out[k]
        prev = num(out.get("prev_close"))
        if prev:
            out["change_pct"] = round((f / prev - 1) * 100, 2)
    out["ltp"] = f
    return out
