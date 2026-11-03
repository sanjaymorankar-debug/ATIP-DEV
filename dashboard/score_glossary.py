"""
What each score column means (UX audit H5, 3 Nov 2026): one place for the one-line definitions and the colour bands, so
the ATIP Scores page, its Top-25 tabs and the Screener explain the same acronym the same way.

    DEFS            key -> (full name, one-line meaning, "higher" | "lower" is better)
    band(key, v)    "strong" | "neutral" | "weak" for a 0-100 score, CRI read the other way round
    abbr(key, txt)  a column header: <abbr title> plus an accessible description
    legend_html()   the keyboard-reachable "What do the scores mean?" panel, with the real bands and signal rules

The bands are the ones the pills have always been coloured by (dashboard/server.py pill(): 70 and 45); the signal rules
are scores/engine.py's own thresholds, read at render time so the legend cannot drift from the engine.
"""

from __future__ import annotations

import html

STRONG, NEUTRAL = 70, 45          # pill colour bands: >= 70 strong, 45-69 neutral, < 45 weak

DEFS = {
    "ATIP": ("ATIP score", "The master score: VPI, recovery, momentum, buy zone, trend, fundamentals and institutional "
                           "flow combined into one 0-100 number.", "higher"),
    "VPI": ("Volatility-Potential Index", "Room to move: volatility, trend strength, relative strength, liquidity "
                                          "and volume.", "higher"),
    "MRI": ("Momentum-Reversal Index", "Momentum now: MACD, RSI, ADX, volume, moving averages and news.", "higher"),
    "RRI": ("Recovery Index", "How strongly the stock is bouncing off its 52-week low: RSI recovery, volume, "
                              "institutional buying and support.", "higher"),
    "ZPI": ("Buy-Zone Index", "Whether the price sits in a buy zone: technicals, institutional and delivery "
                              "accumulation, sector and news.", "higher"),
    "CRI": ("Crash-Risk Index", "Downside and volatility risk. Lower is better; above 75 ATIP sells or avoids the "
                                "stock.", "lower"),
    "ACS": ("AI Confidence Score", "How much the model trusts today's signal for this stock.", "higher"),
    "BETA": ("Beta (1 year)", "Moves against Nifty 50: above 1 swings more than the market, below 1 less.", ""),
}


def _e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def band(key: str, v) -> str:
    """strong / neutral / weak for a 0-100 score; CRI is a risk, so a low CRI is the strong one."""
    if v is None:
        return ""
    x = float(v)
    if key.upper().startswith("CRI"):
        x = 100 - x
    return "strong" if x >= STRONG else "neutral" if x >= NEUTRAL else "weak"


def describe(key: str) -> str:
    k = key.upper().rstrip("↓ ").replace("CRI↓", "CRI")
    name, what, better = DEFS.get(k, (key, "", ""))
    tail = f" {'Higher' if better == 'higher' else 'Lower'} is better." if better and "better" not in what else ""
    return f"{name}: {what}{tail}"


def abbr(key: str, text: str | None = None) -> str:
    """A score column header that says what the acronym means (hover, and the accessible name/description)."""
    k = key.upper().replace("↓", "").strip()
    return f'<abbr title="{_e(describe(k))}">{_e(text if text is not None else key)}</abbr>'


def _rules() -> list:
    try:
        from scores import engine as E
        b, lo = E.BUY_STRICT, E.BUY_LOOSE
        return [f"BUY: ATIP ≥ {lo['atip']}, VPI ≥ {lo['vpi']}, ZPI ≥ {lo['zpi']} and market health ≥ {lo['mh']} "
                f"(or the stricter set ATIP ≥ {b['atip']}, VPI ≥ {b['vpi']}, ZPI ≥ {b['zpi']}, ACS ≥ {b['acs']}).",
                f"SELL: ATIP below {E.SELL_ATIP_FLOOR}, or CRI above {E.SELL_CRI_DANGER} with a weak ATIP.",
                f"HOLD: ATIP {E.HOLD_ATIP_FLOOR} or more without a BUY; WAIT: ATIP below {E.HOLD_ATIP_FLOOR}, "
                f"or the market regime is high-risk."]
    except Exception:
        return []


def legend_html(uid: str = "scoreLegend") -> str:
    """A <details> panel (keyboard: Tab to it, Enter opens) with every definition, the colour bands and the signal
    rules. Colour is never the only cue: each band is named in words."""
    rows = "".join(f'<dt><abbr title="{_e(n)}">{_e(k if k != "BETA" else "Beta")}</abbr></dt><dd>{_e(n)}: {_e(w)}</dd>'
                   for k, (n, w, _) in DEFS.items())
    rules = "".join(f"<li>{_e(r)}</li>" for r in _rules())
    return (f'<details class="ax-legendbox" id="{_e(uid)}"><summary>What do the scores mean?</summary>'
            f'<dl class="ax-kv">{rows}</dl>'
            f'<p class="ax-small"><b>Colour bands (0–100):</b> <span class="sp sp-strong">70+ strong</span> '
            f'<span class="sp sp-neutral">45–69 neutral</span> <span class="sp sp-weak">below 45 weak</span>. '
            f'CRI is a risk, so it reads the other way: 30 or less is strong, above 55 is weak.</p>'
            f'{f"<ul class=ax-small>{rules}</ul>" if rules else ""}</details>')
