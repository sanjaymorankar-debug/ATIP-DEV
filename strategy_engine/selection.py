"""
Strategy selection and combination across strategies (SE-07, SE-08).

REGIME MAPPING (SE-08). Which strategies are in play in each Market Health
regime -- the labels ATIP already computes (scores/engine.py compute_mh):

    STRONG_BULL  BULL  NEUTRAL  BEAR  HIGH_RISK   (+ DEFAULT for anything else)

stored in strategy_regime_mapping and editable (API / CLI): per regime an
ordered list of strategies with priority and weight, or NO_TRADE -- no new
entries in that regime. The shipped default (library/regime_mapping.json) is
loaded once when the table is empty:

    STRONG_BULL -> momentum_rank, atip_zpi_momentum
    BULL        -> atip_zpi_momentum, volume_breakout
    NEUTRAL     -> rsi_mean_reversion, atip_multi_factor
    BEAR        -> NO_TRADE   (ATIP has no defensive strategy yet: map one here
                               when it exists)
    HIGH_RISK   -> NO_TRADE   (capital preservation)

With no mapping rows at all, every strategy in a decision state is in play.

COMBINATION (SE-07). combine() turns several strategies' decisions on one
symbol into one decision. Only strategies in a decision state (PAPER / READY
/ ACTIVE -- the lifecycle is the enable/disable switch) and enabled in the
mapping take part.

    priority  the strategy with the lowest priority number that has an opinion
              (anything but WAIT) decides
    weighted  net = sum(weight x confidence x (+1 BUY, -1 SELL)) / sum(weight);
              BUY when net >= threshold, SELL when net <= -threshold, else HOLD
              when any member holds, else WAIT (threshold default 0.3)
    vote      BUY when BUY votes > SELL votes and BUY >= min_votes (default 1);
              SELL symmetrically; a BUY/SELL tie is a CONFLICT -> WAIT

Conflict handling in every mode: a member's NO_TRADE (its risk requirement
blocked the entry) vetoes a combined BUY; in a NO_TRADE regime a combined BUY
becomes NO_TRADE; SELL is never vetoed (reducing risk is always allowed).

    Strategy A BUY, Strategy B BUY, Strategy C HOLD  --vote-->  BUY

Combined decisions are derived on request from the stored per-strategy
decisions; they are not a strategy of their own and are not stored.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from strategy_engine import lifecycle

MH_REGIMES = ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "HIGH_RISK")
MAP_KEYS = MH_REGIMES + ("DEFAULT",)
MODES = ("priority", "weighted", "vote")
DEFAULT_MAPPING = Path(__file__).resolve().parent / "library" / "regime_mapping.json"


class SelectionError(ValueError):
    pass


# -- regime mapping ---------------------------------------------------------

def get_mapping(conn) -> dict:
    out = {r: {"no_trade": False, "strategies": []} for r in MAP_KEYS}
    for row in conn.execute("SELECT * FROM strategy_regime_mapping ORDER BY regime, priority, strategy_id"):
        d = dict(row)
        slot = out.setdefault(d["regime"], {"no_trade": False, "strategies": []})
        if d["no_trade"]:
            slot["no_trade"] = True
        elif d["strategy_id"]:
            slot["strategies"].append({"strategy_id": d["strategy_id"], "priority": d["priority"],
                                       "weight": d["weight"], "enabled": bool(d["enabled"])})
    return out


def set_mapping(conn, regime: str, entries, actor: str = "owner") -> dict:
    """Replace one regime's mapping. entries: "NO_TRADE", or a list of
    {strategy_id, priority?, weight?, enabled?}."""
    regime = (regime or "").upper()
    if regime not in MAP_KEYS:
        raise SelectionError(f"regime must be one of {MAP_KEYS}")
    rows = []
    if entries == "NO_TRADE" or entries == ["NO_TRADE"]:
        rows = [(regime, None, 1, 100, 1.0, 1)]
    else:
        if not isinstance(entries, list):
            raise SelectionError('entries must be "NO_TRADE" or a list of {strategy_id, priority, weight, enabled}')
        seen = set()
        for i, e in enumerate(entries):
            sid = (e or {}).get("strategy_id")
            if not sid or not conn.execute("SELECT 1 FROM strategy WHERE strategy_id=?", (sid,)).fetchone():
                raise SelectionError(f"entries[{i}]: unknown strategy {sid!r}")
            if sid in seen:
                raise SelectionError(f"{sid} listed twice for {regime}")
            seen.add(sid)
            w = float(e.get("weight", 1.0))
            if w < 0:
                raise SelectionError("weight cannot be negative")
            rows.append((regime, sid, 0, int(e.get("priority", 100 + i)), w, int(bool(e.get("enabled", True)))))
    conn.execute("DELETE FROM strategy_regime_mapping WHERE regime=?", (regime,))
    now = datetime.now()
    conn.executemany("INSERT INTO strategy_regime_mapping (regime,strategy_id,no_trade,priority,weight,enabled,"
                     "updated_at) VALUES (?,?,?,?,?,?,?)", [r + (now,) for r in rows])
    lifecycle.log_event(conn, None, "REGIME_MAPPING", f"{regime} mapping replaced", details={"entries": entries},
                        actor=actor, commit=False)
    conn.commit()
    return get_mapping(conn)[regime]


def seed_default_mapping(conn) -> bool:
    """Load library/regime_mapping.json when no mapping exists yet."""
    if conn.execute("SELECT 1 FROM strategy_regime_mapping LIMIT 1").fetchone() or not DEFAULT_MAPPING.exists():
        return False
    spec = json.loads(DEFAULT_MAPPING.read_text(encoding="utf-8"))
    for regime, entries in spec.get("mapping", {}).items():
        known = entries if entries == "NO_TRADE" else [
            e for e in entries if conn.execute("SELECT 1 FROM strategy WHERE strategy_id=?", (e["strategy_id"],)).fetchone()]
        set_mapping(conn, regime, known, actor="library")
    return True


def selected_strategies(conn, regime: str | None) -> dict:
    """{regime, no_trade, strategies: [{strategy_id, current_version, status, priority, weight}]} --
    the strategies in play for a regime: mapped, enabled, and in a decision state."""
    mapping = get_mapping(conn)
    has_rows = conn.execute("SELECT 1 FROM strategy_regime_mapping LIMIT 1").fetchone() is not None
    slot = mapping.get(regime or "DEFAULT") if regime in mapping else mapping["DEFAULT"]
    live = {r[0]: dict(r) for r in conn.execute(
        f"SELECT strategy_id, current_version, status, priority, weight FROM strategy WHERE status IN "
        f"({','.join('?' * len(lifecycle.DECISION_STATES))})", lifecycle.DECISION_STATES)}
    if not has_rows:
        chosen = sorted(live.values(), key=lambda s: (s["priority"], s["strategy_id"]))
        return {"regime": regime, "no_trade": False, "mapped": False, "strategies": chosen}
    chosen = []
    for e in slot["strategies"]:
        if e["enabled"] and e["strategy_id"] in live:
            chosen.append({**live[e["strategy_id"]], "priority": e["priority"], "weight": e["weight"]})
    return {"regime": regime, "no_trade": slot["no_trade"], "mapped": True,
            "strategies": sorted(chosen, key=lambda s: (s["priority"], s["strategy_id"]))}


# -- combination --------------------------------------------------------------

def combine(votes: list, mode: str = "vote", no_trade_regime: bool = False, threshold: float = 0.3,
            min_votes: int = 1) -> dict:
    """
    votes: [{"strategy_id", "decision", "confidence", "priority", "weight"}] for ONE symbol.
    Returns {"decision", "confidence", "reason", "conflict"}.
    """
    if mode not in MODES:
        raise SelectionError(f"mode must be one of {MODES}")
    opinions = [v for v in votes if v["decision"] != "WAIT"]
    veto = any(v["decision"] == "NO_TRADE" for v in votes)
    tag = ", ".join(f"{v['strategy_id']}:{v['decision']}" for v in votes)
    conflict = False
    if mode == "priority":
        top = sorted(opinions, key=lambda v: (v.get("priority", 100), v["strategy_id"]))
        dec = top[0]["decision"] if top else "WAIT"
        conf = top[0].get("confidence") if top else None
        why = f"priority: {top[0]['strategy_id']} decides" if top else "no member has an opinion"
    elif mode == "weighted":
        tot = sum(float(v.get("weight", 1)) for v in votes) or 1.0
        sign = {"BUY": 1, "SELL": -1}
        net = sum(float(v.get("weight", 1)) * float(v.get("confidence") or 0) * sign.get(v["decision"], 0)
                  for v in votes) / tot
        dec = ("BUY" if net >= threshold else "SELL" if net <= -threshold else
               "HOLD" if any(v["decision"] == "HOLD" for v in votes) else "WAIT")
        conf = round(abs(net), 4)
        conflict = any(v["decision"] == "BUY" for v in votes) and any(v["decision"] == "SELL" for v in votes)
        why = f"weighted net {net:+.3f} (threshold {threshold})"
    else:
        buys = sum(1 for v in votes if v["decision"] == "BUY")
        sells = sum(1 for v in votes if v["decision"] == "SELL")
        if buys and buys == sells:
            dec, conflict = "WAIT", True
        elif buys > sells and buys >= min_votes:
            dec = "BUY"
        elif sells > buys and sells >= min_votes:
            dec = "SELL"
        else:
            dec = "HOLD" if any(v["decision"] == "HOLD" for v in votes) else "WAIT"
        conf = round(max(buys, sells) / len(votes), 4) if votes else None
        why = f"votes BUY {buys} / SELL {sells} of {len(votes)}" + (" — conflict" if conflict else "")
    if dec == "BUY" and (veto or no_trade_regime):
        dec, why = "NO_TRADE", why + ("; vetoed: a member's risk requirement blocked the entry" if veto
                                      else "; regime is NO_TRADE")
    return {"decision": dec, "confidence": conf, "reason": f"{why} [{tag}]", "conflict": conflict}


def combined_decisions(conn, as_of=None, mode: str = "vote", threshold: float = 0.3, min_votes: int = 1) -> dict:
    """Combine the stored decisions of the strategies in play for as_of's regime."""
    from strategy_engine.regime import MarketHealthRegime
    as_of = date.fromisoformat(str(as_of)) if as_of else None
    if as_of is None:
        r = conn.execute("SELECT MAX(as_of) FROM strategy_decision").fetchone()[0]
        if not r:
            return {"as_of": None, "regime": None, "strategies": [], "decisions": []}
        as_of = r if isinstance(r, date) else date.fromisoformat(str(r)[:10])
    regime = MarketHealthRegime(conn, as_of, as_of).on(as_of).get("regime")
    sel = selected_strategies(conn, regime)
    meta = {s["strategy_id"]: s for s in sel["strategies"]}
    per = {}
    for sid, s in meta.items():
        for row in conn.execute("SELECT symbol, decision, confidence FROM strategy_decision WHERE strategy_id=? "
                                "AND version=? AND as_of=?", (sid, s["current_version"], str(as_of))):
            per.setdefault(row[0], []).append({"strategy_id": sid, "decision": row[1], "confidence": row[2],
                                               "priority": s["priority"], "weight": s["weight"]})
    out = []
    for sym in sorted(per):
        c = combine(per[sym], mode, sel["no_trade"], threshold, min_votes)
        out.append({"symbol": sym, **c, "members": per[sym]})
    return {"as_of": str(as_of), "regime": regime, "no_trade_regime": sel["no_trade"], "mode": mode,
            "strategies": [{"strategy_id": k, "version": v["current_version"], "priority": v["priority"],
                            "weight": v["weight"]} for k, v in meta.items()],
            "decisions": out}
