"""
Asset-class registry (W12): what ATIP's wealth view supports and how each class
is valued.

ALLOCATION CLASSES (what W14 allocates between and W15 rebalances):
    EQUITY         Indian listed shares and equity ETFs
    INTL_EQUITY    international shares and international-equity ETFs
    BONDS          government / corporate bonds, T-bills, bond and gilt ETFs
    GOLD           gold ETFs, sovereign gold bonds, physical / digital gold
    SILVER         silver ETFs, physical silver
    CASH           bank / savings balances, liquid ETFs
    REAL_ESTATE    property (manual valuation)
    OTHER          anything else the owner tracks (manual valuation)

EXCLUDED (brief section 3, not implemented; the API refuses them with a clear
message): MUTUAL_FUND, FD, NPS, INSURANCE. They are listed here only so the
refusal is explicit and a later release can switch one on in one place.

INSTRUMENTS carry the valuation method:
    MARKET       quantity x ATIP mark price for `symbol` (live quote today, else last
                 close in prices_daily)
    GOLD_SPOT    grams x international spot (global_markets.gold, USD/oz) x USD/INR
                 / 31.1035 x (1 + gold_domestic_premium_pct). The premium approximates
                 Indian import duty + GST; it is an assumption, shown with the value.
    SILVER_SPOT  same for silver (global_markets.silver)
    FX_MANUAL    quantity x manual price in a foreign currency x FX (USD/INR from
                 global_markets for USD, otherwise the fx_rate entered)
    MANUAL       quantity x the last manual price entered
    CASH         quantity is the amount in rupees

LISTED-SYMBOL CLASSIFICATION (broker holdings and MARKET holdings): an explicit
map of known ETFs first, then a per-owner override (wealth_classification),
otherwise an Indian share (EQUITY / STOCK). Symbols that merely contain "GOLD"
or "ETF" (GOLDIAM, JETFREIGHT, ...) are companies, not ETFs, which is why this
is a list and not a pattern.
"""

from __future__ import annotations

ALLOCATION_CLASSES = {
    "EQUITY": {"label": "Indian equity", "liquidity": "HIGH", "growth": True},
    "INTL_EQUITY": {"label": "International equity", "liquidity": "HIGH", "growth": True},
    "BONDS": {"label": "Bonds / debt", "liquidity": "MEDIUM", "growth": False},
    "GOLD": {"label": "Gold", "liquidity": "HIGH", "growth": False},
    "SILVER": {"label": "Silver", "liquidity": "HIGH", "growth": False},
    "CASH": {"label": "Cash & liquid", "liquidity": "HIGH", "growth": False},
    "REAL_ESTATE": {"label": "Real estate", "liquidity": "LOW", "growth": True},
    "OTHER": {"label": "Other", "liquidity": "LOW", "growth": False},
}

EXCLUDED_CLASSES = {
    "MUTUAL_FUND": "Mutual funds are outside ATIP's current scope (Waves 11-20 exclusion).",
    "FD": "Fixed deposits are outside ATIP's current scope (Waves 11-20 exclusion).",
    "NPS": "NPS is outside ATIP's current scope (Waves 11-20 exclusion).",
    "INSURANCE": "Insurance is outside ATIP's current scope (Waves 11-20 exclusion).",
}

INSTRUMENTS = {
    # instrument: (allowed classes, default valuation, allowed valuations)
    "STOCK": (("EQUITY", "INTL_EQUITY"), "MARKET", ("MARKET", "FX_MANUAL", "MANUAL")),
    "ETF": (("EQUITY", "INTL_EQUITY", "BONDS", "GOLD", "SILVER", "CASH"), "MARKET", ("MARKET", "MANUAL")),
    "BOND": (("BONDS",), "MANUAL", ("MARKET", "MANUAL")),
    "TBILL": (("BONDS",), "MANUAL", ("MANUAL",)),
    "SGB": (("GOLD",), "GOLD_SPOT", ("GOLD_SPOT", "MARKET", "MANUAL")),
    "PHYSICAL_GOLD": (("GOLD",), "GOLD_SPOT", ("GOLD_SPOT", "MANUAL")),
    "DIGITAL_GOLD": (("GOLD",), "GOLD_SPOT", ("GOLD_SPOT", "MANUAL")),
    "PHYSICAL_SILVER": (("SILVER",), "SILVER_SPOT", ("SILVER_SPOT", "MANUAL")),
    "BANK_ACCOUNT": (("CASH",), "CASH", ("CASH",)),
    "PROPERTY": (("REAL_ESTATE",), "MANUAL", ("MANUAL",)),
    "OTHER": (tuple(ALLOCATION_CLASSES), "MANUAL", ("MANUAL", "MARKET")),
}
VALUATIONS = ("MARKET", "GOLD_SPOT", "SILVER_SPOT", "FX_MANUAL", "MANUAL", "CASH")
TROY_OUNCE_GRAMS = 31.1035

# Known NSE ETFs by what they hold (explicit; see module doc). Anything ending in
# BEES / ETF / IETF that is not listed here and not in NOT_ETF counts as an
# Indian-equity ETF.
ETF_CLASS = {
    **dict.fromkeys(("GOLDBEES", "GOLDIETF", "GOLD1", "GOLD360", "GOLDADD", "GOLDAXIS", "GOLDBETA", "GOLDCASE",
                     "GOLDETF", "SETFGOLD", "BSLGOLDETF", "TWCGOLDETF", "HDFCGOLD", "AXISGOLD", "LICMFGOLD",
                     "IVZINGOLD", "QGOLDHALF", "EGOLD", "GOLDSHARE", "UTIGOLD", "KOTAKGOLD"), "GOLD"),
    **dict.fromkeys(("SILVERBEES", "SILVERIETF", "SILVERETF", "SILVER1", "AXISILVER", "HDFCSILVER",
                     "SBISILVER", "TATSILV", "SILVERADD", "SILVERCASE"), "SILVER"),
    **dict.fromkeys(("LIQUIDBEES", "LIQUIDBETF", "LIQUIDETF", "LIQUIDIETF", "LIQGRWBEES", "SBILIQETF", "CASHIETF",
                     "LIQUIDCASE", "LIQUIDADD", "LIQUID1", "LIQUIDSHRI"), "CASH"),
    **dict.fromkeys(("BBETF0432", "EBBETF0430", "EBBETF0431", "EBBETF0433", "GILT5YBEES", "LTGILTBEES",
                     "GSEC10IETF", "GSEC5IETF", "LICNETFGSC", "SETF10GILT", "BBNPNBETF",
                     "SDL26BEES", "GSEC10YEAR", "GSEC10ABSL"), "BONDS"),
    **dict.fromkeys(("MON100", "MAFANG", "MASPTOP50", "N100", "HNGSNGBEES", "MAHKTECH", "NASDAQ100", "MONQ50"),
                    "INTL_EQUITY"),
}
NOT_ETF = {"JETFREIGHT", "GOLDIAM", "GOLDTECH", "GOLDSTAR", "GOLDKART", "NETFREIGHT"}


def classify_symbol(symbol: str, overrides: dict | None = None) -> dict:
    """{asset_class, instrument, source} for a listed symbol."""
    s = (symbol or "").upper().strip()
    if overrides and s in overrides:
        o = overrides[s]
        return {"asset_class": o["asset_class"], "instrument": o.get("instrument") or "STOCK", "source": "override"}
    if s in ETF_CLASS:
        return {"asset_class": ETF_CLASS[s], "instrument": "ETF", "source": "etf_map"}
    if s not in NOT_ETF and (s.endswith("BEES") or s.endswith("ETF") or s.endswith("IETF") or "ETF" in s[-6:]):
        return {"asset_class": "EQUITY", "instrument": "ETF", "source": "etf_suffix"}
    return {"asset_class": "EQUITY", "instrument": "STOCK", "source": "default"}


def check_class(asset_class: str) -> str:
    c = (asset_class or "").upper().strip()
    if c in EXCLUDED_CLASSES:
        raise ValueError(EXCLUDED_CLASSES[c])
    if c not in ALLOCATION_CLASSES:
        raise ValueError(f"asset_class must be one of {sorted(ALLOCATION_CLASSES)}")
    return c


def check_instrument(instrument: str, asset_class: str, valuation: str | None) -> tuple:
    i = (instrument or "").upper().strip()
    if i not in INSTRUMENTS:
        raise ValueError(f"instrument must be one of {sorted(INSTRUMENTS)}")
    classes, default_val, vals = INSTRUMENTS[i]
    if asset_class not in classes:
        raise ValueError(f"a {i} can be classed as {list(classes)}, not {asset_class}")
    v = (valuation or default_val).upper()
    if v not in vals:
        raise ValueError(f"a {i} can be valued by {list(vals)}, not {v}")
    return i, v


def registry() -> dict:
    return {"allocation_classes": ALLOCATION_CLASSES, "excluded": EXCLUDED_CLASSES,
            "instruments": {k: {"classes": list(v[0]), "default_valuation": v[1], "valuations": list(v[2])}
                            for k, v in INSTRUMENTS.items()},
            "valuations": list(VALUATIONS)}
