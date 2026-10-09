"""
Option overlays (W40, ENT-15 "strategies do not emit option intents yet"): a strategy kind whose
decisions are multi-leg OPTION intents on the paper options book.

    {
      "strategy_id": "nifty_monthly_condor", "name": "...", "version": "1.0.0",
      "kind": "option_overlay",
      "underlyings": {"source": "symbols", "symbols": ["NIFTY50"]},
      "template": "iron_condor",
      "strikes": {"method": "delta", "near": 0.20, "far": 0.10},
      "expiry": {"rule": "monthly", "min_days": 20},
      "lots": 1,
      "exits": {"profit_take_pct": 50, "stop_loss_pct": 100, "days_before_expiry": 2},
      "option_risk": {"max_loss_pct": 2.0},
      "allow_naked_short_calls": false,
      "position": {"max_positions": 3, "max_new_per_day": 1}
    }

UNDERLYINGS (which symbols get an overlay; ATIP symbols -- NIFTY50 trades the NIFTY chain)
    symbols   {"source": "symbols", "symbols": [...]}                      always candidates
    held      {"source": "held", "symbols": [...] optional filter}          shares held in the PAPER cash
                                                                            book (covered calls, protective puts)
    rule      {"source": "rule", "entry": <condition tree>, "symbols"?}    a scan: rules.py conditions on the
                                                                            features (e.g. score_signal == "BUY")
    strategy  {"source": "strategy", "strategy_id", "version"}             a signal source: that strategy
                                                                            version's BUY decisions that day
TEMPLATE  one of quant/options_strategy.py's TEMPLATES whose option legs have NEAR / FAR strike roles
    (SUPPORTED below). The roles come from the template's own build(): on each option type the leg
    nearest the money is NEAR, the other FAR. covered_call / protective_put are the template's FUT leg
    replaced by shares held in the paper cash book (lots <= held shares / lot size).
STRIKES   from the stored chain on the decision date (data/derivatives_store.chain_on), traded contracts
    (volume or OI > 0) of the chosen expiry only:
    delta     {"method": "delta", "near": 0.30, "far": 0.15}     |Black-Scholes delta| closest to the target
              (IV: the contract's stored IV, else implied from its price, else 18%); far < near
    pct_otm   {"method": "pct_otm", "near": 2, "far": 5}         strike closest to spot x (1 +/- pct/100)
              (CE above, PE below); far > near; 0 = at the money
    wing_strikes  {"...", "near": ..., "wing_strikes": 2}         instead of far: the far leg n listed strikes
              beyond the near one
    Ties go to the further-out-of-the-money strike; a far leg that lands on (or inside) its near leg moves
    to the next listed strike beyond it.
EXPIRY    {"rule": "monthly" | "nearest", "min_days": N}: the first listed expiry at least N days away
    that is monthly -- the last listed expiry of its calendar month falling in the month's final 8 days
    (NSE's monthly contract; a weekly never qualifies) -- or simply the nearest.
LOTS      structure lots (every leg trades lots x lot size); the risk engine may size it down
EXITS     each optional; checked on every decision run against the position's mark at the chain mid
    profit_take_pct     unrealised P&L >= pct% of max profit (unlimited max profit: of the premium paid)
    stop_loss_pct       unrealised P&L <= -pct% of max loss (unlimited max loss: of the credit received)
    days_before_expiry  close when that many days or fewer remain; otherwise expiry settles at intrinsic
    a covered call whose shares are no longer held is closed at once (COVER_GONE)
OPTION_RISK  {"max_loss_pct": % of paper equity (default 2), "max_loss_rupees": Rs} -- the strategy's open
    risk amounts (execution/options_paper.structure_metrics) may not exceed the smaller
ALLOW_NAKED_SHORT_CALLS  false by default; a template that sells a call with no long call (short_call,
    short_straddle, short_strangle) is refused at validation without it, and the risk engine refuses any
    naked short call (e.g. a covered call whose shares are gone) unless it is true.

DECISIONS (one per underlying)
    open position  exit rule fires -> OPTION_CLOSE (reason code PROFIT_TAKE / STOP_LOSS / EXPIRY_WINDOW /
                   COVER_GONE); else HOLD with its mark
    candidate      no open position, chain / expiry / strikes found -> OPTION_OPEN (confidence = the
                   structure's probability of profit); else NO_ACTION OPTION_UNAVAILABLE with the reason.
                   The strategy's own risk gate (max_cri, blocked_regimes, kill switch) can block it;
                   position.max_positions / max_new_per_day apply (best probability of profit first).
    The plan (legs, template, expiry, lots) travels in the decision's features as "option_plan"; the W4
    risk engine sizes and checks it (execution/option_intents.py) and the OMS routes it to the paper
    options book as one all-or-nothing order (execution/options_paper.py). LIVE is refused there.

    dry_run(conn, strategy_id, as_of)   today's legs and risk check -- nothing stored, nothing placed
    replay(conn, strategy_id, start, end)  a simple historical replay on stored daily option prices
                   (fo_contract_daily: the nearest NEAREST_EXPIRIES expiries of each session) -- see its
                   docstring for what it does not model. A W2 backtest cannot trade option intents: it
                   counts them as not simulated.
"""

from __future__ import annotations

import calendar
import dataclasses
from datetime import date

from quant.options_strategy import DEFAULT_IV, RISK_FREE, TEMPLATES as QUANT_TEMPLATES
from strategy_engine import rules as R
from strategy_engine.decisions import A_BLOCKED, A_BUY, A_HOLD, A_NONE, A_OPTION_CLOSE, A_OPTION_OPEN, risk_gate
from strategy_engine.kinds import EVALUATORS, Evaluator, make_evaluator
from strategy_engine.params import value as pval

SUPPORTED = ("covered_call", "protective_put", "bull_call_spread", "bear_put_spread", "iron_condor",
             "bull_put_spread", "bear_call_spread", "iron_butterfly", "long_call", "long_put", "short_put",
             "short_call", "long_straddle", "long_strangle", "short_straddle", "short_strangle")
SHARE_TEMPLATES = ("covered_call", "protective_put")          # the template's FUT leg = shares held
SOURCES = {"symbols": {"source", "symbols"}, "held": {"source", "symbols"}, "rule": {"source", "symbols", "entry"},
           "strategy": {"source", "strategy_id", "version"}}
STRIKE_KEYS = {"method", "near", "far", "wing_strikes"}
EXIT_KEYS = {"profit_take_pct", "stop_loss_pct", "days_before_expiry"}
RISK_KEYS = {"max_loss_pct", "max_loss_rupees"}
POSITION_KEYS = {"max_positions", "max_new_per_day"}


class OverlayError(ValueError):
    pass


def _roles(template: str) -> list:
    """The option legs of a quant/options_strategy.py template with their strike role: on each option
    type the leg nearest the money is NEAR, the other FAR. FUT legs (covered call, protective put) are
    the shares held and are dropped."""
    a, w = 1000.0, 10.0
    opts = [leg for leg in QUANT_TEMPLATES[template]["build"](a, w, "2099-12-31") if leg["kind"] in ("CE", "PE")]
    out = []
    for leg in opts:
        if int(leg.get("lots") or 1) != 1:
            raise OverlayError(f"{template}: ratio legs are not supported")
        nearest = min(abs(x["strike"] - a) for x in opts if x["kind"] == leg["kind"])
        out.append({"option_type": leg["kind"], "side": leg["side"],
                    "role": "near" if abs(leg["strike"] - a) == nearest else "far"})
    return out


LEG_ROLES = {t: _roles(t) for t in SUPPORTED}
FAR_TEMPLATES = tuple(t for t in SUPPORTED if any(x["role"] == "far" for x in LEG_ROLES[t]))
NAKED_CALL_TEMPLATES = tuple(
    t for t in SUPPORTED if t not in SHARE_TEMPLATES and
    sum(1 for x in LEG_ROLES[t] if x["option_type"] == "CE" and x["side"] == "SELL") >
    sum(1 for x in LEG_ROLES[t] if x["option_type"] == "CE" and x["side"] == "BUY"))


# ── validation ──────────────────────────────────────────────────────────────────────────────

def _num(v, path, lo, hi, specs, integer=False, lo_open=False, hi_open=False):
    """A literal number (or a {"param": name} whose default is checked) within [lo, hi]; returns the
    number checked, or None for a parameter with no default."""
    if isinstance(v, dict) and set(v) == {"param"}:
        sp = specs.get(v["param"])
        if sp is None:
            raise OverlayError(f"{path}: undeclared parameter {v['param']!r}")
        if sp.default is None:
            return None
        v = sp.default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise OverlayError(f"{path} must be a number")
    if integer and int(v) != v:
        raise OverlayError(f"{path} must be a whole number")
    if v < lo or v > hi or (lo_open and v == lo) or (hi_open and v == hi):
        raise OverlayError(f"{path} must be in {'(' if lo_open else '['}{lo:g}, {hi:g}{')' if hi_open else ']'}")
    return v


def _obj(d, key, allowed, default=None):
    v = d.setdefault(key, default if default is not None else {})
    if not isinstance(v, dict):
        raise OverlayError(f"{key} must be an object")
    extra = set(v) - allowed
    if extra:
        raise OverlayError(f"{key}: unknown keys {sorted(extra)} (allowed: {sorted(allowed)})")
    return v


def validate_overlay(d: dict, declared: set, specs: dict) -> set:
    """Strict check of an option_overlay definition (called by definition.validate, which has already
    checked the common keys). Normalises it in place (defaults written, universe derived) and returns
    the features it uses."""
    used = {"close"}
    t = d.get("template")
    if t not in SUPPORTED:
        raise OverlayError(f"template must be one of {SUPPORTED} (quant/options_strategy.py templates whose "
                           f"legs have near / far strikes)")
    bad = set(d.get("position") or {}) - POSITION_KEYS
    if bad:
        raise OverlayError(f"position keys {sorted(bad)} are not used by option_overlay (sizing is in lots; exits "
                           f"are the exits object)")
    for k in POSITION_KEYS & set(d.get("position") or {}):
        _num(d["position"][k], f"position.{k}", 1, 1000, specs, integer=True)

    u = d.get("underlyings")
    if not isinstance(u, dict) or u.get("source") not in SOURCES:
        raise OverlayError(f"underlyings must be an object with source one of {sorted(SOURCES)}")
    extra = set(u) - SOURCES[u["source"]]
    if extra:
        raise OverlayError(f"underlyings ({u['source']}): unknown keys {sorted(extra)}")
    if "symbols" in u:
        if not isinstance(u["symbols"], list) or not all(isinstance(s, str) and s.strip() for s in u["symbols"]):
            raise OverlayError("underlyings.symbols must be a list of symbols")
        u["symbols"] = sorted({s.strip().upper() for s in u["symbols"]})
    if u["source"] == "symbols" and not u.get("symbols"):
        raise OverlayError("underlyings.symbols must list at least one symbol")
    if u["source"] == "rule":
        if not u.get("entry"):
            raise OverlayError("underlyings.entry (a condition tree) is required for source rule")
        used |= R.validate(u["entry"], declared, "underlyings.entry")
    if u["source"] == "strategy":
        sid, ver = u.get("strategy_id"), u.get("version")
        if not isinstance(sid, str) or not sid or not isinstance(ver, str) or not ver:
            raise OverlayError("underlyings.strategy_id and underlyings.version are required for source strategy")
        if sid == d.get("strategy_id"):
            raise OverlayError("an option overlay cannot take its signals from itself")
    if u.get("symbols"):
        d["universe"] = {"type": "symbols", "symbols": list(u["symbols"])}

    st = _obj(d, "strikes", STRIKE_KEYS)
    method = st.get("method")
    if method not in ("delta", "pct_otm"):
        raise OverlayError("strikes.method must be delta or pct_otm")
    if "near" not in st:
        raise OverlayError("strikes.near is required")
    if method == "delta":
        near = _num(st["near"], "strikes.near (|delta|)", 0, 1, specs, lo_open=True, hi_open=True)
    else:
        near = _num(st["near"], "strikes.near (% OTM)", 0, 50, specs)
    has_far, has_wing = st.get("far") is not None, st.get("wing_strikes") is not None
    if t in FAR_TEMPLATES:
        if has_far == has_wing:
            raise OverlayError(f"{t} has far legs: give strikes.far or strikes.wing_strikes (one of them)")
        if has_far:
            if method == "delta":
                far = _num(st["far"], "strikes.far (|delta|)", 0, 1, specs, lo_open=True, hi_open=True)
                if near is not None and far is not None and far >= near:
                    raise OverlayError("strikes.far must be a smaller |delta| than strikes.near (further out)")
            else:
                far = _num(st["far"], "strikes.far (% OTM)", 0, 50, specs, lo_open=True)
                if near is not None and far is not None and far <= near:
                    raise OverlayError("strikes.far must be further out of the money than strikes.near")
        else:
            _num(st["wing_strikes"], "strikes.wing_strikes", 1, 50, specs, integer=True)
    elif has_far or has_wing:
        raise OverlayError(f"{t} has no far leg: strikes.far / wing_strikes are not used")

    ex = _obj(d, "expiry", {"rule", "min_days"}, {"rule": "monthly", "min_days": 7})
    ex.setdefault("rule", "monthly")
    ex.setdefault("min_days", 7)
    if ex["rule"] not in ("monthly", "nearest"):
        raise OverlayError("expiry.rule must be monthly or nearest")
    _num(ex["min_days"], "expiry.min_days", 0, 365, specs, integer=True)

    d.setdefault("lots", 1)
    _num(d["lots"], "lots", 1, 1000, specs, integer=True)

    exits = _obj(d, "exits", EXIT_KEYS)
    if "profit_take_pct" in exits:
        _num(exits["profit_take_pct"], "exits.profit_take_pct", 0, 1000, specs, lo_open=True)
    if "stop_loss_pct" in exits:
        _num(exits["stop_loss_pct"], "exits.stop_loss_pct", 0, 1000, specs, lo_open=True)
    if "days_before_expiry" in exits:
        _num(exits["days_before_expiry"], "exits.days_before_expiry", 0, 60, specs, integer=True)

    rk = _obj(d, "option_risk", RISK_KEYS)
    if not rk:
        rk["max_loss_pct"] = 2.0
    if "max_loss_pct" in rk:
        _num(rk["max_loss_pct"], "option_risk.max_loss_pct", 0, 100, specs, lo_open=True)
    if "max_loss_rupees" in rk:
        _num(rk["max_loss_rupees"], "option_risk.max_loss_rupees", 0, 1e12, specs, lo_open=True)

    naked = d.setdefault("allow_naked_short_calls", False)
    if not isinstance(naked, bool):
        raise OverlayError("allow_naked_short_calls must be true or false")
    if t in NAKED_CALL_TEMPLATES and not naked:
        raise OverlayError(f"{t} sells a naked call: set allow_naked_short_calls: true to allow it (the risk "
                           f"engine refuses naked short calls otherwise)")
    return used


def overlay_config(defn: dict, params: dict) -> dict:
    """The definition with every {"param": ...} resolved, re-checked at run time."""
    v = lambda x: pval(x, params)                                     # noqa: E731
    st, ex = defn["strikes"], defn.get("expiry") or {}
    cfg = {"template": defn["template"], "method": st["method"], "near": float(v(st["near"])),
           "far": float(v(st["far"])) if st.get("far") is not None else None,
           "wing_strikes": int(v(st["wing_strikes"])) if st.get("wing_strikes") is not None else None,
           "expiry_rule": ex.get("rule", "monthly"), "min_days": int(v(ex.get("min_days", 7))),
           "lots": int(v(defn.get("lots", 1))),
           "exits": {k: v(x) for k, x in (defn.get("exits") or {}).items()},
           "allow_naked": defn.get("allow_naked_short_calls") is True}
    if cfg["method"] == "delta" and not 0 < cfg["near"] < 1:
        raise OverlayError("strikes.near must be a |delta| in (0, 1)")
    if cfg["far"] is not None and ((cfg["method"] == "delta" and cfg["far"] >= cfg["near"]) or
                                   (cfg["method"] == "pct_otm" and cfg["far"] <= cfg["near"])):
        raise OverlayError("strikes.far must be further out of the money than strikes.near")
    if cfg["lots"] < 1:
        raise OverlayError("lots must be at least 1")
    return cfg


# ── expiry and strike selection ────────────────────────────────────────────────────────────

def _date(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def is_monthly(e: date, listed: list) -> bool:
    """NSE's monthly contract: the last listed expiry of its calendar month, in the month's final 8 days."""
    dim = calendar.monthrange(e.year, e.month)[1]
    same = [x for x in listed if (x.year, x.month) == (e.year, e.month)]
    return e == max(same) and e.day >= dim - 7


def select_expiry(expiries, as_of, rule: str = "monthly", min_days: int = 7) -> str | None:
    """The first listed expiry >= min_days away (monthly ones only for rule monthly), or None."""
    d = _date(as_of)
    listed = sorted({_date(e) for e in expiries})
    for e in listed:
        if (e - d).days >= int(min_days) and (rule == "nearest" or is_monthly(e, listed)):
            return str(e)
    return None


def leg_delta(q: dict, spot: float, strike: float, years: float, option_type: str) -> tuple:
    """(Black-Scholes delta, IV used): the stored IV, else implied from the price, else DEFAULT_IV."""
    from quant.derivatives import greeks, implied_vol
    kind = "call" if option_type == "CE" else "put"
    iv = q.get("iv") or implied_vol(q["price"], spot, strike, years, RISK_FREE, kind) or DEFAULT_IV
    return greeks(spot, strike, years, RISK_FREE, iv, kind)["delta"], iv


def _pick(cands: list, option_type: str, method: str, target: float, spot: float, years: float) -> float:
    """The strike (from [(strike, quote)], ascending) closest to the target; ties -> further OTM."""
    def dist(kq):
        k, q = kq
        if method == "delta":
            return abs(abs(leg_delta(q, spot, k, years, option_type)[0]) - target)
        aim = spot * (1 + target / 100) if option_type == "CE" else spot * (1 - target / 100)
        return abs(k - aim)
    return min(cands, key=lambda kq: (round(dist(kq), 9), -kq[0] if option_type == "CE" else kq[0]))[0]


def select_legs(chain: dict, expiry: str, spot: float, cfg: dict, as_of) -> list:
    """The template's legs on the stored chain: strikes by cfg (delta / pct_otm / wing_strikes), traded
    contracts of `expiry` only. Each leg: {leg_no, option_type, side, role, strike, expiry, mid, iv, delta}."""
    years = max((_date(expiry) - _date(as_of)).days, 0) / 365.0
    if years <= 0:
        raise OverlayError(f"expiry {expiry} is not after {as_of}")
    roles = LEG_ROLES[cfg["template"]]
    cands = {}
    for ot in sorted({r["option_type"] for r in roles}):
        rows = sorted((k[1], q) for k, q in chain["quotes"].items()
                      if k[0] == expiry and k[2] == ot and ((q.get("oi") or 0) > 0 or (q.get("volume") or 0) > 0))
        if not rows:
            raise OverlayError(f"no traded {ot} strikes for the {expiry} expiry")
        cands[ot] = rows
    near = {ot: _pick(c, ot, cfg["method"], cfg["near"], spot, years) for ot, c in cands.items()}
    far = {}
    for ot in {r["option_type"] for r in roles if r["role"] == "far"}:
        ks = [k for k, _q in cands[ot]]
        i, step = ks.index(near[ot]), (1 if ot == "CE" else -1)
        if cfg.get("wing_strikes"):
            j = i + step * int(cfg["wing_strikes"])
        else:
            kf = _pick(cands[ot], ot, cfg["method"], cfg["far"], spot, years)
            j = ks.index(kf)
            if (j - i) * step <= 0:                      # on or inside the near leg: the next strike out
                j = i + step
        if not 0 <= j < len(ks):
            raise OverlayError(f"the {expiry} {ot} chain has no strike beyond {near[ot]:g} for the far leg")
        far[ot] = ks[j]
    legs = []
    for n, r in enumerate(roles, start=1):
        k = near[r["option_type"]] if r["role"] == "near" else far[r["option_type"]]
        q = chain["quotes"][(expiry, k, r["option_type"])]
        delta, iv = leg_delta(q, spot, k, years, r["option_type"])
        legs.append({"leg_no": n, "option_type": r["option_type"], "side": r["side"], "role": r["role"], "strike": k,
                     "expiry": expiry, "mid": q["price"], "iv": round(iv, 4) if iv else None,
                     "delta": round(delta, 4) if delta is not None else None})
    return legs


def plan_overlay(conn, symbol: str, as_of, cfg: dict, held: dict, close=None, chain=None,
                 free_shares: int | None = None) -> tuple:
    """(plan, info) for opening an overlay on `symbol` on `as_of`, or (None, why not). info carries the
    structure's payoff at the chain mid (execution/options_paper.structure_metrics). Share templates
    use the shares held (`held`, and for a covered call those no open covered call already covers),
    unless `free_shares` says how many there are (the replay)."""
    from data.derivatives_store import chain_on, fo_symbol
    from execution import options_paper as O
    u = fo_symbol(symbol)
    ch = chain if chain is not None else chain_on(conn, u, as_of)
    if not ch["quotes"]:
        return None, f"no option chain stored for {u} on or before {as_of} (not an F&O underlying, or no F&O data)"
    age = O.chain_age(ch, as_of)
    lim = int(O.settings()["max_chain_age_sessions"])
    if age is not None and age > lim:
        return None, f"the stored {u} chain is {age} sessions old ({ch['session']})"
    spot = float(ch["spot"] or close or 0)
    if spot <= 0:
        return None, f"no underlying price for {u}"
    L = ch["lot_size"]
    if not L:
        return None, f"lot size unknown for {u}"
    exp = select_expiry(ch["expiries"], as_of, cfg["expiry_rule"], cfg["min_days"])
    if not exp:
        return None, (f"no {cfg['expiry_rule']} expiry >= {cfg['min_days']} days in the stored {u} chain "
                      f"({', '.join(ch['expiries']) or 'none'})")
    t, lots, free = cfg["template"], cfg["lots"], 0
    if t in SHARE_TEMPLATES:
        if free_shares is not None:
            free = int(free_shares)
        else:
            h = held.get(symbol) or {}
            own = 0 if h.get("short") else int(h.get("qty") or 0)
            free = min(O.cover_available(conn, symbol), own) if t == "covered_call" else own
        if free // L < 1:
            return None, f"{t} needs held shares: {free} free share(s) of {symbol}, one lot is {L}"
        lots = min(lots, free // L)
    try:
        legs = select_legs(ch, exp, spot, cfg, as_of)
        m = O.structure_metrics([{**leg, "price": leg["mid"]} for leg in legs], spot, L, lots, cover_shares=free,
                                uses_shares=t == "covered_call", allow_naked=cfg["allow_naked"], as_of=_date(as_of))
    except ValueError as e:
        return None, str(e)
    plan = {"action": A_OPTION_OPEN, "symbol": symbol, "underlying": u, "template": t, "expiry": exp, "lots": lots,
            "lot_size": L, "uses_shares": t == "covered_call", "exits": cfg["exits"], "spot": spot,
            "chain": ch["source"],
            "legs": [{k: leg[k] for k in ("leg_no", "option_type", "side", "role", "strike", "expiry")} for leg in legs]}
    return plan, {"legs": legs, "metrics": m, "chain": ch}


# ── the evaluator ────────────────────────────────────────────────────────────────────────────

class OptionOverlayEvaluator(Evaluator):
    """Kind option_overlay: OPTION_OPEN / HOLD / OPTION_CLOSE / NO_ACTION per underlying (module docstring)."""

    def __init__(self, defn, params, loader=None, depth=0):
        super().__init__(defn, params, loader, depth)
        self.cfg = overlay_config(defn, params)
        self.conn = None                     # an open connection to read from (dry run / replay); else its own
        self.member = None
        u = defn["underlyings"]
        if u["source"] == "strategy":
            if depth > 3:
                raise ValueError("strategies nested more than 3 deep")
            if loader is None:
                raise ValueError("an option overlay with a strategy signal source needs a loader")
            from strategy_engine.definition import specs
            from strategy_engine.params import resolve
            mdef = loader(u["strategy_id"], u["version"])
            self.member = make_evaluator(mdef, resolve(specs(mdef)), loader, depth + 1)

    def decide(self, env, as_of, held, session_index=None):
        if self.conn is not None:
            return self._decide(self.conn, env, as_of, held or {}, session_index)
        from db.schema import get_connection
        conn = get_connection()
        try:
            return self._decide(conn, env, as_of, held or {}, session_index)
        finally:
            conn.close()

    def _gate(self, dec):
        """The strategy's own risk gate (max_cri, blocked regimes, kill switch) on an OPTION_OPEN, as
        decisions.risk_gate applies it to a BUY."""
        probe = dataclasses.replace(dec, action=A_BUY, reasons=list(dec.reasons), reason_codes=[])
        risk_gate(probe, self.risk, dec.features)
        if probe.action == A_BLOCKED:
            dec.block(probe.blocked_reason)
            dec.reason_codes.append("RISK_BLOCKED")
        return dec

    def candidates(self, env, as_of, held, session_index=None) -> list:
        """[(symbol, ctx, why)] the underlying selection picks on as_of. Listed and held underlyings need no
        bar that day -- ctx may be None: an index (NIFTY50) has no tradeable bars, its spot comes from the
        chain; a scan or a signal strategy needs the symbol's features, so only symbols with a bar count."""
        u, out = self.defn["underlyings"], []
        syms = list(u.get("symbols") or [])
        if u["source"] == "symbols":
            pool = [(s, "listed underlying") for s in syms]
        elif u["source"] == "held":
            pool = [(s, f"held {h.get('qty')} shares") for s, h in sorted(held.items())
                    if not h.get("short") and (h.get("qty") or 0) > 0 and (not syms or s in syms)]
        elif u["source"] == "rule":
            pool = []
            for s in (syms or list(env.universe)):
                ctx = env.context(s, as_of)
                if ctx is None:
                    continue
                met, _t, _n, trace = R.evaluate(u["entry"], ctx, self.params)
                if met:
                    pool.append((s, "scan: " + "; ".join(trace)[:300]))
        else:
            pool = [(d.symbol, f"signal {u['strategy_id']}@{u['version']} BUY: {d.reason[:200]}")
                    for d in self.member.decide(env, as_of, held, session_index) if d.action == A_BUY]
        for s, why in pool:
            ctx = env.context(s, as_of)
            if ctx is not None or u["source"] in ("symbols", "held"):
                out.append((s, ctx, why))
        return out

    def _decide(self, conn, env, as_of, held, session_index):
        from execution import options_paper as O
        sid, out = self.defn["strategy_id"], []
        # a decision for a past date (a W2 backtest bar) must not see positions opened after it; a live run
        # (as_of within the last few days: the latest session, a weekend run) sees every open position
        live = (date.today() - _date(as_of)).days <= 4
        open_pos = {p["symbol"]: p for p in O.open_positions(conn, sid)
                    if live or str(p["opened_at"])[:10] <= str(as_of)}
        for sym, pos in sorted(open_pos.items()):
            ctx = env.context(sym, as_of)
            mk = O.mark_position(conn, pos, as_of)
            code, why = mk["exit_signal"], mk["exit_why"]
            if not code and pos.get("covered_shares"):
                h = held.get(sym) or {}
                if h.get("short") or int(h.get("qty") or 0) < int(pos["covered_shares"]):
                    code, why = "COVER_GONE", (f"the {pos['covered_shares']} shares covering the short call are no "
                                               f"longer held ({int(h.get('qty') or 0)})")
            head = f"{pos['template']} {pos['underlying']} {pos['expiry']} ({pos['position_id']})"
            expired = _date(pos["expiry"]) < _date(as_of)        # settle_expired closes it, not an order
            if code and not expired:
                dec = self.intent(sym, as_of, A_OPTION_CLOSE, 1.0, f"{head}: {why}", ctx)
                dec.reason_codes.append(code)
            else:
                code = None
                u_ = "unknown (a leg has no price)" if mk["unrealized"] is None else f"{mk['unrealized']:,.0f}"
                dec = self.intent(sym, as_of, A_HOLD, None, f"{head}: " + (
                    "expired, awaiting settlement at intrinsic value" if expired else f"holding, unrealised {u_}"), ctx)
            dec.features.update({
                "option_position_id": pos["position_id"], "option_template": pos["template"],
                "option_underlying": pos["underlying"], "option_expiry": str(pos["expiry"])[:10],
                "option_lots": pos["lots"], "option_mark": {k: mk[k] for k in ("liq_value", "unrealized", "complete",
                                                                              "spot", "session", "source")},
                "option_exit": code})
            if mk.get("spot"):
                dec.features.setdefault("close", mk["spot"])
            if code:
                dec.features["option_plan"] = {"action": A_OPTION_CLOSE, "symbol": sym, "underlying": pos["underlying"],
                                               "position_id": pos["position_id"], "exit_reason": code,
                                               "template": pos["template"], "expiry": str(pos["expiry"])[:10],
                                               "lots": pos["lots"],
                                               "legs": [{k: leg[k] for k in ("leg_no", "option_type", "side",
                                                                             "strike", "expiry")}
                                                        for leg in pos["legs"]]}
            out.append(dec)
        entries = []
        for sym, ctx, why in self.candidates(env, as_of, held, session_index):
            if sym in open_pos:
                continue
            plan, info = plan_overlay(conn, sym, as_of, self.cfg, held, ctx.get("close") if ctx else None)
            if plan is None:
                dec = self.intent(sym, as_of, A_NONE, None, f"{why}; no option entry: {info}", ctx)
                dec.reason_codes.append("OPTION_UNAVAILABLE")
                out.append(dec)
                continue
            m = info["metrics"]
            mp = "unlimited" if m["max_profit"] is None else f"{m['max_profit']:,.0f}"
            ml = "unlimited" if m["max_loss"] is None else f"{-m['max_loss']:,.0f}"
            legs_txt = ", ".join(f"{x['side']} {x['strike']:g}{x['option_type']}" for x in info["legs"])
            dec = self.intent(sym, as_of, A_OPTION_OPEN, (m["pop_pct"] or 0) / 100,
                              f"{why} -> {plan['template']} {plan['underlying']} {plan['expiry']} x{plan['lots']}: "
                              f"{legs_txt}; net {'credit' if m['net_premium'] > 0 else 'debit'} "
                              f"{abs(m['net_premium']):,.0f}, max profit {mp}, max loss {ml}, POP {m['pop_pct']}%",
                              ctx)
            dec.reason_codes.append("OPTION_ENTRY_" + plan["template"].upper())
            dec.features.update({
                "option_template": plan["template"], "option_underlying": plan["underlying"],
                "option_expiry": plan["expiry"], "option_lots": plan["lots"], "option_lot_size": plan["lot_size"],
                "option_spot": plan["spot"], "option_chain": plan["chain"], "option_legs": info["legs"],
                "option_net_premium": m["net_premium"], "option_max_profit": m["max_profit"],
                "option_max_loss": m["max_loss"], "option_risk_amount": m["risk_amount"],
                "option_margin": m["margin"], "option_pop_pct": m["pop_pct"], "option_breakevens": m["breakevens"],
                "option_plan": plan})
            dec.features.setdefault("close", plan["spot"])         # the intent's reference: the underlying
            entries.append(self._gate(dec))
        max_pos, max_new = self.pos.get("max_positions"), self.pos.get("max_new_per_day")
        taken = 0
        for dec in sorted(entries, key=lambda x: (-(x.confidence or 0), x.symbol)):
            if dec.action == A_OPTION_OPEN:
                if (max_pos is None or len(open_pos) + taken < max_pos) and (max_new is None or taken < max_new):
                    taken += 1
                else:
                    dec.action = A_NONE
                    dec.reason_codes.append("POSITION_LIMIT")
                    dec.reasons = dec.reasons + ["entry found but position limits reached (max_positions / "
                                                 "max_new_per_day)"]
            out.append(dec)
        return out


EVALUATORS["option_overlay"] = OptionOverlayEvaluator


# ── dry run ──────────────────────────────────────────────────────────────────────────────────

def dry_run(conn, strategy_id: str, as_of=None, version: str | None = None) -> dict:
    """Today's (or as_of's) decisions of an option overlay with the legs, the payoff and the W4 risk check
    each OPTION intent would get -- nothing stored, nothing placed. `risk` is the full W4 evaluation
    (store=False), gates included; when a gate stops it before the option checks (a DRAFT strategy, the
    kill switch ...), `option_checks` also shows the option checks alone (execution/option_intents.preview)."""
    from execution import option_intents as OI
    from execution import risk_engine as RE
    from strategy_engine import registry
    from strategy_engine.engine import generate_decisions
    v = registry.get_version(conn, strategy_id, version)
    if not v:
        raise ValueError(f"no strategy {strategy_id} {version or ''}".strip())
    if v["definition"].get("kind") != "option_overlay":
        raise ValueError(f"{strategy_id} is a {v['definition'].get('kind')} strategy; the option dry run is for "
                         f"option_overlay strategies")
    st = conn.execute("SELECT status FROM strategy WHERE strategy_id=?", (strategy_id,)).fetchone()
    r = generate_decisions(strategy_id, v["version"], as_of, "PAPER", store=False, conn=conn)
    intents = {i["decision_id"]: i for i in r["intents"]}
    rows = []
    for d in r["decisions"]:
        f = d.get("features") or {}
        if not any(k.startswith("option_") for k in f) and "OPTION_UNAVAILABLE" not in (d.get("reason_codes") or []):
            continue
        row = {"symbol": d["symbol"], "action": d["action"], "decision": d["decision"], "reasons": d["reasons"],
               "reason_codes": d["reason_codes"], "confidence": d["confidence"],
               "blocked_reason": d.get("blocked_reason"),
               **{k[len("option_"):]: val for k, val in f.items() if k.startswith("option_") and k != "option_plan"}}
        it = intents.get(d["decision_id"])
        if it:
            row_it = {**it, "version": it["strategy_version"], "as_of": r["as_of"], "book": "PAPER",
                      "authorization_status": "NOT_AUTHORIZED", "_option_plan": f.get("option_plan")}
            rd = RE.evaluate(conn, it["intent_id"], actor="dry_run", store=False, intent=row_it)
            row["risk"] = {"status": rd.risk_status, "approved_lots": rd.approved_quantity,
                           "requested_lots": rd.requested_quantity, "reason": rd.rejection_reason,
                           "est_value": rd.est_value, "checks": rd.risk_checks}
            if not any(c["check"] in ("instrument", "options_book") for c in rd.risk_checks):
                p = OI.preview(conn, row_it)
                row["option_checks"] = {"status": p.risk_status, "approved_lots": p.approved_quantity,
                                        "reason": p.rejection_reason, "checks": p.risk_checks}
        rows.append(row)
    return {"strategy_id": strategy_id, "version": v["version"], "status": st[0] if st else None,
            "as_of": r["as_of"], "dry_run": True, "stored": False, "placed": False, "counts": r["counts"],
            "decisions": rows,
            "note": "Dry run: decisions and the risk check for this date, computed and discarded. Nothing is "
                    "stored and no order is placed; fills would re-price every leg from the chain at that time."}


# ── historical replay ───────────────────────────────────────────────────────────────────────

def replay(conn, strategy_id: str, start, end, version: str | None = None) -> dict:
    """
    A SIMPLE replay of an option overlay on the daily option prices ATIP stores (fo_contract_daily).

    Every session in [start, end] with stored contracts for the underlying, in order:
      1. an open position whose expiry has come is settled at intrinsic value (the expiry day's close);
         otherwise it is marked at that session's close and closed at the fill rule (close -/+
         slippage_bps) when an exit rule fires
      2. with no position (and none closed that session), the overlay opens one exactly as the live
         strategy would select it -- expiry, strikes by delta / % OTM -- filled at the fill rule
    What it is NOT: the W4 risk caps are not applied (lots as defined); covered calls / protective puts
    assume the shares are held and report the option legs' P&L alone; only underlyings.source "symbols"
    is replayed; fo_contract_daily keeps only the nearest `derivatives.nearest_expiries` (2) expiries
    of each session, so a monthly expiry further out (an index with weeklies) is often not there --
    those sessions are counted in `skipped`; EOD closes of thin strikes can be stale. A hypothesis
    check, not a backtest of record.
    """
    from data.derivatives_store import chain_on, fo_symbol
    from execution import options_paper as O
    from strategy_engine import registry
    from strategy_engine.definition import specs
    from strategy_engine.params import resolve
    v = registry.get_version(conn, strategy_id, version)
    if not v or v["definition"].get("kind") != "option_overlay":
        raise ValueError(f"{strategy_id}: no option_overlay strategy version to replay")
    defn = v["definition"]
    if defn["underlyings"]["source"] != "symbols":
        raise ValueError("the replay covers underlyings.source 'symbols' only (held shares, scans and signal "
                         "strategies need a book / feature history it does not rebuild)")
    cfg = overlay_config(defn, resolve(specs(defn)))
    s = O.settings()
    d0, d1 = _date(start), _date(end)
    if d1 < d0:
        raise ValueError("end is before start")
    trades, skipped, curve = [], {}, []
    for sym in defn["underlyings"]["symbols"]:
        u = fo_symbol(sym)
        dates = [_date(r[0]) for r in conn.execute(
            "SELECT DISTINCT date FROM fo_contract_daily WHERE symbol=? AND date>=? AND date<=? AND option_type IN "
            "('CE','PE') ORDER BY date", (u, str(d0), str(d1)))]
        pos = None
        for d in dates:
            ch = chain_on(conn, u, d)
            closed_today = False
            if pos and _date(pos["expiry"]) <= d:
                spot = (O.settlement_spot(conn, u, pos["expiry"], exact=True)
                        or O.settlement_spot(conn, u, pos["expiry"]))
                if spot:
                    px = [O.intrinsic(l["option_type"], l["strike"], spot) for l in pos["legs"]]
                    trades.append(_replay_close(pos, px, d, "EXPIRY", s, spot))
                    pos, closed_today = None, True
            elif pos:
                mk = O.mark_position(conn, pos, d, chain=ch)
                if mk["exit_signal"]:
                    px = []
                    for l in pos["legs"]:
                        q = O.leg_quote(conn, u, l["expiry"], l["strike"], l["option_type"], O.CLOSING[l["side"]], s,
                                        chain=ch)
                        px.append(q["price"] if q else None)
                    if None not in px:
                        trades.append(_replay_close(pos, px, d, mk["exit_signal"], s, ch.get("spot")))
                        pos, closed_today = None, True
            if pos is None and not closed_today:
                plan, info = plan_overlay(conn, sym, d, cfg, {}, chain=ch, free_shares=10 ** 9)
                if plan is None:
                    key = info.split(":")[0][:80]
                    skipped[key] = skipped.get(key, 0) + 1
                    continue
                priced = []
                for leg in info["legs"]:
                    q = O.leg_quote(conn, u, leg["expiry"], leg["strike"], leg["option_type"], leg["side"], s, chain=ch)
                    priced.append({**leg, "price": q["price"]})
                try:
                    m = O.structure_metrics(priced, plan["spot"], plan["lot_size"], plan["lots"], cover_shares=10 ** 9,
                                            uses_shares=plan["uses_shares"], allow_naked=cfg["allow_naked"], s=s,
                                            as_of=d)
                except ValueError as e:
                    skipped[str(e)[:80]] = skipped.get(str(e)[:80], 0) + 1
                    continue
                q = plan["lots"] * plan["lot_size"]
                pos = {"position_id": f"replay-{sym}-{d}", "underlying": u, "symbol": sym, "template": plan["template"],
                       "expiry": plan["expiry"], "lots": plan["lots"], "lot_size": plan["lot_size"], "opened": str(d),
                       "exits": cfg["exits"], "profit_base": m["profit_base"], "loss_base": m["loss_base"],
                       "fees": m["fees"], "net_premium": m["net_premium"], "spot_at_open": plan["spot"],
                       "legs": [{**leg, "qty": q, "entry_price": leg["price"]} for leg in priced]}
            if pos is not None:
                mk = O.mark_position(conn, pos, d, chain=ch)
                curve.append({"date": str(d), "symbol": sym, "unrealized": mk["unrealized"]})
        if pos is not None:
            trades.append({"symbol": sym, "template": pos["template"], "opened": pos["opened"], "closed": None,
                           "expiry": pos["expiry"], "lots": pos["lots"], "reason": "OPEN at the end",
                           "pnl": None, "net_premium": pos["net_premium"]})
    done = [t for t in trades if t["pnl"] is not None]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for t in sorted(done, key=lambda x: x["closed"]):
        cum += t["pnl"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    wins = [t for t in done if t["pnl"] > 0]
    return {"strategy_id": strategy_id, "version": v["version"], "start": str(d0), "end": str(d1),
            "trades": trades, "n_closed": len(done), "wins": len(wins),
            "win_rate": round(len(wins) / len(done), 4) if done else None,
            "total_pnl": round(sum(t["pnl"] for t in done), 2), "max_drawdown": round(mdd, 2),
            "skipped": skipped, "marks": curve[-250:],
            "limitations": ["W4 risk caps not applied (lots as defined)",
                            "share-covered templates: option legs only, shares assumed held",
                            "only the nearest stored expiries of each session exist (fo_contract_daily)",
                            "EOD closes; thin strikes may be stale"]}


def _replay_close(pos, prices, d, reason, s, spot) -> dict:
    from execution import options_paper as O
    pnl = fees = 0.0
    for leg, px in zip(pos["legs"], prices):
        pnl += O.SIGN[leg["side"]] * leg["qty"] * (px - leg["entry_price"])
        fees += O._fees(O.CLOSING[leg["side"]], leg["qty"] * px, s) if px > 0 else 0.0
    total = pnl - fees - pos["fees"]
    return {"symbol": pos["symbol"], "template": pos["template"], "opened": pos["opened"], "closed": str(d),
            "expiry": pos["expiry"], "lots": pos["lots"], "reason": reason, "spot_open": pos["spot_at_open"],
            "spot_close": spot, "net_premium": pos["net_premium"],
            "legs": [{"side": l["side"], "strike": l["strike"], "option_type": l["option_type"],
                      "entry": l["entry_price"], "exit": round(px, 2)} for l, px in zip(pos["legs"], prices)],
            "fees": round(fees + pos["fees"], 2), "pnl": round(total, 2)}

