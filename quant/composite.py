"""
Composite factors: weighted combinations of factor scores, versioned.

CompositeDef: name, version, components [{factor_id, weight, direction?,
include?}], min_coverage (share of total weight that must be present for a
symbol to get a composite), normalization (of the composite value, default
percentile). Content-hashed; quant_composite stores it immutably.

composite value = sum(w x s) / sum(w) over PRESENT, INCLUDED components, where
s = the factor's direction-adjusted score (0..100). A component's `direction`
overrides the factor's default (-1 flips the score). Result rows go to
quant_factor_score with factor_key "composite:<name>@<version>" and the same
pct / score / rank / sector_rank fields as factors.

Built-ins:
  vqm@1             value + quality + momentum (+ low volatility)
  mom_lowvol_liq@1  momentum + low volatility + liquidity (price data only)
  vqmg_lowvol@1     value + quality + momentum + growth + low volatility
With fundamental_data empty, composites needing fundamentals fail min_coverage
and produce no rows -- by design, not a fallback to partial scores.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from quant import factors as FX
from quant import normalize as NZ


@dataclass
class CompositeDef:
    name: str
    version: str
    components: list
    min_coverage: float = 0.6
    normalization: dict = field(default_factory=lambda: {"method": "percentile", "winsorize": 0})
    description: str = ""

    def __post_init__(self):
        if not re.match(r"^[a-z][a-z0-9_]{1,40}$", self.name or ""):
            raise ValueError("composite name: lowercase letters, digits, underscore")
        if not self.components:
            raise ValueError("composite needs components")
        for c in self.components:
            FX.get(c["factor_id"])
            if float(c.get("weight", 1)) < 0:
                raise ValueError("weights cannot be negative")
            if c.get("direction", 1) not in (1, -1):
                raise ValueError("direction must be 1 or -1")

    @property
    def key(self):
        return f"composite:{self.name}@{self.version}"

    @property
    def content_hash(self):
        d = asdict(self); d.pop("description")
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


BUILTIN = [
    CompositeDef("vqm", "1", [
        {"factor_id": "earnings_yield", "weight": 0.15}, {"factor_id": "book_to_market", "weight": 0.10},
        {"factor_id": "roe", "weight": 0.15}, {"factor_id": "leverage", "weight": 0.10},
        {"factor_id": "mom_12_1", "weight": 0.25}, {"factor_id": "risk_adj_mom_125", "weight": 0.10},
        {"factor_id": "vol_60", "weight": 0.15}], 0.6, description="value + quality + momentum + low volatility"),
    CompositeDef("mom_lowvol_liq", "1", [
        {"factor_id": "mom_12_1", "weight": 0.30}, {"factor_id": "risk_adj_mom_125", "weight": 0.15},
        {"factor_id": "vol_60", "weight": 0.20}, {"factor_id": "downside_vol_60", "weight": 0.10},
        {"factor_id": "traded_value_20", "weight": 0.25}], 0.6, description="momentum + low volatility + liquidity"),
    CompositeDef("vqmg_lowvol", "1", [
        {"factor_id": "earnings_yield", "weight": 0.15}, {"factor_id": "roe", "weight": 0.15},
        {"factor_id": "cash_quality", "weight": 0.10}, {"factor_id": "mom_12_1", "weight": 0.20},
        {"factor_id": "revenue_growth", "weight": 0.10}, {"factor_id": "growth_consistency", "weight": 0.10},
        {"factor_id": "vol_60", "weight": 0.20}], 0.6,
        description="value + quality + momentum + growth + low volatility"),
]


def save(conn, cd: CompositeDef) -> dict:
    ex = conn.execute("SELECT content_hash FROM quant_composite WHERE name=? AND version=?",
                      (cd.name, cd.version)).fetchone()
    if ex and ex[0] != cd.content_hash:
        raise ValueError(f"composite {cd.name}@{cd.version} exists with different content; use a new version")
    if not ex:
        conn.execute("INSERT INTO quant_composite (name,version,components_json,min_coverage,normalization_json,"
                     "description,content_hash,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (cd.name, cd.version, json.dumps(cd.components), cd.min_coverage, json.dumps(cd.normalization),
                      cd.description, cd.content_hash, "ACTIVE", datetime.now()))
        conn.commit()
    return get(conn, cd.name, cd.version)


def get(conn, name, version=None) -> dict | None:
    q = "SELECT * FROM quant_composite WHERE name=?" + (" AND version=?" if version else "") + \
        " ORDER BY created_at DESC LIMIT 1"
    r = conn.execute(q, (name, version) if version else (name,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["components"] = json.loads(d.pop("components_json"))
    d["normalization"] = json.loads(d.pop("normalization_json") or "{}")
    return d


def ensure_builtin(conn):
    return [save(conn, c) for c in BUILTIN]


def _exposures(conn, as_of, spec) -> dict:
    """W39 (PF-15): {name: {symbol: raw}} for a composite's numeric neutralize names -- the factors'
    raw values stored for as_of (compute the factor first). A name with nothing stored is left
    out, so normalize.apply refuses it."""
    out = {}
    for x in ((spec or {}).get("neutralize") or []):
        if not isinstance(x, str) or x == "sector":
            continue
        fk = FX.get(x).key
        vals = {s: r for s, r in conn.execute("SELECT symbol, raw FROM quant_factor_score WHERE as_of=? AND "
                                              "factor_key=?", (str(as_of), fk))}
        if vals:
            out[x] = vals
    return out


def compute_composites(conn, as_of, names) -> dict:
    from quant.engine import _store, sectors
    out = {}
    sec = sectors()
    for nm in names:
        name, _, ver = nm.partition("@")
        c = get(conn, name, ver or None)
        if not c:
            out[nm] = "unknown composite"; continue
        comps = [x for x in c["components"] if x.get("include", True)]
        total = sum(float(x.get("weight", 1)) for x in comps)
        scores = {}
        for x in comps:
            fk = FX.get(x["factor_id"]).key
            for s, sc in conn.execute("SELECT symbol, score FROM quant_factor_score WHERE as_of=? AND factor_key=?",
                                      (str(as_of), fk)):
                if sc is None:
                    continue
                d = x.get("direction", 1)
                scores.setdefault(s, []).append((float(x.get("weight", 1)), sc if d > 0 else 100 - sc))
        vals = {}
        for s, parts in scores.items():
            w = sum(p[0] for p in parts)
            if total and w / total >= c["min_coverage"]:
                vals[s] = sum(p[0] * p[1] for p in parts) / w
        key = f"composite:{c['name']}@{c['version']}"
        if not vals:
            _store(conn, as_of, [], [key])
            out[key] = {"rows": 0, "reason": "no symbol met min_coverage (missing factor data)"}
            continue
        try:
            norm = NZ.apply(vals, c["normalization"], sec, _exposures(conn, as_of, c["normalization"]))
        except ValueError as e:                         # W39: a neutralize name with no stored raw values
            out[key] = {"rows": 0, "error": f"normalization failed: {e}"}
            continue
        pct = NZ.percentile(vals)
        rk = NZ.rank(pct)
        srank = {}
        for grp in {sec.get(s) for s in vals if sec.get(s)}:
            srank.update(NZ.rank({s: pct[s] for s in vals if sec.get(s) == grp}))
        rows = [(str(as_of), s, key, "composite", v, norm.get(s), pct[s], pct[s], rk[s], sec.get(s), srank.get(s),
                 len(vals)) for s, v in vals.items()]
        _store(conn, as_of, rows, [key])
        out[key] = {"rows": len(rows)}
    return out
