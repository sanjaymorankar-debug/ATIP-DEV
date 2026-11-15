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
    "ATIP": ("ATIP score", "The master score: VPI, safety (100 − CRI), recovery, momentum, buy zone, trend, fundamentals "
                           "and institutional flow combined into one 0-100 number.", "higher"),
    "VPI": ("Volatility-Potential Index", "Energy to move without the noise: calm (low) volatility, trend strength, "
                                          "relative strength, liquidity and volume.", "higher"),
    "MRI": ("Momentum-Reversal Index", "A dip that is turning: a falling MACD histogram, RSI 40-55, trend strength "
                                       "(ADX), volume, EMA 9 over 21 and news.", "higher"),
    "RRI": ("Recovery Index", "How strongly the stock is bouncing off its 52-week low: RSI recovery, volume, "
                              "institutional buying and support.", "higher"),
    "ZPI": ("Buy-Zone Index", "Whether the price sits in a calm buy zone: support and trend vs the 200-day average, "
                              "trend strength, low volatility, delivery accumulation, sector and news.", "higher"),
    "CRI": ("Crash-Risk Index", "Downside and volatility risk. Lower is better; above 75 ATIP sells or avoids the "
                                "stock.", "lower"),
    "ACS": ("AI Confidence Score", "How much the model trusts today's signal for this stock.", "higher"),
    "BETA": ("Beta (1 year)", "Moves against Nifty 50: above 1 swings more than the market, below 1 less.", ""),
}


# The formula as implemented, in one line each (scores/engine.py compute_*; weights from weight_config as seeded). The
# score badges' tooltips and the legend use these, so "why is this stock scored this way" is one hover away.
FORMULAS = {
    "ATIP": "Weighted mean of the parts present: VPI .20 · SAFETY (100 − CRI) .20 · SPI .15 · RRI .10 · MRI .10 · "
            "MSI .10 · ZPI .10 · technical .10 · fundamental .10 · institutional .05",
    "VPI": "100 − ATR % ×12 (calm) .18 · ADX .15 · 20-day return vs Nifty 50 .12 · liquidity .10 · volume ratio .10 · RSI room .10 · "
           "EPS growth .10 · FII+DII flow .08 · news .07",
    "MRI": "100 − MACD histogram .25 · RSI 40-55 band .20 · ADX .15 · volume ratio .15 · EMA 9 > 21 .15 · news .10",
    "RRI": "Rise off the 52-week low .25 · above the 200-day average .20 · RSI − 30 .15 · volume .15 · DII flow .15 · news .10",
    "ZPI": "200-DMA support .15 · ADX .15 · calm ATR .10 · delivery accumulation .10 · RSI 30-50 .10 · sector rank .10 · "
           "200-DMA trend .10 · quiet volume .10 · news .10",
    "CRI": "ATR % ×15 .20 · debt/equity .20 · distribution volume .15 · below 200-DMA / death cross .15 · negative news .15 · "
           "weak market .15 — lower is better",
    "ACS": "90-day hit rate .25 · agreement of VPI/MRI/RRI/ZPI/MSI above 60 .20 · market regime .20 · data quality .15 · "
           "liquidity .10 · news confidence .10",
}
GLYPH = {"strong": "●", "neutral": "◐", "weak": "○"}


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


def badge(key: str, v, label: str | None = None) -> str:
    """A score badge: "VPI 72" with its colour band, the band's glyph and word (colour is never the only cue) and a
    tooltip with the meaning and the formula in one line. CRI is shown as CRI↓ and a low value is the strong one."""
    k = key.upper().replace("↓", "").strip()
    shown = label or ("CRI↓" if k == "CRI" else k)
    if v is None or v != v:
        return f'<span class="ax-score" title="{_e(describe(k))} No value for this stock.">{_e(shown)} —</span>'
    b = band(k, v)
    tip = f"{describe(k)} {FORMULAS.get(k, '')}".strip() + f" Now {float(v):.0f}: {b}."
    return (f'<span class="ax-score" data-band="{b}" title="{_e(tip)}" aria-label="{_e(shown)} {float(v):.0f}, {b}">'
            f'<span aria-hidden="true">{GLYPH.get(b, "")}</span>{_e(shown)} <b>{float(v):.0f}</b></span>')


def badges(snap: dict | None, keys=("ATIP", "VPI", "MRI", "RRI", "ZPI", "CRI", "ACS")) -> str:
    """The seven badges for a score snapshot {atip, vpi, ...} (orders/session_board._snapshot shape)."""
    if not snap:
        return '<span class="ax-muted">—</span>'
    return '<span class="ax-badges">' + "".join(badge(k, snap.get(k.lower())) for k in keys) + "</span>"


def abbr(key: str, text: str | None = None) -> str:
    """A score column header that says what the acronym means (hover, and the accessible name/description)."""
    k = key.upper().replace("↓", "").strip()
    return f'<abbr title="{_e(describe(k))}">{_e(text if text is not None else key)}</abbr>'


def _rules() -> list:
    try:
        from scores import engine as E
        b, lo = E.BUY_STRICT, E.BUY_LOOSE
        return [f"BUY: ATIP ≥ {lo['atip']}, VPI ≥ {lo['vpi']}, ZPI ≥ {lo['zpi']}, market health ≥ {lo['mh']} and CRI below "
                f"{E.BUY_MAX_CRI} (or the stricter set ATIP ≥ {b['atip']}, VPI ≥ {b['vpi']}, ZPI ≥ {b['zpi']}, ACS ≥ {b['acs']}).",
                f"SELL: ATIP below {E.SELL_ATIP_FLOOR}, or CRI above {E.SELL_CRI_DANGER} with a weak ATIP.",
                f"HOLD: ATIP {E.HOLD_ATIP_FLOOR} or more without a BUY; WAIT: ATIP below {E.HOLD_ATIP_FLOOR}, "
                f"or the market regime is high-risk."]
    except Exception:
        return []


def legend_html(uid: str = "scoreLegend") -> str:
    """A <details> panel (keyboard: Tab to it, Enter opens) with every definition, the colour bands and the signal
    rules. Colour is never the only cue: each band is named in words."""
    rows = "".join(f'<dt><abbr title="{_e(n)}">{_e(k if k != "BETA" else "Beta")}</abbr></dt><dd>{_e(n)}: {_e(w)}'
                   + (f'<br><span class="ax-small ax-muted">Formula: {_e(FORMULAS[k])}</span>' if k in FORMULAS else "") + '</dd>'
                   for k, (n, w, _) in DEFS.items())
    rules = "".join(f"<li>{_e(r)}</li>" for r in _rules())
    return (f'<details class="ax-legendbox" id="{_e(uid)}"><summary>What do the scores mean?</summary>'
            f'<dl class="ax-kv">{rows}</dl>'
            f'<p class="ax-small"><b>Colour bands (0–100):</b> <span class="sp sp-strong">70+ strong</span> '
            f'<span class="sp sp-neutral">45–69 neutral</span> <span class="sp sp-weak">below 45 weak</span>. '
            f'CRI is a risk, so it reads the other way: 30 or less is strong, above 55 is weak.</p>'
            f'{f"<ul class=ax-small>{rules}</ul>" if rules else ""}</details>')
