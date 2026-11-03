"""
Estimated tax on session cycles (SES-06). An ESTIMATE, NOT TAX ADVICE: every figure built from this module is
labelled "est.", and the rates are the owner's to check with a CA before relying on them.

The law (checked 3 Nov 2026): the Income-tax Act, 2025 replaced the 1961 Act from 1 April 2026 ("tax year" for
"previous year"); Budget 2026 (Finance Act 2026) left the slabs and the capital-gains rates as they were in FY 2025-26.
Old section numbers are kept in brackets because contract notes and most guides still quote them.

    STCG, listed equity, STT paid, held 12 months or less     20%     s.196 (was s.111A)
    LTCG, held more than 12 months, above Rs 1.25 lakh a year 12.5%   s.198 (was s.112A)
    STT                         never deducted from capital gains,   s.72(3) (was the proviso to s.48); brokerage,
                                exchange, SEBI, stamp, GST and DP charges are (costs of the transfer). It IS a
                                business expense against intraday and F&O income.
    intraday (speculative) and F&O (non-speculative business) income: added to total income, taxed at the slabs
    surcharge                   income over Rs 50 lakh: 10% / over 1 crore 15% / over 2 crore 25% (new regime cap);
                                never more than 15% on the 20% and 12.5% gains
    health and education cess   4% of tax plus surcharge, always
    basic exemption             a resident individual whose other income is below the nil slab uses the shortfall
                                against the 20% and 12.5% gains (s.196(2), s.198)

Slabs, tax year 2026-27 (individual, resident, below 60):
    new regime, the default (s.202, was s.115BAC): Rs 0-4 lakh nil, 4-8 5%, 8-12 10%, 12-16 15%, 16-20 20%,
                                                    20-24 25%, above 24 lakh 30%
    old regime (opted for):                         Rs 0-2.5 lakh nil, 2.5-5 5%, 5-10 20%, above 10 lakh 30%

Rates live in atip_data/config.json so they change without code ("verify before go-live"):

    "tax": {
      "effective_from": "2026-04-01",   the date these rates apply from (tax year 2026-27)
      "regime": "new",                  "new" or "old": which slab table
      "stcg_pct": 20.0, "ltcg_pct": 12.5, "ltcg_exemption": 125000,
      "other_income": null,             the owner's other taxable income for the year (salary, interest... after
                                        deductions), in rupees. With it the slab rate is worked out from the table
                                        and the basic-exemption shortfall is used; null = not given
      "slab_pct": 30.0,                 the slab rate for intraday and F&O when other_income is not given (30% = the
                                        top slab, so the estimate errs high)
      "surcharge_pct": 0.0,             0 below Rs 50 lakh of total income; capped at 15 on capital gains
      "cess_pct": 4.0,
      "addons_enabled": true,           surcharge and cess in the per-cycle estimate and the liability (the law
                                        always adds the cess; false only reproduces the brief's worked example)
      "stt_deductible": false           true deducts STT from capital gains too, which s.72(3) does not allow (the
                                        brief's worked example did); intraday and F&O always deduct it
    }

Heads
    STCG          equity delivery (CNC) held 12 months or less
    LTCG          equity delivery held more than 12 months
    SPECULATIVE   intraday (MIS / INTRADAY)
    BUSINESS      F&O (NRML / futures / options)

Per cycle (provisional): a gain is taxed at its head's rate; a loss carries no tax (0, never a credit). The FY
statement (fy_statement) does the proper set-off over Apr 1 - Mar 31 across every session:
    1. each head is netted (its gains less its losses);
    2. a net short-term loss is set off against net long-term gains;
    3. a net business (F&O) loss is set off against speculative income, then STCG, then LTCG;
    4. what is left of a long-term loss, a speculative loss or a business loss is carried forward (listed,
       not applied: earlier years' carry-forward is not modelled);
    5. the Rs 1.25 lakh LTCG exemption once; with other_income, the slab tax that intraday and F&O income adds
       on top of it and the basic-exemption shortfall against STCG then LTCG; surcharge and cess on top.
Not modelled: the s.156 rebate (was s.87A: it never covers the 20% and 12.5% gains, but can cut slab tax on
intraday or F&O income when total income is Rs 12 lakh or less), marginal relief, grandfathering of pre-2018
holdings, advance-tax dates. When a setting changes, cycles.sync re-estimates the stored cycles and the ledger
posts the difference.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"
DISCLAIMER = "Estimate, not tax advice. Check the rates and your figures with a chartered accountant."
HEADS = ("STCG", "LTCG", "SPECULATIVE", "BUSINESS")
CAPITAL = ("STCG", "LTCG")
HEAD_LABEL = {"STCG": "Short-term capital gain (s.196, was s.111A)", "LTCG": "Long-term capital gain (s.198, was s.112A)",
              "SPECULATIVE": "Speculative business income (intraday)", "BUSINESS": "Business income (F&O)"}
DEFAULTS = {"effective_from": "2026-04-01", "regime": "new", "stcg_pct": 20.0, "ltcg_pct": 12.5,
            "ltcg_exemption": 125000.0, "other_income": None, "slab_pct": 30.0, "surcharge_pct": 0.0, "cess_pct": 4.0,
            "addons_enabled": True, "stt_deductible": False}
NUMBERS = ("stcg_pct", "ltcg_pct", "ltcg_exemption", "slab_pct", "surcharge_pct", "cess_pct")
CAPITAL_SURCHARGE_CAP = 15.0
# Slabs for tax year 2026-27, individual, resident, below 60: (upper limit of the slab, rate %); None = no limit.
SLABS = {"new": ((400000, 0.0), (800000, 5.0), (1200000, 10.0), (1600000, 15.0), (2000000, 20.0), (2400000, 25.0),
                 (None, 30.0)),
         "old": ((250000, 0.0), (500000, 5.0), (1000000, 20.0), (None, 30.0))}
REGIME_LABEL = {"new": "New regime (default, s.202)", "old": "Old regime (opted for)"}
INTRADAY = ("MIS", "INTRADAY", "BO", "CO")
FNO = ("NRML", "FNO", "FUT", "FUTURES", "OPT", "OPTIONS", "MARGIN")


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _income(v) -> float | None:
    """other_income: a non-negative number of rupees, or None when not given."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return float(str(v).replace(",", "").replace("₹", "").strip())


def settings(cfg: dict | None = None) -> dict:
    """The rates in force: config.json "tax" over DEFAULTS."""
    raw = (_config() if cfg is None else cfg).get("tax") or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for k in NUMBERS:
        out[k] = float(out[k])
    out["regime"] = "old" if str(out["regime"]).strip().lower() == "old" else "new"
    try:
        out["other_income"] = _income(out["other_income"])
    except ValueError:
        out["other_income"] = None
    out["addons_enabled"] = bool(out["addons_enabled"])
    out["stt_deductible"] = bool(out["stt_deductible"])
    return out


def validate(fields: dict) -> dict:
    """Plain-English checks on a change to the rates; returns the cleaned values."""
    out, errors = {}, []
    for k, v in fields.items():
        if k not in DEFAULTS:
            errors.append(f"{k} is not a tax setting")
            continue
        if k in NUMBERS:
            try:
                v = float(v)
            except (TypeError, ValueError):
                errors.append(f"{k.replace('_', ' ')} must be a number")
                continue
            if v < 0 or (k.endswith("_pct") and v > 100):
                errors.append(f"{k.replace('_', ' ')} must be between 0 and 100" if k.endswith("_pct")
                              else f"{k.replace('_', ' ')} cannot be negative")
                continue
        elif k == "other_income":
            try:
                v = _income(v)
            except ValueError:
                errors.append("other income must be an amount in rupees, or left empty")
                continue
            if v is not None and v < 0:
                errors.append("other income cannot be negative")
                continue
        elif k == "regime":
            v = str(v).strip().lower()
            if v not in SLABS:
                errors.append("regime must be new or old")
                continue
        elif k == "effective_from":
            try:
                v = date.fromisoformat(str(v)[:10]).isoformat()
            except ValueError:
                errors.append("effective from must be a date like 2026-04-01")
                continue
        else:
            v = str(v).strip().lower() in ("1", "true", "on", "yes") if not isinstance(v, bool) else v
        out[k] = v
    if errors:
        raise ValueError("; ".join(errors))
    return out


def save(fields: dict) -> dict:
    """Write the "tax" block of config.json, keeping every other key; returns the rates now in force."""
    clean = validate(fields)
    cfg = _config()
    cfg["tax"] = {**(cfg.get("tax") or {}), **clean}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return settings(cfg)


def _d(v) -> date | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _plus_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def head(product_type: str | None, bought, sold) -> str:
    """The tax head of one closed trade."""
    p = str(product_type or "CNC").upper()
    if p in INTRADAY:
        return "SPECULATIVE"
    if p in FNO:
        return "BUSINESS"
    b, s = _d(bought), _d(sold)
    if b and s and s > _plus_months(b, 12):
        return "LTCG"
    return "STCG"


def slab_tax(income: float, regime: str = "new") -> float:
    """Tax at the slab rates on `income` (before surcharge, cess and rebate)."""
    tax, lower = 0.0, 0.0
    for upper, pct in SLABS.get(regime, SLABS["new"]):
        if income <= lower:
            break
        top = income if upper is None else min(income, upper)
        tax += (top - lower) * pct / 100
        if upper is None:
            break
        lower = upper
    return tax


def marginal_rate(income: float, regime: str = "new") -> float:
    """The slab rate on the next rupee above `income`."""
    for upper, pct in SLABS.get(regime, SLABS["new"]):
        if upper is None or income < upper:
            return pct
    return SLABS["new"][-1][1]


def basic_exemption(regime: str = "new") -> float:
    """The nil slab's upper limit."""
    return float(SLABS.get(regime, SLABS["new"])[0][0])


def rate(h: str, cfg: dict | None = None) -> float:
    """The rate on a head: flat for capital gains; the slab for intraday and F&O, worked out from other_income when
    it is given, otherwise slab_pct."""
    cfg = cfg or settings()
    if h == "STCG":
        return cfg["stcg_pct"]
    if h == "LTCG":
        return cfg["ltcg_pct"]
    if cfg.get("other_income") is not None:
        return marginal_rate(cfg["other_income"], cfg.get("regime", "new"))
    return cfg["slab_pct"]


def taxable(net_pnl: float, h: str, stt: float, cfg: dict) -> float:
    """What is taxed on one cycle: net P&L (after every charge), with STT added back on capital gains unless
    stt_deductible (s.72(3))."""
    add_back = h in CAPITAL and not cfg["stt_deductible"]
    return float(net_pnl) + (float(stt or 0) if add_back else 0.0)


def surcharge_pct(h: str, cfg: dict) -> float:
    return min(cfg["surcharge_pct"], CAPITAL_SURCHARGE_CAP) if h in CAPITAL else cfg["surcharge_pct"]


def _addons(tax: float, h: str, cfg: dict) -> float:
    sur = tax * surcharge_pct(h, cfg) / 100
    return (tax + sur) * cfg["cess_pct"] / 100 + sur


def cycle_tax(net_pnl: float, h: str = "STCG", stt: float = 0.0, cfg: dict | None = None) -> float:
    """The provisional tax on one cycle: its head's rate on a gain, nothing on a loss."""
    cfg = cfg or settings()
    base = taxable(net_pnl, h, stt, cfg)
    if base <= 0:
        return 0.0
    tax = base * rate(h, cfg) / 100
    if cfg["addons_enabled"]:
        tax += _addons(tax, h, cfg)
    return round(tax + 1e-9, 2)


def fy_of(d) -> str:
    """'FY 2026-27' for any date from 1 Apr 2026 to 31 Mar 2027."""
    d = _d(d) or date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY {start}-{str(start + 1)[-2:]}"


def fy_bounds(label: str) -> tuple[date, date]:
    """'FY 2026-27' -> (2026-04-01, 2027-03-31)."""
    try:
        start = int(str(label).split()[-1].split("-")[0])
    except (ValueError, IndexError):
        raise ValueError(f"{label!r} is not a financial year like FY 2026-27")
    return date(start, 4, 1), date(start + 1, 3, 31)


def fy_statement(cycles: list, fy: str, cfg: dict | None = None) -> dict:
    """The FY view: every closed cycle of the year (closed_at inside Apr 1 - Mar 31) by head, set-offs, the LTCG
    exemption once, the liability and what was provisionally set aside per cycle. `cycles` are session_cycles rows
    (dicts with net_pnl, tax_head, tax_est, closed_at, session_id, ch_stt)."""
    cfg = cfg or settings()
    lo, hi = fy_bounds(fy)
    rows = [c for c in cycles if (d := _d(c.get("closed_at"))) and lo <= d <= hi]
    gains = {h: 0.0 for h in HEADS}
    losses = {h: 0.0 for h in HEADS}
    by_session = {}
    for c in rows:
        h = c.get("tax_head") or "STCG"
        base = taxable(c.get("net_pnl") or 0, h, c.get("ch_stt") or 0, cfg)
        if base >= 0:
            gains[h] += base
        else:
            losses[h] += -base
        s = by_session.setdefault(c.get("session_id"), {"session_id": c.get("session_id"), "cycles": 0,
                                                         "net_pnl": 0.0, "provisional_tax": 0.0})
        s["cycles"] += 1
        s["net_pnl"] += float(c.get("net_pnl") or 0)
        s["provisional_tax"] += float(c.get("tax_est") or 0)
    net = {h: gains[h] - losses[h] for h in HEADS}
    steps = []

    def move(frm, to, why):
        if net[frm] < 0 < net[to]:
            amt = min(-net[frm], net[to])
            net[frm] += amt
            net[to] -= amt
            steps.append({"from": frm, "against": to, "amount": round(amt, 2), "rule": why})

    move("STCG", "LTCG", "short-term loss against long-term gain (within capital gains)")
    for to in ("SPECULATIVE", "STCG", "LTCG"):
        move("BUSINESS", to, "business loss against other income of the year (inter-head set-off)")
    carry = {h: round(-net[h], 2) for h in HEADS if net[h] < 0}
    taxable_ = {h: max(0.0, net[h]) for h in HEADS}
    exemption = min(taxable_["LTCG"], cfg["ltcg_exemption"])
    taxable_["LTCG"] -= exemption
    other, regime = cfg.get("other_income"), cfg.get("regime", "new")
    business = taxable_["SPECULATIVE"] + taxable_["BUSINESS"]
    shortfall_used = 0.0
    if other is not None:
        # the nil slab not used by other income and intraday / F&O income goes against STCG first (20%), then LTCG
        left = max(0.0, basic_exemption(regime) - other - business)
        for h in CAPITAL:
            use = min(left, taxable_[h])
            taxable_[h] -= use
            left -= use
            shortfall_used += use
    rates = {h: rate(h, cfg) for h in HEADS}
    tax = {h: round(taxable_[h] * rates[h] / 100, 2) for h in CAPITAL}
    if other is not None and business > 0:
        added = slab_tax(other + business, regime) - slab_tax(other, regime)      # what trading adds to the slab tax
        for h in ("SPECULATIVE", "BUSINESS"):
            tax[h] = round(added * taxable_[h] / business, 2)
            rates[h] = round(added / business * 100, 2)
    else:
        for h in ("SPECULATIVE", "BUSINESS"):
            tax[h] = round(taxable_[h] * rates[h] / 100, 2)
    base_tax = round(sum(tax.values()), 2)
    sur = round(sum(tax[h] * surcharge_pct(h, cfg) / 100 for h in HEADS), 2)
    cess = round((base_tax + sur) * cfg["cess_pct"] / 100, 2)
    liability = round(base_tax + (sur + cess if cfg["addons_enabled"] else 0.0), 2)
    provisional = round(sum(float(c.get("tax_est") or 0) for c in rows), 2)
    return {
        "fy": fy, "from": lo.isoformat(), "to": hi.isoformat(), "cycles": len(rows), "rates": cfg,
        "regime": regime, "regime_label": REGIME_LABEL[regime], "other_income": other,
        "heads": [{"head": h, "label": HEAD_LABEL[h], "gains": round(gains[h], 2), "losses": round(losses[h], 2),
                   "net": round(gains[h] - losses[h], 2), "after_set_off": round(net[h], 2),
                   "taxable": round(taxable_[h], 2), "rate_pct": rates[h], "tax": tax[h]} for h in HEADS],
        "set_offs": steps, "carried_forward": carry, "ltcg_exemption_used": round(exemption, 2),
        "basic_exemption_used": round(shortfall_used, 2),
        "tax_before_addons": base_tax, "surcharge": sur, "cess": cess, "addons_applied": cfg["addons_enabled"],
        "liability": liability, "provisional_set_aside": provisional,
        "difference": round(provisional - liability, 2),
        "by_session": sorted(({**v, "net_pnl": round(v["net_pnl"], 2), "provisional_tax": round(v["provisional_tax"], 2)}
                              for v in by_session.values()), key=lambda r: int(str(r["session_id"]).lstrip("S") or 0)),
        "disclaimer": DISCLAIMER}
