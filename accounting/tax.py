"""
Estimated tax on session cycles (SES-06). An ESTIMATE, NOT TAX ADVICE: every figure built from this module is
labelled "est.", and the rates are the owner's to check with a CA before relying on them.

Rates live in atip_data/config.json so they change without code ("verify before go-live"):

    "tax": {
      "effective_from": "2026-04-01",   the date these rates apply from (FY 2026-27)
      "stcg_pct": 20.0,                 listed equity, delivery, held 12 months or less (s.111A)
      "ltcg_pct": 12.5,                 held more than 12 months (s.112A) ...
      "ltcg_exemption": 125000,         ... above Rs 1.25 lakh of LTCG a financial year, applied once across sessions
      "slab_pct": 30.0,                 intraday (speculative business income) and F&O (non-speculative business
                                        income) are taxed at the owner's slab rate
      "surcharge_pct": 0.0, "cess_pct": 4.0,
      "addons_enabled": false,          surcharge and cess are shown on the FY statement; they enter the per-cycle
                                        estimate and the liability only when this is true
      "stt_deductible": true            the per-cycle estimate taxes net P&L after every charge (the owner's worked
                                        example). Under s.48 STT is not deductible from capital gains: set false
                                        to tax gross minus every charge except STT
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
    3. a net business (F&O) loss is set off against speculative income, then STCG, then LTCG (s.71);
    4. what is left of a long-term loss, a speculative loss or a business loss is carried forward (listed,
       not applied: earlier years' carry-forward is not modelled);
    5. the Rs 1.25 lakh LTCG exemption once, then the rates; surcharge and cess on top when enabled.
Not modelled: the basic exemption limit, the s.87A rebate, grandfathering of pre-2018 holdings, advance-tax dates.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"
DISCLAIMER = "Estimate, not tax advice. Check the rates and your figures with a chartered accountant."
HEADS = ("STCG", "LTCG", "SPECULATIVE", "BUSINESS")
HEAD_LABEL = {"STCG": "Short-term capital gain (s.111A)", "LTCG": "Long-term capital gain (s.112A)",
              "SPECULATIVE": "Speculative business income (intraday)", "BUSINESS": "Business income (F&O)"}
DEFAULTS = {"effective_from": "2026-04-01", "stcg_pct": 20.0, "ltcg_pct": 12.5, "ltcg_exemption": 125000.0,
            "slab_pct": 30.0, "surcharge_pct": 0.0, "cess_pct": 4.0, "addons_enabled": False,
            "stt_deductible": True}
NUMBERS = ("stcg_pct", "ltcg_pct", "ltcg_exemption", "slab_pct", "surcharge_pct", "cess_pct")
INTRADAY = ("MIS", "INTRADAY", "BO", "CO")
FNO = ("NRML", "FNO", "FUT", "FUTURES", "OPT", "OPTIONS", "MARGIN")


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def settings(cfg: dict | None = None) -> dict:
    """The rates in force: config.json "tax" over DEFAULTS."""
    raw = (_config() if cfg is None else cfg).get("tax") or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for k in NUMBERS:
        out[k] = float(out[k])
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


def rate(h: str, cfg: dict | None = None) -> float:
    cfg = cfg or settings()
    return {"STCG": cfg["stcg_pct"], "LTCG": cfg["ltcg_pct"]}.get(h, cfg["slab_pct"])


def _addons(tax: float, cfg: dict) -> float:
    sur = tax * cfg["surcharge_pct"] / 100
    return (tax + sur) * cfg["cess_pct"] / 100 + sur


def cycle_tax(net_pnl: float, h: str = "STCG", stt: float = 0.0, cfg: dict | None = None) -> float:
    """The provisional tax on one cycle: its head's rate on a gain, nothing on a loss."""
    cfg = cfg or settings()
    base = float(net_pnl) + (0.0 if cfg["stt_deductible"] else float(stt or 0))
    if base <= 0:
        return 0.0
    tax = base * rate(h, cfg) / 100
    if cfg["addons_enabled"]:
        tax += _addons(tax, cfg)
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
        base = float(c.get("net_pnl") or 0) + (0.0 if cfg["stt_deductible"] else float(c.get("ch_stt") or 0))
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

    move("STCG", "LTCG", "short-term loss against long-term gain (s.74 / s.70)")
    for to in ("SPECULATIVE", "STCG", "LTCG"):
        move("BUSINESS", to, "business loss against other income of the year (s.71)")
    carry = {h: round(-net[h], 2) for h in HEADS if net[h] < 0}
    taxable = {h: max(0.0, net[h]) for h in HEADS}
    exemption = min(taxable["LTCG"], cfg["ltcg_exemption"])
    taxable["LTCG"] -= exemption
    tax = {h: round(taxable[h] * rate(h, cfg) / 100, 2) for h in HEADS}
    base_tax = round(sum(tax.values()), 2)
    sur = round(base_tax * cfg["surcharge_pct"] / 100, 2)
    cess = round((base_tax + sur) * cfg["cess_pct"] / 100, 2)
    liability = round(base_tax + (sur + cess if cfg["addons_enabled"] else 0.0), 2)
    provisional = round(sum(float(c.get("tax_est") or 0) for c in rows), 2)
    return {
        "fy": fy, "from": lo.isoformat(), "to": hi.isoformat(), "cycles": len(rows), "rates": cfg,
        "heads": [{"head": h, "label": HEAD_LABEL[h], "gains": round(gains[h], 2), "losses": round(losses[h], 2),
                   "net": round(gains[h] - losses[h], 2), "after_set_off": round(net[h], 2),
                   "taxable": round(taxable[h], 2), "rate_pct": rate(h, cfg), "tax": tax[h]} for h in HEADS],
        "set_offs": steps, "carried_forward": carry, "ltcg_exemption_used": round(exemption, 2),
        "tax_before_addons": base_tax, "surcharge": sur, "cess": cess, "addons_applied": cfg["addons_enabled"],
        "liability": liability, "provisional_set_aside": provisional,
        "difference": round(provisional - liability, 2),
        "by_session": sorted(({**v, "net_pnl": round(v["net_pnl"], 2), "provisional_tax": round(v["provisional_tax"], 2)}
                              for v in by_session.values()), key=lambda r: int(str(r["session_id"]).lstrip("S") or 0)),
        "disclaimer": DISCLAIMER}
