"""
W40 (ENT-15 "strategies do not emit option intents yet"): option-overlay strategies emit multi-leg OPTION
intents; the W4 risk engine sizes / refuses them; the OMS routes each approved one to the paper options
book as one all-or-nothing order; positions are marked daily, exited by the definition's rules and settled
at expiry; a dry run shows today's legs and risk check without storing anything.

The seeded chain: NIFTY (ATIP symbol NIFTY50, an index -- no tradeable bars, as in production) at 25,000,
strikes 23,000..27,000 every 100, lot 75, IV 15%; RELIANCE at 1,400, strikes 1,200..1,600 every 20, lot 500,
IV 25%. Three expiries: a weekly W, the first monthly M1 at least 30 days away (W = M1 - 7 days, the same
month) and the next monthly M2. Prices are Black-Scholes, so every |delta| is known.
"""

import calendar
import json
import math
from datetime import date, datetime, timedelta

import pytest

W4_OFF = {"max_sector_exposure_pct": None, "daily_loss_limit_pct": None, "portfolio_drawdown_limit_pct": None,
          "strategy_drawdown_limit_pct": None}
CASH = 1_000_000.0
CTX = {}                          # the env fixture's config writer and options-settings state


def _last_session(d=None):
    from utils.trading_calendar import is_trading_day
    d = d or date.today()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _last_tuesday(y, m):
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


AS_OF = _last_session()
PREV = _last_session(AS_OF - timedelta(days=1))      # positions open on PREV; their exits are decided on AS_OF


def _monthlies(n=2, min_days=30):
    out, y, m = [], AS_OF.year, AS_OF.month
    while len(out) < n:
        e = _last_tuesday(y, m)
        if (e - AS_OF).days >= min_days or out:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


M1, M2 = _monthlies()
W = M1 - timedelta(days=7)
UNDERLYINGS = {"NIFTY": (25000.0, 23000, 27000, 100, 75, 0.15), "RELIANCE": (1400.0, 1200, 1600, 20, 500, 0.25)}


def _bs(spot, k, exp, ot, iv, on=AS_OF):
    from quant.derivatives import bs_price
    return bs_price(spot, k, (exp - on).days / 365.0, 0.065, iv, "call" if ot == "CE" else "put")


def _seed_chain(conn, u, on=AS_OF, spot=None, iv_shift=0.0, skip=()):
    s0, lo, hi, step, lot, iv = UNDERLYINGS[u]
    spot = spot or s0
    conn.execute("DELETE FROM fo_contract_daily WHERE symbol=? AND date=?", (u, str(on)))
    for exp in (W, M1, M2):
        for k in range(lo, hi + 1, step):
            for ot in ("CE", "PE"):
                if (exp, k, ot) in skip:
                    continue
                px = round(max(0.05, _bs(spot, k, exp, ot, iv + iv_shift, on)), 2)
                conn.execute("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,"
                             "settle,oi,volume,underlying,lot_size,iv) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (str(on), u, "IDO" if u == "NIFTY" else "STO", str(exp), float(k), ot, px, px, 1000.0,
                              100.0, spot, lot, round((iv + iv_shift) * 100, 3)))
    conn.execute("INSERT OR REPLACE INTO fo_underlying_daily (date,symbol,kind,underlying_price,lot_size) VALUES "
                 "(?,?,?,?,?)", (str(on), u, "INDEX" if u == "NIFTY" else "STOCK", spot, lot))
    conn.commit()


def _bar(conn, sym, close, on=AS_OF, source="bhavcopy"):
    conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                 "(?,?,?,?,?,?,?,?)", (sym, str(on), close, close * 1.01, close * 0.99, close, 1e6, source))
    conn.commit()


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch):
    """A database with the paper tables, 10 lakh of paper cash, the options book ON (it ships off), the
    two seeded chains and their underlyings' bars; W4 limits needing a P&L history switched off."""
    from db.schema import get_connection, init_db
    from execution import config as XC
    from execution import options_paper as O
    from orders import risk
    from orders.paper import ensure_tables
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(risk, "CONFIG_PATH", cfg)
    monkeypatch.setattr(XC, "CONFIG_PATH", cfg)
    state = {"options": {**O.DEFAULTS, "enabled": True}}
    monkeypatch.setattr(O, "settings", lambda: dict(state["options"]))

    def write(**execution):
        cfg.write_text(json.dumps({"execution": {"max_market_data_age_sessions": None, **execution},
                                   "w4_risk_limits": W4_OFF}), encoding="utf-8")
    write()
    init_db()
    conn = get_connection()
    ensure_tables(conn)
    O.ensure_tables(conn)
    conn.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1, ?, ?)",
                 (CASH, str(datetime.now())))
    conn.commit()
    for u in UNDERLYINGS:
        _seed_chain(conn, u, on=PREV)
        _seed_chain(conn, u)
    _bar(conn, "NIFTY50", 25000.0, source="nse_index")
    _bar(conn, "RELIANCE", 1400.0)
    CTX.update(config=write, state=state)
    yield conn
    conn.close()


def _defn(**kw):
    d = {"strategy_id": "nifty_condor", "name": "NIFTY monthly condor", "version": "1.0.0",
         "kind": "option_overlay", "underlyings": {"source": "symbols", "symbols": ["NIFTY50"]},
         "template": "iron_condor", "strikes": {"method": "delta", "near": 0.20, "wing_strikes": 2},
         "expiry": {"rule": "monthly", "min_days": 20}, "lots": 1, "option_risk": {"max_loss_pct": 5.0},
         "exits": {"profit_take_pct": 50, "stop_loss_pct": 100, "days_before_expiry": 2}}
    d.update(kw)
    return d


def _register(conn, d, status="PAPER"):
    from strategy_engine import lifecycle as L, registry as REG
    REG.create_strategy(conn, d)
    if status != "DRAFT":
        L.transition(conn, d["strategy_id"], "DISABLED", "test")
        L.transition(conn, d["strategy_id"], status, "test")
    return d["strategy_id"]


def _decide(conn, sid, as_of=AS_OF):
    from strategy_engine.engine import generate_decisions
    return generate_decisions(sid, as_of=str(as_of), book="PAPER", conn=conn)


def _option_intent(conn, sid, action="OPTION_OPEN"):
    r = conn.execute("SELECT intent_id FROM strategy_position_intent WHERE strategy_id=? AND action=? ORDER BY "
                     "created_at DESC LIMIT 1", (sid, action)).fetchone()
    return r[0] if r else None


def _checks(rd):
    rc = rd.risk_checks if hasattr(rd, "risk_checks") else rd["risk_checks"]
    return {c["check"]: c for c in rc}


def _open_condor(conn, lots=1, **kw):
    """Register, decide (on PREV), evaluate and execute an iron condor; returns (sid, order, position).
    The fill prices every leg from the latest stored chain (AS_OF's, the same prices until a test moves it)."""
    from execution import options_paper as O
    from execution import pipeline as XP
    from execution import risk_engine as RE
    sid = _register(conn, _defn(lots=lots, **kw))
    _decide(conn, sid, PREV)
    rd = RE.evaluate(conn, _option_intent(conn, sid))
    assert rd.risk_status == "APPROVED", rd.rejection_reason
    o = XP.execute_approved(conn, rd.risk_decision_id)
    pos = O.open_positions(conn, sid)
    return sid, o, (pos[0] if pos else None)


# ── 1. definition validation ─────────────────────────────────────────────────────────────

def test_a_valid_definition_is_normalised():
    from strategy_engine.definition import KINDS, validate
    d = validate(_defn(exits={}, expiry={"rule": "nearest"}, option_risk={}))
    assert "option_overlay" in KINDS and d["category"] == "options"
    assert d["universe"] == {"type": "symbols", "symbols": ["NIFTY50"]}           # a listed overlay trades its list
    assert d["expiry"] == {"rule": "nearest", "min_days": 7} and d["lots"] == 1
    assert d["option_risk"] == {"max_loss_pct": 2.0} and d["allow_naked_short_calls"] is False
    assert "close" in d["features_used"] and "bars" in d["inputs"]
    p = validate(_defn(parameters=[{"name": "d", "type": "float", "default": 0.25, "min": 0.05, "max": 0.45}],
                       strikes={"method": "delta", "near": {"param": "d"}, "wing_strikes": 2}))
    assert p["strikes"]["near"] == {"param": "d"}
    s = validate(_defn(underlyings={"source": "rule", "entry": {"feature": "score_signal", "op": "==", "value": "BUY"}},
                       template="bull_call_spread", strikes={"method": "pct_otm", "near": 0, "far": 3}))
    assert "score_signal" in s["features_used"] and s["universe"] == {"type": "tracked_current"}


@pytest.mark.parametrize("edit,match", [
    ({"template": "call_ratio_spread"}, "template must be one of"),
    ({"strikes": {"method": "delta", "near": 0.2, "far": 0.1, "width": 2}}, "unknown keys"),
    ({"strikes": {"method": "gamma", "near": 0.2, "far": 0.1}}, "delta or pct_otm"),
    ({"strikes": {"method": "delta", "near": 0.2, "far": 0.3}}, "smaller |delta|"),
    ({"strikes": {"method": "delta", "near": 1.2, "far": 0.1}}, r"strikes.near \(\|delta\|\) must be in"),
    ({"strikes": {"method": "pct_otm", "near": 3, "far": 2}}, "further out of the money"),
    ({"strikes": {"method": "delta", "near": 0.2}}, "give strikes.far or strikes.wing_strikes"),
    ({"strikes": {"method": "delta", "near": 0.2, "far": 0.1, "wing_strikes": 2}}, "one of them"),
    ({"template": "long_call", "strikes": {"method": "delta", "near": 0.5, "far": 0.2}}, "no far leg"),
    ({"expiry": {"rule": "weekly"}}, "monthly or nearest"),
    ({"expiry": {"rule": "monthly", "min_days": -1}}, "min_days"),
    ({"lots": 0}, "lots must be in"),
    ({"lots": 1.5}, "whole number"),
    ({"exits": {"profit_take_pct": 0}}, "profit_take_pct"),
    ({"exits": {"trail_pct": 10}}, "unknown keys"),
    ({"exits": {"days_before_expiry": 99}}, "days_before_expiry"),
    ({"option_risk": {"max_loss_pct": 0}}, "max_loss_pct"),
    ({"template": "short_call", "strikes": {"method": "delta", "near": 0.2}}, "naked call"),
    ({"allow_naked_short_calls": "yes"}, "true or false"),
    ({"position": {"stop_pct": 3}}, "not used by option_overlay"),
    ({"underlyings": {"source": "scan"}}, "source one of"),
    ({"underlyings": {"source": "symbols", "symbols": []}}, "at least one symbol"),
    ({"underlyings": {"source": "rule"}}, "entry"),
    ({"underlyings": {"source": "strategy", "strategy_id": "nifty_condor", "version": "1.0.0"}}, "from itself"),
    ({"underlyings": {"source": "symbols", "symbols": ["NIFTY50"], "entry": {}}}, "unknown keys"),
    ({"stop_z": 3}, "keys not used by a option_overlay"),
])
def test_bad_definitions_are_refused(edit, match):
    from strategy_engine.definition import DefinitionError, validate
    with pytest.raises(DefinitionError, match=match):
        validate(_defn(**edit))


def test_a_naked_short_call_template_validates_only_when_allowed():
    from strategy_engine.definition import validate
    d = validate(_defn(template="short_call", strikes={"method": "delta", "near": 0.2}, allow_naked_short_calls=True))
    assert d["allow_naked_short_calls"] is True


# ── 2. expiry and strike selection on the seeded chain ──────────────────────────────────

def test_expiry_rule_monthly_nearest_and_min_days():
    from strategy_engine.option_overlay import is_monthly, select_expiry
    listed = [W, M1, M2]
    assert not is_monthly(W, listed) and is_monthly(M1, listed) and is_monthly(M2, listed)
    assert select_expiry(listed, AS_OF, "monthly", 20) == str(M1)
    assert select_expiry(listed, AS_OF, "nearest", 0) == str(W)
    assert select_expiry(listed, AS_OF, "monthly", (M1 - AS_OF).days + 1) == str(M2)
    assert select_expiry(listed, AS_OF, "monthly", (M2 - AS_OF).days + 1) is None
    # a lone weekly at the start of a month is never "monthly"
    early = date(M2.year, M2.month, 3)
    assert not is_monthly(early, [early])


def test_chain_on_reads_the_stored_session_and_maps_the_index_symbol(env):
    from data.derivatives_store import chain_on
    ch = chain_on(env, "NIFTY50", AS_OF)
    assert ch["symbol"] == "NIFTY" and ch["session"] == str(AS_OF) and ch["lot_size"] == 75 and ch["spot"] == 25000
    assert ch["expiries"] == [str(W), str(M1), str(M2)]
    q = ch["quotes"][(str(M1), 25000.0, "CE")]
    assert q["iv"] == pytest.approx(0.15) and q["price"] == pytest.approx(_bs(25000, 25000, M1, "CE", 0.15), abs=0.01)
    # an intraday snapshot that day overrides the quote with its mid
    env.execute("INSERT INTO option_chain_snapshot (ts,symbol,expiry,strike,option_type,ltp,bid,ask,iv,oi,volume,"
                "underlying) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (f"{AS_OF} 11:00:00", "NIFTY", str(M1), 25000.0, "CE",
                                                                 400.0, 390.0, 396.0, 15.0, 10.0, 5.0, 25010.0))
    env.commit()
    ch = chain_on(env, "NIFTY50", AS_OF)
    assert ch["quotes"][(str(M1), 25000.0, "CE")]["price"] == 393.0 and ch["spot"] == 25010.0


def test_strikes_by_delta_are_the_closest_listed_deltas(env):
    from data.derivatives_store import chain_on
    from strategy_engine.option_overlay import leg_delta, overlay_config, select_legs
    from strategy_engine.definition import validate
    ch = chain_on(env, "NIFTY", AS_OF)
    cfg = overlay_config(validate(_defn(strikes={"method": "delta", "near": 0.20, "far": 0.10})), {})
    legs = select_legs(ch, str(M1), 25000.0, cfg, AS_OF)
    yrs = (M1 - AS_OF).days / 365
    for leg in legs:
        target = 0.20 if leg["role"] == "near" else 0.10
        cands = [(k[1], q) for k, q in ch["quotes"].items() if k[0] == str(M1) and k[2] == leg["option_type"]]
        best = min(abs(abs(leg_delta(q, 25000.0, k, yrs, leg["option_type"])[0]) - target) for k, q in cands)
        assert abs(abs(leg["delta"]) - target) == pytest.approx(best, abs=1e-4)
    pb, ps, cs, cb = (leg["strike"] for leg in legs)
    assert pb < ps < 25000 < cs < cb                                    # wings outside the short strikes


def test_strikes_by_pct_otm_and_wing_strikes(env):
    from data.derivatives_store import chain_on
    from strategy_engine.definition import validate
    from strategy_engine.option_overlay import overlay_config, select_legs
    ch = chain_on(env, "NIFTY", AS_OF)
    cfg = overlay_config(validate(_defn(strikes={"method": "pct_otm", "near": 2, "far": 5})), {})
    assert [x["strike"] for x in select_legs(ch, str(M1), 25000.0, cfg, AS_OF)] == [23700.0, 24500.0, 25500.0,
                                                                                     26300.0]
    cfg = overlay_config(validate(_defn(strikes={"method": "pct_otm", "near": 2, "wing_strikes": 3})), {})
    assert [x["strike"] for x in select_legs(ch, str(M1), 25000.0, cfg, AS_OF)] == [24200.0, 24500.0, 25500.0,
                                                                                     25800.0]
    # a far target that falls on the near strike moves one listed strike further out
    cfg = overlay_config(validate(_defn(strikes={"method": "pct_otm", "near": 2, "far": 2.1})), {})
    assert [x["strike"] for x in select_legs(ch, str(M1), 25000.0, cfg, AS_OF)] == [24400.0, 24500.0, 25500.0,
                                                                                     25600.0]


# RELIANCE 1,400: 5% OTM is 1,470, half-way between the listed 1,460 / 1,480 -- the tie goes further out
@pytest.mark.parametrize("template,strikes,check", [
    ("covered_call", {"method": "pct_otm", "near": 3},
     lambda L: [(x["option_type"], x["side"]) for x in L] == [("CE", "SELL")] and L[0]["strike"] == 1440.0),
    ("protective_put", {"method": "delta", "near": 0.5},
     lambda L: [(x["option_type"], x["side"]) for x in L] == [("PE", "BUY")] and abs(L[0]["strike"] - 1400) <= 20),
    ("bull_call_spread", {"method": "pct_otm", "near": 0, "far": 5},
     lambda L: [(x["option_type"], x["side"], x["strike"]) for x in L] == [("CE", "BUY", 1400.0),
                                                                          ("CE", "SELL", 1480.0)]),
    ("bear_put_spread", {"method": "pct_otm", "near": 0, "far": 5},
     lambda L: [(x["option_type"], x["side"], x["strike"]) for x in L] == [("PE", "BUY", 1400.0),
                                                                          ("PE", "SELL", 1320.0)]),
    ("iron_condor", {"method": "pct_otm", "near": 5, "wing_strikes": 1},
     lambda L: [(x["option_type"], x["side"], x["strike"]) for x in L] == [
         ("PE", "BUY", 1300.0), ("PE", "SELL", 1320.0), ("CE", "SELL", 1480.0), ("CE", "BUY", 1500.0)]),
])
def test_each_template_builds_its_legs(env, template, strikes, check):
    from strategy_engine.option_overlay import plan_overlay, overlay_config
    from strategy_engine.definition import validate
    env.execute("INSERT INTO paper_position (symbol, quantity, avg_price) VALUES ('RELIANCE', 1000, 1300)")
    env.commit()
    d = validate(_defn(strategy_id="rel_overlay", underlyings={"source": "held", "symbols": ["RELIANCE"]},
                       template=template, strikes=strikes, lots=5))
    plan, info = plan_overlay(env, "RELIANCE", AS_OF, overlay_config(d, {}), {"RELIANCE": {"qty": 1000}})
    assert plan, info
    assert check(info["legs"]), info["legs"]
    assert plan["expiry"] == str(M1) and plan["lot_size"] == 500
    # a share template is capped by the held shares: 1,000 shares = 2 lots of 500
    assert plan["lots"] == (2 if template in ("covered_call", "protective_put") else 5)


def test_a_covered_call_needs_a_lot_of_free_shares(env):
    from strategy_engine.definition import validate
    from strategy_engine.option_overlay import overlay_config, plan_overlay
    d = validate(_defn(strategy_id="rel_cc", underlyings={"source": "held"}, template="covered_call",
                       strikes={"method": "delta", "near": 0.3}))
    plan, why = plan_overlay(env, "RELIANCE", AS_OF, overlay_config(d, {}), {"RELIANCE": {"qty": 300}})
    assert plan is None and "one lot is 500" in why


# ── 3. decisions -> OPTION intents ──────────────────────────────────────────────────────────

def test_the_strategy_emits_one_multi_leg_option_intent(env):
    sid = _register(env, _defn())
    r = _decide(env, sid)
    (it,) = r["intents"]
    assert (it["symbol"], it["action"], it["side"]) == ("NIFTY50", "OPTION_OPEN", "BUY")
    assert it["entry_reference"] == 25000.0 and it["authorization_status"] == "NOT_AUTHORIZED"
    (dec,) = r["decisions"]
    assert dec["decision"] == "BUY" and "OPTION_ENTRY_IRON_CONDOR" in dec["reason_codes"]
    plan = dec["features"]["option_plan"]
    assert plan["template"] == "iron_condor" and plan["expiry"] == str(M1) and plan["lots"] == 1
    assert [(x["option_type"], x["side"]) for x in plan["legs"]] == [("PE", "BUY"), ("PE", "SELL"), ("CE", "SELL"),
                                                                     ("CE", "BUY")]
    assert 0 < dec["confidence"] < 1 and dec["features"]["option_pop_pct"] == pytest.approx(dec["confidence"] * 100)
    stored = json.loads(env.execute("SELECT features_json FROM strategy_decision WHERE decision_id=?",
                                    (dec["decision_id"],)).fetchone()[0])
    assert stored["option_plan"]["legs"] == plan["legs"]


def test_a_scan_and_a_signal_strategy_select_the_underlyings(env):
    _bar(env, "TCS", 900.0)
    spread = {"template": "bull_call_spread", "strikes": {"method": "pct_otm", "near": 0, "far": 5}}
    scan = _register(env, _defn(strategy_id="rel_scan", universe={"type": "symbols", "symbols": ["RELIANCE", "TCS"]},
                                underlyings={"source": "rule", "entry": {"feature": "close", "op": ">",
                                                                         "value": 1000}}, **spread))
    r = _decide(env, scan)
    assert [(d["symbol"], d["action"]) for d in r["decisions"]] == [("RELIANCE", "OPTION_OPEN")]
    assert r["decisions"][0]["reasons"][0].startswith("scan: ")
    _register(env, {"strategy_id": "rel_signal", "name": "signal", "version": "1.0.0", "kind": "rule",
                    "universe": {"type": "symbols", "symbols": ["RELIANCE", "TCS"]},
                    "entry": {"feature": "close", "op": ">", "value": 1000}})
    sig = _register(env, _defn(strategy_id="rel_on_signal", universe={"type": "symbols", "symbols": ["RELIANCE", "TCS"]},
                               underlyings={"source": "strategy", "strategy_id": "rel_signal", "version": "1.0.0"},
                               **spread))
    r = _decide(env, sig)
    (d,) = r["decisions"]
    assert (d["symbol"], d["action"]) == ("RELIANCE", "OPTION_OPEN") and "signal rel_signal@1.0.0 BUY" in d["reasons"][0]
    assert [(x["side"], x["strike"]) for x in d["features"]["option_legs"]] == [("BUY", 1400.0), ("SELL", 1480.0)]


def test_no_chain_no_intent_and_position_limits(env):
    sid = _register(env, _defn(strategy_id="two_names", underlyings={"source": "symbols",
                                                                      "symbols": ["NIFTY50", "TCS", "RELIANCE"]},
                               position={"max_new_per_day": 1}))
    _bar(env, "TCS", 3500.0)
    r = _decide(env, sid)
    by = {d["symbol"]: d for d in r["decisions"]}
    assert by["TCS"]["action"] == "NO_ACTION" and "no option chain stored for TCS" in by["TCS"]["reasons"][0]
    assert "OPTION_UNAVAILABLE" in by["TCS"]["reason_codes"]
    opened = [d for d in r["decisions"] if d["action"] == "OPTION_OPEN"]
    limited = [d for d in r["decisions"] if "POSITION_LIMIT" in d["reason_codes"]]
    assert len(opened) == 1 and len(limited) == 1 and len(r["intents"]) == 1


# ── 4. risk engine: sizing and refusals ─────────────────────────────────────────────────────

def test_risk_engine_sizes_the_condor_in_lots_under_the_strategy_max_loss(env):
    from execution import options_paper as O
    from execution import risk_engine as RE
    sid = _register(env, _defn(lots=10))
    _decide(env, sid)
    rd = RE.evaluate(env, _option_intent(env, sid), store=False)
    c = _checks(rd)
    assert rd.risk_status == "APPROVED" and rd.action == "OPTION_OPEN"
    for name in ("instrument", "option_definition", "option_position", "option_chain", "option_expiry",
                 "option_legs", "naked_short_call", "option_risk", "max_loss_per_strategy", "lot_sizing"):
        assert name in c, name
    assert c["naked_short_call"]["status"] == "SKIP"                  # every short leg has its wing
    # the per-lot risk at the FILL prices (mid -/+ 50 bps), and the 5% of 10 lakh cap
    plan = json.loads(env.execute("SELECT features_json FROM strategy_decision WHERE strategy_id=?",
                                  (sid,)).fetchone()[0])["option_plan"]
    priced = [{**l, **O.leg_quote(env, "NIFTY", l["expiry"], l["strike"], l["option_type"], l["side"])}
              for l in plan["legs"]]
    m1 = O.structure_metrics(priced, 25000.0, 75, 1)
    assert m1["max_loss"] is not None and m1["risk_amount"] == pytest.approx(-m1["max_loss"])
    assert m1["margin"] == pytest.approx(m1["risk_amount"])           # a credit structure blocks its max loss
    expect = math.floor(CASH * 0.05 / m1["risk_amount"])         # option_risk.max_loss_pct 5 of 10 lakh
    assert 1 <= expect < 10
    assert rd.approved_quantity == expect and rd.requested_quantity == 10
    assert c["max_loss_per_strategy"]["status"] == "WARN" and rd.est_value == pytest.approx(m1["risk_amount"] * expect,
                                                                                             abs=1)


def test_over_the_max_loss_is_rejected(env):
    from execution import risk_engine as RE
    sid = _register(env, _defn(option_risk={"max_loss_rupees": 1000}))
    _decide(env, sid)
    rd = RE.evaluate(env, _option_intent(env, sid))
    assert rd.risk_status == "REJECTED"
    assert rd.rejection_reason == "no room for one option lot under max_loss_per_strategy"
    assert _checks(rd)["max_loss_per_strategy"]["status"] == "FAIL"
    st = env.execute("SELECT authorization_status FROM strategy_position_intent WHERE intent_id=?",
                     (rd.intent_id,)).fetchone()[0]
    assert st == "REJECTED"


def test_live_is_refused_by_the_risk_engine_the_oms_and_the_adapter(env, monkeypatch):
    from execution import adapters as A
    from execution import config as XC
    from execution import risk_engine as RE
    from execution.errors import LiveTradingDisabled
    sid = _register(env, _defn())
    _decide(env, sid)
    iid = _option_intent(env, sid)
    env.execute("UPDATE strategy_position_intent SET book='LIVE' WHERE intent_id=?", (iid,))
    env.commit()
    # even with the live gate open and the LIVE book executed, an OPTION intent never goes LIVE
    monkeypatch.setattr(RE, "live_gate", lambda: (True, "open (test)"))
    CTX["config"](execute_books=["PAPER", "LIVE"])
    rd = RE.evaluate(env, iid, store=False)
    assert rd.risk_status == "BLOCKED" and rd.rejection_reason == "LIVE options are not built"
    assert _checks(rd)["instrument"]["status"] == "FAIL"
    with pytest.raises(LiveTradingDisabled, match="LIVE options are not built"):
        A.get_adapter(env, XC.LIVE, instrument="OPT")
    assert isinstance(A.get_adapter(env, XC.PAPER, instrument="OPT"), A.OptionsPaperAdapter)


def test_an_approved_intent_cannot_become_a_live_order(env):
    from execution import order_manager as OM
    from execution import risk_engine as RE
    from execution.errors import InvalidIntentError
    sid = _register(env, _defn())
    _decide(env, sid)
    rd = RE.evaluate(env, _option_intent(env, sid))
    assert rd.risk_status == "APPROVED"
    CTX["config"](mode="LIVE")                                       # the owner flips the mode afterwards
    with pytest.raises(InvalidIntentError, match="LIVE options are not built"):
        OM.create_order(env, rd.risk_decision_id)
    assert env.execute("SELECT COUNT(*) FROM oms_order").fetchone()[0] == 0


def test_a_naked_short_call_is_refused_unless_the_definition_allows_it(env):
    from execution import risk_engine as RE
    env.execute("INSERT INTO paper_position (symbol, quantity, avg_price) VALUES ('RELIANCE', 1000, 1300)")
    env.commit()
    d = _defn(strategy_id="rel_cc", underlyings={"source": "held", "symbols": ["RELIANCE"]}, template="covered_call",
              strikes={"method": "delta", "near": 0.3}, lots=2)
    sid = _register(env, d)
    _decide(env, sid)
    iid = _option_intent(env, sid)
    rd = RE.evaluate(env, iid, store=False)
    c = _checks(rd)
    assert rd.risk_status == "APPROVED" and rd.approved_quantity == 2
    assert c["naked_short_call"]["status"] == "PASS" and c["covered_shares"]["status"] == "PASS"
    # the shares are sold before the order: the short call would be naked -> refused
    env.execute("UPDATE paper_position SET quantity=0 WHERE symbol='RELIANCE'")
    env.commit()
    rd = RE.evaluate(env, iid, store=False)
    assert rd.risk_status == "REJECTED" and rd.rejection_reason.startswith("naked short call")
    assert "does not allow naked short calls" in _checks(rd)["naked_short_call"]["message"]
    # a new version that explicitly allows naked calls: approved, with the SPAN-like naked margin
    # (15% of 1,400 x 500 a lot) and the loss at a 15% rally counted toward a larger max loss
    from execution import options_paper as O
    from strategy_engine import registry as REG
    REG.add_version(env, {**d, "version": "1.1.0", "allow_naked_short_calls": True,
                          "option_risk": {"max_loss_pct": 20}})
    REG.set_current_version(env, sid, "1.1.0")
    env.execute("UPDATE strategy_position_intent SET version='1.1.0' WHERE intent_id=?", (iid,))
    env.commit()
    rd = RE.evaluate(env, iid, store=False)
    c = _checks(rd)
    assert rd.risk_status == "APPROVED" and rd.approved_quantity == 2, rd.rejection_reason
    assert c["naked_short_call"]["status"] == "WARN" and "naked" in c["option_risk"]["message"]
    plan = json.loads(env.execute("SELECT features_json FROM strategy_decision WHERE strategy_id=?",
                                  (sid,)).fetchone()[0])["option_plan"]
    (leg,) = plan["legs"]
    q = O.leg_quote(env, "RELIANCE", leg["expiry"], leg["strike"], "CE", "SELL")
    m = O.structure_metrics([{**leg, "price": q["price"], "iv": q["iv"]}], 1400.0, 500, 2, uses_shares=True,
                            allow_naked=True)
    assert m["margin"] == pytest.approx(0.15 * 1400 * 1000) and m["naked_call_lots"] == 2
    stress = 2 * 500 * (max(0.0, 1400 * 1.15 - leg["strike"]) - q["price"])
    assert m["risk_amount"] == pytest.approx(stress, rel=1e-6) and rd.est_value == pytest.approx(stress, abs=1)


def test_the_options_book_off_rejects(env):
    from execution import risk_engine as RE
    sid = _register(env, _defn())
    _decide(env, sid)
    CTX["state"]["options"]["enabled"] = False
    rd = RE.evaluate(env, _option_intent(env, sid), store=False)
    assert rd.risk_status == "REJECTED" and "options.enabled" in rd.rejection_reason


# ── 5. OMS -> paper options book: fill, all-or-nothing, daily mark ─────────────────────────

def test_an_approved_intent_fills_all_legs_as_one_order(env):
    from execution import options_paper as O
    sid, o, pos = _open_condor(env, lots=2)
    order = env.execute("SELECT * FROM oms_order WHERE order_id=?", (o["order_id"],)).fetchone()
    assert (order["instrument"], order["adapter"], order["status"], order["quantity"]) == ("OPT", "paper_opt",
                                                                                           "FILLED", 2)
    plan = json.loads(order["legs_json"])
    assert plan["action"] == "OPTION_OPEN" and len(plan["legs"]) == 4 and plan["lots"] == 2
    assert env.execute("SELECT COUNT(*) FROM oms_fill WHERE order_id=?", (o["order_id"],)).fetchone()[0] == 0
    assert pos["status"] == "OPEN" and pos["lots"] == 2 and pos["lot_size"] == 75 and pos["open_order_id"] == o["order_id"]
    ch_mid = {(l["strike"], l["option_type"]): l for l in pos["legs"]}
    for leg in pos["legs"]:
        mid = round(_bs(25000, leg["strike"], M1, leg["option_type"], 0.15), 2)
        want = mid * (1.005 if leg["side"] == "BUY" else 0.995)       # the fill rule: mid -/+ 50 bps
        assert leg["entry_price"] == pytest.approx(max(0.05, round(want, 2)), abs=0.011) and leg["qty"] == 150
    assert len(ch_mid) == 4
    trades = env.execute("SELECT COUNT(*), SUM(fees) FROM paper_option_strategy_trade WHERE position_id=?",
                         (pos["position_id"],)).fetchone()
    assert trades[0] == 4 and trades[1] == pytest.approx(pos["fees"])
    flow = sum(-O.SIGN[l["side"]] * l["qty"] * l["entry_price"] for l in pos["legs"])
    assert pos["net_premium"] == pytest.approx(flow, abs=0.01) and flow > 0                 # a credit
    cash = env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
    assert cash == pytest.approx(CASH + flow - pos["fees"] - pos["margin_blocked"], abs=0.02)
    assert pos["realized_pnl"] == pytest.approx(-pos["fees"])
    # the same strategy cannot stack a second position on NIFTY
    r = O.place_multileg(env, {"order_id": "X", "strategy_id": sid, "quantity": 1}, plan)
    assert r["status"] == "REJECTED" and "already holds" in r["message"]


def test_a_leg_without_a_price_rejects_the_whole_order(env):
    from execution import options_paper as O
    from execution import risk_engine as RE
    from execution import pipeline as XP
    sid = _register(env, _defn())
    _decide(env, sid)
    rd = RE.evaluate(env, _option_intent(env, sid))
    plan = json.loads(env.execute("SELECT features_json FROM strategy_decision WHERE strategy_id=?",
                                  (sid,)).fetchone()[0])["option_plan"]
    far_call = plan["legs"][3]
    env.execute("DELETE FROM fo_contract_daily WHERE symbol='NIFTY' AND expiry=? AND strike=? AND option_type='CE'",
                (far_call["expiry"], far_call["strike"]))
    env.commit()
    o = XP.execute_approved(env, rd.risk_decision_id)
    assert o["status"] == "REJECTED"
    reason = env.execute("SELECT reason FROM oms_order WHERE order_id=?", (o["order_id"],)).fetchone()[0]
    assert "all-or-nothing" in reason and f"{far_call['strike']:g} CE" in reason
    assert O.open_positions(env, sid) == []
    assert env.execute("SELECT COUNT(*) FROM paper_option_strategy_trade").fetchone()[0] == 0
    assert env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0] == CASH


def test_daily_mark_values_the_legs_at_the_chain_mid(env):
    from execution import options_paper as O
    sid, _o, pos = _open_condor(env)
    _seed_chain(env, "NIFTY", spot=25000.0, iv_shift=0.03)          # vol up: the short condor loses
    r = O.mark_strategy_positions(AS_OF, env)
    assert r["status"] == "SUCCESS" and r["rows"] == 1
    mk = env.execute("SELECT * FROM paper_option_strategy_mark WHERE position_id=?", (pos["position_id"],)).fetchone()
    want = sum(O.SIGN[l["side"]] * l["qty"] * (round(_bs(25000, l["strike"], M1, l["option_type"], 0.18), 2)
                                                - l["entry_price"]) for l in pos["legs"])
    assert mk["unrealized"] == pytest.approx(want, abs=0.05) and mk["unrealized"] < 0 and mk["complete"] == 1
    p = O.get_position(env, pos["position_id"])
    assert p["unrealized"] == pytest.approx(want, abs=0.05) and str(p["last_mark_date"]) == str(AS_OF)


# ── 6. exits: profit-take, stop, days before expiry, expiry settlement ─────────────────────

def test_exit_rules_by_hand(env):
    from execution import options_paper as O
    pos = {"expiry": str(M1), "exits": {"profit_take_pct": 50, "stop_loss_pct": 100, "days_before_expiry": 2},
           "profit_base": 4000.0, "loss_base": 3500.0}
    assert O.exit_signal(pos, {"complete": True, "unrealized": 2000.0}, AS_OF)[0] == "PROFIT_TAKE"
    assert O.exit_signal(pos, {"complete": True, "unrealized": 1999.0}, AS_OF) is None
    assert O.exit_signal(pos, {"complete": True, "unrealized": -3500.0}, AS_OF)[0] == "STOP_LOSS"
    assert O.exit_signal(pos, {"complete": False, "unrealized": None}, AS_OF) is None      # no price, no P&L rule
    assert O.exit_signal(pos, {"complete": False}, M1 - timedelta(days=2))[0] == "EXPIRY_WINDOW"
    assert O.exit_signal(pos, {"complete": True, "unrealized": 0.0}, M1 - timedelta(days=3)) is None


def test_profit_take_closes_the_position_through_the_risk_engine_and_oms(env):
    from execution import options_paper as O
    from execution import pipeline as XP
    from execution import risk_engine as RE
    sid, _o, pos = _open_condor(env)
    _seed_chain(env, "NIFTY", spot=25000.0, iv_shift=-0.08)         # vol crush: the condor's value collapses
    r = _decide(env, sid, AS_OF)                                     # the next session's run
    (dec,) = [d for d in r["decisions"] if d["symbol"] == "NIFTY50"]
    assert dec["action"] == "OPTION_CLOSE" and "PROFIT_TAKE" in dec["reason_codes"], dec["reasons"]
    assert dec["features"]["option_plan"]["position_id"] == pos["position_id"]
    rd = RE.evaluate(env, _option_intent(env, sid, "OPTION_CLOSE"))
    assert rd.risk_status == "APPROVED" and rd.approved_quantity == 1 and rd.side == "SELL"
    cash0 = env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
    o = XP.execute_approved(env, rd.risk_decision_id)
    assert o["status"] == "FILLED"
    p = O.get_position(env, pos["position_id"])
    assert p["status"] == "CLOSED" and p["exit_reason"] == "PROFIT_TAKE" and p["margin_blocked"] == 0
    pnl = sum(O.SIGN[l["side"]] * l["qty"] * (l["exit_price"] - l["entry_price"]) for l in p["legs"])
    assert pnl > 0 and p["realized_pnl"] == pytest.approx(pnl - p["fees"], abs=0.05)
    close_flow = sum(O.SIGN[l["side"]] * l["qty"] * l["exit_price"] for l in p["legs"])
    close_fees = p["fees"] - pos["fees"]
    cash1 = env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
    assert cash1 == pytest.approx(cash0 + close_flow - close_fees + pos["margin_blocked"], abs=0.05)
    # every rupee accounted: the round trip moved cash by exactly the realised P&L
    assert cash1 - CASH == pytest.approx(p["realized_pnl"], abs=0.05)
    for leg in p["legs"]:                                            # closing fills: the other side's slippage
        mid = round(_bs(25000, leg["strike"], M1, leg["option_type"], 0.07), 2)
        want = mid * (0.995 if leg["side"] == "BUY" else 1.005)
        assert leg["exit_price"] == pytest.approx(max(0.05, round(want, 2)), abs=0.011)


def test_the_execution_cycle_runs_option_intents_end_to_end(env):
    from execution import options_paper as O
    from execution.pipeline import run_execution_cycle
    sid = _register(env, _defn(lots=2))
    _decide(env, sid)
    r = run_execution_cycle(execute=True, conn=env)
    assert r["risk"] == {"APPROVED": 1} and r["orders"][0]["status"] == "FILLED" and not r["errors"]
    (pos,) = O.open_positions(env, sid)
    assert pos["lots"] == 2 and pos["template"] == "iron_condor" and pos["expiry"] in (M1, str(M1))


def test_a_covered_call_is_closed_when_its_shares_are_sold(env):
    from execution import options_paper as O
    from execution import pipeline as XP
    from execution import risk_engine as RE
    env.execute("INSERT INTO paper_position (symbol, quantity, avg_price) VALUES ('RELIANCE', 1000, 1300)")
    env.commit()
    sid = _register(env, _defn(strategy_id="rel_cc", underlyings={"source": "held", "symbols": ["RELIANCE"]},
                               template="covered_call", strikes={"method": "delta", "near": 0.3}, lots=2, exits={}))
    _decide(env, sid, PREV)
    rd = RE.evaluate(env, _option_intent(env, sid))
    XP.execute_approved(env, rd.risk_decision_id)
    (pos,) = O.open_positions(env, sid)
    assert pos["covered_shares"] == 1000 and pos["margin_blocked"] == 0         # the shares are the collateral
    assert O.cover_available(env, "RELIANCE") == 0
    env.execute("UPDATE paper_position SET quantity=400 WHERE symbol='RELIANCE'")  # 600 shares sold elsewhere
    env.commit()
    r = _decide(env, sid, AS_OF)
    (dec,) = [d for d in r["decisions"] if d["symbol"] == "RELIANCE"]
    assert dec["action"] == "OPTION_CLOSE" and "COVER_GONE" in dec["reason_codes"]


def test_a_stop_loss_fires_on_a_big_move(env):
    from execution import options_paper as O
    sid, _o, pos = _open_condor(env, exits={"stop_loss_pct": 25})
    _seed_chain(env, "NIFTY", spot=26500.0)                           # through the short call and its wing
    mk = O.mark_position(env, O.get_position(env, pos["position_id"]), AS_OF)
    assert mk["exit_signal"] == "STOP_LOSS" and mk["unrealized"] <= -0.25 * pos["loss_base"] < 0
    r = _decide(env, sid, AS_OF)
    assert [d["action"] for d in r["decisions"] if d["symbol"] == "NIFTY50"] == ["OPTION_CLOSE"]


def test_expiry_settlement_at_intrinsic_value(env):
    from execution import options_paper as O
    sid, _o, pos = _open_condor(env)
    cash0 = env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
    ps, cs = sorted(l["strike"] for l in pos["legs"] if l["side"] == "SELL")
    settle = cs + 50.0                                                  # expires 50 points through the short call
    # before the expiry day's close is stored, nothing settles
    assert O.settle_strategy_positions(env, M1) == 0
    env.execute("INSERT INTO fo_underlying_daily (date,symbol,kind,underlying_price) VALUES (?,?,?,?)",
                (str(M1), "NIFTY", "INDEX", settle))
    env.commit()
    r = O.settle_expired(M1, env)
    assert r["strategy_positions"] == 1
    p = O.get_position(env, pos["position_id"])
    assert p["status"] == "SETTLED" and p["exit_reason"] == "EXPIRY"
    for leg in p["legs"]:
        assert leg["exit_price"] == pytest.approx(O.intrinsic(leg["option_type"], leg["strike"], settle))
    short_call = next(l for l in p["legs"] if l["option_type"] == "CE" and l["side"] == "SELL")
    assert short_call["exit_price"] == 50.0
    flow = sum(O.SIGN[l["side"]] * l["qty"] * l["exit_price"] for l in p["legs"])
    fees = p["fees"] - pos["fees"]
    cash1 = env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
    assert cash1 == pytest.approx(cash0 + flow - fees + pos["margin_blocked"], abs=0.05)
    assert cash1 - CASH == pytest.approx(p["realized_pnl"], abs=0.05)
    assert O.open_positions(env, sid) == []


# ── 7. dry run, pages ──────────────────────────────────────────────────────────────────────

def test_dry_run_shows_legs_and_risk_and_stores_nothing(env):
    from strategy_engine.option_overlay import dry_run
    sid = _register(env, _defn(lots=3), status="DRAFT")
    r = dry_run(env, sid, str(AS_OF))
    assert r["dry_run"] and not r["stored"] and not r["placed"] and r["status"] == "DRAFT"
    (d,) = r["decisions"]
    assert d["action"] == "OPTION_OPEN" and d["template"] == "iron_condor" and len(d["legs"]) == 4
    assert d["risk"]["status"] == "BLOCKED" and "DRAFT" in d["risk"]["reason"]   # the W4 gate: not trading yet
    oc = d["option_checks"]                                                       # ... the option checks alone
    assert oc["status"] == "APPROVED" and 1 <= oc["approved_lots"] <= 3
    assert {c["check"] for c in oc["checks"]} >= {"option_legs", "max_loss_per_strategy", "lot_sizing"}
    for t in ("strategy_decision", "strategy_position_intent", "risk_decision", "oms_order",
              "paper_option_strategy_position"):
        assert env.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert env.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0] == CASH


def test_strategy_pages_list_option_positions_and_the_dry_run(env, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from strategy_engine.performance import performance
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    sid, _o, pos = _open_condor(env)
    perf = {p["strategy_id"]: p for p in performance(env)}
    ob = perf[sid]["books"]["OPTIONS"]
    assert ob["open_positions"] == 1 and ob["fills"] == 4 and ob["realized"] == pytest.approx(-pos["fees"])
    assert "PAPER" not in perf[sid]["books"]                          # no cash fills: the legs are not oms_fill rows
    client = TestClient(server.app)
    r = client.get(f"/api/strategies/{sid}/options")
    assert r.status_code == 200
    body = r.json()
    assert body["book"]["open_positions"] == 1 and body["positions"][0]["position_id"] == pos["position_id"]
    assert len(body["positions"][0]["legs"]) == 4
    r = client.get(f"/api/strategies/{sid}/options/dry-run", params={"as_of": str(AS_OF)})
    assert r.status_code == 200 and r.json()["placed"] is False
    (d,) = r.json()["decisions"]
    assert d["action"] == "HOLD" and d["position_id"] == pos["position_id"]
    assert client.get("/api/strategies/nosuch/options").status_code == 404
    page = client.get("/strategies").text
    assert "optDry" in page and "Derivatives (paper)" in page


def test_a_w2_backtest_of_an_option_overlay_is_refused_with_the_replay_named(env):
    """W2 simulates cash (and futures) positions, not option legs: it refuses the strategy up front rather
    than reporting a run with no trades; replay() is the historical check."""
    from backtest import service as bts
    sid = _register(env, _defn())
    with pytest.raises(ValueError, match="option_overlay.replay"):
        bts.create({"strategy_id": sid, "strategy_version": "1.0.0", "start": str(AS_OF - timedelta(days=60)),
                    "end": str(AS_OF)})


def test_replay_on_stored_daily_option_prices(env):
    from strategy_engine.option_overlay import replay
    sid = _register(env, _defn(exits={"profit_take_pct": 30}))
    day2 = AS_OF + timedelta(days=1)
    from utils.trading_calendar import is_trading_day
    while not is_trading_day(day2):
        day2 += timedelta(days=1)
    _seed_chain(env, "NIFTY", on=day2, spot=25000.0, iv_shift=-0.08)
    r = replay(env, sid, str(AS_OF), str(day2))
    assert r["n_closed"] == 1 and r["trades"][0]["reason"] == "PROFIT_TAKE"
    t = r["trades"][0]
    assert t["opened"] == str(AS_OF) and t["closed"] == str(day2) and t["pnl"] > 0 and r["win_rate"] == 1.0
    assert env.execute("SELECT COUNT(*) FROM paper_option_strategy_position").fetchone()[0] == 0
