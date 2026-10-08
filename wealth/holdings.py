"""
Multi-asset wealth (W12, ATIP-WLT-001): one normalized view of everything the
investor holds, valued with its source and date.

SOURCES
    BROKER   the synced broker account (portfolio_holdings, the latest successful
             sync -- portfolio.pnl.live_book_date). House book: default tenant only.
    MANUAL   wealth_holding rows the owner enters (any supported class)
    PAPER    the paper book (paper_position). Simulated: shown, never counted in net
             worth. House book only.

    positions(conn, owner)        normalized rows (see POSITION below)
    summary(conn, owner)          net worth, by class / source / instrument / sector,
                                  concentration, liquidity, risk overlay (ATIP scores),
                                  portfolio health, reconciliation, data-quality issues
    record_snapshot / snapshots   daily net-worth history (wealth_snapshot)

POSITION  {key, source, holding_id, asset_class, instrument, symbol, name, quantity,
           unit, avg_cost, cost, price, price_source, price_as_of, currency, fx_rate,
           value, unrealized, unrealized_pct, stale, sector, include_in_net_worth,
           classification}

Staleness: a MARKET price older than 5 calendar days, a spot price older than 5
days, a MANUAL price older than 90 days, or no price at all. A position with no
price is valued at cost and flagged -- never silently at zero.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from wealth import assets as A
from wealth import common as C
from wealth.config import settings

STALE_MARKET_DAYS = 5
STALE_MANUAL_DAYS = 90
LIABILITY_KINDS = ("HOME_LOAN", "VEHICLE_LOAN", "PERSONAL_LOAN", "EDUCATION_LOAN", "CREDIT_CARD", "LOAN_AGAINST_SHARES",
                   "OTHER")


# ── Prices ─────────────────────────────────────────────────────────────────
def _d(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def market_price(conn, symbol: str, on: date | None = None) -> dict:
    """ATIP's mark for a listed symbol: the latest live quote today, else the last
    close in prices_daily on or before `on`."""
    on = on or date.today()
    if on == date.today() and C.table_exists(conn, "live_quotes"):
        r = conn.execute("SELECT ltp, timestamp FROM live_quotes WHERE symbol=? AND substr(timestamp,1,10)=? AND "
                         "ltp>0 ORDER BY timestamp DESC LIMIT 1", (symbol, str(on))).fetchone()
        if r:
            return {"price": float(r[0]), "as_of": str(r[1])[:10], "source": "live_quote"}
    r = conn.execute("SELECT close, date FROM prices_daily WHERE symbol=? AND date<=? AND close>0 ORDER BY date DESC "
                     "LIMIT 1", (symbol, str(on))).fetchone()
    if r:
        return {"price": float(r[0]), "as_of": str(r[1])[:10], "source": "prices_daily"}
    return {"price": None, "as_of": None, "source": "none"}


def spot(conn) -> dict:
    """Latest gold / silver (USD per troy ounce) and USD/INR from global_markets."""
    out = {"gold_usd_oz": None, "silver_usd_oz": None, "usd_inr": None, "as_of": None}
    if not C.table_exists(conn, "global_markets"):
        return out
    for col, key in (("gold", "gold_usd_oz"), ("silver", "silver_usd_oz"), ("usd_inr", "usd_inr")):
        r = conn.execute(f"SELECT {col}, date FROM global_markets WHERE {col}>0 ORDER BY date DESC, id DESC "
                         f"LIMIT 1").fetchone()
        if r:
            out[key] = float(r[0])
            out["as_of"] = max(filter(None, (out["as_of"], str(r[1])[:10])))
    return out


def spot_inr_per_gram(conn, metal: str) -> dict:
    s = spot(conn)
    usd = s["gold_usd_oz"] if metal == "GOLD" else s["silver_usd_oz"]
    if not usd or not s["usd_inr"]:
        return {"price": None, "as_of": s["as_of"], "source": "none"}
    prem = settings()["gold_domestic_premium_pct"]          # same assumption applied to silver
    px = usd * s["usd_inr"] / A.TROY_OUNCE_GRAMS * (1 + prem / 100)
    return {"price": round(px, 2), "as_of": s["as_of"], "source": f"global_markets.{metal.lower()} x usd_inr / "
            f"{A.TROY_OUNCE_GRAMS} x (1+{prem:g}% domestic premium)"}


def _stale(as_of, days) -> bool:
    d = _d(as_of)
    return d is None or (date.today() - d).days > days


# ── Classification overrides ───────────────────────────────────────────────
def overrides(conn, owner) -> dict:
    return {r["symbol"]: {"asset_class": r["asset_class"], "instrument": r["instrument"]} for r in conn.execute(
        "SELECT symbol, asset_class, instrument FROM wealth_classification WHERE tenant_id=? AND owner_id=?",
        (owner["tenant_id"], owner["owner_id"]))}


def set_classification(conn, owner, symbol, asset_class, instrument="ETF") -> dict:
    sym = C.symbol(symbol)
    cls = A.check_class(asset_class)
    inst, _ = A.check_instrument(instrument, cls, None)
    conn.execute("INSERT INTO wealth_classification (tenant_id,owner_id,symbol,asset_class,instrument,updated_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,owner_id,symbol) DO UPDATE SET "
                 "asset_class=excluded.asset_class, instrument=excluded.instrument, updated_at=excluded.updated_at",
                 (owner["tenant_id"], owner["owner_id"], sym, cls, inst, C.now()))
    conn.commit()
    return {"symbol": sym, "asset_class": cls, "instrument": inst}


def clear_classification(conn, owner, symbol) -> dict:
    sym = C.symbol(symbol)
    n = conn.execute("DELETE FROM wealth_classification WHERE tenant_id=? AND owner_id=? AND symbol=?",
                     (owner["tenant_id"], owner["owner_id"], sym)).rowcount
    conn.commit()
    if not n:
        raise LookupError("no override for that symbol")
    return {"symbol": sym, "cleared": True}


# ── Manual holdings ────────────────────────────────────────────────────────
HOLDING_FIELDS = ("asset_class", "instrument", "valuation", "name", "symbol", "quantity", "unit", "avg_cost",
                  "currency", "fx_rate", "manual_price", "manual_price_as_of", "maturity_date", "coupon_pct", "notes")


def _clean_holding(b: dict, existing: dict | None = None) -> dict:
    x = dict(existing or {})
    unknown = set(b) - set(HOLDING_FIELDS)
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}; known {list(HOLDING_FIELDS)}")
    x.update({k: v for k, v in b.items()})
    cls = A.check_class(x.get("asset_class"))
    inst, val = A.check_instrument(x.get("instrument") or ("BANK_ACCOUNT" if cls == "CASH" else "OTHER"), cls,
                                   x.get("valuation"))
    out = {"asset_class": cls, "instrument": inst, "valuation": val,
           "name": C.text(x.get("name"), "name", 120),
           "symbol": C.symbol(x.get("symbol"), "symbol", required=(val == "MARKET")),
           "quantity": C.num(x.get("quantity"), "quantity", 0, 1e12),
           "unit": C.text(x.get("unit"), "unit", 20, required=False) or
           ("INR" if val == "CASH" else "grams" if val in ("GOLD_SPOT", "SILVER_SPOT") else "units"),
           "avg_cost": C.num(x.get("avg_cost"), "avg_cost", 0, 1e12, required=False),
           "currency": (C.text(x.get("currency"), "currency", 3, required=False) or "INR").upper(),
           "fx_rate": C.num(x.get("fx_rate"), "fx_rate", 0, 1e6, required=False),
           "manual_price": C.num(x.get("manual_price"), "manual_price", 0, 1e12,
                                 required=val in ("MANUAL", "FX_MANUAL")),
           "manual_price_as_of": C.parse_date(x.get("manual_price_as_of"), "manual_price_as_of", required=False),
           "maturity_date": C.parse_date(x.get("maturity_date"), "maturity_date", required=False),
           "coupon_pct": C.num(x.get("coupon_pct"), "coupon_pct", 0, 100, required=False),
           "notes": C.text(x.get("notes"), "notes", 500, required=False)}
    if val == "FX_MANUAL" and out["currency"] == "INR":
        raise ValueError("FX_MANUAL needs a foreign currency (e.g. USD)")
    if val == "FX_MANUAL" and out["currency"] != "USD" and not out["fx_rate"]:
        raise ValueError(f"fx_rate (INR per {out['currency']}) is required for {out['currency']}")
    if val in ("MANUAL", "FX_MANUAL") and not out["manual_price_as_of"]:
        out["manual_price_as_of"] = date.today()
    if val == "CASH":
        out["avg_cost"] = 1.0
    return out


def add_holding(conn, owner, b: dict, actor="owner") -> dict:
    n = conn.execute("SELECT COUNT(*) FROM wealth_holding WHERE tenant_id=? AND owner_id=? AND status='ACTIVE'",
                     (owner["tenant_id"], owner["owner_id"])).fetchone()[0]
    if n >= C.MAX_HOLDINGS:
        raise ValueError(f"at most {C.MAX_HOLDINGS} active holdings per investor")
    h = _clean_holding(b)
    hid = C.new_id("hold")
    at = C.now()
    conn.execute("INSERT INTO wealth_holding (holding_id,tenant_id,owner_id,asset_class,instrument,valuation,name,symbol,"
                 "quantity,unit,avg_cost,currency,fx_rate,manual_price,manual_price_as_of,maturity_date,coupon_pct,"
                 "notes,status,created_at,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                 "'ACTIVE',?,?,?)",
                 (hid, owner["tenant_id"], owner["owner_id"], h["asset_class"], h["instrument"], h["valuation"],
                  h["name"], h["symbol"], h["quantity"], h["unit"], h["avg_cost"], h["currency"], h["fx_rate"],
                  h["manual_price"], h["manual_price_as_of"], h["maturity_date"], h["coupon_pct"], h["notes"],
                  at, at, actor))
    conn.commit()
    return get_holding(conn, owner, hid)


def get_holding(conn, owner, hid) -> dict:
    r = conn.execute("SELECT * FROM wealth_holding WHERE holding_id=? AND tenant_id=? AND owner_id=?",
                     (hid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("holding not found")
    return dict(r)


def update_holding(conn, owner, hid, b: dict, actor="owner") -> dict:
    cur = get_holding(conn, owner, hid)
    if cur["status"] != "ACTIVE":
        raise ValueError("holding is closed")
    if "manual_price" in b and "manual_price_as_of" not in b:
        b = {**b, "manual_price_as_of": date.today().isoformat()}
    h = _clean_holding(b, {k: cur[k] for k in HOLDING_FIELDS})
    conn.execute("UPDATE wealth_holding SET asset_class=?,instrument=?,valuation=?,name=?,symbol=?,quantity=?,unit=?,"
                 "avg_cost=?,currency=?,fx_rate=?,manual_price=?,manual_price_as_of=?,maturity_date=?,coupon_pct=?,"
                 "notes=?,updated_at=?,updated_by=? WHERE holding_id=? AND tenant_id=? AND owner_id=?",
                 (h["asset_class"], h["instrument"], h["valuation"], h["name"], h["symbol"], h["quantity"], h["unit"],
                  h["avg_cost"], h["currency"], h["fx_rate"], h["manual_price"], h["manual_price_as_of"],
                  h["maturity_date"], h["coupon_pct"], h["notes"], C.now(), actor, hid, owner["tenant_id"],
                  owner["owner_id"]))
    conn.commit()
    return get_holding(conn, owner, hid)


def close_holding(conn, owner, hid, actor="owner") -> dict:
    get_holding(conn, owner, hid)
    conn.execute("UPDATE wealth_holding SET status='CLOSED', updated_at=?, updated_by=? WHERE holding_id=? AND "
                 "tenant_id=? AND owner_id=?", (C.now(), actor, hid, owner["tenant_id"], owner["owner_id"]))
    conn.commit()
    return get_holding(conn, owner, hid)


def list_holdings(conn, owner, include_closed=False) -> list:
    q = "SELECT * FROM wealth_holding WHERE tenant_id=? AND owner_id=?" + ("" if include_closed else
                                                                           " AND status='ACTIVE'")
    return [dict(r) for r in conn.execute(q + " ORDER BY asset_class, name", (owner["tenant_id"], owner["owner_id"]))]


# ── Liabilities ────────────────────────────────────────────────────────────
def _clean_liability(b, existing=None):
    x = dict(existing or {})
    unknown = set(b) - {"kind", "name", "outstanding", "interest_pct", "emi", "end_date", "notes"}
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    x.update(b)
    kind = str(x.get("kind") or "").upper()
    if kind not in LIABILITY_KINDS:
        raise ValueError(f"kind must be one of {list(LIABILITY_KINDS)}")
    return {"kind": kind, "name": C.text(x.get("name"), "name", 120),
            "outstanding": C.num(x.get("outstanding"), "outstanding", 0, 1e12),
            "interest_pct": C.num(x.get("interest_pct"), "interest_pct", 0, 100, required=False),
            "emi": C.num(x.get("emi"), "emi", 0, 1e10, required=False),
            "end_date": C.parse_date(x.get("end_date"), "end_date", required=False),
            "notes": C.text(x.get("notes"), "notes", 500, required=False)}


def add_liability(conn, owner, b, actor="owner") -> dict:
    x = _clean_liability(b)
    lid = C.new_id("liab")
    conn.execute("INSERT INTO wealth_liability (liability_id,tenant_id,owner_id,kind,name,outstanding,interest_pct,emi,"
                 "end_date,notes,status,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?)",
                 (lid, owner["tenant_id"], owner["owner_id"], x["kind"], x["name"], x["outstanding"],
                  x["interest_pct"], x["emi"], x["end_date"], x["notes"], C.now(), actor))
    conn.commit()
    return get_liability(conn, owner, lid)


def get_liability(conn, owner, lid) -> dict:
    r = conn.execute("SELECT * FROM wealth_liability WHERE liability_id=? AND tenant_id=? AND owner_id=?",
                     (lid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("liability not found")
    return dict(r)


def update_liability(conn, owner, lid, b, actor="owner") -> dict:
    cur = get_liability(conn, owner, lid)
    x = _clean_liability(b, {k: cur[k] for k in ("kind", "name", "outstanding", "interest_pct", "emi", "end_date",
                                                   "notes")})
    conn.execute("UPDATE wealth_liability SET kind=?,name=?,outstanding=?,interest_pct=?,emi=?,end_date=?,notes=?,"
                 "updated_at=?,updated_by=? WHERE liability_id=? AND tenant_id=? AND owner_id=?",
                 (x["kind"], x["name"], x["outstanding"], x["interest_pct"], x["emi"], x["end_date"], x["notes"],
                  C.now(), actor, lid, owner["tenant_id"], owner["owner_id"]))
    conn.commit()
    return get_liability(conn, owner, lid)


def close_liability(conn, owner, lid, actor="owner") -> dict:
    get_liability(conn, owner, lid)
    conn.execute("UPDATE wealth_liability SET status='CLOSED', updated_at=?, updated_by=? WHERE liability_id=? AND "
                 "tenant_id=? AND owner_id=?", (C.now(), actor, lid, owner["tenant_id"], owner["owner_id"]))
    conn.commit()
    return get_liability(conn, owner, lid)


def list_liabilities(conn, owner) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM wealth_liability WHERE tenant_id=? AND owner_id=? AND "
                                          "status='ACTIVE' ORDER BY outstanding DESC",
                                          (owner["tenant_id"], owner["owner_id"]))]


# ── Normalized positions ───────────────────────────────────────────────────
def _sectors() -> dict:
    try:
        from data.index_constituents import get_symbol_industry_map
        return get_symbol_industry_map() or {}
    except Exception:
        return {}


def _position(**kw) -> dict:
    p = {"key": None, "source": None, "holding_id": None, "asset_class": None, "instrument": None, "symbol": None,
         "name": None, "quantity": 0.0, "unit": "units", "avg_cost": None, "cost": None, "price": None,
         "price_source": None, "price_as_of": None, "currency": "INR", "fx_rate": None, "value": 0.0,
         "unrealized": None, "unrealized_pct": None, "stale": False, "sector": None, "include_in_net_worth": True,
         "classification": None}
    p.update(kw)
    q = p["quantity"] or 0.0
    if p["cost"] is None and p["avg_cost"] is not None:
        p["cost"] = round(q * p["avg_cost"], 2)
    if p["price"] is None:
        p["stale"] = True
        p["value"] = p["cost"] or 0.0                 # valued at cost, flagged
        p["price_source"] = (p["price_source"] or "none") + " (no price: valued at cost)"
    if p["cost"]:
        p["unrealized"] = round(p["value"] - p["cost"], 2)
        p["unrealized_pct"] = round(p["unrealized"] / p["cost"] * 100, 2)
    p["value"] = round(p["value"], 2)
    return p


def _value_manual(conn, h: dict, spots: dict) -> dict:
    val, q = h["valuation"], float(h["quantity"] or 0)
    base = dict(key=f"MANUAL:{h['holding_id']}", source="MANUAL", holding_id=h["holding_id"],
                asset_class=h["asset_class"], instrument=h["instrument"], symbol=h["symbol"], name=h["name"],
                quantity=q, unit=h["unit"], avg_cost=h["avg_cost"], currency=h["currency"],
                classification="manual")
    if val == "CASH":
        return _position(**base, price=1.0, value=q, price_source="cash", price_as_of=str(date.today()), cost=q)
    if val == "MARKET":
        m = market_price(conn, h["symbol"])
        v = q * m["price"] if m["price"] else 0.0
        return _position(**base, price=m["price"], value=v, price_source=m["source"], price_as_of=m["as_of"],
                         stale=_stale(m["as_of"], STALE_MARKET_DAYS))
    if val in ("GOLD_SPOT", "SILVER_SPOT"):
        m = spots["GOLD" if val == "GOLD_SPOT" else "SILVER"]
        v = q * m["price"] if m["price"] else 0.0
        return _position(**base, price=m["price"], value=v, price_source=m["source"], price_as_of=m["as_of"],
                         stale=_stale(m["as_of"], STALE_MARKET_DAYS))
    if val == "FX_MANUAL":
        fx = h["fx_rate"] if h["currency"] != "USD" or h["fx_rate"] else spots["usd_inr"]
        px = h["manual_price"] * fx if (h["manual_price"] is not None and fx) else None
        v = q * px if px else 0.0
        # avg_cost of a foreign holding is entered in rupees per unit
        return _position(**base, price=px, value=v, fx_rate=fx,
                         price_source=f"manual {h['currency']} price x {'entered FX' if h['fx_rate'] else 'USD/INR'}",
                         price_as_of=str(h["manual_price_as_of"])[:10] if h["manual_price_as_of"] else None,
                         stale=_stale(h["manual_price_as_of"], STALE_MANUAL_DAYS))
    px = h["manual_price"]
    return _position(**base, price=px, value=q * px if px is not None else 0.0, price_source="manual",
                     price_as_of=str(h["manual_price_as_of"])[:10] if h["manual_price_as_of"] else None,
                     stale=_stale(h["manual_price_as_of"], STALE_MANUAL_DAYS))


def broker_positions(conn, owner, ovr=None, sectors=None) -> tuple[list, dict]:
    """The synced broker account as positions + reconciliation info."""
    info = {"status": "NOT_AVAILABLE", "book_date": None}
    if not C.owns_house_book(owner) or not C.table_exists(conn, "portfolio_holdings"):
        return [], info
    try:
        from portfolio.pnl import live_book_date
        bd = live_book_date(conn)
    except Exception:
        r = conn.execute("SELECT MAX(date) FROM portfolio_holdings").fetchone()
        bd = str(r[0]) if r and r[0] else None
    if not bd:
        return [], {"status": "EMPTY", "book_date": None}
    ovr = ovr if ovr is not None else overrides(conn, owner)
    sectors = sectors if sectors is not None else {}
    out, reported = [], 0.0
    for r in conn.execute("SELECT symbol, qty, avg_price, cmp, current_val, sector FROM portfolio_holdings WHERE "
                          "date=? AND qty>0", (bd,)):
        cl = A.classify_symbol(r["symbol"], ovr)
        m = market_price(conn, r["symbol"])
        px, src, asof = m["price"], m["source"], m["as_of"]
        if px is None and r["cmp"]:
            px, src, asof = float(r["cmp"]), "broker_reported", bd
        q = float(r["qty"])
        reported += float(r["current_val"] or (q * (r["cmp"] or 0)))
        out.append(_position(key=f"BROKER:{r['symbol']}", source="BROKER", asset_class=cl["asset_class"],
                             instrument=cl["instrument"], symbol=r["symbol"], name=r["symbol"], quantity=q,
                             avg_cost=float(r["avg_price"]) if r["avg_price"] is not None else None, price=px,
                             value=q * px if px else 0.0, price_source=src, price_as_of=asof,
                             stale=_stale(asof, STALE_MARKET_DAYS),
                             sector=r["sector"] or sectors.get(r["symbol"]), classification=cl["source"]))
    ours = sum(p["value"] for p in out)
    info = {"status": "OK", "book_date": bd, "positions": len(out), "broker_reported_value": round(reported, 2),
            "atip_marked_value": round(ours, 2), "difference": round(ours - reported, 2),
            "note": "ATIP marks at the latest live quote / close; the broker figure is from the sync on book_date."}
    return out, info


def paper_positions(conn, owner, ovr=None) -> list:
    if not C.owns_house_book(owner) or not C.table_exists(conn, "paper_position"):
        return []
    out = []
    for r in conn.execute("SELECT symbol, quantity, avg_price FROM paper_position WHERE quantity>0"):
        cl = A.classify_symbol(r["symbol"], ovr)
        m = market_price(conn, r["symbol"])
        q = float(r["quantity"])
        out.append(_position(key=f"PAPER:{r['symbol']}", source="PAPER", asset_class=cl["asset_class"],
                             instrument=cl["instrument"], symbol=r["symbol"], name=r["symbol"], quantity=q,
                             avg_cost=float(r["avg_price"] or 0), price=m["price"],
                             value=q * m["price"] if m["price"] else 0.0, price_source=m["source"],
                             price_as_of=m["as_of"], stale=_stale(m["as_of"], STALE_MARKET_DAYS),
                             include_in_net_worth=False, classification=cl["source"]))
    return out


def positions(conn, owner, include_paper=True) -> tuple[list, dict]:
    ovr = overrides(conn, owner)
    sectors = _sectors()
    spots = {"GOLD": spot_inr_per_gram(conn, "GOLD"), "SILVER": spot_inr_per_gram(conn, "SILVER"),
             "usd_inr": spot(conn)["usd_inr"]}
    out, recon = broker_positions(conn, owner, ovr, sectors)
    for h in list_holdings(conn, owner):
        p = _value_manual(conn, h, spots)
        if p["symbol"] and p["asset_class"] == "EQUITY":
            p["sector"] = sectors.get(p["symbol"])
        out.append(p)
    if include_paper:
        out += paper_positions(conn, owner, ovr)
    return out, recon


# ── Summary ────────────────────────────────────────────────────────────────
def _group(pos, key, total):
    g = {}
    for p in pos:
        k = p.get(key) or "UNKNOWN"
        e = g.setdefault(k, {"value": 0.0, "cost": 0.0, "count": 0})
        e["value"] += p["value"]
        e["cost"] += p["cost"] or 0.0
        e["count"] += 1
    return {k: {"value": round(v["value"], 2), "weight_pct": round(v["value"] / total * 100, 2) if total else 0.0,
                "cost": round(v["cost"], 2), "unrealized": round(v["value"] - v["cost"], 2) if v["cost"] else None,
                "count": v["count"]} for k, v in sorted(g.items(), key=lambda kv: -kv[1]["value"])}


def _risk_overlay(conn, syms) -> dict:
    if not syms or not C.table_exists(conn, "ai_scores"):
        return {}
    r = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()
    if not r or not r[0]:
        return {}
    d = str(r[0])
    out = {}
    ph = ",".join("?" * len(syms))
    for x in conn.execute(f"SELECT symbol, atip_score, vpi, cri, zpi, `signal`, regime FROM ai_scores WHERE date=? AND "
                          f"symbol IN ({ph})", (d, *syms)):
        out[x["symbol"]] = {**dict(x), "date": d}
    return out


def summary(conn, owner) -> dict:
    cfg = settings()
    pos, recon = positions(conn, owner)
    counted = [p for p in pos if p["include_in_net_worth"]]
    gross = round(sum(p["value"] for p in counted), 2)
    liabs = list_liabilities(conn, owner)
    liab_total = round(sum(float(x["outstanding"] or 0) for x in liabs), 2)
    cost = round(sum(p["cost"] or 0 for p in counted if p["asset_class"] != "CASH"), 2)
    invested_val = round(sum(p["value"] for p in counted if p["asset_class"] != "CASH"), 2)

    # concentration (single holdings, of gross assets; cash accounts excluded).
    # The single-stock cap applies to shares only: an index ETF is already diversified.
    by_symbol = {}
    for p in counted:
        if p["asset_class"] == "CASH":
            continue
        k = p["symbol"] or p["key"]
        by_symbol[k] = by_symbol.get(k, 0.0) + p["value"]
    ranked = sorted(by_symbol.items(), key=lambda kv: -kv[1])
    w = [v / gross for _, v in ranked] if gross else []
    cap = cfg["single_stock_cap_pct"]
    stock_syms = {p["symbol"] for p in counted if p["instrument"] == "STOCK" and p["symbol"]}
    breaches = [{"symbol": k, "weight_pct": round(v / gross * 100, 2)} for k, v in ranked
                if gross and k in stock_syms and v / gross * 100 > cap]
    conc = {"largest_pct": round(w[0] * 100, 2) if w else None, "top5_pct": round(sum(w[:5]) * 100, 2) if w else None,
            "hhi": round(sum(x * x for x in w) * 10000, 1) if w else None,
            "effective_holdings": round(1 / sum(x * x for x in w), 1) if w and sum(x * x for x in w) else None,
            "single_stock_cap_pct": cap, "single_stock_breaches": breaches}

    liq = {"HIGH": 0.0, "MEDIUM": 0.0, "LOW": 0.0}
    for p in counted:
        liq[A.ALLOCATION_CLASSES.get(p["asset_class"], {}).get("liquidity", "LOW")] += p["value"]
    liquidity = {k: {"value": round(v, 2), "pct": round(v / gross * 100, 2) if gross else 0.0} for k, v in liq.items()}

    eq_syms = sorted({p["symbol"] for p in counted if p["symbol"] and p["asset_class"] == "EQUITY"})
    overlay = _risk_overlay(conn, eq_syms)
    at_risk = []
    for s in eq_syms:
        o = overlay.get(s)
        if o and ((o["cri"] or 0) >= 75 or o["signal"] in ("SELL", "EXIT", "AVOID")):
            at_risk.append({"symbol": s, "cri": o["cri"], "signal": o["signal"], "atip_score": o["atip_score"],
                            "value": round(by_symbol.get(s, 0.0), 2)})
    for p in pos:
        o = overlay.get(p["symbol"]) if p["symbol"] else None
        if o:
            p["atip"] = {"atip_score": o["atip_score"], "cri": o["cri"], "vpi": o["vpi"], "signal": o["signal"]}

    phs = None
    if C.owns_house_book(owner):
        try:
            from scores.portfolio_health import compute_phs
            phs = compute_phs(conn=conn)
        except Exception as e:
            phs = {"error": str(e)}

    issues = []
    for p in pos:
        if p["stale"]:
            issues.append({"key": p["key"], "name": p["name"], "issue": f"price stale or missing "
                           f"({p['price_source']}, as of {p['price_as_of']})"})
    if recon.get("status") == "OK" and recon.get("book_date") and _stale(recon["book_date"], STALE_MARKET_DAYS):
        issues.append({"key": "BROKER", "name": "broker sync", "issue": f"last successful sync {recon['book_date']}"})
    paper = [p for p in pos if p["source"] == "PAPER"]
    return {
        "as_of": C.now(), "owner": owner,
        "totals": {"gross_assets": gross, "liabilities": liab_total, "net_worth": round(gross - liab_total, 2),
                   "invested_value": invested_val, "invested_cost": cost,
                   "unrealized": round(invested_val - cost, 2) if cost else None,
                   "unrealized_pct": round((invested_val - cost) / cost * 100, 2) if cost else None,
                   "positions": len(counted)},
        "by_asset_class": _group(counted, "asset_class", gross),
        "by_source": _group(counted, "source", gross),
        "by_instrument": _group(counted, "instrument", gross),
        "equity_by_sector": _group([p for p in counted if p["asset_class"] == "EQUITY"], "sector",
                                   sum(p["value"] for p in counted if p["asset_class"] == "EQUITY")),
        "concentration": conc, "liquidity": liquidity,
        "holdings_at_risk": at_risk, "portfolio_health": phs,
        "reconciliation": {"broker": recon,
                           "paper_book": {"positions": len(paper),
                                          "value": round(sum(p["value"] for p in paper), 2),
                                          "note": "simulated; not counted in net worth"}},
        "liabilities": liabs, "data_quality": issues,
        "spot": {"gold_inr_g": spot_inr_per_gram(conn, "GOLD"), "silver_inr_g": spot_inr_per_gram(conn, "SILVER"),
                 "usd_inr": spot(conn)["usd_inr"]},
        "positions": pos, "disclaimer": C.DISCLAIMER,
    }


def allocation_weights(conn, owner) -> dict:
    """{asset_class: weight_pct} of net-worth assets (what W14 / W15 compare against)."""
    pos, _ = positions(conn, owner, include_paper=False)
    gross = sum(p["value"] for p in pos if p["include_in_net_worth"])
    w = {c: 0.0 for c in A.ALLOCATION_CLASSES}
    for p in pos:
        if p["include_in_net_worth"]:
            w[p["asset_class"]] = w.get(p["asset_class"], 0.0) + p["value"]
    return {"gross": round(gross, 2), "values": {k: round(v, 2) for k, v in w.items()},
            "weights_pct": {k: round(v / gross * 100, 4) if gross else 0.0 for k, v in w.items()}}


# ── Snapshots ──────────────────────────────────────────────────────────────
def record_snapshot(conn, owner, on: date | None = None) -> dict:
    s = summary(conn, owner)
    d = on or date.today()
    by = {k: v["value"] for k, v in s["by_asset_class"].items()}
    conn.execute("INSERT INTO wealth_snapshot (tenant_id,owner_id,date,net_worth,gross_assets,liabilities,"
                 "invested_cost,by_class_json,positions,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(tenant_id,owner_id,date) DO UPDATE SET net_worth=excluded.net_worth,"
                 "gross_assets=excluded.gross_assets,liabilities=excluded.liabilities,"
                 "invested_cost=excluded.invested_cost,by_class_json=excluded.by_class_json,"
                 "positions=excluded.positions,created_at=excluded.created_at",
                 (owner["tenant_id"], owner["owner_id"], str(d), s["totals"]["net_worth"], s["totals"]["gross_assets"],
                  s["totals"]["liabilities"], s["totals"]["invested_cost"], C.dumps(by), s["totals"]["positions"],
                  C.now()))
    conn.commit()
    return {"date": str(d), **s["totals"], "by_asset_class": by}


def snapshots(conn, owner, days: int = 730) -> list:
    since = str(date.today() - timedelta(days=int(days)))
    out = []
    for r in conn.execute("SELECT * FROM wealth_snapshot WHERE tenant_id=? AND owner_id=? AND date>=? ORDER BY date",
                          (owner["tenant_id"], owner["owner_id"], since)):
        d = dict(r)
        d["by_asset_class"] = C.loads(d.pop("by_class_json"), {})
        out.append(d)
    return out
