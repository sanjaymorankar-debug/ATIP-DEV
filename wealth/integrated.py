"""
Integrated intelligence (W17, ATIP-INT-001): the investor side and the trader side
of ATIP as one system.

INVESTOR CYCLE  investor_cycle(conn, owner)
    1 net-worth snapshot (W12)
    2 ledger import from the paper book / OMS / broker syncs (W15.5)
    3 Investor DNA: re-scored when the goals' risk requirement has moved more than
      REQ_REFRESH_POINTS from the stored one (W11 <- W13)
    4 allocation run (W14) and today's goal projections (W13)
    5 rebalance check (W15)
    6 a performance report once a week (W15.5, PAPER; LIVE too for the house owner)
    7 alerts (alert_log, and Telegram when configured), each de-duplicated per day:
      REBALANCE verdict, goal status worsened to OFF_TRACK, profile STALE, holdings
      ATIP flags, stale prices
    Each step is isolated: one failing step is reported and the rest still run.

SCHEDULE  run_scheduled(trade_date) is called from the post-market pipeline
    (pipeline/scheduler.py) and does nothing unless config wealth.enabled is true.
    It runs the cycle for every owner with an Investor DNA.

OVERVIEW  overview(conn, owner): the Investor-mode home in one call -- DNA, net worth,
    goals, allocation and drift, latest performance headline, the advisor's briefing,
    today's ATIP signals checked for suitability, and the mode.

MODE  Investor / Trader view preference (wealth_preference). It changes which page
    the investor sees first; it never changes a trading setting.

SIGNAL SUITABILITY  signal_suitability(conn, owner, symbol): the trader-side bridge.
    For a symbol with an ATIP signal: does it fit the Investor DNA, is equity under or
    over its target, is it already held and at what weight, how much room is left
    under the single-stock cap, what does the goal horizon say. Informational.

STATUS  status(conn, owner): which parts of the chain are in place and how fresh they are.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from wealth import common as C
from wealth.config import settings

log = logging.getLogger("atip.wealth.integrated")
REQ_REFRESH_POINTS = 5.0
MODES = ("INVESTOR", "TRADER")


def _alert(title, body, key, severity="warning"):
    try:
        from alerts.telegram import notify
        return notify(f"<b>{title}</b>\n{body}", category="wealth", severity=severity, key=key)
    except Exception as e:
        log.warning(f"wealth alert not recorded: {e}")
        return {"recorded": False}


def _step(out, name, fn):
    try:
        out["steps"][name] = {"status": "OK", **(fn() or {})}
    except Exception as e:                               # isolate: record and continue
        out["steps"][name] = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
        log.warning(f"investor cycle step {name} failed: {e}")


def investor_cycle(conn, owner, trade_date: date | None = None, alerts: bool = True) -> dict:
    from wealth import allocation as AL
    from wealth import dna
    from wealth import goals as G
    from wealth import holdings as H
    from wealth import rebalance as RB
    from wealth.perf import ledger as PL
    from wealth.perf import report as PR
    td = trade_date or date.today()
    out = {"owner": owner, "date": str(td), "steps": {}, "alerts": []}
    tag = f"{owner['tenant_id']}:{owner['owner_id']}"

    _step(out, "snapshot", lambda: {"net_worth": H.record_snapshot(conn, owner, td)["net_worth"]})
    _step(out, "ledger", lambda: PL.sync(conn, owner))

    def dna_step():
        cur = dna.current(conn, owner)
        if not cur:
            return {"detail": "no Investor DNA"}
        req = G.required_return_for_owner(conn, owner)
        if not req:
            return {"detail": "no essential / important goals", "profile_status": cur["status"]}
        new = dna.compute(cur["answers"], None, req)["scores"]["risk_requirement"]
        old = cur["scores"]["risk_requirement"]
        if cur["requirement_source"] != "GOALS" or abs(new - old) > REQ_REFRESH_POINTS:
            p = dna.refresh_requirement(conn, owner, actor="investor_cycle")
            return {"refreshed": True, "from": old, "to": p["scores"]["risk_requirement"], "version": p["version"]}
        return {"refreshed": False, "requirement": old, "profile_status": cur["status"]}
    _step(out, "dna", dna_step)

    def alloc_step():
        if not dna.current(conn, owner):
            return {"detail": "skipped: no Investor DNA"}
        r = AL.run(conn, owner, actor="investor_cycle")
        return {"run_id": r["run_id"], "band": r["investor"]["band"]}
    _step(out, "allocation", alloc_step)

    def goals_step():
        prev = {r[0]: r[1] for r in conn.execute(
            "SELECT goal_id, status FROM wealth_goal_projection p WHERE tenant_id=? AND owner_id=? AND as_of="
            "(SELECT MAX(as_of) FROM wealth_goal_projection q WHERE q.goal_id=p.goal_id AND q.as_of<?)",
            (owner["tenant_id"], owner["owner_id"], str(td)))}
        done, worse = 0, []
        for g in G.list_goals(conn, owner, mc=False):
            try:
                r = G.record_projection(conn, owner, g["goal_id"])
            except ValueError:
                continue
            done += 1
            if r["status"] == "OFF_TRACK" and prev.get(g["goal_id"]) not in (None, "OFF_TRACK"):
                worse.append(g["name"])
        if alerts and worse:
            out["alerts"].append(_alert("ATIP wealth: goal off track", "Now off track: " + ", ".join(worse),
                                        f"wealth:goal_off:{tag}"))
        return {"projections": done, "newly_off_track": worse}
    _step(out, "goals", goals_step)

    def rbl_step():
        if not dna.current(conn, owner):
            return {"detail": "skipped: no Investor DNA"}
        chk = RB.check_public(conn, owner)
        if alerts and chk["verdict"] == "REBALANCE":
            out["alerts"].append(_alert("ATIP wealth: rebalance suggested",
                                        "; ".join(f"{t['trigger']}: {t['detail']}" for t in chk["triggers"]),
                                        f"wealth:rebalance:{tag}"))
        return {"verdict": chk["verdict"], "triggers": [t["trigger"] for t in chk["triggers"]]}
    _step(out, "rebalance", rbl_step)

    def perf_step():
        last = conn.execute("SELECT MAX(created_at) FROM perf_report_run WHERE tenant_id=? AND owner_id=?",
                            (owner["tenant_id"], owner["owner_id"])).fetchone()[0]
        if last:
            last = last if isinstance(last, datetime) else datetime.fromisoformat(str(last)[:19])
            if C.now() - last < timedelta(days=7):
                return {"detail": f"latest report {str(last)[:10]} is under a week old"}
        made = []
        pfs = ["PAPER", "LIVE"] if C.owns_house_book(owner) else ["MANUAL"]
        for pf in pfs:
            if conn.execute("SELECT 1 FROM perf_ledger WHERE tenant_id=? AND owner_id=? AND portfolio=? LIMIT 1",
                            (owner["tenant_id"], owner["owner_id"], pf)).fetchone():
                made.append(PR.run(conn, owner, pf, td - timedelta(days=365), td, actor="investor_cycle")["report_id"])
        return {"reports": made}
    _step(out, "performance", perf_step)

    def alert_step():
        n = 0
        cur = dna.current(conn, owner)
        if alerts and cur and cur["status"] == "STALE":
            out["alerts"].append(_alert("ATIP wealth: Investor DNA out of date",
                                        f"Your profile is {cur['age_days']} days old; retake the questionnaire.",
                                        f"wealth:dna_stale:{tag}", "info"))
            n += 1
        s = H.summary(conn, owner)
        if alerts and s["holdings_at_risk"]:
            out["alerts"].append(_alert("ATIP wealth: holdings flagged",
                                        ", ".join(f"{h['symbol']} (CRI {h['cri']}, {h['signal']})"
                                                  for h in s["holdings_at_risk"]), f"wealth:at_risk:{tag}"))
            n += 1
        if alerts and s["data_quality"]:
            out["alerts"].append(_alert("ATIP wealth: stale prices",
                                        f"{len(s['data_quality'])} holding(s) have stale or missing prices.",
                                        f"wealth:stale:{tag}", "info"))
            n += 1
        return {"checks": n}
    _step(out, "alerts", alert_step)
    failed = [k for k, v in out["steps"].items() if v["status"] == "FAILED"]
    out["status"] = "PARTIAL" if failed else "SUCCESS"
    out["failed_steps"] = failed
    conn.execute("INSERT INTO wealth_cycle_run (tenant_id,owner_id,run_date,status,result_json,created_at) VALUES "
                 "(?,?,?,?,?,?)", (owner["tenant_id"], owner["owner_id"], str(td), out["status"], C.dumps(out),
                                   C.now()))
    conn.commit()
    return out


def owners_with_profiles(conn) -> list:
    return [{"tenant_id": r[0], "owner_id": r[1]} for r in conn.execute(
        "SELECT tenant_id, owner_id FROM investor_profile ORDER BY tenant_id, owner_id")]


def run_scheduled(trade_date=None) -> dict:
    """Post-market hook (pipeline/scheduler.py). SKIPPED unless config wealth.enabled."""
    if not settings()["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "wealth.enabled is false"}
    from db.schema import get_connection
    conn = get_connection()
    try:
        td = trade_date if isinstance(trade_date, date) else (date.fromisoformat(str(trade_date)[:10])
                                                              if trade_date else date.today())
        res = [investor_cycle(conn, o, td) for o in owners_with_profiles(conn)]
    finally:
        conn.close()
    bad = [r for r in res if r["status"] != "SUCCESS"]
    return {"status": "SUCCESS" if not bad else "PARTIAL", "rows": len(res),
            "owners": [{"owner": r["owner"], "status": r["status"], "failed": r["failed_steps"]} for r in res]}


def cycles(conn, owner, limit=30) -> list:
    out = []
    for r in conn.execute("SELECT run_date, status, result_json, created_at FROM wealth_cycle_run WHERE tenant_id=? AND "
                          "owner_id=? ORDER BY created_at DESC LIMIT ?", (owner["tenant_id"], owner["owner_id"],
                                                                         int(limit))):
        d = dict(r)
        res = C.loads(d.pop("result_json"), {})
        d["steps"] = {k: v.get("status") for k, v in (res.get("steps") or {}).items()}
        d["alerts"] = len(res.get("alerts") or [])
        out.append(d)
    return out


# ── Mode ───────────────────────────────────────────────────────────────────
def get_mode(conn, owner) -> dict:
    r = conn.execute("SELECT value FROM wealth_preference WHERE tenant_id=? AND owner_id=? AND `key`='mode'",
                     (owner["tenant_id"], owner["owner_id"])).fetchone()
    if r:
        return {"mode": r[0], "source": "preference"}
    from wealth import dna
    p = dna.current(conn, owner)
    if p:
        return {"mode": p["personality"]["default_view"], "source": "Investor DNA"}
    return {"mode": "INVESTOR", "source": "default"}


def set_mode(conn, owner, mode) -> dict:
    m = str(mode or "").upper()
    if m not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    conn.execute("INSERT INTO wealth_preference (tenant_id,owner_id,`key`,value,updated_at) VALUES (?,?,'mode',?,?) "
                 "ON CONFLICT(tenant_id,owner_id,`key`) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (owner["tenant_id"], owner["owner_id"], m, C.now()))
    conn.commit()
    return get_mode(conn, owner)


# ── Trader bridge ──────────────────────────────────────────────────────────
def signal_suitability(conn, owner, symbol: str, _ctx: dict | None = None) -> dict:
    from wealth import dna
    from wealth import holdings as H
    sym = C.symbol(symbol)
    ctx = _ctx or {}
    d = ctx.get("dna") or dna.summary(conn, owner)
    summ = ctx.get("summary") or H.summary(conn, owner)
    sc = conn.execute("SELECT date, atip_score, `signal`, cri, vpi, confidence FROM ai_scores WHERE symbol=? ORDER BY "
                      "date DESC LIMIT 1", (sym,)).fetchone()
    sc = dict(sc) if sc else None
    checks = []

    def chk(name, ok, detail):
        checks.append({"check": name, "result": ok, "detail": detail})
    if d["status"] == "MISSING":
        chk("investor_profile", "UNKNOWN", "No Investor DNA: suitability cannot be judged.")
    else:
        band = d["band"]
        if band in ("CONSERVATIVE", "MODERATELY_CONSERVATIVE") and sc and (sc["cri"] or 0) >= 50:
            chk("risk_profile", "CAUTION", f"{band.lower()} profile and CRI {sc['cri']:.0f}")
        else:
            chk("risk_profile", "OK", f"profile {band.lower()}")
        if d["horizon"]["bucket"] == "SHORT":
            chk("horizon", "CAUTION", "short investment horizon: single shares are volatile over under 3 years")
    gross = summ["totals"]["gross_assets"]
    held = sum(p["value"] for p in summ["positions"] if p["symbol"] == sym and p["include_in_net_worth"])
    cap = summ["concentration"]["single_stock_cap_pct"]
    if gross:
        w = held / gross * 100
        room = max(0.0, cap / 100 * gross - held)
        chk("concentration", "OK" if w < cap else "BREACH", f"held {w:.1f}% of assets; room under the {cap:g}% cap "
                                                          f"Rs {room:,.0f}")
    else:
        room = None
        chk("concentration", "UNKNOWN", "no holdings recorded")
    eq = ctx.get("equity_drift")
    if eq is None:
        try:
            from wealth import rebalance as RB
            dr = {x["class"]: x for x in RB.check_public(conn, owner)["drift"]}
            eq = dr.get("EQUITY")
        except ValueError:
            eq = None
    if eq:
        res = "CAUTION" if eq["drift_pp"] > 0 and eq["out_of_band"] else "OK"
        chk("allocation", res, f"equity {eq['current_pct']}% vs target {eq['target_pct']}%"
                               + (" (already overweight: a buy adds to the drift)" if res == "CAUTION" else ""))
    verdict = "NOT_SUITABLE" if any(c["result"] == "BREACH" for c in checks) else \
        "CAUTION" if any(c["result"] in ("CAUTION", "UNKNOWN") for c in checks) else "SUITABLE"
    return {"symbol": sym, "atip": sc, "held_value": round(held, 2), "room_under_cap": round(room, 2) if room is not None
            else None, "checks": checks, "verdict": verdict,
            "note": "informational fit with your profile and plan; not a trading instruction"}


def todays_signals(conn, owner, limit=15) -> list:
    from wealth import dna
    from wealth import holdings as H
    r = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()
    if not r or not r[0]:
        return []
    d0 = r[0]
    ctx = {"dna": dna.summary(conn, owner), "summary": H.summary(conn, owner)}
    try:
        from wealth import rebalance as RB
        ctx["equity_drift"] = {x["class"]: x for x in RB.check_public(conn, owner)["drift"]}.get("EQUITY") or {}
    except ValueError:
        ctx["equity_drift"] = {}
    held = {p["symbol"] for p in ctx["summary"]["positions"] if p["symbol"]}
    rows = conn.execute("SELECT symbol, `signal`, atip_score FROM ai_scores WHERE date=? AND (`signal`='BUY' OR (symbol IN "
                        f"({','.join('?' * len(held)) or 'NULL'}) AND `signal` IN ('SELL','EXIT','AVOID'))) ORDER BY "
                        "atip_score DESC LIMIT ?", (d0, *held, int(limit))).fetchall()
    out = []
    for x in rows:
        s = signal_suitability(conn, owner, x["symbol"], ctx)
        out.append({"symbol": x["symbol"], "signal": x["signal"], "atip_score": x["atip_score"], "held": x["symbol"] in held,
                    "verdict": s["verdict"], "checks": s["checks"], "date": str(d0)[:10]})
    return out


# ── Overview ───────────────────────────────────────────────────────────────
def overview(conn, owner) -> dict:
    from wealth import advisor as ADV
    from wealth import dna
    from wealth import goals as G
    from wealth import holdings as H
    out = {"as_of": str(C.now()), "mode": get_mode(conn, owner), "dna": dna.summary(conn, owner)}
    s = H.summary(conn, owner)
    out["wealth"] = {"totals": s["totals"], "by_asset_class": {k: v["weight_pct"] for k, v in s["by_asset_class"].items()},
                     "data_quality_issues": len(s["data_quality"]), "holdings_at_risk": s["holdings_at_risk"]}
    out["goals"] = G.overview(conn, owner)
    try:
        from wealth import rebalance as RB
        chk = RB.check_public(conn, owner)
        out["allocation"] = {"target": chk["target"], "current": chk["current"], "verdict": chk["verdict"],
                             "triggers": chk["triggers"], "target_source": chk["target_source"]}
    except ValueError as e:
        out["allocation"] = {"verdict": "NO_PROFILE", "detail": str(e)}
    rep = ADV._latest_perf(conn, owner)
    out["performance"] = {"report_id": rep.get("report_id"), "portfolio": rep["portfolio"], "period": rep["period"],
                          "returns": {c["return"]: c["total_return_pct"] for c in rep["comparison"]}} if rep else None
    cl, rec, cav = ADV._t_overview(conn, owner)
    out["briefing"] = {"claims": cl, "recommendations": ADV._suitability(conn, owner, rec), "caveats": cav}
    out["signals"] = todays_signals(conn, owner)
    out["last_cycle"] = (cycles(conn, owner, 1) or [None])[0]
    out["disclaimer"] = C.DISCLAIMER
    return out


def status(conn, owner) -> dict:
    def age(q, args):
        r = conn.execute(q, args).fetchone()
        return str(r[0])[:19] if r and r[0] else None
    o = (owner["tenant_id"], owner["owner_id"])
    parts = {
        "investor_dna": age("SELECT updated_at FROM investor_profile WHERE tenant_id=? AND owner_id=?", o),
        "wealth_snapshot": age("SELECT MAX(date) FROM wealth_snapshot WHERE tenant_id=? AND owner_id=?", o),
        "goals": age("SELECT MAX(updated_at) FROM wealth_goal WHERE tenant_id=? AND owner_id=? AND status='ACTIVE'", o),
        "allocation_run": age("SELECT MAX(created_at) FROM wealth_allocation_run WHERE tenant_id=? AND owner_id=?", o),
        "rebalance_plan": age("SELECT MAX(created_at) FROM wealth_rebalance_plan WHERE tenant_id=? AND owner_id=?", o),
        "ledger_import": age("SELECT MAX(created_at) FROM perf_ledger WHERE tenant_id=? AND owner_id=?", o),
        "performance_report": age("SELECT MAX(created_at) FROM perf_report_run WHERE tenant_id=? AND owner_id=?", o),
        "investor_cycle": age("SELECT MAX(created_at) FROM wealth_cycle_run WHERE tenant_id=? AND owner_id=?", o),
    }
    market = {"prices_daily": age("SELECT MAX(date) FROM prices_daily WHERE symbol='NIFTY50'", ()),
              "ai_scores": age("SELECT MAX(date) FROM ai_scores", ()),
              "market_health": age("SELECT MAX(date) FROM market_health", ()),
              "global_markets": age("SELECT MAX(date) FROM global_markets", ()),
              "signal_log": age("SELECT MAX(signal_date) FROM signal_log", ())}
    missing = [k for k in ("investor_dna", "wealth_snapshot", "goals", "allocation_run") if not parts[k]]
    stale = [k for k, v in market.items() if not v or (date.today() - date.fromisoformat(v[:10])).days > 5]
    return {"owner": owner, "components": parts, "market_data": market, "scheduled_cycle": settings()["enabled"],
            "missing": missing, "stale_market_data": stale,
            "state": "READY" if not missing and not stale else "DEGRADED" if not missing else "INCOMPLETE",
            "next_step": ("answer the Investor DNA questionnaire" if not parts["investor_dna"] else
                          "add your goals" if not parts["goals"] else
                          "compute an allocation" if not parts["allocation_run"] else
                          "record a wealth snapshot" if not parts["wealth_snapshot"] else None)}
