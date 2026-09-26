"""
Explainable AI investment advisor (W16, ATIP-AIA-001).

The advisor is an EVIDENCE ENGINE first. Every answer is built deterministically
from ATIP's own stored data and engines (W11-W15.5, the scores, the regime); each
statement is a CLAIM that carries its EVIDENCE (source table / engine, field, value,
as-of date). There is no black box: the same data gives the same answer, and every
number can be traced.

    ask(conn, owner, question, topic=None, narrate=None)

TOPICS (routed from the question by keywords, or given explicitly)
    overview     net worth, drift verdict, goals, latest performance, DNA flags, regime
    risk         profile band vs the portfolio's volatility / bad year, concentration,
                 holdings ATIP flags
    goals        each goal's status, success probability, gap, extra monthly needed
    allocation   target vs current, the strongest tactical signals, rebalance verdict
    performance  the latest stored performance report: model / executable / actual /
                 benchmark
    symbol       one stock: ATIP scores and factors, regime, ML / strategy views (W5
                 ml/assistant.explain_symbol), whether it is held and at what weight,
                 suitability against the single-stock cap and the risk profile
    scenario     stress test: equity -20% and -30%, gold +10%, rates shock on bonds;
                 the net-worth impact and the result vs the stated maximum loss
    market       regime and the tactical signals

RECOMMENDATIONS pass a SUITABILITY check against the Investor DNA before they are
shown: an action that would raise risk for a CONSERVATIVE profile, or any action
without an Investor DNA, is replaced by "complete / revisit your Investor DNA".
Recommendations are advisory text; nothing here can create an order.

OPTIONAL NARRATION (config wealth.advisor_llm_enabled, default false): Claude
(Anthropic SDK, already an ATIP dependency; key from the environment, never stored)
rewrites the evidence pack as a short plain-language answer. It is instructed to use
only the facts in the pack and to invent no numbers; its text is shown ALONGSIDE the
deterministic answer and evidence, never instead of them. Any error, refusal or
missing key falls back silently to the deterministic answer (narration.status says
why).

Every exchange is logged in wealth_advice_log (question, topic, full response,
narration model) for audit.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from wealth import common as C
from wealth.config import settings

log = logging.getLogger("atip.wealth.advisor")
METHODOLOGY_VERSION = "AIA-1.0"
TOPICS = ("overview", "risk", "goals", "allocation", "performance", "symbol", "scenario", "market")
KEYWORDS = [
    ("scenario", ("crash", "fall", "falls", "drop", "stress", "scenario", "what if", "correction", "bear market")),
    ("goals", ("goal", "retire", "retirement", "education", "on track", "house", "sip", "corpus")),
    ("allocation", ("allocation", "rebalanc", "drift", "overweight", "underweight", "asset mix", "gold", "bonds")),
    ("performance", ("performance", "return", "returns", "benchmark", "nifty", "xirr", "alpha", "beat")),
    ("risk", ("risk", "volatil", "safe", "concentrat", "loss", "drawdown")),
    ("market", ("market", "regime", "vix", "sentiment")),
    ("overview", ("overview", "how am i", "summary", "doing", "status", "health")),
]
GROWTH_UP = {"EQUITY", "INTL_EQUITY", "SILVER"}


def ev(source, field, value, as_of=None):
    return {"source": source, "field": field, "value": value, "as_of": str(as_of)[:10] if as_of else None}


def claim(text, *evidence):
    return {"text": text, "evidence": list(evidence)}


def _route(conn, question: str) -> tuple[str, str | None]:
    q = (question or "").lower()
    # an explicit symbol: an upper-case token that ATIP scores
    for tok in re.findall(r"\b[A-Z][A-Z0-9&\-]{1,19}\b", question or ""):
        if tok in ("ATIP", "SIP", "NIFTY", "ETF", "VIX", "XIRR", "CAGR", "DNA", "FII", "INR", "USD"):
            continue
        if conn.execute("SELECT 1 FROM ai_scores WHERE symbol=? LIMIT 1", (tok,)).fetchone():
            return "symbol", tok
    for topic, words in KEYWORDS:
        if any(w in q for w in words):
            return topic, None
    return "overview", None


# ── Topic builders: each returns (claims, recommendations, caveats) ─────────
def _dna(conn, owner):
    from wealth import dna
    return dna.summary(conn, owner)


def _t_overview(conn, owner):
    from wealth import goals as G
    from wealth import holdings as H
    from wealth import rebalance as RB
    cl, rec, cav = [], [], []
    d = _dna(conn, owner)
    s = H.summary(conn, owner)
    t = s["totals"]
    cl.append(claim(f"Net worth is Rs {t['net_worth']:,.0f} (assets Rs {t['gross_assets']:,.0f}, liabilities "
                    f"Rs {t['liabilities']:,.0f}) across {t['positions']} positions.",
                    ev("wealth.holdings.summary", "net_worth", t["net_worth"], s["as_of"])))
    if d["status"] == "MISSING":
        cl.append(claim("There is no Investor DNA yet, so no risk profile, allocation target or suitability check.",
                        ev("investor_profile", "status", "MISSING")))
    else:
        cl.append(claim(f"Your risk profile is {d['band'].replace('_', ' ').lower()} (score {d['risk_score']:.0f}/100)"
                        f"{' and it is STALE' if d['status'] == 'STALE' else ''}.",
                        ev("investor_profile_version", "band", d["band"]),
                        ev("investor_profile_version", "risk_score", d["risk_score"])))
        if d["flags"]:
            cl.append(claim("Profile flags: " + ", ".join(d["flags"]) + ".",
                            ev("investor_profile_version", "flags", d["flags"])))
        try:
            chk = RB.check_public(conn, owner)
            cl.append(claim(f"Rebalance check: {chk['verdict'].replace('_', ' ').lower()}"
                            + (f" ({'; '.join(x['trigger'] + ': ' + x['detail'] for x in chk['triggers'][:3])})"
                               if chk["triggers"] else "") + ".",
                            ev("wealth.rebalance.check", "verdict", chk["verdict"], chk["as_of"])))
            if chk["verdict"] == "REBALANCE":
                rec.append({"action": "Review a rebalance plan (Rebalance tab, 'to band edges' mode for least turnover).",
                            "rationale": "Holdings are outside their allocation bands.", "raises_risk": False,
                            "evidence": [ev("wealth.rebalance.check", "triggers", [x["trigger"] for x in chk["triggers"]])]})
        except ValueError:
            pass
    ov = G.overview(conn, owner)
    if ov["goals"]:
        sc = ov["status_counts"]
        cl.append(claim(f"{ov['goals']} active goal(s): " + ", ".join(f"{v} {k.replace('_', ' ').lower()}"
                                                                      for k, v in sc.items()) + ".",
                        ev("wealth.goals.overview", "status_counts", sc)))
        if ov["additional_monthly"] > 0:
            rec.append({"action": f"Consider adding about Rs {ov['additional_monthly']:,.0f} a month across your goals, "
                                  f"or extending dates / trimming targets.",
                        "rationale": "The current plan does not reach every goal at the assumed return.",
                        "raises_risk": False, "evidence": [ev("wealth.goals.overview", "additional_monthly",
                                                              ov["additional_monthly"])]})
    else:
        cav.append("No goals yet: add them on the Goals tab so the plan has targets.")
    rep = _latest_perf(conn, owner)
    if rep:
        cl += _perf_claims(rep)[:2]
    reg = _regime(conn)
    if reg:
        cl.append(claim(f"Market regime is {reg['regime']} (market health {reg['mh_score']:.0f}).",
                        ev("market_health", "regime", reg["regime"], reg["date"]),
                        ev("market_health", "mh_score", reg["mh_score"], reg["date"])))
    if s["data_quality"]:
        cav.append(f"{len(s['data_quality'])} holding(s) have stale or missing prices; values may be off.")
    return cl, rec, cav


def _t_risk(conn, owner):
    from wealth import holdings as H
    from wealth import rebalance as RB
    cl, rec, cav = [], [], []
    d = _dna(conn, owner)
    s = H.summary(conn, owner)
    if d["status"] != "MISSING":
        cl.append(claim(f"Capacity {d['risk_capacity']:.0f}, tolerance {d['risk_tolerance']:.0f}, requirement "
                        f"{d['risk_requirement']:.0f}: profile {d['band'].replace('_', ' ').lower()}.",
                        ev("investor_profile_version", "risk_capacity", d["risk_capacity"]),
                        ev("investor_profile_version", "risk_tolerance", d["risk_tolerance"]),
                        ev("investor_profile_version", "risk_requirement", d["risk_requirement"])))
        try:
            chk = RB.check_public(conn, owner)
            cs, ts = chk["stats"]["current"], chk["stats"]["target"]
            cl.append(claim(f"On ATIP's long-term assumptions your current mix has volatility {cs['volatility_pct']}% "
                            f"(target {ts['volatility_pct']}%) and a 1-in-20 bad year of {cs['bad_year_pct']}%.",
                            ev("wealth.allocation.portfolio_stats", "current", cs),
                            ev("wealth.allocation.portfolio_stats", "target", ts)))
            from wealth import dna
            p = dna.current(conn, owner)
            prof_loss = float(p["answers"]["max_annual_loss"]) if p else None
            if prof_loss and cs["bad_year_pct"] < -prof_loss:
                rec.append({"action": "Reduce growth assets towards your target allocation (Rebalance tab).",
                            "rationale": f"A bad year ({cs['bad_year_pct']}%) exceeds the {prof_loss:g}% loss you said "
                                         f"you could live with.", "raises_risk": False,
                            "evidence": [ev("investor_profile_version", "max_annual_loss", prof_loss)]})
        except ValueError:
            pass
    c = s["concentration"]
    if c["largest_pct"] is not None:
        cl.append(claim(f"Largest holding is {c['largest_pct']}% of assets; top five {c['top5_pct']}%; "
                        f"{c['effective_holdings']} effective holdings.",
                        ev("wealth.holdings.summary", "concentration", {k: c[k] for k in
                                                                        ("largest_pct", "top5_pct", "hhi")})))
    for b in c["single_stock_breaches"]:
        rec.append({"action": f"Trim {b['symbol']} towards the {c['single_stock_cap_pct']}% single-stock cap.",
                    "rationale": f"It is {b['weight_pct']}% of net worth.", "raises_risk": False,
                    "evidence": [ev("wealth.holdings.summary", "single_stock_breaches", b)]})
    for h in s["holdings_at_risk"]:
        cl.append(claim(f"ATIP flags {h['symbol']}: CRI {h['cri']}, signal {h['signal']} (value Rs {h['value']:,.0f}).",
                        ev("ai_scores", "cri", h["cri"]), ev("ai_scores", "signal", h["signal"])))
    return cl, rec, cav


def _t_goals(conn, owner):
    from wealth import goals as G
    cl, rec, cav = [], [], []
    goals = G.list_goals(conn, owner)
    if not goals:
        return [], [{"action": "Add your goals (Goals tab).", "rationale": "No goals are defined.",
                     "raises_risk": False, "evidence": []}], []
    for g in goals:
        e = g.get("evaluation") or {}
        if "error" in e or not e:
            cav.append(f"{g['name']}: {e.get('error', 'not evaluated')}")
            continue
        mc = e.get("monte_carlo") or {}
        cl.append(claim(f"{g['name']} ({g['target_date']}): {e['status'].replace('_', ' ').lower()}, "
                        f"{mc.get('success_probability')}% of simulated paths reach Rs {e['future_target']:,.0f}; "
                        f"projected Rs {e['projected']:,.0f} (gap Rs {e['gap']:,.0f}).",
                        ev("wealth.goals.evaluate", "success_probability", mc.get("success_probability"), e["as_of"]),
                        ev("wealth.goals.evaluate", "future_target", e["future_target"]),
                        ev("wealth.goals.evaluate", "assumptions", e["assumptions"])))
        if e["status"] in ("OFF_TRACK", "AT_RISK") and e.get("additional_monthly"):
            rec.append({"action": f"{g['name']}: add about Rs {e['additional_monthly']:,.0f}/month, or Rs "
                                  f"{e['lumpsum_today']:,.0f} today, or move the date / target.",
                        "rationale": f"Required return {e['required_return_pct']}% vs assumed "
                                     f"{e['assumptions']['expected_return_pct']}%.", "raises_risk": False,
                        "evidence": [ev("wealth.goals.evaluate", "additional_monthly", e["additional_monthly"])]})
            if e["required_return_pct"] and e["required_return_pct"] > e["assumptions"]["expected_return_pct"] + 3:
                cav.append(f"{g['name']}: meeting it by taking more risk instead would need about "
                           f"{e['required_return_pct']}% a year, above what your profile supports.")
    return cl, rec, cav


def _t_allocation(conn, owner):
    from wealth import allocation as AL
    from wealth import rebalance as RB
    cl, rec, cav = [], [], []
    tgt = AL.current_target(conn, owner)
    if not tgt:
        return [], [], ["No Investor DNA, so no target allocation."]
    t = tgt["target"]
    cl.append(claim("Target allocation: " + ", ".join(f"{k.replace('_', ' ').lower()} {v:.0f}%" for k, v in t.items()
                                                   if v) + f" ({tgt['source']}).",
                    ev("wealth.allocation", "target", t, tgt.get("as_of"))))
    sig = sorted([s for s in tgt["signals"] if s["score"] is not None], key=lambda s: -abs(s["score"]))[:3]
    for s in sig:
        cl.append(claim(f"Signal {s['signal']}: {s['score']:+.2f} ({s['note']}).",
                        ev(s["source"], s["signal"], s["value"], s["as_of"])))
    try:
        chk = RB.check_public(conn, owner)
        off = [d for d in chk["drift"] if d["out_of_band"]]
        for d in off:
            cl.append(claim(f"{d['class'].replace('_', ' ').title()} is {d['current_pct']}% against a {d['target_pct']}% "
                            f"target ({d['drift_pp']:+.1f} pts).", ev("wealth.rebalance.check", d["class"], d)))
        if off:
            rec.append({"action": "Build a 'to band edges' rebalance plan; or direct new money to the underweight "
                                  "classes ('invest new cash only').",
                        "rationale": "Classes are outside their bands.", "raises_risk": any(
                            d["class"] in GROWTH_UP and d["drift_pp"] < 0 for d in off),
                        "evidence": [ev("wealth.rebalance.check", "verdict", chk["verdict"])]})
    except ValueError:
        pass
    return cl, rec, cav


def _latest_perf(conn, owner):
    r = conn.execute("SELECT report_id FROM perf_report_run WHERE tenant_id=? AND owner_id=? ORDER BY created_at DESC "
                     "LIMIT 1", (owner["tenant_id"], owner["owner_id"])).fetchone() if C.table_exists(
        conn, "perf_report_run") else None
    if not r:
        return None
    from wealth.perf import report as PR
    return PR.get(conn, owner, r[0])


def _perf_claims(rep):
    out = []
    by = {c["return"]: c for c in rep["comparison"]}
    parts = [f"{k.lower()} {by[k]['total_return_pct']}%" for k in ("MODEL", "EXECUTABLE", "ACTUAL", "BENCHMARK")
             if by.get(k) and by[k]["total_return_pct"] is not None]
    out.append(claim(f"{rep['portfolio']} {rep['period']['start']} to {rep['period']['end']}: " + ", ".join(parts) + ".",
                     ev(f"perf_report_run {rep.get('report_id')}", "comparison",
                        {k: by[k]["total_return_pct"] for k in by})))
    g = rep["gaps"]
    if g.get("model_minus_executable_pp") is not None:
        out.append(claim(f"Trading the signals cost {g['model_minus_executable_pp']} pts versus the model; "
                         f"your own timing / selection accounts for {g.get('executable_minus_actual_pp')} pts.",
                         ev(f"perf_report_run {rep.get('report_id')}", "gaps", g)))
    return out


def _t_performance(conn, owner):
    rep = _latest_perf(conn, owner)
    if not rep:
        return [], [{"action": "Build a performance report (Performance tab).", "rationale": "None is stored yet.",
                     "raises_risk": False, "evidence": []}], []
    cav = ["Figures are from the stored report of " + str(rep.get("created_at"))[:16] + "; build a new one for today."]
    if rep["period"]["sessions"] < 60:
        cav.append("Under three months of history: returns this short say little about skill.")
    return _perf_claims(rep), [], cav


def _t_symbol(conn, owner, sym):
    from ml.assistant import explain_symbol
    from wealth import holdings as H
    cl, rec, cav = [], [], []
    f = explain_symbol(conn, sym)
    s = f.get("atip_scores") or {}
    if s:
        cl.append(claim(f"{sym} on {f['as_of']}: ATIP score {s.get('atip_score')}, signal {s.get('signal')}, CRI "
                        f"{s.get('cri')}, VPI {s.get('vpi')}; top factors "
                        + ", ".join(str(s.get(k)) for k in ("top_factor_1", "top_factor_2", "top_factor_3") if s.get(k))
                        + ".", ev("ai_scores", "atip_score", s.get("atip_score"), f["as_of"]),
                        ev("ai_scores", "signal", s.get("signal"), f["as_of"]), ev("ai_scores", "cri", s.get("cri"))))
    for p in f.get("ml_predictions") or []:
        cl.append(claim(f"Model {p['model_id']} ({p['version_status']}): {p['prediction']} (confidence "
                        f"{p['confidence']}).", ev("ml_prediction", "prediction", p["prediction"], f["as_of"])))
    for dcs in f.get("strategy_decisions") or []:
        cl.append(claim(f"Strategy {dcs['strategy_id']}: {dcs['decision']} ({', '.join(dcs['reason_codes'])}).",
                        ev("strategy_decision", "decision", dcs["decision"], f["as_of"])))
    summ = H.summary(conn, owner)
    held = [p for p in summ["positions"] if p["symbol"] == sym and p["include_in_net_worth"]]
    gross = summ["totals"]["gross_assets"]
    cap = summ["concentration"]["single_stock_cap_pct"]
    if held:
        v = sum(p["value"] for p in held)
        w = v / gross * 100 if gross else 0
        cl.append(claim(f"You hold {sym} worth Rs {v:,.0f} ({w:.1f}% of assets).",
                        ev("wealth.holdings", "value", round(v, 2)), ev("wealth.holdings", "weight_pct", round(w, 2))))
        if (s.get("cri") or 0) >= 75 or s.get("signal") in ("SELL", "EXIT", "AVOID"):
            rec.append({"action": f"Review your {sym} position.", "rationale": "ATIP's crash-risk / signal flags it.",
                        "raises_risk": False, "evidence": [ev("ai_scores", "cri", s.get("cri"))]})
    elif not gross:
        cl.append(claim(f"You do not hold {sym}, and no holdings are recorded yet, so ATIP cannot size a position "
                        f"for you (add holdings on the Wealth tab).", ev("wealth.holdings.summary", "gross_assets", 0)))
    else:
        room = cap / 100 * gross
        cl.append(claim(f"You do not hold {sym}. Under your {cap:g}% single-stock cap a position could be up to about "
                        f"Rs {room:,.0f}.", ev("wealth.config", "single_stock_cap_pct", cap),
                        ev("wealth.holdings.summary", "gross_assets", gross)))
        if s.get("signal") == "BUY":
            rec.append({"action": f"If you act on the {sym} BUY signal, size it within the cap and your equity target, "
                                  f"with a stop.", "rationale": "ATIP signal BUY.", "raises_risk": True,
                        "evidence": [ev("ai_scores", "signal", "BUY", f["as_of"])]})
    cav.append("ATIP scores are model outputs, not validated forecasts; see the Performance tab for how signals "
               "have actually done.")
    return cl, rec, cav


SHOCKS = {"equity -20%": {"EQUITY": -20, "INTL_EQUITY": -15, "SILVER": -10, "GOLD": 5, "BONDS": 1},
          "equity -30%": {"EQUITY": -30, "INTL_EQUITY": -25, "SILVER": -20, "GOLD": 8, "BONDS": 2},
          "gold +10%": {"GOLD": 10, "SILVER": 8},
          "rates +1%": {"BONDS": -5, "EQUITY": -5, "REAL_ESTATE": -3}}


def _t_scenario(conn, owner):
    from wealth import dna
    from wealth import holdings as H
    cl, rec, cav = [], [], []
    s = H.summary(conn, owner)
    by = {k: v["value"] for k, v in s["by_asset_class"].items()}
    nw = s["totals"]["net_worth"]
    p = dna.current(conn, owner)
    lim = float(p["answers"]["max_annual_loss"]) if p else None
    worst = None
    for name, sh in SHOCKS.items():
        delta = sum(by.get(k, 0.0) * pct / 100 for k, pct in sh.items())
        pct = delta / nw * 100 if nw else 0.0
        worst = pct if worst is None else min(worst, pct)
        cl.append(claim(f"Scenario '{name}': net worth changes by Rs {delta:,.0f} ({pct:+.1f}%).",
                        ev("wealth.advisor.SHOCKS", name, sh), ev("wealth.holdings.summary", "by_asset_class",
                                                                   {k: round(v, 0) for k, v in by.items()})))
    if lim and worst is not None and worst < -lim:
        rec.append({"action": "Hold more bonds / cash relative to equity, or confirm you can live with this fall.",
                    "rationale": f"The worst scenario ({worst:.1f}%) is beyond the {lim:g}% loss you said you "
                                 f"could accept.", "raises_risk": False,
                    "evidence": [ev("investor_profile_version", "max_annual_loss", lim)]})
    cav.append("Scenario shocks are illustrative assumptions, not forecasts; liabilities are held constant.")
    return cl, rec, cav


def _regime(conn):
    r = conn.execute("SELECT date, regime, mh_score, vix_level FROM market_health ORDER BY date DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _t_market(conn, owner):
    from wealth import allocation as AL
    cl = []
    reg = _regime(conn)
    if reg:
        cl.append(claim(f"Regime {reg['regime']}, market health {reg['mh_score']:.0f}, India VIX {reg['vix_level']}.",
                        ev("market_health", "regime", reg["regime"], reg["date"])))
    for s in AL.signals(conn):
        if s["score"] is not None:
            cl.append(claim(f"{s['signal']}: {s['score']:+.2f} — {s['note']}.", ev(s["source"], s["signal"], s["value"],
                                                                                   s["as_of"])))
    return cl, [], ["Signals describe current conditions; they are not predictions of returns."]


BUILDERS = {"overview": _t_overview, "risk": _t_risk, "goals": _t_goals, "allocation": _t_allocation,
            "performance": _t_performance, "scenario": _t_scenario, "market": _t_market}


def _suitability(conn, owner, recs):
    d = _dna(conn, owner)
    out = []
    for r in recs:
        r = dict(r)
        if d["status"] == "MISSING" and r.get("raises_risk"):
            r = {"action": "Complete your Investor DNA before acting on this.", "rationale": "No risk profile exists, "
                 "so ATIP cannot check whether '" + r["action"] + "' suits you.", "raises_risk": False,
                 "evidence": [ev("investor_profile", "status", "MISSING")], "suitability": "BLOCKED_NO_PROFILE"}
        elif r.get("raises_risk") and d.get("band") == "CONSERVATIVE":
            r["suitability"] = "CAUTION_CONSERVATIVE_PROFILE"
            r["rationale"] += " Note: this adds risk and your profile is conservative."
        elif d.get("status") == "STALE":
            r["suitability"] = "PROFILE_STALE"
        else:
            r["suitability"] = "OK"
        out.append(r)
    return out


# ── Narration (optional, Claude) ───────────────────────────────────────────
NARRATION_SYSTEM = (
    "You are the narration layer of ATIP's investment advisor, speaking to an individual investor in India. "
    "You receive an evidence pack: claims with their evidence, suitability-checked recommendations, and caveats, "
    "all computed by ATIP's deterministic engines. Rewrite them as a short, clear answer to the investor's question "
    "(at most about 180 words, plain prose, no headings). Rules: use ONLY facts and numbers present in the pack; "
    "never invent, estimate or round beyond the pack; do not add recommendations that are not in the pack; keep "
    "every caveat that matters; say plainly when the pack does not answer the question; never promise returns. "
    "End with one line reminding the reader that this is informational and not SEBI-registered advice."
)


def _narrate(question, pack) -> dict:
    cfg = settings()
    if not cfg.get("advisor_llm_enabled"):
        return {"status": "OFF", "detail": "wealth.advisor_llm_enabled is false"}
    try:
        import anthropic
    except ImportError:
        return {"status": "UNAVAILABLE", "detail": "anthropic package not installed"}
    model = cfg.get("advisor_llm_model") or "claude-opus-5"
    try:
        client = anthropic.Anthropic(timeout=60.0, max_retries=2)
        resp = client.beta.messages.create(
            model=model, max_tokens=2000,
            system=[{"type": "text", "text": NARRATION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Question: {question}\n\nEvidence pack (JSON):\n{C.dumps(pack)}"}],
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default", "output_config": {"effort": "low"}},
        )
    except anthropic.AuthenticationError:
        return {"status": "UNAVAILABLE", "detail": "no valid Anthropic credentials"}
    except anthropic.RateLimitError:
        return {"status": "ERROR", "detail": "rate limited"}
    except anthropic.APIStatusError as e:
        return {"status": "ERROR", "detail": f"API error {e.status_code}"}
    except anthropic.APIConnectionError:
        return {"status": "ERROR", "detail": "network error"}
    except Exception as e:                                   # never let narration break an answer
        log.warning(f"advisor narration failed: {e}")
        return {"status": "ERROR", "detail": type(e).__name__}
    if resp.stop_reason == "refusal":
        return {"status": "REFUSED", "detail": getattr(resp.stop_details, "category", None)}
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        return {"status": "EMPTY"}
    return {"status": "OK", "model": resp.model, "text": text,
            "note": "AI narration of the evidence below; the evidence and deterministic answer are authoritative."}


# ── Entry point ────────────────────────────────────────────────────────────
def ask(conn, owner, question: str, topic: str | None = None, narrate: bool | None = None, actor="owner") -> dict:
    question = C.text(question, "question", 500, required=topic is None) or ""
    sym = None
    if topic:
        t = str(topic).lower()
        if t not in TOPICS:
            raise ValueError(f"topic must be one of {list(TOPICS)}")
        if t == "symbol":
            m = re.findall(r"\b[A-Z][A-Z0-9&\-]{1,19}\b", question)
            sym = m[0] if m else None
            if not sym:
                raise ValueError("topic symbol needs a symbol in the question (e.g. 'RELIANCE')")
    else:
        t, sym = _route(conn, question)
    if t == "symbol":
        cl, rec, cav = _t_symbol(conn, owner, sym)
    else:
        cl, rec, cav = BUILDERS[t](conn, owner)
    rec = _suitability(conn, owner, rec)
    n_ev = sum(len(c["evidence"]) for c in cl)
    confidence = "HIGH" if cl and n_ev >= len(cl) and not cav else "MEDIUM" if cl else "LOW"
    pack = {"topic": t, "symbol": sym, "claims": cl, "recommendations": rec, "caveats": cav}
    answer = [c["text"] for c in cl] + [("Suggestion: " + r["action"] + " — " + r["rationale"]) for r in rec] + \
             [("Caveat: " + c) for c in cav]
    if not cl:
        answer.insert(0, "ATIP has no data to answer this yet.")
    want = settings().get("advisor_llm_enabled") if narrate is None else narrate
    narration = _narrate(question or t, pack) if want else {"status": "OFF", "detail": "not requested"}
    res = {"methodology_version": METHODOLOGY_VERSION, "question": question, "topic": t, "symbol": sym,
           "answer": answer, **pack, "confidence": confidence, "narration": narration,
           "as_of": str(C.now()), "disclaimer": C.DISCLAIMER,
           "trading_note": "The advisor cannot place, modify or cancel orders."}
    aid = C.new_id("adv")
    conn.execute("INSERT INTO wealth_advice_log (advice_id,tenant_id,owner_id,asked_at,asked_by,question,topic,"
                 "response_json,narration_status,narration_model) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (aid, owner["tenant_id"], owner["owner_id"], C.now(), actor, question, t, C.dumps(res),
                  narration["status"], narration.get("model")))
    conn.commit()
    return {"advice_id": aid, **res}


def history(conn, owner, limit=50) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT advice_id, asked_at, question, topic, narration_status, feedback, feedback_note FROM wealth_advice_log "
        "WHERE tenant_id=? AND owner_id=? ORDER BY asked_at DESC LIMIT ?", (owner["tenant_id"], owner["owner_id"],
                                                                            int(limit)))]


def get(conn, owner, aid) -> dict:
    r = conn.execute("SELECT * FROM wealth_advice_log WHERE advice_id=? AND tenant_id=? AND owner_id=?",
                     (aid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("advice not found")
    d = C.loads(r["response_json"], {})
    d.update({"advice_id": aid, "feedback": r["feedback"], "feedback_note": r["feedback_note"]})
    return d


def feedback(conn, owner, aid, helpful, note=None) -> dict:
    if not isinstance(helpful, bool):
        raise ValueError("helpful must be true or false")
    get(conn, owner, aid)
    conn.execute("UPDATE wealth_advice_log SET feedback=?, feedback_note=? WHERE advice_id=? AND tenant_id=? AND "
                 "owner_id=?", ("HELPFUL" if helpful else "NOT_HELPFUL", C.text(note, "note", 500, required=False),
                                aid, owner["tenant_id"], owner["owner_id"]))
    conn.commit()
    return {"advice_id": aid, "feedback": "HELPFUL" if helpful else "NOT_HELPFUL"}


def suggested_questions() -> list:
    return ["How am I doing overall?", "Am I taking too much risk?", "Am I on track for my goals?",
            "Should I rebalance?", "How have ATIP's signals performed versus NIFTY?",
            "What happens to my wealth if the market falls 30%?", "What does the market look like now?",
            "Tell me about RELIANCE."]
